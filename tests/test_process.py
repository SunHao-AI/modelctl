import sys
import time

import pytest

from modelctl.core import process


def test_pid_file_path(monkeypatch, tmp_path):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    assert process.pid_file("demo") == tmp_path / "demo.pid"


@pytest.mark.skipif(
    sys.platform == "win32", reason="process.py 目标平台为 Linux（os.killpg/fuser/pkill 不可用于 Windows）"
)
def test_start_and_is_running(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    pid = process.start_detached("sleeper", [sys.executable, "-c", "import time; time.sleep(60)"], {})
    assert pid > 0
    assert process.is_running("sleeper")
    process.stop_instance("sleeper", port=1, patterns=[])
    deadline = time.time() + 5
    while process.is_running("sleeper") and time.time() < deadline:
        time.sleep(0.2)
    assert not process.is_running("sleeper")


def test_is_running_no_pidfile(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    assert not process.is_running("ghost")


def test_launch_log_created(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    process.start_detached("echoer", [sys.executable, "-c", "print('hello-log')"], {})
    time.sleep(1)
    log = process.launch_log("echoer")
    assert log is not None and "hello-log" in log.read_text(encoding="utf-8", errors="replace")


def test_launch_log_overwrites_previous(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    process.start_detached("echoer", [sys.executable, "-c", "print('first-run')"], {})
    time.sleep(0.5)
    process.start_detached("echoer", [sys.executable, "-c", "print('second-run')"], {})
    time.sleep(0.5)
    log = process.launch_log("echoer")
    assert log is not None
    content = log.read_text(encoding="utf-8", errors="replace")
    assert "second-run" in content
    assert "first-run" not in content


def test_tail_file(tmp_path):
    f = tmp_path / "x.log"
    f.write_text("\n".join(f"line{i}" for i in range(100)), encoding="utf-8")
    assert process.tail_file(f, 3).splitlines() == ["line97", "line98", "line99"]


def test_wait_health_exponential_backoff(monkeypatch):
    import urllib.error as _ue

    sleeps: list[float] = []
    monkeypatch.setattr(process.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:  # first two attempts fail, third succeeds
            raise _ue.URLError("down")
        return _Resp()

    monkeypatch.setattr(process.urllib.request, "urlopen", fake_urlopen)
    assert process.wait_health("http://x/health", timeout=60) is True
    # backoff doubles: ~1s then ~2s before the successful third probe
    assert len(sleeps) == 2
    assert abs(sleeps[0] - 1.0) < 1e-6
    assert abs(sleeps[1] - 2.0) < 1e-6


def test_stop_instance_windows_uses_taskkill(monkeypatch, tmp_path):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    (tmp_path / "app.pid").write_text("4242", encoding="utf-8")
    ran: list[list[str]] = []

    def _run_recorder(cmd, **k):
        ran.append(cmd)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(process.subprocess, "run", _run_recorder)
    stopped = process.stop_instance("app", port=9, patterns=["foo"])
    assert stopped is True
    assert any(r[0] == "taskkill" and "/PID" in r and "4242" in r and "/T" in r and "/F" in r for r in ran)
    # POSIX-only tools must NOT be invoked on Windows
    assert not any("fuser" in r for r in ran)
    assert not any("pkill" in r for r in ran)


def test_stop_instance_posix_uses_fuser_pkill(monkeypatch, tmp_path):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    (tmp_path / "app.pid").write_text("777", encoding="utf-8")
    ran: list[list[str]] = []

    def _run_recorder(cmd, **k):
        ran.append(cmd)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(process.subprocess, "run", _run_recorder)
    # make the process appear already dead so the SIGTERM wait loop exits immediately

    def fake_kill(pid, sig):
        raise OSError("no such process")

    monkeypatch.setattr(process.os, "kill", fake_kill)
    monkeypatch.setattr(process.os, "killpg", fake_kill, raising=False)  # os.killpg 仅 POSIX 存在
    stopped = process.stop_instance("app", port=9, patterns=["foo"])
    assert stopped is True
    assert any("fuser" in r for r in ran)
    assert any(r and r[0] == "pkill" and "foo" in r for r in ran)
    assert not any("taskkill" in r for r in ran)
