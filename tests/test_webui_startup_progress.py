"""GET /models/{name}/startup 快照端点 + start 任务 stage 事件广播。"""
from __future__ import annotations

import asyncio
import json
import pathlib
from unittest import mock

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_startup"


@pytest.fixture()
def admin_client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    logger.remove()
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c


def _get(client, path):
    return client.get(path, headers={"Authorization": f"Bearer {KEY}"})


def test_startup_snapshot_404_when_absent(admin_client, monkeypatch):
    monkeypatch.setattr("modelctl.core.paths.cache_dir",
                        lambda: pathlib.Path("/nonexistent-xyz"))
    r = _get(admin_client, "/admin/api/models/nope/startup")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_startup_snapshot_camel_case(admin_client, monkeypatch, tmp_path):
    (tmp_path / "q.startup.json").write_text(json.dumps({
        "profile": "q", "engine": "vllm", "runtime": "docker",
        "updated_at": "2026-09-08 03:18:41",
        "stages": [{"stage": "prepare_env", "status": "running", "label": "拉取镜像",
                    "pct": 0.45, "eta_s": 360, "error": None,
                    "started_at": "2026-09-08 03:00:00", "finished_at": None}],
    }), encoding="utf-8")
    monkeypatch.setattr("modelctl.core.paths.cache_dir", lambda: tmp_path)
    r = _get(admin_client, "/admin/api/models/q/startup")
    assert r.status_code == 200
    body = r.json()
    assert body["updatedAt"] == "2026-09-08 03:18:41"
    assert body["stages"][0]["etaSeconds"] == 360
    assert body["stages"][0]["stage"] == "prepare_env"


def test_startup_snapshot_corrupt_json_404(admin_client, monkeypatch, tmp_path):
    (tmp_path / "q.startup.json").write_text("{ broken", encoding="utf-8")
    monkeypatch.setattr("modelctl.core.paths.cache_dir", lambda: tmp_path)
    assert _get(admin_client, "/admin/api/models/q/startup").status_code == 404


def _parse_sse_frames(payloads: list[str]) -> list[tuple[str, dict]]:
    """SSE 文本帧（`event: x\\ndata: {...}\\n\\n`）→ [(事件名, data dict)]。"""
    frames = []
    for p in payloads:
        event_type = p.split("\n", 1)[0].split(": ", 1)[1]
        frames.append((event_type, json.loads(p.split("data: ", 1)[1].strip())))
    return frames


def test_do_start_stage_events_reach_subscriber_queue_from_worker_thread():
    """Critical-1：_on_stage 跑在 to_thread 工作线程（无当前事件循环），stage 帧必须
    经 call_soon_threadsafe 投递进真实 Task 订阅队列——不得 monkeypatch task.event。"""
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import Profile
    from modelctl.core.webui.admin_models import _do_start
    from modelctl.core.webui.admin_tasks import TaskManager

    caps = Capabilities(gpu_count=1, compute_capability="8.9", binaries={})
    prof = Profile(name="q", engine="vllm", port=8000, engine_config={"model": "/m/x"})

    async def main():
        task = TaskManager().create_task("model_start", "start", "q")
        q = task.subscribe()

        def fake_start(profile, caps, timeout, on_progress=None):
            from modelctl.core.all_service import ComponentResult
            from modelctl.core.startup_progress import StageEvent
            # 当前线程即 asyncio.to_thread 工作线程（生产链路）：直接 task.event 会丢帧
            on_progress(StageEvent("prepare_env", "running", "拉取镜像（3/9 层）", pct=0.4, eta_s=120))
            return ComponentResult("model:q", "ok", "http://127.0.0.1:8000")

        with mock.patch("modelctl.core.all_service.start_profile", side_effect=fake_start):
            await _do_start(prof, caps, 600, task, None)
        await asyncio.sleep(0.05)  # 让 call_soon_threadsafe 排队的回调执行
        return task, _parse_sse_frames([q.get_nowait() for _ in range(q.qsize())])

    task, frames = asyncio.run(main())
    stages = [d for et, d in frames if et == "stage"]
    assert stages, f"订阅队列未收到 stage 帧（跨线程派发丢失）；实际帧={([e for e, _ in frames],)}"
    assert stages[-1]["stage"] == "prepare_env" and stages[-1]["pct"] == 0.4
    assert stages[-1].get("etaSeconds") == 120
    assert task.status == "success"
