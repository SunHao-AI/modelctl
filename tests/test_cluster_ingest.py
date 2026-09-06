#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_ingest.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : 中心心跳回流（容量/模型状态/stage/漂移）与 ack 组装（sync/action）
# ===============================================================================

import pytest

from modelctl.core.cluster import wsproto
from modelctl.core.cluster.goals import GoalService
from modelctl.core.cluster.nodes import NodeRegistry
from modelctl.core.cluster.store import ClusterStore

YAML = "port: 8101\nengine_config:\n  tensor_parallel_size: 2\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    store = ClusterStore(tmp_path / "m.db")
    store.init_db()
    models = tmp_path / "models"
    (models / "vllm").mkdir(parents=True)
    (models / "vllm" / "qwen.yaml").write_text(YAML, encoding="utf-8")
    monkeypatch.setattr("modelctl.core.cluster.goals.MODELS_DIR", models)
    goals = GoalService(store)
    reg = NodeRegistry(store, goals=goals)
    store.upsert_node(node_id="w-1", node_token="NT-1", lan_id="lan-1", role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    # gate._verdict_one 的 runtimes 检查排在容量之前：runtimes=None 直接 SKIP
    # （"未上报≠可用"是 Task 4 钉死的保守口径）。set_goals 类用例必须先有
    # runtimes/容量，与 Task 11 计划夹具同口径。
    store.update_node_capacity("w-1", capacity={"gpu_count": 4, "vram_total_mb": 157280},
                               runtimes={"vllm": {"ok": True}}, local_profiles=["qwen"])
    return store, goals, reg


def _hb(**over):
    """构造 parse_heartbeat_v2 之后的形状（中心只见消毒过的形状）。"""
    raw = {"payload": {"profiles": {}, "local_profiles": ["qwen"],
                       "capacity": {"gpu_count": 4, "vram_total_mb": 157280},
                       "runtimes": {"vllm": {"ok": True}}, "goal_sync": {"revision": ""}}}
    raw["payload"].update(over)
    return wsproto.parse_heartbeat_v2(raw)


def test_heartbeat_returns_ack_dict_and_touches_lease(env):
    store, _goals, reg = env
    ack = reg.handle_heartbeat("w-1", _hb(), now=100.0)
    assert ack["t"] == "ack"
    assert store.get_node("w-1")["status"] == "online"


def test_heartbeat_persists_capacity_and_runtimes(env):
    store, _goals, reg = env
    reg.handle_heartbeat("w-1", _hb(), now=100.0)
    node = store.get_node("w-1")
    assert node["capacity"]["gpu_count"] == 4
    assert node["runtimes"]["vllm"]["ok"] is True
    assert node["local_profiles"] == ["qwen"]


def test_missing_sections_do_not_wipe_known_facts(env):
    """旧版 worker 不上报 capacity：中心必须保留既有值，而非写空。"""
    store, _goals, reg = env
    reg.handle_heartbeat("w-1", _hb(), now=100.0)
    reg.handle_heartbeat("w-1", wsproto.parse_heartbeat_v2({}), now=110.0)
    assert store.get_node("w-1")["capacity"]["gpu_count"] == 4


def test_ack_carries_sync_when_revision_differs(env):
    store, goals, reg = env
    reg.handle_heartbeat("w-1", _hb(), now=100.0)
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    ack = reg.handle_heartbeat("w-1", _hb(), now=110.0)
    assert ack["sync"]["goals"][0]["goal_id"] == "qwen@@w-1"
    assert store.get_node("w-1")["last_goal_sync_sha"] == ack["sync"]["revision"]


def test_ack_omits_sync_when_revision_matches(env):
    """revision 一致就不带快照：否则每 10s 白传一份 YAML 全集。"""
    store, goals, reg = env
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    reg.handle_heartbeat("w-1", _hb(), now=100.0)
    rev = goals.snapshot_for("w-1")["revision"]
    ack = reg.handle_heartbeat("w-1", _hb(goal_sync={"revision": rev}), now=110.0)
    assert "sync" not in ack


def test_ack_records_worker_stage_back_to_goal(env):
    store, goals, reg = env
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    reg.handle_heartbeat("w-1", _hb(profiles={"qwen": {"stage": "READY", "state": "READY",
                                                       "port": 8101, "gpu": [0, 1]}}), now=120.0)
    g = store.get_goal("qwen@@w-1")
    assert g["stage"] == "READY"
    assert store.list_model_states(node_id="w-1")[0]["port"] == 8101


def test_ack_records_failed_stage_with_error_class(env):
    store, goals, reg = env
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    reg.handle_heartbeat("w-1", _hb(profiles={"qwen": {
        "stage": "FAILED", "state": "down", "reason": "venv 缺失", "error_class": "venv_missing"}}),
        now=120.0)
    g = store.get_goal("qwen@@w-1")
    assert g["stage"] == "FAILED" and g["error_class"] == "venv_missing"


def test_stage_of_unmanaged_profile_is_ignored(env):
    """worker 本机自跑的模型（中心无 goal）只进 model_states，不得污染任何 goal 阶段。"""
    store, goals, reg = env
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    reg.handle_heartbeat("w-1", _hb(profiles={"local-only": {"stage": "READY"}}), now=120.0)
    assert store.get_goal("qwen@@w-1")["stage"] == "PENDING_PROFILE_SYNC"
    assert [r["profile"] for r in store.list_model_states(node_id="w-1")] == ["local-only"]


def test_drift_reported_becomes_event(env):
    store, goals, reg = env
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    reg.handle_heartbeat("w-1", _hb(drift=["qwen@@w-1"]), now=130.0)
    kinds = [e["kind"] for e in store.recent_events(limit=20, node_id="w-1")]
    assert "goal.drift" in kinds


def test_drift_event_is_not_duplicated_every_beat(env):
    """漂移是持续状态而非事件：只在"无→有"时记一次，否则 events 表每 10s 涨一条。"""
    store, goals, reg = env
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    reg.handle_heartbeat("w-1", _hb(drift=["qwen@@w-1"]), now=130.0)
    reg.handle_heartbeat("w-1", _hb(drift=["qwen@@w-1"]), now=140.0)
    kinds = [e["kind"] for e in store.recent_events(limit=50, node_id="w-1")]
    assert kinds.count("goal.drift") == 1


def test_force_sync_survives_offline_until_next_online_heartbeat(env):
    """mark_force_sync 后节点离线：标记必须在进程内存活到它下次上线的首枚心跳。

    这是 REST 强制同步对离线节点仍有意义的全部依据——本地文件被改坏时，人最想立刻
    修好的正是还没连回来的那台。变异：`mark_force_sync` 里加在线校验（或心跳前丢弃
    标记）→ 本钉的 sync/force 断言即红；把 `_consume_force_sync` 改成不清标记 → 第三拍
    （revision 已一致）仍带快照，即末条断言红。
    """
    _store, goals, reg = env
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    rev = goals.snapshot_for("w-1")["revision"]
    reg.mark_force_sync("w-1")
    reg.store.set_node_status("w-1", "offline")            # 打标后掉线
    ack = reg.handle_heartbeat("w-1", _hb(goal_sync={"revision": rev}), now=200.0)
    assert ack["sync"]["force"] is True and ack["sync"]["revision"] == rev
    assert "sync" not in reg.handle_heartbeat(            # 一次性：下一拍恢复不带
        "w-1", _hb(goal_sync={"revision": rev}), now=210.0)


def test_push_action_is_delivered_once(env):
    _store, _goals, reg = env
    assert reg.push_action("w-1", "start", goal_id="qwen@@w-1") is True
    frames = reg.drain_actions("w-1")
    assert frames[0]["action"] == "start" and frames[0]["goal_id"] == "qwen@@w-1"
    assert reg.drain_actions("w-1") == []


def test_queued_action_reaches_ack(env):
    _store, _goals, reg = env
    reg.push_action("w-1", "stop", goal_id="qwen@@w-1")
    ack = reg.handle_heartbeat("w-1", _hb(), now=100.0)
    assert ack["actions"][0]["action"] == "stop"


def test_action_queue_is_capped(env):
    """离线节点长时间不收指令时队列必须有上限，否则内存被单个节点撑爆。"""
    _store, _goals, reg = env
    for i in range(40):
        reg.push_action("w-1", "retry", goal_id=f"g{i}")
    assert len(reg.drain_actions("w-1")) <= 16


def test_push_action_unknown_node_still_queued(env):
    """节点暂离线也要入队：中心重启后 worker 回连时才拿得到 pending retry。"""
    _store, _goals, reg = env
    assert reg.push_action("ghost", "start", goal_id="x@@ghost") is True


def test_heartbeat_for_unknown_node_returns_ack_without_crash(env):
    reg = env[2]
    ack = reg.handle_heartbeat("ghost", _hb(), now=1.0)
    assert ack["t"] == "ack"


def test_profiles_none_keeps_model_states(env):
    """profiles=None（旧版 worker 未上报）绝不抹 model_states：这是三态落库的
    核心不变量，M0 worker 每 10s 心跳一次，写空 = dashboard 集体假 down。"""
    store, _goals, reg = env
    reg.handle_heartbeat("w-1", _hb(profiles={"qwen": {"stage": "READY"}}), now=100.0)
    assert store.list_model_states(node_id="w-1")
    reg.handle_heartbeat("w-1", wsproto.parse_heartbeat_v2({}), now=110.0)
    assert [r["profile"] for r in store.list_model_states(node_id="w-1")] == ["qwen"]


def test_drain_seq_never_reuses_numbers(env):
    """跨 drain 的 seq 必须递增：撞号会让 worker 把新指令当旧回执直接丢弃。"""
    _store, _goals, reg = env
    reg.push_action("w-1", "start", goal_id="g1")
    assert reg.drain_actions("w-1")[0]["seq"] == 1
    reg.push_action("w-1", "stop", goal_id="g2")
    assert reg.drain_actions("w-1")[0]["seq"] == 2


def test_push_action_rejects_when_queue_full(env):
    """超限必须显式返回 False：静默 True 会让调用方以为指令已排队。"""
    _store, _goals, reg = env
    for i in range(16):
        assert reg.push_action("w-1", "retry", goal_id=f"g{i}") is True
    assert reg.push_action("w-1", "retry", goal_id="overflow") is False
    assert len(reg.drain_actions("w-1")) == 16


def test_ack_omits_sync_on_snapshot_overflow(env, monkeypatch):
    """封顶的执行点在 ack：快照超限 → 不带 sync 段，worker 保持上一份完整快照。"""
    store, goals, reg = env
    monkeypatch.setenv("CLUSTER_MAX_SNAPSHOT_BYTES", "65536")
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    store.update_goal("qwen@@w-1", now=2.0, profile_yaml="x" * 70000)   # 灌大越限
    assert goals.snapshot_for("w-1")["sync_overflow"] is True
    ack = reg.handle_heartbeat("w-1", _hb(goal_sync={"revision": ""}), now=100.0)
    assert "sync" not in ack
    # 双钉：ack 不带 sync 段之外，投递水位同样不得写入——写了水位就等于"承认已送达"，
    # worker 下拍报同一 revision，毒 goal 撤掉前永不再尝试。
    assert store.get_node("w-1")["last_goal_sync_sha"] is None
