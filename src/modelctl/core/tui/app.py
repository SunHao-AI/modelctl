#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/app.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : TuiApp 主类骨架（主循环 / 渲染入口 / smoke 模式）
# ===============================================================================

"""TuiApp 主类骨架（Task 0）。

- run(smoke=True)：CI 冒烟模式，消费 3 个虚拟 key 事件后干净退出，返回 0，不渲染
- run(smoke=False)：真实主循环（loop_begin → 键盘轮询 → render_once → loop_end），
  Task 1/2 起填充键盘派发与面板渲染
- KeyboardInterrupt（Ctrl-C）无 traceback：捕获后走 loop_end 并返回 130
- 真实模式占位：Task 0 无可用面板，smoke=False 直接返回 0（Task 2 起渲染 Dashboard）
"""

from __future__ import annotations

from rich.console import Console

from modelctl.core.tui.keyboard import KeyboardInput, Key
from modelctl.core.tui.state import TUIState

SMOKE_KEY_SEQUENCE_LEN = 3  # smoke 模式消费的虚拟 key 事件数


class TuiApp:
    """TUI 主应用骨架。"""

    def __init__(self, state: TUIState, console: Console) -> None:
        self.state = state
        self.console = console
        self.keyboard = KeyboardInput()
        self._theme = "dark"

    def run(self, *, smoke: bool = False) -> int:
        """主入口。smoke=True 消费 3 个 key 事件后退出（CI 模式），返回 0。"""
        try:
            if smoke:
                for _ in range(SMOKE_KEY_SEQUENCE_LEN):
                    key = self.keyboard.read_key_block(timeout=0.0)
                    if key == Key.Q:
                        break
                self.realize_render_once()
                self.loop_end()
                return 0
            self.loop_begin()
            return 0  # Task 0 占位；真实主循环（键盘派发 + 面板轮询）在 Task 1/2 填充
        except KeyboardInterrupt:
            # 兜底：任何锁屏/渲染阶段的 KeyboardInterrupt 都不允许产生 traceback
            self.loop_end()
            return 130

    def loop_begin(self) -> None:
        """进入主循环前置（Task 1 起做 raw 模式 / size 探测）。"""

    def loop_end(self) -> None:
        """退出主循环后置（Task 1 起 restore_term()）。"""
        self.keyboard.restore_term()

    def realize_render_once(self) -> None:
        """渲染一帧（Task 0 空操作；Task 2 起按 active_view 派发至 panels）。"""

    def render_once(self) -> None:
        """对外渲染入口（Task 2 起 console.print/clear 组合）。"""
        self.realize_render_once()
