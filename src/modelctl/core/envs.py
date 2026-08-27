"""core/envs.py — 托管引擎专用虚拟环境管理。"""

from __future__ import annotations

import os
import subprocess
import sys
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


def ensure_env(engine: str) -> Path:
    if engine not in MANAGED_ENGINES:
        raise ValueError(f"非托管引擎：{engine}")
    if not has_env(engine):
        raise EngineEnvError(
            f"引擎 {engine} 的专用环境未创建，请先执行：modelctl env setup {engine}"
        )
    return VENV_ROOT / engine
