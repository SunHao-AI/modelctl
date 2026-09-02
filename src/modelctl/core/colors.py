#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/colors.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/1 10:00
# @Desc   : CLI 彩色输出核心模块
# ===============================================================================

"""CLI 彩色输出核心模块。

功能：
- 自动检测终端颜色能力（NO_COLOR / NOansi / TERM=dumb / CI / FORCE_COLOR / 256 色 / TrueColor）
- 语义化样式映射（STATUS_RUNNING / SUCCESS / ERROR / WARNING / INFO / TABLE_HEADER / DIM）
- 全局开关与配色方案自定义
- 简单易用的函数 API（cprint / bold / green / red 等）
- 非 TTY（管道/重定向/测试）自动回退纯文本

用法：
    from modelctl.core.colors import cprint, status_color, bold

    cprint("服务已启动", style="success")
    print(bold("标题"))
    color_enabled()
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TextIO

__all__ = [
    "ColorScheme",
    "Color",
    "display_width",
    "pad_width",
    "style_of",
    "color_enabled",
    "set_color_enabled",
    "get_scheme",
    "load_scheme_from_env",
    "status_color",
    "reset_color",
    "format_status",
    "cprint",
    "bold",
    "dim",
    "italic",
    "underline",
    "cyan",
    "magenta",
    "blue",
    "yellow",
    "red",
    "green",
]


# ═══════════════════════════════════════════════════
# 终端显示宽度（CJK 字符按 2 列、ASCII 按 1 列）
# ═══════════════════════════════════════════════════


def _is_wide(ord: int) -> bool:
    """判断 Unicode codepoint 在等宽终端是否占 2 列（全角/中文）。

    覆盖常见 CJK 区段（未尝试区分窄角变体，CLI 输出现实中不会出现）：
    CJK 统一表意文字、扩展 A/B、谚文音节、全角符号、象形文字补充、CJK 康熙部首。
    """
    return (
        0x1100 <= ord <= 0x115F        # Hangul 兼容 Jamo
        or 0x2E80 <= ord <= 0x303E      # CJK 部首补充
        or 0x3041 <= ord <= 0x33FF      # 日文/韩文/中文标点+假名
        or 0x3400 <= ord <= 0x4DBF      # CJK 扩展 A
        or 0x4E00 <= ord <= 0x9FFF      # CJK 统一表意文字
        or 0xA000 <= ord <= 0xA4CF      # 彝文
        or 0xAC00 <= ord <= 0xD7A3      # 谚文音节
        or 0xF900 <= ord <= 0xFAFF      # CJK 兼容表意文字
        or 0xFE30 <= ord <= 0xFE4F      # CJK 兼容形式
        or 0xFF00 <= ord <= 0xFF60      # 全角 ASCII
        or 0xFFE0 <= ord <= 0xFFE6      # 全角符号
        or 0x20000 <= ord <= 0x3FFFD    # CJK 扩展 B+（含 B-H）
    )


def display_width(text: str) -> int:
    """返回文本在等宽终端的显示列数（含 CJK 双宽调整）。"""
    w = 0
    for ch in text:
        w += 2 if _is_wide(ord(ch)) else 1
    return w


def pad_width(text: str, width: int, *, align: str = "left") -> str:
    """按显示宽度填充/截断文本。

    - align="left":  左侧对齐，右侧补空格到 width 列
    - align="right": 右侧对齐，左侧补空格到 width 列
    若 display_width(text) > width，原样返回（不截断，避免破坏路径）。
    """
    gap = max(0, width - display_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


#: 日志级别名称 → ANSI 颜色（供 loguru console format 的 {level} 使用）。
#: 键为转换后的 hex key，如 {TRACE}、{DEBUG}、{INFO}、{SUCCESS}、{WARNING}、{ERROR}、{CRITICAL}。
LOGURU_COLORS: dict[str, str] = {
    "{TRACE}": "\x1b[2m\x1b[37m",
    "{DEBUG}": "\x1b[2m\x1b[37m",
    "{INFO}": "\x1b[32m",
    "{SUCCESS}": "\x1b[1;32m",
    "{WARNING}": "\x1b[33m",
    "{ERROR}": "\x1b[1;31m",
    "{CRITICAL}": "\x1b[1;41;97m",
}
LOGURU_RESET = "\x1b[0m"

# ANSI 转义码
_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_ITALIC = "\x1b[3m"
_UNDERLINE = "\x1b[4m"


# ═══════════════════════════════════════════════════
# 终端颜色能力检测
# ═══════════════════════════════════════════════════


def _detect_support(force: bool | None = None) -> bool:
    """自动检测终端是否支持 ANSI 颜色。

    检测顺序（任一为否即关闭）：
    1. modelctl_no_color（值 true/1/yes/on，忽略大小写）→ 关闭
    2. FORCE_COLOR（存在且非 "0"）→ 强制开启
    3. NO_COLOR（存在且任意值）→ 关闭
    4. MODELCTL_NO_COLOR（同上）→ 关闭
    5. TERM == "dumb" → 关闭
    6. CI（存在且非空且非 "0"）→ 关闭
    7. sys.stdout.isatty() → 非终端则关闭
    """
    if force is not None:
        return force
    # 1. modelctl 自身开关
    nc = os.environ.get("modelctl_no_color", "").strip().lower()
    if nc in ("1", "true", "yes", "on"):
        return False
    # 2. FORCE_COLOR 强制开启
    if "FORCE_COLOR" in os.environ and os.environ["FORCE_COLOR"] != "0":
        return True
    # 3. NO_COLOR 标准约定
    if "NO_COLOR" in os.environ and os.environ["NO_COLOR"].strip() != "":
        return False
    # 4. MODELCTL_NO_COLOR
    if "MODELCTL_NO_COLOR" in os.environ and os.environ["MODELCTL_NO_COLOR"].strip() != "":
        return False
    # 5. TERM=dumb
    if os.environ.get("TERM", "") == "dumb":
        return False
    # 6. CI 环境
    if os.environ.get("CI", "") and os.environ["CI"] != "0":
        return False
    # 7. TTY 检查
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


#: 模块级状态：颜色是否已启用（--no-color / FORCE_COLOR 等可覆盖）。
_enabled: bool | None = None  # None = 未显式设置，首次调用时自动检测


def set_color_enabled(v: bool | None) -> None:
    """全局开关切换。传 bool 强制开/关；传 None 恢复自动检测。"""
    global _enabled
    _enabled = v


def color_enabled() -> bool:
    """当前是否输出 ANSI 颜色。"""
    global _enabled
    if _enabled is None:
        _enabled = _detect_support()
    return _enabled


# ═══════════════════════════════════════════════════
# 颜色数据模型
# ═══════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class Color:
    """单个颜色/样式描述（fg/bg/bold/dim/italic/underline）。"""

    name: str
    fg: str | None = None
    bg: str | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False

    def to_ansi(self) -> str:
        """拼接 ANSI 转义序列（不含末尾 reset）。"""
        parts: list[str] = []
        if self.bold:
            parts.append("1")
        if self.dim:
            parts.append("2")
        if self.italic:
            parts.append("3")
        if self.underline:
            parts.append("4")
        if self.fg:
            parts.append(self.fg)
        if self.bg:
            parts.append(self.bg)
        return "\x1b[" + ";".join(parts) + "m" if parts else ""


# ═══════════════════════════════════════════════════
# 语义样式映射
# ═══════════════════════════════════════════════════

#: NAME(语义样式名) → Color
_STYLE_MAP: dict[str, Color] = {
    # 基础状态
    "STATUS_RUNNING": Color("STATUS_RUNNING", fg="32", bold=True),
    "STATUS_EXTERNAL": Color("STATUS_EXTERNAL", fg="36", bold=True),
    "STATUS_STOPPED": Color("STATUS_STOPPED", fg="90"),
    "STATUS_ERROR": Color("STATUS_ERROR", fg="31", bold=True),
    "STATUS_WARNING": Color("STATUS_WARNING", fg="33"),
    "STATUS_SKIPPED": Color("STATUS_SKIPPED", fg="90"),
    "STATUS_HEALTHY": Color("STATUS_HEALTHY", fg="32", bold=True),
    "STATUS_UNHEALTHY": Color("STATUS_UNHEALTHY", fg="31"),
    "STATUS_NA": Color("STATUS_NA", fg="90"),
    # 信息类别
    "SUCCESS": Color("SUCCESS", fg="32", bold=True),
    "INFO": Color("INFO", fg="34"),
    "ERROR": Color("ERROR", fg="31"),
    "WARNING": Color("WARNING", fg="33"),
    "DEBUG": Color("DEBUG", fg="37", dim=True),
    "NOTE": Color("NOTE", fg="37", italic=True),
    "TITLE": Color("TITLE", fg="36", bold=True),
    "SECTION": Color("SECTION", fg="36"),
    "COMMAND": Color("COMMAND", fg="37", bold=True),
    # 表格
    "TABLE_HEADER": Color("TABLE_HEADER", fg="36", bold=True),
    "TABLE_SEP": Color("TABLE_SEP", fg="90"),
    "TABLE_DIM": Color("TABLE_DIM", fg="90"),
    # 通用修饰
    "BOLD": Color("BOLD", bold=True),
    "DIM": Color("DIM", dim=True),
    "ITALIC": Color("ITALIC", italic=True),
    "UNDERLINE": Color("UNDERLINE", underline=True),
    "CYAN": Color("CYAN", fg="36"),
    "MAGENTA": Color("MAGENTA", fg="35"),
    "BLUE": Color("BLUE", fg="34"),
    "YELLOW": Color("YELLOW", fg="33"),
    "RED": Color("RED", fg="31"),
    "GREEN": Color("GREEN", fg="32"),
}


@dataclass(frozen=True, slots=True)
class ColorScheme:
    """配色方案：语义名 → Color 映射。"""

    styles: dict[str, Color]

    def get(self, name: str) -> Color:
        """按语义名取颜色，未知返回 DIM。"""
        return self.styles.get(name, _STYLE_MAP["DIM"])


DEFAULT_SCHEME = ColorScheme(dict(_STYLE_MAP))
_active_scheme = DEFAULT_SCHEME


def get_scheme() -> ColorScheme:
    """返回当前配色方案。"""
    return _active_scheme


def _apply_scheme(scheme: ColorScheme) -> None:
    global _active_scheme
    _active_scheme = scheme


def _parse_color_spec(spec: str) -> Color:
    """解析 "STATUS_RUNNING=green|bold" 形式的颜色规格。

    格式：key=color_spec，color_spec 为名称列表（| 或逗号或分号分隔），如 "green|bold"、"32;1"。
    支持：fg 色号（30-37/90-97/38;5;N/38;2;r;g;b）、bg 色号（40-47/100-107）、
         命名颜色（black/red/green/yellow/blue/magenta/cyan/white/gray 及 bright 前缀）、
         样式名（bold/dim/italic/underline）。
    """
    # 命名颜色 → 标准 ANSI 前景/背景色号
    _NAMED_FG = {
        "black": "30", "red": "31", "green": "32", "yellow": "33",
        "blue": "34", "magenta": "35", "cyan": "36", "white": "37",
        "gray": "90", "grey": "90", "bright_red": "91", "bright_green": "92",
        "bright_yellow": "93", "bright_blue": "94", "bright_magenta": "95",
        "bright_cyan": "96", "bright_white": "97",
    }
    _NAMED_BG = {
        "on_black": "40", "on_red": "41", "on_green": "42", "on_yellow": "43",
        "on_blue": "44", "on_magenta": "45", "on_cyan": "46", "on_white": "47",
        "on_gray": "100", "on_grey": "100", "on_bright_red": "101",
        "on_bright_green": "102", "on_bright_yellow": "103", "on_bright_blue": "104",
        "on_bright_magenta": "105", "on_bright_cyan": "106", "on_bright_white": "107",
    }
    spec_stripped = spec.strip()
    if "=" in spec_stripped:
        key, _, value = spec_stripped.partition("=")
    else:
        key, value = "CUSTOM", spec_stripped
    key = key.strip()
    # 预定义语义名
    if key.upper() in _STYLE_MAP:
        base = _STYLE_MAP[key.upper()]
    else:
        base = Color(key)
    # 解析 value：按 | 或 , 或 分号 分割
    for part in value.replace(",", "|").replace(";", "|").split("|"):
        part = part.strip()
        low = part.lower()
        if not part:
            continue
        if low in ("bold", "1"):
            base = Color(base.name, base.fg, base.bg, True, base.dim, base.italic, base.underline)
        elif low in ("dim", "2"):
            base = Color(base.name, base.fg, base.bg, base.bold, True, base.italic, base.underline)
        elif low in ("italic", "3"):
            base = Color(base.name, base.fg, base.bg, base.bold, base.dim, True, base.underline)
        elif low in ("underline", "4"):
            base = Color(base.name, base.fg, base.bg, base.bold, base.dim, base.italic, True)
        elif low in _NAMED_FG:
            base = Color(base.name, _NAMED_FG[low], base.bg, base.bold, base.dim, base.italic, base.underline)
        elif low in _NAMED_BG:
            base = Color(base.name, base.fg, _NAMED_BG[low], base.bold, base.dim, base.italic, base.underline)
        elif low.startswith("bright_"):
            base = Color(base.name, _NAMED_FG.get(low, base.fg), base.bg, base.bold, base.dim, base.italic, base.underline)
        elif low in ("38;5", "256"):
            # 256 色：已在 part 中形如 "38;5;N"
            base = Color(base.name, part, base.bg, base.bold, base.dim, base.italic, base.underline)
        elif part[:6] == "38;2;" or part[:6] == "48;2;":
            # 24-bit TrueColor
            base = Color(base.name, part if part[:6] == "38;2;" else base.fg,
                         part if part[:6] == "48;2;" else base.bg,
                         base.bold, base.dim, base.italic, base.underline)
        elif part.isdigit() and 30 <= int(part) <= 37:
            base = Color(base.name, part, base.bg, base.bold, base.dim, base.italic, base.underline)
        elif part.isdigit() and 90 <= int(part) <= 97:
            base = Color(base.name, part, base.bg, base.bold, base.dim, base.italic, base.underline)
        elif part.isdigit() and 40 <= int(part) <= 47:
            base = Color(base.name, base.fg, part, base.bold, base.dim, base.italic, base.underline)
        elif part.isdigit() and 100 <= int(part) <= 107:
            base = Color(base.name, base.fg, part, base.bold, base.dim, base.italic, base.underline)
        elif part.startswith("38"):
            base = Color(base.name, part, base.bg, base.bold, base.dim, base.italic, base.underline)
        elif part.startswith("48"):
            base = Color(base.name, base.fg, part, base.bold, base.dim, base.italic, base.underline)
    return base


def load_scheme_from_env() -> bool:
    """从 MODELCTL_COLORS 环境变量加载自定义配色方案。

    MODELCTL_COLORS 格式：分号分隔的键值对，如
        "STATUS_RUNNING=magenta|bold;ERROR=red;WARNING=yellow|bold"
    返回 True 表示成功加载自定义方案，False 表示未设置（保持默认）。
    """
    raw = os.environ.get("MODELCTL_COLORS", "").strip()
    if not raw:
        return False
    merged = dict(_STYLE_MAP)
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        color = _parse_color_spec(chunk)
        merged[color.name] = color
    _apply_scheme(ColorScheme(merged))
    return True


# ═══════════════════════════════════════════════════
# 格式化函数
# ═══════════════════════════════════════════════════


def style_of(name: str) -> str:
    """按语义名取 ANSI 色码（未启用或未知返回空串）。大小写不敏感。"""
    if not color_enabled():
        return ""
    key = (name or "").upper()
    return get_scheme().styles.get(key, Color("")).to_ansi()


def reset_color() -> str:
    """返回 ANSI 重置序列（未启用返回空串）。"""
    if not color_enabled():
        return ""
    return _RESET


def status_color(state: str) -> str:
    """实例状态 → ANSI 色码。未知状态回退 DIM。

    映射：
    - 运行中 → STATUS_RUNNING（绿色加粗）
    - 已停止 → STATUS_STOPPED（灰色）
    - 未就绪 → STATUS_NA（灰色）
    - 正常   → STATUS_HEALTHY（绿色加粗）
    - 无响应 → STATUS_UNHEALTHY（红色）
    - PID 异常 → STATUS_ERROR（红色加粗）
    - 未知   → DIM
    """
    mapping = {
        "运行中": "STATUS_RUNNING",
        "已外部启动": "STATUS_EXTERNAL",
        "已停止": "STATUS_STOPPED",
        "未就绪": "STATUS_NA",
        "正常": "STATUS_HEALTHY",
        "无响应": "STATUS_UNHEALTHY",
        "PID 异常": "STATUS_ERROR",
        "unknown": "STATUS_NA",
    }
    return style_of(mapping.get(state, "DIM"))


def loguv_color(level_name: str) -> str:
    """loguru 级别名称 → ANSI 色码（供 console handler format 使用）。"""
    if not color_enabled():
        return ""
    return LOGURU_COLORS.get("{" + level_name.upper() + "}", "")


def format_status(text: str, state: str) -> str:
    """按状态语义着色文本（非 TTY 时原样返回）。"""
    if not color_enabled():
        return text
    return f"{status_color(state)}{text}{_RESET}"


# ═══════════════════════════════════════════════════
# 便捷函数 API
# ═══════════════════════════════════════════════════


def _apply(text: str, name: str) -> str:
    if not color_enabled() or not text:
        return text
    code = style_of(name)
    if not code:
        return text
    return f"{code}{text}{_RESET}"


def cprint(
    text: str = "",
    style: str | None = None,
    *,
    bold: bool = False,
    end: str = "\n",
    file: TextIO | None = None,
    **_kwargs,
) -> None:
    """带样式的 print 快捷方式。

    Args:
        text: 待输出文本（缺省空串 = 仅换行/换行符）。
        style: 语义样式名（如 "success"/"error"/"warning"/"info"/"status_running"）。
        bold: 额外加粗。
        end: 行尾（默认换行）。
        file: 输出目标（默认 sys.stdout）。
    """
    output = text
    if style:
        output = _apply(output, style)
    if bold and color_enabled():
        output = f"{_BOLD}{output}{_RESET}"
    print(output, end=end, file=file)


def bold(text: str) -> str:
    return _apply(text, "BOLD")


def dim(text: str) -> str:
    return _apply(text, "DIM")


def italic(text: str) -> str:
    return _apply(text, "ITALIC")


def underline(text: str) -> str:
    return _apply(text, "UNDERLINE")


def cyan(text: str) -> str:
    return _apply(text, "CYAN")


def magenta(text: str) -> str:
    return _apply(text, "MAGENTA")


def blue(text: str) -> str:
    return _apply(text, "BLUE")


def yellow(text: str) -> str:
    return _apply(text, "YELLOW")


def red(text: str) -> str:
    return _apply(text, "RED")


def green(text: str) -> str:
    return _apply(text, "GREEN")
