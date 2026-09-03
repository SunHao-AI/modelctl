#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Debug: 打印 admin_router 的所有路由。"""
from __future__ import annotations

import sys

sys.path.insert(0, "src")
import os

os.environ.setdefault("API_KEY", "test_key_12345")

from modelctl.core.gateway import create_app

app = create_app(admin=True)
for r in app.routes:
    p = getattr(r, "path", None)
    if p is None:
        continue
    methods = getattr(r, "methods", None)
    if p.startswith("/admin/api"):
        print(p, methods)
print("---")
print("total routes:", len(app.routes))
