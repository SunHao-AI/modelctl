#!/usr/bin/env python3
"""engines/unsloth.py — Unsloth 无头推理服务（unsloth studio run --api-only）适配器。

API key 说明：`unsloth studio run` 不支持指定 API key——每次加载模型时自动生成
一把 `sk-unsloth-…` key，并在启动日志中打印一行 `API Key: <key>`（该横幅在模型
加载完成后才出现）。因此健康检查 / 预热 / 网关转发需从启动日志解析这把运行时
key（见 upstream_api_key），profile.api_key 仅作兜底。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
import urllib.request
from pathlib import Path

from modelctl.core.capabilities import free_vram_total_mb
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.process import launch_log, wait_health
from modelctl.engines._persist import persist_model_path
from modelctl.engines.base import EngineAdapter, RequirementError
from modelctl.engines.llamacpp import download_gguf

# Unsloth 无头推理固定参数：`studio` 是命令组，run 子命令承载模型/网络 flag；
# 未知 flag 会透传给底层 llama-server（GGUF）。run 明确拒绝 --api-key 等认证类
# flag（Unsloth 自管认证），故此处不传任何 API key 参数。
UNSLOTH_BIN = "unsloth"
STUDIO_RUN_ARGS = ["studio", "run", "--api-only"]

# 启动日志中的运行时 API key 行（横幅与静默模式两种格式均可匹配）。
_API_KEY_RE = re.compile(r"^\s*API Key:\s*(\S+)", re.MULTILINE)


def _runtime_api_key_from_log(name: str) -> str | None:
    """从启动日志尾部解析最近一次加载打印的 API key；未找到返回 None。"""
    log_path = launch_log(name)
    if log_path is None:
        return None
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-65536:]
    except OSError:
        return None
    matches = _API_KEY_RE.findall(tail)
    return matches[-1] if matches else None


class UnslothAdapter(EngineAdapter):
    def check_requirements(self) -> None:
        if not self.caps.binaries.get("unsloth"):
            raise RequirementError("未安装 unsloth（PATH 中找不到 unsloth 命令）")
        cfg = self.profile.engine_config
        if not cfg.get("model") and not cfg.get("download"):
            raise RequirementError(f"{self.profile.name}：unsloth.model 必填（或配置 download 段自动下载）")
        # API key 由 unsloth 运行时自动生成并打印到启动日志，无需在 profile 配置。
        if cfg.get("tensor_parallel") and self.caps.gpu_count < 2:
            raise RequirementError(f"tensor_parallel 需要至少 2 块 GPU，当前 {self.caps.gpu_count}")
        self._check_vram(cfg)
        # 用量统计降级提示：无头 API 模式的 /metrics 端点尚未验证
        self.warnings.append("unsloth 引擎暂未验证 /metrics 端点，用量统计降级为'不支持精确统计'")

    def _check_vram(self, cfg: dict) -> None:
        """GGUF 本地文件存在时按文件大小做显存预检。"""
        model = str(cfg.get("model") or "")
        if not model:
            return
        p = Path(model).expanduser()
        if not p.is_file():
            return
        need_mb = p.stat().st_size / 1024 / 1024 * 1.1
        free_mb = free_vram_total_mb(self.caps)
        if need_mb > free_mb:
            raise RequirementError(f"剩余显存不足：模型约需 {need_mb:.0f}MB（×1.1），剩余 {free_mb}MB")

    def _model_ref(self, cfg: dict) -> str:
        """构造 --model 参数：本地路径规范化后原样；HF ID 追加 :<gguf_variant>。"""
        model = str(cfg["model"])
        p = Path(model).expanduser()
        if p.is_file() or p.is_dir():
            return str(p)
        variant = cfg.get("gguf_variant")
        return f"{model}:{variant}" if variant else model

    def pre_start(self) -> None:
        cfg = self.profile.engine_config
        model = str(cfg.get("model") or "")
        if model and (Path(model).expanduser().is_file() or Path(model).expanduser().is_dir()):
            return
        if not cfg.get("download"):
            return
        dl = cfg["download"]
        model_root = Path(os.environ.get("MODEL_ROOT") or PROJECT_ROOT.parent / "model-gguf")
        try:
            model_match, _draft = download_gguf(dl["modelscope_id"], model_root, dl.get("quant", "UD-Q8_K_XL"), want_dspark=False)
        except RequirementError as error:
            raise RequirementError(
                f"{self.profile.name}：ModelScope 下载失败。\n{error}\n" "可配置 HF_ENDPOINT=https://hf-mirror.com 后从 Hugging Face 手动下载 " "unsloth GGUF 仓库，并将本地路径填入 unsloth.model。"
            ) from error
        assert self.profile.path is not None  # 加载的 profile 必有真实文件路径
        persist_model_path(self.profile.path, "unsloth", str(model_match.resolve()))
        cfg["model"] = str(model_match.resolve())

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        cmd = [UNSLOTH_BIN, *STUDIO_RUN_ARGS, "-H", "0.0.0.0", "-p", str(self.profile.port)]
        cmd += ["--model", self._model_ref(cfg)]
        if cfg.get("context_length"):
            cmd += ["--context-length", str(cfg["context_length"])]
        if cfg.get("tensor_parallel"):
            cmd += ["--tensor-parallel"]
        if cfg.get("load_in_4bit"):
            cmd += ["--load-in-4bit"]
        # 不传 --api-key：run 自管认证，key 自动生成后打印到启动日志。
        if cfg.get("extra_args"):
            cmd += shlex.split(str(cfg["extra_args"]))
        env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
        return cmd, env

    def health_url(self) -> str:
        return f"http://127.0.0.1:{self.profile.port}/v1/models"

    def metrics_mapping(self) -> None:
        return None

    def upstream_model_name(self) -> str:
        return str(self.profile.engine_config.get("model") or self.profile.name)

    def upstream_api_key(self) -> str | None:
        """优先返回启动日志中运行时生成的 key；未找到时兜底 profile.api_key。"""
        return _runtime_api_key_from_log(self.profile.name) or self.profile.api_key

    def wait_ready(self, timeout: float) -> bool:
        """先等启动日志出现 API Key 行（即模型加载完成），再用该 key 探测 /v1/models。"""
        deadline = time.time() + timeout
        while True:
            key = _runtime_api_key_from_log(self.profile.name)
            if key:
                remaining = max(deadline - time.time(), 2.0)
                return wait_health(self.health_url(), remaining, key)
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            time.sleep(min(2.0, remaining))

    def post_start(self) -> None:
        """预热：向 OpenAI 兼容端点发一个最小请求，降低首个请求冷启动延迟；失败不阻塞启动。"""
        api_key = self.upstream_api_key()
        body = json.dumps({"model": "default", "messages": [{"role": "user", "content": "ping"}]}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.profile.port}/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
            },
        )
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except OSError:
            pass  # 预热失败不影响启动结果

    def stop_patterns(self) -> list[str]:
        return ["unsloth"]
