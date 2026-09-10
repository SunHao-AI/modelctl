#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/panels/base.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : TUI 面板公共 helper（section_title / error_flash / keybar 骨架）
# ===============================================================================

"""TUI 面板公共 helper 骨架。

- section_title：区块标题条（含 `-` 线分隔）
- error_flash：错误/告警闪烁条（Task 2 起接 state.errors）
- keybar：底部快捷键提示条（KeyboardPrompt 占位）

含 CJK 的输出一律用 core/colors.py 的 display_width/pad_width 对齐。
"""

from __future__ import annotations

from rich.text import Text

from modelctl.core.colors import display_width, pad_width


def section_title(title: str, width: int) -> Text:
    """区块标题条：`── <title> ──` 补宽至 width（CJK 感知）。"""
    prefix = "-- "
    suffix = " --"
    body = pad_width(title, max(0, width - display_width(prefix) - display_width(suffix)))
    text = Text()
    text.append(prefix, style="dim")
    text.append(body, style="bold cyan")
    text.append(suffix, style="dim")
    return text


def error_flash(errors: list[str], width: int) -> Text | None:
    """错误闪烁条：无错误返回 None；有错误取首条按 width 截位段补宽。"""
    if not errors:
        return None
    first = str(errors[0])
    text = Text()
    text.append("! ", style="bold red")
    content = pad_width(first, max(0, width - 2))
    if display_width(first) > width - 2:  # 超宽不截断，只留 0 余量
        content = first
    text.append(content, style="red")
    return text


def keybar() -> Text:
    """底部快捷键提示条（占位；Task 2 起按视图输出不同 keybar）。"""
    text = Text()
    text.append("q 退出 | ? 帮助 | t 切换主题", style="cyan")
    return text


__all__ = ["error_flash", "keybar", "section_title"]
