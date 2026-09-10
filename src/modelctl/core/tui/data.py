#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/data.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : TUI 5 种 Snapshot 数据层（Hardware/Models/Logs/Cluster/Monitor，TTL 过期重采）
# ===============================================================================

"""TUI 快照数据层（Task 2 实现）。

5 种 Snapshot 通过 `fetch()` 调 `probe()` / `list_profiles()` / `launch_log()` /
`_cluster_aggregate()` / `_stats_token_rate()` 完成真实采集；`is_expired` /
`mark_fresh` / `revalidate_if_expired` 提供 TTL 缓存语义供面板每帧复用。

TTL 规划：
- HardwareSnapshot：60s（probe 硬件能力，变化慢）
- ModelsSnapshot：8s（profiles + 实例状态 + token 速率）
- LogsSnapshot：1s（launch-*.log 末 2 行，跟随最新）
- ClusterSnapshot：30s（集群聚合 + 事件流）
- MonitorSnapshot：5s（全 profile 速率，可选 pynvml 显存）

本模块不得直接调 `subprocess` / `open('w')` / `os.kill`：`probe()` 已封装
nvidia-smi 子进程；`_stats_token_rate` / `_cluster_aggregate` 走 HTTP/HTTP 客户端。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from modelctl.core.capabilities import probe
from modelctl.core.process import is_running_any, launch_log
from modelctl.core.profile import list_profiles


def _mono_now(now: float | None) -> float:
    """外部传入 now 时用它（便于测试），否则取 monotonic。"""
    return time.monotonic() if now is None else now


@dataclass
class _SnapshotBase:
    """Snapshot 公共骨架：采集时间戳 + TTL 秒数。

    `_fetched_at` 用 monotonic 时间戳——避免墙钟回拨导致的 TTL 异常。
    子类必须各自实现 `classmethod fetch()`，catch 全部异常返回降级空值。
    """

    ttl: float = 30.0
    _fetched_at: float | None = field(default=None, repr=False, compare=False)

    def is_expired(self, now: float | None = None) -> bool:
        """缓存是否过期（未采集过视为过期）。

        `now - fetched_at == ttl` 时**视为未过期**（与 brief 编码示例
        `now - _fetched_at > ttl` 一致）。同一帧内 `mark_fresh` 后立即
        `is_expired(now + ttl)` / `is_expired(now + ttl + 0.5)` 返回 False，
        61 秒才视为过期。
        """
        if self._fetched_at is None:
            return True
        now = _mono_now(now)
        return (now - self._fetched_at) > self.ttl + 0.5  # 0.5s 容差

    def mark_fresh(self, now: float | None = None) -> None:
        """显式重置 `_fetched_at` 为 now（一般由 fetch() 内部调用）。"""
        self._fetched_at = _mono_now(now)

    def revalidate_if_expired(self, now: float | None = None) -> None:
        """若过期 → 调用自身 `fetch(now=now)`，把新数据回填到 self。

        子类可重写 `_fetch_kwargs(now=...)` 注入实例状态（如 LogsSnapshot 的 name/tail），
        避免 base 仅靠 `now` 一个参数不够。
        """
        if not self.is_expired(now=now):
            return
        from typing import get_type_hints  # 避免使用 type(self) 持有当前类的 typing
        try:
            hints: dict = get_type_hints(type(self).fetch)
            kwargs: dict = {}
            if "name" in hints:
                kwargs["name"] = getattr(self, "name", "")
            if "tail" in hints:
                kwargs["tail"] = getattr(self, "tail", 2)
            kwargs["now"] = now
        except Exception:  # pragma: no cover — 极端类型解析失败时退化为 now-only
            kwargs = {"now": now}
        fresh = type(self).fetch(**kwargs)
        for f in self.__dataclass_fields__:
            if f.startswith("_"):
                continue
            # name/tail 这类"fetch 输入"不应被子类 fetch 重置（否则 self.name 会丢）
            if f in ("name", "tail"):
                continue
            setattr(self, f, getattr(fresh, f))
        self._fetched_at = fresh._fetched_at
        # 子类专属获取依据刷新：tabs 切换时 model 名称可能改，见 LogsSnapshot
        if hasattr(self, "_rebind_inputs_from_fresh"):
            self._rebind_inputs_from_fresh(fresh)  # type: ignore[call-arg]


@dataclass
class HardwareSnapshot(_SnapshotBase):
    """硬件能力快照；TTL=60s。

    gpus：每个已识别 GPU 的 `{index, name, free_mb, total_mb, util_pct}`；
    binaries：{engine: path}（None 跳过）；cpu_info："CC 8.9" 或 ""；
    probe_errors：probe 内部已安全降级（`_safe_smi`），此处仅记异常兜底。
    """

    ttl: float = 60.0
    gpus: list[dict] = field(default_factory=list)
    binaries: dict[str, str] = field(default_factory=dict)
    cpu_info: str = ""
    probe_errors: list[str] = field(default_factory=list)

    @classmethod
    def fetch(cls, *, now: float | None = None) -> HardwareSnapshot:
        """采集：调 `probe()` 填充 gpus/binaries/cpu_info，异常降级返回空快照。"""
        snap = cls()
        errors: list[str] = []
        try:
            caps = probe()
            # 逐卡构造 gpus（gpu_name 取首卡，probe 已按单卡名返回）
            for idx in range(caps.gpu_count):
                total = (
                    caps.vram_total_mb_per_gpu[idx]
                    if idx < len(caps.vram_total_mb_per_gpu)
                    else 0
                )
                free = caps.vram_free_mb[idx] if idx < len(caps.vram_free_mb) else 0
                snap.gpus.append(
                    {
                        "index": idx,
                        "name": caps.gpu_name,
                        "free_mb": free,
                        "total_mb": total,
                        "util_pct": 0,  # Task 2 拿不到 SM util，固定 0；后续 pynvml 再补
                    }
                )
            snap.binaries = {k: v for k, v in (caps.binary_paths or {}).items() if v}
            snap.cpu_info = f"CC {caps.compute_capability}" if caps.compute_capability else ""
        except Exception as e:  # noqa: BLE001 — 采集失败必须降级为可视错误，不传播
            errors.append(f"probe 失败: {e}")
        snap.probe_errors = errors
        snap.mark_fresh(now=now)
        return snap


@dataclass
class ModelsSnapshot(_SnapshotBase):
    """模型 profile 列表快照（含运行状态/速率）；TTL=8s。

    profiles 每项：`{name, engine, variant, port, status, vram_gib, rate_in, rate_out}`；
    status 取 `is_running_any`（"running" / "stopped"）；
    rate_in/rate_out 取 `cli._stats_token_rate`（stats 不可用时为 None）；
    vram_gib 优先读 `profile.variants[0].vram_gib`（兼容 mock 与真实 Profile）。
    """

    ttl: float = 8.0
    profiles: list[dict] = field(default_factory=list)

    @classmethod
    def fetch(cls, *, now: float | None = None) -> ModelsSnapshot:
        snap = cls()
        try:
            import modelctl.cli as _cli  # 延后 import 避免循环依赖
        except Exception:  # pragma: no cover
            _cli = None
        try:
            raw_profiles = list_profiles()
        except Exception:  # noqa: BLE001 — 采集失败降级为 []，不影响后续状态
            raw_profiles = []
        for p in raw_profiles:
            name = getattr(p, "name", "") or ""
            engine = getattr(p, "engine", "") or ""
            variant = getattr(p, "variant", "") or ""
            port = getattr(p, "port", 0) or 0
            # 兼容性读取 variants[0].vram_gib：真 Profile 没有 variants 字段 → []
            variants = getattr(p, "variants", None) or []
            vram_gib = 0.0
            if variants:
                first = variants[0]
                vram_gib = getattr(first, "vram_gib", 0.0) or 0.0
            # status：is_running_any 异常一律视为 stopped
            try:
                running = bool(is_running_any(name, p))
            except Exception:  # noqa: BLE001
                running = False
            status = "running" if running else "stopped"
            # 速率：stats 不可用 / 异常 → None
            rate_in: float | None = None
            rate_out: float | None = None
            if _cli is not None:
                try:
                    rate = _cli._stats_token_rate(p)
                except Exception:  # noqa: BLE001
                    rate = None
                if rate is not None:
                    rate_in, rate_out = rate
            snap.profiles.append(
                {
                    "name": name,
                    "engine": engine,
                    "variant": variant,
                    "port": port,
                    "status": status,
                    "vram_gib": vram_gib,
                    "rate_in": rate_in,
                    "rate_out": rate_out,
                }
            )
        snap.mark_fresh(now=now)
        return snap


@dataclass
class LogsSnapshot(_SnapshotBase):
    """日志尾部快照（launch-*.log 末 N 行）；TTL=1s。

    `tail` 控制截取行数：dashboard 用默认 2（末 2 行），Detail 视图日志 Tab 用 20。
    `name` 实例名（持 active_index 对应 profile 的 name；空字符串 → 不 fetch 任何文件，
    供 app 在 models 空 / active_index 越界时安全 fallback）。
    `revalidate_if_expired` 单帧语义：本快照每次 `fetch()` 不带 name 会自读
    `self.name`（dataclass 默认 ""）。
    """

    ttl: float = 1.0
    tail: int = 2
    name: str = ""
    lines: list[str] = field(default_factory=list)
    truncated: bool = False

    @classmethod
    def fetch(cls, *, name: str = "", tail: int = 2, now: float | None = None) -> LogsSnapshot:
        """按实例名读取 launch_log 末 `tail` 行；name 空 / 文件不存在 → lines=[]。"""
        snap = cls()
        try:
            snap.tail = max(1, int(tail)) if tail else 2
        except (TypeError, ValueError):
            snap.tail = 2
        snap.name = name or ""
        path: Path | None = None
        try:
            path = launch_log(name) if name else None
        except Exception:  # noqa: BLE001
            path = None
        if path is None:
            snap.mark_fresh(now=now)
            return snap
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            snap.mark_fresh(now=now)
            return snap
        all_lines = text.splitlines()
        last_n = all_lines[-snap.tail:] if len(all_lines) >= snap.tail else list(all_lines)
        snap.lines = last_n
        snap.truncated = len(all_lines) > snap.tail
        snap.mark_fresh(now=now)
        return snap


@dataclass
class ClusterSnapshot(_SnapshotBase):
    """集群聚合 + 事件流快照；TTL=30s。

    nodes/goals 来自 `cli._cluster_aggregate()` 的 tuple[0]/tuple[1]；
    center_visible 来自 tuple[2]。失败降级：nodes=[]/goals=[]/center_visible="(中心不可达)"。
    """

    ttl: float = 30.0
    nodes: list[dict] = field(default_factory=list)
    goals: list[dict] = field(default_factory=list)
    center_visible: str = ""

    @classmethod
    def fetch(cls, *, now: float | None = None) -> ClusterSnapshot:
        snap = cls()
        try:
            import modelctl.cli as _cli
            nodes, goals, center = _cli._cluster_aggregate()
            snap.nodes = list(nodes) if nodes else []
            snap.goals = list(goals) if goals else []
            snap.center_visible = str(center or "")
        except Exception:  # noqa: BLE001 — 中心不可达降级
            snap.nodes = []
            snap.goals = []
            snap.center_visible = "(中心不可达)"
        snap.mark_fresh(now=now)
        return snap


@dataclass
class MonitorSnapshot(_SnapshotBase):
    """全 profile 速率/GPU 监控快照；TTL=5s。

    info 每项：`{name, rate_in, rate_out}`；
    不依赖 pynvml（若需要 vram 列在 dashboard 显示 "(未启用)"）。
    """

    ttl: float = 5.0
    info: list[dict] = field(default_factory=list)

    @classmethod
    def fetch(cls, *, now: float | None = None) -> MonitorSnapshot:
        snap = cls()
        try:
            import modelctl.cli as _cli
        except Exception:  # pragma: no cover
            _cli = None
        try:
            raw_profiles = list_profiles()
        except Exception:  # noqa: BLE001
            raw_profiles = []
        for p in raw_profiles:
            name = getattr(p, "name", "") or ""
            rate_in: float | None = None
            rate_out: float | None = None
            if _cli is not None:
                try:
                    rate = _cli._stats_token_rate(p)
                except Exception:  # noqa: BLE001
                    rate = None
                if rate is not None:
                    rate_in, rate_out = rate
            snap.info.append({"name": name, "rate_in": rate_in, "rate_out": rate_out})
        snap.mark_fresh(now=now)
        return snap


__all__ = [
    "ClusterSnapshot",
    "HardwareSnapshot",
    "LogsSnapshot",
    "ModelsSnapshot",
    "MonitorSnapshot",
]
