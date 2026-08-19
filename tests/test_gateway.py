"""modelctl.core.gateway 单元测试（注册表构建 + model 解析 + 路由转发）。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx

from modelctl.core.gateway import GatewayModel, build_registry, create_app, resolve_model


def _run(coro):
    return asyncio.run(coro)


async def _post(app, path: str, json: dict | None = None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=json or {})


def test_build_registry(tmp_path):
    (tmp_path / "qwen.yaml").write_text(
        "name: qwen3.8\nengine: ollama\nport: 11434\n\nollama:\n  model: qwen3.8:27b\n", encoding="utf-8"
    )
    (tmp_path / "ds.yaml").write_text(
        "name: deepseek-v4-flash\nengine: llamacpp\nport: 18888\n", encoding="utf-8"
    )
    reg = build_registry(models_dir=tmp_path)
    assert set(reg) == {"qwen3.8", "deepseek-v4-flash"}
    assert reg["qwen3.8"].backend_url == "http://127.0.0.1:11434"
    assert reg["qwen3.8"].upstream_model == "qwen3.8:27b"
    assert reg["deepseek-v4-flash"].upstream_model == "deepseek-v4-flash"


def test_resolve_model():
    reg = {
        "a": GatewayModel("a", "ollama", "http://127.0.0.1:1", "a:1", None, "http://127.0.0.1:1/"),
        "b": GatewayModel("b", "llamacpp", "http://127.0.0.1:2", "b", None, "http://127.0.0.1:2/"),
    }
    assert resolve_model(reg, "a", "b") is reg["a"]        # 显式命中
    assert resolve_model(reg, "unknown", "a") is reg["a"]  # 未知 → 回退默认
    assert resolve_model(reg, None, "b") is reg["b"]       # 省略 → 默认
    assert resolve_model(reg, "unknown", None) is None


async def _get(app, path: str):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


def test_list_models_health_filtered():
    reg = {"qwen3.8": GatewayModel("qwen3.8", "ollama", "http://upstream", "qwen3.8:27b", None, "http://upstream/")}
    app = create_app(reg, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with patch("modelctl.core.gateway.is_model_healthy", return_value=True):
        resp = _run(_get(app, "/v1/models"))
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["data"]] == ["qwen3.8"]


def test_proxy_rewrites_model_to_upstream():
    captured = {}

    def upstream(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "1", "object": "chat.completion", "model": captured["body"]["model"]})

    reg = {"qwen3.8": GatewayModel("qwen3.8", "ollama", "http://upstream", "qwen3.8:27b", None, "http://upstream/")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(upstream))
    resp = _run(
        _post(app, "/v1/chat/completions", json={"model": "qwen3.8", "messages": [{"role": "user", "content": "hi"}]})
    )
    assert resp.status_code == 200
    assert captured["body"]["model"] == "qwen3.8:27b"   # 已改写为 ollama 期望名
    assert resp.json()["model"] == "qwen3.8:27b"


def test_proxy_unknown_model_falls_back_to_default():
    def upstream(request):
        return httpx.Response(200, json={"id": "1", "model": json.loads(request.content)["model"]})

    reg = {
        "ds": GatewayModel("deepseek-v4-flash", "llamacpp", "http://upstream", "deepseek-v4-flash", None, "http://upstream/")
    }
    app = create_app(reg, default_model="ds", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "ghost-model", "messages": []}))
    assert resp.status_code == 200
    assert resp.json()["model"] == "deepseek-v4-flash"


def test_proxy_404_when_no_default_matches():
    reg = {
        "ds": GatewayModel("deepseek-v4-flash", "llamacpp", "http://upstream", "deepseek-v4-flash", None, "http://upstream/")
    }
    app = create_app(reg, default_model=None, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "ghost-model", "messages": []}))
    assert resp.status_code == 404


def test_proxy_streaming_sse_passthrough():
    def upstream(request):
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b'data: {"a": 1}\n\ndata: [DONE]\n\n'),
            headers={"content-type": "text/event-stream"},
        )

    reg = {
        "ds": GatewayModel("deepseek-v4-flash", "llamacpp", "http://upstream", "deepseek-v4-flash", None, "http://upstream/")
    }
    app = create_app(reg, default_model="ds", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "ds", "stream": True, "messages": []}))
    assert resp.status_code == 200
    assert 'data: {"a": 1}' in resp.text
    assert "data: [DONE]" in resp.text


def test_proxy_502_on_upstream_unreachable():
    def upstream(request):
        raise httpx.ConnectError("connection refused")

    reg = {
        "ds": GatewayModel("deepseek-v4-flash", "llamacpp", "http://upstream", "deepseek-v4-flash", None, "http://upstream/")
    }
    app = create_app(reg, default_model="ds", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "ds", "messages": []}))
    assert resp.status_code == 502


def test_proxy_passthrough_upstream_status():
    def upstream(request):
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    reg = {
        "ds": GatewayModel("deepseek-v4-flash", "llamacpp", "http://upstream", "deepseek-v4-flash", None, "http://upstream/")
    }
    app = create_app(reg, default_model="ds", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "ds", "messages": []}))
    assert resp.status_code == 429
    assert resp.json()["error"]["message"] == "rate limited"


def test_build_registry_registers_aliases(tmp_path):
    (tmp_path / "ds.yaml").write_text(
        "name: deepseek-v4-flash-llamacpp\nalias: deepseek-v4-flash\nengine: llamacpp\nport: 18888\n",
        encoding="utf-8",
    )
    reg = build_registry(models_dir=tmp_path)
    assert set(reg) == {"deepseek-v4-flash-llamacpp", "deepseek-v4-flash"}
    assert reg["deepseek-v4-flash"] is reg["deepseek-v4-flash-llamacpp"]
