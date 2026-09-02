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
import subprocess
from pathlib import Path

from modelctl.core import envs
from modelctl.core.gpu_lock import acquire_gpu_lock
from modelctl.core.gpu_utils import GPUValidationError
from modelctl.core.process import docker_container_alive, wait_health
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
            # 清冲突残留容器（幂等）
            try:
                subprocess.run(["docker", "rm", "-f", f"{self.profile.name}-trtllm"],
                               capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
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
        # §2.2：可执行 `modelctl trtllm build <name>` 触发 build_command 编译
        self.warnings.append(
            f"TensorRT-LLM engine_dir {engine_dir} 不存在或为空，"
            "请先执行 trtllm-build 编译或配置 docker_image 使用预编译镜像"
            "（或 `modelctl trtllm build <profile-name>` 自动编译）"
        )

    def build_compile_command(self) -> tuple[list[str], dict[str, str]]:
        """§2.2：trtllm-build 编译命令（CLI `modelctl trtllm build` 调用）。

        使用 venv 内的 trtllm-build 可执行文件编译 HuggingFace 模型到 engine_dir；
        docker 模式不支持自动编译（trtllm-build 需 host 端 GPU 与 Python 运行时）。
        编译完成前 modelctl start 会因 engine_dir 空而报错。
        """
        cfg = self.profile.engine_config
        gpus = self.selected_gpus()
        tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
        runtime, _image = self._resolve_runtime()
        if runtime == "docker":
            raise RequirementError(
                "trtllm build 仅在 venv 模式下支持（docker 模式请使用预编译镜像或 host 手动执行 trtllm-build）"
            )
        model = str(cfg["model"])
        engine_dir = Path(str(cfg.get("engine_dir") or "")).expanduser()
        engine_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            "trtllm-build",
            f"--model_dir={model}",
            f"--workspace_dir={engine_dir}",
            f"--tensor_parallelism_size={tp}",
        ]
        for key, flag in (
            ("quantization", "--quantization"),
            ("max_input_len", "--max_input_len"),
            ("max_output_len", "--max_output_len"),
            ("max_batch_size", "--max_batch_size"),
        ):
            if cfg.get(key):
                cmd.append(f"{flag}={cfg[key]}")
        if cfg.get("dtype"):
            cmd.append(f"--dtype={cfg['dtype']}")
        cmd += shlex.split(str(cfg.get("extra_args") or ""))
        env = {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "tensorrt_llm")
        env["PATH"] = str(envs.VENV_ROOT / "tensorrt_llm" / "bin") + os.pathsep + os.environ.get("PATH", "")
        return cmd, env

    def ensure_bin(self) -> None:
        """校验托管 venv 已安装（modelctl trtllm build 前置）。"""
        cfg = self.profile.engine_config
        runtime, _image = self._resolve_runtime()
        if runtime == "docker":
            raise RequirementError("trtllm build 仅在 venv 模式下支持")
        envs.ensure_env("tensorrt_llm")
        if not cfg.get("model"):
            raise RequirementError(f"{self.profile.name}：tensorrt_llm.model 必填")
        if not cfg.get("engine_dir"):
            raise RequirementError(f"{self.profile.name}：tensorrt_llm.engine_dir 必填（编译产物缓存目录）")

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
                "--name", self._container_name,
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
        cmd += extra
        env = {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "tensorrt_llm")
        env["PATH"] = str(envs.engine_python("tensorrt_llm").parent) + os.pathsep + os.environ.get("PATH", "")
        return cmd, env

    def metrics_mapping(self) -> dict[str, list[str]]:
        # §2.2 速率 gauge 补全：参照 vllm/sglang 风格暴露 tokens/sec gauge。
        # TensorRT-LLM 0.11+ 提供 *_tokens_per_second 增量 gauge（老版本缺则退化为窗口差分）。
        return {
            "prompt_total": ["trtllm:prompt_tokens_total"],
            "predicted_total": ["trtllm:generation_tokens_total"],
            "prompt_rate": [
                "trtllm:prompt_tokens_per_second",
                "trtllm:avg_prompt_throughput_tokens_per_second",
            ],
            "predicted_rate": [
                "trtllm:generation_tokens_per_second",
                "trtllm:avg_generation_throughput_tokens_per_second",
            ],
        }

    def _gpus_json(self, gpus, tp) -> str:
        if gpus:
            seq = list(gpus)
        else:
            seq = list(range(int(self.caps.gpu_count or tp)))
        return '"device=' + ",".join(str(g) for g in seq) + '"'

    def _container_name(self) -> str:
        return f"{self.profile.name}-trtllm"

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
            return ["tensorrt_llm.serve"]
        name = self._container_name
        return [f"docker run --rm --detach --name {name}"]

    def is_docker_runtime(self) -> bool:
        """trtllm 路径判定：docker_image 字段非空时走 docker runtime。"""
        return self._resolve_runtime()[0] == "docker"

    def stop_backend(self) -> None:
        """docker 分支：docker rm -f <container>；venv 分支：基类 stop_instance。"""
        if self._resolve_runtime()[0] == "docker":
            from modelctl.core.process import stop_docker_instance
            stop_docker_instance(self.profile.name, self._container_name())
        else:
            super().stop_backend()
