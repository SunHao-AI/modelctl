#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web UI Smoke test: create_app(admin=True) 全部端点是否响应。

预期（在 API_KEY 已配置时）：
- /admin/api/health            200
- /admin/api/overview 等       200
- 无凭据访问管理面             401
- /v1/models                   仅网关客户端 key 放行；管理面 API_KEY 一律 401
  （数据面/管理面凭据严格隔离，详见 2026-09-07-gateway-client-auth spec）

原为脚本（模块级 create_app + print + sys.exit），会被 pytest 收集并在
collection 阶段 sys.exit 掀翻整个 session；现改为标准用例。
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_12345"
# 数据面客户端 key：与管理面 KEY 严格隔离，/v1* 只认它
CLIENT_KEY = "sk-smoke-client-key-1a2b"


@pytest.fixture()
def admin_client(monkeypatch, tmp_path):
    """注入管理面 API_KEY + 网关客户端 key 并挂载管理面；AUDIT_DIR 由 conftest 隔离到 tmp_path。"""
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", CLIENT_KEY)
    logger.remove()  # 冒烟期间静音 loguru（与脚本行为一致）
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c


# (path, params, expect)：expect=None 表示接受 200/401
CHECKS = [
    ("/admin/api/health", {}, 200),
    ("/admin/api/overview", {}, 200),
    ("/admin/api/models", {}, 200),
    ("/admin/api/services", {}, 200),
    ("/admin/api/audit", {"limit": 1}, 200),
    ("/admin/api/envs", {}, 200),
    ("/admin/api/probe", {}, 200),
    ("/admin/api/nginx-snippet", {"node": "210", "host": "x"}, 200),
    ("/admin/api/cluster/status", {}, 404),
    ("/admin/api/config/static", {}, 200),
]


@pytest.mark.parametrize("path,params,expect", CHECKS, ids=[c[0] for c in CHECKS])
def test_admin_endpoints_respond(admin_client, path, params, expect):
    r = admin_client.get(path, headers={"Authorization": f"Bearer {KEY}"}, params=params or None)
    assert r.status_code == expect, f"{path} -> {r.status_code} (expect {expect})"


def test_admin_routes_registered(admin_client):
    """管理面路由必须全部挂载（防回归：漏挂 router 时端点静默 404）。

    路由枚举走 app.openapi()["paths"]：该 FastAPI 版本的 app.routes 里 include_router
    产出的是懒展开内部结构（_IncludedRouter），直接遍历平铺不到子路由。
    """
    paths = set(admin_client.app.openapi()["paths"])
    for path, _, _ in CHECKS:
        if path.startswith("/admin/api"):
            assert path in paths, f"管理面缺少路由 {path}"


def test_login_accepts_both_field_names(admin_client):
    """login 端点同时接受 api_key / key 两种字段名。"""
    for field in ("api_key", "key"):
        r = admin_client.post("/admin/api/login", json={field: KEY})
        assert r.status_code == 200, f"login({field}) -> {r.status_code}"


def test_admin_requires_auth(admin_client):
    """无凭据访问管理面返回 401。"""
    r = admin_client.get("/admin/api/overview")
    assert r.status_code == 401


def test_v1_models_requires_client_key(admin_client):
    """数据面/管理面凭据隔离：/v1/models 只认网关客户端 key，管理面 API_KEY 一律 401。"""
    assert admin_client.get("/v1/models", headers={"Authorization": f"Bearer {CLIENT_KEY}"}).status_code == 200
    assert admin_client.get("/v1/models", headers={"Authorization": f"Bearer {KEY}"}).status_code == 401
    assert admin_client.get("/v1/models").status_code == 401


def test_non_ascii_bearer_key_returns_401_not_500(admin_client):
    """WEB-P2-1：hmac.compare_digest 直接吃含非 ASCII 的 str 抛 TypeError → 500。

    客户端用一个中文 Bearer 头就能把管理面打出 500；修复后必须恒定 401。
    头值以 UTF-8 原始字节发送（等价 curl 行为）：httpx 对 str 头值强制 ascii
    编码，会在**客户端**就抛 UnicodeEncodeError，请求根本到不了服务端。
    """
    raw = "Bearer 测试键".encode("utf-8")
    r = admin_client.get("/admin/api/config/static", headers=[(b"authorization", raw)])
    assert r.status_code == 401, f"非 ASCII 凭据必须 401，实得 {r.status_code}"
