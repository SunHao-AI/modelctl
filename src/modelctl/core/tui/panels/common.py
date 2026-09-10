#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/panels/common.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/10 10:00
# @Desc   : TUI 面板共享 helper（resolve_profile_from_apps 跨 detail/plan/cluster 三处复用）
# ===============================================================================

"""TUI 面板共享 helper（Task 5 抽取）。

`resolve_profile_from_apps` 抽自 T3 detail.py 与 T4 plan.py 的本地副本——
两处 lazy import list_profiles() 按 name 精确匹配的语义完全一致，T5-9 接力项统一收口。

detail / plan 渲染入口仍按 name 匹配（不接 engine / port 三段式校验，匹配语义与 T3/T4 一致，
避免双份实现漂移）。`engine` / `port` 形参为 T5 接口固定保留位，暂未使用（后续键盘派发
做 Tab/Shift-Tab 前的字段校验时再启用）。
"""

from __future__ import annotations


def resolve_profile_from_apps(
    name: str,
    engine: str = "",
    port: int | None = None,
    *,
    profiles_index: list[dict] | None = None,
) -> object | None:
    """按 `name` 从 `list_profiles()` 查真实 Profile；未命中 / 离线 → None。

    lazy import 避免 `modelctl.core.profile` 顶层引入循环依赖；
    `list_profiles()` 抛任何异常则静默返回 None（与 data.py 同语义）。
    """
    if not name:
        return None
    try:
        from modelctl.core.profile import list_profiles
    except ImportError:  # pragma: no cover — 极端情况
        return None
    try:
        for p in list_profiles():
            if getattr(p, "name", None) == name:
                return p
    except Exception:  # noqa: BLE001 — scan 失败 / 网络不可达降级
        return None
    return None


__all__ = ["resolve_profile_from_apps"]
