#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/engines/tokenspeed.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/1 10:00
# @Desc   : TokenSpeed 引擎适配器
# ===============================================================================

"""engines/tokenspeed.py — TokenSpeed 适配器。"""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

from modelctl.core import envs
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.gpu_lock import acquire_gpu_lock
from modelctl.core.gpu_utils import GPUValidationError
from modelctl.engines._download import download_repo
from modelctl.engines._persist import persist_model_path
from modelctl.engines.base import EngineAdapter, RequirementError


class TokenSpeedAdapter(EngineAdapter):
    def _resolve_runtime(self) -> tuple[str, str | None]:
        cfg = self.profile.engine_config
        image = str(cfg.get("docker_image") or "").strip()
        return ("docker", image) if image else ("venv", None)

    def check_requirements(self) -> None:
        cfg = self.profile.engine_config
        runtime, image = self._resolve_runtime()
        if runtime == "docker":
            if shutil.which("docker") is None:
                raise RequirementError("docker 命令不在 PATH")
            if shutil.which("nvidia-smi") is None:
                raise RequirementError("docker 模式需要 nvidia-container-toolkit")
        else:
            envs.ensure_env("tokenspeed")
        if not cfg.get("model") and not cfg.get("download"):
            raise RequirementError(f"{self.profile.name}：tokenspeed.model 必填（或配置 download 段自动下载）")
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
        cfg = self.profile.engine_config
        model = str(cfg.get("model") or "")
        if not (model and Path(model).expanduser().is_dir()):
            if cfg.get("download"):
                modelscope_id = cfg["download"]["modelscope_id"]
                model_root = Path(os.environ.get("MODEL_ROOT") or PROJECT_ROOT.parent / "model-hf")
                local_dir = download_repo(modelscope_id, model_root)
                if self.profile.path is None:
                    raise RequirementError(f"{self.profile.name}：profile 文件路径缺失")
                persist_model_path(self.profile.path, "tokenspeed", str(local_dir.resolve()))
                cfg["model"] = str(local_dir.resolve())

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        gpus = self.selected_gpus()
        tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
        runtime, image = self._resolve_runtime()
        extra = shlex.split(str(cfg.get("extra_args") or ""))
        model = str(cfg["model"])

        if runtime == "docker":
            model_local = Path(model).expanduser().resolve()
            cmd = [
                "docker", "run", "--rm", "--detach",
                "--name", f"{self.profile.name}-tokenspeed",
                "--gpus", self._gpus_json(gpus, tp),
                "-p", f"{self.profile.port}:8000",
                "-v", f"{model_local.parent.as_posix()}:/models:ro",
                "--ipc=host",
                image,
                "serve", f"/models/{model_local.name}",
                "--host", "0.0.0.0",
                "--port", "8000",
                "--tp", str(tp),
            ]
            if cfg.get("max_model_len"):
                cmd += ["--max-model-len", str(cfg["max_model_len"])]
            cmd += extra
            env = {}
            if gpus:
                env.update(self.cuda_visible_devices(gpus))
            return cmd, env

        cmd = [
            str(envs.engine_bin("tokenspeed", "tokenspeed")),
            "serve",
            model,
            "--host", "0.0.0.0",
            "--port", str(self.profile.port),
            "--tp", str(tp),
        ]
        if cfg.get("max_model_len"):
            cmd += ["--max-model-len", str(cfg["max_model_len"])]
        cmd += self.api_key_args() + extra
        env = {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "tokenspeed")
        env["PATH"] = str(envs.engine_bin("tokenspeed", "tokenspeed").parent) + os.pathsep + os.environ.get("PATH", "")
        return cmd, env

    def metrics_mapping(self) -> dict[str, list[str]]:
        return {
            "prompt_total": ["tokenspeed:prompt_tokens_total"],
            "predicted_total": ["tokenspeed:generation_tokens_total"],
        }

    def _gpus_json(self, gpus, tp) -> str:
        if gpus:
            seq = list(gpus)
        else:
            seq = list(range(int(self.caps.gpu_count or tp)))
        return '"device=' + ",".join(str(g) for g in seq) + '"'

    def wait_ready(self, timeout: float) -> bool:
        from modelctl.core.process import wait_health
        if self._resolve_runtime()[0] == "docker":
            return wait_health(self.health_url(), timeout, self.upstream_api_key(), alive_check=None)
        return super().wait_ready(timeout)

    def stop_patterns(self) -> list[str]:
        if self._resolve_runtime()[0] != "docker":
            return ["tokenspeed serve"]
        name = f"{self.profile.name}-tokenspeed"
        return [f"docker run --rm --detach --name {name}"]
