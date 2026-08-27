#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_compat_flow.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 能力检测集成测试
# ===============================================================================

"""能力检测两段式集成测试。"""

import os
import sys
from pathlib import Path

import modelctl.core.compat_rules  # noqa: F401 —— 导入即注册
from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import load_profile
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError
from tests.test_engines_vllm import _stub_venv

CAPS8 = Capabilities(gpu_count=8, compute_capability="8.9", binaries={"vllm": True})


def _write(tmp_path, text, name="m.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return load_profile(name[:-5], tmp_path)


def test_vllm_preflight_blocks_deepseek_v4_before_download(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(
        tmp_path,
        "name: ds4\nengine: vllm\nport: 8000\nvllm:\n  model: deepseek-ai/DeepSeek-V4-Flash\n",
    )
    adapter = get_adapter("vllm")(p, CAPS8)
    try:
        adapter.check_requirements()
        raise AssertionError("应抛 RequirementError")
    except RequirementError as e:
        assert "deepseek_v4_mhc" in str(e) and "ds4" in str(e)


def test_vllm_preflight_blocks_torch_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", "/x")
    # 虚拟 site-packages：vllm 要求 torch==2.13.0，已装 2.9.1
    import modelctl.core.compat as compat

    sp = tmp_path / "sp"
    (sp / "vllm-0.27.1.dist-info").mkdir(parents=True)
    (sp / "vllm-0.27.1.dist-info" / "METADATA").write_text(
        "Name: vllm\nVersion: 0.27.1\nRequires-Dist: torch==2.13.0\n", encoding="utf-8"
    )
    (sp / "torch-2.9.1.dist-info").mkdir(parents=True)
    (sp / "torch-2.9.1.dist-info" / "METADATA").write_text("Name: torch\nVersion: 2.9.1\n", encoding="utf-8")
    monkeypatch.setattr(compat, "_current_site_packages", lambda: sp)

    p = _write(tmp_path, "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: Qwen/Qwen3-32B\n")
    _stub_venv(tmp_path, monkeypatch, "vllm")
    adapter = get_adapter("vllm")(p, CAPS8)
    try:
        adapter.check_requirements()
        raise AssertionError("应抛 RequirementError")
    except RequirementError as e:
        assert "vllm_torch_abi" in str(e)


def test_vllm_post_download_precise_check(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch, "vllm")
    # 精检：目录名不含 DeepSeek 特征（预检 is_deepseek_v4=False 放行），
    # 但本地 config.json 的 architectures 暴露 DeepSeek-V4 → pre_start 精检拦截
    model_dir = tmp_path / "m1"
    model_dir.mkdir()
    (model_dir / "config.json").write_text(
        '{"architectures": ["DeepseekV4ForCausalLM"]}', encoding="utf-8"
    )
    p = _write(
        tmp_path,
        f"name: ds4\nengine: vllm\nport: 8000\nvllm:\n  model: {model_dir}\n",
    )
    adapter = get_adapter("vllm")(p, CAPS8)
    adapter.check_requirements()  # 预检：name_hint 为路径 m1，不含 deepseek-v4 特征 → 放行
    try:
        adapter.pre_start()
        raise AssertionError("精检应抛 RequirementError")
    except RequirementError as e:
        assert "deepseek_v4_mhc" in str(e)


# === Task 7：run_compat_checks 按引擎 venv 扫描 site-packages 的集成断言 ===


def _venv_site_path(venv_root: Path, engine: str) -> Path:
    """venv 内 site-packages 目录路径（与 envs.engine_site_packages 跨平台布局一致）。"""
    if os.name == "nt":
        return venv_root / engine / "Lib" / "site-packages"
    return venv_root / engine / "lib" / "python3.12" / "site-packages"


def test_run_compat_checks_scans_engine_venv_site_packages(tmp_path, monkeypatch):
    """托管 + venv 已建：run_compat_checks 应扫 venv 内 site-packages 而非当前解释器的。

    在 tmp 的受控 venv site-packages 写入唯一 fakeflag 包；若误扫宿主的 _current_site_packages，
    EnvSpec.packages 与 site_packages 均不会命中该 fakeflag 包，断言必然失败。
    """
    venv_root = _stub_venv(tmp_path, monkeypatch, "vllm")
    sp = _venv_site_path(venv_root, "vllm")
    sp.mkdir(parents=True, exist_ok=True)
    (sp / "fakeflag-9.9.9.dist-info").mkdir()
    (sp / "fakeflag-9.9.9.dist-info" / "METADATA").write_text("Name: fakeflag\nVersion: 9.9.9\n", encoding="utf-8")

    # 屏蔽 env_var_missing 降级规则（本测试只关心 site_packages 指向，避免宿主环境变量差异）
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    monkeypatch.setenv("HF_HOME", "/tmp/hf")
    monkeypatch.setenv("MODELSCOPE_CACHE", "/tmp/ms")
    # 仅扫 venv 受控目录时返回唯一 fakeflag，其余任何 sp（如宿主）返回空 → 隔离真实磁盘
    monkeypatch.setattr(
        "modelctl.core.compat._read_installed_packages",
        lambda d: {"fakeflag": "9.9.9"} if d == sp else {},
    )
    monkeypatch.setattr("modelctl.core.compat._read_wheel_requires", lambda d: {})
    monkeypatch.setattr("modelctl.core.compat._scan_nvidia_so", lambda d: set())

    p = _write(tmp_path, "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: Qwen/Qwen3-32B\n")
    adapter = get_adapter("vllm")(p, Capabilities(gpu_count=0, compute_capability="", binaries={"vllm": True}))
    adapter.run_compat_checks()
    env = adapter._compat_env
    # 指向 venv 内 site-packages（而非宿主解释器 site-packages）
    assert env.site_packages == sp
    # venv 内包被读到；宿主包未泄漏进 EnvSpec
    assert env.packages.get("fakeflag") == "9.9.9"


def test_run_compat_checks_falls_back_to_current_env_when_no_venv(tmp_path, monkeypatch):
    """未建环境：engine_site_packages 返回 None → EnvSpec.from_env() 无参走 _current_site_packages 回退。"""
    import modelctl.core.envs as envs_mod

    monkeypatch.setattr(envs_mod, "VENV_ROOT", tmp_path / "missing-venvs")  # 无 .venvs → has_env False
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    monkeypatch.setenv("HF_HOME", "/tmp/hf")
    monkeypatch.setenv("MODELSCOPE_CACHE", "/tmp/ms")
    monkeypatch.setattr("modelctl.core.compat._read_installed_packages", lambda d: {})
    monkeypatch.setattr("modelctl.core.compat._read_wheel_requires", lambda d: {})
    monkeypatch.setattr("modelctl.core.compat._scan_nvidia_so", lambda d: set())

    p = _write(tmp_path, "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: Qwen/Qwen3-32B\n")
    adapter = get_adapter("vllm")(p, Capabilities(gpu_count=0, compute_capability="", binaries={"vllm": True}))
    adapter.run_compat_checks()
    env = adapter._compat_env
    # 回退到当前解释器 site-packages（与 _current_site_packages 同源：sys.path 含 "site-packages" 的条目）
    expected = None
    for path in sys.path:
        if "site-packages" in path:
            expected = Path(path)
            break
    assert env.site_packages == expected
    # 回退后 site_packages 不是 venv 受控目录（与托管路径无关）
    assert env.site_packages != _venv_site_path(tmp_path / "missing-venvs", "vllm")
