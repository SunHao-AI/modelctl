#!/usr/bin/env python3
"""engines/ollama.py — ollama 适配器（serve 常驻 + 模型按需加载/卸载）。"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request

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
        return ["ollama", "serve"], env

    def check_requirements(self) -> None:
        if not self.caps.binaries.get("ollama"):
            raise RequirementError("未安装 ollama（PATH 中找不到 ollama 命令）")
        if not self.profile.engine_config.get("model"):
            raise RequirementError(f"{self.profile.name}：ollama.model 必填（如 qwen3:32b）")

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
