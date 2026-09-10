#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/data.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : TUI 5 种 Snapshot 数据层空壳（Hardware/Models/Logs/Cluster/Monitor，TTL 过期重采）
# ===============================================================================

"""TUI 快照数据层（Task 0 空壳）。

5 种 Snapshot 类均带 `_fetched_at` / `ttl` 字段骨架；
真实采集逻辑（probe / list_profiles / launch_log / _cluster_aggregate /
_stats_token_rate）与 TTL 过期重采在 Task 2 实现。

各快照 TTL 规划（Task 2 落地）：
- HardwareSnapshot：60s（probe 硬件能力）
- ModelsSnapshot：8s（profiles + 实例状态 + token 速率）
- LogsSnapshot：1s（launch-*.log 末 20 行）
- ClusterSnapshot：30s（集群聚合 + 事件流）
- MonitorSnapshot：5s（全 profile 速率，可选 pynvml 显存）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _SnapshotBase:
    """Snapshot 公共骨架：采集时间戳 + TTL 秒数。"""

    ttl: float = 30.0
    _fetched_at: float | None = field(default=None, repr=False, compare=False)

    def is_expired(self, now: float | None = None) -> bool:
        """缓存是否过期（未采集过视为过期）；Task 2 接线重采后使用。"""
        if self._fetched_at is None:
            return True
        now = now if now is not None else time.monotonic()
        return (now - self._fetched_at) >= self.ttl


@dataclass
class HardwareSnapshot(_SnapshotBase):
    """硬件能力快照（probe 结果）；TTL=60s。"""

    ttl: float = 60.0


@dataclass
class ModelsSnapshot(_SnapshotBase):
    """模型 profile 列表快照（含运行状态/速率）；TTL=8s。"""

    ttl: float = 8.0


@dataclass
class LogsSnapshot(_SnapshotBase):
    """日志尾部快照（launch-*.log 末 20 行）；TTL=1s。"""

    ttl: float = 1.0


@dataclass
class ClusterSnapshot(_SnapshotBase):
    """集群聚合 + 事件流快照；TTL=30s。"""

    ttl: float = 30.0


@dataclass
class MonitorSnapshot(_SnapshotBase):
    """全 profile 速率/GPU 监控快照；TTL=5s。"""

    ttl: float = 5.0
