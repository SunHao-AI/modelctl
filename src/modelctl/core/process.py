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

from loguru import logger

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


def start_detached(name: str, command: list[str], extra_env: dict[str, str]) -> tuple[int, subprocess.Popen]:
    """后台启动进程，返回 (pid, Popen)。Popen 供调用方在等待健康检查期间探测早退（fail-fast）。"""
    log_path = log_dir() / f"launch-{name}.log"
    env = {**os.environ, **extra_env}
    fp = open(log_path, "w", encoding="utf-8")  # "w"：每次启动覆盖旧日志
    kwargs: dict = {"stdout": fp, "stderr": subprocess.STDOUT, "env": env, "stdin": subprocess.DEVNULL}
    kwargs["start_new_session"] = True  # nohup 语义：SSH 断开不影响
    proc = subprocess.Popen(command, **kwargs)
    pid_file(name).write_text(str(proc.pid), encoding="utf-8")
    return proc.pid, proc


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


def open_local(request: urllib.request.Request, timeout: float):
    """本机回环探测专用 opener：绕过 http(s)_proxy/no_proxy 环境变量。

    项目内所有健康检查目标均为 127.0.0.1；若沿用 urlopen 默认行为，设置了系统代理的机器上
    回环请求也会被转发给代理（通常无法访问本机端口），导致探测永远失败、启动卡满超时。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout)


def wait_health(url: str, timeout: float, api_key: str | None = None, alive_check=None) -> bool:
    """轮询探测健康端点直至成功或超时。

    alive_check：可选的进程存活探针（返回 bool）。引擎进程先行退出时立即结束等待，
    不再空转到超时——但先完成当次探测再判定，保证共享后端场景（如 ollama 多 profile
    共用一个 serve）中本实例子进程退出、端口仍由他人服务时不误报失败。
    """
    deadline = time.time() + timeout
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    interval = 1.0
    last_err = ""
    while True:
        healthy = False
        try:
            req = urllib.request.Request(url, headers=headers)
            with open_local(req, timeout=5) as resp:
                if 200 <= resp.status < 300:
                    healthy = True
                else:
                    last_err = f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            # URLError.reason 比 str(e) 更简洁（如 "Name or service not known"）
            last_err = str(getattr(e, "reason", None) or e)
        if healthy:
            return True
        if alive_check is not None and not alive_check():
            logger.warning("引擎进程已提前退出，中止健康检查等待")
            break
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
        interval = min(interval * 2, 5.0)
    if last_err:
        logger.warning(f"健康检查未通过（{url}），最后错误：{last_err}")
    return False


def tail_file(path: Path, lines: int) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


# 启动失败诊断的错误标记：命中行的前 _EXCERPT_BEFORE、后 _EXCERPT_AFTER 行构成一个上下文块。
_EXCERPT_MARKERS = (
    "Traceback (most recent call last)",
    "CUDA error",
    "out of memory",
    "OutOfMemory",
    "NCCL",
    "RuntimeError",
    "ValueError",
    "AssertionError",
    "ImportError",
    "ModuleNotFoundError",
    "Address already in use",
    "Engine core initialization failed",
)
_EXCERPT_BEFORE = 10
_EXCERPT_AFTER = 60
_EXCERPT_MAX_BLOCKS = 3
_EXCERPT_LINE_WIDTH = 240


def log_excerpt(path: Path) -> str | None:
    """按错误标记截取日志关键片段（多区块合并），用于进程早退时的失败诊断。

    vLLM 等引擎崩溃时真实异常常位于日志中部，尾部 50 行可能只是 traceback 的尾巴；
    此函数定位 Traceback / OOM / NCCL 等标记并带上下文输出（最多 3 个区块）。
    无标记或读取失败返回 None。
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    ranges: list[list[int]] = []  # [start, end)，按出现顺序收集、重叠即并入前一区段
    for i, line in enumerate(lines):
        if any(m in line for m in _EXCERPT_MARKERS):
            start = max(0, i - _EXCERPT_BEFORE)
            end = min(len(lines), i + _EXCERPT_AFTER)
            if ranges and start <= ranges[-1][1]:
                ranges[-1][1] = max(ranges[-1][1], end)
            else:
                ranges.append([start, end])
            if len(ranges) >= _EXCERPT_MAX_BLOCKS:
                break
    if not ranges:
        return None
    out: list[str] = []
    for start, end in ranges:
        out.append(f"—— 第 {start + 1}-{end} 行 ——")
        for n in range(start, end):
            text = lines[n]
            if len(text) > _EXCERPT_LINE_WIDTH:
                text = text[:_EXCERPT_LINE_WIDTH] + " …(截断)"
            out.append(f"{n + 1:>6} | {text}")
        if end < len(lines):
            out.append(f"...（后续还有 {len(lines) - end} 行，完整内容见日志文件）")
    return "\n".join(out)
