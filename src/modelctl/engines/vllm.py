#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/engines/vllm.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : vLLM 引擎适配器
# ===============================================================================

"""engines/vllm.py — vLLM 适配器。"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from modelctl.core import envs
from modelctl.core.capabilities import all_vram_total_mb, selected_vram_total_mb
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.gpu_lock import acquire_gpu_lock
from modelctl.core.gpu_utils import GPUValidationError
from modelctl.engines._download import download_repo
from modelctl.engines._persist import persist_model_path
from modelctl.engines.base import EngineAdapter, RequirementError


class VllmAdapter(EngineAdapter):
    def check_requirements(self) -> None:
        cfg = self.profile.engine_config
        runtime, _image = self._resolve_runtime()
        if runtime == "docker":
            container_name = f"{self.profile.name}-vllm"
            # docker / nvidia-smi 都在 PATH；硬拦截不降级
            if shutil.which("docker") is None:
                raise RequirementError(
                    "docker 命令不在 PATH——docker_image 已配置，请先安装 docker "
                    "（参考 `apt install docker.io docker-compose` 或官网）"
                )
            if shutil.which("nvidia-smi") is None:
                raise RequirementError(
                    "nvidia-smi 不在 PATH / nvidia-container-toolkit 未就绪——"
                    "docker 方式下 --gpus 设定需要 toolkit 支持，"
                    "请先安装 nvidia-container-toolkit"
                )
            # 清冲突残留容器（幂等）
            try:
                subprocess.run(["docker", "rm", "-f", container_name],
                               capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
            # model 必填
            if not cfg.get("model") and not cfg.get("download"):
                raise RequirementError(
                    f"{self.profile.name}：vllm.model 必填（或配置 download 段自动下载）"
                )
        else:
            # 现状路径：venv 检查、model 检查
            envs.ensure_env("vllm")
            if not cfg.get("model") and not cfg.get("download"):
                raise RequirementError(f"{self.profile.name}：vllm.model 必填（或配置 download 段自动下载）")
        # 共享部分：GPU / TP / VRAM / compat / gpu lock
        try:
            gpus = self.selected_gpus()
        except (GPUValidationError, ValueError) as exc:
            raise RequirementError(f"[gpu_list] {exc}") from exc
        if gpus is not None:
            self.validate_gpu_selection(gpus)
            tp = int(cfg.get("tensor_parallel_size", len(gpus)))
            if tp != len(gpus):
                raise RequirementError(
                    f"gpu_list 指定了 {len(gpus)} 块 GPU，但 tensor_parallel_size={tp}，二者必须一致"
                )
        else:
            tp = int(cfg.get("tensor_parallel_size", 1))
            if self.caps.gpu_count and tp > self.caps.gpu_count:
                raise RequirementError(f"tensor_parallel_size={tp} 超过实际 GPU 数 {self.caps.gpu_count}")
        self._check_vram_advisory(cfg, gpus)
        self.run_compat_checks()  # 预检：软件规则 + 模型 id 特征
        if gpus is not None:
            acquire_gpu_lock(self.profile.name, gpus)

    def _check_vram_advisory(self, cfg: dict, gpus: list[int] | None) -> None:
        """HF 权重粗估（spec §2.1）：权重大小超可用显存上限时仅告警、不硬拦截。

        上限按 总显存 × gpu_memory_utilization 估算；未计 KV cache/激活，
        HF 权重加载行为复杂，故不做硬性 block（vllm 自身启动时会 OOM 报错）。
        """
        model = Path(str(cfg.get("model") or "")).expanduser()
        if not model.is_dir():
            return  # 尚未下载或非本地路径，无法估算
        size_bytes = sum(p.stat().st_size for pat in ("*.safetensors", "*.bin") for p in model.rglob(pat))
        weights_mb = size_bytes / 1024 / 1024
        if weights_mb <= 0:
            return
        fraction = float(cfg.get("gpu_memory_utilization", 0.9))
        total_mb = selected_vram_total_mb(self.caps, gpus) if gpus else all_vram_total_mb(self.caps)
        cap_mb = total_mb * fraction
        if total_mb > 0 and weights_mb > cap_mb:
            self.warnings.append(
                f"模型权重约 {weights_mb:.0f}MB，超过估算可用显存上限 {cap_mb:.0f}MB"
                f"（总显存 {total_mb}MB × gpu_memory_utilization={fraction}，未计 KV cache）；"
                "若实际剩余显存不足 vllm 启动会失败，可更换 gpu_list 或调整 gpu_memory_utilization"
            )

    def pre_start(self) -> None:
        cfg = self.profile.engine_config
        model = str(cfg.get("model") or "")
        if not (model and (Path(model).expanduser().is_dir() or Path(model).expanduser().is_file())):
            if cfg.get("download"):
                modelscope_id = cfg["download"]["modelscope_id"]
                model_root = Path(os.environ.get("MODEL_ROOT") or PROJECT_ROOT.parent / "model-hf")
                local_dir = download_repo(modelscope_id, model_root)
                if self.profile.path is None:
                    raise RequirementError(f"{self.profile.name}：profile 文件路径缺失，无法写回模型路径")
                persist_model_path(self.profile.path, "vllm", str(local_dir.resolve()))
                cfg["model"] = str(local_dir.resolve())
        # 精检：模型文件就位后，以 config.json 判定更精确的模型特征
        # 延迟导入 ModelSpec，避免 compat 部分初始化时经 engines/__init__ 回环
        from modelctl.core.compat import ModelSpec

        local = Path(str(cfg.get("model") or "")).expanduser()
        if local.is_dir():
            self.run_compat_checks(ModelSpec.from_local(self.profile.engine, local))

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        gpus = self.selected_gpus()
        tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
        runtime, image = self._resolve_runtime()

        # 共用：--served-model-name 之后的 model_args
        extra = shlex.split(str(cfg.get("extra_args") or ""))
        model_args = [
            "--served-model-name", self.upstream_model_name(),
            "--host", "0.0.0.0",
            "--tensor-parallel-size", str(tp),
            "--gpu-memory-utilization", str(cfg.get("gpu_memory_utilization", 0.9)),
            "--disable-uvicorn-access-log",
        ]
        if cfg.get("max_model_len"):
            model_args += ["--max-model-len", str(cfg["max_model_len"])]
        if cfg.get("quantization"):
            model_args += ["--quantization", str(cfg["quantization"])]
        if cfg.get("kv_cache_dtype"):
            model_args += ["--kv-cache-dtype", str(cfg["kv_cache_dtype"])]
        model_args += self.api_key_args() + extra

        if runtime == "venv":
            cmd = [str(envs.engine_bin("vllm", "vllm")), "serve", str(cfg["model"])] + model_args
            return cmd, self._venv_env(gpus)

        # docker 分支
        model_raw = str(cfg.get("model") or "").strip()
        model_local = Path(model_raw).expanduser()
        if not model_local.is_absolute() or not model_local.is_dir():
            raise RequirementError(
                f"{self.profile.name}：docker_image 路径下 model 必须为本地绝对路径"
                f"且目录已存在（当前: {model_raw}——HF id 需先 modelctl start 触发 pre_start 下载）"
            )
        model_local = model_local.resolve()
        cmd = [
            "docker", "run",
            "--name", self._container_name,
            "--gpus", self._gpus_json(),
            "-p", f"{self.profile.port}:8000",
            "-v", f"{model_local.parent.as_posix()}:/models:ro",
            "--ipc=host",
            "--detach",
            image,
            "vllm", "serve", f"/models/{model_local.name}",
        ] + model_args + ["--port", "8000"]
        env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        return cmd, env

    def metrics_mapping(self) -> dict[str, list[str]]:
        return {
            "prompt_total": ["vllm:prompt_tokens_total"],
            "predicted_total": ["vllm:generation_tokens_total"],
            # 实时速率 gauge：vLLM 自带（内部滑动窗口），客户端直连模型端口（绕过网关）时
            # 也能统计到真实吞吐；缺失/为 0 时 stats 退化为窗口差分
            "prompt_rate": [
                "vllm:prompt_tokens_seconds",
                "vllm:avg_prompt_throughput_toks_per_sec",
            ],
            "predicted_rate": [
                "vllm:generation_tokens_seconds",
                "vllm:avg_generation_throughput_toks_per_sec",
            ],
        }

    def upstream_model_name(self) -> str:
        """vLLM 对外暴露的 served 模型名 = profile.name。

        与 build_command 的 --served-model-name、modelctl list 标识符一致：
        无论经网关转发还是直连 vLLM 端口，请求体 model 都用 profile.name（如 qwen3.8-vllm）。
        """
        return self.profile.name

    def _resolve_runtime(self) -> tuple[str, str | None]:
        """yaml 字段 vllm.docker_image 非空→('docker', image)；其余回退 ('venv', None)。

        留空时与改造前完全等价（仍走 `envs.engine_bin("vllm", "vllm")`），
        已部署的 7 个 models/vllm/*.yaml 行为零变化。
        """
        cfg = self.profile.engine_config
        image = str(cfg.get("docker_image") or "").strip()
        return ("docker", image) if image else ("venv", None)

    @property
    def _container_name(self) -> str:
        return f"{self.profile.name}-vllm"

    def _gpus_json(self) -> str:
        """返回 docker --gpus 参数值（带引号的 JSON 格式）。"""
        gpus = self.selected_gpus()
        if gpus:
            seq = list(gpus)
        else:
            tp = int(self.profile.engine_config.get("tensor_parallel_size", 1))
            seq = list(range(int(self.caps.gpu_count or tp)))
        return '"device=' + ",".join(str(g) for g in seq) + '"'

    def _venv_env(self, gpus: list[int] | None) -> dict[str, str]:
        """现状 env 注入段（HF_HOME / VIRTUAL_ENV / PATH）抽取为独立方法。"""
        env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "vllm")
        env["PATH"] = str(envs.engine_bin("vllm", "vllm").parent) + os.pathsep + \
            os.environ.get("PATH", os.environ["PATH"])
        return env

    def stop_patterns(self) -> list[str]:
        # 用启动命令特征而非引擎短名：`modelctl stop qwen3.8-vllm` 自身命令行
        # 含 "vllm"，若 pkill -f "vllm" 会误杀 modelctl 进程（shell 打印 Terminated）
        if self._resolve_runtime()[0] != "docker":
            return ["vllm serve"]
        # docker 分支：Popen cmdline 为空格连接，模式 1 是其中连续子串，精准命中
        name = self._container_name
        gpus_json = self._gpus_json()
        model_raw = str(self.profile.engine_config.get("model") or "").strip()
        root = Path(model_raw).expanduser().resolve().parent.as_posix() if model_raw else "/"
        return [
            f"docker run --name {name} --gpus {gpus_json}",
            f"-v {root}:/models:ro",
        ]
