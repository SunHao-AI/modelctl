#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_nodes.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : cluster.nodes 注册/心跳/视图编排测试
# ===============================================================================
from __future__ import annotations

from pathlib import Path

import pytest

from modelctl.core.cluster.nodes import AuthError, NodeRegistry
from modelctl.core.cluster.store import ClusterStore
from modelctl.core.cluster.wsproto import HelloMsg


@pytest.fixture()
def reg(tmp_path: Path, monkeypatch) -> NodeRegistry:
    monkeypatch.setenv("CLUSTER_LEASE_S", "90")
    s = ClusterStore(db_path=tmp_path / "m.db")
    s.init_db()
    return NodeRegistry(s)


def test_ensure_join_token_stable(reg: NodeRegistry) -> None:
    t = reg.ensure_join_token()
    assert t.startswith("JT-")
    assert reg.ensure_join_token() == t


def test_first_join_issues_node_token(reg: NodeRegistry) -> None:
    jt = reg.ensure_join_token()
    welcome, node_id = reg.handle_hello(HelloMsg(node_id="w-1", lan="lan-2", key=jt,
                                                 meta={"host_ip": "10.0.0.5"}))
    assert node_id == "w-1"
    assert welcome["t"] == "welcome" and welcome["node_token"].startswith("NT-")


def test_rejoin_reuses_node_token(reg: NodeRegistry) -> None:
    jt = reg.ensure_join_token()
    nt = reg.handle_hello(HelloMsg(node_id="w-1", lan="", key=jt, meta={}))[0]["node_token"]
    welcome2, _ = reg.handle_hello(HelloMsg(node_id="w-1", lan="", key=nt, meta={}))
    assert welcome2["node_token"] == nt


def test_bad_token_rejected(reg: NodeRegistry) -> None:
    reg.ensure_join_token()
    with pytest.raises(AuthError):
        reg.handle_hello(HelloMsg(node_id="w-x", lan="", key="bogus", meta={}))


def test_heartbeat_then_sweep(reg: NodeRegistry) -> None:
    jt = reg.ensure_join_token()
    reg.handle_hello(HelloMsg(node_id="w-1", lan="", key=jt, meta={}))
    reg.handle_heartbeat("w-1", {"profiles": {}}, now=0.0)
    assert reg.store.get_node("w-1")["status"] == "online"
    assert ("w-1", "stale") in reg.sweep(now=95.0)


def test_node_view_masks_token(reg: NodeRegistry) -> None:
    jt = reg.ensure_join_token()
    nt = reg.handle_hello(HelloMsg(node_id="w-1", lan="lan-9", key=jt,
                                   meta={"hostname": "w1"}))[0]["node_token"]
    reg.handle_heartbeat("w-1", {}, now=100.0)
    view = reg.list_node_views(now=105.0)[0]
    assert "node_token" not in view
    assert view["token_mask"] == "***" + nt[-4:]
    assert view["since_seen_s"] == pytest.approx(5.0)
    assert view["lease_left_s"] == pytest.approx(85.0)
