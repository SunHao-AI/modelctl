#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_goals_http.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : goal REST（创建/更新/删除/retry/强制同步/远程启停/导出）+ WS 世代表与 result 落账
# ===============================================================================
"""admin_cluster v2：goal 控制面端点与 WS 世代仲裁。

端点断言只盯两件事：① 台账/快照的真实变化（不信任响应体自述）；
② ack 里真正出现了应有的 sync/action（递交确实发生）。
"""
from __future__ import annotations

import re
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("websockets")
from fastapi.testclient import TestClient  # noqa: E402

from modelctl.core.cluster import conns  # noqa: E402
from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_12345"
YAML = "port: 8101\napi_key: ${API_KEY}\nengine_config:\n  tensor_parallel_size: 2\n"
GOALS = "/admin/api/cluster/goals"


def _h():
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture()
def center(monkeypatch, tmp_path):
    """中心角色 + 在线节点 w-1（4 卡 / vllm 可用 / 已有 qwen profile 文件）。"""
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CLUSTER_ROLE", "both")
    models = tmp_path / "models"
    (models / "vllm").mkdir(parents=True)
    (models / "vllm" / "qwen.yaml").write_text(YAML, encoding="utf-8")
    monkeypatch.setattr("modelctl.core.cluster.goals.MODELS_DIR", models)

    import modelctl.core.webui.admin_cluster as ac

    ac._REGISTRY = None
    ac._CONNS = conns.ConnectionRegistry()
    app = create_app(admin=True)
    with TestClient(app) as c:
        reg = ac.get_registry()
        reg.store.upsert_node(node_id="w-1", node_token="NT-1", lan_id="lan-1", role="worker",
                              host_ip="", hostname="", engines=None, now=time.time())
        reg.store.upsert_node(node_id="w-2", node_token="NT-2", lan_id="lan-2", role="worker",
                              host_ip="", hostname="", engines=None, now=time.time())
        reg.store.update_node_capacity(
            "w-1", capacity={"gpu_count": 4, "vram_total_mb": 157280},
            runtimes={"vllm": {"ok": True}}, local_profiles=["qwen"])
        reg.store.update_node_capacity(
            "w-2", capacity={"gpu_count": 4, "vram_total_mb": 157280},
            runtimes={"vllm": {"ok": True}}, local_profiles=[])
        yield c
    ac._REGISTRY = None
    ac._CONNS = conns.ConnectionRegistry()


def _create(client, **over):
    body = {"profile": "qwen", "node_ids": ["w-1"], "create": True}
    body.update(over)
    return client.post(GOALS, json=body, headers=_h())


def _heartbeat(ws, **payload):
    body = {"profiles": {}}
    body.update(payload)      # 不用 dict({"profiles": {}}, **payload)：键冲突时行为不明确
    ws.send_json({"t": "heartbeat", "payload": body})
    return ws.receive_json()


# ---------------- 列表 / 创建 ----------------
def test_goals_list_empty(center) -> None:
    assert center.get(GOALS, headers=_h()).json()["goals"] == []


def test_create_goal_reports_gate_and_writes_ledger(center) -> None:
    r = _create(center)
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 1 and body["reason"] == ""
    assert "w-1" in body["report"]
    assert [g["goal_id"] for g in center.get(GOALS, headers=_h()).json()["goals"]] == ["qwen@@w-1"]


def test_create_goal_missing_profile_422(center) -> None:
    assert center.post(GOALS, json={"node_ids": ["w-1"]}, headers=_h()).status_code == 422


def test_create_goal_unknown_node_400(center) -> None:
    r = _create(center, node_ids=["ghost"])
    assert r.status_code == 400 and "节点" in r.json()["detail"]


def test_create_goal_bad_intent_400(center) -> None:
    assert _create(center, intent="reboot").status_code == 400


def test_create_goal_duplicate_gpus_400(center) -> None:
    """重复卡位是典型误操作：`0,0` 会被 gate 当成 1 卡需求，静默接受比报错更危险。"""
    assert _create(center, gpus="0,0").status_code == 400


def test_create_goal_gpus_land_in_params_and_placement(center) -> None:
    import modelctl.core.webui.admin_cluster as ac

    _create(center, gpus="2,3")
    goal = ac.get_registry().store.get_goal("qwen@@w-1")
    assert goal["params"]["gpu_list"] == [2, 3] and goal["placement"]["gpu_count"] == 2


def test_create_goal_bad_target_role_400(center) -> None:
    assert _create(center, target_role="standby").status_code == 400


def test_create_goal_secret_overlay_400(center) -> None:
    r = _create(center, env_overlay={"API_KEY": "sk-leak"})
    assert r.status_code == 400 and "凭据" in r.json()["detail"]


def test_create_goal_dry_run_writes_nothing(center) -> None:
    body = _create(center, dry_run=True).json()
    assert body["created"] == 1 and "[dry-run]" in body["report"]
    assert center.get(GOALS, headers=_h()).json()["goals"] == []


def test_create_goal_all_nodes_creates_every_candidate(center) -> None:
    """create=True 时 w-2 本机没有 qwen 文件也算 ok（快照下发后由 worker 写盘）。"""
    body = _create(center, node_ids=None, all_nodes=True, create=True).json()
    assert body["created"] == 2
    assert len(center.get(GOALS, headers=_h()).json()["goals"]) == 2


def test_create_goal_all_nodes_without_create_skips_missing_profile(center) -> None:
    """未加 create：w-1 已有文件 → ok；w-2 无文件 → 逐节点 skip（不是整体失败）。"""
    body = _create(center, node_ids=None, all_nodes=True, create=False).json()
    assert body["created"] == 1 and body["skipped"] == 1
    assert [g["node_id"] for g in center.get(GOALS, headers=_h()).json()["goals"]] == ["w-1"]


def test_create_goal_offline_node_is_skipped_not_created(center) -> None:
    import modelctl.core.webui.admin_cluster as ac

    ac.get_registry().store.set_node_status("w-1", "offline")
    body = _create(center).json()
    assert body["created"] == 0 and body["skipped"] == 1


def test_create_goal_response_scoped_to_requested_nodes(center) -> None:
    """响应只回本次请求命中的节点：按 profile 全量列会混入未请求节点（fix round 1 Minor-1）。"""
    _create(center, node_ids=None, all_nodes=True, create=True)      # w-1 / w-2 各一条 goal
    body = _create(center, node_ids=["w-1"]).json()
    assert {g["node_id"] for g in body["goals"]} <= {"w-1"}


def test_create_goal_ambiguous_profile_refuses_then_engine_picks_side(center) -> None:
    """同名 YAML 散落多个引擎子目录：**绝不猜**，但必须给出选边出口（review P-1）。

    不带 engine → 歧义拒发（本仓 models/*/qwen3.8.yaml 有 8 份同名，是高频现实场景）；
    带 engine → 命中该引擎那份并落库。旧代码 `_GoalCreateBody` 无 engine 字段，
    Pydantic 静默忽略未知字段 → 恒歧义，用户遇 `[err] 歧义` 后无路可走。
    """
    import modelctl.core.cluster.goals as goals_mod
    import modelctl.core.webui.admin_cluster as ac

    for engine in ("vllm", "sglang"):
        (goals_mod.MODELS_DIR / engine).mkdir(parents=True, exist_ok=True)
        (goals_mod.MODELS_DIR / engine / "dup.yaml").write_text(YAML, encoding="utf-8")

    r = _create(center, profile="dup", create=True)
    assert r.status_code == 400 and "歧义" in r.json()["detail"]
    assert ac.get_registry().store.list_goals(profile="dup") == []   # created 恒 0

    r = _create(center, profile="dup", create=True, engine="vllm")
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1
    assert [g["engine"] for g in r.json()["goals"]] == ["vllm"]
    assert ac.get_registry().store.get_goal("dup@@w-1")["engine"] == "vllm"


def test_list_goals_filters_by_node_and_profile(center) -> None:
    _create(center, node_ids=None, all_nodes=True)
    assert len(center.get(f"{GOALS}?node_id=w-1", headers=_h()).json()["goals"]) == 1
    assert center.get(f"{GOALS}?profile=nope", headers=_h()).json()["goals"] == []


# ---------------- 视图形状 ----------------
def test_goal_view_exposes_stage_and_runtime_facts(center) -> None:
    _create(center)
    jt = _jt(center)
    with center.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 2, "node_id": "w-1", "lan": "lan-1",
                      "key": jt, "meta": {}})
        assert ws.receive_json()["t"] == "welcome"
        _heartbeat(ws, profiles={"qwen": {"stage": "READY", "state": "READY",
                                          "port": 8101, "gpu": [0, 1], "pid": 4321}})
    # WS 关闭后再查台账：回流已落库，视图不依赖长连接存活
    goal = center.get(GOALS, headers=_h()).json()["goals"][0]
    assert goal["stage"] == "READY" and goal["port"] == 8101 and goal["gpu"] == [0, 1]
    assert "profile_yaml" not in goal


def test_goal_view_times_are_project_format(center) -> None:
    _create(center)
    goal = center.get(GOALS, headers=_h()).json()["goals"][0]
    pattern = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
    assert re.match(pattern, goal["created_at"]) and re.match(pattern, goal["updated_at"])
    assert goal["age_s"] >= 0


# ---------------- 更新 ----------------
def test_update_goal_params_bumps_snapshot_revision(center) -> None:
    import modelctl.core.webui.admin_cluster as ac

    _create(center)
    before = ac.get_registry().goals.snapshot_for("w-1")["revision"]
    r = center.put(f"{GOALS}/qwen@@w-1", json={"params": {"gpu_list": [2, 3]}}, headers=_h())
    assert r.status_code == 200 and r.json()["goal"]["goal_id"] == "qwen@@w-1"
    assert ac.get_registry().goals.snapshot_for("w-1")["revision"] != before


def test_update_goal_intent_persists(center) -> None:
    _create(center)
    center.put(f"{GOALS}/qwen@@w-1", json={"intent": "stop"}, headers=_h())
    assert center.get(GOALS, headers=_h()).json()["goals"][0]["intent"] == "stop"


def test_update_goal_empty_body_400(center) -> None:
    _create(center)
    assert center.put(f"{GOALS}/qwen@@w-1", json={}, headers=_h()).status_code == 400


def test_update_goal_bad_env_overlay_400(center) -> None:
    _create(center)
    r = center.put(f"{GOALS}/qwen@@w-1", json={"env_overlay": {"NOPPE": "1"}}, headers=_h())
    assert r.status_code == 400


def test_update_goal_missing_404(center) -> None:
    r = center.put(f"{GOALS}/ghost@@w-1", json={"intent": "stop"}, headers=_h())
    assert r.status_code == 404


def test_update_goal_target_role_persists(center) -> None:
    """PUT 声明可改的键必须真落库：service 白名单与 store 白名单不一致会假报 200（Major-1）。"""
    import modelctl.core.webui.admin_cluster as ac

    _create(center)
    store = ac.get_registry().store
    before = store.get_goal("qwen@@w-1")["updated_at"]
    r = center.put(f"{GOALS}/qwen@@w-1", json={"target_role": "replica"}, headers=_h())
    assert r.status_code == 200
    goal = store.get_goal("qwen@@w-1")
    assert goal["target_role"] == "replica"
    assert goal["updated_at"] > before
    events = store.recent_events(limit=10, node_id="w-1")
    update = [e for e in events if e["kind"] == "goal.update"][0]
    assert update["payload"]["fields"] == ["target_role"]


def test_update_goal_runtime_ref_persists(center) -> None:
    import modelctl.core.webui.admin_cluster as ac

    _create(center)
    store = ac.get_registry().store
    r = center.put(f"{GOALS}/qwen@@w-1", json={"runtime_ref": "rt-1"}, headers=_h())
    assert r.status_code == 200
    assert store.get_goal("qwen@@w-1")["runtime_ref"] == "rt-1"
    events = store.recent_events(limit=10, node_id="w-1")
    update = [e for e in events if e["kind"] == "goal.update"][0]
    assert update["payload"]["fields"] == ["runtime_ref"]


def test_update_goal_profile_version_null_becomes_empty(center) -> None:
    """显式 null 归一为空串：str(None) 会把字符串 "None" 写进台账当版本号（Major-2）。"""
    import modelctl.core.webui.admin_cluster as ac

    _create(center)
    r = center.put(f"{GOALS}/qwen@@w-1", json={"profile_version": None}, headers=_h())
    assert r.status_code == 200
    assert ac.get_registry().store.get_goal("qwen@@w-1")["profile_version"] == ""


# ---------------- 删除 ----------------
def test_delete_goal_disappears_from_snapshot(center) -> None:
    import modelctl.core.webui.admin_cluster as ac

    _create(center)
    r = center.delete(f"{GOALS}/qwen@@w-1", headers=_h())
    assert r.status_code == 200 and r.json()["removed"] == ["qwen@@w-1"]
    assert ac.get_registry().goals.snapshot_for("w-1")["goals"] == []


def test_delete_goal_missing_404(center) -> None:
    assert center.delete(f"{GOALS}/qwen@@ghost", headers=_h()).status_code == 404


# ---------------- retry / 强制同步 / 远程启停 ----------------
def test_retry_goal_queues_action_for_next_ack(center) -> None:
    _create(center)
    rev = center_revision(center, "w-1")      # 先取 revision：WS 上下文内不再发 HTTP 请求
    r = center.post(f"{GOALS}/qwen@@w-1/retry", headers=_h())
    assert r.status_code == 200 and r.json()["queued"] is True
    jt = _jt(center)
    with center.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 2, "node_id": "w-1", "lan": "", "key": jt,
                      "meta": {}})
        ws.receive_json()
        ack = _heartbeat(ws, goal_sync={"revision": rev})
    assert ack["actions"][0]["action"] == "retry"
    assert ack["actions"][0]["goal_id"] == "qwen@@w-1"


def test_retry_goal_keeps_reported_stage_untouched(center) -> None:
    """中心绝不乐观改写 stage：节点离线时假 PENDING 比真 FAILED 更有害。"""
    import modelctl.core.webui.admin_cluster as ac

    _create(center)
    ac.get_registry().goals.mark_stage("qwen@@w-1", "FAILED", error_class="oom", now=time.time())
    center.post(f"{GOALS}/qwen@@w-1/retry", headers=_h())
    assert ac.get_registry().store.get_goal("qwen@@w-1")["stage"] == "FAILED"


def test_retry_unknown_goal_404(center) -> None:
    assert center.post(f"{GOALS}/ghost@@w-1/retry", headers=_h()).status_code == 404


def test_force_sync_makes_ack_carry_snapshot(center) -> None:
    _create(center)
    rev = center_revision(center, "w-1")
    jt = _jt(center)
    assert center.post("/admin/api/cluster/nodes/w-1/sync", headers=_h()).status_code == 200
    with center.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 2, "node_id": "w-1", "lan": "", "key": jt,
                      "meta": {}})
        ws.receive_json()
        ack = _heartbeat(ws, goal_sync={"revision": rev})   # revision 相同也必须带
    assert ack["sync"]["force"] is True and ack["sync"]["revision"] == rev


def test_force_sync_is_one_shot(center) -> None:
    """一次性：第二拍 revision 已一致就恢复不带快照，否则每 10s 白传全量 YAML。"""
    _create(center)
    jt = _jt(center)
    center.post("/admin/api/cluster/nodes/w-1/sync", headers=_h())
    with center.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 2, "node_id": "w-1", "lan": "", "key": jt,
                      "meta": {}})
        ws.receive_json()
        rev = _heartbeat(ws, goal_sync={"revision": ""})["sync"]["revision"]
        assert "sync" not in _heartbeat(ws, goal_sync={"revision": rev})


def test_force_sync_unknown_node_404(center) -> None:
    r = center.post("/admin/api/cluster/nodes/ghost/sync", headers=_h())
    assert r.status_code == 404


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_model_verb_queues_action(center, verb) -> None:
    _create(center)
    r = center.post("/admin/api/cluster/nodes/w-1/model/qwen/stop", headers=_h())
    assert r.status_code == 200 and r.json()["queued"] is True


def test_model_verb_reaches_ack_as_action_frame(center) -> None:
    """verb 必须真投到 ack（P-2 承前）：`queued: true` 只证明入队，不证明投递。

    retry 有同款钉，verb 没有 → push_action 的 `profile=` 参数丢失、或 ack 组装漏
    drain_actions，都只剩 worker 侧"本机没有目标"的失败回执可观察。action 帧经
    `deliver_ack` → `handle_actions` 的投递由 test_cluster_agent_v2 的替身钉覆盖。
    """
    _create(center)
    rev = center_revision(center, "w-1")
    assert center.post("/admin/api/cluster/nodes/w-1/model/qwen/stop", headers=_h()).status_code == 200
    jt = _jt(center)
    with center.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 2, "node_id": "w-1", "lan": "", "key": jt,
                      "meta": {}})
        ws.receive_json()
        ack = _heartbeat(ws, goal_sync={"revision": rev})
    frame = ack["actions"][0]
    assert frame["action"] == "stop" and frame["goal_id"] == "qwen@@w-1"
    assert frame["profile"] == "qwen"                       # worker 侧按 profile 定位模型


def test_model_verb_unknown_verb_400(center) -> None:
    _create(center)
    r = center.post("/admin/api/cluster/nodes/w-1/model/qwen/purge", headers=_h())
    assert r.status_code == 400


def test_model_verb_requires_existing_goal_404(center) -> None:
    """未托管的 profile 不能远程操作：worker 侧 handle_actions 同样会拒绝。"""
    r = center.post("/admin/api/cluster/nodes/w-1/model/ghost/stop", headers=_h())
    assert r.status_code == 404


# ---------------- 单节点详情 / 导出 / 闸门 ----------------
def test_nodes_list_exposes_capacity_text(center) -> None:
    """Global Constraints：capacity 由后端格式化成展示串，前端/CLI 不二次加工。"""
    body = center.get("/admin/api/cluster/nodes", headers=_h()).json()
    w1 = [n for n in body["nodes"] if n["node_id"] == "w-1"][0]
    assert w1["capacity_text"] == "4 卡 / 154 GiB"     # 157280 MiB → 153.6 → 四舍五入 154


def test_capacity_text_dash_when_missing(center) -> None:
    import modelctl.core.webui.admin_cluster as ac
    reg = ac.get_registry()
    # 传 {} 才是"清空"——capacity=None 是"不覆盖"（Task 1 的合并语义），w-2 夹具已带容量
    reg.store.update_node_capacity("w-2", capacity={}, runtimes=None,
                                   local_profiles=None)
    body = center.get("/admin/api/cluster/nodes", headers=_h()).json()
    w2 = [n for n in body["nodes"] if n["node_id"] == "w-2"][0]
    assert w2["capacity_text"] == "-"


def test_node_detail_groups_goals_and_states(center) -> None:
    _create(center)
    r = center.get("/admin/api/cluster/nodes/w-1", headers=_h())
    body = r.json()
    assert body["node"]["node_id"] == "w-1"
    assert [g["goal_id"] for g in body["goals"]] == ["qwen@@w-1"]


def test_node_detail_missing_404(center) -> None:
    assert center.get("/admin/api/cluster/nodes/ghost", headers=_h()).status_code == 404


def test_export_covers_nodes_goals_states_without_tokens(center) -> None:
    _create(center)
    body = center.get("/admin/api/cluster/export", headers=_h()).json()
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", body["exported_at"])
    assert len(body["nodes"]) == 2 and len(body["goals"]) == 1
    assert body["goals"][0]["profile_yaml"]            # 导出是 restore 素材，必须含原文
    assert all("node_token" not in n for n in body["nodes"])


def test_solo_role_returns_404_on_goal_endpoints(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CLUSTER_ROLE", "solo")
    import modelctl.core.webui.admin_cluster as ac

    ac._REGISTRY = None
    app = create_app(admin=True)
    with TestClient(app) as c:
        assert c.get(GOALS, headers=_h()).status_code == 404
        assert c.post(GOALS, json={"profile": "qwen", "node_ids": ["w-1"]}, headers=_h()).status_code == 404
        assert c.post("/admin/api/cluster/nodes/w-1/sync", headers=_h()).status_code == 404
    ac._REGISTRY = None


# ---------------- WS：result 落账 + 世代表 ----------------
def test_ws_result_frame_recorded_as_event(center) -> None:
    import modelctl.core.webui.admin_cluster as ac

    jt = _jt(center)
    with center.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 2, "node_id": "w-1", "lan": "", "key": jt,
                      "meta": {}})
        ws.receive_json()
        ws.send_json({"t": "result", "seq": 3, "ok": False, "detail": "本机没有目标 x@@w-1"})
        assert ws.receive_json()["t"] == "ack"
    events = ac.get_registry().store.recent_events(limit=10, node_id="w-1")
    result = [e for e in events if e["kind"] == "action.result"][0]
    assert result["payload"] == {"seq": 3, "ok": False, "detail": "本机没有目标 x@@w-1"}


def test_second_connection_for_same_node_supersedes_first(center) -> None:
    """同 node_id 后来者胜：旧连接下一次发送即被踢，避免 action 双投/投给僵尸。"""
    jt = _jt(center)

    def _hello(ws) -> None:
        ws.send_json({"t": "hello", "v": 2, "node_id": "w-1", "lan": "", "key": jt, "meta": {}})
        assert ws.receive_json()["t"] == "welcome"

    from starlette.websockets import WebSocketDisconnect

    with center.websocket_connect("/admin/api/ws/cluster") as old:
        _hello(old)
        with center.websocket_connect("/admin/api/ws/cluster") as new:
            _hello(new)
            old.send_json({"t": "heartbeat", "payload": {"profiles": {}}})
            assert old.receive_json()["t"] == "error"
            with pytest.raises(WebSocketDisconnect):
                old.receive_json()
            # 新连接不受旧连接退场影响（release 只摘自己的 epoch）
            assert _heartbeat(new, profiles={})["t"] == "ack"


def _jt(client) -> str:
    return client.post("/admin/api/cluster/join-tokens/rotate", headers=_h()).json()["join_token"]


def center_revision(client, node_id: str) -> str:
    import modelctl.core.webui.admin_cluster as ac

    return ac.get_registry().goals.snapshot_for(node_id)["revision"]
