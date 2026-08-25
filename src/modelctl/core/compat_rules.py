#!/usr/bin/env python3
"""core/compat_rules.py — 内置能力检测规则注册（导入即注册）。"""

from __future__ import annotations

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
        reason=f"vllm 要求 torch{req}，当前已装 {installed}（ABI 不匹配）。"
        f"建议执行：MAX_JOBS=4 uv sync --extra vllm 以对齐依赖。",
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
        reason=(
            f"检测到 nvidia 依赖包文件缺失（空壳包）：{', '.join(missing[:5])}。"
            "建议执行：uv pip install --reinstall \"nvidia-cudnn-cu13\" \"nvidia-nccl-cu13\" 等对应包。"
        ),
    )


def _cuda_lib_resolvable_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    if not env.libs_resolvable_known:
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
        reason=(
            f"CUDA 运行库无法解析：{', '.join(missing)}。"
            "请将对应 nvidia 库目录加入 LD_LIBRARY_PATH 或 /etc/ld.so.conf.d/ 后执行 ldconfig。"
        ),
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
        reason="vllm 依赖版本不匹配：" + "；".join(problems) + "。建议执行：MAX_JOBS=4 uv sync --extra vllm 以对齐依赖。",
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
