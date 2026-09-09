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


class ModelDownloadError(RuntimeError):
    """ModelScope 下载失败统一异常（404 / 网络 / 鉴权 / 代理）。

    由 :func:`download_repo` 包装，message 用中文提示，原始上游错误作为
    :attr:`cause_message` 保留以便日志链追溯。
    """

    def __init__(self, message: str, *, cause_message: str | None = None) -> None:
        super().__init__(message)
        self.cause_message = cause_message

    def __str__(self) -> str:  # pragma: no cover (mirror cause 行)
        if self.cause_message:
            return f"{self.args[0]}\n  cause: {self.cause_message[:200]}"
        return self.args[0]


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


def _friendly_download_error(original: Exception, modelscope_id: str) -> ModelDownloadError:
    """把上游原始异常包装成中文提示。

    分支（判别顺序）：
    - 上游 message 含 "404" / "record not found" / "NotExistError" → 仓库不存在。
      附验证入口：`modelscope list <ns/name>` 或浏览器 https://modelscope.cn/models/<ns/name>。
    - 上游 message 含 429 / rate limit → 限流，建议重试或设 HF_ENDPOINT 镜像。
    - 含 proxy / timeout / connect → 网络/代理。
    - 其他 → 通用提示。
    """
    raw = str(original)
    low = raw.lower()
    if "404" in raw or "record not found" in low or "notexisterror" in low:
        msg = (
            f"ModelScope 仓库不存在或已改名：`{modelscope_id}`（上游 404）。"
            f"请先用 `modelscope list {modelscope_id}` 或在浏览器访问 "
            f"https://modelscope.cn/models/{modelscope_id} 验证仓库 ID 是否存在，"
            f"再修改 profile YAML 中 `model` / `download.modelscope_id` 字段。"
        )
    elif "429" in raw or "rate limit" in low:
        msg = (
            f"ModelScope 限流（HTTP 429），请 1-2 分钟后重试；"
            f"或多源回退：在 .env 设 `HF_ENDPOINT=https://hf-mirror.com` 走镜像。"
        )
    elif "proxy" in low or "timeout" in low or "connect" in low:
        msg = (
            f"网络/代理访问 ModelScope 失败，请检查代理设置或 `HF_ENDPOINT` 镜像。"
        )
    else:
        msg = (
            f"ModelScope 下载 `{modelscope_id}` 失败，请查看 launch-<name>.log 中的上游原始堆栈"
            f"与 request_id 直接联系 ModelScope 支持。"
        )
    return ModelDownloadError(msg, cause_message=raw or None)


def download_repo(modelscope_id: str, local_root: Path) -> Path:
    """下载 ModelScope 仓库到 repo_local_dir()，返回本地目录。

    目录已存在且含权重文件时直接复用，不触发 modelscope 安装与下载。
    上游异常（404/网络/鉴权）统一包装为 :class:`ModelDownloadError`，
    保留原始上游错误为 :attr:`ModelDownloadError.cause_message`。
    """
    destination = repo_local_dir(modelscope_id, local_root)
    if _is_populated(destination):
        logger.info(f"本地已存在模型目录，跳过下载：{destination}")
        return destination
    destination.mkdir(parents=True, exist_ok=True)
    logger.info(f"从 ModelScope 下载 {modelscope_id} 到 {destination}")
    # 注意：snapshot_download 的 allow_file_pattern 由调用方决定（本入口拉取全部）。
    try:
        _snapshot_download(modelscope_id, str(destination))
    except Exception as exc:  # noqa: BLE001 — 上游异常类型多变，宽捕获包装
        # 半成品目录清理：已 mkdir destination 但下载中途失败，留下空/碎目录污染下次
        # `_is_populated` 判断（下次直接 skip 下载）。这里 best-effort rmdir，不抛。
        try:
            import shutil

            shutil.rmtree(destination, ignore_errors=True)
        except Exception:  # pragma: no cover (rm 失败不阻塞错误透传)
            pass
        wrapped = _friendly_download_error(exc, modelscope_id)
        logger.error(
            f"ModelScope 下载 {modelscope_id} 失败：{wrapped.args[0]}"
            f"{f' | cause: {wrapped.cause_message[:200]}' if wrapped.cause_message else ''}"
        )
        raise wrapped from exc
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
