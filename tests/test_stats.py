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


def test_build_payload_includes_rate_fields():
    from modelctl.core.stats import build_usage_payload
    import time
    tokens = {"prompt_total": 1000, "predicted_total": 500, "prompt_rate": 10.0, "predicted_rate": 5.0}
    payload = build_usage_payload(tokens, {"price_in": 1.0, "price_out": 2.0}, start_time=time.time() - 10, now=time.time())
    assert payload["prompt_rate"] == 10.0
    assert payload["predicted_rate"] == 5.0
    assert "生成速率 5.0 tok/s" in payload["extra"]


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
