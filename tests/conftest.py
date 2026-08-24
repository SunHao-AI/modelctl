"""共享测试 fixtures。"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time

import pytest


@pytest.fixture()
def dead_pid() -> int:
    """一个确定已死的 PID：派生短命子进程、退出后释放本端全部句柄，并轮询确认探测为死。

    不用硬编码大数（某些平台上该数值可能是合法 PID），也不做单次探测——
    Windows 上杀毒软件等外部瞬态句柄或 PID 复用可能导致短暂误判存活，须轮询确认。
    """
    from modelctl.core.process import is_pid_alive

    deadline = time.time() + 10
    while True:
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait(timeout=15)
        pid = p.pid
        if sys.platform == "win32":
            # Windows：内核对象随任一打开的句柄驻留，须主动释放父进程对已退出子进程的句柄，
            # 否则 OpenProcess 仍会探测为存活
            h = int(p._handle)
            p._handle.closed = True  # 防止 Popen 析构时二次 CloseHandle 报警告
            ctypes.windll.kernel32.CloseHandle(h)
        del p
        probe_deadline = time.time() + 5
        while is_pid_alive(pid) and time.time() < probe_deadline:
            time.sleep(0.1)
        if not is_pid_alive(pid):
            return pid
        if time.time() > deadline:
            raise AssertionError(f"无法获得确定已死的 PID（pid {pid} 持续被探测为存活超过 10s）")
