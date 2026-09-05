#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_conns.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : 连接世代表（后来者胜 / release 幂等 / 跨节点互不影响）
# ===============================================================================

from modelctl.core.cluster.conns import ConnectionRegistry


def test_join_returns_monotonic_epochs():
    reg = ConnectionRegistry()
    first = reg.join("w-1")
    second = reg.join("w-1")
    assert second > first


def test_new_join_supersedes_old_epoch():
    """worker 重启后旧进程的连接必须被判非当前，从而停止消费 ack（避免双份执行）。"""
    reg = ConnectionRegistry()
    old = reg.join("w-1")
    new = reg.join("w-1")
    assert reg.is_current("w-1", new) and not reg.is_current("w-1", old)


def test_nodes_are_isolated():
    reg = ConnectionRegistry()
    a = reg.join("w-1")
    b = reg.join("w-2")
    assert reg.is_current("w-1", a) and reg.is_current("w-2", b)
    assert not reg.is_current("w-1", b)


def test_unknown_node_or_epoch_is_never_current():
    reg = ConnectionRegistry()
    assert not reg.is_current("ghost", 1)
    assert reg.current_epoch("ghost") is None


def test_release_only_clears_own_epoch():
    """旧连接的 finally 绝不能把新连接踢下线（否则会误杀活着的 worker）。"""
    reg = ConnectionRegistry()
    old = reg.join("w-1")
    new = reg.join("w-1")
    reg.release("w-1", old)
    assert reg.is_current("w-1", new) and reg.count() == 1
    reg.release("w-1", new)
    assert reg.current_epoch("w-1") is None and reg.count() == 0


def test_release_is_idempotent_and_tolerates_unknown():
    reg = ConnectionRegistry()
    e = reg.join("w-1")
    reg.release("w-1", e)
    reg.release("w-1", e)          # 二次释放不抛
    reg.release("ghost", 99)       # 未知节点不抛
    assert reg.count() == 0


def test_rejoin_after_release_advances_epoch():
    reg = ConnectionRegistry()
    e1 = reg.join("w-1")
    reg.release("w-1", e1)
    e2 = reg.join("w-1")
    assert e2 > e1 and reg.is_current("w-1", e2)


def test_concurrent_joins_keep_exactly_one_current():
    """并发 join 不得出现两条同时 current（否则指令会被双投）。"""
    import threading

    reg = ConnectionRegistry()
    epochs: list[int] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        epochs.append(reg.join("w-1"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for e in epochs if reg.is_current("w-1", e)) == 1
    assert reg.count() == 1
