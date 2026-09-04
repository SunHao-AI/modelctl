#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_gate.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/5 01:25
# @Desc   : placement gate 全分支测试（运行时/容量/冲突/LAN/幂等/创建/源失败/报告）
# ===============================================================================

from modelctl.core.cluster import gate as G
from modelctl.core.colors import display_width

SRC = {"name": "qwen", "ok": True, "engine": "vllm", "yaml": "port: 8101\n",
       "sha": "sha256:" + "a" * 64, "version": "2026-09-04-aaaaaa",
       "raw": {"port": 8101, "engine_config": {"tensor_parallel_size": 2}},
       "gpu_count": 2, "min_vram_mb": 0, "requested_gpus": []}


_UNSET = object()   # 哨兵：区分"省略该参数"与"显式传 None（节点从未上报运行时）"


def _node(nid, *, status="online", gpu_count=4, vram=40960, runtimes=_UNSET, lan=""):
    return {"node_id": nid, "status": status, "lan_id": lan,
            "capacity": {"gpu_count": gpu_count, "vram_total_mb": vram},
            "runtimes": {"vllm": {"ok": True}} if runtimes is _UNSET else runtimes}


def _ev(candidates=None, **over):
    kw = dict(candidates=[_node("w-1")], source=SRC, in_use={}, existing_goal_ids=set(),
              lan_allow=[], create=True, profile_exists={"w-1": True})
    kw.update(over)
    if candidates is not None:
        kw["candidates"] = candidates
    return G.evaluate_gate(**kw)


def _one(**over):
    return _ev(**over)[0]


def test_runtime_probe_failure_skips_with_setup_hint():
    v = _one(candidates=[_node("w-1", runtimes={"vllm": {"ok": False}})])
    assert v.result == "skip" and "env setup vllm" in v.reason


def test_runtime_unknown_is_treated_as_unavailable():
    """runtimes 为 None（节点从未上报）→ 保守 skip，避免盲发导致 worker 侧报错。"""
    assert _one(candidates=[_node("w-1", runtimes=None)]).result == "skip"


def test_gpu_capacity_shortage():
    v = _one(candidates=[_node("w-1", gpu_count=1)])
    assert v.result == "skip" and "gpu_count" in v.reason


def test_vram_capacity_shortage():
    v = _one(candidates=[_node("w-1", vram=1024)], source={**SRC, "min_vram_mb": 40960})
    assert v.result == "skip" and "vram" in v.reason


def test_gpu_conflict_with_in_use_sets():
    v = _one(source={**SRC, "requested_gpus": [1, 2]}, in_use={"w-1": [2, 3]})
    assert v.result == "skip" and "2" in v.reason and "GPU" in v.reason


def test_all_gpus_busy_blocks_even_without_request():
    v = _one(candidates=[_node("w-1", gpu_count=2)], in_use={"w-1": [0, 1]})
    assert v.result == "skip" and "已占满" in v.reason


def test_lan_allow_filter():
    v = _one(candidates=[_node("w-1", lan="lan-9")], lan_allow=["lan-1", "lan-2"])
    assert v.result == "skip" and "lan" in v.reason.lower()


def test_existing_goal_skipped_for_idempotency():
    assert _one(existing_goal_ids={"qwen@@w-1"}).reason.count("已存在") == 1


def test_missing_profile_without_create_is_skip():
    v = _one(create=False, profile_exists={"w-1": False})
    assert v.result == "skip" and "--create" in v.reason


def test_missing_profile_with_create_is_ok_and_flagged():
    v = _one(create=True, profile_exists={"w-1": False})
    assert v.result == "ok" and "待同步" in v.reason


def test_offline_candidate_skipped():
    assert "offline" in _one(candidates=[_node("w-1", status="offline")]).reason


def test_stale_candidate_allowed():
    """stale 必须在白名单内：Task 5 的 --all 会收 stale 节点，gate 若拒发则 goal 永不落。"""
    assert _one(candidates=[_node("w-1", status="stale")]).result == "ok"


def test_source_failure_is_error_and_short_circuits():
    got = _ev(candidates=[_node("w-1"), _node("w-2")], source={"name": "qwen", "ok": False,
                                                               "reason": "YAML 语法错误"})
    assert [v.result for v in got] == ["error", "error"]
    assert got[0].reason == "YAML 语法错误"


def test_candidate_order_and_reasons_preserved():
    got = _ev(candidates=[_node("w-1", vram=1024), _node("w-2", gpu_count=1), _node("w-3")],
              source={**SRC, "min_vram_mb": 40960})
    assert [v.node_id for v in got] == ["w-1", "w-2", "w-3"]
    assert [v.result for v in got] == ["skip", "skip", "ok"]


def test_no_capacity_reported_skips_vram_and_gpu_checks():
    """老 worker 未上报 capacity（None）：容量维度不可判，交由 worker 侧兜底 → ok。"""
    n = {"node_id": "w-old", "status": "online", "lan_id": "", "capacity": None,
         "runtimes": {"vllm": {"ok": True}}}
    assert _one(candidates=[n], source={**SRC, "min_vram_mb": 999999}).result == "ok"


def test_need_gpus_falls_back_to_profile_when_source_omits_gpu_count():
    """Task 5 未附加 gpu_count 时必须回读 profile 事实（raw 里 tp=2）：1 卡节点该拒。

    回落缺失（恒取 1 卡）会让"节点只有 1 卡却下发 tp=2"一路放行到 worker 才失败。
    """
    src = {k: v for k, v in SRC.items() if k != "gpu_count"}
    v = _one(candidates=[_node("w-1", gpu_count=1)], source=src)
    assert v.result == "skip" and "需 2 卡" in v.reason


def test_declared_gpu_count_per_engine():
    assert G.declared_gpu_count({"engine_config": {"tensor_parallel_size": 4}}, "vllm") == 4
    assert G.declared_gpu_count({"engine_config": {"gpu_count": 2}}, "llamacpp") == 2
    # unsloth 的字段名是 tensor_parallel（与 vram_estimator._ctx_tokens_and_gpus 同源，非 _size）
    assert G.declared_gpu_count({"engine_config": {"tensor_parallel": 3}}, "unsloth") == 3
    # lmdeploy/tokenspeed 同样读 tensor_parallel_size（engines/lmdeploy.py、engines/tokenspeed.py
    # 实际取值口径；models/tokenspeed/qwen3.5-397b.yaml 就是 tp=8）；漏补表则回落 gpu_count → 误判 1 卡
    assert G.declared_gpu_count({"engine_config": {"tensor_parallel_size": 8}}, "tokenspeed") == 8
    assert G.declared_gpu_count({"engine_config": {"tensor_parallel_size": 1}}, "lmdeploy") == 1
    assert G.declared_gpu_count({}, "vllm") == 1
    assert G.declared_gpu_count({"engine_config": {"tensor_parallel_size": "bad"}}, "vllm") == 1
    assert G.declared_gpu_count(None, "vllm") == 1
    assert G.declared_gpu_count({"engine_config": {"tensor_parallel_size": 0}}, "vllm") == 1


def test_estimate_vram_returns_none_on_unparseable_or_missing_model():
    assert G.estimate_vram_mb({"port": 1}, "vllm", "x") is None
    assert G.estimate_vram_mb({"port": 1, "engine_config": {"tensor_parallel_size": 1}},
                              "totally-unknown-engine", "x") is None


def test_estimate_vram_returns_int_for_known_model():
    """架构表命中必须给出确定 MB 数（原计划用例 `is None or isinstance(int)` 两侧都真，
    int 路径零验证——round(kv,1) 忘了转 int 也照样绿）。qwen3.8-27b 架构表 + 1024 token
    × fp16 → 64×4×256×2×2×1024 B = 恰好 256MB。"""
    got = G.estimate_vram_mb(
        {"port": 8101, "engine_config": {"model": "/models/qwen3.8-27b", "max_model_len": 1024,
                                         "tensor_parallel_size": 2}}, "vllm", "qwen")
    # 契约声明是 int：vram_estimator 返回 round(kv,1) 的 float，256.0 == 256 为真，
    # 故必须显式钉类型，否则"忘了 int() 转换"这类缺陷测不出来。
    assert got == 256 and isinstance(got, int)
    # HF 仓库名不在架构表、本地也无 config.json → None（不抛）
    assert G.estimate_vram_mb(
        {"port": 8101, "engine_config": {"model": "Qwen/Qwen3-8B", "max_model_len": 32768,
                                         "tensor_parallel_size": 2}}, "vllm", "qwen") is None


def test_report_lists_every_node_with_result_and_counts():
    got = _ev(candidates=[_node("w-1", vram=1024), _node("w-2")], source={**SRC, "min_vram_mb": 40960})
    text = G.format_gate_report(got, created=1, dry_run=True)
    assert "w-1" in text and "w-2" in text
    assert text.startswith("[dry-run]") and "created=1" in text
    assert len(text.strip().split("\n")) == 3     # 两节点行 + 一行 summary


def test_report_dry_run_prefix_marker_for_cli():
    """CLI 靠 `[dry-run]` 前缀区分演练与真实下发（供测试断言）。"""
    text = G.format_gate_report([G.NodeVerdict("w-1", "ok")], created=0, dry_run=True)
    assert text.startswith("[dry-run]")
    assert not G.format_gate_report([G.NodeVerdict("w-1", "ok")], created=1, dry_run=False).startswith("[dry-run]")


def test_report_pads_cjk_node_id_by_display_width():
    """node_id 由运维自定义（可含中文），列宽必须按显示宽度算，否则整表右移错位。

    钉的是"reason 起始显示列逐行一致"这个不变量：宽度若按 `len()` 算，
    `算力节点-2`（len 6 / 显示 10）会把该行 reason 右推 4 列。
    """
    got = [G.NodeVerdict("w-1", "ok", "理由甲"),
           G.NodeVerdict("算力节点-2", "skip", "理由乙"),
           G.NodeVerdict("w-33", "error", "理由丙")]
    lines = G.format_gate_report(got, created=1, dry_run=False).split("\n")
    starts = {display_width(line[:line.index(v.reason)])
              for line, v in zip(lines[: len(got)], got, strict=True)}
    assert len(starts) == 1, lines
