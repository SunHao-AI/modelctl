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
import subprocess
from pathlib import Path

from modelctl.core import docker_setup, envs
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.gpu_lock import acquire_gpu_lock
from modelctl.core.gpu_utils import GPUValidationError
from modelctl.core.process import docker_container_alive, wait_health
from modelctl.engines._download import download_repo
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
            missing = docker_setup.path_level_missing()
            if missing:
                raise RequirementError(f"docker_image 已配置但 Docker 环境未就绪：{'；'.join(missing)}——{docker_setup.MSG_GUIDE}")
            # 清冲突残留容器（幂等）
            try:
                subprocess.run(["docker", "rm", "-f", f"{self.profile.name}-tokenspeed"],
                               capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
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
                # 落地路径由 MODEL_ROOT + modelscope_id 确定性推导，目录已存在即复用；
                # 仅更新内存中的 cfg，不写回 YAML（保持 profile 文件干净、多机可移植）。
                local_dir = download_repo(modelscope_id, model_root)
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
                "--name", self._container_name,
                "--gpus", self._gpus_json(gpus, tp),
                "-p", f"{self.profile.port}:8000",
                "-v", f"{model_local.parent.as_posix()}:/models:ro",
                "--ipc=host",
                # 容器时区只能靠 -e（start_detached 的 env 进不了容器），否则日志是 UTC
                *self.docker_timezone_args(),
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
        # §2.2 速率 gauge 补全：参照 vllm/sglang 风格暴露 avg_*_throughput gauge。
        # 缺失/恒为 0 时 stats 退化为窗口差分（不会出错）。
        return {
            "prompt_total": ["tokenspeed:prompt_tokens_total"],
            "predicted_total": ["tokenspeed:generation_tokens_total"],
            "prompt_rate": [
                "tokenspeed:prompt_tokens_seconds",
                "tokenspeed:avg_prompt_throughput_toks_per_sec",
            ],
            "predicted_rate": [
                "tokenspeed:generation_tokens_seconds",
                "tokenspeed:avg_generation_throughput_toks_per_sec",
            ],
        }

    def _gpus_json(self, gpus, tp) -> str:
        if gpus:
            seq = list(gpus)
        else:
            seq = list(range(int(self.caps.gpu_count or tp)))
        return '"device=' + ",".join(str(g) for g in seq) + '"'

    @property
    def _container_name(self) -> str:
        return f"{self.profile.name}-tokenspeed"

    def wait_ready(self, timeout: float) -> bool:
        if self._resolve_runtime()[0] == "docker":
            return wait_health(
                self.health_url(),
                timeout,
                self.upstream_api_key(),
                alive_check=lambda: docker_container_alive(self._container_name),
            )
        return super().wait_ready(timeout)

    def backend_dead(self) -> bool:
        """docker 分支：后端死亡 = 容器已退出/不存在（客户端进程早退不等于容器死亡）。"""
        if self._resolve_runtime()[0] == "docker":
            return not docker_container_alive(self._container_name)
        return super().backend_dead()

    def stop_patterns(self) -> list[str]:
        if self._resolve_runtime()[0] != "docker":
            return ["tokenspeed serve"]
        name = self._container_name
        return [f"docker run --rm --detach --name {name}"]

    def is_docker_runtime(self) -> bool:
        """tokenspeed 路径判定：docker_image 字段非空时走 docker runtime。"""
        return self._resolve_runtime()[0] == "docker"

    def stop_backend(self) -> None:
        """docker 分支：docker rm -f <container>；venv 分支：基类 stop_instance。"""
        if self._resolve_runtime()[0] == "docker":
            from modelctl.core.process import stop_docker_instance
            stop_docker_instance(self.profile.name, self._container_name)
        else:
            super().stop_backend()
