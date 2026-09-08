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
import math
import os
import re
import threading
import time
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
        except (OSError, ValueError):
            # ValueError 覆盖 JSONDecodeError 与 UnicodeDecodeError（均为其子类）
            return {}
        if not isinstance(raw, dict):
            return {}
        # 形状过滤：结构畸形的记录整条丢弃，保证下游 eta/record 不会再遇到坏值
        return {
            k: {"ema_s": float(v["ema_s"]), "n": v["n"]}
            for k, v in raw.items()
            if isinstance(v, dict)
            and isinstance(v.get("ema_s"), (int, float))
            and not isinstance(v.get("ema_s"), bool)
            and isinstance(v.get("n"), int)
            and not isinstance(v.get("n"), bool)
            and v["n"] >= 0
            and math.isfinite(v["ema_s"])
        }

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
        # 非正/非有限耗时是坏样本（时钟回拨、异常路径），直接忽略以保护基线
        if elapsed_s <= 0 or not math.isfinite(elapsed_s):
            return
        key = f"{engine}:{stage}"
        prev = self._data.get(key)
        n = (prev.get("n", 0) if prev else 0) + 1
        if n < _MIN_SAMPLES_EMA:
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


# ---------------------------------------------------------------------------
# 阶段进度事件与发射器（on_progress 回调 + 快照文件 + EMA ETA）
# ---------------------------------------------------------------------------

STAGES: tuple[str, ...] = ("preflight", "prepare_env", "launch", "loading", "health")
STAGE_LABELS: dict[str, str] = {
    "preflight": "依赖检查",
    "prepare_env": "准备环境",
    "launch": "拉起进程",
    "loading": "加载模型",
    "health": "就绪",
}


@dataclass
class StageEvent:
    """一次阶段状态变更；status ∈ running | done | error。"""

    stage: str
    status: str
    label: str
    pct: float | None = None  # 0.0–1.0；None = 不确定态（前端条纹动画）
    eta_s: int | None = None  # 预估剩余秒；None = 无历史样本
    error: str | None = None  # status=error 的原因（RequirementError 文案原样透传）

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "status": self.status,
            "label": self.label,
            "pct": self.pct,
            "eta_s": self.eta_s,
            "error": self.error,
        }


@dataclass
class _StageState:
    """单阶段在快照中的落盘态（含起止时间戳，_t0 仅用于测本次耗时不落盘）。"""

    stage: str
    status: str = "pending"
    label: str = ""
    pct: float | None = None
    eta_s: int | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    _t0: float = 0.0

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "status": self.status,
            "label": self.label,
            "pct": self.pct,
            "eta_s": self.eta_s,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class StartupTracker:
    """一次 start 的阶段进度发射器：on_progress 回调 + 快照文件 + EMA ETA。

    pct 单调不减由各来源保证（PullParser 逐层历史最大、LoadingWatcher._last_pct），
    Tracker 只做状态搬运与落盘，不二次夹紧——避免误伤同阶段重开（begin 重置）。
    """

    def __init__(self, profile_name: str, engine: str, runtime: str,
                 on_progress=None, timing: StartupTiming | None = None,
                 snapshot_path: Path | None = None) -> None:
        self.profile_name = profile_name
        self.engine = engine
        self.runtime = runtime
        self._on_progress = on_progress
        self._timing = timing or StartupTiming()
        self._snapshot_path = snapshot_path or (
            self._timing._default_dir() / f"{profile_name}.startup.json")
        self._stages: dict[str, _StageState] = {
            s: _StageState(stage=s, label=STAGE_LABELS[s]) for s in STAGES}

    # -- 阶段生命周期 --------------------------------------------------------

    def begin(self, stage: str, label: str | None = None, pct: float | None = None) -> None:
        st = self._stages[stage]
        st.status = "running"
        st.label = label or STAGE_LABELS[stage]
        st.pct = pct
        st.error = None
        # 重开阶段必须清掉上一轮收尾时间戳：done/fail 后迟到的 progress 会走到这里，
        # 残留 finished_at 会让快照同时出现 status=running + 旧完成时间（前端矛盾态）。
        st.finished_at = None
        st.started_at = _now_str()
        st._t0 = time.monotonic()
        st.eta_s = self._timing.eta(self.engine, stage, pct)
        self._emit(StageEvent(stage, "running", st.label, pct, st.eta_s, None))

    def progress(self, stage: str, label: str, pct: float | None = None) -> None:
        st = self._stages[stage]
        if st.status != "running":
            # 未 begin 或已收尾：以 running 重新开一帧（done/error 后仍来的进度按运行处理）
            self.begin(stage, label, pct)
            return
        st.label = label
        st.pct = pct
        st.eta_s = self._timing.eta(self.engine, stage, pct)
        self._emit(StageEvent(stage, "running", label, pct, st.eta_s, None))

    def done(self, stage: str, label: str | None = None, pct: float | None = 1.0) -> None:
        st = self._stages[stage]
        st.status = "done"
        st.label = label or STAGE_LABELS[stage]
        st.pct = pct
        st.eta_s = None
        st.finished_at = _now_str()
        # 耗时仅在真正 begin 过（_t0 非零）时取样，避免未开阶段（如 health）记伪样本
        elapsed = time.monotonic() - st._t0 if st._t0 else 0.0
        if elapsed > 0:
            self._timing.record(self.engine, stage, elapsed)
        self._emit(StageEvent(stage, "done", st.label, pct, None, None))

    def fail(self, stage: str, label: str, error: str) -> None:
        st = self._stages[stage]
        st.status = "error"
        st.label = label
        st.error = error
        st.finished_at = _now_str()
        self._emit(StageEvent(stage, "error", label, st.pct, None, error))

    # -- 输出 ---------------------------------------------------------------

    def _emit(self, event: StageEvent) -> None:
        if self._on_progress is not None:
            try:
                self._on_progress(event)
            except Exception as exc:  # noqa: BLE001 —— 进度回调异常绝不回灌启动线程
                logger.debug(f"on_progress 回调异常（忽略）：{exc}")
        self._write_snapshot()

    def _write_snapshot(self) -> None:
        payload = {
            "profile": self.profile_name,
            "engine": self.engine,
            "runtime": self.runtime,
            "updated_at": _now_str(),
            "stages": [self._stages[s].to_dict() for s in STAGES],
        }
        try:
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            # tmp 带 PID + 线程 ID：跨进程（多 profile 共享 cache 目录）与同进程跨线程
            # （LoadingWatcher daemon 与主线程并发 _emit）各写各的中间文件，
            # 否则两个 writer 交替写同一 tmp，os.replace 可能搬走半写文件 → 快照 JSON 损坏。
            tmp = self._snapshot_path.with_name(
                f"{self._snapshot_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._snapshot_path)
        except OSError as exc:
            logger.debug(f"startup 快照落盘失败（忽略）：{exc}")


# ---------------------------------------------------------------------------
# 引擎日志模式表 + loading 段 tail 监视（launch log → 阶段子进度）
# ---------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# engine -> 有序 [(compiled_regex, label 模板, base, scale)]；pct = base + scale*捕获的 0-100 值/100
# 捕获组为百分数（0-100）。无捕获组的固定行 scale=0。
PATTERNS: dict[str, list[tuple[re.Pattern, str, float, float]]] = {
    "vllm": [
        (re.compile(r"vLLM API server"), "引擎进程初始化", 0.05, 0.0),
        (re.compile(r"Downloading shards:.*?(\d+)%"), "下载模型权重", 0.05, 0.45),
        (re.compile(r"Loading safetensors checkpoint shards:.*?(\d+)%"), "加载模型权重", 0.5, 0.3),
        (re.compile(r"Capturing CUDA graph shapes:.*?(\d+)%"), "捕获 CUDA graph", 0.8, 0.15),
        (re.compile(r"Starting API server|Application startup complete"), "启动 HTTP 服务", 0.97, 0.0),
    ],
    # docker-capable 但 vLLM 之外引擎：首版仅 banner（其余整段兜底文案由 watcher 处理）
    "tokenspeed": [(re.compile(r"tokenspeed|API server", re.IGNORECASE), "引擎初始化中", 0.05, 0.0)],
    "tensorrt_llm": [(re.compile(r"TensorRT-LLM|API server", re.IGNORECASE), "引擎初始化中", 0.05, 0.0)],
}

# 每 tick 读取的日志尾部窗口行数
_TAIL_WINDOW = 400
# loading 段静默（无*新增*进展，含日志尚未生成）超过该秒数 → 兜底文案（本次事故缺失的那句）
_LOADING_FALLBACK_SEC = 120
_LOADING_FALLBACK_LABEL = "引擎初始化中（首次冷启动在 docker/WSL2 上可达 15 分钟）"


def match_progress(engine: str, line: str) -> tuple[str, float] | None:
    """按引擎模式表匹配日志行，返回 (label, pct)；无命中返回 None。

    docker logs 非 TTY 无颜色码；venv 路径可能含 ANSI，先剥离再匹配。
    """
    pats = PATTERNS.get(engine)
    if not pats:
        return None
    line = _ANSI.sub("", line or "")
    for rx, label, base, scale in pats:
        m = rx.search(line)
        if not m:
            continue
        if scale == 0.0 or not m.groups():
            return label, base
        val = float(m.group(1)) / 100.0
        return label, round(base + scale * val, 4)
    return None


class LoadingWatcher:
    """loading 段：daemon 线程 tail launch log，按模式表推进 tracker。

    只做进度，不做失败判定（死亡判定交 wait_ready/backend_dead）。
    pct 单调不减由本类的 `_last_pct` 保证（Tail 窗口内可能出现重复/回退行）。
    """

    def __init__(self, tracker, engine: str, log_path, interval: float = 2.0,
                 fallback_sec: float | None = None) -> None:
        self._tr = tracker
        self._engine = engine
        self._log = log_path
        self._interval = interval
        # 兜底阈值可注入：生产用 120s，单测无需真等两分钟
        self._fallback_sec = _LOADING_FALLBACK_SEC if fallback_sec is None else fallback_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen = 0            # 已消费的 tail 窗口行数
        self._last_pct = 0.0
        # 兜底语义「无*新增*进展」的计时基准：start/每次有效进展时刷新
        self._last_advance_ts = 0.0
        self._fallback_sent = False  # 同一段静默只发一次；新进展后重置、可再发

    def start(self) -> None:
        if self._thread is not None:  # 幂等：重复 start 不起第二个线程
            return
        self._last_advance_ts = time.monotonic()  # 兜底计时起点 = 监视开始（begin）
        self._thread = threading.Thread(target=self._run, name="loading-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        from modelctl.core.process import tail_file  # 延迟导入：本模块不与 process 循环依赖

        while not self._stop.wait(self._interval):
            try:
                self._tick(tail_file)
            except Exception as exc:  # noqa: BLE001 —— watcher 异常只失进度不断启动
                # 单次 tick 失败只丢这一轮子进度，循环必须继续（设计 §5）：
                # return 会让 watcher 永久终止，后续进展/兜底全部失效
                logger.debug(f"LoadingWatcher tick 异常（忽略）：{exc}")
                continue

    def _tick(self, tail_file) -> None:
        advanced = False
        # 日志缺失/读取失败按「无进展」继续走兜底判定：watcher 早于 launch log
        # 首行输出启动（docker 容器创建→首行），这段窗口正该显示兜底文案
        if Path(self._log).is_file():
            text = tail_file(self._log, _TAIL_WINDOW)
            if text:  # 读取失败（如 Windows 文件占用）返回空串，不能当 1 行空行推进位点
                # 只处理新增尾部：tail_file 无字节位点，退化为按已见行数增量。
                # 行数减少（截断/轮转）或窗口打满（后续滑动会使行号增量恒为空而漏新行）
                # → 整体重扫；重复行无害（pct 单调不减已过滤）。
                lines = text.split("\n")
                if self._seen > len(lines) or len(lines) >= _TAIL_WINDOW:
                    new = lines
                else:
                    new = lines[self._seen:]
                self._seen = len(lines)
                for line in new:
                    hit = match_progress(self._engine, line)
                    if hit and hit[1] > self._last_pct:
                        self._last_pct = hit[1]
                        self._tr.progress("loading", hit[0], pct=hit[1])
                        advanced = True
        # spec §4.2「超阈值无进展」= 无*新增*进展：静默（含日志未生成）超 fallback_sec
        # → 兜底文案（pct=None，前端条纹动画）。同一段静默只发一次，出现新进展后重置可再发。
        now = time.monotonic()
        if advanced:
            self._last_advance_ts = now
            self._fallback_sent = False
        elif not self._fallback_sent and (now - self._last_advance_ts) > self._fallback_sec:
            self._fallback_sent = True
            self._tr.progress("loading", _LOADING_FALLBACK_LABEL, pct=None)
