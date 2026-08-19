#!/usr/bin/env python3
"""engines/sglang.py — SGLang 适配器。"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from modelctl.core.envfile import PROJECT_ROOT
from modelctl.engines._download import download_repo
from modelctl.engines._persist import persist_model_path
from modelctl.engines.base import EngineAdapter, RequirementError


class SglangAdapter(EngineAdapter):
    def check_requirements(self) -> None:
        if not self.caps.binaries.get("sglang"):
            raise RequirementError("未安装 sglang（PATH 中找不到 sglang 命令）")
        cfg = self.profile.engine_config
        if not cfg.get("model") and not cfg.get("download"):
            raise RequirementError(f"{self.profile.name}：sglang.model 必填（或配置 download 段自动下载）")
        tp = int(cfg.get("tensor_parallel_size", 1))
        if self.caps.gpu_count and tp > self.caps.gpu_count:
            raise RequirementError(f"tensor_parallel_size={tp} 超过实际 GPU 数 {self.caps.gpu_count}")

    def pre_start(self) -> None:
        cfg = self.profile.engine_config
        model = str(cfg.get("model") or "")
        if model and (Path(model).expanduser().is_dir() or Path(model).expanduser().is_file()):
            return
        if cfg.get("download"):
            modelscope_id = cfg["download"]["modelscope_id"]
            model_root = Path(os.environ.get("MODEL_ROOT") or PROJECT_ROOT.parent / "model-hf")
            local_dir = download_repo(modelscope_id, model_root)
            persist_model_path(self.profile.path, "sglang", str(local_dir.resolve()))
            cfg["model"] = str(local_dir.resolve())

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        cmd = [
            sys.executable,
            "-m",
            "sglang.launch_server",
            "--model-path",
            str(cfg["model"]),
            "--host",
            "0.0.0.0",
            "--port",
            str(self.profile.port),
            "--tp",
            str(cfg.get("tensor_parallel_size", 1)),
        ]
        if cfg.get("context_length"):
            cmd += ["--context-length", str(cfg["context_length"])]
        if cfg.get("mem_fraction_static"):
            cmd += ["--mem-fraction-static", str(cfg["mem_fraction_static"])]
        if cfg.get("extra_args"):
            cmd += shlex.split(str(cfg["extra_args"]))
        env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
        return cmd, env

    def metrics_mapping(self) -> dict[str, list[str]]:
        return {
            "prompt_total": ["sglang:prompt_tokens_total"],
            "predicted_total": ["sglang:generation_tokens_total"],
            "prompt_rate": [],
            "predicted_rate": [],
        }

    def upstream_model_name(self) -> str:
        return str(self.profile.engine_config.get("model") or self.profile.name)

    def stop_patterns(self) -> list[str]:
        return ["sglang"]
