#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/gpu_lock.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : GPU 占用文件锁
# ===============================================================================

"""core/gpu_lock.py — GPU 占用文件锁（best-effort，拦截同卡争抢）。

锁文件位于 data/cache/<name>.gpu-lock，内容为 JSON：{"gpus":[...], "pid":..., "updated_at":...}。
持有进程已退出时视为残留锁并自动清理。不做并发原子性保证（spec 明确 best-effort）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.process import is_pid_alive

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
        if is_pid_alive(int(data["pid"])):
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


def update_gpu_lock_owner(name: str, pid: int) -> None:
    """将锁的持有者 PID 更新为引擎真实进程（CLI 启动后退出，须改挂到长驻引擎上）。幂等、缺文件时 no-op。"""
    path = _lock_path(name)
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict):
        data["pid"] = pid
        data["updated_at"] = time.time()
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def release_gpu_lock(name: str) -> None:
    _lock_path(name).unlink(missing_ok=True)
