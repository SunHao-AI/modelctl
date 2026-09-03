#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_router.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/2 10:00
# @Desc   : 管理 API 主路由聚合
# ===============================================================================

"""core/webui/admin_router.py — /admin/api 主路由聚合。

create_admin_router() 返回聚合了全部子路由的 APIRouter（前缀 /admin/api 由调用
方设置）。FastAPI 在模块顶部导入——本包由 gateway.py::create_app / main 在已确
认 fastapi 可用时再 import，模块级导入安全（与 admin_auth/admin_tasks 保持一致
的 from __future__ import annotations 约定）。子模块经 _include_subrouter 延迟导
入并容错，规避循环依赖与分阶段落地缺项。
"""

from __future__ import annotations

# 认证/任务流为基础能力（与 P0 同期落地），直接导入；
# 其余子路由（admin_models/services/probe/envs/audit/config）为 P1/P2 模块，
# 通过 _include_subrouter 延迟导入并容错：未实现时跳过而非中断整个 admin 路由。
import asyncio
import json

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from modelctl.core.webui.admin_auth import require_auth, require_auth_or_query
from modelctl.core.webui.admin_tasks import Task, TaskManager

# 延迟导入的子模块及其前缀（按设计文档 §3.4 命名）。
# - admin_models：@router.get("") 与 @router.get("/{name}") 使用相对路径，需 "/models" 前缀
# - admin_audit：@router.get("") 与 @router.get("/stats") 等使用相对路径，需 "/audit" 前缀
# - 其余模块：@router 内的路径已是完整路径（/overview /services /envs /nginx-snippet 等），
#   前缀设为空串（等价于不挂前缀）
_SUBROUTER_MODULES: tuple[tuple[str, str], ...] = (
    ("modelctl.core.webui.admin_models", "/models"),
    ("modelctl.core.webui.admin_services", ""),
    ("modelctl.core.webui.admin_probe", ""),
    ("modelctl.core.webui.admin_envs", "/envs"),
    ("modelctl.core.webui.admin_audit", "/audit"),
    ("modelctl.core.webui.admin_config", ""),
)


def _include_subrouter(main_router: APIRouter, module_name: str, prefix: str) -> None:
    """延迟导入子模块并 include 其 _router() 工厂返回的 APIRouter。

    子模块须提供 `def _router() -> APIRouter` 工厂；prefix 非空时为子路由挂上
    子前缀（模块内端点须使用相对路径），空串时直接 include（模块内端点已是
    完整路径）。模块缺失或无工厂时静默跳过（分阶段落地时 P2 模块可能尚未
    实现），不中断其余路由的注册。
    """
    import importlib

    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return
    build = getattr(module, "_router", None)
    if not callable(build):
        return
    try:
        if prefix:
            main_router.include_router(build(), prefix=prefix)
        else:
            main_router.include_router(build())
    except Exception:  # noqa: BLE001 — 路由注册失败不应摧毁整个 admin API
        return


async def _sse_task_stream(task: Task):
    """任务 SSE async 流：先回推当前状态/已知日志，再实时广播新事件。

    - 首帧：把现有 logs 推完（一次性 tail 语义），再推 step 当前状态；
    - 之后从 task.subscribe() 入队，消费 task.event() 写入的 payload 后
      原样透传（payload 已是 ``event: type\ndata: json\n\n`` 形态）。
    - 10s 无活动推一次 heartbeat，最终在收到 done 事件后立即结束。
    - 兜底：客户端断开（GeneratorExit）必须 unsubscribe，避免队列泄漏。
    """
    q = task.subscribe()
    try:
        # 初始化：先把已有日志 flush（语义同 log/tail 端点）
        for line in task.logs:
            yield f"event: log\ndata: {{\"line\": {json.dumps(line, ensure_ascii=False)}}}\n\n"
        if task.status == "running" or task.status == "queued":
            yield (
                f"event: step\ndata: "
                f"{json.dumps({'step': 0, 'label': task.target, 'status': task.status, 'task_id': task.id}, ensure_ascii=False)}\n\n"
            )
        else:
            # 任务已结束，直接补一条 done 后返回
            payload = (
                f"event: done\ndata: "
                f"{json.dumps({'status': task.status, 'exit_code': task.exit_code, 'task_id': task.id, 'message': task.detail or ''}, ensure_ascii=False)}\n\n"
            )
            yield payload
            return
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=10.0)
            except asyncio.TimeoutError:
                yield "event: heartbeat\ndata: {}\n\n"
                continue
            yield payload
            # 解析 event type；若为 done 立即退出流（避免等待客户端断开）
            if payload.startswith("event: done"):
                return
    finally:
        task.unsubscribe(q)


def create_admin_router() -> APIRouter:
    """构建管理 API 主路由（聚合全部子路由），前缀 /admin/api 由调用方设置。

    /login、/health 不要求认证（见各子路由），其余端点用
    Depends(require_auth) 注入鉴权。SSE 端点（任务流 / 模型日志流）额外
    支持 ``?key=`` query 降级，因为浏览器 EventSource 无法携带 Authorization
    头（前端 TaskButton / SseLogViewer 用 query 携带 token）。

    返回的 router 携带运行时属性 `task_manager`（单例 TaskManager），
    调用方 include 后提升到 app 级：
        admin_router = create_admin_router()
        app.state.task_manager = admin_router.task_manager
        app.include_router(admin_router, prefix="/admin/api")
    """
    router = APIRouter()
    router.task_manager = TaskManager()  # 运行时附加属性，非 FastAPI 字段

    for module_name, prefix in _SUBROUTER_MODULES:
        _include_subrouter(router, module_name, prefix)

    # ------------------------------------------------------------------
    # 任务管理：/tasks 列表 + /tasks/{id}/stream SSE 流
    # EventSource 不能带 header，因此本两组端点用 require_auth_or_query
    # （query 携带 token 降级）。
    # ------------------------------------------------------------------

    @router.get("/tasks")
    async def list_tasks(
        request: Request,
        key: str = Query(default=""),
        _: None = Depends(require_auth_or_query),
    ):
        """GET /admin/api/tasks — 最近活动任务列表（最新在前）。"""
        tm: TaskManager = request.app.state.task_manager
        tasks = tm.list_tasks(limit=50)
        return {"tasks": [t.to_dict() for t in tasks]}

    @router.get("/tasks/{task_id}/stream")
    async def task_stream(
        task_id: str,
        request: Request,
        key: str = Query(default=""),
        _: None = Depends(require_auth_or_query),
    ):
        """GET /admin/api/tasks/{task_id}/stream — SSE 任务进度流。

        前端 TaskButton 在 start/restart/setup 等异步 202 后订阅本端点，
        收到 done 事件即结束。客户端断开时 unsubscribe 回收队列。
        """
        tm: TaskManager = request.app.state.task_manager
        task = tm.get_task(task_id)
        if task is None:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "not_found", "message": f"任务 {task_id} 不存在或已过期"}},
            )
        return StreamingResponse(
            _sse_task_stream(task),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router

