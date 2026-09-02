#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/engines/base.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 引擎适配器抽象基类
# ===============================================================================

"""engines/base.py — 引擎适配器抽象基类。"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import subprocess

from modelctl.core.capabilities import Capabilities
from modelctl.core.gpu_utils import GPUValidationError, resolve_gpu_list, validate_gpu_selection
from modelctl.core.process import wait_health
from modelctl.core.profile import Profile

if TYPE_CHECKING:
    from modelctl.core.compat import ModelSpec


class RequirementError(RuntimeError):
    """硬性条件不满足，拒绝启动。"""


class EngineAdapter(ABC):
    def __init__(self, profile: Profile, caps: Capabilities):
        self.profile = profile
        self.caps = caps
        self.warnings: list[str] = []
        # 本次 start_detached 拉起的进程句柄（由 all_service.start_profile 注入）；
        # None 表示非本工具拉起或尚未启动，健康检查不做早退探测
        self.spawned_proc: subprocess.Popen | None = None

    @abstractmethod
    def build_command(self) -> tuple[list[str], dict[str, str]]:
        """返回 (启动命令, 需注入的环境变量)。"""

    @abstractmethod
    def check_requirements(self) -> None:
        """校验硬件/配置门槛；可降级的写 self.warnings，硬性不满足抛 RequirementError。"""

    @abstractmethod
    def metrics_mapping(self) -> dict[str, list[str]] | None:
        """Prometheus 指标名映射；None 表示该引擎不支持精确统计。"""

    def native_metrics_mapping(self) -> dict[str, str] | None:
        """per-request 原生指标字段名映射（网关喂 stats collector 时用）。

        键固定为 {rate, ttft_ms, gen_time_ms, prompt_tokens, completion_tokens}，
        值为该引擎 SSE 末块 / 响应根级 "metrics" 对象中真实字段名。
        默认 None 表示该引擎不提供 per-request 原生指标（stats 侧短路）。
        """
        return None

    def health_url(self) -> str:
        return f"http://127.0.0.1:{self.profile.port}/health"

    def selected_gpus(self) -> list[int] | None:
        """按 profile.gpu_list > 环境变量 MODELCTL_GPUS（CLI --gpus 亦写入此变量）解析。"""
        cfg = self.profile.engine_config
        return resolve_gpu_list(cfg.get("gpu_list"), None, os.environ.get("MODELCTL_GPUS"))

    def validate_gpu_selection(self, gpus: list[int] | None = None) -> None:
        gpus = gpus if gpus is not None else self.selected_gpus()
        if gpus is None:
            return
        try:
            validate_gpu_selection(gpus, self.caps.gpu_indices)
        except GPUValidationError as exc:
            raise RequirementError(str(exc)) from exc

    def cuda_visible_devices(self, gpus: list[int]) -> dict[str, str]:
        return {"CUDA_VISIBLE_DEVICES": ",".join(str(g) for g in gpus)}

    def pre_start(self) -> None:
        """启动前钩子（下载/编译/pull）。"""
        return None

    def post_start(self) -> None:
        """启动后钩子（如 ollama 预加载模型）。"""
        return None

    def unload_model(self) -> None:
        """stop 时卸载模型；默认无操作（仅 ollama 需要）。"""
        return None

    def stop_patterns(self) -> list[str]:
        return []

    def upstream_model_name(self) -> str:
        """后端 API 期望的模型名（网关改写请求体 model 字段的目标）。

        默认 = profile.name（llama-server 等忽略 model 名，无需改写）。
        """
        return self.profile.name

    def api_key_args(self) -> list[str]:
        return ["--api-key", self.profile.api_key] if self.profile.api_key else []

    def upstream_api_key(self) -> str | None:
        """健康检查 / 预热 / 网关转发使用的上游 Bearer key；默认 profile.api_key。"""
        return self.profile.api_key

    def run_compat_checks(self, model: ModelSpec | None = None) -> None:
        """能力检测入口：block 抛 RequirementError，degrade 写 self.warnings。

        model 缺省时按 profile 的 model 字段构造 id 特征 ModelSpec（预检）。
        函数内延迟 import 避免 core.compat 与 engines.base 的循环依赖。
        """
        from modelctl.core.compat import EnvSpec, GpuSpec, ModelSpec, apply_compat, run_compat
        from modelctl.core.envs import engine_site_packages

        if model is None:
            download = self.profile.engine_config.get("download")
            # download 段可能是字符串等非 dict 配置，先判型再取值（防御 AttributeError）
            download_id = download.get("modelscope_id") if isinstance(download, dict) else ""
            model = ModelSpec.from_id(
                self.profile.engine,
                str(self.profile.engine_config.get("model") or ""),
                str(download_id or ""),
                quantization=str(self.profile.engine_config.get("quantization") or ""),
            )
        # EnvSpec 单次进程内缓存：check_requirements 探测一次，pre_start 精检复用（spec 第 5 节）
        env = getattr(self, "_compat_env", None)
        if env is None:
            sp = engine_site_packages(self.profile.engine)
            env = EnvSpec.from_env(site_packages=sp)
            self._compat_env = env
        issues = run_compat(self.profile.engine, GpuSpec.from_caps(self.caps), env, model)
        apply_compat(self.profile.name, self.profile.engine, self.warnings, issues)

    def wait_ready(self, timeout: float) -> bool:
        """等待后端就绪（默认：以上游 API key 探测 health_url；本工具拉起的进程早退则立即失败）。

        docker 分支（spawned_proc 是 `docker run` 客户端）客户端 daemonize 后立刻退出，
        但容器在 daemon 后台持续运行——此时不能把客户端早退当作早退，否则 600s 超时被
        1 秒中断，必须等待 /health ready。子类（VllmAdapter）覆盖此方法注入路径判定。
        """
        alive_check = (lambda: self.spawned_proc.poll() is None) if self.spawned_proc else None
        return wait_health(self.health_url(), timeout, self.upstream_api_key(), alive_check=alive_check)

    def backend_dead(self) -> bool:
        """后端是否真正死亡（用于失败诊断措辞）。

        默认 venv 语义：本工具拉起的进程早退即视为死亡。docker 子类覆盖为容器状态探测
        （容器退出/不存在即视为死亡，客户端进程 daemonize 后早退不等同于容器死亡）。
        spawned_proc 为 None（非本工具拉起）时不视为死亡，回退到"健康超时"措辞。
        """
        return self.spawned_proc is not None and self.spawned_proc.poll() is not None

    def ui_spec(self, port: int | None = None, host: str | None = None) -> dict | None:
        """Web 管理控制台规格 {cmd, env, port, host, allow_from}；引擎不提供时返回 None。

        port/host/allow_from 可经 yaml 与 CLI 参数覆盖，优先级由实现方保证。
        """
        return None

    @staticmethod
    def _check_weights_advisory(
        model_path: str,
        gpus: list[int] | None,
        caps: Capabilities,
        fraction: float,
        fraction_key: str,
        engine_name: str,
        warnings: list[str],
    ) -> None:
        """HF 权重粗估：权重大小超可用显存上限时仅告警、不硬拦截。

        上限 = 总显存 × fraction（各引擎的 gpu_memory_utilization / mem_fraction_static）。
        未计 KV cache/激活，引擎自身启动时会 OOM 报错——本步骤只做前置提醒。
        """
        from pathlib import Path

        from modelctl.core.capabilities import all_vram_total_mb, selected_vram_total_mb

        model = Path(model_path).expanduser()
        if not model.is_dir():
            return
        size_bytes = sum(p.stat().st_size for pat in ("*.safetensors", "*.bin") for p in model.rglob(pat))
        weights_mb = size_bytes / 1024 / 1024
        if weights_mb <= 0:
            return
        total_mb = selected_vram_total_mb(caps, gpus) if gpus else all_vram_total_mb(caps)
        cap_mb = total_mb * fraction
        if total_mb > 0 and weights_mb > cap_mb:
            warnings.append(
                f"模型权重约 {weights_mb:.0f}MB，超过估算可用显存上限 {cap_mb:.0f}MB"
                f"（总显存 {total_mb}MB × {fraction_key}={fraction}，未计 KV cache）；"
                f"若实际剩余显存不足 {engine_name} 启动会失败，可更换 gpu_list 或调整 {fraction_key}"
            )
