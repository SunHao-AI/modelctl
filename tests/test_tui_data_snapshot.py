#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_tui_data_snapshot.py
# @IDE    : PyCharm
# @Author : modelctl
# @Email  : modelctl@example.com
# @Date   : 2026-09-10
# @Desc   : TUI 5 种 Snapshot 数据层测试（fetch 字段 / TTL / revalidate / 空值降级）
# ===============================================================================

"""TUI 5 种 Snapshot 数据层测试（Task 2 Phase A）。

覆盖：
- HardwareSnapshot / ModelsSnapshot / LogsSnapshot / ClusterSnapshot / MonitorSnapshot
  的 `fetch()` 字段填充断言
- TTL 过期语义：`is_expired()` 在 ttl 内 False / 过期后 True
- `mark_fresh()` 重置 `_fetched_at`
- `revalidate_if_expired()` 过期才重采（只调一次 mock）
- 空值 fallback：probe 0 GPU → gpus=[]；list_profiles 空 → profiles=[]；launch_log 缺失 → lines=[]
"""

from __future__ import annotations

import time
from unittest import mock

from modelctl.core.tui.data import (
    ClusterSnapshot,
    HardwareSnapshot,
    LogsSnapshot,
    ModelsSnapshot,
    MonitorSnapshot,
)

# ─────────────────────────────────────────────────────────
# helper：统一的 monotonic now
# ─────────────────────────────────────────────────────────


def _now() -> float:
    return time.monotonic()


# ─────────────────────────────────────────────────────────
# HardwareSnapshot
# ─────────────────────────────────────────────────────────


def _cap_mock(gpu_count: int = 2):
    return mock.Mock(
        gpu_count=gpu_count,
        gpu_indices=list(range(gpu_count)),
        gpu_name="RTX 4090",
        vram_total_mb_per_gpu=[24576] * gpu_count,
        vram_free_mb=[22000] * gpu_count,
        compute_capability="8.9",
        binaries={"vllm": True, "llamacpp": True},
        binary_paths={"vllm": "/bin/vllm", "llamacpp": "/bin/llama-server"},
    )


def test_hardware_snapshot_fetch_populates_fields():
    """调 probe() 后 gpus/binaries/cpu_info 正确填充。"""
    with mock.patch("modelctl.core.tui.data.probe", return_value=_cap_mock(gpu_count=2)) as _p:
        snap = HardwareSnapshot.fetch(now=_now())
    assert _p.call_count == 1
    assert len(snap.gpus) == 2
    assert snap.gpus[0]["name"] == "RTX 4090"
    assert snap.gpus[0]["free_mb"] == 22000
    assert snap.gpus[0]["total_mb"] == 24576
    assert snap.gpus[0]["util_pct"] == 0
    assert snap.gpus[1]["index"] == 1
    assert snap.binaries == {"vllm": "/bin/vllm", "llamacpp": "/bin/llama-server"}
    assert snap.cpu_info == "CC 8.9"
    assert snap.probe_errors == []
    assert snap._fetched_at is not None


def test_hardware_snapshot_zero_gpu():
    """probe 0 GPU → gpus=[]（vram 列表为空，逐卡缺省）。"""
    cap = mock.Mock(
        gpu_count=0, gpu_indices=[], gpu_name="", vram_total_mb_per_gpu=[],
        vram_free_mb=[], compute_capability="", binaries={}, binary_paths={},
    )
    with mock.patch("modelctl.core.tui.data.probe", return_value=cap):
        snap = HardwareSnapshot.fetch(now=_now())
    assert snap.gpus == []
    assert snap.binaries == {}
    assert snap.cpu_info == ""


def test_hardware_snapshot_ttl():
    """TTL=60s：现 fetch 立即未过期，61s 后过期。"""
    cap = _cap_mock(gpu_count=1)
    with mock.patch("modelctl.core.tui.data.probe", return_value=cap):
        snap = HardwareSnapshot.fetch(now=_now())
    assert snap.ttl >= 60.0
    now = _now()
    assert not snap.is_expired(now=now)
    assert snap.is_expired(now=now + 61)


def test_hardware_snapshot_revalidate_if_expired():
    """revalidate_if_expired 过期才重采，单次过期触发一次。"""
    cap = _cap_mock(gpu_count=1)
    with mock.patch("modelctl.core.tui.data.probe", return_value=cap) as p:
        snap = HardwareSnapshot.fetch()
        p.reset_mock()
        assert not snap.is_expired(now=_now())
        # 未过期：不应重采
        snap.revalidate_if_expired(now=_now())
        assert p.call_count == 0
        # 过期：应重采一次
        snap.revalidate_if_expired(now=_now() + 61)
        assert p.call_count == 1
        p.reset_mock()
        # 重采后恢复 fresh：同帧内再次 revalidate 不应再采
        snap.revalidate_if_expired(now=_now() + 61)
        assert p.call_count == 0


def test_hardware_snapshot_mark_fresh():
    """mark_fresh 重置 _fetched_at，使原本过期的快照刷新为未过期。"""
    with mock.patch("modelctl.core.tui.data.probe", return_value=_cap_mock(1)):
        snap = HardwareSnapshot.fetch(now=_now())
    # 设到过期
    assert snap.is_expired(now=_now() + 61)
    snap.mark_fresh(now=_now())
    assert not snap.is_expired(now=_now() + 60.5)


# ─────────────────────────────────────────────────────────
# ModelsSnapshot
# ─────────────────────────────────────────────────────────


def _profile_mock(name: str, engine: str, port: int, vram: float):
    """构造 profile mock。

    `mock.Mock(name=...)` 会把 `name` 当作 mock 的**标签**而非属性；
    这里显式注入 `p.name = name` 保证 `getattr(p, "name")` 返回给定字符串。
    """
    p = mock.Mock(
        engine=engine,
        variants=[mock.Mock(vram_gib=vram)],
        port=port,
    )
    p.name = name
    return p


def test_models_snapshot_fetch_populates():
    """list_profiles + is_running_any + _stats_token_rate 联合填充。"""
    vllm = _profile_mock("demo-vllm", "vllm", 8866, 30.0)
    llama = _profile_mock("demo-llamacpp", "llamacpp", 8867, 40.0)
    with mock.patch("modelctl.core.tui.data.list_profiles", return_value=[vllm, llama]), \
         mock.patch("modelctl.core.tui.data.is_running_any", side_effect=[True, False]), \
         mock.patch("modelctl.cli._stats_token_rate", side_effect=[(155.0, 33.0), None]):
        snap = ModelsSnapshot.fetch(now=_now())
    assert len(snap.profiles) == 2
    p0 = snap.profiles[0]
    assert p0["name"] == "demo-vllm"
    assert p0["engine"] == "vllm"
    assert p0["port"] == 8866
    assert p0["status"] == "running"
    assert p0["vram_gib"] == 30.0
    assert p0["rate_in"] == 155.0
    assert p0["rate_out"] == 33.0
    p1 = snap.profiles[1]
    assert p1["name"] == "demo-llamacpp"
    assert p1["status"] == "stopped"
    assert p1["rate_in"] is None
    assert p1["rate_out"] is None
    assert snap._fetched_at is not None


def test_models_snapshot_empty_profiles():
    """list_profiles 空 → profiles=[]。"""
    with mock.patch("modelctl.core.tui.data.list_profiles", return_value=[]):
        snap = ModelsSnapshot.fetch(now=_now())
    assert snap.profiles == []


def test_models_snapshot_ttl():
    """TTL=8s：现未过期，9s 后过期。"""
    vllm = _profile_mock("demo-vllm", "vllm", 8866, 30.0)
    with mock.patch("modelctl.core.tui.data.list_profiles", return_value=[vllm]), \
         mock.patch("modelctl.core.tui.data.is_running_any", return_value=True), \
         mock.patch("modelctl.cli._stats_token_rate", return_value=(10.0, 5.0)):
        snap = ModelsSnapshot.fetch(now=_now())
    assert snap.ttl >= 8.0
    now = _now()
    assert not snap.is_expired(now=now)
    assert snap.is_expired(now=now + 9)


# ─────────────────────────────────────────────────────────
# LogsSnapshot
# ─────────────────────────────────────────────────────────


def test_logs_snapshot_short_file(tmp_path):
    """launch_log 返回末 2 行（读文本 splitlines()[-2:]）。

    文件仅 2 行（恰好 dash 显示容量）→ lines 全量，truncated=False。
    """
    log_path = tmp_path / "launch-demo.log"
    log_path.write_text("line2\nline3\n", encoding="utf-8")
    with mock.patch("modelctl.core.tui.data.launch_log", return_value=log_path):
        snap = LogsSnapshot.fetch(name="demo", now=_now())
    assert snap.lines == ["line2", "line3"]
    assert snap.truncated is False


def test_logs_snapshot_missing_file():
    """launch_log 返 None（文件不存在）→ lines=[] / truncated=False，不抛错。"""
    with mock.patch("modelctl.core.tui.data.launch_log", return_value=None):
        snap = LogsSnapshot.fetch(name="ghost", now=_now())
    assert snap.lines == []
    assert snap.truncated is False


def test_logs_snapshot_truncated_when_long(tmp_path):
    """内容超过 2 行 → truncated=True。"""
    log_path = tmp_path / "launch-demo.log"
    log_path.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    with mock.patch("modelctl.core.tui.data.launch_log", return_value=log_path):
        snap = LogsSnapshot.fetch(name="demo", now=_now())
    assert snap.lines == ["d", "e"]
    assert snap.truncated is True


# ─────────────────────────────────────────────────────────
# ClusterSnapshot
# ─────────────────────────────────────────────────────────


def test_cluster_snapshot_fetch_success():
    """_cluster_aggregate 正常 → nodes/center_visible 填充。"""
    nodes = [{"node_id": "w-210", "status": "online"}, {"node_id": "w-100", "status": "offline"}]
    goals = [{"profile": "demo-vllm", "intent": "start"}]
    with mock.patch("modelctl.cli._cluster_aggregate", return_value=(nodes, goals, "")):
        snap = ClusterSnapshot.fetch(now=_now())
    assert snap.nodes == nodes
    assert snap.goals == goals
    assert snap.center_visible == ""


def test_cluster_snapshot_fallback():
    """_cluster_aggregate 异常 → nodes=[] / goals=[] / center_visible=降级文案。"""
    with mock.patch("modelctl.cli._cluster_aggregate", side_effect=OSError("timeout")):
        snap = ClusterSnapshot.fetch(now=_now())
    assert snap.nodes == []
    assert snap.goals == []
    assert snap.center_visible == "(中心不可达)"


def test_cluster_snapshot_ttl():
    """TTL=30s。"""
    with mock.patch("modelctl.cli._cluster_aggregate", return_value=([], [], "")):
        snap = ClusterSnapshot.fetch(now=_now())
    assert snap.ttl >= 30.0
    now = _now()
    assert not snap.is_expired(now=now)
    assert snap.is_expired(now=now + 31)


# ─────────────────────────────────────────────────────────
# MonitorSnapshot
# ─────────────────────────────────────────────────────────


def test_monitor_snapshot_fetch():
    """MonitorSnapshot 仅速率列，全 profile 遍历 _stats_token_rate。"""
    vllm = _profile_mock("demo-vllm", "vllm", 8866, 30.0)
    llama = _profile_mock("demo-llamacpp", "llamacpp", 8867, 40.0)
    with mock.patch("modelctl.core.tui.data.list_profiles", return_value=[vllm, llama]), \
         mock.patch("modelctl.cli._stats_token_rate", side_effect=[(155.0, 33.0), None]):
        snap = MonitorSnapshot.fetch(now=_now())
    assert len(snap.info) == 2
    assert snap.info[0]["name"] == "demo-vllm"
    assert snap.info[0]["rate_in"] == 155.0
    assert snap.info[0]["rate_out"] == 33.0
    assert snap.info[1]["name"] == "demo-llamacpp"
    assert snap.info[1]["rate_in"] is None
    assert snap.info[1]["rate_out"] is None


def test_monitor_snapshot_ttl():
    """TTL=5s。"""
    with mock.patch("modelctl.core.tui.data.list_profiles", return_value=[_profile_mock("a", "vllm", 8000, 30.0)]), \
         mock.patch("modelctl.cli._stats_token_rate", return_value=(1.0, 0.5)):
        snap = MonitorSnapshot.fetch(now=_now())
    assert snap.ttl >= 5.0
    assert snap.ttl <= 6.0
    now = _now()
    assert not snap.is_expired(now=now)
    assert snap.is_expired(now=now + 6)
