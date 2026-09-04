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


def _snapshot_download(modelscope_id: str, local_dir: str, **_kwargs: object) -> None:
    """延迟获取 modelscope.snapshot_download；未安装时自动安装。

    用函数（而非模块级 `from ... import snapshot_download`）以避免：
    1）测试里 monkeypatch "modelctl.engines._download.snapshot_download" 失效
       （重新 import 会绕开 patch）；
    2）模块加载时无条件执行 import（首次调用前不需要真实安装）。
    """
    global snapshot_download
    if snapshot_download is None:
        ensure_packages("modelscope")
        import modelscope  # type: ignore[import-not-found]
        snapshot_download = modelscope.snapshot_download
    assert snapshot_download is not None  # pragma: no cover (assert for mypy)
    snapshot_download(model_id=modelscope_id, local_dir=local_dir, **_kwargs)  # type: ignore[operator]


def ensure_modelscope() -> None:
    """确保 modelscope 已安装，否则按 core/deps.py 的多源回退策略安装。"""
    ensure_packages("modelscope")


def repo_local_dir(modelscope_id: str, local_root: Path) -> Path:
    """仓库对应的本地落地目录：local_root/<repo_last_part>（路径确定性推导）。

    落地路径完全由 modelscope_id + MODEL_ROOT 决定，因此无需把路径写回 profile YAML：
    同一台机器下次启动会推导出同一路径，目录存在即复用。
    """
    return local_root / modelscope_id.rsplit("/", 1)[-1]


def download_repo(modelscope_id: str, local_root: Path) -> Path:
    """下载 ModelScope 仓库到 repo_local_dir()，返回本地目录。

    目录已存在且含权重文件时直接复用，不触发 modelscope 安装与下载。
    """
    destination = repo_local_dir(modelscope_id, local_root)
    if _is_populated(destination):
        logger.info(f"本地已存在模型目录，跳过下载：{destination}")
        return destination
    destination.mkdir(parents=True, exist_ok=True)
    logger.info(f"从 ModelScope 下载 {modelscope_id} 到 {destination}")
    # 注意：snapshot_download 的 allow_file_pattern 由调用方决定（本入口拉取全部）。
    _snapshot_download(modelscope_id, str(destination))
    return destination


def _is_populated(path: Path) -> bool:
    """目录存在且含常见权重/配置文件时视为已就位（避免半成品目录被当成已下载）。"""
    if not path.is_dir():
        return False
    names = {p.name for p in path.iterdir()}
    markers = {"config.json", "model.safetensors", "model.safetensors.index.json"}
    if markers & names:
        return True
    return any(p.suffix == ".safetensors" for p in path.iterdir())
