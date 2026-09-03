#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_envs.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/2 10:00
# @Desc   : Web UI 环境管理 API 端点
# ===============================================================================

"""core/webui/admin_envs.py — Web UI 托管引擎环境（venv）管理 API 端点。

所有端点前缀 ``/admin/api/envs``（由调用方 include_router 时设置）。
托管引擎（vllm/sglang/...）与 gateway 子项目的 venv 建在同一 repo 下，依赖
``modelctl.core.envs`` 的 ``known_targets()/status()/setup()/remove()``。

setup 是一个 28min+ 的在线程中跑的长操作，走 TaskManager 异步任务 + SSE（见
admin_models._do_start 同款模式）；remove 是快速的 rmtree，同步返回。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from loguru import logger

from modelctl.core.webui.admin_auth import require_auth

router = APIRouter()

# 非托管引擎：原生二进制 / 官方安装器 / 源码编译，不建 venv，因此不出现在 targets 表格里。
# 仅用于在 UI 上说明「为什么这里看不到它们」并给出安装方式。
UNMANAGED_ENGINES = ("ollama", "unsloth", "llamacpp")
# llamacpp 无发布版可执行文件，需源码编译出 llama-server（产物路径由 pre_start 定位）
_LLAMACPP_CLONE = "git clone https://github.com/ggml-org/llama.cpp.git"
_LLAMACPP_BUILD = "cmake -B build -DGGML_CUDA=ON && cmake --build build -j 4"
UNMANAGED_INSTALL_HINTS = {
    "ollama": "curl -fsSL https://ollama.com/install.sh | sh",
    "unsloth": "curl -fsSL https://unsloth.ai/install.sh | sh",
    "llamacpp": f"{_LLAMACPP_CLONE}\n{_LLAMACPP_BUILD}",
}


def _router() -> APIRouter:
    """子路由工厂：返回 APIRouter（主路由 include_router 时由其调用）。"""
    return router


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


def _unmanaged_targets() -> list[dict]:
    """非托管引擎的安装情况（原生二进制 / 安装器 / 编译产物），供 UI 说明用。

    探测口径与 ``modelctl probe`` 一致：PATH 探测，llamacpp 额外看编译产物。
    """
    from modelctl.core.capabilities import binary_paths, find_llamacpp_binary

    paths = binary_paths(list(UNMANAGED_ENGINES))
    if not paths.get("llamacpp"):
        paths["llamacpp"] = find_llamacpp_binary()
    return [
        {
            "name": name,
            "installed": bool(paths.get(name)),
            "path": paths.get(name),
            "install_hint": UNMANAGED_INSTALL_HINTS.get(name, ""),
        }
        for name in UNMANAGED_ENGINES
    ]


@router.get("")
async def list_envs(_: None = Depends(require_auth)):
    """GET /admin/api/envs — 列出全部受管 target（6 托管引擎 + gateway）及其状态。

    同时返回 ``unmanaged``：ollama / unsloth / llamacpp 的安装情况。它们不走
    modelctl 托管 venv（原生二进制或官方安装器），故不在 targets 中，仅在 UI 说明。
    """
    from modelctl.core import envs

    status = await asyncio.to_thread(envs.status)
    targets = await asyncio.to_thread(envs.known_targets)

    out = []
    for t in targets:
        entry = status.get(t) or {}
        if not entry:
            # status() 缺 key 时的兜底（防御性：保证输出结构稳定）
            entry = {"exists": False}
        if entry.get("exists"):
            installed = True
            parts: list[str] = []
            py = entry.get("python")
            if py:
                parts.append(f"python {py}")
            packages = entry.get("packages")
            if isinstance(packages, dict) and packages:
                parts.append(f"{len(packages)} 个包")
            detail = "，".join(parts) if parts else "已安装"
        else:
            installed = False
            detail = "未安装"
        out.append({"name": t, "installed": installed, "detail": detail})

    unmanaged = await asyncio.to_thread(_unmanaged_targets)
    return {"targets": out, "unmanaged": unmanaged}


@router.post("/{target}/setup")
async def setup_env(
    target: str,
    request: Request,
    _: None = Depends(require_auth),
):
    """POST /admin/api/envs/{target}/setup — 异步建 venv，返回 202 + task_id。"""
    from modelctl.core.envs import known_targets
    from modelctl.core.envfile import load_env

    load_env()

    is_known = await asyncio.to_thread(lambda: target in known_targets())
    if not is_known:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"target {target} 不受管"}},
        )

    tm: object = request.app.state.task_manager
    lock = await tm.acquire(target, "setup")
    if lock is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "task_conflict", "message": f"target {target} 已有进行中的任务"}},
        )

    try:
        task = tm.create_task(kind="env_setup", action="setup", target=target)
        task.update_status("queued")
        asyncio.ensure_future(_do_env_setup(target, task))
        return JSONResponse(
            status_code=202,
            content={"task_id": task.id, "stream_url": f"/admin/api/tasks/{task.id}/stream"},
        )
    finally:
        await tm.release(target, "setup")


@router.post("/{target}/remove")
async def remove_env(
    target: str,
    _: None = Depends(require_auth),
):
    """POST /admin/api/envs/{target}/remove — 同步删除 venv，返回 ok/detail。"""
    from modelctl.core import envs

    is_known = await asyncio.to_thread(lambda: target in envs.known_targets())
    if not is_known:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"target {target} 不受管"}},
        )

    try:
        await asyncio.to_thread(envs.remove, target)
        return {"ok": True, "detail": f"已移除 {target} 环境"}
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "bad_target", "message": str(exc)}},
        )
    except Exception as exc:  # noqa: BLE001 — 删除失败统一 500
        logger.exception(f"环境移除异常 ({target}): {exc}")
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal", "message": f"移除失败: {exc}"}},
        )


# ---------------------------------------------------------------------------
# 工作线程
# ---------------------------------------------------------------------------


async def _do_env_setup(target: str, task) -> None:
    """在 worker 线程中执行环境安装，完成后更新 task。"""
    from modelctl.core.envs import setup as envs_setup

    task.update_status("running")
    try:
        await asyncio.to_thread(envs_setup, target)
        task.complete()
    except Exception as exc:
        logger.exception(f"环境安装异常 ({target}): {exc}")
        task.error(exit_code=1, message=str(exc))
