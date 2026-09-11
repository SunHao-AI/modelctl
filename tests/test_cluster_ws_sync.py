#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_ws_sync.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/11 10:00
# @Desc   : WS 消息循环同步 SQLite 卸载回归测试（CLU-P1-1）
# ===============================================================================

"""WS 消息处理中的同步 SQLite 必须卸载到线程，否则逐条 commit 头阻塞整个 loop。"""
from __future__ import annotations

import asyncio


def test_slow_db_call_does_not_block_other_tasks():
    """模拟 append_event 慢 100ms：期间同 loop 的其他任务必须照常推进。"""
    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    def slow_append(*a, **k):
        import time
        time.sleep(0.1)

    async def main():
        t = asyncio.ensure_future(ticker())
        await asyncio.to_thread(slow_append)  # 基线：to_thread 下 ticker 正常跑
        await t

    asyncio.run(main())
    assert ticks >= 5  # 基线 sanity：to_thread 语义本身成立

    # 静态护栏：ws_cluster 的 heartbeat/event/result 分支体内不允许出现
    # 未卸载的同步 DB 调用（只允许 `asyncio.to_thread(...)` 形式）。
    import inspect

    import modelctl.core.webui.admin_cluster as ac

    src = inspect.getsource(ac.ws_cluster)
    for call in ("handle_heartbeat(", "append_event(", "_sweep_if_due("):
        for idx, line in enumerate(src.splitlines()):
            stripped = line.strip()
            if call in stripped and "to_thread" not in stripped and not stripped.startswith("#"):
                raise AssertionError(f"ws_cluster 存在未卸载的同步调用: L{idx}: {stripped}")
