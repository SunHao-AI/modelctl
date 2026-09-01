#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/engines/aphrodite.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/1 10:00
# @Desc   : Aphrodite 引擎适配器
# ===============================================================================

"""engines/aphrodite.py — Aphrodite 适配器。"""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

from modelctl.core import envs
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.gpu_lock import acquire_gpu_lock
from modelctl.core.gpu_utils import GPUValidationError
from modelctl.engines.base import EngineAdapter, RequirementError


class AphroditeAdapter(EngineAdapter):
    def check_requirements(self) -> None:
        envs.ensure_env("aphrodite")
        cfg = self.profile.engine_config
        if not cfg.get("model"):
            raise RequirementError(f"{self.profile.name}：aphrodite.model 必填")
        try:
            gpus = self.selected_gpus()
        except (GPUValidationError, ValueError) as exc:
            raise RequirementError(f"[gpu_list] {exc}") from exc
        if gpus is not None:
            self.validate_gpu_selection(gpus)
            tp = int(cfg.get("tensor_parallel_size", len(gpus)))
            if tp != len(gpus):
                raise RequirementError(
                    f"gpu_list 指定了 {len(gpus)} 块 GPU，但 tensor_parallel_size={tp}"
                )
        else:
            tp = int(cfg.get("tensor_parallel_size", 1))
            if self.caps.gpu_count and tp > self.caps.gpu_count:
                raise RequirementError(f"tensor_parallel_size={tp} 超过实际 GPU 数")
        self.run_compat_checks()
        if gpus is not None:
            acquire_gpu_lock(self.profile.name, gpus)

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        gpus = self.selected_gpus()
        tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
        extra = shlex.split(str(cfg.get("extra_args") or ""))
        cmd = [
            str(envs.engine_bin("aphrodite", "aphrodite")),
            "run",
            str(cfg["model"]),
            "--host", "0.0.0.0",
            "--port", str(self.profile.port),
            "--tensor-parallel-size", str(tp),
            "--served-model-name", self.upstream_model_name(),
        ]
        if cfg.get("quantization"):
            cmd += ["--quantization", str(cfg["quantization"])]
        if cfg.get("max_model_len"):
            cmd += ["--max-model-len", str(cfg["max_model_len"])]
        cmd += self.api_key_args() + extra
        env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "aphrodite")
        env["PATH"] = str(envs.engine_bin("aphrodite", "aphrodite").parent) + os.pathsep + os.environ.get("PATH", "")
        return cmd, env

    def metrics_mapping(self) -> dict[str, list[str]]:
        return {
            "prompt_total": ["aphrodite:prompt_tokens_total"],
            "predicted_total": ["aphrodite:generation_tokens_total"],
            "prompt_rate": ["aphrodite:avg_prompt_throughput_toks_per_sec"],
            "predicted_rate": ["aphrodite:avg_generation_throughput_toks_per_sec"],
        }

    def upstream_model_name(self) -> str:
        return self.profile.name
