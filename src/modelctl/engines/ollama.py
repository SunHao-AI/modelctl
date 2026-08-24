#!/usr/bin/env python3
"""engines/ollama.py — ollama 适配器（serve 常驻 + 模型按需加载/卸载）。"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request

from modelctl.core.gpu_utils import GPUValidationError
from modelctl.engines.base import EngineAdapter, RequirementError


class OllamaAdapter(EngineAdapter):
    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        env = {"OLLAMA_HOST": f"0.0.0.0:{self.profile.port}"}
        if os.environ.get("OLLAMA_MODELS"):
            env["OLLAMA_MODELS"] = os.environ["OLLAMA_MODELS"]
        env["OLLAMA_NUM_PARALLEL"] = str(cfg.get("num_parallel", 2))
        if cfg.get("context_length"):
            env["OLLAMA_CONTEXT_LENGTH"] = str(cfg["context_length"])
        gpus = self.selected_gpus()
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        return ["ollama", "serve"], env

    def check_requirements(self) -> None:
        if not self.caps.binaries.get("ollama"):
            raise RequirementError("未安装 ollama（PATH 中找不到 ollama 命令）")
        if not self.profile.engine_config.get("model"):
            raise RequirementError(f"{self.profile.name}：ollama.model 必填（如 qwen3:32b）")
        # 附录 B.5：适配器按 profile 的 port 设置 OLLAMA_HOST（build_command 内），
        # 因此**支持**每个 ollama profile 用独立端口启动各自 serve 并配合 gpu_list 隔离 GPU。
        # 注意：现有 ollama/*.yaml 均使用 11434（共享 serve 语义），stop 时仅卸载模型不杀进程
        #（见 all_service.stop_profile 的 ollama 特判）；如需 per-profile 隔离，请给 profile 配不同端口。
        try:
            gpus = self.selected_gpus()
        except (GPUValidationError, ValueError) as exc:
            raise RequirementError(f"[gpu_list] {exc}") from exc
        if gpus is not None:
            self.validate_gpu_selection(gpus)
        self.run_compat_checks()  # 预检：软件规则 + 模型 id 特征

    def health_url(self) -> str:
        return f"http://127.0.0.1:{self.profile.port}/"

    def pre_start(self) -> None:
        model = str(self.profile.engine_config["model"])
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True)
        installed = {line.split()[0] for line in out.stdout.splitlines() if line.strip()}
        if model not in installed:
            try:
                subprocess.run(["ollama", "pull", model], capture_output=True, text=True, check=True)
            except subprocess.CalledProcessError as e:
                detail = (e.stderr or "").strip() or str(e)
                raise RequirementError(
                    f"ollama pull {model} 失败：{detail}。"
                    f"请确认模型名/tag 存在（可运行 `ollama search {model.split(':')[0]}` 查询），"
                    "或检查网络与镜像配置（如 OLLAMA_MODELS / ollama 镜像地址）。"
                ) from e

    def post_start(self) -> None:
        self._call_generate(self.profile.engine_config.get("keep_alive", -1))

    def unload_model(self) -> None:
        """stop 时卸载模型而非杀 serve（多模型共享服务）。"""
        try:
            self._call_generate(0)
        except OSError:
            pass

    def _call_generate(self, keep_alive) -> None:
        body = json.dumps({"model": self.profile.engine_config["model"], "keep_alive": keep_alive}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.profile.port}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=600).read()

    def metrics_mapping(self) -> None:
        return None

    def upstream_model_name(self) -> str:
        # ollama 严格校验 body.model，必须改写为 ollama.model（如 qwen3.8:27b）
        return str(self.profile.engine_config["model"])
