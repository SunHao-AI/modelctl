"""admin_config 长路径补测：TensorRT-LLM 编译端点/worker + nginx 端口兜底。

覆盖率 36.5% 的缺口集中在 trtllm 编译链（build/status/worker/同步执行体）与
nginx_snippet 的 GATEWAY_PORT 兜底分支。编译命令**绝不真跑**（trtllm-build 需 GPU
且 30min+），沿"只测投递契约 + 打桩 subprocess"口径；engine_dir 的 stat 走 tmp_path
真实磁盘。
"""

from __future__ import annotations

import asyncio
import types

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402
from modelctl.core.webui import admin_config  # noqa: E402

KEY = "test_key_config_build"


@pytest.fixture()
def admin_client(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    logger.remove()
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c


def _h():
    return {"Authorization": f"Bearer {KEY}"}


def _profile(name="t1", engine="tensorrt_llm", engine_dir=None):
    from modelctl.core.profile import Profile

    cfg = {"model": "/m/x"}
    if engine_dir:
        cfg["engine_dir"] = engine_dir
    return Profile(name=name, engine=engine, port=8000, engine_config=cfg)


def _patch_profiles(monkeypatch, *profiles):
    monkeypatch.setattr("modelctl.core.profile.list_profiles", lambda d=None: list(profiles))


def _capture_spawn(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "modelctl.core.webui.admin_tasks.TaskManager.spawn",
        lambda tm, target, action, coro_factory: calls.append((target, action)),
    )
    return calls


class TestBuildEndpoint:
    def test_404_unknown_profile(self, admin_client, monkeypatch):
        _patch_profiles(monkeypatch)
        r = admin_client.post("/admin/api/trtllm/nope/build", headers=_h())
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"

    def test_412_non_trtllm_engine(self, admin_client, monkeypatch):
        _patch_profiles(monkeypatch, _profile(engine="vllm"))
        r = admin_client.post("/admin/api/trtllm/t1/build", headers=_h())
        assert r.status_code == 412 and r.json()["error"]["code"] == "unsupported_engine"

    def test_202_delivers_build_worker(self, admin_client, monkeypatch):
        _patch_profiles(monkeypatch, _profile())
        calls = _capture_spawn(monkeypatch)
        r = admin_client.post("/admin/api/trtllm/t1/build", headers=_h())
        assert r.status_code == 202
        assert r.json()["stream_url"].startswith("/admin/api/tasks/")
        assert calls == [("t1", "build")]

    def test_409_conflict(self, admin_client, monkeypatch):
        _patch_profiles(monkeypatch, _profile())
        asyncio.run(admin_client.app.state.task_manager.acquire("t1", "build"))
        r = admin_client.post("/admin/api/trtllm/t1/build", headers=_h())
        assert r.status_code == 409

    def test_requires_auth(self, admin_client):
        assert admin_client.post("/admin/api/trtllm/t1/build").status_code == 401


class TestStatusEndpoint:
    def test_404_without_engine_dir(self, admin_client, monkeypatch):
        _patch_profiles(monkeypatch, _profile())  # 无 engine_dir
        r = admin_client.get("/admin/api/trtllm/t1/status", headers=_h())
        assert r.status_code == 404

    def test_reports_real_dir_stat(self, admin_client, monkeypatch, tmp_path):
        (tmp_path / "rank0.engine").write_bytes(b"\x00")
        (tmp_path / "config.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(admin_config, "_trtllm_engine_dir", lambda name: str(tmp_path))
        r = admin_client.get("/admin/api/trtllm/t1/status", headers=_h())
        assert r.status_code == 200
        body = r.json()
        assert body["built"] is True and body["files"] == 2
        assert body["engine_dir"] == str(tmp_path)

    def test_empty_dir_not_built(self, admin_client, monkeypatch, tmp_path):
        empty = tmp_path / "eng"
        empty.mkdir()
        monkeypatch.setattr(admin_config, "_trtllm_engine_dir", lambda name: str(empty))
        body = admin_client.get("/admin/api/trtllm/t1/status", headers=_h()).json()
        assert body["built"] is False and body["files"] == 0

    def test_missing_dir_not_built(self, admin_client, monkeypatch, tmp_path):
        ghost = tmp_path / "gone"
        monkeypatch.setattr(admin_config, "_trtllm_engine_dir", lambda name: str(ghost))
        body = admin_client.get("/admin/api/trtllm/t1/status", headers=_h()).json()
        assert body["built"] is False and body["engine_dir"] == str(ghost)


class TestEngineDirLookup:
    def test_hit_and_miss(self, monkeypatch):
        _patch_profiles(monkeypatch, _profile(engine_dir="/data/eng"))
        assert admin_config._trtllm_engine_dir("t1") == "/data/eng"
        assert admin_config._trtllm_engine_dir("other") is None
        _patch_profiles(monkeypatch, _profile())  # 无 engine_dir 键
        assert admin_config._trtllm_engine_dir("t1") is None


class TestBuildWorker:
    def test_success_completes_task(self, monkeypatch):
        monkeypatch.setattr(admin_config, "_run_trtllm_build_sync", lambda name: None)
        task = _mk_task()
        asyncio.run(admin_config._do_trtllm_build("t1", task))
        assert task.status == "success"

    def test_failure_marks_error(self, monkeypatch):
        def boom(name):
            raise RuntimeError("CUDA OOM")

        monkeypatch.setattr(admin_config, "_run_trtllm_build_sync", boom)
        task = _mk_task()
        asyncio.run(admin_config._do_trtllm_build("t1", task))
        assert task.status == "error" and task.exit_code == 1 and task.detail == "CUDA OOM"


def _mk_task():
    from modelctl.core.webui.admin_tasks import TaskManager

    return TaskManager().create_task(kind="trtllm_build", action="build", target="t1")


class TestRunBuildSync:
    def _fake_adapter(self, monkeypatch, cmd=None, env=None):
        seen: dict = {}
        adapter = types.SimpleNamespace(
            ensure_bin=lambda: seen.setdefault("ensured", 0) or seen.__setitem__("ensured", 1),
            build_compile_command=lambda: (cmd or ["trtllm-build", "--x"], env or {"K": "V"}),
        )
        monkeypatch.setattr("modelctl.engines.tensorrt_llm.TensorRtLlmAdapter",
                            lambda p, c: adapter, raising=True)
        monkeypatch.setattr("modelctl.core.capabilities.probe", lambda *a, **k: object())
        return seen

    def test_profile_missing_raises(self, monkeypatch):
        _patch_profiles(monkeypatch)
        with pytest.raises(ValueError, match="不存在"):
            admin_config._run_trtllm_build_sync("nope")

    def test_engine_dir_missing_raises(self, monkeypatch):
        _patch_profiles(monkeypatch, _profile())  # 无 engine_dir
        self._fake_adapter(monkeypatch)
        with pytest.raises(ValueError, match="engine_dir"):
            admin_config._run_trtllm_build_sync("t1")

    def test_idempotent_skip_when_artifacts_exist(self, monkeypatch, tmp_path):
        (tmp_path / "rank0.engine").write_bytes(b"\x00")
        _patch_profiles(monkeypatch, _profile(engine_dir=str(tmp_path)))
        self._fake_adapter(monkeypatch)
        called = []
        monkeypatch.setattr(admin_config.subprocess, "run",
                            lambda *a, **k: called.append(1))
        admin_config._run_trtllm_build_sync("t1")
        assert called == []  # 已有产物：绝不重跑 30min 编译

    def test_runs_command_and_forwards_env(self, monkeypatch, tmp_path):
        _patch_profiles(monkeypatch, _profile(engine_dir=str(tmp_path / "fresh")))
        self._fake_adapter(monkeypatch, cmd=["trtllm-build", "--force"], env={"K": "V"})
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"], seen["env"] = cmd, kw
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(admin_config.subprocess, "run", fake_run)
        admin_config._run_trtllm_build_sync("t1")
        assert seen["cmd"] == ["trtllm-build", "--force"]
        assert seen["env"]["env"] == {"K": "V"}  # adapter 给的 env 原样透传给子进程
        assert seen["env"].get("capture_output") is True

    def test_nonzero_exit_raises_with_stderr_tail(self, monkeypatch, tmp_path):
        _patch_profiles(monkeypatch, _profile(engine_dir=str(tmp_path / "fresh")))
        self._fake_adapter(monkeypatch)
        monkeypatch.setattr(
            admin_config.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(returncode=137, stdout="", stderr="Killed " + "x" * 600))
        with pytest.raises(RuntimeError, match="退出码 137"):
            admin_config._run_trtllm_build_sync("t1")


class TestNginSnippetPortFallback:
    def test_port_fallback_from_env(self, admin_client, monkeypatch):
        seen = {}

        def fake_build(profiles, node, host, port):
            seen["port"] = port
            return "SNIPPET"

        monkeypatch.setattr("modelctl.core.nginx_snippet.build_llm_map", fake_build)
        monkeypatch.setattr("modelctl.core.profile.list_profiles", lambda d=None: [])
        # 兜底只在 import 本身抛异常时触发（`port: int = GATEWAY_PORT` 仅是注解，
        # setattr 成字符串不会抛）→ 删属性制造 ImportError，才走 env 兜底。
        monkeypatch.delattr("modelctl.core.gateway.GATEWAY_PORT")
        monkeypatch.setenv("GATEWAY_PORT", "5099")
        r = admin_client.get("/admin/api/nginx-snippet?node=210&host=10.0.0.1", headers=_h())
        assert r.status_code == 200
        assert r.json() == {"ok": True, "snippet": "SNIPPET"}
        assert seen["port"] == 5099  # int("bad") 抛 → 兜底读 env
