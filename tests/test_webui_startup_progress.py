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


def _patch_profiles(monkeypatch, *names):
    """钉 list_profiles：/startup 端点先判 profile 存在性（F5），测试须显式提供 profile。"""
    from modelctl.core.profile import Profile

    profiles = [Profile(name=n, engine="vllm", port=8000, engine_config={"model": "/m/x"})
                for n in names]
    monkeypatch.setattr("modelctl.core.profile.list_profiles", lambda d=None: list(profiles))


def test_startup_snapshot_404_when_absent(admin_client, monkeypatch):
    _patch_profiles(monkeypatch, "nope")  # profile 存在但快照文件缺失 → 404
    monkeypatch.setattr("modelctl.core.paths.cache_dir",
                        lambda: pathlib.Path("/nonexistent-xyz"))
    r = _get(admin_client, "/admin/api/models/nope/startup")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_startup_snapshot_camel_case(admin_client, monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, "q")
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
    _patch_profiles(monkeypatch, "q")
    (tmp_path / "q.startup.json").write_text("{ broken", encoding="utf-8")
    monkeypatch.setattr("modelctl.core.paths.cache_dir", lambda: tmp_path)
    assert _get(admin_client, "/admin/api/models/q/startup").status_code == 404


def test_startup_404_when_profile_deleted_but_snapshot_exists(admin_client, monkeypatch, tmp_path):
    """F5：profile 存在性优先于文件——快照文件在但 profile 已删 → 404。"""
    _patch_profiles(monkeypatch)  # 无任何 profile
    (tmp_path / "ghost.startup.json").write_text(json.dumps({"profile": "ghost", "stages": []}),
                                                 encoding="utf-8")
    monkeypatch.setattr("modelctl.core.paths.cache_dir", lambda: tmp_path)
    r = _get(admin_client, "/admin/api/models/ghost/startup")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_startup_rejects_path_traversal_name(admin_client, monkeypatch, tmp_path):
    """F5：name 含 .. / 非法字符 → 404，且绝不读取 cache 目录之外的路径。"""
    _patch_profiles(monkeypatch, "q")
    opened: list = []
    real_read = pathlib.Path.read_text

    def spy(self, *a, **kw):
        opened.append(str(self))
        return real_read(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "read_text", spy)
    for bad in ("..%2F..%2Fetc%2Fpasswd", "..%2Fq", "q..q", "a%2Fb"):
        r = _get(admin_client, f"/admin/api/models/{bad}/startup")
        assert r.status_code == 404, bad
    assert not any("passwd" in p or "startup.json" in p for p in opened), opened


# ---------------------------------------------------------------------------
# F1：/all/start、/all/restart 的 timeout 自适应透传
# ---------------------------------------------------------------------------

def _do_all_args(timeout_query):
    """直接驱动 _do_all：返回传给 start_all 的位置参数 (models_dir, model, timeout)。"""
    from modelctl.core.webui.admin_services import _do_all

    captured = {}

    def fake_start_all(models_dir, model, timeout):
        captured["args"] = (models_dir, model, timeout)
        return []

    class _Task:
        def update_status(self, s):
            return None

        def update_detail(self, d):
            return None

        def complete(self):
            return None

        def error(self, **kw):
            raise AssertionError(f"task.error 不应被调用：{kw}")

    thru = {"model": None, "timeout": timeout_query, "gpus": None}
    asyncio.run(_do_all(fake_start_all, thru, _Task()))
    return captured["args"]


def test_do_all_passes_none_timeout_for_autoscale(monkeypatch):
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    assert _do_all_args(None)[2] is None  # 未显式指定 → None 透传给 start_all 做自适应


def test_do_all_passes_explicit_timeout():
    assert _do_all_args(120)[2] == 120.0  # 显式指定 → 原样 float 透传


def test_all_start_endpoint_timeout_default_none(admin_client, monkeypatch):
    """POST /all/start 不带 timeout → start_all 收到 None；带 timeout=120 → 收到 120.0。

    端点是 202 + 后台任务（_do_all → asyncio.to_thread），须轮询等后台线程消费完。
    """
    import time

    seen: list = []

    def fake_start_all(models_dir, model, timeout):
        seen.append(timeout)
        from modelctl.core.all_service import ComponentResult
        return [ComponentResult("model:q", "ok", "ok")]

    # 端点内函数级 from-import 在调用时取模块属性，patch 源模块即可生效
    monkeypatch.setattr("modelctl.core.all_service.start_all", fake_start_all)

    def wait_for(n, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline and len(seen) < n:
            time.sleep(0.02)
        assert len(seen) >= n, seen

    r = admin_client.post("/admin/api/all/start", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 202
    wait_for(1)
    r2 = admin_client.post("/admin/api/all/start?timeout=120",
                           headers={"Authorization": f"Bearer {KEY}"})
    assert r2.status_code == 202
    wait_for(2)
    assert seen[:2] == [None, 120.0], seen
    # le=7200 对齐单模型端点：超限 422（仅参数校验，不触发后台执行）
    r3 = admin_client.post("/admin/api/all/start?timeout=9999",
                           headers={"Authorization": f"Bearer {KEY}"})
    assert r3.status_code == 422


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
