"""admin_services 长路径补测：服务启停 + 一键启停的端点契约与 worker 编排。

覆盖率热区（45.8%）里未触达的全是"改坏即故障"的编排：409 互斥、worker 的
函数映射与 error 分级（0/1/≥2 三档）、锁移交。打桩口径与 test_webui_startup_progress
一致——端点/worker 内**延迟 import**，patch 源模块 `modelctl.core.all_service.*` 即生效。
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402
from modelctl.core.webui import admin_services  # noqa: E402

KEY = "test_key_services_ops"


@pytest.fixture()
def admin_client(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    logger.remove()
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c


def _h():
    return {"Authorization": f"Bearer {KEY}"}


class CR:
    def __init__(self, component, status, detail=""):
        self.component, self.status, self.detail = component, status, detail


def _stub_all_service(monkeypatch, **fns):
    for name, fn in fns.items():
        monkeypatch.setattr(f"modelctl.core.all_service.{name}", fn)


def _lock_now(tm, target, action):
    asyncio.run(tm.acquire(target, action))


def _capture_spawn(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "modelctl.core.webui.admin_tasks.TaskManager.spawn",
        lambda tm, target, action, coro_factory: calls.append((target, action)),
    )
    return calls


class TestCrState:
    def test_three_states(self):
        assert admin_services._cr_state(CR("x", "error", "爆炸")) == "error"
        assert admin_services._cr_state(CR("x", "ok", "运行中 (pid 1)")) == "running"
        assert admin_services._cr_state(CR("x", "ok", "已停止")) == "stopped"
        assert admin_services._cr_state(CR("x", "ok", "别的描述")) == "error"  # 兜底


class TestServicesOverview:
    def test_overview_stitches_cards_and_family(self, admin_client, monkeypatch):
        from modelctl.core.profile import Profile

        _stub_all_service(
            monkeypatch,
            status_stats=lambda *a, **k: CR("stats", "ok", "运行中"),
            status_gateway=lambda *a, **k: CR("gateway", "error", "探测失败"),
        )
        monkeypatch.setattr(
            "modelctl.core.profile.list_profiles",
            lambda d=None: [Profile(name="m-vllm", engine="vllm", port=8000, group="g1"),
                            Profile(name="m-ollama", engine="ollama", port=11434, group="g1")],
        )
        monkeypatch.setattr("modelctl.core.process.is_running", lambda n: n == "m-vllm")
        monkeypatch.setenv("GATEWAY_DEFAULT_MODEL", "m-vllm")
        r = admin_client.get("/admin/api/services", headers=_h())
        assert r.status_code == 200
        body = r.json()
        assert body["stats"]["state"] == "running"
        assert body["gateway"]["state"] == "error"
        assert body["default_model"] == "m-vllm"
        members = body["family_routing"]["g1"]
        assert [m["name"] for m in members] == sorted(
            [m["name"] for m in members], key=lambda n: 0 if n == "m-vllm" else 1)  # 引擎优先级排序
        assert {m["name"]: m["running"] for m in members} == {"m-vllm": True, "m-ollama": False}

    def test_family_routing_is_running_exception_degrades(self, monkeypatch):
        from modelctl.core.profile import Profile

        def boom(name):
            raise OSError("ps 失败")

        monkeypatch.setattr("modelctl.core.process.is_running", boom)
        groups = admin_services._family_routing([Profile(name="a", engine="vllm", port=1)])
        assert groups["(其它)"][0]["running"] is False

    def test_requires_auth(self, admin_client):
        assert admin_client.get("/admin/api/services").status_code == 401


class TestServiceAction:
    def test_unknown_service_404_and_action_422(self, admin_client):
        assert admin_client.post("/admin/api/services/nope/start", headers=_h()).status_code == 404
        assert admin_client.post("/admin/api/services/stats/launch", headers=_h()).status_code == 422

    def test_stop_is_synchronous(self, admin_client, monkeypatch):
        _stub_all_service(monkeypatch, stop_stats=lambda: CR("stats", "ok", "已停止"))
        r = admin_client.post("/admin/api/services/stats/stop", headers=_h())
        assert r.status_code == 200
        assert r.json() == {"ok": True, "detail": "已停止"}
        _stub_all_service(monkeypatch, stop_gateway=lambda: CR("gateway", "error", "杀不掉"))
        r = admin_client.post("/admin/api/services/gateway/stop", headers=_h())
        assert r.json() == {"ok": False, "detail": "杀不掉"}

    def test_start_202_delivers_worker(self, admin_client, monkeypatch):
        calls = _capture_spawn(monkeypatch)
        r = admin_client.post("/admin/api/services/gateway/start", headers=_h())
        assert r.status_code == 202
        assert r.json()["stream_url"].startswith("/admin/api/tasks/")
        assert calls == [("gateway", "start")]

    def test_conflict_409_when_locked(self, admin_client):
        _lock_now(admin_client.app.state.task_manager, "stats", "restart")
        r = admin_client.post("/admin/api/services/stats/restart", headers=_h())
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "task_conflict"

    def test_spawn_failure_releases_lock(self, admin_client, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("loop down")

        monkeypatch.setattr("modelctl.core.webui.admin_tasks.TaskManager.spawn", boom)
        with pytest.raises(RuntimeError):
            admin_client.post("/admin/api/services/stats/start", headers=_h())
        tm = admin_client.app.state.task_manager
        assert not tm.get_lock("stats").locked()  # 锁必须被归还


class TestDoServiceAction:
    @pytest.mark.parametrize("svc,action,fn", [
        ("stats", "start", "start_stats"),
        ("stats", "restart", "restart_stats"),
        ("gateway", "start", "start_gateway"),
        ("gateway", "restart", "restart_gateway"),
    ])
    def test_dispatch_table(self, monkeypatch, svc, action, fn):
        called = []
        _stub_all_service(monkeypatch, **{fn: lambda: (called.append(1), CR(svc, "ok", "好"))[1]})
        task = _mk_task(svc, action)
        asyncio.run(admin_services._do_service_action(svc, action, task))
        assert called == [1]
        assert task.status == "success" and task.detail == "好"

    def test_error_result_marks_task_error(self, monkeypatch):
        _stub_all_service(monkeypatch, start_stats=lambda: CR("stats", "error", "健康检查超时"))
        task = _mk_task("stats", "start")
        asyncio.run(admin_services._do_service_action("stats", "start", task))
        assert task.status == "error" and task.exit_code == 1 and task.detail == "健康检查超时"

    def test_exception_marks_task_error(self, monkeypatch):
        def boom():
            raise OSError("磁盘满了")

        _stub_all_service(monkeypatch, restart_gateway=boom)
        task = _mk_task("gateway", "restart")
        asyncio.run(admin_services._do_service_action("gateway", "restart", task))
        assert task.status == "error" and "磁盘满了" in task.detail


def _mk_task(target, action, kind="service"):
    from modelctl.core.webui.admin_tasks import TaskManager

    return TaskManager().create_task(kind=kind, action=action, target=target)


class TestAllEndpoints:
    def test_all_start_202_and_conflict(self, admin_client, monkeypatch):
        calls = _capture_spawn(monkeypatch)
        r = admin_client.post("/admin/api/all/start?model=m1&gpus=0", headers=_h())
        assert r.status_code == 202 and calls == [("all", "start")]
        _lock_now(admin_client.app.state.task_manager, "all", "start")
        r = admin_client.post("/admin/api/all/start", headers=_h())
        assert r.status_code == 409

    def test_all_restart_202_and_conflict(self, admin_client, monkeypatch):
        calls = _capture_spawn(monkeypatch)
        r = admin_client.post("/admin/api/all/restart", headers=_h())
        assert r.status_code == 202 and calls == [("all", "restart")]
        _lock_now(admin_client.app.state.task_manager, "all", "restart")
        assert admin_client.post("/admin/api/all/restart", headers=_h()).status_code == 409

    def test_all_stop_partitions_results(self, admin_client, monkeypatch):
        _stub_all_service(monkeypatch, stop_all=lambda md: [
            CR("stats", "ok", "已停止"), CR("gateway", "skipped", "未启动"),
            CR("m1", "error", "杀不掉")])
        r = admin_client.post("/admin/api/all/stop", headers=_h())
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["stopped"] == ["stats", "gateway"]
        assert body["errors"] == [{"component": "m1", "detail": "杀不掉"}]

    def test_all_status_lists_components(self, admin_client, monkeypatch):
        _stub_all_service(monkeypatch, status_all=lambda md: [CR("gateway", "ok", "运行中")])
        r = admin_client.get("/admin/api/all/status", headers=_h())
        assert r.json() == {"components": [{"component": "gateway", "status": "ok", "detail": "运行中"}]}


class TestDoAll:
    def _task(self):
        return _mk_task("all", "start", kind="all_start")

    def test_zero_errors_completes(self):
        def fake(md, model, timeout, gpus=None):
            return [CR("a", "ok"), CR("b", "skipped")]

        fake.__name__ = "start_all"
        task = self._task()
        asyncio.run(admin_services._do_all(fake, {"model": None, "timeout": None, "gpus": None}, task))
        assert task.status == "success" and task.detail == "2 个组件已处理"

    def test_single_error_warning_wording(self):
        def fake(md, model, timeout, gpus=None):
            return [CR("stats", "error", "boom"), CR("gw", "ok")]

        fake.__name__ = "start_all"
        task = self._task()
        asyncio.run(admin_services._do_all(fake, {"model": None, "timeout": 5, "gpus": None}, task))
        assert task.status == "error"
        assert task.detail == "1 个组件失败（共 2）：stats: boom"

    def test_multi_error_message(self):
        def fake(md, model, timeout, gpus=None):
            return [CR("a", "error", "x"), CR("b", "error", "y"), CR("c", "ok")]

        fake.__name__ = "restart_all"
        task = self._task()
        asyncio.run(admin_services._do_all(fake, {"model": None, "timeout": None, "gpus": None}, task))
        assert task.status == "error" and task.detail.startswith("2 个组件失败（共 3）：")

    def test_gpus_resolved_and_forwarded(self, monkeypatch):
        seen = {}

        def fake(md, model, timeout, gpus=None):
            seen["gpus"] = gpus
            return []

        fake.__name__ = "start_all"
        task = self._task()
        asyncio.run(admin_services._do_all(fake, {"model": "m", "timeout": "30", "gpus": "0,1"}, task))
        assert seen["gpus"] == [0, 1]  # resolve_gpu_list 真解析（纯函数不打桩）
        assert task.status == "success"

    def test_exception_marks_error(self):
        def fake(md, model, timeout, gpus=None):
            raise RuntimeError("编排炸了")

        fake.__name__ = "start_all"
        task = self._task()
        asyncio.run(admin_services._do_all(fake, {"model": None, "timeout": None, "gpus": None}, task))
        assert task.status == "error" and task.detail == "编排炸了"
