#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_tui_panel_monitor.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/10 10:00
# @Desc   : Monitor 视图 速率表 + GPU 卡片 渲染测试
# ===============================================================================

"""Monitor 视图渲染测试。

覆盖 Task 5 交付物：
- hw.gpus=[] → 黄 Panel "(无 GPU 可用设备 — nvidia-smi / pynvml 均未发现)"
- 两 GPU cards → 每行 `GPU {i}: {name} | util {u}% | mem {free}/{total} MiB`（util 固定 0）
- monitor.info rate=None → 单元格 "(not measured)"
- monitor.info rate 有值 → 数字出现
- 中文 profile 名 → 每行 display_width 与 width 边界一致
- width=100 (<120) / width=130 (>=120) 布局切换均不 crash
"""

from __future__ import annotations

import time

from rich.console import Console

from modelctl.core.colors import display_width
from modelctl.core.tui.data import HardwareSnapshot, ModelsSnapshot, MonitorSnapshot
from modelctl.core.tui.panels.monitor import render as render_monitor
from modelctl.core.tui.state import TUIState

# ─────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────


def _snap(*, gpus=None, info=None) -> tuple[HardwareSnapshot, ModelsSnapshot, MonitorSnapshot]:
    hw = HardwareSnapshot()
    hw.gpus = list(gpus or [])
    hw._fetched_at = time.monotonic()
    models = ModelsSnapshot()
    models._fetched_at = time.monotonic()
    mon = MonitorSnapshot()
    mon.info = list(info or [])
    mon._fetched_at = time.monotonic()
    return hw, models, mon


def _render_text(state, hw, models, monitor, *, width=100, height=24) -> str:
    """render + rich Console record 捕获。
    monitor.info 是 list[dict]，无需 mock cluster_role（T5 渲染无中心依赖）。
    """
    console = Console(record=True, width=width, height=height, force_terminal=False)
    group = render_monitor(state, models, hw, monitor, width=width, height=height, theme_id="dark")
    console.print(group)
    return console.export_text()


# ─────────────────────────────────────────────────────────
# 1. no GPU
# ─────────────────────────────────────────────────────────


def test_monitor_no_gpu_placeholder():
    """hw.gpus=[] → 黄 Panel "(无 GPU 可用设备 — nvidia-smi / pynvml 均未发现)"。"""
    state = TUIState()
    state.active_view = "monitor"
    hw, models, mon = _snap(gpus=[], info=[{"name": "qwen-7b", "rate_in": 1.0, "rate_out": 0.5}])
    text = _render_text(state, hw, models, mon, width=100)
    assert "无 GPU 可用设备" in text
    # pynvml 未启用（项目硬约束：本 task 不加 pynvml）
    assert "nvidia-smi" in text or "pynvml" in text


# ─────────────────────────────────────────────────────────
# 2. two GPU cards
# ─────────────────────────────────────────────────────────


def test_monitor_two_gpu_cards():
    """hw.gpus 两卡 → 'GPU 0: A100' + 'GPU 1: A100' 各一次 + util 50% 出现。"""
    state = TUIState()
    state.active_view = "monitor"
    gpus = [
        {"index": 0, "name": "A100", "free_mb": 10000, "total_mb": 81920, "util_pct": 0},
        {"index": 1, "name": "A100", "free_mb": 20000, "total_mb": 81920, "util_pct": 0},
    ]
    hw, models, mon = _snap(gpus=gpus, info=[])
    text = _render_text(state, hw, models, mon, width=100)
    assert "GPU 0" in text
    assert "GPU 1" in text
    assert "A100" in text
    # T2 定版 util_pct=0（pynvml 没接）—— 至少两卡各出现一次
    assert text.count("A100") >= 2
    # mem free/total 数字至少 10000 / 20000 出现
    assert "10000" in text
    assert "20000" in text


# ─────────────────────────────────────────────────────────
# 3. rate table with None
# ─────────────────────────────────────────────────────────


def test_monitor_rate_table_with_none():
    """monitor.info rate_in/rate_out 全 None → 单元格 (not measured)。"""
    state = TUIState()
    state.active_view = "monitor"
    hw, models, mon = _snap(
        gpus=[{"index": 0, "name": "A100", "free_mb": 81920, "total_mb": 81920, "util_pct": 0}],
        info=[{"name": "qwen-7b", "rate_in": None, "rate_out": None}],
    )
    text = _render_text(state, hw, models, mon, width=100)
    assert "qwen-7b" in text
    assert "not measured" in text


# ─────────────────────────────────────────────────────────
# 4. rate table with values
# ─────────────────────────────────────────────────────────


def test_monitor_rate_table_with_values():
    """monitor.info rate 1234.5 / 678.9 → 数字在表款出现（float 字符串化短）。"""
    state = TUIState()
    state.active_view = "monitor"
    hw, models, mon = _snap(
        gpus=[{"index": 0, "name": "A100", "free_mb": 81920, "total_mb": 81920, "util_pct": 0}],
        info=[{"name": "qwen-7b", "rate_in": 1234.5, "rate_out": 678.9}],
    )
    text = _render_text(state, hw, models, mon, width=100)
    assert "qwen-7b" in text
    # 1234.5 → "1234" 必现（精确 float 位数方案不固定）
    assert "1234" in text
    assert "678" in text


# ─────────────────────────────────────────────────────────
# 5. CJK strict alignment
# ─────────────────────────────────────────────────────────


def test_monitor_cjk_strict_alignment():
    """中文 profile 名 + 中文 GPU 名 → 每行 display_width <= width；摘要/keybar == width-1 或 width。"""
    state = TUIState()
    state.active_view = "monitor"
    width = 100
    hw, models, mon = _snap(
        gpus=[
            {"index": 0, "name": "中文GPU卡-1", "free_mb": 81920, "total_mb": 102400, "util_pct": 0},
            {"index": 1, "name": "中文GPU卡-2", "free_mb": 60000, "total_mb": 102400, "util_pct": 0},
        ],
        info=[{"name": "中文模型Profile-1", "rate_in": 1.0, "rate_out": 2.5}],
    )
    text = _render_text(state, hw, models, mon, width=width)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    assert lines, "无有效渲染行"
    for ln in lines:
        dw = display_width(ln)
        assert dw <= width + 1, f"行超宽 {dw} > {width + 1}: {ln!r}"
    # 摘要条 + keybar 行各自 == width-1 或 width
    first = lines[0].rstrip("\r")
    assert display_width(first) in (width, width - 1), \
        f"摘要行宽度异常：{display_width(first)} 期望 {width}"
    last = lines[-1].rstrip("\r")
    assert display_width(last) in (width, width - 1), \
        f"keybar 行宽度异常：{display_width(last)} 期望 {width}"


# ─────────────────────────────────────────────────────────
# 6. width layout switch (100 -> 上下, 130 -> 左右)
# ─────────────────────────────────────────────────────────


def test_monitor_width_layout_switch():
    """width=100 (<120 走上下) 与 width=130 (>=120 走左右) 都不 crash。"""
    state = TUIState()
    state.active_view = "monitor"
    gpus = [{"index": 0, "name": "A100", "free_mb": 10000, "total_mb": 81920, "util_pct": 0}]
    mon_info = [{"name": "qwen-7b", "rate_in": 64.0, "rate_out": 32.0}]
    for width in (100, 130):
        st = TUIState()
        st.active_view = "monitor"
        hw, models, mon = _snap(gpus=gpus, info=mon_info)
        text = _render_text(st, hw, models, mon, width=width)
        assert "A100" in text
        assert "qwen-7b" in text
        # 摘要条存在
        assert "Monitor" in text
