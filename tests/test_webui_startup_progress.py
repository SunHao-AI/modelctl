"""GET /models/{name}/startup 快照端点 + start 任务 stage 事件广播。"""
from __future__ import annotations

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


def test_do_start_bridges_stage_events_to_task():
    from modelctl.core.webui.admin_models import _do_start
    from modelctl.core.webui.admin_tasks import Task
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import Profile

    caps = Capabilities(gpu_count=1, compute_capability="8.9", binaries={})
    prof = Profile(name="q", engine="vllm", port=8000, engine_config={"model": "/m/x"})
    task = Task(id="t1", kind="model_start", action="start", target="q")
    seen = []
    task.event = lambda et, data: seen.append((et, data))

    def fake_start(profile, caps, timeout, on_progress=None):
        from modelctl.core.startup_progress import StageEvent
        from modelctl.core.all_service import ComponentResult
        on_progress(StageEvent("prepare_env", "running", "拉取镜像（3/9 层）", pct=0.4, eta_s=120))
        return ComponentResult("model:q", "ok", "http://127.0.0.1:8000")

    import asyncio
    with mock.patch("modelctl.core.all_service.start_profile", side_effect=fake_start):
        asyncio.run(_do_start(prof, caps, 600, task, None))
    stages = [d for et, d in seen if et == "stage"]
    assert stages and stages[-1]["stage"] == "prepare_env" and stages[-1]["pct"] == 0.4
    assert task.status == "success"
