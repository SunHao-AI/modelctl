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

import os
import subprocess as sp
import sys
import time
import urllib.error

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


# ---- docker_container_alive：容器存活探测 ----

def _fake_run(stdout: str = "", returncode: int = 0, stderr: str = "", exc: Exception | None = None):
    def _run(cmd, **kwargs):
        assert cmd[:3] == ["docker", "inspect", "--format"]
        assert cmd[3] == "{{.State.Running}}"
        if exc is not None:
            raise exc
        return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()
    return _run


def test_docker_container_alive_running_true(monkeypatch):
    monkeypatch.setattr(process.subprocess, "run", _fake_run(stdout="true\n"))
    assert process.docker_container_alive("q-vllm") is True


def test_docker_container_alive_stopped_false(monkeypatch):
    """容器存在但已退出（Running=false）——docker run -d 后 vllm 起不了立刻退出。"""
    monkeypatch.setattr(process.subprocess, "run", _fake_run(stdout="false\n"))
    assert process.docker_container_alive("q-vllm") is False


def test_docker_container_alive_missing_container_false(monkeypatch):
    """docker inspect 对不存在的容器：exit 1 + stderr 含 'No such object' → 视为已死。

    这也是 --rm 容器崩溃后被 daemon 自动回收的形态：inspect 找不到对象。
    """
    monkeypatch.setattr(
        process.subprocess, "run",
        _fake_run(returncode=1, stderr="Error: No such object: q-vllm\n"),
    )
    assert process.docker_container_alive("q-vllm") is False


def test_docker_container_alive_subprocess_error_true(monkeypatch):
    """docker 不在 PATH / inspect 超时：未知→保守当存活，保留健康检查兜底。

    设计克制：探针误报（容器其实活着却判死了）比探针漏报
    （容器真死了但空转到超时）代价更高——前者会假死 + 不建议。
    """
    monkeypatch.setattr(process.subprocess, "run", _fake_run(exc=OSError("docker not found")))
    assert process.docker_container_alive("q-vllm") is True


def test_docker_container_alive_docker_daemon_down_true(monkeypatch):
    """docker 命令存在但 daemon 查不到/拒访问（非 'No such object'）→ 保守当存活。"""
    monkeypatch.setattr(
        process.subprocess, "run",
        _fake_run(returncode=1, stderr="Cannot connect to the Docker daemon\n"),
    )
    assert process.docker_container_alive("q-vllm") is True


# ---- is_running_any / start_detached(write_pid) / stop_docker_instance ----


def _write_pid(name: str, pid: int, cache_path) -> None:
    (cache_path / f"{name}.pid").write_text(str(pid), encoding="utf-8")


class _FakeProfile:
    """最小化 profile：只暴露 is_running_any 用到的 name/port/engine_config/api_key。"""
    def __init__(self, name, port, api_key=None, ec=None):
        self.name = name
        self.port = port
        self.api_key = api_key
        self.engine_config = ec or {}


def _fake_resp_200(req, timeout):
    """仅返回 status=200 的伪对象，便于 mock open_local。"""
    return _Resp200()


class _Resp200:
    def __init__(self): self.status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _raise_url_error(req, timeout):
    """模拟端口不可达（连接被拒）。"""
    raise urllib.error.URLError("refused")


def test_is_running_any_port_healthy_true(monkeypatch, tmp_path):
    """profile 有且端口 /health 200 → True（不触碰 PID 文件）"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(process, "open_local", _fake_resp_200)
    result = process.is_running_any("p1", _FakeProfile("p1", 8100))
    assert result is True


def test_is_running_any_port_up_pid_dead_preserves_file(monkeypatch, tmp_path):
    """端口 200 但 PID 文件已死 → True，且判定不得有副作用（dead PID 文件保留，留给 stop 清理）"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _write_pid("p2", 999999, tmp_path / "cache")  # 999999 在 /proc 必死
    monkeypatch.setattr(process, "open_local", _fake_resp_200)
    assert process.is_running_any("p2", _FakeProfile("p2", 8101)) is True
    assert (tmp_path / "cache" / "p2.pid").is_file()  # 无副作用：不删


def test_is_running_any_port_down_pid_dead_preserves_file(monkeypatch, tmp_path):
    """端口不通 + PID 已死 → False，dead PID 文件仍保留（CLI 据此报 "PID 残留"）"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _write_pid("p2b", 999999, tmp_path / "cache")
    monkeypatch.setattr(process, "open_local", _raise_url_error)
    assert process.is_running_any("p2b", _FakeProfile("p2b", 8109)) is False
    assert (tmp_path / "cache" / "p2b.pid").is_file()


def test_is_running_any_all_none_false(monkeypatch, tmp_path):
    """profile=None + 无 PID 文件 → False"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    assert process.is_running_any("p3", None) is False


def test_is_running_any_profile_none_with_corrupt_pid_file(monkeypatch, tmp_path):
    """profile=None 且 PID 文件损坏（无法解析为 int）→ False 且文件保留（判定无副作用）"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache" / "p4.pid").write_text("not-a-pid", encoding="utf-8")
    assert process.is_running_any("p4", None) is False
    assert (tmp_path / "cache" / "p4.pid").is_file()  # 无副作用：不删


def test_is_running_any_port_down_pid_alive_true(monkeypatch, tmp_path):
    """venv 情况：/health 还没起来但 venv 进程确实活着 → True（PID 兜底）"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _write_pid("p5", os.getpid(), tmp_path / "cache")  # 自己的 PID 必活
    # 模拟端口不通（抛 URLError）
    monkeypatch.setattr(process, "open_local", _raise_url_error)
    assert process.is_running_any("p5", _FakeProfile("p5", 8102)) is True


def test_is_running_any_unknown_name_returns_false(monkeypatch, tmp_path):
    """兜底：profile 存在但端口 / PID 都无 → False（不抛错）"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    assert process.is_running_any("ghost", _FakeProfile("ghost", 1)) is False


def test_start_detached_write_pid_false(monkeypatch, tmp_path):
    """write_pid=False 时不写 PID 文件，但返回 (pid, proc) 维持签名"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    pid, proc = process.start_detached("no-pid-w", [sys.executable, "-c", "pass"], {}, write_pid=False)
    assert (tmp_path / "cache" / "no-pid-w.pid").exists() is False
    assert isinstance(pid, int) and pid > 0
    assert isinstance(proc, sp.Popen)
    proc.wait()


def test_start_detached_write_pid_default_true(monkeypatch, tmp_path):
    """默认 write_pid=True 保持向后兼容：PID 文件照常写入"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    pid, proc = process.start_detached("has-pid-w", [sys.executable, "-c", "pass"], {})
    assert (tmp_path / "cache" / "has-pid-w.pid").is_file() is True
    assert int((tmp_path / "cache" / "has-pid-w.pid").read_text()) == pid
    proc.wait()


def test_stop_docker_instance_runs_rm_and_cleans(monkeypatch, tmp_path):
    """docker rm -f 被记录 + 残留 PID 被清理 + gpu_lock 被释放"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _write_pid("app", 12345, tmp_path / "cache")
    invocations = []

    def _fake_run(cmd, **kw):
        invocations.append((cmd, kw))
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(process.subprocess, "run", _fake_run)
    release_calls = []
    monkeypatch.setattr("modelctl.core.gpu_lock.release_gpu_lock",
                        lambda name: release_calls.append(name) or None)
    ok = process.stop_docker_instance("app", "app-vllm")
    assert ok is True
    assert invocations and invocations[0][0] == ["docker", "rm", "-f", "app-vllm"]
    assert "timeout" in invocations[0][1] and invocations[0][1]["timeout"] == 10
    assert "capture_output" in invocations[0][1] and invocations[0][1]["capture_output"] is True
    assert not (tmp_path / "cache" / "app.pid").is_file()
    assert release_calls == ["app"]


def test_stop_docker_instance_docker_unavailable(monkeypatch, tmp_path):
    """docker 命令缺失（OSError）时仍 True 但清本地 PID + 释放锁"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _write_pid("x", 1, tmp_path / "cache")

    def _boom(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(process.subprocess, "run", _boom)
    release_calls = []
    monkeypatch.setattr("modelctl.core.gpu_lock.release_gpu_lock",
                        lambda name: release_calls.append(name) or None)
    assert process.stop_docker_instance("x", "x-vllm") is True
    assert not (tmp_path / "cache" / "x.pid").is_file()
    assert release_calls == ["x"]
