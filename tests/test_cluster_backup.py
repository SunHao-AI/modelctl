#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_backup.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : 备份三函数：热备对账 / 校验拒坏档 / 恢复往返 + 事件落新库
# ===============================================================================
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from modelctl.core.cluster.backup import BackupError, create_backup, restore_backup, verify_backup
from modelctl.core.cluster.store import ClusterStore


@pytest.fixture()
def live(tmp_path, monkeypatch):
    db = tmp_path / "cluster-meta.db"
    # store.py 在 __init__ 内 lazy import cache_dir，patch 目标必须是 core.process（brief 模板笔误）
    monkeypatch.setattr("modelctl.core.process.cache_dir", lambda: tmp_path, raising=False)
    s = ClusterStore(db)
    s.init_db()
    s.upsert_node(node_id="w-1", node_token="NT-1", lan_id="lan-1", role="worker",
                  host_ip="", hostname="", engines=None, now=100.0)
    s.set_meta("join_token", "JT-x")
    return s, db


def test_create_backup_roundtrip_sha(live, tmp_path):
    _, db = live
    dest = tmp_path / "bk" / "m.db"
    dest.parent.mkdir()
    out = create_backup(dest)
    assert out["bytes"] == dest.stat().st_size > 0
    assert out["sha256"] == hashlib.sha256(dest.read_bytes()).hexdigest()


def test_create_backup_rejects_existing_without_force(live, tmp_path):
    _, db = live
    dest = tmp_path / "m.db"
    create_backup(dest)
    with pytest.raises(BackupError):
        create_backup(dest)
    create_backup(dest, force=True)          # force 覆盖成功


def test_create_backup_missing_parent_dir_errors(live, tmp_path):
    _, db = live
    with pytest.raises(BackupError):
        create_backup(tmp_path / "nope" / "m.db")


def test_verify_accepts_good_backup(live, tmp_path):
    _, db = live
    dest = tmp_path / "m.db"
    create_backup(dest)
    ok, reason = verify_backup(dest)
    assert ok, reason


def test_verify_rejects_non_db_and_missing_tables(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite file at all")
    ok, reason = verify_backup(bad)
    assert not ok and reason

    empty = tmp_path / "empty.db"
    # brief 模板的 .execute("COMMIT") 在 py3.13 下抛 "no transaction is active"
    # （DDL 不开隐式事务）；最小适配为显式关闭连接，断言逐字未动
    conn = sqlite3.connect(str(empty))
    conn.execute("CREATE TABLE x (a int)")
    conn.close()
    ok, reason = verify_backup(empty)
    assert not ok and "nodes" in reason


def test_verify_warns_but_passes_without_join_token(live, tmp_path):
    s, db = live
    s.set_meta("join_token", "")
    dest = tmp_path / "m.db"
    create_backup(dest)
    conn = sqlite3.connect(str(dest))
    conn.execute("DELETE FROM meta WHERE key='join_token'")
    conn.commit()
    conn.close()
    ok, _ = verify_backup(dest)
    assert ok                                  # 仅告警不硬失败


def test_restore_refuses_when_center_running(live, tmp_path, monkeypatch):
    s, db = live
    dest = tmp_path / "m.db"
    create_backup(dest)
    monkeypatch.setattr("modelctl.core.cluster.backup._center_running", lambda: True)
    with pytest.raises(BackupError):
        restore_backup(dest)


def test_restore_replaces_and_writes_event_into_new_db(live, tmp_path, monkeypatch):
    s, db = live
    dest = tmp_path / "m.db"
    create_backup(dest)
    # 备份之后再写一条 goal——恢复后它必须"消失"（对拍语义）
    s.upsert_goal(goal_id="q@@w-1", node_id="w-1", profile="q", engine="vllm",
                  profile_yaml="port: 1\n", profile_sha="s", profile_version=None,
                  intent="start", params=None, env_overlay=None, placement=None,
                  runtime_ref=None, target_role="primary", stage="READY",
                  created_by="op", now=200.0)
    monkeypatch.setattr("modelctl.core.cluster.backup._center_running", lambda: False)
    # 平台必需适配：fixture 的 s 连接仍持着 -wal 句柄（SQLite 共享模式不含 DELETE，
    # Windows 下阻塞 restore 的文件改名/删除）；真实 restore 是独立 CLI 进程、自身未开台账，
    # 这里等价地先关同进程连接。断言逐字未动。
    if s._conn is not None:
        s._conn.close()
    bak = restore_backup(dest)
    assert bak.exists() and ".pre-restore." in bak.name

    after = ClusterStore(db)
    after.init_db()
    assert after.list_goals() == []            # 恢复回备份时刻
    assert after.get_node("w-1") is not None   # 备份时刻已有的数据在
    kinds = [e["kind"] for e in after.recent_events()]
    assert "db.restore" in kinds               # 事件写恢复后的新库（spec §1 裁决）


def test_restore_refuses_corrupt_source(live, tmp_path, monkeypatch):
    s, db = live
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"garbage")
    monkeypatch.setattr("modelctl.core.cluster.backup._center_running", lambda: False)
    with pytest.raises(BackupError):
        restore_backup(bad)
    assert ClusterStore(db).get_node("w-1") is not None   # 现库未被碰
