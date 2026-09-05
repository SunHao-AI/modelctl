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
import yaml

from modelctl.core.cluster import profiles
from modelctl.core.cluster.goals import (
    ENV_OVERLAY_ALLOWLIST,
    GPU_OCCUPYING_STATES,
    GoalService,
    goal_id_of,
    validate_env_overlay,
)
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


def _add_display_profile(models):
    """stem=qwen-fast、展示名=qwen-display 的 profile（remove 侧归一用例共用）。"""
    (models / "vllm" / "qwen-fast.yaml").write_text(
        "port: 8001\nname: qwen-display\nvllm:\n  tensor_parallel_size: 2\n", encoding="utf-8")


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
    # 无 --gpus：必须逐字节原文透传（裁决A 只在显式点卡时改写引擎段）。带 --gpus 的
    # 落地路径由 test_set_goals_lands_gpu_list_into_dispatched_yaml 单独钉住。
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)
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
    assert g["params"] is None                          # 无 --gpus 不进 params
    assert g["version"] and g["intent"] == "start"
    assert g["env_overlay"] is None
    # 快照是"下发协议载荷"的形状，绝不能夹带台账内部列（stage/placement 等）：
    # 它们随 ack 进网络帧、进 worker 落盘清单，多一个键就多一处 worker 侧误读面。
    assert set(g) == {"goal_id", "profile", "engine", "yaml", "sha",
                      "version", "intent", "params", "env_overlay"}


def test_set_goals_lands_gpu_list_into_dispatched_yaml(store, svc):
    """裁决A：显式 --gpus 必须"落地"进下发 YAML 引擎段，而不只是留在 params/placement。

    worker 的 selected_gpus() 读的是下发 YAML 引擎段 gpu_list（不是 params），中心只记
    requested 不写回 = 中心按 requested 判、worker 按 profile 声明锁卡的双源不一致毒 goal。
    故落库/下发的 profile_yaml 必须含 gpu_list，且 sha 对**合并后文本**重算（worker 拿
    它做漂移检测，若仍对原文取哈希，worker 一写盘就误判"本地被篡改"）。
    """
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True, gpu_list=[0, 1])
    g = store.get_goal("qwen@@w-1")
    # 引擎段真的带上了卡位，且原 tp 段其他键不丢（safe_load/dump 往返）
    section = yaml.safe_load(g["profile_yaml"])["vllm"]
    assert section["gpu_list"] == [0, 1]
    assert section["tensor_parallel_size"] == 2
    # 占位符永不插值（密钥不出中心），合并只动 gpu_list 一个键
    assert "${API_KEY}" in g["profile_yaml"]
    # sha 对合并后文本重算，且 != 原文哈希（证明落地真的改变了下发内容）
    assert g["profile_sha"] == profiles.profile_sha(g["profile_yaml"])
    assert g["profile_sha"] != "sha256:" + hashlib.sha256(YAML.encode("utf-8")).hexdigest()
    # 快照与台账同源：下发出去的 yaml/sha 就是落库值
    snap = svc.snapshot_for("w-1")["goals"][0]
    assert snap["yaml"] == g["profile_yaml"] and snap["sha"] == g["profile_sha"]


def test_set_goals_without_gpus_keeps_yaml_byte_identical(store, svc):
    """无 --gpus 绝不改写 YAML（原文逐字节铁律）：这条与上一条互为镜像，共同钉住
    裁决A 的"仅 gpu_list 非空才合并"边界——只测带 --gpus 会漏掉"改写不该改写时"的回归。"""
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    g = store.get_goal("qwen@@w-1")
    assert g["profile_yaml"] == YAML
    assert g["profile_sha"] == profiles.profile_sha(YAML)
    # 原文里本来没写 gpu_list，改写后也不该凭空多出该键
    assert "gpu_list" not in yaml.safe_load(g["profile_yaml"])["vllm"]


def test_set_goals_version_derived_from_final_sha(store, svc):
    """裁决4：带 --gpus 时 profile_version 必须对**最终落库 sha**（合并后文本）派生。

    单一哈希链：yaml ↔ sha ↔ version 三者同源。version 若仍从原文 sha 派生，worker
    与 dashboard 会看到"版本号对应内容哈希"的指针悬空——version 的可追溯性
    （default_profile_version 的 Triton version policy 语义）正是那 13 位 sha 前缀。
    """
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True, gpu_list=[0, 1])
    g = store.get_goal("qwen@@w-1")
    assert g["profile_version"] == profiles.default_profile_version(g["profile_sha"])
    # version 的哈希段就是 profile_sha 的 sha256 前缀（与 default_profile_version 同口径）
    assert g["profile_version"].endswith(g["profile_sha"][len("sha256:"):13])


def test_dry_run_and_real_run_agree_on_goal_limit(store, svc, monkeypatch):
    """裁决4：MAX_GOALS_PER_NODE 检查必须在 dry_run 计数**之前**。

    顺序颠倒时 dry-run 报告"预计 created=1"而实跑 skip（created=0）——Task 12 CLI 以
    dry-run 结论决定退出码/提示，会误导运维"演练通过"却在实跑被静默跳过。
    把上限压到 1 避免为测试插 512 行。"""
    monkeypatch.setattr("modelctl.core.cluster.goals.MAX_GOALS_PER_NODE", 1)
    _online(store, "w-1")
    store.upsert_goal(goal_id="filler@@w-1", node_id="w-1", profile="filler", engine="vllm",
                      profile_yaml="port: 9401\n", profile_sha="sha256:filler",
                      profile_version=None, intent="start", params=None, env_overlay=None,
                      placement=None, runtime_ref=None, target_role="primary",
                      stage="READY", created_by="op", now=1.0)
    dry = svc.set_goals(profile="qwen", node_ids=["w-1"], create=True, dry_run=True)
    real = svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    assert dry["created"] == 0 == real["created"]
    assert dry["skipped"] == 1 == real["skipped"]
    assert "上限" in dry["verdicts"][0].reason and "上限" in real["verdicts"][0].reason
    assert store.list_goals(profile="qwen") == []        # dry-run 依旧分毫不动


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
    """撤托管必须连带删掉该 profile 的运行态：残留的 running 会让 dashboard 在
    worker 已剪枝之后继续显示"在跑"，也会被 _in_use_gpus 当成占卡而挡住新下发。"""
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    svc.record_model_states("w-1", {"qwen": {"state": "running", "gpu": [0, 1]}}, now=2.0)
    svc.remove_goals(profile="qwen", node_ids=["w-1"])
    assert store.list_model_states(node_id="w-1") == []


# ---------------- remove 侧展示名归一（裁决2）----------------
def test_remove_goals_by_display_name_hits_stem_goal(store, svc, models):
    """set 能用展示名，remove/stop 也必须能用（Task 12 CLI `goal remove --profile 展示名`）。

    remove 侧不归一时，调用方原词（展示名）拼出的 goal_id 查无此行 → 全进 missing，
    与 set 的体验分裂；台账里 stem goal 永远撤不掉，直到运维发现文件名才知情。"""
    _add_display_profile(models)
    _online(store, "w-1")
    store.update_node_capacity("w-1", capacity={"gpu_count": 4, "vram_total_mb": 157280},
                               runtimes={"vllm": {"ok": True}},
                               local_profiles=["qwen-fast"], now=1.0)
    assert svc.set_goals(profile="qwen-display", node_ids=["w-1"], create=True)["created"] == 1
    out = svc.remove_goals(profile="qwen-display", node_ids=["w-1"])
    assert out["removed"] == ["qwen-fast@@w-1"]
    assert store.list_goals() == []
    # 裁决2 算法（候选名 × 节点全组合）的必然副产品：展示名拼出的 goal_id 恒不落库
    # （set 侧只认 stem），故它进 missing。钉住而非回避——missing 语义是"查无此目标"，
    # 本就属实；若改成"stem 命中即不再生成原词组合"，本断言转红、逼迫重裁决。
    assert out["missing"] == ["qwen-display@@w-1"]


def test_remove_goals_works_after_profile_file_deleted(store, svc, models):
    """profile 文件已删仍必须能撤掉台账 goal（裁决2 硬要求）。

    归一失败不得连带撤不掉：set 之后 YAML 被删是正常运维序列（撤模型 = 先删源再撤
    托管，或反过来），_resolve_names 读取失败必须退化为 [原词] 单候选。若归一实现
    在读取失败时返回空候选，remove 会查无目标 → goal 永远撤不掉、worker 永久托管。"""
    _online(store, "w-1")
    assert svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)["created"] == 1
    (models / "vllm" / "qwen.yaml").unlink()
    out = svc.remove_goals(profile="qwen", node_ids=["w-1"])
    assert out["removed"] == ["qwen@@w-1"]
    assert store.list_goals() == []


def test_remove_all_nodes_by_display_name(store, svc, models):
    """--all 路径按展示名查库（list_goals(profile=展示名)）恒空 → 必须归一为 stem 再查。"""
    _add_display_profile(models)
    _online(store, "w-1")
    _online(store, "w-2")
    for nid in ("w-1", "w-2"):
        store.update_node_capacity(nid, capacity={"gpu_count": 4, "vram_total_mb": 157280},
                                   runtimes={"vllm": {"ok": True}},
                                   local_profiles=["qwen-fast"], now=1.0)
    assert svc.set_goals(profile="qwen-display", node_ids=None,
                         all_nodes=True, create=True)["created"] == 2
    out = svc.remove_goals(profile="qwen-display", node_ids=None, all_nodes=True)
    assert sorted(out["removed"]) == ["qwen-fast@@w-1", "qwen-fast@@w-2"]
    assert store.list_goals() == []


def test_remove_by_display_name_prunes_model_state(store, svc, models):
    """连带清 model_states 必须按**被删 goal 行的 profile 字段**（stem），不能用调用方
    原词：model_states.profile 来自 worker 回流 = stem，拿展示名去删恒不命中 →
    goal 已撤而运行态残留，dashboard 继续显示"在跑"且 _in_use_gpus 继续把它当占卡。"""
    _add_display_profile(models)
    _online(store, "w-1")
    store.update_node_capacity("w-1", capacity={"gpu_count": 4, "vram_total_mb": 157280},
                               runtimes={"vllm": {"ok": True}},
                               local_profiles=["qwen-fast"], now=1.0)
    svc.set_goals(profile="qwen-display", node_ids=["w-1"], create=True)
    svc.record_model_states("w-1", {"qwen-fast": {"state": "running", "gpu": [0, 1]}}, now=2.0)
    out = svc.remove_goals(profile="qwen-display", node_ids=["w-1"])
    assert out["removed"] == ["qwen-fast@@w-1"]
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
    svc.record_model_states("w-1", {"qwen": {"state": "running", "port": 8101,
                                             "gpu": [0, 1], "pid": 7}}, now=1.0)
    row = store.list_model_states(node_id="w-1")[0]
    assert row["state"] == "running" and row["port"] == 8101 and row["gpu"] == [0, 1]
    svc.record_model_states("w-1", {}, now=2.0)          # 空集 = 该节点当前无在跑模型
    assert store.list_model_states(node_id="w-1") == []


# ---------------- 占卡词表（裁决1）----------------
#: 与 Task 9 reconcile._STATE_OF_STAGE 的写入值逐一对应（全仓唯一写入口是
#: record_model_states）。夹具若用生产从不产生的虚构值（如大写 READY），实现与
#: 夹具同错 → 单测全绿而生产"在用 GPU"恒为空集，gate 两项卡位检查静默失效。
OCCUPYING = [("running", [0, 1]), ("starting", [1, 2]), ("degraded", [3])]
#: stopped/failed 是终态（worker 已释放卡位）；pending 是 goal 侧 stage 回流、从未占卡。
#: 词表内任何一个值的增减都可能静默改变放行面，四态 + 未知态必须钉死。
FREE = [("stopped", [0, 1]), ("failed", [0, 1]), ("pending", [0, 1]), ("weird-state", [0, 1])]


def test_occupying_states_constant_matches_task9_vocabulary():
    """占卡词表是跨任务共享常量（Task 9 写入 / gate 消费 / 计划文本引用的唯一来源）。

    小写六值 = Task 9 _STATE_OF_STAGE 实际写入值 + M0 心跳透传的过渡期兼容值；
    任何一侧改词表必须同步改这条断言，防止"中心白名单与生产写入值零交集"复发。"""
    assert {"running", "starting", "degraded", "READY", "STARTING", "UP"} == GPU_OCCUPYING_STATES


def test_in_use_gpus_counts_production_states(store, svc):
    """running/starting/degraded 逐一占卡；stopped/failed/未知态不占卡。

    白名单少一个小写值 → 对应生产态不占卡 → 卡位冲突/节点占满检查静默失效；
    白名单多一个终态 → 已停模型永久占卡，节点被越积越多的僵尸行填满、再不可下发。
    record_model_states 全量覆盖同节点，故每个状态单独一拍写入后立即断言。"""
    _online(store, "w-1")
    _online(store, "w-2")
    for state, gpus in OCCUPYING:
        svc.record_model_states("w-1", {"qwen": {"state": state, "gpu": gpus}}, now=1.0)
        assert svc._in_use_gpus(["w-1"]).get("w-1") == gpus, f"{state} 必须占卡"
    for state, _ in FREE:
        svc.record_model_states("w-1", {"qwen": {"state": state, "gpu": [0, 1]}}, now=2.0)
        assert "w-1" not in svc._in_use_gpus(["w-1"]), f"{state} 不得占卡"
    # 按节点过滤：他节点的占卡不得混入本节点（gate 的冲突判定以 node 为键）
    svc.record_model_states("w-2", {"qwen": {"state": "running", "gpu": [3]}}, now=3.0)
    assert svc._in_use_gpus(["w-2"]) == {"w-2": [3]}
    assert svc._in_use_gpus(["w-1"]) == {}


def test_set_goals_skips_when_production_state_occupies_gpus(store, svc):
    """端到端：Task 9 写入的真实 state（running）必须让 gate 判卡位冲突。

    白名单与写入值零交集时本用例转红——这是"gate 卡位冲突/节点占满检查生产静默
    失效"的最小复现：夹具改用 running 后，旧实现的 `in ("READY","STARTING","UP")`
    过滤把在用集滤成空，冲突下发被放行成 worker 侧 gpu_lock 冲突的毒 goal。"""
    _online(store, "w-1")
    svc.record_model_states("w-1", {"other": {"state": "running", "gpu": [0, 1]}}, now=2.0)
    out = svc.set_goals(profile="qwen", node_ids=["w-1"], create=True, gpu_list=[0, 1])
    assert out["created"] == 0 and out["skipped"] == 1
    assert "占用" in out["verdicts"][0].reason


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
