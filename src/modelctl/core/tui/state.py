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


@dataclass
class TUIState:
    """TUI 全局可变态。

    各字段先定义骨架；过滤/排序/分页等业务方法在 Task 2 追加。
    快照缓存（caches）由 app.py / data.py 持有，不放在本状态里。
    """

    active_view: str = "dashboard"  # dashboard / detail / plan / cluster / monitor
    active_index: int = 0
    active_detail_subtab: int = 0
    plan_edit: dict = field(default_factory=dict)
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
