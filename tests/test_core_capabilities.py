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
    ENGINE_PROBE_HINT_BOTH_SET,
    ENGINE_PROBE_HINT_VENV_ONLY,
    binary_paths,
    docker_ready,
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


def test_engine_install_hints_contains_env_setup():
    """所有 MANAGED 提示语必须含 `modelctl env setup <engine>` 子串。

    2026-09 反转后 token 微调过前缀（如 "，venv 兜底路径："），但**安装动词 + 引擎名**
    子串必须保留——UI/CLI 多处用 startswith / 子串匹配该 hint 作为锚点，
    不能因措辞优化破坏。
    """
    for name in ("vllm", "sglang", "aphrodite", "lmdeploy", "tokenspeed", "tensorrt_llm"):
        hint = ENGINE_INSTALL_HINTS.get(name, "")
        assert "modelctl env setup" in hint, f"{name} 提示语缺少 env setup：{hint!r}"
        assert name in hint, f"{name} 提示语缺少引擎名：{hint!r}"
    # 非托管引擎（ollama / unsloth）保留原始 hint（curl 安装器），不被误判
    assert "curl -fsSL" in ENGINE_INSTALL_HINTS["ollama"]
    assert "curl -fsSL" in ENGINE_INSTALL_HINTS["unsloth"]


def test_engine_install_hints_tensorrt_llm_tokenspeed_venv_fallback_touches_docker_main_path():
    """docker-capable 引擎（vllm/tokenspeed/tensorrt_llm）的 hint 必须明示 docker 主路径。

    2026-09 反转后这些引擎 yaml 写 docker_image 即走容器，venv 是兜底；
    hint 文案须含 "docker 主路径" 字样（或等价表述）告知用户"即使 venv 缺位也可用"。
    """
    for name in ("vllm", "tokenspeed", "tensorrt_llm"):
        hint = ENGINE_INSTALL_HINTS[name]
        assert "venv 兜底" in hint, f"{name} 提示语未说明 venv 兜底语义：{hint!r}"


def test_engine_probe_hints_both_set_and_venv_only():
    """ENGINE_PROBE_HINT_* 必须含 docker 主路径 / venv fallback 关键引用。"""
    # BOTH_SET 用于 venv 缺位但 docker 主路径就绪的场景
    assert "docker_image" in ENGINE_PROBE_HINT_BOTH_SET
    assert "modelctl env setup" in ENGINE_PROBE_HINT_BOTH_SET
    # VENV_ONLY 用于 venv-only 引擎可达性标注
    assert "modelctl env setup" in ENGINE_PROBE_HINT_VENV_ONLY


def test_docker_ready_requires_both_docker_and_nvidia_smi(tmp_path, monkeypatch):
    """docker_ready() = shutil.which(docker) 且 shutil.which(nvidia-smi) 真。"""
    _redirect(tmp_path, monkeypatch)
    # docker ✓ 若 nvidia-smi 缺 → False
    monkeypatch.setattr("modelctl.core.capabilities.shutil.which",
                        lambda n: "/fake/bin/docker" if n == "docker" else None)
    assert docker_ready() is False
    # nvidia-smi ✓ 若 docker 缺 → False
    monkeypatch.setattr("modelctl.core.capabilities.shutil.which",
                        lambda n: "/fake/bin/nvidia-smi" if n == "nvidia-smi" else None)
    assert docker_ready() is False
    # 都在 → True
    monkeypatch.setattr("modelctl.core.capabilities.shutil.which",
                        lambda n: f"/fake/bin/{n}")
    assert docker_ready() is True
    # 都缺 → False
    monkeypatch.setattr("modelctl.core.capabilities.shutil.which", lambda n: None)
    assert docker_ready() is False


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
