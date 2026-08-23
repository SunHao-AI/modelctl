#!/usr/bin/env python3
"""engines/base.py — 引擎适配器抽象基类。"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

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

    @abstractmethod
    def build_command(self) -> tuple[list[str], dict[str, str]]:
        """返回 (启动命令, 需注入的环境变量)。"""

    @abstractmethod
    def check_requirements(self) -> None:
        """校验硬件/配置门槛；可降级的写 self.warnings，硬性不满足抛 RequirementError。"""

    @abstractmethod
    def metrics_mapping(self) -> dict[str, list[str]] | None:
        """Prometheus 指标名映射；None 表示该引擎不支持精确统计。"""

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
            env = EnvSpec.from_env()
            self._compat_env = env
        issues = run_compat(self.profile.engine, GpuSpec.from_caps(self.caps), env, model)
        apply_compat(self.profile.name, self.profile.engine, self.warnings, issues)

    def wait_ready(self, timeout: float) -> bool:
        """等待后端就绪（默认：以上游 API key 探测 health_url）。"""
        return wait_health(self.health_url(), timeout, self.upstream_api_key())

    def ui_spec(self, port: int | None = None, host: str | None = None) -> dict | None:
        """Web 管理控制台规格 {cmd, env, port, host, allow_from}；引擎不提供时返回 None。

        port/host/allow_from 可经 yaml 与 CLI 参数覆盖，优先级由实现方保证。
        """
        return None
