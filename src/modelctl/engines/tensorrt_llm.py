#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/engines/tensorrt_llm.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/1 10:00
# @Desc   : TensorRT-LLM 引擎适配器
# ===============================================================================

"""engines/tensorrt_llm.py — TensorRT-LLM 适配器。"""

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


class TensorRtLlmAdapter(EngineAdapter):
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
            envs.ensure_env("tensorrt_llm")
        if not cfg.get("model"):
            raise RequirementError(f"{self.profile.name}：tensorrt_llm.model 必填")
        if not cfg.get("engine_dir"):
            raise RequirementError(f"{self.profile.name}：tensorrt_llm.engine_dir 必填（编译产物缓存目录）")
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
        engine_dir = Path(str(cfg.get("engine_dir") or "")).expanduser()
        if engine_dir.exists() and any(engine_dir.iterdir()):
            return
        # 编译产物缺失：记录警告，由用户手动触发编译（避免首次 28min 阻塞在 modelctl start）
        self.warnings.append(
            f"TensorRT-LLM engine_dir {engine_dir} 不存在或为空，"
            "请先执行 trtllm-build 编译或配置 docker_image 使用预编译镜像"
        )

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        gpus = self.selected_gpus()
        tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
        runtime, image = self._resolve_runtime()
        extra = shlex.split(str(cfg.get("extra_args") or ""))
        model = str(cfg["model"])
        engine_dir = str(Path(str(cfg["engine_dir"])).expanduser())

        if runtime == "docker":
            model_local = Path(model).expanduser().resolve()
            engine_local = Path(engine_dir).expanduser().resolve()
            cmd = [
                "docker", "run", "--rm", "--detach",
                "--name", f"{self.profile.name}-trtllm",
                "--gpus", self._gpus_json(gpus, tp),
                "-p", f"{self.profile.port}:8000",
                "-v", f"{model_local.parent.as_posix()}:/models:ro",
                "-v", f"{engine_local.as_posix()}:/engines:ro",
                "--ipc=host",
                image,
                "serve", f"/models/{model_local.name}",
                "--engine_dir", "/engines",
                "--host", "0.0.0.0",
                "--port", "8000",
                "--tp", str(tp),
            ] + extra
            env = {}
            if gpus:
                env.update(self.cuda_visible_devices(gpus))
            return cmd, env

        cmd = [
            str(envs.engine_python("tensorrt_llm")),
            "-m", "tensorrt_llm.serve",
            model,
            "--engine_dir", engine_dir,
            "--host", "0.0.0.0",
            "--port", str(self.profile.port),
            "--tp", str(tp),
        ]
        if cfg.get("quantization"):
            cmd += ["--quantization", str(cfg["quantization"])]
        if cfg.get("max_input_len"):
            cmd += ["--max_input_len", str(cfg["max_input_len"])]
        if cfg.get("max_output_len"):
            cmd += ["--max_output_len", str(cfg["max_output_len"])]
        if cfg.get("max_batch_size"):
            cmd += ["--max_batch_size", str(cfg["max_batch_size"])]
        cmd += self.api_key_args() + extra
        env = {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "tensorrt_llm")
        env["PATH"] = str(envs.engine_python("tensorrt_llm").parent) + os.pathsep + os.environ.get("PATH", "")
        return cmd, env

    def metrics_mapping(self) -> dict[str, list[str]]:
        return {
            "prompt_total": ["nv_inference_request_success", "trtllm:prompt_tokens_total"],
            "predicted_total": ["trtllm:generation_tokens_total"],
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
            return ["tensorrt_llm.serve"]
        name = f"{self.profile.name}-trtllm"
        return [f"docker run --rm --detach --name {name}"]
