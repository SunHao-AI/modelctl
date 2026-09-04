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


def test_join_check_valid_token_registers_offline_node(center_client) -> None:
    jt = center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h()).json()["join_token"]
    r = center_client.post("/admin/api/cluster/join-check",
                           json={"node_id": "w-c", "key": jt, "lan": "lan-7"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["node_token"].startswith("NT-")
    nodes = center_client.get("/admin/api/cluster/nodes", headers=_h()).json()["nodes"]
    target = [n for n in nodes if n["node_id"] == "w-c"][0]
    assert target["status"] == "offline"  # 预注册未连接：offline，WS hello 后转 online


def test_join_check_bad_token_401(center_client) -> None:
    center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h())
    r = center_client.post("/admin/api/cluster/join-check",
                           json={"node_id": "w-x", "key": "bogus", "lan": ""})
    assert r.status_code == 401


def test_join_check_rejoin_keeps_offline(center_client) -> None:
    """rejoined 分支也必须恒置 offline：join-check 是预注册，online 只属于 WS hello。"""
    import modelctl.core.webui.admin_cluster as ac

    jt = center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h()).json()["join_token"]
    r1 = center_client.post("/admin/api/cluster/join-check", json={"node_id": "w-r", "key": jt, "lan": ""})
    assert r1.status_code == 200
    nodes = center_client.get("/admin/api/cluster/nodes", headers=_h()).json()["nodes"]
    assert [n for n in nodes if n["node_id"] == "w-r"][0]["status"] == "offline"
    # 模拟该节点曾真实上线（WS hello 后会处于 online），再走一次 join-check（rejoined）
    ac.get_registry().store.set_node_status("w-r", "online")
    r2 = center_client.post("/admin/api/cluster/join-check", json={"node_id": "w-r", "key": jt, "lan": ""})
    assert r2.status_code == 200 and r2.json()["ok"] is True
    nodes = center_client.get("/admin/api/cluster/nodes", headers=_h()).json()["nodes"]
    target = [n for n in nodes if n["node_id"] == "w-r"][0]
    assert target["status"] == "offline", "rejoin 不得把节点伪装成 online"


def test_join_check_node_token_wrong_id_401(center_client) -> None:
    """node_token 命中他人节点时不得 200：防止把矛盾的 (node_id, NT) 写进 worker .env。"""
    jt = center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h()).json()["join_token"]
    r = center_client.post("/admin/api/cluster/join-check", json={"node_id": "w-owner", "key": jt, "lan": ""})
    nt = r.json()["node_token"]
    bad = center_client.post("/admin/api/cluster/join-check", json={"node_id": "w-impostor", "key": nt, "lan": ""})
    assert bad.status_code == 401


def test_join_check_empty_node_id_422(center_client) -> None:
    """空 node_id 属输入校验失败：422 拒绝，不得落库污染台账。"""
    jt = center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h()).json()["join_token"]
    r = center_client.post("/admin/api/cluster/join-check", json={"node_id": "", "key": jt, "lan": ""})
    assert r.status_code == 422


def test_sweep_failure_swallowed_endpoint_still_200(center_client, monkeypatch) -> None:
    """lease 扫描抛异常（模拟 SQLite 抖动）必须被吞掉：端点仍 200，不得掀掉请求/长连接。"""
    import modelctl.core.webui.admin_cluster as ac

    def _boom(now: float = 0.0):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ac.get_registry(), "sweep", _boom)
    monkeypatch.setattr(ac, "_last_sweep", 0.0)  # 强制本轮真正触发 sweep
    r = center_client.get("/admin/api/cluster/nodes", headers=_h())
    assert r.status_code == 200
    assert r.json()["nodes"] == []
