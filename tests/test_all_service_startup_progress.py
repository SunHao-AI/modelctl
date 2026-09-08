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

    def log_fallback_cmd(self):
        return None

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


# ---------------------------------------------------------------------------
# 最终评审修复波：F3 端口预检 stage 事件 / F4 launch 失败事件 / F8 tee 顺序与兜底
# ---------------------------------------------------------------------------

def test_port_in_use_emits_preflight_running_then_failed():
    """F3：端口占用失败也必须产生 preflight running→failed 事件（tracker 先于预检创建）。"""
    class A(_FakeAdapter):
        def check_requirements(self):
            raise AssertionError("端口占用应在 check_requirements 之前被拦截")

    events = []
    with _patch_common(None, A(_profile(), CAPS)), \
         mock.patch.object(all_service, "port_in_use", return_value=True), \
         mock.patch.object(all_service, "describe_port_listener", return_value="PID 4242"):
        with pytest.raises(RequirementError, match="端口 8000 已被占用"):
            all_service.start_profile(_profile(), CAPS, 5, on_progress=events.append)
    seq = [(e.stage, e.status) for e in events]
    assert seq == [("preflight", "running"), ("preflight", "error")]
    assert "PID 4242" in events[-1].error


def test_launch_failure_marks_launch_error_and_kills_tee():
    """F4：build_command/start_detached 抛异常 → launch running→failed，且 kill_log_tee 兜底回收。"""
    baseline = [0]

    class A(_FakeAdapter):
        def build_command(self):
            # 进入 launch 时入口残留清理已调过 1 次 kill_log_tee，记录基线用于区分兜底回收
            baseline[0] = kill.call_count
            raise RuntimeError("docker image manifest 解析失败")

    events = []
    with _patch_common(None, A(_profile(), CAPS)), \
         mock.patch.object(all_service, "kill_log_tee") as kill:
        with pytest.raises(RuntimeError, match="manifest"):
            all_service.start_profile(_profile(), CAPS, 5, on_progress=events.append)
    seq = [(e.stage, e.status) for e in events]
    assert seq[-2:] == [("launch", "running"), ("launch", "error")]
    assert events[-1].error == "docker image manifest 解析失败"
    assert kill.call_count == baseline[0] + 1  # 失败分支额外回收一次 tee


def test_kill_log_tee_runs_after_log_excerpt_output(tmp_path):
    """F8-1：tee 是写入方，摘录 logger 输出之后才能 kill，否则丢未 flush 尾部行。"""
    class A(_FakeAdapter):
        def wait_ready(self, timeout):
            return False

        def backend_dead(self):
            return True

    log = tmp_path / "launch-q.log"
    log.write_text("Traceback (most recent call last):\nRuntimeError: boom\n", encoding="utf-8")
    order = []
    with _patch_common(None, A(_profile(), CAPS)), \
         mock.patch.object(all_service, "launch_log", return_value=log), \
         mock.patch.object(all_service, "kill_log_tee",
                           side_effect=lambda name: order.append("kill")), \
         mock.patch.object(all_service, "logger") as lg:
        lg.warning.side_effect = lambda *a, **k: order.append("warn")
        r = all_service.start_profile(_profile(), CAPS, 5)
    assert r.status == "error" and r.detail == "引擎进程提前退出"
    assert order[-1] == "kill", f"kill_log_tee 必须在摘录输出之后：{order}"
    assert "warn" in order


def test_died_excerpt_falls_back_to_docker_logs(tmp_path):
    """F8-2：摘录仅容器 ID 行（tee 没挂上）→ docker logs --tail 50 兜底内容做摘录。"""
    class A(_FakeAdapter):
        is_docker = True
        log_tee = ["docker", "logs", "-f", "--tail", "all", "q-vllm"]

        def wait_ready(self, timeout):
            return False

        def backend_dead(self):
            return True

        def log_fallback_cmd(self):
            return ["docker", "logs", "--tail", "50", "q-vllm"]

    log = tmp_path / "launch-q.log"
    log.write_text("a" * 64 + "\n", encoding="utf-8")  # 仅一行 64-hex 容器 ID
    warned = []
    proc = mock.Mock(stdout="CUDA error: out of memory\n", stderr="")
    with _patch_common(None, A(_profile(docker=True), CAPS)), \
         mock.patch.object(all_service, "launch_log", return_value=log), \
         mock.patch.object(all_service.subprocess, "run", return_value=proc) as run, \
         mock.patch.object(all_service, "logger") as lg:
        lg.warning.side_effect = lambda *a, **k: warned.append(str(a[0]) if a else "")
        r = all_service.start_profile(_profile(docker=True), CAPS, 5)
    assert r.status == "error"
    run.assert_called_once()
    assert run.call_args.args[0] == ["docker", "logs", "--tail", "50", "q-vllm"]
    assert run.call_args.kwargs["capture_output"] is True and run.call_args.kwargs["timeout"] == 30
    assert any("CUDA error" in m for m in warned), warned


def test_venv_engine_without_fallback_cmd_skips_docker_logs(tmp_path):
    """F8-3：log_fallback_cmd None（venv 引擎）→ 不跑 docker logs 兜底。"""
    class A(_FakeAdapter):
        def wait_ready(self, timeout):
            return False

        def backend_dead(self):
            return True

    log = tmp_path / "launch-q.log"
    log.write_text("a" * 64 + "\n", encoding="utf-8")
    with _patch_common(None, A(_profile(), CAPS)), \
         mock.patch.object(all_service, "launch_log", return_value=log), \
         mock.patch.object(all_service.subprocess, "run") as run:
        r = all_service.start_profile(_profile(), CAPS, 5)
    assert r.status == "error"
    run.assert_not_called()
