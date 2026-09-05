#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/goals.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/5 10:00
# @Desc   : GoalService：声明式目标的唯一写入口（§6.1/§6.6/§10.5）
# ===============================================================================

"""core/cluster/goals.py — goal 的创建/删除/期望快照（source of truth 写入侧）。

设计要点：
1) **只有 gate 判定 ok 的候选才落库**——中心从不"先写 goal 再等 worker 报错"，
   避免把明显放不下的目标推到 worker 上产生 OOM 循环（§6.6 的动机）。
2) **快照 = 全量 + 内容哈希 revision**：中心不记投递水位、不做重试。worker 上报
   自己的 revision，中心只在两者不同时带回全量快照，于是"重连/中心重启/丢帧"
   三种情况共用一条自愈路径。
3) **env_overlay 白名单**：只允许路径/卡位类键，任何名字含 API_KEY/TOKEN/... 的
   键直接拒绝。profile YAML 默认原文逐字节下发、永不插值（占位符原样保留），密钥
   因此永不出中心；仅显式 --gpus 时把生效卡位合并进引擎段 `gpu_list`（同样不插值，
   裁决A：让 worker 的 declared==requested，消除中心/worker 双源锁卡不一致），无
   --gpus 时保持纯原文透传不破"原文"铁律。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import yaml
from loguru import logger

from modelctl.core.cluster import gate, profiles
from modelctl.core.cluster.store import ClusterStore
from modelctl.core.envfile import PROJECT_ROOT

#: 测试/多实例可整体替换的 profile 读取根（默认为仓库 models/）
MODELS_DIR = PROJECT_ROOT / "models"

#: 允许中心下发的 env 覆盖键：全部是路径或卡位，无一是凭据（§6.4）
ENV_OVERLAY_ALLOWLIST: frozenset[str] = frozenset({
    "MODEL_ROOT", "MODELSCOPE_CACHE", "HF_HOME", "OLLAMA_MODELS",
    "LOG_DIR", "AUDIT_DIR", "MODELCTL_GPUS",
})

#: 键名命中任一子串即拒绝（纵深防御：防止有人把密钥塞进白名单形状的键里）
SECRET_KEY_HINTS: tuple[str, ...] = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "KEY")

#: 占卡状态词表（裁决1）：**model_states.state 判"GPU 在用"的唯一来源**，Task 7/9
#: 与计划文本都引用本常量，禁止各处再抄一份字面量。两套词表并收：
#: - 小写 = Task 9 `reconcile._STATE_OF_STAGE` 的实际写入值（running/starting/
#:   degraded 为"卡还握着"的三态；stopped/failed/pending 等终态或未起动态**不占卡**），
#:   M1 起的主词表——中心唯一写入口 `record_model_states` 透传 worker 上报，此前
#:   白名单只有大写值与生产写入值零交集，gate 的卡位冲突/占满两项检查恒空转；
#: - 大写 = M0 心跳透传与历史数据可能携带的旧值，过渡期兼容，worker 全量升级后可删。
GPU_OCCUPYING_STATES: frozenset[str] = frozenset(
    {"running", "starting", "degraded", "READY", "STARTING", "UP"})

VALID_INTENTS: tuple[str, ...] = ("start", "stop")
VALID_TARGET_ROLES: tuple[str, ...] = ("primary", "replica", "benchmark")

#: 单节点 goal 上限（防误操作 --all 打爆 worker 磁盘与心跳体积）
MAX_GOALS_PER_NODE = 512


def goal_id_of(profile: str, node_id: str) -> str:
    return f"{profile}@@{node_id}"


def validate_env_overlay(raw: dict | None) -> tuple[dict | None, str]:
    """清洗 env 覆盖项。返回 (清洗后的 dict 或 None, 错误文案)。

    None/{} 视为"无覆盖"→ (None, "")；任何越界一律整份拒绝而非静默丢键，
    否则调用方会以为"部分生效"而误判结果。
    """
    if raw is None:
        return None, ""
    if not isinstance(raw, dict):
        return None, "env_overlay 必须是映射"
    if not raw:
        return None, ""
    clean: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            return None, f"env_overlay 键必须是字符串：{key!r}"
        upper = key.strip().upper()
        if any(h in upper for h in SECRET_KEY_HINTS):
            return None, f"env_overlay 禁止下发凭据类键 {key!r}（worker 本地 .env 自行配置）"
        if upper not in ENV_OVERLAY_ALLOWLIST:
            return None, (f"env_overlay 键 {key!r} 不在白名单 "
                          f"{sorted(ENV_OVERLAY_ALLOWLIST)} 内")
        if not isinstance(value, str):
            return None, f"env_overlay.{key} 的值必须是字符串，收到 {type(value).__name__}"
        clean[upper] = value
    return clean, ""


class GoalService:
    """goal 读写编排。所有写操作都记 events（§10.7 审计）。"""

    def __init__(self, store: ClusterStore) -> None:
        self.store = store

    # ---------------- 创建 / 删除 ----------------
    def set_goals(
        self, *, profile: str, node_ids: list[str] | None, all_nodes: bool = False,
        intent: str = "start", create: bool = False, params: dict | None = None,
        env_overlay: dict | None = None, gpu_list: list[int] | None = None,
        lan_allow: list[str] | None = None, runtime_ref: str | None = None,
        target_role: str = "primary", created_by: str = "", dry_run: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """按 gate 结论批量建 goal。永不抛业务异常：一切失败以 verdicts/reason 呈现。"""
        now = time.time() if now is None else now
        if intent not in VALID_INTENTS:
            return self._abort(f"非法 intent {intent!r}（仅 {VALID_INTENTS}）")
        if target_role not in VALID_TARGET_ROLES:
            return self._abort(f"非法 target_role {target_role!r}（仅 {VALID_TARGET_ROLES}）")
        overlay, err = validate_env_overlay(env_overlay)
        if err:
            return self._abort(err)

        source = profiles.read_profile_source(profile, MODELS_DIR)
        candidates = self._candidates(node_ids=node_ids, all_nodes=all_nodes)
        if not source.get("ok"):
            # 源不可用时仍逐候选出 verdict：CLI/REST 的报告形状在"全 error"与
            # "部分 skip"下必须一致，否则调用方要为失败单独写一套渲染分支。
            verdicts = gate.evaluate_gate(
                candidates=candidates or [{"node_id": n} for n in (node_ids or [])],
                source=source, in_use={}, existing_goal_ids=set(),
                lan_allow=lan_allow or [], create=create, profile_exists={})
            report = gate.format_gate_report(verdicts, created=0, dry_run=dry_run)
            return {"verdicts": verdicts, "report": report, "created": 0, "skipped": 0,
                    "errors": len(verdicts), "reason": str(source.get("reason", ""))}
        if not candidates:
            return self._abort("无可下发节点（--node 指定的节点不存在或已 offline）")

        # 幂等集/落库/事件一律用 source["name"]（文件 stem），绝不用调用方原词：
        # 寻址名可能是展示名（Task 3 条款④），goal_id 与 worker 写盘文件名只认 stem。
        # 用原词查 existing 会让"展示名重跑"查不到已有 goal → gate 判 ok → upsert
        # 把 stage 重置回 PENDING_PROFILE_SYNC，等于把 worker 状态机清零。
        name = str(source["name"])
        # gpu_list 同时进 params（审计/回显）与 placement（gate 冲突判定用）。
        merged_params = dict(params or {})
        if gpu_list:
            merged_params["gpu_list"] = list(gpu_list)
        need_gpus = len(gpu_list) if gpu_list else gate.declared_gpu_count(source.get("raw"), source["engine"])
        est = gate.estimate_vram_mb(source.get("raw"), source["engine"], name) or 0
        # 裁决A（Task 4 终审）：显式 --gpus 必须"落地"进下发 YAML 引擎段 gpu_list。
        # worker selected_gpus() 读的是下发 YAML（非 params.gpu_list），中心只记 requested
        # 不写回 = profile 声明与中心点卡双源不一致 → 中心按 requested 判、worker 按
        # declared 锁卡的毒 goal。合并后 worker declared==requested，双源不一致从源头消除，
        # gate 零改动。仅 --gpus 时改写：无 gpu_list 保持原文逐字节透传（原文铁律不破）。
        effective_yaml = str(source["yaml"])
        effective_sha = str(source["sha"])
        if gpu_list:
            merged = _merge_gpu_list_into_yaml(effective_yaml, str(source["engine"]), list(gpu_list))
            if merged is not None:
                effective_yaml = merged
                effective_sha = profiles.profile_sha(effective_yaml)
            else:
                # 原文已能 safe_load（read_profile_source 校验过），dump 失败属极端畸形；
                # 保守回退原文下发（等价无 --gpus），绝不冒异常炸破 set_goals"永不抛"契约。
                logger.warning(f"profile {name} 的 gpu_list 合并进引擎段失败，按原文下发（worker 按 profile 声明锁卡）")
        enriched = {**source, "yaml": effective_yaml, "sha": effective_sha,
                    # 裁决4：version 对**最终落库 sha** 派生（单一哈希链）。带 --gpus 时
                    # sha 是合并后文本的哈希，version 若仍从原文 sha 派生，版本号的
                    # 13 位哈希前缀指向一份不存在于任何台账行的内容，可追溯性悬空。
                    "version": profiles.default_profile_version(effective_sha),
                    "gpu_count": need_gpus, "min_vram_mb": est,
                    "requested_gpus": list(gpu_list or [])}

        in_use = self._in_use_gpus([str(c["node_id"]) for c in candidates])
        existing = {g["goal_id"] for g in self.store.list_goals(profile=name)}
        has_profile = {str(c["node_id"]): name in (c.get("local_profiles") or [])
                       for c in candidates}
        verdicts = gate.evaluate_gate(candidates=candidates, source=enriched, in_use=in_use,
                                      existing_goal_ids=existing, lan_allow=lan_allow or [],
                                      create=create, profile_exists=has_profile)

        created = 0
        for v in verdicts:
            if v.result != gate.RESULT_OK:
                continue
            # 裁决4：上限检查必须在 dry_run 计数之前——否则 dry-run 报"预计 created=1"
            # 而实跑 skip，Task 11/12 据 dry-run 结论给退出码/提示，演练通过实跑却被
            # 静默跳过。dry-run 下 _count_for_node 读的是当前台账（演练不写库），
            # 与紧随其后的实跑起点一致，两侧结论必然相同。
            if self._count_for_node(v.node_id) >= MAX_GOALS_PER_NODE:
                v.result, v.reason = gate.RESULT_SKIP, f"节点 goal 数已达上限 {MAX_GOALS_PER_NODE}"
                continue
            if dry_run:
                created += 1
                continue
            # enriched 而非 source：_write_goal 读 min_vram_mb 进 placement，
            # 估算值只挂在 enriched 上——传 source 会让该列恒 0（死字段）。
            self._write_goal(profile=name, source=enriched, node_id=v.node_id,
                             intent=intent, params=merged_params, env_overlay=overlay,
                             gpu_count=need_gpus, runtime_ref=runtime_ref,
                             target_role=target_role, created_by=created_by, now=now)
            created += 1
        report = gate.format_gate_report(verdicts, created=created, dry_run=dry_run)
        return {"verdicts": verdicts, "report": report, "created": created,
                "skipped": sum(1 for v in verdicts if v.result == gate.RESULT_SKIP),
                "errors": sum(1 for v in verdicts if v.result == gate.RESULT_ERROR), "reason": ""}

    def remove_goals(self, *, profile: str, node_ids: list[str] | None,
                     all_nodes: bool = False, created_by: str = "",
                     now: float | None = None) -> dict[str, Any]:
        """删 goal。worker 侧的 YAML 剪枝无需中心额外传话：goal 消失后下一次快照
        就不含它，worker 的 managed 清单对照快照即知要删（Task 8 的 prune）。
        """
        now = time.time() if now is None else now
        targets = self._targets_for_removal(profile=profile, node_ids=node_ids, all_nodes=all_nodes)
        removed: list[str] = []
        for goal_id in targets:
            gone = self.store.delete_goal(goal_id)
            if gone is None:
                continue
            removed.append(goal_id)
            # 连带清运行态：残留的占卡行会让 dashboard 在 worker 已剪枝后继续显示
            # "在跑"，且 _in_use_gpus 会把它当占卡而挡住后续下发。必须按**被删 goal 行
            # 的 profile 字段**（恒为 stem）删——调用方原词可能是展示名，model_states
            # 是 worker 回流（stem 建键），拿展示名去删恒不命中。
            self.store.delete_model_state(str(gone["node_id"]), str(gone["profile"]))
            self.store.append_event("goal.delete", node_id=str(gone["node_id"]), goal_id=goal_id,
                                    payload={"profile": profile, "operator": created_by}, now=now)
        missing = [g for g in targets if g not in removed]
        # report 面向操作者并点名 profile：撤的是哪个模型是运维唯一能确认的线索。
        report = (f"profile {profile}：已删除 {len(removed)} 个 goal"
                  + (f"；不存在 {len(missing)} 个" if missing else "")
                  if targets else f"profile {profile} 无任何 goal")
        return {"removed": removed, "missing": missing, "report": report}

    # ---------------- 下发快照（心跳 ack 捎带）----------------
    def snapshot_for(self, node_id: str) -> dict[str, Any]:
        """该节点的全量期望状态。revision 是内容哈希：同一 goal 集在中心重启后同值，
        故 worker 端"要不要重写盘"的判据在两侧都稳定。空节点用空串（不是哈希）。
        """
        rows = self.store.list_goals(node_id=node_id)
        goals = [{"goal_id": g["goal_id"], "profile": g["profile"], "engine": g["engine"],
                  "yaml": g["profile_yaml"], "sha": g["profile_sha"],
                  "version": g.get("profile_version") or "", "intent": g["intent"],
                  "params": g.get("params"), "env_overlay": g.get("env_overlay")} for g in rows]
        if not goals:
            return {"revision": "", "goals": []}
        canon = json.dumps(goals, sort_keys=True, ensure_ascii=False)
        return {"revision": hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16], "goals": goals}

    # ---------------- worker 回流（Task 7 调用）----------------
    def mark_stage(self, goal_id: str, stage: str, *, reason: str = "",
                   error_class: str = "", now: float | None = None) -> None:
        """回写 goal 生命周期阶段。goal 可能已被删（worker 状态滞后）→ 静默忽略。"""
        self.store.update_goal(goal_id, now=time.time() if now is None else now,
                               stage=stage, stage_reason=reason[:500], error_class=error_class)

    def record_model_states(self, node_id: str, profiles_reported: dict, now: float) -> None:
        """心跳回流：全量覆盖该节点的 model_states（空 dict 表示"当前无在跑模型"）。"""
        seen: set[str] = set()
        for name, info in profiles_reported.items():
            if not isinstance(info, dict) or not profiles.is_safe_name(str(name)):
                continue
            seen.add(str(name))
            self.store.upsert_model_state(node_id=node_id, profile=str(name),
                                          state=str(info.get("state", "unknown"))[:32],
                                          gpu=_int_list(info.get("gpu")),
                                          port=_safe_int(info.get("port")),
                                          pid=_safe_int(info.get("pid")),
                                          reason=str(info.get("reason", ""))[:500],
                                          error_class=str(info.get("error_class", ""))[:64],
                                          now=now)
        for row in self.store.list_model_states(node_id=node_id):
            if row["profile"] not in seen:
                self.store.delete_model_state(node_id, row["profile"])

    # ---------------- 内部 ----------------
    def _abort(self, reason: str) -> dict[str, Any]:
        return {"verdicts": [], "report": "", "created": 0, "skipped": 0, "errors": 0, "reason": reason}

    def _candidates(self, *, node_ids: list[str] | None, all_nodes: bool) -> list[dict[str, Any]]:
        """gate 候选 = 台账行（含 capacity/runtimes/local_profiles）；指定 --node 时保序。"""
        rows = {n["node_id"]: n for n in self.store.list_nodes()}
        if all_nodes:
            return [n for n in self.store.list_nodes() if not n.get("disabled")]
        return [rows[n] for n in (node_ids or []) if n in rows]

    def _in_use_gpus(self, node_ids: list[str]) -> dict[str, list[int]]:
        """在用 GPU：来自 model_states 心跳回流中处于占卡状态（GPU_OCCUPYING_STATES）
        的行。worker 侧 gpu_lock 真值只在本地，中心 M1 拿不到，不做回退上报路径。
        """
        out: dict[str, list[int]] = {}
        for row in self.store.list_model_states():
            if row["node_id"] not in node_ids or row["state"] not in GPU_OCCUPYING_STATES:
                continue
            for g in (row.get("gpu") or []):
                out.setdefault(row["node_id"], []).append(int(g))
        return out

    def _count_for_node(self, node_id: str) -> int:
        return len(self.store.list_goals(node_id=node_id))

    def _write_goal(self, *, profile: str, source: dict, node_id: str, intent: str,
                    params: dict, env_overlay: dict | None, gpu_count: int,
                    runtime_ref: str | None, target_role: str, created_by: str, now: float) -> None:
        goal_id = goal_id_of(profile, node_id)
        existed = self.store.get_goal(goal_id) is not None
        self.store.upsert_goal(
            goal_id=goal_id, node_id=node_id, profile=profile, engine=str(source["engine"]),
            profile_yaml=str(source["yaml"]), profile_sha=str(source["sha"]),
            profile_version=str(source.get("version") or ""), intent=intent,
            params=params or None, env_overlay=env_overlay,
            placement={"gpu_count": gpu_count, "min_vram_mb": int(source.get("min_vram_mb") or 0)},
            runtime_ref=runtime_ref, target_role=target_role,
            stage="PENDING_PROFILE_SYNC", created_by=created_by, now=now)
        self.store.append_event("goal.update" if existed else "goal.create", node_id=node_id,
                                goal_id=goal_id,
                                payload={"profile": profile, "intent": intent,
                                         "engine": source["engine"], "operator": created_by}, now=now)

    def _targets_for_removal(self, *, profile: str, node_ids: list[str] | None,
                             all_nodes: bool) -> list[str]:
        """remove/stop 侧的展示名归一（裁决2）：候选名 = stem 优先 + 原词兜底。

        set 侧已归一（source["name"]），remove 侧不归一会让展示名寻址全进 missing、
        --all 查库查不到——同一 CLI 参数在 set/remove 两种语义是体验分裂。
        """
        names = self._resolve_names(profile)
        if all_nodes:
            seen: dict[str, None] = {}
            for name in names:
                for g in self.store.list_goals(profile=name):
                    seen.setdefault(str(g["goal_id"]), None)
            return list(seen)
        out: dict[str, None] = {}
        for name in names:
            for n in (node_ids or []):
                out.setdefault(goal_id_of(name, n), None)
        return list(out)

    def _resolve_names(self, profile: str) -> list[str]:
        """寻址名 → 候选规范名列表：读得到源 → [stem, 原词]；读不到 → [原词]。

        读取失败**必须**回退单元素原词而非报错/空表——profile 文件被删后仍要能撤掉
        台账 goal（撤模型 = 删 YAML 与撤 goal 是两个可任意先后的操作，归一失败不得
        连带撤不掉）。stem==原词时天然去重为单元素。read_profile_source 契约是
        "绝不抛异常"，故只看 ok 位。
        """
        source = profiles.read_profile_source(profile, MODELS_DIR)
        if source.get("ok"):
            return list(dict.fromkeys([str(source["name"]), profile]))
        return [profile]


def _safe_int(value: Any) -> int | None:
    """心跳里的 port/pid 来自 worker，坏值按"未上报"处理（None），绝不让回流炸掉。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _int_list(value: Any) -> list[int] | None:
    """GPU 卡位列表容错解析：非列表 → None；逐项只收 int，截断到 64 防心跳放大。"""
    if not isinstance(value, list):
        return None
    return [v for v in value if isinstance(v, int) and not isinstance(v, bool)][:64]


def _merge_gpu_list_into_yaml(text: str, engine: str, gpu_list: list[int]) -> str | None:
    """把生效卡位合并进 YAML 引擎段 `gpu_list`（裁决A）；无法安全合并返回 None。

    只做一处最小改动：safe_load → `data[engine]["gpu_list"] = gpu_list` → safe_dump。
    绝不插值（`${VAR}` 原样保留），"密钥不出中心"铁律不破；sort_keys=False 保键序、
    allow_unicode 保中文、width 拉大避免长卡位列表折行影响 sha 观感。段缺失/顶层或段
    非映射/dump 抛错一律返回 None——调用方据此回退原文下发，宁可退回"等价无 --gpus"，
    也不产出畸形 YAML，更不抛异常炸破 set_goals 的"永不抛业务异常"契约（REST 层不转 500）。
    """
    try:
        data = yaml.safe_load(text)
        if not isinstance(data, dict) or not isinstance(data.get(engine), dict):
            return None
        data[engine]["gpu_list"] = list(gpu_list)
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                              default_flow_style=False, width=4096)
    except Exception:  # noqa: BLE001 — RecursionError/yaml 等绝不冒泡（契约同 profiles._scan）
        return None
