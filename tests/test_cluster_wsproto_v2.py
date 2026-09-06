#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_wsproto_v2.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : wsproto v2（sync/action/result/心跳扩展）编解码与输入消毒测试
# ===============================================================================

from modelctl.core.cluster import wsproto


def test_proto_version_bumped():
    assert wsproto.PROTO_VERSION == 2
    assert wsproto.make_hello("n", "l", "k", {})["v"] == 2


def test_make_sync_shape():
    assert wsproto.make_sync("rev-1", [{"goal_id": "a@@w-1"}]) == {
        "t": "sync", "revision": "rev-1", "goals": [{"goal_id": "a@@w-1"}]}


def test_make_action_and_result_shapes():
    assert wsproto.make_action(3, "start", goal_id="a@@w-1") == {
        "t": "action", "seq": 3, "action": "start", "goal_id": "a@@w-1", "profile": ""}
    assert wsproto.make_result(3, True, "ok") == {"t": "result", "seq": 3, "ok": True, "detail": "ok"}


def test_make_result_truncates_detail():
    assert len(wsproto.make_result(1, False, "x" * 900)["detail"]) == 500


def test_parse_action_sanitizes_types():
    assert wsproto.parse_action({"t": "action", "seq": "x", "action": 1, "goal_id": None}) == {
        "seq": 0, "action": "", "goal_id": "", "profile": ""}
    assert wsproto.parse_action("nope") == {"seq": 0, "action": "", "goal_id": "", "profile": ""}
    # bool 不是合法 seq（True == 1 会造成静默 seq 冲突）
    assert wsproto.parse_action({"seq": True})["seq"] == 0


def test_parse_ack_none_when_no_control_fields():
    """M0 中心的 ack 无控制字段：缺字段是常态，须回落默认而非抛错（滚动升级）。"""
    assert wsproto.parse_ack({"t": "ack"}) == {"seq": 0, "sync": None, "actions": []}
    assert wsproto.parse_ack("nope") == {"seq": 0, "sync": None, "actions": []}


def test_parse_ack_extracts_sync_and_actions():
    got = wsproto.parse_ack({"t": "ack", "seq": 5,
                             "sync": {"revision": "r", "goals": [], "pruned": ["x"]},
                             "actions": [{"t": "action", "seq": 9, "action": "stop", "goal_id": "a@@w"}]})
    assert got["seq"] == 5
    assert got["sync"]["revision"] == "r"
    assert got["actions"] == [{"seq": 9, "action": "stop", "goal_id": "a@@w", "profile": ""}]


def test_parse_ack_drops_malformed_entries():
    got = wsproto.parse_ack({"sync": ["not", "a", "dict"], "actions": ["junk", {"seq": 1}]})
    assert got["sync"] is None
    assert got["actions"] == [{"seq": 1, "action": "", "goal_id": "", "profile": ""}]


def test_parse_heartbeat_v2_defaults():
    """缺字段 → None（= 未知），**不得**回落到 {}/[]。空值是"worker 明确说没有"，
    与"旧版 worker 没上报该字段"必须可区分，否则中心会拿空值覆盖既有事实
    （profiles=None 而非 {}：否则旧版 worker 会把台账里在跑的模型全抹掉）。"""
    assert wsproto.parse_heartbeat_v2({}) == {
        "profiles": None, "goal_sync": None, "drift": None, "local_profiles": None,
        "capacity": None, "runtimes": None}
    assert wsproto.parse_heartbeat_v2(None) == {
        "profiles": None, "goal_sync": None, "drift": None, "local_profiles": None,
        "capacity": None, "runtimes": None}


def test_parse_heartbeat_v2_empty_list_means_explicit_empty():
    got = wsproto.parse_heartbeat_v2({"payload": {"local_profiles": [], "drift": []}})
    assert got["local_profiles"] == [] and got["drift"] == []


def test_parse_heartbeat_v2_keeps_whitelisted_shapes_only():
    got = wsproto.parse_heartbeat_v2({"t": "heartbeat", "payload": {
        "profiles": {"a": {"state": "READY", "port": 8101}, "b": "junk"},
        "goal_sync": {"revision": "r1", "drift": True},
        "drift": ["a@@w-1", 5],
        "local_profiles": ["qwen-vllm", 7],
        "capacity": {"gpu_count": 4}, "runtimes": ["junk"],
        "injected": "ignored"}})
    assert got["profiles"] == {"a": {"state": "READY", "port": 8101}}
    assert got["goal_sync"] == {"revision": "r1", "drift": True}
    assert got["drift"] == ["a@@w-1"]
    assert got["local_profiles"] == ["qwen-vllm"]   # gate 的 --create 判定依赖它
    assert got["capacity"] == {"gpu_count": 4}      # 改进 B：容量随心跳上报
    assert got["runtimes"] is None                  # 非 dict 消毒为 None（未知≠没有）


def test_parse_heartbeat_v2_accepts_top_level_for_rolling_upgrade():
    """M0 心跳把字段放 payload；对混版本对端，顶层位置也须能解。"""
    got = wsproto.parse_heartbeat_v2({"profiles": {"a": {"state": "READY"}}})
    assert got["profiles"] == {"a": {"state": "READY"}}


def test_parse_result_sanitizes_shape():
    assert wsproto.parse_result({"t": "result", "seq": 4, "ok": True, "detail": "已受理"}) == {
        "seq": 4, "ok": True, "detail": "已受理"}


def test_parse_result_ok_missing_is_unknown_not_false():
    """缺 ok 字段 → None（未知），不得折叠成 False：台账假报"指令失败"会误导排障。"""
    assert wsproto.parse_result({"t": "result", "seq": 1})["ok"] is None
    assert wsproto.parse_result({"t": "result", "seq": 1, "ok": "yes"})["ok"] is None


def test_parse_result_rejects_non_dict_and_bool_seq():
    assert wsproto.parse_result(None) == {"seq": 0, "ok": None, "detail": ""}
    assert wsproto.parse_result({"seq": True, "ok": True, "detail": 5}) == {
        "seq": 0, "ok": True, "detail": ""}


def test_opt_str_list_caps_each_entry_length():
    """条数封顶之外，单条也须截断（drift/local_profiles 元素是 worker 自由串）。"""
    long_item = "x" * 2000
    out = wsproto._opt_str_list([long_item, "ok"], limit=10)
    assert out == ["x" * 512, "ok"]
