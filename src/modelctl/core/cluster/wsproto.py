#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/wsproto.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : 集群 WebSocket 消息协议（JSON 编解码，无网络依赖）
# ===============================================================================

"""core/cluster/wsproto.py — 一行一条 JSON 的 WS 消息编解码（设计文档 §5）。

M0 只用 hello/welcome/heartbeat/event/error；goal.sync/status.query 等 M1+ 再加。
零第三方依赖，可脱离 WS 单测。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTO_VERSION = 1


@dataclass
class HelloMsg:
    node_id: str = ""
    lan: str = ""
    key: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def make_hello(node_id: str, lan: str, key: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {"t": "hello", "v": PROTO_VERSION, "node_id": node_id, "lan": lan, "key": key, "meta": meta}


def make_welcome(node_token: str, interval_s: int, lease_s: int) -> dict[str, Any]:
    return {"t": "welcome", "node_token": node_token, "interval_s": interval_s, "lease_s": lease_s}


def make_heartbeat(payload: dict[str, Any]) -> dict[str, Any]:
    return {"t": "heartbeat", "payload": payload}


def make_event(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"t": "event", "kind": kind, "payload": payload}


def make_error(message: str) -> dict[str, Any]:
    return {"t": "error", "message": message}


def dumps(msg: dict[str, Any]) -> str:
    return json.dumps(msg, ensure_ascii=False)


def parse_type(raw: str) -> str:
    """解析消息类型；非法 JSON / 非 dict / 缺 t 一律返回空串（调用侧回 error 帧）。

    RecursionError：对端可构造数千层嵌套的 JSON 帧击穿 CPython 递归上限，该异常
    不属 ValueError 家族，若不外捕会直接掀掉中心侧的 WS 处理循环。
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return ""
    return str(data.get("t", "")) if isinstance(data, dict) else ""


def parse_hello(data: dict[str, Any]) -> HelloMsg:
    meta = data.get("meta")
    return HelloMsg(
        node_id=str(data.get("node_id", "")),
        lan=str(data.get("lan", "")),
        key=str(data.get("key", "")),
        meta=meta if isinstance(meta, dict) else {},
    )


def parse_heartbeat(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else {}
