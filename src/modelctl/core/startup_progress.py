#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/startup_progress.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/8 10:00
# @Desc   : 模型启动阶段进度（docker pull 解析 / 模式表 / EMA 计时 / 快照）
# ===============================================================================

"""core/startup_progress.py — 引擎无关的启动阶段进度产出。

不 import docker_setup / all_service / process（避免循环）；只依赖 stdlib + paths。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loguru import logger

# ---------------------------------------------------------------------------
# docker pull 非 TTY 逐层输出解析
# ---------------------------------------------------------------------------

# 层 ID 实测为 12 位小写十六进制；放宽到 [0-9a-z]{3,} 兼容短 fixture 与个别 digest 变体。
# 非层行（Digest:/Status:/latest: Pulling from …）不会命中下游状态分支/进度正则，仍返回 None。
_PULL_LINE = re.compile(r"^(?P<id>[0-9a-z]{3,}):\s+(?P<rest>.*)$")
# Downloading/Extracting 的 "<进度条>? cur/total" —— 进度条可能不存在（非 TTY 纯状态行）
_PROGRESS = re.compile(
    r"(?P<stage>Downloading|Extracting).*?"
    r"(?P<cur>\d+(?:\.\d+)?)\s*(?P<cu>bytes|kB|MB|GB|TB|KiB|MiB|GiB)"
    r"\s*/\s*(?P<total>\d+(?:\.\d+)?)\s*(?P<tu>bytes|kB|MB|GB|TB|KiB|MiB|GiB)",
    re.IGNORECASE,
)
_UNITS = {"bytes": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
          "kib": 1024, "mib": 1024**2, "gib": 1024**3}

# 每层状态权重：pending 0 / downloading frac / downloaded 0.5 /
# extracting 0.5+0.5*frac / done 1.0
# （设计口径：pct =（done 层数 + Σ 进行中层字节比）/ 总层数；extracting 占 done 前 0.5 子权重）
_S_PENDING, _S_DOWN, _S_DOWNED, _S_EXT, _S_DONE = "pending", "down", "downed", "ext", "done"


def _to_bytes(num: float, unit: str) -> float:
    return num * _UNITS.get(unit.lower(), 1)


@dataclass
class PullUpdate:
    """一次 pull 行解析后的聚合快照。"""

    pct: float | None
    label: str
    done_layers: int
    total_layers: int


class PullParser:
    """聚合 `docker pull` 逐层输出为 [0,1] 总进度。

    总层数在首批 `Pulling fs layer` 齐全后确定；重试前调用 reset() 清空状态机
    （docker 会复用已下载 layer，新一次 pull 的 pct 从当前已完成数重算属真实语义）。
    """

    def __init__(self, image: str) -> None:
        self.image = image
        self._layers: dict[str, tuple[str, float]] = {}  # id -> (state, frac)
        # id -> 历史最大权重：跨状态迁移（down frac≈1 → downed 0.5 → ext frac≈0）时
        # 原始权重会回落，求和取历史最大值保证 pct 单调不减（进度条不倒退）。
        self._max_w: dict[str, float] = {}

    def reset(self) -> None:
        self._layers.clear()
        self._max_w.clear()

    def feed(self, line: str) -> PullUpdate | None:
        line = (line or "").strip()
        if not line:
            return None
        m = _PULL_LINE.match(line)
        if not m:
            return None
        lid, rest = m.group("id"), m.group("rest")
        if rest.startswith("Pulling fs layer"):
            self._layers.setdefault(lid, (_S_PENDING, 0.0))
        elif rest.startswith("Already exists"):
            self._layers[lid] = (_S_DONE, 1.0)
        elif rest.startswith("Download complete"):
            self._layers[lid] = (_S_DOWNED, 0.0)
        elif rest.startswith("Pull complete"):
            self._layers[lid] = (_S_DONE, 1.0)
        else:
            pm = _PROGRESS.search(rest)
            if not pm:
                return None
            cur = _to_bytes(float(pm.group("cur")), pm.group("cu"))
            tot = _to_bytes(float(pm.group("total")), pm.group("tu"))
            # docker 偶发 cur > total（如 1.5GB/1GB），夹紧到 1.0 保证 pct ∈ [0,1]
            frac = min(cur / tot, 1.0) if tot > 0 else 0.0
            stage = pm.group("stage").lower()
            self._layers[lid] = (_S_DOWN if stage == "downloading" else _S_EXT, frac)
        return self._update()

    def _weight(self, state: str, frac: float) -> float:
        if state == _S_DONE:
            return 1.0
        if state == _S_DOWNED:
            return 0.5
        if state == _S_DOWN:
            return frac
        if state == _S_EXT:
            return 0.5 + 0.5 * frac
        return 0.0

    def _update(self) -> PullUpdate:
        total = len(self._layers)
        if total == 0:
            return PullUpdate(pct=None, label=f"拉取镜像 {self.image}", done_layers=0, total_layers=0)
        done = sum(1 for st, _ in self._layers.values() if st == _S_DONE)
        acc = 0.0
        for lid, (st, fr) in self._layers.items():
            w = max(self._weight(st, fr), self._max_w.get(lid, 0.0))
            self._max_w[lid] = w
            acc += w
        pct = acc / total
        label = f"拉取镜像 {self.image}（{done}/{total} 层）"
        return PullUpdate(pct=round(pct, 4), label=label, done_layers=done, total_layers=total)


# ---------------------------------------------------------------------------
# 阶段耗时 EMA 计时（data/cache/startup-timing.json）
# ---------------------------------------------------------------------------

_EMA_ALPHA = 0.4
_MIN_SAMPLES_EMA = 5  # n<该值用算术均值，之后转 EMA


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class StartupTiming:
    """按 `engine:stage` 记录阶段耗时滑动估计，给出剩余时间 ETA。

    文件损坏/不可写 → 静默降级为无 ETA（返回 None），绝不影响启动。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (self._default_dir() / "startup-timing.json")
        self._data: dict[str, dict] = self._load()

    @staticmethod
    def _default_dir() -> Path:
        from modelctl.core.paths import cache_dir

        return cache_dir()

    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def eta(self, engine: str, stage: str, pct: float | None) -> int | None:
        rec = self._data.get(f"{engine}:{stage}")
        if not rec:
            return None
        base = float(rec.get("ema_s", 0.0))
        if base <= 0:
            return None
        remain = (1.0 - (pct or 0.0))
        return max(0, round(base * remain))

    def record(self, engine: str, stage: str, elapsed_s: float) -> None:
        key = f"{engine}:{stage}"
        prev = self._data.get(key)
        n = (prev.get("n", 0) if prev else 0) + 1
        if n < _MIN_SAMPLES_EMA or not prev:
            # 前几样本用增量算术均值累积，避免单次异常值定死基线
            prev_avg = prev.get("ema_s", 0.0) if prev else 0.0
            avg = ((prev_avg * (n - 1)) + elapsed_s) / n
        else:
            avg = _EMA_ALPHA * elapsed_s + (1 - _EMA_ALPHA) * prev["ema_s"]
        self._data[key] = {"ema_s": round(avg, 1), "n": n, "updated_at": _now_str()}
        self._flush()

    def _flush(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.debug(f"startup-timing 落盘失败（忽略）：{exc}")
