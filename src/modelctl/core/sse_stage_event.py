#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/sse_stage_event.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : Docker 一键安装 SSE 阶段事件统一数据契约
# ===============================================================================

"""core/sse_stage_event.py — Docker 一键安装 SSE 阶段事件数据契约（单一事实来源）。

Docker Desktop 一键安装（windows_setup.run_install）与后端 SSE 端点共享同一
`StageEvent` dataclass：emit 端按 frozen=True 防止中途篡改，订阅端按
`to_sse_dict()` 输出 dict —— None 字段被剥掉，避免向后端 body 注入 null。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StageEvent:
    """SSE 阶段事件的最小数据契约（12 值 stage 枚举见 windows_setup.STAGES）。

    字段约定：
    - type: "stage" | "log" | "error" | "complete"
    - stage: 12 值枚举，详见 windows_setup.STAGES
    - message: 人类可读中文；type=log 时是原始 stdout 一行
    - ts: "YYYY-MM-DD HH:mm:ss"
    - code: 仅 error / complete 时给
    - payload: post_install_plan 给 {"steps": [...]}；error 给 {"tail": [...]}
    """

    type: str
    stage: str
    message: str
    ts: str
    code: int | None = None
    payload: dict | None = None

    def to_sse_dict(self) -> dict:
        """序列化为 SSE body dict；None 字段不输出。"""
        out: dict = {
            "type": self.type,
            "stage": self.stage,
            "message": self.message,
            "ts": self.ts,
        }
        if self.code is not None:
            out["code"] = self.code
        if self.payload is not None:
            out["payload"] = self.payload
        return out
