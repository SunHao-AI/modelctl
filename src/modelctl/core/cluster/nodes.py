#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/nodes.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : 中心侧节点注册/心跳/lease 扫描/脱敏视图编排
# ===============================================================================

"""core/cluster/nodes.py — 中心 NodeRegistry（设计文档 §5、§10.2）。

纯逻辑可单测；admin_cluster 仅做 HTTP/WS 薄封装。
"""

from __future__ import annotations

import time
from typing import Any

from modelctl.core.cluster import config, tokens, wsproto
from modelctl.core.cluster.goals import GoalService
from modelctl.core.cluster.store import ClusterStore, mask_tail
from modelctl.core.cluster.wsproto import HelloMsg, make_welcome

MAX_QUEUED_ACTIONS = 16

_JOIN_TOKEN_META_KEY = "join_token"


def capacity_text(capacity: dict | None) -> str:
    """把心跳上报的容量映射格式化为"4 卡 / 154 GiB"。缺任一字段一律 "-"。

    vram_total_mb → GiB 四舍五入取整（展示用，精确值仍在 capacity 原始字段里）。
    """
    if not isinstance(capacity, dict):
        return "-"
    gpus, vram = capacity.get("gpu_count"), capacity.get("vram_total_mb")
    if not isinstance(gpus, int) or not isinstance(vram, int) or gpus <= 0 or vram <= 0:
        return "-"
    return f"{gpus} 卡 / {round(vram / 1024)} GiB"


class AuthError(Exception):
    """hello 鉴权失败：key 既不是 join_token 也不匹配任何 node_token。"""


class NodeRegistry:
    def __init__(self, store: ClusterStore, goals: GoalService | None = None) -> None:
        self.store = store
        self.goals = goals
        # node_id → 待下发 action 帧队列（心跳 ack 取空）。进程内即可：中心重启后
        # 队列清空是可接受的——worker 侧 reconciler 会自行把实际状态逼向本地 goal。
        self._actions: dict[str, list[dict[str, Any]]] = {}
        self._seq_base: dict[str, int] = {}
        # 待处理的"强制全量 sync"标记（REST 打标 → 下一枚 ack 消费掉）
        self._force_sync: set[str] = set()
        # node_id → 上次已上报的漂移集合，用于"只在变化时记事件"
        self._drift_seen: dict[str, set[str]] = {}

    def ensure_join_token(self) -> str:
        existing = self.store.get_meta(_JOIN_TOKEN_META_KEY)
        if existing:
            return existing
        fresh = tokens.new_join_token()
        self.store.set_meta(_JOIN_TOKEN_META_KEY, fresh)
        return fresh

    def handle_hello(self, hello: HelloMsg) -> tuple[dict[str, Any], str]:
        if not hello.node_id:
            raise AuthError("hello 缺少 node_id")
        join_token = self.ensure_join_token()
        if tokens.token_matches(hello.key, join_token):
            node_token = tokens.new_node_token()  # 首次 join：签发节点专属 token
        else:
            known = self.store.find_node_by_token(hello.key)
            if known is None:
                raise AuthError("无效的 join/node token")
            # node_token 与签发时的 node_id 绑定：防止持自己的 NT 冒充他人 node_id，
            # 经 upsert 的 ON CONFLICT 覆盖受害者行的 node_token（跨节点身份劫持）
            if str(known["node_id"]) != hello.node_id:
                raise AuthError("node_id 与节点令牌不匹配")
            node_token = str(known["node_token"])  # 重连：沿用既有 token
        engines = hello.meta.get("engines")
        self.store.upsert_node(
            node_id=hello.node_id, node_token=node_token, lan_id=hello.lan,
            role="worker", host_ip=str(hello.meta.get("host_ip", "")),
            hostname=str(hello.meta.get("hostname", "")),
            engines=engines if isinstance(engines, dict) else None,
            now=time.time(),
        )
        self.store.append_event("node.join", node_id=hello.node_id)
        welcome = make_welcome(node_token, config.heartbeat_interval_s(), config.lease_s())
        return welcome, hello.node_id

    # ---- 心跳回流 + ack 组装（M1）----
    def handle_heartbeat(self, node_id: str, hb: dict[str, Any], now: float) -> dict[str, Any]:
        """落库 worker 事实，并组装 ack（sync 捎带 + action 投递）。

        `hb` 必须是 wsproto.parse_heartbeat_v2 的输出。三段落库遵循同一原则：
        **None 表示 worker 未上报（保留既有事实），[]/{} 表示明确为空（照实覆盖）**
        ——混用会让旧版 worker 每 10s 把新版写入的容量/模型状态抹成空。
        """
        self.store.touch_heartbeat(node_id, now=now, lease_s=config.lease_s())
        self.store.update_node_capacity(
            node_id, capacity=hb.get("capacity"), runtimes=hb.get("runtimes"),
            local_profiles=hb.get("local_profiles"), now=now)

        profiles = hb.get("profiles")
        if self.goals is not None and isinstance(profiles, dict):
            self.goals.record_model_states(node_id, profiles, now)
            self._sync_stages(node_id, profiles, now)

        ack: dict[str, Any] = {"t": "ack"}
        if self.goals is not None:
            snapshot = self.goals.snapshot_for(node_id)
            reported = ""
            goal_sync = hb.get("goal_sync")
            if isinstance(goal_sync, dict):
                reported = str(goal_sync.get("revision", ""))
            # forced：REST 打标的"强制全量 sync"。即便 revision 与 worker 上报值一致
            # 也必须带快照——这正是"强制"的全部含义：worker 本地文件被改坏而 revision
            # 未变，短路会让漂移永不自愈。标记一次性（消费即清），不会每拍白传全量。
            forced = self._consume_force_sync(node_id)
            if snapshot["revision"] != reported or forced:
                ack["sync"] = dict(snapshot, force=True) if forced else snapshot
                self.store.set_node_last_goal_sync_sha(node_id, snapshot["revision"])
        self._record_drift(node_id, hb.get("drift"), now=now)
        actions = self.drain_actions(node_id)
        if actions:
            ack["actions"] = actions
        return ack

    def _sync_stages(self, node_id: str, profiles: dict[str, Any], now: float) -> None:
        """把 worker 上报的 goal 阶段回写 goals 表（只认本节点声明过的 profile）。"""
        goals = self.goals
        if goals is None:  # 调用点已保证非 None，此守卫仅为类型窄化（mypy）
            return
        for goal in self.store.list_goals(node_id=node_id):
            info = profiles.get(str(goal["profile"]))
            if not isinstance(info, dict):
                continue
            stage = str(info.get("stage", ""))
            if not stage:
                continue
            goals.mark_stage(str(goal["goal_id"]), stage,
                             reason=str(info.get("reason", "")),
                             error_class=str(info.get("error_class", "")), now=now)

    def _record_drift(self, node_id: str, drift: Any, *, now: float) -> None:
        """漂移是持续状态：只在集合发生新增时记事件，避免每心跳刷一条。"""
        if not isinstance(drift, list):
            return
        current = {str(d) for d in drift}
        previous = self._drift_seen.get(node_id, set())
        for goal_id in sorted(current - previous):
            self.store.append_event("goal.drift", node_id=node_id, goal_id=goal_id,
                                    payload={"message": "worker 本地 profile 与中心声明不一致"},
                                    now=now)
        if current:
            self._drift_seen[node_id] = current
        else:
            self._drift_seen.pop(node_id, None)

    # ---- 指令队列（REST/WS 写入，心跳 ack 取走）----
    def push_action(self, node_id: str, action: str, *, goal_id: str = "",
                    profile: str = "") -> bool:
        """排队一条指令。节点离线也保留（等其回连）；超出上限拒绝而非静默丢弃。"""
        queue = self._actions.setdefault(node_id, [])
        if len(queue) >= MAX_QUEUED_ACTIONS:
            return False
        queue.append(wsproto.make_action(len(queue) + 1, action,
                                         goal_id=goal_id, profile=profile))
        return True

    def drain_actions(self, node_id: str) -> list[dict[str, Any]]:
        """取空队列并统一编号（seq 用全局递增的进程内计数，保证同连接内不重复）。

        入队时的 seq 只用于人读；真正给 worker 的 seq 在此重排，避免"队列被取空
        后再次入队"产生与历史 seq 撞号，导致 worker 把新指令当旧回执。
        """
        queue = self._actions.pop(node_id, [])
        base = int(self._seq_base.get(node_id, 0))
        self._seq_base[node_id] = base + len(queue)
        return [dict(f, seq=base + i + 1) for i, f in enumerate(queue)]

    def sweep(self, now: float) -> list[tuple[str, str]]:
        return self.store.sweep_expired(now=now, lease_s=config.lease_s())

    def node_view(self, node: dict[str, Any], now: float) -> dict[str, Any]:
        view = {k: v for k, v in node.items() if k != "node_token"}
        view["token_mask"] = mask_tail(str(node.get("node_token", "")))
        last_seen = node.get("last_seen")
        lease_expiry = node.get("lease_expiry")
        view["since_seen_s"] = round(now - last_seen, 1) if last_seen is not None else None
        view["lease_left_s"] = round(lease_expiry - now, 1) if lease_expiry is not None else None
        view["capacity_text"] = capacity_text(node.get("capacity"))
        return view

    def list_node_views(self, now: float) -> list[dict[str, Any]]:
        return [self.node_view(n, now) for n in self.store.list_nodes()]

    # ---- 强制全量 sync（REST 打标，心跳消费）----
    def mark_force_sync(self, node_id: str) -> None:
        """打标"下次心跳无条件带全量快照"。set 而非 dict[bool]：幂等天然成立。

        刻意不落 SQLite：强制 sync 是"此刻这次运维动作"，中心重启即作废是可接受的
        ——重启后 worker 重连时 revision 若已一致，说明确实没有内容要补发。
        节点若一直离线，标记会留存到它下次上线，属期望行为（那正是最想修盘的时刻）。
        """
        self._force_sync.add(node_id)

    def _consume_force_sync(self, node_id: str) -> bool:
        """取走并清除标记：**一次性**。否则该节点每 10s 收一份全量 YAML 直到永远。"""
        if node_id not in self._force_sync:
            return False
        self._force_sync.discard(node_id)
        return True
