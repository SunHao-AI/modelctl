#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_governance.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : M2 治理 core：disabled 写路径 / revoke / 级联退役 / hello 拒禁用
# ===============================================================================
"""治理动作的正确性只盯台账与世表实态，不信任方法返回值自述。"""
from __future__ import annotations

import pytest

from modelctl.core.cluster import conns as conns_mod
from modelctl.core.cluster.goals import GoalService
from modelctl.core.cluster.nodes import AuthError, NodeRegistry
from modelctl.core.cluster.store import ClusterStore
from modelctl.core.cluster.wsproto import make_hello


@pytest.fixture()
def store(tmp_path):
    s = ClusterStore(tmp_path / "cluster-meta.db")
    s.init_db()
    return s


@pytest.fixture()
def reg(store):
    return NodeRegistry(store, goals=GoalService(store))


def _node(store, node_id="w-1", now=100.0):
    store.upsert_node(node_id=node_id, node_token=f"NT-{node_id}", lan_id="lan-1",
                      role="worker", host_ip="", hostname="", engines=None, now=now)


def _goal(store, goal_id, node_id, profile):
    store.upsert_goal(goal_id=goal_id, node_id=node_id, profile=profile, engine="vllm",
                      profile_yaml="port: 8101\n", profile_sha="sha-a", profile_version=None,
                      intent="start", params=None, env_overlay=None, placement=None,
                      runtime_ref=None, target_role="primary",
                      stage="READY", created_by="op", now=100.0)


# ---------------- store：disabled / 级联 ----------------
def test_set_node_disabled_sets_status_and_flag(store):
    _node(store)
    assert store.set_node_disabled("w-1", True, status="disabled") is True
    row = store.get_node("w-1")
    assert row["disabled"] == 1 and row["status"] == "disabled"
    assert store.set_node_disabled("w-1", False) is True
    row = store.get_node("w-1")
    assert row["disabled"] == 0 and row["status"] == "disabled"  # 不带 status 则不动 status


def test_set_node_disabled_missing_node_returns_false(store):
    assert store.set_node_disabled("ghost", True, status="disabled") is False


def test_delete_node_cascade_removes_goals_and_states_keeps_events(store):
    _node(store)
    _goal(store, "qwen@@w-1", "w-1", "qwen")
    _goal(store, "llm@@w-1", "w-1", "llm")
    _goal(store, "qwen@@w-2", "w-2", "qwen")
    store.upsert_model_state(node_id="w-1", profile="qwen", state="running",
                             gpu=[0], port=8101, pid=7, now=100.0)
    store.append_event("node.join", node_id="w-1", now=100.0)
    assert store.delete_node_cascade("w-1") == 2
    assert store.get_node("w-1") is None
    assert [g["goal_id"] for g in store.list_goals()] == ["qwen@@w-2"]  # 他节点不动
    assert store.list_model_states(node_id="w-1") == []
    assert [e["kind"] for e in store.recent_events()] == ["node.join"]  # 审计留痕


def test_retire_node_returns_count_and_records_event(store):
    _node(store)
    _goal(store, "qwen@@w-1", "w-1", "qwen")
    out = store.retire_node("w-1", append_event_fn=store.append_event)
    assert out == {"removed_goals": 1}
    kinds = [e["kind"] for e in store.recent_events()]
    assert "node.retire" in kinds


def test_retire_node_missing_returns_none_without_event(store):
    assert store.retire_node("ghost", append_event_fn=store.append_event) is None
    assert store.recent_events() == []


# ---------------- conns.revoke ----------------
def test_revoke_invalidates_current_epoch_and_returns_it():
    c = conns_mod.ConnectionRegistry()
    epoch = c.join("w-1")
    assert c.revoke("w-1") == epoch
    assert c.is_current("w-1", epoch) is False
    assert c.current_epoch("w-1") is None


def test_revoke_without_connection_returns_none():
    assert conns_mod.ConnectionRegistry().revoke("w-1") is None


def test_release_after_revoke_does_not_evict_new_join():
    # revoke 后旧连接 finally 调 release(旧 epoch)：不得误杀 revoke 后重连的新 epoch
    c = conns_mod.ConnectionRegistry()
    old = c.join("w-1")
    c.revoke("w-1")
    new = c.join("w-1")
    c.release("w-1", old)
    assert c.is_current("w-1", new) is True


# ---------------- NodeRegistry 复合动作（签名与实现逐字对齐）----------------
def test_disable_online_node_kicks_connection(store, reg):
    _node(store)
    c = conns_mod.ConnectionRegistry()
    epoch = c.join("w-1")
    out = reg.disable_node("w-1", conns_registry=c)
    assert out == {"kicked": True} and c.is_current("w-1", epoch) is False
    assert store.get_node("w-1")["status"] == "disabled"
    assert "node.disable" in [e["kind"] for e in store.recent_events()]


def test_disable_without_registry_keeps_connection(store, reg):
    # CLI 直调面（无进程内连接面）：不传 registry 也能禁用，kicked 恒 False
    _node(store)
    assert reg.disable_node("w-1") == {"kicked": False}


def test_disable_missing_node_returns_none(reg):
    assert reg.disable_node("ghost") is None
    assert reg.enable_node("ghost") is None
    assert reg.kick_node("ghost", conns_mod.ConnectionRegistry()) is None


def test_enable_clears_disabled_flag(store, reg):
    _node(store)
    reg.disable_node("w-1")
    assert reg.enable_node("w-1") is not None
    assert store.get_node("w-1")["disabled"] == 0


def test_kick_records_event_and_revokes(store, reg):
    _node(store)
    c = conns_mod.ConnectionRegistry()
    epoch = c.join("w-1")
    assert reg.kick_node("w-1", c) == {"kicked": True}
    assert c.is_current("w-1", epoch) is False
    assert "node.kick" in [e["kind"] for e in store.recent_events()]


def test_kick_offline_node_reports_not_kicked(reg):
    _node(reg.store)
    assert reg.kick_node("w-1", conns_mod.ConnectionRegistry()) == {"kicked": False}


# ---------------- hello 拒 disabled ----------------
def _hello(node_id="w-1", key="NT-w-1"):
    from modelctl.core.cluster.wsproto import parse_hello
    return parse_hello(make_hello(node_id, "lan-1", key, {}))


def test_hello_rejects_disabled_node_with_node_token(store, reg):
    _node(store)
    store.set_node_disabled("w-1", True, status="disabled")
    with pytest.raises(AuthError):
        reg.handle_hello(_hello())


def test_hello_rejects_disabled_node_with_join_token(store, reg):
    _node(store)
    store.set_node_disabled("w-1", True, status="disabled")
    join = reg.ensure_join_token()
    with pytest.raises(AuthError):
        reg.handle_hello(_hello(key=join))  # join token 合法但节点禁用——身份对也拒


def test_hello_rejects_disabled_before_upsert(store, reg):
    # 禁用节点的 last_seen 不得因被拒的 hello 而刷新（防"禁而不死"的假在线心跳）
    _node(store, now=100.0)
    store.set_node_disabled("w-1", True, status="disabled")
    with pytest.raises(AuthError):
        reg.handle_hello(_hello())
    assert store.get_node("w-1")["last_seen"] == 100.0
