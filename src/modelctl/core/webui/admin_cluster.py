#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_cluster.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : 集群控制面 REST + WebSocket 端点（仅中心角色启用）
# ===============================================================================

"""core/webui/admin_cluster.py — /admin/api/cluster/* 与 /admin/api/ws/cluster。

非中心角色（solo/worker）全部端点 404；REST 过 require_auth（operator）；WS 在
hello 帧内用 join_token/node_token 鉴权（worker 不持有 API_KEY）。NodeRegistry
进程内单例，REST/WS 共享同一 SQLite 台账。设计文档 §5、§6.5、§10。
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field

from modelctl.core.cluster import backup, config, conns, tokens, wsproto
from modelctl.core.cluster import events as events_mod
from modelctl.core.cluster.goals import GoalService, goal_id_of
from modelctl.core.cluster.nodes import AuthError, NodeRegistry
from modelctl.core.cluster.store import ClusterStore, mask_tail
from modelctl.core.gpu_utils import GPUValidationError, parse_gpu_list
from modelctl.core.webui.admin_auth import require_auth

router = APIRouter()

_REGISTRY: NodeRegistry | None = None
#: WS 世代表（同 node_id 后来者胜）。测试直接赋新实例复位，不经 get_registry()。
_CONNS = conns.ConnectionRegistry()

_SWEEP_INTERVAL_S = 10.0
_last_sweep = 0.0

#: 远程启停允许的动词；`retry` 只在 goal 端点暴露（语义是"重置失败状态"，不是起停）
_MODEL_VERBS: frozenset[str] = frozenset({"start", "stop", "restart"})


def get_registry() -> NodeRegistry:
    """NodeRegistry 进程内单例（懒建库）。测试经 admin_cluster._REGISTRY=None 重置。"""
    global _REGISTRY
    if _REGISTRY is None:
        store = ClusterStore()
        store.init_db()
        _REGISTRY = NodeRegistry(store, goals=GoalService(store))
    return _REGISTRY


def _router() -> APIRouter:
    return router


def _disabled() -> JSONResponse | None:
    return None if config.is_center() else JSONResponse(status_code=404, content={"detail": "cluster disabled"})


def _sweep_if_due() -> None:
    """惰性 lease 扫描：任一 REST/WS 事件顺带触发，≥10s 才真正扫一次（免后台线程）。

    台账（SQLite）异常不得传导到长连接：扫描失败仅告警，下轮事件重试，
    绝不掀掉 worker WS 连接/心跳 ack。
    """
    global _last_sweep
    now = time.time()
    if now - _last_sweep < _SWEEP_INTERVAL_S:
        return
    _last_sweep = now
    try:
        for node_id, new_status in get_registry().sweep(now=now):
            logger.info(f"节点 {node_id} 状态迁移 → {new_status}")
    except Exception as exc:  # noqa: BLE001 —— 台账抖动与请求/长连接解耦，任何异常都吞掉
        logger.warning(f"集群 lease 扫描失败（忽略，下轮重试）: {exc}")


def _goals() -> GoalService:
    """goal 唯一写入口（与 WS 共用同一 NodeRegistry.store，台账才一致）。"""
    return get_registry().goals


def _fmt_ts(value: Any) -> str:
    """epoch float → 项目规范时间串；缺值/非法返回空串（不显示 None）。"""
    if not isinstance(value, (int, float)):
        return ""
    try:
        return _dt.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return ""


def _node_id_of_goal(goal_id: str) -> str:
    """goal_id = `<profile>@@<node_id>`：node_id 取末段（profile 理论上可含 @@）。"""
    return goal_id.rsplit("@@", 1)[-1] if "@@" in goal_id else ""


def _goal_view(goal: dict[str, Any], state: dict[str, Any] | None, *,
               with_yaml: bool = False, now: float | None = None) -> dict[str, Any]:
    """goal 视图（spec §7.5 表格行）：声明侧字段 + model_states 的运行事实。

    连接键是 `(node_id, profile)`：worker 上报的是 profile 名（与它磁盘上的文件
    名同源），goal_id 对 worker 是透明串，两侧只在中心台账里join。
    刻意不含 profile_yaml——列表载荷不背整份 YAML，导出走 `with_yaml=True`。
    """
    now = time.time() if now is None else now
    created = goal.get("created_at")
    updated = goal.get("updated_at")
    view: dict[str, Any] = {
        "goal_id": goal["goal_id"], "node_id": goal["node_id"], "profile": goal["profile"],
        "engine": goal["engine"], "intent": goal["intent"], "stage": goal["stage"],
        "reason": goal.get("stage_reason") or "", "error_class": goal.get("error_class") or "",
        "target_role": goal.get("target_role") or "", "profile_version": goal.get("profile_version") or "",
        "state": (state or {}).get("state") or "", "gpu": (state or {}).get("gpu"),
        "port": (state or {}).get("port"), "pid": (state or {}).get("pid"),
        "created_at": _fmt_ts(created), "updated_at": _fmt_ts(updated),
        "age_s": round(now - created, 1) if isinstance(created, (int, float)) else None,
    }
    if with_yaml:
        view["profile_yaml"] = goal.get("profile_yaml") or ""
    return view


def _goal_views(rows: list[dict[str, Any]], *, now: float) -> list[dict[str, Any]]:
    states = {(s["node_id"], s["profile"]): s for s in get_registry().store.list_model_states()}
    return [_goal_view(g, states.get((g["node_id"], g["profile"])), now=now) for g in rows]


@router.get("/cluster/status")
async def cluster_status(_base: None = Depends(require_auth)):
    if (off := _disabled()) is not None:
        return off
    _sweep_if_due()
    reg = get_registry()
    nodes = reg.store.list_nodes()
    return {"role": config.cluster_role(), "is_center": config.is_center(),
            "nodes_total": len(nodes), "nodes_online": sum(1 for n in nodes if n["status"] == "online")}


@router.get("/cluster/nodes")
async def cluster_nodes(_base: None = Depends(require_auth)):
    if (off := _disabled()) is not None:
        return off
    _sweep_if_due()
    return {"nodes": get_registry().list_node_views(now=time.time())}


@router.get("/cluster/events")
async def cluster_events(node_id: str = Query(""), kind: str = Query(""),
                         limit: int = Query(100, ge=1, le=1000),
                         _base: None = Depends(require_auth)):
    """事件流定版（spec §2.2）：{ts,node_id,goal_id,kind,text}，ts/text 后端单端格式化。"""
    if (off := _disabled()) is not None:
        return off
    if kind and not events_mod.is_known_kind(kind):
        return _bad_request(f"未知事件类型 {kind!r}")
    rows = get_registry().store.recent_events(limit=limit, node_id=node_id or None,
                                              kind=kind or None)
    return {"events": [{"ts": _fmt_ts(r["ts"]), "node_id": r["node_id"],
                        "goal_id": r["goal_id"], "kind": r["kind"],
                        "text": events_mod.event_text(r)} for r in rows]}


# ================================ 目标状态（M1，spec §6.5）================================
class _GoalCreateBody(BaseModel):
    profile: str = Field(min_length=1, max_length=64)
    node_ids: list[str] | None = None
    all_nodes: bool = False
    intent: str = "start"
    create: bool = False
    params: dict | None = None
    env_overlay: dict | None = None
    gpus: str = ""
    #: 同名 YAML 散落多引擎时的选边出口（review P-1）：空串 = 不选边（多引擎同名即歧义拒发）
    engine: str = ""
    lan_allow: list[str] | None = None
    runtime_ref: str | None = None
    target_role: str = "primary"
    dry_run: bool = False


class _GoalUpdateBody(BaseModel):
    """PUT 载荷：全部可选 + `exclude_unset`，未提供的字段一律不动。

    刻意**不提供** `profile` / `node_ids`：改目标节点等价于换 goal_id，必须
    "先 create 新 goal 再 remove 旧 goal"，否则历史 stage/事件与 model_states
    会与新节点的运行事实串台。
    """

    intent: str | None = None
    params: dict | None = None
    env_overlay: dict | None = None
    placement: dict | None = None
    runtime_ref: str | None = None
    target_role: str | None = None
    profile_version: str | None = Field(default=None, max_length=64)


def _bad_request(reason: str) -> JSONResponse:
    """core 层业务失败统一 400（与既有端点的 401/404 一样只回固定短文案）。"""
    return JSONResponse(status_code=400, content={"detail": reason})


@router.get("/cluster/goals")
async def list_goals(node_id: str = Query("", max_length=64),
                     profile: str = Query("", max_length=64),
                     _base: None = Depends(require_auth)):
    if (off := _disabled()) is not None:
        return off
    _sweep_if_due()
    rows = get_registry().store.list_goals(node_id=node_id, profile=profile)
    return {"goals": _goal_views(rows, now=time.time())}


@router.post("/cluster/goals")
async def create_goals(body: _GoalCreateBody, _base: None = Depends(require_auth)):
    """批量下发 goal（过 placement gate）。gate 的 skip 是 200 + report，不是 HTTP 错误。

    只有"参数本身非法/无任何候选节点"才 400：逐节点容量不足是运维要逐行读的
    **结果**，不是请求失败——把 20 个节点里 3 个装不下变成 400，CLI 就拿不到报告了。
    """
    if (off := _disabled()) is not None:
        return off
    try:
        gpu_list = parse_gpu_list(body.gpus or "")   # 非法/重复即抛 GPUValidationError
    except GPUValidationError as exc:
        return _bad_request(f"非法 gpus {body.gpus!r}：{exc}")
    result = _goals().set_goals(profile=body.profile, node_ids=body.node_ids,
                                all_nodes=body.all_nodes, intent=body.intent,
                                create=body.create, params=body.params,
                                env_overlay=body.env_overlay, gpu_list=gpu_list,
                                lan_allow=body.lan_allow, runtime_ref=body.runtime_ref,
                                target_role=body.target_role, created_by="api",
                                dry_run=body.dry_run, engine=body.engine or None,
                                now=time.time())
    if result["reason"]:
        return _bad_request(result["reason"])
    now = time.time()
    if body.dry_run:
        rows: list[dict[str, Any]] = []
    elif body.all_nodes or not body.node_ids:
        # 响应只回本次请求范围（fix round 1 Minor-1）：all_nodes/未点名 node_ids 时
        # 本次范围就是该 profile 的全量。
        rows = get_registry().store.list_goals(profile=body.profile)
    else:
        # 点名 node_ids：只回命中的行，否则未请求节点的历史 goal 会混进响应，
        # 看起来像"这次也下发了它们"。
        wanted = set(body.node_ids)
        rows = [r for r in get_registry().store.list_goals(profile=body.profile)
                if str(r["node_id"]) in wanted]
    # reason 恒为空串才走到这行（非空已在上面转 400）；仍显式回带：Interfaces 契约
    # 把它列为成功响应体键，CLI（Task 12）据此区分"受理成功"与"整体失败"两套渲染。
    return {"created": result["created"], "skipped": result["skipped"],
            "errors": result["errors"], "report": result["report"],
            "reason": result["reason"], "goals": _goal_views(rows, now=now)}


@router.put("/cluster/goals/{goal_id}")
async def update_goal(goal_id: str, body: _GoalUpdateBody, _base: None = Depends(require_auth)):
    """更新 goal 的声明式字段；worker 侧由 revision 变化自动重新写盘并重置状态机。"""
    if (off := _disabled()) is not None:
        return off
    goal, reason = _goals().update_goal(goal_id, fields=body.model_dump(exclude_unset=True),
                                        created_by="api", now=time.time())
    if reason:
        return _bad_request(reason)
    if goal is None:
        return JSONResponse(status_code=404, content={"detail": f"目标 {goal_id} 不存在"})
    return {"goal": _goal_view(goal, None, now=time.time())}


@router.delete("/cluster/goals/{goal_id}")
async def delete_goal(goal_id: str, _base: None = Depends(require_auth)):
    """删除 goal。worker 侧删除无需额外指令：goal 从下一份快照消失即剪枝语义（§6.4）。"""
    if (off := _disabled()) is not None:
        return off
    if get_registry().store.get_goal(goal_id) is None:
        return JSONResponse(status_code=404, content={"detail": f"目标 {goal_id} 不存在"})
    out = _goals().remove_goals(profile=goal_id.rsplit("@@", 1)[0],
                                node_ids=[_node_id_of_goal(goal_id)], created_by="api")
    return {"removed": out["removed"], "missing": out["missing"]}


@router.post("/cluster/goals/{goal_id}/retry")
async def retry_goal(goal_id: str, _base: None = Depends(require_auth)):
    """人工重试失败的 goal（spec §8.1：失败是终态，只有 retry 或 goal 变更能走出）。

    只做两件事：入队 retry 指令 + 记事件。**刻意不改 stage**——节点可能已离线，
    中心乐观写成 PENDING_PROFILE_SYNC 会让 dashboard 在故障机器上显示假的重启中。
    真正的状态回落由 worker 下一拍 reconcile 上报。
    """
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    goal = reg.store.get_goal(goal_id)
    if goal is None:
        return JSONResponse(status_code=404, content={"detail": f"目标 {goal_id} 不存在"})
    queued = reg.push_action(str(goal["node_id"]), "retry", goal_id=goal_id)
    reg.store.append_event("goal.retry", node_id=str(goal["node_id"]), goal_id=goal_id,
                           payload={"queued": queued, "operator": "api"}, now=time.time())
    return {"queued": queued}


@router.get("/cluster/nodes/{node_id}")
async def cluster_node_detail(node_id: str, _base: None = Depends(require_auth)):
    """单节点详情：台账视图 + 该节点 goals + model_states（dashboard 详情页数据源）。"""
    if (off := _disabled()) is not None:
        return off
    _sweep_if_due()
    reg = get_registry()
    node = reg.store.get_node(node_id)
    if node is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    now = time.time()
    return {"node": reg.node_view(node, now),
            "goals": _goal_views(reg.store.list_goals(node_id=node_id), now=now),
            "model_states": reg.store.list_model_states(node_id=node_id)}


# ================================ 节点治理（M2，spec §2.1）================================
@router.post("/cluster/nodes/{node_id}/disable")
async def disable_node(node_id: str, _base: None = Depends(require_auth)):
    """禁用节点：hello/join-check 此后拒绝；goal 台账不动（禁用≠撤销声明）。"""
    if (off := _disabled()) is not None:
        return off
    out = get_registry().disable_node(node_id, conns_registry=_CONNS)
    if out is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    return out


@router.post("/cluster/nodes/{node_id}/enable")
async def enable_node(node_id: str, _base: None = Depends(require_auth)):
    """解除禁用：只清 disabled 位，状态由其下次 hello/心跳自行恢复。"""
    if (off := _disabled()) is not None:
        return off
    out = get_registry().enable_node(node_id)
    if out is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    return out


@router.post("/cluster/nodes/{node_id}/rotate-token")
async def rotate_node_token(node_id: str, _base: None = Depends(require_auth)):
    """轮换节点 token：**响应一次性返回明文**（同 join-check 先例），连带 kick。

    旧 token 即刻失效（rotate 后 find_node_by_token 不再命中旧值）+ revoke 断连，
    worker 用 .env 里的旧 token 重连会被拒——必须人工把新 token 写进该节点 .env。
    """
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    if reg.store.get_node(node_id) is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    fresh = reg.store.rotate_node_token(node_id)
    if fresh is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    kicked = _CONNS.revoke(node_id) is not None
    reg.store.append_event("token.rotate", node_id=node_id,
                           payload={"scope": "node", "kicked": kicked, "operator": "api"},
                           now=time.time())
    return {"node_token": fresh, "kicked": kicked,
            "hint": "请在该节点 .env 更新 CLUSTER_NODE_TOKEN 后重启 webui"}


@router.post("/cluster/nodes/{node_id}/kick")
async def kick_node(node_id: str, _base: None = Depends(require_auth)):
    """主动断连（世代表摘除 → 下一帧自退）。一次性动作：worker 退避重连后即恢复。"""
    if (off := _disabled()) is not None:
        return off
    out = get_registry().kick_node(node_id, _CONNS)
    if out is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    return out


@router.delete("/cluster/nodes/{node_id}")
async def retire_node(node_id: str, _base: None = Depends(require_auth)):
    """节点退役：先 kick，再级联删 goals/model_states（连带撤销声明，防幽灵 goal）。

    有 goals 被连带删除时响应带 removed_goals 计数（CLI 二次确认文案消费）；
    events 不删——审计留痕。
    """
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    if reg.store.get_node(node_id) is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    _CONNS.revoke(node_id)
    out = reg.store.retire_node(node_id, append_event_fn=reg.store.append_event)
    if out is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    return {"removed": True, **out}


@router.post("/cluster/nodes/{node_id}/sync")
async def force_node_sync(node_id: str, _base: None = Depends(require_auth)):
    """强制全量 sync：下一枚 ack 无条件带 `sync.force=true`，worker 跳过 revision 短路重写盘。

    离线节点也接受（标记留存到其下次上线）——本地文件被改坏时，人最想立刻修好的
    正是还没连回来的那台。一次性标记见 `NodeRegistry._consume_force_sync`。
    """
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    if reg.store.get_node(node_id) is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    reg.mark_force_sync(node_id)
    reg.store.append_event("node.sync", node_id=node_id, payload={"operator": "api"},
                           now=time.time())
    return {"queued": True}


@router.post("/cluster/nodes/{node_id}/model/{profile}/{verb}")
async def model_verb(node_id: str, profile: str, verb: str,
                     _base: None = Depends(require_auth)):
    """远程启停单个模型。**必须先有 goal**。

    未托管的 profile 一律 404：中心一旦能对 worker 本机自跑的模型下指令，声明式
    边界就破了（运维会看到"我没下发过的模型被中心停了"）。worker 侧
    `Reconciler.handle_actions` 用同一立场兜底（goal 不在本地清单 → ok=False）。
    """
    if (off := _disabled()) is not None:
        return off
    if verb not in _MODEL_VERBS:
        return _bad_request(f"不支持的操作 {verb!r}（仅 {sorted(_MODEL_VERBS)}）")
    reg = get_registry()
    goal = reg.store.get_goal(goal_id_of(profile, node_id))
    if goal is None:
        return JSONResponse(status_code=404,
                            content={"detail": f"节点 {node_id} 上没有 profile {profile} 的托管目标"})
    queued = reg.push_action(node_id, verb, goal_id=str(goal["goal_id"]), profile=profile)
    reg.store.append_event("node.model_action", node_id=node_id, goal_id=str(goal["goal_id"]),
                           payload={"verb": verb, "queued": queued, "operator": "api"},
                           now=time.time())
    return {"queued": queued}


# ================================ 只读目录/设置（M2，spec §2.3/§4.3）================================
@router.get("/cluster/profiles")
async def cluster_profiles(_base: None = Depends(require_auth)):
    """profile 目录（goal 弹窗数据源）：只呈现不裁决，选边留在 POST gate。"""
    if (off := _disabled()) is not None:
        return off
    from modelctl.core.cluster import goals as goals_module
    from modelctl.core.cluster import profiles

    return {"profiles": profiles.list_profile_catalog(goals_module.MODELS_DIR)}


@router.get("/cluster/settings")
async def cluster_settings(_base: None = Depends(require_auth)):
    """集群配置只读展示（spec §4.3 定版新端点）：零写端点；join token 仅脱敏出参。"""
    if (off := _disabled()) is not None:
        return off
    join = get_registry().store.get_meta("join_token")
    return {"role": config.cluster_role(), "center_url": config.center_url(),
            "heartbeat_interval_s": config.heartbeat_interval_s(), "lease_s": config.lease_s(),
            "reconcile_interval_s": config.reconcile_interval_s(),
            "max_snapshot_bytes": config.max_snapshot_bytes(),
            "join_token_mask": mask_tail(join)}


@router.get("/cluster/export")
async def cluster_export(_base: None = Depends(require_auth)):
    """全量 goal + 节点状态导出（备份/迁移素材；`cluster backup` 属 M2，此处只给数据）。"""
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    now = time.time()
    return {"exported_at": _fmt_ts(now), "role": config.cluster_role(),
            "nodes": [reg.node_view(n, now) for n in reg.store.list_nodes()],
            "goals": [_goal_view(g, None, with_yaml=True, now=now)
                      for g in reg.store.list_goals()],
            "model_states": reg.store.list_model_states()}


@router.get("/cluster/backup")
async def cluster_backup_download(background: BackgroundTasks,
                                  _base: None = Depends(require_auth)):
    """台账热备下载（spec §2.1）：FileResponse 附件 + X-Backup-Sha256 + db.backup 事件。

    备份含 node_token/join_token 明文——require_auth 管理员域（总 spec §11 信任模型），
    事件只记动作与字节数，永不记内容。
    """
    if (off := _disabled()) is not None:
        return off
    import tempfile

    from fastapi.responses import FileResponse

    tmp_dir = Path(tempfile.mkdtemp(prefix="modelctl-backup-"))
    fname = f"modelctl-cluster-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    dest = tmp_dir / fname
    try:
        out = backup.create_backup(dest)
    except backup.BackupError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return _bad_request(str(exc))
    get_registry().store.append_event("db.backup",
                                      payload={"bytes": out["bytes"], "operator": "api"},
                                      now=time.time())
    background.add_task(shutil.rmtree, tmp_dir, True)
    return FileResponse(path=str(dest), filename=fname,
                        headers={"X-Backup-Sha256": out["sha256"]})


@router.post("/cluster/join-tokens/rotate")
async def rotate_join_token(_base: None = Depends(require_auth)):
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    fresh = tokens.new_join_token()
    reg.store.set_meta("join_token", fresh)
    reg.store.append_event("token.rotate", payload={"scope": "join"})
    return {"join_token": fresh}


class _JoinCheckBody(BaseModel):
    node_id: str = Field(min_length=1, max_length=64)
    key: str
    lan: str = Field(default="", max_length=64)
    host_ip: str = ""
    hostname: str = ""


@router.post("/cluster/join-check")
async def join_check(body: _JoinCheckBody):
    """CLI join 预检：凭据=请求体 join token（同 WS hello 校验路径，参照 /login 先例）。

    成功即预注册节点（status=offline，WS hello 后转 online）并同步返回 node_token，
    CLI 直接落 .env，Agent 首连即用节点身份。
    """
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    if tokens.token_matches(body.key, reg.ensure_join_token()):
        # 禁用闸门（M2）：与 WS hello 同构——先认身份再谈解禁，判据取被 join 的
        # node_id 行（换 join token 绕不开节点级意志）。
        existing = reg.store.get_node(body.node_id)
        if existing is not None and existing.get("disabled"):
            return JSONResponse(status_code=401, content={"detail": "节点已禁用"})
        node_token = tokens.new_node_token()
        result = reg.store.upsert_node(node_id=body.node_id, node_token=node_token, lan_id=body.lan,
                                       role="worker", host_ip=body.host_ip, hostname=body.hostname,
                                       engines=None, now=time.time())
        # 预注册恒 offline（joined/rejoined 皆然）：upsert 会置 online，须立即回落，
        # 真实 online 只属于 WS hello；否则 offline 节点可经 join-check 伪装在线且 lease=NULL。
        reg.store.set_node_status(body.node_id, "offline")
        reg.store.append_event("node.join_check", node_id=body.node_id, payload={"result": result})
        return {"ok": True, "node_token": node_token}
    known = reg.store.find_node_by_token(body.key)
    if known is None:
        return JSONResponse(status_code=401, content={"detail": "无效的 join/node token"})
    if str(known["node_id"]) != body.node_id:
        return JSONResponse(status_code=401, content={"detail": "node_id 与节点令牌不匹配"})
    # 第二处闸门：校验链已过 node_id 比对，known 即被 join 的 node_id 行；同文案
    # 不泄露"token 有效但节点被禁"与"其他 401"的差异。
    if known.get("disabled"):
        return JSONResponse(status_code=401, content={"detail": "节点已禁用"})
    return {"ok": True, "node_token": str(known["node_token"])}


@router.websocket("/ws/cluster")
async def ws_cluster(ws: WebSocket):
    """worker 通道：hello（token 鉴权）→ welcome → heartbeat/event/result 循环。

    身份绑定：node_id 只存本连接的局部变量，handle_hello 已强制 NT↔node_id 一致，
    故后续帧只能落到已鉴权的那个节点，无法伪造他人身份。
    对端可控输入（raw 帧、mtype）一律不回显原文：错误帧只用固定文案。

    世代表：hello 成功后向 _CONNS 登记 epoch，此后每帧先验 epoch。同 node_id 的旧连接
    （进程重启后遗留的半开连接）若继续活着，中心的 action 会被投给僵尸或被双份执行，
    故后来者胜、旧连接下一帧即退场。旧连接被动感知（不主动踢）是接受的取舍：它下一次
    发送才被踢，而它不发时中心只会用新连接投递，不影响正确性；主动 kick 需给
    ConnectionRegistry 加反向通知，M2 与 `audit.query` 一并做。
    """
    if not config.is_center():
        await ws.close(code=4404)
        return
    await ws.accept()
    reg = get_registry()
    node_id = ""
    epoch = 0
    try:
        hello_raw = await ws.receive_text()
        # parse_type 仅在"可解析且为 dict"时返回非空，故其返回 hello 时下面 loads 必成功
        if wsproto.parse_type(hello_raw) != "hello":
            await ws.send_text(wsproto.dumps(wsproto.make_error("首帧须为 hello")))
            await ws.close(code=4400)
            return
        welcome, node_id = reg.handle_hello(wsproto.parse_hello(json.loads(hello_raw)))
        epoch = _CONNS.join(node_id)
        await ws.send_text(wsproto.dumps(welcome))
        while True:
            raw = await ws.receive_text()
            if not _CONNS.is_current(node_id, epoch):
                await ws.send_text(wsproto.dumps(wsproto.make_error("连接已被同节点新连接取代")))
                await ws.close(code=4409)
                return
            mtype = wsproto.parse_type(raw)
            try:
                data: dict[str, Any] = json.loads(raw)
            except (ValueError, TypeError, RecursionError):
                # RecursionError：深嵌套 JSON 击穿递归上限，与非法 JSON 同等处置
                await ws.send_text(wsproto.dumps(wsproto.make_error("消息解析失败")))
                continue
            if mtype == "heartbeat":
                ack = reg.handle_heartbeat(node_id, wsproto.parse_heartbeat_v2(data), now=time.time())
                _sweep_if_due()
                await ws.send_text(wsproto.dumps(ack))
            elif mtype == "event":
                payload = data.get("payload")
                reg.store.append_event(str(data.get("kind", "")), node_id=node_id,
                                       payload=payload if isinstance(payload, dict) else None)
                await ws.send_text(wsproto.dumps({"t": "ack"}))
            elif mtype == "result":
                # 指令回执只落账不裁决：ok=False 时改不改状态由 worker 的 reconcile 决定，
                # 中心重复动作会与"失败即终态 + 人工 retry"的立场冲突。
                res = wsproto.parse_result(data)
                reg.store.append_event("action.result", node_id=node_id,
                                       payload={"seq": res["seq"], "ok": res["ok"],
                                                "detail": res["detail"]}, now=time.time())
                await ws.send_text(wsproto.dumps({"t": "ack"}))
            else:
                await ws.send_text(wsproto.dumps(wsproto.make_error("未知消息类型")))
    except WebSocketDisconnect:
        return
    except AuthError:
        await ws.send_text(wsproto.dumps(wsproto.make_error("鉴权失败")))
        await ws.close(code=4401)
        return
    finally:
        if node_id:
            _CONNS.release(node_id, epoch)   # 只摘自己的 epoch，绝不误杀新连接
