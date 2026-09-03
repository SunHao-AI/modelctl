#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_config.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/2 10:00
# @Desc   : Web UI 配置/nginx 片段/引擎编译 API 端点
# ===============================================================================

"""core/webui/admin_config.py — nginx map 片段 / .env 配置读取 / TensorRT-LLM 编译端点。

``build_llm_map`` 与 ``.env`` 解析是毫秒级的纯读操作，同步执行；
TensorRT-LLM 编译在线程中跑（trtllm-build 可能 30min+），走 TaskManager 异步任务
（参见 admin_models._do_start 同款模式）。
"""

from __future__ import annotations

import asyncio
import subprocess

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from loguru import logger

from modelctl.core.webui.admin_auth import require_auth

# .env 中需要脱敏的键（值只展示 *** + 末 4 位）
_SENSITIVE_KEYS = {"API_KEY", "UNSLOTH_API_KEY"}

router = APIRouter()


def _router() -> APIRouter:
    """子路由工厂：返回 APIRouter（主路由 include_router 时由其调用）。"""
    return router


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _mask_value(value: str) -> str:
    """敏感值脱敏：返回 ``***<末 4 位>``；空值原样返回。"""
    if not value or len(value) <= 4:
        return "***"
    return f"***{value[-4:]}"


def _load_env_file(path) -> dict[str, str]:
    """解析 .env 文件为 dict；缺文件返回空 dict（与 core.envfile.parse_env_file 公式一致）。"""
    from modelctl.core.envfile import parse_env_file

    return parse_env_file(path)


def _trtllm_engine_dir(name: str) -> str | None:
    """按 profile 读取 tensorrt_llm.engine_dir 配置；未找到返回 None。"""
    from modelctl.core.profile import list_profiles

    for p in list_profiles(None):
        if p.name == name:
            cfg = p.engine_config or {}
            return cfg.get("engine_dir") or None
    return None


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get("/nginx-snippet")
async def nginx_snippet(
    node: str = Query(..., description="节点数字前缀，如 210"),
    host: str = Query(..., description="节点 IP 或域名"),
    _: None = Depends(require_auth),
):
    """GET /admin/api/nginx-snippet — 生成 nginx map 路由片段（按 node/host 参数）。"""
    from modelctl.core.nginx_snippet import build_llm_map
    from modelctl.core.profile import list_profiles

    def _work() -> str:
        try:
            from modelctl.core.gateway import GATEWAY_PORT

            port: int = GATEWAY_PORT
        except Exception:  # noqa: BLE001
            # 与 core.gateway 的默认值保持一致（5003）
            port = int(__import__("os").environ.get("GATEWAY_PORT", "5003"))
        return build_llm_map(list_profiles(None), node, host, port)

    content = await asyncio.to_thread(_work)
    return {"content": content}


@router.get("/config/static")
async def read_env(_base: None = Depends(require_auth)):
    """GET /admin/api/config/static — 读取 .env 中的键值，敏感键脱敏返回。"""
    from modelctl.core.envfile import PROJECT_ROOT, load_env

    load_env()

    env_path = PROJECT_ROOT / ".env"
    data = await asyncio.to_thread(_load_env_file, env_path)

    keys = []
    for k, v in data.items():
        keys.append(
            {"key": k, "value": _mask_value(v) if k in _SENSITIVE_KEYS else v, "sensitive": k in _SENSITIVE_KEYS}
        )
    return {
        "path": str(env_path),
        "exists": env_path.is_file(),
        "entries": keys,
    }


@router.post("/trtllm/{name}/build")
async def build_trtllm(
    name: str,
    request: Request,
    _: None = Depends(require_auth),
):
    """POST /admin/api/trtllm/{name}/build — 异步编译 TensorRT-LLM 引擎，返回 202 + task_id。

    ``modelctl trtllm build <name>`` 同款流程；引擎目录由 profile ``engine_dir`` 字段
    决定，engine_dir 已含产物时快速跳过（28min+ 仅首次）。
    """
    from modelctl.core.envfile import load_env
    from modelctl.core.profile import list_profiles

    load_env()

    profile = None
    for p in await asyncio.to_thread(lambda: list_profiles(None)):
        if p.name == name:
            profile = p
            break
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )
    if profile.engine != "tensorrt_llm":
        return JSONResponse(
            status_code=412,
            content={"error": {"code": "unsupported_engine", "message": f"引擎 {profile.engine} 不支持本端点（仅 tensorrt_llm）"}},
        )

    tm: object = request.app.state.task_manager
    lock = await tm.acquire(name, "build")
    if lock is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "task_conflict", "message": f"{name} 已有进行中的任务"}},
        )

    try:
        task = tm.create_task(kind="trtllm_build", action="build", target=name)
        task.update_status("queued")
        asyncio.ensure_future(_do_trtllm_build(name, task))
        return JSONResponse(
            status_code=202,
            content={"task_id": task.id, "stream_url": f"/admin/api/tasks/{task.id}/stream"},
        )
    finally:
        await tm.release(name, "build")


@router.get("/trtllm/{name}/status")
async def trtllm_status(name: str, _: None = Depends(require_auth)):
    """GET /admin/api/trtllm/{name}/status — 查询 TensorRT-LLM 引擎编译产物。"""
    engine_dir = await asyncio.to_thread(_trtllm_engine_dir, name)
    if not engine_dir:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 未配置 tensorrt_llm.engine_dir"}},
        )

    def _stat() -> tuple[str, int, int]:
        from pathlib import Path

        d = Path(engine_dir).expanduser()
        if not d.is_dir():
            return str(d), False, 0
        files = [p for p in d.iterdir() if p.is_file()]
        return str(d), len(files) > 0, len(files)

    path, built, count = await asyncio.to_thread(_stat)
    return {"built": built, "engine_dir": path, "files": count}


# ---------------------------------------------------------------------------
# 工作线程
# ---------------------------------------------------------------------------


async def _do_trtllm_build(name: str, task) -> None:
    """在 worker 线程中执行 trtllm-build，完成后更新 task。"""
    task.update_status("running")
    try:
        await asyncio.to_thread(_run_trtllm_build_sync, name)
        task.complete()
    except Exception as exc:
        logger.exception(f"TensorRT-LLM 编译异常 ({name}): {exc}")
        task.error(exit_code=1, message=str(exc))


def _run_trtllm_build_sync(name: str) -> None:
    """同步执行 trtllm-build（在线程中调用）。"""
    from pathlib import Path

    from modelctl.core.capabilities import probe
    from modelctl.core.profile import list_profiles
    from modelctl.engines.tensorrt_llm import TensorRtLlmAdapter

    profile = None
    for p in list_profiles(None):
        if p.name == name:
            profile = p
            break
    if profile is None:
        raise ValueError(f"模型 {name} 不存在")

    caps = probe()
    adapter = TensorRtLlmAdapter(profile, caps)
    adapter.ensure_bin()
    cmd, env = adapter.build_compile_command()

    engine_dir_str = str((profile.engine_config or {}).get("engine_dir") or "").strip()
    engine_dir = Path(engine_dir_str).expanduser() if engine_dir_str else None
    if engine_dir is None:
        raise ValueError(f"{name}：profile 缺少 tensorrt_llm.engine_dir")

    # 已有产物：跳过编译，直接成功返回（幂等，规避重复 30min）
    if engine_dir.exists() and any(engine_dir.iterdir()):
        return

    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise RuntimeError(f"trtllm-build 退出码 {proc.returncode}：{msg}")
