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

import modelctl.core.envs as envs_mod
from modelctl.core.envfile import PROJECT_ROOT


def _redirect(tmp_path: Path, monkeypatch) -> Path:
    """把 envs.VENV_ROOT 重定向到 tmp_path/.venvs 并返回该根目录。"""
    monkeypatch.setattr(envs_mod, "VENV_ROOT", tmp_path / ".venvs")
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

    assert MANAGED_ENGINES == ("vllm", "sglang", "aphrodite", "lmdeploy", "tokenspeed", "tensorrt_llm")


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




# === Task 2：setup / remove / status（外部命令层）===


class _RunResult:
    """模拟 subprocess.run 的返回值。"""

    def __init__(self, returncode: int = 0):
        self.returncode = returncode


def test_setup_calls_uv_sync_linux(tmp_path, monkeypatch):
    from modelctl.core.envs import ENVS_ROOT, setup

    monkeypatch.setattr(envs_mod, "_is_linux", lambda: True)
    root = _redirect(tmp_path, monkeypatch)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return _RunResult(0)

    monkeypatch.setattr(envs_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(envs_mod.shutil, "which", lambda name: "uv")

    code = setup("vllm")

    assert code == 0
    assert len(calls) == 1
    call = calls[0]
    # 断言调用了 ["uv", "sync", "--project", str(ENVS_ROOT / "vllm")]
    assert call["cmd"] == ["uv", "sync", "--project", str(ENVS_ROOT / "vllm")]
    # 断言环境变量含 UV_PROJECT_ENVIRONMENT=str(重定向后的 VENV_ROOT / "vllm")
    assert call["kwargs"]["env"]["UV_PROJECT_ENVIRONMENT"] == str(root / "vllm")


def test_setup_raises_on_non_linux(tmp_path, monkeypatch):
    from modelctl.core.envs import EngineEnvError, setup

    _redirect(tmp_path, monkeypatch)
    monkeypatch.setattr(envs_mod, "_is_linux", lambda: False)
    with pytest.raises(EngineEnvError) as excinfo:
        setup("vllm")
    assert "Linux" in str(excinfo.value)
    assert "modelctl env setup vllm" in str(excinfo.value)


def test_setup_unknown_engine_rejected():
    from modelctl.core.envs import setup

    with pytest.raises(ValueError):
        setup("unknown")


def test_setup_uv_not_found(tmp_path, monkeypatch):
    from modelctl.core.envs import EngineEnvError, setup

    _redirect(tmp_path, monkeypatch)
    monkeypatch.setattr(envs_mod, "_is_linux", lambda: True)
    monkeypatch.setattr("modelctl.core.envs.shutil.which", lambda name: None)
    with pytest.raises(EngineEnvError) as excinfo:
        setup("vllm")
    assert "uv" in str(excinfo.value)


def test_remove_calls_rmtree(tmp_path, monkeypatch):
    from modelctl.core.envs import remove

    root = _redirect(tmp_path, monkeypatch)
    calls = []

    def spied_rmtree(path, **kwargs):
        calls.append({"path": path, "kwargs": kwargs})

    monkeypatch.setattr(envs_mod.shutil, "rmtree", spied_rmtree)
    remove("vllm")
    assert len(calls) == 1
    # 断言参数是 (被重定向的) VENV_ROOT / "vllm"（Path）
    assert calls[0]["path"] == envs_mod.VENV_ROOT / "vllm"
    assert calls[0]["path"] == root / "vllm"
    assert calls[0]["kwargs"]["ignore_errors"] is True


def test_remove_unknown_engine_rejected():
    from modelctl.core.envs import remove

    with pytest.raises(ValueError):
        remove("unknown")


def test_status_reads_version_and_packages_windows(tmp_path, monkeypatch):
    from modelctl.core.envs import status

    assert os.name == "nt"
    root = _redirect(tmp_path, monkeypatch)
    venv = root / "vllm"
    scripts = venv / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"fake")
    (venv / "pyvenv.cfg").write_text(
        "home = C:\\Python312\nversion = 3.12.1\ninclude-system-site-packages = false\n",
        encoding="utf-8",
    )
    dist_info = venv / "Lib" / "site-packages" / "vllm-0.27.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: vllm\nVersion: 0.27.0\n",
        encoding="utf-8",
    )

    result = status()
    # vllm：已建环境，读出 python 版本与 vllm 包版本
    assert result["vllm"] == {"exists": True, "python": "3.12.1", "packages": {"vllm": "0.27.0"}}
    # sglang：未建环境
    assert result["sglang"] == {"exists": False}
    # 新增托管引擎：未建环境
    assert result["aphrodite"] == {"exists": False}
    assert result["lmdeploy"] == {"exists": False}
    assert result["tokenspeed"] == {"exists": False}


def test_status_reads_version_and_packages_linux(tmp_path, monkeypatch):
    from modelctl.core.envs import status

    monkeypatch.setattr("os.name", "posix")
    root = _redirect(tmp_path, monkeypatch)
    venv = root / "sglang"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_bytes(b"fake")
    (venv / "pyvenv.cfg").write_text(
        "home = /usr/bin\nversion = 3.12.1\ninclude-system-site-packages = false\n",
        encoding="utf-8",
    )
    dist_info = venv / "lib" / "python3.12" / "site-packages" / "sglang-0.5.9.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: sglang\nVersion: 0.5.9\n",
        encoding="utf-8",
    )

    result = status()
    assert result["sglang"] == {"exists": True, "python": "3.12.1", "packages": {"sglang": "0.5.9"}}
    assert result["vllm"] == {"exists": False}
    # 新增托管引擎：未建环境
    assert result["aphrodite"] == {"exists": False}
    assert result["lmdeploy"] == {"exists": False}
    assert result["tokenspeed"] == {"exists": False}


def test_status_absent_engines_no_python_no_packages(tmp_path, monkeypatch):
    from modelctl.core.envs import status

    _redirect(tmp_path, monkeypatch)
    result = status()
    assert result["vllm"] == {"exists": False}
    assert result["sglang"] == {"exists": False}
    assert result["aphrodite"] == {"exists": False}
    assert result["lmdeploy"] == {"exists": False}
    assert result["tokenspeed"] == {"exists": False}


# === Task 7：engine_site_packages ===


def test_engine_site_packages_vllm_no_env(tmp_path, monkeypatch):
    from modelctl.core.envs import engine_site_packages

    # 仅重定向 VENV_ROOT 到缺失目录即可让 has_env 自然返回 False，无需再 patch has_env
    _redirect(tmp_path, monkeypatch)
    assert engine_site_packages("vllm") is None


def test_engine_site_packages_unmanaged():
    from modelctl.core.envs import engine_site_packages

    assert engine_site_packages("ollama") is None


def test_engine_site_packages_vllm_env_present_linux(tmp_path, monkeypatch):
    from modelctl.core.envs import engine_site_packages

    monkeypatch.setattr("os.name", "posix")
    root = _redirect(tmp_path, monkeypatch)
    sp = root / "vllm" / "lib" / "python3.12" / "site-packages"
    sp.mkdir(parents=True)
    (sp / "x-1.0.dist-info").mkdir()
    (sp / "x-1.0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: x\nVersion: 1.0\n", encoding="utf-8")
    _make_env(root, "vllm", windows=False)
    assert engine_site_packages("vllm") == sp


def test_engine_site_packages_vllm_env_present_windows(tmp_path, monkeypatch):
    from modelctl.core.envs import engine_site_packages

    monkeypatch.setattr("os.name", "nt")
    root = _redirect(tmp_path, monkeypatch)
    sp = root / "vllm" / "Lib" / "site-packages"
    sp.mkdir(parents=True)
    (sp / "x-1.0.dist-info").mkdir()
    (sp / "x-1.0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: x\nVersion: 1.0\n", encoding="utf-8")
    _make_env(root, "vllm", windows=True)
    assert engine_site_packages("vllm") == sp
