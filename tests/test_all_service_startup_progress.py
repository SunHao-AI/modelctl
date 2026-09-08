"""start_profile 5 段阶段序列：正常 / preflight 失败 / 超时 / docker 失败 / 超时自适应。"""
from __future__ import annotations

from unittest import mock

import pytest

from modelctl.core import all_service
from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import Profile
from modelctl.engines.base import RequirementError

CAPS = Capabilities(gpu_count=1, compute_capability="8.9", binaries={})


def _profile(engine="vllm", docker=False):
    ec = {"model": "/m/x"}
    if docker:
        ec["docker_image"] = "img:tag"
    return Profile(name="q", engine=engine, port=8000, engine_config=ec)


class _FakeAdapter:
    is_docker = False
    log_tee = None

    def __init__(self, profile, caps):
        self.profile, self.caps = profile, caps
        self.warnings: list[str] = []
        self.spawned_proc = None
        self._progress_cb = None
        self.dead = False

    def check_requirements(self):
        return None

    def pre_start(self):
        return None

    def build_command(self):
        return ["echo", "hi"], {}

    def selected_gpus(self):
        return None

    def wait_ready(self, timeout):
        return True

    def post_start(self):
        return None

    def is_docker_runtime(self):
        return self.is_docker

    def log_tee_cmd(self):
        return self.log_tee

    def set_progress_sink(self, cb):
        self._progress_cb = cb

    def backend_dead(self):
        return self.dead

    def upstream_api_key(self):
        return None

    def metrics_mapping(self):
        return None


def _patch_common(tmp_path, adapter):
    """统一打桩：隔离 launch log / cache / 进程 / 快照，返回 patch 列表上下文。"""
    import contextlib

    @contextlib.contextmanager
    def ctx():
        with mock.patch.object(all_service, "get_adapter", return_value=lambda p, c: adapter), \
             mock.patch.object(all_service, "is_running_any", return_value=False), \
             mock.patch.object(all_service, "port_in_use", return_value=False), \
             mock.patch.object(all_service, "start_detached", return_value=(1234, None)), \
             mock.patch("modelctl.core.startup_progress.StartupTracker._write_snapshot", return_value=None), \
             mock.patch("modelctl.core.startup_progress.StartupTiming.__init__",
                        return_value=None), \
             mock.patch("modelctl.core.startup_progress.StartupTiming.eta", return_value=None), \
             mock.patch("modelctl.core.startup_progress.StartupTiming.record", return_value=None), \
             mock.patch("modelctl.core.startup_progress.LoadingWatcher.start", return_value=None), \
             mock.patch("modelctl.core.startup_progress.LoadingWatcher.stop", return_value=None), \
             mock.patch.object(all_service, "launch_log", return_value=None), \
             mock.patch.object(all_service, "kill_log_tee"), \
             mock.patch.object(all_service, "spawn_log_tee", return_value=1) as tee:
            yield tee

    return ctx()


def test_happy_path_stage_sequence(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELCTL_NO_GPU_LOCK", "1")
    events = []
    with _patch_common(tmp_path, _FakeAdapter(_profile(), CAPS)):
        r = all_service.start_profile(_profile(), CAPS, 5, on_progress=events.append)
    assert r.status == "ok"
    assert [(e.stage, e.status) for e in events] == [
        ("preflight", "running"), ("preflight", "done"),
        ("prepare_env", "running"), ("prepare_env", "done"),
        ("launch", "running"), ("launch", "done"),
        ("loading", "running"), ("loading", "done"),
        ("health", "done"),
    ]


def test_requirement_error_stops_at_preflight():
    class A(_FakeAdapter):
        def check_requirements(self):
            raise RequirementError("docker 命令不在 PATH")

    events = []
    with _patch_common(None, A(_profile(), CAPS)):
        with pytest.raises(RequirementError):
            all_service.start_profile(_profile(), CAPS, 5, on_progress=events.append)
    assert events[-1].stage == "preflight" and events[-1].status == "error"
    assert "docker" in events[-1].error


def test_timeout_marks_loading_error_with_detail():
    class A(_FakeAdapter):
        def wait_ready(self, timeout):
            return False

    events = []
    with _patch_common(None, A(_profile(), CAPS)):
        r = all_service.start_profile(_profile(), CAPS, 5, on_progress=events.append)
    assert r.status == "error" and r.detail == "健康检查超时"
    assert events[-1].stage == "loading" and events[-1].status == "error"
    assert events[-1].error == "健康检查超时"


def test_docker_path_spawns_log_tee():
    class A(_FakeAdapter):
        is_docker = True
        log_tee = ["docker", "logs", "-f", "--tail", "all", "q-vllm"]

    with _patch_common(None, A(_profile(docker=True), CAPS)) as tee:
        r = all_service.start_profile(_profile(docker=True), CAPS, 5)
    assert r.status == "ok"
    tee.assert_called_once_with("q", ["docker", "logs", "-f", "--tail", "all", "q-vllm"])


def test_no_progress_callback_is_backward_compatible():
    with _patch_common(None, _FakeAdapter(_profile(), CAPS)):
        assert all_service.start_profile(_profile(), CAPS, 5).status == "ok"


def test_default_start_timeout_by_runtime(monkeypatch):
    monkeypatch.delenv("MODELCTL_START_TIMEOUT", raising=False)
    d = _profile(docker=True)
    v = _profile(docker=False)
    # docker 判定经 adapter.is_docker_runtime；用 fake 覆盖两条
    with mock.patch.object(all_service, "get_adapter",
                           return_value=lambda p, c: type("X", (), {"is_docker_runtime": lambda s: True})()):
        assert all_service.default_start_timeout(d, CAPS) == 1800.0
    with mock.patch.object(all_service, "get_adapter",
                           return_value=lambda p, c: type("X", (), {"is_docker_runtime": lambda s: False})()):
        assert all_service.default_start_timeout(v, CAPS) == 600.0
    monkeypatch.setenv("MODELCTL_START_TIMEOUT", "90")
    assert all_service.default_start_timeout(d, CAPS) == 90.0
