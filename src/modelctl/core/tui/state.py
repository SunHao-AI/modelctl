#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/state.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : TUI 全局可变态（视图/选中/过滤/排序/搜索/错误缓存）
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
