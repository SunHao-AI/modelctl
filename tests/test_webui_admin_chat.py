#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_webui_admin_chat.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/12 10:00
# @Desc   : Task 3 契约 — /admin/api/chat/completions 代理端点
# ===============================================================================

"""/admin/api/chat/completions 契约：direct/gateway 路由、SSE 透传、错误原样透传。"""

from __future__ import annotations

import asyncio
import json

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from modelctl.core.gateway import GatewayModel, create_app  # noqa: E402

_ADMIN_KEY = "sk-admin-chat-test-1234"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("API_KEY", _ADMIN_KEY)


def _gm(name, port, api_key="sk-up", group=None, engine="vllm"):
    return GatewayModel(
        name=name, engine=engine, backend_url=f"http://127.0.0.1:{port}",
        upstream_model=f"{name}-up", api_key=api_key,
        health_url=f"http://127.0.0.1:{port}/health", group=group,
    )


class _Body(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for c in self._chunks:
            yield c


def _run(coro):
    return asyncio.run(coro)


def _sse_capture(captured: dict):
    """记录上游请求要素，返回一段标准 OpenAI SSE。"""

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_Body([
                b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
                b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
                b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
                b'data: [DONE]\n\n',
            ]),
        )

    return httpx.MockTransport(handler)


def _post(app, payload, headers=None):
    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=30
        ) as c:
            return await c.post(
                "/admin/api/chat/completions", json=payload,
                headers=headers if headers is not None else {"Authorization": f"Bearer {_ADMIN_KEY}"},
            )

    return _run(go())


def _app(reg, groups=None):
    return create_app(admin=True, registry=reg, groups=groups or {})


def test_direct_mode_hits_selected_backend(monkeypatch):
    captured: dict = {}
    reg = {"a": _gm("a", 8001), "b": _gm("b", 8002)}
    monkeypatch.setattr("modelctl.core.gateway.is_model_available", lambda m: True)
    app = _app(reg)
    app.state.chat_transport = _sse_capture(captured)
    resp = _post(app, {"model": "a", "route_mode": "direct",
                       "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert captured["url"] == "http://127.0.0.1:8001/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-up"          # 上游 key 来自 profile，非调用方
    assert captured["body"]["model"] == "a-up"          # 已改写为 upstream_model
    assert captured["body"]["stream"] is True
    assert captured["body"]["stream_options"] == {"include_usage": True}
    assert resp.headers["x-chat-routed-to"] == "a"
    assert resp.headers["x-accel-buffering"] == "no"
    assert "Hel" in resp.text and "lo" in resp.text


def test_direct_mode_ignores_group_route(monkeypatch):
    """direct 的语义就是选谁打谁：model 命中 group 名也不做家族解析。"""
    captured: dict = {}
    a = _gm("a", 8001, group="fam")
    monkeypatch.setattr("modelctl.core.gateway.is_model_available", lambda m: True)
    app = _app({"a": a, "fam": a}, {"fam": [a]})
    app.state.chat_transport = _sse_capture(captured)
    resp = _post(app, {"model": "fam", "route_mode": "direct",
                       "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 404  # "fam" 是 group 名而非 profile 名 → 不做家族解析


def test_gateway_mode_follows_group_route(monkeypatch):
    captured: dict = {}
    a = _gm("a", 8001, group="fam")
    monkeypatch.setattr("modelctl.core.gateway.is_model_available", lambda m: True)
    app = _app({"a": a, "fam": a}, {"fam": [a]})
    app.state.chat_transport = _sse_capture(captured)
    resp = _post(app, {"model": "fam", "route_mode": "gateway",
                       "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert captured["url"] == "http://127.0.0.1:8001/v1/chat/completions"
    assert resp.headers["x-chat-routed-to"] == "a"
    assert resp.headers["x-chat-route-reason"] == "group_route"


def test_model_not_running_409(monkeypatch):
    reg = {"a": _gm("a", 8001)}
    monkeypatch.setattr("modelctl.core.gateway.is_model_available", lambda m: False)
    resp = _post(_app(reg), {"model": "a", "route_mode": "direct",
                             "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "model_not_running"


def test_upstream_error_passes_through_with_route_header(monkeypatch):
    reg = {"a": _gm("a", 8001)}
    monkeypatch.setattr("modelctl.core.gateway.is_model_available", lambda m: True)
    app = _app(reg)
    app.state.chat_transport = httpx.MockTransport(
        lambda req: httpx.Response(400, json={"error": {"message": "max 0 images allowed"}})
    )
    resp = _post(app, {"model": "a", "route_mode": "direct",
                       "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 400
    assert "max 0 images" in resp.json()["error"]["message"]
    assert resp.headers["x-chat-routed-to"] == "a"   # 报 400 也要知道打给了谁


def test_system_prompt_prepended_and_whitelist(monkeypatch):
    captured: dict = {}
    reg = {"a": _gm("a", 8001)}
    monkeypatch.setattr("modelctl.core.gateway.is_model_available", lambda m: True)
    app = _app(reg)
    app.state.chat_transport = _sse_capture(captured)
    _post(app, {"model": "a", "route_mode": "direct", "system": "be brief",
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.2, "tools": [{"type": "function"}]})
    body = captured["body"]
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert body["temperature"] == 0.2
    assert "tools" not in body          # 白名单外字段不透传


def test_include_usage_false(monkeypatch):
    captured: dict = {}
    reg = {"a": _gm("a", 8001)}
    monkeypatch.setattr("modelctl.core.gateway.is_model_available", lambda m: True)
    app = _app(reg)
    app.state.chat_transport = _sse_capture(captured)
    _post(app, {"model": "a", "route_mode": "direct", "include_usage": False,
                "messages": [{"role": "user", "content": "hi"}]})
    assert "stream_options" not in captured["body"]


def test_empty_messages_400():
    resp = _post(_app({}), {"model": "a", "messages": []})
    assert resp.status_code == 400


def test_oversized_body_413():
    big = "x" * (24 * 1024 * 1024 + 10)
    resp = _post(_app({}), {"model": "a", "messages": [{"role": "user", "content": big}]})
    assert resp.status_code == 413


def test_upstream_unreachable_502_includes_route_headers(monkeypatch):
    """上游不可达（连接失败）走 502，同样必须带 X-Chat-Routed-To（spec §4.1 line 77）。"""
    reg = {"a": _gm("a", 8001)}
    monkeypatch.setattr("modelctl.core.gateway.is_model_available", lambda m: True)
    app = _app(reg)
    app.state.chat_transport = httpx.MockTransport(
        lambda req: (_ for _ in ()).throw(httpx.ConnectError("boom"))
    )
    resp = _post(app, {"model": "a", "route_mode": "direct",
                       "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_error"
    assert resp.headers["x-chat-routed-to"] == "a"


def test_requires_admin_auth():
    resp = _post(_app({}), {"model": "a", "messages": [{"role": "user", "content": "hi"}]}, headers={})
    assert resp.status_code == 401
