"""startup_progress 单测：pull 解析 / 模式表 / EMA / 快照 / 阶段序列。"""
from __future__ import annotations

import json

import pytest

from modelctl.core.startup_progress import PullParser, PullUpdate


def test_pull_parser_counts_layers():
    p = PullParser("img:tag")
    assert p.feed("latest: Pulling from vllm/vllm-openai") is None
    u1 = p.feed("aaa: Pulling fs layer")
    assert isinstance(u1, PullUpdate) and u1.total_layers == 1
    p.feed("bbb: Pulling fs layer")
    # 半层下载：512MB/1GB ≈ 0.5
    u = p.feed("aaa: Downloading 512MB/1GB")
    assert u is not None and 0.2 < u.pct < 0.3  # (0.5*0.5)/1... 两半层 → 半*0.5/2
    p.feed("aaa: Download complete")
    p.feed("aaa: Pull complete")
    u2 = p.feed("bbb: Pull complete")
    assert u2.pct == 1.0 and u2.done_layers == 2 and u2.total_layers == 2


def test_pull_parser_already_exists_counts_done():
    p = PullParser("img:tag")
    p.feed("aaa: Already exists")
    u = p.feed("bbb: Pull complete")
    assert u.done_layers == 2 and u.total_layers == 2 and u.pct == 1.0


def test_pull_parser_reset_clears_layers():
    p = PullParser("img:tag")
    p.feed("aaa: Pulling fs layer")
    p.feed("aaa: Pull complete")
    p.reset()
    u = p.feed("zzz: Pulling fs layer")
    assert u.done_layers == 0 and u.total_layers == 1


def test_pull_parser_bytes_units_and_extracting():
    p = PullParser("img")
    p.feed("aaa: Pulling fs layer")
    p.feed("aaa: Downloading 100kB/1MB")
    p.feed("aaa: Download complete")
    # extracting 半程：权重 0.5 + 0.5*0.5 = 0.75
    u = p.feed("aaa: Extracting 500kB/1MB")
    assert 0.7 < u.pct < 0.8


def test_pct_clamped_when_cur_exceeds_total():
    # docker 偶发 cur > total（如 1.5GB/1GB）：pct 必须夹紧在 [0,1]，否则下游 eta 为负
    p = PullParser("img")
    p.feed("aaa: Pulling fs layer")
    u = p.feed("aaa: Downloading 1.5GB/1GB")
    assert u is not None and 0.0 <= u.pct <= 1.0


def test_pct_monotonic_across_download_complete():
    # 单层完整生命周期逐行喂入：pct 序列必须单调不减（进度条不许倒退）
    p = PullParser("img")
    lines = [
        "aaa: Pulling fs layer",
        "aaa: Downloading 900MB/1GB",
        "aaa: Downloading 1GB/1GB",
        "aaa: Download complete",
        "aaa: Extracting 0bytes/1GB",
        "aaa: Extracting 1GB/1GB",
        "aaa: Pull complete",
    ]
    pcts = [u.pct for u in (p.feed(x) for x in lines)]
    assert all(a <= b for a, b in zip(pcts, pcts[1:], strict=False)), pcts


def test_timing_cold_start_returns_none(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    t = StartupTiming(path=tmp_path / "timing.json")
    assert t.eta("vllm", "prepare_env", 0.5) is None


def test_timing_record_then_eta_scales_with_pct(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    t = StartupTiming(path=tmp_path / "timing.json")
    t.record("vllm", "prepare_env", 100.0)
    # pct=0.5 → 剩余 50；pct=None → 全量 100
    assert t.eta("vllm", "prepare_env", 0.5) == 50
    assert t.eta("vllm", "prepare_env", None) == 100


def test_timing_persists_across_instances(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    p = tmp_path / "timing.json"
    StartupTiming(path=p).record("vllm", "loading", 200.0)
    assert StartupTiming(path=p).eta("vllm", "loading", 0.0) == 200


def test_timing_corrupt_file_survives(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    p = tmp_path / "timing.json"
    p.write_text("{ not json", encoding="utf-8")
    t = StartupTiming(path=p)
    assert t.eta("vllm", "loading", 0.5) is None
    t.record("vllm", "loading", 10.0)  # 损坏文件被忽略后仍可写
    assert t.eta("vllm", "loading", 0.5) == 5


@pytest.mark.parametrize("payload", [
    '{"vllm:loading": "oops"}',                      # 值非 dict → AttributeError
    '{"vllm:loading": {"ema_s": NaN, "n": 3}}',      # NaN → int() ValueError
    '{"vllm:loading": {"ema_s": Infinity, "n": 2}}', # 非有限
    '{"vllm:loading": {"n": 9}}',                    # 缺 ema_s → EMA 分支 KeyError
    '{"vllm:loading": {"ema_s": 5.0, "n": "3"}}',    # n 为字符串 → TypeError
])
def test_timing_malformed_records_never_raise(tmp_path, payload):
    # 结构畸形但 JSON 合法的记录：eta/record 均不得抛异常，坏记录被丢弃
    from modelctl.core.startup_progress import StartupTiming

    p = tmp_path / "timing.json"
    p.write_text(payload, encoding="utf-8")
    t = StartupTiming(path=p)
    assert t.eta("vllm", "loading", 0.5) is None
    t.record("vllm", "loading", 10.0)  # 坏记录被过滤后仍可正常记录
    assert t.eta("vllm", "loading", 0.5) == 5


def test_timing_invalid_utf8_file_survives(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    p = tmp_path / "timing.json"
    p.write_bytes(b'{"vllm:loading": {"ema_s": 1.0, "n": 1}} \xff\xfe')
    t = StartupTiming(path=p)  # UnicodeDecodeError 不得冒泡
    assert t.eta("vllm", "loading", 0.5) is None


def test_timing_record_ignores_bad_samples(tmp_path):
    # 非正/非有限耗时样本必须被忽略：既不产生 ETA，也不得污染已有基线
    from modelctl.core.startup_progress import StartupTiming

    p = tmp_path / "timing.json"
    t = StartupTiming(path=p)
    for bad in (-50.0, 0.0, float("nan"), float("inf")):
        t.record("vllm", "loading", bad)
        assert t.eta("vllm", "loading", 0.5) is None
    assert not p.exists()  # 全坏样本不应落盘

    t.record("vllm", "loading", 100.0)
    assert t.eta("vllm", "loading", 0.5) == 50
    t.record("vllm", "loading", -50.0)  # 坏样本不得污染基线
    assert t.eta("vllm", "loading", 0.5) == 50


def test_timing_ema_takes_over_after_min_samples(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    p = tmp_path / "timing.json"
    t = StartupTiming(path=p)
    for _ in range(5):
        t.record("vllm", "loading", 100.0)
    t.record("vllm", "loading", 550.0)
    # n≥5 后转 EMA(α=0.4)：0.4*550 + 0.6*100 = 280，且跨实例持久化
    assert StartupTiming(path=p).eta("vllm", "loading", 0.0) == 280
    assert json.loads(p.read_text(encoding="utf-8"))["vllm:loading"]["n"] == 6
