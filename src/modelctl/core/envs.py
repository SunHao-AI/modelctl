"""core/envs.py — 托管引擎专用虚拟环境管理。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from modelctl.core.envfile import PROJECT_ROOT

MANAGED_ENGINES = ("vllm", "sglang")
ENVS_ROOT = PROJECT_ROOT / "envs"
VENV_ROOT = PROJECT_ROOT / ".venvs"


class EngineEnvError(RuntimeError):
    """引擎专用环境缺失或不可用。"""


def _is_windows() -> bool:
    return os.name == "nt"


def venv_bin_dir(engine: str) -> Path:
    return VENV_ROOT / engine / ("Scripts" if _is_windows() else "bin")


def engine_python(engine: str) -> Path:
    if engine not in MANAGED_ENGINES:
        raise ValueError(f"非托管引擎：{engine}")
    return venv_bin_dir(engine) / ("python.exe" if _is_windows() else "python")


def engine_bin(engine: str, name: str) -> Path:
    if engine not in MANAGED_ENGINES:
        raise ValueError(f"非托管引擎：{engine}")
    exe = name + (".exe" if _is_windows() else "")
    return venv_bin_dir(engine) / exe


def has_env(engine: str) -> bool:
    py = engine_python(engine)
    return py.is_file()


def engine_site_packages(engine: str) -> Path | None:
    """返回引擎专用 venv 内 site-packages 目录;非托管或未建时返回 None。"""
    if engine not in MANAGED_ENGINES:
        return None
    if not has_env(engine):
        return None
    root = VENV_ROOT / engine
    if _is_windows():
        sp = root / "Lib/site-packages"
    else:
        candidates = list(root.glob("lib/python*/site-packages"))
        sp = candidates[0] if candidates else None
    return sp if sp is not None and sp.is_dir() else None


def ensure_env(engine: str) -> Path:
    if engine not in MANAGED_ENGINES:
        raise ValueError(f"非托管引擎：{engine}")
    if not has_env(engine):
        raise EngineEnvError(
            f"引擎 {engine} 的专用环境未创建，请先执行：modelctl env setup {engine}"
        )
    return VENV_ROOT / engine


def setup(engine: str) -> int:
    if engine not in MANAGED_ENGINES:
        raise ValueError(f"非托管引擎：{engine}")
    exe = shutil.which("uv")
    if exe is None:
        raise EngineEnvError("未找到 uv，请先安装：pip install uv")
    env = {
        **os.environ,
        "UV_PROJECT_ENVIRONMENT": str(VENV_ROOT / engine),
    }
    proc = subprocess.run(
        [exe, "sync", "--project", str(ENVS_ROOT / engine)],
        env=env,
    )
    return proc.returncode


def remove(engine: str) -> None:
    if engine not in MANAGED_ENGINES:
        raise ValueError(f"非托管引擎：{engine}")
    shutil.rmtree(VENV_ROOT / engine, ignore_errors=True)


def status() -> dict[str, dict]:
    result: dict[str, dict] = {}
    for engine in MANAGED_ENGINES:
        root = VENV_ROOT / engine
        entry: dict = {"exists": False}
        if not has_env(engine):
            result[engine] = entry
            continue
        entry["exists"] = True
        python = _read_pyvenv_version(root)
        if python:
            entry["python"] = python
        packages = _read_installed_packages(root)
        if packages:
            entry["packages"] = packages
        result[engine] = entry
    return result


def _read_pyvenv_version(root: Path) -> str:
    cfg = root / "pyvenv.cfg"
    if not cfg.is_file():
        return ""
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            return line.split("=", 1)[1].strip()
    return ""


def _read_installed_packages(root: Path) -> dict[str, str]:
    sp_dirs = list(root.glob("lib/python*/site-packages")) if not _is_windows() else [root / "Lib/site-packages"]
    result: dict[str, str] = {}
    for sp in sp_dirs:
        if not sp.is_dir():
            continue
        for meta in sp.glob("*.dist-info/METADATA"):
            name = version = ""
            for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Name:") and not name:
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("Version:") and not version:
                    version = line.split(":", 1)[1].strip()
                if name and version:
                    break
            if name and version:
                result[name] = version
    return result
