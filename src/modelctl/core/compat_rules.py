#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/compat_rules.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 能力检测规则注册
# ===============================================================================

"""core/compat_rules.py — 内置能力检测规则注册（导入即注册）。"""

from __future__ import annotations

import os
import re
import sys

from modelctl.core.capabilities import cc_at_least
from modelctl.core.compat import (
    CompatIssue,
    CompatRule,
    EnvSpec,
    GpuSpec,
    ModelSpec,
    _spec_matches,
    register_rule,
)


def _deepseek_v4_mhc_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    if model is None or not model.is_deepseek_v4:
        return None
    major = gpu.cc_major
    if major is None:
        return None  # CC 未知，不误报
    if major in (9, 10):
        return None
    gpu_name = gpu.gpu_name or f"GPU（CC {gpu.cc}）"
    return CompatIssue(
        level="block",
        rule_id="deepseek_v4_mhc",
        reason=(
            f"DeepSeek-V4 的 mHC（HyperConnection）层依赖 DeepGEMM hyperconnection 内核，"
            f"官方仅支持 Hopper/Blackwell DC（计算能力 9.0/10.0），"
            f"当前 GPU 为 {gpu_name}（CC {gpu.cc}）。"
            "如仍需在当前架构部署，可改用 llamacpp 引擎运行 GGUF 版本（models/llamacpp/deepseek-v4-flash.yaml）。"
        ),
    )


def _fp8_quant_cc_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    if model is None or "fp8" not in model.quantization:
        return None
    if not gpu.cc or gpu.cc_major is None:
        return None  # CC 未知或无法解析，不误报
    if cc_at_least(gpu.cc, 8, 9):
        return None
    return CompatIssue(
        level="block",
        rule_id="fp8_quant_cc",
        reason=f"FP8 量化需要计算能力 ≥ 8.9，当前 CC {gpu.cc}。建议改用 bf16 权重或更换 GPU。",
    )


def _fp4_quant_blackwell_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    if model is None or "fp4" not in model.quantization:
        return None
    major = gpu.cc_major
    if major is None:
        return None
    if major in (10, 12):
        return None
    return CompatIssue(
        level="block",
        rule_id="fp4_quant_blackwell",
        reason=f"FP4 量化仅支持 Blackwell（计算能力 10.0/12.0），当前 CC {gpu.cc}。",
    )


def _vllm_torch_abi_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    req = env.wheel_requires.get("vllm", {}).get("torch")
    if not req:
        return None
    installed = env.packages.get("torch")
    if installed is None or _spec_matches(req, installed):
        return None
    return CompatIssue(
        level="block",
        rule_id="vllm_torch_abi",
        reason=f"vllm 要求 torch{req}，当前已装 {installed}（ABI 不匹配）。" f"建议执行：modelctl env setup vllm 以重建引擎 venv 并对齐依赖。",
    )


# vLLM / SGLang 的 KV cache 显存布局与 NCCL 通信内核依赖 CUDA 13 运行库（cu13 wheel 线）。
# torch 的 CUDA 小版本不由 modelctl 选择——它取决于 vllm 钉死的 torch 版本 + 所选 index，
# 而 PyPI 默认 wheel 的 CUDA 构建会随 torch 版本漂移（2.9 前是 cu12x）。因此安装期无法
# 保证，必须在启动前显式校验，避免静默拿到 cu12x 构建后在运行期才炸。
DEFAULT_TORCH_CUDA_MAJOR = 13
TORCH_CUDA_MAJOR_ENV = "MODELCTL_TORCH_CUDA_MAJOR"

# torch 版本 local 标签：2.13.0+cu130 / 2.7.0-cu128（PEP 440 local version）
_CU_TAG_RE = re.compile(r"[+\-]cu(\d+)")
# nvidia wheel 包名后缀：nvidia-nccl-cu13 / nvidia-cudnn-cu12
_CU_PKG_RE = re.compile(r"-cu(\d+)$")


def _cuda_major_of(tag_value: int) -> int:
    """把两种 cu 标签格式统一成 CUDA 主版本。

    - nvidia wheel 后缀是主版本本身：cu13 → 13；
    - torch local 标签是 major*10+minor：cu130（=13.0）→ 13、cu128（=12.8）→ 12。
    """
    return tag_value // 10 if tag_value >= 100 else tag_value


def _required_cuda_major() -> int:
    """期望的 CUDA 主版本；可用 TORCH_CUDA_MAJOR_ENV 覆盖（如需回退 cu12 的旧卡环境）。"""
    raw = (os.environ.get(TORCH_CUDA_MAJOR_ENV) or "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_TORCH_CUDA_MAJOR


def _torch_cuda_major(env: EnvSpec) -> int | None:
    """推断 venv 内 torch 实际链接的 CUDA 主版本，无法判定返回 None。

    判定优先级：
    1. torch 版本 local 标签（`2.13.0+cu130`，来自 PyTorch 官方 index 的显式标注）；
    2. torch 强绑定的 nvidia wheel 包名后缀（cudnn/nccl 二者必随 CUDA 主版本变化）；
    3. 都取不到（CPU-only wheel 或元数据缺失）→ None，交由调用方跳过、不误报。
    """
    tag = _CU_TAG_RE.search(env.packages.get("torch", ""))
    if tag:
        return _cuda_major_of(int(tag.group(1)))
    for pkg in ("nvidia-cudnn", "nvidia-nccl"):
        for name in env.packages:
            if name.startswith(pkg + "-cu"):
                m = _CU_PKG_RE.search(name)
                if m:
                    return _cuda_major_of(int(m.group(1)))
    return None


def _torch_cuda_build_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    if "torch" not in env.packages:
        return None  # 环境不含 torch（llamacpp / ollama 等），不适用
    actual = _torch_cuda_major(env)
    if actual is None:
        return None  # 无法判定 CUDA 构建，不误报
    required = _required_cuda_major()
    if actual == required:
        return None
    return CompatIssue(
        level="block",
        rule_id="torch_cuda_build",
        reason=(
            f"torch 为 cu{actual} 构建（已装 {env.packages['torch']}），需要 cu{required}。"
            f"KV cache 与 NCCL 通信内核依赖 CUDA {required} 运行库。"
            f"若确需以 cu{actual} 运行，可设 {TORCH_CUDA_MAJOR_ENV}={actual} 放宽本检查。"
        ),
    )


def _nvidia_pkg_complete_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    sp = env.site_packages
    if sp is None or not sp.is_dir():
        return None
    missing: list[str] = []
    for record in sorted(sp.glob("nvidia_*.dist-info/RECORD")):
        for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
            rel = line.split(",", 1)[0].replace("\\", "/")
            if rel.endswith(".so") or ".so." in rel:
                # 以磁盘实际存在为准：RECORD 声明的 .so 不一定位于 nvidia/ 下
                # （如 cudnn/、nvidia_cutlass_dsl/ 等顶层包目录），仅与 nvidia/ 扫描集
                # 比对会对这些本就存在的文件产生误报。
                if not (sp / rel).is_file():
                    missing.append(rel)
        if len(missing) >= 5:
            break
    if not missing:
        return None
    return CompatIssue(
        level="block",
        rule_id="nvidia_pkg_complete",
        reason=(f"检测到 nvidia 依赖包文件缺失（空壳包）：{', '.join(missing[:5])}。" '建议执行：uv pip install --reinstall "nvidia-cudnn-cu13" "nvidia-nccl-cu13" 等对应包。'),
    )


def _cuda_lib_resolvable_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    if not env.libs_resolvable_known:
        return None
    if sys.platform.startswith("win") and "libcuda.dll" in env.cuda_libs_resolvable:
        # Windows 路径：nvidia-smi 已确认 GPU 栈存在（libcuda.dll 命中）。
        # torch 的 cudnn/cudart 走 wheel 内带库或 Python 进程内 runtime，
        # 无 LD 等价物，按"视为可解析"放行 .so 名称检查，避免误报 block。
        return None
    needed: set[str] = set()
    for pkg, version in env.packages.items():
        if not version:
            continue  # 空版本防御：避免产出 "libcudart.so." 畸形库名
        if pkg == "nvidia-cuda-runtime":
            needed.add(f"libcudart.so.{version.split('.')[0]}")
        elif pkg.startswith("nvidia-cudnn"):
            needed.add("libcudnn.so.9")
        elif pkg.startswith("nvidia-nccl"):
            needed.add("libnccl.so.2")
    missing = sorted(n for n in needed if n not in env.cuda_libs_resolvable)
    if not missing:
        return None
    return CompatIssue(
        level="block",
        rule_id="cuda_lib_resolvable",
        reason=(f"CUDA 运行库无法解析：{', '.join(missing)}。" "请将对应 nvidia 库目录加入 LD_LIBRARY_PATH 或 /etc/ld.so.conf.d/ 后执行 ldconfig。"),
    )


_DEP_MISMATCH_KEYS = ("xgrammar", "flashinfer-python", "tokenizers", "transformers", "triton")


def _engine_dep_missing_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    reqs = env.wheel_requires.get("vllm", {})
    problems: list[str] = []
    for dep in _DEP_MISMATCH_KEYS:
        req = reqs.get(dep)
        if not req:
            continue
        installed = env.packages.get(dep)
        if installed is None or _spec_matches(req, installed):
            continue
        problems.append(f"{dep}{req}（当前 {installed}）")
    if not problems:
        return None
    return CompatIssue(
        level="block",
        rule_id="engine_dep_missing",
        reason="vllm 依赖版本不匹配：" + "；".join(problems) + "。建议执行：modelctl env setup vllm 以重建引擎 venv 并对齐依赖。",
    )


_ENV_VAR_WARN = ("HF_HOME", "MODELSCOPE_CACHE")


def _env_var_missing_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    if not env.env_vars:
        return None  # 未探测环境变量（env_vars 为空），不误报
    missing = [k for k in _ENV_VAR_WARN if not env.env_vars.get(k)]
    if not missing:
        return None
    return CompatIssue(
        level="degrade",
        rule_id="env_var_missing",
        reason=f"环境变量未设置：{'、'.join(missing)}（将使用默认路径）。",
    )


def _register() -> None:
    register_rule(CompatRule(id="deepseek_v4_mhc", engines=("vllm", "sglang"), check=_deepseek_v4_mhc_check))
    register_rule(CompatRule(id="fp8_quant_cc", engines=("vllm", "sglang"), check=_fp8_quant_cc_check))
    register_rule(CompatRule(id="fp4_quant_blackwell", engines=("vllm",), check=_fp4_quant_blackwell_check))
    register_rule(CompatRule(id="vllm_torch_abi", engines=("vllm",), check=_vllm_torch_abi_check))
    register_rule(CompatRule(id="torch_cuda_build", engines=("vllm", "sglang"), check=_torch_cuda_build_check))
    register_rule(CompatRule(id="nvidia_pkg_complete", engines=("vllm", "sglang"), check=_nvidia_pkg_complete_check))
    register_rule(CompatRule(id="cuda_lib_resolvable", engines=("vllm", "sglang"), check=_cuda_lib_resolvable_check))
    register_rule(CompatRule(id="engine_dep_missing", engines=("vllm",), check=_engine_dep_missing_check))
    register_rule(
        CompatRule(
            id="env_var_missing",
            engines=("vllm", "sglang", "llamacpp", "unsloth", "ollama"),
            check=_env_var_missing_check,
        )
    )


_register()
