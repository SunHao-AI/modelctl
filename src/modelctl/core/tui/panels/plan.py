#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/panels/plan.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/10 10:00
# @Desc   : Plan 视图（字段表单 + KV 估算 + 引擎预检三态，CJK 对齐）
# ===============================================================================

"""Plan 视图渲染（Task 4 交付）。

布局：
- 顶部 header：`-- Plan: <profile_name> | [0]硬件 [1]KV估算 [2]precheck --`
- 中部 body（无 active profile 时占位）：字段表单 + 硬件 GPU 预览 + KV 估算 + precheck 四 Panel
- 底部 keybar：Plan 视图快捷键

字段表单按 `profile.engine_config` 字段动态拉行（dict 保序）：
- llamacpp：`ctx_size` / `n_gpu_layers` / `parallel` / `gpu_count`
- vllm：`max_model_len` / `tensor_parallel_size` / `kv_cache_dtype` / `max_num_seqs`
- sglang：`context_length` / `tensor_parallel_size`
- ollama：`context_length` / `num_parallel`
- unsloth：`max_model_len` / `tensor_parallel` / `batch_size`
- aphrodite / lmdeploy / tokenspeed：`max_model_len` / `tensor_parallel_size`
- 其他 engine：`engine_config` 全部 key（保序）
- 字段集合为空：黄 placeholder "(无 profile engine_config)"

dry-run 3 子 Panel（按 brief 顺序）：
1. 硬件资源预览：`GPU {count}x {name}` + 每卡 `gpu[{i}] free={f}/total={t}MB` + 总 free
2. vram_estimator 估算值：`kv_total_mb` / `per_card_mb` / `gpu_count` / `cache_dtype`；
   无法解析 → 黄 "(无法估算：KV 口径或架构无法解析)"
3. engine check_requirements 预检：成功 → 绿 "precheck passed"；
   RequirementError → 红 "FAIL: {msg}"；其他 → 黄 "precheck 异常：{type}：{msg}"；
   无 adapter → 黄 "(无 adapter)"

本函数**不直接**调 `subprocess` / `open('w')` / `os.kill` / `engine_config.launch`。
precheck 的 `caps` 参数由 host 侧 `HardwareSnapshot.caps` 注入（T5-3 收口，
与 T3 detail.py 经 `render(caps=hw.caps)` 注入同 pattern）；None 时 adapter 走
fallback 默认值路径，仍**不**在 panel 内 `probe()`。
含 CJK 的输出全部 `pad_width` 到 width（`display_width` 双宽打包兜）。
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from modelctl.core.colors import pad_width
from modelctl.core.tui.data import HardwareSnapshot, ModelsSnapshot
from modelctl.core.tui.panels.common import resolve_profile_from_apps as _resolve_profile_from_apps
from modelctl.core.tui.state import TUIState
from modelctl.core.tui.theme import get_rich_theme


def _header(state: TUIState, profile_name: str, width: int, theme: dict) -> Text:
    """顶部标题行：`-- Plan: <profile_name> | [0]硬件 [1]KV估算 [2]precheck --`。

    Plan 视图无子 Tab 切换（brief 仅约定 [0..2] 是分区标识），无 `>` 指示；
    pad_width 到 width（CJK 双宽由 pad_width 兜）。
    """
    raw = f"-- Plan: {profile_name or '(未在 dashboard 选中)'} | [0]硬件 [1]KV估算 [2]precheck --"
    return Text(pad_width(raw, width), style=theme["title"])


def _keybar_plan(width: int, theme: dict) -> Text:
    """Plan 视图底部快捷键：`Tab 切字段 | 方向 ±1 页 | D 预览 | ? 帮助 | Esc 回 dashboard | q 退`。"""
    label = "Tab 切字段 | 方向 ±1 页 | D 预览 | ? 帮助 | Esc 回 dashboard | q 退"
    return Text(pad_width(label, width), style=theme["keybar"])


def _profile_fields(profile) -> list[tuple[str, str]]:
    """按 engine 列出字段 `(key, value_str)` 元组（保序）。

    - llamacpp：`ctx_size` / `n_gpu_layers` / `parallel` / `gpu_count`
    - vllm：`max_model_len` / `tensor_parallel_size` / `kv_cache_dtype` / `max_num_seqs`
    - sglang：`context_length` / `tensor_parallel_size`
    - ollama：`context_length` / `num_parallel`
    - unsloth：`max_model_len` / `tensor_parallel` / `batch_size`
    - aphrodite / lmdeploy / tokenspeed：`max_model_len` / `tensor_parallel_size`
    - 其他 / 未匹配：fallback `engine_config` 全部 key（保序）
    - `engine_config` 为 None / 空 / 非 dict → 返回 []
    """
    ec = getattr(profile, "engine_config", None)
    if not isinstance(ec, dict) or not ec:
        return []
    engine = str(getattr(profile, "engine", "") or "")
    known: dict[str, list[str]] = {
        "llamacpp": ["ctx_size", "n_gpu_layers", "parallel", "gpu_count"],
        "vllm": ["max_model_len", "tensor_parallel_size", "kv_cache_dtype", "max_num_seqs"],
        "sglang": ["context_length", "tensor_parallel_size"],
        "ollama": ["context_length", "num_parallel"],
        "unsloth": ["max_model_len", "tensor_parallel", "batch_size"],
        "aphrodite": ["max_model_len", "tensor_parallel_size"],
        "lmdeploy": ["max_model_len", "tensor_parallel_size"],
        "tokenspeed": ["max_model_len", "tensor_parallel_size"],
    }
    keys = known.get(engine)
    if keys is None:
        keys = list(ec.keys())
    else:
        keys = [k for k in keys if k in ec] + [k for k in ec if k not in keys]
    out: list[tuple[str, str]] = []
    for k in keys:
        v = ec.get(k)
        out.append((k, "" if v is None else str(v)))
    return out


def _type_hint(value: str) -> str:
    """value 字符串 → 简单类型提示（`int` / `str` / `list` / `bool`）。

    仅用于表单右侧的 `(type_hint)` 标签：
    - 空串 / "None" → "(str)"
    - 纯数字（含负号） → "(int)"
    - 含 `[`(`]`) → "(list)"
    - "True" / "False" → "(bool)"
    - 其他/未知 → "(str)"
    """
    s = (value or "").strip()
    if not s or s == "None":
        return "str"
    if s in ("True", "False"):
        return "bool"
    if "[" in s and "]" in s:
        return "list"
    try:
        float(s)
        return "int" if "." not in s else "float"
    except (ValueError, TypeError):
        return "str"


def _render_form(state: TUIState, profile, width: int, theme: dict) -> Panel:
    """字段表单 Panel：每个字段行 `pad_width(cursor,3) + pad_width(name, key_w) + "=" + value + "  (type)"`。

    - 选中行（`state.plan_edit_cursor == i`）：cursor 列显示 `>`；主题 title 色
    - 非选中行：cursor 列为空格；主题 dim 色
    - `engine_config` 缺失：黄 "(无 profile engine_config)"
    """
    inner_w = max(20, width - 4)
    fields = _profile_fields(profile)
    if not fields:
        return Panel(
            Text(pad_width("(无 profile engine_config)", inner_w), style=theme["warning"]),
            title="字段", title_align="left",
            width=width, border_style=theme["warning"],
        )
    # 字段名宽度：`display_width` 兜底 CJK；`+ 2` 与 T3 detail 同 pattern
    from modelctl.core.colors import display_width
    key_w = max(display_width(k) for k, _v in fields) + 2
    cursor_w = 2
    body: list[Text] = []
    try:
        cursor = int(state.plan_edit_cursor)
    except (TypeError, ValueError):
        cursor = 0
    for i, (k, v) in enumerate(fields):
        hint = _type_hint(v)
        prefix = ">" if i == cursor else " "
        row = (
            pad_width(prefix, cursor_w)
            + pad_width(k, key_w)
            + "=" + v
            + "  (" + hint + ")"
        )
        body.append(Text(row, style=theme["title"] if i == cursor else theme["dim"]))
    return Panel(Group(*body), title="字段", title_align="left", width=width)


def _render_hw_preview(hw: HardwareSnapshot, width: int, theme: dict) -> Panel:
    """硬件 GPU 预览 Panel：`GPU {count}x {name}` + 每卡 `gpu[{i}]  free={f}MB / total={t}MB` + 总 free。

    无 GPU：黄 "(无 GPU 信息)" 占位。
    """
    inner_w = max(20, width - 4)
    gpus = hw.gpus or []
    if not gpus:
        return Panel(
            Text(pad_width("(无 GPU 信息)", inner_w), style=theme["warning"]),
            title="硬件", title_align="left",
            width=width, border_style=theme["warning"],
        )
    lines: list[Text] = []
    first_name = str((gpus[0] or {}).get("name", "") or "?")
    header = f"GPU {len(gpus)}x {first_name}"
    lines.append(Text(pad_width(header, inner_w), style=theme["title"]))
    for i, g in enumerate(gpus):
        f = int(g.get("free_mb", 0) or 0)
        t = int(g.get("total_mb", 0) or 0)
        lines.append(
            Text(pad_width(f"  gpu[{i}]  free={f}MB / total={t}MB", inner_w),
                 style=theme["dim"])
        )
    total_free = sum(int(g.get("free_mb", 0) or 0) for g in gpus)
    total_all = sum(int(g.get("total_mb", 0) or 0) for g in gpus)
    lines.append(
        Text(pad_width(f"总 free={total_free} / total={total_all} MB", inner_w),
             style=theme["success"])
    )
    return Panel(Group(*lines), title="硬件", title_align="left", width=width)


def _render_kv_estimate(profile, width: int, theme: dict) -> Panel:
    """vram_estimator 估算 Panel：`kv_total_mb=X | per_card_mb=Y | gpu_count=N | cache_dtype=D`。

    - `kv_estimate_for_profile(profile)` 返回 None → 黄 "(无法估算：KV 口径或架构无法解析)"
    - 抛异常 → 黄 "估算异常：{type}：{msg}"
    - 缺 profile → 黄 "(无 profile，跳过)"
    """
    inner_w = max(20, width - 4)
    if profile is None:
        return Panel(
            Text(pad_width("(无 profile，跳过)", inner_w), style=theme["dim"]),
            title="KV 估算", title_align="left",
            width=width, border_style=theme["dim"],
        )
    # lazy import：core.tui → core.vram_estimator 顶层环（T5 接力 T5-8）
    try:
        from modelctl.core.vram_estimator import kv_estimate_for_profile
        result = kv_estimate_for_profile(profile)
    except Exception as e:  # noqa: BLE001 — estimator 异常 / import 失败统一黄
        return Panel(
            Text(pad_width(f"估算异常：{type(e).__name__}：{e}", inner_w),
                 style=theme["warning"]),
            title="KV 估算", title_align="left",
            width=width, border_style=theme["warning"],
        )
    if not isinstance(result, dict):
        return Panel(
            Text(pad_width("(无法估算：KV 口径或架构无法解析)", inner_w),
                 style=theme["warning"]),
            title="KV 估算", title_align="left",
            width=width, border_style=theme["warning"],
        )
    lines = [
        Text(pad_width(f"kv_total_mb={result.get('kv_total_mb')}", inner_w),
             style=theme["success"]),
        Text(pad_width(f"per_card_mb={result.get('per_card_mb')}", inner_w),
             style=theme["success"]),
        Text(pad_width(f"gpu_count={result.get('gpu_count')}", inner_w),
             style=theme["success"]),
        Text(pad_width(f"cache_dtype={result.get('cache_dtype')}", inner_w),
             style=theme["success"]),
    ]
    return Panel(Group(*lines), title="KV 估算", title_align="left",
                 width=width, border_style=theme["success"])


def _render_precheck(profile, width: int, theme: dict, caps: object | None = None) -> RenderableType:
    """健康预检 Panel：对齐 T3 detail.py 三态逻辑（generalized prefix）。

    - RequirementError → 红 `FAIL: {msg}`
    - 其他 Exception → 黄 `precheck 异常：{type}：{msg}`
    - adapter 不存在 / import 失败 → 黄 `(无 adapter)`
    - 无异常 → 绿 `precheck passed`

    T5-3 收口：本函数**不** `probe()`；`caps` 由 `render(... caps=hw.caps)` 透传，
    None 时（数据层 hw 来自 mock / probe 失败）adapter 走 fallback 路径（如
    `Caps().gpu_count == 0` 触发 llamacpp RequirementError）。
    """
    inner_w = max(20, width - 4)
    try:
        from modelctl.engines import get_adapter
        from modelctl.engines.base import RequirementError
    except ImportError as e:  # pragma: no cover
        return Panel(
            Text(pad_width(f"导入异常：{e}", inner_w), style=theme["warning"]),
            title="预检", title_align="left",
            width=width, border_style=theme["warning"],
        )
    if profile is None:
        return Panel(
            Text(pad_width("(无 profile，跳过 precheck)", inner_w), style=theme["dim"]),
            title="预检", title_align="left",
            width=width, border_style=theme["dim"],
        )
    try:
        engine = str(getattr(profile, "engine", "") or "")
        adapter_cls = get_adapter(engine)
    except Exception:  # noqa: BLE001 — 引擎未实现/未注册
        return Panel(
            Text(pad_width("(无 adapter)", inner_w), style=theme["warning"]),
            title="预检", title_align="left",
            width=width, border_style=theme["warning"],
        )
    try:
        adapter = adapter_cls(profile, caps)
        # 渲染路径只读：严禁清容器 / 抢 GPU 锁等写副作用（TUI-P1-1）
        adapter.check_requirements(readonly=True)
    except Exception as e:  # noqa: BLE001
        if isinstance(e, RequirementError):
            body = Text(pad_width(f"FAIL: {e}", inner_w), style=theme["error"])
            border = theme["error"]
        else:
            body = Text(pad_width(f"precheck 异常：{type(e).__name__}：{e}", inner_w),
                        style=theme["warning"])
            border = theme["warning"]
        return Panel(body, title="预检", title_align="left",
                     width=width, border_style=border)
    body = Text(pad_width("precheck passed", inner_w), style=theme["success"])
    return Panel(body, title="预检", title_align="left",
                 width=width, border_style=theme["success"])


def _empty_panel(width: int, theme: dict) -> Panel:
    """无 profile 占位 Panel：黄 "(无 profile)"。"""
    inner_w = max(20, width - 4)
    return Panel(
        Text(pad_width("(无 profile)", inner_w), style=theme["warning"]),
        title="Plan", title_align="left",
        width=width, border_style=theme["warning"],
    )


def render(
    state: TUIState,
    hw: HardwareSnapshot,
    models: ModelsSnapshot,
    width: int,
    height: int,
    theme_id: str = "dark",
    *,
    caps: object | None = None,
) -> Group:
    """渲染 Plan 视图，返回 `rich.Group`。

    步骤：
    1. 主题
    2. 从 `models.profiles[state.active_index]` 取 active profile dict 项
    3. 通过 `_resolve_profile_from_apps` 绑真实 Profile（缺失 → 各 Panel 自降级）
    4. 中部 4 Panel：字段表单 / 硬件 / KV 估算 / 预检
    5. header / keybar

    `caps`：`core.capabilities.Capabilities` 对象，由 host 经 `hw.caps` 注入；
    None 时 `_render_precheck` 不传 `Caps` 给 adapter（与 detail.py 一致：
    adapter 侧按 `Caps().gpu_count == 0` 走 fallback 路径）。
    precheck 块内**不** `probe()`（收敛到帧级每次一致）。

    **不**调 subprocess / open('w') / os.kill / engine_config.launch。
    """
    theme = get_rich_theme(theme_id)
    profiles = models.profiles or []
    profile = None
    profile_name = ""
    if profiles:
        idx = state.active_index
        if idx < 0 or idx >= len(profiles):
            if idx >= len(profiles):
                state.active_index = 0
            idx = 0
        item = profiles[idx]
        profile_name = str(item.get("name", "") or "")
        raw_engine = item.get("engine", "")
        if raw_engine is None:
            engine = ""
        elif isinstance(raw_engine, str):
            engine = raw_engine
        else:
            engine = ""
        raw_port = item.get("port") or 0
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            port = 0
        profile = _resolve_profile_from_apps(profile_name, engine, port)
    if profile is None:
        header = _header(state, profile_name or "(无 profile)", width, theme)
        body = _empty_panel(width, theme)
        keybar = _keybar_plan(width, theme)
        return Group(header, body, keybar)
    form = _render_form(state, profile, width, theme)
    hw_p = _render_hw_preview(hw, width, theme)
    kv = _render_kv_estimate(profile, width, theme)
    pre = _render_precheck(profile, width, theme, caps=caps)
    header = _header(state, profile_name, width, theme)
    keybar = _keybar_plan(width, theme)
    return Group(header, form, hw_p, kv, pre, keybar)


__all__ = ["render"]
