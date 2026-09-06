#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_governance_http.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : M2 治理 5 端点 + join-check 拒禁用（REST 面实态断言）
# ===============================================================================
from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from modelctl.core.cluster import conns  # noqa: E402
from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_12345"
BASE = "/admin/api/cluster/nodes"


def _h():
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture()
def center(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CLUSTER_ROLE", "both")
    monkeypatch.setattr("modelctl.core.cluster.goals.MODELS_DIR", tmp_path / "models")
    import modelctl.core.webui.admin_cluster as ac

    ac._REGISTRY = None
    ac._CONNS = conns.ConnectionRegistry()
    app = create_app(admin=True)
    with TestClient(app) as c:
        reg = ac.get_registry()
        reg.store.upsert_node(node_id="w-1", node_token="NT-1", lan_id="lan-1", role="worker",
                              host_ip="", hostname="", engines=None, now=time.time())
        yield c
    ac._REGISTRY = None
    ac._CONNS = conns.ConnectionRegistry()


def _store():
    import modelctl.core.webui.admin_cluster as ac
    return ac.get_registry().store


def test_disable_sets_disabled_status_and_event(center):
    r = center.post(f"{BASE}/w-1/disable", headers=_h())
    assert r.status_code == 200 and r.json()["kicked"] is False
    assert _store().get_node("w-1")["status"] == "disabled"
    kinds = [e["kind"] for e in _store().recent_events()]
    assert "node.disable" in kinds


def test_enable_round_trip(center):
    center.post(f"{BASE}/w-1/disable", headers=_h())
    r = center.post(f"{BASE}/w-1/enable", headers=_h())
    assert r.status_code == 200
    assert _store().get_node("w-1")["disabled"] == 0


def test_disable_missing_node_404(center):
    assert center.post(f"{BASE}/ghost/disable", headers=_h()).status_code == 404
    assert center.post(f"{BASE}/ghost/enable", headers=_h()).status_code == 404
    assert center.post(f"{BASE}/ghost/kick", headers=_h()).status_code == 404
    assert center.post(f"{BASE}/ghost/rotate-token", headers=_h()).status_code == 404
    assert center.delete(f"{BASE}/ghost", headers=_h()).status_code == 404


def test_rotate_token_returns_new_plaintext_once(center):
    before = _store().get_node("w-1")["node_token"]
    r = center.post(f"{BASE}/w-1/rotate-token", headers=_h())
    body = r.json()
    assert r.status_code == 200 and body["node_token"].startswith("NT-")
    assert body["node_token"] != before
    assert _store().get_node("w-1")["node_token"] == body["node_token"]
    assert "hint" in body
    # GET 列表/详情仍只出 mask，明文不外泄
    assert center.get("/admin/api/cluster/nodes", headers=_h()).json()["nodes"][0]["token_mask"].startswith("***")


def test_rotate_token_kicks_connection(center):
    import modelctl.core.webui.admin_cluster as ac
    epoch = ac._CONNS.join("w-1")
    center.post(f"{BASE}/w-1/rotate-token", headers=_h())
    assert ac._CONNS.is_current("w-1", epoch) is False


def test_kick_returns_kicked_flag(center):
    import modelctl.core.webui.admin_cluster as ac
    ac._CONNS.join("w-1")
    assert center.post(f"{BASE}/w-1/kick", headers=_h()).json()["kicked"] is True
    assert center.post(f"{BASE}/w-1/kick", headers=_h()).json()["kicked"] is False


def test_delete_retires_and_reports_removed_goals(center):
    _store().upsert_goal(goal_id="qwen@@w-1", node_id="w-1", profile="qwen", engine="vllm",
                         profile_yaml="port: 8101\n", profile_sha="sha-a", profile_version=None,
                         intent="start", params=None, env_overlay=None, placement=None,
                         runtime_ref=None, target_role="primary", stage="READY",
                         created_by="op", now=time.time())
    r = center.delete(f"{BASE}/w-1", headers=_h())
    assert r.status_code == 200
    assert r.json() == {"removed": True, "removed_goals": 1}
    assert _store().get_node("w-1") is None
    assert "node.retire" in [e["kind"] for e in _store().recent_events()]


def test_join_check_rejects_disabled(center):
    import modelctl.core.webui.admin_cluster as ac
    join = ac.get_registry().ensure_join_token()
    center.post(f"{BASE}/w-1/disable", headers=_h())
    r = center.post("/admin/api/cluster/join-check",
                    json={"node_id": "w-1", "key": join})
    assert r.status_code == 401 and "禁用" in r.json()["detail"]


# ---------------- GET /cluster/backup（Task 6）----------------
def test_backup_download_streams_and_records_event(center):
    r = center.get("/admin/api/cluster/backup", headers=_h())
    assert r.status_code == 200
    sha_header = r.headers.get("x-backup-sha256", "")
    import hashlib
    assert sha_header == hashlib.sha256(r.content).hexdigest()
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "modelctl-cluster-" in r.headers.get("content-disposition", "")
    kinds = [e["kind"] for e in _store().recent_events()]
    assert "db.backup" in kinds


def test_backup_download_requires_auth(center):
    assert center.get("/admin/api/cluster/backup").status_code == 401
