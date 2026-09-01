#!/usr/bin/env python3
# -*- coding: utf-8 -*-  # noqa: UP009
# ===============================================================================
# @File   : tests/test_core_capabilities.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/8/27 10:00
# @Desc   : capabilities.py 托管引擎 venv 探测测试
# ===============================================================================

"""core/capabilities.py 测试：托管引擎（vllm/sglang）走 has_env 判定与 venv 路径，
非托管引擎维持 shutil.which 逻辑；提示语指向 modelctl env setup。"""

from __future__ import annotations

import os
from pathlib import Path

import modelctl.core.envs as envs_mod
from modelctl.core.capabilities import (
    ENGINE_BINARIES,
    ENGINE_INSTALL_HINTS,
    binary_paths,
    probe,
    which_binaries,
)


def _redirect(tmp_path: Path, monkeypatch) -> Path:
    """把 envs.VENV_ROOT 重定向到 tmp_path/.venvs 并返回该根目录。"""
    monkeypatch.setattr(envs_mod, "VENV_ROOT", tmp_path / ".venvs")
    return tmp_path / ".venvs"


def _make_env(venv_root: Path, engine: str, windows: bool) -> None:
    bin_dir = "Scripts" if windows else "bin"
    exe = "python.exe" if windows else "python"
    d = venv_root / engine / bin_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / exe).write_bytes(b"fake")


def _venv_bin_name(engine: str, name: str) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    return exe


def test_engine_binaries_list_kept():
    """ENGINE_BINARIES 保持已注册引擎列表（新增 aphrodite/lmdeploy/tensorrt_llm/tokenspeed）。"""
    assert ENGINE_BINARIES == [
        "ollama", "vllm", "sglang", "unsloth", "llamacpp",
        "aphrodite", "lmdeploy", "tensorrt_llm", "tokenspeed",
    ]


def test_which_binaries_vllm_env_present(tmp_path, monkeypatch):
    """has_env 为 True 时，which_binaries(["vllm"]) 返回 True。"""
    root = _redirect(tmp_path, monkeypatch)
    _make_env(root, "vllm", windows=(os.name == "nt"))
    result = which_binaries(["vllm"])
    assert result == {"vllm": True}
    assert result["vllm"] is True


def test_which_binaries_vllm_env_absent(tmp_path, monkeypatch):
    """has_env 为 False 时，which_binaries(["vllm"]) 返回 False。"""
    _redirect(tmp_path, monkeypatch)
    result = which_binaries(["vllm"])
    assert result == {"vllm": False}


def test_which_binaries_sglang_env_present(tmp_path, monkeypatch):
    """sglang 同样以 has_env 判定。"""
    root = _redirect(tmp_path, monkeypatch)
    _make_env(root, "sglang", windows=(os.name == "nt"))
    result = which_binaries(["sglang"])
    assert result == {"sglang": True}


def test_binary_paths_vllm_env_present(tmp_path, monkeypatch):
    """has_env True 时，binary_paths 返回 venv 内路径（与 engine_bin 一致）。"""
    root = _redirect(tmp_path, monkeypatch)
    _make_env(root, "vllm", windows=(os.name == "nt"))
    expected = root / "vllm" / ("Scripts" if os.name == "nt" else "bin") / _venv_bin_name("vllm", "vllm")
    result = binary_paths(["vllm"])
    assert result == {"vllm": str(expected)}
    assert result["vllm"] == str(envs_mod.engine_bin("vllm", "vllm"))


def test_binary_paths_vllm_env_absent(tmp_path, monkeypatch):
    """has_env False 时，binary_paths 返回 None。"""
    _redirect(tmp_path, monkeypatch)
    result = binary_paths(["vllm"])
    assert result == {"vllm": None}


def test_which_binaries_unmanaged_uses_shutil_which(tmp_path, monkeypatch):
    """非托管引擎（ollama）仍走 shutil.which 路径（mock 返回 fake path 时为 True）。"""
    _redirect(tmp_path, monkeypatch)
    monkeypatch.setattr("modelctl.core.capabilities.shutil.which", lambda n: "/fake/bins/" + n)
    result = which_binaries(["ollama"])
    assert result == {"ollama": True}


def test_binary_paths_unmanaged_uses_shutil_which(tmp_path, monkeypatch):
    """非托管引擎（ollama）仍走 shutil.which 路径（返回 fake path）。"""
    _redirect(tmp_path, monkeypatch)
    monkeypatch.setattr("modelctl.core.capabilities.shutil.which", lambda n: "/fake/bins/" + n)
    result = binary_paths(["ollama"])
    assert result == {"ollama": "/fake/bins/ollama"}


def test_which_binaries_unmanaged_which_none(tmp_path, monkeypatch):
    """非托管引擎 shutil.which 返回 None 时，which_binaries 为 False。"""
    _redirect(tmp_path, monkeypatch)
    monkeypatch.setattr("modelctl.core.capabilities.shutil.which", lambda n: None)
    result = which_binaries(["ollama"])
    assert result == {"ollama": False}


def test_engine_install_hints_vllm_sglang():
    """vllm / sglang 提示语包含 modelctl env setup。"""
    assert "modelctl env setup" in ENGINE_INSTALL_HINTS["vllm"]
    assert "modelctl env setup" in ENGINE_INSTALL_HINTS["sglang"]


def test_engine_install_hints_tensorrt_llm():
    """tensorrt_llm 提示语指向 modelctl env setup tensorrt_llm。"""
    assert "modelctl env setup tensorrt_llm" in ENGINE_INSTALL_HINTS["tensorrt_llm"]


def test_probe_managed_engines_absent_by_default(tmp_path, monkeypatch):
    """未建设 venv 时，probe() 默认对托管引擎返回 False，与现状（5 项全 False）一致。"""
    _redirect(tmp_path, monkeypatch)
    monkeypatch.setattr("modelctl.core.capabilities.shutil.which", lambda n: None)
    caps = probe(nvidia_smi_output="")
    assert caps.binaries["vllm"] is False
    assert caps.binaries["sglang"] is False
    assert caps.binaries["ollama"] is False
    assert caps.binaries["unsloth"] is False
    assert caps.binaries["llamacpp"] is False
    assert caps.binary_paths["vllm"] is None
    assert caps.binary_paths["sglang"] is None


def test_probe_managed_engine_present(tmp_path, monkeypatch):
    """建设 venv 后，probe() 对托管引擎返回真实 venv 路径。"""
    root = _redirect(tmp_path, monkeypatch)
    _make_env(root, "vllm", windows=(os.name == "nt"))
    monkeypatch.setattr("modelctl.core.capabilities.shutil.which", lambda n: None)
    caps = probe(nvidia_smi_output="")
    assert caps.binaries["vllm"] is True
    assert caps.binary_paths["vllm"] == str(envs_mod.engine_bin("vllm", "vllm"))
