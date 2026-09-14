"""admin_router 补测：子路由容错注册 + 任务 SSE 流的帧序与泄漏守卫。

`_sse_task_stream` 是前端 TaskButton 的唯一进度来源（51.2% → 目标全绿）：
done 后立即结束、10s heartbeat、客户端断开必须 unsubscribe（队列泄漏 = 长跑服务
内存泄漏）。直接驱动 async generator，比走 HTTP 稳且能覆盖超时分支。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

pytest.importorskip("fastapi")

from fastapi import APIRouter, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402
from modelctl.core.webui import admin_router  # noqa: E402
from modelctl.core.webui.admin_tasks import TaskManager  # noqa: E402

KEY = "test_key_admin_router"


@pytest.fixture()
def admin_client(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    logger.remove()
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c


def _h():
    return {"Authorization": f"Bearer {KEY}"}


def _frames(payload: str) -> list[tuple[str, dict]]:
    out = []
    for block in payload.split("\n\n"):
        if not block.strip():
            continue
        etype, data = "", "{}"
        for ln in block.splitlines():
            if ln.startswith("event: "):
                etype = ln[len("event: "):]
            elif ln.startswith("data: "):
                data = ln[len("data: "):]
        out.append((etype, json.loads(data)))
    return out


class TestIncludeSubrouter:
    def test_missing_module_silently_skipped(self):
        admin_router._include_subrouter(APIRouter(), "modelctl.core.webui.definitely_not_here", "")

    def test_module_without_factory_skipped(self, monkeypatch):
        mod = types.ModuleType("fake_sub_no_factory")
        monkeypatch.setitem(sys.modules, "fake_sub_no_factory", mod)
        admin_router._include_subrouter(APIRouter(), "fake_sub_no_factory", "")

    def test_include_exception_swallowed(self, monkeypatch):
        mod = types.ModuleType("fake_sub_boom")
        mod._router = lambda: (_ for _ in ()).throw(RuntimeError("路由注册炸了"))
        monkeypatch.setitem(sys.modules, "fake_sub_boom", mod)
        admin_router._include_subrouter(APIRouter(), "fake_sub_boom", "/x")  # 不抛即通过

    def test_prefix_applied(self, monkeypatch):
        mod = types.ModuleType("fake_sub_ok")
        inner = APIRouter()

        @inner.get("/ping")
        async def _ping():
            return {"ok": True}

        mod._router = lambda: inner
        monkeypatch.setitem(sys.modules, "fake_sub_ok", mod)
        main = APIRouter()
        admin_router._include_subrouter(main, "fake_sub_ok", "/pre")
        # fastapi>=0.141 的 include_router 是惰性 _IncludedRouter（router.routes 上
        # 不 materialize path），挂到 app 后按真实路由行为断言比翻内部结构更稳。
        app = FastAPI()
        app.include_router(main, prefix="/admin/api")
        with TestClient(app) as c:
            r = c.get("/admin/api/pre/ping")
        assert r.status_code == 200 and r.json() == {"ok": True}


class TestSseTaskStream:
    def test_finished_task_flushes_logs_then_done(self):
        task = TaskManager().create_task(kind="k", action="a", target="t")
        task.log_line("第一行")
        task.complete()
        frames = asyncio.run(_drain(admin_router._sse_task_stream(task), max_frames=2))
        assert frames[0] == ("log", {"line": "第一行"})
        assert frames[1][0] == "done" and frames[1][1]["status"] == "success"

    def test_running_task_streams_events_until_done(self):
        tm = TaskManager()
        task = tm.create_task(kind="k", action="a", target="t")
        task.update_status("queued")

        async def drive():
            gen = admin_router._sse_task_stream(task)
            got = [await gen.__anext__()]  # 首帧 step（queued）
            task.update_status("running")
            task.update_detail("编译中")
            got.append(await gen.__anext__())  # step running（event() 同步投递进队列）
            task.complete()
            got.append(await gen.__anext__())  # step（detail）或 done 之前的广播
            # 继续消费直到 done
            for _ in range(5):
                f = await gen.__anext__()
                got.append(f)
                if f.startswith("event: done"):
                    break
            with pytest.raises(StopAsyncIteration):
                await gen.__anext__()  # done 后生成器必须自己结束
            return got

    def test_heartbeat_on_timeout(self, monkeypatch):
        task = TaskManager().create_task(kind="k", action="a", target="t")
        task.update_status("running")

        async def timeout_boom(coro, timeout=None):
            coro.close()
            raise TimeoutError

        monkeypatch.setattr(admin_router.asyncio, "wait_for", timeout_boom)

        async def drive():
            gen = admin_router._sse_task_stream(task)
            first = await gen.__anext__()  # step running
            second = await gen.__anext__()  # wait_for 超时 → heartbeat
            await gen.aclose()
            return first, second

        first, second = asyncio.run(drive())
        assert first.startswith("event: step")
        assert second == "event: heartbeat\ndata: {}\n\n"

    def test_unsubscribes_on_client_disconnect(self):
        task = TaskManager().create_task(kind="k", action="a", target="t")
        task.update_status("running")

        async def drive():
            gen = admin_router._sse_task_stream(task)
            await gen.__anext__()
            assert len(task._subscribers) == 1
            await gen.aclose()  # 模拟客户端断开（GeneratorExit 路径）

        asyncio.run(drive())
        assert task._subscribers == []  # 泄漏守卫：断开后订阅者必须清零


async def _drain(gen, max_frames: int = 10) -> list[tuple[str, dict]]:
    raw = ""
    for _ in range(max_frames):
        try:
            raw += await gen.__anext__()
        except StopAsyncIteration:
            break
    return _frames(raw)


class TestTaskEndpoints:
    def test_list_tasks_newest_first(self, admin_client):
        tm = admin_client.app.state.task_manager
        t1 = tm.create_task(kind="k", action="a", target="t1")
        t1.update_status("running")  # started_at 填充 → 应排最前
        t2 = tm.create_task(kind="k", action="a", target="t2")
        r = admin_client.get("/admin/api/tasks", headers=_h())
        assert r.status_code == 200
        ids = [t["id"] for t in r.json()["tasks"]]
        assert ids.index(t1.id) < ids.index(t2.id)

    def test_get_task_detail_and_404(self, admin_client):
        tm = admin_client.app.state.task_manager
        t = tm.create_task(kind="k", action="a", target="t")
        t.log_line("hello")
        body = admin_client.get(f"/admin/api/tasks/{t.id}", headers=_h()).json()
        assert body["logs"] == ["hello"]
        r = admin_client.get("/admin/api/tasks/task-nope", headers=_h())
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"

    def test_stream_404_and_headers(self, admin_client):
        r = admin_client.get("/admin/api/tasks/task-nope/stream", headers=_h())
        assert r.status_code == 404
        tm = admin_client.app.state.task_manager
        t = tm.create_task(kind="k", action="a", target="t")
        t.complete()  # 已结束任务：流会立即出帧并结束，TestClient 可直接读 body
        r = admin_client.get(f"/admin/api/tasks/{t.id}/stream", headers=_h())
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["x-accel-buffering"] == "no"
        frames = _frames(r.text)
        assert frames and frames[-1][0] == "done"

    def test_stream_auth_via_query(self, admin_client):
        tm = admin_client.app.state.task_manager
        t = tm.create_task(kind="k", action="a", target="t")
        t.complete()
        r = admin_client.get(f"/admin/api/tasks/{t.id}/stream?key={KEY}")
        assert r.status_code == 200
        assert admin_client.get(f"/admin/api/tasks/{t.id}/stream").status_code == 401
        assert admin_client.get(f"/admin/api/tasks/{t.id}/stream?key=wrong").status_code == 401
