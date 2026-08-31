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

"""engines/_download.py — 统一的 ModelScope 下载工具。

安装策略已迁移到 core/deps.py（多源回退：uv → pip，镜像 → 官方源）。
本模块保留 snapshot_download 顶层 FAKE 引用 + ensure_modelscope() 入口，
以保证 llamacpp 引擎侧原有的 monkeypatch / 延迟重导入行为不变。
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from modelctl.core.deps import ensure_packages

# 顶层可 patch 的引用（方案 D）：测试直接 monkeypatch 本模块属性即可；
# 未安装 modelscope 时为 None，调用前由 ensure_modelscope() 安装并延迟重导入。
snapshot_download = None  # type: ignore[assignment, misc]


def ensure_modelscope() -> None:
    """确保 modelscope 已安装，否则按 core/deps.py 的多源回退策略安装。"""
    ensure_packages("modelscope")


def download_repo(modelscope_id: str, local_root: Path) -> Path:
    """下载 ModelScope 仓库到 local_root/<repo_last_part>，返回本地目录。"""
    ensure_packages("modelscope")
    import modelscope  # type: ignore[import-not-found]

    destination = local_root / modelscope_id.rsplit("/", 1)[-1]
    destination.mkdir(parents=True, exist_ok=True)
    logger.info(f"从 ModelScope 下载 {modelscope_id} 到 {destination}")
    # 注意：snapshot_download 的 allow_file_pattern 由调用方决定（本入口拉取全部）。
    modelscope.snapshot_download(
        model_id=modelscope_id,
        local_dir=str(destination),
    )
    return destination
