#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_config.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : cluster.config 集群配置读取测试
# ===============================================================================
"""CLUSTER_* 配置读取：默认值、角色集合、非法值回退。"""
from __future__ import annotations

import pytest

from modelctl.core.cluster import config

CLUSTER_KEYS = [
    "CLUSTER_ROLE", "CLUSTER_CENTER_URL", "CLUSTER_NODE_ID", "CLUSTER_LAN",
    "CLUSTER_JOIN_TOKEN", "CLUSTER_NODE_TOKEN", "CLUSTER_HEARTBEAT_INTERVAL_S",
    "CLUSTER_LEASE_S", "CLUSTER_WS_INSECURE",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in CLUSTER_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_default_role_is_solo() -> None:
    assert config.cluster_role() == "solo"
    assert not config.is_center() and not config.is_worker()


def test_invalid_role_falls_back_to_solo(monkeypatch) -> None:
    monkeypatch.setenv("CLUSTER_ROLE", "BOSS")
    assert config.cluster_role() == "solo"


def test_both_is_center_and_worker(monkeypatch) -> None:
    monkeypatch.setenv("CLUSTER_ROLE", "BOTH")  # 大小写不敏感
    assert config.is_center() and config.is_worker()


def test_worker_only_is_not_center(monkeypatch) -> None:
    monkeypatch.setenv("CLUSTER_ROLE", "worker")
    assert config.is_worker() and not config.is_center()


def test_interval_and_lease_defaults_and_floor(monkeypatch) -> None:
    assert config.heartbeat_interval_s() == 10
    assert config.lease_s() == 90
    monkeypatch.setenv("CLUSTER_HEARTBEAT_INTERVAL_S", "0")   # 低于下限 → 回退
    monkeypatch.setenv("CLUSTER_LEASE_S", "abc")              # 非法 → 回退
    assert config.heartbeat_interval_s() == 10
    assert config.lease_s() == 90


def test_ws_insecure_flag(monkeypatch) -> None:
    assert not config.ws_insecure()
    monkeypatch.setenv("CLUSTER_WS_INSECURE", "1")
    assert config.ws_insecure()
