"""modelctl.core.stats 单元测试（多引擎指标映射 + 用量折算）。"""

import json
import time

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
