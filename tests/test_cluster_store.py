#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_store.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : cluster.store SQLite 中心台账测试
# ===============================================================================
"""ClusterStore：建表、meta、节点 upsert、心跳/lease 三态迁移、事件。"""
from __future__ import annotations

from pathlib import Path

import pytest

from modelctl.core.cluster.store import ClusterStore, mask_tail


@pytest.fixture()
def store(tmp_path: Path) -> ClusterStore:
    s = ClusterStore(db_path=tmp_path / "cluster-meta.db")
    s.init_db()
    return s


def test_meta_roundtrip(store: ClusterStore) -> None:
    assert store.get_meta("join_token") == ""
    store.set_meta("join_token", "JT-1")
    assert store.get_meta("join_token") == "JT-1"


def test_upsert_node_join_then_rejoin(store: ClusterStore) -> None:
    assert store.upsert_node(node_id="w-210", node_token="t1", lan_id="lan-2",
                             role="worker", host_ip="10.0.0.5", hostname="w210",
                             engines={"vllm": "0.9.1"}, now=1000.0) == "joined"
    assert store.upsert_node(node_id="w-210", node_token="t1", lan_id="lan-2",
                             role="worker", host_ip="10.0.0.6", hostname="w210",
                             engines=None, now=1001.0) == "rejoined"
    node = store.get_node("w-210")
    assert node is not None and node["host_ip"] == "10.0.0.6"
    assert node["engines"] == {"vllm": "0.9.1"}  # None 不覆盖既有 engines


def test_find_by_token(store: ClusterStore) -> None:
    store.upsert_node(node_id="w-1", node_token="secret", lan_id="", role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    assert store.find_node_by_token("secret")["node_id"] == "w-1"
    assert store.find_node_by_token("nope") is None


def test_lease_three_states(store: ClusterStore) -> None:
    store.upsert_node(node_id="w-1", node_token="t", lan_id="", role="worker",
                      host_ip="", hostname="", engines=None, now=0.0)
    store.touch_heartbeat("w-1", now=0.0, lease_s=90)
    assert store.get_node("w-1")["status"] == "online"
    # lease 过期但未过 3×lease → stale
    assert ("w-1", "stale") in store.sweep_expired(now=95.0, lease_s=90)
    assert store.get_node("w-1")["status"] == "stale"
    # 已是 stale 再扫不重复报告；last_seen 过 3×lease → offline
    transitions = store.sweep_expired(now=95.0, lease_s=90)
    assert ("w-1", "stale") not in transitions
    assert ("w-1", "offline") in store.sweep_expired(now=300.0, lease_s=90)
    assert store.get_node("w-1")["status"] == "offline"


def test_rejoin_clears_stale_lease(store: ClusterStore) -> None:
    store.upsert_node(node_id="w-1", node_token="t", lan_id="", role="worker",
                      host_ip="", hostname="", engines=None, now=0.0)
    store.touch_heartbeat("w-1", now=0.0, lease_s=90)
    assert ("w-1", "offline") in store.sweep_expired(now=1000.0, lease_s=90)
    # rejoin：注册即 online，旧 lease 必须清空，否则首个心跳前会被 sweep 误判 stale
    assert store.upsert_node(node_id="w-1", node_token="t", lan_id="", role="worker",
                             host_ip="", hostname="", engines=None, now=1001.0) == "rejoined"
    node = store.get_node("w-1")
    assert node is not None and node["status"] == "online"
    assert node["lease_expiry"] is None
    assert ("w-1", "stale") not in store.sweep_expired(now=1002.0, lease_s=90)


def test_set_node_status_rejects_invalid(store: ClusterStore) -> None:
    store.upsert_node(node_id="w-1", node_token="t", lan_id="", role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    with pytest.raises(ValueError):
        store.set_node_status("w-1", "stalee")


def test_rotate_node_token(store: ClusterStore) -> None:
    store.upsert_node(node_id="w-1", node_token="old", lan_id="", role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    new = store.rotate_node_token("w-1")
    assert new and new != "old"
    assert store.get_node("w-1")["node_token"] == new
    assert store.rotate_node_token("ghost") is None


def test_events_ordering_and_filter(store: ClusterStore) -> None:
    store.append_event("node.join", node_id="w-1", now=1.0)
    store.append_event("node.heartbeat", node_id="w-2", now=2.0)
    assert [e["kind"] for e in store.recent_events()] == ["node.heartbeat", "node.join"]
    assert [e["kind"] for e in store.recent_events(node_id="w-1")] == ["node.join"]


def test_recent_events_same_ts_stable_order(store: ClusterStore) -> None:
    """同一 ts 的多条事件按 id 倒序稳定返回（ORDER BY ts DESC, id DESC）。"""
    store.append_event("node.join", node_id="w-1", now=5.0)
    store.append_event("token.rotate", node_id="w-1", now=5.0)
    assert [e["kind"] for e in store.recent_events()] == ["token.rotate", "node.join"]


def test_sweep_after_failed_write_no_transaction_leak(store: ClusterStore) -> None:
    """写方法 DML 失败不得让后续 sweep 抛 'within a transaction'（autocommit 加固）。"""
    store.upsert_node(node_id="w-1", node_token="t", lan_id="", role="worker",
                      host_ip="", hostname="", engines=None, now=0.0)
    store.touch_heartbeat("w-1", now=0.0, lease_s=90)
    try:  # 制造 DML 失败：set_node_status 非法值在 UPDATE 前 raise，不残留事务
        store.set_node_status("w-1", "stalee")
    except ValueError:
        pass
    assert store.sweep_expired(now=1000.0, lease_s=90) == [("w-1", "offline")]


def test_mask_tail() -> None:
    assert mask_tail("") == "***"
    assert mask_tail("abcdef") == "***cdef"
    assert mask_tail("ab") == "***"


def test_recent_events_kind_filter(store: ClusterStore) -> None:
    store.append_event("node.join", node_id="w-1", now=1.0)
    store.append_event("node.heartbeat", node_id="w-1", now=2.0)
    out = store.recent_events(kind="node.join")
    assert [e["kind"] for e in out] == ["node.join"]


def test_append_event_unknown_kind_still_stored(store: ClusterStore, caplog) -> None:
    # 告警语义（全局裁决 1）：未知 kind 入库 + warning，绝不抛——worker 上报炸不得
    store.append_event("vendor.custom", node_id="w-1", now=1.0)
    assert [e["kind"] for e in store.recent_events()] == ["vendor.custom"]


def test_explicit_columns_cover_full_row(store: ClusterStore) -> None:
    """显式列改造的护栏：三类读路径返回的键集合必须与 schema 全列一致。"""
    store.upsert_node(node_id="w-1", node_token="t", lan_id="l", role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    store.upsert_goal(goal_id="g@@w-1", node_id="w-1", profile="g", engine="vllm",
                      profile_yaml="port: 1\n", profile_sha="s", profile_version=None,
                      intent="start", params=None, env_overlay=None, placement=None,
                      runtime_ref=None, target_role="primary", stage="READY",
                      created_by="op", now=1.0)
    store.upsert_model_state(node_id="w-1", profile="g", state="running",
                             gpu=[0], port=8001, pid=1, now=1.0)
    node = store.get_node("w-1")
    goal = store.get_goal("g@@w-1")
    ms = store.list_model_states(node_id="w-1")[0]
    # brief 缺陷适配：_row_to_node 恒附加 capacity/runtimes/local_profiles 三个派生键
    # （gate/nodes/goals/reconcile 承重，"行为零变更"禁删），逐字 == 全列恒假；
    # 先减派生键，原始列集合断言逐字未动。
    assert set(node) - {"capacity", "runtimes", "local_profiles"} == {
        "node_id", "node_token", "lan_id", "role", "host_ip", "hostname", "engines",
        "created_at", "last_seen", "lease_expiry", "status", "disabled", "capacity_json",
        "runtime_json", "gateway_url", "last_goal_sync_sha", "local_profiles_json"}
    assert set(goal) == {
        "goal_id", "node_id", "profile", "engine", "profile_yaml", "profile_sha",
        "profile_version", "intent", "params", "env_overlay", "placement", "runtime_ref",
        "target_role", "traffic_weight", "stage", "stage_reason", "error_class",
        "created_by", "created_at", "updated_at"}
    assert set(ms) == {
        "node_id", "profile", "state", "gpu", "port", "pid", "reason", "endpoint_url",
        "endpoint_ready", "engine_version", "gpu_util", "metrics_p50_ms", "last_probe_ms",
        "error_class", "updated_at"}
