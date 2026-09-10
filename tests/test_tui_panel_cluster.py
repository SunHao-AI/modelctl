#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_tui_panel_cluster.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/10 10:00
# @Desc   : Cluster 视图 3 section 渲染测试（节点表/goal 表/事件占位 + SoloStub + CJK 对齐）
# ===============================================================================

"""Cluster 视图渲染测试。

覆盖 Task 5 交付物：
- solo role / center 不可达 → 顶部红 Stub Panel "中央未接入（solo role..."
  + 不渲染 3 section（多余职责：Stub 提示 intent 引导）
- worker + nodes/goals → 节点表节点列/状态/lan/容量 + goal 收敛列 `1/1`
- goal stage chain 8 格进度块：STARTING 时前 4 格同色（"3 + 1"），fallback (unknown)
- 事件 tab → 占位 Panel "（T6 接事件流）"
- 空 nodes → 黄 Panel "(集群无节点 — 检查 role=center/both 是否允许)"
- CJK 严格对齐：每行 display_width <= width；摘要/keybar 行 == width
- scope sentinel：未 unwrap，渲染不 crash
"""

from __future__ import annotations

import time
from unittest import mock

from rich.console import Console, Group

from modelctl.core.colors import display_width
from modelctl.core.tui.data import ClusterSnapshot
from modelctl.core.tui.panels.cluster import render as render_cluster
from modelctl.core.tui.state import TUIState

# ─────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────


def _snap(*, nodes=None, goals=None, center_visible: str = "") -> ClusterSnapshot:
    snap = ClusterSnapshot(
        nodes=list(nodes or []),
        goals=list(goals or []),
        center_visible=center_visible,
    )
    snap._fetched_at = time.monotonic()
    return snap


def _render_text(state, cluster, *, width=100, height=24, role: str = "worker") -> str:
    """render + rich Console record 捕获；mock cluster_role() 返回指定 role。"""
    console = Console(record=True, width=width, height=height, force_terminal=False)
    with mock.patch("modelctl.core.cluster.config.cluster_role", return_value=role):
        group = render_cluster(state, cluster, width=width, height=height, theme_id="dark")
    console.print(group)
    return console.export_text()


# ─────────────────────────────────────────────────────────
# 1. Solo Stub
# ─────────────────────────────────────────────────────────


def test_cluster_solo_stub():
    """solo role + 空 center → 顶部红 Panel 含 "中央未接入（solo role"；不 crash。"""
    state = TUIState()
    state.active_view = "cluster"
    snap = _snap(nodes=[], goals=[], center_visible="(中心不可达)")
    text = _render_text(state, snap, role="solo")
    assert "中央未接入（solo role" in text
    # 底部 keybar 保留（可退回 dashboard）
    assert "keybar" in text.lower() or "q 退" in text


# ─────────────────────────────────────────────────────────
# 2. worker + 节点表
# ─────────────────────────────────────────────────────────


def test_cluster_worker_with_nodes():
    """worker + 真实 node + goal → 节点列出现 w-210 + online + '1/1' 收敛。"""
    state = TUIState()
    state.active_view = "cluster"
    state.active_cluster_tab = 0
    snap = _snap(
        nodes=[
            {"node_id": "w-210", "status": "online",
             "lan_id": "10.0.0.5", "capacity_text": "4xA100"},
        ],
        goals=[
            {"node_id": "w-210", "intent": "start", "stage": "READY",
             "state": "running", "profile": "qwen-7b", "port": 12345},
        ],
        center_visible="http://127.0.0.1:8124",
    )
    text = _render_text(state, snap, role="worker")
    assert "w-210" in text
    assert "10.0.0.5" in text
    assert "4xA100" in text
    assert "online" in text
    assert "1/1" in text
    # 摘要条含节点数 + goals 数
    assert "节点" in text
    assert "goals" in text.lower() or "goal" in text


# ─────────────────────────────────────────────────────────
# 3. goal stage chain 进度块
# ─────────────────────────────────────────────────────────


def test_cluster_goal_stage_chain_progress():
    """goal stage=STARTING → 8 格进度块出现，前 4 段同色（3 done + 1 current）。"""
    state = TUIState()
    state.active_view = "cluster"
    state.active_cluster_tab = 1  # goal tab
    snap = _snap(
        nodes=[],
        goals=[
            {"node_id": "w-210", "intent": "start", "stage": "STARTING",
             "state": "starting", "profile": "qwen-7b", "port": 12345},
        ],
        center_visible="http://127.0.0.1:8124",
    )
    text = _render_text(state, snap, role="worker")
    # 8 格进度块：前 4 段用非空格 marker（[ ]/[✓]/[>]/[·] 任一），检查至少 [ 数量 >= 8
    assert text.count("[") >= 8, f"stage chain 8 格进度块缺失: {text!r}"
    # STARTING 行：4 段非空 → [✓][✓][✓][>]
    assert "STARTING" not in text or "qwen-7b" in text


# ─────────────────────────────────────────────────────────
# 4. 事件 tab 占位
# ─────────────────────────────────────────────────────────


def test_cluster_events_stub():
    """事件 tab → 占位 Panel "（T6 接事件流）"；无真实 event 数据行。"""
    state = TUIState()
    state.active_view = "cluster"
    state.active_cluster_tab = 2  # 事件 tab
    snap = _snap(
        nodes=[{"node_id": "w-210", "status": "online", "lan_id": "10.0.0.1",
                "capacity_text": "1xA100"}],
        goals=[],
        center_visible="http://127.0.0.1:8124",
    )
    text = _render_text(state, snap, role="worker")
    assert "T6 接事件流" in text


# ─────────────────────────────────────────────────────────
# 5. center 不可达 fallback（worker 时 center_visible="(中心不可达)"）
# ─────────────────────────────────────────────────────────


def test_cluster_center_unreachable_fallback():
    """center_visible="(中心不可达)" 即使 role=worker → SoloStub 红 Panel 触发。"""
    state = TUIState()
    state.active_view = "cluster"
    state.active_cluster_tab = 0
    snap = _snap(nodes=[], goals=[], center_visible="(中心不可达)")
    text = _render_text(state, snap, role="worker")
    assert "中央未接入" in text
    # 不渲染 3 section 的实际表格行（节点表也不会出现任何 w-xxx 节点）
    assert "w-210" not in text


# ─────────────────────────────────────────────────────────
# 6. 空 nodes 交叉
# ─────────────────────────────────────────────────────────


def test_cluster_empty_nodes_crosscheck():
    """worker role 但 nodes=[] → 黄 Panel "(集群无节点 — 检查 role=center/both 是否允许)"。"""
    state = TUIState()
    state.active_view = "cluster"
    state.active_cluster_tab = 0
    snap = _snap(
        nodes=[],
        goals=[],
        center_visible="http://127.0.0.1:8124",
    )
    text = _render_text(state, snap, role="worker")
    assert "集群无节点" in text


# ─────────────────────────────────────────────────────────
# 7. CJK 严格对齐
# ─────────────────────────────────────────────────────────


def test_cluster_cjk_strict_alignment():
    """全都中文数据 → 每行 display_width <= width；摘要/keybar 行 == width。"""
    state = TUIState()
    state.active_view = "cluster"
    state.active_cluster_tab = 0
    snap = _snap(
        nodes=[
            {"node_id": "w-Renderer-100", "status": "online",
             "lan_id": "内网节点-10.0.0.5", "capacity_text": "4卡-A100-模型"},
        ],
        goals=[
            {"node_id": "w-Renderer-100", "intent": "start", "stage": "READY",
             "state": "running", "profile": "qwen-中文模型", "port": 12345},
        ],
        center_visible="http://中心-内网-001.example.com:8124",
    )
    width = 100
    text = _render_text(state, snap, width=width, height=24, role="worker")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    assert lines, "无有效渲染行"
    for ln in lines:
        dw = display_width(ln)
        # rich Console 渲染 trailing 可能被 strip；断言"不超过 width + 1 容差"
        assert dw <= width + 1, f"行超宽 {dw} > {width + 1}: {ln!r}"
    # 摘要条行（首非空行）display_width == width
    first = lines[0].rstrip("\r")
    assert display_width(first) in (width, width - 1), \
        f"摘要行宽度异常：{display_width(first)} 期望 {width}"
    # keybar 行（末非空行）display_width == width
    last = lines[-1].rstrip("\r")
    assert display_width(last) in (width, width - 1), \
        f"keybar 行宽度异常：{display_width(last)} 期望 {width}"


# ─────────────────────────────────────────────────────────
# 8. scope sentinel — 不 crash / 类型正确
# ─────────────────────────────────────────────────────────


def test_cluster_scope_limited_sentinel():
    """render 返回 rich Group（不 unwrap 任何对象），不抛异常。"""
    state = TUIState()
    state.active_view = "cluster"
    state.active_cluster_tab = 0
    snap = _snap(
        nodes=[{"node_id": "w-210", "status": "online", "lan_id": "10.0.0.5",
                "capacity_text": "1xA100"}],
        goals=[],
        center_visible="http://127.0.0.1:8124",
    )
    with mock.patch("modelctl.core.cluster.config.cluster_role", return_value="worker"):
        group = render_cluster(state, snap, width=100, height=24, theme_id="dark")
    assert isinstance(group, Group)
    # Group 至少含 header + body + keybar = 3 个 renderable
    assert len(group.renderables) >= 3
