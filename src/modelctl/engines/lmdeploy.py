#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/engines/lmdeploy.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/1 10:00
# @Desc   : LMDeploy 引擎适配器
# ===============================================================================

"""engines/lmdeploy.py — LMDeploy 适配器。"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from modelctl.core import envs
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.gpu_lock import acquire_gpu_lock
from modelctl.core.gpu_utils import GPUValidationError
from modelctl.engines._download import download_repo
from modelctl.engines._persist import persist_model_path
from modelctl.engines.base import EngineAdapter, RequirementError


class LmdeployAdapter(EngineAdapter):
    def check_requirements(self) -> None:
        envs.ensure_env("lmdeploy")
        cfg = self.profile.engine_config
        if not cfg.get("model"):
            raise RequirementError(f"{self.profile.name}：lmdeploy.model 必填")
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

    def pre_start(self) -> None:
        """§2.2 下载接入：与 tokenspeed 同套机制（modelscope_id + 幂等目录）。"""
        cfg = self.profile.engine_config
        model = str(cfg.get("model") or "")
        if not (model and Path(model).expanduser().is_dir()):
            if cfg.get("download"):
                modelscope_id = cfg["download"]["modelscope_id"]
                model_root = Path(os.environ.get("MODEL_ROOT") or PROJECT_ROOT.parent / "model-hf")
                local_dir = download_repo(modelscope_id, model_root)
                if self.profile.path is None:
                    raise RequirementError(f"{self.profile.name}：profile 文件路径缺失")
                persist_model_path(self.profile.path, "lmdeploy", str(local_dir.resolve()))
                cfg["model"] = str(local_dir.resolve())

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        gpus = self.selected_gpus()
        tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
        extra = shlex.split(str(cfg.get("extra_args") or ""))
        cmd = [
            str(envs.engine_bin("lmdeploy", "lmdeploy")),
            "serve", "api_server",
            str(cfg["model"]),
            "--server-name", "0.0.0.0",
            "--server-port", str(self.profile.port),
            "--tp", str(tp),
        ]
        if cfg.get("session_len"):
            cmd += ["--session-len", str(cfg["session_len"])]
        if cfg.get("cache_max_entry_count") is not None:
            cmd += ["--cache-max-entry-count", str(cfg["cache_max_entry_count"])]
        if cfg.get("quant_policy"):
            cmd += ["--quant-policy", str(cfg["quant_policy"])]
        if self.profile.api_key:
            cmd += ["--api-keys", self.profile.api_key]
        cmd += ["--model-name", self.upstream_model_name()]
        cmd += extra
        env = {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "lmdeploy")
        env["PATH"] = str(envs.engine_bin("lmdeploy", "lmdeploy").parent) + os.pathsep + os.environ.get("PATH", "")
        return cmd, env

    def metrics_mapping(self) -> dict[str, list[str]]:
        return {
            "prompt_total": ["lmdeploy:prompt_tokens_total"],
            "predicted_total": ["lmdeploy:generation_tokens_total"],
            "prompt_rate": ["lmdeploy:avg_prompt_throughput_toks_per_sec"],
            "predicted_rate": ["lmdeploy:avg_generation_throughput_toks_per_sec"],
        }
