#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/account_self.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/9 16:00
# @Desc   : WebUI 账号自助面板 — 登录 / Key 管理 / 用量 / 会话
# ===============================================================================

"""core/webui/account_self.py — 账号自助面板（`/api/account/*`）。

与 `admin_accounts` 的差异：
- **鉴权 Bearer JWT**（`require_account`），非管 API_KEY；
- **归属自治**：全部端点只命中**当前 JWT 携带 user_id** 的资源；跨用户撞
  `session_id` / `key_id` 一律 404（store DAO 层 `WHERE id=? AND user_id=?`
  保证，本模块不重复判断）；
- **不暴露** 密码 hash / 别家账号 / 全局用量聚合（那是 admin 面板的事）；
- **不暴露** `key` 明文，除了 POST /keys 201 响应里**一次性**（同 admin 语义）。

工厂签名：`create_account_self_router() -> APIRouter`；**不走** `_SUBROUTER_MODULES`
（那个模式是给 admin 路由聚合用的——`/admin/api` 前缀统一挂），本模块要在
`gateway.create_app` 里直接 `app.include_router(..., prefix="/api/account")`。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request

from modelctl.core.webui.account_auth import (
    AccountPrincipal,
    issue_token,
    require_account,
)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _get_store(request: Request):
    store = request.app.state.accounts
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "accounts_disabled",
                    "message": "账号体系未启用（.env 设 ACCOUNTS_ENABLED=true）"})
    return store


def _key_public(row: dict) -> dict:
    """api_keys 行 → JSON。**永不含 key_hash / 明文**。"""
    return {c: row[c] for c in (
        "id", "user_id", "key_prefix", "name", "status",
        "expires_at", "last_used_at", "created_at") if c in row}


def _session_public(row: dict) -> dict:
    return {c: row[c] for c in (
        "id", "user_id", "key_id", "model", "session_key", "title",
        "message_count", "created_at", "last_active_at") if c in row}


def _require_store(request: Request):
    return _get_store(request)


# ---------------------------------------------------------------------------
# 登录（唯一免鉴权端点）
# ---------------------------------------------------------------------------

def create_account_self_router() -> APIRouter:
    """构建账号自助路由（`/api/account/*` 前缀由调用方 include 时设置）。"""
    router = APIRouter()

    @router.post("/login")
    async def login(request: Request, payload: dict):
        """POST /api/account/login — 用户名 + 密码换 JWT。

        一致性弱命题：未知/错密码/禁用账号都 401 且 shape 完全相同
        （`{"code":"auth","message":"用户名或密码错误"}`）——避免枚举
        用户名。503 accounts_disabled 分支只在 accounts **整体**未启用
        时触发，与"账号不存在"语义不同。
        """
        store = _get_store(request)
        username = (payload.get("username") or "").strip()
        password = payload.get("password") or ""
        if not username or not isinstance(password, str):
            raise HTTPException(status_code=401,
                                detail={"code": "auth",
                                        "message": "用户名或密码错误"})
        from modelctl.core.accounts.hashing import verify_password

        user = store.get_user_by_username(username)
        # 三个失败合并为一个 401（存在性不泄漏）：
        if user is None:
            raise HTTPException(status_code=401,
                                detail={"code": "auth",
                                        "message": "用户名或密码错误"})
        if user.get("status") != "active":
            raise HTTPException(status_code=401,
                                detail={"code": "auth",
                                        "message": "用户名或密码错误"})
        if not verify_password(password, user.get("password_hash") or ""):
            raise HTTPException(status_code=401,
                                detail={"code": "auth",
                                        "message": "用户名或密码错误"})
        now = int(time.time())
        token = issue_token(user_id=int(user["id"]),
                            is_admin=bool(user.get("is_admin")),
                            now=now)
        # 401 → Hash 校验完成：签发 token，同时回一份 profile 摘要
        # 让前端顶部用户名牌立刻可用（不必再 GET /api/account/profile）。
        return {
            "token": token,
            "user": {
                "id": user["id"],
                "username": user.get("username", ""),
                "display_name": user.get("display_name") or "",
                "is_admin": bool(user.get("is_admin", False)),
            },
        }

    # ------------------------------------------------------------------
    # Key 管理
    # ------------------------------------------------------------------

    @router.get("/keys")
    async def list_keys(request: Request,
                        principal: AccountPrincipal = Depends(require_account)):
        """GET /api/account/keys — 本账号全部 Key（含 prefix 脱敏）。"""
        store = _get_store(request)
        rows = store.list_keys_for_user(principal.user_id)
        return {"keys": [_key_public(r) for r in rows]}

    @router.post("/keys", status_code=201)
    async def create_key(request: Request, payload: dict,
                         principal: AccountPrincipal = Depends(require_account)):
        """POST /api/account/keys — 自助签发，**一次性**返回明文。"""
        store = _get_store(request)
        from modelctl.core.accounts.hashing import (generate_api_key, hash_api_key,
                                                     key_prefix)

        name = str(payload.get("name") or "")
        expires_days = payload.get("expires_in_days")
        now = time.time()
        expires_at = None
        if expires_days is not None:
            try:
                days = float(expires_days)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400,
                                    detail={"code": "bad_request",
                                            "message": "expires_in_days 必须为数字"})
            if days > 0:
                expires_at = now + days * 86400.0
        cred = generate_api_key()
        kid = store.create_key(user_id=principal.user_id,
                               key_hash=hash_api_key(cred),
                               key_prefix=key_prefix(cred),
                               name=name, expires_at=expires_at, now=now)
        row = store.get_key_by_id(kid)
        assert row is not None
        out = _key_public(row)
        out["key"] = cred  # **一次性**明文
        return out

    @router.put("/keys/{key_id}")
    async def update_key_status(key_id: int, request: Request, payload: dict,
                                principal: AccountPrincipal = Depends(require_account)):
        """PUT /api/account/keys/{kid} — 启/禁/吊销自家 Key。跨用户 404。"""
        store = _get_store(request)
        from modelctl.core.accounts.store import KEY_STATUSES

        key = store.get_key_by_id(key_id)
        if key is None or int(key.get("user_id") or 0) != principal.user_id:
            raise HTTPException(status_code=404,
                                detail={"code": "not_found",
                                        "message": "key 不存在"})
        status = payload.get("status")
        if status is None:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": "status 必填（active/disabled/revoked）"})
        if status not in KEY_STATUSES:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": f"status 越权：{status!r}"})
        try:
            store.set_key_status(key_id, status)
        except ValueError as exc:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request", "message": str(exc)})
        row = store.get_key_by_id(key_id)
        return _key_public(row) if row else {}

    @router.delete("/keys/{key_id}")
    async def delete_key(key_id: int, request: Request,
                         principal: AccountPrincipal = Depends(require_account)):
        """DELETE /api/account/keys/{kid} — 硬删自家 Key。重复删 404。"""
        store = _get_store(request)
        key = store.get_key_by_id(key_id)
        if key is None or int(key.get("user_id") or 0) != principal.user_id:
            raise HTTPException(status_code=404,
                                detail={"code": "not_found",
                                        "message": "key 不存在"})
        try:
            with store._lock:
                conn = store._db()
                owned = conn.execute(
                    "SELECT id FROM api_keys WHERE id=? AND user_id=?",
                    (key_id, principal.user_id)).fetchone()
                if owned is None:
                    raise HTTPException(
                        status_code=404,
                        detail={"code": "not_found",
                                "message": "key 不存在"})
                conn.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
                conn.commit()
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500,
                                detail={"code": "internal", "message": str(exc)})
        return {"id": key_id}

    # ------------------------------------------------------------------
    # 用量
    # ------------------------------------------------------------------

    @router.get("/usage")
    async def usage(request: Request,
                    principal: AccountPrincipal = Depends(require_account)):
        """GET /api/account/usage — 本账号累计用量 + 预算消费（JSON 数字）。"""
        store = _get_store(request)
        user = store.get_user_by_id(principal.user_id)
        if user is None:
            # JWT 已发但账号被 admin 删除：401 语义（当前主体已失效）
            # 但保留同一 JWT 大小以便前端 handler 复用 401 分派
            raise HTTPException(status_code=401,
                                detail={"code": "auth",
                                        "message": "账号已失效"})
        cfg = store.sum_usage_for_user(principal.user_id)
        out = dict(cfg)
        out["budget_consumed"] = int(user.get("budget_consumed") or 0)
        out["token_budget"] = user.get("token_budget")
        out["budget_period"] = user.get("budget_period")
        out["budget_reset_at"] = user.get("budget_reset_at")
        return out

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    # **顺序敏感**：`/sessions/search` 必须在 `/sessions/{session_id}` **之前**
    # 注册——后者的 `{session_id}` 是 int 但 FastAPI 路由匹配是"先注册先命中"，
    # 顺序颠倒导致 `search` 被当作 int 解析（422）。这是本项目路由五方法齐全时
    # 的护栏，别换顺序。

    @router.get("/sessions/search")
    async def search_sessions(request: Request, q: str = "",
                              principal: AccountPrincipal = Depends(require_account)):
        """GET /api/account/sessions/search?q=<kw> — 消息内容搜索，命中带会话上下文。

        前端用途：'我的历史消息里出现过 X'；返回 [{id, session_id, role,
        content, title, model}, ...]——`title` / `model` 从 sessions 表 join
        取来，方便前端直接渲染分组。
        """
        store = _get_store(request)
        if not q or not q.strip():
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": "q 必填且非空"})
        rows = store.search_messages(principal.user_id, q.strip(), limit=100)
        return {"messages": rows}

    @router.get("/sessions")
    async def list_sessions(request: Request, q: str = "",
                            principal: AccountPrincipal = Depends(require_account)):
        """GET /api/account/sessions — 自家会话列表（活跃倒序）。

        `q` 非空时命中标题 **或** 消息内容（走 SQL `LIKE ? ESCAPE '\\'`，
        与 store.list_sessions 一致）。
        """
        store = _get_store(request)
        rows = store.list_sessions(principal.user_id, q=q.strip(), limit=200)
        return {"sessions": [_session_public(r) for r in rows]}

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: int, request: Request,
                          principal: AccountPrincipal = Depends(require_account)):
        """GET /api/account/sessions/{id} — 单条会话。跨用户 404。"""
        store = _get_store(request)
        row = store.get_session(session_id, user_id=principal.user_id)
        if row is None:
            raise HTTPException(status_code=404,
                                detail={"code": "not_found",
                                        "message": "会话不存在"})
        return _session_public(row)

    @router.get("/sessions/{session_id}/export")
    async def export_session(session_id: int, request: Request,
                             principal: AccountPrincipal = Depends(require_account)):
        """GET /api/account/sessions/{id}/export — 会话元 + 全部消息（时间正序）。"""
        store = _get_store(request)
        payload = store.export_session(session_id, user_id=principal.user_id)
        if payload is None:
            raise HTTPException(status_code=404,
                                detail={"code": "not_found",
                                        "message": "会话不存在"})
        return {
            "session": _session_public(payload["session"]),
            "messages": payload["messages"],
        }

    @router.delete("/sessions/{session_id}")
    async def delete_session(session_id: int, request: Request,
                             principal: AccountPrincipal = Depends(require_account)):
        """DELETE /api/account/sessions/{id} — 幂等（跨用户/已删均 404）。"""
        store = _get_store(request)
        ok = store.delete_session(session_id, user_id=principal.user_id)
        if not ok:
            raise HTTPException(status_code=404,
                                detail={"code": "not_found",
                                        "message": "会话不存在"})
        return {"id": session_id}

    return router


__all__ = ["create_account_self_router"]
