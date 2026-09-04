#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/conftest.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 共享测试 fixtures
# ===============================================================================

"""共享测试 fixtures。"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_dirs(tmp_path, monkeypatch):
    """PID 文件与日志目录默认指向 tmp_path，杜绝测试触碰项目外的真实运行目录。

    CACHE_DIR：可用口径（is_model_available / _instance_state）会读 pid_file(name)
      区分"外部启动"与"PID 异常"，不隔离会让测试结论依赖仓库 data/cache 的真实内容。
    LOG_DIR：缺省值落到项目外的 <项目根>/../logs（见 core/logging.py）。未显式设置的
      用例调 cli.main() → setup_logging() 时，loguru 文件 sink 会在该路径打开句柄；
      该句柄跨用例存活，被 pytest 的 gc.collect() 析构时 close() 抛 OSError(EBADF)，
      表现为"当时正在跑的用例"莫名失败（与用例自身逻辑无关）。
    AUDIT_DIR：网关审计日志缺省写 CWD 下 data/audit，不隔离会把测试请求写进仓库。
    GATEWAY_* delenv：cli 入口与 admin 端点会 load_env() 把**开发者本地 .env** 经
      os.environ.setdefault 注入进程（不受 monkeypatch 管辖、跨用例存活）。典型翻车：
      .env 里 GATEWAY_DEFAULT_MODEL=qwen3.8 泄漏后，create_app(default_model=None) 被
      env 兜底，未知 model 不再 404——test_gateway 在全量跑时即因此失败、单跑通过。
      每个用例前强制清除，测试只认用例自己显式设置的值。
    """
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.delenv("GATEWAY_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("GATEWAY_CONTEXT_SWITCH", raising=False)
    # CLUSTER_* delenv：同 GATEWAY_* 口径——开发者 .env 里的 CLUSTER_ROLE 等经 load_env()
    # 注入后，solo 用例的 404 断言会被"意外启用"的集群角色破坏。cluster 用例在自己的
    # fixture/用例内 setenv，晚于本 autouse fixture 执行，不受影响。
    monkeypatch.delenv("CLUSTER_ROLE", raising=False)
    monkeypatch.delenv("CLUSTER_CENTER_URL", raising=False)
    monkeypatch.delenv("CLUSTER_NODE_ID", raising=False)
    monkeypatch.delenv("CLUSTER_LAN", raising=False)
    monkeypatch.delenv("CLUSTER_JOIN_TOKEN", raising=False)
    monkeypatch.delenv("CLUSTER_NODE_TOKEN", raising=False)
    monkeypatch.delenv("CLUSTER_LEASE_S", raising=False)
    monkeypatch.delenv("CLUSTER_HEARTBEAT_INTERVAL_S", raising=False)
    monkeypatch.delenv("CLUSTER_WS_INSECURE", raising=False)


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
