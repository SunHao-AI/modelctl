#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_http.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : admin_cluster REST/WS 端点测试
# ===============================================================================
"""admin_cluster：中心角色端点行为、鉴权、solo 404、WS hello/heartbeat 全流程。"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("websockets")
from fastapi.testclient import TestClient  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_12345"


def _h():
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture()
def center_client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CLUSTER_ROLE", "both")
    import modelctl.core.webui.admin_cluster as ac

    ac._REGISTRY = None  # 重置单例（CACHE_DIR 已由 conftest 隔离到 tmp_path）
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c
    ac._REGISTRY = None


def test_status_reports_center(center_client) -> None:
    r = center_client.get("/admin/api/cluster/status", headers=_h())
    assert r.status_code == 200 and r.json()["is_center"] is True


def test_nodes_empty(center_client) -> None:
    assert center_client.get("/admin/api/cluster/nodes", headers=_h()).json()["nodes"] == []


def test_events_empty(center_client) -> None:
    assert center_client.get("/admin/api/cluster/events", headers=_h()).json()["events"] == []


def test_join_token_rotate(center_client) -> None:
    r = center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h())
    assert r.status_code == 200 and r.json()["join_token"].startswith("JT-")


def test_cluster_requires_auth(center_client) -> None:
    assert center_client.get("/admin/api/cluster/nodes").status_code == 401


def test_solo_role_returns_404(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CLUSTER_ROLE", "solo")
    import modelctl.core.webui.admin_cluster as ac

    ac._REGISTRY = None
    app = create_app(admin=True)
    with TestClient(app) as c:
        assert c.get("/admin/api/cluster/nodes", headers=_h()).status_code == 404
    ac._REGISTRY = None


def test_ws_hello_heartbeat_registers_node(center_client) -> None:
    jt = center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h()).json()["join_token"]
    with center_client.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 1, "node_id": "w-9", "lan": "lan-1", "key": jt, "meta": {}})
        welcome = ws.receive_json()
        assert welcome["t"] == "welcome" and welcome["node_token"].startswith("NT-")
        ws.send_json({"t": "heartbeat", "payload": {"profiles": {}}})
        assert ws.receive_json()["t"] == "ack"
    nodes = center_client.get("/admin/api/cluster/nodes", headers=_h()).json()["nodes"]
    assert any(n["node_id"] == "w-9" and n["status"] == "online" for n in nodes)


def test_ws_bad_token_closes(center_client) -> None:
    from starlette.websockets import WebSocketDisconnect

    center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h())
    with center_client.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 1, "node_id": "w-x", "lan": "", "key": "bogus", "meta": {}})
        msg = ws.receive_json()
        assert msg["t"] == "error"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_ws_malformed_frame_errors_but_keeps_connection(center_client) -> None:
    """坏帧回 error 且不回显原文；连接必须存活（后续 heartbeat 仍能 ack）。"""
    jt = center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h()).json()["join_token"]
    with center_client.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 1, "node_id": "w-m", "lan": "", "key": jt, "meta": {}})
        assert ws.receive_json()["t"] == "welcome"
        ws.send_text("not-json SECRETRAW")
        msg = ws.receive_json()
        assert msg["t"] == "error" and msg["message"] == "消息解析失败"
        ws.send_json({"t": "heartbeat", "payload": {}})
        assert ws.receive_json()["t"] == "ack"


def test_ws_unknown_type_error_does_not_echo_payload(center_client) -> None:
    """未知类型的错误帧用固定文案，绝不回显对端可控的 t 原文。"""
    jt = center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h()).json()["join_token"]
    with center_client.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 1, "node_id": "w-u", "lan": "", "key": jt, "meta": {}})
        assert ws.receive_json()["t"] == "welcome"
        ws.send_json({"t": "INJECT-<script>"})
        msg = ws.receive_json()
        assert msg["t"] == "error" and msg["message"] == "未知消息类型"
        assert "INJECT" not in str(msg)
