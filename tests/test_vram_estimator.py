#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_vram_estimator.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 显存估算模块测试
# ===============================================================================

"""modelctl.core.vram_estimator（附录 B.4）单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

from modelctl.core.profile import Profile
from modelctl.core.vram_estimator import (
    DTYPE_BYTES,
    dtype_bytes_for,
    estimate_kv_bytes,
    estimate_kv_mb,
    kv_estimate_for_profile,
    kv_estimate_warnings,
    per_token_kv_bytes,
    read_model_arch,
)

# Qwen3.8-27B 口径：64 层、4 KV heads、256 head_dim
N_LAYERS, KV_HEADS, HEAD_DIM = 64, 4, 256


def test_dtype_bytes_mapping():
    assert DTYPE_BYTES["fp8"] == 1.0
    assert DTYPE_BYTES["q8_0"] == 1.0
    assert DTYPE_BYTES["fp16"] == 2.0
    assert DTYPE_BYTES["q5_0"] == 0.625
    assert DTYPE_BYTES["q4_0"] == 0.5
    assert dtype_bytes_for("FP8") == 1.0  # 大小写不敏感
    assert dtype_bytes_for("unknown") == 2.0  # 未知回退 fp16


def test_per_token_kv_bytes():
    # q8_0：64×4×256×2×1 = 131072 字节 = 128KB/token
    assert per_token_kv_bytes(N_LAYERS, KV_HEADS, HEAD_DIM, 1.0) == 131072
    # fp16 翻倍
    assert per_token_kv_bytes(N_LAYERS, KV_HEADS, HEAD_DIM, 2.0) == 262144


def test_estimate_kv_bytes_matches_128gb_doc_case():
    """回归：Qwen3.8-27B 原配置（262144×4 槽、q8_0）总 KV 应接近 128GB。"""
    total_tokens = 262144 * 4  # ctx_size × parallel
    kv_bytes = estimate_kv_bytes(total_tokens, N_LAYERS, KV_HEADS, HEAD_DIM, dtype_bytes_for("q8_0"))
    # 1048576 × 131072 = 137,438,953,472 字节 ≈ 128GB（1GB=1024³）
    assert 137e9 < kv_bytes < 138e9


def test_estimate_kv_mb():
    kv_mb = estimate_kv_mb(16384, N_LAYERS, KV_HEADS, HEAD_DIM, dtype_bytes_for("q8_0"))
    assert kv_mb == round(16384 * 131072 / 1024 / 1024, 1)


def test_read_model_arch_from_config_json(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {"num_hidden_layers": 64, "num_key_value_heads": 4, "hidden_size": 1024, "num_attention_heads": 4}
        ),
        encoding="utf-8",
    )
    assert read_model_arch(tmp_path / "config.json") == {"n_layers": 64, "kv_heads": 4, "head_dim": 256}


def test_read_model_arch_head_dim_field_direct(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"num_hidden_layers": 32, "num_key_value_heads": 8, "head_dim": 128}))
    assert read_model_arch(tmp_path / "config.json") == {"n_layers": 32, "kv_heads": 8, "head_dim": 128}


def test_read_model_arch_invalid(tmp_path):
    (tmp_path / "config.json").write_text("not json", encoding="utf-8")
    assert read_model_arch(tmp_path / "config.json") is None
    assert read_model_arch(tmp_path / "missing.json") is None


def _profile(name="qwen3.8", engine="llamacpp", cfg=None, port=18888):
    return Profile(name=name, engine=engine, port=port, engine_config=cfg or {})


def test_kv_estimate_llamacpp_qwen3():
    """llamacpp：ctx_size × parallel 为总 token，GPU 均摊。"""
    p = _profile(
        cfg={
            "model": "Qwen/Qwen3.8-27B",
            "ctx_size": 262144,
            "parallel": 4,
            "gpu_count": 8,
            "cache_type_v": "q8_0",
        }
    )
    est = kv_estimate_for_profile(p)
    assert est is not None
    assert est["cache_dtype"] == "q8_0"
    assert est["gpu_count"] == 8
    assert 130_000 < est["kv_total_mb"] < 140_000  # ≈128GB
    assert abs(est["per_card_mb"] - est["kv_total_mb"] / 8) < 0.1


def test_kv_estimate_vllm_qwen3():
    """vllm：KV = max_num_seqs × max_model_len，dtype 取 kv_cache_dtype。"""
    p = _profile(
        engine="vllm",
        cfg={"model": "Qwen/Qwen3.8-27B", "max_model_len": 262144, "tensor_parallel_size": 4, "kv_cache_dtype": "fp8"},
    )
    est = kv_estimate_for_profile(p)
    assert est is not None
    assert est["cache_dtype"] == "fp8"
    # 262144 × 131072 字节 ≈ 34.4GB，TP4 每卡 ≈ 8.6GB
    assert abs(est["kv_total_mb"] - 262144 * 131072 / 1024 / 1024) < 1.0
    assert abs(est["per_card_mb"] - est["kv_total_mb"] / 4) < 0.1


def test_kv_estimate_unknown_model_returns_none():
    """未收录架构且非本地目录 → 无法估算。"""
    p = _profile(cfg={"model": "moonshotai/Kimi-K2.5-Instruct", "ctx_size": 65536, "parallel": 2})
    assert kv_estimate_for_profile(p) is None


def test_kv_estimate_reads_local_config(tmp_path):
    """本地模型目录存在 config.json 时按实际架构估算。"""
    (tmp_path / "config.json").write_text(
        json.dumps({"num_hidden_layers": 64, "num_key_value_heads": 4, "head_dim": 256}),
        encoding="utf-8",
    )
    p = _profile(cfg={"model": str(tmp_path), "ctx_size": 16384, "parallel": 1, "gpu_count": 2, "cache_type_v": "q8_0"})
    est = kv_estimate_for_profile(p)
    assert est is not None
    assert est["per_card_mb"] == round(16384 * 131072 / 1024 / 1024 / 2, 1)


def test_kv_warnings_over_limit():
    """超限配置应返回 OOM 警告。"""
    p = _profile(cfg={"model": "Qwen/Qwen3.8-27B", "ctx_size": 262144, "parallel": 4, "gpu_count": 1, "cache_type_v": "fp16"})
    warnings = kv_estimate_warnings(p, per_card_limit_mb=48 * 1024)
    assert warnings and "OOM" in warnings[0]


def test_kv_warnings_near_limit():
    """接近上限（>90%）时给出提示。"""
    p = _profile(cfg={"model": "Qwen/Qwen3.8-27B", "ctx_size": 262144, "parallel": 4, "gpu_count": 8, "cache_type_v": "fp16"})
    # 128GB×2 / 8 卡 = 32GB/卡，单卡 48GB 的 66% → 无警告
    assert kv_estimate_warnings(p, per_card_limit_mb=48 * 1024) == []
    # 若单卡上限压到 40GB（32GB > 36GB 的 90%？不，32<36）→ 仍无；用更小上限验证
    assert len(kv_estimate_warnings(p, per_card_limit_mb=34 * 1024)) == 1  # 32GB > 30.6GB(90%) → 接近警告


def test_kv_warnings_empty_when_unestimable():
    p = _profile(cfg={"model": "ghost/unknown-model", "ctx_size": 1000})
    assert kv_estimate_warnings(p) == []
