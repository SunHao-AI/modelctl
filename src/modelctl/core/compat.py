#!/usr/bin/env python3
"""core/compat.py — 启动前能力检测框架（硬件 GpuSpec + 软件 EnvSpec + 规则库）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from modelctl.core.capabilities import Capabilities

# 计算能力主版本 -> 架构家族（仅用于错误消息展示，不参与规则判定）
ARCH_FAMILY_LABELS: dict[int, str] = {
    8: "Ampere/Ada",
    9: "Hopper",
    10: "Blackwell",
    12: "Blackwell-Consumer",
}

# DeepSeek-V4 的 mHC（Manifold-Constrained Hyper-Connections）层依赖 DeepGEMM 的
# tf32_hc_prenorm_gemm 内核，官方仅提供 SM90（Hopper）/SM100（Blackwell DC）实现。
_DEEPSEEK_V4_ARCHS = ("DeepseekV4ForCausalLM",)
_DEEPSEEK_V4_NAME_MARKERS = ("deepseek-v4", "deepseek_v4")


def cc_major(cc: str) -> int | None:
    """提取 compute capability 主版本号（"8.9" -> 8）；无法解析返回 None。"""
    try:
        return int(cc.split(".", 1)[0])
    except (ValueError, AttributeError):
        return None


def _is_deepseek_v4_name(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _DEEPSEEK_V4_NAME_MARKERS)


@dataclass
class GpuSpec:
    """硬件能力快照。"""

    cc: str = ""
    gpu_count: int = 0
    gpu_name: str = ""
    vram_total_mb: int = 0
    vram_free_mb: list[int] = field(default_factory=list)

    @property
    def cc_major(self) -> int | None:
        return cc_major(self.cc)

    @property
    def arch_family(self) -> str:
        major = self.cc_major
        if major is None:
            return "unknown"
        return ARCH_FAMILY_LABELS.get(major, "unknown")

    @classmethod
    def from_caps(cls, caps: Capabilities) -> GpuSpec:
        return cls(
            cc=caps.compute_capability,
            gpu_count=caps.gpu_count,
            gpu_name=caps.gpu_name,
            vram_total_mb=caps.vram_total_mb,
            vram_free_mb=list(caps.vram_free_mb),
        )


@dataclass
class ModelSpec:
    """模型特征（预检 source=id / 精检 source=local）。"""

    engine: str
    source: Literal["local", "id"] = "id"
    architectures: tuple[str, ...] = ()
    model_type: str = ""
    quantization: str = ""
    name_hint: str = ""

    @property
    def is_deepseek_v4(self) -> bool:
        if any(a in _DEEPSEEK_V4_ARCHS for a in self.architectures):
            return True
        if "deepseek_v4" in self.model_type.lower():
            return True
        return _is_deepseek_v4_name(self.name_hint)

    @classmethod
    def from_local(cls, engine: str, path: str | Path) -> ModelSpec:
        data: dict = {}
        config = Path(path).expanduser() / "config.json"
        if config.is_file():
            try:
                data = json.loads(config.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        quant = str((data.get("quantization_config") or {}).get("quant_method") or "").lower()
        return cls(
            engine=engine,
            source="local",
            architectures=tuple(str(a) for a in data.get("architectures") or []),
            model_type=str(data.get("model_type") or ""),
            quantization=quant,
            name_hint=str(path),
        )

    @classmethod
    def from_id(cls, engine: str, model_id: str, download_id: str = "", quantization: str = "") -> ModelSpec:
        return cls(
            engine=engine,
            source="id",
            quantization=quantization.lower(),
            name_hint=f"{model_id} {download_id}".strip(),
        )
