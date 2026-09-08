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

import logging
import os
import sys

from loguru import logger

from modelctl.core.colors import color_enabled, load_scheme_from_env
from modelctl.core.paths import log_dir


def _build_console_format() -> str:
    """根据颜色开关构造 console loguru 格式（非 TTY 回退纯文本）。"""
    if color_enabled():
        return "<dim>{time:HH:mm:ss}</dim> | <level>{level:<7}</level> | {message}"
    return "{time:HH:mm:ss} | {level:<7} | {message}"


def _console_colorize() -> bool:
    """console handler 是否上色（显式决定，绝不交给 loguru 自动探测）。

    loguru 的自动探测（_colorama.should_colorize）对 sys.stderr 有 Windows 特判：
    只要 os.environ 里有 TERM（Git Bash / IDE 集成终端常见）就返回 True——哪怕
    stderr 已被 `start_detached` 重定向到 launch-*.log 文件，于是文件里全是 ANSI
    色码（"乱码"根因之一）。显式传 colorize 彻底规避。
    """
    return color_enabled()


def setup_logging(*, file_sink: bool = True) -> None:
    """配置控制台与文件日志（LOG_DIR，默认 <项目根>/data/logs，见 core/paths.py）。

    控制台彩色输出由 colors 模块统一控制（自动检测 NO_COLOR/TERM/CI/TTY），
    文件日志固定无颜色以避免日志污染。

    file_sink=False：后台子进程（gateway / webui / stats）专用——它们的日志文件
    就是 stdout 重定向的 launch-<name>.log，再写一份 modelctl.log 会引入多进程
    同文件轮转冲突；同时获得 LOG_LEVEL=INFO 的 DEBUG 过滤与统一格式。
    """
    # 从 MODELCTL_COLORS 环境变量加载自定义配色方案（如已设置）
    load_scheme_from_env()
    logger.remove()
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.add(sys.stderr, level=level, format=_build_console_format(), colorize=_console_colorize())
    if file_sink:
        logger.add(log_dir() / "modelctl.log", level=level, rotation="10 MB", retention="7 days", encoding="utf-8")


class AccessLogDebugFilter(logging.Filter):
    """把 uvicorn.access 记录按 DEBUG 语义处理：LOG_LEVEL=INFO（默认）丢弃，=DEBUG 放行。

    uvicorn 的 access 记录级别固定是 INFO（access_logger.info），无法从外部降级记录
    本身；用 filter 实现"等价 DEBUG"的可见性语义——前端 3-5s 轮询 /admin/api、
    健康检查等心跳式访问行默认不进日志，排查时 LOG_LEVEL=DEBUG 即恢复。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return os.environ.get("LOG_LEVEL", "INFO").upper() in ("TRACE", "DEBUG")


def uvicorn_log_config() -> dict:
    """webui / gateway 等 uvicorn 子进程的 log_config：access 降级为 DEBUG 语义。

    结构与 uvicorn.LOGGING_CONFIG 一致，仅给 access handler 挂
    AccessLogDebugFilter，避免心跳式轮询刷满 INFO 日志。filter 直接传类对象
    （dictConfig 对 callable 不做字符串导入解析），避免 gateway venv 里安装的
    modelctl 副本抢在 PYTHONPATH 的 src/ 之前被导入。
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"access_debug_only": {"()": AccessLogDebugFilter}},
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(message)s",
                "use_colors": None,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            },
        },
        "handlers": {
            "default": {"formatter": "default", "class": "logging.StreamHandler", "stream": "ext://sys.stderr"},
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "filters": ["access_debug_only"],
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "DEBUG", "propagate": False},
        },
    }
