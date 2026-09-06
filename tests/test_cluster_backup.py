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
import subprocess
import sys
from pathlib import Path

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


# ---- fix round 1：Important 1 / Important 2 的聚焦回归 ----

@pytest.mark.parametrize("broken", ["read_bytes", "replace"])
def test_create_backup_oserror_becomes_backup_error(live, tmp_path, monkeypatch, broken):
    """Windows dest 被占用是现实场景：PermissionError 必须转 BackupError 且不留 tmp。"""
    _, db = live
    dest = tmp_path / "occ" / "m.db"
    dest.parent.mkdir()

    def boom(*_a, **_kw):
        raise PermissionError(13, "The process cannot access the file because it is being used")

    monkeypatch.setattr(Path, broken, boom)
    with pytest.raises(BackupError):
        create_backup(dest)
    assert not dest.exists()                              # dest 未被污染
    assert not dest.with_name(dest.name + ".tmp").exists()  # tmp 未残留


def test_restore_bak_snapshot_covers_uncheckpointed_wal(live, tmp_path, monkeypatch):
    """安全网 .bak 必须走 backup API：台账 -wal 里已提交未 checkpoint 的数据也要在内。"""
    s, db = live
    dest = tmp_path / "m.db"
    create_backup(dest)
    if s._conn is not None:                               # 释放句柄，让子进程独占 WAL 写入
        s._conn.close()
    # 子进程提交后 os._exit：进程死亡不留句柄，-wal 里留下未 checkpoint 的已提交事务
    child = (
        "import os, sqlite3; "
        f"c = sqlite3.connect(r'{db}'); "
        "c.execute('PRAGMA journal_mode=WAL'); "
        "c.execute(\"INSERT INTO meta(key,value) VALUES('pre-crash','yes')\"); "
        "c.commit(); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", child], check=True)
    wal = db.with_name(db.name + "-wal")
    assert wal.is_file() and wal.stat().st_size > 0       # 场景成立：确有未回放日志
    monkeypatch.setattr("modelctl.core.cluster.backup._center_running", lambda: False)
    bak = restore_backup(dest)

    assert bak.exists() and ".pre-restore." in bak.name
    conn = sqlite3.connect(f"file:{bak}?mode=ro", uri=True)
    try:
        assert str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower() == "ok"
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"nodes", "goals", "model_states", "events", "meta"} <= have
        row = conn.execute("SELECT value FROM meta WHERE key='pre-crash'").fetchone()
        assert row is not None and row[0] == "yes"        # 裸 copyfile 会静默丢掉这行
        assert conn.execute("SELECT 1 FROM nodes WHERE node_id='w-1'").fetchone() is not None
    finally:
        conn.close()


# ---------------- CLI 接线（probe 打桩）----------------
BASE = "http://center:4173"


# brief 缺陷最小适配：cli_env 在模板中定义却未挂在用例上（同 goal_cli 先例为 autouse），
# 不生效则 CLUSTER_CENTER_URL 缺失、URL 断言必挂；补 autouse=True，用例体逐字未动。
@pytest.fixture(autouse=True)
def cli_env(monkeypatch, tmp_path):
    for k in ("CLUSTER_ROLE", "CLUSTER_CENTER_URL", "CLUSTER_NODE_ID", "CLUSTER_LAN",
              "CLUSTER_JOIN_TOKEN", "CLUSTER_NODE_TOKEN", "API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLUSTER_CENTER_URL", BASE)
    monkeypatch.setenv("API_KEY", "sk-cli")
    import modelctl.core.envfile as ef

    monkeypatch.setattr(ef, "PROJECT_ROOT", tmp_path)


def _main(argv):
    from modelctl import cli

    return cli.main(argv)


def test_cli_backup_downloads_and_verifies_sha(monkeypatch, tmp_path, capsys):
    from modelctl.core.cluster import center_probe

    payload = b"fake-sqlite-bytes"
    import hashlib
    sha = hashlib.sha256(payload).hexdigest()

    def fake_download(url, dest, api_key="", timeout=60.0):
        assert url.startswith(BASE + "/admin/api/cluster/backup")
        dest.write_bytes(payload)
        return 200, {"sha256": sha, "header_sha256": sha, "bytes": len(payload)}

    monkeypatch.setattr(center_probe, "download_file", fake_download)
    dest = tmp_path / "bk.db"
    assert _main(["cluster", "backup", "--to", str(dest)]) == 0
    assert dest.read_bytes() == payload
    assert sha[:12] in capsys.readouterr().out


def test_cli_backup_sha_mismatch_exit2_and_removes_file(monkeypatch, tmp_path):
    from modelctl.core.cluster import center_probe

    def fake_download(url, dest, api_key="", timeout=60.0):
        dest.write_bytes(b"x")
        return 200, {"sha256": "0" * 64, "header_sha256": "f" * 64, "bytes": 1}

    monkeypatch.setattr(center_probe, "download_file", fake_download)
    dest = tmp_path / "bk.db"
    assert _main(["cluster", "backup", "--to", str(dest)]) == 2
    assert not dest.exists()                   # 对账失败的文件不留


def test_cli_restore_yes_calls_core_restore(monkeypatch, tmp_path):
    calls = {}

    def fake_restore(src, *, assume_stopped=False):
        calls["src"] = src
        from pathlib import Path

        return Path(str(src) + ".bak")

    monkeypatch.setattr("modelctl.core.cluster.backup.restore_backup", fake_restore)
    src = tmp_path / "bk.db"
    src.write_bytes(b"x")
    assert _main(["cluster", "restore", "--from", str(src), "--yes"]) == 0
    assert calls["src"] == src


def test_cli_restore_declined_no_call(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr("modelctl.core.cluster.backup.restore_backup",
                        lambda *a, **k: called.append(1))
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    src = tmp_path / "bk.db"
    src.write_bytes(b"x")
    assert _main(["cluster", "restore", "--from", str(src)]) == 2
    assert called == []
