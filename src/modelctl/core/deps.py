#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/deps.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/8/31 16:30
# @Desc   : 缺失依赖按需检测与安装
# ===============================================================================

"""core/deps.py — 缺失依赖按需检测与单包安装。

供 modelctl 各命令在运行前按需自动补齐缺失的 Python 包。
安装策略（按用户选择）：仅按需安装单包，不做 uv sync 全量同步。

- 检测：importlib.util.find_spec(module)，找不到则视为缺失
- 安装器优先级：uv pip install > python -m pip install
  （uv 创建的 venv 默认无 pip 模块，需走 uv）
- 镜像策略：阿里 PyPI 镜像加速 → 失败回退官方 PyPI 源
- 二次校验：装后再 find_spec 一次；仍不可解析视为失败（便于告警/回滚）
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys

from loguru import logger

# 阿里 PyPI 镜像：国内加速；与 uv.toml 的 [[index]] default 保持一致
ALIYUN_PYPI_MIRROR = "https://mirrors.aliyun.com/pypi/simple/"
ALIYUN_TRUSTED_HOST = "mirrors.aliyun.com"

# 各命令组对应的"模块 ↔ PyPI 安装要求"映射（键为顶层 import 名，值为 spec；
# 如 PyYAML 的顶层 import 是 yaml）。
# - "core"：CLI 入口自身必需（loguru/yaml），缺失意味着整个工具不可用
# - "gateway"：modelctl core.gateway 子进程所需（fastapi/uvicorn/httpx）
# - "stats"：modelctl core.stats 子进程所需（纯 stdlib，理论无缺失）
# - "modelscope"：模型下载工具（按需 lazy import）
PACKAGE_CHECKLISTS: dict[str, dict[str, str]] = {
    "core": {"loguru": "loguru>=0.7", "yaml": "PyYAML>=6.0"},
    "gateway": {"fastapi": "fastapi>=0.110", "uvicorn": "uvicorn>=0.29", "httpx": "httpx>=0.27"},
    "stats": {},
    "modelscope": {"modelscope": "modelscope>=1.0"},
}


def _find_uv() -> str | None:
    """查找 uv 可执行文件（无则 None）。"""
    return shutil.which("uv") or shutil.which("uv.exe")


def _pip_available() -> bool:
    """当前解释器内 pip 是否可用（uv 创建的 venv 默认无 pip 模块）。"""
    return subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        capture_output=True,
    ).returncode == 0


def _install_with_pip(req: str) -> bool:
    """用 python -m pip 安装指定 requirement，返回是否成功。"""
    return (
        subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "-i", ALIYUN_PYPI_MIRROR, "--trusted-host", ALIYUN_TRUSTED_HOST, req],
            capture_output=False,
            text=True,
        ).returncode == 0
    )


def _install_with_uv(req: str) -> bool:
    """用 uv pip install 安装指定 requirement，返回是否成功。"""
    uv = _find_uv()
    if uv is None:
        return False
    return (
        subprocess.run(
            [uv, "pip", "install", "--python", sys.executable,
             "-i", ALIYUN_PYPI_MIRROR, "--allow-insecure-host", ALIYUN_TRUSTED_HOST, req],
            capture_output=False,
            text=True,
        ).returncode == 0
    )


def _install_requirement(req: str) -> bool:
    """单包安装：先用 uv（如可用），失败回退 pip；任一成功后即返回 True。"""
    if _find_uv() is not None:
        if _install_with_uv(req):
            return True
        logger.warning("uv 安装失败（阿里镜像），尝试 pip 阿里镜像 ...")
    if _pip_available():
        if _install_with_pip(req):
            return True
    return False


def _module_exists(module: str) -> bool:
    """模块是否可导入（仅 find_spec；"已安装但无法解析"也返 False，便于告警）。

    注：对假模块（未填 __spec__ 的 mock）find_spec 会抛 ValueError，这里
    直接当成缺失——用户重装/重启会经过真实 find_spec，不会破坏普通使用场景。
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ValueError, AttributeError):
        return False


def missing_modules(checklist: str) -> list[tuple[str, str]]:
    """返回 checklist 中缺失的 (module_name, requirement) 列表。"""
    pkgs = PACKAGE_CHECKLISTS.get(checklist, {})
    return [(module, req) for module, req in pkgs.items() if not _module_exists(module)]


def ensure_packages(checklist: str) -> bool:
    """确保 checklist 中所有模块已安装；缺失的按需安装并二次校验。

    返回 True：所有目标模块可被顶层 import（含原本就存在的）；
    返回 False：有任一模块安装失败或安装后仍无法解析。
    """
    missing = missing_modules(checklist)
    if not missing:
        return True

    failed: list[str] = []
    for module, req in missing:
        logger.info(f"检测到缺失依赖 {req}，开始自动安装 ...")
        if not _install_requirement(req):
            logger.error(f"安装 {req} 失败（uv / pip 均失败）")
            failed.append(req)
            continue
        # 二次校验：装后重查顶层 import 名（如 PyYAML → yaml）
        if not _module_exists(module):
            logger.warning(f"{req} 安装完成但仍无法解析模块 {module}，请手动检查虚拟环境")
            failed.append(req)
    if failed:
        return False
    logger.info(f"缺失依赖补齐完成（{len(missing)} 个）")
    return True
