#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_models.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/2 10:00
# @Desc   : Web UI 模型管理 API 端点
# ===============================================================================

"""core/webui/admin_models.py — Web UI 模型相关 API 端点。

所有端点前缀 ``/admin/api/models``（由调用方 include_router 时设置）。
依赖 FastAPI + ``admin_auth.require_auth`` + ``admin_tasks.TaskManager``（均在
``modelctl.core.webui`` 子包内），故 fastapi 在顶部导入（Web UI 复用 gateway 独立
venv，其中必然装有 fastapi），modelctl.core 子模块在函数体内延迟导入以避免
循环依赖与不必要的模块加载。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from modelctl.core.webui.admin_auth import require_auth, require_auth_or_query

router = APIRouter()


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _find_profile(name: str):
    """按 name 精确匹配 list_profiles 中的 profile；未找到返回 None。"""
    from modelctl.core.profile import list_profiles

    for p in list_profiles(None):
        if p.name == name:
            return p
    return None


def _read_pid_raw(name: str) -> int | None:
    """读取 PID 文件中的 pid（不过滤存活状态），供前端展示诊断。"""
    from modelctl.core.process import pid_file

    pf = pid_file(name)
    if not pf.is_file():
        return None
    try:
        return int(pf.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _check_health(port: int, api_key: str | None) -> bool | None:
    """单次健康探测（短超时）；返回 True/False；探测异常返回 None。"""
    from modelctl.core.process import open_local

    url = f"http://127.0.0.1:{port}/health"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        req = urllib.request.Request(url, headers=headers)
        with open_local(req, timeout=2) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _mask_key(key: str | None) -> str | None:
    """API key 脱敏：仅暴露末 4 位（***3876）；None 原样返回。"""
    if not key:
        return None
    if len(key) <= 4:
        return f"***{key}"
    return f"***{key[-4:]}"


def _model_summary(p) -> dict:
    """构建 ModelSummary dict（列表端点用）。"""
    running = False
    try:
        from modelctl.core.process import is_running

        running = is_running(p.name)
    except Exception:
        running = False

    health: str | None = None
    if running:
        ok = _check_health(p.port, p.api_key)
        if ok is not None:
            health = "healthy" if ok else "unhealthy"
        else:
            health = "unknown"

    pid = _read_pid_raw(p.name)
    log_path: str | None = None
    try:
        from modelctl.core.process import launch_log

        lp = launch_log(p.name)
        if lp is not None:
            log_path = str(lp)
    except Exception:
        pass

    return {
        "name": p.name,
        "group": p.group,
        "engine": p.engine,
        "variant": p.variant,
        "port": p.port,
        "aliases": list(p.aliases),
        "state": "running" if running else "stopped",
        "health": health,
        "rates": None,  # TODO: from stats later
        "api_key_masked": _mask_key(p.api_key),
        "pid": pid,
        "log_path": log_path,
    }


def _fail_task(task, exit_code: int, message: str, engine: str) -> None:
    """失败收尾统一入口：附启动失败分类码，任务抽屉据此渲染修复动作。

    分类只认 venv_missing/docker_missing（reconcile.classify_start_failure），
    其余失败 code/engine 均为 None——宁缺毋滥。
    """
    from modelctl.core.cluster.reconcile import classify_start_failure

    code, eng = classify_start_failure(message, engine)
    task.error(exit_code=exit_code, message=message, code=code, engine=eng)


def _stage_event_bridge(task, loop: asyncio.AbstractEventLoop):
    """构建 stage 事件桥接回调（start/restart 共用），把进度事件安全投递到事件循环线程。

    `on_progress` 由 worker 线程（`asyncio.to_thread`）与 LoadingWatcher daemon 线程调用，
    而 `Task.event` 依赖当前线程的事件循环——工作线程里没有，直接调用会抛 RuntimeError
    并被其内部 except 静默吞掉，浏览器永远收不到 `stage` 帧。故此处以调用方在**事件循环
    线程**捕获的 `loop` 经 `call_soon_threadsafe` 派发；loop 已关闭时降级 debug 日志
    （进度尽力而为，绝不影响启动）。
    """
    def _on_stage(ev) -> None:
        """阶段事件 → task SSE（与 docker 一键安装同一事件基建，_sse_task_stream 零改动透传）。"""
        payload = {
            "stage": ev.stage,
            "status": ev.status,
            "label": ev.label,
            "pct": ev.pct,
            "etaSeconds": ev.eta_s,
            "error": ev.error,
            "task_id": task.id,
        }
        pct = "" if ev.pct is None else f" {round(ev.pct * 100)}%"
        detail = f"{ev.label}{pct}"

        def _dispatch() -> None:
            task.event("stage", payload)
            task.update_detail(detail)

        try:
            loop.call_soon_threadsafe(_dispatch)
        except (RuntimeError, TypeError) as exc:
            logger.debug(f"stage 事件投递失败（循环已关闭，忽略）：{exc}")

    return _on_stage


async def _do_start(profile, caps, timeout: float, task, gpus: str | None) -> None:
    """在 worker 线程中执行启动，完成后更新 task。"""
    from modelctl.core.all_service import start_profile
    from modelctl.core.gpu_utils import resolve_gpu_list

    # 进入 to_thread 前仍在事件循环线程：此处取到的 loop 才能供跨线程派发
    loop = asyncio.get_running_loop()
    _on_stage = _stage_event_bridge(task, loop)

    # gpus 逗号字符串 → 环境变量（让 adapter.selected_gpus() 可见）
    prev_gpus = os.environ.get("MODELCTL_GPUS")
    if gpus:
        parsed = resolve_gpu_list(None, None, gpus)
        if parsed:
            os.environ["MODELCTL_GPUS"] = ",".join(str(g) for g in parsed)

    try:
        task.update_status("running")
        result = await asyncio.to_thread(
            lambda: start_profile(profile, caps, timeout, on_progress=_on_stage)
        )
        task.update_detail(result.detail)
        if result.status == "error":
            _fail_task(task, 1, result.detail, profile.engine)
        else:
            task.complete()
    except Exception as exc:
        logger.exception(f"启动任务异常 ({profile.name}): {exc}")
        try:
            from modelctl.engines.base import RequirementError

            if isinstance(exc, RequirementError):
                _fail_task(task, 2, str(exc), profile.engine)
            else:
                _fail_task(task, 1, str(exc), profile.engine)
        except ImportError:
            _fail_task(task, 1, str(exc), profile.engine)
    finally:
        # 恢复环境变量
        if gpus:
            if prev_gpus is None:
                os.environ.pop("MODELCTL_GPUS", None)
            else:
                os.environ["MODELCTL_GPUS"] = prev_gpus


async def _do_restart(profile, caps, timeout: float, task, gpus: str | None) -> None:
    """在 worker 线程中执行重启，完成后更新 task。"""
    from modelctl.core.all_service import restart_profile
    from modelctl.core.gpu_utils import resolve_gpu_list

    # 与 _do_start 同构：事件循环线程捕获 loop，restart 复用 start 的阶段序列
    loop = asyncio.get_running_loop()
    _on_stage = _stage_event_bridge(task, loop)

    prev_gpus = os.environ.get("MODELCTL_GPUS")
    if gpus:
        parsed = resolve_gpu_list(None, None, gpus)
        if parsed:
            os.environ["MODELCTL_GPUS"] = ",".join(str(g) for g in parsed)

    try:
        task.update_status("running")
        result = await asyncio.to_thread(
            lambda: restart_profile(profile, caps, timeout, on_progress=_on_stage)
        )
        task.update_detail(result.detail)
        if result.status == "error":
            _fail_task(task, 1, result.detail, profile.engine)
        else:
            task.complete()
    except Exception as exc:
        logger.exception(f"重启任务异常 ({profile.name}): {exc}")
        try:
            from modelctl.engines.base import RequirementError

            if isinstance(exc, RequirementError):
                _fail_task(task, 2, str(exc), profile.engine)
            else:
                _fail_task(task, 1, str(exc), profile.engine)
        except ImportError:
            _fail_task(task, 1, str(exc), profile.engine)
    finally:
        if gpus:
            if prev_gpus is None:
                os.environ.pop("MODELCTL_GPUS", None)
            else:
                os.environ["MODELCTL_GPUS"] = prev_gpus


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get("")
async def list_models(_: None = Depends(require_auth)):
    """GET /admin/api/models — 按家族分组列出所有模型。"""
    from modelctl.core.gateway import ENGINE_PRIORITY
    from modelctl.core.profile import list_profiles

    profiles = list_profiles(None)

    # 按 group 分组，保持首次出现顺序；未声明 group 的归入 "(其它)"
    groups: dict[str, list] = {}
    for p in profiles:
        g = p.group or "(其它)"
        groups.setdefault(g, []).append(p)

    # 组内按引擎优先级排序
    for members in groups.values():
        members.sort(key=lambda m: ENGINE_PRIORITY.get(m.engine, 99))

    # 构建响应
    out_groups = []
    for g, members in sorted(groups.items(), key=lambda x: (x[0] == "(其它)", x[0])):
        out_groups.append({
            "group": g,
            "models": [_model_summary(m) for m in members],
        })

    return {
        "groups": out_groups,
        "default_model": os.environ.get("GATEWAY_DEFAULT_MODEL", ""),
    }


@router.get("/{name}")
async def get_model(name: str, _: None = Depends(require_auth)):
    """GET /admin/api/models/{name} — 单模型详情。"""
    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )

    running = False
    try:
        from modelctl.core.process import is_running

        running = is_running(profile.name)
    except Exception:
        running = False

    health: str | None = None
    if running:
        ok = _check_health(profile.port, profile.api_key)
        if ok is not None:
            health = "healthy" if ok else "unhealthy"
        else:
            health = "unknown"

    engine_config = profile.engine_config
    # 脱敏 engine_config 中可能存在的 api_key
    ec_out = dict(engine_config)
    if "api_key" in ec_out and isinstance(ec_out["api_key"], str):
        ec_out["api_key"] = _mask_key(ec_out["api_key"])

    # 读取启动日志路径
    launch_log_path: str | None = None
    try:
        from modelctl.core.process import launch_log

        lp = launch_log(profile.name)
        if lp is not None:
            launch_log_path = str(lp)
    except Exception:
        pass

    return {
        "name": profile.name,
        "group": profile.group,
        "engine": profile.engine,
        "variant": profile.variant,
        "port": profile.port,
        "aliases": list(profile.aliases),
        "state": "running" if running else "stopped",
        "health": health,
        "rates": None,
        "api_key_masked": _mask_key(profile.api_key),
        "pid": _read_pid_raw(profile.name),
        "log_path": launch_log_path,
        "engine_config": ec_out,
        "model_path": ec_out.get("model"),
        "tool_call_rounds": profile.tool_call_rounds,
        "max_output_tokens": profile.max_output_tokens,
        "usage": profile.usage,
        "thinking_disabled": profile.thinking_disabled,
    }


@router.post("/{name}/start")
async def start_model(
    name: str,
    request: Request,
    timeout: float | None = Query(default=None, ge=1, le=7200),
    gpus: str | None = Query(default=None),
    _: None = Depends(require_auth),
):
    """POST /admin/api/models/{name}/start — 异步启动模型，返回 202 + task_id。"""
    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )

    # 确保 .env 已加载（API_KEY 等环境变量可用）
    from modelctl.core.envfile import load_env

    load_env()

    tm: object = request.app.state.task_manager
    lock = await tm.acquire(name, "start")
    if lock is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "task_conflict", "message": f"模型 {name} 已有进行中的任务"}},
        )

    try:
        caps = await asyncio.to_thread(_probe_caps)
        from modelctl.core.all_service import default_start_timeout

        # timeout 缺省 → 按 profile/运行时自适应（docker 1800s，其余 600s）
        eff_timeout = timeout if timeout is not None else await asyncio.to_thread(
            default_start_timeout, profile, caps
        )
        task = tm.create_task(kind="model_start", action="start", target=name)
        task.update_status("queued")
        asyncio.ensure_future(_do_start(profile, caps, eff_timeout, task, gpus))
        return JSONResponse(
            status_code=202,
            content={"task_id": task.id, "stream_url": f"/admin/api/tasks/{task.id}/stream"},
        )
    finally:
        await tm.release(name, "start")


def _probe_caps():
    """探测硬件能力（在线程中调用，避免阻塞事件循环）。"""
    from modelctl.core.capabilities import probe

    return probe()


@router.post("/{name}/stop")
async def stop_model(name: str, _: None = Depends(require_auth)):
    """POST /admin/api/models/{name}/stop — 同步停止模型。"""
    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )

    from modelctl.core.all_service import stop_profile
    from modelctl.core.capabilities import probe

    caps = await asyncio.to_thread(probe)
    result = await asyncio.to_thread(stop_profile, profile, caps, None)
    return {"ok": result.status != "error", "detail": result.detail}


@router.post("/{name}/restart")
async def restart_model(
    name: str,
    request: Request,
    timeout: float | None = Query(default=None, ge=1, le=7200),
    gpus: str | None = Query(default=None),
    _: None = Depends(require_auth),
):
    """POST /admin/api/models/{name}/restart — 异步重启模型，返回 202 + task_id。"""
    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )

    from modelctl.core.envfile import load_env

    load_env()

    tm: object = request.app.state.task_manager
    lock = await tm.acquire(name, "restart")
    if lock is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "task_conflict", "message": f"模型 {name} 已有进行中的任务"}},
        )

    try:
        caps = await asyncio.to_thread(_probe_caps)
        from modelctl.core.all_service import default_start_timeout

        # timeout 缺省 → 按 profile/运行时自适应（docker 1800s，其余 600s）
        eff_timeout = timeout if timeout is not None else await asyncio.to_thread(
            default_start_timeout, profile, caps
        )
        task = tm.create_task(kind="model_restart", action="restart", target=name)
        task.update_status("queued")
        asyncio.ensure_future(_do_restart(profile, caps, eff_timeout, task, gpus))
        return JSONResponse(
            status_code=202,
            content={"task_id": task.id, "stream_url": f"/admin/api/tasks/{task.id}/stream"},
        )
    finally:
        await tm.release(name, "restart")


@router.get("/{name}/startup")
async def get_startup_progress(name: str, _: None = Depends(require_auth)):
    """GET /admin/api/models/{name}/startup — 最近一次启动的阶段进度快照。

    数据源为 all_service 每次阶段事件覆写的 `cache/<name>.startup.json`（尽力而为，
    写失败则无快照 → 404）。非发起者浏览器 / 页面刷新后据此渲染进度卡片，不依赖 SSE 时序。
    profile 存在性优先于文件：快照存在但 profile 已删 → 404（设计 §5）；name 先做
    路径安全校验，拒绝 `..`/分隔符等越目录拼接。
    """
    from modelctl.core.paths import cache_dir
    from modelctl.core.startup_progress import STAGES

    # name 参与文件路径拼接：白名单字符（禁分隔符）+ 拒绝 `..`，防目录穿越（如 ..%2F..%2Fetc）
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or ".." in name:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )
    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )

    path = cache_dir() / f"{name}.startup.json"
    if not path.is_file():
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 暂无启动进度记录"}},
        )
    try:
        raw = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # ValueError 覆盖 JSONDecodeError 与 UnicodeDecodeError（快照半写/编码异常）
        logger.warning(f"启动进度快照不可读（{path}）：{exc}")
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 启动进度快照损坏"}},
        )
    if not isinstance(raw, dict):
        logger.warning(f"启动进度快照结构异常（{path}）：非对象 JSON")
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 启动进度快照损坏"}},
        )
    stages = []
    for s in raw.get("stages") or []:
        if not isinstance(s, dict):
            continue
        stages.append({
            "stage": s.get("stage"),
            "status": s.get("status", "pending"),
            "label": s.get("label") or "",
            "pct": s.get("pct"),
            "etaSeconds": s.get("eta_s"),
            "error": s.get("error"),
            "startedAt": s.get("started_at"),
            "finishedAt": s.get("finished_at"),
        })
    return {
        "profile": raw.get("profile") or name,
        "engine": raw.get("engine") or "",
        "runtime": raw.get("runtime") or "",
        "updatedAt": raw.get("updated_at") or "",
        "stages": stages,
        "knownStages": list(STAGES),
    }


@router.get("/{name}/log")
async def get_model_log(name: str, _: None = Depends(require_auth), lines: int = Query(default=200, ge=1)):
    """GET /admin/api/models/{name}/log — 读取启动日志尾部。"""
    from modelctl.core.process import launch_log, tail_file

    log_path = launch_log(name)
    if log_path is None:
        return {"lines": []}
    tail = await asyncio.to_thread(tail_file, log_path, lines)
    return {"lines": tail.strip().split("\n") if tail else []}


@router.get("/{name}/log/stream")
async def stream_model_log(
    name: str,
    key: str = Query(default=""),
    _: None = Depends(require_auth_or_query),
):
    """GET /admin/api/models/{name}/log/stream — SSE 实时日志尾随。

    EventSource 无法携带 Authorization header，前端按 ``?key=`` 传 token，
    鉴权依赖 ``require_auth_or_query`` 与 Bearer 同等强度。
    """
    from modelctl.core.process import launch_log, tail_file

    return StreamingResponse(
        _sse_log_stream(name),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _sse_log_stream(name: str):
    """SSE 日志 async 流：先推送已有行，之后每 2s 轮询新行，每 10s 心跳。"""
    from modelctl.core.process import launch_log, tail_file

    log_path = launch_log(name)
    if log_path is None:
        yield f"event: log\ndata: {json.dumps({'line': '（日志文件不存在）'}, ensure_ascii=False)}\n\n"
        return

    # 初始行
    initial = await asyncio.to_thread(tail_file, log_path, 200)
    if initial:
        for line in initial.strip().split("\n"):
            yield f"event: log\ndata: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
        pos = (await asyncio.to_thread(log_path.stat)).st_size
    else:
        pos = 0

    last_activity = time.monotonic()
    while True:
        await asyncio.sleep(2)

        # 文件被删除/轮转？
        if not log_path.is_file():
            yield f"event: log\ndata: {json.dumps({'line': '（日志文件已删除，等待重建）'}, ensure_ascii=False)}\n\n"
            pos = 0
            last_activity = time.monotonic()
            continue

        # 读取新增内容
        try:
            current_size = (await asyncio.to_thread(log_path.stat)).st_size
            if current_size > pos:
                raw = await asyncio.to_thread(_read_file_range, log_path, pos, current_size)
                new_lines = raw.split("\n")
                # 最后一个元素可能不完整（无末尾换行）
                if new_lines and new_lines[-1] == "":
                    new_lines.pop()
                for line in new_lines:
                    yield f"event: log\ndata: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
                pos = current_size
                last_activity = time.monotonic()
        except OSError:
            pass

        # 心跳
        if time.monotonic() - last_activity >= 10:
            yield "event: heartbeat\ndata: {}\n\n"
            last_activity = time.monotonic()


def _read_file_range(path: Path, start: int, end: int) -> str:
    """读取文件的 [start, end) 字节区间（在线程中调用）。"""
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(end - start)
    return data.decode("utf-8", errors="replace")


@router.get("/{name}/yaml")
async def get_model_yaml(name: str, _: None = Depends(require_auth)):
    """GET /admin/api/models/{name}/yaml — 读取原始 YAML 文本。"""
    from modelctl.core.envfile import PROJECT_ROOT

    models_dir = PROJECT_ROOT / "models"
    # 优先根目录，其次递归
    candidates = [
        models_dir / f"{name}.yaml",
        *[p for p in sorted(models_dir.rglob(f"{name}.yaml"))],
    ]
    for path in candidates:
        if path.is_file():
            content = await asyncio.to_thread(path.read_text, encoding="utf-8")
            return {"content": content, "path": str(path)}

    return JSONResponse(
        status_code=404,
        content={"error": {"code": "not_found", "message": f"模型 {name} 的 YAML 文件未找到"}},
    )


@router.post("/{name}/ui/start")
async def start_model_ui(
    name: str,
    request: Request,
    _: None = Depends(require_auth),
):
    """POST /admin/api/models/{name}/ui/start — 启动 Unsloth Web 管理控制台。

    请求体（可选）: ``{port?: int, host?: str, allow_from?: list[str] | str}``
    仅 engine=unsloth 可用，其余返回 412。
    """
    from modelctl.core.capabilities import probe
    from modelctl.core.envfile import load_env
    from modelctl.core.process import (
        is_running,
        launch_log,
        start_detached,
    )
    from modelctl.core.ufw import ensure_ufw_allow
    from modelctl.engines import get_adapter

    load_env()

    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )
    if profile.engine != "unsloth":
        return JSONResponse(
            status_code=412,
            content={"error": {"code": "unsupported_engine", "message": f"引擎 {profile.engine} 不支持 Web 管理控制台（仅 unsloth 支持）"}},
        )

    # 可选 body（无 body 时静默跳过）
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass

    port = body.get("port")
    host = body.get("host")
    allow_from = body.get("allow_from")

    caps = await asyncio.to_thread(probe)
    adapter = get_adapter(profile.engine)(profile, caps)
    spec = adapter.ui_spec(port=port, host=host)

    instance = f"ui-{profile.name}"
    if is_running(instance):
        return {"ok": True, "detail": f"Web 控制台已在运行（http://{spec['host']}:{spec['port']}）", "already_running": True}

    # ufw 白名单
    allow_list = allow_from or spec["allow_from"]
    if isinstance(allow_list, str):
        allow_list = [allow_list]
    for src in allow_list:
        try:
            await asyncio.to_thread(ensure_ufw_allow, src, spec["port"])
        except Exception as exc:
            logger.warning(f"添加 ufw 规则失败（{src} → :{spec['port']}）: {exc}")

    pid, _ = await asyncio.to_thread(start_detached, instance, spec["cmd"], spec["env"])
    log = launch_log(instance)
    return {
        "ok": True,
        "detail": f"Web 控制台已启动（PID {pid}），监听 http://{spec['host']}:{spec['port']}",
        "pid": pid,
        "url": f"http://{spec['host']}:{spec['port']}",
        "allow_from": allow_list,
        "log_path": str(log) if log is not None else None,
    }


@router.post("/{name}/ui/stop")
async def stop_model_ui(name: str, _: None = Depends(require_auth)):
    """POST /admin/api/models/{name}/ui/stop — 停止 Unsloth Web 管理控制台。

    仅 engine=unsloth 可用，其余返回 412。
    """
    from modelctl.core.capabilities import probe
    from modelctl.core.process import is_running, pid_file, stop_instance
    from modelctl.engines import get_adapter

    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )
    if profile.engine != "unsloth":
        return JSONResponse(
            status_code=412,
            content={"error": {"code": "unsupported_engine", "message": f"引擎 {profile.engine} 不支持 Web 管理控制台（仅 unsloth 支持）"}},
        )

    instance = f"ui-{profile.name}"
    caps = await asyncio.to_thread(probe)
    adapter = get_adapter(profile.engine)(profile, caps)

    if not is_running(instance) and not pid_file(instance).is_file():
        return {"ok": True, "detail": "Web 控制台未在运行"}

    ui_port = (adapter.ui_spec() or {}).get("port", 0)
    await asyncio.to_thread(stop_instance, instance, ui_port, [])
    return {"ok": True, "detail": "已停止 Web 控制台"}


# ---------------------------------------------------------------------------
# 子路由工厂（供 admin_router 聚合挂载；prefix="/models" 由聚合方设置）
# ---------------------------------------------------------------------------


def _router() -> APIRouter:
    """返回模型管理子路由（已注册全部端点），供 admin_router 聚合调用。

    使用模块级 router 单例：同一进程内重复调用 create_admin_router() 时，
    既避免重复挂载端点产生路径冲突，也保持前端刷新后 SSE 连接与端点状态一致。
    """
    return router
