#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/config.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : CLUSTER_* 集群配置读取
# ===============================================================================

"""core/cluster/config.py — 集群配置项读取（口径与全库一致：os.environ 就地读取，无集中 settings）。"""

from __future__ import annotations

import os

VALID_ROLES: tuple[str, ...] = ("solo", "worker", "control-plane", "both")

_DEFAULT_INTERVAL_S = 10
_DEFAULT_LEASE_S = 90


def cluster_role() -> str:
    """CLUSTER_ROLE；非法/未设回退 solo（现有部署零影响的关键闸门）。"""
    role = os.environ.get("CLUSTER_ROLE", "solo").strip().lower()
    return role if role in VALID_ROLES else "solo"


def is_center() -> bool:
    return cluster_role() in ("control-plane", "both")


def is_worker() -> bool:
    return cluster_role() in ("worker", "both")


def center_url() -> str:
    return os.environ.get("CLUSTER_CENTER_URL", "").strip()


def node_id() -> str:
    return os.environ.get("CLUSTER_NODE_ID", "").strip()


def lan_id() -> str:
    return os.environ.get("CLUSTER_LAN", "").strip()


def join_token() -> str:
    return os.environ.get("CLUSTER_JOIN_TOKEN", "").strip()


def node_token() -> str:
    return os.environ.get("CLUSTER_NODE_TOKEN", "").strip()


def _int_env(key: str, default: int, floor: int) -> int:
    raw = os.environ.get(key, "")
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= floor else default


def heartbeat_interval_s() -> int:
    return _int_env("CLUSTER_HEARTBEAT_INTERVAL_S", _DEFAULT_INTERVAL_S, floor=1)


def lease_s() -> int:
    return _int_env("CLUSTER_LEASE_S", _DEFAULT_LEASE_S, floor=5)


def ws_insecure() -> bool:
    return os.environ.get("CLUSTER_WS_INSECURE", "").strip() == "1"
