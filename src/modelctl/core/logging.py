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

"""loguru 统一日志初始化。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

from modelctl.core.envfile import PROJECT_ROOT


def setup_logging() -> None:
    """配置控制台与文件日志（LOG_DIR，默认项目根上级 logs/）。"""
    logger.remove()
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.add(sys.stderr, level=level, format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}")
    log_dir = Path(os.environ.get("LOG_DIR") or PROJECT_ROOT.parent / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(log_dir / "modelctl.log", level=level, rotation="10 MB", retention="7 days", encoding="utf-8")
