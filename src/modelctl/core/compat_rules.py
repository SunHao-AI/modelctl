#!/usr/bin/env python3
"""core/compat_rules.py — 内置能力检测规则注册（导入即注册）。"""

from __future__ import annotations

from modelctl.core.capabilities import cc_at_least
from modelctl.core.compat import (
    CompatIssue,
    CompatRule,
    EnvSpec,
    GpuSpec,
    ModelSpec,
    register_rule,
)


def _deepseek_v4_mhc_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    if model is None or not model.is_deepseek_v4:
        return None
    major = gpu.cc_major
    if major is None:
        return None  # CC 未知，不误报
    if major in (9, 10):
        return None
    gpu_name = gpu.gpu_name or f"GPU（CC {gpu.cc}）"
    return CompatIssue(
        level="block",
        rule_id="deepseek_v4_mhc",
        reason=(
            f"DeepSeek-V4 的 mHC（HyperConnection）层依赖 DeepGEMM hyperconnection 内核，"
            f"官方仅支持 Hopper/Blackwell DC（计算能力 9.0/10.0），"
            f"当前 GPU 为 {gpu_name}（CC {gpu.cc}）。"
            "如仍需在当前架构部署，可改用 llamacpp 引擎运行 GGUF 版本（models/llamacpp/deepseek-v4-flash.yaml）。"
        ),
    )


def _fp8_quant_cc_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    if model is None or "fp8" not in model.quantization:
        return None
    if not gpu.cc or cc_at_least(gpu.cc, 8, 9):
        return None
    return CompatIssue(
        level="block",
        rule_id="fp8_quant_cc",
        reason=f"FP8 量化需要计算能力 ≥ 8.9，当前 CC {gpu.cc}。建议改用 bf16 权重或更换 GPU。",
    )


def _fp4_quant_blackwell_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    if model is None or "fp4" not in model.quantization:
        return None
    major = gpu.cc_major
    if major is None:
        return None
    if major in (10, 12):
        return None
    return CompatIssue(
        level="block",
        rule_id="fp4_quant_blackwell",
        reason=f"FP4 量化仅支持 Blackwell（计算能力 10.0/12.0），当前 CC {gpu.cc}。",
    )


def _register() -> None:
    register_rule(CompatRule(id="deepseek_v4_mhc", engines=("vllm", "sglang"), check=_deepseek_v4_mhc_check))
    register_rule(CompatRule(id="fp8_quant_cc", engines=("vllm", "sglang"), check=_fp8_quant_cc_check))
    register_rule(CompatRule(id="fp4_quant_blackwell", engines=("vllm",), check=_fp4_quant_blackwell_check))


_register()
