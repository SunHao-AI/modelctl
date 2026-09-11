#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/account_auth.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/9 16:00
# @Desc   : WebUI 账号 JWT 认证（登录签发 + 依赖注入）
# ===============================================================================

"""core/webui/account_auth.py — WebUI 账号 JWT 认证基础。

对外只暴露三组入口给上层路由用（`require_account` / `require_admin` /
`issue_token`）；登录端点本身（password → JWT）在 `account_self.py` 里的
`/login` 端点直接调 `issue_token` 组装。

设计要点（改动前必读）：

1. **JWT 自管**：PyJWT HS256，payload 字段固定 `user_id / is_admin / iat / exp`
   （**不**塞 username/display_name/UI 展示列——那些应来自 store 现查，避免
   token 里的信息过期）。`iat` / `exp` 用 `int(time.time())` 秒（CLAUDE.md
   约定，避免 ms 意外触发 PyJWT 3.x 的浮点秒陷阱）。
2. **SECRET 独立密钥（fail-fast）**：只认 `ACCOUNTS_JWT_SECRET`，**不回退**
   `API_KEY`——管理面（API_KEY）与账号面（JWT）是两个信任域，共用一把秘密会让
   "管理面密钥泄漏"直接升级成"任意用户身份伪造"。缺失即抛 `RuntimeError`
   硬错暴露给部署者；测试须显式 monkeypatch `ACCOUNTS_JWT_SECRET`。
3. **`require_account` 走 Bearer JWT**，不嗅探 `x-api-key`（后者是数据面
   `x-api-key` 语义、走 `resolve_account` 独立链路；本函数的 JWT 是 WebUI 面板
   语义，关闭双通道防止 token 被误当 Key 放进 `x-api-key` 触发限流窗口）。
4. **`require_admin` 复用 `require_account`** 后按 `payload["is_admin"]` 判
   权——保持"JWT 里打钩"而非查库，让"当前 admin 视图"随登录时刻生效期
   固定；管理员被撤权的生效滚动到 token 过期（8h）为止。测试可以按 `exp`
   主动构造过期 token 验证 rolling 边界。
5. **`require_account` 所以字段越少越好**：`AccountPrincipal` 只含
   `user_id / is_admin / username`（username 兜底为 store 现查结果；JWT 里
   并未携带，保留字段是为前端 / 日志使用便利）。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

#: Token 有效期（秒）— 8 小时：足够覆盖一次工作时段 + 时区模糊；admin 撤销权限
#: 到 token 到期都还能用（滚动），如需紧急踢出 kill token 走管理员 `invalidate`
#: 端点（本 Task 未实现，Task 8 前端会展示刷新按钮）。
JWT_TTL_S = 8 * 3600

#: 账号面 JWT 专用签名密钥环境变量。
#: **不回退 API_KEY**：管理面（API_KEY）与账号面（JWT）是两个信任域，共用一把秘密
#: 会让"管理面密钥泄漏"直接升级成"任意用户身份伪造"。缺失即 fail-fast。
_JWT_SECRET_ENV = "ACCOUNTS_JWT_SECRET"

# `auto_error=False`：缺 header 不抛内置 403，统一 401（与 admin_auth 同口径，
# 前端拦截器只识 `{"code":"auth"}` 一种 401 形状）。
_bearer_jwt = HTTPBearer(auto_error=False)


@dataclass
class AccountPrincipal:
    """一次通过 JWT 认证的账号主体（webui 面板使用身份快照）。"""

    user_id: int
    is_admin: bool
    username: str = ""
    #: 展示名（供前端顶部用户名牌）。JWT 未携带时上层可空置，不影响权限。
    display_name: str = ""


def _jwt_secret() -> str:
    """取账号面 JWT 签名 secret；未配置抛 `RuntimeError`（强暴露给部署者）。"""
    raw = os.environ.get(_JWT_SECRET_ENV, "").strip()
    if raw:
        return raw
    raise RuntimeError(
        f"JWT 签名密钥未配置：请设置 {_JWT_SECRET_ENV}（账号面专用，不复用 API_KEY）")


def _auth_error(message: str) -> HTTPException:
    return HTTPException(status_code=401, detail={"code": "auth", "message": message})


def issue_token(*, user_id: int, is_admin: bool, now: int | None = None) -> str:
    """签发一个 HS256 JWT（`user_id / is_admin / iat / exp`）。

    `now` 供测试注入，生产路径缺省走 `int(time.time())`。装载侧用
    `_jwt_secret()`，签发/校验必须**同一 secret**（不一致 → 签名不匹配
    → verify_token 抛 401，测试可测）。
    """
    import jwt as _jwt  # 延迟导入：主包缺 PyJWT 时至少 allowing 其他 WebUI 子模块

    if now is None:
        now = int(time.time())
    payload = {
        "user_id": int(user_id),
        "is_admin": bool(is_admin),
        "iat": int(now),
        "exp": int(now) + JWT_TTL_S,
    }
    return _jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def verify_token(token: str) -> dict[str, Any]:
    """解码并校验 HS256 JWT；任何失败均抛 401（code=auth）。

    不区分"签名错 / 过期 / 缺字段"—上层前端只需一个"token 已失效"
    的用户提示，细分只会给中间人做弱字典探测（根据 `ExpiredSignatureError`
    区分可绕出服务端时钟偏差等）。
    """
    import jwt as _jwt

    if not token or not isinstance(token, str):
        raise _auth_error("缺少认证 token")
    try:
        payload = _jwt.decode(
            token, _jwt_secret(), algorithms=["HS256"],
            options={"require": ["exp", "iat", "user_id"]},
        )
    except _jwt.ExpiredSignatureError:
        raise _auth_error("token 已过期")
    except _jwt.InvalidTokenError:
        raise _auth_error("token 无效")
    # 显式再校一次 user_id：PyJWT `require` 只保证字段存在，不校类型
    try:
        user_id = int(payload["user_id"])
    except (KeyError, TypeError, ValueError):
        raise _auth_error("token 载荷缺少 user_id")
    return {"user_id": user_id,
            "is_admin": bool(payload.get("is_admin", False)),
            "username": str(payload.get("username", ""))}


def _ensure_env_loaded() -> None:
    """同 `admin_auth._ensure_env_loaded`——首次调 JWT 前确保 .env 已加载。

    与 admin_auth 双进度各自的 `_env_loaded` 独立：两处的 load_env() 幂等，
    重复调用无副作用（`.env` 加载后是 no-op）。本函数在 require_account
    入口调用一次防"gateway webui 首次访问 /api/account" 场景 env 未加载。
    """
    try:
        from modelctl.core.envfile import load_env

        load_env()
    except Exception:  # noqa: BLE001
        pass


async def require_account(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_jwt),
) -> AccountPrincipal:
    """强制 Bearer JWT 认证依赖；成功返回 `AccountPrincipal`。

    - 缺凭据/非 Bearer/签名错/过期/payload user_id 缺失 → 401（code=auth）。
    - **本依赖不查库**（避免每次请求都 `get_user_by_id`）；UI 需要的 display_name
      等现值让路由自己 `store.get_user_by_id(principal.user_id)` 现取，或前端
      缓存在 login 响应的 profile 里。
    """
    _ensure_env_loaded()
    if credentials is None:
        raise _auth_error("未提供认证凭据")
    if credentials.scheme != "Bearer":
        raise _auth_error("不支持的认证方案")
    decoded = verify_token(credentials.credentials)
    return AccountPrincipal(user_id=decoded["user_id"],
                            is_admin=decoded["is_admin"],
                            username=decoded.get("username", ""))


async def require_admin(
    principal: AccountPrincipal = Depends(require_account),
) -> AccountPrincipal:
    """在 require_account 基础上加"必须 admin"二次校验。

    401 vs 403 语义：require_account 已是 401 通道（token 无效 / 缺），
    这里的 `is_admin=False` 是"已认证但越权"，走 403 区分两种失败因。
    前端拦截器需感知 403：跳 403 页而非静默刷新 token。
    """
    if not principal.is_admin:
        raise HTTPException(status_code=403,
                            detail={"code": "forbidden",
                                    "message": "需要管理员权限"})
    return principal


__all__ = [
    "AccountPrincipal",
    "JWT_TTL_S",
    "issue_token",
    "verify_token",
    "require_account",
    "require_admin",
]
