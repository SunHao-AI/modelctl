#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_admin_tasks.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:24
# @Desc   : 任务端点与 update_detail 广播测试
# ===============================================================================

"""admin_tasks / admin_router 任务端点测试。"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_tasks"


@pytest.fixture()
def admin_client(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    logger.remove()
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c


def _mk_task(client: TestClient) -> str:
    """直接操作 app.state.task_manager 造一条 running 任务（不依赖 envs 端点副作用）。"""
    tm = client.app.state.task_manager
    task = tm.create_task(kind="test", action="start", target="t1")
    task.update_status("running")
    task.log_line("hello")
    return task.id


def test_get_task_ok(admin_client):
    tid = _mk_task(admin_client)
    r = admin_client.get(f"/admin/api/tasks/{tid}", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == tid
    assert body["status"] == "running"
    assert body["logs"] == ["hello"]


def test_get_task_not_found(admin_client):
    r = admin_client.get("/admin/api/tasks/task-nope", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


@pytest.mark.filterwarnings(
    r"ignore:There is no current event loop:DeprecationWarning:modelctl\.core\.webui\.admin_tasks"
)
def test_update_detail_broadcasts_step():
    """update_detail 必须让订阅者收到 step 事件（detail 可见性）。"""
    from modelctl.core.webui.admin_tasks import Task

    task = Task(id="task-x", kind="k", action="a", target="t")
    q = task.subscribe()
    task.update_status("running")
    task.update_detail("安装中 30%")
    # event() 经 call_soon_threadsafe 投递，无 running loop 时同步 put_nowait
    payloads = []
    while not q.empty():
        payloads.append(q.get_nowait())
    assert any("event: step" in p and "安装中 30%" in p for p in payloads), payloads
