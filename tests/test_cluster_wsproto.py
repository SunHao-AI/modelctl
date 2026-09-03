#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_wsproto.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : cluster.wsproto 消息编解码测试
# ===============================================================================
from __future__ import annotations

from modelctl.core.cluster import wsproto


def test_hello_roundtrip() -> None:
    msg = wsproto.make_hello("w-210", "lan-2", "NT-x", {"host_ip": "10.0.0.5"})
    assert msg["t"] == "hello" and msg["v"] == wsproto.PROTO_VERSION
    assert wsproto.parse_type(wsproto.dumps(msg)) == "hello"
    h = wsproto.parse_hello(msg)
    assert h.node_id == "w-210" and h.key == "NT-x" and h.meta["host_ip"] == "10.0.0.5"


def test_welcome_and_heartbeat() -> None:
    w = wsproto.make_welcome("NT-new", 10, 90)
    assert w["t"] == "welcome" and w["node_token"] == "NT-new" and w["lease_s"] == 90
    hb = wsproto.make_heartbeat({"profiles": {}, "gpu": {}})
    assert hb["t"] == "heartbeat"
    assert wsproto.parse_heartbeat(hb) == {"profiles": {}, "gpu": {}}


def test_parse_type_invalid() -> None:
    assert wsproto.parse_type("not-json") == ""
    assert wsproto.parse_type("[1,2]") == ""
    assert wsproto.parse_type('{"no_type":1}') == ""


def test_parse_type_deep_nesting_is_not_fatal() -> None:
    """深嵌套 JSON 帧触发 RecursionError，必须吞掉返回空串，不得击穿中心。"""
    assert wsproto.parse_type("[" * 6000) == ""


def test_event_and_error() -> None:
    assert wsproto.make_event("model.up", {"profile": "q"})["kind"] == "model.up"
    assert wsproto.make_error("bad token")["t"] == "error"
