# nginx 多模型路由与统一网关 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 nginx 单模型硬编码路由升级为多模型路由（路径式直连 + 按 model 参数路由的统一网关），模型注册表单一来源为 `models/*.yaml`。

**Architecture:** 新增两个组件：① `modelctl nginx-snippet` 从 profiles 生成 nginx `map $uri $llm_model_target` 注册表片段（B 机 include）；② FastAPI 轻量网关 `modelctl gateway`（与 modelctl 同节点，读本节点 profiles，按请求体 `model` 字段路由到本节点各引擎端口，流式 SSE 透传，未知/缺省 model 回退默认模型）。nginx 层保持四条固定 location（顺序即优先级：旧用量 > 按模型用量 > 网关 > 模型直连），仅 map 注册表随模型增删变化。

**Tech Stack:** Python 3.12、FastAPI + uvicorn + httpx（optional extra `gateway`）、PyYAML、pytest（httpx ASGITransport + MockTransport 测试）。

## Global Constraints

- `requires-python = ">=3.12"`；基础运行期依赖仅 `PyYAML>=6.0` + `loguru>=0.7`；网关依赖仅存在于 optional extra `gateway`（`fastapi>=0.110`、`uvicorn>=0.29`、`httpx>=0.27`），未安装时不得破坏 `modelctl list/start/status` 等既有命令（gateway.py 顶部不得 import fastapi/httpx/uvicorn，必须函数内延迟导入）。
- 测试必须可在 Windows 开发机上运行；新增 pytest 用例全部通过：`python -m pytest tests/ -v`。
- 代码注释用中文。
- 模型注册表单一来源 = `models/*.yaml`；profile 的 `name` 全局唯一（既有约束）。
- nginx 路由顺序固定为：旧用量 > 按模型用量 > 网关 > 模型直连；**`v1` 必须优先于模型名匹配**（否则 `/llm/v1/...` 会被误判为模型名 `v1`）。
- 网关与 modelctl 同节点部署；`GATEWAY_PORT` 默认 `5003`；后端地址固定 `http://127.0.0.1:<profile.port>`。
- 未知/缺省 body.model → 回退 `GATEWAY_DEFAULT_MODEL`（默认 `deepseek-v4-flash`），保持旧卡片行为。
- 向后兼容：`/<node>/llm/v1/...` 与 `/<node>/llm/v1/api/usage` 的行为与现状完全一致。
- 网关依赖 fastapi/httpx/uvicorn 的导入必须延迟到 `create_app()` / `main()` 内部。

---

### Task 1: 引擎适配器新增 `upstream_model_name()` 接口

**Files:**
- Modify: `src/modelctl/engines/base.py`
- Modify: `src/modelctl/engines/ollama.py`
- Modify: `src/modelctl/engines/vllm.py`
- Modify: `src/modelctl/engines/sglang.py`
- Modify: `src/modelctl/engines/unsloth.py`
- Test: `tests/test_engines_upstream.py`（新建）

**Interfaces:**
- Produces: `EngineAdapter.upstream_model_name(self) -> str` —— 后端 API 期望的模型名（请求体 `model` 字段改写目标）。llamacpp 走基类默认（返回 `profile.name`，llama-server 忽略 model 名无需改写）；ollama 必须返回 `engine_config["model"]`（如 `qwen3.8:27b`）；vllm/sglang/unsloth 返回 `engine_config.get("model") or profile.name`。

- [ ] **Step 1: 写失败测试 `tests/test_engines_upstream.py`**

```python
"""引擎适配器 upstream_model_name() 单元测试（网关模型名改写依据）。"""

from __future__ import annotations

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import Profile
from modelctl.engines import get_adapter


def _profile(name: str, engine: str, port: int, engine_config: dict | None = None) -> Profile:
    return Profile(name=name, engine=engine, port=port, engine_config=engine_config or {})


def test_upstream_model_ollama():
    adapter = get_adapter("ollama")(_profile("qwen3.8", "ollama", 11434, {"model": "qwen3.8:27b"}), Capabilities())
    assert adapter.upstream_model_name() == "qwen3.8:27b"


def test_upstream_model_llamacpp_uses_profile_name():
    adapter = get_adapter("llamacpp")(_profile("deepseek-v4-flash", "llamacpp", 18888), Capabilities())
    assert adapter.upstream_model_name() == "deepseek-v4-flash"


def test_upstream_model_vllm_config_or_name():
    vllm = get_adapter("vllm")(_profile("qwen3.8", "vllm", 8000, {"model": "Qwen/Qwen3.8-27B"}), Capabilities())
    assert vllm.upstream_model_name() == "Qwen/Qwen3.8-27B"
    vllm_empty = get_adapter("vllm")(_profile("qwen3.8", "vllm", 8000), Capabilities())
    assert vllm_empty.upstream_model_name() == "qwen3.8"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_engines_upstream.py -v`
Expected: FAIL，`AttributeError: 'LlamacppAdapter' object has no attribute 'upstream_model_name'`

- [ ] **Step 3: 实现接口**

`src/modelctl/engines/base.py` 的 `EngineAdapter` 类中（`stop_patterns` 方法附近）追加：

```python
    def upstream_model_name(self) -> str:
        """后端 API 期望的模型名（网关改写请求体 model 字段的目标）。

        默认 = profile.name（llama-server 等忽略 model 名，无需改写）。
        """
        return self.profile.name
```

`src/modelctl/engines/ollama.py` 追加（`metrics_mapping` 之后）：

```python
    def upstream_model_name(self) -> str:
        # ollama 严格校验 body.model，必须改写为 ollama.model（如 qwen3.8:27b）
        return str(self.profile.engine_config["model"])
```

`src/modelctl/engines/vllm.py`、`sglang.py`、`unsloth.py` 各追加：

```python
    def upstream_model_name(self) -> str:
        return str(self.profile.engine_config.get("model") or self.profile.name)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_engines_upstream.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/engines/base.py src/modelctl/engines/ollama.py src/modelctl/engines/vllm.py src/modelctl/engines/sglang.py src/modelctl/engines/unsloth.py tests/test_engines_upstream.py
git commit -m "feat: 引擎适配器新增 upstream_model_name() 接口"
```

---

### Task 2: 网关注册表构建与 nginx map 生成器（纯逻辑）

**Files:**
- Create: `src/modelctl/core/gateway.py`
- Create: `src/modelctl/core/nginx_snippet.py`
- Test: `tests/test_gateway.py`（追加）、`tests/test_nginx_snippet.py`（新建）

**Interfaces:**
- Produces:
  - `GATEWAY_PORT: int = 5003`
  - `@dataclass GatewayModel`: `name: str`、`engine: str`、`backend_url: str`、`upstream_model: str`、`api_key: str | None`、`health_url: str`
  - `build_registry(models_dir: Path | None = None, host: str = "127.0.0.1") -> dict[str, GatewayModel]`
  - `resolve_model(registry: dict[str, GatewayModel], body_model: str | None, default_model: str | None) -> GatewayModel | None`
  - `build_llm_map(profiles: list[Profile], node_id: str, host: str) -> str`（`modelctl.core.nginx_snippet`）

**依赖说明：** `gateway.py` 顶部只允许 import 标准库与 `modelctl.core.{envfile,process,profile}`、`modelctl.engines`、`modelctl.core.capabilities`；**禁止**在模块顶部 import fastapi/httpx/uvicorn（Task 3 在函数内延迟导入）。

- [ ] **Step 1: 写失败测试**

`tests/test_gateway.py` 追加：

```python
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
    assert resolve_model(reg, None, "b") is reg["b"]       # 缺省 → 默认
    assert resolve_model(reg, "unknown", None) is None
```

`tests/test_nginx_snippet.py`（新建）：

```python
"""modelctl.core.nginx_snippet 单元测试。"""

from __future__ import annotations

import pytest

from modelctl.core.nginx_snippet import build_llm_map
from modelctl.core.profile import Profile, ProfileError


def test_build_llm_map():
    profiles = [
        Profile(name="deepseek-v4-flash", engine="llamacpp", port=18888),
        Profile(name="qwen3.8", engine="ollama", port=11434),
    ]
    out = build_llm_map(profiles, "210", "192.168.77.210")
    lines = out.splitlines()
    assert lines[0] == "map $uri $llm_model_target {"
    assert '    default "";' in lines
    assert "~^/210/llm/deepseek-v4-flash/  http://192.168.77.210:18888;" in lines
    assert "~^/210/llm/qwen3.8/  http://192.168.77.210:11434;" in lines
    assert lines[-1] == "}"


def test_build_llm_map_rejects_unsafe_name():
    with pytest.raises(ProfileError):
        build_llm_map([Profile(name="a b", engine="vllm", port=8000)], "210", "x")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_gateway.py tests/test_nginx_snippet.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'modelctl.core.gateway'`

- [ ] **Step 3: 实现 `src/modelctl/core/gateway.py`（纯逻辑部分）**

```python
#!/usr/bin/env python3
"""core/gateway.py — 轻量 OpenAI 兼容网关（按请求体 model 参数路由）。

将本节点 models/*.yaml 中的模型注册为 OpenAI 兼容后端，按请求体中的
model 字段路由到对应引擎端口；未知/缺省 model 回退默认模型。
依赖 fastapi / uvicorn / httpx（可选 extra "gateway"），本模块顶部不导入。
独立运行：
    python -m modelctl.core.gateway
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from modelctl.core.capabilities import Capabilities
from modelctl.core.envfile import load_env
from modelctl.core.process import wait_health
from modelctl.core.profile import list_profiles
from modelctl.engines import get_adapter

GATEWAY_PORT = 5003


@dataclass
class GatewayModel:
    name: str
    engine: str
    backend_url: str
    upstream_model: str
    api_key: str | None
    health_url: str


def build_registry(models_dir: Path | None = None, host: str = "127.0.0.1") -> dict[str, GatewayModel]:
    """从 models/*.yaml 构建 模型名 -> 后端信息 注册表（单一来源）。"""
    registry: dict[str, GatewayModel] = {}
    for profile in list_profiles(models_dir):
        adapter = get_adapter(profile.engine)(profile, Capabilities())
        registry[profile.name] = GatewayModel(
            name=profile.name,
            engine=profile.engine,
            backend_url=f"http://{host}:{profile.port}",
            upstream_model=adapter.upstream_model_name(),
            api_key=profile.api_key,
            health_url=adapter.health_url(),
        )
    return registry


def resolve_model(
    registry: dict[str, GatewayModel], body_model: str | None, default_model: str | None
) -> GatewayModel | None:
    """按 body.model 解析目标模型；未知或缺省均回退 default_model；都不可用时返回 None。"""
    if body_model and body_model in registry:
        return registry[body_model]
    if default_model and default_model in registry:
        return registry[default_model]
    return None


def is_model_healthy(model: GatewayModel, timeout: float = 2.0) -> bool:
    """后端存活探测（复用 process.wait_health，失败不抛异常）。"""
    return wait_health(model.health_url, timeout, model.api_key)
```

- [ ] **Step 4: 实现 `src/modelctl/core/nginx_snippet.py`**

```python
#!/usr/bin/env python3
"""core/nginx_snippet.py — 从 models/*.yaml 生成 nginx 多模型路由 map 片段。"""

from __future__ import annotations

import re

from modelctl.core.profile import Profile, ProfileError

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def build_llm_map(profiles: list[Profile], node_id: str, host: str) -> str:
    """生成 `map $uri $llm_model_target` 片段，供 B 机 nginx include。

    node_id 为 URL 数字前缀（如 210），host 为节点 IP（如 192.168.77.210）。
    模型名必须是 nginx 正则安全的标识符（字母数字、点、连字符、下划线）。
    """
    for p in profiles:
        if not _SAFE_NAME_RE.match(p.name):
            raise ProfileError(f"模型名 {p.name} 含 nginx 正则不安全字符（仅允许 [A-Za-z0-9._-]）")
    lines = ["map $uri $llm_model_target {", '    default "";']
    for p in sorted(profiles, key=lambda x: x.name):
        lines.append(f"    ~^/{node_id}/llm/{p.name}/  http://{host}:{p.port};")
    lines.append("}")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_gateway.py tests/test_nginx_snippet.py -v`
Expected: PASS（Task 1 的 3 个 + 本任务 4 个 = 7 passed）

- [ ] **Step 6: Commit**

```bash
git add src/modelctl/core/gateway.py src/modelctl/core/nginx_snippet.py tests/test_gateway.py tests/test_nginx_snippet.py
git commit -m "feat: 网关注册表构建与 nginx map 片段生成器"
```

---

### Task 3: FastAPI 网关应用（转发、流式、错误处理）

**Files:**
- Modify: `src/modelctl/core/gateway.py`（追加 `create_app` 与转发端点）
- Test: `tests/test_gateway.py`（追加）

**Interfaces:**
- Produces: `create_app(registry: dict[str, GatewayModel] | None = None, default_model: str | None = None, read_timeout: float = 600.0, transport=None) -> FastAPI`
  - 端点：`GET /v1/models`（健康过滤）；`POST /v1/{path:path}`（`chat/completions` / `completions` / `embeddings`，按 model 路由转发，`stream: true` 时 SSE 流式透传）
  - `transport` 仅供测试注入 `httpx.MockTransport`；默认 `None` 走真实网络
- Consumes: Task 2 的 `GatewayModel` / `build_registry` / `resolve_model` / `is_model_healthy`

**依赖说明：** fastapi / httpx 必须在 `create_app()` 函数体内 `import`（顶部不得导入）。

- [ ] **Step 1: 写失败测试**

`tests/test_gateway.py` 追加：

```python
import asyncio
import json
from unittest.mock import patch

import httpx


def _run(coro):
    return asyncio.run(coro)


def test_list_models_health_filtered():
    reg = {"qwen3.8": GatewayModel("qwen3.8", "ollama", "http://upstream", "qwen3.8:27b", None, "http://upstream/")}
    app = create_app(reg, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with patch("modelctl.core.gateway.is_model_healthy", return_value=True):
        resp = _run(_post(app, "/v1/models"))
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["data"]] == ["qwen3.8"]


def test_proxy_rewrites_model_to_upstream():
    captured = {}

    def upstream(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "1", "object": "chat.completion", "model": captured["body"]["model"]})

    reg = {"qwen3.8": GatewayModel("qwen3.8", "ollama", "http://upstream", "qwen3.8:27b", None, "http://upstream/")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": "qwen3.8", "messages": [{"role": "user", "content": "hi"}]}))
    assert resp.status_code == 200
    assert captured["body"]["model"] == "qwen3.8:27b"   # 已改写为 ollama 期望名
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
```

`_post` 辅助（同样追加在 `tests/test_gateway.py`）：

```python
async def _post(app, path: str, json: dict | None = None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=json or {})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_gateway.py -v`
Expected: FAIL，`ImportError: cannot import name 'create_app' from 'modelctl.core.gateway'`

- [ ] **Step 3: 实现 `create_app`（追加到 `src/modelctl/core/gateway.py` 末尾）**

```python
def create_app(
    registry: dict[str, GatewayModel] | None = None,
    default_model: str | None = None,
    read_timeout: float = 600.0,
    transport=None,
):
    """构建 FastAPI 网关应用（transport 供测试注入 httpx.MockTransport）。

    环境变量：GATEWAY_DEFAULT_MODEL（默认模型，缺省/未知 model 回退目标）。
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse
    import httpx

    registry = registry if registry is not None else build_registry()
    default_model = default_model or os.environ.get("GATEWAY_DEFAULT_MODEL")
    app = FastAPI(title="modelctl gateway", docs_url="/docs", openapi_url="/openapi.json")

    @app.get("/v1/models")
    async def list_models() -> dict:
        data = [
            {"id": m.name, "object": "model", "created": 0, "owned_by": "modelctl"}
            for m in registry.values()
            if is_model_healthy(m)
        ]
        return {"object": "list", "data": data}

    @app.post("/v1/{path:path}")
    async def proxy(path: str, request: Request):
        if path not in ("chat/completions", "completions", "embeddings"):
            return JSONResponse(status_code=404, content={"error": {"message": f"unknown endpoint: /v1/{path}", "type": "invalid_request_error"}})
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(status_code=400, content={"error": {"message": "请求体必须是 JSON", "type": "invalid_request_error"}})
        target = resolve_model(registry, body.get("model"), default_model)
        if target is None:
            return JSONResponse(status_code=404, content={"error": {"message": f"model not found: {body.get('model')}", "type": "invalid_request_error"}})
        # 改写为后端期望的模型名（ollama 严格校验，llamacpp 忽略）
        body["model"] = target.upstream_model
        headers = {"Content-Type": "application/json"}
        auth = request.headers.get("Authorization") or (f"Bearer {target.api_key}" if target.api_key else None)
        if auth:
            headers["Authorization"] = auth
        url = f"{target.backend_url}/v1/{path}"
        async with httpx.AsyncClient(timeout=read_timeout, transport=transport) as client:
            try:
                if body.get("stream"):
                    req = client.build_request("POST", url, json=body, headers=headers)
                    upstream = await client.send(req, stream=True)  # stream=True：连接保持打开，逐块读 SSE
                    if upstream.status_code >= 400:
                        content = await upstream.aread()
                        return Response(status_code=upstream.status_code, content=content, media_type=upstream.headers.get("content-type"))
                    return StreamingResponse(
                        _iter_stream(upstream),
                        status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type"),
                    )
                upstream = await client.post(url, json=body, headers=headers)
                return Response(status_code=upstream.status_code, content=upstream.content, media_type=upstream.headers.get("content-type"))
            except httpx.HTTPError as error:
                return JSONResponse(status_code=502, content={"error": {"message": f"后端不可达：{error}", "type": "upstream_error"}})

    return app


async def _iter_stream(upstream):
    """逐块透传后端响应体（SSE 流式）。"""
    async for chunk in upstream.aiter_bytes():
        yield chunk
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_gateway.py -v`
Expected: PASS（Task 1/2 的 7 个 + 本任务 7 个 = 14 passed）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/gateway.py tests/test_gateway.py
git commit -m "feat: FastAPI 网关 create_app（model 路由转发、SSE 流式、错误处理）"
```

---

### Task 4: 网关独立入口与 CLI 子命令（gateway / nginx-snippet）

**Files:**
- Modify: `src/modelctl/core/gateway.py`（追加 `main()`）
- Modify: `src/modelctl/cli.py`
- Test: `tests/test_modelctl.py`（追加）

**Interfaces:**
- Consumes: `modelctl.core.gateway.GATEWAY_PORT`、`modelctl.core.nginx_snippet.build_llm_map`、`modelctl.core.process.start_detached/stop_instance/is_running/wait_health`
- Produces: CLI `modelctl gateway start|stop|status`、`modelctl nginx-snippet --node <id> --host <ip>`；独立入口 `python -m modelctl.core.gateway`（uvicorn 监听 `GATEWAY_HOST:GATEWAY_PORT`，延迟导入 uvicorn）

- [ ] **Step 1: 写失败测试**

`tests/test_modelctl.py` 追加：

```python
def test_nginx_snippet_output(tmp_path, capsys):
    (tmp_path / "qwen.yaml").write_text("name: qwen3.8\nengine: ollama\nport: 11434\n", encoding="utf-8")
    rc = cli.main(["nginx-snippet", "--node", "210", "--host", "192.168.77.210", "--models-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "~^/210/llm/qwen3.8/  http://192.168.77.210:11434;" in out


def test_gateway_start_detaches(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    called: dict = {}
    monkeypatch.setattr(cli, "start_detached", lambda name, cmd, extra_env: called.update(name=name, cmd=cmd) or 123)
    monkeypatch.setattr(cli, "is_running", lambda name: False)
    rc = cli.main(["gateway", "start"])
    assert rc == 0
    assert called["name"] == "llm-gateway"
    assert called["cmd"][-1].endswith("modelctl.core.gateway")


def test_gateway_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    called: dict = {}
    monkeypatch.setattr(cli, "stop_instance", lambda name, port, patterns: called.update(name=name, port=port))
    rc = cli.main(["gateway", "stop"])
    assert rc == 0
    assert called["name"] == "llm-gateway"
    assert called["port"] == 5003
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_modelctl.py -v`
Expected: FAIL，`error: invalid choice: 'gateway'`（argparse 未注册 gateway / nginx-snippet 子命令）

- [ ] **Step 3: 实现 `main()`（追加到 `src/modelctl/core/gateway.py` 末尾）**

```python
def main() -> None:
    """独立运行入口：python -m modelctl.core.gateway。"""
    load_env()
    host = os.environ.get("GATEWAY_HOST", "0.0.0.0")
    port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
    read_timeout = float(os.environ.get("GATEWAY_READ_TIMEOUT", "600"))
    default_model = os.environ.get("GATEWAY_DEFAULT_MODEL")
    import uvicorn

    app = create_app(default_model=default_model, read_timeout=read_timeout)
    print(f"modelctl 网关运行于 http://{host}:{port}/v1（默认模型：{default_model or '未配置'}）", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 实现 CLI（`src/modelctl/cli.py`）**

顶部 import 追加：

```python
from modelctl.core.gateway import GATEWAY_PORT
from modelctl.core.nginx_snippet import build_llm_map
```

`build_parser()` 中 `sp = sub.add_parser("stats", ...)` 之后追加：

```python
    gp = sub.add_parser("gateway", help="统一网关（model 参数路由）控制")
    gp.add_argument("action", choices=["start", "stop", "status"])
    ns = sub.add_parser("nginx-snippet", help="生成 nginx 多模型路由 map 片段")
    ns.add_argument("--node", required=True, help="节点编号（URL 前缀，如 210）")
    ns.add_argument("--host", required=True, help="节点 IP（如 192.168.77.210）")
```

`_cmd_stats_stop()` 之后追加三个网关命令函数：

```python
def _cmd_gateway_start() -> int:
    if is_running("llm-gateway"):
        logger.info("网关已在运行")
        return 0
    script_dir = str(Path(__file__).resolve().parents[1])
    extra_env = {"PYTHONPATH": script_dir + os.pathsep + os.environ.get("PYTHONPATH", "")}
    pid = start_detached("llm-gateway", [sys.executable, "-m", "modelctl.core.gateway"], extra_env)
    port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
    logger.info(f"网关已启动（PID {pid}），监听端口 {port}")
    return 0


def _cmd_gateway_stop() -> int:
    port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
    stop_instance("llm-gateway", port, ["modelctl.core.gateway"])
    logger.info("网关已停止")
    return 0


def _cmd_gateway_status() -> int:
    if is_running("llm-gateway"):
        port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
        ok = wait_health(f"http://127.0.0.1:{port}/v1/models", 3.0)
        print("网关：运行中，/v1/models " + ("正常" if ok else "无响应"))
        return 0
    print("网关：已停止")
    return 0


def _cmd_nginx_snippet(args, models_dir) -> int:
    print(build_llm_map(list_profiles(models_dir), args.node, args.host), end="")
    return 0
```

`main()` 的分发分支（`if args.command == "stats":` 之后追加）：

```python
        if args.command == "gateway":
            if args.action == "start":
                return _cmd_gateway_start()
            if args.action == "stop":
                return _cmd_gateway_stop()
            return _cmd_gateway_status()
        if args.command == "nginx-snippet":
            return _cmd_nginx_snippet(args, models_dir)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_modelctl.py tests/test_gateway.py tests/test_nginx_snippet.py -v`
Expected: PASS（全部通过）

- [ ] **Step 6: Commit**

```bash
git add src/modelctl/core/gateway.py src/modelctl/cli.py tests/test_modelctl.py
git commit -m "feat: modelctl gateway / nginx-snippet 子命令与独立运行入口"
```

---

### Task 5: 依赖、环境变量、nginx 示例与文档

**Files:**
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Create: `docs/nginx/llm-routing.example.conf`
- Modify: `README.md`

**Interfaces:**
- Produces: optional extra `gateway`；`.env` 新增 `NODE_ID/NODE_HOST/GATEWAY_HOST/GATEWAY_PORT/GATEWAY_DEFAULT_MODEL/GATEWAY_READ_TIMEOUT`；B 机 nginx 可落地的完整示例配置

- [ ] **Step 1: 修改 `pyproject.toml` 增加可选依赖组**

`[project.optional-dependencies]` 中 `modelscope` 行之后追加：

```toml
gateway = [
    "fastapi>=0.110",
    "uvicorn>=0.29",
    "httpx>=0.27",
]
```

- [ ] **Step 2: 修改 `.env.example`，在用量统计段之后追加**

```bash
# ---------- 统一网关（model 参数路由）----------
# 本节点编号（nginx URL 前缀）与 IP（nginx-snippet 生成 map 用）
NODE_ID=210
NODE_HOST=192.168.77.210
# 网关监听地址与端口（B 机 nginx 统一转发 /<node>/llm/v1/ 到 5003）
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=5003
# 未知/缺省 model 的回退模型（保持旧 cc-switch 卡片行为）
GATEWAY_DEFAULT_MODEL=deepseek-v4-flash
# 后端转发读超时（秒，与 nginx proxy_read_timeout 对齐）
GATEWAY_READ_TIMEOUT=600
```

- [ ] **Step 3: 创建 `docs/nginx/llm-routing.example.conf`（B 机 nginx 参考配置）**

```nginx
# ================= LLM 多模型路由（B 机 nginx 参考配置） =================
# 使用步骤：
#   1. C 机（模型节点）执行：modelctl nginx-snippet --node 210 --host 192.168.77.210
#      输出 llm-routes-210.conf，上传到 B 机 /etc/nginx/llm-routes/ 目录
#   2. 主配置 nginx.conf 的 http 块加入：include /etc/nginx/llm-routes/*.conf;
#   3. 将下方 locations 并入监听 5000 的 server 块，替换原 ^/210/llm/ 相关规则
#   4. nginx -t && systemctl reload nginx
#
# 说明：map 注册表由 nginx-snippet 生成，勿手写；新增模型 = 重新生成该文件。

# ---------- http 块（由 nginx-snippet 生成，这里仅示意） ----------
# map $uri $llm_model_target {
#     default "";
#     ~^/210/llm/deepseek-v4-flash/  http://192.168.77.210:18888;
#     ~^/210/llm/qwen3.8/            http://192.168.77.210:8000;
# }

server {
    # ================= LLM 多模型路由 =================
    # 顺序即优先级：旧用量 > 按模型用量 > 网关 > 模型直连
    #（v1 必须优先于模型名匹配，否则 /llm/v1/... 会被误判为模型名）

    # 1) 旧用量统计（兼容旧 cc-switch 卡片）
    location ~ ^/(\d+)/llm/v1/api/usage(.*)$ {
        proxy_pass http://192.168.77.$1:5002/api/usage$2;
    }

    # 2) 按模型用量统计（stats 服务已支持 ?model= 路由）
    location ~ ^/(?<node_id>\d+)/llm/(?<model_name>[^/]+)/v1/api/usage$ {
        proxy_pass http://192.168.77.$node_id:5002/api/usage?model=$model_name;
    }

    # 3) 网关（model 参数场景 + 旧地址兼容），关闭缓冲保证 SSE 流式
    location ~ ^/(?<node_id>\d+)/llm/v1/(?<llm_rest>.*)$ {
        proxy_pass http://192.168.77.$node_id:5003/$llm_rest;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header X-Accel-Buffering no;
    }

    # 4) 模型直连（map 注册表来自 nginx-snippet；未知模型 404）
    location ~ ^/(?<node_id>\d+)/llm/(?<model_name>[^/]+)/(?<llm_rest>.*)$ {
        if ($llm_model_target = "") {
            return 404;
        }
        rewrite ^/\d+/llm/[^/]+/(.*)$ /$1 break;
        proxy_pass $llm_model_target;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
    }
}
```

- [ ] **Step 4: 修改 `README.md`，在「用量统计服务」小节之后追加「多模型路由与统一网关」**

```markdown
### 7. 多模型路由与统一网关

B 机 nginx 通过 URL 路径把请求路由到不同模型；同时提供按 `model` 参数的统一网关。

**访问地址**

| 方式 | baseUrl / URL | 说明 |
|---|---|---|
| 路径式直连 | `https://xxx:5000/210/llm/deepseek-v4-flash/v1` | cc-switch 每模型一张卡片 |
| 路径式直连 | `https://xxx:5000/210/llm/qwen3.8/v1` | 同上 |
| 统一网关 | `https://xxx:5000/210/llm/v1` | body 里 `model=模型名` 切换；缺省/未知回退默认模型 |
| 用量查询 | `https://xxx:5000/210/llm/<模型名>/v1/api/usage` | cc-switch 用量卡片 |

**生成 nginx 注册表**

```bash
modelctl nginx-snippet --node 210 --host 192.168.77.210
```

输出 `map $uri $llm_model_target` 片段，上传到 B 机 `/etc/nginx/llm-routes/` 并 include（完整示例见 `docs/nginx/llm-routing.example.conf`）。新增模型只需新增一条 profile，重新生成即可。

**启动/停止网关**

```bash
bash script/modelctl.sh gateway start    # 或 modelctl gateway start
modelctl gateway status
modelctl gateway stop
```

网关依赖 `fastapi/uvicorn/httpx`（optional extra）：

```bash
uv sync --extra dev --extra gateway
```

`.env` 中新增 `NODE_ID`、`NODE_HOST`、`GATEWAY_HOST`、`GATEWAY_PORT`、`GATEWAY_DEFAULT_MODEL`、`GATEWAY_READ_TIMEOUT`（见 `.env.example`）。
```

- [ ] **Step 5: 验证依赖组与既有命令不受影响**

Run:
```bash
uv sync --extra dev --extra gateway
python -m pytest tests/ -v
```
Expected: 全部测试通过；`modelctl list` 正常输出（未安装 gateway 的机器上既有命令仍可用，因为 `cli.py` 顶部 import 的 `modelctl.core.gateway` 模块本身不依赖 fastapi/httpx）。

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .env.example docs/nginx/llm-routing.example.conf README.md
git commit -m "docs: 多模型路由配置示例、环境变量与 README"
```

---

### Task 6: 端到端集成验证（部署环境手工清单）

**Files:** 无代码改动（B 机 nginx 与 C 机部署操作）

- [ ] **Step 1: C 机启动网关并验证**

Run:
```bash
uv sync --extra gateway
modelctl gateway start
curl -s http://127.0.0.1:5003/v1/models
```
Expected: 返回本节点注册表模型列表（含 deepseek-v4-flash；qwen3.8 若已启动则也在列）

- [ ] **Step 2: C 机生成 nginx 片段并上传 B 机**

Run:
```bash
modelctl nginx-snippet --node 210 --host 192.168.77.210 > llm-routes-210.conf
scp llm-routes-210.conf root@B机:/etc/nginx/llm-routes/
```

- [ ] **Step 3: B 机应用 nginx 配置**

按 `docs/nginx/llm-routing.example.conf` 将 4 条 location 并入监听 5000 的 server 块（替换原 `^/210/llm/` 规则），并确保 nginx.conf http 块 `include /etc/nginx/llm-routes/*.conf;`。

Run: `nginx -t && systemctl reload nginx`
Expected: 语法检查通过，reload 成功

- [ ] **Step 4: curl 校验矩阵（B 机或 A 设备）**

```bash
# 1) 路径式直连（旧模型）
curl -s http://<B机>:5000/210/llm/deepseek-v4-flash/v1/models
# 2) 路径式直连（新模型，若已部署）
curl -s http://<B机>:5000/210/llm/qwen3.8/v1/models
# 3) 网关（model 参数）—— 缺省回退
curl -s http://<B机>:5000/210/llm/v1/models
# 4) 网关 + body model 切换（未识别的名字回退默认模型）
curl -s http://<B机>:5000/210/llm/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8","stream":false,"messages":[{"role":"user","content":"hi"}]}'
# 5) 旧用量查询兼容
curl -s http://<B机>:5000/210/llm/v1/api/usage
# 6) 按模型用量查询
curl -s http://<B机>:5000/210/llm/deepseek-v4-flash/v1/api/usage
# 7) 未知模型路径 → 404
curl -s -o /dev/null -w '%{http_code}\n' http://<B机>:5000/210/llm/ghost-model/v1/models
```

Expected: 1/2/3 返回模型列表 JSON；4 返回 chat 响应且模型名被改写；5/6 返回 cc-switch 兼容用量 JSON；7 返回 404

- [ ] **Step 5: cc-switch 客户端验证**

- 新增卡片：baseUrl `https://xxx:5000/210/llm/qwen3.8/v1`（若部署 qwen3.8）——选择模型并发送消息
- 旧卡片：baseUrl `https://xxx:5000/210/llm/v1`——行为与改造前一致
- 用量卡片：`https://xxx:5000/210/llm/deepseek-v4-flash/v1/api/usage`——显示累计费用

Expected: 各卡片正常对话；流式输出即时到达；用量正常展示
