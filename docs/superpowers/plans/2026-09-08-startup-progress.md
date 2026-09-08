# 模型启动进度可视化（StartupProgress）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给所有引擎的模型 start 流程加一条引擎无关的 5 段阶段进度通道（含 docker 拉镜像百分比与阶段级均值 ETA），并把 docker 运行时容器日志 tee 进 launch log，修掉前端「连接中」永不更新与「引擎进程提前退出」误报。

**Architecture:** 新增 `core/startup_progress.py`（StageEvent / StartupTracker / LoadingWatcher / PATTERNS / pull 解析 / EMA 计时）作为唯一进度产出口；`all_service.start_profile` 按 5 段插桩并注入 `on_progress` 回调；WebUI 复用既有 `Task.event("stage")` SSE 通道 + 新增快照端点 `GET /models/{name}/startup`；前端新增 `StartupProgressCard.vue` 消费。

**Tech Stack:** Python 3.10+（stdlib 为主）、FastAPI、loguru、pytest；Vue 3 `<script setup>` + TypeScript + UnoCSS、EventSource。

## Global Constraints

- 时间格式一律 `YYYY-MM-DD HH:mm:ss`（`datetime.now().strftime("%Y-%m-%d %H:%M:%S")`），后端已格式化字段前端不二次加工。
- UI 不显示真实 ID（task_id 等虚拟 ID 可显示）。
- 后端遵循 PEP 8；统一 try-except + logger；进度落盘尽力而为（失败仅 log，绝不影响启动）。
- CSS 类名 BEM；子组件内部样式改动用 `:deep()`。
- 组件库无 element-plus，确认框用 `window.confirm`；toast 用 `@/utils/toast`。
- 前端无单测运行器，验证 = `cd web; npm run typecheck`（vue-tsc）通过。
- 后端测试全部 mock，不依赖真 docker / 真 GPU。
- 严禁 DDL；本计划无数据库改动。
- 缺省零行为变化：所有新增参数默认 `None`，CLI/集群旧调用路径行为不变。

**关键既有接口（本计划依赖，勿改名）：**
- `all_service.ComponentResult(tag, status, detail)` → `.tag/.status/.detail`，`status ∈ {"ok","skipped","error"}`
- `all_service.start_profile(profile, caps, timeout) -> ComponentResult`（当前 timeout 必填 float）
- `EngineAdapter.is_docker_runtime() -> bool`、`.pre_start()`、`.wait_ready(timeout) -> bool`、`.backend_dead() -> bool`、`._container_name`（vllm/tokenspeed/trtllm property）
- `docker_setup.ensure_image(image, attempts=None) -> bool`、`.image_present(image) -> bool`
- `process.start_detached(name, cmd, env, write_pid=True) -> (pid, Popen)`、`.launch_log(name) -> Path|None`、`.log_dir()`、`.tail_file(path, n)`
- `paths.cache_dir() -> Path`（`data/cache`，自动 mkdir）
- `Task.event(event_type, data)`（线程安全 SSE 广播）、`Task.update_detail(detail)`
- `reconcile.classify_start_failure(message, engine) -> (code, engine)`
- 失败 detail 文案（all_service L141）：`"引擎进程提前退出"`（died）/ `"健康检查超时"`

---

## Task 1: docker pull 进度解析器（纯函数）

**Files:**
- Create: `src/modelctl/core/startup_progress.py`
- Test: `tests/test_startup_progress.py`

**Interfaces:**
- Consumes: 无（纯 stdlib）
- Produces:
  - `PullUpdate`（dataclass: `pct: float | None`, `label: str`, `done_layers: int`, `total_layers: int`）
  - `PullParser(image: str)`：`.feed(line: str) -> PullUpdate | None`，`.reset() -> None`

- [ ] **Step 1: 写失败测试**

`tests/test_startup_progress.py`:

```python
"""startup_progress 单测：pull 解析 / 模式表 / EMA / 快照 / 阶段序列。"""
from __future__ import annotations

from modelctl.core.startup_progress import PullParser, PullUpdate


def test_pull_parser_counts_layers():
    p = PullParser("img:tag")
    assert p.feed("latest: Pulling from vllm/vllm-openai") is None
    u1 = p.feed("aaa: Pulling fs layer")
    assert isinstance(u1, PullUpdate) and u1.total_layers == 1
    p.feed("bbb: Pulling fs layer")
    # 半层下载：512MB/1GB ≈ 0.5
    u = p.feed("aaa: Downloading 512MB/1GB")
    assert u is not None and 0.2 < u.pct < 0.3  # (0.5*0.5)/1... 两半层 → 半*0.5/2
    p.feed("aaa: Download complete")
    p.feed("aaa: Pull complete")
    u2 = p.feed("bbb: Pull complete")
    assert u2.pct == 1.0 and u2.done_layers == 2 and u2.total_layers == 2


def test_pull_parser_already_exists_counts_done():
    p = PullParser("img:tag")
    p.feed("aaa: Already exists")
    u = p.feed("bbb: Pull complete")
    assert u.done_layers == 2 and u.total_layers == 2 and u.pct == 1.0


def test_pull_parser_reset_clears_layers():
    p = PullParser("img:tag")
    p.feed("aaa: Pulling fs layer")
    p.feed("aaa: Pull complete")
    p.reset()
    u = p.feed("zzz: Pulling fs layer")
    assert u.done_layers == 0 and u.total_layers == 1


def test_pull_parser_bytes_units_and_extracting():
    p = PullParser("img")
    p.feed("aaa: Pulling fs layer")
    p.feed("aaa: Downloading 100kB/1MB")
    p.feed("aaa: Download complete")
    # extracting 半程：权重 0.5 + 0.5*0.5 = 0.75
    u = p.feed("aaa: Extracting 500kB/1MB")
    assert 0.7 < u.pct < 0.8
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_startup_progress.py -v`
Expected: FAIL — `ModuleNotFoundError: modelctl.core.startup_progress`

- [ ] **Step 3: 写最小实现**

Create `src/modelctl/core/startup_progress.py`（本任务先只放 pull 解析部分）:

```python
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

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# docker pull 非 TTY 逐层输出解析
# ---------------------------------------------------------------------------

_PULL_LINE = re.compile(r"^(?P<id>[0-9a-f]{8,}):\s+(?P<rest>.*)$")
# Downloading/Extracting 的 "<进度条>? cur/total" —— 进度条可能不存在（非 TTY 纯状态行）
_PROGRESS = re.compile(
    r"(?P<stage>Downloading|Extracting).*?"
    r"(?P<cur>\d+(?:\.\d+)?)\s*(?P<cu>bytes|kB|MB|GB|TB|KiB|MiB|GiB)"
    r"\s*/\s*(?P<total>\d+(?:\.\d+)?)\s*(?P<tu>bytes|kB|MB|GB|TB|KiB|MiB|GiB)",
    re.IGNORECASE,
)
_UNITS = {"bytes": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
          "kib": 1024, "mib": 1024**2, "gib": 1024**3}

# 每层状态权重：pending 0 / downloading 0.5*frac / downloaded 0.5 /
# extracting 0.5+0.5*frac / done 1.0
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

    def reset(self) -> None:
        self._layers.clear()

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
            frac = cur / tot if tot > 0 else 0.0
            stage = pm.group("stage").lower()
            self._layers[lid] = (_S_DOWN if stage == "downloading" else _S_EXT, frac)
        return self._update()

    def _weight(self, state: str, frac: float) -> float:
        if state == _S_DONE:
            return 1.0
        if state == _S_DOWNED:
            return 0.5
        if state == _S_DOWN:
            return 0.5 * frac
        if state == _S_EXT:
            return 0.5 + 0.5 * frac
        return 0.0

    def _update(self) -> PullUpdate:
        total = len(self._layers)
        if total == 0:
            return PullUpdate(pct=None, label=f"拉取镜像 {self.image}", done_layers=0, total_layers=0)
        done = sum(1 for st, _ in self._layers.values() if st == _S_DONE)
        acc = sum(self._weight(st, fr) for st, fr in self._layers.values())
        pct = acc / total
        label = f"拉取镜像 {self.image}（{done}/{total} 层）"
        return PullUpdate(pct=round(pct, 4), label=label, done_layers=done, total_layers=total)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_startup_progress.py -v`
Expected: PASS（4 个）

- [ ] **Step 5: 提交**

```bash
git add src/modelctl/core/startup_progress.py tests/test_startup_progress.py
git commit -m "feat(startup-progress): docker pull 逐层进度解析器"
```

---

## Task 2: 阶段耗时 EMA 计时存储

**Files:**
- Modify: `src/modelctl/core/startup_progress.py`
- Test: `tests/test_startup_progress.py`

**Interfaces:**
- Consumes: `paths.cache_dir()`
- Produces: `StartupTiming(path: Path | None = None)`：`.eta(engine: str, stage: str, pct: float | None) -> int | None`、`.record(engine: str, stage: str, elapsed_s: float) -> None`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_startup_progress.py`:

```python
def test_timing_cold_start_returns_none(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    t = StartupTiming(path=tmp_path / "timing.json")
    assert t.eta("vllm", "prepare_env", 0.5) is None


def test_timing_record_then_eta_scales_with_pct(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    t = StartupTiming(path=tmp_path / "timing.json")
    t.record("vllm", "prepare_env", 100.0)
    # pct=0.5 → 剩余 50；pct=None → 全量 100
    assert t.eta("vllm", "prepare_env", 0.5) == 50
    assert t.eta("vllm", "prepare_env", None) == 100


def test_timing_persists_across_instances(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    p = tmp_path / "timing.json"
    StartupTiming(path=p).record("vllm", "loading", 200.0)
    assert StartupTiming(path=p).eta("vllm", "loading", 0.0) == 200


def test_timing_corrupt_file_survives(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    p = tmp_path / "timing.json"
    p.write_text("{ not json", encoding="utf-8")
    t = StartupTiming(path=p)
    assert t.eta("vllm", "loading", 0.5) is None
    t.record("vllm", "loading", 10.0)  # 损坏文件被忽略后仍可写
    assert t.eta("vllm", "loading", 0.5) == 5
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_startup_progress.py -k timing -v`
Expected: FAIL — `ImportError: cannot import name 'StartupTiming'`

- [ ] **Step 3: 写实现**

追加到 `startup_progress.py`:

```python
import json
import os
from pathlib import Path

from loguru import logger

_EMA_ALPHA = 0.4
_MIN_SAMPLES_EMA = 5  # n<该值用算术均值，之后转 EMA


def _now_str() -> str:
    from datetime import datetime

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
            # 前几样本用算术均值累积，避免单次异常值定死基线
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
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_startup_progress.py -v`
Expected: PASS（8 个）

- [ ] **Step 5: 提交**

```bash
git add src/modelctl/core/startup_progress.py tests/test_startup_progress.py
git commit -m "feat(startup-progress): 阶段耗时 EMA 计时存储"
```

---

## Task 3: StageEvent + StartupTracker（发射 + 快照 + ETA）

**Files:**
- Modify: `src/modelctl/core/startup_progress.py`
- Test: `tests/test_startup_progress.py`

**Interfaces:**
- Consumes: `StartupTiming`（Task 2）
- Produces:
  - `STAGES: tuple[str, ...] = ("preflight","prepare_env","launch","loading","health")`
  - `STAGE_LABELS: dict[str,str]`
  - `StageEvent(stage, status, label, pct=None, eta_s=None, error=None)`：`.to_dict() -> dict`（snake_case，键含 stage/status/label/pct/eta_s/error）
  - `StartupTracker(profile_name, engine, runtime, on_progress=None, timing=None, snapshot_path=None)`：
    - `.begin(stage, label, pct=None)` / `.progress(stage, label, pct=None)` / `.done(stage, label=None, pct=None)` / `.fail(stage, label, error)`
    - `.emit(event: StageEvent)`（内部；on_progress + 写快照）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_startup_progress.py`:

```python
def test_tracker_emits_and_writes_snapshot(tmp_path):
    from modelctl.core.startup_progress import StartupTracker, STAGES

    events = []
    snap = tmp_path / "startup.json"
    tr = StartupTracker("q", "vllm", "docker", on_progress=events.append,
                        timing=StartupTiming(path=tmp_path / "t.json"),
                        snapshot_path=snap)
    tr.begin("preflight", "依赖检查")
    tr.done("preflight")
    tr.begin("prepare_env", "准备环境")
    tr.progress("prepare_env", "拉取镜像（3/9 层）", pct=0.4)
    assert events[0].stage == "preflight" and events[0].status == "running"
    assert events[-1].pct == 0.4
    import json
    data = json.loads(snap.read_text(encoding="utf-8"))
    assert data["runtime"] == "docker"
    assert [s["stage"] for s in data["stages"]] == list(STAGES)
    assert data["stages"][0]["status"] == "done"
    assert data["stages"][1]["pct"] == 0.4


def test_tracker_fail_marks_error(tmp_path):
    from modelctl.core.startup_progress import StartupTracker

    events = []
    tr = StartupTracker("q", "vllm", "docker", on_progress=events.append,
                        timing=StartupTiming(path=tmp_path / "t.json"),
                        snapshot_path=tmp_path / "s.json")
    tr.begin("preflight", "依赖检查")
    tr.fail("preflight", "依赖检查", "docker 不在 PATH")
    assert events[-1].status == "error" and "docker" in events[-1].error


def test_tracker_eta_from_timing(tmp_path):
    from modelctl.core.startup_progress import StartupTiming, StartupTracker

    timing = StartupTiming(path=tmp_path / "t.json")
    timing.record("vllm", "loading", 200.0)
    events = []
    tr = StartupTracker("q", "vllm", "docker", on_progress=events.append,
                        timing=timing, snapshot_path=tmp_path / "s.json")
    tr.begin("loading", "加载模型", pct=0.5)
    assert events[-1].eta_s == 100  # 200*(1-0.5)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_startup_progress.py -k tracker -v`
Expected: FAIL — `ImportError: cannot import name 'StartupTracker'`

- [ ] **Step 3: 写实现**

追加到 `startup_progress.py`:

```python
from dataclasses import dataclass, field

STAGES: tuple[str, ...] = ("preflight", "prepare_env", "launch", "loading", "health")
STAGE_LABELS = {
    "preflight": "依赖检查",
    "prepare_env": "准备环境",
    "launch": "拉起进程",
    "loading": "加载模型",
    "health": "就绪",
}


@dataclass
class StageEvent:
    stage: str
    status: str  # running | done | error
    label: str
    pct: float | None = None
    eta_s: int | None = None
    error: str | None = None

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
    """一次 start 的阶段进度发射器：on_progress 回调 + 快照文件 + EMA ETA。"""

    def __init__(self, profile_name: str, engine: str, runtime: str,
                 on_progress=None, timing: StartupTiming | None = None,
                 snapshot_path: Path | None = None) -> None:
        import time

        self._time = time
        self.profile_name = profile_name
        self.engine = engine
        self.runtime = runtime
        self._on_progress = on_progress
        self._timing = timing or StartupTiming()
        self._snapshot_path = snapshot_path or (self._timing._default_dir() / f"{profile_name}.startup.json")
        self._stages: dict[str, _StageState] = {s: _StageState(stage=s, label=STAGE_LABELS[s]) for s in STAGES}

    # -- 阶段生命周期 --------------------------------------------------------

    def begin(self, stage: str, label: str | None = None, pct: float | None = None) -> None:
        st = self._stages[stage]
        st.status = "running"
        st.label = label or STAGE_LABELS[stage]
        st.pct = pct
        st.error = None
        st.started_at = _now_str()
        st._t0 = self._time.monotonic()
        st.eta_s = self._timing.eta(self.engine, stage, pct) if pct is not None else self._timing.eta(self.engine, stage, None)
        self._emit(StageEvent(stage, "running", st.label, pct, st.eta_s, None))

    def progress(self, stage: str, label: str, pct: float | None = None) -> None:
        st = self._stages[stage]
        if st.status != "running":
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
        elapsed = self._time.monotonic() - st._t0 if st._t0 else 0.0
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
            tmp = self._snapshot_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._snapshot_path)
        except OSError as exc:
            logger.debug(f"startup 快照落盘失败（忽略）：{exc}")
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_startup_progress.py -v`
Expected: PASS（11 个）

- [ ] **Step 5: 提交**

```bash
git add src/modelctl/core/startup_progress.py tests/test_startup_progress.py
git commit -m "feat(startup-progress): StageEvent + StartupTracker（发射/快照/ETA）"
```

---

## Task 4: 引擎日志模式表 + LoadingWatcher

**Files:**
- Modify: `src/modelctl/core/startup_progress.py`
- Test: `tests/test_startup_progress.py`

**Interfaces:**
- Consumes: `StartupTracker`（Task 3）、`process.tail_file`
- Produces:
  - `match_progress(engine: str, line: str) -> tuple[str, float] | None`（返回 `(label, pct)`；pct 单调性由 watcher 保证）
  - `LoadingWatcher(tracker, engine, log_path, interval=2.0)`：`.start()`、`.stop()`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_startup_progress.py`:

```python
def test_match_progress_vllm_shards_monotonic():
    from modelctl.core.startup_progress import match_progress

    # banner
    assert match_progress("vllm", "INFO vLLM API server version 0.28.0")[1] == 0.05
    # shard 加载 50% → 0.5+0.3*0.5=0.65
    lab, pct = match_progress("vllm", "(APIServer) Loading safetensors checkpoint shards:  50% Completed | 3/6")
    assert abs(pct - 0.65) < 1e-6
    # CUDA graph 捕获 100% → 0.8+0.15=0.95
    assert abs(match_progress("vllm", "Capturing CUDA graph shapes: 100%")[1] - 0.95) < 1e-6
    # 无关行
    assert match_progress("vllm", "GET /metrics HTTP/1.1 200 OK") is None


def test_match_progress_unknown_engine_banner_only():
    from modelctl.core.startup_progress import match_progress

    # tokenspeed 无 shard 模式 → 只有 banner，其它 None
    assert match_progress("tokenspeed", "Loading safetensors checkpoint shards: 50%") is None


def test_loading_watcher_advances_tracker(tmp_path):
    import time
    from modelctl.core.startup_progress import LoadingWatcher, StartupTracker, StartupTiming

    log = tmp_path / "launch-q.log"
    log.write_text("(APIServer) vLLM API server version 0.28.0\n", encoding="utf-8")
    events = []
    tr = StartupTracker("q", "vllm", "docker", on_progress=events.append,
                        timing=StartupTiming(path=tmp_path / "t.json"),
                        snapshot_path=tmp_path / "s.json")
    tr.begin("loading", "加载模型")
    w = LoadingWatcher(tr, "vllm", log, interval=0.05)
    w.start()
    # 追加一行 shard 加载
    with log.open("a", encoding="utf-8") as f:
        f.write("(APIServer) Loading safetensors checkpoint shards: 100% Completed\n")
    deadline = time.time() + 2
    while time.time() < deadline and not any(e.pct == 0.8 for e in events):
        time.sleep(0.05)
    w.stop()
    assert any(e.pct == 0.8 for e in events)  # 0.5+0.3*1.0
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_startup_progress.py -k "progress or watcher" -v`
Expected: FAIL — `ImportError: cannot import name 'match_progress'`

- [ ] **Step 3: 写实现**

追加到 `startup_progress.py`:

```python
import threading

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

# 无命中且 loading 已持续超过该秒数 → 兜底文案（本次事故缺失的那句）
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
    """

    def __init__(self, tracker, engine: str, log_path, interval: float = 2.0) -> None:
        self._tr = tracker
        self._engine = engine
        self._log = log_path
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pos = 0
        self._last_pct = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"loading-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        import time

        from modelctl.core.process import tail_file

        t0 = time.monotonic()
        while not self._stop.wait(self._interval):
            try:
                self._tick(tail_file, time, t0)
            except Exception as exc:  # noqa: BLE001 —— watcher 异常只失进度不断启动
                logger.debug(f"LoadingWatcher tick 异常（忽略）：{exc}")
                return

    def _tick(self, tail_file, time, t0) -> None:
        from pathlib import Path

        if not Path(self._log).is_file():
            return
        text = tail_file(self._log, 400)
        # 只处理新增尾部：按行数增量（tail_file 无字节位点，退化为按已见行数）
        lines = text.split("\n")
        new = lines[self._seen:] if hasattr(self, "_seen") else lines
        self._seen = len(lines)
        advanced = False
        for line in new:
            hit = match_progress(self._engine, line)
            if hit and hit[1] > self._last_pct:
                self._last_pct = hit[1]
                self._tr.progress("loading", hit[0], pct=hit[1])
                advanced = True
        # 无进展且超阈值 → 兜底文案（pct=None，前端条纹动画）
        if not advanced and self._last_pct == 0.0 and (time.monotonic() - t0) > _LOADING_FALLBACK_SEC:
            self._tr.progress("loading", _LOADING_FALLBACK_LABEL, pct=None)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_startup_progress.py -v`
Expected: PASS（14 个）

- [ ] **Step 5: 提交**

```bash
git add src/modelctl/core/startup_progress.py tests/test_startup_progress.py
git commit -m "feat(startup-progress): 引擎日志模式表 + LoadingWatcher"
```

---

## Task 5: `ensure_image` 流式化 + `on_progress`

**Files:**
- Modify: `src/modelctl/core/docker_setup.py:170-208`
- Test: `tests/test_docker_setup_pull.py`

**Interfaces:**
- Consumes: `PullParser`（Task 1）
- Produces: `ensure_image(image, attempts=None, on_progress=None) -> bool`，`on_progress(label: str, pct: float | None)`

- [ ] **Step 1: 写失败测试**

Create `tests/test_docker_setup_pull.py`:

```python
"""ensure_image 流式 pull：on_progress 收到聚合进度、失败分类不变、回调缺省兼容。"""
from __future__ import annotations

from unittest import mock

from modelctl.core import docker_setup


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self._lines = lines
        self.returncode = returncode
        self.stderr = ""
        self.stdout = ""

    def __iter__(self):
        return iter(self._lines)


def test_ensure_image_streams_progress():
    lines = [
        "aaa: Pulling fs layer",
        "aaa: Downloading 512MB/1GB",
        "aaa: Pull complete",
        "Status: Downloaded newer image",
    ]
    seen = []
    with mock.patch.object(docker_setup, "image_present", return_value=False), \
         mock.patch.object(docker_setup.subprocess, "Popen", return_value=_FakeProc(lines)):
        ok = docker_setup.ensure_image("img:tag", on_progress=lambda lab, pct: seen.append(pct))
    assert ok is True
    assert 0 < max(seen) <= 1.0


def test_ensure_image_cached_skips_pull():
    with mock.patch.object(docker_setup, "image_present", return_value=True):
        assert docker_setup.ensure_image("img:tag", on_progress=lambda l, p: None) is True


def test_ensure_image_failure_no_callback_still_works():
    proc = _FakeProc(["Error: manifest unknown"], returncode=1)
    with mock.patch.object(docker_setup, "image_present", return_value=False), \
         mock.patch.object(docker_setup.subprocess, "Popen", return_value=proc), \
         mock.patch.object(docker_setup.time, "sleep"):
        assert docker_setup.ensure_image("img:tag", attempts=1) is False
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_docker_setup_pull.py -v`
Expected: FAIL — `ensure_image() got unexpected keyword 'on_progress'`

- [ ] **Step 3: 写实现**

在 `docker_setup.py` 顶部 import 区加 `from modelctl.core.startup_progress import PullParser`。整体替换 `ensure_image`:

```python
def ensure_image(image: str, attempts: int | None = None,
                 on_progress: "Callable[[str, float | None], None] | None" = None) -> bool:
    """确保 docker_image 就位：本地已有即复用，否则流式拉取并按错误类型决定重试。

    on_progress(label, pct)：逐层聚合进度（PullParser）；缺省 None 行为与旧实现一致
    （仍收集全文用于失败分类）。
    """
    if image_present(image):
        logger.info(f"镜像已就位，跳过拉取：{image}")
        return True
    attempts = attempts or PULL_ATTEMPTS
    parser = PullParser(image)
    tail = ""
    err_all = ""
    for i in range(1, attempts + 1):
        logger.info(f"拉取镜像（第 {i}/{attempts} 次）：{image}")
        parser.reset()
        err_lines: list[str] = []
        try:
            proc = subprocess.Popen(
                ["docker", "pull", image],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error(f"docker pull 无法执行：{exc}")
            return False
        for line in proc:
            err_lines.append(line)
            upd = parser.feed(line)
            if upd and on_progress:
                on_progress(f"{upd.label}（第 {i}/{attempts} 次）" if attempts > 1 else upd.label, upd.pct)
        proc.wait()
        if proc.returncode == 0:
            logger.info(f"镜像拉取完成：{image}")
            return True
        err_all = "".join(err_lines).strip()
        tail = err_all.splitlines()[-1] if err_all else ""
        kind = classify_pull_error(err_all)
        if kind == "dead-mirror":
            logger.error(f"registry-mirror 域名解析失败，重试无意义：{tail}")
            logger.error(f"执行 `modelctl env setup docker --run` 清理停服源（{DAEMON_JSON}）")
            return False
        if kind == "missing-tag":
            logger.error(f"mirror 上没有该镜像的 manifest，重试无意义：{tail}")
            logger.error(f"改走反代显式拉取再回打 tag：docker pull <mirror>/{image}")
            return False
        if i < attempts:
            logger.warning(f"拉取中断（{kind}），已完成的 layer 会被复用，"
                           f"{PULL_RETRY_WAIT}s 后重试：{tail}")
            time.sleep(PULL_RETRY_WAIT)
    logger.error(f"镜像拉取连续 {attempts} 次失败，最后一条错误：{tail}")
    return False
```

> 注意：`docker_setup.py` 已 `from typing import Callable` 且 `import time`，无需新增。`docker pull` 的 `stderr` 合并进 `stdout`（旧实现分类读 `proc.stderr or proc.stdout`，`classify_pull_error` 对混合文本不敏感，行为等价）。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_docker_setup_pull.py -v`
Expected: PASS（3 个）

- [ ] **Step 5: 回归既有 docker 测试**

Run: `python -m pytest tests/ -k docker -q`
Expected: 全绿（既有 `ensure_image` 相关测试仍过——回调缺省零变化）

- [ ] **Step 6: 提交**

```bash
git add src/modelctl/core/docker_setup.py tests/test_docker_setup_pull.py
git commit -m "feat(startup-progress): ensure_image 流式 pull + on_progress 回调"
```

---

## Task 6: 引擎适配器钩子 `set_progress_sink` / `log_tee_cmd`

**Files:**
- Modify: `src/modelctl/engines/base.py:35-43`（__init__）+ 类体
- Modify: `src/modelctl/engines/vllm.py:116-129`（pre_start ensure_image 透传）
- Modify: `src/modelctl/engines/tokenspeed.py`、`tensorrt_llm.py`（同 pre_start 透传）
- Test: `tests/test_engine_progress_hooks.py`

**Interfaces:**
- Consumes: `docker_setup.ensure_image(..., on_progress=...)`（Task 5）
- Produces:
  - `EngineAdapter.set_progress_sink(cb: Callable[[str, float|None], None] | None)`（存 `self._progress_cb`）
  - `EngineAdapter.log_tee_cmd() -> list[str] | None`（默认 None；docker 子类返回 `["docker","logs","-f","--tail","all",<container>]`）
  - `EngineAdapter.progress_cb` 属性（供 all_service 判定是否有 sink；默认返回 `self._progress_cb`）

- [ ] **Step 1: 写失败测试**

Create `tests/test_engine_progress_hooks.py`:

```python
"""适配器进度钩子：默认 None、docker 子类返回 tee 命令、pre_start 透传 ensure_image。"""
from __future__ import annotations

from unittest import mock

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import Profile
from modelctl.engines.vllm import VllmAdapter

CAPS = Capabilities(gpu_count=1, compute_capability="8.9", binaries={})


def _profile():
    return Profile(name="q", engine="vllm", port=8000,
                   engine_config={"model": "/m/x", "docker_image": "img:tag"})


def test_default_hooks_none():
    a = VllmAdapter(_profile(), CAPS)
    assert a.log_tee_cmd() is None  # 未设 docker 路径前默认无 tee（走 venv）


def test_docker_log_tee_cmd():
    a = VllmAdapter(_profile(), CAPS)
    with mock.patch.object(a, "_resolve_runtime", return_value=("docker", "img:tag", None)):
        assert a.log_tee_cmd() == ["docker", "logs", "-f", "--tail", "all", "q-vllm"]


def test_pre_start_forwards_progress_to_ensure_image():
    a = VllmAdapter(_profile(), CAPS)
    seen = []
    a.set_progress_sink(lambda lab, pct: seen.append(lab))
    with mock.patch.object(a, "_resolve_runtime", return_value=("docker", "img:tag", None)), \
         mock.patch("modelctl.engines.vllm.docker_setup.ensure_image", return_value=True) as ei, \
         mock.patch("modelctl.engines.vllm.Path.is_dir", return_value=True), \
         mock.patch("modelctl.engines.vllm.Path.is_file", return_value=False):
        a.pre_start()
    kw = ei.call_args.kwargs
    assert "on_progress" in kw
    kw["on_progress"]("拉取中", 0.5)
    assert seen == ["拉取中"]
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_engine_progress_hooks.py -v`
Expected: FAIL — `AttributeError: 'VllmAdapter' has no attribute 'log_tee_cmd'`

- [ ] **Step 3a: base.py 加钩子**

在 `EngineAdapter.__init__` 末尾（`self.spawned_proc = ...` 之后）加：

```python
        # 启动进度回调（set_progress_sink 注入）：签名 (label: str, pct: float|None) -> None
        self._progress_cb: "Callable[[str, float | None], None] | None" = None
```

在 base.py 顶部 import 补 `from typing import TYPE_CHECKING, Callable`（`Callable` 加到现有 typing import）。

类体内加方法（放在 `pre_start` 附近）：

```python
    def set_progress_sink(self, cb) -> None:
        """注入启动进度回调 (label, pct)；None 清除。docker 拉镜像子进度经此上报。"""
        self._progress_cb = cb

    def log_tee_cmd(self) -> list[str] | None:
        """docker 日志续写命令（`docker logs -f`）；非 docker runtime 返回 None。

        all_service 在容器启动后 spawn 本命令，stdout 追加进 launch log，
        使 SSE / CLI logs / 早退摘录对 docker 运行时也能看到真实引擎输出。
        """
        return None
```

- [ ] **Step 3b: 三个 docker 适配器覆盖 `log_tee_cmd`**

`vllm.py` 在 `is_docker_runtime()` 之后加：

```python
    def log_tee_cmd(self) -> list[str] | None:
        """docker 分支：`docker logs -f --tail all <container>` 续写容器输出。

        用 `--tail all` 而非 `--tail 0`：`docker run --detach` 秒返回，只跟新行会漏掉
        tee 挂上前数百毫秒内的 banner 行，伤及 loading 段模式表匹配。launch log 此
        前只有一行容器 ID，全量重放无副作用；重复行由 watcher 的 pct 单调不减兜住。
        """
        if self._resolve_runtime()[0] != "docker":
            return None
        return ["docker", "logs", "-f", "--tail", "all", self._container_name]
```

`tokenspeed.py` / `tensorrt_llm.py` 各加同构方法，容器名用各自的 `self._container_name`（tokenspeed 后缀 `-tokenspeed`、trtllm `-trtllm` 由各自 property 决定，勿硬编码）。

- [ ] **Step 3c: 三个适配器 `pre_start` 透传 `on_progress`**

`vllm.py:125` 原：

```python
        if runtime == "docker" and not docker_setup.ensure_image(image):
```

改为：

```python
        if runtime == "docker" and not docker_setup.ensure_image(image, on_progress=self._progress_cb):
```

`tokenspeed.py` / `tensorrt_llm.py` 的对应 `ensure_image(image)` 调用同样加 `on_progress=self._progress_cb`。

- [ ] **Step 3d: 修正 Step 1 里平台相关的默认用例**

`test_default_hooks_none` 改为显式锁定 venv 分支（避免 Windows 上 `_resolve_runtime` 真返回 docker 造成假失败）：

```python
def test_default_hooks_none():
    a = VllmAdapter(_profile(), CAPS)
    with mock.patch.object(a, "_resolve_runtime", return_value=("venv", None, None)):
        assert a.log_tee_cmd() is None
    assert a._progress_cb is None
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_engine_progress_hooks.py -v`
Expected: PASS（3 个）

- [ ] **Step 5: 回归引擎测试**

Run: `python -m pytest tests/test_engines_vllm.py tests/test_engines_tokenspeed.py -q`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add src/modelctl/engines/base.py src/modelctl/engines/vllm.py src/modelctl/engines/tokenspeed.py src/modelctl/engines/tensorrt_llm.py tests/test_engine_progress_hooks.py
git commit -m "feat(startup-progress): 适配器 set_progress_sink / log_tee_cmd 钩子"
```

---

## Task 7: `start_profile` 5 段插桩 + tee 生命周期 + 超时自适应

**Files:**
- Modify: `src/modelctl/core/all_service.py:74-141`（start_profile）/ `:144-162`（stop_profile）/ `:165-173`（restart_profile）
- Modify: `src/modelctl/core/process.py`（新增 `spawn_log_tee` / `kill_log_tee` / `tee_pid_file`）
- Test: `tests/test_all_service_startup_progress.py`、`tests/test_docker_log_tee.py`

**Interfaces:**
- Consumes: `StartupTracker` / `LoadingWatcher` / `StageEvent`（Task 3/4）、`adapter.set_progress_sink` / `log_tee_cmd`（Task 6）
- Produces:
  - `all_service.start_profile(profile, caps, timeout, on_progress=None) -> ComponentResult`
  - `all_service.restart_profile(profile, caps, timeout, on_progress=None) -> ComponentResult`
  - `all_service.default_start_timeout(profile, caps) -> float`
  - `process.tee_pid_file(name) -> Path`、`process.spawn_log_tee(name, cmd) -> int | None`、`process.kill_log_tee(name) -> None`
  - CLI 侧 `on_progress=lambda ev: ...`（Task 8）；WebUI 侧绑 `task.event("stage", ...)`（Task 9）

- [ ] **Step 1: 写 tee 生命周期失败测试**

Create `tests/test_docker_log_tee.py`:

```python
"""docker 日志 tee 生命周期：spawn 参数、残留清理、kill、非 docker 不 spawn。"""
from __future__ import annotations

from unittest import mock

from modelctl.core import process


def test_spawn_log_tee_appends_and_writes_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(process, "cache_dir", lambda: tmp_path)
    with mock.patch.object(process.subprocess, "Popen") as popen:
        popen.return_value.pid = 4321
        pid = process.spawn_log_tee("q", ["docker", "logs", "-f", "--tail", "all", "q-vllm"])
    assert pid == 4321
    assert (tmp_path / "q.log-tee.pid").read_text(encoding="utf-8") == "4321"
    kwargs = popen.call_args.kwargs
    assert kwargs["stdout"] is not None and kwargs["stderr"] == process.subprocess.STDOUT
    # 以 append 打开，不截断 start_detached 写入的容器 ID 首行
    assert kwargs["stdout"].mode.startswith("ab") or "a" in kwargs["stdout"].mode


def test_spawn_log_tee_kills_residual_first(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(process, "cache_dir", lambda: tmp_path)
    (tmp_path / "q.log-tee.pid").write_text("9999", encoding="utf-8")
    with mock.patch.object(process, "is_pid_alive", return_value=True), \
         mock.patch.object(process, "kill_pid_tree") as killed, \
         mock.patch.object(process.subprocess, "Popen") as popen:
        popen.return_value.pid = 111
        process.spawn_log_tee("q", ["docker", "logs", "-f", "c"])
    killed.assert_called_once_with(9999)


def test_kill_log_tee_idempotent_without_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "cache_dir", lambda: tmp_path)
    process.kill_log_tee("nope")  # 不应抛
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_docker_log_tee.py -v`
Expected: FAIL — `module 'modelctl.core.process' has no attribute 'spawn_log_tee'`

- [ ] **Step 3: 实现 tee 生命周期（process.py）**

`process.py` 需先补一个 `kill_pid_tree(pid)`（复用 `stop_instance` 已有的 killpg / taskkill 语义，抽成公用函数供 tee 与 stop 共用）。在 `pid_file` 之后加：

```python
def tee_pid_file(name: str) -> Path:
    """docker 日志 tee 子进程的 PID 文件（与引擎 PID 分离，docker 路径不写引擎 PID）。"""
    return cache_dir() / f"{name}.log-tee.pid"


def kill_pid_tree(pid: int) -> None:
    """终止进程及其子进程树（POSIX killpg / Windows taskkill /T /F）；进程已死则静默。"""
    if not is_pid_alive(pid):
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    try:
        os.killpg(pid, signal.SIGKILL)  # type: ignore[attr-defined]  # POSIX-only
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)  # type: ignore[attr-defined]
        except OSError:
            pass


def spawn_log_tee(name: str, command: list[str]) -> int | None:
    """后台续写引擎日志到 launch log（append），PID 记 `<name>.log-tee.pid`。

    先清理残留 tee（双 tee 会重复写同一 launch log）。launch log 不存在时退回
    `launch-<name>.log` 直接建文件（docker 路径下 start_detached 已建，正常不触发）。
    失败仅告警返回 None —— 日志续写是 nice-to-have，绝不影响启动。
    """
    tp = tee_pid_file(name)
    if tp.is_file():
        try:
            old = int(tp.read_text(encoding="utf-8").strip())
        except ValueError:
            old = None
        if old is not None:
            kill_pid_tree(old)
        tp.unlink(missing_ok=True)
    path = launch_log(name) or (log_dir() / f"launch-{name}.log")
    try:
        # "ab"：追加，保留 start_detached 写入的容器 ID 首行；二进制避免编码耦合
        fp = open(path, "ab")
        proc = subprocess.Popen(command, stdout=fp, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"引擎日志 tee 启动失败（工作日志将无 docker 容器输出）：{exc}")
        return None
    tp.write_text(str(proc.pid), encoding="utf-8")
    return proc.pid


def kill_log_tee(name: str) -> None:
    """终止日志 tee 子进程并清 PID（幂等）。"""
    tp = tee_pid_file(name)
    if not tp.is_file():
        return
    try:
        pid = int(tp.read_text(encoding="utf-8").strip())
    except ValueError:
        pid = None
    if pid is not None:
        kill_pid_tree(pid)
    tp.unlink(missing_ok=True)
```

- [ ] **Step 4: 运行确认 tee 测试通过**

Run: `python -m pytest tests/test_docker_log_tee.py -v`
Expected: PASS（3 个）

- [ ] **Step 5: 写 `start_profile` 插桩失败测试**

Create `tests/test_all_service_startup_progress.py`:

```python
"""start_profile 5 段阶段序列：正常 / preflight 失败 / 超时 / docker 失败 / 超时自适应。"""
from __future__ import annotations

from unittest import mock

import pytest

from modelctl.core import all_service
from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import Profile
from modelctl.engines.base import RequirementError

CAPS = Capabilities(gpu_count=1, compute_capability="8.9", binaries={})


def _profile(engine="vllm", docker=False):
    ec = {"model": "/m/x"}
    if docker:
        ec["docker_image"] = "img:tag"
    return Profile(name="q", engine=engine, port=8000, engine_config=ec)


class _FakeAdapter:
    is_docker = False
    log_tee = None

    def __init__(self, profile, caps):
        self.profile, self.caps = profile, caps
        self.warnings: list[str] = []
        self.spawned_proc = None
        self._progress_cb = None
        self.dead = False

    def check_requirements(self):
        return None

    def pre_start(self):
        return None

    def build_command(self):
        return ["echo", "hi"], {}

    def selected_gpus(self):
        return None

    def wait_ready(self, timeout):
        return True

    def post_start(self):
        return None

    def is_docker_runtime(self):
        return self.is_docker

    def log_tee_cmd(self):
        return self.log_tee

    def set_progress_sink(self, cb):
        self._progress_cb = cb

    def backend_dead(self):
        return self.dead

    def upstream_api_key(self):
        return None

    def metrics_mapping(self):
        return None


def _patch_common(tmp_path, adapter):
    """统一打桩：隔离 launch log / cache / 进程 / 快照，返回 patch 列表上下文。"""
    import contextlib

    @contextlib.contextmanager
    def ctx():
        with mock.patch.object(all_service, "get_adapter", return_value=lambda p, c: adapter), \
             mock.patch.object(all_service, "is_running_any", return_value=False), \
             mock.patch.object(all_service, "port_in_use", return_value=False), \
             mock.patch.object(all_service, "start_detached", return_value=(1234, None)), \
             mock.patch("modelctl.core.startup_progress.StartupTracker._write_snapshot", return_value=None), \
             mock.patch("modelctl.core.startup_progress.StartupTiming.__init__",
                        return_value=None) as _t, \
             mock.patch("modelctl.core.startup_progress.LoadingWatcher.start", return_value=None), \
             mock.patch("modelctl.core.startup_progress.LoadingWatcher.stop", return_value=None), \
             mock.patch.object(all_service, "launch_log", return_value=None), \
             mock.patch.object(all_service, "kill_log_tee"), \
             mock.patch.object(all_service, "spawn_log_tee", return_value=1) as tee:
            _t.side_effect = None
            yield tee

    return ctx()


def test_happy_path_stage_sequence(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELCTL_NO_GPU_LOCK", "1")
    events = []
    with _patch_common(tmp_path, _FakeAdapter(_profile(), CAPS)):
        r = all_service.start_profile(_profile(), CAPS, 5, on_progress=events.append)
    assert r.status == "ok"
    assert [(e.stage, e.status) for e in events] == [
        ("preflight", "running"), ("preflight", "done"),
        ("prepare_env", "running"), ("prepare_env", "done"),
        ("launch", "running"), ("launch", "done"),
        ("loading", "running"), ("loading", "done"),
        ("health", "done"),
    ]


def test_requirement_error_stops_at_preflight():
    class A(_FakeAdapter):
        def check_requirements(self):
            raise RequirementError("docker 命令不在 PATH")

    events = []
    with _patch_common(None, A(_profile(), CAPS)):
        with pytest.raises(RequirementError):
            all_service.start_profile(_profile(), CAPS, 5, on_progress=events.append)
    assert events[-1].stage == "preflight" and events[-1].status == "error"
    assert "docker" in events[-1].error


def test_timeout_marks_loading_error_with_detail():
    class A(_FakeAdapter):
        def wait_ready(self, timeout):
            return False

    events = []
    with _patch_common(None, A(_profile(), CAPS)):
        r = all_service.start_profile(_profile(), CAPS, 5, on_progress=events.append)
    assert r.status == "error" and r.detail == "健康检查超时"
    assert events[-1].stage == "loading" and events[-1].status == "error"
    assert events[-1].error == "健康检查超时"


def test_docker_path_spawns_log_tee():
    class A(_FakeAdapter):
        is_docker = True
        log_tee = ["docker", "logs", "-f", "--tail", "all", "q-vllm"]

    with _patch_common(None, A(_profile(docker=True), CAPS)) as tee:
        r = all_service.start_profile(_profile(docker=True), CAPS, 5)
    assert r.status == "ok"
    tee.assert_called_once_with("q", ["docker", "logs", "-f", "--tail", "all", "q-vllm"])


def test_no_progress_callback_is_backward_compatible():
    with _patch_common(None, _FakeAdapter(_profile(), CAPS)):
        assert all_service.start_profile(_profile(), CAPS, 5).status == "ok"


def test_default_start_timeout_by_runtime(monkeypatch):
    monkeypatch.delenv("MODELCTL_START_TIMEOUT", raising=False)
    d = _profile(docker=True)
    v = _profile(docker=False)
    with mock.patch.object(all_service, "get_adapter"):
        pass
    # docker 判定经 adapter.is_docker_runtime；用 fake 覆盖两条
    with mock.patch.object(all_service, "get_adapter",
                           return_value=lambda p, c: type("X", (), {"is_docker_runtime": lambda s: True})()):
        assert all_service.default_start_timeout(d, CAPS) == 1800.0
    with mock.patch.object(all_service, "get_adapter",
                           return_value=lambda p, c: type("X", (), {"is_docker_runtime": lambda s: False})()):
        assert all_service.default_start_timeout(v, CAPS) == 600.0
    monkeypatch.setenv("MODELCTL_START_TIMEOUT", "90")
    assert all_service.default_start_timeout(d, CAPS) == 90.0
```

- [ ] **Step 6: 运行确认失败**

Run: `python -m pytest tests/test_all_service_startup_progress.py -v`
Expected: FAIL — `start_profile() takes 3 positional arguments but 4 were given`

- [ ] **Step 7: 实现 `start_profile` 插桩 + `default_start_timeout`**

`all_service.py` 顶部 import 区补：

```python
from modelctl.core.process import kill_log_tee, spawn_log_tee
```

（其余 `is_running_any` / `port_in_use` / `start_detached` / `launch_log` 该模块已导入，保持原名以便上面测试 monkeypatch。）

在 `start_profile` 之前加：

```python
#: 健康检查默认超时：docker 运行时（容器内引擎冷启动含 import torch + 权重加载，
#: WSL2 上实测单 import 就 800s+）放宽到 1800s，避免慢而正常的启动被误判失败。
START_TIMEOUT_DEFAULT = 600.0
START_TIMEOUT_DOCKER = 1800.0


def default_start_timeout(profile: Profile, caps: Capabilities) -> float:
    """健康检查超时缺省值：MODELCTL_START_TIMEOUT > docker 1800 > 其它 600。"""
    raw = (os.environ.get("MODELCTL_START_TIMEOUT") or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning(f"MODELCTL_START_TIMEOUT 非数字，忽略：{raw!r}")
    try:
        if get_adapter(profile.engine)(profile, caps).is_docker_runtime():
            return START_TIMEOUT_DOCKER
    except Exception as exc:  # noqa: BLE001 —— 运行时判定失败退回保守默认
        logger.debug(f"is_docker_runtime 判定异常，用默认超时：{exc}")
    return START_TIMEOUT_DEFAULT
```

`start_profile` 整体替换为（保持既有执行顺序与文案，只加阶段事件 + tee + watcher）：

```python
def start_profile(profile: Profile, caps: Capabilities, timeout: float,
                  on_progress: "Callable[[Any], None] | None" = None) -> ComponentResult:
    """启动单个模型 profile（幂等：已运行返回 skipped）。

    check_requirements 失败时抛 RequirementError（配置错误语义，交给调用方/编排处理）。
    逻辑迁移自 cli._cmd_start。

    on_progress：可选 `StageEvent` 回调（core.startup_progress.StageEvent）。WebUI 绑
    task SSE、CLI 打日志；缺省 None 时除多写一份快照文件外行为与旧实现一致。
    """
    from modelctl.core.startup_progress import LoadingWatcher, StartupTracker

    tag = f"model:{profile.name}"
    if is_running_any(profile.name, profile):
        return ComponentResult(tag, "skipped", "已在运行")
    # 残留 tee 清理：上一次 start 未走完 / 容器被外力杀时 tee 可能仍在跟随旧容器
    kill_log_tee(profile.name)
    # 端口占用预检：走到这里端口仍被占 ⇒ 占用者不是本 profile，引擎启动后 bind 必然
    # EADDRINUSE 秒退；提前拦截并点名占用者，替代"空等健康检查 + 事后翻日志"。
    # RequirementError → cli exit 2，与配置/环境错误语义一致。
    # ollama 豁免：多个 ollama profile 共享同一 11434 serve 是设计语义（见 stop_profile
    # 同族特判）——第二个 profile 启动时 is_running_any 探 /health 得 404 不会 skip，
    # 靠"新 serve bind 失败但健康检查命中已有 serve"就绪，端口被占是正常状态。
    if profile.engine != "ollama" and port_in_use(profile.port):
        who = describe_port_listener(profile.port)
        raise RequirementError(
            f"端口 {profile.port} 已被占用（{who or '占用者未知'}），无法启动 {profile.name}。"
            f"请先释放该端口，或修改 profile 的 port 后重试"
        )
    adapter = get_adapter(profile.engine)(profile, caps)
    is_docker = adapter.is_docker_runtime()
    tracker = StartupTracker(profile.name, profile.engine, "docker" if is_docker else "venv",
                             on_progress=on_progress)

    def _env_sink(label: str, pct: float | None) -> None:
        tracker.progress("prepare_env", label, pct=pct)

    adapter.set_progress_sink(_env_sink if on_progress is not None else None)

    # ---- preflight：依赖检查 / 端口 / 兼容预检 ----
    tracker.begin("preflight")
    try:
        adapter.check_requirements()  # RequirementError 向上抛
    except RequirementError as exc:
        tracker.fail("preflight", STAGE_LABELS_PREFLIGHT, str(exc))
        raise
    for warning in adapter.warnings:
        logger.warning(warning)
    for warning in kv_estimate_warnings(profile):  # 附录 B.4：KV 显存预检（仅告警，不拦截）
        logger.warning(warning)
    tracker.done("preflight")

    # ---- prepare_env：pre_start（docker 拉镜像子进度 / 模型下载 / 编译） ----
    tracker.begin("prepare_env")
    try:
        adapter.pre_start()
    except RequirementError as exc:
        tracker.fail("prepare_env", STAGE_LABELS_PREPARE_ENV, str(exc))
        raise
    tracker.done("prepare_env")

    # ---- launch：build_command + start_detached（docker 路径随后挂日志 tee） ----
    tracker.begin("launch")
    cmd, env = adapter.build_command()
    # docker runtime（is_docker_runtime True）走 `docker run --detach`：容器在 daemon 后台续
    # 不会随 client 早退，PID 文件不写（write_pid=False）；venv runtime 维持默认 write_pid=True。
    pid, proc = start_detached(profile.name, cmd, env, write_pid=not is_docker)
    adapter.spawned_proc = proc  # 供 wait_ready 在进程早退时 fail-fast
    if is_docker:
        tee_cmd = adapter.log_tee_cmd()
        if tee_cmd:
            spawn_log_tee(profile.name, tee_cmd)
    try:
        from modelctl.core.gpu_lock import update_gpu_lock_owner

        if adapter.selected_gpus():
            update_gpu_lock_owner(profile.name, pid)
    except Exception:
        pass
    tracker.done("launch")

    # ---- loading：等待窗口内 tail 引擎日志按模式表推进 ----
    logger.info(f"已启动 {profile.name}（PID {pid}），等待健康检查（超时 {timeout:g}s）...")
    tracker.begin("loading", "等待引擎初始化")
    watcher: LoadingWatcher | None = None
    log = launch_log(profile.name)
    if log is not None:
        watcher = LoadingWatcher(tracker, profile.engine, log)
        watcher.start()
    try:
        ready = adapter.wait_ready(timeout)
    finally:
        if watcher is not None:
            watcher.stop()

    if ready:
        tracker.done("loading", "引擎初始化完成")
        tracker.done("health", f"就绪：http://127.0.0.1:{profile.port}")
        upstream_key = adapter.upstream_api_key()
        if upstream_key and upstream_key != profile.api_key:
            logger.info(f"上游 API Key（本次启动自动生成）：{upstream_key}")
        adapter.post_start()
        log = launch_log(profile.name)
        logger.info(f"启动成功：{profile.name} 运行于 http://127.0.0.1:{profile.port}")
        if log is not None:
            logger.info(f"日志：{log}")
        if profile.usage or adapter.metrics_mapping() is not None:
            logger.info("提示：用量统计可通过 `modelctl stats start` 启动")
        return ComponentResult(tag, "ok", f"http://127.0.0.1:{profile.port}")

    kill_log_tee(profile.name)
    # 死亡判定交给引擎适配器：docker 分支以容器状态衡量（客户端进程早退≠容器死亡），
    # venv 分支维持"本工具拉起的进程早退即死亡"的语义
    died = adapter.backend_dead()
    detail = "引擎进程提前退出" if died else "健康检查超时"
    tracker.fail("loading", detail, detail)
    if log is None:
        logger.warning("引擎未在时限内就绪，且未找到启动日志")
    elif died:
        # 进程早退：真实异常通常在日志中部，按错误标记截取上下文；无标记时退回尾部 50 行
        logger.warning(f"引擎进程提前退出（PID {pid}），未能就绪。相关日志摘录（{log}）：")
        logger.warning(log_excerpt(log) or tail_file(log, 50))
    else:
        logger.warning(f"健康检查超时，日志尾部 50 行（{log}）：")
        logger.warning(tail_file(log, 50))
    return ComponentResult(tag, "error", detail)
```

在 `all_service.py` 模块级（`start_profile` 之前）加两个 label 常量（避免字符串字面量散落）：

```python
STAGE_LABELS_PREFLIGHT = "依赖检查"
STAGE_LABELS_PREPARE_ENV = "准备环境"
```

`stop_profile` 在 `adapter.stop_backend()` 之后（以及 ollama 分支两个子路径之后）加 `kill_log_tee(profile.name)`——docker 容器被删后 `docker logs -f` 会自行退出，但 stop 时主动 kill 保证 PID 文件与句柄即时释放：

```python
    else:
        adapter.stop_backend()
    kill_log_tee(profile.name)
    logger.info(f"已停止：{profile.name}")
```

`restart_profile` 加尾参并透传：

```python
def restart_profile(profile: Profile, caps: Capabilities, timeout: float,
                    on_progress: "Callable[[Any], None] | None" = None) -> ComponentResult:
    ...
    if is_running_any(profile.name, profile):
        stop_profile(profile, caps, None)
    return start_profile(profile, caps, timeout, on_progress=on_progress)
```

- [ ] **Step 8: 运行确认通过**

Run: `python -m pytest tests/test_all_service_startup_progress.py -v`
Expected: PASS（6 个）

> 若 happy-path 序列断言因 `loading` 兜底文案多出 `running` 帧而失败，属预期差异：把断言改成**按 (stage,status) 去重后**的序列比较，或在 `LoadingWatcher._tick` 里首帧不兜底（`_last_pct == 0.0` 且已 `advanced=False` 时要求 `t0` 超阈值，实现里已如此）。不要为此放宽断言到「只查首尾」。

- [ ] **Step 9: 回归既有 all_service / reconcile 测试**

Run: `python -m pytest tests/ -k "all_service or reconcile or start" -q`
Expected: 全绿（`reconcile._default_starter` 不传 on_progress，行为不变）

- [ ] **Step 10: 提交**

```bash
git add src/modelctl/core/all_service.py src/modelctl/core/process.py tests/test_all_service_startup_progress.py tests/test_docker_log_tee.py
git commit -m "feat(startup-progress): start_profile 5 段插桩 + 日志 tee 生命周期 + 超时自适应"
```

---

## Task 8: CLI `--timeout` 缺省自适应 + 进度单行日志

**Files:**
- Modify: `src/modelctl/cli.py:118-121`（start/restart parser）、`:132`（all parser）、`:410-427`（`_cmd_start` / `_cmd_restart`）
- Test: `tests/test_cli_startup_progress.py`

**Interfaces:**
- Consumes: `all_service.default_start_timeout` / `start_profile(..., on_progress=)`（Task 7）、`StageEvent.to_dict`（Task 3）
- Produces:
  - `cli._start_timeout(args, profile, caps) -> float`
  - `cli._cli_progress(profile) -> Callable[[Any], None]`（logger 单行；调用侧拼好完整消息，遵守 CLAUDE.md 日志对齐规范）

- [ ] **Step 1: 写失败测试**

Create `tests/test_cli_startup_progress.py`:

```python
"""CLI：--timeout 未显式指定时按运行时自适应；start 传 on_progress 打单行进度。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from modelctl import cli
from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import Profile

CAPS = Capabilities(gpu_count=1, compute_capability="8.9", binaries={})
_P = Profile(name="q", engine="vllm", port=8000, engine_config={"model": "/m/x"})


def test_start_timeout_none_delegates(monkeypatch):
    args = SimpleNamespace(timeout=None)
    with mock.patch.object(cli.all_service, "default_start_timeout", return_value=1800.0) as d:
        assert cli._start_timeout(args, _P, CAPS) == 1800.0
    d.assert_called_once_with(_P, CAPS)


def test_start_timeout_explicit_wins(monkeypatch):
    args = SimpleNamespace(timeout=42)
    with mock.patch.object(cli.all_service, "default_start_timeout", return_value=1800.0):
        assert cli._start_timeout(args, _P, CAPS) == 42.0


def test_cmd_start_passes_progress_callback():
    from modelctl.core.startup_progress import StageEvent

    events = []
    with mock.patch.object(cli.all_service, "start_profile", return_value=SimpleNamespace(
            status="ok", detail="ok")) as sp, \
         mock.patch.object(cli, "load_profile", return_value=_P), \
         mock.patch.object(cli, "_start_timeout", return_value=600.0), \
         mock.patch.object(cli, "_cli_progress", return_value=events.append):
        assert cli._cmd_start(SimpleNamespace(name="q", timeout=None), None, CAPS) == 0
    assert sp.call_args.kwargs.get("on_progress") is events.append


def test_cli_progress_line_single_and_label():
    from modelctl.core.startup_progress import StageEvent

    msgs: list[str] = []
    sink = cli._cli_progress(_P)
    sink(StageEvent("prepare_env", "running", "拉取镜像 img:tag（3/9 层）", pct=0.4, eta_s=120))
    # 通过 logger 捕获验证单行 + 关键内容
    from loguru import logger
    logger.remove()
    logger.add(lambda m: msgs.append(m.record["message"]))
    sink(StageEvent("prepare_env", "running", "拉取镜像 img:tag（3/9 层）", pct=0.4, eta_s=120))
    assert len(msgs) == 1 and "\n" not in msgs[0]
    assert "拉取镜像" in msgs[0] and "40%" in msgs[0] and "约剩 2 分钟" in msgs[0]


def test_cli_progress_error_state_shows_error():
    from modelctl.core.startup_progress import StageEvent

    msgs: list[str] = []
    from loguru import logger
    logger.remove()
    logger.add(lambda m: msgs.append(m.record["message"]))
    cli._cli_progress(_P)(StageEvent("preflight", "error", "依赖检查", error="docker 不在 PATH"))
    assert "docker 不在 PATH" in msgs[-1]
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_cli_startup_progress.py -v`
Expected: FAIL — `module 'modelctl.cli' has no attribute '_start_timeout'`

- [ ] **Step 3: 实现**

`cli.py` 中 `_cmd_start` 之前加：

```python
def _start_timeout(args, profile, caps) -> float:
    """--timeout 未显式指定（None）→ 按运行时自适应；显式值优先（含 MODELCTL_START_TIMEOUT）。"""
    if getattr(args, "timeout", None) is not None:
        return float(args.timeout)
    return all_service.default_start_timeout(profile, caps)


def _cli_progress(profile):
    """CLI 侧 StageEvent → loguru 单行；消息在调用侧拼好（日志对齐规范：loguru 只管前缀）。"""
    def sink(ev) -> None:
        if ev.status == "error":
            logger.error(f"[{profile.name}] {STAGE_LABELS.get(ev.stage, ev.stage)}失败：{ev.error}")
            return
        parts = [STAGE_LABELS.get(ev.stage, ev.stage), ev.label]
        if ev.pct is not None:
            parts.append(f"{round(ev.pct * 100)}%")
        if ev.eta_s is not None:
            parts.append(f"约剩 {_human_eta(ev.eta_s)}")
        sep = " —— " if ev.status == "running" else "："
        msg = parts[0] + sep + parts[1] + ("（" + "，".join(parts[2:]) + "）" if len(parts) > 2 else "")
        if ev.status == "done":
            logger.info(f"[{profile.name}] {msg}")
        else:
            logger.info(f"[{profile.name}] {msg}")
    return sink


def _human_eta(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    m, s = divmod(int(seconds), 60)
    return f"{m} 分钟" if not s else f"{m} 分 {s} 秒"
```

`from modelctl.core.startup_progress import STAGE_LABELS` 放函数内延迟 import（`cli.py` 与 core 的既有延迟导入约定一致），改写为：

```python
def _cli_progress(profile):
    from modelctl.core.startup_progress import STAGE_LABELS
    ...
```

`_cmd_start` / `_cmd_restart` 替换：

```python
def _cmd_start(args, models_dir: Path | None, caps) -> int:
    profile = load_profile(args.name, models_dir)
    r = all_service.start_profile(profile, caps, _start_timeout(args, profile, caps),
                                  on_progress=_cli_progress(profile))
    if r.status == "skipped":
        logger.info(r.detail)
    return 0 if r.status in ("ok", "skipped") else 1


def _cmd_restart(args, models_dir: Path | None, caps) -> int:
    profile = load_profile(args.name, models_dir)
    r = all_service.restart_profile(profile, caps, _start_timeout(args, profile, caps),
                                    on_progress=_cli_progress(profile))
    return 0 if r.status in ("ok", "skipped") else 1
```

parser 三处 `--timeout` 的 `default=600` → `default=None`，帮助文案改为：

```python
            # 未显式指定时按运行时自适应：docker 1800s（容器内引擎冷启动含 import + 权重
            # 加载，WSL2 实测单 import 就 800s+）/ 其它 600s；MODELCTL_START_TIMEOUT 覆盖
            p.add_argument("--timeout", type=float, default=None,
                           help="健康检查超时秒数（默认 docker 1800 / 其它 600）")
```

`all` 子命令的 `--timeout` 同样改 `default=None`，并在其调用 `start_profile` 处（`all_service.all_start` 链路）沿用 `default_start_timeout`：`all_service` 内若有 `timeout or 600` 的兜底，改为 `timeout if timeout is not None else default_start_timeout(profile, caps)`。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_cli_startup_progress.py -v`
Expected: PASS（4 个）

- [ ] **Step 5: 回归 CLI 测试**

Run: `python -m pytest tests/ -k cli -q`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add src/modelctl/cli.py src/modelctl/core/all_service.py tests/test_cli_startup_progress.py
git commit -m "feat(startup-progress): CLI 超时自适应 + 阶段进度单行日志"
```

---

## Task 9: WebUI — stage 事件广播 + `/startup` 快照端点 + 超时自适应

**Files:**
- Modify: `src/modelctl/core/webui/admin_models.py:148-224`（`_do_start`/`_do_restart`）、`:328-335` / `:395-402`（timeout 参数）、新增端点
- Test: `tests/test_webui_startup_progress.py`

**Interfaces:**
- Consumes: `start_profile(..., on_progress=)`（Task 7）、`StartupTracker` 快照路径 `cache/<name>.startup.json`、`Task.event`
- Produces:
  - `GET /admin/api/models/{name}/startup` → `200` camelCase：`{profile, engine, runtime, updatedAt, stages:[{stage,status,label,pct,etaSeconds,error,startedAt,finishedAt}]}`；无快照 → `404 {error:{code:"not_found"}}`
  - start/restart 的 `timeout: float | None = Query(default=None, ge=1, le=7200)`

- [ ] **Step 1: 写失败测试**

Create `tests/test_webui_startup_progress.py`:

```python
"""GET /models/{name}/startup 快照端点 + start 任务 stage 事件广播。"""
from __future__ import annotations

import json
from unittest import mock

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_startup"


@pytest.fixture()
def admin_client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    logger.remove()
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c


def _get(client, path):
    return client.get(path, headers={"Authorization": f"Bearer {KEY}"})


def test_startup_snapshot_404_when_absent(admin_client, monkeypatch):
    monkeypatch.setattr("modelctl.core.paths.cache_dir",
                        lambda: admin_client.app.state.__class__ and __import__("pathlib").Path("/nonexistent-xyz"))
    r = _get(admin_client, "/admin/api/models/nope/startup")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_startup_snapshot_camel_case(admin_client, monkeypatch, tmp_path):
    (tmp_path / "q.startup.json").write_text(json.dumps({
        "profile": "q", "engine": "vllm", "runtime": "docker",
        "updated_at": "2026-09-08 03:18:41",
        "stages": [{"stage": "prepare_env", "status": "running", "label": "拉取镜像",
                    "pct": 0.45, "eta_s": 360, "error": None,
                    "started_at": "2026-09-08 03:00:00", "finished_at": None}],
    }), encoding="utf-8")
    monkeypatch.setattr("modelctl.core.paths.cache_dir", lambda: tmp_path)
    r = _get(admin_client, "/admin/api/models/q/startup")
    assert r.status_code == 200
    body = r.json()
    assert body["updatedAt"] == "2026-09-08 03:18:41"
    assert body["stages"][0]["etaSeconds"] == 360
    assert body["stages"][0]["stage"] == "prepare_env"


def test_startup_snapshot_corrupt_json_404(admin_client, monkeypatch, tmp_path):
    (tmp_path / "q.startup.json").write_text("{ broken", encoding="utf-8")
    monkeypatch.setattr("modelctl.core.paths.cache_dir", lambda: tmp_path)
    assert _get(admin_client, "/admin/api/models/q/startup").status_code == 404


def test_do_start_bridges_stage_events_to_task():
    from modelctl.core.webui.admin_models import _do_start
    from modelctl.core.webui.admin_tasks import Task
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import Profile

    caps = Capabilities(gpu_count=1, compute_capability="8.9", binaries={})
    prof = Profile(name="q", engine="vllm", port=8000, engine_config={"model": "/m/x"})
    task = Task(id="t1", kind="model_start", action="start", target="q")
    seen = []
    task.event = lambda et, data: seen.append((et, data))

    def fake_start(profile, caps, timeout, on_progress=None):
        from modelctl.core.startup_progress import StageEvent
        from modelctl.core.all_service import ComponentResult
        on_progress(StageEvent("prepare_env", "running", "拉取镜像（3/9 层）", pct=0.4, eta_s=120))
        return ComponentResult("model:q", "ok", "http://127.0.0.1:8000")

    import asyncio
    with mock.patch("modelctl.core.all_service.start_profile", side_effect=fake_start):
        asyncio.run(_do_start(prof, caps, 600, task, None))
    stages = [d for et, d in seen if et == "stage"]
    assert stages and stages[-1]["stage"] == "prepare_env" and stages[-1]["pct"] == 0.4
    assert task.status == "success"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_webui_startup_progress.py -v`
Expected: FAIL — 404/405（端点不存在）

- [ ] **Step 3: 实现 `/startup` 端点**

在 `admin_models.py` 的 `get_model_log` 之前加：

```python
@router.get("/{name}/startup")
async def get_startup_progress(name: str, _: None = Depends(require_auth)):
    """GET /admin/api/models/{name}/startup — 最近一次启动的阶段进度快照。

    数据源为 all_service 每次阶段事件覆写的 `cache/<name>.startup.json`（尽力而为，
    写失败则无快照 → 404）。非发起者浏览器 / 页面刷新后据此渲染进度卡片，不依赖 SSE 时序。
    """
    from modelctl.core.paths import cache_dir
    from modelctl.core.startup_progress import STAGES

    path = cache_dir() / f"{name}.startup.json"
    if not path.is_file():
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 暂无启动进度记录"}},
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"启动进度快照不可读（{path}）：{exc}")
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 启动进度快照损坏"}},
        )
    stages = []
    for s in raw.get("stages") or []:
        stages.append({
            "stage": s.get("stage"),
            "status": s.get("status", "pending"),
            "label": s.get("label") or "",
            "pct": s.get("pct"),
            "etaSeconds": s.get("eta_s"),
            "error": s.get("error"),
            "startedAt": s.get("started_at"),
            "finishedAt": s.get("finished_at"),
        })
    return {
        "profile": raw.get("profile") or name,
        "engine": raw.get("engine") or "",
        "runtime": raw.get("runtime") or "",
        "updatedAt": raw.get("updated_at") or "",
        "stages": stages,
        "knownStages": list(STAGES),
    }
```

- [ ] **Step 4: 实现 stage 事件桥接 + 超时自适应**

`_do_start` / `_do_restart` 内，把 `start_profile` / `restart_profile` 调用改为带回调，并把事件广播到 task。`_do_start` 的 try 块替换：

```python
    def _on_stage(ev) -> None:
        """阶段事件 → task SSE（与 docker 一键安装同一事件基建，_sse_task_stream 零改动透传）。"""
        task.event("stage", {
            "stage": ev.stage,
            "status": ev.status,
            "label": ev.label,
            "pct": ev.pct,
            "etaSeconds": ev.eta_s,
            "error": ev.error,
            "task_id": task.id,
        })
        pct = "" if ev.pct is None else f" {round(ev.pct * 100)}%"
        task.update_detail(f"{ev.label}{pct}")

    try:
        task.update_status("running")
        result = await asyncio.to_thread(
            lambda: start_profile(profile, caps, timeout, on_progress=_on_stage)
        )
        task.update_detail(result.detail)
        if result.status == "error":
            _fail_task(task, 1, result.detail, profile.engine)
        else:
            task.complete()
```

> `asyncio.to_thread(fn, *args)` 无法传闭包 kwargs 的语义不变，这里用 `lambda` 包一层保持 `asyncio.to_thread` 的线程切换；`_do_restart` 同构替换 `restart_profile`。

两个端点签名 `timeout: float = Query(default=600, ge=1, le=3600)` → `timeout: float | None = Query(default=None, ge=1, le=7200)`，并在 `asyncio.ensure_future` 之前解析：

```python
    from modelctl.core.all_service import default_start_timeout

    eff_timeout = timeout if timeout is not None else await asyncio.to_thread(
        default_start_timeout, profile, caps
    )
```

`_do_start(profile, caps, eff_timeout, task, gpus)` 用 `eff_timeout`。

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest tests/test_webui_startup_progress.py -v`
Expected: PASS（4 个）

- [ ] **Step 6: 回归 webui 测试**

Run: `python -m pytest tests/test_webui_smoke.py tests/test_admin_tasks.py tests/test_webui_admin_envs.py -q`
Expected: 全绿

- [ ] **Step 7: 提交**

```bash
git add src/modelctl/core/webui/admin_models.py tests/test_webui_startup_progress.py
git commit -m "feat(startup-progress): WebUI stage 事件广播 + /startup 快照端点 + 超时自适应"
```

---

## Task 10: 前端 — 类型 / API / `StartupProgressCard` / 挂载 / SSE `onopen`

**Files:**
- Modify: `web/src/api/types.ts`
- Modify: `web/src/api/models.ts`
- Create: `web/src/components/startup/StartupProgressCard.vue`
- Modify: `web/src/views/ModelDetailView.vue`
- Modify: `web/src/api/sse.ts`、`web/src/components/common/SseLogViewer.vue`

**Interfaces:**
- Consumes: `GET /models/{name}/startup`（Task 9）、task SSE `stage` 事件
- Produces:
  - `StartupSnapshot` / `StartupStage` 类型 + `getStartup(name)` API
  - `<StartupProgressCard :snapshot :runtime-engine />`

- [ ] **Step 1: 加类型**

`web/src/api/types.ts` 追加：

```ts
/** 启动阶段名（与后端 STAGES 严格一致） */
export type StartupStageName = 'preflight' | 'prepare_env' | 'launch' | 'loading' | 'health';

/** 单阶段快照 */
export interface StartupStage {
  stage: StartupStageName;
  status: 'pending' | 'running' | 'done' | 'error';
  label: string;
  /** 0–1；null 表示不确定态（条纹动画） */
  pct: number | null;
  /** 预估剩余秒；null 表示首次运行无预估 */
  etaSeconds: number | null;
  error: string | null;
  startedAt: string | null;
  finishedAt: string | null;
}

/** GET /models/{name}/startup 响应 */
export interface StartupSnapshot {
  profile: string;
  engine: string;
  runtime: 'docker' | 'venv' | string;
  updatedAt: string;
  stages: StartupStage[];
  knownStages: StartupStageName[];
}
```

- [ ] **Step 2: 加 API + 去掉前端写死的 timeout**

`web/src/api/models.ts`：`ModelActionOpts.timeout` 注释改为「不传则由后端按运行时自适应（docker 1800 / 其它 600）」，并追加：

```ts
import type { StartupSnapshot } from './types';

/** 最近一次启动的阶段进度快照（无记录 404，调用方需 catch）。 */
export function getStartup(name: string): Promise<StartupSnapshot> {
  return dataOf<StartupSnapshot>(client.get(`/models/${encodeURIComponent(name)}/startup`));
}
```

- [ ] **Step 3: `StartupProgressCard.vue`**

Create `web/src/components/startup/StartupProgressCard.vue`（视觉语言复用 DockerInstallPanel 的时间轴 + 徽标）:

```vue
<script setup lang="ts">
import { computed } from 'vue';
import type { StartupSnapshot, StartupStage, StartupStageName } from '@/api/types';

/**
 * 启动进度卡片：5 段时间轴 + 当前阶段进度条（确定态实条 / 不确定态条纹）+ ETA + 错误态。
 *
 * 数据源由父组件提供：发起页合并 task SSE 的 stage 帧，旁观/刷新后读 /startup 快照。
 * 时间字段（startedAt / finishedAt）后端已按 YYYY-MM-DD HH:mm:ss 格式化，前端不再加工。
 */
const props = defineProps<{
  snapshot: StartupSnapshot;
  /** docker 时错误提示引导「环境」页修 docker */
  toEnvPage?: () => void;
}>();

const ORDER: StartupStageName[] = ['preflight', 'prepare_env', 'launch', 'loading', 'health'];
const LABELS: Record<StartupStageName, string> = {
  preflight: '依赖检查',
  prepare_env: '准备环境',
  launch: '拉起进程',
  loading: '加载模型',
  health: '就绪',
};

/** 按固定顺序补齐缺失阶段（后端快照可能只含部分阶段） */
const stages = computed<StartupStage[]>(() =>
  ORDER.map(
    (n) =>
      props.snapshot.stages.find((s) => s.stage === n) ?? {
        stage: n,
        status: 'pending',
        label: LABELS[n],
        pct: null,
        etaSeconds: null,
        error: null,
        startedAt: null,
        finishedAt: null,
      },
  ),
);

const currentIndex = computed(() => {
  const run = stages.value.findIndex((s) => s.status === 'running');
  if (run >= 0) return run;
  const err = stages.value.findIndex((s) => s.status === 'error');
  if (err >= 0) return err;
  const lastDone = stages.value.reduce((acc, s, i) => (s.status === 'done' ? i : acc), -1);
  return Math.min(stages.value.length - 1, lastDone + 1);
});

const current = computed(() => stages.value[currentIndex.value]);
const failed = computed(() => stages.value.some((s) => s.status === 'error'));
const succeeded = computed(
  () => !failed.value && stages.value.every((s) => s.status === 'done'),
);

/** 当前阶段百分比（null → 条纹动画） */
const pctInt = computed(() =>
  current.value.pct === null ? null : Math.max(0, Math.min(100, Math.round(current.value.pct * 100))),
);
const etaText = computed(() => {
  const s = current.value.etaSeconds;
  if (s === null || s === undefined) return '首次运行，无预估';
  if (s < 60) return `约剩 ${s} 秒`;
  const m = Math.round(s / 60);
  return `约剩 ${m} 分钟`;
});
/** 错误文案含环境类关键词时给出跳转入口 */
const showEnvLink = computed(() =>
  failed.value && /(环境未创建|未安装|不在 PATH|Docker 环境)/.test(current.value.error ?? ''),
);

function dotClass(i: number): string {
  const st = stages.value[i].status;
  if (st === 'error') return 'bg-red-400';
  if (st === 'done') return 'bg-emerald-400/70';
  if (st === 'running') return 'bg-emerald-400 animate-pulse';
  return 'bg-slate-600';
}
function textClass(i: number): string {
  const st = stages.value[i].status;
  if (st === 'error') return 'text-red-300';
  if (i === currentIndex.value) return 'text-emerald-300';
  if (st === 'done') return 'text-emerald-400/70';
  return 'text-slate-500';
}
</script>

<template>
  <div class="startup-card card space-y-3">
    <!-- 标题 + 状态徽标 -->
    <div class="flex items-baseline justify-between">
      <div>
        <h3 class="text-sm font-medium text-slate-200">启动进度</h3>
        <p class="mt-0.5 text-xs text-slate-500">
          运行时 <span class="font-mono text-slate-300">{{ snapshot.runtime }}</span> ·
          引擎 <span class="font-mono text-slate-300">{{ snapshot.engine }}</span> ·
          更新于 {{ snapshot.updatedAt || '—' }}
        </p>
      </div>
      <span
        v-if="succeeded"
        class="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-600/15 px-2 py-0.5 text-xs text-emerald-300"
      ><span class="size-1.5 rounded-full bg-emerald-400" />已就绪</span>
      <span
        v-else-if="failed"
        class="inline-flex items-center gap-1.5 rounded-full border border-red-500/30 bg-red-600/15 px-2 py-0.5 text-xs text-red-300"
      ><span class="size-1.5 rounded-full bg-red-400" />启动失败</span>
    </div>

    <!-- 5 段时间轴 -->
    <ol class="startup-card__steps flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs">
      <li v-for="(s, i) in stages" :key="s.stage" :class="['flex items-center gap-1.5', textClass(i)]">
        <span :class="['size-1.5 rounded-full', dotClass(i)]" />
        <span>{{ LABELS[s.stage] }}</span>
        <span v-if="i < stages.length - 1" class="mx-1 text-slate-600">→</span>
      </li>
    </ol>

    <!-- 当前阶段进度条 -->
    <div class="space-y-1.5">
      <div class="flex flex-wrap items-center justify-between gap-2 text-xs">
        <span class="text-slate-300">{{ current.label }}</span>
        <span v-if="current.status !== 'done'" class="text-slate-400">
          {{ pctInt !== null ? `${pctInt}% · ${etaText}` : etaText }}
        </span>
      </div>
      <div class="startup-card__track h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
        <div
          v-if="pctInt !== null"
          class="h-full rounded-full bg-emerald-400/80 transition-[width] duration-500"
          :style="{ width: `${pctInt}%` }"
        />
        <div v-else class="startup-card__stripes h-full w-full" />
      </div>
    </div>

    <!-- 错误态 -->
    <div v-if="failed && current.error" class="space-y-2">
      <div class="rounded-md border border-red-500/30 bg-red-600/10 px-3 py-2 text-sm leading-5 whitespace-pre-line text-red-300">
        {{ current.error }}
      </div>
      <button v-if="showEnvLink && toEnvPage" class="btn-ghost !py-1 !px-2 text-xs" @click="toEnvPage">
        去环境页
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 不确定态条纹动画（BEM：块 startup-card，元素 __stripes） */
.startup-card__stripes {
  background-image: repeating-linear-gradient(
    45deg,
    rgba(52, 211, 153, 0.45) 0,
    rgba(52, 211, 153, 0.45) 8px,
    rgba(52, 211, 153, 0.15) 8px,
    rgba(52, 211, 153, 0.15) 16px
  );
  background-size: 22.6px 100%;
  animation: startup-card-stripes 1.1s linear infinite;
}
@keyframes startup-card-stripes {
  from {
    background-position: 0 0;
  }
  to {
    background-position: 22.6px 0;
  }
}
@media (prefers-reduced-motion: reduce) {
  .startup-card__stripes {
    animation: none;
  }
}
</style>
```

- [ ] **Step 4: 挂载到 `ModelDetailView.vue`**

import 区加：

```ts
import { getStartup } from '@/api/models';
import type { StartupSnapshot } from '@/api/types';
import StartupProgressCard from '@/components/startup/StartupProgressCard.vue';
```

script 内加状态与拉取（快照 + `starting` 期间 2s 轮询；失败静默——无快照属正常）：

```ts
const startup = ref<StartupSnapshot | null>(null);
let startupTimer: number | undefined;
/** 拉一次启动进度快照（无记录 404 → 清空卡片） */
async function refreshStartup() {
  try {
    startup.value = await getStartup(name.value);
  } catch {
    startup.value = null;
  }
}
/** 卡片可见：启动中/停止态但进度未收尾，或失败收尾 */
const showStartup = computed(() => {
  const s = startup.value;
  if (!s) return false;
  if (s.stages.some((x) => x.status === 'error')) return true;
  if (detail.value?.state === 'running') return false;
  return s.stages.some((x) => x.status === 'running' || x.status === 'pending');
});
```

`onMounted` 内加 `void refreshStartup();`，并在 `timer` 之后加轮询：

```ts
  startupTimer = window.setInterval(() => void refreshStartup(), 2000);
```

`onBeforeUnmount` 加：

```ts
  if (startupTimer !== undefined) clearInterval(startupTimer);
```

模板中在「上部分览」`</section>` 之后、「中部 tab」`<section class="card !p-0">` 之前插入：

```html
    <!-- 启动进度卡片（启动中 / 启动失败时常驻） -->
    <StartupProgressCard
      v-if="showStartup && startup"
      :snapshot="startup"
      :to-env-page="() => router.push({ name: 'envs' })"
    />
```

`@success` 回调里同步刷新进度：`@success="() => { refresh(); void refreshStartup(); }"`（启动/重启两个 TaskButton）。

- [ ] **Step 5: 修「连接中」假状态（`onopen`）**

`web/src/api/sse.ts`：`LogStreamHooks` 加 `onOpen?: () => void;`，在 `openModelLogStream` 内注册：

```ts
  const onOpen = () => hooks.onOpen?.();
  es.addEventListener('open', onOpen);
```

`close()` 里补 `es.removeEventListener('open', onOpen);`。

`SseLogViewer.vue`：
- `open()` 的 hooks 加 `onOpen: () => { if (state.value === 'connecting') state.value = 'open'; }`；
- `push()` 里删除 `if (state.value === 'connecting') state.value = 'open';`（状态改由 `onOpen` 统一驱动，不再依赖首行日志）。

- [ ] **Step 6: 类型检查通过**

Run: `cd web; npm run typecheck`
Expected: 无错误（vue-tsc 通过）

- [ ] **Step 7: 构建产物并冒烟**

Run: `cd web; npm run build`
Expected: 构建成功（后端静态目录刷新）

- [ ] **Step 8: 提交**

```bash
git add web/src/api/types.ts web/src/api/models.ts web/src/api/sse.ts web/src/components/startup/StartupProgressCard.vue web/src/components/common/SseLogViewer.vue web/src/views/ModelDetailView.vue
git commit -m "feat(startup-progress): StartupProgressCard 卡片 + 详情页挂载 + SSE onopen 修假连接中"
```

---

## Task 11: 已知陷阱沉淀 + 全量回归

**Files:**
- Modify: `docs/known-pitfalls/README.md`
- Create: `docs/known-pitfalls/backend/启动进度与日志可观测性.md`

**Interfaces:**
- Consumes: 本计划全部
- Produces: 渐进式披露的问题条目（摘要层 + 详情层）

- [ ] **Step 1: 全量后端回归**

Run: `python -m pytest tests/ -q --tb=short`
Expected: 全绿（若历史用例因 `start_profile` 新增尾参而失败，改测试为关键字传参 `on_progress=`，勿放宽断言）

- [ ] **Step 2: 前端类型检查**

Run: `cd web; npm run typecheck`
Expected: 通过

- [ ] **Step 3: 详情层文件**

Create `docs/known-pitfalls/backend/启动进度与日志可观测性.md`，按 CLAUDE.md 规范（文件头写"原始单文件已并入本文件归档"，每问题 `## <标题>`，含根因 / 解决方案 / 代码示例），记录三条：

1. `docker run --detach` 路径下 launch log 只有容器 ID，SSE / CLI logs / 早退摘录全废 —— 根因（容器输出在 daemon 侧）、解决（`docker logs -f --tail all` tee 到 launch log，append + 独立 PID 文件 + start 前清残留 + stop 时 kill）、代码示例。
2. 长启动静默导致误判"卡死"并人为重试杀掉慢而正常的冷启动 —— 根因（`subprocess.run(capture_output=True)` 吞 pull 输出、`_do_start` 全程零事件、`--tail 0` 漏 banner），解决（5 段阶段机 + PullParser + LoadingWatcher + 120s 兜底文案 + docker 默认超时 1800s）。
3. `EventSource` 无首行日志时前端永显「连接中…」—— 根因（状态只在 `onLine` 里翻转）、解决（注册 `open` 事件驱动 `state='open'`）。

- [ ] **Step 4: 摘要层**

在 `docs/known-pitfalls/README.md` 的索引表（或对应分类列表）末尾追加 3 行，格式与既有条目一致（标题 / 分类 / 日期 / 一句话描述），指向 `backend/启动进度与日志可观测性.md`：

```markdown
| docker 路径 launch log 只有容器 ID，日志全废 | backend | 2026-09-08 | 容器输出在 daemon 侧，需 `docker logs -f --tail all` tee 到 launch log（[详情](backend/启动进度与日志可观测性.md#docker-run---detach-路径下-launch-log-只有容器-id)） |
| 长启动静默导致误杀慢启动（Exit 137） | backend | 2026-09-08 | pull/加载全程零输出 + 600s 默认超时误杀 docker 冷启动，需阶段机 + 1800s docker 超时（[详情](backend/启动进度与日志可观测性.md#长启动静默导致误判卡死并人为重试杀掉慢而正常的冷启动)） |
| SSE 无首行日志时前端永显「连接中…」 | frontend | 2026-09-08 | 状态只在首行日志回调里翻转，需注册 `EventSource` 的 `open` 事件（[详情](backend/启动进度与日志可观测性.md#eventsource-无首行日志时前端永显连接中)） |
```

（若 README 现有格式是列表而非表格，则按现有格式改写以上 3 条，保持"标题 + 分类 + 日期 + 一句话描述 + 详情链接"五要素即可。）

- [ ] **Step 5: 端到端手动验收（Windows docker 路径）**

前置：`API_KEY` 已设，Docker Desktop 运行中，`models/vllm/qwen2.5-1.5b.yaml` 含 `docker_image: vllm/vllm-openai:latest`。

1. `python -m modelctl.cli start qwen2.5-1.5b-vllm`（或管理端点触发）——观察 CLI 逐段输出「[1/5] 依赖检查 → [2/5] 准备环境（拉取镜像 x%）→ [3/5] 拉起进程 → [4/5] 加载模型 → [5/5] 就绪」；
2. 期间打开详情页：启动进度卡片随轮询推进，条纹动画出现在 pct=None 阶段；日志窗口应立即显示「已连接」并在镜像拉完前就有容器日志流入；
3. 等 `docker ps` 出现容器、健康检查通过：`curl http://127.0.0.1:8107/v1/models` 返回模型列表；卡片在 running 后隐藏；
4. 再次启动同名 profile：`prepare_env` 应秒过（镜像已在本地，快照 pct 直接 done）；
5. 人为制造失败（临时把 `docker_image` 改成不存在的 tag）：卡片显示红色错误段 + 分类文案，「去环境页」按钮可点。

- [ ] **Step 6: 提交文档**

```bash
git add docs/known-pitfalls/README.md "docs/known-pitfalls/backend/启动进度与日志可观测性.md"
git commit -m "docs(known-pitfalls): 启动进度与日志可观测性三条陷阱沉淀"
```

---

## 执行顺序与依赖

Task 1 → 2 → 3 → 4（纯新增模块，逐个 TDD）→ 5（docker_setup 改造，依赖 1）→ 6（适配器层，依赖 3 的回调类型）→ 7（start_profile 编排，依赖 3/4/5/6）→ 8（CLI，依赖 7）→ 9（Web API，依赖 3/7）→ 10（前端，依赖 9 的端点契约）→ 11（回归 + 沉淀）。Task 8 与 Task 9/10 无相互依赖，可并行。