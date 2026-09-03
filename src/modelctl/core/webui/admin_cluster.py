#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_cluster.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : 集群控制面 REST + WebSocket 端点（仅中心角色启用）
# ===============================================================================

"""core/webui/admin_cluster.py — /admin/api/cluster/* 与 /admin/api/ws/cluster。

非中心角色（solo/worker）全部端点 404；REST 过 require_auth（operator）；WS 在
hello 帧内用 join_token/node_token 鉴权（worker 不持有 API_KEY）。NodeRegistry
进程内单例，REST/WS 共享同一 SQLite 台账。设计文档 §5、§6.5、§10。
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from modelctl.core.cluster import config, tokens, wsproto
from modelctl.core.cluster.nodes import AuthError, NodeRegistry
from modelctl.core.cluster.store import ClusterStore
from modelctl.core.webui.admin_auth import require_auth

router = APIRouter()

_REGISTRY: NodeRegistry | None = None

_SWEEP_INTERVAL_S = 10.0
_last_sweep = 0.0


def get_registry() -> NodeRegistry:
    """NodeRegistry 进程内单例（懒建库）。测试经 admin_cluster._REGISTRY=None 重置。"""
    global _REGISTRY
    if _REGISTRY is None:
        store = ClusterStore()
        store.init_db()
        _REGISTRY = NodeRegistry(store)
    return _REGISTRY


def _router() -> APIRouter:
    return router


def _disabled() -> JSONResponse | None:
    return None if config.is_center() else JSONResponse(status_code=404, content={"detail": "cluster disabled"})


def _sweep_if_due() -> None:
    """惰性 lease 扫描：任一 REST/WS 事件顺带触发，≥10s 才真正扫一次（免后台线程）。"""
    global _last_sweep
    now = time.time()
    if now - _last_sweep < _SWEEP_INTERVAL_S:
        return
    _last_sweep = now
    for node_id, new_status in get_registry().sweep(now=now):
        logger.info(f"节点 {node_id} 状态迁移 → {new_status}")


@router.get("/cluster/status")
async def cluster_status(_base: None = Depends(require_auth)):
    if (off := _disabled()) is not None:
        return off
    _sweep_if_due()
    reg = get_registry()
    nodes = reg.store.list_nodes()
    return {"role": config.cluster_role(), "is_center": config.is_center(),
            "nodes_total": len(nodes), "nodes_online": sum(1 for n in nodes if n["status"] == "online")}


@router.get("/cluster/nodes")
async def cluster_nodes(_base: None = Depends(require_auth)):
    if (off := _disabled()) is not None:
        return off
    _sweep_if_due()
    return {"nodes": get_registry().list_node_views(now=time.time())}


@router.get("/cluster/events")
async def cluster_events(node_id: str = Query(""), limit: int = Query(100, ge=1, le=1000),
                         _base: None = Depends(require_auth)):
    if (off := _disabled()) is not None:
        return off
    events = get_registry().store.recent_events(limit=limit, node_id=node_id or None)
    return {"events": events}


@router.post("/cluster/join-tokens/rotate")
async def rotate_join_token(_base: None = Depends(require_auth)):
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    fresh = tokens.new_join_token()
    reg.store.set_meta("join_token", fresh)
    reg.store.append_event("token.rotate", payload={"scope": "join"})
    return {"join_token": fresh}


class _JoinCheckBody(BaseModel):
    node_id: str
    key: str
    lan: str = ""
    host_ip: str = ""
    hostname: str = ""


@router.post("/cluster/join-check")
async def join_check(body: _JoinCheckBody):
    """CLI join 预检：凭据=请求体 join token（同 WS hello 校验路径，参照 /login 先例）。

    成功即预注册节点（status=offline，WS hello 后转 online）并同步返回 node_token，
    CLI 直接落 .env，Agent 首连即用节点身份。
    """
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    if tokens.token_matches(body.key, reg.ensure_join_token()):
        node_token = tokens.new_node_token()
        result = reg.store.upsert_node(node_id=body.node_id, node_token=node_token, lan_id=body.lan,
                                       role="worker", host_ip=body.host_ip, hostname=body.hostname,
                                       engines=None, now=time.time())
        if result == "joined":
            reg.store.set_node_status(body.node_id, "offline")  # 预注册：等待首次 WS hello
        reg.store.append_event("node.join_check", node_id=body.node_id, payload={"result": result})
        return {"ok": True, "node_token": node_token}
    known = reg.store.find_node_by_token(body.key)
    if known is None:
        return JSONResponse(status_code=401, content={"detail": "无效的 join/node token"})
    return {"ok": True, "node_token": str(known["node_token"])}


@router.websocket("/ws/cluster")
async def ws_cluster(ws: WebSocket):
    """worker 通道：hello（token 鉴权）→ welcome → heartbeat/event 循环。

    身份绑定：node_id 只存本连接的局部变量，handle_hello 已强制 NT↔node_id 一致，
    故后续 heartbeat/event 只能落到已鉴权的那个节点，无法伪造他人身份。
    对端可控输入（raw 帧、mtype）一律不回显原文：错误帧只用固定文案，避免把不可信
    内容写回日志/其他客户端。
    """
    if not config.is_center():
        await ws.close(code=4404)
        return
    await ws.accept()
    reg = get_registry()
    node_id = ""
    try:
        hello_raw = await ws.receive_text()
        # parse_type 仅在"可解析且为 dict"时返回非空，故其返回 hello 时下面 loads 必成功
        if wsproto.parse_type(hello_raw) != "hello":
            await ws.send_text(wsproto.dumps(wsproto.make_error("首帧须为 hello")))
            await ws.close(code=4400)
            return
        welcome, node_id = reg.handle_hello(wsproto.parse_hello(json.loads(hello_raw)))
        await ws.send_text(wsproto.dumps(welcome))
        while True:
            raw = await ws.receive_text()
            mtype = wsproto.parse_type(raw)
            try:
                data: dict[str, Any] = json.loads(raw)
            except (ValueError, TypeError, RecursionError):
                # RecursionError：深嵌套 JSON 击穿递归上限，与非法 JSON 同等处置
                await ws.send_text(wsproto.dumps(wsproto.make_error("消息解析失败")))
                continue
            if mtype == "heartbeat":
                reg.handle_heartbeat(node_id, wsproto.parse_heartbeat(data), now=time.time())
                _sweep_if_due()
                await ws.send_text(wsproto.dumps({"t": "ack"}))
            elif mtype == "event":
                payload = data.get("payload")
                reg.store.append_event(str(data.get("kind", "")), node_id=node_id,
                                       payload=payload if isinstance(payload, dict) else None)
                await ws.send_text(wsproto.dumps({"t": "ack"}))
            else:
                await ws.send_text(wsproto.dumps(wsproto.make_error("未知消息类型")))
    except WebSocketDisconnect:
        return
    except AuthError:
        await ws.send_text(wsproto.dumps(wsproto.make_error("鉴权失败")))
        await ws.close(code=4401)
        return
