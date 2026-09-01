"""core/envs.py — 托管引擎专用虚拟环境管理。"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from modelctl.core.envfile import PROJECT_ROOT

# 托管引擎（仅 Linux 部署，venv 落在 .venvs/<engine>，子项目锁定在 envs/<engine>/pyproject.toml）
MANAGED_ENGINES = ("vllm", "sglang", "aphrodite", "lmdeploy", "tokenspeed")
# 独立 venv 子项目（差异：项目内 gateway/ 自带 pyproject，模型引擎在 envs/ 目录且仅 Linux）
GATEWAY_SUBPROJECT: str | None = "gateway"
ENVS_ROOT = PROJECT_ROOT / "envs"
GATEWAY_ROOT = PROJECT_ROOT / "gateway"
VENV_ROOT = PROJECT_ROOT / ".venvs"


def known_targets() -> tuple[str, ...]:
    """所有受 modelctl env 管理的 target。"""
    return MANAGED_ENGINES + ((GATEWAY_SUBPROJECT,) if GATEWAY_SUBPROJECT else ())


class EngineEnvError(RuntimeError):
    """引擎专用环境缺失或不可用。"""


def _is_windows() -> bool:
    return os.name == "nt"


def _is_linux() -> bool:
    """当前运行平台是否为 Linux（托管引擎的目标部署平台）。"""
    return sys.platform.startswith("linux")


def _is_target(target: str) -> bool:
    return target in MANAGED_ENGINES or (GATEWAY_SUBPROJECT is not None and target == GATEWAY_SUBPROJECT)


def venv_bin_dir(target: str) -> Path:
    return VENV_ROOT / target / ("Scripts" if _is_windows() else "bin")


def engine_python(target: str) -> Path:
    if not _is_target(target):
        raise ValueError(f"非受管环境：{target}")
    return venv_bin_dir(target) / ("python.exe" if _is_windows() else "python")


def engine_bin(target: str, name: str) -> Path:
    if target not in MANAGED_ENGINES:
        raise ValueError(f"非托管引擎：{target}")
    exe = name + (".exe" if _is_windows() else "")
    return venv_bin_dir(target) / exe


def has_env(target: str) -> bool:
    if not _is_target(target):
        return False
    py = engine_python(target)
    return py.is_file()


def engine_site_packages(target: str) -> Path | None:
    """返回受管 venv 内 site-packages 目录;非受管或未建时返回 None。"""
    if not _is_target(target):
        return None
    if not has_env(target):
        return None
    root = VENV_ROOT / target
    if _is_windows():
        sp = root / "Lib/site-packages"
    else:
        candidates = list(root.glob("lib/python*/site-packages"))
        sp = candidates[0] if candidates else None
    return sp if sp is not None and sp.is_dir() else None


def ensure_env(target: str) -> Path:
    if not _is_target(target):
        raise ValueError(f"非受管环境：{target}")
    if not has_env(target):
        raise EngineEnvError(
            f"{target} 的专用环境未创建，请先执行：modelctl env setup {target}"
        )
    return VENV_ROOT / target


@functools.lru_cache(maxsize=1)
def vllm_version() -> tuple[int, int, int] | None:
    """探测 vLLM 版本（subprocess vllm --version 解析首 token）；失败 / 未安装返回 None。"""
    bin_path = engine_bin("vllm", "vllm")
    if not bin_path.is_file():
        return None
    try:
        r = subprocess.run([str(bin_path), "--version"], capture_output=True, text=True, timeout=5)
        out = (r.stdout or "").strip()
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def setup(target: str) -> int:
    if not _is_target(target):
        raise ValueError(f"非受管环境：{target}")
    # 托管引擎限定 Linux（CUDA 推理）；gateway 子项目跨 Linux/Windows 通用
    if target in MANAGED_ENGINES and not _is_linux():
        raise EngineEnvError(
            f"引擎 {target} 的目标平台为 Linux，当前平台为 {sys.platform}；"
            f"请到 Linux 部署机上执行：modelctl env setup {target}"
        )
    exe = shutil.which("uv")
    if exe is None:
        raise EngineEnvError("未找到 uv，请先安装：pip install uv")
    # 子项目定位：网关在 <repo>/gateway；托管引擎在 <repo>/envs/<engine>
    project_root = GATEWAY_ROOT if target == GATEWAY_SUBPROJECT else ENVS_ROOT / target
    env = {
        **os.environ,
        "UV_PROJECT_ENVIRONMENT": str(VENV_ROOT / target),
    }
    proc = subprocess.run(
        [exe, "sync", "--project", str(project_root)],
        env=env,
    )
    return proc.returncode


def remove(target: str) -> None:
    if not _is_target(target):
        raise ValueError(f"非受管环境：{target}")
    shutil.rmtree(VENV_ROOT / target, ignore_errors=True)


def status() -> dict[str, dict]:
    result: dict[str, dict] = {}
    for target in known_targets():
        root = VENV_ROOT / target
        entry: dict = {"exists": False}
        if not has_env(target):
            result[target] = entry
            continue
        entry["exists"] = True
        python = _read_pyvenv_version(root)
        if python:
            entry["python"] = python
        packages = _read_installed_packages(root)
        if packages:
            entry["packages"] = packages
        result[target] = entry
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
