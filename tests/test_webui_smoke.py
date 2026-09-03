#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web UI Smoke test: create_app(admin=True) 全部端点是否响应。

预期（在 API_KEY 已配置时）：
- /admin/api/health            200
- /admin/api/login             200 / 401 （body {api_key 或 key}）
- /admin/api/overview          200 / 401
- /admin/api/models            200 / 401
- /admin/api/services          200 / 401
- /admin/api/audit             200 / 401
- /admin/api/envs              200 / 401
- /admin/api/nginx-snippet     200 / 401（需 node/host）
- /admin/api/config/static     200 / 401
- /admin/api/probe             200 / 401
- /v1/models                   200
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, "src")
os.environ.setdefault("API_KEY", "test_key_12345")

from loguru import logger

logger.remove()

from fastapi.testclient import TestClient
from modelctl.core.gateway import create_app

app = create_app(admin=True)
H = {"Authorization": "Bearer test_key_12345"}

with TestClient(app) as c:
    routes = sorted({r.path for r in app.routes if hasattr(r, "path") and r.path.startswith("/admin/api")})
    print("admin routes:")
    for r in routes:
        print("  ", r)
    print()

    checks = [
        ("GET", "/admin/api/health", {}, None),
        ("GET", "/admin/api/overview", {}, 200),
        ("GET", "/admin/api/models", {}, 200),
        ("GET", "/admin/api/services", {}, 200),
        ("GET", "/admin/api/audit", {"limit": 1}, 200),
        ("GET", "/admin/api/envs", {}, 200),
        ("GET", "/admin/api/probe", {}, 200),
        ("GET", "/admin/api/nginx-snippet", {"node": "210", "host": "x"}, 200),
        ("GET", "/admin/api/config/static", {}, 200),
        ("GET", "/v1/models", {}, 200),
    ]
    rc = 0
    for method, path, params, expect in checks:
        if method == "GET":
            r = c.get(path, headers=H, params=params or None)
        status = r.status_code
        ok = (status == expect) if expect is not None else status in (200, 401)
        mark = "OK " if ok else "FAIL"
        if not ok:
            rc = 1
        print(f"{mark} {method} {path} -> {status} (expect {expect})")
    print()
    # login 两个字段都试
    r = c.post("/admin/api/login", json={"api_key": "test_key_12345"})
    print("login(api_key):", r.status_code, r.json() if r.status_code < 500 else r.text[:120])
    r = c.post("/admin/api/login", json={"key": "test_key_12345"})
    print("login(key):", r.status_code, r.json() if r.status_code < 500 else r.text[:120])
    # 401
    r = c.get("/admin/api/overview")
    print("no auth:", r.status_code, r.json())

    sys.exit(rc)
