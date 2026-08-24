#!/usr/bin/env python3
"""core/vram_estimator.py — KV cache 显存预检估算（附录 B.4）。

公式：KV cache 显存 ≈ 总上下文 token 数 × 每 token KV 字节数
    per_token_kv_bytes = n_layers × kv_head_count × head_dim × 2(K+V) × bytes_per_element
    （llamacpp 的总上下文 = ctx_size × parallel，即 llama-server --ctx-size 总量）

纯标准库实现，可独立测试；接入位置：all_service.start_profile 启动前告警。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modelctl.core.profile import Profile

# KV 数据类型 → 每元素字节数（q5_0=5bit、q4_0=4bit 按位宽折算）
DTYPE_BYTES: dict[str, float] = {
    "fp8": 1.0,
    "f8": 1.0,
    "f8_e4m3": 1.0,
    "q8_0": 1.0,
    "fp16": 2.0,
    "f16": 2.0,
    "bf16": 2.0,
    "f32": 4.0,
    "q5_0": 0.625,
    "q4_0": 0.5,
    "q4_k_m": 0.5,
    "q4_k_s": 0.5,
}

# 已知模型架构（n_layers / kv_heads / head_dim）；未收录模型可读取本地 config.json 推断。
# 注：Qwen3.8-27B 参数来自 docs/rtx5880-performance-report.md 的估算口径。
KNOWN_MODEL_ARCHS: dict[str, dict[str, int]] = {
    "qwen3.8-27b": {"n_layers": 64, "kv_heads": 4, "head_dim": 256},
}


def dtype_bytes_for(cache_type: str) -> float:
    """KV 量化类型 → 每元素字节数；未知类型回退 fp16（2 字节）。"""
    return DTYPE_BYTES.get(str(cache_type).strip().lower(), 2.0)


def per_token_kv_bytes(n_layers: int, kv_heads: int, head_dim: int, dtype_bytes: float) -> float:
    """单个 token 的 KV cache 字节数（K+V 各一份）。"""
    return n_layers * kv_heads * head_dim * 2 * dtype_bytes


def estimate_kv_bytes(ctx_tokens: int, n_layers: int, kv_heads: int, head_dim: int, dtype_bytes: float) -> float:
    """估算总 KV cache 字节数。"""
    return ctx_tokens * per_token_kv_bytes(n_layers, kv_heads, head_dim, dtype_bytes)


def estimate_kv_mb(ctx_tokens: int, n_layers: int, kv_heads: int, head_dim: int, dtype_bytes: float) -> float:
    """估算总 KV cache 显存（MB）。"""
    return estimate_kv_bytes(ctx_tokens, n_layers, kv_heads, head_dim, dtype_bytes) / 1024 / 1024


def read_model_arch(config_path: Path) -> dict[str, int] | None:
    """从 HF config.json 读取架构参数 {n_layers, kv_heads, head_dim}；不可用返回 None。"""
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    n_layers = data.get("num_hidden_layers")
    kv_heads = data.get("num_key_value_heads")
    head_dim = data.get("head_dim")
    if not head_dim:
        hidden = data.get("hidden_size")
        heads = data.get("num_attention_heads")
        if isinstance(hidden, (int, float)) and isinstance(heads, (int, float)) and heads:
            head_dim = hidden / heads
    if not all(isinstance(x, (int, float)) and x > 0 for x in (n_layers, kv_heads, head_dim)):
        return None
    return {"n_layers": int(n_layers), "kv_heads": int(kv_heads), "head_dim": int(head_dim)}


def _arch_for_model(model: str) -> dict[str, int] | None:
    """按模型标识解析架构：先查内置表，再尝试读取本地 config.json。"""
    key = str(model).lower()
    for name, arch in KNOWN_MODEL_ARCHS.items():
        if name in key:
            return arch
    model_dir = Path(model).expanduser()
    if model_dir.is_dir():
        return read_model_arch(model_dir / "config.json")
    return None


def _ctx_tokens_and_gpus(profile: Profile) -> tuple[int, int, str] | None:
    """按引擎提取 (总上下文 token 数, GPU 数, KV dtype 标识)；无法提取返回 None。"""
    ec = profile.engine_config
    engine = profile.engine
    if engine == "llamacpp":
        ctx = ec.get("ctx_size")
        ctx_per_slot = int(ctx) if ctx not in (None, "") else 1_048_576
        parallel = int(ec.get("parallel", 1) or 1)
        gpus = int(ec.get("gpu_count", 1) or 1)
        dtype = ec.get("cache_type_v") or ec.get("cache_type_k") or "fp16"
        return ctx_per_slot * parallel, gpus, str(dtype)
    if engine == "vllm":
        ctx = ec.get("max_model_len")
        if not ctx:
            return None
        gpus = int(ec.get("tensor_parallel_size", 1) or 1)
        dtype = ec.get("kv_cache_dtype") or "fp16"
        # vLLM 按 max_num_seqs × max_model_len 预分配 KV；未配置 max_num_seqs 时按下限 1 序列估算
        return int(ctx) * int(ec.get("max_num_seqs", 1) or 1), gpus, str(dtype)
    if engine == "sglang":
        ctx = ec.get("context_length")
        if not ctx:
            return None
        gpus = int(ec.get("tensor_parallel_size", 1) or 1)
        dtype = "fp8" if "kv-cache-dtype fp8" in str(ec.get("extra_args", "")) else "fp16"
        return int(ctx), gpus, dtype
    if engine == "ollama":
        ctx = ec.get("context_length")
        if not ctx:
            return None
        parallel = int(ec.get("num_parallel", 1) or 1)
        # ollama 为全局 serve 进程，无法按卡细分，按单卡口径预警
        return int(ctx) * parallel, 1, "fp16"
    return None  # unsloth 等暂无稳定估算口径


def kv_estimate_for_profile(profile: Profile) -> dict[str, Any] | None:
    """估算 profile 的 KV cache 显存。

    返回 {kv_total_mb, per_card_mb, gpu_count, cache_dtype}；架构或上下文无法解析时返回 None。
    """
    extracted = _ctx_tokens_and_gpus(profile)
    if extracted is None:
        return None
    ctx_tokens, gpus, dtype = extracted
    arch = _arch_for_model(str(profile.engine_config.get("model") or ""))
    if arch is None:
        return None
    kv_mb = estimate_kv_mb(
        ctx_tokens,
        arch["n_layers"],
        arch["kv_heads"],
        arch["head_dim"],
        dtype_bytes_for(dtype),
    )
    return {
        "kv_total_mb": round(kv_mb, 1),
        "per_card_mb": round(kv_mb / max(gpus, 1), 1),
        "gpu_count": gpus,
        "cache_dtype": dtype,
    }


def kv_estimate_warnings(profile: Profile, per_card_limit_mb: float = 48 * 1024) -> list[str]:
    """启动前 KV 显存预检（附录 B.4）：返回告警文案列表；无需估算时为空列表。

    per_card_limit_mb 默认 48GB（RTX 5880 单卡），可注入便于测试。
    """
    est = kv_estimate_for_profile(profile)
    if est is None:
        return []
    if est["per_card_mb"] > per_card_limit_mb:
        return [
            f"显存预检：{profile.name} KV cache 估算约 {est['kv_total_mb']:.0f}MB"
            f"（{est['gpu_count']} 卡均摊 {est['per_card_mb']:.0f}MB/卡，{est['cache_dtype']}），"
            f"超过单卡上限 {per_card_limit_mb:.0f}MB，启动可能 OOM；"
            "建议降低上下文/并发或升级 KV 量化（如 fp8/q8_0）"
        ]
    if est["per_card_mb"] > per_card_limit_mb * 0.9:
        return [
            f"显存预检：{profile.name} KV cache 估算 {est['per_card_mb']:.0f}MB/卡"
            f"（{est['cache_dtype']}），接近单卡上限 {per_card_limit_mb:.0f}MB，请留意峰值显存"
        ]
    return []
