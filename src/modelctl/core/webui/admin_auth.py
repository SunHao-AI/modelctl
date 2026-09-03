#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_auth.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/2 10:00
# @Desc   : 管理 API Bearer Token 认证依赖
# ===============================================================================

"""core/webui/admin_auth.py — 管理 API 的 Bearer Token 认证依赖。

复用 .env 中 API_KEY 作为唯一令牌，与 CLI/网关的密钥口径一致；比较用
hmac.compare_digest 恒定时间比较防时序泄露。FastAPI 在 create_admin_router 内
部延迟导入后作为 Depends 注入到各端点（/login、/health 外全部需要）。
"""

from __future__ import annotations

import hmac
import os

from fastapi import Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Bearer令牌掩码前缀长度之外的可视位数
API_KEY_ENV = "API_KEY"

# auto_error=False：缺头时不抛内置 403，统一由 require_auth 抛 401（code=auth），
# 与前端拦截器期望的 {"code":"auth"} 一致。
bearer_scheme = HTTPBearer(auto_error=False)

# 密钥脱敏保留的末位字符数
_MASK_KEEP_TAIL = 4

# 懒加载标记：进程内只触发一次 load_env（与 load_env 自身的幂等性叠加）
_env_loaded: bool = False


def _ensure_env_loaded() -> None:
    """首次认证调用前确保 .env 已加载（懒加载，进程级单次副作用）。

    修复场景：webui 子命令在 _cmd_webui 中不显式 load_env，首次访问 /admin/api/*
    时环境变量可能尚未从 .env 加载到位，导致 is_valid_key 与 require_auth 都按
    "API_KEY 未配置" 处理。本函数在 require_auth 入口调用一次，与 /login 端点的
    load_env 行为一致；对纯 gateway 进程零影响（admin 路由未挂载时不会触发）。
    """
    global _env_loaded
    if _env_loaded:
        return
    try:
        from modelctl.core.envfile import load_env

        load_env()
    except Exception:  # noqa: BLE001 — 加载失败时直接走"未配置"分支（保持 401 路径）
        pass
    _env_loaded = True


def _auth_error(message: str) -> HTTPException:
    """统一 401 响应格式：detail 为 {"code": "auth", "message": "..."}。"""
    return HTTPException(status_code=401, detail={"code": "auth", "message": message})


def is_valid_key(key: str) -> bool:
    """key 是否与环境变量 API_KEY 恒定时间相等；API_KEY 未配置时恒 False。"""
    expected = os.environ.get(API_KEY_ENV)
    if not expected or not key:
        return False
    return hmac.compare_digest(key, expected)


def mask_key(key: str) -> str:
    """密钥脱敏：返回 "***" + 末4位；短于4位时仅 "***"。"""
    if not key:
        return "***"
    return "***" + key[-_MASK_KEEP_TAIL:]


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    """强制认证依赖：任一校验失败即抛 401（code=auth）。

    校验顺序：API_KEY 已配置 → 已提供凭据 → 方案为 Bearer → token 恒定时间比较通过。
    """
    _ensure_env_loaded()
    if not os.environ.get(API_KEY_ENV):
        raise _auth_error("API_KEY 未配置")
    if credentials is None:
        raise _auth_error("未提供认证凭据")
    if credentials.scheme != "Bearer":
        raise _auth_error("不支持的认证方案")
    if not hmac.compare_digest(credentials.credentials, os.environ[API_KEY_ENV]):
        raise _auth_error("认证失败")


async def optional_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> bool:
    """可选认证：合法 Bearer token 且匹配 API_KEY 返回 True，否则 False。

    供 /login 端点做存在性校验（不抛异常，由端点自行决定 200/401）。
    """
    if credentials is None or credentials.scheme != "Bearer":
        return False
    return is_valid_key(credentials.credentials)


async def require_auth_or_query(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    key: str = Query(default="", description="事件源(SSE)无 header 时的 query 鉴权备选"),
) -> None:
    """认证依赖：Bearer header 或 ?key= query 任一匹配 API_KEY 即通过。

    事件源(EventSource)不能自定义 header，前端 Task/Model 日志流用 ``?key=``
    携带 token；本依赖在标准 require_auth 失败时回退到 query。
    与 require_auth 同源（``_ensure_env_loaded()`` + 恒定时间比较）。
    """
    _ensure_env_loaded()
    if not os.environ.get(API_KEY_ENV):
        raise _auth_error("API_KEY 未配置")
    # Bearer 头优先
    if (
        credentials is not None
        and credentials.scheme == "Bearer"
        and hmac.compare_digest(credentials.credentials, os.environ[API_KEY_ENV])
    ):
        return
    # query 降级（事件源；key 为空时 fail-closed 走 401）
    if key and key.strip() and hmac.compare_digest(key.strip(), os.environ[API_KEY_ENV]):
        return
    raise _auth_error("未提供认证凭据或认证失败")
