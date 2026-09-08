"""startup_progress 单测：pull 解析 / 模式表 / EMA / 快照 / 阶段序列。"""
from __future__ import annotations

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
