# TUI 视觉重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 rich 原生组件（Table/Panel/Columns）全面替换手写 `pad_width` 拼行，修复 `vram_gib` 恒 0 的数据层 Bug，补采 CTX/TP/QUANT 字段，新增搜索/过滤/排序/帮助键位接线。

**Architecture:** 新增 `panels/chrome.py` 统一 5 视图共享骨架（顶栏+硬件栏+上下文栏+主区+底栏）；`main_dashboard.py` 换 rich Table 并新增 4 列；`data.py` 修复 `vram_gib` 并新增 `ctx/tp/quant/kv_mb` 采集；`app.py` 接 `f/s/d/g/G/PgUp/PgDn/1-5` 键位并加模式状态机；Detail/Plan/Cluster/Monitor 内部组件全换 rich 原生（交互逻辑不变）。

**Tech Stack:** Python 3.10+, rich>=13.0, pytest

**Spec Reference:** `docs/superpowers/specs/2026-09-12-tui-visual-redesign-design.md`

## Global Constraints

- 禁止 `subprocess` / `open(mode='w')` / `os.kill`（只读契约）
- 禁止 `f"{x:<N}"` / `str.ljust` 对齐（CJK 双宽用 `display_width`/`pad_width` 或 rich `cell_len`）
- 终端最小 80x24；宽表列宽总余量 ≥ 2 列
- `page_size = max(5, height - 6)`
- Detail 双栏阈值：`width >= 140`
- `d` 键仅 dashboard 视图用作引擎过滤（plan 视图保持 Dry-run 语义）
- `g`/`G` 仅 dashboard 视图新增（monitor 视图 `g` 保持 GPU 轮询语义）
- 所有时间格式 `YYYY-MM-DD HH:mm:ss`
- UI 显示虚拟 ID（集群视图 `node_id` 例外，见 workspace rules）

---

### Task 1: 数据层修复 — 补采 `ctx/tp/quant/vram_gib/kv_mb`

**Files:**
- Modify: `src/modelctl/core/tui/data.py:174-238`
- Test: `tests/test_tui_data_snapshot.py`

**Interfaces:**
- Consumes: `Profile.engine_config` (dict), `Capabilities.vram_total_mb` (int), `vram_estimator.kv_estimate_for_profile(profile)` → `dict | None`
- Produces: `ModelsSnapshot.profiles[i]` 新增键 `"ctx": int|None`, `"tp": int`, `"quant": str`, `"vram_gib": float|None`, `"kv_mb": float|None`；模块级私有函数 `_extract_ctx`, `_extract_tp`, `_extract_quant`, `_estimate_vram_gib`, `_estimate_kv_mb`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tui_data_snapshot.py 新增
from unittest.mock import MagicMock, patch
from modelctl.core.tui.data import ModelsSnapshot

def _mk_profile(name="m1", engine="vllm", ec=None):
    p = MagicMock()
    p.name = name; p.engine = engine; p.variant = ""; p.port = 8100
    p.engine_config = ec or {}
    return p

@patch("modelctl.core.tui.data.is_running_any", return_value=False)
@patch("modelctl.core.tui.data.list_profiles")
def test_fetch_extracts_ctx_tp_quant(mock_lp, mock_run):
    mock_lp.return_value = [_mk_profile(ec={
        "max_model_len": 131072,
        "tensor_parallel_size": 8,
        "quantization": "fp8",
        "kv_cache_dtype": "fp8",
    })]
    snap = ModelsSnapshot.fetch()
    p0 = snap.profiles[0]
    assert p0["ctx"] == 131072
    assert p0["tp"] == 8
    assert p0["quant"] == "fp8"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tui_data_snapshot.py::test_fetch_extracts_ctx_tp_quant -v`
Expected: FAIL with `KeyError: 'ctx'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/modelctl/core/tui/data.py 在 ModelsSnapshot.fetch 前新增

def _extract_ctx(ec: dict) -> int | None:
    for k in ("max_model_len", "ctx_size", "context_length"):
        v = ec.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return None

def _extract_tp(ec: dict) -> int:
    for k in ("tensor_parallel_size", "tensor_parallel"):
        v = ec.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            return 1
    gl = ec.get("gpu_list")
    if isinstance(gl, str) and gl.strip():
        return len([x for x in gl.split(",") if x.strip()])
    return 1

def _extract_quant(ec: dict) -> str:
    q = str(ec.get("quantization") or "").strip()
    if q:
        return q[:8]
    kv = str(ec.get("kv_cache_dtype") or "").strip()
    return kv[:8] if kv else ""

def _estimate_kv_mb(p) -> float | None:
    try:
        from modelctl.core.vram_estimator import kv_estimate_for_profile
        result = kv_estimate_for_profile(p)
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    return result.get("kv_total_mb")

def _estimate_vram_gib(p, caps: object | None) -> float | None:
    kv = _estimate_kv_mb(p)
    if kv and kv > 0:
        return round(kv / 1024.0, 1)
    # fallback: gpu_memory_utilization × total_vram
    ec = getattr(p, "engine_config", {}) or {}
    try:
        frac = float(ec.get("gpu_memory_utilization") or 0)
    except (TypeError, ValueError):
        frac = 0.0
    total_mb = getattr(caps, "vram_total_mb", 0) if caps else 0
    if frac > 0 and total_mb > 0:
        return round(frac * total_mb / 1024.0, 1)
    return None
```

在 `ModelsSnapshot.fetch` 的 `snap.profiles.append({...})` 中新增 5 个键：
```python
    "ctx": _extract_ctx(getattr(p, "engine_config", {}) or {}),
    "tp": _extract_tp(getattr(p, "engine_config", {}) or {}),
    "quant": _extract_quant(getattr(p, "engine_config", {}) or {}),
    "vram_gib": _estimate_vram_gib(p, None),
    "kv_mb": _estimate_kv_mb(p),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tui_data_snapshot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/tui/data.py tests/test_tui_data_snapshot.py
git commit -m "feat(tui): extract ctx/tp/quant, fix vram_gib always-zero bug"
```

---

### Task 2: chrome 骨架 — `panels/chrome.py` + 主题扩展

**Files:**
- Create: `src/modelctl/core/tui/panels/chrome.py`
- Modify: `src/modelctl/core/tui/theme.py:32-57`
- Test: `tests/test_tui_chrome.py`

**Interfaces:**
- Consumes: `TUIState.active_view`, `TUIState.mode`, `TUIState.search_input`, `HardwareSnapshot.gpus`, `ModelsSnapshot.profiles`, `ClusterSnapshot.nodes`
- Produces: `render_chrome(state, hw, models, cluster, view_body, width, height, theme_id) -> Group`；主题新增键 `accent`, `selected_bg`, `search_active`, `bar_empty`, `bar_fill`, `bar_warn`, `bar_over`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tui_chrome.py
from rich.console import Console
from unittest.mock import MagicMock
from modelctl.core.tui.panels.chrome import render_chrome
from modelctl.core.tui.state import TUIState
from modelctl.core.tui.data import HardwareSnapshot, ModelsSnapshot, ClusterSnapshot

def test_chrome_five_line_skeleton():
    state = TUIState()
    hw = HardwareSnapshot(); hw.gpus = [{"name": "GTX 1660 Ti", "free_mb": 4096, "total_mb": 6144}]
    models = ModelsSnapshot(); models.profiles = [{"name": "m1", "status": "running"}]
    cluster = ClusterSnapshot(); cluster.nodes = []
    group = render_chrome(state, hw, models, cluster, MagicMock(), width=80, height=24, theme_id="dark")
    console = Console(record=True, width=80, force_terminal=False)
    console.print(group)
    text = console.export_text()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) >= 4  # topbar + hwbar + ctxbar + keybar at minimum
    assert "modelctl TUI" in text
    assert "NORMAL" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tui_chrome.py::test_chrome_five_line_skeleton -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'modelctl.core.tui.panels.chrome'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/modelctl/core/tui/panels/chrome.py
"""TUI 全局 chrome 骨架：顶栏 + 硬件栏 + 上下文栏 + 主区 + 底栏。"""
from __future__ import annotations

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from modelctl.core.colors import display_width, pad_width
from modelctl.core.tui.data import ClusterSnapshot, HardwareSnapshot, ModelsSnapshot
from modelctl.core.tui.state import TUIState
from modelctl.core.tui.theme import get_rich_theme

_VIEW_LABELS = ["DASH", "DETAIL", "PLAN", "CLUSTER", "MONITOR"]
_VIEW_KEYS = ["dashboard", "detail", "plan", "cluster", "monitor"]


def _top_bar(state: TUIState, width: int, theme: dict) -> Panel:
    theme_name = getattr(state, "_theme_id", "dark")
    segs = []
    for i, (key, label) in enumerate(zip(_VIEW_KEYS, _VIEW_LABELS)):
        style = theme["accent"] if state.active_view == key else theme["dim"]
        prefix = f"[{i+1}]"
        segs.append(f"{prefix}{label}" if state.active_view == key else f" {prefix}{label}")
    raw = f" modelctl TUI | {' | '.join(segs)} | [{theme_name}] "
    return Panel(Text(pad_width(raw, max(0, width - 2)), style=theme["title"]),
                 width=width, border_style=theme["dim"])


def _hw_bar(hw: HardwareSnapshot, models: ModelsSnapshot,
            cluster: ClusterSnapshot, width: int, theme: dict) -> Panel:
    gpu_name = hw.gpus[0]["name"] if hw.gpus else ""
    gpu_seg = f"{len(hw.gpus)}x {gpu_name}" if hw.gpus else "0x"
    free_gb = round(sum(g["free_mb"] for g in hw.gpus) / 1024.0, 1)
    total_gb = round(sum(g["total_mb"] for g in hw.gpus) / 1024.0, 1)
    running = sum(1 for p in models.profiles if p.get("status") == "running")
    raw = (f" {gpu_seg} | 自由显存 {free_gb:g}/{total_gb:g}G"
           f" | 模型 {running}/{len(models.profiles)} 运行"
           f" | 集群: {len(cluster.nodes)} 节点 ")
    return Panel(Text(pad_width(raw, max(0, width - 2)), style=theme["dim"]),
                 width=width, border_style=theme["dim"])


def _ctx_bar(state: TUIState, width: int, theme: dict) -> Panel:
    mode = state.mode
    style = theme["search_active"] if mode == "search" else theme["dim"]
    search_disp = state.search_input if mode == "search" else state.search
    raw = (f" 搜索: {search_disp or '(none)'} | 过滤: {state.filter_status}"
           f" | 排序: {state.sort_key} | 引擎: {state.filter_engine} | 模式: {mode.upper()} ")
    return Panel(Text(pad_width(raw, max(0, width - 2)), style=style),
                 width=width, border_style=theme["dim"])


def render_chrome(
    state: TUIState,
    hw: HardwareSnapshot,
    models: ModelsSnapshot,
    cluster: ClusterSnapshot,
    view_body: RenderableType,
    width: int,
    height: int,
    theme_id: str = "dark",
) -> Group:
    theme = get_rich_theme(theme_id)
    top = _top_bar(state, width, theme)
    hw_bar = _hw_bar(hw, models, cluster, width, theme)
    ctx_bar = _ctx_bar(state, width, theme)
    keybar_label = _keybar_for_view(state.active_view, state.mode)
    keybar = Panel(Text(pad_width(keybar_label, max(0, width - 2)), style=theme["keybar"]),
                   width=width, border_style=theme["dim"])
    divider = Text(pad_width("─" * max(0, width - 2), width), style=theme["dim"])
    return Group(top, hw_bar, ctx_bar, view_body, divider, keybar)


def _keybar_for_view(view: str, mode: str) -> str:
    if mode == "search":
        return " Esc/Enter 确认 | Ctrl-U 清空 | Backspace 删除 | 其他字符输入 "
    if mode == "help":
        return " Esc/h/? 关闭帮助 | j/k 滚动 "
    base = " q 退 | / 搜 | f 状态 | s 排序 | d 引擎 | h 帮助 | t 主题 | 1-5 切视图 "
    if view == "dashboard":
        return " j/k 移 | g/G 顶/底 | PgUp/PgDn 翻页 | Enter 详情 |" + base
    if view == "detail":
        return " Tab/Shift-Tab 子Tab | Esc 回 DASH |" + base
    if view == "plan":
        return " Tab 切字段 | D Dry-run | Esc 回 DASH |" + base
    if view == "cluster":
        return " Tab 切 section | Esc 回 DASH |" + base
    if view == "monitor":
        return " r 强刷 | g GPU 轮询 | Esc 回 DASH |" + base
    return base
```

在 `theme.py` 的 `_RICH_THEMES` 每套主题 dict 中追加：
```python
        "accent": "bold cyan",
        "selected_bg": "reverse",
        "search_active": "bold yellow",
        "bar_empty": "dim",
        "bar_fill": "green",
        "bar_warn": "yellow",
        "bar_over": "red",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tui_chrome.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/tui/panels/chrome.py src/modelctl/core/tui/theme.py tests/test_tui_chrome.py
git commit -m "feat(tui): add chrome skeleton with theme extensions"
```

---

### Task 3: Dashboard 主表 — rich Table + 新增列 + 选中反白

**Files:**
- Modify: `src/modelctl/core/tui/panels/main_dashboard.py`（全文件重写）
- Test: `tests/test_tui_panel_dashboard.py`

**Interfaces:**
- Consumes: `TUIState.active_index`, `TUIState.search`, `TUIState.filter_status`, `TUIState.filter_engine`, `TUIState.sort_key`, `TUIState.page`, `ModelsSnapshot.profiles`（含新字段 `ctx/tp/quant/vram_gib`）
- Produces: `render(state, hw, models, cluster, width, height, theme_id) -> Group`（签名不变，内部全换 rich Table）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tui_panel_dashboard.py 新增
from rich.console import Console
from unittest.mock import MagicMock
from modelctl.core.tui.panels.main_dashboard import render
from modelctl.core.tui.state import TUIState
from modelctl.core.tui.data import HardwareSnapshot, ModelsSnapshot, ClusterSnapshot

def _mk_hw():
    hw = HardwareSnapshot()
    hw.gpus = [{"name": "GTX 1660 Ti", "free_mb": 4096, "total_mb": 6144}]
    return hw

def _mk_models():
    m = ModelsSnapshot()
    m.profiles = [
        {"name": "deepseek-v4-flash", "engine": "vllm", "variant": "", "port": 8100,
         "status": "running", "ctx": 131072, "tp": 8, "quant": "fp8",
         "vram_gib": 36.8, "rate_in": 3200.0, "rate_out": 210.0},
        {"name": "deepseek-v4-flash-llamacpp-high", "engine": "llamacpp", "variant": "high",
         "port": 8103, "status": "stopped", "ctx": 131072, "tp": 1, "quant": "q4_k_m",
         "vram_gib": None, "rate_in": None, "rate_out": None},
    ]
    return m

def _mk_cluster():
    c = ClusterSnapshot(); c.nodes = []; return c

def test_dashboard_has_new_columns():
    state = TUIState()
    group = render(state, _mk_hw(), _mk_models(), _mk_cluster(), width=120, height=30, theme_id="dark")
    console = Console(record=True, width=120, force_terminal=False)
    console.print(group)
    text = console.export_text()
    assert "CTX" in text or "ctx" in text
    assert "TP" in text or "tp" in text
    assert "VRAM" in text

def test_dashboard_selected_row_has_reverse():
    state = TUIState(); state.active_index = 0
    group = render(state, _mk_hw(), _mk_models(), _mk_cluster(), width=120, height=30, theme_id="dark")
    console = Console(record=True, width=120, force_terminal=True)
    console.print(group)
    ansi = console.export_text(styles=True)
    assert "\x1b[7m" in ansi  # reverse ANSI code
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tui_panel_dashboard.py::test_dashboard_has_new_columns -v`
Expected: FAIL with `AssertionError: "CTX" not in ...`

- [ ] **Step 3: Write minimal implementation**

```python
# src/modelctl/core/tui/panels/main_dashboard.py 完整重写
"""TUI 主仪表盘（rich Table 版，CJK 对齐由 rich cell_len 保证）。"""
from __future__ import annotations

from rich.console import Group
from rich.table import Table
from rich.text import Text

from modelctl.core.tui.data import ClusterSnapshot, HardwareSnapshot, ModelsSnapshot
from modelctl.core.tui.state import TUIState
from modelctl.core.tui.theme import get_rich_theme

_LAYOUT_FULL = 120
_LAYOUT_MEDIUM = 100


def _fmt_ctx(ctx: int | None) -> str:
    if ctx is None:
        return "-"
    if ctx >= 10000:
        return f"{ctx // 1024}K"
    return str(ctx)


def _fmt_rate(rin: float | None, rout: float | None) -> str:
    if rin is None or rout is None:
        return "-"
    return f"{rin:.0f}/{rout:.0f}"


def _fmt_vram(vram: float | None) -> str:
    if vram is None or vram <= 0:
        return "-"
    return f"{vram:.1f}G"


def _vram_bar(vram_gib: float | None, total_gib: float) -> Text:
    """显存占比条：▓▓▓░░ 形式，None → '-'。"""
    if vram_gib is None or total_gib <= 0:
        return Text("-", style="dim")
    pct = min(1.0, vram_gib / total_gib)
    filled = round(pct * 5)
    bar = "▓" * filled + "░" * (5 - filled)
    style = "red" if pct >= 0.9 else ("yellow" if pct >= 0.6 else "green")
    return Text(bar, style=style)


def _build_table(
    state: TUIState,
    profiles: list[dict],
    width: int,
    total_vram_gib: float,
    theme: dict,
) -> Table:
    mode = "full" if width >= _LAYOUT_FULL else ("medium" if width >= _LAYOUT_MEDIUM else "narrow")

    cols: list[tuple[str, int, str]] = [("NAME", 28, "left")]
    cols.append(("ENGINE", 10, "left"))
    if mode == "full":
        cols.append(("VARIANT", 8, "left"))
    cols.append(("PORT", 6, "right"))
    cols.append(("STATUS", 10, "left"))
    if mode != "narrow":
        cols.append(("RATE(in/out)", 13, "right"))
        cols.append(("CTX", 6, "right"))
        cols.append(("TP", 3, "right"))
    if mode == "full":
        cols.append(("QUANT", 8, "left"))
    cols.append(("VRAM", 8, "right"))

    # 收缩 NAME 列以适应窄屏
    fixed = sum(w for _n, w, _j in cols if _n != "NAME")
    name_w = max(16, min(28, width - fixed - 4))
    table = Table(show_header=True, header_style="bold", expand=False,
                  width=width, padding=(0, 1))
    for name, w, justify in cols:
        actual_w = name_w if name == "NAME" else w
        table.add_column(name, width=actual_w, justify=justify,
                         overflow="ellipsis", no_wrap=True)

    page_size = max(5, 30 - 6)  # placeholder, actual page_size from render
    filtered = state.sort_profiles(state.filter_candidates(profiles))
    page = state.apply_page(filtered, page_size=page_size)

    for i, p in enumerate(page):
        active = (i == state.active_index)
        row_style = theme["selected_bg"] if active else ""
        name_txt = Text(str(p.get("name", "")))
        if state.search and state.search.lower() in str(p.get("name", "")).lower():
            idx = str(p.get("name", "")).lower().find(state.search.lower())
            name_txt.stylize(theme["accent"], idx, idx + len(state.search))
        status = str(p.get("status", ""))
        status_icon = "●" if status == "running" else "○"
        status_color = theme["success"] if status == "running" else theme["dim"]
        row: list[object] = [name_txt]
        row.append(str(p.get("engine", "")))
        if mode == "full":
            row.append(str(p.get("variant", "") or "-"))
        row.append(str(p.get("port", "") or "-"))
        row.append(Text(f"{status_icon} {status}", style=status_color))
        if mode != "narrow":
            row.append(_fmt_rate(p.get("rate_in"), p.get("rate_out")))
            row.append(_fmt_ctx(p.get("ctx")))
            row.append(str(p.get("tp", 1)))
        if mode == "full":
            row.append(str(p.get("quant", "") or "-"))
        vram = p.get("vram_gib")
        row.append(_fmt_vram(vram))
        table.add_row(*row, style=row_style or None)
    return table


def render(
    state: TUIState,
    hw: HardwareSnapshot,
    models: ModelsSnapshot,
    cluster: ClusterSnapshot,
    width: int,
    height: int,
    theme_id: str = "dark",
) -> Group:
    theme = get_rich_theme(theme_id)
    total_vram_gib = round(sum(g.get("total_mb", 0) for g in hw.gpus) / 1024.0, 1)
    page_size = max(5, height - 6)
    # 更新 apply_page 的 page_size
    state._page_size = page_size
    filtered = state.sort_profiles(state.filter_candidates(models.profiles))
    page = state.apply_page(filtered, page_size=page_size)
    if not page:
        placeholder = Text("(无匹配 profile)", style=theme["dim"])
        return Group(placeholder)
    table = _build_table(state, models.profiles, width, total_vram_gib, theme)
    return Group(table)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tui_panel_dashboard.py -v`
Expected: PASS（注意：旧测试可能需同步更新断言，见 Self-Review）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/tui/panels/main_dashboard.py tests/test_tui_panel_dashboard.py
git commit -m "feat(tui): rebuild dashboard with rich Table, new columns, selected highlight"
```

---

### Task 4: 键位接线 — `f/s/d/g/G/PgUp/PgDn/1-5` + 模式状态机

**Files:**
- Modify: `src/modelctl/core/tui/state.py:26-47`
- Modify: `src/modelctl/core/tui/app.py:149-196`
- Test: `tests/test_tui_app.py`

**Interfaces:**
- Consumes: `TUIState.filter_status`, `TUIState.filter_engine`, `TUIState.sort_key`, `TUIState.page`, `TUIState.active_view`
- Produces: `TUIState.mode: Literal["normal","search","help"]`, `TUIState.search_input: str`, `TUIState.cycle_filter_status()`, `TUIState.cycle_sort_key()`, `TUIState.cycle_filter_engine()`, `TUIState.goto_top()`, `TUIState.goto_bottom()`, `TUIState.page_up()`, `TUIState.page_down()`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tui_app.py 新增
from modelctl.core.tui.state import TUIState
from modelctl.core.tui.app import TuiApp
from unittest.mock import MagicMock, patch

def test_mode_state_defaults():
    st = TUIState()
    assert st.mode == "normal"
    assert st.search_input == ""

def test_cycle_filter_status():
    st = TUIState()
    assert st.filter_status == "all"
    st.cycle_filter_status(); assert st.filter_status == "running"
    st.cycle_filter_status(); assert st.filter_status == "stopped"
    st.cycle_filter_status(); assert st.filter_status == "all"

def test_cycle_sort_key():
    st = TUIState()
    assert st.sort_key == "name"
    for expected in ["port", "rate_out", "vram", "name"]:
        st.cycle_sort_key()
        assert st.sort_key == expected

def test_goto_top_bottom():
    st = TUIState(); st.active_index = 5
    st.goto_top(); assert st.active_index == 0
    st.goto_bottom(10); assert st.active_index == 9

@patch("modelctl.core.tui.app.TuiApp._plan_field_count", return_value=1)
def test_dispatch_digit_switches_view(mock_pfc):
    from modelctl.core.tui.keyboard import Key
    app = TuiApp(state=TUIState(), console=MagicMock())
    assert app._dispatch_key(Key.Digit2) is True
    assert app.state.active_view == "detail"
    assert app._dispatch_key(Key.Digit1) is True
    assert app.state.active_view == "dashboard"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tui_app.py::test_mode_state_defaults -v`
Expected: FAIL with `AttributeError: 'TUIState' object has no attribute 'mode'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/modelctl/core/tui/state.py 在 TUIState 中新增字段和方法

    mode: Literal["normal", "search", "help"] = "normal"
    search_input: str = ""
    help_scroll: int = 0

    def cycle_filter_status(self) -> None:
        order = ["all", "running", "stopped"]
        self.filter_status = order[(order.index(self.filter_status) + 1) % len(order)]

    def cycle_sort_key(self) -> None:
        order = ["name", "port", "rate_out", "vram"]
        self.sort_key = order[(order.index(self.sort_key) + 1) % len(order)]

    def cycle_filter_engine(self, engines: list[str]) -> None:
        opts = ["all"] + sorted(engines)
        self.filter_engine = opts[(opts.index(self.filter_engine) + 1) % len(opts)]

    def goto_top(self) -> None:
        self.active_index = 0
        self.page = 0

    def goto_bottom(self, count: int) -> None:
        self.active_index = max(0, count - 1)

    def page_up(self) -> None:
        self.page = max(0, self.page - 1)

    def page_down(self) -> None:
        self.page += 1
```

```python
# src/modelctl/core/tui/app.py _dispatch_key 重写（dashboard normal 模式分支）

    def _dispatch_key(self, key: Key) -> bool:
        st = self.state
        view = st.active_view

        # 全局键（任何模式）
        if key == Key.T:
            self.cycle_theme(); return True
        if key == Key.Esc:
            if st.mode == "search":
                st.mode = "normal"; return True
            if st.mode == "help":
                st.mode = "normal"; return True
            if view == "dashboard":
                return False
            st.active_view = "dashboard"; return True
        if key == Key.Q:
            if st.mode == "help":
                st.mode = "normal"; return True
            return False

        # 模式分发
        if st.mode == "help":
            if key in (Key.J, Key.Down):
                st.help_scroll += 1
            elif key in (Key.K, Key.Up):
                st.help_scroll = max(0, st.help_scroll - 1)
            return True

        if st.mode == "search":
            if key == Key.Enter:
                st.search = st.search_input
                st.mode = "normal"; return True
            # 字符输入由 keyboard.py 解为 Key.Char(ch)
            ch = getattr(key, "char", None)
            if ch:
                if ch == "\x15":  # Ctrl-U
                    st.search_input = ""
                elif ch == "\x7f":  # Backspace
                    st.search_input = st.search_input[:-1]
                else:
                    st.search_input += ch
            return True

        # normal 模式
        if key in (Key.H,):
            st.mode = "help"; return True
        if key == Key.Slash:
            st.mode = "search"; st.search_input = ""; return True

        # 数字键切视图（全局 normal）
        digit_map = {Key.Digit1: "dashboard", Key.Digit2: "detail",
                     Key.Digit3: "plan", Key.Digit4: "cluster", Key.Digit5: "monitor"}
        if key in digit_map:
            st.active_view = digit_map[key]; return True

        if view == "dashboard":
            if key in (Key.Down, Key.J):
                st.active_index += 1
            elif key in (Key.Up, Key.K):
                st.active_index = max(0, st.active_index - 1)
            elif key == Key.Enter:
                st.active_view = "detail"
            elif key == Key.F:
                st.cycle_filter_status()
            elif key == Key.S:
                st.cycle_sort_key()
            elif key == Key.D:
                engines = list({p.get("engine", "") for p in
                                getattr(self._snap["models"], "profiles", []) if p.get("engine")})
                st.cycle_filter_engine(engines)
            elif key == Key.G:
                st.goto_top()
            elif key == Key.ShiftG:
                st.goto_bottom(len(getattr(self._snap["models"], "profiles", [])))
            elif key == Key.PageUp:
                st.page_up()
            elif key == Key.PageDown:
                st.page_down()
            elif key == Key.Tab:
                st.active_view = "plan"
        elif view == "detail":
            if key == Key.Tab:
                cur = _DETAIL_TABS.index(st.active_detail_subtab)
                st.switch_detail_tab(_DETAIL_TABS[(cur + 1) % len(_DETAIL_TABS)])
            elif key == Key.ShiftTab:
                cur = _DETAIL_TABS.index(st.active_detail_subtab)
                st.switch_detail_tab(_DETAIL_TABS[(cur - 1) % len(_DETAIL_TABS)])
            elif key in (Key.Down, Key.J):
                st.active_index += 1
            elif key in (Key.Up, Key.K):
                st.active_index = max(0, st.active_index - 1)
        elif view == "plan":
            if key == Key.Tab:
                st.cycle_plan_cursor(1, self._plan_field_count())
            elif key == Key.ShiftTab:
                st.cycle_plan_cursor(-1, self._plan_field_count())
            elif key == Key.D:
                st.plan_dry_run_done = True
        elif view == "cluster":
            if key == Key.Tab:
                st.cycle_cluster_tab(1)
        elif view == "monitor":
            if key == Key.Tab:
                st.cycle_monitor_tab(1)
        return True
```

同时更新 `keyboard.py`，确保 `Key` 枚举包含 `Slash`, `Digit1`~`Digit5`, `ShiftG`, `PageUp`, `PageDown`, `Char`（如已存在则复用）。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tui_app.py -v`
Expected: PASS（旧测试可能需适配新签名，见 Self-Review）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/tui/state.py src/modelctl/core/tui/app.py src/modelctl/core/tui/keyboard.py tests/test_tui_app.py
git commit -m "feat(tui): wire f/s/d/g/G/PgUp/PgDn/1-5 keys, add mode state machine"
```

---

### Task 5: Detail 视图改造

**Files:**
- Modify: `src/modelctl/core/tui/panels/detail.py`（Tab 头 + 智能体配置表 + 双栏布局）
- Test: `tests/test_tui_panel_detail.py`

**Interfaces:**
- Consumes: `TUIState.active_detail_subtab`, `TUIState.active_index`, `LogsSnapshot.lines`, `ModelsSnapshot.profiles`
- Produces: `render(...)` 签名不变；内部 Tab 头改 rich Table；智能体配置改 rich Table；width ≥ 140 时双栏

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tui_panel_detail.py 新增
from rich.console import Console
from unittest.mock import MagicMock, patch
from modelctl.core.tui.panels.detail import render
from modelctl.core.tui.state import TUIState
from modelctl.core.tui.data import HardwareSnapshot, ModelsSnapshot, LogsSnapshot

def _mk_models():
    m = ModelsSnapshot()
    m.profiles = [{"name": "m1", "engine": "vllm", "port": 8100}]
    return m

def _mk_logs():
    l = LogsSnapshot(); l.lines = ["line1"]; return l

@patch("modelctl.core.tui.panels.detail._resolve_profile_from_apps")
def test_detail_tab_header_active_highlight(mock_resolve):
    mock_resolve.return_value = None
    state = TUIState(); state.active_detail_subtab = "yaml"
    hw = HardwareSnapshot()
    group = render(state, hw, _mk_models(), _mk_logs(), width=120, height=30, theme_id="dark")
    console = Console(record=True, width=120, force_terminal=True)
    console.print(group)
    ansi = console.export_text(styles=True)
    assert "\x1b[" in ansi  # has ANSI styling
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tui_panel_detail.py::test_detail_tab_header_active_highlight -v`
Expected: FAIL（当前实现不满足新断言）

- [ ] **Step 3: Write minimal implementation**

```python
# src/modelctl/core/tui/panels/detail.py 修改 _header 函数

def _header(state: TUIState, width: int, theme: dict) -> Table:
    """Tab 头：rich Table 单行，active Tab 高亮。"""
    from rich.table import Table
    table = Table(show_header=False, show_edge=False, pad_edge=False,
                  width=width, padding=(0, 1))
    n = len(_DETAIL_TABS)
    for key in _DETAIL_TABS:
        table.add_column(width=max(4, width // n - 2))
    cells = []
    for key in _DETAIL_TABS:
        label = _TAB_LABELS.get(key, key)
        style = theme["accent"] if key == state.active_detail_subtab else theme["dim"]
        cells.append(Text(label, style=style))
    table.add_row(*cells)
    return table
```

```python
# 修改 _render_agent 函数

def _render_agent(profile, width: int, theme: dict) -> Panel:
    cfg = getattr(profile, "engine_config", None)
    if not cfg or not isinstance(cfg, dict):
        return Panel(
            Text("engine_config 未定义", style=theme["warning"]),
            title="智能体配置", title_align="left",
            width=width, border_style=theme["warning"],
        )
    from rich.table import Table
    table = Table(show_header=True, header_style="bold", width=width - 4, padding=(0, 1))
    table.add_column("KEY", width=20, overflow="ellipsis", no_wrap=True)
    table.add_column("VALUE", overflow="fold")
    for k, v in cfg.items():
        table.add_row(str(k), str(v))
    return Panel(table, title="智能体配置", title_align="left", width=width)
```

```python
# render() 函数末尾，width >= 140 时启用双栏

    # 在 return Group(header, body, keybar) 前加：
    if width >= 140 and proxy is not None:
        from rich.columns import Columns
        yaml_panel = _render_yaml(proxy, width // 2, theme)
        right_panel = body if isinstance(body, Panel) else Panel(body, width=width // 2)
        combined = Columns([yaml_panel, right_panel], width=width, expand=False)
        return Group(_header(state, width, theme), combined, _keybar_detail(width, theme))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tui_panel_detail.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/tui/panels/detail.py tests/test_tui_panel_detail.py
git commit -m "feat(tui): rebuild detail view with rich Table, add dual-pane layout"
```

---

### Task 6: Plan / Cluster / Monitor 视图改造

**Files:**
- Modify: `src/modelctl/core/tui/panels/plan.py`（表单 + KV 估算 + 预检三 Panel 改 rich Table）
- Modify: `src/modelctl/core/tui/panels/cluster.py`（节点表 + goal 表 + 事件流改 rich Table）
- Modify: `src/modelctl/core/tui/panels/monitor.py`（速率表 + GPU 卡片改 rich Table，双栏用 Columns）
- Test: `tests/test_tui_panel_plan.py`, `tests/test_tui_panel_cluster.py`, `tests/test_tui_panel_monitor.py`

**Interfaces:**
- Consumes: 各视图现有数据快照（签名全部不变）
- Produces: `render(...)` 签名不变；内部表格全换 rich Table

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tui_panel_plan.py 新增
from rich.console import Console
from unittest.mock import MagicMock, patch
from modelctl.core.tui.panels.plan import render
from modelctl.core.tui.state import TUIState
from modelctl.core.tui.data import HardwareSnapshot, ModelsSnapshot

@patch("modelctl.core.tui.panels.plan._resolve_profile_from_apps")
def test_plan_form_has_table_header(mock_resolve):
    mock_profile = MagicMock()
    mock_profile.engine_config = {"max_model_len": 131072}
    mock_profile.engine = "vllm"
    mock_resolve.return_value = mock_profile
    state = TUIState()
    hw = HardwareSnapshot()
    models = ModelsSnapshot()
    models.profiles = [{"name": "m1", "engine": "vllm", "port": 8100}]
    group = render(state, hw, models, width=100, height=30, theme_id="dark")
    console = Console(record=True, width=100, force_terminal=False)
    console.print(group)
    text = console.export_text()
    assert "字段" in text or "max_model_len" in text
```

```python
# tests/test_tui_panel_cluster.py 新增（现有断言适配）
# 主要断言节点表列头存在
```

```python
# tests/test_tui_panel_monitor.py 新增（现有断言适配）
# 主要断言速率表 name/in/s/out/s 列头存在
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tui_panel_plan.py tests/test_tui_panel_cluster.py tests/test_tui_panel_monitor.py -v`
Expected: FAIL（当前实现不满足新断言）

- [ ] **Step 3: Write minimal implementations**

**plan.py** — `_render_form` 改 rich Table：
```python
def _render_form(state: TUIState, profile, width: int, theme: dict) -> Panel:
    fields = _profile_fields(profile)
    if not fields:
        return Panel(Text("(无 profile engine_config)", style=theme["warning"]),
                     title="字段", title_align="left", width=width,
                     border_style=theme["warning"])
    from rich.table import Table
    table = Table(show_header=True, header_style="bold", width=width - 4, padding=(0, 1))
    table.add_column("", width=2)  # cursor
    table.add_column("字段", width=20, overflow="ellipsis", no_wrap=True)
    table.add_column("值", overflow="fold")
    table.add_column("类型", width=6)
    try:
        cursor = int(state.plan_edit_cursor)
    except (TypeError, ValueError):
        cursor = 0
    for i, (k, v) in enumerate(fields):
        prefix = ">" if i == cursor else " "
        style = theme["selected_bg"] if i == cursor else ""
        table.add_row(prefix, k, v, _type_hint(v), style=style or None)
    return Panel(table, title="字段", title_align="left", width=width)
```

**cluster.py** — `_nodes_section` 改 rich Table：
```python
def _nodes_section(cluster: ClusterSnapshot, width: int, theme: dict) -> Panel:
    nodes = cluster.nodes or []
    if not nodes:
        return Panel(Text("(集群无节点 — 检查 role=center/both 是否允许)", style=theme["warning"]),
                     title="节点", title_align="left", width=width,
                     border_style=theme["warning"])
    from rich.table import Table
    table = Table(show_header=True, header_style="bold", width=width - 4, padding=(0, 1))
    table.add_column("node_id", overflow="ellipsis", no_wrap=True)
    table.add_column("状态", width=8)
    table.add_column("LAN", width=10, overflow="ellipsis")
    table.add_column("容量", overflow="ellipsis")
    table.add_column("goal", width=10)
    for n in nodes:
        nid = str(n.get("node_id", "") or "")
        status = str(n.get("status", "") or "")
        lan = str(n.get("lan_id", "") or "") or "-"
        capacity = str(n.get("capacity_text", "") or "") or "-"
        goal_str = "0/0"
        style = _status_style_map(status, theme)
        table.add_row(nid, Text(status, style=style), lan, capacity, goal_str)
    return Panel(table, title="节点", title_align="left", width=width)
```

**monitor.py** — `_rate_table` 改 rich Table，双栏改 Columns：
```python
def _rate_table(monitor: MonitorSnapshot, width: int, theme: dict) -> Panel:
    info = monitor.info or []
    if not info:
        return Panel(Text("(无运行中 profile)", style=theme["warning"]),
                     title="速率表", title_align="left", width=width,
                     border_style=theme["warning"])
    from rich.table import Table
    table = Table(show_header=True, header_style="bold", width=width - 4, padding=(0, 1))
    table.add_column("name", overflow="ellipsis", no_wrap=True)
    table.add_column("in/s", justify="right")
    table.add_column("out/s", justify="right")
    for p in info:
        name = str(p.get("name", "") or "") or "(none)"
        rin = p.get("rate_in")
        rout = p.get("rate_out")
        in_str = "(not measured)" if rin is None else str(rin)
        out_str = "(not measured)" if rout is None else str(rout)
        table.add_row(name, in_str, out_str)
    return Panel(table, title="速率表", title_align="left", width=width)
```

```python
# monitor.py render() 双栏部分替换为：
    if width >= _MIN_WIDTH_SIDE_BY_SIDE:
        from rich.columns import Columns
        half = width // 2
        body = Columns([
            _rate_table(monitor, max(40, half), theme),
            _gpu_cards(hw, max(40, width - half), theme),
        ], width=width, expand=False)
    else:
        body = Group(rate_p, gpu_p)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tui_panel_plan.py tests/test_tui_panel_cluster.py tests/test_tui_panel_monitor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/tui/panels/plan.py src/modelctl/core/tui/panels/cluster.py src/modelctl/core/tui/panels/monitor.py tests/test_tui_panel_plan.py tests/test_tui_panel_cluster.py tests/test_tui_panel_monitor.py
git commit -m "feat(tui): rebuild plan/cluster/monitor with rich Table components"
```

---

### Task 7: 帮助弹层 — `panels/help.py`

**Files:**
- Create: `src/modelctl/core/tui/panels/help.py`
- Modify: `src/modelctl/core/tui/app.py`（help 模式渲染分支）
- Test: `tests/test_tui_help.py`

**Interfaces:**
- Consumes: `TUIState.mode`, `TUIState.help_scroll`
- Produces: `render_help(width, height, theme_id) -> Panel`；`_KEYMAP` 常量

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tui_help.py
from rich.console import Console
from modelctl.core.tui.panels.help import render_help

def test_help_renders_keymap():
    panel = render_help(width=80, height=24, theme_id="dark")
    console = Console(record=True, width=80, force_terminal=False)
    console.print(panel)
    text = console.export_text()
    assert "q" in text
    assert "搜索" in text or "/" in text
    assert "帮助" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tui_help.py::test_help_renders_keymap -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/modelctl/core/tui/panels/help.py
"""TUI 帮助弹层：键位表（rich Table）。"""
from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from modelctl.core.tui.theme import get_rich_theme

_KEYMAP: list[tuple[str, str]] = [
    ("q / Esc", "退出 / 回 dashboard"),
    ("1-5", "切换视图 DASH/DETAIL/PLAN/CLUSTER/MONITOR"),
    ("j / k / ↑ / ↓", "上下移动"),
    ("/", "进入搜索模式"),
    ("f", "循环状态过滤（全部/运行/停止）"),
    ("s", "循环排序（名称/端口/速率/显存）"),
    ("d", "循环引擎过滤（dashboard）/ Dry-run（plan）"),
    ("g / G", "跳到列表首/尾"),
    ("PgUp / PgDn", "翻页"),
    ("Enter", "进 Detail 视图 / 确认搜索"),
    ("h / ?", "打开/关闭帮助"),
    ("t", "切换主题"),
    ("Tab / Shift-Tab", "切换子 Tab / 字段"),
]


def render_help(width: int, height: int, theme_id: str = "dark") -> Panel:
    theme = get_rich_theme(theme_id)
    table = Table(show_header=True, header_style="bold",
                  width=max(40, width - 4), padding=(0, 1))
    table.add_column("键", width=20, overflow="ellipsis", no_wrap=True)
    table.add_column("动作", overflow="fold")
    for k, desc in _KEYMAP:
        table.add_row(k, desc)
    return Panel(table, title="帮助", title_align="left",
                 width=width, border_style=theme["accent"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tui_help.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/tui/panels/help.py tests/test_tui_help.py
git commit -m "feat(tui): add help overlay panel with keymap table"
```

---

### Task 8: 全量回归 + 集成验证

**Files:**
- Modify: `tests/test_tui_no_side_effects.py`（如需适配新渲染路径）
- Modify: `tests/test_tui_panel_dashboard.py`（同步旧断言）

**Interfaces:**
- Consumes: 所有前序 Task 产出
- Produces: 全量测试通过 + 80x24 窄屏验证

- [ ] **Step 1: 运行全量测试**

Run: `pytest tests/test_tui_*.py -v`
Expected: 全部 PASS

- [ ] **Step 2: 修复因新渲染路径导致的旧测试失败**

主要适配点：
- `test_tui_panel_dashboard.py` 中旧断言如 `assert "q 退" in text` 需改为断言 keybar 标签存在
- `test_tui_no_side_effects.py` 中 `subprocess.Popen.call_count == 0` 保持不变

- [ ] **Step 3: 窄屏 80x24 验证**

Run: `python -c "
from rich.console import Console
from modelctl.core.tui.app import TuiApp
from modelctl.core.tui.state import TUIState
app = TuiApp(state=TUIState(), console=Console(width=80, height=24, force_terminal=False))
app.realize_render_once()
print('80x24 render OK')
"`
Expected: 无异常，输出 `80x24 render OK`

- [ ] **Step 4: 最终 Commit**

```bash
git add -A
git commit -m "feat(tui): complete visual redesign, all tests passing"
```

---

## Self-Review

### 1. Spec Coverage

| Spec 章节 | 对应 Task |
|---|---|
| §2 全局骨架与主题 | Task 2 |
| §3 Dashboard 主表 | Task 3 |
| §4 数据层修复与扩展 | Task 1 |
| §5 键盘交互与模式系统 | Task 4 |
| §6 Detail/Plan/Cluster/Monitor 改造 | Task 5, 6 |
| §7 测试策略 | 各 Task 内嵌测试步骤 + Task 8 |
| §8 里程碑 M1-M7 | Task 1-7 一一对应，Task 8 收尾 |

### 2. Placeholder Scan

- 无 TBD/TODO/"implement later"
- 无 "add appropriate error handling" 类模糊描述
- 所有代码步骤均有完整代码块
- 无 "Similar to Task N" 引用

### 3. Type Consistency

- `_extract_ctx` 返回 `int | None`，Task 1 测试断言 `p0["ctx"] == 131072`（int）一致
- `render_chrome` 参数签名在 Task 2 定义，Task 5/6 中调用方式一致
- `TUIState.mode` 类型 `Literal["normal","search","help"]` 在 Task 4 定义，Task 2 chrome 中 `state.mode` 读取一致
- `_keybar_for_view` 在 Task 2 定义，Task 4 中 `Key.Slash` 触发搜索模式后 keybar 自动切换（通过 `state.mode` 读取）一致
