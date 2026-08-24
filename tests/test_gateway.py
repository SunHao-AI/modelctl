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
