#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/gate.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/5 01:25
# @Desc   : placement gate：goal 下发前的中心侧容量/运行时/冲突校验（§6.6）
# ===============================================================================

"""core/cluster/gate.py — 下发校验门禁（纯函数；Ray Placement Group 借鉴）。

只做**粗筛**：中心掌握的节点事实全部来自心跳（capacity/runtimes/在用 GPU），
卡位分配的真正裁决者是 worker 侧 gpu_lock。中心拦掉"明显放不下"的候选，边界
情形由 worker 侧兜底并上报 error_class=gpu_lock——与 §6.6 目标一致，但把精确
性责任放在信息完整的一侧。

显存估算刻意**不做 ${VAR} 插值**：直接以未插值的 raw dict 构造 Profile，因为中心
.env 未必定义 worker 侧变量（缺变量时 core.profile.load_profile 会抛 ProfileError）。
估算失败一律返回 None → 跳过该维度校验，绝不因估算不可得而阻断下发。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from modelctl.core.colors import display_width, pad_width

RESULT_OK, RESULT_SKIP, RESULT_ERROR = "ok", "skip", "error"

#: 可参与下发的节点状态（offline/disabled 不发；stale 允许——心跳仍在只是迟缓，
#: 中心拒发只会让 goal 永远不落，不如放行由 worker 侧兜底，Task 5 的 --all 同样收 stale）
_GATEABLE = ("online", "stale")

#: 各引擎"用几张卡"的字段名（与 core.vram_estimator._ctx_tokens_and_gpus 及
#: engines/*.py 实际读取的字段同源）。**KNOWN_ENGINES 增删引擎必须同步补表**：
#: 漏项回落到 `gpu_count`，而 tp 系 profile 根本没这个键——tokenspeed 8 卡 profile
#: 会被当 1 卡放行，容量维度形同虚设（错误要到 worker 启动才暴露）。
_GPU_COUNT_KEYS = {
    "vllm": "tensor_parallel_size",
    "sglang": "tensor_parallel_size",
    "aphrodite": "tensor_parallel_size",
    "tensorrt_llm": "tensor_parallel_size",
    "lmdeploy": "tensor_parallel_size",
    "tokenspeed": "tensor_parallel_size",
    "llamacpp": "gpu_count",
    "unsloth": "tensor_parallel",
}

_MARKS = {"ok": "OK  ", "skip": "SKIP", "error": "ERR "}


@dataclass
class NodeVerdict:
    node_id: str
    result: str
    reason: str = ""


def declared_gpu_count(raw: Any, engine: str) -> int:
    """从 profile 原文推断所需 GPU 数；任何异常/缺字段一律保守取 1。"""
    if not isinstance(raw, dict):
        return 1
    ec = raw.get("engine_config")
    if not isinstance(ec, dict):
        return 1
    key = _GPU_COUNT_KEYS.get(engine, "gpu_count")
    try:
        return max(1, int(ec.get(key, 1) or 1))
    except (TypeError, ValueError):
        return 1


def _safe_port(value: Any) -> int:
    """port 容错解析（profile 原文里可能是 str/int/None；估算路径不参与实际启动）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def estimate_vram_mb(raw: Any, engine: str, name: str) -> int | None:
    """KV 显存估算（MB）；不可估算一律 None（gate 跳过该维度，不阻断下发）。

    复用仓库既有 vram_estimator，不重复实现模型架构表。`engine` 不在
    vram_estimator 支持范围时它内部返回 None，故这里无需引擎白名单。
    """
    if not isinstance(raw, dict):
        return None
    try:
        from modelctl.core.profile import Profile
        from modelctl.core.vram_estimator import kv_estimate_for_profile

        ec = raw.get("engine_config")
        profile = Profile(name=name, engine=engine, port=_safe_port(raw.get("port")),
                          engine_config=ec if isinstance(ec, dict) else {})
        est = kv_estimate_for_profile(profile)
    except Exception:  # noqa: BLE001 — 估算在任何异常下都不得影响下发决策
        return None
    if not isinstance(est, dict) or est.get("kv_total_mb") is None:
        return None
    return int(est["kv_total_mb"])


def evaluate_gate(
    *,
    candidates: list[dict[str, Any]],
    source: dict[str, Any],
    in_use: dict[str, list[int]],
    existing_goal_ids: set[str],
    lan_allow: list[str],
    create: bool,
    profile_exists: dict[str, bool],
) -> list[NodeVerdict]:
    """逐候选判定（保序）。`source.ok=False` 时全部 error 并短路——源本身坏了，
    逐节点判定无意义且会产出误导性 reason。

    参数（由 Task 5 的 GoalService 组装）：
      in_use          node_id → 该节点已被占用的 GPU 序号列表
      existing_goal_ids 已存在的 goal_id 集合（`<profile>@@<node_id>`），用于幂等
      lan_allow       LAN 白名单（空 = 不限）
      profile_exists  node_id → 该节点本地是否已有同名 profile 文件（心跳上报）
    """
    if not source.get("ok"):
        reason = str(source.get("reason", "profile 源不可用"))
        return [NodeVerdict(str(c.get("node_id", "")), RESULT_ERROR, reason) for c in candidates]

    name = str(source.get("name", ""))
    engine = str(source.get("engine", ""))
    need_gpus = int(source.get("gpu_count") or declared_gpu_count(source.get("raw"), engine))
    min_vram = int(source.get("min_vram_mb") or 0)
    requested = [g for g in (source.get("requested_gpus") or []) if isinstance(g, int)]

    verdicts: list[NodeVerdict] = []
    for node in candidates:
        nid = str(node.get("node_id", ""))
        verdicts.append(_verdict_one(
            node_id=nid, node=node, name=name, engine=engine, need_gpus=need_gpus,
            min_vram=min_vram, requested=requested, in_use=in_use.get(nid) or [],
            exists=f"{name}@@{nid}" in existing_goal_ids, lan_allow=lan_allow,
            create=create, has_profile=bool(profile_exists.get(nid, False))))
    return verdicts


def _verdict_one(*, node_id: str, node: dict[str, Any], name: str, engine: str, need_gpus: int,
                 min_vram: int, requested: list[int], in_use: list[int], exists: bool,
                 lan_allow: list[str], create: bool, has_profile: bool) -> NodeVerdict:
    status = str(node.get("status", ""))
    if status not in _GATEABLE:
        return NodeVerdict(node_id, RESULT_SKIP, f"节点状态 {status or '未知'} 不可下发（需 online/stale）")
    if exists:
        return NodeVerdict(node_id, RESULT_SKIP, "goal 已存在（幂等跳过；改参请走 PUT 或先 remove）")
    if lan_allow and str(node.get("lan_id") or "") not in lan_allow:
        return NodeVerdict(node_id, RESULT_SKIP,
                           f"LAN {node.get('lan_id') or '未标注'} 不在 --lan-allow {lan_allow} 内")
    runtimes = node.get("runtimes")
    ok_runtime = bool(isinstance(runtimes, dict) and isinstance(runtimes.get(engine), dict)
                      and runtimes[engine].get("ok"))
    if not ok_runtime:
        detail = "（节点未上报运行时信息）" if not runtimes else ""
        return NodeVerdict(node_id, RESULT_SKIP,
                           f"engine {engine} 在 {node_id} 上不可用{detail}，需先 modelctl env setup {engine}")

    capacity = node.get("capacity")
    if isinstance(capacity, dict):
        have_gpus = capacity.get("gpu_count")
        if isinstance(have_gpus, int) and have_gpus < need_gpus:
            return NodeVerdict(node_id, RESULT_SKIP,
                               f"gpu_count 不足：需 {need_gpus} 卡，节点仅 {have_gpus} 卡")
        total_vram = capacity.get("vram_total_mb")
        if min_vram and isinstance(total_vram, int) and total_vram < min_vram:
            return NodeVerdict(node_id, RESULT_SKIP,
                               f"vram 不足：估算需 ≥{min_vram}MB，节点共 {total_vram}MB")

    busy = {g for g in in_use if isinstance(g, int)}
    clash = sorted(set(requested) & busy)
    if clash:
        return NodeVerdict(node_id, RESULT_SKIP,
                           f"GPU {clash} 已被在用模型占用（gpu_list 冲突，请改用空闲卡位）")
    if isinstance(capacity, dict) and isinstance(capacity.get("gpu_count"), int):
        if capacity["gpu_count"] and len(busy) >= capacity["gpu_count"]:
            return NodeVerdict(node_id, RESULT_SKIP,
                               f"节点 GPU 已占满（{sorted(busy)}），无空卡可分配")

    note = "" if has_profile else "（profile 文件待同步）"
    if not has_profile and not create:
        return NodeVerdict(node_id, RESULT_SKIP,
                           f"{node_id} 上无 profile {name}，未加 --create 故跳过")
    return NodeVerdict(node_id, RESULT_OK, f"engine={engine} gpu_count={need_gpus}{note}")


def format_gate_report(verdicts: list[NodeVerdict], *, created: int, dry_run: bool) -> str:
    """逐节点一行的报告（CJK 安全对齐；CLI 直接 print，不含 ANSI）。

    created 是"将创建/已创建"的 goal 数：dry-run 下为预计数，实跑下为实际数，
    文案前缀 [dry-run] 让调用方与测试都能区分演练与真实下发。

    宽度必须按 `display_width` 取：node_id 由运维在 `cluster join --node-id` 时自
    定义（CLAUDE.md 例外条款：集群视图直接显示它），含 CJK 时 `len()` 会算窄列位，
    pad_width 补齐后整表右移错位。
    """
    width = max((display_width(_MARKS.get(v.result, v.result)) for v in verdicts), default=4)
    nid_width = max((display_width(v.node_id) for v in verdicts), default=5)
    head = "[dry-run] " if dry_run else ""
    lines = []
    for v in verdicts:
        mark = head + _MARKS.get(v.result, v.result)
        lines.append(f"{pad_width(mark, len(head) + width)}  {pad_width(v.node_id, nid_width)}  {v.reason}")
    tail = f"（dry-run：预计 created={created}）" if dry_run else f"（created: {created}）"
    lines.append(f"{head}summary: {tail}")
    return "\n".join(lines)
