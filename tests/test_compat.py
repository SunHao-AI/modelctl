#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_compat.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 能力检测框架测试
# ===============================================================================

"""能力检测框架单元测试（GpuSpec / ModelSpec / cc_major）。"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import modelctl.core.compat_rules  # noqa: F401 —— 导入即注册
from modelctl.core import compat as _compat_module
from modelctl.core.capabilities import Capabilities
from modelctl.core.compat import (
    CompatIssue,
    CompatRule,
    EnvSpec,
    GpuSpec,
    ModelSpec,
    _resolvable_cuda_libs,
    _spec_matches,
    apply_compat,
    cc_major,
    register_rule,
    run_compat,
)
from modelctl.engines.base import RequirementError


@pytest.fixture(autouse=True)
def _reset_rules() -> None:
    """每个测试前重置规则注册表：清空测试内注册的规则，并重新导入内置规则（导入即注册）。"""
    _compat_module._RULES.clear()
    importlib.reload(modelctl.core.compat_rules)


@pytest.fixture(autouse=True)
def _set_env_vars(monkeypatch) -> None:
    """固定 HF_HOME / MODELSCOPE_CACHE，使 env_var_missing 规则判定不依赖宿主机环境（确定性）。"""
    monkeypatch.setenv("HF_HOME", "/tmp/hf")
    monkeypatch.setenv("MODELSCOPE_CACHE", "/tmp/modelscope")


def test_cc_major_parsing():
    assert cc_major("8.9") == 8
    assert cc_major("12.0") == 12
    assert cc_major("") is None
    assert cc_major("abc") is None


def test_gpu_spec_from_caps():
    caps = Capabilities(
        gpu_count=8,
        compute_capability="8.9",
        gpu_name="RTX 5880",
        vram_total_mb=49152,
        vram_free_mb=[100, 200],
    )
    gpu = GpuSpec.from_caps(caps)
    assert gpu.cc_major == 8
    assert gpu.arch_family == "Ampere/Ada"
    assert gpu.gpu_count == 8
    assert gpu.vram_free_mb == [100, 200]


def test_gpu_spec_arch_family_unknown():
    assert GpuSpec(cc="").arch_family == "unknown"


def test_model_spec_from_local(tmp_path):
    (tmp_path / "config.json").write_text(
        '{"architectures": ["DeepseekV4ForCausalLM"], "model_type": "deepseek_v4", '
        '"quantization_config": {"quant_method": "deepseek_v4_fp8"}}',
        encoding="utf-8",
    )
    m = ModelSpec.from_local("vllm", tmp_path)
    assert m.source == "local"
    assert m.is_deepseek_v4
    assert "fp8" in m.quantization


def test_model_spec_from_local_missing_config(tmp_path):
    m = ModelSpec.from_local("vllm", tmp_path)
    assert m.source == "local"
    assert not m.is_deepseek_v4
    assert m.quantization == ""


def test_model_spec_from_local_bad_json(tmp_path):
    (tmp_path / "config.json").write_text("{ not json", encoding="utf-8")
    m = ModelSpec.from_local("vllm", tmp_path)
    assert not m.is_deepseek_v4


def test_model_spec_from_id_detects_deepseek_v4():
    m = ModelSpec.from_id("vllm", "deepseek-ai/DeepSeek-V4-Flash")
    assert m.source == "id"
    assert m.is_deepseek_v4


def test_model_spec_from_id_download_id():
    m = ModelSpec.from_id("vllm", "", "deepseek-ai/DeepSeek-V4-Flash")
    assert m.is_deepseek_v4
    assert m.name_hint == "deepseek-ai/DeepSeek-V4-Flash"


def test_is_deepseek_v4_model_type_branch():
    """model_type 分支独立成立：不依赖 architectures/name_hint。"""
    m = ModelSpec(engine="vllm", architectures=(), model_type="deepseek_v4")
    assert m.is_deepseek_v4


def test_is_deepseek_v4_false_for_other_model():
    """非 DeepSeek 模型（如 Qwen）反向断言：is_deepseek_v4 为 False。"""
    m = ModelSpec(engine="vllm", name_hint="Qwen/Qwen3-32B")
    assert not m.is_deepseek_v4


def test_model_spec_from_id_quantization():
    m = ModelSpec.from_id("vllm", "Qwen/Qwen3-32B", quantization="fp8")
    assert "fp8" in m.quantization


def _fake_site_packages(tmp_path) -> Path:
    """构造虚拟 site-packages：vllm METADATA（含 torch 约束）、nvidia cudnn 包与假 .so。"""
    sp = tmp_path / "site-packages"
    vllm_dist = sp / "vllm-0.27.1.dist-info"
    vllm_dist.mkdir(parents=True)
    (vllm_dist / "METADATA").write_text(
        "Name: vllm\nVersion: 0.27.1\nRequires-Dist: torch==2.13.0\nRequires-Dist: xgrammar>=0.2.3\n",
        encoding="utf-8",
    )
    torch_dist = sp / "torch-2.9.1.dist-info"
    torch_dist.mkdir(parents=True)
    (torch_dist / "METADATA").write_text("Name: torch\nVersion: 2.9.1\n", encoding="utf-8")
    cudnn = sp / "nvidia" / "cudnn" / "lib"
    cudnn.mkdir(parents=True)
    (cudnn / "libcudnn.so.9").write_bytes(b"")
    return sp


def test_env_spec_metadata(tmp_path):
    env = EnvSpec.from_env(site_packages=_fake_site_packages(tmp_path))
    assert env.packages["torch"] == "2.9.1"
    assert env.packages["vllm"] == "0.27.1"
    assert env.wheel_requires["vllm"]["torch"] == "==2.13.0"
    assert env.wheel_requires["vllm"]["xgrammar"] == ">=0.2.3"


def test_env_spec_nvidia_so(tmp_path):
    env = EnvSpec.from_env(site_packages=_fake_site_packages(tmp_path))
    assert "nvidia/cudnn/lib/libcudnn.so.9" in env.nvidia_so
    assert "libcudnn.so.9" in env.cuda_libs_resolvable  # venv 内 nvidia 库并入可解析集


def test_env_spec_empty_site_packages(tmp_path):
    env = EnvSpec.from_env(site_packages=tmp_path / "nonexistent")
    assert env.packages == {}
    assert env.wheel_requires == {}
    assert env.nvidia_so == set()


def test_env_spec_env_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HOME", "/data/hf")
    monkeypatch.setenv("MODEL_ROOT", "")
    env = EnvSpec.from_env(site_packages=tmp_path)
    assert env.env_vars["HF_HOME"] == "/data/hf"
    assert env.env_vars["MODEL_ROOT"] == ""


def test_resolvable_cuda_libs_glibc_ldconfig(monkeypatch):
    """glibc 系统 ldconfig -p 行含架构注释（如 (libc6,x86-64)），应解析出库名而非注释。"""

    class _FakeCompleted:
        returncode = 0
        stdout = "libcudart.so.13 (libc6,x86-64) => /lib/x86_64-linux-gnu/libcudart.so.13\n"
        stderr = ""

    monkeypatch.setattr(
        "modelctl.core.compat.subprocess.run", lambda *args, **kwargs: _FakeCompleted()
    )
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    names, known = _resolvable_cuda_libs()
    assert known is True
    assert names == {"libcudart.so.13"}


def test_env_spec_libs_resolvable_unknown_without_ldconfig(monkeypatch, tmp_path):
    """ldconfig 不可用且 Windows 兜底（nvidia-smi/PATH .dll）也空时 libs_resolvable_known 应为 False。"""

    def _raise_oserror(*args, **kwargs):
        raise OSError("command not found")

    monkeypatch.setattr("modelctl.core.compat.subprocess.run", _raise_oserror)
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    # Windows 兜底被显式 mock 为空，确保不论 OS 都进入"未知"分支
    monkeypatch.setattr(
        "modelctl.core.compat._resolvable_cuda_libs_windows", lambda: (set(), False),
    )
    env = EnvSpec.from_env(site_packages=tmp_path)
    assert env.libs_resolvable_known is False


def _rule(rule_id: str, issue: CompatIssue | None):
    return CompatRule(id=rule_id, engines=("vllm",), check=lambda g, e, m: issue)


def test_run_compat_filters_by_engine():
    register_rule(_rule("r1", CompatIssue("block", "r1", "x")))
    issues = run_compat("vllm", GpuSpec(), EnvSpec(), None)
    assert [i.rule_id for i in issues] == ["r1"]
    # 假引擎名不在任何内置规则 engines 内，验证引擎过滤（避免受内置 env_var_missing 等规则干扰）
    assert run_compat("fake-engine", GpuSpec(), EnvSpec(), None) == []


def test_run_compat_sorts_block_first():
    register_rule(_rule("d1", CompatIssue("degrade", "d1", "x")))
    register_rule(_rule("b1", CompatIssue("block", "b1", "x")))
    issues = run_compat("vllm", GpuSpec(), EnvSpec(), None)
    assert [i.level for i in issues] == ["block", "degrade"]


def test_register_rule_duplicate_raises():
    register_rule(_rule("dup", None))
    try:
        register_rule(_rule("dup", None))
        raise AssertionError("应抛 ValueError")
    except ValueError:
        pass


def test_apply_compat_block_raises():
    issues = [CompatIssue("degrade", "d", "警告a"), CompatIssue("block", "b", "阻断b")]
    try:
        apply_compat("ds4", "vllm", [], issues)
        raise AssertionError("应抛 RequirementError")
    except RequirementError as e:
        assert "ds4" in str(e) and "阻断b" in str(e)


def test_apply_compat_degrade_writes_warnings():
    warnings: list[str] = []
    apply_compat("ds4", "vllm", warnings, [CompatIssue("degrade", "d", "警告a")])
    assert warnings == ["[d] 警告a"]


def test_spec_matches():
    assert _spec_matches("==2.13.0", "2.13.0")
    assert not _spec_matches("==2.13.0", "2.9.1")
    assert _spec_matches(">=0.2.3", "0.2.3")
    assert _spec_matches(">=0.2.3", "0.3.0")
    assert _spec_matches("garbage", "1.0")  # 无法解析不误报


def test_spec_matches_pep440_local_label():
    """PEP 440 local 标签（2.13.0+cu128）应与基础版本分段比较视为匹配，且不崩溃。"""
    assert _spec_matches("==2.13.0", "2.13.0+cu128")
    assert _spec_matches(">=0.2.3", "0.2.3+cu130")


def test_spec_matches_not_equal_uses_segmented_compare():
    """!= 分支应与 == 一致采用版本分段比较，而非字符串比较。"""
    assert _spec_matches("!=1.0.0", "1.0.1")
    assert not _spec_matches("!=1.0.0", "1.0.0")
    # local 标签（1.0.0+cu128）：字符串比较会判 != 成立，分段比较剥标签后判相等 → 锁定分段实现
    assert not _spec_matches("!=1.0.0", "1.0.0+cu128")


def _run(engine: str, cc: str, model: ModelSpec | None):
    return run_compat(engine, GpuSpec(cc=cc), EnvSpec(), model)


def _ds4(engine: str = "vllm") -> ModelSpec:
    return ModelSpec(engine=engine, source="id", name_hint="deepseek-ai/DeepSeek-V4-Flash")


def test_deepseek_v4_mhc_block_on_ada():
    issues = _run("vllm", "8.9", _ds4())
    assert any(i.rule_id == "deepseek_v4_mhc" and i.level == "block" for i in issues)


def test_deepseek_v4_mhc_block_on_sm120():
    issues = _run("sglang", "12.0", _ds4("sglang"))
    assert any(i.rule_id == "deepseek_v4_mhc" and i.level == "block" for i in issues)


def test_deepseek_v4_mhc_allowed_on_hopper_blackwell_dc():
    assert _run("vllm", "9.0", _ds4()) == []
    assert _run("vllm", "10.0", _ds4()) == []


def test_deepseek_v4_mhc_skips_when_cc_unknown():
    assert _run("vllm", "", _ds4()) == []


def test_deepseek_v4_mhc_not_applicable_to_other_model():
    assert _run("vllm", "8.9", ModelSpec(engine="vllm", name_hint="Qwen/Qwen3-32B")) == []


def test_fp8_quant_cc():
    m = ModelSpec(engine="vllm", name_hint="Qwen/Qwen3-32B", quantization="fp8")
    assert any(i.rule_id == "fp8_quant_cc" for i in _run("vllm", "7.5", m))
    assert _run("vllm", "8.9", m) == []
    assert _run("vllm", "", m) == []  # CC 未知跳过
    assert _run("vllm", "7.5", ModelSpec(engine="vllm", name_hint="m", quantization="awq")) == []


def test_fp4_quant_blackwell():
    m = ModelSpec(engine="vllm", name_hint="m", quantization="fp4")
    assert any(i.rule_id == "fp4_quant_blackwell" for i in _run("vllm", "8.9", m))
    assert _run("vllm", "10.0", m) == []
    assert _run("vllm", "12.0", m) == []


def test_fp4_quant_blackwell_skips_when_cc_unknown():
    m = ModelSpec(engine="vllm", name_hint="m", quantization="fp4")
    assert _run("vllm", "", m) == []  # CC 未知跳过


def test_fp4_quant_blackwell_not_registered_for_sglang():
    """fp4 规则 engines 仅 vllm：sglang 引擎不触发。"""
    m = ModelSpec(engine="sglang", name_hint="m", quantization="fp4")
    assert _run("sglang", "8.9", m) == []


def test_fp8_quant_cc_triggers_on_sglang():
    """fp8 规则注册了 sglang 引擎：CC 7.5 下应触发 block。"""
    m = ModelSpec(engine="sglang", name_hint="Qwen/Qwen3-32B", quantization="fp8")
    assert any(i.rule_id == "fp8_quant_cc" for i in _run("sglang", "7.5", m))


def test_fp8_quant_cc_skips_when_cc_unparseable():
    """非空但无法解析的 CC（如 "abc"）应视为 CC 未知跳过，不误报 block。"""
    m = ModelSpec(engine="vllm", name_hint="m", quantization="fp8")
    assert _run("vllm", "abc", m) == []


def _env(tmp_path, vllm_reqs="", packages=None, nvidia_missing=False):
    sp = tmp_path / "sp"
    (sp / "vllm-0.27.1.dist-info").mkdir(parents=True)
    (sp / "vllm-0.27.1.dist-info" / "METADATA").write_text(
        f"Name: vllm\nVersion: 0.27.1\n{vllm_reqs}", encoding="utf-8"
    )
    for pkg, ver in (packages or {"torch": "2.9.1"}).items():
        dist = sp / f"{pkg}-{ver}.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(f"Name: {pkg}\nVersion: {ver}\n", encoding="utf-8")
    if nvidia_missing:
        # RECORD 声明 libcudnn.so.9 但磁盘缺失
        dist = sp / "nvidia_cudnn_cu13-9.20.0.48.dist-info"
        dist.mkdir(parents=True)
        (dist / "RECORD").write_text("nvidia/cudnn/lib/libcudnn.so.9,,", encoding="utf-8")
    return EnvSpec.from_env(site_packages=sp)


def test_vllm_torch_abi_block(tmp_path):
    env = _env(tmp_path, "Requires-Dist: torch==2.13.0\n")
    issues = run_compat("vllm", GpuSpec(), env, None)
    assert any(i.rule_id == "vllm_torch_abi" and i.level == "block" and "2.13.0" in i.reason for i in issues)


def test_vllm_torch_abi_pass_when_matched(tmp_path):
    env = _env(tmp_path, "Requires-Dist: torch==2.9.1\n", packages={"torch": "2.9.1"})
    assert run_compat("vllm", GpuSpec(), env, None) == []


def test_vllm_torch_abi_skip_when_no_req(tmp_path):
    assert run_compat("vllm", GpuSpec(), _env(tmp_path, ""), None) == []


def test_nvidia_pkg_complete_block(tmp_path):
    env = _env(tmp_path, nvidia_missing=True)
    issues = run_compat("vllm", GpuSpec(), env, None)
    assert any(i.rule_id == "nvidia_pkg_complete" and i.level == "block" for i in issues)


def test_nvidia_pkg_complete_pass_when_present(tmp_path):
    env = EnvSpec.from_env(site_packages=tmp_path)  # 无 nvidia dist-info
    assert run_compat("vllm", GpuSpec(), env, None) == []


def test_nvidia_pkg_complete_pass_when_so_matches(tmp_path):
    """RECORD 声明的 .so 与磁盘实际存在匹配 → 无 issue。"""
    sp = tmp_path / "sp"
    dist = sp / "nvidia_cudnn_cu13-9.20.0.48.dist-info"
    dist.mkdir(parents=True)
    (dist / "RECORD").write_text("nvidia/cudnn/lib/libcudnn.so.9,,", encoding="utf-8")
    lib = sp / "nvidia" / "cudnn" / "lib" / "libcudnn.so.9"
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b"")
    env = EnvSpec.from_env(site_packages=sp)
    assert run_compat("vllm", GpuSpec(), env, None) == []


def test_nvidia_pkg_complete_pass_when_so_outside_nvidia_dir(tmp_path):
    """RECORD 声明的 .so 位于 nvidia/ 之外（如 cudnn/、nvidia_cutlass_dsl/）但磁盘存在 → 不误报。"""
    sp = tmp_path / "sp"
    dist = sp / "nvidia_cudnn_cu13-9.20.0.48.dist-info"
    dist.mkdir(parents=True)
    (dist / "RECORD").write_text(
        "cudnn/_compiled_module.cpython-312-x86_64-linux-gnu.so,,\n"
        "nvidia_cutlass_dsl/lib/libcuda_dialect_runtime.so,,",
        encoding="utf-8",
    )
    (sp / "cudnn").mkdir(parents=True)
    (sp / "cudnn" / "_compiled_module.cpython-312-x86_64-linux-gnu.so").write_bytes(b"")
    (sp / "nvidia_cutlass_dsl" / "lib").mkdir(parents=True)
    (sp / "nvidia_cutlass_dsl" / "lib" / "libcuda_dialect_runtime.so").write_bytes(b"")
    env = EnvSpec.from_env(site_packages=sp)
    assert run_compat("vllm", GpuSpec(), env, None) == []


def test_nvidia_pkg_complete_block_when_missing_outside_nvidia_dir(tmp_path):
    """RECORD 声明的 .so 在 nvidia/ 之外且磁盘确实缺失 → 仍应 block。"""
    sp = tmp_path / "sp"
    dist = sp / "nvidia_cudnn_cu13-9.20.0.48.dist-info"
    dist.mkdir(parents=True)
    (dist / "RECORD").write_text(
        "cudnn/_compiled_module.cpython-312-x86_64-linux-gnu.so,,", encoding="utf-8"
    )
    env = EnvSpec.from_env(site_packages=sp)
    issues = run_compat("vllm", GpuSpec(), env, None)
    assert any(i.rule_id == "nvidia_pkg_complete" and i.level == "block" for i in issues)


@pytest.mark.parametrize(
    ("pkg", "version", "lib"),
    [
        ("nvidia-cuda-runtime", "13.0.96", "libcudart.so.13"),
        ("nvidia-cudnn-cu13", "9.20.0.48", "libcudnn.so.9"),
        ("nvidia-nccl-cu13", "2.32.0", "libnccl.so.2"),
    ],
)
def test_cuda_lib_resolvable_block(tmp_path, pkg, version, lib):
    env = _env(tmp_path, packages={pkg: version})
    env.cuda_libs_resolvable = set()  # 模拟库不可解析
    env.libs_resolvable_known = True
    issues = run_compat("vllm", GpuSpec(), env, None)
    assert any(i.rule_id == "cuda_lib_resolvable" and lib in i.reason for i in issues)


def test_cuda_lib_resolvable_skip_when_unknown(tmp_path):
    env = _env(tmp_path, packages={"nvidia-cuda-runtime": "13.0.96"})
    env.libs_resolvable_known = False
    assert run_compat("vllm", GpuSpec(), env, None) == []


def test_engine_dep_missing(tmp_path):
    env = _env(tmp_path, "Requires-Dist: xgrammar>=0.2.3\n", packages={"xgrammar": "0.1.0"})
    issues = run_compat("vllm", GpuSpec(), env, None)
    assert any(i.rule_id == "engine_dep_missing" and i.level == "block" for i in issues)


def test_engine_dep_missing_pass_when_matched(tmp_path):
    """依赖约束与已装版本匹配 → 无 issue。"""
    env = _env(tmp_path, "Requires-Dist: xgrammar>=0.2.3\n", packages={"xgrammar": "0.3.0"})
    assert run_compat("vllm", GpuSpec(), env, None) == []


def test_engine_dep_missing_skip_when_no_req(tmp_path):
    """vllm 无依赖约束 → 无 issue。"""
    assert run_compat("vllm", GpuSpec(), _env(tmp_path, ""), None) == []


def test_env_var_missing_degrade(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("MODELSCOPE_CACHE", raising=False)
    env = EnvSpec.from_env(site_packages=tmp_path)
    issues = run_compat("llamacpp", GpuSpec(), env, None)
    assert any(i.rule_id == "env_var_missing" and i.level == "degrade" for i in issues)
