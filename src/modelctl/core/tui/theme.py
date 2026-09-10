#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/theme.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/10 10:00
# @Desc   : TUI 三套主题（dark / light / high-contrast）与切换函数（含 theme 持久化）
# ===============================================================================

"""TUI 主题定义。

三套主题与 core/colors.py 的语义色词汇对齐；主题 ID 切换函数供 `t` 键调用。
Task 0 先给出结构；实际 Style 渲染接线在 Task 2 起随面板落地。

Task 6 追加 theme 持久化（load_theme / save_theme / theme_file_path）——
存 <cache_dir>/tui.theme，格式 {"theme": "<id>"}；损坏回落 DEFAULT_THEME="dark"。
"""

from __future__ import annotations

import json
from pathlib import Path

from modelctl.core.paths import cache_dir

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


# ---------------------------------------------------------------------------
# Task 6：theme 持久化
#
# 存储位置：`<cache_dir>/tui.theme`（见 modelctl/core/paths.py `cache_dir()`，
# 单点解析，支持 CACHE_DIR 环境变量覆盖）。
# 文件格式：`{"theme": "<id>"}`，UTF-8，单对象。
# 损坏语义：任何加载异常（IO / 解码 / 非 JSON / 非对象 / 值不在词表）一律
#           fallback 到 `DEFAULT_THEME="dark"`，不打 traceback，不影响主循环。
# ---------------------------------------------------------------------------

#: 持久化文件名（相对 `cache_dir()`）。
THEME_FILE = "tui.theme"


def theme_file_path() -> Path:
    """theme 持久化文件路径：`<cache_dir>/tui.theme`。

    调用方负责 `mkdir(parents=True, exist_ok=True)`（save_theme 内部已处理）。
    """
    return cache_dir() / THEME_FILE


def load_theme(path: Path | None = None) -> str:
    """读 theme 持久化文件。

    - 文件不存在 → 返回 `DEFAULT_THEME`
    - 文件损坏（非 JSON / 非对象 / value 不在 THEME_IDS）→ 返回 `DEFAULT_THEME`
      （fallback 不打 traceback）
    - 正常 → 返回存储的主题 ID（已在 THEME_IDS 内）
    """
    p = path or theme_file_path()
    if not p.exists():
        return DEFAULT_THEME
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return DEFAULT_THEME
    if not isinstance(data, dict):
        return DEFAULT_THEME
    tid = data.get("theme", DEFAULT_THEME)
    if tid not in THEME_IDS:
        return DEFAULT_THEME
    return tid


def save_theme(theme_id: str, path: Path | None = None) -> bool:
    """写 theme 持久化文件。

    - `theme_id` 不在 THEME_IDS → 不写，返回 False
    - 写成功（含父目录 mkdir parents）→ 返回 True
    - 写入失败（权限/IO/OSError）→ 异常不抛出，返回 False
    - 文件权限：POSIX 平台 `chmod 0o600`；Windows 上 `chmod` 有限支持，静默忽略
    - JSON 结构: `{"theme": "<id>"}`
    """
    if theme_id not in THEME_IDS:
        return False
    p = path or theme_file_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"theme": theme_id}, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            p.chmod(0o600)  # POSIX：600 防同机他人读；Windows 静默忽略
        except OSError:
            pass
        return True
    except OSError:
        return False
