#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_webui_admin_config.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/11 10:00
# @Desc   : 配置端点契约测试：.env 脱敏模式匹配（WEB-P1-1）与 nginx-snippet 响应键（X-P1）
# ===============================================================================

"""admin_config：脱敏口径与 nginx-snippet 契约。

- ``GET /admin/api/config/static``：历史上只按 2 项白名单脱敏，新增的
  ``*_SECRET``/``*_TOKEN``/``GATEWAY_CLIENT_API_KEY`` 等明文回显（WEB-P1-1）；
  修复后按"显式名单 + 语义模式"判定。
- ``GET /admin/api/nginx-snippet``：后端曾返回 ``{"content": ...}`` 而前端
  （types.ts / ConfigView.vue）读 ``.snippet``，生成器永远空白（X-P1）。
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402
from modelctl.core.webui.admin_config import _is_sensitive_key  # noqa: E402

KEY = "test_key_admin_config"


@pytest.fixture()
def admin_client(monkeypatch):
    """管理面客户端；数据目录由 conftest autouse 隔离到 tmp_path。"""
    monkeypatch.setenv("API_KEY", KEY)
    logger.remove()
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c


def _get(client, path, **kw):
    return client.get(path, headers={"Authorization": f"Bearer {KEY}"}, params=kw or None)


@pytest.mark.parametrize("key", [
    "API_KEY", "UNSLOTH_API_KEY", "GATEWAY_CLIENT_API_KEY", "ACCOUNTS_JWT_SECRET",
    "DB_PASSWORD", "GH_TOKEN", "ACCESS_SECRET", "admin_passwd", "upstream-key",
])
def test_sensitive_keys_matched_by_pattern(key: str):
    assert _is_sensitive_key(key) is True


@pytest.mark.parametrize("key", [
    "MODEL_ROOT", "MODELSCOPE_CACHE", "HF_HOME", "GATEWAY_PORT",
    "CLUSTER_ROLE", "LOG_DIR", "OLLAMA_MODELS", "TZ",
])
def test_non_sensitive_keys_not_masked(key: str):
    assert _is_sensitive_key(key) is False


def test_read_env_masks_pattern_matched_keys(monkeypatch, admin_client):
    """端点级：命中语义模式的键必须脱敏，普通键保持明文可读。"""
    from modelctl.core.webui import admin_config as ac

    payload = {
        "MODEL_ROOT": "/models",
        "ACCOUNTS_JWT_SECRET": "super-secret-value-1234",
        "GATEWAY_CLIENT_API_KEY": "sk-client-abcd1234",
        "DB_PASSWORD": "p@ssw0rd-plain",
    }
    monkeypatch.setattr(ac, "_load_env_file", lambda path: dict(payload))
    body = _get(admin_client, "/admin/api/config/static").json()
    entries = {e["key"]: e for e in body["entries"]}

    secret = entries["ACCOUNTS_JWT_SECRET"]
    assert secret["sensitive"] is True
    assert secret["value"].startswith("***")
    assert "super-secret" not in secret["value"]
    assert entries["GATEWAY_CLIENT_API_KEY"]["sensitive"] is True
    assert entries["DB_PASSWORD"]["sensitive"] is True
    assert "p@ssw0rd" not in entries["DB_PASSWORD"]["value"]
    assert entries["MODEL_ROOT"]["sensitive"] is False
    assert entries["MODEL_ROOT"]["value"] == "/models"


def test_nginx_snippet_returns_snippet_key(admin_client):
    """契约键必须是 snippet（web/src/api/types.ts NginxSnippetResponse）。"""
    body = _get(admin_client, "/admin/api/nginx-snippet", node="210", host="10.0.0.5").json()
    assert "snippet" in body, f"契约键必须是 snippet，实得 keys={sorted(body)}"
    assert isinstance(body["snippet"], str)
    assert body.get("ok") is True
