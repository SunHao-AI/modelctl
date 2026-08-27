#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_core_envs.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/8/27 10:00
# @Desc   : core/envs.py 引擎专用 venv 路径/探测测试
# ===============================================================================

"""core/envs.py 测试：函数骨架 + 错误分支（TDD，仅覆盖纯路径逻辑，不跑外部命令）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from modelctl.core.envfile import PROJECT_ROOT


def _redirect(tmp_path: Path, monkeypatch) -> Path:
    """把 envs.VENV_ROOT 重定向到 tmp_path/.venvs 并返回该根目录。"""
    import modelctl.core.envs as envs

    monkeypatch.setattr(envs, "VENV_ROOT", tmp_path / ".venvs")
    return tmp_path / ".venvs"


def _make_env(venv_root: Path, engine: str, windows: bool) -> Path:
    bin_dir = "Scripts" if windows else "bin"
    exe = "python.exe" if windows else "python"
    d = venv_root / engine / bin_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / exe).write_bytes(b"fake")
    return venv_root


def test_managed_engines_constant():
    from modelctl.core.envs import MANAGED_ENGINES

    assert MANAGED_ENGINES == ("vllm", "sglang")


def test_path_constants():
    from modelctl.core.envs import ENVS_ROOT, VENV_ROOT

    assert ENVS_ROOT == PROJECT_ROOT / "envs"
    assert VENV_ROOT == PROJECT_ROOT / ".venvs"


def test_engine_bin_windows():
    import modelctl.core.envs as envs

    assert os.name == "nt"
    assert envs.engine_bin("vllm", "vllm") == envs.VENV_ROOT / "vllm" / "Scripts" / "vllm.exe"


def test_engine_bin_linux(monkeypatch):
    import modelctl.core.envs as envs

    monkeypatch.setattr("os.name", "posix")
    assert envs.engine_bin("vllm", "vllm") == envs.VENV_ROOT / "vllm" / "bin" / "vllm"


def test_engine_python_windows():
    import modelctl.core.envs as envs

    assert os.name == "nt"
    assert envs.engine_python("sglang") == envs.VENV_ROOT / "sglang" / "Scripts" / "python.exe"


def test_engine_python_linux(monkeypatch):
    import modelctl.core.envs as envs

    monkeypatch.setattr("os.name", "posix")
    assert envs.engine_python("sglang") == envs.VENV_ROOT / "sglang" / "bin" / "python"


def test_has_env_absent(tmp_path, monkeypatch):
    from modelctl.core.envs import has_env

    _redirect(tmp_path, monkeypatch)
    assert has_env("vllm") is False


def test_has_env_present_windows(tmp_path, monkeypatch):
    from modelctl.core.envs import has_env

    root = _redirect(tmp_path, monkeypatch)
    assert os.name == "nt"
    _make_env(root, "vllm", windows=True)
    assert has_env("vllm") is True


def test_has_env_present_linux(tmp_path, monkeypatch):
    from modelctl.core.envs import has_env

    monkeypatch.setattr("os.name", "posix")
    root = _redirect(tmp_path, monkeypatch)
    _make_env(root, "vllm", windows=False)
    assert has_env("vllm") is True


def test_ensure_env_returns_root_when_exists(tmp_path, monkeypatch):
    from modelctl.core.envs import ensure_env

    root = _redirect(tmp_path, monkeypatch)
    assert os.name == "nt"
    _make_env(root, "vllm", windows=True)
    assert ensure_env("vllm") == root / "vllm"


def test_ensure_env_raises_when_absent(tmp_path, monkeypatch):
    from modelctl.core.envs import EngineEnvError, ensure_env

    _redirect(tmp_path, monkeypatch)
    with pytest.raises(EngineEnvError) as excinfo:
        ensure_env("vllm")
    assert "modelctl env setup vllm" in str(excinfo.value)


def test_unmanaged_engine_rejected():
    import modelctl.core.envs as envs

    with pytest.raises(ValueError):
        envs.engine_python("unknown")
    with pytest.raises(ValueError):
        envs.engine_bin("unknown", "x")
    with pytest.raises(ValueError):
        envs.ensure_env("unknown")


# 注：status() 测试由 Task 2 的失败测试（Step 2.1）补回，届时再实现 status 函数。
