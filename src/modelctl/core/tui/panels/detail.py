#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/panels/detail.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/10 10:00
# @Desc   : Detail 视图 5 子 Tab 渲染（YAML/智能体配置/日志/速率/健康预检，CJK 对齐守）
# ===============================================================================

"""Detail 视图 5 子 Tab 渲染。

子 Tab（顺序固定，与 `state._DETAIL_TABS` 一致）：
- Tab 0 "YAML"：`profile.path.read_text()` 用 `rich.Syntax` 高亮 yaml；
  文件缺失/不可读 → 红 Panel "未找到 profile"
- Tab 1 "智能体配置"：`profile.engine_config` 字段表（pad_width 对齐）；
  缺省 → 黄 Panel "engine_config 未定义"
- Tab 2 "日志"：`LogsSnapshot.lines`（末 20 行）；launch_log None → "no log"；
  truncated 时顶部提示"（已截断，仅显示末 N 行）"
- Tab 3 "速率"：`models.profiles[active_index]` 的 rate_in/rate_out；
  None → "(not measured)"
- Tab 4 "健康预检"：`adapter.check_requirements()` 捕获 RequirementError
  → 红线只读异常消息列表；adapter 不存在 → 黄 "(无 adapter)"

渲染**不直接调** subprocess / open('w') / os.kill——`read_text()` 只读，
`check_requirements()` 仅做前置能力校验，无写副作用。
含 CJK 的输出逐段 pad_width（字级 CJK 双宽由 `display_width` / `pad_width` 兜）。
precheck 的 `Caps` 由 host 侧 `HardwareSnapshot.caps` 注入（T5-3）；本模块**不**
在正文内调 `probe()`。
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from modelctl.core.colors import pad_width
from modelctl.core.tui.data import HardwareSnapshot, LogsSnapshot, ModelsSnapshot
from modelctl.core.tui.panels.common import resolve_profile_from_apps as _resolve_profile_from_apps
from modelctl.core.tui.state import _DETAIL_TABS, TUIState
from modelctl.core.tui.theme import get_rich_theme

# Tab 标签（顺序与 _DETAIL_TABS 一致；header 用）
_TAB_LABELS = {
    "yaml": "YAML",
    "agent": "智能体配置",
    "log": "日志",
    "rate": "速率",
    "precheck": "健康预检",
}


def _header(state: TUIState, width: int, theme: dict) -> Text:
    """顶部标题行：`-- [0]YAML [1]智能体配置 [2]日志 [3]速率 [4]健康预检 --`。

    当前 active 子 Tab 前置 `>` 指示；pad_width 到 width（CJK 双宽由 pad_width 兜）。
    """
    segs: list[str] = []
    active = state.active_detail_subtab
    for i, key in enumerate(_DETAIL_TABS):
        label = _TAB_LABELS.get(key, key)
        prefix = ">" if key == active else " "
        segs.append(f"[{i}]{prefix}{label}")
    raw = "-- " + " ".join(segs) + " --"
    return Text(pad_width(raw, width), style=theme["title"])


def _keybar_detail(width: int, theme: dict) -> Text:
    """底部快捷键提示：Detail 视图专用。"""
    label = "Tab/Shift-Tab 切子Tab | Esc 回 dashboard | r 跟踪日志 | q 退 | ? 帮助 | t 切主题"
    return Text(pad_width(label, width), style=theme["keybar"])


def _no_profile_panel(width: int, theme: dict) -> Panel:
    """空 profile 列表占位：黄 "(无 profile)"。"""
    return Panel(
        Text(pad_width("(无 profile)", max(20, width - 4)), style=theme["warning"]),
        title="Detail", title_align="left",
        width=width, border_style=theme["warning"],
    )


def _render_yaml(profile, width: int, theme: dict) -> RenderableType:
    """YAML Tab：read_text + Syntax 高亮；文件缺失/不可读 → 红 Panel 未找到 profile。"""
    path = getattr(profile, "path", None)
    if path is None:
        return Panel(
            Text(pad_width("未找到 profile", max(20, width - 4)), style=theme["error"]),
            title="YAML", title_align="left",
            width=width, border_style=theme["error"],
        )
    try:
        content = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return Panel(
            Text(pad_width("未找到 profile", max(20, width - 4)), style=theme["error"]),
            title="YAML", title_align="left",
            width=width, border_style=theme["error"],
        )
    syn = Syntax(content or "(空)", "yaml", word_wrap=True, line_numbers=False)
    return Panel(syn, title="YAML", title_align="left", width=width)


def _render_agent(profile, width: int, theme: dict) -> RenderableType:
    """智能体配置 Tab：engine_config 字段表（pad_width 对齐）；缺 → 黄 Panel。"""
    cfg = getattr(profile, "engine_config", None)
    if not cfg or not isinstance(cfg, dict):
        return Panel(
            Text(pad_width("engine_config 未定义", max(20, width - 4)), style=theme["warning"]),
            title="智能体配置", title_align="left",
            width=width, border_style=theme["warning"],
        )
    # 字段表：`key = value` 逐行渲染，pad_width 对齐 key 列至"key 列最宽 CJK 双宽 + 2"。
    # 不强制整行 == width（CJK 长 value 可能超宽，逐行 superimpose 在 Panel 边框内即可）。
    def _key_col_w(k: str) -> int:
        from modelctl.core.colors import display_width  # 延迟 import 避免顶层
        return display_width(str(k))
    keys = list(cfg.keys())
    key_w = max((_key_col_w(k) for k in keys), default=0) + 2
    lines: list[Text] = []
    for k in keys:
        kstr = str(k)
        vstr = str(cfg[k])
        row = pad_width(kstr, key_w) + "=" + vstr
        lines.append(Text(row, style=theme["dim"]))
    return Panel(Group(*lines), title="智能体配置", title_align="left", width=width)


def _render_log(logs: LogsSnapshot, width: int, theme: dict) -> RenderableType:
    """日志 Tab：logs.lines 末 N 行；launch_log None → "no log"；truncated 提示。"""
    inner_w = max(20, width - 4)
    if not logs.lines:
        return Panel(
            Text(pad_width("no log", inner_w), style=theme["dim"]),
            title="日志", title_align="left",
            width=width, border_style=theme["dim"],
        )
    body: list[Text] = []
    if logs.truncated:
        hint = f"（已截断，仅显示末 {logs.tail} 行）"
        body.append(Text(pad_width(hint, inner_w), style=theme["warning"]))
    for line in logs.lines:
        body.append(Text(pad_width(line, inner_w), style=theme["dim"]))
    return Panel(Group(*body), title="日志", title_align="left", width=width)


def _render_rate(models: ModelsSnapshot, state: TUIState, width: int, theme: dict) -> RenderableType:
    """速率 Tab：models.profiles[active_index] 的 rate_in/rate_out；None → '(not measured)'。"""
    profiles = models.profiles or []
    if not profiles:
        return Panel(
            Text(pad_width("(无 profile)", max(20, width - 4)), style=theme["warning"]),
            title="速率", title_align="left",
            width=width, border_style=theme["warning"],
        )
    idx = state.active_index
    if idx < 0 or idx >= len(profiles):
        idx = 0
    item = profiles[idx]
    name = str(item.get("name", ""))
    rate_in = item.get("rate_in")
    rate_out = item.get("rate_out")
    inner_w = max(20, width - 4)
    if rate_in is None or rate_out is None:
        line_text = name + " : (not measured)"
        style = theme["dim"]
    else:
        line_text = f"{name} : {rate_in:.1f} in/s, {rate_out:.1f} out/s"
        style = theme["success"]
    return Panel(
        Text(pad_width(line_text, inner_w), style=style),
        title="速率", title_align="left",
        width=width,
    )


def _render_precheck(profile, width: int, theme: dict, caps: object | None = None) -> RenderableType:
    """健康预检 Tab：调 adapter.check_requirements 捕获 RequirementError。

    - 通过（无异常）→ 绿 "precheck passed"
    - RequirementError → 红线只读异常消息
    - 其他异常 → 黄 "precheck 异常：{type}：{msg}"
    - adapter 不存在（get_adapter 抛异常）→ 黄 "(无 adapter)"
    本函数调用时 profile 必须非 None（无 profile 不触发 precheck）。

    `caps`：`core.capabilities.Capabilities`，由 `render(caps=hw.caps)` 注入；
    None 时 adapter 走 fallback 路径（如 `Caps().gpu_count == 0`）。**不**
    在函数体内 `probe()`（T5-3：收敛到帧级每次一致，避免 Tab 切反复触发）。
    """
    inner_w = max(20, width - 4)
    try:
        # 延迟 import：避免 engines 模块在 core.tui.panels 加载时形成循环依赖
        from modelctl.engines import get_adapter
        from modelctl.engines.base import RequirementError
    except ImportError as e:  # pragma: no cover
        return Panel(
            Text(pad_width(f"导入异常：{e}", inner_w), style=theme["warning"]),
            title="健康预检", title_align="left",
            width=width, border_style=theme["warning"],
        )
    # 需求 1：先建 adapter 实例——`get_adapter(engine)` 拿到 cls
    try:
        engine = getattr(profile, "engine", "") or ""
        adapter_cls = get_adapter(engine)
    except Exception:  # noqa: BLE001 — 引擎未实现/未注册均归 "无 adapter"
        return Panel(
            Text(pad_width("(无 adapter)", inner_w), style=theme["warning"]),
            title="健康预检", title_align="left",
            width=width, border_style=theme["warning"],
        )
    try:
        adapter = adapter_cls(profile, caps)
        # 渲染路径只读：严禁清容器 / 抢 GPU 锁等写副作用（TUI-P1-1）
        adapter.check_requirements(readonly=True)
    except Exception as e:  # noqa: BLE001
        # RequirementError（软性条件不满足）→ 红；其他（硬性崩溃 / ImportError 等）→ 黄
        if isinstance(e, RequirementError):
            body = Text(pad_width(str(e), inner_w), style=theme["error"])
            border = theme["error"]
        else:
            body = Text(pad_width(f"precheck 异常：{type(e).__name__}：{e}", inner_w),
                        style=theme["warning"])
            border = theme["warning"]
        return Panel(body, title="健康预检", title_align="left",
                     width=width, border_style=border)
    body = Text(pad_width("precheck passed", inner_w), style=theme["success"])
    return Panel(body, title="健康预检", title_align="left",
                 width=width, border_style=theme["success"])


class _ProfileProxy:
    """render 内部使用的轻量 profile 信封。

    仅暴露 render 路径需要的只读属性：
    - `name` / `engine` / `port`：来自 `models.profiles[active_index]` dict 项
    - `engine_config` / `path`：来自真实 `Profile`（按 name 从 `list_profiles()` 绑定）
    避免上层 render 直接依赖 `modelctl.core.profile.Profile` 的类型提示。
    """

    def __init__(self, *,
                 name: str,
                 engine: str,
                 port: int,
                 engine_config: dict | None = None,
                 path=None) -> None:
        self.name = name
        self.engine = engine
        self.port = port
        self.engine_config = engine_config
        self.path = path


def _dispatch_body(state: TUIState, proxy: _ProfileProxy, hw: HardwareSnapshot,
                   models: ModelsSnapshot, logs: LogsSnapshot,
                   width: int, theme: dict,
                   caps: object | None = None) -> RenderableType:
    """按 state.active_detail_subtab 分发到具体 Tab 渲染。"""
    tab = state.active_detail_subtab
    if tab == "yaml":
        return _render_yaml(proxy, width, theme)
    if tab == "agent":
        return _render_agent(proxy, width, theme)
    if tab == "log":
        return _render_log(logs, width, theme)
    if tab == "rate":
        return _render_rate(models, state, width, theme)
    if tab == "precheck":
        return _render_precheck(proxy, width, theme, caps=caps)
    # 未识别 fallback 回 yaml（Tab 值域收敛由 switch_detail_tab 守；此处兜底防脏值）
    return _render_yaml(proxy, width, theme)


def render(
    state: TUIState,
    hw: HardwareSnapshot,
    models: ModelsSnapshot,
    logs: LogsSnapshot,
    width: int,
    height: int,
    theme_id: str = "dark",
    *,
    caps: object | None = None,
) -> Group:
    """渲染 Detail 视图，返回 `rich.Group`。

    步骤：
    1. 从 `theme_id` 取主题（`get_rich_theme` 未知回落 dark）
    2. 顶部 header：`-- [0]YAML [1]智能体配置 [2]日志 [3]速率 [4]健康预检 --`
    3. 中部：按 `state.active_detail_subtab` 分发到具体 Tab 渲染函数
       - profile 来自 `models.profiles[state.active_index]`（dict 项）
       - dict 项的 `name/engine/port` 兜底；`engine_config/path` 通过 `list_profiles()`
         按 name 绑真实 Profile（缺失 → 走各 Tab 的"未找到 profile" / "engine_config 未定义"
         降级分支）
    4. 底部 keybar：Detail 视图快捷键

    `caps`：`core.capabilities.Capabilities`，由 host 经 `hw.caps` 注入（T5-3）。
    仅在 precheck tab 路径使用；None 时 adapter 走 fallback 路径。

    **不**向本函数传 `profiles` 参数（T2 dashboard 接口一致性守）。
    """
    theme = get_rich_theme(theme_id)
    profiles = models.profiles or []
    if not profiles:
        proxy = None
    else:
        idx = state.active_index
        if idx < 0 or idx >= len(profiles):
            # 静默回卷：active_index 越界时重置为 0（与 dashboard 同语义）
            if idx >= len(profiles):
                state.active_index = 0
            idx = 0
        item = profiles[idx]
        name = str(item.get("name", "") or "")
        # 真实 dict 项的 engine 应为字符串；mock 测试态可能传 Mock —— 直接 str() 会返 "<Mock ...>"，
        # 用 type name 守 Mock 类以保留 precheck "无 adapter" 路径可走
        raw_engine = item.get("engine", "")
        if raw_engine is None:
            engine = ""
        elif isinstance(raw_engine, str):
            engine = raw_engine
        else:
            # Mock / 非常规对象，防御性降级为空字符串（precheck Tab 会触发 "无 adapter"）
            engine = ""
        raw_port = item.get("port") or 0
        try:
            port = int(raw_port)
        except (TypeError, ValueError):  # dict 项可能是 Mock / float 等，容错为 0
            port = 0
        # 从真实 Profile 取 engine_config / path（缺失不阻断，各 Tab 自降级）
        real = _resolve_profile_from_apps(name, engine, port)
        engine_config = getattr(real, "engine_config", None) if real is not None else None
        path = getattr(real, "path", None) if real is not None else None
        # 与 T2 同语义的轻量 proxy：暴露 name/engine/port + engine_config/path
        proxy = _ProfileProxy(
            name=name, engine=engine, port=port,
            engine_config=engine_config, path=path,
        )
    if proxy is None:
        body = _no_profile_panel(width, theme)
    else:
        body = _dispatch_body(state, proxy, hw, models, logs, width, theme,
                              caps=caps)
    header = _header(state, width, theme)
    keybar = _keybar_detail(width, theme)
    return Group(header, body, keybar)


__all__ = ["render"]
