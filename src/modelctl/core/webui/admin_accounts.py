#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_accounts.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/9 16:00
# @Desc   : WebUI 管理面板 — 账号 / Key / 用量 CRUD
# ===============================================================================

"""core/webui/admin_accounts.py — 管理员侧的账号 / Key / 用量面板端点。

挂在 `/admin/api` 前缀下；全部端点走 `Depends(require_auth)`（Bearer API_KEY，
**管理面单钥**——非 JWT；JWT 是账号面板侧的登录态，二者身份体系完全独立）。

**accounts 未启用（app.state.accounts is None）时所有端点 503**：与数据面
"accounts 未启用则完美走 legacy verify_client" 保持对称——管理面板无法在无店
的部署上挂空路由（否则 404 vs 503 会让前端难以区分"尚未部署 accounts"与
"权限不足"，统一 503 更清晰）。

Key 明文**只在签发时刻一次性返回**（POST /accounts/{id}/keys 201 响应体包含
`key` 字段），GET 列表只见 `key_prefix` 脱敏。前端必须在弹窗告知"仅显示一次"
之后收起；后端**没有**任何"再显示一次"的接口。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request

from modelctl.core.webui.admin_auth import require_auth

# 模块级 router 单例：同 admin_models._router() 的用法（Module 内建 path 无起止
# 冲突时 include 一次即可；重复 create_admin_router 得到同一 router，不再 re-mount，
# 防止前缀重入导致 openapi 里出现重复路径模板）。
router = APIRouter()


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------

def _get_store(request: Request):
    """取 `app.state.accounts`；未启用 → 503（与普通 401 语义区分）。

    注：不叫 AccountsStore 类型标注是因为 `AccountsStore` 延迟导入（主包可能
    无 sqlite3 路径依赖……标准库，理论上无依赖；但 accounts/accountant 会在
    启用分支 lazy import 全都是 modelscope/vllm 环境下才装好。本模块只做 SQLite
    读取，不用管这些，但类型标注跳过能保持 grep 时可见性统一）。
    """
    store = request.app.state.accounts
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "accounts_disabled",
                    "message": "账号体系未启用（.env 设 ACCOUNTS_ENABLED=true）"})
    return store


def _require_store(request: Request):
    """与 _get_store 一致，但专为 Depends 用法存在的别名（可读性）。"""
    return _get_store(request)


def _user_public(row: dict, *, include_password_hash: bool = False) -> dict:
    """users 行 → JSON 输出对象。**永不含 password_hash**（`include_password_hash`
    只是个签名占位防御，实际端点不传 True；如改了签名故意保留永乐显性）。
    时间列 REAL → 空位留原样由前端 format，避免后端二次格式化双重口径。
    """
    _ = include_password_hash  # 保留字段位，不出参
    out = {c: row[c] for c in (
        "id", "username", "display_name", "is_admin", "status",
        "concurrency_limit", "rpm_limit", "tpm_limit", "token_budget",
        "budget_consumed", "budget_period", "budget_reset_at", "retention_days",
        "created_at", "updated_at") if c in row}
    return out


def _key_public(row: dict) -> dict:
    """api_keys 行 → JSON 输出。**永不含 key_hash / key 明文**（后者 KY 不存）。"""
    return {c: row[c] for c in (
        "id", "user_id", "key_prefix", "name", "status",
        "expires_at", "last_used_at", "created_at") if c in row}


def _verify_user_exists(store, user_id: int) -> dict:
    user = store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404,
                            detail={"code": "not_found",
                                    "message": f"account {user_id} 不存在"})
    return user


def _verify_key_owned(store, key_id: int, user_id: int) -> dict:
    key = store.get_key_by_id(key_id)
    if key is None or int(key.get("user_id") or 0) != user_id:
        raise HTTPException(status_code=404,
                            detail={"code": "not_found",
                                    "message": f"key {key_id} 不存在或不属于该账号"})
    return key


# ---------------------------------------------------------------------------
# 账号 CRUD
# ---------------------------------------------------------------------------

@router.get("/accounts")
async def list_accounts(request: Request, _: None = Depends(require_auth)):
    """GET /admin/api/accounts — 账号列表（不返回密码 hash）。"""
    store = _get_store(request)
    rows = store.list_users()
    return {"accounts": [_user_public(r) for r in rows]}


@router.post("/accounts", status_code=201)
async def create_account(request: Request, payload: dict,
                         _: None = Depends(require_auth)):
    """POST /admin/api/accounts — 建号。

    必填：`username`（非空 str）、`password`（非空 str）。
    可选：`display_name` / `is_admin` / `concurrency_limit` / `rpm_limit` /
    `tpm_limit` / `token_budget` / `budget_period`（默认 "day"）/ `retention_days`。

    username 重复（UNIQUE）→ 409。
    """
    store = _get_store(request)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not isinstance(username, str):
        raise HTTPException(status_code=400,
                            detail={"code": "bad_request",
                                    "message": "username 必填且为非空字符串"})
    if not password or not isinstance(password, str):
        raise HTTPException(status_code=400,
                            detail={"code": "bad_request",
                                    "message": "password 必填且为非空字符串"})
    # 延迟导入 hashing（走 bcrypt）：管理面板才可写——非数据面路径
    from modelctl.core.accounts.hashing import hash_password

    kwargs: dict = dict(
        username=username,
        password_hash=hash_password(password),
        display_name=str(payload.get("display_name") or ""),
        is_admin=bool(payload.get("is_admin", False)),
        budget_period=str(payload.get("budget_period") or "day"),
    )
    for k in ("concurrency_limit", "rpm_limit", "tpm_limit", "token_budget",
              "retention_days"):
        v = payload.get(k)
        if v is not None:
            kwargs[k] = int(v)
    except_e: Exception | None = None
    try:
        uid = store.create_user(now=time.time(), **kwargs)
    except Exception as exc:  # noqa: BLE001 — sqlite3.IntegrityError → 409
        except_e = exc
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409,
                                detail={"code": "conflict",
                                        "message": f"username {username!r} 已存在"})
        raise
    _ = except_e
    row = store.get_user_by_id(uid)
    return _user_public(row)


@router.get("/accounts/{account_id}")
async def get_account(account_id: int, request: Request,
                      _: None = Depends(require_auth)) -> dict:
    """GET /admin/api/accounts/{id} — 单条账号；不存在 404。"""
    store = _get_store(request)
    row = _verify_user_exists(store, account_id)
    return _user_public(row)


@router.put("/accounts/{account_id}")
async def update_account(account_id: int, request: Request, payload: dict,
                         _: None = Depends(require_auth)) -> dict:
    """PUT /admin/api/accounts/{id} — 改限额 / is_admin / status / budget 等。

    字段白名单（不在白名单的字段静默忽略，与 store._USER_MUTABLE 一致）：
    `display_name` / `is_admin` / `status` / `concurrency_limit` / `rpm_limit` /
    `tpm_limit` / `token_budget` / `budget_period` / `retention_days` /
    `budget_reset_at`。
    `status` 越权（不在 USER_STATUSES）→ 400 再由 store 的 ValueError 兜底。
    """
    store = _get_store(request)
    _verify_user_exists(store, account_id)
    now = time.time()
    fields: dict = {}
    status_val = payload.get("status")
    if status_val is not None:
        # 提前校验 status 白名单：store 端只防 typo → 抛 ValueError；这里先行
        # 转为 400 让前端排错清晰（不吞 try，节约一次 round-trip）。
        from modelctl.core.accounts.store import USER_STATUSES
        if status_val not in USER_STATUSES:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request",
                        "message": f"status 越权：{status_val!r}（允许 {USER_STATUSES}）"})
        try:
            store.set_user_status(account_id, status_val, now=now)
        except ValueError as exc:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request", "message": str(exc)})
    for k in ("display_name", "is_admin", "concurrency_limit", "rpm_limit",
              "tpm_limit", "token_budget", "budget_period",
              "budget_reset_at", "retention_days"):
        v = payload.get(k)
        if v is None:
            continue
        fields[k] = v
    row = store.update_user_limits(account_id, now=now, **fields) if fields else \
        store.get_user_by_id(account_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail={"code": "not_found",
                                    "message": f"account {account_id} 不存在"})
    return _user_public(row)


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: int, request: Request,
                         _: None = Depends(require_auth)) -> dict:
    """DELETE /admin/api/accounts/{id} — 删号并级联清理（keys/usage/sessions/messages）。

    幂等性：首删 200（含 snapshot 摘要 id/username），重复删 404（上层前端
    可忽略 404 视为"已经干净"）。
    """
    store = _get_store(request)
    snapshot = store.delete_user(account_id)
    if snapshot is None:
        raise HTTPException(status_code=404,
                            detail={"code": "not_found",
                                    "message": f"account {account_id} 不存在"})
    return {"id": snapshot["id"], "username": snapshot.get("username", "")}


# ---------------------------------------------------------------------------
# Key 管理（挂在 /accounts/{id} 下，管理员查看全部账号的 Key）
# ---------------------------------------------------------------------------

@router.get("/accounts/{account_id}/keys")
async def list_keys(account_id: int, request: Request,
                    _: None = Depends(require_auth)) -> dict:
    """GET /admin/api/accounts/{id}/keys — 该账号全部 Key（无明文）。"""
    store = _get_store(request)
    _verify_user_exists(store, account_id)
    rows = store.list_keys_for_user(account_id)
    return {"keys": [_key_public(r) for r in rows]}


@router.post("/accounts/{account_id}/keys", status_code=201)
async def create_key(account_id: int, request: Request, payload: dict,
                     _: None = Depends(require_auth)) -> dict:
    """POST /admin/api/accounts/{id}/keys — 签发 Key，**一次性**返回明文。

    请求可选 `name` / `expires_in_days`（None 或 ≤ 0 表示永不过期）。
    201 响应体：`{id, user_id, name, key: "<明文>", key_prefix, expires_at, ...}`。
    后续任何 GET 都只见 `key_prefix`（**不再有** `key` 字段）。
    """
    store = _get_store(request)
    _verify_user_exists(store, account_id)
    from modelctl.core.accounts.hashing import generate_api_key, hash_api_key, key_prefix

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
    kid = store.create_key(user_id=account_id, key_hash=hash_api_key(cred),
                           key_prefix=key_prefix(cred), name=name,
                           expires_at=expires_at, now=now)
    row = store.get_key_by_id(kid)
    assert row is not None  # 刚 INSERT 完立刻读，若 None 属于 store 层 bug
    out = _key_public(row)
    # **本字段仅此一次**；create_key 不接收明文，UI 引导保存
    out["key"] = cred
    return out


@router.put("/accounts/{account_id}/keys/{key_id}")
async def update_key_status(account_id: int, key_id: int, request: Request,
                            payload: dict,
                            _: None = Depends(require_auth)) -> dict:
    """PUT /admin/api/accounts/{id}/keys/{kid} — state machine（active/disabled/revoked）。

    归属校验：key 必须属于 account（不同 user 的 kid 一律 404 防枚举）。
    非法 status → 400。
    """
    store = _get_store(request)
    _verify_user_exists(store, account_id)
    _verify_key_owned(store, key_id, account_id)
    from modelctl.core.accounts.store import KEY_STATUSES
    status = payload.get("status")
    if status is None:
        raise HTTPException(status_code=400,
                            detail={"code": "bad_request",
                                    "message": "status 必填（active/disabled/revoked）"})
    if status not in KEY_STATUSES:
        raise HTTPException(status_code=400,
                            detail={"code": "bad_request",
                                    "message": f"status 越权：{status!r}（允许 {KEY_STATUSES}）"})
    try:
        store.set_key_status(key_id, status)
    except ValueError as exc:
        raise HTTPException(status_code=400,
                            detail={"code": "bad_request", "message": str(exc)})
    row = store.get_key_by_id(key_id)
    return _key_public(row) if row else {}


@router.delete("/accounts/{account_id}/keys/{key_id}")
async def delete_key(account_id: int, key_id: int, request: Request,
                     _: None = Depends(require_auth)) -> dict:
    """DELETE /admin/api/accounts/{id}/keys/{kid} — 硬删 Key。

    重复删除 404（幂等语义：首删 200 + `{id}`，再次请求 404 not_found）。
    与 Key 状态 `revoked` 差异：revoked 仍查得到但鉴权拒绝；sqlite 层 DELETE
    是真删行，前端应提示"删除不可恢复"。
    """
    store = _get_store(request)
    _verify_user_exists(store, account_id)
    _verify_key_owned(store, key_id, account_id)
    # store 未显式暴露 delete_key_by_id（设计上主要靠 set_key_status=revoked
    # 走软删）；硬删这里直连 `_db` 单连接（本进程单线程访问 accounts store，
    # 无跨线程风险）并显式事务覆盖，避免上层误用后残留半删状态。
    try:
        with store._lock:
            conn = store._db()
            owned = conn.execute(
                "SELECT id FROM api_keys WHERE id=? AND user_id=?",
                (key_id, account_id)).fetchone()
            if owned is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "not_found",
                            "message": f"key {key_id} 不存在或不属于该账号"})
            conn.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
            conn.commit()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail={"code": "internal", "message": str(exc)})
    return {"id": key_id}


# ---------------------------------------------------------------------------
# 用量汇总
# ---------------------------------------------------------------------------

@router.get("/accounts/{account_id}/usage")
async def usage_summary(account_id: int, request: Request,
                        since: float | None = None,
                        _: None = Depends(require_auth)) -> dict:
    """GET /admin/api/accounts/{id}/usage — 累计用量 + 预算消费。

    字段 = sum_usage_for_user 的 4 个 SUM + 从 users 拉 `budget_consumed` /
    `token_budget`（默认不作 since 过滤：管理面板看的是"从建号起来的完整账单"）。
    前端如需分周期，加 `?since=<epoch 秒>` query 即可。
    """
    store = _get_store(request)
    user = _verify_user_exists(store, account_id)
    cfg = store.sum_usage_for_user(account_id, since=since)
    out = dict(cfg)
    out["budget_consumed"] = int(user.get("budget_consumed") or 0)
    out["token_budget"] = user.get("token_budget")
    out["budget_period"] = user.get("budget_period")
    out["budget_reset_at"] = user.get("budget_reset_at")
    return out


# ---------------------------------------------------------------------------
# 子路由工厂（admin_router 用 module:// 延迟导入签名）
# ---------------------------------------------------------------------------

def _router() -> APIRouter:
    """返回本模块 router 单例，供 `admin_router._include_subrouter` 聚挂。"""
    return router


__all__ = ["_router", "router"]
