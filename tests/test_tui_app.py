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

覆盖 Task 6 交付物（含主题持久化 + 80 列窄屏适配）：
- theme 持久化 roundtrip（save/load）+ 损坏 / 未知值 fallback
- TuiApp 构造时按 theme_file 读 # 退出时写 theme_file
- Dashboard 80 列窄屏适配：宽度 = 100 时压缩 variant 列、宽度 = 200 时全列、
  所有非空行 display_width == width (CJK 对齐守)
"""

from __future__ import annotations

import json

import pytest
from rich.console import Console

from modelctl.core.tui import TuiApp, TUIState
from modelctl.core.tui.data import (
    ClusterSnapshot,
    HardwareSnapshot,
    LogsSnapshot,
    ModelsSnapshot,
    MonitorSnapshot,
)
from modelctl.core.tui.keyboard import Key, KeyboardInput
from modelctl.core.tui.theme import (
    DEFAULT_THEME,
    THEME_IDS,
    cycle_theme,
    get_rich_theme,
    load_theme,
    save_theme,
    theme_file_path,
    theme_id_for,
)


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


# ─────────────────────────────────────────────────────────
# Task 6：theme 持久化（load / save / theme_file_path）
# ─────────────────────────────────────────────────────────


def test_theme_persistence_roundtrip(tmp_path):
    """theme 持久化：写入 light → load → 断言 'light'。"""
    f = tmp_path / "tui.theme"
    assert save_theme("light", f) is True
    assert load_theme(f) == "light"


def test_theme_persistence_roundtrip_all_ids(tmp_path):
    """三个主题 ID 都跑一遍 roundtrip。"""
    f = tmp_path / "tui.theme"
    for tid in THEME_IDS:
        assert save_theme(tid, f) is True
        assert load_theme(f) == tid


def test_theme_load_fallback_corrupt(tmp_path):
    """theme 文件损坏（非 JSON）时 fallback 到 DEFAULT_THEME。"""
    f = tmp_path / "tui.theme"
    f.write_text("not json", encoding="utf-8")
    assert load_theme(f) == DEFAULT_THEME


def test_theme_load_fallback_bad_value(tmp_path):
    """theme 文件 JSON 合法但值不在 THEME_IDS → DEFAULT_THEME。"""
    f = tmp_path / "tui.theme"
    f.write_text(json.dumps({"theme": "bogus"}), encoding="utf-8")
    assert load_theme(f) == DEFAULT_THEME


def test_theme_load_fallback_not_object(tmp_path):
    """theme 文件是 JSON 但非 object（list / 标量）→ DEFAULT_THEME。"""
    f = tmp_path / "tui.theme"
    f.write_text(json.dumps(["light"]), encoding="utf-8")
    assert load_theme(f) == DEFAULT_THEME


def test_theme_save_rejects_unknown(tmp_path):
    """save_theme 不写未知值，返回 False。"""
    f = tmp_path / "tui.theme"
    assert save_theme("nope", f) is False
    assert not f.exists()


def test_theme_file_path_under_cache_dir():
    """theme_file_path 基于 cache_dir()（与 paths.cache_dir 同步）。"""
    p = theme_file_path()
    assert p.name == "tui.theme"
    assert p.parent.name == "cache"


# ─────────────────────────────────────────────────────────
# Task 6：TuiApp 构造 + 退出时按 theme_file 读写
# ─────────────────────────────────────────────────────────


def test_tui_app_theme_init_from_file(console, tmp_path):
    """TuiApp 构造时 load 文件；退出时 save 文件。"""
    f = tmp_path / "tui.theme"
    f.write_text(json.dumps({"theme": "light"}), encoding="utf-8")
    st = TUIState()
    app = TuiApp(state=st, console=console, theme_file=f)
    assert app._theme == "light"
    # 模拟 run(smoke) → loop_end → save_theme
    sequence = iter([Key.Up, Key.Down, Key.Q])
    monkeypatch_stub = pytest.MonkeyPatch()
    monkeypatch_stub.setattr(
        KeyboardInput, "read_key_block", lambda self, timeout=0.1: next(sequence)
    )
    try:
        rc = app.run(smoke=True)
    finally:
        monkeypatch_stub.undo()
    assert rc == 0
    # 退出后文件被回写到合法 THEME_IDS 之一
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["theme"] in THEME_IDS


def test_tui_app_theme_cycle_t_key_writes_through(console, tmp_path):
    """T 键切主题（cycle_theme）后，退出时把新主题写回 theme_file。"""
    f = tmp_path / "tui.theme"
    f.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    st = TUIState()
    app = TuiApp(state=st, console=console, theme_file=f)
    assert app._theme == "dark"
    # t 键手动切一次
    app.cycle_theme()
    assert app._theme == "light"
    sequence = iter([Key.Q])
    mp = pytest.MonkeyPatch()
    mp.setattr(
        KeyboardInput, "read_key_block", lambda self, timeout=0.1: next(sequence)
    )
    try:
        rc = app.run(smoke=True)
    finally:
        mp.undo()
    assert rc == 0
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["theme"] == "light"


# ─────────────────────────────────────────────────────────
# Task 6：main_dashboard 80 列窄屏适配
# ─────────────────────────────────────────────────────────


def _render_dashboard_lines(width: int, height: int = 24, with_profiles: bool = False) -> list[str]:
    """渲染 dashboard → 行列表。

    `with_profiles=True` 时注入含 CJK name 的 profile，让 `_profile_row`
    的 CJK 对齐路径被实际执行（四档宽度不变量测试依赖此参数）。
    """
    from rich.console import Console

    from modelctl.core.tui.panels.main_dashboard import render as r

    console = Console(
        record=True,
        width=width,
        height=height,
        force_terminal=False,
        soft_wrap=True,
    )
    state = TUIState()
    hw = HardwareSnapshot(gpus=[], binaries={}, cpu_info="", probe_errors=[])
    if with_profiles:
        models = ModelsSnapshot(profiles=[{
            "name": "中文测试-模型",
            "engine": "vllm",
            "variant": "7b",
            "port": "8080",
            "status": "running",
            "vram_gib": 12.0,
            "rate_in": 155.0,
            "rate_out": 33.0,
        }, {
            "name": "qwen3.5-397b-flash",
            "engine": "llamacpp",
            "variant": "-",
            "port": "8081",
            "status": "stopped",
            "vram_gib": 0.0,
            "rate_in": None,
            "rate_out": None,
        }])
    else:
        models = ModelsSnapshot(profiles=[])
    cluster = ClusterSnapshot(nodes=[], goals=[])
    # 给个 time 戳，避免 revalidate 再触发 fetch（render 内部不会再调 fetch）
    import time

    hw._fetched_at = time.monotonic()
    models._fetched_at = time.monotonic()
    cluster._fetched_at = time.monotonic()
    group = r(state, hw, models, cluster, width=width, height=height, theme_id="dark")
    console.print(group)
    return console.export_text().split("\n")


def test_dashboard_full_layout_at_width_200():
    """宽度 200 → full 布局（所有 7 列），含 CJK profile 行，display_width == width。"""
    from modelctl.core.colors import display_width

    width = 200
    lines = _render_dashboard_lines(width=width, height=24, with_profiles=True)
    non_blank = [ln for ln in lines if ln.strip()]
    assert non_blank, "渲染产出为空（fail fast）"
    for ln in non_blank:
        assert display_width(ln) == width, (
            f"未对齐: {ln!r} 当前 {display_width(ln)} 期望 {width}"
        )


def test_dashboard_medium_layout_at_width_120():
    """宽度 120 → medium 布局（隐藏 VARIANT 列），非空行 display_width == width。"""
    from modelctl.core.colors import display_width

    width = 120
    lines = _render_dashboard_lines(width=width, height=24)
    non_blank = [ln for ln in lines if ln.strip()]
    assert non_blank
    for ln in non_blank:
        assert display_width(ln) == width, (
            f"未对齐: {ln!r} 当前 {display_width(ln)} 期望 {width}"
        )


def test_dashboard_narrow_layout_at_width_100():
    """宽度 100 → narrow 布局（隐藏 VARIANT + RATE 拆分 + 压缩 VRAM），行宽 == width。"""
    from modelctl.core.colors import display_width

    width = 100
    lines = _render_dashboard_lines(width=width, height=24)
    non_blank = [ln for ln in lines if ln.strip()]
    assert non_blank
    for ln in non_blank:
        assert display_width(ln) == width, (
            f"未对齐: {ln!r} 当前 {display_width(ln)} 期望 {width}"
        )


def test_dashboard_minimum_width_80():
    """宽度 80 → narrow 布局（最小值），含 CJK profile 行，display_width == width。"""
    from modelctl.core.colors import display_width

    width = 80
    lines = _render_dashboard_lines(width=width, height=24, with_profiles=True)
    non_blank = [ln for ln in lines if ln.strip()]
    assert non_blank
    for ln in non_blank:
        assert display_width(ln) == width, (
            f"未对齐: {ln!r} 当前 {display_width(ln)} 期望 {width}"
        )


# ─────────────────────────────────────────────────────────
# Task 3.2（TUI-P1-2）：非 smoke 主循环 + 按键派发
# ─────────────────────────────────────────────────────────


def _stub_app_loop(app, keys, monkeypatch):
    """屏蔽真实渲染/数据/终端，read_key_block 依次吐 keys（耗尽回 Q 兜底防死循环）。"""
    app._revalidate = lambda: None
    app.realize_render_once = lambda: None
    app.loop_begin = lambda: None
    it = iter(keys)
    monkeypatch.setattr(app.keyboard, "read_key_block",
                        lambda timeout: next(it, Key.Q))
    monkeypatch.setattr(app.keyboard, "restore_term", lambda: None)
    return it


def test_run_non_smoke_loops_until_q(tmp_path, monkeypatch):
    """旧实现非 smoke 只渲染一帧就 return 0；keybar 承诺的按键全无实现。"""
    console = Console(width=120, height=40)
    app = TuiApp(TUIState(), console, theme_file=tmp_path / "tui.theme")
    it = _stub_app_loop(app, [Key.Tab, Key.Esc, Key.Q], monkeypatch)
    assert app.run(smoke=False) == 0
    # Tab → plan；Esc → 回 dashboard；Q → 退出（3 键恰好吃满，无兜底消费）
    assert app.state.active_view == "dashboard"
    assert next(it, None) is None


def test_dashboard_esc_exits_loop(tmp_path, monkeypatch):
    """dashboard 上 Esc 与 q 同义（无更上一层）→ 退出且返回 0。"""
    console = Console(width=120, height=40)
    app = TuiApp(TUIState(), console, theme_file=tmp_path / "tui.theme")
    _stub_app_loop(app, [Key.Esc], monkeypatch)
    assert app.run(smoke=False) == 0
    assert app.state.active_view == "dashboard"


def test_dashboard_arrows_move_selection(tmp_path, monkeypatch):
    console = Console(width=120, height=40)
    app = TuiApp(TUIState(), console, theme_file=tmp_path / "tui.theme")
    _stub_app_loop(app, [Key.Down, Key.Down, Key.Up, Key.Q], monkeypatch)
    app.run(smoke=False)
    assert app.state.active_index == 1  # Down Down Up → 1（下限 clamp 到 0）


def test_dashboard_arrows_clamp_at_zero(tmp_path, monkeypatch):
    console = Console(width=120, height=40)
    app = TuiApp(TUIState(), console, theme_file=tmp_path / "tui.theme")
    _stub_app_loop(app, [Key.Up, Key.K, Key.Q], monkeypatch)
    app.run(smoke=False)
    assert app.state.active_index == 0  # Up / k 不允许为负


def test_enter_opens_detail_and_tab_cycles_subtab(tmp_path, monkeypatch):
    """Enter → detail；detail 内 Tab 切子 Tab（yaml→agent）、Shift-Tab 回退。"""
    console = Console(width=120, height=40)
    app = TuiApp(TUIState(), console, theme_file=tmp_path / "tui.theme")
    _stub_app_loop(app, [Key.Enter, Key.Tab, Key.Tab, Key.ShiftTab, Key.Q],
                   monkeypatch)
    app.run(smoke=False)
    assert app.state.active_view == "detail"  # Esc 未按下，留在 detail
    assert app.state.active_detail_subtab == "agent"  # yaml→agent→log→agent


def test_theme_key_q_semantics_in_loop(tmp_path, monkeypatch):
    """T 键循环主题并在退出时持久化；其余视图 Tab 走各自 section 切换。"""
    console = Console(width=120, height=40)
    app = TuiApp(TUIState(), console, theme_file=tmp_path / "tui.theme")
    _stub_app_loop(app, [Key.T, Key.Tab, Key.Q], monkeypatch)
    app.run(smoke=False)
    assert app._theme == "light"  # T 生效
    import json

    assert json.loads((tmp_path / "tui.theme").read_text(encoding="utf-8"))["theme"] == "light"
    # dashboard 上首个 Tab 是视图切换（plan），不是 section
    assert app.state.active_view == "plan"
    assert app.state.active_cluster_tab == 0
