#!/usr/bin/env python3
"""engines/unsloth.py — Unsloth 无头服务（unsloth studio --api-only）适配器。"""

from __future__ import annotations

import json
import os
import shlex
import urllib.request
from pathlib import Path

from modelctl.core.capabilities import free_vram_total_mb
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.engines._persist import persist_model_path
from modelctl.engines.base import EngineAdapter, RequirementError
from modelctl.engines.llamacpp import download_gguf

# Unsloth 无头服务固定参数。
# 注意：具体 flag 需在目标机器上以 `unsloth --help` / `unsloth start --no-launch`
# 实测确认；如与文档不一致，仅需调整本文件常量，不影响其他引擎。
UNSLOTH_BIN = "unsloth"
STUDIO_ARGS = ["studio", "--api-only", "-H", "0.0.0.0"]


class UnslothAdapter(EngineAdapter):
    def check_requirements(self) -> None:
        if not self.caps.binaries.get("unsloth"):
            raise RequirementError("未安装 unsloth（PATH 中找不到 unsloth 命令）")
        cfg = self.profile.engine_config
        if not cfg.get("model") and not cfg.get("download"):
            raise RequirementError(f"{self.profile.name}：unsloth.model 必填（或配置 download 段自动下载）")
        if not self.profile.api_key:
            raise RequirementError(
                f"{self.profile.name}：unsloth 引擎必须配置 api_key（健康检查 /v1/models 依赖 Bearer 认证）"
            )
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
            model_match, _draft = download_gguf(
                dl["modelscope_id"], model_root, dl.get("quant", "UD-Q8_K_XL"), want_dspark=False
            )
        except RequirementError as error:
            raise RequirementError(
                f"{self.profile.name}：ModelScope 下载失败。\n{error}\n"
                "可配置 HF_ENDPOINT=https://hf-mirror.com 后从 Hugging Face 手动下载 "
                "unsloth GGUF 仓库，并将本地路径填入 unsloth.model。"
            ) from error
        assert self.profile.path is not None  # 加载的 profile 必有真实文件路径
        persist_model_path(self.profile.path, "unsloth", str(model_match.resolve()))
        cfg["model"] = str(model_match.resolve())

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        cmd = [UNSLOTH_BIN, *STUDIO_ARGS, "-p", str(self.profile.port)]
        cmd += ["--model", self._model_ref(cfg)]
        if cfg.get("context_length"):
            cmd += ["--context-length", str(cfg["context_length"])]
        if cfg.get("tensor_parallel"):
            cmd += ["--tensor-parallel"]
        if cfg.get("load_in_4bit"):
            cmd += ["--load-in-4bit"]
        cmd += self.api_key_args()
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

    def post_start(self) -> None:
        """预热：向 OpenAI 兼容端点发一个最小请求，降低首个请求冷启动延迟；失败不阻塞启动。"""
        body = json.dumps({"model": "default", "messages": [{"role": "user", "content": "ping"}]}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.profile.port}/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.profile.api_key}",
            },
        )
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except OSError:
            pass  # 预热失败不影响启动结果

    def stop_patterns(self) -> list[str]:
        return ["unsloth"]
