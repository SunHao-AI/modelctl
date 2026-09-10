#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/keyboard.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : TUI 键盘输入跨平台实现（Key 枚举 + msvcrt / tty+select 双通道 + ANSI 解码）
# ===============================================================================

"""TUI 键盘输入（跨平台）。

- Windows：msvcrt.kbhit() 轮询 + getwch()/getch()，组合键经 ASCII 码（0x12=Ctrl-R 等，无 ESC 前缀）
- Linux/macOS：tty.setraw(stdin) + select.select + os.read，扫描 ANSI escape 序列
- 超时返回 None；未接 Key 的按键返回 Key.Unknown

_TOCTOU 说明：_read_unix / _read_windows 为模块级函数，`KeyboardInput.read_key_block`
内 `self._is_windows = _is_windows()`（实例状态，随 __init__ 时的 sys.platform 快照），
但直接调用 `_read_unix(timeout)` 时按当前 sys.platform 实时判断——
这与测试中 monkeypatch sys.platform 后直接调模块函数的用法一致。
"""

from __future__ import annotations

import os
import sys
import time
from enum import Enum

try:  # pragma: no cover - 平台特定：Unix 专属
    import select
    import tty
except ImportError:  # pragma: no cover - Windows 下无 tty/select
    select = None
    tty = None

try:  # pragma: no cover - 平台特定：Windows 专属
    import msvcrt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    msvcrt = None

ESC = "\x1b"


class Key(Enum):
    Up = "up"
    Down = "down"
    Left = "left"
    Right = "right"
    Enter = "enter"
    Esc = "esc"
    CtrlR = "ctrl-r"
    CtrlU = "ctrl-u"
    CtrlF = "ctrl-f"
    CtrlB = "ctrl-b"
    Digit1 = "1"
    Digit2 = "2"
    Digit3 = "3"
    Digit4 = "4"
    Digit5 = "5"
    T = "t"
    H = "h"
    Q = "q"
    D = "d"
    F = "f"
    S = "s"
    J = "j"
    K = "k"
    G = "g"
    Colon = ":"
    QuestionMark = "?"
    Tab = "tab"
    ShiftTab = "shift-tab"
    PgUp = "pgup"
    PgDn = "pgdn"
    Unknown = "unknown"


# ---------------------------------------------------------------------------
# 解码表（共用）
# ---------------------------------------------------------------------------

# 1 字节 ASCII → Key（text/数字/单键）
_TEXT_KEYS: dict[str, Key] = {
    "\t": Key.Tab,
    "\r": Key.Enter,
    "1": Key.Digit1,
    "2": Key.Digit2,
    "3": Key.Digit3,
    "4": Key.Digit4,
    "5": Key.Digit5,
    "t": Key.T, "h": Key.H, "q": Key.Q, "d": Key.D, "f": Key.F, "s": Key.S,
    "j": Key.J, "k": Key.K, "g": Key.G,
    ":": Key.Colon,
    "?": Key.QuestionMark,
}

# 1 字节 Ctrl 组合键码（0x02..0x19，无前缀 ESC；Windows getch() 与 Unix raw 共用）
_CTRL_CODES: dict[int, Key] = {
    0x02: Key.CtrlB, 0x06: Key.CtrlF, 0x12: Key.CtrlR, 0x15: Key.CtrlU,
}

# 2+ 字节 ANSI 序列（前缀 ESC）→ Key
_ANSI_TABLE: dict[str, Key] = {
    ESC + "[A": Key.Up, ESC + "[B": Key.Down,
    ESC + "[C": Key.Right, ESC + "[D": Key.Left,
    ESC + "[Z": Key.ShiftTab,
    ESC + ESC + "[C": Key.PgDn, ESC + ESC + "[D": Key.PgUp,
}


def _decode_key(raw: bytes) -> Key:
    """字节码 → Key。COMBO 表 + ANSI 表共用。

    - 1 字节：ASCII 文本/数字 → Key 映射 + CTRL 组合键码表；未接 → Key.Unknown
    - 2+ 字节：ESC 序列逐字对比 _ANSI_TABLE；仅 ESC 单独 → Key.Esc
    """
    if len(raw) == 1:
        b0: int = raw[0]
        if b0 == 0x1B:
            return Key.Esc  # 裸 ESC（_read_unix 已完成序列拆分）
        ch: str = raw.decode("latin-1")
        key: Key | None = _TEXT_KEYS.get(ch)
        if key is None:
            key = _CTRL_CODES.get(b0)
        return key if key is not None else Key.Unknown
    if raw.startswith(ESC.encode("latin-1")):
        s: str = raw.decode("latin-1")
        return _ANSI_TABLE.get(s, Key.Unknown)
    return Key.Unknown


# ---------------------------------------------------------------------------
# 平台检测
# ---------------------------------------------------------------------------


def _is_windows() -> bool:
    """检测 Windows（读当前 sys.platform，便于测试 monkeypatch）。"""
    return sys.platform.startswith("win") or (
        sys.platform == "linux" and os.name == "nt"
    )


# ---------------------------------------------------------------------------
# Windows 输入路径
# ---------------------------------------------------------------------------


def _read_windows(timeout: float) -> bytes | None:
    """Windows msvcrt 路径：kbhit() 轮询 + getwch()/getch()。

    - timeout < 0：立即检测——kbhit() False → None（等待语义由上层控制）
    - timeout == 0：单次非阻塞检测（select 单次 OK）
    - timeout > 0：短轮询循环，time.monotonic 计时
    - getwch() 返回 str（Python 3 unicode），单字节 ASCII → latin-1 编码
    - getch() 返回 int（组合键 ASCII 码 0x12=Ctrl-R 等，无 ESC 前缀，3-letter 序列）
    """
    if not _is_windows():
        return None
    if msvcrt is None:  # pragma: no cover
        return None

    deadline: float | None = None
    if timeout is not None and timeout >= 0:
        deadline = time.monotonic() + timeout

    while True:
        try:
            if not msvcrt.kbhit():
                if deadline is None:
                    return None  # 无 deadline 且无按键 → 非阻塞超时
                if time.monotonic() >= deadline:
                    return None  # 阻塞超时
                time.sleep(0.005)
                continue
            ch = msvcrt.getwch()  # str（unicode）或 int（小码/错误时回退）
            if isinstance(ch, str):
                code: int = ord(ch[0]) if ch else 0
            else:
                code = int(ch) & 0xFF
            if code < 0x20 or code in (0x1B, 0x7F):
                # 组合键 / ESC / 控制字：getwch 只返回单字符，需明确要求 getch() 取真实码
                code = msvcrt.getch()  # int（0x12=Ctrl-R / 0x02-0x19 Ctrl 组合键 等）
            return bytes([code & 0xFF])
        except Exception:  # pragma: no cover - 防御 msvcrt 异常
            return None


# ---------------------------------------------------------------------------
# Unix 输入路径
# ---------------------------------------------------------------------------


def _read_unix(timeout: float, *, want_tty: bool = False) -> bytes | None:
    """Linux/macOS 路径：tty.setraw(stdin) + select.select + os.read。

    - 首次 select：timeout 决定剩余可用时间
    - ESC 前缀序列：首次读到 \\x1b 后，需再次 select + read 拼接
      （os.read 单次可能只读 1 字节）；两次 read(1) 按 [ESC][X] 拼接匹配 ANSI 表
    - Esc 裸键（第二次 select 无数据）→ \\x1b（单 ESC → Key.Esc）
    - 单次 read 空字节（EOF）→ 立即返回当前累计（不阻塞）
    - 超时/无按键 → None

    setraw 仅在有效 fd 且当前为 TTY 时启用；非 TTY 环境（CI/管道）跳过 setraw，
    _read_unix 仍可通过 mock 测试字节映射。
    """
    if want_tty and _is_windows():
        return None  # 仅想真 TTY 路径时：Windows 走 msvcrt（见 _read_windows）
    fd = sys.stdin.fileno()
    # 仅当 stdin 为 TTY 时 setraw（避免 CI 下对非 TTY fd 设 raw 报错）
    raw_fd: int | None = None
    if _is_tty(fd):
        try:
            tty.setraw(fd)
            raw_fd = fd
        except Exception:  # pragma: no cover - 防御非终端环境
            raw_fd = None
    try:
        buf: bytes = b""
        deadline = time.monotonic() + timeout

        def _poll(first: bool) -> bytes | None:
            """select + read(1)：返回 1 字节（或 EOF 空字节），超时/无数据 → None"""
            if first:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    return None
                r, _, _ = select.select([sys.stdin], [], [], max(wait, 0.0))
            else:
                r, _, _ = select.select([sys.stdin], [], [], 0.0)
            if not r:
                return None
            return os.read(fd, 1)

        first = _poll(True)
        if first is None:
            return None  # 首次 select 超时
        if first == b"":
            return None  # EOF
        buf += first

        # ESC 前缀：依次读后续字节（最多 4 次，构成最多 5 字节序列）
        if first[0] == 0x1B:
            for _ in range(4):
                chunk = _poll(False)
                if chunk is None or chunk == b"":  # 无后续数据 / EOF
                    break
                buf += chunk
                if _buffer_matches_ansi(buf):
                    break
    finally:
        if raw_fd is not None:
            try:
                tty.setcbreak(raw_fd)
            except Exception:  # pragma: no cover
                pass
    return buf


def _buffer_matches_ansi(buf: bytes) -> bool:
    """缓冲是否已完整匹配某条 _ANSI_TABLE 序列（解码侧验证，避免过度读取）。"""
    try:
        s = buf.decode("latin-1")
    except UnicodeDecodeError:  # pragma: no cover
        return False
    return s in _ANSI_TABLE


def _is_tty(fd: int) -> bool:
    """检测 fd 是否为 TTY（CI/管道环境返回 False，跳过 setraw）。"""
    try:
        return os.isatty(fd)
    except (OSError, ValueError):  # pragma: no cover
        return False


# ---------------------------------------------------------------------------
# 统一输入接口
# ---------------------------------------------------------------------------


class KeyboardInput:
    """跨平台键盘输入。

    - read_key_block(timeout)：阻塞轮询按键，超时返回 None
    - restore_term()：退出前恢复终端 cooked 模式（Unix 专属；Windows 无 raw 模式）

    __init__ 无入参，兼容 TuiApp / Test 直接构造。
    """

    def __init__(self) -> None:
        self._is_windows: bool = sys.platform.startswith("win")
        self._raw_fd: int | None = None  # Unix raw 模式 fd（setraw 后置位）

    def read_key_block(self, timeout: float) -> Key | None:
        """轮询按键最多 timeout 秒，超时返回 None。"""
        raw: bytes | None
        if self._is_windows:
            raw = _read_windows(timeout)
        else:
            raw = _read_unix(timeout)
        if raw is None:
            return None
        return _decode_key(raw)

    def restore_term(self) -> None:
        """恢复终端 cooked 模式（幂等；Windows 无 raw 模式可恢复，直接返回）。"""
        if self._is_windows:
            return
        if self._raw_fd is not None:
            try:
                tty.setcbreak(self._raw_fd)
            except Exception:  # pragma: no cover
                pass
            self._raw_fd = None
