#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/logging.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 统一日志初始化
# ===============================================================================

"""loguru 统一日志初始化（支持彩色输出）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

from modelctl.core.colors import color_enabled, load_scheme_from_env
from modelctl.core.envfile import PROJECT_ROOT


def _build_console_format() -> str:
    """根据颜色开关构造 console loguru 格式（非 TTY 回退纯文本）。"""
    if color_enabled():
        return "<dim>{time:HH:mm:ss}</dim> | <level>{level:<7}</level> | {message}"
    return "{time:HH:mm:ss} | {level:<7} | {message}"


def setup_logging() -> None:
    """配置控制台与文件日志（LOG_DIR，默认项目根上级 logs/）。

    控制台彩色输出由 colors 模块统一控制（自动检测 NO_COLOR/TERM/CI/TTY），
    文件日志固定无颜色以避免日志污染。
    """
    # 从 MODELCTL_COLORS 环境变量加载自定义配色方案（如已设置）
    load_scheme_from_env()
    logger.remove()
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.add(sys.stderr, level=level, format=_build_console_format())
    log_dir = Path(os.environ.get("LOG_DIR") or PROJECT_ROOT.parent / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(log_dir / "modelctl.log", level=level, rotation="10 MB", retention="7 days", encoding="utf-8")
