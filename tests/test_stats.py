"""modelctl.core.stats 单元测试（多引擎指标映射 + 用量折算）。"""

import json
import time
import urllib.request
from unittest.mock import patch

from modelctl.core.stats import build_usage_payload, parse_metrics

LLAMACPP_MAPPING = {
    "prompt_total": ["llamacpp:prompt_tokens_total"],
    "predicted_total": ["llamacpp:tokens_predicted_total"],
    "prompt_rate": ["llamacpp:prompt_tokens_seconds"],
    "predicted_rate": ["llamacpp:predicted_tokens_seconds"],
}
VLLM_MAPPING = {
    "prompt_total": ["vllm:prompt_tokens_total"],
    "predicted_total": ["vllm:generation_tokens_total"],
    "prompt_rate": [],
    "predicted_rate": [],
}

METRICS_TEXT = """
# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed
llamacpp:prompt_tokens_total 1234
llamacpp:tokens_predicted_total 5678
llamacpp:prompt_tokens_seconds 100.5
llamacpp:predicted_tokens_seconds 55.0
"""


def test_parse_metrics_llamacpp():
    got = parse_metrics(METRICS_TEXT, LLAMACPP_MAPPING)
    assert got["prompt_total"] == 1234
    assert got["predicted_total"] == 5678
    assert got["prompt_rate"] == 100.5
    assert got["predicted_rate"] == 55.0


def test_parse_metrics_vllm_no_rate():
    got = parse_metrics("vllm:prompt_tokens_total 10\nvllm:generation_tokens_total 20\n", VLLM_MAPPING)
    assert got["prompt_total"] == 10
    assert got["predicted_total"] == 20
    assert got["prompt_rate"] == 0.0
    assert got["predicted_rate"] == 0.0


def test_build_payload_with_budget():
    tokens = {"prompt_total": 1_000_000, "predicted_total": 500_000, "prompt_rate": 0.0, "predicted_rate": 0.0}
    payload = build_usage_payload(
        tokens, {"price_in": 1.0, "price_out": 2.0, "budget": 100}, start_time=time.time() - 60, now=time.time()
    )
    # 1M 输入 × 1元/M + 0.5M 输出 × 2元/M = 2 元
    assert payload["used"] == 2.0
    assert payload["total"] == 100
    assert payload["remaining"] == 98.0
    assert payload["isValid"] is True
    assert payload["unit"] == "CNY"


def test_build_payload_no_budget():
    tokens = {"prompt_total": 0, "predicted_rate": 0.0, "prompt_rate": 0.0, "predicted_total": 0}
    payload = build_usage_payload(tokens, {"price_in": 1.0, "price_out": 2.0}, start_time=time.time(), now=time.time())
    # 现版语义：无预算时 total/remaining 为 None（字段仍存在）
    assert payload["total"] is None
    assert payload["remaining"] is None


def test_fmt_tokens_units():
    from modelctl.core.stats import _fmt_tokens

    assert _fmt_tokens(648_532) == "648.5k"
    assert _fmt_tokens(499_300) == "499.3k"
    assert _fmt_tokens(149_232) == "149.2k"
    assert _fmt_tokens(1_500_000) == "1.50m"
    assert _fmt_tokens(2_000_000_000) == "2.00g"
    assert _fmt_tokens(999) == "999"
    assert _fmt_tokens(-1500) == "-1.5k"


def test_build_payload_includes_rate_fields():
    import time

    from modelctl.core.stats import build_usage_payload

    tokens = {"prompt_total": 1000, "predicted_total": 500, "prompt_rate": 10.0, "predicted_rate": 5.0}
    payload = build_usage_payload(
        tokens,
        {"price_in": 1.0, "price_out": 2.0},
        start_time=time.time() - 10,
        now=time.time(),
    )
    assert payload["prompt_rate"] == 10.0
    assert payload["predicted_rate"] == 5.0
    assert "累计 1.5k toks" in payload["extra"]
    assert "输入 1.0k/输出 500" in payload["extra"]
    assert "输入速率 10.0 tok/s" in payload["extra"]
    assert "输出速率 5.0 tok/s" in payload["extra"]
    assert "运行" not in payload["extra"]  # 移除运行时间等非必要信息


def test_usage_collector_loads_persisted_totals(tmp_path):
    data_dir = tmp_path / "cache"
    data_dir.mkdir()
    (data_dir / "demo.json").write_text(
        '{"prompt_total": 100, "predicted_total": 200, "updated_at": 1.0}',
        encoding="utf-8",
    )
    from modelctl.core.stats import UsageCollector
    collector = UsageCollector(
        name="demo",
        base_url="http://127.0.0.1:8000",
        poll_interval=5,
        api_key=None,
        data_dir=data_dir,
        mode="poll",
        mapping={},
    )
    snap = collector.snapshot()
    assert snap["prompt_total"] == 100.0
    assert snap["predicted_total"] == 200.0


def test_usage_collector_falls_back_on_non_dict_cache(tmp_path):
    # 损坏缓存：合法 JSON 但非 dict（如数组）——构造不应抛异常，累计基线回退为 0
    data_dir = tmp_path / "cache"
    data_dir.mkdir()
    (data_dir / "demo.json").write_text("[1,2,3]", encoding="utf-8")
    from modelctl.core.stats import UsageCollector
    collector = UsageCollector(
        name="demo",
        base_url="http://127.0.0.1:8000",
        poll_interval=5,
        api_key=None,
        data_dir=data_dir,
        mode="poll",
        mapping={},
    )
    snap = collector.snapshot()
    assert snap["prompt_total"] == 0.0
    assert snap["predicted_total"] == 0.0


def test_usage_collector_persists_totals(tmp_path):
    data_dir = tmp_path / "cache"
    data_dir.mkdir()
    from modelctl.core.stats import UsageCollector
    collector = UsageCollector(
        name="demo",
        base_url="http://127.0.0.1:8000",
        poll_interval=5,
        api_key=None,
        data_dir=data_dir,
        mode="on-demand",
        mapping={},
    )
    # 直接修改基线模拟轮询结果
    collector._baseline = {"prompt_total": 300.0, "predicted_total": 500.0}
    collector._persist(300.0, 500.0)
    content = (data_dir / "demo.json").read_text(encoding="utf-8")
    data = json.loads(content)
    assert data["prompt_total"] == 300.0
    assert data["predicted_total"] == 500.0
    assert "updated_at" in data


def test_usage_collector_sliding_window_rate(tmp_path):
    data_dir = tmp_path / "cache"
    data_dir.mkdir()
    from modelctl.core.stats import UsageCollector
    collector = UsageCollector(
        name="demo",
        base_url="http://127.0.0.1:8000",
        poll_interval=5,
        api_key=None,
        data_dir=data_dir,
        mode="on-demand",
        mapping={},
    )
    now = 1000.0
    collector._record_window(now, 0.0, 0.0)
    collector._record_window(now + 1.0, 100.0, 50.0)
    pr, rr = collector._compute_window_rate()
    assert pr == 100.0
    assert rr == 50.0


def test_usage_collector_record_tokens_updates_totals_and_rate(tmp_path):
    """网关按真实请求累计 token：总数、滑窗速率与持久化都应更新（vLLM gauge 恒 0 的修复）。"""
    data_dir = tmp_path / "cache"
    data_dir.mkdir()
    from modelctl.core.stats import UsageCollector

    collector = UsageCollector(
        name="demo",
        base_url="http://127.0.0.1:8000",
        poll_interval=5,
        api_key=None,
        data_dir=data_dir,
        mode="on-demand",
        mapping={},
    )
    # 注入单调时钟，使滑窗速率可预期：t=1000 累计 (50, 20)，t=1002 累计 (110, 40)
    fake_clock = iter([1000.0, 1002.0])
    collector._monotonic = lambda: next(fake_clock)
    collector.record_tokens(50, 20)
    collector.record_tokens(60, 20)
    snap = collector.snapshot()
    assert snap["prompt_total"] == 110.0
    assert snap["predicted_total"] == 40.0
    # 滑窗速率 = 窗口内首尾差分 / 时间差：(110-50)/2 与 (40-20)/2
    assert snap["prompt_rate"] == 30.0
    assert snap["predicted_rate"] == 10.0
    assert snap["ok"] is True
    # 持久化文件已写入（与 stats 服务共用 data 目录）
    data = json.loads((data_dir / "demo.json").read_text(encoding="utf-8"))
    assert data["prompt_total"] == 110.0
    assert data["predicted_total"] == 40.0


def test_usage_collector_record_tokens_ignores_zero_delta(tmp_path):
    data_dir = tmp_path / "cache"
    data_dir.mkdir()
    from modelctl.core.stats import UsageCollector

    collector = UsageCollector(
        name="demo",
        base_url="http://127.0.0.1:8000",
        poll_interval=5,
        api_key=None,
        data_dir=data_dir,
        mode="on-demand",
        mapping={},
    )
    collector.record_tokens(0, 0)
    assert collector.snapshot()["prompt_total"] == 0.0
    assert collector.snapshot()["predicted_total"] == 0.0


def test_poll_once_prefers_persisted_gateway_totals(tmp_path):
    """回归：vLLM gauge 恒 0 时，stats 轮询须以网关写入的持久化累计为准并差分出真实速率。

    场景：网关把真实请求累计写入 data/cache/<name>.json，stats 服务轮询引擎 /metrics
    拿到的是 0；旧实现只看 metrics 导致累计/速率永远为 0。
    """
    data_dir = tmp_path / "cache"
    data_dir.mkdir()
    from modelctl.core.stats import UsageCollector

    collector = UsageCollector(
        name="demo",
        base_url="http://127.0.0.1:8000",
        poll_interval=5,
        api_key=None,
        data_dir=data_dir,
        mode="on-demand",
        mapping={
            "prompt_total": ["prompt_tokens_total"],
            "predicted_total": ["tokens_predicted_total"],
            "prompt_rate": ["prompt_tokens_seconds"],
            "predicted_rate": ["predicted_tokens_seconds"],
        },
    )

    class FakeResp:
        def __init__(self, body: str = "") -> None:
            self._body = body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._body

    def _write_json(prompt: float, predicted: float) -> None:
        (data_dir / "demo.json").write_text(
            json.dumps({"prompt_total": prompt, "predicted_total": predicted, "updated_at": time.time()}),
            encoding="utf-8",
        )

    # 第一轮：网关已累计 1000/500；引擎 /metrics 空（恒 0）→ 应以持久化值为准
    _write_json(1000.0, 500.0)
    with patch("modelctl.core.stats.time.monotonic", return_value=100.0), patch.object(
        urllib.request, "urlopen", return_value=FakeResp("")
    ):
        collector._poll_once()
    snap = collector.snapshot()
    assert snap["prompt_total"] == 1000.0
    assert snap["predicted_total"] == 500.0

    # 第二轮：网关继续累计到 1100/550（时间差 5s）→ 差分速率 20/10 tok/s
    _write_json(1100.0, 550.0)
    with patch("modelctl.core.stats.time.monotonic", return_value=105.0), patch.object(
        urllib.request, "urlopen", return_value=FakeResp("")
    ):
        collector._poll_once()
    snap = collector.snapshot()
    assert snap["prompt_total"] == 1100.0
    assert snap["prompt_rate"] == 20.0  # (1100-1000)/5
    assert snap["predicted_rate"] == 10.0  # (550-500)/5


def test_build_target_payload_benchmarks_when_idle(monkeypatch):
    """回归：窗口无流量（速率为 0）时用伪造请求测速兜底，cc-switch 不再恒显示 0。"""
    import time
    from unittest.mock import MagicMock

    from modelctl.core.stats import StatsTarget, UsageHandler

    target = StatsTarget(
        name="a",
        data_dir=None,
        metrics_url="http://127.0.0.1:8000/metrics",
        mapping={"predicted_total": ["x"]},
        usage_cfg={"price_in": 1.0, "price_out": 2.0},
        bench_url="http://127.0.0.1:8000/v1/chat/completions",
        bench_model="a",
    )
    UsageHandler.targets = [target]
    UsageHandler.start_time = time.time()
    mock_collector = MagicMock()
    mock_collector.get_snapshot.return_value = {
        "ok": True,
        "error": None,
        "prompt_total": 100.0,
        "predicted_total": 50.0,
        "prompt_rate": 0.0,
        "predicted_rate": 0.0,
    }
    UsageHandler.collectors = {"a": mock_collector}
    from modelctl.core.stats import _BENCH_CACHE

    _BENCH_CACHE.clear()
    monkeypatch.setattr("modelctl.core.stats._bench_cached", lambda t: (12.5, 88.0, 300))
    handler = UsageHandler.__new__(UsageHandler)
    payload = handler._resolve_payload("a")
    assert payload["prompt_rate"] == 12.5
    assert payload["predicted_rate"] == 88.0
    assert "输入速率 12.5 tok/s" in payload["extra"]
    assert "输出速率 88.0 tok/s" in payload["extra"]


def test_build_target_payload_skips_bench_when_active(monkeypatch):
    """窗口有真实流量（速率非 0）时不得用伪造请求覆盖真实速率。"""
    import time
    from unittest.mock import MagicMock

    from modelctl.core.stats import StatsTarget, UsageHandler

    target = StatsTarget(
        name="a",
        data_dir=None,
        metrics_url="http://127.0.0.1:8000/metrics",
        mapping={"predicted_total": ["x"]},
        usage_cfg={"price_in": 1.0, "price_out": 2.0},
        bench_url="http://127.0.0.1:8000/v1/chat/completions",
    )
    UsageHandler.targets = [target]
    UsageHandler.start_time = time.time()
    mock_collector = MagicMock()
    mock_collector.get_snapshot.return_value = {
        "ok": True,
        "error": None,
        "prompt_total": 100.0,
        "predicted_total": 50.0,
        "prompt_rate": 10.0,
        "predicted_rate": 5.0,
    }
    UsageHandler.collectors = {"a": mock_collector}

    def _should_not_call(t):
        raise AssertionError("有真实速率时不应触发伪造测速")

    monkeypatch.setattr("modelctl.core.stats._bench_cached", _should_not_call)
    handler = UsageHandler.__new__(UsageHandler)
    payload = handler._resolve_payload("a")
    assert payload["prompt_rate"] == 10.0
    assert payload["predicted_rate"] == 5.0


def test_benchmark_rates_parses_streaming_usage(monkeypatch):
    """主动测速：从流式响应的 usage 计算输入/输出速率与 TTFT。"""
    import io
    import urllib.request

    from modelctl.core.stats import benchmark_rates

    sse = (
        'data: {"id":"1","usage":{"prompt_tokens":4,"completion_tokens":2}}\n'
        'data: {"id":"1","usage":{"prompt_tokens":4,"completion_tokens":8}}\n'
        "data: [DONE]\n"
    )
    # t_start=0.0, t_ttft=0.5, t_end=2.5 → input=4/0.5=8.0, output=8/2.0=4.0, ttft=500ms
    clock = iter([0.0, 0.5, 2.5])
    monkeypatch.setattr("modelctl.core.stats.time.perf_counter", lambda: next(clock))
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: io.BytesIO(sse.encode("utf-8")))
    assert benchmark_rates("http://127.0.0.1:8101/v1/chat/completions", "k", "qwen3.8") == (8.0, 4.0, 500)


def test_benchmark_rates_returns_none_on_failure(monkeypatch):
    import urllib.error
    import urllib.request

    from modelctl.core.stats import benchmark_rates

    def _refuse(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)
    assert benchmark_rates("http://127.0.0.1:1/v1/chat/completions", None, "m") is None


def test_usage_collector_prefers_engine_rate_gauge(tmp_path):
    """引擎自带实时速率 gauge（vLLM 等）优先于窗口差分：直连模型端口绕过网关也能统计到。"""
    data_dir = tmp_path / "cache"
    data_dir.mkdir()
    from modelctl.core.stats import UsageCollector
    collector = UsageCollector(
        name="demo",
        base_url="http://127.0.0.1:8000",
        poll_interval=5,
        api_key=None,
        data_dir=data_dir,
        mode="on-demand",
        mapping={
            "prompt_total": ["prompt_tokens_total"],
            "predicted_total": ["tokens_predicted_total"],
            "prompt_rate": ["prompt_tokens_seconds"],
            "predicted_rate": ["predicted_tokens_seconds"],
        },
    )
    # 预填窗口制造一个与 gauge 不同的窗口速率（验证 gauge 优先）
    collector._record_window(1000.0, 0.0, 0.0)
    collector._record_window(1001.0, 60.0, 30.0)

    class FakeResp:
        def __init__(self, body: str) -> None:
            self._body = body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._body

    metrics_text = (
        "prompt_tokens_total 2000\n"
        "tokens_predicted_total 3000\n"
        "prompt_tokens_seconds 12.0\n"
        "predicted_tokens_seconds 42.0\n"
    )
    with patch("modelctl.core.stats.time.monotonic", return_value=1005.0), patch.object(
        urllib.request, "urlopen", return_value=FakeResp(metrics_text)
    ):
        collector._poll_once()

    snap = collector.snapshot()
    # 引擎 gauge 非 0 → 直接采用实时速率
    assert snap["prompt_rate"] == 12.0
    assert snap["predicted_rate"] == 42.0


def test_usage_collector_falls_back_to_window_rate(tmp_path):
    """引擎无速率 gauge（或为 0）时退化为滑动窗口差分速率。"""
    data_dir = tmp_path / "cache"
    data_dir.mkdir()
    from modelctl.core.stats import UsageCollector
    collector = UsageCollector(
        name="demo",
        base_url="http://127.0.0.1:8000",
        poll_interval=5,
        api_key=None,
        data_dir=data_dir,
        mode="on-demand",
        mapping={
            "prompt_total": ["prompt_tokens_total"],
            "predicted_total": ["tokens_predicted_total"],
            "prompt_rate": ["prompt_tokens_seconds"],
            "predicted_rate": ["predicted_tokens_seconds"],
        },
    )
    # 预填窗口制造确定性的窗口速率（引擎无 gauge，只能靠窗口差分）
    collector._record_window(1000.0, 0.0, 0.0)
    collector._record_window(1001.0, 60.0, 30.0)

    class FakeResp:
        def __init__(self, body: str) -> None:
            self._body = body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._body

    metrics_text = "prompt_tokens_total 2000\ntokens_predicted_total 3000\n"  # 无速率 gauge
    with patch("modelctl.core.stats.time.monotonic", return_value=1005.0), patch.object(
        urllib.request, "urlopen", return_value=FakeResp(metrics_text)
    ):
        collector._poll_once()

    snap = collector.snapshot()
    # 窗口差分：(2000-0)/(1005-1000)=400，(3000-0)/5=600
    assert snap["prompt_rate"] == 400.0
    assert snap["predicted_rate"] == 600.0


def test_resolve_payload_all_aggregates_targets():
    import time
    from unittest.mock import MagicMock

    from modelctl.core.stats import StatsTarget, UsageHandler

    t1 = StatsTarget(
        name="a",
        data_dir=None,
        metrics_url="http://127.0.0.1:8000/metrics",
        mapping={"predicted_total": ["x"]},
        usage_cfg={"price_in": 1.0, "price_out": 2.0, "budget": 100},
    )
    t2 = StatsTarget(
        name="b",
        data_dir=None,
        metrics_url="http://127.0.0.1:8001/metrics",
        mapping={"predicted_total": ["x"]},
        usage_cfg={"price_in": 1.0, "price_out": 2.0, "budget": 50},
    )
    UsageHandler.targets = [t1, t2]
    mock_snap = {
        "ok": True,
        "error": None,
        "prompt_total": 0.0,
        "predicted_total": 0.0,
        "prompt_rate": 0.0,
        "predicted_rate": 0.0,
    }
    mock_a = MagicMock()
    mock_a.get_snapshot.return_value = dict(mock_snap)
    mock_b = MagicMock()
    mock_b.get_snapshot.return_value = dict(mock_snap)
    UsageHandler.collectors = {"a": mock_a, "b": mock_b}
    UsageHandler.start_time = time.time()
    # 类名调用会缺少 self 绑定，用未初始化实例调用（_resolve_payload 只依赖类属性）
    handler = UsageHandler.__new__(UsageHandler)
    payload = handler._resolve_payload("all")
    assert payload["model"] == "all"
    assert payload["planName"] == "modelctl 聚合用量"
    assert payload["total"] == 150


def test_resolve_payload_single_target_still_works():
    import time
    from unittest.mock import MagicMock

    from modelctl.core.stats import StatsTarget, UsageHandler

    target = StatsTarget(
        name="a",
        data_dir=None,
        metrics_url="http://127.0.0.1:8000/metrics",
        mapping={"predicted_total": ["x"]},
        usage_cfg={"price_in": 1.0, "price_out": 2.0},
    )
    UsageHandler.targets = [target]
    UsageHandler.start_time = time.time()
    mock_collector = MagicMock()
    mock_collector.get_snapshot.return_value = {
        "ok": True,
        "error": None,
        "prompt_total": 100.0,
        "predicted_total": 50.0,
        "prompt_rate": 10.0,
        "predicted_rate": 5.0,
    }
    UsageHandler.collectors = {"a": mock_collector}
    handler = UsageHandler.__new__(UsageHandler)
    payload = handler._resolve_payload("a")
    assert payload["model"] == "a"
    assert payload["prompt_rate"] == 10.0


def test_resolve_payload_matches_alias():
    from modelctl.core.stats import StatsTarget, UsageHandler

    target = StatsTarget(
        name="deepseek-v4-flash-llamacpp",
        data_dir=None,
        metrics_url="http://127.0.0.1:18888/metrics",
        mapping=None,
        aliases=["deepseek-v4-flash"],
    )
    UsageHandler.targets = [target]
    UsageHandler.collectors = {}
    handler = UsageHandler.__new__(UsageHandler)
    # alias 命中 target（mapping=None → 该引擎不支持精确统计）
    assert handler._resolve_payload("deepseek-v4-flash") == {"error": "该引擎不支持精确统计"}
    # 未知名字 → 未知模型
    assert "未知模型" in handler._resolve_payload("ghost")["error"]


def test_run_server_passes_base_url_without_metrics_suffix(tmp_path, monkeypatch):
    """回归：UsageCollector 的 base_url 必须是根地址，否则 _poll_once 会拼出 /metrics/metrics。"""
    from modelctl.core import stats as stats_mod
    from modelctl.core.stats import StatsTarget

    captured: dict = {}

    class FakeCollector:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeServer:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    def fake_collector(*args, **kwargs):
        captured["base_url"] = args[1]
        return FakeCollector()

    monkeypatch.setenv("USAGE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(stats_mod, "UsageCollector", fake_collector)
    monkeypatch.setattr(stats_mod, "ThreadingHTTPServer", FakeServer)

    target = StatsTarget(
        name="x",
        data_dir=tmp_path,
        metrics_url="http://127.0.0.1:18888/metrics",
        mapping={"prompt_total": ["m"]},
    )
    stats_mod.run_server(targets=[target])
    assert captured["base_url"] == "http://127.0.0.1:18888"


def test_build_tier_item_percent_and_extra_json():
    from modelctl.core.stats import build_tier_item

    snap = {"prompt_total": 1_000_000, "predicted_total": 500_000}
    item = build_tier_item("demo", snap, {"price_in": 1.0, "price_out": 2.0, "budget": 100}, "label")
    # 1M 输入 × 1元/M + 0.5M 输出 × 2元/M = 2 元 → 预算 100 元的 2%
    assert item["used"] == 2.0
    assert item["planName"] == "demo"
    assert item["isValid"] is True
    data = json.loads(item["extra"])
    assert data["resetsAt"] is None
    assert data["planLabel"] == "label"


def test_build_tier_item_clamps_over_budget():
    from modelctl.core.stats import build_tier_item

    snap = {"prompt_total": 10_000_000, "predicted_total": 0}
    item = build_tier_item("demo", snap, {"price_in": 1.0, "price_out": 2.0, "budget": 10}, "x")
    assert item["used"] == 100.0


def test_build_tier_item_requires_valid_budget():
    from modelctl.core.stats import _budget_of, build_tier_item

    assert _budget_of({}) is None
    assert _budget_of({"budget": -5}) is None
    assert _budget_of({"budget": "abc"}) is None
    assert _budget_of({"budget": 100}) == 100.0
    try:
        build_tier_item("demo", {}, {"price_in": 1.0, "price_out": 2.0}, "x")
        raise AssertionError("应抛出 ValueError")
    except ValueError as error:
        assert "未配置有效预算" in str(error)


def _make_tier_handler(usage_cfg: dict, ok: bool = True):
    from unittest.mock import MagicMock

    from modelctl.core.stats import StatsTarget, UsageHandler

    target = StatsTarget(
        name="a",
        data_dir=None,
        metrics_url="http://127.0.0.1:8000/metrics",
        mapping={"predicted_total": ["x"]},
        usage_cfg=usage_cfg,
    )
    mock_collector = MagicMock()
    mock_collector.get_snapshot.return_value = {
        "ok": ok,
        "error": None if ok else "boom",
        "prompt_total": 1_000_000.0,
        "predicted_total": 500_000.0,
        "prompt_rate": 0.0,
        "predicted_rate": 0.0,
    }
    UsageHandler.targets = [target]
    UsageHandler.collectors = {"a": mock_collector}
    return UsageHandler.__new__(UsageHandler)


def test_resolve_tier_payload_single_target():
    handler = _make_tier_handler({"price_in": 1.0, "price_out": 2.0, "budget": 100})
    result = handler._resolve_tier_payload("a")
    assert isinstance(result, list) and len(result) == 1
    assert result[0]["planName"] == "a"
    assert result[0]["used"] == 2.0


def test_resolve_tier_payload_single_no_budget():
    handler = _make_tier_handler({"price_in": 1.0, "price_out": 2.0})
    result = handler._resolve_tier_payload("a")
    assert isinstance(result, dict)
    assert "未配置预算" in result["error"]


def test_resolve_tier_payload_all_aggregates_only_budged_targets():
    from unittest.mock import MagicMock

    from modelctl.core.stats import StatsTarget, UsageHandler

    t1 = StatsTarget(
        name="a",
        data_dir=None,
        metrics_url="http://127.0.0.1:8000/metrics",
        mapping={"predicted_total": ["x"]},
        usage_cfg={"price_in": 1.0, "price_out": 2.0, "budget": 100},
    )
    t2 = StatsTarget(
        name="b",
        data_dir=None,
        metrics_url="http://127.0.0.1:8001/metrics",
        mapping={"predicted_total": ["x"]},
        usage_cfg={"price_in": 1.0, "price_out": 2.0},  # 无预算 → 跳过
    )
    mock_a = MagicMock()
    mock_a.get_snapshot.return_value = {
        "ok": True,
        "error": None,
        "prompt_total": 1_000_000.0,
        "predicted_total": 500_000.0,
        "prompt_rate": 0.0,
        "predicted_rate": 0.0,
    }
    UsageHandler.targets = [t1, t2]
    UsageHandler.collectors = {"a": mock_a}
    handler = UsageHandler.__new__(UsageHandler)
    result = handler._resolve_tier_payload(None)
    assert isinstance(result, list) and len(result) == 1
    assert result[0]["planName"] == "a"


def test_resolve_tier_payload_all_no_budget_data():
    from modelctl.core.stats import StatsTarget, UsageHandler

    target = StatsTarget(
        name="a",
        data_dir=None,
        metrics_url="http://127.0.0.1:8000/metrics",
        mapping={"predicted_total": ["x"]},
        usage_cfg={},
    )
    UsageHandler.targets = [target]
    UsageHandler.collectors = {}
    handler = UsageHandler.__new__(UsageHandler)
    result = handler._resolve_tier_payload("all")
    assert isinstance(result, dict)
    assert "无可用预算数据" in result["error"]


def test_do_get_routes_view_tier_param():
    sent: dict = {}

    class FakeWfile:
        def write(self, body):
            sent["body"] = json.loads(body.decode("utf-8"))

    def wire(handler) -> None:
        handler.path = "/api/usage?model=a&view=tier"
        handler.send_response = lambda code: sent.update(code=code)
        handler.send_header = lambda key, value: None
        handler.end_headers = lambda: None
        handler.wfile = FakeWfile()

    # 有预算 → 200 + 徽章数组
    handler = _make_tier_handler({"price_in": 1.0, "price_out": 2.0, "budget": 100})
    wire(handler)
    handler.do_GET()
    assert sent["code"] == 200
    assert isinstance(sent["body"], list) and sent["body"][0]["planName"] == "a"

    # 无预算 → 503 + 错误对象（cc-switch 走查询失败分支）
    sent.clear()
    handler = _make_tier_handler({"price_in": 1.0, "price_out": 2.0})
    wire(handler)
    handler.do_GET()
    assert sent["code"] == 503
    assert "未配置预算" in sent["body"]["error"]
