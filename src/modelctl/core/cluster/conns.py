#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/conns.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : 节点连接世代表（同 node_id 后来者胜；M1 action/sync 投递的正确性前提）
# ===============================================================================

"""core/cluster/conns.py — WS 连接世代表（设计文档 §5.4"同 node_id 只允许一条连接"）。

M0 只有一条被动心跳通道，两条并存连接不会造成错误行为；M1 起中心随心跳 ack
投递 goal 快照与 start/stop 指令，僵尸连接会吞掉或重复消费这些指令。世代表
用"每连接一个递增 epoch + 心跳自证"解决：旧连接在下一轮心跳发现 epoch 已非
自己，主动结束循环——中心不需要持有连接对象，也就无需跨任务 send。
"""

from __future__ import annotations

import itertools
import threading


class ConnectionRegistry:
    """进程内连接世代登记。线程安全（WS 处理循环与 REST 线程都会访问）。"""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._mu = threading.Lock()
        self._current: dict[str, int] = {}

    def join(self, node_id: str) -> int:
        """登记一条新连接并返回其 epoch；同 node_id 的旧 epoch 即刻失效。"""
        with self._mu:
            epoch = next(self._counter)
            self._current[node_id] = epoch
        return epoch

    def is_current(self, node_id: str, epoch: int) -> bool:
        with self._mu:
            return self._current.get(node_id) == epoch

    def current_epoch(self, node_id: str) -> int | None:
        with self._mu:
            return self._current.get(node_id)

    def release(self, node_id: str, epoch: int) -> None:
        """连接结束时摘除。**仅当 epoch 属于自己**才删：旧连接的 finally 晚于
        新连接的 join 是常见时序，无条件删会把刚上线的 worker 误判为离线。
        """
        with self._mu:
            if self._current.get(node_id) == epoch:
                self._current.pop(node_id, None)

    def count(self) -> int:
        with self._mu:
            return len(self._current)
