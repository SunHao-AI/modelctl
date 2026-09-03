#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/nodes.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : 中心侧节点注册/心跳/lease 扫描/脱敏视图编排
# ===============================================================================

"""core/cluster/nodes.py — 中心 NodeRegistry（设计文档 §5、§10.2）。

纯逻辑可单测；admin_cluster 仅做 HTTP/WS 薄封装。
"""

from __future__ import annotations

import time
from typing import Any

from modelctl.core.cluster import config, tokens
from modelctl.core.cluster.store import ClusterStore, mask_tail
from modelctl.core.cluster.wsproto import HelloMsg, make_welcome

_JOIN_TOKEN_META_KEY = "join_token"


class AuthError(Exception):
    """hello 鉴权失败：key 既不是 join_token 也不匹配任何 node_token。"""


class NodeRegistry:
    def __init__(self, store: ClusterStore) -> None:
        self.store = store

    def ensure_join_token(self) -> str:
        existing = self.store.get_meta(_JOIN_TOKEN_META_KEY)
        if existing:
            return existing
        fresh = tokens.new_join_token()
        self.store.set_meta(_JOIN_TOKEN_META_KEY, fresh)
        return fresh

    def handle_hello(self, hello: HelloMsg) -> tuple[dict[str, Any], str]:
        if not hello.node_id:
            raise AuthError("hello 缺少 node_id")
        join_token = self.ensure_join_token()
        if tokens.token_matches(hello.key, join_token):
            node_token = tokens.new_node_token()  # 首次 join：签发节点专属 token
        else:
            known = self.store.find_node_by_token(hello.key)
            if known is None:
                raise AuthError("无效的 join/node token")
            # node_token 与签发时的 node_id 绑定：防止持自己的 NT 冒充他人 node_id，
            # 经 upsert 的 ON CONFLICT 覆盖受害者行的 node_token（跨节点身份劫持）
            if str(known["node_id"]) != hello.node_id:
                raise AuthError("node_id 与节点令牌不匹配")
            node_token = str(known["node_token"])  # 重连：沿用既有 token
        engines = hello.meta.get("engines")
        self.store.upsert_node(
            node_id=hello.node_id, node_token=node_token, lan_id=hello.lan,
            role="worker", host_ip=str(hello.meta.get("host_ip", "")),
            hostname=str(hello.meta.get("hostname", "")),
            engines=engines if isinstance(engines, dict) else None,
            now=time.time(),
        )
        self.store.append_event("node.join", node_id=hello.node_id)
        welcome = make_welcome(node_token, config.heartbeat_interval_s(), config.lease_s())
        return welcome, hello.node_id

    def handle_heartbeat(self, node_id: str, payload: dict[str, Any], now: float) -> None:
        self.store.touch_heartbeat(node_id, now=now, lease_s=config.lease_s())

    def sweep(self, now: float) -> list[tuple[str, str]]:
        return self.store.sweep_expired(now=now, lease_s=config.lease_s())

    def node_view(self, node: dict[str, Any], now: float) -> dict[str, Any]:
        view = {k: v for k, v in node.items() if k != "node_token"}
        view["token_mask"] = mask_tail(str(node.get("node_token", "")))
        last_seen = node.get("last_seen")
        lease_expiry = node.get("lease_expiry")
        view["since_seen_s"] = round(now - last_seen, 1) if last_seen is not None else None
        view["lease_left_s"] = round(lease_expiry - now, 1) if lease_expiry is not None else None
        return view

    def list_node_views(self, now: float) -> list[dict[str, Any]]:
        return [self.node_view(n, now) for n in self.store.list_nodes()]
