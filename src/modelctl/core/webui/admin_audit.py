#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_audit.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/2 10:00
# @Desc   : Web UI 审计日志 API 端点
# ===============================================================================

"""core/webui/admin_audit.py — 请求级审计日志的 Web UI 查询/统计/清理端点。

网关进程不一定能在 Web UI 里复现同一个 ``RequestAuditLog`` 实例，故这里直接读
落盘的 JSONL 文件（``AUDIT_DIR``，默认 ``data/audit``；文件名 ``modelctl-
YYYY-MM-DD.jsonl``，日分片），与 ``modelctl.core.audit`` 的清理口径保持一致：
保留期取 ``AUDIT_RETENTION_DAYS``（默认 30 天），单一目录下总大小取
``AUDIT_MAX_SIZE_MB``（默认 512 MB）。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from loguru import logger

from modelctl.core.webui.admin_auth import require_auth

# 审计文件命名 / 日期解析与 core.audit 保持一致
_AUDIT_DAY_RE = re.compile(r"^modelctl-(\d{4}-\d{2}-\d{2})\.jsonl$")

router = APIRouter()


def _router() -> APIRouter:
    """子路由工厂：返回 APIRouter（主路由 include_router 时由其调用）。"""
    return router


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _audit_dir() -> Path:
    """审计目录（AUDIT_DIR，默认 <项目根>/data/audit；解析统一在 core/paths.py）。"""
    from modelctl.core.paths import audit_dir

    return audit_dir()


def _parse_day(name: str) -> _dt.date | None:
    """从文件名提取日期；不合法返回 None。"""
    m = _AUDIT_DAY_RE.match(name)
    if not m:
        return None
    try:
        return _dt.datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _all_audit_files() -> list[Path]:
    """目录下全部 JSONL 审计文件（按日期升序）。"""
    d = _audit_dir()
    if not d.is_dir():
        return []
    files = [
        p for p in d.iterdir() if p.is_file() and p.name.startswith("modelctl-") and p.name.endswith(".jsonl")
    ]
    return sorted(files, key=lambda p: _parse_day(p.name) or _dt.date.min)


def _parse_since(since: str | None) -> _dt.datetime | None:
    """把 ``30m/1h/24h/7d`` 这样的相对时间字符串解析为 datetime 下限；None 或非法 → None（不限）。"""
    if not since:
        return None
    m = re.fullmatch(r"(\d+)([mhd])", since.strip().lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    delta = {"m": _dt.timedelta(minutes=n), "h": _dt.timedelta(hours=n), "d": _dt.timedelta(days=n)}[unit]
    return _dt.datetime.now().astimezone() - delta


def _entry_time(entry: dict) -> _dt.datetime | None:
    """尽力取审计记录的 ISO 时间戳字段；取不到返回 None。"""
    for key in ("time", "ts", "timestamp", "time_iso", "created_at"):
        v = entry.get(key)
        if isinstance(v, (int, float)):
            try:
                return _dt.datetime.fromtimestamp(v, tz=_dt.timezone.utc)
            except (ValueError, OSError):
                return None
        if isinstance(v, str):
            try:
                dt = _dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.astimezone()
                return dt
            except ValueError:
                continue
    return None


def _entry_is_error(entry: dict) -> bool:
    """审计记录的错误判定：status>=400 或显式 error/error_type/buccess 标志。"""
    st = entry.get("status") or entry.get("status_code") or entry.get("response_status")
    if isinstance(st, int):
        return st >= 400
    return bool(entry.get("error") or entry.get("error_type") or entry.get("is_error"))


def _filter_entries(
    since: str | None,
    model: str | None,
    endpoints: str | None,
) -> tuple[_dt.datetime | None, frozenset[str], frozenset[str]]:
    """构造过滤条件元组（since_dt, model_keys, ep_keys）；空参数回退空集合。"""
    return (
        _parse_since(since),
        frozenset({model}) if model else frozenset(),
        frozenset(e.strip() for e in endpoints.split(",") if e.strip()) if endpoints else frozenset(),
    )


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get("")
async def read_audit(
    since: str | None = Query(default=None, description="相对时间：30m | 1h | 24h | 7d"),
    model: str | None = Query(default=None, description="过滤 model 名"),
    endpoints: str | None = Query(default=None, description="逗号分隔的 endpoint 白名单"),
    limit: int = Query(default=100, ge=1, le=5000),
    _json: bool = Query(default=False, alias="json", description="true 时返回原始 JSONL 行文本"),
    _: None = Depends(require_auth),
):
    """GET /admin/api/audit — 读取匹配的审计记录（最新优先）。

    ``since`` 形如 ``1h/24h/7d``；``model`` 单值；``endpoints`` 逗号分隔白名单；
    ``limit`` 默认 100 上限 5000。``json=true`` 时返回原始 JSONL 行文本
    （``bytes`` 友好），否则解析为 dict 列表。
    """

    since_dt, model_keys, ep_keys = _filter_entries(since, model, endpoints)

    def _collect() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for f in _all_audit_files():
            try:
                with open(f, encoding="utf-8") as fh:
                    raw_lines = fh.readlines()
            except OSError:
                continue
            for line in raw_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if since_dt is not None:
                    t = _entry_time(entry)
                    if t is not None and t < since_dt:
                        continue
                if model_keys and entry.get("model") not in model_keys:
                    continue
                if ep_keys and entry.get("endpoint") not in ep_keys:
                    continue
                out.append(entry)
        # 倒序（最新在前）：基于记录时间戳稳定排序，原始时间无法解析→排最前（视为最早）
        out.sort(
            key=lambda e: (_entry_time(e) is None, _entry_time(e) or _dt.datetime.min),
            reverse=True,
        )
        return out

    entries = await asyncio.to_thread(_collect)
    entries = entries[:limit]
    if _json:
        return {"entries": [json.dumps(e, ensure_ascii=False) for e in entries]}
    return {"entries": entries}


@router.get("/stats")
async def audit_stats(_: None = Depends(require_auth)):
    """GET /admin/api/audit/stats — 审计聚合：total / errors / by_model。"""

    def _collect() -> tuple[int, int, dict[str, int]]:
        total = 0
        errors = 0
        by_model: dict[str, int] = {}
        for f in _all_audit_files():
            try:
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(entry, dict):
                            continue
                        total += 1
                        if _entry_is_error(entry):
                            errors += 1
                        m = entry.get("model")
                        if isinstance(m, str) and m:
                            by_model[m] = by_model.get(m, 0) + 1
            except OSError:
                continue
        return total, errors, by_model

    total, errors, by_model = await asyncio.to_thread(_collect)
    return {"total": total, "errors": errors, "by_model": by_model}


@router.post("/cleanup")
async def audit_cleanup(body: dict | None = None, _: None = Depends(require_auth)):
    """POST /admin/api/audit/cleanup — 按 AUDIT_RETENTION_DAYS / AUDIT_MAX_SIZE_MB 清理。

    Body 可选：``{dry_run?: bool = true}``。保留今日文件；超保留期/超尺码的文件
    从最旧开始删除，直到满足两条约束。
    """
    from modelctl.core.audit import RequestAuditLog

    dry_run = bool((body or {}).get("dry_run", True))

    # 为了与 core.audit 完全一致的清理口径，临时构造一个 RequestAuditLog，
    # 走 collect_dead_files 权威计算应删文件（纯函数，不起线程）。
    log = RequestAuditLog(
        data_dir=_audit_dir(),
        retention_days=_int_env("AUDIT_RETENTION_DAYS", 30),
        max_size_mb=_int_env("AUDIT_MAX_SIZE_MB", 512),
        cleanup_interval_s=float("inf"),  # 不起线程，collect 是同步纯计算
    )

    def _work() -> tuple[list[str], int, list[Path]]:
        dead: list[Path] = []
        try:
            dead = log.collect_dead_files()
        finally:
            log.destroy()
        deleted: list[str] = []
        freed = 0
        if not dry_run:
            for p in dead:
                size = p.stat().st_size if p.exists() else 0
                try:
                    p.unlink()
                    deleted.append(p.name)
                    freed += size
                except OSError as exc:
                    logger.warning(f"审计日志删除失败 {p.name}: {exc}")
                    continue
        return deleted, freed // (1024 * 1024), dead

    deleted, size_mb, dead = await asyncio.to_thread(_work)
    # dry_run 时返回即将删除的沙箱列表
    return {
        "deleted": deleted if not dry_run else [p.name for p in dead],
        "size_mb": size_mb,
        "dry_run": dry_run,
    }


@router.get("/path")
async def audit_path(_: None = Depends(require_auth)):
    """GET /admin/api/audit/path — 返回审计目录的绝对路径。"""
    return {"path": str(_audit_dir())}


# ---------------------------------------------------------------------------
# 小型工具
# ---------------------------------------------------------------------------


def _int_env(key: str, default: int) -> int:
    """读 int 环境变量；非法字符串回退默认值。"""
    v = os.environ.get(key)
    if v is None:
        return default
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default
