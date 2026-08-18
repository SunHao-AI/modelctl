#!/usr/bin/env python3
"""core/capabilities.py — 启动前硬件/环境能力探测（GPU、CC、引擎二进制）。"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field

ENGINE_BINARIES = ["ollama", "vllm", "sglang", "unsloth"]  # llamacpp 由源码编译，不在此列

ENGINE_INSTALL_HINTS = {
    "ollama": "，建议执行：curl -fsSL https://ollama.com/install.sh | sh",
    "vllm": "，建议执行：pip install vllm",
    "sglang": '，建议执行：pip install "sglang[all]"',
    "unsloth": "，建议执行：pip install unsloth",
}


@dataclass
class Capabilities:
    """当前运行环境的硬件与二进制能力摘要。"""

    gpu_count: int = 0
    gpu_name: str = ""
    vram_total_mb: int = 0
    vram_free_mb: list[int] = field(default_factory=list)
    cuda_driver: str = ""
    compute_capability: str = ""
    binaries: dict[str, bool] = field(default_factory=dict)
    binary_paths: dict[str, str | None] = field(default_factory=dict)


def which_binaries(names: list[str]) -> dict[str, bool]:
    """探测给定可执行文件在 PATH 中是否可用。"""
    return {n: shutil.which(n) is not None for n in names}


def binary_paths(names: list[str]) -> dict[str, str | None]:
    """探测给定可执行文件在 PATH 中的完整路径；未找到时返回 None。"""
    return {n: shutil.which(n) for n in names}


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
    rows = [r.strip() for r in text.splitlines() if r.strip()]
    if not rows:
        return caps

    frees: list[int] = []
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
    caps.vram_free_mb = frees
    caps.gpu_count = len(frees)
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
