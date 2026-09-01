#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/capabilities.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 硬件环境能力探测
# ===============================================================================

"""core/capabilities.py — 启动前硬件/环境能力探测（GPU、CC、引擎二进制）。"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from modelctl.core.envs import MANAGED_ENGINES, engine_bin, has_env

ENGINE_BINARIES = ["ollama", "vllm", "sglang", "unsloth", "llamacpp",
                   "aphrodite", "lmdeploy", "tensorrt_llm", "tokenspeed"]

ENGINE_INSTALL_HINTS = {
    "ollama": "，建议执行：curl -fsSL https://ollama.com/install.sh | sh",
    # 托管引擎统一走 modelctl env setup 创建引擎专用 venv，避免在主环境引入互斥依赖
    "vllm": "，建议执行：modelctl env setup vllm",
    "sglang": "，建议执行：modelctl env setup sglang（与 vllm 依赖互斥，需独立 venv）",
    # 无头推理（studio run）依赖官方安装器搭建的运行时，仅 pip install 不够
    "unsloth": "，建议执行：curl -fsSL https://unsloth.ai/install.sh | sh",
    "aphrodite": "，建议执行：modelctl env setup aphrodite",
    "lmdeploy": "，建议执行：modelctl env setup lmdeploy",
    "tensorrt_llm": "，建议执行：modelctl env setup tensorrt_llm",
    "tokenspeed": "，建议执行：modelctl env setup tokenspeed",
    # llamacpp 提示较长（源码下载 + 编译命令），由 cli._cmd_probe 单独多行输出
}


@dataclass
class Capabilities:
    """当前运行环境的硬件与二进制能力摘要。"""

    gpu_count: int = 0
    gpu_indices: list[int] = field(default_factory=list)
    vram_total_mb_per_gpu: list[int] = field(default_factory=list)  # 每卡总显存（MB），与 vram_free_mb 对齐
    gpu_name: str = ""
    vram_total_mb: int = 0
    vram_free_mb: list[int] = field(default_factory=list)
    cuda_driver: str = ""
    compute_capability: str = ""
    binaries: dict[str, bool] = field(default_factory=dict)
    binary_paths: dict[str, str | None] = field(default_factory=dict)


def _managed_binary_path(engine: str, name: str) -> Path | None:
    """托管引擎可执行文件路径；引擎专用 venv 未创建时返回 None。"""
    if engine not in MANAGED_ENGINES:
        return None
    if not has_env(engine):
        return None
    return engine_bin(engine, name)


def which_binaries(names: list[str]) -> dict[str, bool]:
    """探测引擎二进制是否可用。

    托管引擎（vllm / sglang）以专用 venv 是否创建（has_env）判定；
    非托管引擎维持 PATH 探测（shutil.which）。
    """
    result: dict[str, bool] = {}
    for n in names:
        if n in MANAGED_ENGINES:
            result[n] = _managed_binary_path(n, n) is not None
        else:
            result[n] = shutil.which(n) is not None
    return result


def binary_paths(names: list[str]) -> dict[str, str | None]:
    """探测引擎二进制完整路径；未找到时返回 None。

    托管引擎返回 venv 内路径；非托管引擎返回 PATH 中的路径。
    """
    result: dict[str, str | None] = {}
    for n in names:
        if n in MANAGED_ENGINES:
            path = _managed_binary_path(n, n)
            result[n] = str(path) if path is not None else None
        else:
            result[n] = shutil.which(n)
    return result


def find_llamacpp_binary() -> str | None:
    """定位 llama.cpp 编译产物 llama-server。

    依次检查 PATH、LLAMACPP_SOURCE_DIR/build/bin/llama-server、
    LLAMACPP_SOURCE_DIR/llama-server；未找到返回 None（pre_start 会真正编译）。
    """
    in_path = shutil.which("llama-server")
    if in_path:
        return in_path
    source = os.environ.get("LLAMACPP_SOURCE_DIR", "")
    if source:
        source_dir = Path(source)
        for candidate in (source_dir / "build" / "bin" / "llama-server", source_dir / "llama-server"):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _run_nvidia_smi() -> str:
    """调用 nvidia-smi 获取 CSV 格式的 GPU 信息。"""
    out = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.free,driver_version,compute_cap",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return out.stdout if out.returncode == 0 else ""


def _safe_smi() -> str:
    """安全调用 nvidia-smi：失败/异常时返回空字符串。"""
    try:
        return _run_nvidia_smi()
    except (OSError, subprocess.SubprocessError):
        return ""


def probe(nvidia_smi_output: str | None = None) -> Capabilities:
    """探测硬件能力。传入 nvidia_smi_output 时仅解析，不实际调用命令（便于测试）。"""
    text = nvidia_smi_output if nvidia_smi_output is not None else _safe_smi()
    caps = Capabilities(
        binaries=which_binaries(ENGINE_BINARIES),
        binary_paths=binary_paths(ENGINE_BINARIES),
    )
    # llamacpp 不依赖 PATH 二进制，由源码编译；编译产物存在即视为可用
    if not caps.binaries.get("llamacpp"):
        llamacpp_bin = find_llamacpp_binary()
        if llamacpp_bin:
            caps.binaries["llamacpp"] = True
            caps.binary_paths["llamacpp"] = llamacpp_bin
    rows = [r.strip() for r in text.splitlines() if r.strip()]
    if not rows:
        return caps

    frees: list[int] = []
    totals: list[int] = []
    for row in rows:
        parts = [p.strip() for p in row.split(",")]
        if len(parts) < 5:
            continue
        if not caps.gpu_name:
            caps.gpu_name = parts[0]
            caps.cuda_driver = parts[3]
            caps.compute_capability = parts[4]
            try:
                caps.vram_total_mb = int(parts[1])
            except ValueError:
                pass
        try:
            frees.append(int(parts[2]))
        except ValueError:
            frees.append(0)
        try:
            totals.append(int(parts[1]))
        except ValueError:
            totals.append(0)
    caps.vram_free_mb = frees
    caps.gpu_count = len(frees)
    caps.vram_total_mb_per_gpu = totals
    caps.gpu_indices = list(range(len(frees)))
    return caps


def cc_at_least(cc: str, major: int, minor: int) -> bool:
    """判断 compute capability 是否大于等于指定版本。"""
    try:
        hi, lo = (int(x) for x in cc.split(".", 1))
    except (ValueError, AttributeError):
        return False
    return (hi, lo) >= (major, minor)


def free_vram_total_mb(caps: Capabilities) -> int:
    """汇总所有 GPU 的剩余显存（MB）。"""
    return sum(caps.vram_free_mb)


def all_vram_total_mb(caps: Capabilities) -> int:
    """汇总所有 GPU 的总显存（MB）。"""
    return sum(caps.vram_total_mb_per_gpu)


def selected_vram_total_mb(caps: Capabilities, gpus: list[int]) -> int:
    """按选中 GPU 索引汇总各卡总显存。"""
    return sum((caps.vram_total_mb_per_gpu[g] if g < len(caps.vram_total_mb_per_gpu) else 0) for g in gpus)


def selected_vram_free_mb(caps: Capabilities, gpus: list[int]) -> int:
    """按选中 GPU 索引汇总各卡剩余显存（MB）。"""
    return sum((caps.vram_free_mb[g] if g < len(caps.vram_free_mb) else 0) for g in gpus)
