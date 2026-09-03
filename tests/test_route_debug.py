#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理面路由注册冒烟：create_app(admin=True) 的路由清单可枚举且非空。

原为脚本（模块级 create_app + print），收集阶段即构建应用；现改为标准用例。
完整端点行为见 test_webui_smoke.py。
"""
from __future__ import annotations

import pytest
from modelctl.core.gateway import create_app


@pytest.fixture()
def admin_routes(monkeypatch):
    """管理面路由清单，走 app.openapi()["paths"] 口径。

    该 FastAPI 版本的 app.routes 里 include_router 产出懒展开内部结构
    （_IncludedRouter），直接遍历平铺不到子路由；openapi 是稳定公开口径。
    """
    monkeypatch.setenv("API_KEY", "test_key_12345")
    app = create_app(admin=True)
    out = []
    for path, ops in app.openapi()["paths"].items():
        if path.startswith("/admin/api"):
            out.append((path, set(ops)))
    return out


def test_admin_routes_non_empty(admin_routes):
    assert admin_routes, "create_app(admin=True) 未注册任何 /admin/api 路由"


def test_admin_routes_have_methods(admin_routes):
    """每条管理面路由至少注册一个 HTTP 方法。"""
    bad = [p for p, methods in admin_routes if not methods]
    assert not bad, f"管理面路由缺 HTTP 方法注册：{bad}"
