#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_agent.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : cluster.agent 真连假中心测试
# ===============================================================================
"""WorkerAgent：URL 推导、心跳形状、hello→welcome→heartbeat 全链路（假中心）。"""
from __future__ import annotations

import json
import threading

import pytest

pytest.importorskip("websockets")
from websockets.sync.server import serve  # noqa: E402

from modelctl.core.cluster import agent, wsproto  # noqa: E402


def test_ws_url_derivation() -> None:
    assert agent.ws_url("http://a:4173", insecure=False) == "ws://a:4173/admin/api/ws/cluster"
    assert agent.ws_url("https://a:4173", insecure=False) == "wss://a:4173/admin/api/ws/cluster"
    assert agent.ws_url("https://a:4173", insecure=True) == "ws://a:4173/admin/api/ws/cluster"
    assert agent.ws_url("http://a:4173/", insecure=False) == "ws://a:4173/admin/api/ws/cluster"


def test_collect_heartbeat_shape() -> None:
    hb = agent.collect_heartbeat()
    assert "profiles" in hb and "gpu" in hb and "host" in hb


def test_agent_hello_then_heartbeat(tmp_path, monkeypatch) -> None:
    seen: dict[str, str] = {}
    hello_event = threading.Event()
    hb_event = threading.Event()

    def handler(conn):
        hello = wsproto.parse_hello(json.loads(conn.recv()))
        seen["node"] = hello.node_id
        seen["key"] = hello.key
        hello_event.set()
        conn.send(wsproto.dumps(wsproto.make_welcome("NT-signed", 1, 5)))
        msg = json.loads(conn.recv())
        if msg.get("t") == "heartbeat":
            hb_event.set()
        conn.send(wsproto.dumps({"t": "ack"}))

    with serve(handler, "127.0.0.1", 0) as srv:
        port = srv.socket.getsockname()[1]
        # websockets>=15 的 sync serve 只 bind/listen，须自行起线程跑 serve_forever，
        # 否则连接停在 backlog、握手永不完成（17.x 起为文档明确语义）。
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        monkeypatch.setenv("CLUSTER_CENTER_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("CLUSTER_NODE_ID", "w-test")
        monkeypatch.setenv("CLUSTER_JOIN_TOKEN", "JT-bootstrap")
        monkeypatch.setenv("CLUSTER_NODE_TOKEN", "")
        monkeypatch.setenv("CLUSTER_LAN", "lan-x")
        # 指向临时 .env，杜绝测试写仓库根 .env
        import modelctl.core.cluster.agent as ag

        monkeypatch.setattr(ag, "ENV_PATH", tmp_path / ".env")
        stop = threading.Event()
        t = threading.Thread(target=agent.WorkerAgent(stop_event=stop).run, daemon=True)
        t.start()
        assert hello_event.wait(5), "假中心未收到 hello"
        assert hb_event.wait(5), "假中心未收到 heartbeat"
        assert seen["node"] == "w-test" and seen["key"] == "JT-bootstrap"
        stop.set()
        t.join(timeout=5)
        # welcome 带回的 node_token 应已写回目标 .env
        assert "CLUSTER_NODE_TOKEN=NT-signed" in (tmp_path / ".env").read_text(encoding="utf-8")
