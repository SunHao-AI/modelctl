#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/theme.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : TUI 三套主题（dark / light / high-contrast）与切换函数
# ===============================================================================

"""TUI 主题定义。

三套主题与 core/colors.py 的语义色词汇对齐；主题 ID 切换函数供 `t` 键调用。
Task 0 先给出结构；实际 Style 渲染接线在 Task 2 起随面板落地。
"""

from __future__ import annotations

THEME_IDS = ["dark", "light", "high-contrast"]
DEFAULT_THEME = "dark"

# 每套主题的富文本 Style 描述符（key → rich Style 描述符）
_RICH_THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "title": "bold cyan",
        "success": "green",
        "error": "red",
        "warning": "yellow",
        "dim": "dim",
        "keybar": "cyan",
    },
    "light": {
        "title": "bold blue",
        "success": "green",
        "error": "red",
        "warning": "magenta",
        "dim": "dim",
        "keybar": "blue",
    },
    "high-contrast": {
        "title": "bold bright_magenta",
        "success": "bright_green",
        "error": "bright_red",
        "warning": "bright_yellow",
        "dim": "bright_black",
        "keybar": "bright_cyan",
    },
}


def theme_id_for(theme_index: int) -> str:
    """按索引取主题 ID（越界取模回绕）。"""
    return THEME_IDS[theme_index % len(THEME_IDS)]


def cycle_theme(current: str) -> str:
    """current 的下一个主题（循环）。未知 current 回落到 DEFAULT_THEME 的下一个。"""
    try:
        idx = THEME_IDS.index(current)
    except ValueError:
        idx = THEME_IDS.index(DEFAULT_THEME)
    return THEME_IDS[(idx + 1) % len(THEME_IDS)]


def get_rich_theme(theme_id: str) -> dict:
    """取主题 Style 描述符合集；未知 ID 回落 dark。"""
    return dict(_RICH_THEMES.get(theme_id, _RICH_THEMES[DEFAULT_THEME]))
