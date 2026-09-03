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
MANAGED_ENGINES = ("vllm", "sglang", "aphrodite", "lmdeploy", "tokenspeed", "tensorrt_llm")
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


# 平台支持矩阵（§3 / TODO 项 5）：托管引擎 venv 仅在 Linux 可建（CUDA 推理栈），
# gateway 子项目跨平台（纯 FastAPI/uvicorn）。值表示「当前平台是否支持该 target」。
# 该平台限制集中在 CLI 入口与文档中读取；Windows 用户在 Windows 端运行 modelctl
# env setup airgapped 会得到错误友好提示（而非原始 traceback）。
def platform_supports(target: str) -> bool:
    """判断当前运行平台是否支持 target（True 表示可环境 setup / 后续运行）。"""
    if target in MANAGED_ENGINES:
        return _is_linux()
    if target == GATEWAY_SUBPROJECT:
        return True  # gateway 在 Linux/Windows/macOS 均可
    return False


def platform_limitation_message(target: str) -> str | None:
    """返回当前平台不支持 target 的友好提示；支持时返回 None。"""
    if platform_supports(target):
        return None
    current = sys.platform or "unknown"
    if target in MANAGED_ENGINES:
        return (
            f"引擎 {target} 的托管 venv 仅支持 Linux（CUDA 推理栈依赖 nvidia-pypi + cudatools）；"
            f"当前平台 {current!r} 不支持。请在部署机（Linux）上执行 "
            f"`modelctl env setup {target}`，或走 docker 镜像绕过 venv。"
        )
    if target == GATEWAY_SUBPROJECT:
        # gateway 应跨平台；若到达此分支说明 _is_target 异常
        return f"gateway 在当前平台 {current!r} 未提供部署（不应发生，请联系维护）"
    return f"target {target!r} 不受支持"


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
        raise EngineEnvError(f"{target} 的专用环境未创建，请先执行：modelctl env setup {target}")
    return VENV_ROOT / target


# 兜底路径（`vllm --version`）的超时预算：该命令要 `import vllm` 并连带加载 torch 等
# 重型依赖，冷启动（page cache 未命中）轻松超过原来的 5s，导致稳定误报
# 「无法探测 vLLM 版本」。正常情况走下方 dist-info 快路径，此处仅兜底，故给足预算。
VLLM_PROBE_TIMEOUT = 60.0


def _run_probe(cmd: list[str], timeout: float) -> str:
    """执行探测命令并返回 stdout；命令缺失 / 超时 / 非零退出均归约为空串。

    只取 stdout 且要求 returncode == 0：命令失败时解释器写到 stderr 的 traceback
    自带 "Python 3.x.y"，若把 stderr 交给版本正则，会把**解释器版本误判成 vLLM 版本**
    并错误放行版本门控。
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def _parse_version(out: str) -> tuple[int, int, int] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


@functools.lru_cache(maxsize=1)
def vllm_version() -> tuple[int, int, int] | None:
    """探测 vLLM 版本；失败 / 未安装返回 None。

    先纯磁盘读 `.venvs/vllm` 的 dist-info（复用 `status()` 的解析，不起子进程、
    不 import vllm，无超时风险）；仅在元数据缺失 / 解析失败时，才退回
    `vllm --version`（需完整 import，慢，故超时预算给得宽松）。
    """
    v = _parse_version(_read_installed_packages(VENV_ROOT / "vllm").get("vllm") or "")
    if v is not None:
        return v
    bin_path = engine_bin("vllm", "vllm")
    if not bin_path.is_file():
        return None
    return _parse_version(_run_probe([str(bin_path), "--version"], VLLM_PROBE_TIMEOUT))


def setup(
    target: str,
    *,
    wheels_dir: Path | None = None,
    offline: bool = False,
) -> int:
    """同步受管子项目依赖到 `.venvs/<target>`。

    wheels_dir：本地 wheel 目录（透传 `--find-links`），作为额外包来源；
    offline：配合 wheels_dir 使用，透传 `--offline` 完全禁用网络（要求目录内依赖自闭包）。
    两者用于内网/弱网机器绕开跨境 PyPI 下载（先在有网机器 `pip download` 备好目录）。
    """
    if not _is_target(target):
        raise ValueError(f"非受管环境：{target}")
    # 平台限制（§3 / TODO 项 5）：托管引擎仅 Linux；gateway 跨平台。
    # 用 platform_supports / platform_limitation_message 统一判断，确保 CLI 提示
    # 与未来运行时探测保持一致。
    message = platform_limitation_message(target)
    if message is not None:
        raise EngineEnvError(message)
    exe = shutil.which("uv")
    if exe is None:
        raise EngineEnvError("未找到 uv，请先安装：pip install uv")
    if wheels_dir is not None and not wheels_dir.is_dir():
        raise EngineEnvError(f"wheel 目录不存在：{wheels_dir}")
    # 子项目定位：网关在 <repo>/gateway；托管引擎在 <repo>/envs/<engine>
    project_root = GATEWAY_ROOT if target == GATEWAY_SUBPROJECT else ENVS_ROOT / target
    env = {
        **os.environ,
        "UV_PROJECT_ENVIRONMENT": str(VENV_ROOT / target),
    }
    cmd = [exe, "sync", "--project", str(project_root)]
    if wheels_dir is not None:
        cmd += ["--find-links", str(wheels_dir.resolve())]
        if offline:
            cmd.append("--offline")
    proc = subprocess.run(cmd, env=env)
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
