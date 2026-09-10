#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/panels/monitor.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/10 10:00
# @Desc   : Monitor 视图（速率表 + GPU 卡片 横/纵双布局，CJK 对齐）
# ===============================================================================

"""Monitor 视图渲染（Task 5 交付）。

布局：
- 摘要条：`-- Monitor | 速率表 + GPU 卡片 --`
- Tab header：`[0]速率表 [1]GPU 卡片`（active 前置 `>` 指示）
- Body（左右并排当 width >= 120；上下纵向当 width < 120）：
  - 速率表：`name | in/s | out/s` 列；rate 为 None → (not measured)；空 info → 黄 Panel
  - GPU 卡片：每卡 `GPU {i}: {name} | util {u}% | mem {free}/{total} MiB`；空 → 黄 Panel
- keybar：仅展示 `Tab / r / g / Esc / q` 提示（实际键盘派发 T6 接）

硬约束：
- **不读 pynvml**（T2 HardwareSnapshot.gpus[].util_pct 已固定 0）。`util_pct` 永远 0
  是当前现状——T6 接 pynvml 后才允许非零。
- **本 task 不接键盘派发**（state.active_monitor_tab 默认 0，cycle_monitor_tab 是 helper）
- 行数少可 Accept；每行 display_width 恰好 == width 是强约束（CJK 对齐兜底）
- 禁止 subprocess.Popen / open(mode='w') / os.kill（spec §6.2）

数据源：
- monitor.info：list of dict {name, rate_in, rate_out}（MonitorSnapshot TTL=5s）
- hw.gpus：list of dict {index, name, free_mb, total_mb, util_pct}（HardwareSnapshot TTL=60s）
"""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from modelctl.core.colors import display_width, pad_width
from modelctl.core.tui.data import HardwareSnapshot, MonitorSnapshot, ModelsSnapshot
from modelctl.core.tui.state import TUIState
from modelctl.core.tui.theme import get_rich_theme

# Tab 顺序（active_monitor_tab 0/1，与 cycle_monitor_tab 一致）
_MONITOR_TAB_LABELS = ("速率表", "GPU 卡片")

# 左右并排最小宽度（>= 120 走并排；< 120 走上下）
_MIN_WIDTH_SIDE_BY_SIDE = 120


def _summary_line(width: int, theme: dict) -> Text:
    """摘要条：`-- Monitor | 速率表 + GPU 卡片 --`，pad_width 到 width。"""
    raw = "-- Monitor | 速率表 + GPU 卡片 --"
    return Text(pad_width(raw, width), style=theme["title"])


def _tab_header(state: TUIState, width: int, theme: dict) -> Text:
    """Tab header：`[0]速率表 [1]GPU 卡片`，active 前置 `>`。"""
    try:
        idx = int(state.active_monitor_tab)
    except (TypeError, ValueError):
        idx = 0
    if idx < 0 or idx >= len(_MONITOR_TAB_LABELS):
        idx = 0
    segs: list[str] = []
    for i, label in enumerate(_MONITOR_TAB_LABELS):
        prefix = ">" if i == idx else " "
        segs.append(f"[{i}]{prefix}{label}")
    raw = "-- " + " ".join(segs) + " --"
    return Text(pad_width(raw, width), style=theme["title"])


def _keybar_monitor(width: int, theme: dict) -> Text:
    """底部 keybar：`Tab 切 section | r GPU 快刷 (T6 接) | g 启停 GPU 轮询 (T6 接) | Esc 回 dashboard | q 退`。"""
    label = "Tab 切 section | r GPU 快刷 (T6 接) | g 启停 GPU 轮询 (T6 接) | Esc 回 dashboard | q 退"
    return Text(pad_width(label, width), style=theme["keybar"])


def _rate_col_widths(info: list[dict]) -> tuple[int, int, int]:
    """(name_w, in_w, out_w) 三列宽，避免超长按取 max(display_width(value)) + 2。"""
    name_w = max([display_width(str(p.get("name", "") or "")) for p in info] + [20]) + 2
    # rate 字符串：None → `(not measured)` (12 + 1)；数字最多 8 字符（保留 12.0 容差）
    in_vals = [
        "(not measured)" if p.get("rate_in") is None else str(p.get("rate_in"))
        for p in info
    ]
    out_vals = [
        "(not measured)" if p.get("rate_out") is None else str(p.get("rate_out"))
        for p in info
    ]
    in_w = max([display_width(v) for v in in_vals] + [12]) + 2
    out_w = max([display_width(v) for v in out_vals] + [12]) + 2
    return name_w, in_w, out_w


def _rate_table(monitor: MonitorSnapshot, width: int, theme: dict) -> Panel:
    """速率表 Panel：3 列（name / in/s / out/s）。

    - 空 info → 黄 Panel `(无运行中 profile)`（border warning）
    - rate None → 单元格文本 `(not measured)`
    - 空 row：name 填 "(none)"
    """
    info = monitor.info or []
    if not info:
        return Panel(
            Text(
                pad_width("(无运行中 profile)", max(20, width - 4)), style=theme["warning"]
            ),
            title="速率表", title_align="left",
            width=width, border_style=theme["warning"],
        )
    name_w, in_w, out_w = _rate_col_widths(info)
    header = (
        pad_width("name", name_w)
        + pad_width("in/s", in_w)
        + pad_width("out/s", out_w)
    )
    body_lines: list[Text] = [
        Text(pad_width(header, max(20, width - 4)), style=theme["dim"])
    ]
    for p in info:
        name = str(p.get("name", "") or "") or "(none)"
        rin = p.get("rate_in")
        rout = p.get("rate_out")
        in_str = "(not measured)" if rin is None else str(rin)
        out_str = "(not measured)" if rout is None else str(rout)
        text = Text()
        text.append(pad_width(name, name_w), style=theme["title"])
        text.append(pad_width(in_str, in_w), style=theme["dim"])
        text.append(pad_width(out_str, out_w), style=theme["dim"])
        # 追平 inner_w
        inner_w = max(20, width - 4)
        dw = display_width(text.plain)
        if dw < inner_w:
            text.append(" " * (inner_w - dw), style=theme["dim"])
        body_lines.append(text)
    return Panel(Group(*body_lines), title="速率表", title_align="left", width=width)


def _gpu_cards(hw: HardwareSnapshot, width: int, theme: dict) -> Panel:
    """GPU 卡片 Panel：每卡一行 `GPU {i}: {name} | util {u}% | mem {free}/{total} MiB`。

    - hw.gpus 空 → 黄 Panel `(无 GPU 可用设备 — nvidia-smi / pynvml 均未发现)`
    - util_pct 固定 0（T2 不读 SM util，T6 接 pynvml 后才允许非零）
    """
    gpus = hw.gpus or []
    if not gpus:
        return Panel(
            Text(
                pad_width("(无 GPU 可用设备 — nvidia-smi / pynvml 均未发现)",
                          max(20, width - 4)),
                style=theme["warning"],
            ),
            title="GPU 卡片", title_align="left",
            width=width, border_style=theme["warning"],
        )
    # 列宽：按实际数据 max
    col_index = max([display_width(f"GPU {int(g.get('index', 0))}") for g in gpus] + [14]) + 2
    col_name = max([display_width(str(g.get("name", "") or "")) for g in gpus] + [12]) + 2
    col_util = max([5 for g in gpus] + [10]) + 2  # util ≤ 100% → "util 100%" 6+4 col
    col_mem = max([display_width(f"mem {int(g.get('free_mb', 0))}/{int(g.get('total_mb', 0))} MiB")
                   for g in gpus] + [24]) + 2
    body_lines: list[Text] = []
    inner_w = max(20, width - 4)
    for g in gpus:
        idx = int(g.get("index", 0))
        name = str(g.get("name", "") or "") or "(unknown)"
        util = int(g.get("util_pct", 0) or 0)
        free = int(g.get("free_mb", 0) or 0)
        total = int(g.get("total_mb", 0) or 0)
        line = Text()
        line.append(pad_width(f"GPU {idx}", col_index), style=theme["title"])
        line.append(pad_width(name, col_name), style=theme["dim"])
        # util 行单独色（util=0 也走 dim，非 0 走 success——本 task 不接 pynvml 恒 0，
        # 但保留 success 分支给 T6 接 pynvml 后的 live 数据）
        util_style = theme["success"] if util > 0 else theme["dim"]
        line.append(pad_width(f"util {util}%", col_util), style=util_style)
        line.append(pad_width(f"mem {free}/{total} MiB", col_mem), style=theme["dim"])
        dw = display_width(line.plain)
        if dw < inner_w:
            line.append(" " * (inner_w - dw), style=theme["dim"])
        body_lines.append(line)
    return Panel(Group(*body_lines), title="GPU 卡片", title_align="left", width=width)


def render(
    state: TUIState,
    models: ModelsSnapshot,
    hw: HardwareSnapshot,
    monitor: MonitorSnapshot,
    width: int,
    height: int,
    theme_id: str = "dark",
) -> Group:
    """渲染 Monitor 视图，返回 `rich.Group`。

    步骤：
    1. 主题 + 摘要条 + tab_header + keybar
    2. 速率表 + GPU 卡片：`width >= 120` 走**横向并排**——
       - 用 `Text.assemble` 把两 Panel 各按 width//2 宽渲染，再左右拼接
       - 简化方案：rate 固定 width//2，GPU 固定 width//2（差 1/2 留 cont
       余量，centertext 用 `Layout` 会引 smoke 难 mock 问题）
    3. `width < 120` 走**纵向上下**——rate Panel + GPU Panel 各按 width

    **不**调 subprocess / open('w') / os.kill。
    """
    theme = get_rich_theme(theme_id)
    summary = _summary_line(width, theme)
    tab_header = _tab_header(state, width, theme)
    keybar = _keybar_monitor(width, theme)

    rate_p = _rate_table(monitor, width, theme)
    gpu_p = _gpu_cards(hw, width, theme)

    if width >= _MIN_WIDTH_SIDE_BY_SIDE:
        # 横向并排：每个 Panel 占 width//2（差 1 列从 center 取，剩量让给 rate 表）
        half = width // 2
        # T5 不做精算：简单让 rate 表 width=half, GPU width=width-half
        _rate_p_half = _rate_table(monitor, max(40, half), theme)
        _gpu_p_half = _gpu_cards(hw, max(40, width - half), theme)
        # 拼成「左 rate 表.padding( width-half 长空白) + 右 GPU」纯文本拼接——
        # 用 Console(record=) 各自导出，再按位合并（避免 Layout 在 smoke 难 mock）
        from rich.console import Console as _Console
        c1 = _Console(record=True, width=half, height=height, force_terminal=False)
        c1.print(_rate_p_half)
        c2 = _Console(record=True, width=width - half, height=height, force_terminal=False)
        c2.print(_gpu_p_half)
        l1 = c1.export_text()
        l2 = c2.export_text()
        # 行对齐：长短行右补，CRLF strip
        l1_lines = [ln.rstrip("\r\n") for ln in l1.split("\n")]
        l2_lines = [ln.rstrip("\r\n") for ln in l2.split("\n")]
        n = max(len(l1_lines), len(l2_lines))
        lines: list[Text] = []
        for i in range(n):
            a = l1_lines[i] if i < len(l1_lines) else ""
            b = l2_lines[i] if i < len(l2_lines) else ""
            a_pad = a.ljust(half) if display_width(a) < half else a
            # 右半补到 width
            right = Text(pad_width(b, width - half), style=theme["dim"])
            left = Text(a_pad, style=theme["dim"])
            merged = Text()
            merged.append(left.plain, style=theme["dim"])
            merged.append(right.plain, style=theme["dim"])
            lines.append(merged)
        body = Group(*lines)
    else:
        # 纵向上下：rate Panel + GPU Panel 各 width（占满）
        body = Group(rate_p, gpu_p)

    return Group(summary, tab_header, body, keybar)


__all__ = ["render"]
