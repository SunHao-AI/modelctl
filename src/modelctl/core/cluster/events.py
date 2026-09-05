#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/events.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : 事件 kind 词表守卫 + 展示文本单端拼装（M2 spec §1/§2.2）
# ===============================================================================

"""core/cluster/events.py — EVENT_KINDS 守卫与 event_text 拼装。

守卫语义（计划裁决 1，勿改成 fail-fast）：WS event 帧的 kind 是 worker 自由字符串，
对端输入路径只告警不抛；词表的约束力在"中心自有埋点不得野 kind"（测试钉 + 告警
可见性）与"GET /cluster/events 的 kind 过滤白名单"两处兑现。
"""

from __future__ import annotations

from typing import Any

EVENT_KINDS: frozenset[str] = frozenset({
    # 既有（M0/M1）
    "node.join", "node.join_check", "node.sync", "node.model_action",
    "goal.create", "goal.update", "goal.delete", "goal.retry", "goal.drift",
    "action.result", "token.rotate", "node.heartbeat",
    # M2 新增
    "node.disable", "node.enable", "node.kick", "node.retire",
    "db.backup", "db.restore", "goal.sync_overflow",
})


def is_known_kind(kind: str) -> bool:
    return kind in EVENT_KINDS


def _p(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else {}


def event_text(row: dict[str, Any]) -> str:
    """kind+payload → 一句中文展示文本（spec §2.2：中心单端拼装，前端/CLI 零加工）。

    兜底分支必须覆盖"payload 非 dict / kind 词表外"两类对端输入，且永不抛。
    """
    kind = str(row.get("kind") or "")
    p = _p(row)
    if kind == "goal.create":
        return f"创建目标（{p.get('profile', '?')} intent→{p.get('intent', '?')}；操作者 {p.get('operator', '-')}）"
    if kind == "goal.update":
        # payload 经 WS event 帧原样入库（值任意）：fields 可能是非序列/含非字符串
        # 元素，逐项类型过滤保证"永不抛"，否则 poison 行会让读端点持久 500。
        raw = p.get("fields") or []
        items = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        fields = ",".join(str(x) for x in items if isinstance(x, (str, int)))
        return f"更新目标（{p.get('profile', '?')} 字段 {fields or '-'}；操作者 {p.get('operator', '-')}）"
    if kind == "goal.delete":
        return f"撤销目标（{p.get('profile', '?')}；操作者 {p.get('operator', '-')}）"
    if kind == "goal.retry":
        return f"人工重试（排队={'是' if p.get('queued') else '否'}；操作者 {p.get('operator', '-')}）"
    if kind == "goal.drift":
        return "漂移：worker 本地 profile 与中心声明不一致"
    if kind == "action.result":
        ok = p.get("ok")
        verdict = "成功" if ok is True else ("失败" if ok is False else "未知")
        return f"指令回执（seq={p.get('seq', 0)} {verdict}）{str(p.get('detail', ''))[:120]}"
    if kind == "node.join":
        return "节点接入（WS hello）"
    if kind == "node.join_check":
        return f"join 预检（{p.get('result', '-')}）"
    if kind == "node.sync":
        return f"强制全量同步（操作者 {p.get('operator', '-')}）"
    if kind == "node.model_action":
        return f"远程指令 {p.get('verb', '-')}（排队={'是' if p.get('queued') else '否'}）"
    if kind == "token.rotate":
        scope = "join token" if p.get("scope") == "join" else "节点 token"
        return f"轮换 {scope}"
    if kind == "node.heartbeat":
        return "心跳"
    if kind == "node.disable":
        return "禁用节点" + ("（顺带断连）" if p.get("kicked") else "")
    if kind == "node.enable":
        return "解除禁用"
    if kind == "node.kick":
        return "主动踢除连接" + ("" if p.get("kicked") else "（当时无连接）")
    if kind == "node.retire":
        return f"节点退役（连带撤销 {p.get('removed_goals', 0)} 个目标）"
    if kind == "db.backup":
        return f"台账备份（{p.get('bytes', '-')} 字节）"
    if kind == "db.restore":
        return f"台账自备份恢复而来（源 {p.get('source', '-')}）"
    if kind == "goal.sync_overflow":
        return f"快照超限未下发（{p.get('bytes', '?')} > 上限 {p.get('limit', '?')}）"
    digest = " ".join(f"{k}={v}" for k, v in list(p.items())[:4])
    return f"{kind} {digest}".strip()
