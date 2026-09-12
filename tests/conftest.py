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
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# 测试分层判定口径（顺序即优先级，首个命中生效；全不命中落 unit）。
# 口径与 pyproject [tool.pytest.ini_options].markers 一致，分层说明见
# docs/health-checks/2026-09-11-test-coverage-report.md §2。
_SECURITY_PREFIX = "test_security_"
_PERF_PREFIX = "test_perf_"
_E2E_PREFIX = "test_e2e_"

# 「集成层」信号：文件名里出现即代表该文件跨越了模块边界——
#   *_http        直接对 HTTP 端点断言（TestClient）
#   *_cli         走 cli.main() 全链路（参数解析 → core 调用 → 输出）
#   test_webui_* / test_cli_* / test_modelctl  同上两类的前缀形态
#   test_gateway* / test_admin_* / test_accounts_gateway  网关与管理面路由契约
#   test_all_service / test_audit / test_compat_flow  跨模块编排链路
_INTEGRATION_PREFIXES = (
    "test_webui_",
    "test_cli_",
    "test_modelctl",
    "test_gateway",
    "test_accounts_gateway",
    "test_admin_",
    "test_all_service",
    "test_audit",
    "test_compat_flow",
)
_INTEGRATION_TOKENS = ("_http", "_cli")


def _layer_of(stem: str) -> str:
    """测试文件名词干 → 分层 marker 名。"""
    if stem.startswith(_SECURITY_PREFIX):
        return "security"
    if stem.startswith(_PERF_PREFIX):
        return "perf"
    if stem.startswith(_E2E_PREFIX):
        return "e2e"
    if stem.startswith(_INTEGRATION_PREFIXES) or any(t in stem for t in _INTEGRATION_TOKENS):
        return "integration"
    return "unit"


@pytest.fixture(autouse=True)
def isolated_runtime_dirs(tmp_path, monkeypatch):
    """PID 文件与日志目录默认指向 tmp_path，杜绝测试触碰项目外的真实运行目录。

    CACHE_DIR：可用口径（is_model_available / _instance_state）会读 pid_file(name)
      区分"外部启动"与"PID 异常"，不隔离会让测试结论依赖仓库 data/cache 的真实内容。
    LOG_DIR：缺省值为 <项目根>/data/logs（见 core/paths.py）。未显式设置的用例调
      cli.main() → setup_logging() 时，loguru 文件 sink 会在该路径打开句柄；
      该句柄跨用例存活，被 pytest 的 gc.collect() 析构时 close() 抛 OSError(EBADF)，
      表现为"当时正在跑的用例"莫名失败（与用例自身逻辑无关）。
    AUDIT_DIR：网关审计日志缺省写 <项目根>/data/audit，不隔离会把测试请求写进仓库。
    GATEWAY_* delenv：cli 入口与 admin 端点会 load_env() 把**开发者本地 .env** 经
      os.environ.setdefault 注入进程（不受 monkeypatch 管辖、跨用例存活）。典型翻车：
      .env 里 GATEWAY_DEFAULT_MODEL=qwen3.8 泄漏后，create_app(default_model=None) 被
      env 兜底，未知 model 不再 404——test_gateway 在全量跑时即因此失败、单跑通过。
      每个用例前强制清除，测试只认用例自己显式设置的值。
    """
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path / "audit"))
    # USAGE_DATA_DIR：stats 与网关的 token 累计目录（默认 data/usage-data），不隔离会把
    # 测试累计写进仓库 data/，且跨用例污染费率/预算断言。
    monkeypatch.setenv("USAGE_DATA_DIR", str(tmp_path / "usage-data"))
    # HF_ENDPOINT delenv：unsloth build_command 会真实注入该值到子进程 env（见 engines/unsloth.py），
    # 开发者 .env 里的镜像地址泄漏会让"未配置 HF_ENDPOINT"的用例断言到意外的 env 键。
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    # MODELCTL_GPUS delenv：cli.main 的 --gpus 与 admin 端点用 **os.environ[...] = **
    # 直写（非 monkeypatch），跨用例存活。泄漏后 EngineAdapter.validate_gpu_selection
    # 会用上例的卡位撞上本例的 caps（多数用例的 Capabilities 默认 gpu_indices=[]），
    # 全量跑时报"[gpu_list] 配置的 GPU 索引 [0, 1] 超出可用范围"，单跑通过。
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    monkeypatch.delenv("GATEWAY_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("GATEWAY_CONTEXT_SWITCH", raising=False)
    # GATEWAY_GROUP_ROUTE_TTL / GATEWAY_AVAIL_CACHE_TTL delenv：两层 TTL 决定 /health 探测
    # 结果是否跨请求缓存＝**改变控制流**。开发者 .env 里设 0（排障口径）后，所有"第二次
    # 请求应命中缓存"的断言会全量红、单跑绿（同 GATEWAY_DEFAULT_MODEL 的泄漏口径）；
    # 用例需缓存时自己 setenv 对应的层（路由层/列表层各管各的）。
    monkeypatch.delenv("GATEWAY_GROUP_ROUTE_TTL", raising=False)
    monkeypatch.delenv("GATEWAY_AVAIL_CACHE_TTL", raising=False)
    # CLUSTER_* delenv：同 GATEWAY_* 口径——开发者 .env 里的 CLUSTER_ROLE 等经 load_env()
    # 注入后，solo 用例的 404 断言会被"意外启用"的集群角色破坏。cluster 用例在自己的
    # fixture/用例内 setenv，晚于本 autouse fixture 执行，不受影响。
    # 前缀式扫描而非白名单枚举：新增 CLUSTER_* 配置键必然漏进枚举清单（M1 就加了
    # CLUSTER_START_TIMEOUT_S 等），漏一个键=该键的 .env 泄漏静默改变控制流。
    for key in list(os.environ):
        if key.startswith("CLUSTER_"):
            monkeypatch.delenv(key, raising=False)


def pytest_collection_modifyitems(config, items):
    """按测试文件的**命名前缀**自动打分层 marker（unit/integration/e2e/security/perf）。

    为什么不逐文件手写 `@pytest.mark.xxx`：tests/ 已有 110+ 文件、1800+ 用例，逐个加装饰器
    是一次纯机械但极易漏的大改动，且"新文件忘了打 marker"会让 `-m unit` 静默少收用例——
    分层筛选一旦不可信，"只跑快测试"的开发回路就会退回全量 37 分钟。
    前缀→层的映射集中在 _LAYER_BY_PREFIX 一处，新文件按命名约定落地即自动分层。

    判层优先级按 _LAYER_BY_PREFIX 顺序**首个命中**即生效；未命中的一律落 `unit`
    （默认值取最轻的一层：宁可让一个真实集成测试被算进 unit，也不要让 `-m unit`
    漏掉用例而让人误以为它没跑）。
    """
    for item in items:
        marker = _layer_of(Path(str(item.fspath)).stem)
        if marker == "unit":
            item.add_marker(pytest.mark.unit)
        else:
            item.add_marker(getattr(pytest.mark, marker))


@pytest.fixture(autouse=True)
def _default_non_wsl2_daemon(monkeypatch):
    """默认把 docker daemon 判定为**非 WSL2**，使 build_command 结果与本机环境无关。

    vllm docker 分支会调 `docker_setup.daemon_is_wsl2()` 决定是否注入
    `VLLM_WSL2_ENABLE_PIN_MEMORY=1`。不打桩的话，测试会真跑 `docker info`，在装了
    Docker Desktop（WSL2 backend）的开发机上得到 True、在 CI（Linux 无 docker）得到
    False——同一断言两种结果。需要 WSL2 分支的用例在自己的用例内 monkeypatch 覆盖
    （autouse fixture 先于用例执行）。
    """
    monkeypatch.setattr("modelctl.core.docker_setup.daemon_is_wsl2", lambda: False)


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
