#!/usr/bin/env python3
"""core/process.py — 引擎无关的进程生命周期：后台启动、PID、停止、健康检查。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from modelctl.core.envfile import PROJECT_ROOT

if sys.platform == "win32":
    import ctypes


def is_pid_alive(pid: int) -> bool:
    """探测 pid 对应进程是否存活（PID 文件 / GPU 锁共用的统一入口）。"""
    if sys.platform == "win32":
        # Windows 实测：对不存在的 PID 调 CPython os.kill(pid, 0)（内部 OpenProcess）
        # 之后控制台会被投递异步 Ctrl-C 事件、连带杀掉宿主会话；改用 ctypes 直连
        # kernel32.OpenProcess 做存在性探测可完全规避。
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)  # signal 0 = existence probe
        return True
    except OSError:
        return False


def log_dir() -> Path:
    d = Path(os.environ.get("LOG_DIR") or PROJECT_ROOT.parent / "logs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    """进程元数据目录（PID 文件），默认项目根 data/cache（与用量统计缓存一致）。"""
    d = Path(os.environ.get("CACHE_DIR") or PROJECT_ROOT / "data" / "cache")
    d.mkdir(parents=True, exist_ok=True)
    return d


def pid_file(name: str) -> Path:
    return cache_dir() / f"{name}.pid"


def launch_log(name: str) -> Path | None:
    """当前实例的启动日志（固定文件名 launch-<name>.log；未启动过则为 None）。

    固定文件名 + 每次启动覆盖，避免多份时间戳日志堆积。
    """
    path = log_dir() / f"launch-{name}.log"
    return path if path.is_file() else None


def start_detached(name: str, command: list[str], extra_env: dict[str, str]) -> int:
    log_path = log_dir() / f"launch-{name}.log"
    env = {**os.environ, **extra_env}
    fp = open(log_path, "w", encoding="utf-8")  # "w"：每次启动覆盖旧日志
    kwargs: dict = {"stdout": fp, "stderr": subprocess.STDOUT, "env": env, "stdin": subprocess.DEVNULL}
    kwargs["start_new_session"] = True  # nohup 语义：SSH 断开不影响
    proc = subprocess.Popen(command, **kwargs)
    pid_file(name).write_text(str(proc.pid), encoding="utf-8")
    return proc.pid


def is_running(name: str) -> bool:
    pf = pid_file(name)
    if not pf.is_file():
        return False
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except ValueError:
        # 无法解析的 PID 文件直接删除，视为异常
        pf.unlink(missing_ok=True)
        return False
    if is_pid_alive(pid):
        return True
    # 进程已不存在，清理残留的 PID 文件
    pf.unlink(missing_ok=True)
    return False


def stop_instance(name: str, port: int, patterns: list[str]) -> bool:
    """先按 PID 优雅终止（POSIX：进程组 SIGTERM→SIGKILL；Windows：taskkill /T /F），
    POSIX 平台再按端口/进程名兜底。返回是否执行了基于 PID 的停止。"""
    stopped = False
    pf = pid_file(name)
    if pf.is_file():
        try:
            pid = int(pf.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = None
        if sys.platform != "win32":
            if pid is not None:
                try:
                    os.killpg(pid, signal.SIGTERM)  # type: ignore[attr-defined]  # POSIX-only，Windows 类型桩无此 API
                except OSError:
                    pass
                deadline = time.time() + 10
                while time.time() < deadline:
                    if not is_pid_alive(pid):
                        break
                    time.sleep(0.5)
                else:
                    try:
                        os.killpg(pid, signal.SIGKILL)  # type: ignore[attr-defined]  # POSIX-only，Windows 类型桩无此 API
                    except OSError:
                        pass
                stopped = True
        elif pid is not None:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            stopped = True
        pf.unlink(missing_ok=True)
    if sys.platform != "win32":
        subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
        for pat in patterns:
            subprocess.run(["pkill", "-f", pat], capture_output=True)
    try:
        from modelctl.core.gpu_lock import release_gpu_lock

        release_gpu_lock(name)
    except Exception:
        pass
    return stopped


def wait_health(url: str, timeout: float, api_key: str | None = None) -> bool:
    deadline = time.time() + timeout
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    interval = 1.0
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if 200 <= resp.status < 300:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
        interval = min(interval * 2, 5.0)
    return False


def tail_file(path: Path, lines: int) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])
