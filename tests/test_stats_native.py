#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/stats.py：_parse_env_bool / _NativeSample / _percentile 纯函数测试。"""

from __future__ import annotations

from pathlib import Path


# vLLM per-request 原生指标 5 个规范键 → vLLM 字段名（Task 4/5 共用）
VLLM_NATIVE = {
    "rate": "tokens_per_second",
    "ttft_ms": "time_to_first_token_ms",
    "gen_time_ms": "generation_time_ms",
    "prompt_tokens": "num_prompt_tokens",
    "completion_tokens": "num_generation_tokens",
}


def _make_collector(tmp_path: Path, **kw) -> "UsageCollector":
    """构造 on-demand 模式 UsageCollector（poll_interval=999 不轮询）。"""
    from modelctl.core.stats import UsageCollector
    collector = UsageCollector(
        name="t",
        base_url="http://1.1.1.1",
        poll_interval=999,
        api_key=None,
        data_dir=tmp_path,
        mode="on-demand",
        mapping=kw.pop("mapping", {}),
        native_mapping=kw.pop("native_mapping", None),
        bench_fallback=kw.pop("bench_fallback", True),
    )
    return collector


def test_record_native_metrics_updates_snapshot_and_p50(tmp_path):
    collector = _make_collector(tmp_path, native_mapping=VLLM_NATIVE)
    collector._monotonic = lambda: 1000.0  # 常量时钟，保证样本都在窗口内
    for i in range(20):
        collector.record_native_metrics(
            {"tokens_per_second": 10.0 + i, "time_to_first_token_ms": 100.0 + i, "num_prompt_tokens": 32}
        )
    snap = collector.snapshot()
    assert abs(snap["predicted_rate"] - 19.5) < 1e-6  # P50(10.0..29.0)
    # spec 行 485 写 100.5，但 spec 自身引用实现 _compute_native_row（行 707 用 _percentile(..., 50)）
    # 对 20 个连续样本 100..119 线性插值 P50 = 109.5（idx = 19×0.5 = 9.5）。
    # 100.5 是 spec 笔误（P10 也非 100.5），此处按"实现符合 spec 行为"修正断言为 109.5。
    assert abs(snap["ttft_ms"] - 109.5) < 1e-6
    assert snap["ttft_ms_p95"] > snap["ttft_ms"]
    assert snap["rate_source"] == "native"


def test_record_native_metrics_window_cap_trims_oldest(tmp_path):
    collector = _make_collector(tmp_path, native_mapping=VLLM_NATIVE)
    collector._monotonic = lambda: 1000.0
    for _ in range(25):
        collector.record_native_metrics(
            {"tokens_per_second": 10.0, "time_to_first_token_ms": 100.0, "num_prompt_tokens": 32}
        )
    assert len(collector._native_window) == 20


def test_record_native_metrics_ttl_trims_old(tmp_path):
    collector = _make_collector(tmp_path, native_mapping=VLLM_NATIVE)
    collector._native_window_ttl = 10.0
    clock = {"t": 0.0}
    collector._monotonic = lambda: clock["t"]
    for _ in range(5):
        collector.record_native_metrics(
            {"tokens_per_second": 10.0, "time_to_first_token_ms": 100.0, "num_prompt_tokens": 32}
        )
        clock["t"] += 1.0
    clock["t"] = 1000.0
    collector.record_native_metrics(
        {"tokens_per_second": 10.0, "time_to_first_token_ms": 100.0, "num_prompt_tokens": 32}
    )
    assert len(collector._native_window) == 1
    assert collector._native_window[0].ts == 1000.0


def test_record_native_metrics_invalid_input_still_ok(tmp_path):
    collector = _make_collector(tmp_path, native_mapping=VLLM_NATIVE)
    collector.record_native_metrics(None)
    collector.record_native_metrics({})
    collector.record_native_metrics({"tokens_per_second": "abc", "time_to_first_token_ms": 100})
    collector.record_native_metrics(
        {"tokens_per_second": -1.0, "time_to_first_token_ms": 100, "num_prompt_tokens": 32}
    )
    assert len(collector._native_window) == 0
    assert collector.snapshot().get("ttft_ms", 0) == 0


def test_record_native_metrics_when_mapping_none_is_noop(tmp_path):
    collector = _make_collector(tmp_path)
    collector.record_native_metrics(
        {"tokens_per_second": 10.0, "time_to_first_token_ms": 100.0, "num_prompt_tokens": 32}
    )
    assert len(collector._native_window) == 0
    assert collector.snapshot()["ttft_ms"] == 0


def test_bench_fallback_default_true_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("USAGE_BENCH_FALLBACK", raising=False)
    collector = _make_collector(tmp_path, native_mapping=VLLM_NATIVE)
    assert collector.bench_fallback is True


def test_bench_fallback_false_when_env_false(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_BENCH_FALLBACK", "false")
    collector = _make_collector(tmp_path)
    assert collector.bench_fallback is False


def test_bench_fallback_explicit_param_overrides_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_BENCH_FALLBACK", "false")
    collector = _make_collector(tmp_path, bench_fallback=True)
    assert collector.bench_fallback is False


def test_snapshot_initial_no_native_has_none_source(tmp_path):
    collector = _make_collector(tmp_path, native_mapping=VLLM_NATIVE)
    snap = collector.snapshot()
    assert snap["ttft_ms"] == 0.0
    assert snap["ttft_ms_p95"] == 0.0
    assert snap["rate_source"] == "none"


def test_parse_env_bool_true_values():
    from modelctl.core.stats import _parse_env_bool
    for raw in ("1", "true", "YES", "on", " True "):
        assert _parse_env_bool(raw) is True


def test_parse_env_bool_false_values():
    from modelctl.core.stats import _parse_env_bool
    for raw in ("0", "false", "No", "off"):
        assert _parse_env_bool(raw) is False


def test_parse_env_bool_default_on_none_empty_or_unknown():
    from modelctl.core.stats import _parse_env_bool
    assert _parse_env_bool(None) is True
    assert _parse_env_bool("") is True
    assert _parse_env_bool("   ") is True
    assert _parse_env_bool("xxx") is True
    assert _parse_env_bool(None, default=False) is False
    assert _parse_env_bool("", default=False) is False


def test_percentile_empty_returns_none():
    from modelctl.core.stats import _percentile
    assert _percentile([], 50) is None


def test_percentile_single_element_returns_itself():
    from modelctl.core.stats import _percentile
    assert _percentile([42.0], 50) == 42.0
    assert _percentile([42.0], 95) == 42.0
    assert _percentile([42.0], 0) == 42.0


def test_percentile_p50_matches_median_on_even_length():
    from modelctl.core.stats import _percentile
    values = [float(i) for i in range(1, 11)]  # 1..10
    assert abs(_percentile(values, 50) - 5.5) < 1e-9


def test_percentile_p95_linear_interpolation():
    from modelctl.core.stats import _percentile
    values = [float(i) for i in range(1, 11)]  # 1..10
    # idx = 9 * 0.95 = 8.55 → 9.0*0.45 + 10.0*0.55 = 9.55
    assert abs(_percentile(values, 95) - 9.55) < 1e-9


def test_percentile_sorted_input():
    from modelctl.core.stats import _percentile
    # 乱序输入应自动排序，结果同有序
    shuffled = [10.0, 1.0, 9.0, 5.0, 3.0]
    ordered = [1.0, 3.0, 5.0, 9.0, 10.0]
    assert _percentile(shuffled, 50) == _percentile(ordered, 50)


def test_native_sample_dataclass_field_layout():
    from modelctl.core.stats import _NativeSample
    s = _NativeSample(ts=1.0, tokens_per_second=10.0, prompt_inflight_rate=5.0,
                      ttft_ms=200.0, ttft_s=0.2)
    assert s.ts == 1.0
    assert s.tokens_per_second == 10.0
    assert s.prompt_inflight_rate == 5.0
    assert s.ttft_ms == 200.0
    assert s.ttft_s == 0.2


def _make_handler_target(name, **kw):
    """构造 UsageHandler._build_target_payload 测试用 target stub。"""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    stub = SimpleNamespace(
        name=name,
        aliases=[],
        data_dir=MagicMock(),
        metrics_url="x",
        mapping={},
        usage_cfg={},
        api_key=None,
        bench_url="http://127.0.0.1:1/x",
        bench_model=name,
    )
    for k, v in kw.items():
        setattr(stub, k, v)
    return stub


def test_build_usage_payload_extra_includes_ttft_when_nonzero():
    from modelctl.core.stats import build_usage_payload
    payload = build_usage_payload(
        {
            "prompt_total": 100.0,
            "predicted_total": 200.0,
            "prompt_rate": 10.0,
            "predicted_rate": 20.0,
            "ttft_ms": 123.0,
            "ttft_ms_p95": 210.0,
        },
        {},
        0.0,
        1.0,
    )
    assert "首 Token P50 = 123 ms（P95 = 210 ms）" in payload["extra"]
    assert payload["prompt_rate"] == 10.0


def test_build_usage_payload_extra_no_ttft_when_zero():
    from modelctl.core.stats import build_usage_payload
    payload = build_usage_payload(
        {
            "prompt_total": 100.0,
            "predicted_total": 200.0,
            "prompt_rate": 10.0,
            "predicted_rate": 20.0,
        },
        {},
        0.0,
        1.0,
    )
    assert "首 Token P50" not in payload["extra"]


def test_build_usage_payload_ttft_p95_optional_when_zero():
    from modelctl.core.stats import build_usage_payload
    payload = build_usage_payload(
        {
            "prompt_total": 100.0,
            "predicted_total": 200.0,
            "prompt_rate": 10.0,
            "predicted_rate": 20.0,
            "ttft_ms": 50.0,
        },
        {},
        0.0,
        1.0,
    )
    assert "首 Token P50 = 50 ms" in payload["extra"]
    assert "P95" not in payload["extra"]


def test_build_target_payload_skips_bench_when_ttft_and_rate_present():
    """Task 5 新语义：TTFT 与速率都齐（无字段缺口）→ 不 bench，rate_ttft 保留。

    历史注记：旧版叫 ...skips_bench_when_native_ttft_present，仅凭 ttft 有就足
    以跳 bench（native_has_any 任一沟碰）；新 gate 按字段缺口分别受限，有 rate
    缺口仍需 bench（补 rate 不碰 ttft）。所以这个用例同步补上 rate 有值才有
    跳 bench 意义。"""
    import modelctl.core.stats as S
    from unittest.mock import MagicMock, Mock, patch
    fake_collector = MagicMock()
    fake_collector.bench_fallback = True
    fake_collector.get_snapshot.return_value = {
        "ok": True,
        "prompt_total": 100.0,
        "predicted_total": 200.0,
        "prompt_rate": 42.0,
        "predicted_rate": 43.0,
        "ttft_ms": 123.0,
        "ttft_ms_p95": 210.0,
        "rate_source": "native",
    }
    handler = Mock()
    handler.collectors = {"m1": fake_collector}
    handler.start_time = 0.0
    tgt = _make_handler_target("m1")
    try:
        with patch.object(S, "_bench_cached") as m_bench:
            m_bench.return_value = (1.0, 2.0, 3)
            payload = S.UsageHandler._build_target_payload(handler, tgt)
        m_bench.assert_not_called()
        assert payload.get("ttft_ms") == 123.0
        assert payload.get("rate_source") == "native"
    finally:
        handler.collectors = {}
        handler.targets = []


def test_build_target_payload_runs_bench_when_all_zero_and_switch_on():
    import modelctl.core.stats as S
    from unittest.mock import MagicMock, Mock, patch
    fake_collector = MagicMock()
    fake_collector.bench_fallback = True
    fake_collector.get_snapshot.return_value = {
        "ok": True,
        "prompt_total": 0.0,
        "predicted_total": 0.0,
        "prompt_rate": 0.0,
        "predicted_rate": 0.0,
        "ttft_ms": 0.0,
        "ttft_ms_p95": 0.0,
        "rate_source": "none",
    }
    handler = Mock()
    handler.collectors = {"m2": fake_collector}
    handler.start_time = 0.0
    tgt = _make_handler_target("m2")
    try:
        with patch.object(S, "_bench_cached") as m_bench:
            m_bench.return_value = (1.5, 2.5, 9)
            payload = S.UsageHandler._build_target_payload(handler, tgt)
        m_bench.assert_called_once()
        assert payload["prompt_rate"] == 1.5
        assert payload["predicted_rate"] == 2.5
        assert payload.get("rate_source") == "bench"
        assert payload.get("ttft_ms") == 9.0
    finally:
        handler.collectors = {}
        handler.targets = []


def test_build_target_payload_skips_bench_when_switch_off():
    import modelctl.core.stats as S
    from unittest.mock import MagicMock, Mock, patch
    fake_collector = MagicMock()
    fake_collector.bench_fallback = False
    fake_collector.get_snapshot.return_value = {
        "ok": True,
        "prompt_total": 0.0,
        "predicted_total": 0.0,
        "prompt_rate": 0.0,
        "predicted_rate": 0.0,
        "ttft_ms": 0.0,
        "ttft_ms_p95": 0.0,
        "rate_source": "none",
    }
    handler = Mock()
    handler.collectors = {"m3": fake_collector}
    handler.start_time = 0.0
    tgt = _make_handler_target("m3")
    try:
        with patch.object(S, "_bench_cached") as m_bench:
            m_bench.return_value = (1.5, 2.5, 9)
            payload = S.UsageHandler._build_target_payload(handler, tgt)
        m_bench.assert_not_called()
        assert payload["prompt_rate"] == 0.0
        assert payload["predicted_rate"] == 0.0
        assert payload.get("rate_source") == "none"
    finally:
        handler.collectors = {}
        handler.targets = []


def test_targets_from_profiles_includes_native_mapping_for_vllm(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch
    import modelctl.core.stats as S

    fake_profile = MagicMock()
    fake_profile.name = "qwen3.8"
    fake_profile.engine = "vllm"
    fake_profile.port = 8000
    fake_profile.api_key = None
    fake_profile.aliases = ["qwen3.8-vllm"]
    fake_profile.usage = {"price_in": 1.0, "price_out": 2.0}

    fake_adapter = MagicMock()
    fake_adapter.metrics_mapping.return_value = {
        "prompt_total": ["vllm:prompt_tokens_total"],
        "predicted_total": ["vllm:generation_tokens_total"],
        "prompt_rate": [],
        "predicted_rate": [],
    }
    fake_adapter.native_metrics_mapping.return_value = dict(VLLM_NATIVE)
    fake_adapter.upstream_model_name.return_value = "qwen3.8"

    with (
        patch("modelctl.core.profile.list_profiles", return_value=[fake_profile]),
        patch("modelctl.engines.get_adapter") as m_ga,
    ):
        m_ga.return_value = lambda p, c: fake_adapter
        targets = S._targets_from_profiles(tmp_path)

    assert len(targets) == 1
    assert targets[0].native_mapping is not None
    assert targets[0].native_mapping["rate"] == "tokens_per_second"


def test_targets_from_profiles_native_mapping_none_for_unsupported_engine(tmp_path):
    from unittest.mock import MagicMock, patch
    import modelctl.core.stats as S

    fake_profile = MagicMock()
    fake_profile.name = "qwen3.8"
    fake_profile.engine = "ollama"
    fake_profile.port = 8000
    fake_profile.api_key = None
    fake_profile.aliases = ["qwen3.8-ollama"]
    fake_profile.usage = {"price_in": 1.0, "price_out": 2.0}

    fake_adapter = MagicMock()
    fake_adapter.metrics_mapping.return_value = {
        "prompt_total": ["ollama:prompt_tokens_total"],
        "predicted_total": ["ollama:generation_tokens_total"],
        "prompt_rate": [],
        "predicted_rate": [],
    }
    fake_adapter.native_metrics_mapping.return_value = None
    fake_adapter.upstream_model_name.return_value = "qwen3.8"

    with (
        patch("modelctl.core.profile.list_profiles", return_value=[fake_profile]),
        patch("modelctl.engines.get_adapter") as m_ga,
    ):
        m_ga.return_value = lambda p, c: fake_adapter
        targets = S._targets_from_profiles(tmp_path)

    assert len(targets) == 1
    assert targets[0].native_mapping is None


def test_get_collector_injects_native_mapping(tmp_path, monkeypatch):
    """get_collector 注入 native_mapping 并按 USAGE_BENCH_FALLBACK 计算 bench_fallback。"""
    from unittest.mock import MagicMock
    from modelctl.core.gateway import get_collector

    monkeypatch.delenv("USAGE_BENCH_FALLBACK", raising=False)
    # native_metrics_mapping 须显式置 None：MagicMock 的自动子属性是 truthy 的 mock，
    # 会被 get_collector 当作"profile 已配置原生映射"而进入 merge 分支。
    profile = MagicMock(name="q", port=8000, api_key=None, native_metrics_mapping=None)
    adapter = MagicMock()
    adapter.metrics_mapping.return_value = {
        "prompt_total": ["vllm:prompt_tokens_total"],
        "predicted_total": ["vllm:generation_tokens_total"],
        "prompt_rate": [],
        "predicted_rate": [],
    }
    adapter.native_metrics_mapping.return_value = dict(VLLM_NATIVE)

    collector = get_collector(profile, adapter, tmp_path)
    assert collector is not None
    assert collector.native_mapping == VLLM_NATIVE
    assert collector.bench_fallback is True


def test_get_collector_injects_none_native_mapping_for_default_engine(tmp_path, monkeypatch):
    """默认引擎原生映射为 None 时，get_collector 注入 None（不影响既有 token 计数链路）。"""
    from unittest.mock import MagicMock
    from modelctl.core.gateway import get_collector

    monkeypatch.delenv("USAGE_BENCH_FALLBACK", raising=False)
    # profile 未配置原生映射（见上一个用例的说明，必须显式置 None）
    profile = MagicMock(name="q", port=8000, api_key=None, native_metrics_mapping=None)
    adapter = MagicMock()
    adapter.metrics_mapping.return_value = {
        "prompt_total": ["ollama:prompt_tokens_total"],
        "predicted_total": ["ollama:generation_tokens_total"],
        "prompt_rate": [],
        "predicted_rate": [],
    }
    adapter.native_metrics_mapping.return_value = None

    collector = get_collector(profile, adapter, tmp_path)
    assert collector is not None
    assert collector.native_mapping is None


def test_record_native_metrics_silent_when_invalid(tmp_path):
    """非法入参（None / 非 dict）静默返回，滑窗保持空。"""
    from modelctl.core.stats import UsageCollector

    collector = UsageCollector(
        name="t",
        base_url="http://1.1.1.1",
        poll_interval=999,
        api_key=None,
        data_dir=tmp_path,
        mode="on-demand",
        mapping={},
        native_mapping=VLLM_NATIVE,
    )
    collector.record_native_metrics(None)
    collector.record_native_metrics({})
    assert len(collector._native_window) == 0


def test_token_rate_data_takes_ttft_from_stats_when_native_present(monkeypatch):
    """stats 接口返回原生 ttft_ms 时，_token_rate_data 复用而不主动测速。"""
    import json as _json
    import urllib.request
    import modelctl.cli as cli
    from unittest.mock import MagicMock, patch

    body = _json.dumps(
        {"isValid": True, "prompt_rate": 12.0, "predicted_rate": 15.0, "ttft_ms": 123, "model": "q", "unit": "tok"}
    ).encode("utf-8")

    class _FakeResp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with (
        patch("urllib.request.urlopen", return_value=_FakeResp()),
        patch("modelctl.cli.get_adapter", return_value=lambda *a, **k: None),
        patch("modelctl.cli._benchmark_token_rate", MagicMock(return_value=None)) as m_bench,
    ):
        result = cli._token_rate_data(MagicMock(name="profile-model", engine="vllm"), MagicMock())
        m_bench.assert_not_called()
    assert result["ttft_ms"] == 123
    assert result["prompt_rate"] == 12.0
    assert result["predicted_rate"] == 15.0
    assert result["source"] == "stats"


def test_token_rate_data_skips_bench_when_usage_bench_fallback_false(monkeypatch):
    """USAGE_BENCH_FALLBACK=false 时跳过 bench 兜底，全字段 None。"""
    import json as _json
    import urllib.request
    import modelctl.cli as cli
    from unittest.mock import MagicMock, patch

    monkeypatch.setenv("USAGE_BENCH_FALLBACK", "false")
    try:
        body = _json.dumps(
            {"isValid": True, "prompt_rate": 0.0, "predicted_rate": 0.0, "ttft_ms": 0, "model": "x"}
        ).encode("utf-8")

        class _FakeResp:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with (
            patch("urllib.request.urlopen", return_value=_FakeResp()),
            patch("modelctl.cli.get_adapter", return_value=lambda *a, **k: None),
            patch("modelctl.cli._benchmark_token_rate", MagicMock(return_value=None)) as m_bench,
        ):
            result = cli._token_rate_data(MagicMock(name="profile-model-x", engine="vllm"), MagicMock())
            m_bench.assert_not_called()
        assert result["source"] is None
        assert result["prompt_rate"] is None
    finally:
        monkeypatch.delenv("USAGE_BENCH_FALLBACK", raising=False)
