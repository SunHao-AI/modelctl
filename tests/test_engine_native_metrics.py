#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_engine_native_metrics.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/1 10:00
# @Desc   : 引擎 native_metrics_mapping 基类默认值测试
# ===============================================================================

"""engines.base.native_metrics_mapping 默认值测试。"""

from unittest.mock import MagicMock

import modelctl.core.compat_rules  # noqa: F401 —— 导入即注册
from modelctl.core.capabilities import Capabilities
from modelctl.engines import get_adapter
from modelctl.engines.vllm import VllmAdapter


def test_base_native_metrics_mapping_default_none():
    profile = MagicMock()
    profile.name = "o"
    profile.engine_config = {}
    profile.port = 11434
    profile.api_key = None
    adapter = get_adapter("ollama")(profile, Capabilities())
    assert adapter.native_metrics_mapping() is None


def _make_vllm_profile(engine_config: dict):
    profile = MagicMock()
    profile.name = "qwen3.8"
    profile.engine_config = engine_config
    profile.port = 8000
    profile.api_key = None
    return profile


def _patch_vllm_envs(monkeypatch):
    """把 vllm 模块的 envs 指向 MagicMock，隔离对真实 venv 的依赖。"""
    fake_envs = MagicMock()
    fake_envs.ensure_env.return_value = None
    fake_envs.vllm_version.return_value = (0, 14, 0)
    fake_envs.VENV_ROOT = "/tmp/fake_venvs"
    fake_envs.engine_bin.return_value = "/tmp/fake_venvs/vllm/bin/vllm"
    monkeypatch.setattr("modelctl.engines.vllm.envs", fake_envs)
    return fake_envs


def test_vllm_native_metrics_mapping_returns_vllm_fields():
    profile = _make_vllm_profile({})
    adapter = VllmAdapter(profile, Capabilities())
    mapping = adapter.native_metrics_mapping()
    assert mapping is not None
    assert mapping == {
        "rate": "tokens_per_second",
        "ttft_ms": "time_to_first_token_ms",
        "gen_time_ms": "generation_time_ms",
        "prompt_tokens": "num_prompt_tokens",
        "completion_tokens": "num_generation_tokens",
    }


def test_vllm_warns_when_per_request_metrics_on_but_force_off(monkeypatch):
    _patch_vllm_envs(monkeypatch)
    cfg = {
        "model": "/nonexistent/path",
        "enable_per_request_metrics": True,
        "enable_force_include_usage": False,
    }
    profile = _make_vllm_profile(cfg)
    adapter = VllmAdapter(profile, Capabilities())
    monkeypatch.setattr(adapter, "selected_gpus", lambda: None)
    monkeypatch.setattr(adapter, "_check_vram_advisory", lambda *a, **k: None)
    monkeypatch.setattr(adapter, "run_compat_checks", lambda *a, **k: None)
    try:
        adapter.check_requirements()
    except Exception:
        pass  # 本用例断言点是 warnings，允许 RequirementError
    assert any("enable_force_include_usage" in w for w in adapter.warnings)


def test_vllm_no_warning_when_both_flags_on(monkeypatch):
    _patch_vllm_envs(monkeypatch)
    cfg = {
        "model": "/nonexistent/path",
        "enable_per_request_metrics": True,
        "enable_force_include_usage": True,
    }
    profile = _make_vllm_profile(cfg)
    adapter = VllmAdapter(profile, Capabilities())
    monkeypatch.setattr(adapter, "selected_gpus", lambda: None)
    monkeypatch.setattr(adapter, "_check_vram_advisory", lambda *a, **k: None)
    monkeypatch.setattr(adapter, "run_compat_checks", lambda *a, **k: None)
    try:
        adapter.check_requirements()
    except Exception:
        pass
    assert not any("enable_force_include_usage" in w for w in adapter.warnings)


def test_vllm_metrics_mapping_declares_ttft_histogram():
    profile = _make_vllm_profile({})
    adapter = VllmAdapter(profile, Capabilities())
    assert adapter.metrics_mapping()["ttft_ms"] == ["vllm:time_to_first_token_seconds"]
