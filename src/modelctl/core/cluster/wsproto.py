#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/wsproto.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : 集群 WebSocket 消息协议（JSON 编解码，无网络依赖）
# ===============================================================================

"""core/cluster/wsproto.py — 一行一条 JSON 的 WS 消息编解码（设计文档 §5）。

M0 只用 hello/welcome/heartbeat/event/error；goal.sync/status.query 等 M1+ 再加。
零第三方依赖，可脱离 WS 单测。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTO_VERSION = 2


@dataclass
class HelloMsg:
    node_id: str = ""
    lan: str = ""
    key: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def make_hello(node_id: str, lan: str, key: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {"t": "hello", "v": PROTO_VERSION, "node_id": node_id, "lan": lan, "key": key, "meta": meta}


def make_welcome(node_token: str, interval_s: int, lease_s: int) -> dict[str, Any]:
    return {"t": "welcome", "node_token": node_token, "interval_s": interval_s, "lease_s": lease_s}


def make_heartbeat(payload: dict[str, Any]) -> dict[str, Any]:
    return {"t": "heartbeat", "payload": payload}


def make_event(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"t": "event", "kind": kind, "payload": payload}


def make_error(message: str) -> dict[str, Any]:
    return {"t": "error", "message": message}


def dumps(msg: dict[str, Any]) -> str:
    return json.dumps(msg, ensure_ascii=False)


def parse_type(raw: str) -> str:
    """解析消息类型；非法 JSON / 非 dict / 缺 t 一律返回空串（调用侧回 error 帧）。

    RecursionError：对端可构造数千层嵌套的 JSON 帧击穿 CPython 递归上限，该异常
    不属 ValueError 家族，若不外捕会直接掀掉中心侧的 WS 处理循环。
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return ""
    return str(data.get("t", "")) if isinstance(data, dict) else ""


def parse_hello(data: dict[str, Any]) -> HelloMsg:
    meta = data.get("meta")
    return HelloMsg(
        node_id=str(data.get("node_id", "")),
        lan=str(data.get("lan", "")),
        key=str(data.get("key", "")),
        meta=meta if isinstance(meta, dict) else {},
    )


def parse_heartbeat(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else {}


VALID_ACTIONS: tuple[str, ...] = ("start", "stop", "restart", "retry")


def make_sync(revision: str, goals: list[dict[str, Any]]) -> dict[str, Any]:
    """goal 全量快照下发。幂等语义：worker 以 revision 判重、同 sha 跳过写盘。

    不设 pruned 字段：goal 从快照消失即删除语义，worker 用本地 managed 清单对照
    快照自行推导（见 Task 8），中心无需保留"已删除"墓碑。
    """
    return {"t": "sync", "revision": revision, "goals": goals}


def make_action(seq: int, action: str, goal_id: str = "", profile: str = "") -> dict[str, Any]:
    return {"t": "action", "seq": int(seq), "action": action, "goal_id": goal_id, "profile": profile}


def make_result(seq: int, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"t": "result", "seq": int(seq), "ok": bool(ok), "detail": str(detail)[:500]}


def _safe_seq(data: dict[str, Any]) -> int:
    """seq 只接受非 bool 的 int（bool 是 int 子类，会静默变成 0/1 造成回执错配）。"""
    seq = data.get("seq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else 0


def parse_action(data: Any) -> dict[str, Any]:
    """action 帧消毒：文本字段非 str → 空串（对端可控输入不得污染下游类型）。"""
    if not isinstance(data, dict):
        return {"seq": 0, "action": "", "goal_id": "", "profile": ""}
    out: dict[str, Any] = {"seq": _safe_seq(data)}
    for k in ("action", "goal_id", "profile"):
        v = data.get(k)
        out[k] = str(v)[:200] if isinstance(v, str) else ""
    return out


def parse_ack(data: Any) -> dict[str, Any]:
    """ack 消毒：只认 dict 型 sync 与 list 型 actions，其余回落安全默认。

    ack 由中心写、worker 读；旧版中心的 ack 不含控制字段，缺字段是常态而非错误，
    因此一律回落而非抛错（否则 worker 在中心升级前会整条链路失败）。
    """
    out: dict[str, Any] = {"seq": 0, "sync": None, "actions": []}
    if not isinstance(data, dict):
        return out
    out["seq"] = _safe_seq(data)
    sync = data.get("sync")
    if isinstance(sync, dict):
        out["sync"] = sync
    actions = data.get("actions")
    if isinstance(actions, list):
        # 非 dict 条目直接丢弃：若走 parse_action 会得到 seq=0 的默认帧，
        # 等于凭空捏造一条"回执"，下游会拿它去匹配不存在的 action。
        out["actions"] = [parse_action(a) for a in actions if isinstance(a, dict)]
    return out


def _opt_dict(value: Any) -> dict | None:
    return value if isinstance(value, dict) else None


def _opt_str_list(value: Any, *, limit: int) -> list[str] | None:
    """字段缺失 → None（未知）；显式空列表 → []（worker 明确说"没有"）。

    中心据此决定是否覆盖台账：None 保留旧值，[] 清空。二者混淆会让旧版 worker
    （不上报该字段）把新版写入的事实抹掉。列表内非 str 条目丢弃，长度封顶防放大。
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    return [str(v) for v in value if isinstance(v, str)][:limit]


def parse_heartbeat_v2(data: Any) -> dict[str, Any]:
    """心跳扩展段消毒（v2 新增段全部按"缺失=None=未知"处理）。

    profiles 同样遵循"缺失即 None"：中心拿它全量覆盖 model_states，若把"旧版
    worker 没上报"当成"上报了空集"，会把该节点所有在跑模型记录抹掉（dashboard
    集体假 down）。中心侧对 profiles 取 .items() 前须先判 None。
    `local_profiles` 是 worker 本机 models/ 下的 profile 名清单，中心用它决定
    gate 的 --create 判定（该节点是否已有同名 profile 文件）。
    """
    if not isinstance(data, dict):
        return {"profiles": None, "goal_sync": None, "drift": None,
                "local_profiles": None, "capacity": None, "runtimes": None}
    payload = data.get("payload")
    src: dict[str, Any] = payload if isinstance(payload, dict) else data
    profiles_raw = src.get("profiles")
    profiles = ({str(k): v for k, v in profiles_raw.items() if isinstance(v, dict)}
                if isinstance(profiles_raw, dict) else None)
    return {"profiles": profiles,
            "goal_sync": _opt_dict(src.get("goal_sync")),
            "drift": _opt_str_list(src.get("drift"), limit=512),
            "local_profiles": _opt_str_list(src.get("local_profiles"), limit=1024),
            "capacity": _opt_dict(src.get("capacity")),
            "runtimes": _opt_dict(src.get("runtimes"))}
