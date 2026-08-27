#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_process.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 进程管理测试
# ===============================================================================

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
    pid, proc = process.start_detached("sleeper", [sys.executable, "-c", "import time; time.sleep(60)"], {})
    assert pid > 0 and proc.pid == pid
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
    _, proc = process.start_detached("echoer", [sys.executable, "-c", "print('hello-log')"], {})
    proc.wait(timeout=10)
    log = process.launch_log("echoer")
    assert log is not None and "hello-log" in log.read_text(encoding="utf-8", errors="replace")


def test_launch_log_overwrites_previous(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    _, proc1 = process.start_detached("echoer", [sys.executable, "-c", "print('first-run')"], {})
    proc1.wait(timeout=10)
    _, proc2 = process.start_detached("echoer", [sys.executable, "-c", "print('second-run')"], {})
    proc2.wait(timeout=10)
    log = process.launch_log("echoer")
    assert log is not None
    content = log.read_text(encoding="utf-8", errors="replace")
    assert "second-run" in content
    assert "first-run" not in content


def test_tail_file(tmp_path):
    f = tmp_path / "x.log"
    f.write_text("\n".join(f"line{i}" for i in range(100)), encoding="utf-8")
    assert process.tail_file(f, 3).splitlines() == ["line97", "line98", "line99"]


def _write_crash_log(tmp_path) -> object:
    """模拟 vLLM 崩溃日志：真实异常在日志中部，尾部只有 traceback 尾巴。"""
    lines = [f"INFO filler line {i}" for i in range(60)]
    lines += [
        "ERROR WorkerProc hit an exception.",
        "Traceback (most recent call last):",
        '  File "engine.py", line 1, in <module>',
        "torch.AcceleratorError: CUDA error: out of memory",
    ] + [f"tail line {i}" for i in range(40)]
    f = tmp_path / "crash.log"
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


def test_log_excerpt_locates_midfile_error(tmp_path):
    f = _write_crash_log(tmp_path)
    excerpt = process.log_excerpt(f)
    assert excerpt is not None
    # 关键异常行（位于日志中部）必须出现在摘录中
    assert "CUDA error: out of memory" in excerpt
    assert "WorkerProc hit an exception" in excerpt
    # 带上下文但远小于全文
    total_lines = len((tmp_path / "crash.log").read_text(encoding="utf-8").splitlines())
    assert len(excerpt.splitlines()) < total_lines
    # 带行号便于回查完整日志
    assert "| torch.AcceleratorError" in excerpt


def test_log_excerpt_no_markers_returns_none(tmp_path):
    f = tmp_path / "clean.log"
    f.write_text("all good\nnothing to see\n", encoding="utf-8")
    assert process.log_excerpt(f) is None


def test_log_excerpt_missing_file_returns_none(tmp_path):
    assert process.log_excerpt(tmp_path / "nope.log") is None


def test_log_excerpt_caps_blocks_and_truncates_long_lines(tmp_path):
    lines = []
    for i in range(4):  # 4 个相距超过上下文窗口的错误点 → 最多保留前 3 块
        lines += [f"filler {i}-{j}" for j in range(80)]
        lines.append(f"RuntimeError: boom-{i}")
    f = tmp_path / "many.log"
    f.write_text("\n".join(lines), encoding="utf-8")
    excerpt = process.log_excerpt(f)
    assert excerpt.count("boom-") == 3  # 第 4 处被截断
    long_f = tmp_path / "long.log"
    long_f.write_text("x" * 500 + "\nTraceback (most recent call last):\n", encoding="utf-8")
    out_line = next(l for l in process.log_excerpt(long_f).splitlines() if "x" * 100 in l or "(截断)" in l)
    assert "(截断)" in out_line and len(out_line) < 300


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

    def fake_open(request, timeout=5):
        calls["n"] += 1
        if calls["n"] < 3:  # first two attempts fail, third succeeds
            raise _ue.URLError("down")
        return _Resp()

    monkeypatch.setattr(process, "open_local", fake_open)
    assert process.wait_health("http://x/health", timeout=60) is True
    # backoff doubles: ~1s then ~2s before the successful third probe
    assert len(sleeps) == 2
    assert abs(sleeps[0] - 1.0) < 1e-6
    assert abs(sleeps[1] - 2.0) < 1e-6


def test_wait_health_logs_last_error_on_timeout(monkeypatch):
    """回归：健康检查超时时必须留下失败原因（此前所有错误被静默吞掉，故障无法定位）。"""
    import urllib.error as _ue

    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:

        def fake_open(request, timeout=5):
            raise _ue.HTTPError("http://127.0.0.1:9/health", 404, "Not Found", {}, None)

        monkeypatch.setattr(process, "open_local", fake_open)
        assert process.wait_health("http://127.0.0.1:9/health", timeout=0.4) is False
    finally:
        logger.remove(sink_id)
    assert any("HTTP 404" in m for m in messages), f"未记录最后错误：{messages}"


def test_wait_health_alive_check_dead_process_fails_fast(monkeypatch):
    """回归：引擎进程已退出时应立即结束等待（此前空转到超时，故障定位多等数分钟）。"""
    import urllib.error as _ue

    sleeps: list[float] = []
    monkeypatch.setattr(process.time, "sleep", lambda s: sleeps.append(s))

    def fake_open(request, timeout=5):
        raise _ue.URLError("down")

    monkeypatch.setattr(process, "open_local", fake_open)
    t0 = time.time()
    assert process.wait_health("http://x/health", timeout=60, alive_check=lambda: False) is False
    # 首轮探测失败 + 存活探针为假 → 直接 break，不进入任何 sleep
    assert len(sleeps) == 0 and time.time() - t0 < 2.0


def test_wait_health_alive_check_shared_backend_still_ok(monkeypatch):
    """共享后端场景（ollama 多 profile 共用 serve）：本实例子进程退出但端口仍正常响应 → 不得误报失败。"""

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(request, timeout=5):
        return _Resp()

    monkeypatch.setattr(process, "open_local", fake_open)
    assert process.wait_health("http://x/health", timeout=5, alive_check=lambda: False) is True


def test_open_local_bypasses_system_proxy(monkeypatch):
    """回环探测必须绕过 http(s)_proxy 环境变量（空 ProxyHandler），否则系统代理会拦截本机请求。"""
    import urllib.request as _ur

    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_build_opener(handler):
        captured["handler"] = handler
        return type("O", (), {"open": lambda self, req, timeout=None: _Resp()})()

    monkeypatch.setenv("http_proxy", "http://proxy.example:8080")
    monkeypatch.setattr(process.urllib.request, "build_opener", fake_build_opener)
    with process.open_local(_ur.Request("http://127.0.0.1:9/x"), timeout=3.5):
        pass
    assert isinstance(captured["handler"], _ur.ProxyHandler)
    assert captured["handler"].proxies == {}


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


def test_is_pid_alive_current_process():
    import os

    assert process.is_pid_alive(os.getpid()) is True


def test_is_pid_alive_dead_pid(dead_pid):
    # dead_pid fixture（conftest）：确定已死的真实 PID，不假设某个大数一定无效
    assert process.is_pid_alive(dead_pid) is False
