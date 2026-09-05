#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_events_http.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : M2 events 端点定版：形状/过滤/词表守卫/单端拼装
# ===============================================================================
from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from modelctl.core.cluster import conns, events  # noqa: E402
from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_12345"
EV = "/admin/api/cluster/events"
GOALS = "/admin/api/cluster/goals"


def _h():
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture()
def center(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CLUSTER_ROLE", "both")
    models = tmp_path / "models"
    (models / "vllm").mkdir(parents=True)
    (models / "vllm" / "qwen.yaml").write_text("port: 8101\n", encoding="utf-8")
    monkeypatch.setattr("modelctl.core.cluster.goals.MODELS_DIR", models)
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


def test_event_kinds_vocabulary_pinned():
    # 19 = 12 既有 + 7 新增；增删 kind 必须同时改词表（守卫的另一半在 append_event 告警）
    assert len(events.EVENT_KINDS) == 19
    for k in ("node.disable", "node.enable", "node.kick", "node.retire",
              "db.backup", "db.restore", "goal.sync_overflow", "node.heartbeat"):
        assert k in events.EVENT_KINDS


def test_events_response_shape(center):
    store = center  # 造事件走一次真实写路径：disable 即产 node.disable
    store.post("/admin/api/cluster/nodes/w-1/disable", headers=_h())
    body = store.get(EV, headers=_h()).json()["events"]
    row = [e for e in body if e["kind"] == "node.disable"][0]
    assert set(row) == {"ts", "node_id", "goal_id", "kind", "text"}
    assert row["node_id"] == "w-1"
    import re
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row["ts"])
    assert "禁用" in row["text"]          # 后端拼装中文展示，前端零加工


def test_events_kind_filter(center):
    store = center
    store.post("/admin/api/cluster/nodes/w-1/disable", headers=_h())
    store.post("/admin/api/cluster/nodes/w-1/enable", headers=_h())
    kinds = [e["kind"] for e in store.get(EV + "?kind=node.enable", headers=_h()).json()["events"]]
    assert kinds and set(kinds) == {"node.enable"}


def test_events_unknown_kind_400(center):
    assert center.get(EV + "?kind=not.a.kind", headers=_h()).status_code == 400


def test_events_node_filter_and_limit(center):
    import modelctl.core.webui.admin_cluster as ac
    st = ac.get_registry().store
    for i in range(5):
        st.append_event("node.heartbeat", node_id="w-x", now=100.0 + i)
    out = center.get(EV + "?node_id=w-x&limit=3", headers=_h()).json()["events"]
    assert len(out) == 3 and all(e["node_id"] == "w-x" for e in out)


def test_unknown_worker_kind_still_stored_and_renderable(center):
    # worker event 帧 kind 是自由串（M0 契约）：词表外 kind 必须照常入库、照常展示
    import modelctl.core.webui.admin_cluster as ac
    ac.get_registry().store.append_event("vendor.custom", node_id="w-1", payload={"a": 1})
    rows = center.get(EV, headers=_h()).json()["events"]
    row = [e for e in rows if e["kind"] == "vendor.custom"][0]
    assert "vendor.custom" in row["text"]


def test_event_text_never_raises_on_garbage():
    for row in ({}, {"kind": None}, {"kind": "goal.update", "payload": "不是dict"},
                {"kind": "node.join", "payload": {}}):
        assert isinstance(events.event_text(row), str)


def test_create_goal_rejects_disabled_node(center):
    import modelctl.core.webui.admin_cluster as ac
    ac.get_registry().store.set_node_disabled("w-1", True, status="disabled")
    r = center.post(GOALS, json={"profile": "qwen", "node_ids": ["w-1"], "create": True},
                    headers=_h())
    assert r.status_code == 200
    rep = r.json()["report"]
    assert "禁用" in rep and r.json()["created"] == 0
