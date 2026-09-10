#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_tui_app.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : TuiApp 骨架测试（构造 / smoke 退出 / TUIState 初始值）
# ===============================================================================

"""TuiApp 骨架测试。

覆盖 Task 0 交付物：
- TuiApp 可用 mock Console 构造
- run(smoke=True) 执行 3 步虚拟 key 序列后干净退出（返回 0）
- TUIState 各字段初始值正确
"""

from __future__ import annotations

import pytest

from modelctl.core.tui import TuiApp, TUIState
from modelctl.core.tui.data import (
    ClusterSnapshot,
    HardwareSnapshot,
    LogsSnapshot,
    ModelsSnapshot,
    MonitorSnapshot,
)
from modelctl.core.tui.keyboard import Key, KeyboardInput
from modelctl.core.tui.theme import DEFAULT_THEME, THEME_IDS, cycle_theme, get_rich_theme, theme_id_for


@pytest.fixture
def console():
    from rich.console import Console

    return Console(record=True, width=100, height=30, force_terminal=False)


def test_tui_state_initial_values():
    """TUIState 初始字段值与 brief 约定一致。"""
    st = TUIState()
    assert st.active_view == "dashboard"
    assert st.active_index == 0
    assert st.active_detail_subtab == "yaml"
    assert st.plan_edit == {}
    assert st.filter_status == "all"
    assert st.filter_engine == "all"
    assert st.sort_key == "name"
    assert st.search == ""
    assert st.page == 0
    assert st.errors == []


def test_tui_app_construct_with_mock_console(console):
    """TuiApp 可用 mock console 构造并持有 state。"""
    st = TUIState()
    app = TuiApp(state=st, console=console)
    assert app.state is st
    assert app.console is console


def test_tui_app_run_smoke_exits_cleanly(console, monkeypatch):
    """run(smoke=True) 执行 3 步 key 事件后干净退出，返回 0。"""
    st = TUIState()
    app = TuiApp(state=st, console=console)
    # 占位键盘：read_key_block 依次返回 Up / Down / Q（3 步虚拟 key 序列）
    sequence = iter([Key.Up, Key.Down, Key.Q])
    monkeypatch.setattr(KeyboardInput, "read_key_block", lambda self, timeout=0.1: next(sequence))
    rc = app.run(smoke=True)
    assert rc == 0
    # smoke 路径应消费了全部 3 个 key
    assert next(sequence, None) is None


def test_tui_app_run_smoke_with_detail_view(console, monkeypatch):
    """Detail 视图 smoke 路径必须跑通 render_detail（LogsSnapshot 降级链）。"""
    st = TUIState()
    st.active_view = "detail"
    app = TuiApp(state=st, console=console)
    sequence = iter([Key.Up, Key.Down, Key.Q])
    monkeypatch.setattr(KeyboardInput, "read_key_block", lambda self, timeout=0.1: next(sequence))
    rc = app.run(smoke=True)
    assert rc == 0


def test_tui_snapshots_stubs_have_ttl():
    """5 个 Snapshot 空壳均带 _fetched_at / ttl 字段（Task 2 才实现采集）。"""
    for cls in (HardwareSnapshot, ModelsSnapshot, LogsSnapshot, ClusterSnapshot, MonitorSnapshot):
        snap = cls()
        assert snap.ttl > 0
        assert snap._fetched_at is None


def test_keyboard_read_key_block_returns_key_or_none():
    """Task 1：read_key_block(timeout=0.0) 返回 Key 或 None（真实实现，非 Task 0 占位 Key.Q 断言）。"""
    kb = KeyboardInput()
    result = kb.read_key_block(timeout=0.0)
    assert result is None or isinstance(result, Key)


def test_theme_cycle_covers_all_three():
    """theme 三套循环 + theme_id_for 取模。"""
    assert THEME_IDS == ["dark", "light", "high-contrast"]
    assert DEFAULT_THEME == "dark"
    assert theme_id_for(0) == "dark"
    assert theme_id_for(3) == THEME_IDS[0]
    assert cycle_theme("dark") == "light"
    assert cycle_theme("light") == "high-contrast"
    assert cycle_theme("high-contrast") == "dark"
    for tid in THEME_IDS:
        theme = get_rich_theme(tid)
        assert isinstance(theme, dict) and theme
