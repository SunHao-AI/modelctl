#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_gateway.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 网关路由测试
# ===============================================================================

"""modelctl.core.gateway 单元测试（注册表构建 + model 解析 + 路由转发）。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx

from modelctl.core.gateway import GatewayModel, build_groups, build_registry, create_app, resolve_model


def _run(coro):
    return asyncio.run(coro)


async def _post(app, path: str, json: dict | None = None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=json or {})


async def _post_headers(app, path: str, json: dict | None = None, headers: dict | None = None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers or {}) as client:
        return await client.post(path, json=json or {})


def test_build_registry(tmp_path):
    qwen_yaml = "name: qwen3.8\nengine: ollama\nport: 11434\n\nollama:\n  model: qwen3.8:27b\n"
    (tmp_path / "qwen.yaml").write_text(qwen_yaml, encoding="utf-8")
    (tmp_path / "ds.yaml").write_text("name: deepseek-v4-flash\nengine: llamacpp\nport: 18888\n", encoding="utf-8")
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
    assert resolve_model(reg, "a", "b") is reg["a"]  # 显式命中
    assert resolve_model(reg, "unknown", "a") is reg["a"]  # 未知 → 回退默认
    assert resolve_model(reg, None, "b") is reg["b"]  # 省略 → 默认
    assert resolve_model(reg, "unknown", None) is None


async def _get(app, path: str):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


def test_list_models_health_filtered():
    reg = {"qwen3.8": GatewayModel("qwen3.8", "ollama", "http://upstream", "qwen3.8:27b", None, "http://upstream/")}
    app = create_app(reg, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with (
        patch("modelctl.core.gateway.is_model_healthy", return_value=True),
        patch("modelctl.core.gateway.is_running", return_value=True),
    ):
        resp = _run(_get(app, "/v1/models"))
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["data"]] == ["qwen3.8"]


def test_bare_v1_returns_ok_not_redirect():
    """裸 /v1（无尾斜杠）不得 307 重定向：FastAPI redirect_slashes 的 Location 是
    根相对路径 /v1/，经 B 机 nginx 前缀路由后客户端跟随会丢 /<node>/llm 前缀落空。
    裸 /v1 视为连通性探测，返回 200；真实请求走 /v1/chat/completions 子路径。"""
    reg = {"qwen3.8": GatewayModel("qwen3.8", "ollama", "http://upstream", "qwen3.8:27b", None, "http://upstream/")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    for path in ("/v1", "/v1/"):
        resp = _run(_post(app, path))
        assert resp.status_code == 200
        assert resp.headers.get("location") is None  # 不重定向
        assert resp.json()["status"] == "ok"


def test_proxy_rewrites_model_to_upstream():
    captured = {}

    def upstream(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "1", "object": "chat.completion", "model": captured["body"]["model"]})

    reg = {"qwen3.8": GatewayModel("qwen3.8", "ollama", "http://upstream", "qwen3.8:27b", None, "http://upstream/")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(upstream))
    body = {"model": "qwen3.8", "messages": [{"role": "user", "content": "hi"}]}
    resp = _run(_post(app, "/v1/chat/completions", json=body))
    assert resp.status_code == 200
    assert captured["body"]["model"] == "qwen3.8:27b"  # 已改写为 ollama 期望名
    assert resp.json()["model"] == "qwen3.8:27b"


def test_proxy_injects_thinking_disable_for_qwen38_vllm():
    """qwen3.8 家族 + 支持模板 kwargs 的引擎：默认注入 enable_thinking=false。"""
    captured = {}

    def upstream(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "1", "model": captured["body"]["model"]})

    reg = {"qwen3.8": GatewayModel("qwen3.8-vllm", "vllm", "http://upstream", "qwen3.8-vllm", None, "http://upstream/", group="qwen3.8")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "qwen3.8", "messages": [{"role": "user", "content": "hi"}]}))
    assert resp.status_code == 200
    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_proxy_respects_explicit_chat_template_kwargs():
    """请求显式传 chat_template_kwargs 时不被网关覆盖。"""
    captured = {}

    def upstream(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "1", "model": captured["body"]["model"]})

    reg = {"qwen3.8": GatewayModel("qwen3.8-vllm", "vllm", "http://upstream", "qwen3.8-vllm", None, "http://upstream/", group="qwen3.8")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(upstream))
    body = {"model": "qwen3.8", "messages": [{"role": "user", "content": "hi"}], "chat_template_kwargs": {"enable_thinking": True}}
    resp = _run(_post(app, "/v1/chat/completions", json=body))
    assert resp.status_code == 200
    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": True}


def test_proxy_no_inject_for_other_groups_or_engines():
    """非 qwen3.8 家族、或引擎不支持模板 kwargs 时均不注入。"""
    captured = {}

    def upstream(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "1", "model": captured["body"]["model"]})

    # 其他家族 + vllm：不注入
    reg = {"ds": GatewayModel("ds-vllm", "vllm", "http://upstream", "ds-vllm", None, "http://upstream/", group="deepseek-v4-flash")}
    app = create_app(reg, default_model="ds", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "ds", "messages": [{"role": "user", "content": "hi"}]}))
    assert resp.status_code == 200
    assert "chat_template_kwargs" not in captured["body"]

    # qwen3.8 家族但引擎为 llama.cpp（不识别该字段）：不注入
    captured.clear()
    reg = {"q3": GatewayModel("q3-llamacpp", "llamacpp", "http://upstream", "q3-llamacpp", None, "http://upstream/", group="qwen3.8")}
    app = create_app(reg, default_model="q3", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "q3", "messages": [{"role": "user", "content": "hi"}]}))
    assert resp.status_code == 200
    assert "chat_template_kwargs" not in captured["body"]


def test_proxy_unknown_model_falls_back_to_default():
    def upstream(request):
        return httpx.Response(200, json={"id": "1", "model": json.loads(request.content)["model"]})

    reg = {"ds": GatewayModel("deepseek-v4-flash", "llamacpp", "http://upstream", "deepseek-v4-flash", None, "http://upstream/")}
    app = create_app(reg, default_model="ds", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "ghost-model", "messages": []}))
    assert resp.status_code == 200
    assert resp.json()["model"] == "deepseek-v4-flash"


def test_proxy_404_when_no_default_matches():
    reg = {"ds": GatewayModel("deepseek-v4-flash", "llamacpp", "http://upstream", "deepseek-v4-flash", None, "http://upstream/")}
    app = create_app(reg, default_model=None, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "ghost-model", "messages": []}))
    assert resp.status_code == 404


def test_anthropic_messages_passthrough():
    """Anthropic /v1/messages：按 body.model 路由、改写模型名、透传 x-api-key。"""
    captured = {}

    def upstream(request):
        captured["body"] = json.loads(request.content)
        captured["x-api-key"] = request.headers.get("x-api-key")
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
                "model": captured["body"]["model"],
            },
        )

    reg = {"qwen3.8": GatewayModel("qwen3.8-vllm", "vllm", "http://upstream", "qwen3.8-vllm", None, "http://upstream/", group="qwen3.8")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(upstream))
    body = {"model": "qwen3.8", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
    resp = _run(_post_headers(app, "/v1/messages", json=body, headers={"x-api-key": "root123456"}))
    assert resp.status_code == 200
    assert captured["body"]["model"] == "qwen3.8-vllm"  # 改写为后端期望名
    assert captured["x-api-key"] == "root123456"  # x-api-key 头透传
    assert resp.json()["content"][0]["text"] == "hi"


def test_anthropic_messages_uses_target_api_key():
    """网关以 profile 有效 key 认证（同时设置 x-api-key 与 Authorization Bearer）。"""
    captured = {}

    def upstream(request):
        captured["x-api-key"] = request.headers.get("x-api-key")
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "hi"}], "model": "qwen3.8-vllm"},
        )

    reg = {"qwen3.8": GatewayModel("qwen3.8-vllm", "vllm", "http://upstream", "qwen3.8-vllm", "fly@@see", "http://upstream/", group="qwen3.8")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(upstream))
    body = {"model": "qwen3.8", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
    resp = _run(_post_headers(app, "/v1/messages", json=body, headers={"x-api-key": "root123456"}))
    assert resp.status_code == 200
    assert captured["x-api-key"] == "fly@@see"  # 覆盖为 profile 有效 key
    assert captured["auth"] == "Bearer fly@@see"  # 同时设置 Authorization


def test_anthropic_messages_streaming_passthrough():
    """Anthropic 流式 SSE 必须保留 event: 行（与 OpenAI 的 data: 行解析不同）。"""
    def upstream(request):
        sse = (
            "event: message_start\n"
            "data: {\"type\":\"message_start\"}\n\n"
            "event: content_block_delta\n"
            "data: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"text_delta\",\"text\":\"hi\"}}\n\n"
            "event: message_stop\n"
            "data: {\"type\":\"message_stop\"}\n\n"
        )
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    reg = {"qwen3.8": GatewayModel("qwen3.8-vllm", "vllm", "http://upstream", "qwen3.8-vllm", None, "http://upstream/", group="qwen3.8")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(upstream))
    body = {"model": "qwen3.8", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    resp = _run(_post(app, "/v1/messages", json=body))
    assert resp.status_code == 200
    assert "event: message_start" in resp.text
    assert "event: content_block_delta" in resp.text
    assert "event: message_stop" in resp.text
    assert "text_delta" in resp.text


def test_anthropic_messages_404_unknown_model():
    reg = {"qwen3.8": GatewayModel("qwen3.8-vllm", "vllm", "http://upstream", "qwen3.8-vllm", None, "http://upstream/", group="qwen3.8")}
    app = create_app(reg, default_model=None, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    resp = _run(_post(app, "/v1/messages", json={"model": "ghost-model", "messages": []}))
    assert resp.status_code == 404


def test_reasoning_effort_normalized_openai():
    """OpenAI 端点：Claude Code 的 reasoning_effort=high 映射为 vLLM 支持的 xhigh。"""
    captured = {}

    def upstream(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "1", "model": captured["body"]["model"]})

    reg = {"qwen3.8": GatewayModel("qwen3.8-vllm", "vllm", "http://upstream", "qwen3.8-vllm", None, "http://upstream/", group="qwen3.8")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(upstream))
    body = {"model": "qwen3.8", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high"}
    resp = _run(_post(app, "/v1/chat/completions", json=body))
    assert resp.status_code == 200
    assert captured["body"]["reasoning_effort"] == "xhigh"  # high 映射为 xhigh


def test_reasoning_effort_normalized_anthropic():
    """Anthropic 端点：reasoning_effort=ultra 映射为 xhigh，已支持的枚举保持不变。"""
    captured = []

    def upstream(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "hi"}], "model": "qwen3.8-vllm"})

    reg = {"qwen3.8": GatewayModel("qwen3.8-vllm", "vllm", "http://upstream", "qwen3.8-vllm", None, "http://upstream/", group="qwen3.8")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(upstream))

    resp = _run(_post(app, "/v1/messages", json={"model": "qwen3.8", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "ultra"}))
    assert resp.status_code == 200
    assert captured[-1]["reasoning_effort"] == "xhigh"

    resp = _run(_post(app, "/v1/messages", json={"model": "qwen3.8", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "medium"}))
    assert resp.status_code == 200
    assert captured[-1]["reasoning_effort"] == "medium"  # 支持的值不改变


def test_thinking_effort_normalized_anthropic():
    """Claude Code 的 thinking.effort=high 嵌套字段映射为 xhigh（Qwen3.8 模板校验枚举）。"""
    captured = []

    def upstream(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "hi"}], "model": "qwen3.8-vllm"})

    reg = {"qwen3.8": GatewayModel("qwen3.8-vllm", "vllm", "http://upstream", "qwen3.8-vllm", None, "http://upstream/", group="qwen3.8")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(upstream))

    body = {
        "model": "qwen3.8",
        "messages": [{"role": "user", "content": "hi"}],
        "thinking": {"type": "enabled", "budget_tokens": 10000, "effort": "high"},
    }
    resp = _run(_post(app, "/v1/messages", json=body))
    assert resp.status_code == 200
    assert captured[-1]["thinking"]["effort"] == "xhigh"  # 嵌套 effort 已映射

    body["reasoning"] = {"effort": "ultra"}
    resp = _run(_post(app, "/v1/messages", json=body))
    assert resp.status_code == 200
    assert captured[-1]["reasoning"]["effort"] == "xhigh"

    # Claude Code 新版协议：output_config.effort=high（QwenLM/Qwen3.8#217 场景）
    body["output_config"] = {"effort": "high"}
    resp = _run(_post(app, "/v1/messages", json=body))
    assert resp.status_code == 200
    assert captured[-1]["output_config"]["effort"] == "xhigh"


def test_proxy_streaming_sse_passthrough():
    def upstream(request):
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b'data: {"a": 1}\n\ndata: [DONE]\n\n'),
            headers={"content-type": "text/event-stream"},
        )

    reg = {"ds": GatewayModel("deepseek-v4-flash", "llamacpp", "http://upstream", "deepseek-v4-flash", None, "http://upstream/")}
    app = create_app(reg, default_model="ds", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "ds", "stream": True, "messages": []}))
    assert resp.status_code == 200
    assert 'data: {"a": 1}' in resp.text
    assert "data: [DONE]" in resp.text


def test_proxy_502_on_upstream_unreachable():
    def upstream(request):
        raise httpx.ConnectError("connection refused")

    reg = {"ds": GatewayModel("deepseek-v4-flash", "llamacpp", "http://upstream", "deepseek-v4-flash", None, "http://upstream/")}
    app = create_app(reg, default_model="ds", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "ds", "messages": []}))
    assert resp.status_code == 502


def test_proxy_passthrough_upstream_status():
    def upstream(request):
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    reg = {"ds": GatewayModel("deepseek-v4-flash", "llamacpp", "http://upstream", "deepseek-v4-flash", None, "http://upstream/")}
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


def test_is_model_healthy_fails_fast_on_connection_error(monkeypatch):
    """回归：连接失败应立即返回 False（旧实现经 wait_health 的 sleep 重试会拖慢 /v1/models）。"""
    import urllib.error

    from modelctl.core.gateway import GatewayModel, is_model_healthy

    model = GatewayModel("x", "llamacpp", "http://127.0.0.1:1", "x", None, "http://127.0.0.1:1/health")

    def fake_urlopen(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert is_model_healthy(model, timeout=0.5) is False


def test_list_models_uses_alias_as_id():
    """/v1/models 的 id 应优先用 alias（cc-switch 可直接识别短名，而非带引擎后缀的 profile name）。"""
    reg = {
        "deepseek-v4-flash-llamacpp": GatewayModel(
            "deepseek-v4-flash-llamacpp",
            "llamacpp",
            "http://upstream",
            "deepseek-v4-flash-llamacpp",
            None,
            "http://upstream/",
            aliases=["deepseek-v4-flash"],
        )
    }
    app = create_app(reg, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with (
        patch("modelctl.core.gateway.is_model_healthy", return_value=True),
        patch("modelctl.core.gateway.is_running", return_value=True),
    ):
        resp = _run(_get(app, "/v1/models"))
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["data"]] == ["deepseek-v4-flash"]


def test_list_models_excludes_unmanaged_but_alive():
    """回归：modelctl 未管理（is_running False）但端口有响应（如遗留 ollama serve 占用 11434）
    的模型不得出现在 /v1/models——仅健康探测会将其误判为可用。"""
    reg = {
        "deepseek-v4-flash-ollama": GatewayModel(
            "deepseek-v4-flash-ollama", "ollama", "http://upstream", "x", None, "http://upstream/"
        )
    }
    app = create_app(reg, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with (
        patch("modelctl.core.gateway.is_model_healthy", return_value=True),
        patch("modelctl.core.gateway.is_running", return_value=False),
    ):
        resp = _run(_get(app, "/v1/models"))
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_list_models_dedups_alias_and_name():
    """回归：name 与 alias 指向同一对象时 /v1/models 不得重复列出。"""
    gm = GatewayModel("ds-llamacpp", "llamacpp", "http://upstream", "ds-llamacpp", None, "http://upstream/")
    reg = {"ds-llamacpp": gm, "ds": gm}
    app = create_app(reg, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with (
        patch("modelctl.core.gateway.is_model_healthy", return_value=True),
        patch("modelctl.core.gateway.is_running", return_value=True),
    ):
        resp = _run(_get(app, "/v1/models"))
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["data"]] == ["ds-llamacpp"]


def test_build_groups_sorted_by_engine_priority(tmp_path):
    (tmp_path / "a.yaml").write_text(
        "name: qwen3.8-llamacpp\ngroup: qwen3.8\nengine: llamacpp\nport: 18888\nllamacpp:\n  model: q\n",
        encoding="utf-8",
    )
    (tmp_path / "b.yaml").write_text(
        "name: qwen3.8-vllm\ngroup: qwen3.8\nengine: vllm\nport: 8101\nvllm:\n  model: q\n",
        encoding="utf-8",
    )
    (tmp_path / "c.yaml").write_text(
        "name: standalone\nengine: ollama\nport: 11434\nollama:\n  model: s\n",
        encoding="utf-8",
    )
    groups = build_groups(models_dir=tmp_path)
    # c.yaml 未声明 group → 自动从文件名推导为 "c"，故同样进入家族索引
    assert list(groups) == ["qwen3.8", "c"]
    assert [m.name for m in groups["qwen3.8"]] == ["qwen3.8-vllm", "qwen3.8-llamacpp"]  # vllm 优先
    assert [m.name for m in groups["c"]] == ["standalone"]


def test_resolve_model_group_prefers_running_member():
    reg = {}
    members = [
        GatewayModel("qwen3.8-vllm", "vllm", "http://127.0.0.1:2", "q", None, "http://127.0.0.1:2/"),
        GatewayModel("qwen3.8-llamacpp", "llamacpp", "http://127.0.0.1:1", "q", None, "http://127.0.0.1:1/"),
    ]
    groups = {"qwen3.8": members}
    with (
        patch("modelctl.core.gateway.is_running", side_effect=lambda n: n == "qwen3.8-llamacpp"),
        patch("modelctl.core.gateway.is_model_healthy", return_value=True),
    ):
        target = resolve_model(reg, "qwen3.8", None, groups=groups)
    assert target is members[1]  # vllm 不健康（未运行）→ 落到 llamacpp


def test_resolve_model_group_none_running_returns_none():
    reg = {"qwen3.8": GatewayModel("qwen3.8-llamacpp", "llamacpp", "http://127.0.0.1:1", "q", None, "http://127.0.0.1:1/")}
    groups = {"qwen3.8": [reg["qwen3.8"]]}
    with (
        patch("modelctl.core.gateway.is_running", return_value=False),
        patch("modelctl.core.gateway.is_model_healthy", return_value=True),
    ):
        # 裸名 qwen3.8 同时是 llamacpp alias 与 group 名：group 优先，无健康成员 → None（不回退 alias）
        assert resolve_model(reg, "qwen3.8", None, groups=groups) is None


def test_resolve_model_group_as_default():
    reg = {}
    members = [GatewayModel("qwen3.8-vllm", "vllm", "http://127.0.0.1:2", "q", None, "http://127.0.0.1:2/")]
    groups = {"qwen3.8": members}
    with (
        patch("modelctl.core.gateway.is_running", return_value=True),
        patch("modelctl.core.gateway.is_model_healthy", return_value=True),
    ):
        target = resolve_model(reg, "ghost", "qwen3.8", groups=groups)
    assert target is members[0]


def test_list_models_shows_group_when_member_healthy():
    members = [GatewayModel("qwen3.8-vllm", "vllm", "http://upstream", "q", None, "http://upstream/")]
    app = create_app({}, groups={"qwen3.8": members}, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with (
        patch("modelctl.core.gateway.is_model_healthy", return_value=True),
        patch("modelctl.core.gateway.is_running", return_value=True),
    ):
        resp = _run(_get(app, "/v1/models"))
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["data"]] == ["qwen3.8"]


def test_list_models_hides_group_when_no_member_healthy():
    members = [GatewayModel("qwen3.8-vllm", "vllm", "http://upstream", "q", None, "http://upstream/")]
    app = create_app({}, groups={"qwen3.8": members}, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with (
        patch("modelctl.core.gateway.is_model_healthy", return_value=False),
        patch("modelctl.core.gateway.is_running", return_value=False),
    ):
        resp = _run(_get(app, "/v1/models"))
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_proxy_group_routes_to_running_member():
    captured = {}

    def upstream(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "1", "model": captured["body"]["model"]})

    members = [
        GatewayModel("qwen3.8-llamacpp", "llamacpp", "http://upstream", "q3-llama", None, "http://upstream/"),
        GatewayModel("qwen3.8-vllm", "vllm", "http://upstream", "q3-vllm", None, "http://upstream/"),
    ]
    groups = {"qwen3.8": members}
    app = create_app({}, groups=groups, transport=httpx.MockTransport(upstream))
    with (
        patch("modelctl.core.gateway.is_running", side_effect=lambda n: n == "qwen3.8-vllm"),
        patch("modelctl.core.gateway.is_model_healthy", return_value=True),
    ):
        resp = _run(_post(app, "/v1/chat/completions", json={"model": "qwen3.8", "messages": []}))
    assert resp.status_code == 200
    assert captured["body"]["model"] == "q3-vllm"  # 已改写为运行成员的上游模型名


def test_create_app_auto_builds_registry_and_groups():
    """/v1/models 无注入时自动构建 registry 与 groups（生产路径 main() 走这里）。"""
    gm = GatewayModel("qwen3.8-vllm", "vllm", "http://upstream", "q", None, "http://upstream/")
    with (
        patch("modelctl.core.gateway.build_registry", return_value={"qwen3.8-vllm": gm}),
        patch("modelctl.core.gateway.build_groups", return_value={"qwen3.8": [gm]}),
    ):
        app = create_app()  # registry=None, groups=None
    with (
        patch("modelctl.core.gateway.is_model_healthy", return_value=True),
        patch("modelctl.core.gateway.is_running", return_value=True),
    ):
        resp = _run(_get(app, "/v1/models"))
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["data"]] == ["qwen3.8-vllm", "qwen3.8"]


# ---- 网关真实用量统计（vLLM token 计数 gauge 恒 0 的修复）----

class _FakeCollector:
    """记录 record_tokens 调用的假收集器。"""

    def __init__(self):
        self.calls: list[tuple[int, int]] = []

    def record_tokens(self, prompt_delta, completion_delta):
        self.calls.append((prompt_delta, completion_delta))


def _make_model(name="ds", collector=None) -> GatewayModel:
    return GatewayModel(
        name, "vllm", "http://upstream", name, None, "http://upstream/", collector=collector
    )


def test_proxy_non_streaming_records_usage(monkeypatch):
    """非流式：网关应把后端 usage 计入收集器（此前 vLLM 无 metrics 时恒为 0）。"""
    collector = _FakeCollector()

    def upstream(request):
        return httpx.Response(
            200,
            json={
                "id": "1",
                "object": "chat.completion",
                "model": "ds",
                "usage": {"prompt_tokens": 12, "completion_tokens": 34},
            },
        )

    reg = {"ds": _make_model(collector=collector)}
    app = create_app(reg, default_model="ds", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "ds", "messages": []}))
    assert resp.status_code == 200
    assert collector.calls == [(12, 34)]


def test_proxy_non_streaming_no_usage_no_record():
    collector = _FakeCollector()

    def upstream(request):
        return httpx.Response(200, json={"id": "1", "object": "chat.completion", "model": "ds"})

    reg = {"ds": _make_model(collector=collector)}
    app = create_app(reg, default_model="ds", transport=httpx.MockTransport(upstream))
    _run(_post(app, "/v1/chat/completions", json={"model": "ds", "messages": []}))
    assert collector.calls == []  # 后端未回 usage → 不累计


def test_proxy_streaming_records_usage_increments():
    """流式：跨多个 data: 块逐次记录 token 增量（重复块不得重复累计）。"""
    collector = _FakeCollector()

    def upstream(request):
        sse = (
            b'data: {"id":"1","model":"ds","usage":{"prompt_tokens":5,"completion_tokens":3}}\n\n'
            b'data: {"id":"1","model":"ds","usage":{"prompt_tokens":5,"completion_tokens":7}}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, stream=httpx.ByteStream(sse), headers={"content-type": "text/event-stream"})

    reg = {"ds": _make_model(collector=collector)}
    app = create_app(reg, default_model="ds", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "ds", "stream": True, "messages": []}))
    assert resp.status_code == 200
    assert 'data: {"id":"1"' in resp.text
    # 增量：首块 (5,3)，第二块仅新增 completion 4
    assert collector.calls == [(5, 3), (0, 4)]


def test_proxy_streaming_chunk_split_usage_line():
    """回归：usage 的 data: 行被 HTTP chunk 从中间切开时，网关须跨 chunk 拼回整行再统计。"""
    collector = _FakeCollector()
    full = b'data: {"id":"1","model":"ds","usage":{"prompt_tokens":8,"completion_tokens":2}}\n\n'
    half = len(full) // 2

    def upstream(request):
        return httpx.Response(
            200,
            stream=httpx.ByteStream([full[:half], full[half:]]),
            headers={"content-type": "text/event-stream"},
        )

    reg = {"ds": _make_model(collector=collector)}
    app = create_app(reg, default_model="ds", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "ds", "stream": True, "messages": []}))
    assert resp.status_code == 200
    assert collector.calls == [(8, 2)]
