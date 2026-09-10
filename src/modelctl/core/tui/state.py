#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/state.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : TUI 全局可变态（视图/选中/过滤/排序/搜索/错误缓存 + 过滤/排序/分页方法）
# ===============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Detail 视图 5 子 Tab（顺序固定：yaml → agent → log → rate → precheck）
# 类型收窄：仅在 {yaml, agent, log, rate, precheck} 间切换（T5 接力项 T5-2）
_DetailTabKey = Literal["yaml", "agent", "log", "rate", "precheck"]
_DETAIL_TABS: tuple[_DetailTabKey, ...] = ("yaml", "agent", "log", "rate", "precheck")

# Cluster 视图 3 子 Tab（顺序固定：nodes → goals → events）
_CLUSTER_TABS = ("nodes", "goals", "events")


@dataclass
class TUIState:
    """TUI 全局可变态。

    各字段先定义骨架；过滤/排序/分页等业务方法在 Task 2 追加，
    `switch_detail_tab` 在 Task 3 追加（Detail 视图子 Tab 切换）。
    快照缓存（caches）由 app.py / data.py 持有，不放在本状态里。
    """

    active_view: str = "dashboard"  # dashboard / detail / plan / cluster / monitor
    active_index: int = 0
    active_detail_subtab: _DetailTabKey = "yaml"  # yaml / agent / log / rate / precheck
    active_cluster_tab: int = 0  # Cluster 视图 section 切（0=nodes / 1=goals / 2=events）
    plan_edit: dict = field(default_factory=dict)
    plan_edit_cursor: int = 0  # Plan 视图当前编辑字段 index（cycle_plan_cursor 维护）
    plan_dry_run_done: bool = False  # 用户按过 D 键（重绘保留 dry-run 状态，不回滚）
    filter_status: str = "all"
    filter_engine: str = "all"
    sort_key: str = "name"
    search: str = ""
    page: int = 0
    errors: list = field(default_factory=list)

    # ─── 过滤 / 排序 / 分页（Task 2 追加，dashboard 渲染前置处理）───

    def filter_candidates(self, profiles: list[dict]) -> list[dict]:
        """按 filter_status / filter_engine / search 组合过滤，返回剩余清单。

        `filter_status` 值域：`{"all","running","stopped"}`；
        `filter_engine` 值域：`{"all"} | ENGINE_NAME`（如 llamacpp / vllm）；
        `search` 为子串小写比较（区分大小写不敏感）。
        """
        out = profiles
        if self.filter_status != "all":
            out = [p for p in out if p.get("status") == self.filter_status]
        if self.filter_engine != "all":
            out = [p for p in out if p.get("engine") == self.filter_engine]
        if self.search:
            key = self.search.lower()
            out = [p for p in out if key in str(p.get("name", "")).lower()]
        return out

    def sort_profiles(self, profiles: list[dict]) -> list[dict]:
        """按 sort_key 排序；缺省 "name"。

        全字段按字典序（小写）排序；port 等数值字段按 str 比较——dashboard
        不为特殊列做数值排序，数值序需求留给后续 Task 3 升格 row 类型。
        """
        key = self.sort_key or "name"
        return sorted(profiles, key=lambda p: (str(p.get(key, "")) or "").lower())

    def apply_page(self, profiles: list[dict], page_size: int = 10) -> list[dict]:
        """返回当前页（page 越界回第一页或空列表）。

        `page_size <= 0` 视为关闭分页，返回全量。
        """
        if page_size <= 0:
            return profiles
        start = max(0, self.page) * page_size
        return profiles[start:start + page_size]

    def switch_detail_tab(self, key: _DetailTabKey) -> None:
        """切 Detail 子 Tab。key ∈ {'yaml','agent','log','rate','precheck'}；越界 no-op。"""
        if key in _DETAIL_TABS:
            self.active_detail_subtab = key

    def cycle_cluster_tab(self, direction: int, count: int = 3) -> None:
        """Cluster 视图 section ±1 回绕（direction=0 / count=0 → no-op）。

        `direction` ∈ {-1, 0, +1}；`count` = section 数（默认 3，对齐
        _CLUSTER_TABS (nodes / goals / events)）。`count == 0` ⇒ no-op；
        越界自动取模回绕，结果始终在 [0, count) 区间。
        """
        if count <= 0 or direction == 0:
            return
        self.active_cluster_tab = (self.active_cluster_tab + direction) % count

    def cycle_plan_cursor(self, direction: int, count: int) -> None:
        """Plan 字段光标 ±1 回绕（不会被调用方向为 0 / count 为 0）。

        `direction` ∈ {-1, 0, +1}；`count` = 字段总数（由调用侧按
        `len(_profile_fields(profile))` 注入）。`count == 0` ⇒ no-op；
        越界自动取模回绕，结果始终在 [0, count) 区间。
        """
        if count <= 0 or direction == 0:
            return
        self.plan_edit_cursor = (self.plan_edit_cursor + direction) % count
