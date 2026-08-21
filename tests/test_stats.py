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


def test_usage_collector_prefers_gauge_over_window(tmp_path):
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
    # 预填窗口制造确定性的窗口速率：若回填错误覆盖 gauge，会得到 400/600 tok/s
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
    # gauge > 0 时优先使用 gauge，而不是窗口计算速率
    assert snap["prompt_rate"] == 12.0
    assert snap["predicted_rate"] == 42.0


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
