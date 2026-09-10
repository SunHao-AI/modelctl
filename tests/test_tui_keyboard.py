#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_tui_keyboard.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 12:00
# @Desc   : TUI 键盘输入跨平台 mock 测试（Windows msvcrt / Unix tty+select+os.read → Key 枚举）
# ===============================================================================

"""TUI 键盘输入跨平台 mock 测试（Task 1）。

覆盖：
- _decode_key 纯字节映射（Unix ANSI 表 / 1 字节 ASCII / Windows Ctrl 组合键码）
- _read_unix / _read_windows 超时返回 None（mock select / msvcrt，不依赖真实 tty）
- Windows 路径：msvcrt.getwch() / getch() 单字符与组合键（3-letter 无 ESC 前缀）
- Key 枚举 30 个成员齐备
- KeyboardInput.__init__ 无参兼容 + restore_term() 跨平台无副作用
"""

from __future__ import annotations

import sys
import types
from unittest import mock

import pytest

from modelctl.core.tui import keyboard as kbd
from modelctl.core.tui.keyboard import Key, KeyboardInput, _decode_key, _read_unix, _read_windows

_KEY_EXPECTED_COUNT = 31  # Key 枚举实际成员数（Task 0 已定，brief "30" 为笔误）


def test_key_enum_members_consistent():
    """Key 枚举成员与 Task 0 完全一致（名称 + 值，共 31 个，brief 的 "30" 为笔误）。"""
    expected = {
        "Up": "up", "Down": "down", "Left": "left", "Right": "right",
        "Enter": "enter", "Esc": "esc",
        "CtrlR": "ctrl-r", "CtrlU": "ctrl-u", "CtrlF": "ctrl-f", "CtrlB": "ctrl-b",
        "Digit1": "1", "Digit2": "2", "Digit3": "3", "Digit4": "4", "Digit5": "5",
        "T": "t", "H": "h", "Q": "q", "D": "d", "F": "f", "S": "s", "J": "j",
        "K": "k", "G": "g", "Colon": ":", "QuestionMark": "?",
        "Tab": "tab", "ShiftTab": "shift-tab", "PgUp": "pgup", "PgDn": "pgdn",
        "Unknown": "unknown",
    }
    assert len(Key) == _KEY_EXPECTED_COUNT
    for name, value in expected.items():
        assert getattr(Key, name).value == value


# ---------------------------------------------------------------------------
# _decode_key：纯字节映射（平台无关）
# ---------------------------------------------------------------------------

def test_decode_single_ascii_bytes():
    """1 字节 ASCII：文本/数字 → 对应 Key，组合键码走 Ctrl 表，未接 Key 为 Unknown。"""
    assert _decode_key(b"\r") is Key.Enter
    assert _decode_key(b"\t") is Key.Tab
    assert _decode_key(b"1") is Key.Digit1
    assert _decode_key(b"5") is Key.Digit5
    assert _decode_key(b"t") is Key.T
    assert _decode_key(b"h") is Key.H
    assert _decode_key(b"q") is Key.Q
    assert _decode_key(b"s") is Key.S
    assert _decode_key(b"k") is Key.K
    assert _decode_key(b"g") is Key.G
    assert _decode_key(b":") is Key.Colon
    assert _decode_key(b"?") is Key.QuestionMark
    # 组合键（1 字节，无前缀 ESC）
    assert _decode_key(b"\x12") is Key.CtrlR
    assert _decode_key(b"\x15") is Key.CtrlU
    assert _decode_key(b"\x06") is Key.CtrlF
    assert _decode_key(b"\x02") is Key.CtrlB
    # 未接 Key → Unknown
    assert _decode_key(b"\x03") is Key.Unknown
    assert _decode_key(b"m") is Key.Unknown


def test_decode_ansi_sequences():
    """2+ 字节 ESC 序列：方向键 / Shift-Tab / PgUp / PgDn / 裸 ESC。"""
    assert _decode_key(b"\x1b[A") is Key.Up
    assert _decode_key(b"\x1b[B") is Key.Down
    assert _decode_key(b"\x1b[C") is Key.Right
    assert _decode_key(b"\x1b[D") is Key.Left
    assert _decode_key(b"\x1b[Z") is Key.ShiftTab
    assert _decode_key(b"\x1b\x1b[C") is Key.PgDn
    assert _decode_key(b"\x1b\x1b[D") is Key.PgUp
    assert _decode_key(b"\x1b") is Key.Esc
    assert _decode_key(b"\x1b[?") is Key.Unknown


# ---------------------------------------------------------------------------
# Unix 路径：mock tty / select / os.read，不依赖真实 stdin fd
# ---------------------------------------------------------------------------

class _FakeStdin:
    """选通侧需 FileDescriptorType 的伪 stdin（CI 下 sys.stdin 是 InteractiveConsole）。"""

    def __init__(self) -> None:
        self._fd = 3  # 非 stdin 的伪 fd；配合 os.read mock 避免真实 unbuffered read

    def fileno(self) -> int:
        return self._fd


def _select_side_effect(chunks: list[bytes]):
    """side_effect factory：select 调用跟着 input chunk 走，chunk 耗尽 / 空 chunk 返未 ready。"""
    it = iter(chunks)
    empty = ([], [], [])
    ready = (_FakeStdin(), [], [])

    def _sel(r, w, e, t):
        chunk = next(it, None)
        if chunk is None or chunk == b"":
            return empty
        return ready

    return _sel


def _mock_unix_read(monkeypatch, chunks: list[bytes] | None):
    """注入 mock tty/select/os，_read_unix 每次 select 读 1 字节，序列跨多次 select 拼接。"""
    # 返回 (r, w, e)，每次 select 调用消耗一个 chunk（ready 含义：本字节即将被读到）
    sel = mock.Mock(side_effect=_select_side_effect(chunks or []))

    # Windows 下 tty 为 None，注入 SimpleNamespace 供 setattr（生产路径走 sys.stdin.fileno 无副作用）
    if kbd.tty is None:
        monkeypatch.setattr(kbd, "tty", types.SimpleNamespace(
            setraw=mock.Mock(), setcbreak=mock.Mock()))
    else:
        monkeypatch.setattr(kbd.tty, "setraw", mock.Mock())
    if kbd.select is None:
        monkeypatch.setattr(kbd, "select", types.SimpleNamespace(select=sel))
    else:
        monkeypatch.setattr(kbd.select, "select", sel)
    # unix 路径测试以 mock select 为主，isatty(0) 返 False 跳过 setraw
    monkeypatch.setattr(
        kbd.os, "isatty",
        mock.Mock(side_effect=lambda fd: fd != 0),
    )
    reads = iter(chunks or [])
    monkeypatch.setattr(kbd.os, "read", mock.Mock(side_effect=lambda fd, n: next(reads)))
    monkeypatch.setattr(sys, "stdin", _FakeStdin())
    return sel


def test_read_unix_ansi_sequence_cross_select(monkeypatch):
    """ESC 先读，方向键次读：跨两次 select 拼接出 Up。"""
    sel = _mock_unix_read(monkeypatch, [b"\x1b", b"[A"])
    got = _read_unix(0.05)
    assert got is not None
    assert _decode_key(got) is Key.Up
    assert sel.call_count == 2  # 仅 2 次 select，不多不少


def test_read_unix_timeout_returns_none(monkeypatch):
    """无按键：select 返回空 → None，不 decode。"""
    sel = _mock_unix_read(monkeypatch, None)
    assert _read_unix(0.005) is None
    assert sel.call_count == 1


def test_read_unix_ctrl_r(monkeypatch):
    """1 字节 ASCII \\x12 → CtrlR（无 ESC 前缀）。"""
    _mock_unix_read(monkeypatch, [b"\x12"])
    assert _decode_key(_read_unix(0.05)) is Key.CtrlR  # type: ignore[arg-type]


def test_read_unix_ansi_timeout_returns_none(monkeypatch):
    """ESC 已读、后续 select 超时 → 退化为裸 ESC → Key.Esc。

    用独立状态机：首次 select 返 ready、其后返未 ready；mock 独立的 os.read/tty/sys.stdin。
    """
    calls = {"n": 0}

    def sel_side_effect(r, w, e, t):
        calls["n"] += 1
        return [[_FakeStdin()], [], []] if calls["n"] == 1 else [[], [], []]

    spy_sel = mock.Mock(side_effect=sel_side_effect)
    if kbd.tty is None:
        monkeypatch.setattr(kbd, "tty", types.SimpleNamespace(
            setraw=mock.Mock(), setcbreak=mock.Mock()))
    else:
        monkeypatch.setattr(kbd.tty, "setraw", mock.Mock())
    if kbd.select is None:
        monkeypatch.setattr(kbd, "select", types.SimpleNamespace(select=spy_sel))
    else:
        monkeypatch.setattr(kbd.select, "select", spy_sel)
    monkeypatch.setattr(
        kbd.os, "isatty",
        mock.Mock(side_effect=lambda fd: fd != 0),
    )
    stream = iter([b"\x1b"])
    monkeypatch.setattr(kbd.os, "read", mock.Mock(side_effect=lambda fd, n: next(stream, b"")))
    monkeypatch.setattr(sys, "stdin", _FakeStdin())
    got = _read_unix(0.05)
    assert got is not None and _decode_key(got) is Key.Esc


# ---------------------------------------------------------------------------
# Windows 路径：mock msvcrt（win32 环境才生效，CI 其他平台安全跳过）
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_msvcrt(monkeypatch):
    """模拟 getwch/getch/kbhit（msvcrt 模块全局共享，以实例注入 mock 方法）。"""
    # 兜底：非 win32 也保证 msvcrt 模块存在（模块加载时已存在则无需创建）
    if "msvcrt" not in sys.modules:
        monkeypatch.setitem(sys.modules, "msvcrt", types.ModuleType("msvcrt"))
    mod = sys.modules["msvcrt"]
    if not hasattr(mod, "getwch"):
        monkeypatch.setattr(mod, "getwch", mock.Mock())
    if not hasattr(mod, "getch"):
        monkeypatch.setattr(mod, "getch", mock.Mock())
    if not hasattr(mod, "kbhit"):
        monkeypatch.setattr(mod, "kbhit", mock.Mock())
    yield mod


def test_read_windows_timeout_getch_kbhit_false(monkeypatch, mock_msvcrt):
    """timeout 负值：kbhit() False → None（不读）。"""
    mock_msvcrt.kbhit = mock.Mock(return_value=False)
    assert _read_windows(-1) is None


def test_read_windows_getwch_ascii(monkeypatch, mock_msvcrt):
    """timeout >= 0：kbhit() True + getwch() → 'q' → Key.Q 字节。"""
    mock_msvcrt.kbhit = mock.Mock(return_value=True)
    mock_msvcrt.getwch = mock.Mock(return_value="q")
    assert _decode_key(_read_windows(0.05)) is Key.Q  # type: ignore[arg-type]


def test_read_windows_getch_ctrl_r(monkeypatch, mock_msvcrt):
    """Windows getch() 组合键码 0x12 → CtrlR（3-letter 无 ESC 前缀，走 ASCII 码）。"""
    mock_msvcrt.kbhit = mock.Mock(return_value=True)
    mock_msvcrt.getch = mock.Mock(return_value=0x12)
    # getwch 先于 getch 被调用；getwch 返回一个组合键 ASCII（\x12），再由 getch 兜底
    mock_msvcrt.getwch = mock.Mock(return_value=0x12)
    assert _decode_key(_read_windows(0.05)) is Key.CtrlR  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# KeyboardInput 对象：构造 / read / restore
# ---------------------------------------------------------------------------

def test_keyboard_input_init_no_args(monkeypatch):
    """KeyboardInput() 无参构造（TuiApp / Test 兼容）+ _is_windows 跟随 sys.platform。"""
    monkeypatch.setattr("sys.platform", "linux")
    kb = KeyboardInput()
    assert kb._is_windows is False  # type: ignore[attr-defined]

    monkeypatch.setattr("sys.platform", "win32")
    kb2 = KeyboardInput()
    assert kb2._is_windows is True  # type: ignore[attr-defined]


def test_keyboard_input_restore_term_no_side_effects(monkeypatch):
    """restore_term() 跨平台：Windows 直接 return；Unix 调 tty.setcbreak 一次后重置 _raw_fd。"""
    monkeypatch.setattr("sys.platform", "win32")
    kb = KeyboardInput()
    kb.restore_term()  # Windows 路径无副作用，不抛异常

    monkeypatch.setattr("sys.platform", "linux")
    kb2 = KeyboardInput()
    spy = mock.Mock()
    if kbd.tty is None:
        monkeypatch.setattr(kbd, "tty", types.SimpleNamespace(setcbreak=spy))
    else:
        monkeypatch.setattr(kbd.tty, "setcbreak", spy)
    kb2._raw_fd = None  # type: ignore[attr-defined]  # 未 setraw：no-op
    kb2.restore_term()
    spy.assert_not_called()
    kb2._raw_fd = 3  # type: ignore[attr-defined]  # 已 setraw：恢复一次
    kb2.restore_term()
    spy.assert_called_once_with(3)
    assert kb2._raw_fd is None  # type: ignore[attr-defined]  # 幂等：第二次不重调
    kb2.restore_term()
    assert spy.call_count == 1
