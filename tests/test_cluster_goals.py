#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_goals.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/5 10:00
# @Desc   : GoalService（env 白名单 / gate 串联 / 快照 revision / stage 回写）
# ===============================================================================

import hashlib

import pytest

from modelctl.core.cluster.goals import ENV_OVERLAY_ALLOWLIST, GoalService, goal_id_of, validate_env_overlay
from modelctl.core.cluster.store import ClusterStore

#: 引擎段必须挂在**引擎名**键下（gate._engine_section / core.profile._to_profile 同
#: 口径）。夹具若写成字面 `engine_config` 键，实现读错键也能全绿，真实 profile 却在
#: 生产恒判 1 卡（test_cluster_gate.py 同族防护）。tp=2 与下方用例的 gpu_list=[0,1]
#: 配套：tp 系引擎的生效卡位数必须等于 tensor_parallel_size，否则 gate 直接 skip。
YAML = "port: 8001\napi_key: ${API_KEY}\nvllm:\n  tensor_parallel_size: 2\n"


@pytest.fixture()
def store(tmp_path):
    s = ClusterStore(tmp_path / "m.db")
    s.init_db()
    return s


@pytest.fixture()
def models(tmp_path):
    d = tmp_path / "models"
    (d / "vllm").mkdir(parents=True)
    (d / "vllm" / "qwen.yaml").write_text(YAML, encoding="utf-8")
    return d


@pytest.fixture()
def svc(store, models, monkeypatch):
    # gate 读中心 models/，测试把读取根目录指向 tmp（不改生产默认值）
    monkeypatch.setattr("modelctl.core.cluster.goals.MODELS_DIR", models)
    return GoalService(store)


def _online(store, nid, *, lan="", with_runtime=True):
    store.upsert_node(node_id=nid, node_token=f"NT-{nid}", lan_id=lan, role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    store.update_node_capacity(nid, capacity={"gpu_count": 4, "vram_total_mb": 157280},
                               runtimes=({"vllm": {"ok": True}} if with_runtime else {}),
                               local_profiles=["qwen"], now=1.0)


# ---------------- env_overlay 白名单 ----------------
def test_env_overlay_allowlist_covers_paths_only():
    assert {"MODEL_ROOT", "MODELSCOPE_CACHE", "HF_HOME", "OLLAMA_MODELS",
            "LOG_DIR", "AUDIT_DIR", "MODELCTL_GPUS"} <= ENV_OVERLAY_ALLOWLIST


def test_env_overlay_rejects_secret_keys_even_inside_allowlist_shape():
    """凭据类键必须命中"凭据"专用文案，不能只断言键名出现在错误里。

    键名本身会被"不在白名单"的通用文案原样带出（`MODELSCOPE_API_KEY` 含 API_KEY），
    只断言 `"API_KEY" in err` 的话，删掉整条 SECRET 前置检查也照样全绿——两条分支
    的防护强度完全不同：白名单是"没列出就拒"，凭据检查是"即使将来误加进白名单也拒"。
    REST 层（Task 11）同样断言"凭据"字样，两侧共用这条措辞契约。
    """
    got, err = validate_env_overlay({"MODEL_ROOT": "/m", "MODELSCOPE_API_KEY": "sk-1"})
    assert got is None and "凭据" in err and "API_KEY" in err
    got, err = validate_env_overlay({"MY_TOKEN": "x"})
    assert got is None and "凭据" in err and "TOKEN" in err


def test_env_overlay_rejects_unknown_key():
    got, err = validate_env_overlay({"NOPPE": "1"})
    assert got is None and "NOPPE" in err


def test_env_overlay_values_must_be_str():
    got, err = validate_env_overlay({"MODEL_ROOT": 1})
    assert got is None and "字符串" in err


def test_env_overlay_none_and_empty_are_ok():
    assert validate_env_overlay(None) == (None, "")
    assert validate_env_overlay({}) == (None, "")


def test_goal_id_of_uses_double_at():
    assert goal_id_of("qwen", "w-1") == "qwen@@w-1"


# ---------------- set_goals ----------------
def test_set_goals_creates_only_for_ok_candidates(store, svc):
    _online(store, "w-1")
    _online(store, "w-2", with_runtime=False)      # gate 应 skip
    out = svc.set_goals(profile="qwen", node_ids=["w-1", "w-2"], create=True, created_by="op")
    assert out["created"] == 1 and out["skipped"] == 1
    assert [g["node_id"] for g in store.list_goals()] == ["w-1"]
    assert store.get_goal("qwen@@w-1")["stage"] == "PENDING_PROFILE_SYNC"


def test_set_goals_all_nodes_targets_every_gateable_node(store, svc):
    _online(store, "w-1")
    _online(store, "w-2")
    store.upsert_node(node_id="w-9", node_token="NT-9", lan_id="", role="worker", host_ip="",
                      hostname="", engines=None, now=1.0)
    store.set_node_status("w-9", "offline")
    out = svc.set_goals(profile="qwen", node_ids=None, all_nodes=True, create=True)
    assert out["created"] == 2
    assert {g["node_id"] for g in store.list_goals()} == {"w-1", "w-2"}


def test_set_goals_is_idempotent_second_call_creates_zero(store, svc):
    _online(store, "w-1")
    assert svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)["created"] == 1
    out = svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    assert out["created"] == 0 and out["skipped"] == 1
    assert len(store.list_goals()) == 1


def test_set_goals_dry_run_writes_nothing(store, svc):
    """dry-run 的 created 是"预计数"（1），但台账必须分毫不动——计划正文此处
    （created == 0）与其下游 Task 11 契约（created == 1）互斥，以实现语义为准：
    gate 报告本就把 created 当"将创建/已创建"，改 created 语义会同时破坏
    format_gate_report 与 Task 11/12 的退出码判定。"""
    _online(store, "w-1")
    out = svc.set_goals(profile="qwen", node_ids=["w-1"], create=True, dry_run=True)
    assert out["created"] == 1 and out["report"].startswith("[dry-run]")
    assert store.list_goals() == []
    assert store.get_goal("qwen@@w-1") is None


def test_set_goals_missing_profile_source_is_error(store, svc):
    _online(store, "w-1")
    out = svc.set_goals(profile="ghost", node_ids=["w-1"], create=True)
    assert out["created"] == 0 and out["errors"] == 1
    assert "不存在" in out["reason"]
    assert store.list_goals() == []


def test_set_goals_bad_env_overlay_is_rejected_before_write(store, svc):
    _online(store, "w-1")
    out = svc.set_goals(profile="qwen", node_ids=["w-1"], create=True,
                        env_overlay={"API_KEY": "sk"})
    assert out["created"] == 0 and "凭据" in out["reason"]
    assert store.list_goals() == []


def test_set_goals_writes_gpu_list_into_params_and_placement(store, svc):
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True, gpu_list=[0, 1])
    g = store.get_goal("qwen@@w-1")
    assert g["params"]["gpu_list"] == [0, 1]
    assert g["placement"]["gpu_count"] == 2


def test_set_goals_records_event(store, svc):
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True, created_by="op")
    kinds = [e["kind"] for e in store.recent_events(limit=20, node_id="w-1")]
    assert "goal.create" in kinds


def test_set_goals_rejects_unknown_intent(store, svc):
    _online(store, "w-1")
    assert "intent" in svc.set_goals(profile="qwen", node_ids=["w-1"], intent="destroy")["reason"]


def test_set_goals_rejects_unknown_target_role(store, svc):
    _online(store, "w-1")
    out = svc.set_goals(profile="qwen", node_ids=["w-1"], create=True, target_role="standby")
    assert out["created"] == 0 and "target_role" in out["reason"]
    assert store.list_goals() == []


def test_set_goals_no_reachable_node_is_error(store, svc):
    """source 正常但一个候选都取不到（节点不存在）→ reason 非空，让 REST 转 400。
    与"gate 逐节点 skip"区分开：后者是 200 + report（Task 11 的判定口径）。"""
    out = svc.set_goals(profile="qwen", node_ids=["ghost-node"], create=True)
    assert out["created"] == 0 and out["reason"] and out["verdicts"] == []
    assert store.list_goals() == []


def test_set_goals_addressed_by_display_name_normalizes_to_stem(store, svc, models):
    """展示名寻址必须归一为文件 stem（Task 3 条款④的端到端闭环）。

    goal.profile 与 worker 写盘文件名只认 stem；若幂等集/has_profile 用调用方原词
    （展示名）查库，第二次 set_goals 查不到已有 goal → gate 判 ok → upsert 把
    stage 重置回 PENDING_PROFILE_SYNC，等于"幂等重跑把 worker 状态机清零"。
    """
    (models / "vllm" / "qwen-fast.yaml").write_text(
        "port: 8001\nname: qwen-display\nvllm:\n  tensor_parallel_size: 2\n", encoding="utf-8")
    _online(store, "w-1")
    store.update_node_capacity("w-1", capacity={"gpu_count": 4, "vram_total_mb": 157280},
                               runtimes={"vllm": {"ok": True}},
                               local_profiles=["qwen-fast"], now=1.0)
    out = svc.set_goals(profile="qwen-display", node_ids=["w-1"], create=True)
    assert out["created"] == 1
    goal = store.get_goal("qwen-fast@@w-1")
    assert goal is not None and goal["profile"] == "qwen-fast"   # 落库是 stem，不是展示名
    again = svc.set_goals(profile="qwen-display", node_ids=["w-1"], create=True)
    assert again["created"] == 0 and again["skipped"] == 1       # 幂等按 stem 命中


# ---------------- snapshot ----------------
def test_snapshot_shape_and_raw_passthrough(store, svc):
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True, gpu_list=[0, 1])
    snap = svc.snapshot_for("w-1")
    assert len(snap["goals"]) == 1
    g = snap["goals"][0]
    assert g["goal_id"] == "qwen@@w-1" and g["engine"] == "vllm"
    assert "${API_KEY}" in g["yaml"]                    # 原文下发，未插值
    # yaml 必须逐字节等于源文件原文（sha 漂移检测的基准），故比对整串而非片段：
    # "${API_KEY}" 只能证明"没插值"，证明不了"没被改写/没丢段"。
    assert g["yaml"] == YAML
    # 快照里必须带 sha256:<hex>（worker 侧漂移检测的判据）。sha 是对原文的哈希，
    # 不是原文的子串——计划此处写作 `g["sha"] in YAML`，按定义永假。
    assert g["sha"] == "sha256:" + hashlib.sha256(YAML.encode("utf-8")).hexdigest()
    assert g["params"]["gpu_list"] == [0, 1]
    assert g["version"] and g["intent"] == "start"
    assert g["env_overlay"] is None
    # 快照是"下发协议载荷"的形状，绝不能夹带台账内部列（stage/placement 等）：
    # 它们随 ack 进网络帧、进 worker 落盘清单，多一个键就多一处 worker 侧误读面。
    assert set(g) == {"goal_id", "profile", "engine", "yaml", "sha",
                      "version", "intent", "params", "env_overlay"}


def test_snapshot_revision_is_stable_and_content_sensitive(store, svc):
    _online(store, "w-1")
    _online(store, "w-2")
    svc.set_goals(profile="qwen", node_ids=["w-1", "w-2"], create=True)
    a = svc.snapshot_for("w-1")["revision"]
    assert svc.snapshot_for("w-1")["revision"] == a          # 稳定（中心重启后同值）
    # 他节点变更不影响本节点：必须真的改掉 w-2 的 goal 才算钉住"按 node_id 过滤"
    # （若用 set_goals(create=True) 重跑，goal 已存在 → gate 幂等 skip → 什么都没变，
    # 断言恒真，snapshot_for 忘了 node_id 过滤也能全绿）。
    svc.store.update_goal("qwen@@w-2", now=8.0, intent="stop")
    assert store.get_goal("qwen@@w-2")["intent"] == "stop"
    assert svc.snapshot_for("w-1")["revision"] == a
    svc.store.update_goal("qwen@@w-1", now=9.0, intent="stop")
    assert svc.snapshot_for("w-1")["revision"] != a          # 本节点变更 → revision 变


def test_snapshot_empty_for_unknown_node(store, svc):
    assert svc.snapshot_for("nobody") == {"revision": "", "goals": []}


def test_snapshot_goal_order_is_deterministic(store, svc, models):
    """同一 goal 集必须产出同一 revision：revision 直接哈希 goals 列表，顺序漂了
    worker 就会判定"内容变了"而无谓重写盘。故意按 b→a 顺序建，断言快照按 a→b 定序。"""
    _online(store, "w-1")
    for prof in ("b-model", "a-model"):
        (models / "vllm" / f"{prof}.yaml").write_text(YAML, encoding="utf-8")
    for prof in ("b-model", "a-model"):
        assert svc.set_goals(profile=prof, node_ids=["w-1"], create=True)["created"] == 1
    first = svc.snapshot_for("w-1")
    assert [g["profile"] for g in first["goals"]] == ["a-model", "b-model"]
    assert svc.snapshot_for("w-1")["revision"] == first["revision"]


# ---------------- remove / stage / model_states ----------------
def test_remove_goals_snapshots_yaml_for_worker_prune(store, svc):
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    out = svc.remove_goals(profile="qwen", node_ids=["w-1"], created_by="op")
    assert out["removed"] == ["qwen@@w-1"] and store.list_goals() == []
    # report 面向操作者，必须点名 profile：goal 从快照消失即剪枝语义（§6.5），
    # 运维要靠这行确认"撤的是哪个 profile"。
    assert out["report"] and "qwen" in out["report"]
    kinds = [e["kind"] for e in store.recent_events(limit=20, node_id="w-1")]
    assert "goal.delete" in kinds


def test_remove_goals_reports_missing(store, svc):
    assert svc.remove_goals(profile="qwen", node_ids=["w-1"])["missing"] == ["qwen@@w-1"]


def test_remove_all_nodes(store, svc):
    _online(store, "w-1")
    _online(store, "w-2")
    svc.set_goals(profile="qwen", node_ids=None, all_nodes=True, create=True)
    assert len(svc.remove_goals(profile="qwen", node_ids=None, all_nodes=True)["removed"]) == 2


def test_remove_goals_prunes_model_state(store, svc):
    """撤托管必须连带删掉该 profile 的运行态：残留的 READY 会让 dashboard 在
    worker 已剪枝之后继续显示"在跑"，也会被 _in_use_gpus 当成占卡而挡住新下发。"""
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    svc.record_model_states("w-1", {"qwen": {"state": "READY", "gpu": [0, 1]}}, now=2.0)
    svc.remove_goals(profile="qwen", node_ids=["w-1"])
    assert store.list_model_states(node_id="w-1") == []


def test_mark_stage_writes_reason_and_error_class(store, svc):
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    svc.mark_stage("qwen@@w-1", "FAILED", reason="venv 缺失", error_class="venv_missing", now=5.0)
    g = store.get_goal("qwen@@w-1")
    assert g["stage"] == "FAILED" and g["error_class"] == "venv_missing"
    assert g["stage_reason"] == "venv 缺失"


def test_mark_stage_on_absent_goal_is_silent(store, svc):
    svc.mark_stage("nope@@w-1", "READY")     # 不抛：心跳里可能出现已删 goal 的残留状态


def test_record_model_states_overwrites_and_prunes(store, svc):
    _online(store, "w-1")
    svc.record_model_states("w-1", {"qwen": {"state": "READY", "port": 8101,
                                             "gpu": [0, 1], "pid": 7}}, now=1.0)
    row = store.list_model_states(node_id="w-1")[0]
    assert row["state"] == "READY" and row["port"] == 8101 and row["gpu"] == [0, 1]
    svc.record_model_states("w-1", {}, now=2.0)          # 空集 = 该节点当前无在跑模型
    assert store.list_model_states(node_id="w-1") == []


def test_record_model_states_ignores_non_dict_entries(store, svc):
    _online(store, "w-1")
    svc.record_model_states("w-1", {"qwen": "junk", "ok": {"state": "UP"}}, now=1.0)
    assert [r["profile"] for r in store.list_model_states(node_id="w-1")] == ["ok"]


def test_record_model_states_ignores_unsafe_names(store, svc):
    """profile 名是 worker 侧写盘路径成分，回流同样要过白名单（心跳不可信）。"""
    _online(store, "w-1")
    svc.record_model_states("w-1", {"../escape": {"state": "READY"},
                                    "good": {"state": "READY"}}, now=1.0)
    assert [r["profile"] for r in store.list_model_states(node_id="w-1")] == ["good"]
