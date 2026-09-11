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


def _entry_level(entry: dict) -> str:
    """审计记录级别（后端统一派生，前端不再自行判定）。

    vLLM/网关的 JSONL 审计**没有 level 字段**（写入侧只落 status_code / error），
    而前端列表有级别列与级别过滤器，故这里按状态码派生：5xx → error、4xx → warn、
    其余 → info；无状态码但带 error 的（如上游连接异常）按 error 处理。

    4xx 归 warn 而非 error：客户端拼错模型名、鉴权失败属调用方问题，与后端故障
    混在一个级别里会让人找不到真正需要处理的 5xx。
    """
    st = entry.get("status") or entry.get("status_code") or entry.get("response_status")
    if isinstance(st, int):
        if st >= 500:
            return "error"
        if st >= 400:
            return "warn"
        return "info"
    if entry.get("error") or entry.get("error_type") or entry.get("is_error"):
        return "error"
    return "info"


def _entry_is_error(entry: dict) -> bool:
    """错误判定：非 info 即计入错误数（5xx + 4xx + 显式 error 标志）。"""
    return _entry_level(entry) != "info"


def _entry_matches_keyword(entry: dict, needle: str) -> bool:
    """关键字匹配：在所有标量字段（含 status_code 等数字）拼成的文本里做小写子串查找。

    嵌套的 native_metrics / gateway_metrics 不参与——数值字典做子串匹配只会产生噪音。
    """
    parts = [str(v) for v in entry.values() if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
    return needle in " ".join(parts).lower()


def _filter_entries(
    since: str | None,
    model: str | None,
    endpoints: str | None,
    auth: str = "",
    client_ip: str = "",
) -> tuple[_dt.datetime | None, frozenset[str], frozenset[str], str, str]:
    """构造过滤条件元组（since_dt, model_keys, ep_keys, auth, client_ip）；空参数回退空集合/空串。"""
    return (
        _parse_since(since),
        frozenset({model}) if model else frozenset(),
        frozenset(e.strip() for e in endpoints.split(",") if e.strip()) if endpoints else frozenset(),
        auth or "",
        client_ip or "",
    )


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get("")
async def read_audit(
    since: str | None = Query(default=None, description="相对时间：10m | 1h | 6h | 24h | 7d | 30d"),
    model: str | None = Query(default=None, description="过滤 model 名"),
    endpoints: str | None = Query(default=None, description="逗号分隔的 endpoint（path）白名单"),
    auth: str = Query(default="", description="过滤准入结果标签：ok | missing | invalid | unconfigured"),
    client_ip: str = Query(default="", description="过滤来源 IP（精确匹配）"),
    level: str = Query(default="", description="级别过滤：info | warn | error（后端按 status_code 派生）"),
    keyword: str = Query(default="", description="关键字：在所有标量字段上做大小写不敏感子串匹配"),
    limit: int = Query(default=100, ge=1, le=5000),
    _json: bool = Query(default=False, alias="json", description="true 时返回原始 JSONL 行文本"),
    _: None = Depends(require_auth),
):
    """GET /admin/api/audit — 读取匹配的审计记录（最新优先）。

    ``since`` 形如 ``10m/1h/6h/24h/7d/30d``；``model`` 单值；``endpoints`` 逗号分隔白名单；
    ``auth`` / ``client_ip`` 精确匹配准入标签与来源 IP；``level`` / ``keyword`` 为前端
    工具条的级别与关键字过滤；``limit`` 默认 100 上限 5000。
    ``json=true`` 时返回原始 JSONL 行文本，否则解析为 dict 列表。

    响应含 ``total`` / ``error_count``：这是**过滤后未截断**的计数（``entries`` 会被
    ``limit`` 截断），前端"错误数"卡片直接读它，故必须在截断前统计。
    """

    since_dt, model_keys, ep_keys, auth_key, client_ip_key = _filter_entries(
        since, model, endpoints, auth, client_ip
    )
    level_key = level.strip().lower()
    keyword_key = keyword.strip().lower()

    def _collect() -> tuple[list[dict[str, Any]], int, int]:
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
                if ep_keys:
                    # 写入侧（gateway._build_audit_entry）落的是 "path" 键（如
                    # "chat/completions" / "messages"），历史上这里读 "endpoint"，
                    # 取值恒为 None → 端点过滤器永久失效（选任何端点都返回空）。
                    # 保留对 "endpoint" 的兼容读取，以防存在旧格式审计文件。
                    ep = entry.get("path")
                    if ep is None:
                        ep = entry.get("endpoint")
                    if ep not in ep_keys:
                        continue
                if auth_key and entry.get("auth") != auth_key:
                    continue
                if client_ip_key and entry.get("client_ip") != client_ip_key:
                    continue
                if level_key and _entry_level(entry) != level_key:
                    continue
                if keyword_key and not _entry_matches_keyword(entry, keyword_key):
                    continue
                out.append(entry)
        total = len(out)
        errors = sum(1 for e in out if _entry_is_error(e))
        # 倒序（最新在前）：基于记录时间戳稳定排序，原始时间无法解析→排最前（视为最早）
        out.sort(
            key=lambda e: (_entry_time(e) is None, _entry_time(e) or _dt.datetime.min),
            reverse=True,
        )
        return out, total, errors

    entries, total, error_count = await asyncio.to_thread(_collect)
    entries = entries[:limit]
    payload: dict[str, Any] = {
        # 实际生效的 since（前端据此回显；未过滤时为 None）
        "since": since_dt.isoformat(timespec="seconds") if since_dt else None,
        "total": total,
        "error_count": error_count,
    }
    if _json:
        payload["entries"] = [json.dumps(e, ensure_ascii=False) for e in entries]
    else:
        # 级别由后端派生后注入：JSONL 里没有 level 字段，前端若各自判定会出现
        # 列表列与过滤器口径不一致（过滤器按 4xx→warn，列表却全显示成 log）
        payload["entries"] = [{**e, "level": _entry_level(e)} for e in entries]
    return payload


@router.get("/stats")
async def audit_stats(
    since: str | None = Query(default=None, description="相对时间：10m | 1h | 6h | 24h | 7d | 30d"),
    _: None = Depends(require_auth),
):
    """GET /admin/api/audit/stats — 审计聚合：total / errors / by_day / by_model。

    ``since`` 与列表端点同源（缺省不限时间）——前端切时间范围时统计卡必须跟着变，
    否则会出现"列表按 1 小时过滤、统计卡还是全量"的口径分裂。

    ``by_day`` 是 ``[{date, total, error}]`` 按日期升序的数组（前端直接画柱状图）；
    **仅统计有明确日期的文件**，按 ``since`` 过滤时以文件所属日粗筛（整日纳入/排除），
    避免为画图把全部 JSONL 逐行读一遍。
    """
    since_dt = _parse_since(since)

    def _collect() -> tuple[int, int, list[dict[str, int]], dict[str, int]]:
        total = 0
        errors = 0
        by_model: dict[str, int] = {}
        day_total: dict[str, int] = {}
        day_error: dict[str, int] = {}
        for f in _all_audit_files():
            day = _parse_day(f.name)
            # 按日粗筛：整日早于 since 下限的文件直接跳过（日粒度足够画图）
            if since_dt is not None and day is not None and day < since_dt.date():
                continue
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
                        is_err = _entry_is_error(entry)
                        if is_err:
                            errors += 1
                        if day is not None:
                            k = day.isoformat()
                            day_total[k] = day_total.get(k, 0) + 1
                            if is_err:
                                day_error[k] = day_error.get(k, 0) + 1
                        m = entry.get("model")
                        if isinstance(m, str) and m:
                            by_model[m] = by_model.get(m, 0) + 1
            except OSError:
                continue
        by_day = [
            {"date": d, "total": day_total[d], "error": day_error.get(d, 0)}
            for d in sorted(day_total)
        ]
        return total, errors, by_day, by_model

    total, errors, by_day, by_model = await asyncio.to_thread(_collect)
    return {
        "since": since_dt.isoformat(timespec="seconds") if since_dt else None,
        "total": total,
        "errors": errors,
        "by_day": by_day,
        "by_model": by_model,
    }


@router.post("/cleanup")
async def audit_cleanup(body: dict | None = None, _: None = Depends(require_auth)):
    """POST /admin/api/audit/cleanup — 清理过期审计分片。

    Body 可选：``{dry_run?: bool = true, days?: int}``。``days`` 显式给出时按它作保留期
    （前端"清理 30 天前"按钮就靠这个参数表达意图），缺省回退 ``AUDIT_RETENTION_DAYS``；
    尺码约束恒取 ``AUDIT_MAX_SIZE_MB``。保留今日文件；超保留期/超尺码的文件从最旧开始删。

    响应：``removed``（应删/已删文件个数）、``freed_bytes``（释放字节）、``deleted``
    （文件名列表）、``size_mb``（释放 MB，整数除法便于人读）、``dry_run``。
    """
    from modelctl.core.audit import RequestAuditLog

    payload = body if isinstance(body, dict) else {}
    dry_run = bool(payload.get("dry_run", True))
    days_raw = payload.get("days")
    if days_raw is None:
        retention_days = _int_env("AUDIT_RETENTION_DAYS", 30)
    else:
        try:
            retention_days = max(0, int(float(days_raw)))
        except (TypeError, ValueError):
            retention_days = _int_env("AUDIT_RETENTION_DAYS", 30)

    # 为了与 core.audit 完全一致的清理口径，临时构造一个 RequestAuditLog，
    # 走 collect_dead_files 权威计算应删文件（纯函数，不起线程）。
    log = RequestAuditLog(
        data_dir=_audit_dir(),
        retention_days=retention_days,
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
        if dry_run:
            # 预览同样统计预计释放量：只给文件个数不足以判断"这次清理值不值"。
            for p in dead:
                freed += p.stat().st_size if p.exists() else 0
        else:
            for p in dead:
                size = p.stat().st_size if p.exists() else 0
                try:
                    p.unlink()
                    deleted.append(p.name)
                    freed += size
                except OSError as exc:
                    logger.warning(f"审计日志删除失败 {p.name}: {exc}")
                    continue
        return deleted, freed, dead

    deleted, freed_bytes, dead = await asyncio.to_thread(_work)
    names = deleted if not dry_run else [p.name for p in dead]
    return {
        "ok": True,
        "removed": len(names),
        "freed_bytes": freed_bytes,
        "size_mb": freed_bytes // (1024 * 1024),
        "deleted": names,
        "dry_run": dry_run,
        "retention_days": retention_days,
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
