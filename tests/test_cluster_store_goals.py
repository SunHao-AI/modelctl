#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_store_goals.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : ClusterStore M1 扩展测试（nodes 增列迁移 + goals/model_states CRUD）
# ===============================================================================

import sqlite3

import pytest

from modelctl.core.cluster.store import ClusterStore


@pytest.fixture()
def store(tmp_path):
    s = ClusterStore(tmp_path / "cluster-meta.db")
    s.init_db()
    return s


def _mk_goal(store, goal_id="qwen-vllm@@w-1", **over):
    kw = dict(node_id="w-1", profile="qwen-vllm", engine="vllm", profile_yaml="port: 8101\n",
              profile_sha="sha-a", profile_version="2026-09-04-aaaaaa", intent="start",
              params=None, env_overlay=None, placement=None, runtime_ref=None,
              target_role="primary", stage="PENDING_PROFILE_SYNC", created_by="op", now=100.0)
    kw.update(over)
    store.upsert_goal(goal_id=goal_id, **kw)
    return goal_id


def test_init_db_adds_m1_columns_idempotently(tmp_path):
    """旧库（无 M1 列）再 init 必须补齐列，且重复 init 不报错。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE nodes (node_id TEXT PRIMARY KEY, node_token TEXT NOT NULL)")
    conn.commit()
    conn.close()
    s = ClusterStore(db)
    s.init_db()
    s.init_db()  # 第二次：列已存在，不得抛 duplicate column
    cols = {r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(nodes)")}
    assert {"capacity_json", "runtime_json", "gateway_url", "last_goal_sync_sha"} <= cols


def test_update_node_capacity_none_keeps_existing(store):
    store.upsert_node(node_id="w-1", node_token="NT-1", lan_id="lan-1", role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    store.update_node_capacity("w-1", capacity={"gpu_count": 4, "vram_total_mb": 157280},
                               runtimes={"vllm": {"ok": True, "version": "0.9.1"}},
                               local_profiles=["qwen-vllm"], now=2.0)
    node = store.get_node("w-1")
    assert node["capacity"]["gpu_count"] == 4
    assert node["runtimes"]["vllm"]["ok"] is True
    assert node["local_profiles"] == ["qwen-vllm"]
    store.update_node_capacity("w-1", capacity=None, runtimes=None, now=3.0)
    assert store.get_node("w-1")["capacity"]["gpu_count"] == 4  # None 不覆盖（同 engines 语义）
    assert store.get_node("w-1")["local_profiles"] == ["qwen-vllm"]


def test_set_node_last_goal_sync_sha(store):
    store.upsert_node(node_id="w-1", node_token="NT-1", lan_id="", role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    store.set_node_last_goal_sync_sha("w-1", "rev-7")
    assert store.get_node("w-1")["last_goal_sync_sha"] == "rev-7"


def test_goal_upsert_roundtrip_decodes_json(store):
    g = store.get_goal(_mk_goal(store, params={"gpu_list": [0, 1]},
                                env_overlay={"MODEL_ROOT": "/m"},
                                placement={"gpu_count": 2, "min_vram_mb": 4096}))
    assert g["params"] == {"gpu_list": [0, 1]}
    assert g["env_overlay"] == {"MODEL_ROOT": "/m"}
    assert g["placement"]["gpu_count"] == 2
    assert g["stage"] == "PENDING_PROFILE_SYNC"


def test_goal_json_none_stays_none(store):
    g = store.get_goal(_mk_goal(store))
    assert g["params"] is None and g["env_overlay"] is None and g["placement"] is None


def test_goal_upsert_same_id_updates_but_keeps_created_at(store):
    gid = _mk_goal(store)
    created = store.get_goal(gid)["created_at"]
    store.upsert_goal(goal_id=gid, node_id="w-1", profile="qwen-vllm", engine="vllm",
                      profile_yaml="port: 8102\n", profile_sha="sha-b", profile_version="v2",
                      intent="stop", params=None, env_overlay=None, placement=None,
                      runtime_ref=None, target_role="replica", stage="READY",
                      created_by="op2", now=200.0)
    g = store.get_goal(gid)
    assert g["profile_sha"] == "sha-b" and g["intent"] == "stop" and g["stage"] == "READY"
    assert g["created_at"] == created and g["updated_at"] == 200.0


def test_list_goals_filters(store):
    _mk_goal(store, "a@@w-1", node_id="w-1", profile="a")
    _mk_goal(store, "b@@w-1", node_id="w-1", profile="b")
    _mk_goal(store, "a@@w-2", node_id="w-2", profile="a")
    assert {g["goal_id"] for g in store.list_goals(node_id="w-1")} == {"a@@w-1", "b@@w-1"}
    assert {g["goal_id"] for g in store.list_goals(profile="a")} == {"a@@w-1", "a@@w-2"}
    assert len(store.list_goals()) == 3


def test_update_goal_whitelist_and_absent(store):
    gid = _mk_goal(store)
    out = store.update_goal(gid, now=300.0, stage="FAILED", stage_reason="venv 缺失",
                            error_class="venv_missing", bogus_field="x")
    assert out["stage"] == "FAILED" and out["error_class"] == "venv_missing"
    assert "bogus_field" not in out
    assert store.update_goal("nope@@w-1", now=1.0, stage="READY") is None


def test_update_goal_json_field_roundtrip(store):
    gid = _mk_goal(store)
    out = store.update_goal(gid, now=1.0, params={"gpu_list": [3]})
    assert out["params"] == {"gpu_list": [3]}


def test_delete_goal_returns_snapshot_or_none(store):
    gid = _mk_goal(store)
    assert store.delete_goal(gid)["profile"] == "qwen-vllm"
    assert store.get_goal(gid) is None
    assert store.delete_goal(gid) is None


def test_model_state_upsert_and_delete(store):
    store.upsert_model_state(node_id="w-1", profile="qwen-vllm", state="READY",
                             gpu=[0, 1], port=8101, pid=4321, now=10.0)
    store.upsert_model_state(node_id="w-1", profile="qwen-vllm", state="FAILED",
                             gpu=None, port=None, pid=None, reason="gpu 冲突",
                             error_class="gpu_lock", now=20.0)
    rows = store.list_model_states(node_id="w-1")
    assert len(rows) == 1 and rows[0]["state"] == "FAILED"
    assert rows[0]["gpu"] is None and rows[0]["error_class"] == "gpu_lock"
    store.delete_model_state("w-1", "qwen-vllm")
    assert store.list_model_states(node_id="w-1") == []
