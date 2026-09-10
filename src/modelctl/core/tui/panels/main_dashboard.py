#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/panels/main_dashboard.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/10 10:00
# @Desc   : TUI 主仪表盘（GPU 摘要 / profile 表格 / keybar，CJK 对齐守，含 80 列窄屏适配）
# ===============================================================================

"""TUI 主仪表盘。

- 顶部 header：`GPU {n}x {name} | 自由显存 {free}/{total}G | 模型 {run}/{total} 运行 | 集群: {k} 节点`
- 中部 profile 表格（pad_width 对齐，用 `display_width` 校验列总宽 == width）
- 底部 keybar：`q 退 | / 搜 | f 状态 | s 排序 | d 引擎 | j/k 翻页 | Enter 详情 | PgUp/PgDn 整页`

每行严格 pad_width 到 width 列宽（CJK 双宽口径由 `display_width` / `pad_width` 承担）。
渲染**不直接调** `list_profiles()` / `probe()` / `launch_log()`——快照已在 data.py
完成，render 只消费 `ModelsSnapshot.profiles` / `HardwareSnapshot.gpus` /
`ClusterSnapshot.nodes`。

Task 6 追加 80 列窄屏适配：
- `full`（width >= 120）：保留全部 7 列（NAME / ENGINE / VARIANT / PORT / STATUS /
  RATE / VRAM），列表宽 80 单位，剩余空间均分到右侧填充。
- `medium`（100 <= width < 120）：隐藏 VARIANT 列，列表宽 71 单位，右侧均分填充。
- `narrow`（80 <= width < 100）：隐藏 VARIANT 列 + RATE 收窄到 7 单位
  （短速率如 `155/33` 仍可读），列表宽 41 单位，右侧均分填充。

Caps 语义：`_console_size` 已经保证 width 至少 80；本模块不再做"宽度 < 80 强行
80"的二次校验（会跟 _console_size 重复），表宽固定，超宽靠 `pad_width` 兜底
到 `width`，不会截断语义内容。
"""

from __future__ import annotations

from rich.console import Group
from rich.text import Text

from modelctl.core.colors import display_width, pad_width
from modelctl.core.tui.data import ClusterSnapshot, HardwareSnapshot, ModelsSnapshot
from modelctl.core.tui.state import TUIState
from modelctl.core.tui.theme import get_rich_theme

#: 表格列宽分配（CJK 感知前，按 ASCII 列宽预留）；总宽 + 边框（每段 0 边框 = 纯空格分隔）
#: NAME / ENGINE / VARIANT / PORT / STATUS / RATE / VRAM = 20/10/9/5/9/13/14 = 80
COL_NAME = 20
COL_ENGINE = 10
COL_VARIANT = 9
COL_PORT = 5
COL_STATUS = 9
COL_RATE_FULL = 13
COL_RATE_NARROW = 7
COL_VRAM = 14
COL_TOTAL_FULL = COL_NAME + COL_ENGINE + COL_VARIANT + COL_PORT + COL_STATUS + COL_RATE_FULL + COL_VRAM
COL_TOTAL_MEDIUM = COL_NAME + COL_ENGINE + COL_PORT + COL_STATUS + COL_RATE_FULL + COL_VRAM
COL_TOTAL_NARROW = COL_NAME + COL_ENGINE + COL_PORT + COL_STATUS + COL_RATE_NARROW + COL_VRAM

#: 布局模式
LAYOUT_FULL = "full"
LAYOUT_MEDIUM = "medium"
LAYOUT_NARROW = "narrow"


def _layout_for_width(width: int) -> str:
    """按宽度选布局模式。

    - `full`：width >= 120，7 列全显
    - `medium`：100 <= width < 120，隐藏 VARIANT 列
    - `narrow`：80 <= width < 100（含 width < 80 的兜底，表宽不变，靠
      `pad_width` 截断感出现，实际受 _console_size 保护不会走到）
    """
    if width >= 120:
        return LAYOUT_FULL
    if width >= 100:
        return LAYOUT_MEDIUM
    return LAYOUT_NARROW


def _layout_total_width(mode: str) -> int:
    """布局的列表总宽（满分 = 表中列宽之和；右侧剩余 = width - 列表总宽）。"""
    if mode == LAYOUT_FULL:
        return COL_TOTAL_FULL
    if mode == LAYOUT_MEDIUM:
        return COL_TOTAL_MEDIUM
    return COL_TOTAL_NARROW


def _mb_to_gb(mb: int) -> float:
    """MB → GB（保留 1 位）。"""
    if mb <= 0:
        return 0.0
    return round(mb / 1024.0, 1)


def _header_text(state: TUIState, hw: HardwareSnapshot, models: ModelsSnapshot,
                 cluster: ClusterSnapshot, width: int, theme: dict) -> Text:
    """顶部 header：GPU 摘要 + 模型运行率 + 集群节点，一行到位，pad_width 到 width。

    行结构：`-- {gpu_seg} | {vram_seg} | {model_seg} | {cluster_seg} --`
    - GPU：`{count}x {首卡 gpu_name}`（`gpu_count == 0` → "0x"）
    - 自由显存：`{free_gb:g}/{total_gb:g}G`
    - 模型：`{running}/{total} 运行`（不应用 search/filter，仅显状态分布）
    - 集群：`{k} 节点`

    采用"构造完整行字符串 → 整体 pad_width 到 width"：CJK 双宽由
    `display_width` 兜底，避免 character-based truncate。
    """
    gpu_name = hw.gpus[0]["name"] if hw.gpus else ""
    gpu_seg = f"{len(hw.gpus)}x {gpu_name}" if hw.gpus else "0x"
    free_gb = _mb_to_gb(sum(g["free_mb"] for g in hw.gpus))
    total_gb = _mb_to_gb(sum(g["total_mb"] for g in hw.gpus))
    vram_seg = f"自由显存 {free_gb:g}/{total_gb:g}G"
    running = sum(1 for p in models.profiles if p.get("status") == "running")
    model_seg = f"模型 {running}/{len(models.profiles)} 运行"
    cluster_seg = f"集群: {len(cluster.nodes)} 节点"
    raw = f"-- {gpu_seg} | {vram_seg} | {model_seg} | {cluster_seg} --"
    body = pad_width(raw, width)
    return Text(body, style=theme["title"])


def _status_glyph(status: str, theme: dict) -> str:
    """状态徽标：running 实心 ●（主题 success 色），stopped 空心 ○（dim）。"""
    if status == "running":
        return "●"
    if status == "stopped":
        return "○"
    return "?"


def _status_style(status: str, theme: dict) -> str:
    """状态色：running=success 主题色，stopped=dim 主题色。"""
    if status == "running":
        return theme["success"]
    if status == "stopped":
        return theme["dim"]
    return theme["dim"]


def _profile_row(
    state: TUIState,
    idx: int,
    profile: dict,
    width: int,
    theme: dict,
    mode: str,
) -> Text:
    """表格数据行（按布局模式列宽取段，末尾 pad_width 到 width）。

    - 当前选中（state.active_index == idx in 当前页）整行 bold
    - 列按 `mode` 取宽：
      - `full`：NAME / ENGINE / VARIANT / PORT / STATUS / RATE (13) / VRAM
      - `medium`：NAME / ENGINE / PORT / STATUS / RATE (13) / VRAM（隐藏 VARIANT）
      - `narrow`：NAME / ENGINE / PORT / STATUS / RATE (7) / VRAM
    - 状态：● running / ○ stopped；速率无数据显 "—"（narrow 模式截成 7 单位）
    """
    active = idx == state.active_index
    text = Text()
    name = str(profile.get("name", ""))
    engine = str(profile.get("engine", ""))
    variant = str(profile.get("variant", "")) or "-"
    port = str(profile.get("port", "")) or "-"
    status = str(profile.get("status", ""))
    vram = float(profile.get("vram_gib") or 0.0)
    rate_in = profile.get("rate_in")
    rate_out = profile.get("rate_out")
    rate_str_full = (
        f"{rate_in:.0f}/{rate_out:.0f}" if rate_in is not None and rate_out is not None else "—"
    )
    rate_str = rate_str_full
    if mode == LAYOUT_NARROW:
        # 窄屏：列宽固定 7，速率长于 7 显示单位时截断（纯 ASCII 数字串，
        # display_width 与 len 等值，统一走 display_width 守规则）
        if display_width(rate_str_full) > COL_RATE_NARROW:
            rate_str = rate_str_full[:COL_RATE_NARROW]
    vram_str = f"{vram:.0f}G" if vram > 0 else "-"
    vram_width = display_width(vram_str)

    prefix = "bold " if active else ""
    rate_col = COL_RATE_NARROW if mode == LAYOUT_NARROW else COL_RATE_FULL
    cells = [
        (pad_width(name, COL_NAME), theme["title"]),
        (pad_width(engine, COL_ENGINE), theme["dim"]),
    ]
    if mode == LAYOUT_FULL:
        cells.append((pad_width(variant, COL_VARIANT), theme["dim"]))
    cells.extend(
        [
            (pad_width(port, COL_PORT), theme["dim"]),
            (_status_glyph(status, theme), _status_style(status, theme)),
            (
                pad_width(str(status), 8, align="left"),
                theme["success"] if status == "running" else theme["dim"],
            ),
            (
                pad_width(rate_str, rate_col),
                theme["success"] if rate_in is not None else theme["dim"],
            ),
            (vram_str, theme["dim"]),
        ]
    )
    for value, style in cells:
        text.append(value, style=f"{prefix}{style}")
    # 前段实际宽 = 布局总宽 - VRAM 列宽 + 实际 vram_str 可见宽（pad 到宽度需求弱）
    list_total = _layout_total_width(mode)
    rest = max(0, width - (list_total - COL_VRAM + vram_width))
    if rest > 0:
        text.append(" " * rest, style=theme["dim"])
    return text


def _placeholder(width: int, theme: dict) -> Text:
    """空表格占位行：`(尚无 profile)`，pad_width 到 width。"""
    label = "(尚无 profile)"
    return Text(pad_width(label, width), style=theme["dim"])


def _keybar(width: int, theme: dict) -> Text:
    """底部 keybar：brief 指定的快捷键标签，pad_width 到 width。"""
    label = "q 退 | / 搜 | f 状态 | s 排序 | d 引擎 | j/k 翻页 | Enter 详情 | PgUp/PgDn 整页"
    text = Text()
    text.append(pad_width(label, width), style=theme["keybar"])
    return text


def render(
    state: TUIState,
    hw: HardwareSnapshot,
    models: ModelsSnapshot,
    cluster: ClusterSnapshot,
    width: int,
    height: int,
    theme_id: str = "dark",
) -> Group:
    """渲染仪表盘，返回 `rich.Group`。

    步骤：
    1. 从 `theme_id` 取主题（`get_rich_theme` 未知回落 dark）
    2. 顶部 header：GPU + 自由显存 + 模型运行率 + 集群节点
    3. 中部 profile 表格：state 的 `filter_candidates` → `sort_profiles` → `apply_page(page_size=10)`
       后渲染；当前页为空 → 占位行
    4. 底部 keybar：brief 指定的快捷键标签串
    """
    theme = get_rich_theme(theme_id)
    mode = _layout_for_width(width)  # Task 6：按 width 选 full/medium/narrow
    header = _header_text(state, hw, models, cluster, width, theme)
    models_filtered = state.sort_profiles(state.filter_candidates(models.profiles))
    page = state.apply_page(models_filtered, page_size=10)
    rows: list[Text] = []
    if not page:
        rows.append(_placeholder(width, theme))
    else:
        # 选中索引约束：若 active_index 超出当前页 → 取 0 号
        if state.active_index >= len(page):
            state.active_index = 0
        for i, p in enumerate(page):
            rows.append(_profile_row(state, i, p, width, theme, mode))
    keybar = _keybar(width, theme)
    return Group(header, *rows, keybar)
