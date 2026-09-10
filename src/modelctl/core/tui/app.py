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

"""TuiApp 主类（Task 2 起填充：渲染 Dashboard 面板）。

- run(smoke=True)：CI 冒烟模式，消费 3 个虚拟 key 事件后干净退出，返回 0；
  末尾走一次 `realize_render_once` 保证 snap 初始化 + 真实 render 路径发挥（测试冒烟）
- run(smoke=False)：真实主循环（loop_begin → render_once → loop_end），
  Task 2 只接通"快照 revalidate → render → print"骨架；键盘按键派发留给后续 Task
- KeyboardInterrupt（Ctrl-C）无 traceback：捕获后走 loop_end 并返回 130
"""

from __future__ import annotations

from rich.console import Console

from modelctl.core.tui.data import (
    ClusterSnapshot,
    HardwareSnapshot,
    LogsSnapshot,
    ModelsSnapshot,
    _SnapshotBase,
)
from modelctl.core.tui.keyboard import Key, KeyboardInput
from modelctl.core.tui.panels.detail import render as render_detail
from modelctl.core.tui.panels.main_dashboard import render as render_dashboard
from modelctl.core.tui.state import TUIState

SMOKE_KEY_SEQUENCE_LEN = 3  # smoke 模式消费的虚拟 key 事件数
DEFAULT_MIN_SIZE = (80, 24)  # 最小终端尺寸，console.size 探测失败时回落


class TuiApp:
    """TUI 主应用（持有 5 种快照缓存，按 active_view 派发至 panels）。"""

    def __init__(self, state: TUIState, console: Console) -> None:
        self.state = state
        self.console = console
        self.keyboard = KeyboardInput()
        self._theme = "dark"
        # 快照缓存（先 create-空实例，render 时 revalidate_if_expired 触发 fetch）；
        # T3 起启用：硬件 / 模型 / 集群 / 日志；monitor 留给 T5 面板
        self._snap: dict[str, _SnapshotBase] = {
            "hw": HardwareSnapshot(),
            "models": ModelsSnapshot(),
            "cluster": ClusterSnapshot(),
            "logs": LogsSnapshot(),
        }
        # 跟随 active_profile 重切日志尾行的名称缓存（避免每帧读 models.profiles[idx].name）
        self._logs_name: str = ""

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
            self.render_once()
            self.loop_end()
            return 0
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
        """渲染一帧：快照 revalidate → 按 active_view 派发 → console.print。

        T3 起 4 个 view 分派：
        - "dashboard"：T2 原逻辑
        - "detail"：render_detail（snapshot 5 元组 state/hw/models/logs）
        - 其他 view："plan"/"cluster"/"monitor" 暂走 dashboard 兜底（T5+ 再接具体面板）
        """
        self._revalidate()
        width, height = self._console_size()
        # 把 LogsSnapshot.name/tail 联动 active_profile 当前帧 ——
        # 在 revalidate 之前更新，避免缓存陈旧 name 读到错日志
        self._sync_logs_name()
        view = self.state.active_view
        if view == "detail":
            logs = self._snap["logs"]
            logs.name = self._logs_name
            logs.tail = 20  # detail log tab 固定 20 行
            self._snap["logs"].revalidate_if_expired()
            group = render_detail(
                self.state,
                self._snap["hw"],  # type: ignore[arg-type]
                self._snap["models"],  # type: ignore[arg-type]
                self._snap["logs"],  # type: ignore[arg-type]
                width=width,
                height=height,
                theme_id=self._theme,
            )
        elif view == "dashboard":
            group = render_dashboard(
                self.state,
                self._snap["hw"],  # type: ignore[arg-type]
                self._snap["models"],  # type: ignore[arg-type]
                self._snap["cluster"],  # type: ignore[arg-type]
                width=width,
                height=height,
                theme_id=self._theme,
            )
        else:
            # 未实现的 view 先走 dashboard 兜底，T5+ 再接具体面板
            group = render_dashboard(
                self.state,
                self._snap["hw"],  # type: ignore[arg-type]
                self._snap["models"],  # type: ignore[arg-type]
                self._snap["cluster"],  # type: ignore[arg-type]
                width=width,
                height=height,
                theme_id=self._theme,
            )
        self.console.clear()
        self.console.print(group)

    def _sync_logs_name(self) -> None:
        """从 `models.profiles[active_index]` 更新 LogsSnapshot 的 name（空回 "(null)" → ""）。

        不直接读 self._snap["logs"] 的 name——ModelsSnapshot 帧间可能切换 active_index，
        旧日志缓存已过期时（revalidate_if_expired）使用新 name；
        未过期时（TTL=1s 内）保留上次 fetch 结果，避免每帧重读文件。
        """
        models = self._snap["models"]  # type: ignore[assignment]
        profiles = getattr(models, "profiles", None) or []
        idx = self.state.active_index
        if idx < 0 or idx >= len(profiles):
            idx = 0 if profiles else -1
        if idx >= 0 and idx < len(profiles):
            item = profiles[idx]
            name = str(item.get("name", "") or "")
            self._logs_name = name

    def render_once(self) -> None:
        """对外渲染入口（Task 2 起 console.clear/print 组合）。"""
        self.realize_render_once()

    def _revalidate(self) -> None:
        """按 TTL invalidate 各 snapshot，触发 fetch（含 mock 环境自动降级）。"""
        for snap in self._snap.values():
            snap.revalidate_if_expired()

    def _console_size(self) -> tuple[int, int]:
        """探测 console.size；失败或 < 最小终端时回落 (80, 24)。"""
        try:
            w = int(self.console.width or 0)
            h = int(self.console.height or 0)
        except (TypeError, ValueError):
            w, h = 0, 0
        if w < DEFAULT_MIN_SIZE[0]:
            w = DEFAULT_MIN_SIZE[0]
        if h < DEFAULT_MIN_SIZE[1]:
            h = DEFAULT_MIN_SIZE[1]
        return w, h
