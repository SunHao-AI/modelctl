#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_real_env_probe.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/14 10:00
# @Desc   : 真实环境探测补测（nvidia-smi / docker daemon 本机真跑，CI 自动跳过）
# ===============================================================================

"""真实硬件/守护进程探测补测。

CI runner 永远没有 GPU 与 nvidia-container-toolkit，capabilities / docker_setup 的
**真跑分支**（_run_nvidia_smi 的真实 CSV 解析、diagnose 的 daemon 实探）是 CI 结构性
盲区。本文件在具备条件的开发机上真跑这些分支，条件不满足时逐个 skip（绝不假通过）。

契约钉子（真环境跑出的结论同样必须是契约）：
- probe() 真 CSV → gpu_count 与 nvidia-smi 实际行数一致、字段均为合法值；
- diagnose() 的 docker_cli/docker_daemon 两项与 `docker info` 退出码一致；
- docker_ready()（纯 which）与 diagnose()（落子进程）在真机上语义互恰。
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from modelctl.core import capabilities, docker_setup


def _nvidia_smi_ok() -> bool:
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        return subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _docker_daemon_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


requires_gpu = pytest.mark.skipif(not _nvidia_smi_ok(), reason="本机无可用 nvidia-smi（CI/无 GPU 机器跳过）")
requires_docker = pytest.mark.skipif(not _docker_daemon_ok(), reason="本机 docker daemon 不可用（CI 跳过）")


@requires_gpu
def test_probe_parses_real_nvidia_smi_output() -> None:
    """真跑 nvidia-smi：probe() 必须把真实 CSV 解析成与命令行口径一致的 GPU 摘要。"""
    cli = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    cli_rows = [r for r in cli.stdout.splitlines() if r.strip()]
    assert cli_rows, "nvidia-smi 可用但无输出行（环境异常）"

    caps = capabilities.probe()  # 不打桩：走 _safe_smi() 真实子进程路径
    assert caps.gpu_count == len(cli_rows)
    assert caps.gpu_indices == list(range(len(cli_rows)))
    assert caps.vram_free_mb and all(f >= 0 for f in caps.vram_free_mb)
    assert caps.vram_total_mb_per_gpu and all(t > 0 for t in caps.vram_total_mb_per_gpu)
    assert caps.gpu_name and caps.cuda_driver
    # compute_cap 必须是 "major.minor" 且能被 cc_at_least 消化（真机解析契约）
    assert capabilities.cc_at_least(caps.compute_capability, 1, 0)
    # 首行显存总量与命令行口径一致（同一时刻 free<=total 的物理约束）
    name0, total0 = (p.strip() for p in cli_rows[0].split(",", 1))
    assert caps.gpu_name == name0
    assert caps.vram_total_mb == int(total0)
    assert caps.vram_free_mb[0] <= caps.vram_total_mb_per_gpu[0]


@requires_gpu
def test_run_nvidia_smi_real_output_shape() -> None:
    """_run_nvidia_smi 真输出必须是有 5 列的 CSV 行（probe 解析器依赖的列序契约）。"""
    text = capabilities._run_nvidia_smi()
    assert text.strip()
    for row in text.splitlines():
        if not row.strip():
            continue
        parts = [p.strip() for p in row.split(",")]
        assert len(parts) == 5, f"nvidia-smi CSV 列序漂移（probe 解析会静默跳过该行）：{row!r}"
        assert int(parts[1]) > 0 and int(parts[2]) >= 0


def test_safe_smi_swallows_real_subprocess_failures(monkeypatch) -> None:
    """_safe_smi 必须吞掉两类真实故障：子进程抛 OSError、非 0 退出（返回值不是数据）。

    真子进程而非全 mock：非 0 退出分支拿 sys.executable 真跑一条 exit(3)，
    钉的是「rc!=0 → 空串」这条兜底语义在真实进程上成立。capabilities.subprocess
    就是 subprocess 模块本身，lambda 里必须用提前捕获的真 run 引用，否则自引用。
    """
    real_run = subprocess.run

    def boom(*a, **k):
        raise OSError("nvidia-smi: 拒绝访问")

    monkeypatch.setattr(capabilities.subprocess, "run", boom)
    assert capabilities._safe_smi() == ""

    monkeypatch.setattr(
        capabilities.subprocess, "run",
        lambda *a, **k: real_run([sys.executable, "-c", "import sys; sys.exit(3)"],
                                 capture_output=True, text=True, timeout=30))
    assert capabilities._safe_smi() == ""  # rc!=0 → 空串，绝不把 stderr 当数据


@requires_gpu
def test_docker_ready_true_on_real_machine() -> None:
    """真机（docker+nvidia-smi 均在 PATH）：docker_ready 纯 which 判定必须为 True。"""
    assert capabilities.docker_ready() is True


@requires_docker
def test_diagnose_daemon_check_matches_docker_info() -> None:
    """diagnose() 的 daemon 项必须与 `docker info` 实际退出码一致（真子进程路径）。"""
    checks = {c.key: c for c in docker_setup.diagnose()}
    assert checks["docker_cli"].ok is True
    assert checks["docker_daemon"].ok is True, checks["docker_daemon"].detail
    assert checks["docker_daemon"].detail == ""
    # 结构契约：四项齐全、文案与缺失常量对齐
    assert set(checks) == {"docker_cli", "docker_daemon", "nvidia_toolkit", "nvidia_runtime"}
    if not checks["nvidia_toolkit"].ok:
        assert checks["nvidia_toolkit"].detail == "nvidia-ctk / nvidia-container-runtime 均未安装"


def test_path_level_missing_pure_which_semantics(monkeypatch) -> None:
    """path_level_missing 必须绝不落子进程：清空 PATH 后恰好返回两条标准缺失文案。"""
    import os
    import sys

    real_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", sys.prefix + os.sep + "nonexistent-dir")
    missing = docker_setup.path_level_missing()
    assert missing == [docker_setup.MSG_DOCKER_MISSING, docker_setup.MSG_TOOLKIT_MISSING]
    # 反向钉子：恢复真实 PATH 后，本机装有什么就不得报什么缺失（which 判定与真实安装一致）
    monkeypatch.setenv("PATH", real_path)
    missing2 = docker_setup.path_level_missing()
    if shutil.which("docker"):
        assert docker_setup.MSG_DOCKER_MISSING not in missing2
    if shutil.which("nvidia-smi"):
        assert docker_setup.MSG_TOOLKIT_MISSING not in missing2
