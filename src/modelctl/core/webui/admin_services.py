#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_services.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/2 10:00
# Desc   : 服务管理与一键启停 API 端点
# ===============================================================================
"""core/webui/admin_services.py — 服务管理与一键启停 API 端点。

两条产品线：
1. 服务管理：/services（stats/gateway 状态 + 家族路由预览）与
   /services/{svc}/{action}（start/restart 走 TaskManager 异步任务 202；stop 同步 200）。
2. 一键启停：/all/start、/all/restart（异步 202 + TaskManager）、
   /all/stop（同步）、/all/status（同步汇总）。

依赖 FastAPI（Web UI 复用 gateway 独立 venv 中已安装的 fastapi）+ loguru；
modelctl.core 子模块在函数体内延迟导入（与 admin_models/admin_envs 同款约定）。
"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from loguru import logger

from modelctl.core.webui.admin_auth import require_auth

router = APIRouter()


def _router() -> APIRouter:
    """子路由工厂：返回 APIRouter（主路由 include 时由其调用）。"""
    return router


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _cr_state(component_result) -> str:
    """ComponentResult → 简单状态字符串（running / stopped / error）。

    复用现有服务操作的约定：status_gateway/status_stats 仅返回 ok/skipped/error；
    已停止会被 status_* 标为 nonexistent 类，本端点只做"显示层"三态，
    真正的进程探活用 is_running 兜底。
    """
    status = getattr(component_result, "status", None)
    detail = getattr(component_result, "detail", "") or ""
    # status_gateway/stats：已停止的 detail 含"已停止" → stopped，运行中含"运行中" → running；
    # error 统一 error（stop/start 异常时）
    if status == "error":
        return "error"
    if "已停止" in detail:
        return "stopped"
    if "运行中" in detail:
        return "running"
    return "error"


def _family_routing(profiles) -> dict:
    """按 group 聚合家族路由（未声明 group 的归 "(其它)"，组内按引擎优先级排序）。

    与 admin_models.list_models 同款形态；每组每成员附 running 标志。
    NAME → MEMORY（"模型名 → 缓存" 提示）由前端根据 running/running 状态展示，
    本端点不做跨模型复用计算。
    """
    from modelctl.core.gateway import ENGINE_PRIORITY
    from modelctl.core.process import is_running

    def _running(name: str) -> bool:
        try:
            return is_running(name)
        except Exception:  # noqa: BLE001 — 防御：is_running 个别进程扫描异常时降级 False
            return False

    groups: dict = {}
    for p in profiles:
        g = p.group or "(其它)"
        groups.setdefault(g, []).append({
            "name": p.name,
            "engine": p.engine,
            "priority": ENGINE_PRIORITY.get(p.engine, 99),
            "running": _running(p.name),
        })
    for members in groups.values():
        members.sort(key=lambda m: m["priority"])
    return groups


# ---------------------------------------------------------------------------
# 服务管理端点
# ---------------------------------------------------------------------------


@router.get("/services")
async def get_services(request: Request, _: None = Depends(require_auth)):
    """GET /admin/api/services — 概览服务卡片 + 家族路由预览（复用同一数据源）。

    数据源：status_gateway / status_stats（来自 all_service）+ list_profiles + is_running。
    """
    from modelctl.core.all_service import status_gateway, status_stats
    from modelctl.core.gateway import GATEWAY_PORT
    from modelctl.core.profile import list_profiles
    from modelctl.core.stats import USAGE_PORT

    stats_r = await asyncio.to_thread(status_stats)
    gateway_r = await asyncio.to_thread(status_gateway)
    profiles = await asyncio.to_thread(list_profiles)

    return {
        "stats": {"state": _cr_state(stats_r), "port": USAGE_PORT, "detail": stats_r.detail},
        "gateway": {"state": _cr_state(gateway_r), "port": GATEWAY_PORT, "detail": gateway_r.detail},
        "family_routing": _family_routing(profiles),
        "default_model": os.environ.get("GATEWAY_DEFAULT_MODEL", ""),
    }


@router.post("/services/{svc}/{action}")
async def service_action(svc: str, action: str, request: Request, _: None = Depends(require_auth)):
    """POST /admin/api/services/{svc}/{action} — 启动/停止/重启 stats|gateway。

    action ∈ {start, stop, restart}。start/restart 走异步任务（202 + task_id +
    stream_url）；stop 同步（200 {ok, detail}）
    svc ∉ {stats, gateway} → 404 not_found；action 非法 → 422 config_error。
    """
    from modelctl.core.all_service import (
        restart_gateway,
        restart_stats,
        start_gateway,
        start_stats,
        stop_gateway,
        stop_stats,
    )
    from modelctl.core.envfile import load_env

    if svc not in ("stats", "gateway"):
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"未知服务 {svc}"}},
        )
    if action not in ("start", "stop", "restart"):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "config_error", "message": f"未知动作 {action}"}},
        )

    load_env()

    if action == "stop":
        result = await asyncio.to_thread(stop_stats if svc == "stats" else stop_gateway)
        return {"ok": result.status != "error", "detail": result.detail}

    # start / restart —— 走 TaskManager 异步任务（202 + stream_url）
    tm: object = request.app.state.task_manager
    lock = await tm.acquire(svc, action)
    if lock is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "task_conflict", "message": f"服务 {svc} 已有进行中的任务"}},
        )

    try:
        task = tm.create_task(kind=f"service_{action}", action=action, target=svc)
        task.update_status("queued")
        asyncio.ensure_future(_do_service_action(svc, action, task))
        return JSONResponse(
            status_code=202,
            content={"task_id": task.id, "stream_url": f"/admin/api/tasks/{task.id}/stream"},
        )
    finally:
        await tm.release(svc, action)


async def _do_service_action(svc: str, action: str, task) -> None:
    """Worker 线程：执行服务 start/restart（作用于 stats / gateway）。

    与 admin_models._do_start 同款模式：to_thread 执行同步 shell 编排（
    start_stats/restart_stats/... 内部含 nvidia-smi 探测 + wait_health 超时），
    完成后回写 task 状态并触发 SSE event。
    start/restart 超时由调用方控制（all_service.start_stats/restart_stats 内部
    有 wait_health 默认 3s），本处不显式额外 timeout（要地狱级等待见 cli 的
    `modelctl gateway start --timeout N`）。
    """
    from modelctl.core.all_service import (
        restart_gateway,
        restart_stats,
        start_gateway,
        start_stats,
    )

    task.update_status("running")
    try:
        fn = {
            ("stats", "start"): start_stats,
            ("stats", "restart"): restart_stats,
            ("gateway", "start"): start_gateway,
            ("gateway", "restart"): restart_gateway,
        }[(svc, action)]
        result = await asyncio.to_thread(fn)
        task.update_detail(result.detail)
        if result.status == "error":
            task.error(exit_code=1, message=result.detail)
        else:
            task.complete()
    except Exception as exc:  # noqa: BLE001 — 服务操作异常统一记录 + 失败上报
        logger.exception(f"服务操作异常 ({svc} {action}): {exc}")
        task.error(exit_code=1, message=str(exc))


# ---------------------------------------------------------------------------
# 一键启停 /all/*
# ---------------------------------------------------------------------------


@router.post("/all/start")
async def all_start(
    request: Request,
    model: str | None = Query(default=None),
    timeout: float = Query(default=600, ge=1, le=3600),
    gpus: str | None = Query(default=None),
    _: None = Depends(require_auth),
):
    """POST /admin/api/all/start — 一键启动（默认模型 + gateway + stats）。异步。

    model 缺省读 GATEWAY_DEFAULT_MODEL；gpus 透传为逗号串（gateway 启动时由
    adapter 切换 GPU 占用并写回 gpu_lock）；timeout 控制 wait_health。
    start_all/models_dir 允许传 None（项目本地 models/ 由 list_profiles 兜底）。
    """
    from modelctl.core.all_service import start_all
    from modelctl.core.envfile import load_env

    load_env()

    thru = {"model": model, "timeout": timeout, "gpus": gpus}
    tm: object = request.app.state.task_manager
    lock = await tm.acquire("all", "start")
    if lock is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "task_conflict", "message": "一键启动已有进行中任务"}},
        )
    try:
        task = tm.create_task(kind="all_start", action="start", target="all")
        task.update_status("queued")
        asyncio.ensure_future(_do_all(start_all, thru, task))
        return JSONResponse(
            status_code=202,
            content={"task_id": task.id, "stream_url": f"/admin/api/tasks/{task.id}/stream"},
        )
    finally:
        await tm.release("all", "start")


@router.post("/all/stop")
async def all_stop(_: None = Depends(require_auth)):
    """POST /admin/api/all/stop — 一键停止（stats → gateway → 全部运行中模型）。同步。

    加锁原因：stop_all 内部可能触发 ollama 特判（共享 serve 决定 unload_model
    还是 stop_instance），与其他 stop/start 任务并发会产生"半杀"状态；
    侧面规避 + stop 单独也会被 is_running 跑一次——保持幂等即可，不强制阻塞。

    注：stop_all(models_dir) 第一个位置参数 models_dir 必填（无默认值），
    Web UI 不持有 models_dir 路径，传 None 让其回退 models/ 默认目录
    （与 status_all / restart_all 调用约定一致）。
    """
    from modelctl.core.all_service import stop_all
    from modelctl.core.envfile import load_env

    load_env()

    results = await asyncio.to_thread(stop_all, None)
    # 失败 < 2 时只 warning；error 数量 / 0 时分别打 info / 静默
    errors = [r for r in results if r.status == "error"]
    if len(errors) < 2:
        from loguru import logger as _lg

        _lg.debug(f"all/stop 完成：{len(errors)} 个 error / {len(results)} 个组件")
    return {
        "ok": not errors,
        "stopped": [r.component for r in results if r.status in ("ok", "skipped")],
        "errors": [
            {"component": e.component, "detail": e.detail} for e in errors
        ],
    }


@router.post("/all/restart")
async def all_restart(
    request: Request,
    model: str | None = Query(default=None),
    timeout: float = Query(default=600, ge=1, le=3600),
    gpus: str | None = Query(default=None),
    _: None = Depends(require_auth),
):
    """POST /admin/api/all/restart — 一键重启（默认模型 + gateway + stats）。异步。

    语义 = all/stop + all/start 的并发合并；stop_all 顺序：stats → gateway → 全部运行中模型，
    重启时保留 stop 结果作为前序；start 部分复用 all/start 的异步流。
    """
    from modelctl.core.all_service import restart_all
    from modelctl.core.envfile import load_env

    load_env()

    thru = {"model": model, "timeout": timeout, "gpus": gpus}
    tm: object = request.app.state.task_manager
    lock = await tm.acquire("all", "restart")
    if lock is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "task_conflict", "message": "一键重启已有进行中任务"}},
        )
    try:
        task = tm.create_task(kind="all_restart", action="restart", target="all")
        task.update_status("queued")
        asyncio.ensure_future(_do_all(restart_all, thru, task))
        return JSONResponse(
            status_code=202,
            content={"task_id": task.id, "stream_url": f"/admin/api/tasks/{task.id}/stream"},
        )
    finally:
        await tm.release("all", "restart")


@router.get("/all/status")
async def all_status(_: None = Depends(require_auth)):
    """GET /admin/api/all/status — 一键状态汇总（默认模型 + gateway + stats）。同步。

    注：status_all(models_dir) 第一个位置参数 models_dir 必填（无默认值），
    Web UI 不持有 models_dir 路径，传 None 让其回退 models/ 默认目录。
    """
    from modelctl.core.all_service import status_all
    from modelctl.core.envfile import load_env

    load_env()

    results = await asyncio.to_thread(status_all, None)
    return {
        "components": [
            {"component": r.component, "status": r.status, "detail": r.detail}
            for r in results
        ]
    }


async def _do_all(func, thru: dict, task) -> None:
    """Worker 线程：执行一键 start/restart（all_service.start_all / restart_all）。

    gpus 透传为逗号串（解析 + env 注入放在 _apply_gpus 里，成功后恢复原值）；
    model / timeout 直接进 fn 签名（缺省 None → all_service 内部走 env 兜底）。
    结束时按 error 数量分级：== 0 静默、1 个 warning、>= 2 error。
    """
    from modelctl.core.gpu_utils import resolve_gpu_list

    task.update_status("running")
    gpus = thru.get("gpus")
    prev_gpus = os.environ.get("MODELCTL_GPUS")
    if gpus:
        parsed = resolve_gpu_list(None, None, gpus)
        if parsed:
            os.environ["MODELCTL_GPUS"] = ",".join(str(g) for g in parsed)
    try:
        model = thru.get("model")
        timeout = float(thru.get("timeout") or 600)
        results = await asyncio.to_thread(func, None, model, timeout)

        task.update_detail(f"{len(results)} 个组件已处理")
        errors = [r for r in results if r.status == "error"]
        if not errors:
            task.complete()
            # == 0 时静默（debug 级）
            logger.debug(f"一键 {func.__name__} 完成：0 个 error / {len(results)} 个组件")
        else:
            if len(errors) == 1:
                logger.warning(
                    f"一键 {func.__name__} 完成：1 个 error / {len(results)} 个组件 "
                    f"（{errors[0].component}: {errors[0].detail}）"
                )
            else:
                logger.error(
                    f"一键 {func.__name__} 完成：{len(errors)} 个 error / {len(results)} 个组件"
                )
            task.error(
                exit_code=1,
                message=(
                    f"{len(errors)} 个组件失败（共 {len(results)}）："
                    + "; ".join(f"{e.component}: {e.detail}" for e in errors)
                ),
            )
    except Exception as exc:  # noqa: BLE001 — 一键操作异常统一记录 + 失败上报
        logger.exception(f"一键操作异常 ({getattr(func, '__name__', func)}): {exc}")
        task.error(exit_code=1, message=str(exc))
    finally:
        # 恢复 GPU 环境变量（无论 main / exception / 异常 三条路径都恢复）
        if gpus:
            if prev_gpus is None:
                os.environ.pop("MODELCTL_GPUS", None)
            else:
                os.environ["MODELCTL_GPUS"] = prev_gpus
