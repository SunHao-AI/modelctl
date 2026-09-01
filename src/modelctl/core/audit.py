#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/audit.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026-08-31
# @Desc   : 请求级审计日志（JSONL 按天分片 + 定时清理 + 无网关依赖）
# ===============================================================================

"""core/audit.py — 请求级审计日志。

无网关/stats 依赖；独立可测试。线程安全（同一网关进程内通过 threading.Lock 串行化写）。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    pass

_DAY_RE = re.compile(r"^modelctl-(\d{4}-\d{2}-\d{2})\.jsonl$")


def _parse_day_from_name(name: str) -> _dt.date | None:
    """从文件名提取日期；非 audit 文件/非法日期返回 None。"""
    m = _DAY_RE.match(name)
    if not m:
        return None
    try:
        return _dt.datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _retain_files(files: list[Path], retention_days: int, today: _dt.date | None = None) -> list[Path]:
    """保留 ≤ retention_days 内（含当天）的文件；retention_days=0 时不做时间裁剪。"""
    if retention_days <= 0:
        return list(files)
    today = today or _dt.date.today()
    cutoff = today - _dt.timedelta(days=retention_days)
    kept: list[Path] = []
    for f in files:
        d = _parse_day_from_name(f.name)
        if d is not None and d >= cutoff:
            kept.append(f)
    return kept


def _prune_by_size(files: list[Path], max_size_mb: int) -> list[Path]:
    """按总大小裁剪，从最旧删至 ≤ max_size_mb；max_size_mb=0 时不做大小裁剪。"""
    if max_size_mb <= 0:
        return list(files)
    target_bytes = max_size_mb * 1024 * 1024

    def sort_key(p: Path) -> tuple:
        d = _parse_day_from_name(p.name)
        # 同日按 mtime 排序；日期 None 的排最后（视为不可删候选）
        return (d or _dt.date.max, p.stat().st_mtime if p.exists() else 0.0)

    ordered = sorted(files, key=sort_key)
    total = sum(p.stat().st_size for p in ordered if p.exists())
    if total <= target_bytes:
        return list(files)
    removed: set[Path] = set()
    for p in ordered:
        if total <= target_bytes:
            break
        if not p.exists():
            continue
        total -= p.stat().st_size
        removed.add(p)
    return [p for p in files if p not in removed]


class RequestAuditLog:
    """请求级审计日志：JSONL 按天分片 + 后台清理线程。"""

    def __init__(
        self,
        data_dir: Path,
        retention_days: int = 30,
        max_size_mb: int = 512,
        cleanup_interval_s: float = 86400.0,
    ) -> None:
        self._dir = Path(data_dir)
        self._retention_days = retention_days
        self._max_size_mb = max_size_mb
        self._cleanup_interval_s = cleanup_interval_s
        self._lock = threading.Lock()
        self._cleanup_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._cur_path: Path | None = None
        self._cur_day: _dt.date | None = None
        self._fake_today: _dt.date | None = None  # 测试用

    # -- 内部 --
    def _today(self) -> _dt.date:
        return self._fake_today or _dt.date.today()

    def _today_path(self) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        d = self._today()
        return self._dir / f"modelctl-{d.isoformat()}.jsonl"

    def _all_files(self) -> list[Path]:
        if not self._dir.is_dir():
            return []
        return sorted(
            (p for p in self._dir.iterdir() if p.is_file() and p.name.startswith("modelctl-") and p.name.endswith(".jsonl")),
            key=lambda p: _parse_day_from_name(p.name) or _dt.date.min,
        )

    def _today_file(self) -> set[Path]:
        d = self._today()
        return {p for p in self._all_files() if _parse_day_from_name(p.name) == d}

    # -- 公开 API --
    def record(self, entry: dict) -> bool:
        """写入一条记录；失败仅 warning 不冒泡。"""
        try:
            with self._lock:
                d = self._today()
                if self._cur_day != d:
                    self._cur_path = self._today_path()
                    self._cur_day = d
                line = json.dumps(entry, ensure_ascii=False, default=str)
                # O_APPEND 原子追加
                fd = os.open(self._cur_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                try:
                    os.write(fd, line.encode("utf-8") + b"\n")
                finally:
                    os.close(fd)
            return True
        except Exception as exc:  # noqa: BLE001 — 捕获一切，记录隔离
            logger.warning(f"审计日志写入失败：{exc}")
            return False

    def ensure_cleanup_thread(self) -> None:
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            return
        self._stop_event.clear()
        self._cleanup_thread = threading.Thread(target=self._cleanup_worker, daemon=True)
        self._cleanup_thread.start()

    def _cleanup_worker(self) -> None:
        while not self._stop_event.wait(self._cleanup_interval_s):
            try:
                dead = self.collect_dead_files()
                for p in dead:
                    staging = self._dir / f".audit-deleting-{int(time.time() * 1000)}-{p.name}"
                    try:
                        if p.exists():
                            p.rename(staging)
                        os.chmod(staging, 0o600)
                        staging.unlink()
                    except OSError as exc:
                        logger.warning(f"审计日志清理失败 {p.name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"审计日志清理轮询异常: {exc}")

    def collect_dead_files(self) -> list[Path]:
        """计算应删文件（时间∪大小）并排除今日；纯函数不删除。"""
        files = self._all_files()
        retained_by_time = _retain_files(files, self._retention_days, self._today())
        retained_by_size = _prune_by_size(retained_by_time, self._max_size_mb)
        today_files = self._today_file()
        dead = [p for p in files if p not in retained_by_size and p not in today_files]
        return dead

    def stats_summary(self) -> dict:
        files = self._all_files()
        by_day: dict[str, int] = {}
        total = 0
        for p in files:
            d = _parse_day_from_name(p.name)
            if d is None:
                continue
            sz = p.stat().st_size
            by_day[d.isoformat()] = by_day.get(d.isoformat(), 0) + sz
            total += sz
        days = sorted(by_day)
        return {
            "file_count": len(files),
            "total_bytes": total,
            "by_day": by_day,
            "oldest_day": days[0] if days else None,
            "newest_day": days[-1] if days else None,
        }

    def destroy(self) -> None:
        self._stop_event.set()
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=1.0)
            self._cleanup_thread = None


def _new_audit_log(data_dir: Path) -> RequestAuditLog:
    """工厂：从 env / 默认参数构造 RequestAuditLog（测试可 monkeypatch AUDIT_*）。

    供 gateway / cli 复用：AUDIT_DIR 之外的保留期、大小上限、清理间隔均在 AUDIT_* 变量；
    数值解析失败（非法字符串）时回退默认值，绝不抛出。
    """
    def _int_env(key: str, default: int) -> int:
        v = os.environ.get(key)
        if v is None:
            return default
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return default

    return RequestAuditLog(
        data_dir=Path(data_dir),
        retention_days=_int_env("AUDIT_RETENTION_DAYS", 30),
        max_size_mb=_int_env("AUDIT_MAX_SIZE_MB", 512),
        cleanup_interval_s=float(os.environ.get("AUDIT_CLEANUP_INTERVAL", "86400")),
    )


class NoopAuditLog:
    """单测/降级用 no-op 审计日志（与 RequestAuditLog 同 API）。"""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def record(self, entry: dict) -> bool:
        return False

    def ensure_cleanup_thread(self) -> None:
        pass

    def collect_dead_files(self) -> list[Path]:
        return []

    def stats_summary(self) -> dict:
        return {"file_count": 0, "total_bytes": 0, "by_day": {}, "oldest_day": None, "newest_day": None}

    def destroy(self) -> None:
        pass
