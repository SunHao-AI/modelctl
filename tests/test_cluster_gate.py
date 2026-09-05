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
from modelctl.core.profile import KNOWN_ENGINES

# raw 必须是**真实 YAML 形状**：引擎配置段挂在引擎名键下（core/profile.py
# _to_profile: raw.get(engine)），不是字面 `engine_config` 键——夹具若用后者，
# gate 读错键也能全绿，真实 profile 却在生产恒判 1 卡（见 test_declared_gpu_count_*）
SRC = {"name": "qwen", "ok": True, "engine": "vllm", "yaml": "port: 8101\n",
       "sha": "sha256:" + "a" * 64, "version": "2026-09-04-aaaaaa",
       "raw": {"port": 8101, "vllm": {"tensor_parallel_size": 2}},
       "gpu_count": 2, "min_vram_mb": 0, "requested_gpus": []}


_UNSET = object()   # 哨兵：区分"省略该参数"与"显式传 None（节点从未上报运行时）"


def _node(nid, *, status="online", gpu_count=4, vram=40960, runtimes=_UNSET, lan="", disabled=0):
    return {"node_id": nid, "status": status, "lan_id": lan, "disabled": disabled,
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


def test_requested_gpus_override_profile_gpu_count_for_capacity():
    """显式 2 卡卡位时，容量按**实际请求的 2 卡**判（4 卡节点该放行）。

    `gpu_count=8` 模拟 Task 5 按 profile 声明算出的需要量；它不得压过用户显式点名的
    2 张卡——worker 侧 selected_gpus() 就是实际用卡数，按 8 卡判会误拦健康节点。
    （tp 与 gpu_list 的**一致性**由 _tp_conflict 单独把关，见下条。）
    """
    src = {**SRC, "gpu_count": 8, "requested_gpus": [0, 1],
           "raw": {"port": 8101, "vllm": {"tensor_parallel_size": 2}}}
    assert _one(candidates=[_node("w-1", gpu_count=4)], source=src).result == "ok"


def test_requested_gpu_count_must_match_tensor_parallel_size():
    """worker 侧 `len(gpu_list) != tensor_parallel_size` 同文案硬失败（engines/vllm.py:83）：
    中心放行 = 下发一条 worker 必拒、永不收敛的 goal。"""
    v = _one(source={**SRC, "requested_gpus": [0, 1, 2]})   # SRC raw 里 tp=2
    assert v.result == "skip" and "tensor_parallel_size=2" in v.reason
    # 一致 → 不拦
    assert _one(source={**SRC, "requested_gpus": [0, 1]}).result == "ok"
    # profile 未写 tp 键 → 适配器按 len(gpus) 兜底，天然一致，不该拦
    src = {**SRC, "requested_gpus": [0, 1], "raw": {"port": 8101, "vllm": {}}}
    assert _one(source=src).result == "ok"


def test_disabled_node_skipped_even_when_status_online():
    """disabled 位与 status 独立：rejoin 会把 status 刷回 online，只查 status 漏拦。"""
    v = _one(candidates=[_node("w-1", disabled=1)])
    assert v.result == "skip" and "停用" in v.reason


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


def test_gpu_count_table_covers_known_engines():
    """KNOWN_ENGINES 全集必须逐一落表（_GPU_COUNT_KEYS 或 _GPU_FLAG_KEYS）。

    漏表回落 gpu_count → tp 系 profile 恒判 1 卡（known-pitfalls 已沉淀过一次），
    靠人肉记忆补表必然复发，这里用集合等式把"增引擎必须补表"变成硬约束。
    """
    assert set(KNOWN_ENGINES) <= set(G._GPU_COUNT_KEYS) | set(G._GPU_FLAG_KEYS)


def test_declared_gpu_count_per_engine():
    # 段键 = 引擎名（真实 YAML 形状，见 SRC 注释）；写成 engine_config 会恒空段
    assert G.declared_gpu_count({"vllm": {"tensor_parallel_size": 4}}, "vllm") == 4
    assert G.declared_gpu_count({"llamacpp": {"gpu_count": 2}}, "llamacpp") == 2
    # 引擎段键必须与 engine 参数一致：tokenspeed 段配给 vllm 判定 = 空段 → 1
    assert G.declared_gpu_count({"tokenspeed": {"tensor_parallel_size": 8}}, "vllm") == 1
    # unsloth 的 tensor_parallel 是布尔开关不是卡数（models/unsloth/*.yaml 现实值
    # 只有 true/false；worker 要求 ≥2 卡，engines/unsloth.py:74）。按卡数 int(True)=1
    # 会把多卡 profile 判成 1 卡放行。
    assert G.declared_gpu_count({"unsloth": {"tensor_parallel": True}}, "unsloth") == 2
    assert G.declared_gpu_count({"unsloth": {"tensor_parallel": False}}, "unsloth") == 1
    # lmdeploy/tokenspeed 同样读 tensor_parallel_size（engines/lmdeploy.py、engines/tokenspeed.py
    # 实际取值口径；models/tokenspeed/qwen3.5-397b.yaml 就是 tp=8）；漏补表则回落 gpu_count → 误判 1 卡
    assert G.declared_gpu_count({"tokenspeed": {"tensor_parallel_size": 8}}, "tokenspeed") == 8
    assert G.declared_gpu_count({"lmdeploy": {"tensor_parallel_size": 1}}, "lmdeploy") == 1
    # llamacpp 缺省跟随适配器：engines/llamacpp.py:239 是 cfg.get("gpu_count", 8)，
    # 中心按 1 判会放行 worker 必拒（"gpu_count=8 超过实际 GPU 数"）的下发
    assert G.declared_gpu_count({"llamacpp": {}}, "llamacpp") == 8
    assert G.declared_gpu_count({}, "llamacpp") == 8
    # gpu_list 覆盖计数字段（适配器口径：selected_gpus() 就是实际用卡数）
    assert G.declared_gpu_count({"llamacpp": {"gpu_list": "0,1"}}, "llamacpp") == 2
    assert G.declared_gpu_count({"vllm": {"gpu_list": [0, 1]}}, "vllm") == 2
    # gpu_list 坏值（重复/非整数）不作为容量依据，回落计数字段口径
    assert G.declared_gpu_count({"llamacpp": {"gpu_list": "0,0", "gpu_count": 2}}, "llamacpp") == 2
    assert G.declared_gpu_count({"llamacpp": {"gpu_list": "x", "gpu_count": 2}}, "llamacpp") == 2
    assert G.declared_gpu_count({}, "vllm") == 1
    assert G.declared_gpu_count({"vllm": {"tensor_parallel_size": "bad"}}, "vllm") == 1
    assert G.declared_gpu_count(None, "vllm") == 1
    assert G.declared_gpu_count({"vllm": {"tensor_parallel_size": 0}}, "vllm") == 1


def test_declared_gpu_count_ignores_literal_engine_config_key():
    """`engine_config` 是 Profile 字段名，真实 YAML 原文里没有这个键。

    把它当段键读 = 恒空段（8 卡 tp profile 判 1 卡放行）。钉死"只认引擎名段键"，
    防止实现/夹具再次同步漂移回 engine_config 形状。
    """
    assert G.declared_gpu_count(
        {"engine_config": {"tensor_parallel_size": 8}}, "tokenspeed") == 1


def test_declared_gpu_count_reads_real_multicard_profiles():
    """真 profile 回归：models/ 里的多卡声明必须原样判对，不许悄悄漂成别的值。

    用 read_profile_source 读**仓库真实 YAML**（而非合成夹具）才能钉住"段键 =
    引擎名"这条口径：夹具与实现若同用 engine_config 假形状，单测全绿而生产恒 1 卡。
    """
    from modelctl.core.cluster.profiles import read_profile_source
    from modelctl.core.envfile import PROJECT_ROOT

    root = PROJECT_ROOT / "models"
    for name, engine, want in [("qwen3.5-397b", "tokenspeed", 8),    # tensor_parallel_size: 8
                               ("qwen3.8-flash-next", "unsloth", 2),  # tensor_parallel: true
                               ("qwen2.5-1.5b", "llamacpp", 1)]:      # gpu_count: 1
        src = read_profile_source(name, root, engine)
        assert src.get("ok"), src.get("reason")
        assert G.declared_gpu_count(src["raw"], engine) == want, name


def test_estimate_vram_returns_none_on_unparseable_or_missing_model():
    assert G.estimate_vram_mb({"port": 1}, "vllm", "x") is None
    assert G.estimate_vram_mb({"port": 1, "totally-unknown-engine": {"tensor_parallel_size": 1}},
                              "totally-unknown-engine", "x") is None


def test_estimate_vram_returns_int_for_known_model():
    """架构表命中必须给出确定 MB 数（原计划用例 `is None or isinstance(int)` 两侧都真，
    int 路径零验证——round(kv,1) 忘了转 int 也照样绿）。qwen3.8-27b 架构表 + 1024 token
    × fp16 → 64×4×256×2×2×1024 B = 恰好 256MB。"""
    got = G.estimate_vram_mb(
        {"port": 8101, "vllm": {"model": "/models/qwen3.8-27b", "max_model_len": 1024,
                                "tensor_parallel_size": 2}}, "vllm", "qwen")
    # 契约声明是 int：vram_estimator 返回 round(kv,1) 的 float，256.0 == 256 为真，
    # 故必须显式钉类型，否则"忘了 int() 转换"这类缺陷测不出来。
    assert got == 256 and isinstance(got, int)
    # HF 仓库名不在架构表、本地也无 config.json → None（不抛）
    assert G.estimate_vram_mb(
        {"port": 8101, "vllm": {"model": "Qwen/Qwen3-8B", "max_model_len": 32768,
                                "tensor_parallel_size": 2}}, "vllm", "qwen") is None


def test_estimate_vram_rounds_up_fractional_estimate(monkeypatch):
    """估算值是"节点至少要有这么多显存"的下界：255.4MB 必须判 256，不许 int() 截断。

    255MB 的节点会放过实际放不下的下发。整数夹具（256）对截断不敏感，故这里
    monkeypatch 估算器喂分数值——上面那条整除用例钉不住这条不变量。
    """
    import modelctl.core.vram_estimator as ve

    monkeypatch.setattr(ve, "kv_estimate_for_profile", lambda p: {"kv_total_mb": 255.4})
    assert G.estimate_vram_mb({"port": 8101, "vllm": {}}, "vllm", "qwen") == 256


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
