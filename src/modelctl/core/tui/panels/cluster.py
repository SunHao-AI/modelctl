#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/panels/cluster.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/10 10:00
# @Desc   : Cluster 视图 3 section 渲染（节点表 / goal 表 / 事件占位 + SoloStub，CJK 对齐）
# ===============================================================================

"""Cluster 视图渲染（Task 5 交付）。

布局：
- 摘要条：`-- Cluster | role: {role} | 节点: {total} nodes (...) | goals: {goal_count} | center: {url} --`
- 未接入 stub（role=solo 或 center_visible="(中心不可达)"）→ 顶部红 Panel + keybar 仅，跳过 3 section
- 否则渲染 "Tab header + section + keybar"：
  - Tab 0「节点」：node_id / 状态 / LAN / 容量 / goal(start 收敛/声明)
  - Tab 1「goal」：profile / 节点 / intent / stage chain 8 格 / state / 端口（按 profile 分组）
  - Tab 2「事件」：T6 接事件流前为占位 Panel `(T6 接事件流)`

聚合逻辑 1:1 从 cli._cmd_status_cluster 抄写：
- `per[node_id]["start"] / ["stop"] / ["ready"]`（stage == "READY" 时 ready += 1）
- goal 列 = `f"{ready}/{start}" if c else "0/0"`

场景覆盖：
- Solo Stub：solo role 或 center 不可达 → 顶部红 Panel "中央未接入（solo role：...）"，3 section 不渲染
- 空 nodes：黄 Panel "(集群无节点 — 检查 role=center/both 是否允许)"
- 空 goals：黄 Panel "集群暂无 goal（中心台账为空）"（与 CLI 文案对齐）

本函数**不**调 subprocess / open('w') / os.kill——只读快照。
含 CJK 输出一律 `pad_width` 到 width（`display_width` 双宽兜底）。
"""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from modelctl.core.colors import display_width, pad_width
from modelctl.core.tui.data import ClusterSnapshot
from modelctl.core.tui.state import TUIState
from modelctl.core.tui.theme import get_rich_theme

# Tab 顺序（与 state.active_cluster_tab 0/1/2 对齐）
_CLUSTER_TAB_LABELS = ("节点", "goal", "事件")

# stage chain 正常链路 + 旁路（VALID_STAGES 顺序，与 reconcile.VALID_STAGES 一致）
_STAGE_ORDER = (
    "PENDING_PROFILE_SYNC", "PROFILE_SYNCED", "RUNTIME_OK", "STARTING",
    "READY", "DEGRADED", "FAILED", "STOPPED",
)


def _center_visible_short(center_visible: str) -> str:
    """center_visible 截前 12 字符；空 → '-'；超长加 '…' 标识。"""
    if not center_visible:
        return "-"
    if display_width(center_visible) <= 12:
        return center_visible
    # 按 codepoint 截到显示宽度 11 + '…'（显示宽 12）
    out = ""
    w = 0
    for ch in center_visible:
        cw = 2 if len(ch) and display_width(ch) >= 2 else 1
        if w + cw > 11:
            break
        out += ch
        w += cw
    return out + "…"


def _summary_line(
    role: str, cluster: ClusterSnapshot, width: int, theme: dict
) -> Text:
    """顶部摘要条：`-- Cluster | role: {role} | 节点: {total} nodes ... | center: {url} --`。"""
    online = sum(1 for n in cluster.nodes if str(n.get("status", "")) == "online")
    offline = sum(1 for n in cluster.nodes if str(n.get("status", "")) == "offline")
    total = len(cluster.nodes)
    goal_count = len(cluster.goals)
    center_short = _center_visible_short(cluster.center_visible)
    raw = (
        f"-- Cluster | role: {role} | 节点: {total} nodes ({online} 在线/{offline} 离线)"
        f" | goals: {goal_count} | center: {center_short} --"
    )
    return Text(pad_width(raw, width), style=theme["title"])


def _tab_header(state: TUIState, width: int, theme: dict) -> Text:
    """Tab header：`[0]节点 [1]goal [2]事件` 当前 active 前置 `>` 指示。"""
    try:
        idx = int(state.active_cluster_tab)
    except (TypeError, ValueError):
        idx = 0
    if idx < 0 or idx >= len(_CLUSTER_TAB_LABELS):
        idx = 0
    segs: list[str] = []
    for i, label in enumerate(_CLUSTER_TAB_LABELS):
        prefix = ">" if i == idx else " "
        segs.append(f"[{i}]{prefix}{label}")
    raw = "-- " + " ".join(segs) + " --"
    return Text(pad_width(raw, width), style=theme["title"])


def _keybar_cluster(width: int, theme: dict) -> Text:
    """底部 keybar：`Tab 切 section | ↑↓ 行内 | c 回 dashboard (Esc 同) | ? 帮助 | q 退`。"""
    label = "Tab 切 section | ↑↓ 行内 | c 回 dashboard (Esc 同) | ? 帮助 | q 退"
    return Text(pad_width(label, width), style=theme["keybar"])


def _status_style_map(status: str, theme: dict) -> str:
    """节点状态 → 主题色（online=success, offline=error, degraded=warning, 其他=dim）。"""
    if status == "online":
        return theme["success"]
    if status == "offline":
        return theme["error"]
    if status == "degraded":
        return theme["warning"]
    return theme["dim"]


def _nodes_section(cluster: ClusterSnapshot, width: int, theme: dict) -> Panel:
    """Tab 0「节点」：表头 + 节点表（聚合 goals → start/stop/ready）。

    空 nodes → 黄 Panel "(集群无节点 — 检查 role=center/both 是否允许)"（与 CLI 文案一致）。
    """
    nodes = cluster.nodes or []
    if not nodes:
        return Panel(
            Text(pad_width("(集群无节点 — 检查 role=center/both 是否允许)",
                           max(20, width - 4)), style=theme["warning"]),
            title="节点", title_align="left",
            width=width, border_style=theme["warning"],
        )

    # 按 cli._cmd_status_cluster 同口径聚合
    per: dict[str, dict[str, int]] = {}
    for g in cluster.goals:
        cell = per.setdefault(str(g.get("node_id", "")),
                              {"start": 0, "stop": 0, "ready": 0})
        intent = str(g.get("intent", "start"))
        if intent in ("start", "stop"):
            cell[intent] = cell.get(intent, 0) + 1
        if g.get("stage") == "READY":
            cell["ready"] += 1

    # 列宽：计算每节点实际所需列宽（拿每节点 max）
    rows: list[dict] = []
    for n in nodes:
        nid = str(n.get("node_id", "") or "")
        status = str(n.get("status", "") or "")
        lan = str(n.get("lan_id", "") or "") or "-"
        capacity = str(n.get("capacity_text", "") or "") or "-"
        c = per.get(nid, {})
        goal_str = f"{c.get('ready', 0)}/{c.get('start', 0)}" if c else "0/0"
        rows.append({
            "nid": nid, "status": status, "lan": lan, "capacity": capacity,
            "goal": goal_str,
        })

    # 列头
    col_node = max([display_width(r["nid"]) for r in rows] + [6]) + 2
    col_status = max([display_width(r["status"]) for r in rows] + [8]) + 2
    col_lan = max([display_width(r["lan"]) for r in rows] + [10]) + 2
    col_cap = max([display_width(r["capacity"]) for r in rows] + [10]) + 2
    col_goal = max([display_width(r["goal"]) for r in rows] + [14]) + 2

    header = (
        pad_width("node_id", col_node)
        + pad_width("状态", col_status)
        + pad_width("LAN", col_lan)
        + pad_width("容量", col_cap)
        + pad_width("goal(start 收敛/声明)", col_goal)
    )
    body: list[Text] = [Text(pad_width(header, max(20, width - 4)), style=theme["dim"])]
    for r in rows:
        status = r["status"]
        # 简单一致：整行取主题 dim，status 单独色（先拼 cell 再上色）
        parts: list[tuple[str, str]] = [
            (pad_width(r["nid"], col_node), theme["title"]),
            (pad_width(status, col_status),
             _status_style_map(status, theme)),
            (pad_width(r["lan"], col_lan), theme["dim"]),
            (pad_width(r["capacity"], col_cap), theme["dim"]),
            (pad_width(r["goal"], col_goal),
             theme["success"] if status == "online" else theme["dim"]),
        ]
        text = Text()
        for value, style in parts:
            text.append(value, style=style)
        # 追平 width（防短行 + 保 CJK 对齐）—— display_width 取纯文本
        plain = text.plain
        inner_w = max(20, width - 4)
        if display_width(plain) < inner_w:
            text.append(" " * (inner_w - display_width(plain)), style=theme["dim"])
        body.append(text)

    return Panel(Group(*body), title="节点", title_align="left", width=width)


def _stage_chain(stage: str) -> tuple[str, str]:
    """stage chain 8 格进度块：返回 (string, style) 元组。

    含义：
    - ✓ 已 done（i < cur）
    - > 当前（i == cur）
    - · 旁路（DEGRADED / FAILED / STOPPED）只画"走过"（cur >= 5）
    - 空格 = 未到

    顺序按 _STAGE_ORDER；`stage not in _STAGE_ORDER` 时 fallback 单段
    `f"{stage} (unknown)"` + warning 色（让未知值显眼）。
    """
    if stage not in _STAGE_ORDER:
        return f"{stage} (unknown)", "yellow"
    cur = _STAGE_ORDER.index(stage)
    chunks: list[str] = []
    for i, s in enumerate(_STAGE_ORDER):
        if i < cur:
            chunks.append("✓")
        elif i == cur:
            chunks.append(">")
        elif s in ("DEGRADED", "FAILED", "STOPPED") and cur >= 5:
            chunks.append("·")
        else:
            chunks.append(" ")
    return "[" + "][".join(chunks) + "]", "cyan"


def _goals_section(cluster: ClusterSnapshot, width: int, theme: dict) -> Panel:
    """Tab 1「goal」：goal 表按 profile 分组；stage chain 8 格进度块。

    空 goals → 黄 Panel "集群暂无 goal（中心台账为空）"（与 CLI 文案对齐）。
    闭合行：每个 profile 组的 port 序列合并显示 `ports = "-".join(p or "-" for p in ports)`。
    """
    goals = cluster.goals or []
    if not goals:
        return Panel(
            Text(pad_width("集群暂无 goal（中心台账为空）", max(20, width - 4)),
                 style=theme["warning"]),
            title="goal", title_align="left",
            width=width, border_style=theme["warning"],
        )

    # 按 profile 分组（与 CLI 顺序保持：按 group 字典序）
    grouped: dict[str, list[dict]] = {}
    for g in goals:
        grouped.setdefault(str(g.get("profile", "")), []).append(g)

    body: list[Text] = []
    for profile in sorted(grouped):
        rows = grouped[profile]
        # 组标题
        body.append(Text(pad_width(f"{profile}（{len(rows)} 节点）",
                                   max(20, width - 4)), style=theme["title"]))
        # 表头
        col_node = max([display_width(str(g.get("node_id", ""))) for g in rows] + [6]) + 2
        col_intent = max([display_width(str(g.get("intent", ""))) for g in rows] + [8]) + 2
        col_stage = 24
        col_state = max([display_width(str(g.get("state", "") or "")) for g in rows] + [8]) + 2
        col_port = max([display_width(str(g.get("port", "") or "")) for g in rows] + [5]) + 2
        header = (
            pad_width("节点", col_node)
            + pad_width("intent", col_intent)
            + pad_width("stage", col_stage)
            + pad_width("state", col_state)
            + "端口"
        )
        body.append(Text(pad_width(header, max(20, width - 4)), style=theme["dim"]))
        # 数据行
        for g in rows:
            nid = str(g.get("node_id", "") or "")
            intent = str(g.get("intent", "") or "")
            stage = str(g.get("stage", "") or "")
            state = str(g.get("state", "") or "") or "-"
            port = str(g.get("port", "") or "") or "-"
            stage_str, stage_style = _stage_chain(stage)
            line = Text()
            line.append(pad_width(nid, col_node), style=theme["title"])
            line.append(pad_width(intent, col_intent), style=theme["dim"])
            line.append(pad_width(stage_str, col_stage), style=stage_style)
            line.append(pad_width(state, col_state), style=theme["dim"])
            line.append(pad_width(port, col_port), style=theme["dim"])
            body.append(line)
        # 闭合行：ports = "-".join(p or "-" for p in ports)
        ports = [str(g.get("port", "") or "") or "-" for g in rows]
        ports_str = "-".join(ports)
        body.append(Text(
            pad_width(f"  ports: {ports_str}", max(20, width - 4)), style=theme["dim"]
        ))
        if profile != sorted(grouped)[-1]:
            body.append(Text("", style=theme["dim"]))

    return Panel(Group(*body), title="goal", title_align="left", width=width)


def _events_section(width: int, theme: dict) -> Panel:
    """Tab 2「事件」：T6 接事件流前为占位 Panel（不实际拉取）。

    ClusterSnapshot 不含 events 字段；T6 spec 再补 Snapshot 扩展。
    """
    return Panel(
        Text(pad_width("（T6 接事件流）", max(20, width - 4)), style=theme["warning"]),
        title="事件", title_align="left",
        width=width, border_style=theme["warning"],
    )


def _solo_stub(role: str, width: int, theme: dict) -> Panel:
    """SoloStub 顶部红 Panel：未接入 central 时引导用户接 center。"""
    label = "中央未接入（solo role：请在 cluster init 中使用 --center-url ...）"
    if role != "solo":
        # center 不可达但角色为 worker/controller：文案侧重"中心不可达"
        label = "中央未接入（中心不可达：检查 has-set CLUSTER_CENTER_URL / 网络 / 鉴权）"
    return Panel(
        Text(pad_width(label, max(20, width - 4)), style=theme["error"]),
        title="未接入", title_align="left",
        width=width, border_style=theme["error"],
    )


def render(
    state: TUIState,
    cluster: ClusterSnapshot,
    width: int,
    height: int,
    theme_id: str = "dark",
) -> Group:
    """渲染 Cluster 视图，返回 `rich.Group`。

    步骤：
    1. 主题 + 读 cluster_role()（lazy import 避免 core.tui → core.cluster 顶层环）
    2. SoloStub 触发条件（role==solo 或 center_visible==(中心不可达)）→
       `Group(summary, red stub, keybar)` 直返，跳过 3 section
    3. 否则 `Group(summary, tab_header, section, keybar)`：
       - active_cluster_tab 0 → _nodes_section
       - active_cluster_tab 1 → _goals_section
       - active_cluster_tab 2 → _events_section

    **不**调 subprocess / open('w') / os.kill。
    """
    theme = get_rich_theme(theme_id)
    try:
        from modelctl.core.cluster.config import cluster_role
        role = cluster_role()
    except Exception:  # noqa: BLE001 — config 读取失败 → 视 solo（无副作用路径上保持可见错误）
        role = "solo"

    summary = _summary_line(role, cluster, width, theme)
    keybar = _keybar_cluster(width, theme)

    # SoloStub 触发：solo role 或 center 不可达（统一红 Panel）
    if role == "solo" or cluster.center_visible == "(中心不可达)":
        stub = _solo_stub(role, width, theme)
        return Group(summary, stub, keybar)

    # Tab 渲染
    try:
        tab_idx = int(state.active_cluster_tab)
    except (TypeError, ValueError):
        tab_idx = 0
    if tab_idx < 0 or tab_idx >= len(_CLUSTER_TAB_LABELS):
        tab_idx = 0
    if tab_idx == 0:
        section = _nodes_section(cluster, width, theme)
    elif tab_idx == 1:
        section = _goals_section(cluster, width, theme)
    else:
        section = _events_section(width, theme)
    tab_header = _tab_header(state, width, theme)
    return Group(summary, tab_header, section, keybar)


__all__ = ["render"]
