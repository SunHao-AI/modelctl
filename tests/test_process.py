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
