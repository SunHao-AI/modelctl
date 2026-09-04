#!/usr/bin/env python3
# ===============================================================================
# @File   : src/modelctl/core/paths.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 18:30
# @Desc   : 运行时数据目录统一解析（默认 <项目根>/data/*）
# ===============================================================================

"""core/paths.py — 运行时数据目录的**唯一**真值来源。

设计约束（改动前必读）：

1. **默认值只在这里定义**。历史上 LOG_DIR / CACHE_DIR / USAGE_DATA_DIR / AUDIT_DIR 的默认值
   分散在各调用点的 `os.environ.get(...) or <default>` 里，结果长成三套口径：logs 落到
   项目根**上级**、usage 与 cache 撞同一目录、audit 是**相对 CWD** 的 `data/audit`
   （从别处执行 CLI 就写错位置）。新增数据目录一律在本模块加一个 `*_dir()`。
2. **相对路径按 PROJECT_ROOT 解析，绝不按 CWD**。`.env` 的值是纯字符串直接进 os.environ，
   `Path("data/logs")` 会跟着进程当前目录漂移。
3. **每次调用重读 os.environ，不做模块级缓存**。原 gpu_lock 的 `LOCK_DIR` 常量就是模块级
   求值，导致设了 `CACHE_DIR` 也不生效（PID 与 .gpu-lock 分家，GPU 互斥失效）。
4. 本模块只依赖 `envfile.PROJECT_ROOT`，不得 import process / gpu_lock / stats 等业务模块
   （engines ↔ core 之间已有循环导入约束，见 gpu_lock.py 顶部注释）。
"""

from __future__ import annotations

import os
from pathlib import Path

from modelctl.core.envfile import PROJECT_ROOT

# 运行时数据总目录；四项默认值均为它的直接子目录
DATA_ROOT = PROJECT_ROOT / "data"


def resolve_data_dir(env_value: str | None, subdir: str) -> Path:
    """解析单个数据目录：env 值优先，空/未设置回退 `DATA_ROOT/<subdir>`。

    相对 env 值按 PROJECT_ROOT 解析（而非 CWD），使 `LOG_DIR=data/logs` 这类写法在任何
    工作目录下都指向同一位置。
    """
    raw = (env_value or "").strip()
    if not raw:
        return DATA_ROOT / subdir
    p = Path(raw).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def log_dir() -> Path:
    """日志目录（loguru `modelctl.log` + 启动日志 `launch-<name>.log`），默认 `data/logs`。"""
    d = resolve_data_dir(os.environ.get("LOG_DIR"), "logs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    """进程元数据目录（`*.pid` / `*.gpu-lock` / `cluster-meta.db`），默认 `data/cache`。"""
    d = resolve_data_dir(os.environ.get("CACHE_DIR"), "cache")
    d.mkdir(parents=True, exist_ok=True)
    return d


def usage_data_dir() -> Path:
    """用量累计目录（`<name>.json`），默认 `data/usage-data`。

    stats 服务与网关**必须**共用本函数：两侧写不同目录会让 token 累计口径分家。
    """
    d = resolve_data_dir(os.environ.get("USAGE_DATA_DIR"), "usage-data")
    d.mkdir(parents=True, exist_ok=True)
    return d


def audit_dir() -> Path:
    """审计 JSONL 目录（`modelctl-YYYY-MM-DD.jsonl`），默认 `data/audit`。

    故意不 mkdir：写入方 `RequestAuditLog._today_path` 每次落盘前幂等建目录，而只读的
    `modelctl audit stats` / webui 审计列表在目录不存在时应返回空结果，不该有建目录副作用。
    """
    return resolve_data_dir(os.environ.get("AUDIT_DIR"), "audit")
