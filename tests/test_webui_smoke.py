#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web UI Smoke test: create_app(admin=True) 全部端点是否响应。

预期（在 API_KEY 已配置时）：
- /admin/api/health            200
- /admin/api/overview 等       200
- /v1/models                   200
- 无凭据访问管理面             401

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


@pytest.fixture()
def admin_client(monkeypatch, tmp_path):
    """注入 API_KEY 并挂载管理面；AUDIT_DIR 由 conftest 隔离到 tmp_path。"""
    monkeypatch.setenv("API_KEY", KEY)
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
    ("/v1/models", {}, 200),
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
