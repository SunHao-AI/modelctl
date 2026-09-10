#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/keyboard.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : TUI 键盘输入骨架（Key 枚举 + read_key_block 占位，Task 1 完整实现）
# ===============================================================================

"""TUI 键盘输入骨架。

Task 0 仅提供 Key 枚举与占位实现；Task 1 实现跨平台逻辑：
- Windows：msvcrt.kbhit() 轮询 + getwch()/getch()，组合键经 ASCII 码（0x12=Ctrl-R 等）
- Linux/macOS：tty.setraw(stdin) + select.select + os.read，扫描 ANSI escape 序列
"""

from __future__ import annotations

from enum import Enum


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


class KeyboardInput:
    """键盘输入骨架。

    Task 0：read_key_block 直接返回 Key.Q（占位，保证 smoke 序列可驱动）；
    Task 1：真实跨平台轮询实现，取消 raw 模式调 restore_term()。
    """

    def read_key_block(self, timeout: float) -> Key | None:
        """Poll for a key press up to timeout seconds. Returns None on timeout."""
        return Key.Q  # placeholder in Task 0

    def restore_term(self) -> None:
        """恢复终端 cooked 模式（Task 1 实现 tty/unraw 逻辑）。"""
        return None
