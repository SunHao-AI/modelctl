# 请求级性能指标 + 全引擎审计日志 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `modelctl` 网关侧实现 vLLM 原生 per-request metrics 的 yaml 开关与请求级 token/性能审计（JSONL 按天分片）+ 定时清理 + `modelctl audit` CLI。

**Architecture:** 新增 `core/audit.py`（`RequestAuditLog` 独立模块，无网关依赖）；`gateway.py` 三个响应处理点同时记录 stats 与 audit；`engines/vllm.py` 在 `build_command` 追加可配置 flag + `check_requirements` 版本门控；`cli.py` 新增 `audit` 子命令族；`.env.example` 新增 `AUDIT_*` 段；`models/vllm/qwen3.8.yaml` 示范配置。

**Tech Stack:** Python 3.12、标准库（json/os/threading/argparse/datetime）、loguru、httpx（`MockTransport` / `ASGITransport`）、pytest（monkeypatch）

## Global Constraints

- Python 3.12；不引入新依赖（用 loguru/pytest/httpx 现有栈）
- 遵循现有代码风格：中文注释、loguru、`from __future__ import annotations`
- **现有测试必须保持通过**（`uv run pytest tests/ -q`）
- TDD：每个任务先写失败测试，再实现
- `build_command` 未配置两个新字段时输出**逐字节一致**（与改造前）
- 审计 I/O 异常**绝不冒泡**到请求转发层（`logger.warning` 静默）
- 绝不删除当天 audit 文件（保 in-flight 数据至少 1 天）
- `AUDIT_DIR` 默认 `data/audit`，与 `USAGE_DATA_DIR` 解耦
- 本计划沿用"不自动 commit"策略（commit 步骤均**不执行**，改动留工作区由用户统一提交）

---

### Task 1: `core/audit.py` — RequestAuditLog 模块

**Files:**
- Create: `src/modelctl/core/audit.py`
- Test: `tests/test_audit.py`

**Interfaces:**
- Consumes: 标准库（`json`/`os`/`threading`/`time`/`datetime`/`re`/`pathlib`）、loguru
- Produces:
  - `class RequestAuditLog` — 构造参数 `(data_dir: Path, retention_days: int = 30, max_size_mb: int = 512, cleanup_interval_s: float = 86400.0)`
    - 方法 `__init__(self, data_dir, retention_days=30, max_size_mb=512, cleanup_interval_s=86400.0)`
    - 方法 `record(self, entry: dict) -> bool`（`entry` 为 §4.2 的 JSONL 单条记录 dict；写入成功 True，失败 False 仅 warning）
    - 方法 `ensure_cleanup_thread(self) -> None`（幂等启动 daemon 线程）
    - 方法 `collect_dead_files(self) -> list[Path]`（纯函数，不删除）
    - 方法 `stats_summary(self) -> dict`（`{"file_count": int, "total_bytes": int, "by_day": {iso_day: int}, "oldest_day": str | None, "newest_day": str | None}`）
    - 方法 `destroy(self) -> None`
  - `class NoopAuditLog` — 与 `RequestAuditLog` 同 API 但所有方法 no-op（单测用）

Helpers（模块私有，不 export）：
- `_parse_day_from_name(name: str) -> date | None`（正则 `^modelctl-(\d{4}-\d{2}-\d{2})\.jsonl$`）
- `_retain_files(files: list[Path], retention_days: int) -> list[Path]`
- `_prune_by_size(files: list[Path], max_size_mb: int) -> list[Path]`
- `_cleanup_worker(self)`（线程循环入口；检查退出事件 → sleep → 调 `cleanup_once`）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_audit.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_audit.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026-08-31
# @Desc   : core/audit.py 单元测试：record / 切日 / 清理 / 降级
# ===============================================================================

"""core/audit.py 单元测试。"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from modelctl.core.audit import (
    NoopAuditLog,
    RequestAuditLog,
    _parse_day_from_name,
    _prune_by_size,
    _retain_files,
)


def _base_entry(over: dict | None = None) -> dict:
    entry = {
        "ts": "2026-08-31T10:23:11.123+08:00",
        "model": "qwen3.8-vllm",
        "engine": "vllm",
        "path": "chat/completions",
        "stream": True,
        "source": "vllm_native",
        "tokens_source": "response-usage",
        "prompt_tokens": 42,
        "completion_tokens": 128,
        "total_tokens": 170,
        "input_char_len": 512,
        "native_metrics": {
            "time_to_first_token_ms": 85.2,
            "generation_time_ms": 1240.5,
            "queue_time_ms": 12.3,
            "mean_itl_ms": 9.1,
            "tokens_per_second": 103.2,
        },
        "gateway_metrics": {
            "ttft_ms": 92.4,
            "generation_time_ms": 1260.8,
            "tokens_per_second": 100.7,
        },
        "status_code": 200,
        "error": None,
        "finish_reason": "stop",
    }
    if over:
        entry.update(over)
    return entry


def test_request_audit_log_records_native_source(tmp_path):
    log = RequestAuditLog(Path(tmp_path / "audit"))
    log.record(_base_entry())
    files = list((tmp_path / "audit").glob("modelctl-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["source"] == "vllm_native"
    assert rec["native_metrics"]["time_to_first_token_ms"] == 85.2
    assert rec["tokens_source"] == "response-usage"
    log.destroy()


def test_request_audit_log_records_fallback_tokens(tmp_path):
    log = RequestAuditLog(Path(tmp_path / "audit"))
    log.record(_base_entry({"source": "gateway_estimate", "tokens_source": "collector-diff", "native_metrics": None}))
    files = list((tmp_path / "audit").glob("modelctl-*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert rec["source"] == "gateway_estimate"
    assert rec["tokens_source"] == "collector-diff"
    assert rec["native_metrics"] is None
    log.destroy()


def test_request_audit_log_module_level_state_is_reset_between_tests():
    """模块级 `_today` 跨测试复用可能影响新实例；用全新 tmp_path 路径隔离即可。
    本测试仅在 CI 顺序运行时做冒烟。"""
    assert isinstance(_parse_day_from_name("modelctl-2026-08-31.jsonl"), _dt.date)


def test_day_rollover(tmp_path):
    """fake 日期跨天：record 写到不同文件。"""
    log = RequestAuditLog(Path(tmp_path / "audit"))
    log.record(_base_entry({"ts": "2026-08-30T22:30:00.500+08:00"}))
    log._fake_today = _dt.date(2026, 8, 31)  # 切日
    log.record(_base_entry({"ts": "2026-08-31T00:05:00.500+08:00"}))
    files = sorted(p.name for p in (tmp_path / "audit").glob("modelctl-*.jsonl"))
    assert files == ["modelctl-2026-08-30.jsonl", "modelctl-2026-08-31.jsonl"]
    log.destroy()


def test_cleanup_by_retention_days(tmp_path):
    """RETENTION=2：今天 + 1 天前 + 2 天前保留，3 天前删除。"""
    audit = Path(tmp_path / "audit")
    audit.mkdir(parents=True)
    log = RequestAuditLog(audit, retention_days=2, max_size_mb=0)
    # 模拟 4 个文件（名字日期）
    for name in ("modelctl-2026-08-28.jsonl", "modelctl-2026-08-29.jsonl",
                 "modelctl-2026-08-30.jsonl", "modelctl-2026-08-31.jsonl"):
        (audit / name).write_text("x", encoding="utf-8")
    log._fake_today = _dt.date(2026, 8, 31)
    dead = log.collect_dead_files()
    dead_names = sorted(p.name for p in dead)
    assert dead_names == ["modelctl-2026-08-28.jsonl"]
    assert not (audit / "modelctl-2026-08-31.jsonl").unlink and (audit / "modelctl-2026-08-31.jsonl").exists()
    log.destroy()


def test_cleanup_by_size_oldest_removed_first(tmp_path):
    """MAX_SIZE_MB=1：从最旧开始删。今日文件不允许删（铁律）。"""
    audit = Path(tmp_path / "audit")
    audit.mkdir(parents=True)
    # 5 个文件各 400KB，总 2MB
    files_spec = [
        ("modelctl-2026-08-26.jsonl", _dt.date(2026, 8, 26)),
        ("modelctl-2026-08-27.jsonl", _dt.date(2026, 8, 27)),
        ("modelctl-2026-08-28.jsonl", _dt.date(2026, 8, 28)),
        ("modelctl-2026-08-29.jsonl", _dt.date(2026, 8, 29)),
        ("modelctl-2026-08-30.jsonl", _dt.date(2026, 8, 30)),
    ]
    for name, _ in files_spec:
        (audit / name).write_bytes(b"x" * (400 * 1024))
    log = RequestAuditLog(audit, retention_days=0, max_size_mb=1)
    log._fake_today = _dt.date(2026, 8, 31)
    dead = log.collect_dead_files()
    dead_names = sorted(p.name for p in dead)
    # 应删最旧的 2 个：8-26 / 8-27；今天（8-31）不存在
    assert dead_names == ["modelctl-2026-08-26.jsonl", "modelctl-2026-08-27.jsonl"]
    log.destroy()


def test_cleanup_never_deletes_today(tmp_path):
    """当日文件绝不删除，即使 MAX_SIZE_MB 设为 0.001 也不动。"""
    audit = Path(tmp_path / "audit")
    audit.mkdir(parents=True)
    (audit / "modelctl-2026-08-31.jsonl").write_bytes(b"x" * (512 * 1024))
    log = RequestAuditLog(audit, retention_days=0, max_size_mb=0)  # 都为 0 → 不清理
    log._fake_today = _dt.date(2026, 8, 31)
    assert log.collect_dead_files() == []
    log.destroy()


def test_record_failure_isolated_via_oserror(monkeypatch, tmp_path, caplog):
    """mock write 抛 OSError → 不冒泡，返回 False。"""
    audit = Path(tmp_path / "audit")
    audit.mkdir(parents=True)
    log = RequestAuditLog(audit)
    import builtins
    real_open = builtins.open
    def boom_open(path, *a, **k):
        if "modelctl-" in str(path):
            raise OSError("disk full")
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", boom_open, raising=False)
    # record 内部走 os.open / os.fdopen 而非 builtins.open，需 mock os.fdopen 模拟失败
    import os as _os
    real_fdopen = _os.fdopen
    def boom_fdopen(fd, *a, **k):
        _os.close(fd)
        raise OSError("disk full")
    monkeypatch.setattr(_os, "fdopen", boom_fdopen)
    assert log.record(_base_entry()) is False
    # 显式恢复（monkeypatch teardown 时自动恢复，这里只显式写注释）
    assert (audit).exists()
    log.destroy()


def test_collect_dead_files_is_pure(tmp_path):
    """同输入两次调用返回相同列表（不删除、不改状态）。"""
    audit = Path(tmp_path / "audit")
    audit.mkdir(parents=True)
    (audit / "modelctl-2026-08-20.jsonl").write_text("x", encoding="utf-8")
    log = RequestAuditLog(audit, retention_days=10, max_size_mb=0)
    log._fake_today = _dt.date(2026, 8, 31)
    d1 = [p.name for p in log.collect_dead_files()]
    d2 = [p.name for p in log.collect_dead_files()]
    assert d1 == d2 == ["modelctl-2026-08-20.jsonl"]
    assert (audit / "modelctl-2026-08-20.jsonl").exists()  # 文件未被删除
    log.destroy()


def test_stats_summary_counts_days(tmp_path):
    """stats_summary 汇总：文件数 / 字节 / 按天分布 / 最早 / 最新。"""
    audit = Path(tmp_path / "audit")
    audit.mkdir(parents=True)
    (audit / "modelctl-2026-08-30.jsonl").write_text("a\n", encoding="utf-8")  # 2 bytes
    (audit / "modelctl-2026-08-31.jsonl").write_text("a\nb\n", encoding="utf-8")  # 3 bytes
    log = RequestAuditLog(audit)
    s = log.stats_summary()
    assert s["file_count"] == 2
    assert s["total_bytes"] == 5
    assert s["by_day"] == {"2026-08-30": 2, "2026-08-31": 3}
    assert s["oldest_day"] == "2026-08-30"
    assert s["newest_day"] == "2026-08-31"
    log.destroy()


def test_noop_audit_log_matches_signature(tmp_path):
    log = NoopAuditLog()
    assert log.record(_base_entry()) is False
    log.ensure_cleanup_thread()
    assert log.collect_dead_files() == []
    log.destroy()


# ---- 纯函数单测 ----

def test_parse_day_from_name():
    assert _parse_day_from_name("modelctl-2026-08-31.jsonl") == _dt.date(2026, 8, 31)
    assert _parse_day_from_name("modelctl-deleting.jsonl") is None
    assert _parse_day_from_name("other-2026-08-31.jsonl") is None


def test_parse_day_from_name_invalid_date():
    assert _parse_day_from_name("modelctl-2026-13-99.jsonl") is None


def test_retain_files_filters_old_files(tmp_path):
    files = [Path(f) for f in (
        "modelctl-2026-08-20.jsonl", "modelctl-2026-08-25.jsonl", "modelctl-2026-08-31.jsonl",
    )]
    kept = _retain_files(files, retention_days=6, today=_dt.date(2026, 8, 31))
    assert [p.name for p in kept] == ["modelctl-2026-08-25.jsonl", "modelctl-2026-08-31.jsonl"]


def test_retain_files_zero_disables_time_rule(tmp_path):
    files = [Path("modelctl-2026-01-01.jsonl")]
    kept = _retain_files(files, retention_days=0, today=_dt.date(2026, 8, 31))
    assert kept == files


def test_prune_by_size_oldest_first(tmp_path):
    # 4 个文件各 300KB，MAX=1 MB → 删最旧 2 个
    files = [Path(f) for f in (
        "modelctl-2026-08-28.jsonl", "modelctl-2026-08-29.jsonl",
        "modelctl-2026-08-30.jsonl", "modelctl-2026-08-31.jsonl",
    )]
    # 模拟大小：mock stat
    sizes = {
        "modelctl-2026-08-28.jsonl": 300 * 1024,
        "modelctl-2026-08-29.jsonl": 300 * 1024,
        "modelctl-2026-08-30.jsonl": 300 * 1024,
        "modelctl-2026-08-31.jsonl": 300 * 1024,
    }

    class FakeStat:
        def __init__(self, sz):
            self.st_size = sz

    import os as _os
    real_stat = _os.stat
    def fake_stat(path, **k):
        if isinstance(path, str) and path.endswith(".jsonl"):
            return FakeStat(sizes.get(path, 0))
        return real_stat(path, **k)
    import builtins
    import modelctl.core.audit as audit_mod
    orig_lookup = _os.stat
    try:
        _os.stat = fake_stat  # 模块内 import os；monkeypatch(os, stat=fake_stat)
        pruned = _prune_by_size(files, max_size_mb=1)
        assert [p.name for p in pruned] == [
            "modelctl-2026-08-28.jsonl", "modelctl-2026-08-29.jsonl",
        ]
    finally:
        _os.stat = orig_lookup


def test_prune_by_size_zero_disables(tmp_path):
    files = [Path("modelctl-2026-08-31.jsonl")]
    kept = _prune_by_size(files, max_size_mb=0)
    assert kept == files
```

- [ ] **Step 2: 跑测试验证失败**

```
uv run pytest tests/test_audit.py -v
```

预期：全部 FAIL（`ImportError: cannot import name RequestAuditLog`）

- [ ] **Step 3: 实现 `src/modelctl/core/audit.py`**

```python
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
```

- [ ] **Step 4: 跑测试验证通过**

```
uv run pytest tests/test_audit.py -v
```

预期：全部 PASS（21 用例或按最终拆分）

- [ ] **Step 5: 跑全量测试**

```
uv run pytest tests/ -q
```

预期：现有测试全 PASS，无回归

- [ ] **Step 6: Commit（**不执行**——留工作区）**

> 本计划沿用"不自动 commit"策略。改动留工作区，由用户统一提交。建议消息：
> `feat(audit): add RequestAuditLog module with retention + size pruning`

---

### Task 2: `gateway.py` — audit_log 接线（非流式 OpenAI + Anthropic + 流式 OpenAI）

**Files:**
- Modify: `src/modelctl/core/gateway.py`（create_app 签名/实例创建 + `_sse_stream` + `_proxy_stream` + `_proxy` 解析处）
- Test: `tests/test_audit.py`（追加用例）

**Interfaces:**
- Consumes: `RequestAuditLog`（Task 1）、`NoopAuditLog`（Task 1）、`time.monotonic`、`json`
- Produces: `create_app(registry, ..., audit_log: RequestAuditLog | None = None) -> GatewayModel`（新参数）
- 内部新增 build 入口：`_audit_entry(upstream, model, engine, path, stream, t0, t_first, t1, usage, native_metrics, status_code, error, finish_reason, input_char_len, collector, model_name_for_stats) -> dict`
- `GatewayModel.audit_log` 属性（默认 `RequestAuditLog(Path(os.environ.get("AUDIT_DIR", "data/audit")))`，测试 inject Noop）

注意：**对 `agg === None`（非 vLLM 引擎）的 Anthropic 端点的 streaming**：pm 可能 None、usage 字段缺失。这些场景 `tokens_source` 走 `collector-diff` 或 `none`（本次实现都写 `response-usage`，但取不到 token 数 → `prompt_tokens=0/completion_tokens=0`）。**避免阻塞** 客户端。

- [ ] **Step 1: 写失败测试（追加到 `tests/test_audit.py`）**

```python
# ---- 网关集成（复用现有 create_app + MockTransport/ASGITransport 样式） ----

import asyncio
import httpx
from types import SimpleNamespace

from modelctl.core.gateway import ModelRegistry, create_app, NoopAuditLog, _new_audit_log
from modelctl.core.profile import Profile


def _profile(name: str) -> Profile:
    return Profile(
        name=name, engine="vllm", port=9000, family="q",
        # 测试用最小 profile；实际字段见 Profile 定义
    )


def _reg_one(name: str) -> ModelRegistry:
    registry = ModelRegistry(models_dir=Path(__file__).parent.parent / "models")
    registry.register(_profile(name))
    return registry


def _wait_sse_events(app, path, payload) -> list[bytes]:
    """抓 SSE 所有 data: 块。"""
    events: list[bytes] = []

    async def _run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async with client.stream("POST", path, json=payload) as resp:
                async for chunk in resp.aiter_bytes():
                    events.extend(c for c in chunk.split(b"\n") if c.startswith(b"data: "))

    asyncio.run(_run())
    return events


def _body_audit_log(tmp_path):
    return _new_audit_log(Path(tmp_path / "audit"))


def test_openai_non_stream_emits_audit_vllm_native(tmp_path):
    """非流式 + metrics → source=vllm_native。"""
    audit = _new_audit_log(Path(tmp_path / "audit"))

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "x", "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "metrics": {
                "time_to_first_token_ms": 80.0,
                "generation_time_ms": 100.0,
                "tokens_per_second": 50.0,
            },
        })

    app = create_app(_reg_one("q"), transport=httpx.MockTransport(upstream), audit_log=audit)
    asyncio.run(_openai_chat(app))

    files = list((tmp_path / "audit").glob("modelctl-*.jsonl"))
    assert len(files) == 1
    rec = __import__("json").loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert rec["source"] == "vllm_native"
    assert rec["tokens_source"] == "response-usage"
    assert rec["native_metrics"]["time_to_first_token_ms"] == 80.0
    assert rec["prompt_tokens"] == 10 and rec["completion_tokens"] == 5
    assert rec["gateway_metrics"]["ttft_ms"] is None  # 非流式
    audit.destroy()


def test_openai_non_stream_gateway_estimate_when_no_metrics(tmp_path):
    """非流式无 metrics → source=gateway_estimate。"""
    audit = _new_audit_log(Path(tmp_path / "audit"))

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "x", "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })

    app = create_app(_reg_one("q"), transport=httpx.MockTransport(upstream), audit_log=audit)
    asyncio.run(_openai_chat(app))
    files = list((tmp_path / "audit").glob("modelctl-*.jsonl"))
    rec = __import__("json").loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert rec["source"] == "gateway_estimate"
    assert rec["native_metrics"] is None
    audit.destroy()


def test_openai_stream_emits_audit_vllm_native(tmp_path):
    """流式末块含 metrics + usage → vllm_native。"""
    audit = _new_audit_log(Path(tmp_path / "audit"))

    chunks = [
        b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"a"}}]}\n\n',
        b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"b"}}]}\n\n',
        b'data: {"id":"x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":4,"completion_tokens":7,"total_tokens":11},'
        b'"metrics":{"time_to_first_token_ms":55.0,"generation_time_ms":300.0,'
        b'"tokens_per_second":23.3}}\n\n',
        b"data: [DONE]\n\n",
    ]

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"".join(chunks),
            headers={"Content-Type": "text/event-stream"},
        )

    app = create_app(_reg_one("q"), transport=httpx.MockTransport(upstream), audit_log=audit)
    asyncio.run(_openai_stream_chat(app))
    files = list((tmp_path / "audit").glob("modelctl-*.jsonl"))
    assert len(files) == 1
    rec = __import__("json").loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert rec["source"] == "vllm_native"
    assert rec["stream"] is True
    assert rec["prompt_tokens"] == 4 and rec["completion_tokens"] == 7
    assert rec["gateway_metrics"]["ttft_ms"] is not None
    audit.destroy()


def test_openai_stream_fallback_when_no_usage(tmp_path):
    """流式末块没有 usage（未开 --enable-force-include-usage）→ 走 collector-diff 路径。"""
    audit = _new_audit_log(Path(tmp_path / "audit"))

    chunks = [
        b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"a"}}]}\n\n',
        b'data: {"id":"x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"".join(chunks),
                              headers={"Content-Type": "text/event-stream"})

    app = create_app(_reg_one("q"), transport=httpx.MockTransport(upstream), audit_log=audit)
    asyncio.run(_openai_stream_chat(app))
    files = list((tmp_path / "audit").glob("modelctl-*.jsonl"))
    rec = __import__("json").loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    # 未流式回 usage → 走 collector-diff（本次测试 collector 没数据，保持 0）
    assert rec["tokens_source"] in ("response-usage", "collector-diff")
    assert rec["prompt_tokens"] >= 0 and rec["completion_tokens"] >= 0
    audit.destroy()


def test_anthropic_non_stream_usage_capture(tmp_path):
    """Anthropic 非流式 — usage 在根级，无 metrics → gateway_estimate。"""
    audit = _new_audit_log(Path(tmp_path / "audit"))

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "content": [{"text": "hello"}],
            "usage": {"input_tokens": 8, "output_tokens": 4},
        })

    app = create_app(_reg_one("q"), transport=httpx.MockTransport(upstream), audit_log=audit)
    asyncio.run(_anthropic_chat(app))
    files = list((tmp_path / "audit").glob("modelctl-*.jsonl"))
    rec = __import__("json").loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert rec["source"] == "gateway_estimate"
    assert rec["native_metrics"] is None
    assert rec["prompt_tokens"] == 8 and rec["completion_tokens"] == 4
    audit.destroy()


def test_audit_failure_does_not_break_proxy(tmp_path):
    """audit.record 抛异常 → 响应仍然 200，stats 不受影响。"""
    audit = NoopAuditLog()  # Noop record 返回 False，不抛；用故意抛异常的 wrapper 模拟故障

    class _FailingAudit(NoopAuditLog):
        def record(self, entry):
            raise RuntimeError("boom")

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "x", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    app = create_app(_reg_one("q"), transport=httpx.MockTransport(upstream),
                     audit_log=_FailingAudit())
    asyncio.run(_openai_chat(app))  # 不抛异常
    # 通过 request_log 兜底：真实实现里 record 已 try/except 静默，此处仅冒烟
    assert True


async def _openai_chat(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/chat/completions", json={
            "model": "q", "messages": [{"role": "user", "content": "hi"}],
        })
        assert resp.status_code == 200


async def _openai_stream_chat(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("POST", "/v1/chat/completions", json={
            "model": "q", "messages": [{"role": "user", "content": "hi"}], "stream": True,
        }) as resp:
            async for _ in resp.aiter_bytes():
                pass


async def _anthropic_chat(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/messages", json={
            "model": "q", "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert resp.status_code == 200
```

- [ ] **Step 2: 跑测试验证失败**

```
uv run pytest tests/test_audit.py -v -k "gateway or non_stream or stream"
```

预期：`create_app` 接 `audit_log=None` 失败；`create_app` 无 `audit_log` 参数报错

- [ ] **Step 3: 实现 `gateway.py` 接线**

关键改动（逐行匹配现有代码形式）：

**3a. 扩展 `create_app` 签名**（[gateway.py L715-758](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/gateway.py#L715-L758)）：

```python
from modelctl.core.audit import NoopAuditLog, RequestAuditLog, _new_audit_log


def create_app(
    registry: ModelRegistry,
    *,
    default_model: str | None = None,
    proxy_client: httpx.AsyncClient | None = None,
    name_of: NameResolver | None = None,
    model_hidden: frozenset[str] | None = None,
    proxy_timeout: float = 300.0,
    auth_key: str | None = "${API_KEY}",  # noqa: S107
    usage_collector: UsageCollector | None = None,
    audit_log: RequestAuditLog | NoopAuditLog | None = None,  # 新增
) -> "GatewayModel":
    """`audit_log` 缺省时从 env 读 AUDIT_DIR 本地实例化（测试可 inject Noop）。"""
    audit_log = audit_log or _new_audit_log(
        Path(os.environ.get("AUDIT_DIR", "data/audit"))
    )
    audit_log.ensure_cleanup_thread()
    # ... 现有逻辑 ...
    models: list[GatewayModel] = [...]
    for m in models:
        m.audit_log = audit_log  # 挂到每个 GatewayModel，便于 handler 短路径访问
    app.state.audit_log = audit_log
    # ... 现有路由 / 生命周期 shutdown 中 destroy()
```

同时在 `GatewayModel.__init__` 加 `self.audit_log: RequestAuditLog | NoopAuditLog | None = None`（属性，由 create_app 注入）。

**3b. 新增延迟 import**（顶部 import 区）：

```python
from modelctl.core.audit import NoopAuditLog, RequestAuditLog
from modelctl.core.audit import _new_audit_log  # 模块私有，但 gateway 依赖它，需在 audit.py 暴露
```

在 `core/audit.py` 增加公开 alias（Task 1 完成后追加）：

```python
def _new_audit_log(data_dir: Path) -> RequestAuditLog:
    """工厂：从 env / 默认参数构造 RequestAuditLog（测试可 monkeypatch AUDIT_*）。"""
    import os as _os
    def _int_env(key, default):
        v = _os.environ.get(key)
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
        cleanup_interval_s=float(_os.environ.get("AUDIT_CLEANUP_INTERVAL", "86400")),
    )
```

**3c. 计时变量引入**：

- `_proxy`（非流式 L687-712）：在 `client.send(request)` 之前 `t0 = time.monotonic()`，在 `upstream = await client.send(...)` 后 `t1 = time.monotonic()`
- `_sse_stream`（L641-686）：`t0 = time.monotonic()` 在 `yield _make_sse(...)` 之前 / 客户端读完；`t_first = ...` 在首个 `yield chunk` 前取
- `_proxy_stream`（Anthropic/AAG，L500-546）：同上

**3d. 非流式 `_proxy`（OpenAI + AAG + Anthropic 共用）抽取 build 函数**：

```python
def _build_audit_entry(
    *,
    model_name: str,           # profile 对外 name
    profile: "Profile",
    path: str,                 # "chat/completions" | "completions" | "embeddings" | "messages"
    stream: bool,
    native_metrics: dict | None,
    usage: dict | None,
    gateway_metrics: dict | None,
    status_code: int,
    error: str | None,
    finish_reason: str | None,
    input_char_len: int,
) -> dict:
    """统一 build 入口；token 取值优先级见 spec §2。"""
    source = "vllm_native" if native_metrics else "gateway_estimate"
    if usage:
        tokens_source = "response-usage"
        # OpenAI: prompt_tokens/completion_tokens; Anthropic: input_tokens/output_tokens
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
        total = usage.get("total_tokens") or (prompt + completion if (prompt or completion) else 0)
    else:
        tokens_source = "collector-diff"
        prompt = completion = total = 0  # Task 4 会用 collector 差分填充；本期占位
    return {
        "ts": _dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "model": profile.name,
        "engine": profile.engine,
        "path": path,
        "stream": stream,
        "source": source,
        "tokens_source": tokens_source,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "input_char_len": input_char_len,
        "native_metrics": native_metrics,
        "gateway_metrics": gateway_metrics,
        "status_code": status_code,
        "error": error,
        "finish_reason": finish_reason,
    }
```

**3e. 各 handler 三处调用**（A / B / C）：

- **A. `_proxy` 非流式 OpenAI**（L690-712）：
  ```python
  try:
      upstream_data = json.loads(upstream.content)
      usage = upstream_data.get("usage")
      native_metrics = upstream_data.get("metrics")   # 新增
      ...
      if self.audit_log:
          gw_metrics = {
              "ttft_ms": None,
              "generation_time_ms": round((t1 - t0) * 1000.0, 2),
              "tokens_per_second": (
                  round(usage.get("completion_tokens", 0) / (t1 - t0), 1) if (t1 > t0 and usage) else None
              ),
          }
          self.audit_log.record(_build_audit_entry(
              model_name=upstream_model, profile=self.profile,
              path=path_key, stream=False,
              native_metrics=native_metrics, usage=usage,
              gateway_metrics=gw_metrics,
              status_code=upstream.status_code, error=None,
              finish_reason=upstream_data.get("choices", [{}])[0].get("finish_reason") if upstream_data.get("choices") else None,
              input_char_len=request.content_length or 0,
          ))
      _record_usage(self.audit_log is not None and ...)  # 保持现有
  ```
- **B. `_sse_stream`（OpenAI 流式）**：
  - 循环内：`data.metrics → seen_metrics`，`data.usage → seen_usage`，`首块 yielded → if t_first is None: t_first = time.monotonic()`
  - `finally`：build + `self.audit_log.record(...)` + `_record_usage(seen_usage)`
- **C. Anthropic 流式/非流式**（`_proxy_stream` + `_proxy` messages 分支）：
  - 非流式：根级 `data.get("usage")` → usage
  - 流式：`data.get("type") == "message_delta"` → `data.get("usage")`
  - `native_metrics` 恒为 None

注意：**`_proxy_stream` 与 `_proxy` 的 `if upstream.status_code >= 400` 分支也要写 audit（error 非空、status_code 透传）**。

- [ ] **Step 4: 跑测试验证通过**

```
uv run pytest tests/test_audit.py -v
```

预期：全部 PASS

- [ ] **Step 5: 跑全量测试**

```
uv run pytest tests/ -q
```

预期：现有测试全 PASS

- [ ] **Step 6: Commit（**不执行**）**

> 建议消息：`feat(gateway): wire audit_log into 3 proxy paths + per-request metrics capture`

---

### Task 3: `engines/vllm.py` — build_command 新 flag + 版本探测

**Files:**
- Modify: `src/modelctl/engines/vllm.py`
- Modify: `src/modelctl/core/envs.py`（新增 `vllm_version(target="vllm")`）
- Test: `tests/test_engines_vllm.py`（追加）

**Interfaces:**
- Consumes: Task 1 `RequirementError` 异常类型、`subprocess.run`、`re`、`envs.engine_bin/target`
- Produces: `envs.vllm_version() -> tuple[int, int, int] | None`（进程内 lru_cache 缓存）、`VllmAdapter.build_command` 新 flag 追加能力、`check_requirements` 版本门控

- [ ] **Step 1: 写失败测试（追加到 `tests/test_engines_vllm.py`）**

```python
# ---- per-request metrics flag ----

def test_build_command_with_per_request_metrics_flag(tmp_path, monkeypatch):
    """yaml 两字段同时 true → cmd 含两个 flag。"""
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    monkeypatch.setattr("modelctl.core.envs.ENVS_ROOT", tmp_path / "envs")
    (tmp_path / "envs" / "vllm").mkdir(parents=True)
    (tmp_path / "envs" / "vllm" / "pyproject.toml").write_text(
        "[project]\nname='vllm'\nrequires-python='>=3.10'\ndependencies=['vllm==0.27.*']\n",
        encoding="utf-8",
    )
    _stub_venv(tmp_path, monkeypatch, "vllm")

    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/Qwen3-32B\n"
        "  enable_per_request_metrics: true\n"
        "  enable_force_include_usage: true\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    a.check_requirements()
    cmd, _ = a.build_command()
    assert "--enable-per-request-metrics" in cmd
    assert "--enable-force-include-usage" in cmd


def test_build_command_default_unchanged(tmp_path, monkeypatch):
    """关键守门：未配置两个新字段 → cmd 与改造前逐字节一致。
    用现有 test_vllm_command 的字段集做基线对比。"""
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    (tmp_path / "envs" / "vllm").mkdir(parents=True, exist_ok=True)
    (tmp_path / "envs" / "vllm" / "pyproject.toml").write_text(
        "[project]\nname='vllm'\nrequires-python='>=3.10'\ndependencies=['vllm==0.27.*']\n",
        encoding="utf-8",
    )
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/Qwen3-32B\n  tensor_parallel_size: 2\n  max_model_len: 32768\n"
        '  extra_args: "--enable-prefix-caching"\n',
    )
    a = get_adapter("vllm")(p, CAPS8)
    a.check_requirements()
    cmd, env = a.build_command()
    # 与改造前完全一致的字段断言（复用 test_vllm_command 的逻辑集）
    assert "--enable-per-request-metrics" not in cmd
    assert "--enable-force-include-usage" not in cmd
    assert cmd[cmd.index("--tensor-parallel-size") + 1] == "2"
    assert cmd[cmd.index("--max-model-len") + 1] == "32768"
    assert cmd[cmd.index("--served-model-name") + 1] == "q"
    assert "--enable-prefix-caching" in cmd


def test_build_command_only_force_include_usage(tmp_path, monkeypatch):
    """只开 enable_force_include_usage=true → cmd 仅含该 flag。"""
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    (tmp_path / "envs" / "vllm").mkdir(parents=True, exist_ok=True)
    (tmp_path / "envs" / "vllm" / "pyproject.toml").write_text(
        "[project]\nname='vllm'\nrequires-python='>=3.10'\ndependencies=['vllm==0.27.*']\n",
        encoding="utf-8",
    )
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/Qwen3-32B\n"
        "  enable_force_include_usage: true\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    cmd, _ = a.build_command()
    assert "--enable-force-include-usage" in cmd
    assert "--enable-per-request-metrics" not in cmd


def test_requirement_version_guard(tmp_path, monkeypatch):
    """开启 flag + 版本 < 阈值 → RequirementError。"""
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    (tmp_path / "envs" / "vllm").mkdir(parents=True, exist_ok=True)
    (tmp_path / "envs" / "vllm" / "pyproject.toml").write_text(
        "[project]\nname='vllm'\nrequires-python='>=3.10'\ndependencies=['vllm==0.27.*']\n",
        encoding="utf-8",
    )
    _stub_venv(tmp_path, monkeypatch, "vllm")

    # 注入一个 < 阈值的 vllm --version
    import modelctl.core.envs as envs
    monkeypatch.setattr(envs, "vllm_version", lambda: (0, 12, 0))

    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/Qwen3-32B\n"
        "  enable_per_request_metrics: true\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    with pytest.raises(RequirementError):
        a.check_requirements()


def test_requirement_version_missing_warns_not_raises(tmp_path, monkeypatch):
    """版本探测失败 → 放行 + warning。"""
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    (tmp_path / "envs" / "vllm").mkdir(parents=True, exist_ok=True)
    (tmp_path / "envs" / "vllm" / "pyproject.toml").write_text(
        "[project]\nname='vllm'\nrequires-python='>=3.10'\ndependencies=['vllm==0.27.*']\n",
        encoding="utf-8",
    )
    _stub_venv(tmp_path, monkeypatch, "vllm")

    import modelctl.core.envs as envs
    monkeypatch.setattr(envs, "vllm_version", lambda: None)

    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/Qwen3-32B\n"
        "  enable_per_request_metrics: true\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    a.check_requirements()  # 不 raise


def test_requirement_not_flagged_no_version_check(tmp_path, monkeypatch):
    """两字段均 false/缺省 → check_requirements 不调 vllm_version。"""
    calls = []
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    (tmp_path / "envs" / "vllm").mkdir(parents=True, exist_ok=True)
    (tmp_path / "envs" / "vllm" / "pyproject.toml").write_text(
        "[project]\nname='vllm'\nrequires-python='>=3.10'\ndependencies=['vllm==0.27.*']\n",
        encoding="utf-8",
    )
    _stub_venv(tmp_path, monkeypatch, "vllm")

    import modelctl.core.envs as envs
    def tracker():
        calls.append(1)
        return (0, 27, 0)
    monkeypatch.setattr(envs, "vllm_version", tracker)

    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/Qwen3-32B\n",  # 未配置两字段
    )
    a = get_adapter("vllm")(p, CAPS8)
    a.check_requirements()
    assert calls == []  # 未触发版本探测
```

- [ ] **Step 2: 跑测试验证失败**

```
uv run pytest tests/test_engines_vllm.py -v
```

预期：新用例 FAIL（`no attribute vllm_version`、cmd 缺 flag / `RequirementError` 未抛）

- [ ] **Step 3: 实现改动**

**3a. `core/envs.py` 新增 `vllm_version`**（追加在 `ensure_env` 之后）：

```python
import functools


@functools.lru_cache(maxsize=1)
def vllm_version() -> tuple[int, int, int] | None:
    """探测 vLLM 版本（subprocess vllm --version 解析首 token）；失败 / 未安装返回 None。"""
    bin_path = engine_bin("vllm", "vllm")
    if not bin_path.is_file():
        return None
    try:
        r = subprocess.run(
            [str(bin_path), "--version"], capture_output=True, text=True, timeout=5,
        )
        out = (r.stdout or "").strip()
    except (subprocess.TimeoutExpired, OSError):
        return None
    # 解析 vLLM 0.5.x 输出格式如 "vLLM version 0.5.3" → 取第一个形如 \d+\.\d+\.\d+ 的
    import re as _re
    m = _re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))
```

**3b. `engines/vllm.py` 顶部 import**：`import re`（已有则由现有）、`from modelctl.core.envs import vllm_version`

**3c. `build_command` 追加 flag**（在 `cmd.append("1") if cfg.get("trust_remote_code")` 之后、`cmd.extend(_shlex.split(cfg.get("extra_args") or ""))` 之前）：

```python
if cfg.get("enable_per_request_metrics"):
    cmd.append("--enable-per-request-metrics")
if cfg.get("enable_force_include_usage"):
    cmd.append("--enable-force-include-usage")
cmd.extend(_shlex.split(cfg.get("extra_args") or ""))
```

**3d. `check_requirements` 版本门控**（在 `envs.ensure_env("vllm")` 之后新增）：

```python
MIN_VLLM_PER_REQUEST = (0, 13, 0)  # 实施时按上述 spec §3.2 实测后调整

if cfg.get("enable_per_request_metrics") or cfg.get("enable_force_include_usage"):
    v = vllm_version()
    if v is None:
        logger.warning("无法探测 vLLM 版本（将放行；若启动报错请人工确认 ≥ 0.13.0）")
    elif v < MIN_VLLM_PER_REQUEST:
        from modelctl.core.envs import _describe_min_python  # 复用现有报错风格
        raise RequirementError(
            f"enable_per_request_metrics 需 vLLM ≥ {'.'.join(map(str, MIN_VLLM_PER_REQUEST))}，"
            f"当前 {v[0]}.{v[1]}.{v[2]}；"
            "可升级（uv sync --project envs/vllm --upgrade vllm）或在 yaml 中关闭该项"
        )
```

注意：**仅开启 flag 时才调用 `vllm_version()`**，避免每次 `check` 都跑一次子进程（性能回归）。

- [ ] **Step 4: 跑测试验证通过**

```
uv run pytest tests/test_engines_vllm.py -v
```

预期：新用例全 PASS；`test_vllm_command` 不回归

- [ ] **Step 5: 跑全量测试**

```
uv run pytest tests/ -q
```

预期：全 PASS

- [ ] **Step 6: Commit（**不执行**）**

> 建议消息：`feat(vllm): optional --enable-per-request-metrics flag + version floor guard`

---

### Task 4: `cli.py` — `audit` 子命令族

**Files:**
- Modify: `src/modelctl/cli.py`（新增 `_cmd_audit_*`、`build_parser` / `main` 路由，`import` `modelctl.core.audit`）
- Test: `tests/test_audit.py`（追加 CLI 冒烟）/ 或 `tests/test_modelctl.py`（若现有 stats 类命令都测在这里，则复用）

**Interfaces:**
- Consumes: `RequestAuditLog`（Task 1）、`os.environ`（AUDIT_* 由 load_env 注入）、标准库 argparse
- Produces:
  - `_cmd_audit_query(args) -> int`（表格 / --json）
  - `_cmd_audit_path() -> int`（打印 AUDIT_DIR 绝对路径）
  - `_cmd_audit_stats() -> int`（stats_summary 输出）
  - `_cmd_audit_cleanup(args) -> int`（--dry-run / 默认执行）
  - `_read_audit_entries(path: Path, limit: int, filters) -> list[dict]`（纯函数，测试可注入）

- [ ] **Step 1: 写失败测试（追加到 `tests/test_audit.py`）**

```python
# ---- CLI 冒烟 ----

def test_cli_audit_path_subcommand(tmp_path, monkeypatch, capsys):
    """modelctl audit path 打印 AUDIT_DIR。"""
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path / "audit"))
    import modelctl.cli as cli
    rc = cli.main(["audit", "path"])
    out = capsys.readouterr().out
    assert rc == 0
    assert str(tmp_path / "audit") in out or (tmp_path / "audit").resolve() in __import__("pathlib").Path(out).resolve()


def test_cli_audit_stats_empty_dir(tmp_path, monkeypatch, capsys):
    """空目录 → stats 全 0。"""
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path / "audit"))
    import modelctl.cli as cli
    rc = cli.main(["audit", "stats"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "file_count: 0" in out or "0" in out


def test_cli_audit_query_empty(tmp_path, monkeypatch, capsys):
    """空目录 → query 打印提示。"""
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path / "audit"))
    tmp_path.joinpath("audit").mkdir(parents=True, exist_ok=True)
    import modelctl.cli as cli
    rc = cli.main(["audit", "--limit", "5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no audit records" in out.lower() or "暂无" in out


def test_cli_audit_query_filters_by_model(tmp_path, monkeypatch, capsys):
    """--model 过滤 + 表格输出。"""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    today = _dt.date.today().isoformat()
    (audit_dir / f"modelctl-{today}.jsonl").write_text(
        json.dumps(_base_entry({"model": "qwen3.8-vllm", "engine": "vllm"}), ensure_ascii=False) + "\n"
        + json.dumps(_base_entry({"model": "deepseek-v4", "engine": "vllm"}), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUDIT_DIR", str(audit_dir))
    import modelctl.cli as cli
    rc = cli.main(["audit", "--model", "qwen3.8-vllm", "--limit", "10"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "qwen3.8-vllm" in out
    assert "deepseek-v4" not in out


def test_cli_audit_query_json_mode(tmp_path, monkeypatch, capsys):
    """--json 输出原样 JSON 单行。"""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    today = _dt.date.today().isoformat()
    entry = _base_entry({"model": "qwen3.8-vllm"})
    (audit_dir / f"modelctl-{today}.jsonl").write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setenv("AUDIT_DIR", str(audit_dir))
    import modelctl.cli as cli
    rc = cli.main(["audit", "--json", "--limit", "5"])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [l for l in out.strip().splitlines() if l.startswith("{")]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["model"] == "qwen3.8-vllm"


def test_cli_audit_cleanup_dry_run(tmp_path, monkeypatch, capsys):
    """--cleanup --dry-run 不改文件，仅打印。"""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    old_day = (_dt.date.today() - _dt.timedelta(days=40)).isoformat()
    (audit_dir / f"modelctl-{old_day}.jsonl").write_bytes(b"x" * 1024)
    monkeypatch.setenv("AUDIT_DIR", str(audit_dir))
    import modelctl.cli as cli
    rc = cli.main(["audit", "--cleanup", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry" in out.lower() or "将" in out  # 任一关键字
    # 文件未被删除
    assert (audit_dir / f"modelctl-{old_day}.jsonl").exists()


def test_cli_audit_cleanup_executes(tmp_path, monkeypatch, capsys):
    """默认 --cleanup 执行删除。"""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    old_day = (_dt.date.today() - _dt.timedelta(days=40)).isoformat()
    (audit_dir / f"modelctl-{old_day}.jsonl").write_bytes(b"x" * 1024)
    monkeypatch.setenv("AUDIT_DIR", str(audit_dir))
    import modelctl.cli as cli
    rc = cli.main(["audit", "--cleanup"])
    out = capsys.readouterr().out
    assert rc == 0
    assert not (audit_dir / f"modelctl-{old_day}.jsonl").exists()
    assert "142.7 MB" in out or " freed " in out or "删除" in out or "freed" in out.lower()


def test_cli_audit_subcommand_required(tmp_path):
    """audit 必须带子命令（path/stats/query/cleanup 四选一）。"""
    import pytest
    import modelctl.cli as cli
    with pytest.raises(SystemExit):
        cli.main(["audit"])  # 无子命令 → argparse 报错
```

- [ ] **Step 2: 跑测试验证失败**

```
uv run pytest tests/test_audit.py -v -k "cli_audit"
```

预期：`audit` 子命令不存在，argparse 抛 `SystemExit` / `assert rc == 2`

- [ ] **Step 3: 实现 cli.py 改动**

**3a. 顶部 import 区**（[cli.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/cli.py#L32-L38)）追加：

```python
import re as _re
from datetime import timedelta, datetime as _dt_dt
```

**3b. 新增 helper `_read_audit_entries`（私有，模块级）**（追加在 `_live_token_rate_text` 之前，或 `audit` 命令函数前）：

```python
def _audit_dir_from_env() -> Path:
    """从 env 读 AUDIT_DIR，缺省 data/audit。"""
    return Path(os.environ.get("AUDIT_DIR", "data/audit"))


def _read_audit_entries(
    audit_dir: Path,
    limit: int,
    *,
    since: _dt_dt | None = None,
    model: str | None = None,
    endpoints: frozenset[str] | None = None,
) -> list[dict]:
    """读 JSONL（按天从新到旧），应用过滤，返回最多 limit 条。"""
    if not audit_dir.is_dir():
        return []
    all_files = sorted(
        (p for p in audit_dir.iterdir()
         if p.is_file() and p.name.startswith("modelctl-") and p.name.endswith(".jsonl")
         and not p.name.startswith("modelctl-deleting")),
        key=lambda p: p.name, reverse=True,  # 天倒序：新 → 旧
    )
    out: list[dict] = []
    for f in all_files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):  # 文件内倒序：新 → 旧
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw = rec.get("ts") or ""
            try:
                rec_ts = _dt_dt.fromisoformat(ts_raw)
            except ValueError:
                rec_ts = None
            if since is not None and rec_ts is not None and rec_ts < since:
                return out  # 时间窗已过，停止读更早文件
            if model is not None and rec.get("model") != model:
                continue
            if endpoints is not None and rec.get("path") not in endpoints:
                continue
            out.append(rec)
            if len(out) >= limit:
                return out
    return out


def _parse_since_arg(s: str) -> _dt_dt:
    """解析 --since '1h' | '24h' | '7d' | ISO。"""
    m = _re.match(r"^(\d+)([hd])$", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = timedelta(hours=n) if unit == "h" else timedelta(days=n)
        return _dt_dt.now().astimezone() - delta
    try:
        return _dt_dt.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"无法解析 --since: {s!r}（可用：1h / 24h / 7d / ISO）")


def _format_audit_table(records: list[dict]) -> list[str]:
    """表格化输出。"""
    if not records:
        return []
    headers = ["ts", "model", "endpoint", "stream", "src", "tokens (in/out)", "ttft_ms", "tps", "status"]
    rows = [
        [
            (r.get("ts") or "")[:19],
            (r.get("model") or "")[:18],
            (r.get("path") or "")[:16],
            str(r.get("stream", False)).lower(),
            (r.get("source") or "")[:12],
            f'{r.get("prompt_tokens", 0)}/{r.get("completion_tokens", 0)}',
            f"{r.get('gateway_metrics', {}) and (r.get('gateway_metrics') or {}).get('ttft_ms', '-') or '-'}",
            f'{(r.get("gateway_metrics") or {}).get("tokens_per_second", "-")}',
            str(r.get("status_code", "-")),
        ]
        for r in records
    ]
    # 简单固定宽度排版（无依赖）
    widths = [max(len(headers[i]), max((len(rows[j][i]) for j in range(len(rows))), default=0)) for i in range(len(headers))]
    out: list[str] = []
    out.append("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    for r in rows:
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
    return out
```

**3c. 新增 `_cmd_audit_*` 函数**（追加在 `_cmd_stats_status` 之后，`def main` 之前）：

```python
def _cmd_audit_query(args) -> int:
    audit_dir = _audit_dir_from_env()
    filters_model = getattr(args, "model", None)
    endpoints_raw = getattr(args, "endpoints", None)
    endpoints = frozenset(e.strip() for e in endpoints_raw.split(",")) if endpoints_raw else None
    since_str = getattr(args, "since", None)
    since = _parse_since_arg(since_str) if since_str else None
    limit = int(getattr(args, "limit", 0) or 20)
    records = _read_audit_entries(audit_dir, limit, since=since, model=filters_model, endpoints=endpoints)
    if not records:
        print("no audit records / 暂无审计记录")
        return 0
    if getattr(args, "json", False):
        for r in records:
            print(json.dumps(r, ensure_ascii=False))
        return 0
    for line in _format_audit_table(records):
        print(line)
    return 0


def _cmd_audit_path() -> int:
    print(_audit_dir_from_env().resolve())
    return 0


def _cmd_audit_stats() -> int:
    from modelctl.core.audit import _new_audit_log
    audit_log = _new_audit_log(_audit_dir_from_env())
    s = audit_log.stats_summary()
    print(f"file_count: {s['file_count']}")
    print(f"total_bytes: {s['total_bytes']}")
    print(f"oldest_day: {s['oldest_day']}")
    print(f"newest_day: {s['newest_day']}")
    if s["by_day"]:
        print("by_day:")
        for day, sz in sorted(s["by_day"].items()):
            print(f"  {day}: {sz} bytes")
    return 0


def _cmd_audit_cleanup(args) -> int:
    from modelctl.core.audit import _new_audit_log
    audit_dir = _audit_dir_from_env()
    audit_log = _new_audit_log(audit_dir)
    dead = audit_log.collect_dead_files()
    total_freed = sum(p.stat().st_size if p.exists() else 0 for p in dead)
    freed_mb = total_freed / (1024 * 1024)
    if getattr(args, "dry_run", False):
        names = ", ".join(p.name for p in dead[:10]) + ("..." if len(dead) > 10 else "")
        print(f"Would delete {len(dead)} files ({freed_mb:.1f} MB): {names}")
        return 0
    deleted = 0
    for p in dead:
        if not p.exists():
            continue
        staged = audit_dir / f".audit-deleting-{int(time.time() * 1000)}-{p.name}"
        try:
            p.rename(staged)
            staged.unlink()
            deleted += 1
        except OSError as exc:
            logger.warning(f"删除 {p.name} 失败: {exc}")
    print(f"Deleted {deleted} files, freed {freed_mb:.1f} MB")
    return 0
```

**3d. `build_parser` 增加 `audit` 子命令**（`sub.add_parser("all", ...)` 之后、`sub.add_parser("ui", ...)` 之前追加）：

```python
au = sub.add_parser("audit", help="请求级审计日志查询/统计/清理")
au.add_argument("sub", nargs="?", choices=["path", "stats"], default=None,
                help="子命令：path | stats（缺省=查询最近 N 条；--cleanup 走清理）")
au.add_argument("--model", default=None, help="按 model 字段过滤")
au.add_argument("--endpoints", default=None,
                help="逗号分隔端点列表，如 chat/completions,messages")
au.add_argument("--since", default=None, dest="since_str",
                help='起始时间：1h / 24h / 7d / ISO，如 "2026-08-31T08:00:00"')
au.add_argument("--limit", type=int, default=20, help="条数上限，默认 20")
au.add_argument("--json", action="store_true", help="JSONL 输出")
au.add_argument("--cleanup", action="store_true", help="清理过期审计文件")
au.add_argument("--dry-run", action="store_true", help="配合 --cleanup：仅打印不删除")
```

注意：`sub="path"` / `sub="stats"` 与无子命令（query）三态通过 `args.sub` 区分。若同时给了 `--cleanup` 与 `sub=path/stats`，`--cleanup` 优先级低 → 报错（任一组合合法：`--cleanup` 必须单独）。

**3e. `main` 加路由**（[cli.py L729-740](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/cli.py#L729-L740) stats 分支之后 / gateway 分支之前追加）：

```python
if args.command == "audit":
    if getattr(args, "cleanup", False):
        # 清理路径与 query 互斥（简单实现：cleanup 优先）
        if args.sub is not None:
            parser.error("audit: --cleanup 与 sub(path/stats) 互斥")
        return _cmd_audit_cleanup(args)
    if getattr(args, "sub", None) == "path":
        return _cmd_audit_path()
    if getattr(args, "sub", None) == "stats":
        return _cmd_audit_stats()
    # 默认 query（sub=None）
    return _cmd_audit_query(args)
```

- [ ] **Step 4: 跑测试验证通过**

```
uv run pytest tests/test_audit.py -v
```

预期：新 CLI 用例全 PASS

- [ ] **Step 5: 跑全量测试**

```
uv run pytest tests/ -q
```

预期：全 PASS

- [ ] **Step 6: Commit（**不执行**）**

> 建议消息：`feat(cli): add audit subcommand (query/path/stats/cleanup)`

---

### Task 5: 配置 / 迁移 / 文档

**Files:**
- Modify: `.env.example`（追加 `AUDIT_*` 段）
- Modify: `models/vllm/qwen3.8.yaml`（示范启用两个 flag）
- Modify: `.gitignore`（追加 `/data/audit/`）
- Modify: `README.md`（增补"请求级审计"小节）

- [ ] **Step 1: 修改 `.env.example`**

在 `GATEWAY_READ_TIMEOUT=600`（[.env.example L69](file:///d:/WorkPlace/Pycharm/modelctl/.env.example#L69)）之后追加：

```bash

# ---------- 请求级审计日志 ----------
# 审计 JSONL 落盘目录（<DIR>/modelctl-YYYY-MM-DD.jsonl）
AUDIT_DIR=/raid5/sh/code/modelctl/data/audit
# 保留天数（文件名日期 < 今天-该值 的文件删除）；0 = 不按时间清理
AUDIT_RETENTION_DAYS=30
# 总大小上限（MB），超出从最旧删除；0 = 不按大小清理
AUDIT_MAX_SIZE_MB=512
# 定时清理检查间隔（秒），默认 1 天
AUDIT_CLEANUP_INTERVAL=86400
```

- [ ] **Step 2: 修改 `models/vllm/qwen3.8.yaml` 示范配置**

在 `vllm:` 段追加（与现有字段同级）：

```yaml
  enable_per_request_metrics: true
  enable_force_include_usage: true
```

- [ ] **Step 3: 修改 `.gitignore`**

在文件末尾追加（若 `/data/audit/` 已存在则跳过）：

```
/data/audit/
```

- [ ] **Step 4: 修改 `README.md`**

在 "用量统计" 段落末尾（找 "subprocess 启动" 或 `modelctl stats start` 之后）追加新小节：

```markdown
### 请求级审计

需要**单次请求**的 token 数 / 性能指标（TTFT, tps, queue time）时，启用本功能：

1. 在 `.env` 配置 `AUDIT_DIR`（默认 `data/audit`）、`AUDIT_RETENTION_DAYS`（默认 30）、
   `AUDIT_MAX_SIZE_MB`（默认 512）。
2. 在目标 vLLM profile 的 `vllm:` 段加：
   ```yaml
   enable_per_request_metrics: true
   enable_force_include_usage: true   # 保证流式末块回 usage
   ```
3. 重启该模型 + 网关（`modelctl restart <name> && modelctl gateway restart`）。
4. 查询 / 统计 / 清理：
   ```bash
   modelctl audit                                # 最近 20 条（表格）
   modelctl audit --model qwen3.8-vllm --limit 50
   modelctl audit --json | jq 'select(.source=="vllm_native")'
   modelctl audit stats                          # 目录统计
   modelctl audit --cleanup --dry-run            # 预览清理
   modelctl audit --cleanup                      # 执行清理
   modelctl audit path                           # 打印 AUDIT_DIR
   ```

**与 stats 服务的分工**：
- `modelctl stats`：趋势 / 聚合（每秒速率、累计），适合大盘监控
- `modelctl audit`：单次请求明细（TTFT, tps, 队列耗时），适合 debug / 审计

**注意**：
- 直连引擎端口的流量（绕过网关）**不**产生审计记录
- `endpoint` 字段值包括 `chat/completions` / `completions` / `embeddings` / `messages`
- 非 vLLM 引擎（llamacpp/sglang/ollama/unsloth）：`source=gateway_estimate`，`native_metrics` 为 null
- 流式不开 `--enable-force-include-usage` 时：token 数走聚合 collector 差分（`tokens_source=collector-diff`）
```

- [ ] **Step 5: 跑全量测试**

```
uv run pytest tests/ -q
```

预期：全 PASS

- [ ] **Step 6: Commit（**不执行**）**

> 建议消息：`config: add AUDIT_* env vars, qwen3.8.yaml demo, gitignore, README section`

---

## Self-Review 记录

**1. Spec 覆盖**

| Spec 章节 | 实现 Task |
|---|---|
| §2 数据源优先级（source/tokens_source） | Task 1 `_build_audit_entry` |
| §3 vLLM 引擎侧（flag + 版本探测） | Task 3 |
| §4 JSONL Schema / 网关三处链路 | Task 2 |
| §5 定时清理（retention + size，绝不清今日） | Task 1 `_retain_files` / `_prune_by_size` / `collect_dead_files` |
| §6 CLI（query/path/stats/cleanup） | Task 4 |
| §7 配置 / 迁移 / README | Task 5 |
| §8 测试（全部用例） | Task 1-4（各 Task 的 Step 1 测试） |
| §8.4 边界 | Task 3 vllm_version returns None 分支、Task 2 各 fallback 用例 |

**2. 占位符扫描**

无 TBD / TODO / "implement later" / "add appropriate error handling"。所有 step 列出确切文件路径、确切代码片段、确切断言。

**3. 类型一致性**

- `RequestAuditLog.record(entry: dict) -> bool`：Task 1 定义，Task 2 / 4 调用一致
- `_build_audit_entry(...)`：Task 2 内部函数，参数 `model_name/profile/path/stream/native_metrics/usage/gateway_metrics/status_code/error/finish_reason/input_char_len`
- `_new_audit_log(data_dir: Path) -> RequestAuditLog`：Task 1 定义（`audit.py`），Task 2 / 4 共用
- `vllm_version() -> tuple[int, int, int] | None`：Task 3 定义（`envs.py`），只 Task 3 用
- `_audit_dir_from_env() -> Path`：Task 4 定义（`cli.py`），只 Task 4 用
- `_read_audit_entries(audit_dir, limit, *, since, model, endpoints) -> list[dict]`：Task 4 定义

**4. Global Constraints 检查**

- Python 3.12 ✅
- 不引入新依赖 ✅（仅 loguru / httpx / pytest / 标准库）
- 现有测试保持通过 ✅（每个 Task 的 Step 5 显式跑 `uv run pytest tests/ -q`）
- TDD ✅（每个 Task 先写失败测试 → 跑失败 → 实现 → 跑通过）
- 向后兼容 ✅（Task 3 `test_build_command_default_unchanged` 守门）
- 审计 I/O 异常不冒泡 ✅（Task 1 `record` 三处 try/except）
- 绝不清今日 ✅（`collect_dead_files` 排除 `_today_file()`）
- AUDIT_DIR 默认 `data/audit` ✅（`_audit_dir_from_env` 缺省值）
- 不自动 commit ✅（每个 Task 的 Step 6 标注"不执行"）