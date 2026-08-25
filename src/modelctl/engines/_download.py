#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/engines/_download.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : ModelScope 模型下载工具
# ===============================================================================

"""engines/_download.py — 统一的 ModelScope 下载工具。"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from loguru import logger

# 阿里 PyPI 镜像：国内加速 pip/uv 安装；模型下载仍走 ModelScope（国内服务，无需镜像切换）。
ALIYUN_PYPI_MIRROR = "https://mirrors.aliyun.com/pypi/simple/"

# 模块级可 patch 的引用（方案 D）：测试直接 monkeypatch 本模块属性即可；
# 未安装 modelscope 时保持 None，调用前由 ensure_modelscope() 安装并延迟重导入。
try:  # pragma: no cover - 真实环境由 ensure_modelscope 安装
    from modelscope import snapshot_download  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    snapshot_download = None  # type: ignore[assignment]


def _install_pip_packages(args: list[str]) -> bool:
    """执行 pip 安装命令，返回是否成功（失败不抛异常，供镜像回退逻辑判定）。"""
    result = subprocess.run([sys.executable, "-m", "pip", *args], capture_output=False, text=True)
    return result.returncode == 0


def _install_uv_pip_packages(args: list[str]) -> bool:
    """在 uv 虚拟环境（无 pip 模块）下用 uv 安装，返回是否成功。"""
    result = subprocess.run(["uv", "pip", "install", "--python", sys.executable, *args], capture_output=False, text=True)
    return result.returncode == 0


def ensure_modelscope() -> None:
    """确保 modelscope 已安装，否则自动安装。

    镜像策略：
    1. 优先走阿里 PyPI 镜像（加速国内网络）
    2. 镜像失败（404 / 解析错误等）时回退官方 PyPI 源兜底

    安装器优先级：python -m pip > uv pip install（uv 创建的虚拟环境默认无 pip）。
    """
    if importlib.util.find_spec("modelscope") is not None:
        return
    logger.info("未安装 modelscope，正在安装（优先阿里镜像，失败回退官方源）...")
    pip_ok = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True).returncode == 0

    if pip_ok:
        ok = _install_pip_packages(
            ["install", "-U", "-i", ALIYUN_PYPI_MIRROR, "--trusted-host", "mirrors.aliyun.com", "modelscope"]
        )
        if ok:
            return
        logger.warning("阿里镜像安装 modelscope 失败，回退官方 PyPI 源...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "modelscope"],
            check=True,
        )
        return

    logger.info("当前解释器无 pip（uv 虚拟环境），改用 uv pip install...")
    ok = _install_uv_pip_packages(
        ["--index-url", ALIYUN_PYPI_MIRROR, "--allow-insecure-host", "mirrors.aliyun.com", "-U", "modelscope"]
    )
    if ok:
        return
    logger.warning("阿里镜像安装 modelscope 失败，回退官方 PyPI 源...")
    subprocess.run(
        ["uv", "pip", "install", "--python", sys.executable, "-U", "modelscope"],
        check=True,
    )


def download_repo(modelscope_id: str, local_root: Path) -> Path:
    """下载 ModelScope 仓库到 local_root/<repo_last_part>，返回本地目录。"""
    ensure_modelscope()
    from modelscope import snapshot_download  # type: ignore[import-not-found]

    destination = local_root / modelscope_id.rsplit("/", 1)[-1]
    destination.mkdir(parents=True, exist_ok=True)
    logger.info(f"从 ModelScope 下载 {modelscope_id} 到 {destination}")
    snapshot_download(
        model_id=modelscope_id,
        local_dir=str(destination),
    )
    return destination
