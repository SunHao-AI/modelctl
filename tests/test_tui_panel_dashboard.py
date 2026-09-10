#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_tui_panel_dashboard.py
# @IDE    : PyCharm
# @Author : modelctl
# @Email  : modelctl@example.com
# @Date   : 2026-09-10
# @Desc   : TUI Dashboard 面板渲染测试（CJK 对齐断言 / header 摘要 / 表格 / keybar / 过滤排序）
# ===============================================================================

"""TUI Dashboard 面板渲染测试（Task 2 Phase B）。

覆盖：
- `render()` 产出 `Group` 且每行 `display_width <= width`（CJK 双宽口径）
- 顶部 header：含 GPU 摘要（"2x RTX 4090"）+ 模型运行率（"1/2 运行"）+ 集群节点（"3 节点"）
- 表格：包含运行中 profile 名 "demo-vllm" 与已停止 "demo-llamacpp"
- 状态徽：running=实心 ●，stopped=空心 ○
- keybar：含 "q 退"、"/ 搜"、"f 状态"、"s 排序"、"d 引擎"、"j/k 翻页"、"Enter 详情"
- state 方法：`filter_candidates` / `sort_profiles` / `apply_page` 行为正确
- 空值降级：`list_profiles` 空 → 表格只渲染占位行
"""

from __future__ import annotations

from rich.console import Console, Group

from modelctl.core.colors import display_width
from modelctl.core.tui.data import (
    ClusterSnapshot,
    HardwareSnapshot,
    ModelsSnapshot,
)
from modelctl.core.tui.panels.main_dashboard import render
from modelctl.core.tui.state import TUIState

# ─────────────────────────────────────────────────────────
# helpers：构造 snapshot
# ─────────────────────────────────────────────────────────


def _hw_mock(gpu_count: int = 2, name: str = "RTX 4090", free_mb: int = 22000, total_mb: int = 24576):
    import time

    snap = HardwareSnapshot(
        gpus=[
            {"index": i, "name": name, "free_mb": free_mb, "total_mb": total_mb, "util_pct": 5}
            for i in range(gpu_count)
        ],
        binaries={"vllm": "/bin/vllm"},
        cpu_info="CC 8.9",
        probe_errors=[],
    )
    snap._fetched_at = time.monotonic()
    return snap


def _models_mock(running: bool = True):
    import time

    snap = ModelsSnapshot(
        profiles=[
            {
                "name": "demo-vllm",
                "engine": "vllm",
                "variant": "55in",
                "port": 8866,
                "status": "running" if running else "stopped",
                "vram_gib": 30.0,
                "rate_in": 155.0 if running else None,
                "rate_out": 33.0 if running else None,
            },
            {
                "name": "demo-llamacpp",
                "engine": "llamacpp",
                "variant": "70b",
                "port": 8867,
                "status": "stopped",
                "vram_gib": 40.0,
                "rate_in": None,
                "rate_out": None,
            },
        ]
    )
    snap._fetched_at = time.monotonic()
    return snap


def _cluster_mock(node_count: int = 3, center_visible: str = ""):
    import time

    snap = ClusterSnapshot(
        nodes=[{"node_id": f"w-{100+i}", "status": "online"} for i in range(node_count)],
        goals=[{"profile": "demo-vllm", "intent": "start"}],
        center_visible=center_visible,
    )
    snap._fetched_at = time.monotonic()
    return snap


def _render_to_lines(
    state, hw, models, cluster, *, width=100, height=24
) -> list[str]:
    """渲染 → 输出文本行。dashboard 部分文本自带 padding 到 width，断言用 display_width。

    `Console(no_wrap=True, soft_wrap=True)`：禁用 rich 的 character-based 宽度换行，
    避免 CJK 行（含 2 列字符）在 width-1 处被截掉 1 列导致 `display_width != width`。
    """
    console = Console(
        record=True, width=width, height=height,
        force_terminal=False, soft_wrap=True,
    )
    group = render(state, hw, models, cluster, width=width, height=height, theme_id="dark")
    console.print(group)
    # rich export_text 签名无 end 参数，全量导出（含 trailing newline）
    text = console.export_text()
    return text.split("\n")


# ─────────────────────────────────────────────────────────
# dashboard 渲染 — 基础结构
# ─────────────────────────────────────────────────────────


def test_dashboard_render_returns_group():
    """render 返回 Group，可用 rich Console 输出。"""
    state = TUIState()
    hw, models, cluster = _hw_mock(), _models_mock(), _cluster_mock()
    g = render(state, hw, models, cluster, width=100, height=24, theme_id="dark")
    assert isinstance(g, Group)


def test_dashboard_header_contains_gpu_and_running_counts():
    """顶部 header：GPU "2x RTX 4090" + 模型 "1/2 运行" + 集群 "3 节点"。"""
    state = TUIState()
    hw, models, cluster = _hw_mock(gpu_count=2), _models_mock(), _cluster_mock(node_count=3)
    lines = _render_to_lines(state, hw, models, cluster, width=100, height=24)
    full = "\n".join(lines)
    assert "2x RTX 4090" in full
    assert "1/2 运行" in full
    assert "3 节点" in full


def test_dashboard_table_contains_both_profiles():
    """表格同时包含 running / stopped 两个 profile 名。"""
    state = TUIState()
    hw, models, cluster = _hw_mock(), _models_mock(), _cluster_mock()
    lines = _render_to_lines(state, hw, models, cluster)
    full = "\n".join(lines)
    assert "demo-vllm" in full
    assert "demo-llamacpp" in full
    # running 用实心 ●，stopped 用空心 ○
    assert "●" in full
    assert "○" in full


def test_dashboard_each_line_within_cap_width():
    """渲染后每行 display_width 不应超过 cap_width（CJK 双宽口径）。

    非空行的 display_width 必须 ≤ width（表格行通过 pad_width 严格对齐到 width，
    允许 ≤ 但不超过；任何表格单元格超宽都会暴露在这里）。
    """
    state = TUIState()
    hw, models, cluster = _hw_mock(), _models_mock(), _cluster_mock()
    width = 100
    lines = _render_to_lines(state, hw, models, cluster, width=width, height=24)
    for line in lines:
        if line.strip():
            assert display_width(line) <= width, (
                f"line 超宽: {line!r} len={display_width(line)} > {width}"
            )


def test_dashboard_table_rows_pad_to_width_exactly():
    """表格行（含 CJK 容器名场景）每行 display_width 严格 == width。

    这是 brief 的"CJK 对齐守"——pad_width 保证整行等宽。header/keybar
    两行均通过 pad_width 到 width；表头/分隔线/数据行也通过 pad_width 到 width。
    """
    state = TUIState()
    hw, models, cluster = _hw_mock(), _models_mock(), _cluster_mock()
    width = 100
    lines = _render_to_lines(state, hw, models, cluster, width=width, height=24)
    non_blank = [line for line in lines if line.strip()]
    assert non_blank, "渲染产出为空（fail fast）"
    # 同一份 header 包含 CJK "运行"/"节点"——验证 pad_width 在 CJK 后能回到 width
    for line in non_blank:
        assert display_width(line) == width, (
            f"未对齐: {line!r} 当前 {display_width(line)} 期望 {width}"
        )


def test_dashboard_keybar_contains_shortcut_labels():
    """keybar 行包含 brief 指定的快捷键标签。"""
    state = TUIState()
    hw, models, cluster = _hw_mock(), _models_mock(), _cluster_mock()
    lines = _render_to_lines(state, hw, models, cluster)
    last_non_blank = [line for line in lines if line.strip()][-1]
    for token in ("q 退", "/ 搜", "f 状态", "s 排序", "d 引擎", "j/k 翻页", "Enter 详情"):
        assert token in last_non_blank, f"缺少 keybar 标签 {token!r} 在 {last_non_blank!r}"


# ─────────────────────────────────────────────────────────
# 过滤 / 排序 / 分页（state methods）
# ─────────────────────────────────────────────────────────


def test_state_filter_engine_returns_only_subset():
    """filter_engine='vllm' → 仅 1 项（running 的 demo-vllm），llamacpp 被排除。"""
    state = TUIState()
    snap = _models_mock()
    matched = state.filter_candidates(snap.profiles)
    assert len(matched) == 2
    state.filter_engine = "vllm"
    matched = state.filter_candidates(snap.profiles)
    assert len(matched) == 1
    assert matched[0]["name"] == "demo-vllm"


def test_state_filter_status_running_returns_only_running():
    """filter_status='running' → 仅 1 项（demo-vllm 命中）。"""
    state = TUIState()
    snap = _models_mock()
    state.filter_status = "running"
    matched = state.filter_candidates(snap.profiles)
    assert len(matched) == 1
    assert matched[0]["name"] == "demo-vllm"


def test_state_search_substring_matches_lower():
    """search='demo' 用子串小写比较命中 2 项。"""
    state = TUIState()
    snap = _models_mock()
    state.search = "demo"
    matched = state.filter_candidates(snap.profiles)
    assert len(matched) == 2
    # 大小写不敏感
    state.search = "DEMO"
    assert len(state.filter_candidates(snap.profiles)) == 2


def test_state_sort_by_port_orders_numerically_sorting_by_str_slow_padded():
    """sort_key='port' → demo-vllm(8866) 在 demo-llamacpp(8867) 前（按字符串排序，
    位数相同时字典序也等于数值序——这里的用例只验证列分隔排序能由 sort_key 驱动）。"""
    state = TUIState()
    snap = _models_mock()
    state.sort_key = "port"
    sorted_profiles = state.sort_profiles(snap.profiles)
    assert [p["name"] for p in sorted_profiles] == ["demo-vllm", "demo-llamacpp"]


def test_state_sort_by_engine_llamacpp_before_vllm_alphabetically():
    """sort_key='engine' → llamacpp 字典序早于 vllm（字典序按 ASCII）。"""
    state = TUIState()
    snap = _models_mock()
    # 重置为 engine 排序
    state.sort_key = "engine"
    sorted_profiles = state.sort_profiles(snap.profiles)
    assert [p["name"] for p in sorted_profiles] == ["demo-llamacpp", "demo-vllm"]


def test_state_apply_page_returns_first_page_only():
    """page_size=1，page=0 → 第一项；page=1 → 第二项；page=2 → 空（越界回）。"""
    state = TUIState()
    snap = _models_mock()
    first = state.apply_page(snap.profiles, page_size=1)
    assert len(first) == 1
    assert first[0]["name"] == "demo-vllm"
    state.page = 1
    second = state.apply_page(snap.profiles, page_size=1)
    assert len(second) == 1
    assert second[0]["name"] == "demo-llamacpp"
    state.page = 2
    empty = state.apply_page(snap.profiles, page_size=1)
    assert empty == []


def test_state_apply_page_default_page_size_is_10():
    """12 项，默认 page_size=10 → 第一页 10，第二页 2。"""
    state = TUIState()
    many = [
        {"name": f"p{i:02d}", "engine": "vllm", "variant": "", "port": 8000 + i,
         "status": "running", "vram_gib": 1.0, "rate_in": 1.0, "rate_out": 1.0}
        for i in range(12)
    ]
    page0 = state.apply_page(many)  # 默认 10
    assert len(page0) == 10
    state.page = 1
    page1 = state.apply_page(many)
    assert len(page1) == 2


# ─────────────────────────────────────────────────────────
# 空值降级
# ─────────────────────────────────────────────────────────


def test_dashboard_empty_models_still_renders():
    """models=[] → 表格渲染占位行；header 含 "0/0 运行"；集群 0 节点。"""
    state = TUIState()
    hw = _hw_mock(gpu_count=1)
    models = ModelsSnapshot(profiles=[])
    cluster = _cluster_mock(node_count=0)
    lines = _render_to_lines(state, hw, models, cluster)
    assert len([line for line in lines if line.strip()]) >= 3
    full = "\n".join(lines)
    assert "0/0 运行" in full
    assert "0 节点" in full
