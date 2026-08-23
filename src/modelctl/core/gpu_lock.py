#!/usr/bin/env python3
"""core/gpu_lock.py — GPU 占用文件锁（best-effort，拦截同卡争抢）。

锁文件位于 data/cache/<name>.gpu-lock，内容为 JSON：{"gpus":[...], "pid":..., "updated_at":...}。
持有进程已退出时视为残留锁并自动清理。不做并发原子性保证（spec 明确 best-effort）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from modelctl.core.envfile import PROJECT_ROOT


if sys.platform == "win32":
    import ctypes


def _pid_alive(pid: int) -> bool:
    """探测 pid 对应进程是否存活。"""
    if sys.platform == "win32":
        # Windows 实测：对不存在的 PID 调 CPython os.kill(pid, 0)（内部 OpenProcess）
        # 之后控制台会被投递异步 Ctrl-C 事件、连带杀掉宿主会话；改用 ctypes 直连
        # kernel32.OpenProcess 做存在性探测可完全规避。
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)  # signal 0 = existence probe
        return True
    except OSError:
        return False

# 注意：不在模块顶层 import modelctl.engines.base —— engines/__init__ 会立即导入
# llamacpp/unsloth（它们又在本模块被导入），首个入口是 gpu_lock 时会构成循环导入；
# 故在 acquire_gpu_lock 内延迟导入 RequirementError。
LOCK_DIR = PROJECT_ROOT / "data" / "cache"
LOCK_SUFFIX = ".gpu-lock"


def _lock_path(name: str) -> Path:
    return LOCK_DIR / f"{name}{LOCK_SUFFIX}"


def _read_lock(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and "pid" in data:
        if _pid_alive(int(data["pid"])):
            return data
        path.unlink(missing_ok=True)  # holder gone → stale lock, clean up
        return None
    return None


def list_gpu_locks() -> dict[int, str]:
    """返回 {gpu_index: owning_model_name}；自动清理失效锁。"""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[int, str] = {}
    for path in sorted(LOCK_DIR.glob(f"*{LOCK_SUFFIX}")):
        data = _read_lock(path)
        if data is None:
            continue
        name = path.name[: -len(LOCK_SUFFIX)]
        for g in data.get("gpus", []):
            result[int(g)] = name
    return result


def acquire_gpu_lock(name: str, gpus: list[int]) -> None:
    """占用指定 GPU；若与其他存活模型冲突抛 RequirementError（同名可重入）。"""
    from modelctl.engines.base import RequirementError  # 延迟导入，避免循环依赖

    if not gpus:
        return
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    locks = list_gpu_locks()
    conflicts = {g: locks[g] for g in gpus if g in locks and locks[g] != name}
    if conflicts:
        detail = "; ".join(f"GPU {g} 已被模型 {n} 占用" for g, n in sorted(conflicts.items()))
        raise RequirementError(f"[gpu_lock] {detail}。请先停止占用模型，或更换 gpu_list。")
    _lock_path(name).write_text(
        json.dumps({"gpus": gpus, "pid": os.getpid(), "updated_at": time.time()}, ensure_ascii=False),
        encoding="utf-8",
    )


def release_gpu_lock(name: str) -> None:
    _lock_path(name).unlink(missing_ok=True)
