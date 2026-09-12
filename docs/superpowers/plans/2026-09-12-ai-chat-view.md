# AI 对话（模型调试台）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 modelctl 管理面板侧边导航新增「AI 对话」，选中已启动且健康的模型直接对话，看到流式输出、reasoning 分区、token/耗时统计与上游请求响应原文。

**Architecture:** 后端新增 `POST /admin/api/chat/completions`（管理面 Bearer 鉴权），复用从 `gateway.py` 抽出的 `prepare_openai_upstream()` 得到目标模型与改写后请求体，SSE 原样透传；`route_mode` 支持 `direct`（选谁打谁）与 `gateway`（复现家族路由/上下文切换落点）。前端 `views/chat/index.vue` + `components/chat/*` 三栏布局，用 `fetch` 读 `ReadableStream` 逐块渲染，历史存 localStorage。

**Tech Stack:** FastAPI + httpx（`httpx.MockTransport` 测试）；Vue 3 `<script setup>` + Pinia + UnoCSS；pytest / vitest。

**Spec:** `docs/superpowers/specs/2026-09-12-ai-chat-view-design.md`

## Global Constraints

- 新建 `.py` 文件遵循仓库既有文件头格式（见 `src/modelctl/core/webui/admin_models.py` 顶部：`#!/usr/bin/env python3` + `# -*- coding: utf-8 -*-` + `@File/@IDE/@Author/@Date/@Desc` 块）。
- 严禁执行任何 DDL；本计划不建库、不改 schema、不碰 `accounts` 的 `sessions/messages` 表。
- UI 只显示虚拟 ID；时间统一 `YYYY-MM-DD HH:mm:ss`。
- 每个行为变更先写失败测试再实现（TDD）；每个 Task 结束提交一次。
- PowerShell 不支持 `&&`，多命令用 `;`。
- 后端测试从仓库根执行：`python -m pytest tests/<file> -v`；前端从 `web/` 执行：`npm test -- <pattern>`。
- 前端样式用 UnoCSS（presetWind3，Tailwind 兼容类名），非 Element Plus；改子组件内部样式必须 `:deep()`。
- 组件 `<script setup name="Xxx">`；路由视图 `views/chat/index.vue` 的 name 用 `chat`，与 `route.name` 一致以配合 keep-alive。

---

### Task 1: 抽出 `prepare_openai_upstream()` 并挂到 `app.state`

唯一触碰既有网关代码的 Task，风险最高：先用等价性回归测试兜住，再抽取。抽取范围严格限定「路由解析 + body 改写 + 上游 headers/url 拼装」，审计与流式分支一律不动。

**Files:**
- Modify: `src/modelctl/core/gateway.py`（新增 `PreparedUpstream`/`PreparedError`/`prepare_openai_upstream`，插在 `resolve_model()` 之后约 L823；改 `proxy()` L1567-1624 调用它；`create_app()` 内 L957 附近挂 `app.state`）
- Test: `tests/test_gateway.py`

**Interfaces:**
- Consumes: `resolve_model()`(L792)、`apply_context_switch()`(L608)、`estimate_prompt_tokens()`(L375)、`_normalize_reasoning_effort()`(L295)、`GatewayModel.upstream_api_key()`(L416)、`_THINKING_DISABLED_GROUPS/_THINKING_DISABLED_ENGINES`(L275-277)。
- Produces:
  ```python
  @dataclass
  class PreparedUpstream:
      target: GatewayModel
      body: dict                 # 已改写：model=upstream_model / reasoning_effort / chat_template_kwargs
      headers: dict              # {"Content-Type": …, 可选 Authorization}
      url: str                   # f"{target.backend_url}/v1/{path}"
      route_reason: str | None   # None | "group_route" | "context_switch"

  @dataclass
  class PreparedError:
      status_code: int
      payload: dict              # {"error": {"message": …, "type": …}}

  def prepare_openai_upstream(registry, groups, group_cache, default_model,
                              context_rules, body: dict, path: str)
      -> PreparedUpstream | PreparedError: ...
  ```
- Produces（app.state，Task 3 消费）：`gateway_registry` / `gateway_groups` / `gateway_default_model` / `gateway_context_rules`（`group_route_cache` 已存在于 L959）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_gateway.py` 末尾追加（`GatewayModel` 已在文件头 import）：

```python
def _gm(name, engine="llamacpp", group=None, api_key=None):
    return GatewayModel(
        name=name, engine=engine, backend_url="http://127.0.0.1:9999",
        upstream_model=f"{name}-up", api_key=api_key,
        health_url="http://127.0.0.1:9999/health", group=group,
    )


def test_prepare_openai_upstream_direct_uses_named_model():
    from modelctl.core.gateway import prepare_openai_upstream
    reg = {"a": _gm("a", api_key="sk-up")}
    out = prepare_openai_upstream(reg, {}, None, None, {}, {"model": "a", "messages": []}, "chat/completions")
    assert out.target.name == "a"
    assert out.body["model"] == "a-up"                       # 改写为 upstream_model
    assert out.headers["Authorization"] == "Bearer sk-up"
    assert out.url == "http://127.0.0.1:9999/v1/chat/completions"
    assert out.route_reason is None


def test_prepare_openai_upstream_not_found():
    from modelctl.core.gateway import PreparedError, prepare_openai_upstream
    out = prepare_openai_upstream({}, {}, None, None, {}, {"model": "nope", "messages": []}, "chat/completions")
    assert isinstance(out, PreparedError) and out.status_code == 404


def test_prepare_openai_upstream_rewrites_like_proxy():
    """同一 body 经 prepare 得到的 url/headers/改写后 body 与 proxy() 的判定一致。"""
    from modelctl.core.gateway import prepare_openai_upstream
    reg = {"qwen3.8": _gm("qwen3.8", engine="vllm", group="qwen3.8", api_key="k")}
    body = {"model": "qwen3.8", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high"}
    out = prepare_openai_upstream(reg, {"qwen3.8": [reg["qwen3.8"]]}, None, None, {}, body, "chat/completions")
    assert out.target.name == "qwen3.8"
    assert out.body["reasoning_effort"] == "xhigh"                       # vLLM 枚举映射
    assert out.body["chat_template_kwargs"] == {"enable_thinking": False}  # group+引擎白名单注入
    assert out.url == "http://127.0.0.1:9999/v1/chat/completions"
    assert body.get("model") == "qwen3.8"   # 调用方 dict 不被就地改写


def test_prepare_openai_upstream_group_route_reason():
    from modelctl.core.gateway import prepare_openai_upstream
    a = _gm("a", group="fam")
    out = prepare_openai_upstream({"a": a}, {"fam": [a]}, None, None, {},
                                  {"model": "fam", "messages": []}, "chat/completions")
    assert out.target.name == "a"
    assert out.route_reason == "group_route"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_gateway.py -k prepare_openai_upstream -v`
Expected: FAIL — `ImportError: cannot import name 'prepare_openai_upstream'`

- [ ] **Step 3: 实现两个 dataclass + 函数**

在 `gateway.py` 的 `resolve_model()` 之后（L823 附近）插入。逻辑照搬 `proxy()` L1567-1624 的路由+改写段，不含审计、不含 httpx：

```python
@dataclass
class PreparedUpstream:
    """OpenAI 请求的上游转发要素（路由解析 + body 改写的结果）。"""

    target: GatewayModel
    body: dict
    headers: dict
    url: str
    route_reason: str | None = None


@dataclass
class PreparedError:
    """路由/改写失败：调用方直接按 status_code + payload 回响应。"""

    status_code: int
    payload: dict


def prepare_openai_upstream(
    registry: dict[str, GatewayModel],
    groups: dict[str, list[GatewayModel]] | None,
    group_cache: "GroupRouteCache | None",
    default_model: str | None,
    context_rules: dict[str, list[ContextSwitchRule]] | None,
    body: dict,
    path: str,
):
    """把 OpenAI 请求体解析成「目标模型 + 改写后 body + 上游 headers + url」。

    与 proxy() 的路由判定同源，四步：家族/名称解析 → 上下文长度切换 →
    upstream_model 改写 + reasoning_effort 归一 + thinking 注入 → 上游认证
    （**永不用调用方 key**，见 gateway 原注释）。失败返回 PreparedError。

    从 proxy() 抽出是为了让管理面 `/admin/api/chat/completions` 复用同一份网关
    语义；proxy() 的审计、accounts 限流、SSE 透传不在此职责内。
    """
    original_model = str(body.get("model") or "")
    target = resolve_model(registry, body.get("model"), default_model, groups, group_cache)
    if target is None:
        return PreparedError(
            404,
            {"error": {"message": f"model not found: {body.get('model')}", "type": "invalid_request_error"}},
        )
    route_reason: str | None = None
    # 请求名命中 group（家族）且落点是成员名 → 家族路由（供 UI 解释"为何不是它"）
    if groups and original_model and original_model in groups and target.name != original_model:
        route_reason = "group_route"
    if context_rules:
        prompt_tokens = estimate_prompt_tokens(body)
        switched = apply_context_switch(
            registry, context_rules, (original_model, target.group or "", target.name), prompt_tokens,
        )
        if switched is not None and switched.name != target.name:
            target = switched
            route_reason = "context_switch"
    body = dict(body)  # 不改调用方 dict：proxy 的审计与 UI 原文都要看改写后结果
    body["model"] = target.upstream_model
    _normalize_reasoning_effort(body, target.reasoning_effort_map)
    should_disable = (
        target.thinking_disabled
        if target.thinking_disabled is not None
        else (target.group in _THINKING_DISABLED_GROUPS and target.engine in _THINKING_DISABLED_ENGINES)
    )
    if should_disable and "chat_template_kwargs" not in body:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Content-Type": "application/json"}
    up_key = target.upstream_api_key()
    if up_key:
        headers["Authorization"] = f"Bearer {up_key}"
    return PreparedUpstream(target=target, body=body, headers=headers,
                            url=f"{target.backend_url}/v1/{path}", route_reason=route_reason)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_gateway.py -k prepare_openai_upstream -v`
Expected: PASS ×4

- [ ] **Step 5: `proxy()` 改为调用它（行为零变化）**

把 `proxy()` 中从 `target = resolve_model(registry, …)`（L1567）到 `url = f"{target.backend_url}/v1/{path}"`（L1624）整段替换：

```python
        prepared = prepare_openai_upstream(
            registry, groups, group_cache, default_model, context_rules, body, path,
        )
        if isinstance(prepared, PreparedError):
            _release_if_acquired()
            err_msg = prepared.payload["error"]["message"]
            # 审计：模型名写请求原始值（resolve 失败即无 profile 可参照）
            if audit_log is not None:
                audit_log.record(_build_audit_entry(
                    model_name=str(body.get("model") or ""), profile_name="", profile_engine="",
                    path=path, stream=bool(body.get("stream")),
                    native_metrics=None, usage=None, gateway_metrics=None,
                    status_code=prepared.status_code, error=err_msg, finish_reason=None,
                    input_char_len=body_char_len, auth=label, client_ip=client_ip_of(request),
                    user_id=(identity.user_id if identity else None),
                    key_id=(identity.key_id if identity else None),
                ))
            return JSONResponse(status_code=prepared.status_code, content=prepared.payload)
        # 改写后的 body 供后续审计与转发使用（prepare 返回的是副本）
        body = prepared.body
        target = prepared.target
        headers = prepared.headers
        url = prepared.url
        # 审计：每次请求取目标模型的 audit_log（create_app 已统一注入）
        self_audit_log = target.audit_log
        logger.info(
            f"OpenAI 上游路由 model={original_model!r} -> {target.name}"
            + (f"（{prepared.route_reason}）" if prepared.route_reason else "")
        )
```

在替换段之前（`body_char_len` 计算处）加一行，供上面日志与审计使用：

```python
        original_model = str(body.get("model") or "")
```

> 被删掉的原始代码段（不要保留）：`resolve_model` 调用 + 其 404 审计块、`if context_rules:` 上下文切换块、`body["model"] = target.upstream_model`、`_normalize_reasoning_effort(...)`、`_should_disable_thinking` 块、`headers = {...}` / `up_key` / `url = ...`。原 L1601-1602 的 `self_audit_log` 赋值已并入上面代码块。

- [ ] **Step 6: 跑既有网关全量测试确保零回归**

Run: `python -m pytest tests/test_gateway.py tests/test_gateway_context_switch.py tests/test_audit.py tests/test_accounts_gateway.py -v`
Expected: 全 PASS。若有断言因 body 变副本而失败，检查是否漏了 `body = prepared.body`。

- [ ] **Step 7: 挂载 `app.state`**

在 `create_app()` 内 `app.state.audit_log = audit_log`（L957）之后追加：

```python
    # 管理面 `/admin/api/chat/completions` 复用同一份路由判定（见 admin_chat.py）：
    # 这些数据原本只活在闭包里，挂出来才能被同 app 的管理面子路由读到。
    app.state.gateway_registry = registry
    app.state.gateway_groups = groups
    app.state.gateway_default_model = default_model
    app.state.gateway_context_rules = context_rules
    app.state.gateway_read_timeout = read_timeout
```

- [ ] **Step 8: 冒烟测试 app.state**

`tests/test_gateway.py` 追加：

```python
def test_create_app_exposes_gateway_state():
    app = create_app(registry={}, groups={})
    assert app.state.gateway_registry == {}
    assert app.state.gateway_groups == {}
    assert app.state.gateway_default_model is None
    assert app.state.gateway_context_rules == {}
```

Run: `python -m pytest tests/test_gateway.py::test_create_app_exposes_gateway_state -v`
Expected: PASS

- [ ] **Step 9: 提交**

```bash
git add src/modelctl/core/gateway.py tests/test_gateway.py
git commit -m "refactor(gateway): extract prepare_openai_upstream from proxy and expose gateway state"
```

---

### Task 2: 模型摘要新增 `vision` 软徽章字段

**Files:**
- Modify: `src/modelctl/core/webui/admin_models.py`（新增 `_vision_capability`；`_model_summary` 返回 dict L117-130 加字段）
- Modify: `web/src/api/types.ts`（`ModelInfo` 接口 L28-53）
- Test: `tests/test_admin_models_vision.py`（新建）

**Interfaces:**
- Consumes: `Profile.engine` / `Profile.engine_config`（`src/modelctl/core/profile.py:34-56`）。llamacpp 的 vision 语义参照 `src/modelctl/engines/llamacpp.py:268`（`on/true/1` 为开）。
- Produces: `_vision_capability(p) -> bool | None`；`_model_summary()` 结果多出 `"vision": bool | None`；前端 `ModelInfo.vision: boolean | null`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_admin_models_vision.py`（文件头按仓库规范补全，作者信息沿用同目录 `admin_models.py`）：

```python
"""_vision_capability 单元测试：多模态只能尽力推断，不能硬门禁。"""

from __future__ import annotations

from modelctl.core.profile import Profile
from modelctl.core.webui.admin_models import _vision_capability


def test_llamacpp_vision_on():
    assert _vision_capability(Profile(name="m", engine="llamacpp", port=1,
                                      engine_config={"vision": "on"})) is True


def test_llamacpp_vision_off():
    assert _vision_capability(Profile(name="m", engine="llamacpp", port=1,
                                      engine_config={"vision": "off"})) is False


def test_llamacpp_vision_absent_is_unknown():
    assert _vision_capability(Profile(name="m", engine="llamacpp", port=1, engine_config={})) is None


def test_other_engine_is_unknown():
    """vLLM 等引擎 profile 层无权威视觉字段 → None（前端只不显示徽章，绝不禁用图片）。"""
    assert _vision_capability(Profile(name="m", engine="vllm", port=1, engine_config={})) is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_admin_models_vision.py -v`
Expected: FAIL — `ImportError: cannot import name '_vision_capability'`

- [ ] **Step 3: 实现并接入**

在 `admin_models.py` 的 `_model_summary` 之前插入：

```python
def _vision_capability(p) -> bool | None:
    """尽力推断视觉（多模态）能力——profile 层没有统一字段，只能按引擎猜。

    仅 llamacpp 在 `engine_config.vision` 显式声明（见 engines/llamacpp.py 的
    mmproj 判定，on/true/1 为开）。返回 None 表示"该引擎无权威字段"：前端据此
    **不显示徽章但绝不禁用图片按钮**——把猜测当门禁会误杀 vLLM 等已起视觉模型
    但 profile 未声明的情况。
    """
    if p.engine == "llamacpp":
        raw = str(p.engine_config.get("vision", "")).lower()
        if raw in ("on", "true", "1"):
            return True
        if raw in ("off", "false", "0"):
            return False
    return None
```

在 `_model_summary` 返回 dict 的 `"log_path": log_path,` 之后加：

```python
        "vision": _vision_capability(p),
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_admin_models_vision.py tests/test_admin_models_classify.py -v`
Expected: 全 PASS

- [ ] **Step 5: 前端类型同步**

`web/src/api/types.ts` 的 `ModelInfo` 接口内 `log_path: string | null;` 之后加：

```ts
  /** 视觉能力软徽章：true/false 为已声明，null 为引擎无权威字段（不禁用图片） */
  vision: boolean | null;
```

- [ ] **Step 6: 类型检查**

Run: `cd web; npx vue-tsc --noEmit`
Expected: 无错误（`ModelInfo` 只有后端构造，前端无手写字面量需补字段）。

- [ ] **Step 7: 提交**

```bash
git add src/modelctl/core/webui/admin_models.py tests/test_admin_models_vision.py web/src/api/types.ts
git commit -m "feat(models): expose best-effort vision capability in model summary"
```

---

### Task 3: 后端 `admin_chat.py` — `POST /admin/api/chat/completions`

**Files:**
- Create: `src/modelctl/core/webui/admin_chat.py`
- Modify: `src/modelctl/core/webui/admin_router.py`（`_SUBROUTER_MODULES` L40-52 追加一项）
- Test: `tests/test_webui_admin_chat.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `app.state.gateway_registry/gateway_groups/gateway_default_model/gateway_context_rules/gateway_read_timeout/group_route_cache`、`prepare_openai_upstream`、`PreparedError`；既有 `is_model_available`(gateway.py:635)、`require_auth`(admin_auth.py:94)。
- Produces: `POST /admin/api/chat/completions`；请求体白名单 `model, messages, route_mode, temperature, top_p, max_tokens, system, include_usage`；响应 `StreamingResponse`（或透传上游错误），带响应头 `X-Chat-Routed-To` / `X-Chat-Route-Reason` / `X-Accel-Buffering: no`。
- 测试钩子：`app.state.chat_transport`（注入 `httpx.MockTransport`，无则 `None`）。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_webui_admin_chat.py`（文件头按仓库规范补全）：

```python
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


def test_requires_admin_auth():
    resp = _post(_app({}), {"model": "a", "messages": [{"role": "user", "content": "hi"}]}, headers={})
    assert resp.status_code == 401
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_webui_admin_chat.py -v`
Expected: FAIL — 全部 404（路由未注册）

- [ ] **Step 3: 实现 `admin_chat.py`**

新建 `src/modelctl/core/webui/admin_chat.py`（含仓库规范文件头）：

```python
"""core/webui/admin_chat.py — AI 对话（模型调试台）代理端点。

由 `_include_subrouter` 挂 `/chat` 前缀，最终路径 `/admin/api/chat/completions`。
鉴权走**管理面** `require_auth`（Bearer API_KEY）：前端只持有管理面 token，
数据面 `GATEWAY_CLIENT_API_KEY` 默认空且刻意不进浏览器（见 .env.example:118）。

复用 Task 1 抽出的 `prepare_openai_upstream`，两种模式：
  - route_mode=direct （默认）：把 model 当精确 profile 名，禁家族/上下文/默认回退，
    选谁打谁——调试结果可信；
  - route_mode=gateway：完整复现网关路由，回答"外部客户端打这个名字会落到哪"。
两种都**不走 HTTP 自调用 /v1**：复现路由判定即可，不必占两条连接、不必占数据面
并发槽、不必计入 accounts 记账与限流（调试流量不该污染生产统计）。

上游 4xx/5xx 原样透传状态码与响应体，并同样带上 X-Chat-Routed-To——"打给谁却报
400"是最常见的调试场景。落点信息走响应头而非自造 SSE 帧：前端一次 fetch 就能拿全。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from loguru import logger

from modelctl.core.webui.admin_auth import require_auth

router = APIRouter()

# base64 图片必须有界：24 MB 约合 6 张 1280px JPEG（前端已压到 200-400 KB）
_MAX_BODY_BYTES = 24 * 1024 * 1024
# 透传给上游的采样字段白名单（未知字段丢弃，不整体转发调用方 JSON）
_PASSTHROUGH_FIELDS = ("temperature", "top_p", "max_tokens")


def _router() -> APIRouter:
    return router


def _err(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"message": message, "type": "invalid_request_error", **extra}})


@router.post("/completions")
async def chat_completions(request: Request, _: None = Depends(require_auth)):
    # 经模块属性访问 is_model_available，测试可 patch modelctl.core.gateway.is_model_available
    from modelctl.core import gateway as gw

    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        return _err(413, "请求体超过 24MB 上限（图片过大）")
    try:
        payload = json.loads(raw or b"{}")
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        return _err(400, "请求体必须是 JSON 对象")

    model = str(payload.get("model") or "")
    route_mode = str(payload.get("route_mode") or "direct")
    if route_mode not in ("direct", "gateway"):
        return _err(400, f"route_mode 只能是 direct/gateway：{route_mode}")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return _err(400, "messages 不能为空")

    state = request.app.state
    # system 由服务端转成首条 system 消息：前端消息数组里不存 system，避免与历史混淆
    sys_prompt = payload.get("system")
    upstream_messages = (
        [{"role": "system", "content": sys_prompt}] if isinstance(sys_prompt, str) and sys_prompt else []
    ) + list(messages)

    gateway_body: dict = {"model": model, "messages": upstream_messages, "stream": True}
    for field in _PASSTHROUGH_FIELDS:
        if payload.get(field) is not None:
            gateway_body[field] = payload[field]
    if payload.get("include_usage", True):
        # 统计全靠 usage；个别引擎的 OpenAI 兼容层不认 stream_options → 前端关掉重发
        gateway_body["stream_options"] = {"include_usage": True}

    if route_mode == "direct":
        target = state.gateway_registry.get(model)
        if target is None:
            return _err(404, f"model not found: {model}")
        if not gw.is_model_available(target):
            return JSONResponse(status_code=409, content={"error": {
                "code": "model_not_running", "message": f"模型未运行：{model}",
                "type": "invalid_request_error"}})
        # 单元素注册表 + 空 groups/rules/default → 不可能发生家族/上下文跳转
        prepared = gw.prepare_openai_upstream({model: target}, {}, None, None, {},
                                              gateway_body, "chat/completions")
    else:
        prepared = gw.prepare_openai_upstream(
            state.gateway_registry, state.gateway_groups,
            getattr(state, "group_route_cache", None),
            state.gateway_default_model, state.gateway_context_rules,
            gateway_body, "chat/completions",
        )
        if isinstance(prepared, gw.PreparedError):
            return JSONResponse(status_code=prepared.status_code, content=prepared.payload)
        if not gw.is_model_available(prepared.target):
            return JSONResponse(status_code=409, content={"error": {
                "code": "model_not_running", "message": f"路由目标未运行：{prepared.target.name}",
                "type": "invalid_request_error"}})

    route_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "X-Chat-Routed-To": prepared.target.name,
        "X-Chat-Route-Reason": prepared.route_reason or "",
    }

    import httpx

    client = httpx.AsyncClient(
        timeout=float(getattr(state, "gateway_read_timeout", 600.0)),
        transport=getattr(state, "chat_transport", None),
    )
    try:
        req = client.build_request("POST", prepared.url, json=prepared.body, headers=prepared.headers)
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as error:
        await client.aclose()
        logger.warning(f"chat 上游不可达 model={prepared.target.name}: {error}")
        return JSONResponse(status_code=502, content={"error": {
            "message": f"后端不可达：{error}", "type": "upstream_error"}})

    if upstream.status_code >= 400:
        content = await upstream.aread()
        await client.aclose()
        return Response(status_code=upstream.status_code, content=content,
                        media_type=upstream.headers.get("content-type"), headers=route_headers)

    async def relay():
        # 生命周期教训同 gateway.proxy 流式分支：不能用 async with 包住再返回
        # StreamingResponse——SSE 惰性迭代，客户端会在端点返回时提前切断。
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(relay(),
                             media_type=upstream.headers.get("content-type", "text/event-stream"),
                             headers=route_headers)
```

> 端点内一律经 `from modelctl.core import gateway as gw` 再 `gw.is_model_available(...)` 调用；若在函数内 `from modelctl.core.gateway import is_model_available`，测试的 patch 会失效。

- [ ] **Step 4: 注册子路由**

`admin_router.py` 的 `_SUBROUTER_MODULES` 元组末尾追加：

```python
    # AI 对话（模型调试台）：管理面 Bearer 鉴权，复用网关路由判定；相对路径需 "/chat"
    ("modelctl.core.webui.admin_chat", "/chat"),
```

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest tests/test_webui_admin_chat.py -v`
Expected: 全 PASS。若 409 用例失败，确认端点内是 `gw.is_model_available(...)` 经模块属性访问（函数内 `from x import y` 会让 patch 失效）。

- [ ] **Step 6: 跑管理面相关测试确保未打挂聚合路由**

Run: `python -m pytest tests/test_webui_admin_accounts.py tests/test_admin_models_classify.py -v`
Expected: 全 PASS

- [ ] **Step 7: 提交**

```bash
git add src/modelctl/core/webui/admin_chat.py src/modelctl/core/webui/admin_router.py tests/test_webui_admin_chat.py
git commit -m "feat(webui): add /admin/api/chat/completions proxy for AI chat debugger"
```

---

### Task 4: markdown 渲染工具 + 依赖

**Files:**
- Modify: `web/package.json`、`web/package-lock.json`
- Create: `web/src/utils/markdown.ts`
- Test: `web/src/utils/markdown.test.ts`

**Interfaces:**
- Produces: `renderMarkdown(text: string): string` — 返回已消毒 HTML，供 `v-html` 使用。

- [ ] **Step 1: 安装依赖**

Run: `cd web; npm install markdown-it dompurify highlight.js; npm install -D @types/markdown-it`
Expected: `dependencies` 增 3 项，`devDependencies` 增 `@types/markdown-it`。

- [ ] **Step 2: 写失败测试**

新建 `web/src/utils/markdown.test.ts`：

```ts
import { describe, it, expect } from 'vitest';
import { renderMarkdown } from './markdown';

describe('renderMarkdown', () => {
  it('渲染列表与代码块', () => {
    const html = renderMarkdown('- a\n- b\n```js\nconst x = 1;\n```');
    expect(html).toContain('<li>a</li>');
    expect(html).toContain('hljs');
  });

  it('上游内容不可信：script 与事件属性必须被消毒', () => {
    const html = renderMarkdown('<img src=x onerror="alert(1)">\n<script>alert(2)<\/script>');
    expect(html.toLowerCase()).not.toContain('<script');
    expect(html.toLowerCase()).not.toContain('onerror');
  });

  it('空输入返回空串而不抛', () => {
    expect(renderMarkdown('')).toBe('');
  });
});
```

- [ ] **Step 3: 运行确认失败**

Run: `cd web; npm test -- markdown`
Expected: FAIL — 无法解析 `./markdown`

- [ ] **Step 4: 实现 `markdown.ts`**

```ts
import MarkdownIt from 'markdown-it';
import DOMPurify from 'dompurify';
import hljs from 'highlight.js/lib/common';
import 'highlight.js/styles/github-dark.css';

/**
 * 对话正文 markdown 渲染（唯一使用点，便于以后换实现）。
 *
 * 上游模型输出是**不可信内容**：html:false 从源头不解析内联 HTML，
 * DOMPurify 再兜一层（引擎兼容层有时会把 HTML 塞进 code fence 之外）。
 */
const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  highlight: (code, lang) => {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return `<pre class="hljs"><code class="language-${lang}">${hljs.highlight(code, { language: lang }).value}</code></pre>`;
      } catch {
        /* 高亮失败落到转义分支，绝不因渲染问题打断流式显示 */
      }
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(code)}</code></pre>`;
  },
});

export function renderMarkdown(text: string): string {
  if (!text) return '';
  return DOMPurify.sanitize(md.render(text), { ADD_ATTR: ['target'] });
}
```

- [ ] **Step 5: 运行确认通过**

Run: `cd web; npm test -- markdown`
Expected: PASS ×3

- [ ] **Step 6: 提交**

```bash
git add web/package.json web/package-lock.json web/src/utils/markdown.ts web/src/utils/markdown.test.ts
git commit -m "feat(web): add sanitized markdown renderer for AI chat"
```

---

### Task 5: 流式 API 客户端 + chat store

**Files:**
- Create: `web/src/api/chat.ts`
- Create: `web/src/stores/chat.ts`
- Test: `web/src/stores/chat.test.ts`

**Interfaces:**
- Consumes: `useAuthStore()`（`token` / `clear()`，`web/src/stores/auth.ts:27-52`）；`router`（`@/router`）。
- Produces:
  - `web/src/api/chat.ts`：
    ```ts
    export interface ChatUsage { prompt_tokens: number; completion_tokens: number }
    export interface ChatMeta { routedTo: string; routeReason: string; status: number }
    export interface ChatHandlers {
      onDelta(t: string): void; onReasoning(t: string): void; onUsage(u: ChatUsage): void;
      onMeta(m: ChatMeta): void; onDone(): void; onError(e: { message: string; raw?: string }): void;
    }
    export async function streamChatCompletions(
      payload: Record<string, unknown>, h: ChatHandlers, signal: AbortSignal, onRaw: (s: string) => void,
    ): Promise<void>
    ```
  - `web/src/stores/chat.ts`：`useChatStore()` → `sessions`、`activeId`、`active`、`messages`、`model`、`routeMode`、`params`、`streaming`、`rawText`、`storageDegraded`、`setModel()`、`newSession()`、`selectSession()`、`send(text, images)`、`stop()`、`persist()`；类型 `ChatMsg` / `ChatSession` / 常量 `LS_KEY`。

- [ ] **Step 1: 写失败测试**

新建 `web/src/stores/chat.test.ts`：

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';

function fakeFetch(chunks: string[], headers: Record<string, string> = {}) {
  let i = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(c) { if (i < chunks.length) c.enqueue(new TextEncoder().encode(chunks[i++])); else c.close(); },
  });
  return vi.fn(async () => ({
    ok: true, status: 200, body,
    headers: new Headers({ 'x-chat-routed-to': 'a', 'x-chat-route-reason': 'group_route', ...headers }),
  }));
}

const SSE_OK = [
  'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
  'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
  'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
  'data: [DONE]\n\n',
];

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
});

it('逐块拼接 content，usage 帧不当正文', async () => {
  global.fetch = fakeFetch(SSE_OK) as never;
  const { useChatStore } = await import('./chat');
  const s = useChatStore();
  s.setModel('a');
  await s.send('hi', []);
  const last = s.messages[s.messages.length - 1];
  expect(last.role).toBe('assistant');
  expect(last.content).toBe('Hello');
  expect(last.usage).toEqual({ prompt_tokens: 5, completion_tokens: 2 });
  expect(last.routedTo).toBe('a');
  expect(s.streaming).toBe(false);
});

it('reasoning 与 content 分区', async () => {
  global.fetch = fakeFetch([
    'data: {"choices":[{"delta":{"reasoning_content":"想一想"}}]}\n\n',
    'data: {"choices":[{"delta":{"content":"答"}}]}\n\n',
    'data: [DONE]\n\n',
  ]) as never;
  const { useChatStore } = await import('./chat');
  const s = useChatStore();
  s.setModel('a');
  await s.send('q', []);
  const last = s.messages[s.messages.length - 1];
  expect(last.reasoning).toBe('想一想');
  expect(last.content).toBe('答');
});

it('畸形帧不中断流，原文进 rawText', async () => {
  global.fetch = fakeFetch([
    'data: not-json\n\n',
    'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
    'data: [DONE]\n\n',
  ]) as never;
  const { useChatStore } = await import('./chat');
  const s = useChatStore();
  s.setModel('a');
  await s.send('hi', []);
  expect(s.messages[s.messages.length - 1].content).toBe('ok');
  expect(s.rawText).toContain('not-json');
});

it('HTTP 400 保留状态码与上游原文', async () => {
  global.fetch = vi.fn(async () => ({
    ok: false, status: 400, body: null,
    headers: new Headers({ 'x-chat-routed-to': 'a' }),
    text: async () => '{"error":{"message":"max 0 images"}}',
  })) as never;
  const { useChatStore } = await import('./chat');
  const s = useChatStore();
  s.setModel('a');
  await s.send('hi', ['data:image/jpeg;base64,AAA']);
  const last = s.messages[s.messages.length - 1];
  expect(last.error?.message).toContain('400');
  expect(last.error?.raw).toContain('max 0 images');
});

it('会话持久化到 localStorage 并可回读', async () => {
  global.fetch = fakeFetch(SSE_OK) as never;
  const { useChatStore, LS_KEY } = await import('./chat');
  const s = useChatStore();
  s.setModel('a');
  await s.send('你好', []);
  const saved = JSON.parse(localStorage.getItem(LS_KEY) || '[]');
  expect(saved).toHaveLength(1);
  expect(saved[0].title).toBe('你好');
});

it('超限时先剥最旧会话图片，仍超再丢整会话', async () => {
  const { useChatStore, LS_KEY, LS_CAP } = await import('./chat');
  const s = useChatStore();
  s.setModel('a');
  s.newSession();
  s.active!.messages.push({ id: 'm1', role: 'user', content: 'old', reasoning: '', images: ['data:image/jpeg;base64,' + 'A'.repeat(LS_CAP / 2)] });
  s.newSession();
  s.active!.messages.push({ id: 'm2', role: 'user', content: 'new', reasoning: '', images: ['data:image/jpeg;base64,' + 'B'.repeat(LS_CAP / 2)] });
  s.persist();
  const saved = JSON.parse(localStorage.getItem(LS_KEY) || '[]');
  expect(s.storageDegraded).toBe(true);
  expect(saved.length).toBeLessThanOrEqual(2);
  expect(saved.every((x: { messages: { images: unknown[] }[] }) =>
    x.messages.every((m) => Array.isArray(m.images)))).toBe(true);
});
```

- [ ] **Step 2: 运行确认失败**

Run: `cd web; npm test -- stores/chat`
Expected: FAIL — 无法解析 `./chat`

- [ ] **Step 3: 实现 `api/chat.ts`**

```ts
import { useAuthStore } from '@/stores/auth';
import router from '@/router';

export interface ChatUsage {
  prompt_tokens: number;
  completion_tokens: number;
}

export interface ChatMeta {
  routedTo: string;
  routeReason: string;
  status: number;
}

export interface ChatHandlers {
  onDelta(t: string): void;
  onReasoning(t: string): void;
  onUsage(u: ChatUsage): void;
  onMeta(m: ChatMeta): void;
  onDone(): void;
  onError(e: { message: string; raw?: string }): void;
}

/**
 * 逐帧消费 SSE：按空行切帧 → 取 `data:` 行 → JSON.parse。
 * 畸形帧不中断（上游各引擎的流格式差异正是调试台要看见的东西），交 onRaw 累积。
 */
async function consumeSse(res: Response, h: ChatHandlers, onRaw: (s: string) => void): Promise<void> {
  const reader = res.body!.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split('\n')) {
        const s = line.trim();
        if (!s.startsWith('data:')) continue;
        const data = s.slice(5).trim();
        if (data === '[DONE]') {
          h.onDone();
          return;
        }
        try {
          const obj = JSON.parse(data);
          const delta = obj.choices?.[0]?.delta;
          if (delta?.content) h.onDelta(delta.content);
          // vLLM/SGLang 的思考通道两种字段名都出现过
          const reasoning = delta?.reasoning ?? delta?.reasoning_content;
          if (reasoning) h.onReasoning(reasoning);
          // include_usage 的帧 choices 为空：只取 usage，绝不当正文
          if (obj.usage) h.onUsage(obj.usage);
        } catch {
          onRaw(data);
        }
      }
    }
  }
  h.onDone();
}

/**
 * 对话流式请求。**必须 fetch 而非 axios**：要读 ReadableStream 边到边渲染，
 * 并读响应头 X-Chat-Routed-To。代价是绕过 client.ts 的 401 拦截器，
 * 故此处自己复刻同样的清 token + 跳登录语义。
 */
export async function streamChatCompletions(
  payload: Record<string, unknown>,
  h: ChatHandlers,
  signal: AbortSignal,
  onRaw: (s: string) => void,
): Promise<void> {
  const auth = useAuthStore();
  let res: Response;
  try {
    res = await fetch('/admin/api/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${auth.token}` },
      body: JSON.stringify(payload),
      signal,
    });
  } catch (e) {
    if ((e as Error).name === 'AbortError') {
      h.onDone();
      return;
    }
    h.onError({ message: `网络中断：${e}` });
    return;
  }

  if (res.status === 401) {
    auth.clear();
    const cur = router.currentRoute.value;
    if (cur.path !== '/login') router.push({ path: '/login', query: { redirect: cur.fullPath } });
    h.onError({ message: '登录态失效' });
    return;
  }

  h.onMeta({
    routedTo: res.headers.get('x-chat-routed-to') || '',
    routeReason: res.headers.get('x-chat-route-reason') || '',
    status: res.status,
  });

  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => '');
    h.onError({ message: `HTTP ${res.status}`, raw: text });
    return;
  }
  await consumeSse(res, h, onRaw);
}
```

- [ ] **Step 4: 实现 `stores/chat.ts`**

```ts
import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import { streamChatCompletions, type ChatUsage } from '@/api/chat';

export interface ChatMsg {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  reasoning: string;
  images: string[];
  usage?: ChatUsage;
  ttftMs?: number;
  totalMs?: number;
  error?: { message: string; raw?: string };
  interrupted?: boolean;
  routedTo?: string;
  routeReason?: string;
}

export interface ChatSession {
  id: string;
  title: string;
  model: string;
  createdAt: string;
  messages: ChatMsg[];
}

export const LS_KEY = 'modelctl.chat.v1';
export const LS_CAP = 4 * 1024 * 1024;
const LS_MAX_SESSIONS = 50;
const TITLE_LEN = 24;

function uid(): string {
  return Math.random().toString(36).slice(2);
}

/** `YYYY-MM-DD HH:mm:ss`（sv-SE locale 天然是该形状，避免引 dayjs 只做这一件事）。 */
function now(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export const useChatStore = defineStore('chat', () => {
  const sessions = ref<ChatSession[]>([]);
  const activeId = ref('');
  const model = ref('');
  const routeMode = ref<'direct' | 'gateway'>('direct');
  const params = ref({ temperature: 0.7, top_p: 0.95, max_tokens: 2048, system: '', includeUsage: true });
  const streaming = ref(false);
  const rawText = ref('');
  const storageDegraded = ref(false);
  let controller: AbortController | null = null;

  const active = computed(() => sessions.value.find((s) => s.id === activeId.value) || null);
  const messages = computed(() => active.value?.messages ?? []);

  function hydrate() {
    try {
      sessions.value = JSON.parse(localStorage.getItem(LS_KEY) || '[]');
    } catch {
      sessions.value = [];
    }
  }

  /**
   * 容量降级顺序（图片 data URL 是体积大头）：
   * 截会话数 → 从最旧开始剥图片 → 仍超则从最旧整会话丢弃。
   */
  function persist() {
    let list = sessions.value.slice(-LS_MAX_SESSIONS);
    let str = JSON.stringify(list);
    if (str.length > LS_CAP) {
      for (const s of [...list].reverse()) {
        for (const m of s.messages) if (m.images?.length) m.images = [];
        str = JSON.stringify(list);
        if (str.length <= LS_CAP) {
          storageDegraded.value = true;
          break;
        }
      }
    }
    while (str.length > LS_CAP && list.length > 1) {
      list = list.slice(1);
      str = JSON.stringify(list);
      storageDegraded.value = true;
    }
    try {
      localStorage.setItem(LS_KEY, str);
    } catch {
      /* 隐私模式 / 配额硬失败：对话本身不该因此报错 */
    }
  }

  function setModel(m: string) {
    model.value = m;
  }

  function newSession() {
    const s: ChatSession = { id: uid(), title: '新对话', model: model.value, createdAt: now(), messages: [] };
    sessions.value.push(s);
    activeId.value = s.id;
    persist();
  }

  function selectSession(id: string) {
    activeId.value = id;
  }

  async function send(text: string, images: string[] = []) {
    if (!active.value) newSession();
    const s = active.value!;
    if (!text && !images.length) return;
    if (s.title === '新对话') s.title = (text || '图片对话').slice(0, TITLE_LEN);
    s.messages.push({ id: uid(), role: 'user', content: text, reasoning: '', images });
    const asst: ChatMsg = { id: uid(), role: 'assistant', content: '', reasoning: '', images: [] };
    s.messages.push(asst);

    streaming.value = true;
    rawText.value = '';
    controller = new AbortController();
    const t0 = performance.now();
    let first = true;
    const markFirst = () => {
      if (first) {
        asst.ttftMs = performance.now() - t0;
        first = false;
      }
    };

    const content: Array<Record<string, unknown>> = images.map((d) => ({ type: 'image_url', image_url: { url: d } }));
    if (text) content.push({ type: 'text', text });

    await streamChatCompletions(
      {
        model: s.model,
        messages: [{ role: 'user', content }],
        route_mode: routeMode.value,
        temperature: params.value.temperature,
        top_p: params.value.top_p,
        max_tokens: params.value.max_tokens,
        system: params.value.system || undefined,
        include_usage: params.value.includeUsage,
      },
      {
        onDelta: (t) => { markFirst(); asst.content += t; },
        onReasoning: (t) => { markFirst(); asst.reasoning += t; },
        onUsage: (u) => { asst.usage = u; },
        onMeta: (m) => { asst.routedTo = m.routedTo; asst.routeReason = m.routeReason; },
        onDone: () => { asst.totalMs = performance.now() - t0; streaming.value = false; controller = null; persist(); },
        onError: (e) => { asst.error = e; streaming.value = false; controller = null; persist(); },
      },
      controller.signal,
      (r) => { rawText.value += r + '\n'; },
    );
  }

  function stop() {
    controller?.abort();
    const last = messages.value[messages.value.length - 1];
    if (last?.role === 'assistant') last.interrupted = true;
    streaming.value = false;
    persist();
  }

  hydrate();

  return {
    sessions, activeId, active, messages, model, routeMode, params, streaming, rawText, storageDegraded,
    setModel, newSession, selectSession, send, stop, persist, hydrate,
  };
});
```

- [ ] **Step 5: 运行确认通过**

Run: `cd web; npm test -- stores/chat`
Expected: PASS ×6。若 `usage 帧不当正文` 失败，检查 `consumeSse` 是否对 `choices: []` 做了可选链。

- [ ] **Step 6: 提交**

```bash
git add web/src/api/chat.ts web/src/stores/chat.ts web/src/stores/chat.test.ts
git commit -m "feat(web): add streaming chat API client and chat store with SSE parsing"
```

---

### Task 6: 模型选择纯函数 + 三栏视图与子组件

**Files:**
- Create: `web/src/utils/chatModels.ts`
- Test: `web/src/utils/chatModels.test.ts`
- Create: `web/src/views/chat/index.vue`
- Create: `web/src/components/chat/ChatMessage.vue`
- Create: `web/src/components/chat/ChatComposer.vue`
- Create: `web/src/components/chat/ChatParamsPanel.vue`
- Create: `web/src/components/chat/ChatStatsPanel.vue`
- Create: `web/src/components/chat/ChatRawPanel.vue`
- Create: `web/src/components/chat/ChatHistoryList.vue`

**Interfaces:**
- Consumes: `useChatStore()`（Task 5）、`renderMarkdown`（Task 4）、`listModels(): Promise<ModelsListResponse>`（`web/src/api/models.ts:23`）、`ModelInfo.vision`（Task 2）。
- Produces: `pickRunnableModels(models: ModelInfo[]): ModelInfo[]`；路由组件 `name="chat"`。

- [ ] **Step 1: 写过滤失败测试**

新建 `web/src/utils/chatModels.test.ts`：

```ts
import { describe, it, expect } from 'vitest';
import { pickRunnableModels } from './chatModels';
import type { ModelInfo } from '@/api/types';

const m = (name: string, state: ModelInfo['state'], health: ModelInfo['health']): ModelInfo =>
  ({
    name, group: null, engine: 'vllm', variant: null, port: 1, aliases: [],
    state, health, rates: null, api_key_masked: null, pid: null, log_path: null, vision: null,
  });

describe('pickRunnableModels', () => {
  it('只保留 running 且 healthy（对齐后端 _model_summary 口径）', () => {
    const out = pickRunnableModels([
      m('a', 'running', 'healthy'),
      m('b', 'stopped', null),
      m('c', 'running', 'unknown'),
    ]);
    expect(out.map((x) => x.name)).toEqual(['a']);
  });
});
```

- [ ] **Step 2: 运行确认失败**

Run: `cd web; npm test -- chatModels`
Expected: FAIL — 无法解析 `./chatModels`

- [ ] **Step 3: 实现 `chatModels.ts`**

```ts
import type { ModelInfo } from '@/api/types';

/** 可选模型 = 已启动且健康；口径与后端 `_model_summary` 的 state/health 一致。 */
export function pickRunnableModels(models: ModelInfo[]): ModelInfo[] {
  return models.filter((x) => x.state === 'running' && x.health === 'healthy');
}

/** 把 listModels 的分组响应摊平成数组。 */
export function flattenGroups(resp: { groups: { models: ModelInfo[] }[] }): ModelInfo[] {
  return resp.groups.flatMap((g) => g.models);
}
```

Run: `cd web; npm test -- chatModels`
Expected: PASS

- [ ] **Step 4: `ChatMessage.vue`**

新建 `web/src/components/chat/ChatMessage.vue`：

```vue
<script setup name="ChatMessage">
import { computed } from 'vue';
import { renderMarkdown } from '@/utils/markdown';

const props = defineProps({ msg: { type: Object, required: true } });

const html = computed(() => renderMarkdown(props.msg.content));
const stats = computed(() => {
  const m = props.msg;
  const out = [];
  if (m.ttftMs != null) out.push(`TTFT ${(m.ttftMs / 1000).toFixed(2)}s`);
  if (m.totalMs != null) out.push(`总 ${(m.totalMs / 1000).toFixed(1)}s`);
  if (m.usage) out.push(`${m.usage.prompt_tokens}/${m.usage.completion_tokens} tok`);
  if (m.routedTo) out.push(`落到 ${m.routedTo}${m.routeReason ? `（${m.routeReason}）` : ''}`);
  return out;
});
</script>

<template>
  <div
    :class="[
      'rounded-lg px-3 py-2 text-sm leading-relaxed',
      msg.role === 'user' ? 'self-end max-w-[75%] bg-blue-600 text-slate-50' : 'self-start max-w-[90%] bg-slate-800',
    ]"
  >
    <div v-if="msg.images && msg.images.length" class="mb-1 flex gap-1">
      <img v-for="(d, i) in msg.images" :key="i" :src="d" class="h-16 rounded" alt="附件" />
    </div>

    <details v-if="msg.reasoning" class="mb-1 text-slate-400">
      <summary class="cursor-pointer select-none">思考过程</summary>
      <div class="mt-1 whitespace-pre-wrap border-l-2 border-slate-600 pl-2">{{ msg.reasoning }}</div>
    </details>

    <div v-if="msg.error" class="mb-1 rounded border-l-2 border-red-500 bg-red-500/10 p-2 text-red-300">
      {{ msg.error.message }}
    </div>
    <div
      v-if="msg.error && msg.error.raw"
      class="mb-1 break-all rounded bg-slate-950 p-2 font-mono text-xs whitespace-pre-wrap"
    >
      {{ msg.error.raw }}
    </div>

    <!-- 助手正文是上游 markdown（不可信），renderMarkdown 内已 DOMPurify 消毒 -->
    <div v-if="msg.role === 'assistant'" class="chat-md" v-html="html"></div>
    <div v-else class="whitespace-pre-wrap">{{ msg.content }}</div>

    <div
      v-if="stats.length || msg.interrupted"
      class="mt-1 flex flex-wrap gap-2 border-t border-dashed border-slate-600 pt-1 text-xs text-slate-500"
    >
      <span v-for="s in stats" :key="s">{{ s }}</span>
      <span v-if="msg.interrupted" class="text-amber-400">已中断</span>
    </div>
  </div>
</template>

<style scoped>
/* v-html 注入的节点不带 scoped 属性，必须 :deep() 才能命中 */
.chat-md :deep(pre.hljs) {
  padding: 0.5rem 0.75rem;
  border-radius: 0.375rem;
  overflow-x: auto;
  margin: 0.25rem 0;
}
.chat-md :deep(p) { margin: 0.25rem 0; }
.chat-md :deep(ul), .chat-md :deep(ol) { padding-left: 1.25rem; margin: 0.25rem 0; }
.chat-md :deep(code:not(.hljs code)) {
  background: rgb(2 6 23 / 0.6);
  padding: 0.05rem 0.25rem;
  border-radius: 0.25rem;
}
.chat-md :deep(table) { border-collapse: collapse; margin: 0.25rem 0; }
.chat-md :deep(th), .chat-md :deep(td) { border: 1px solid rgb(51 65 85); padding: 0.2rem 0.4rem; }
</style>
```

- [ ] **Step 5: `ChatComposer.vue`（输入 + 图片粘贴 + 发送/停止）**

新建 `web/src/components/chat/ChatComposer.vue`：

```vue
<script setup name="ChatComposer">
import { ref } from 'vue';

const props = defineProps({ streaming: { type: Boolean, default: false } });
const emit = defineEmits(['send', 'stop']);

const text = ref('');
const images = ref([]); // dataURL[]
const MAX_IMAGES = 4;

/** 压到最长边 1280 / JPEG 0.82（≈200-400 KB），否则 base64 很快撑爆 localStorage 与 24MB 上限。 */
function compress(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, 1280 / Math.max(img.width, img.height));
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(url);
      resolve(canvas.toDataURL('image/jpeg', 0.82));
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('图片解码失败')); };
    img.src = url;
  });
}

async function addFiles(list) {
  const files = list.filter((f) => f && f.type.startsWith('image/'));
  for (const f of files) {
    if (images.value.length >= MAX_IMAGES) break;
    try {
      images.value.push(await compress(f));
    } catch {
      /* 单张解码失败不影响其余 */
    }
  }
}

function onPaste(e) {
  const items = Array.from(e.clipboardData?.items || []);
  addFiles(items.map((it) => it.getAsFile()));
}

function onPick(e) {
  addFiles(Array.from(e.target.files || []));
  e.target.value = '';
}

function submit() {
  if (props.streaming) return;
  if (!text.value && !images.value.length) return;
  emit('send', text.value, images.value);
  text.value = '';
  images.value = [];
}
</script>

<template>
  <div class="border-t border-slate-800 bg-slate-900 p-2">
    <div v-if="images.length" class="mb-1 flex gap-1">
      <div v-for="(d, i) in images" :key="i" class="relative">
        <img :src="d" class="h-14 rounded" alt="待发送图片" />
        <button
          class="absolute -top-1 -right-1 flex size-4 items-center justify-center rounded-full bg-slate-700 text-xs text-slate-200"
          @click="images.splice(i, 1)"
        >
          ×
        </button>
      </div>
    </div>

    <textarea
      v-model="text"
      rows="2"
      class="w-full resize-none rounded border border-slate-700 bg-slate-950 p-2 text-sm"
      placeholder="输入消息，可直接粘贴图片…"
      @paste="onPaste"
      @keydown.enter.exact.prevent="submit"
    ></textarea>

    <div class="mt-1 flex items-center justify-end gap-2">
      <label class="mr-auto cursor-pointer rounded-full border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:text-slate-200">
        图片
        <input type="file" accept="image/*" multiple class="hidden" @change="onPick" />
      </label>
      <button
        v-if="streaming"
        class="rounded-full bg-red-900 px-3 py-1 text-sm text-red-200"
        @click="emit('stop')"
      >
        停止
      </button>
      <button
        v-else
        class="rounded-full bg-blue-600 px-3 py-1 text-sm text-white"
        @click="submit"
      >
        发送
      </button>
    </div>
  </div>
</template>
```

- [ ] **Step 6: 右栏三个面板与左栏历史**

`web/src/components/chat/ChatParamsPanel.vue`（**不用 props**：面板直接读写 store 的 `params`，避免通过 prop 引用改嵌套对象触发 `vue/no-mutating-props`）：

```vue
<script setup name="ChatParamsPanel">
import { useChatStore } from '@/stores/chat';

const chat = useChatStore();
</script>

<template>
  <div class="mb-2 rounded border border-slate-800 p-2">
    <b class="mb-1 block text-xs uppercase tracking-wide text-slate-400">推理参数（下次发送生效）</b>
    <label class="text-xs text-slate-500">temperature {{ chat.params.temperature.toFixed(2) }}</label>
    <input v-model.number="chat.params.temperature" type="range" min="0" max="2" step="0.05" class="mb-1 w-full" />
    <label class="text-xs text-slate-500">top_p {{ chat.params.top_p.toFixed(2) }}</label>
    <input v-model.number="chat.params.top_p" type="range" min="0" max="1" step="0.05" class="mb-1 w-full" />
    <label class="text-xs text-slate-500">max_tokens</label>
    <input v-model.number="chat.params.max_tokens" type="number" min="1" class="mb-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
    <label class="mt-1 block text-xs text-slate-500">system prompt</label>
    <textarea v-model="chat.params.system" rows="2" class="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm"></textarea>
    <label class="mt-1 flex items-center gap-1 text-xs text-slate-500">
      <input v-model="chat.params.includeUsage" type="checkbox" />
      请求 usage（个别引擎不认 stream_options 时可关）
    </label>
  </div>
</template>
```

`web/src/components/chat/ChatStatsPanel.vue`：

```vue
<script setup name="ChatStatsPanel">
import { computed } from 'vue';

const props = defineProps({ msg: { type: Object, default: null } });

const tps = computed(() => {
  const m = props.msg;
  if (!m || !m.usage || !m.totalMs || m.totalMs <= 0) return null;
  return (m.usage.completion_tokens / (m.totalMs / 1000)).toFixed(1);
});
</script>

<template>
  <div class="rounded border border-slate-800 p-2 text-xs">
    <b class="mb-1 block uppercase tracking-wide text-slate-400">统计 / 落点</b>
    <template v-if="msg">
      <div class="flex justify-between"><span class="text-slate-500">实际落到</span><span class="text-slate-200">{{ msg.routedTo || '-' }}</span></div>
      <div class="flex justify-between"><span class="text-slate-500">路由原因</span><span class="text-slate-200">{{ msg.routeReason || '直连命中' }}</span></div>
      <div class="flex justify-between"><span class="text-slate-500">TTFT</span><span class="text-slate-200">{{ msg.ttftMs != null ? (msg.ttftMs / 1000).toFixed(2) + 's' : '-' }}</span></div>
      <div class="flex justify-between"><span class="text-slate-500">总耗时</span><span class="text-slate-200">{{ msg.totalMs != null ? (msg.totalMs / 1000).toFixed(1) + 's' : '-' }}</span></div>
      <div class="flex justify-between"><span class="text-slate-500">tok/s</span><span class="text-slate-200">{{ tps ?? '-' }}</span></div>
      <div class="flex justify-between"><span class="text-slate-500">prompt / completion</span><span class="text-slate-200">{{ msg.usage ? `${msg.usage.prompt_tokens}/${msg.usage.completion_tokens}` : '无 usage' }}</span></div>
    </template>
    <div v-else class="text-slate-600">发送后显示</div>
  </div>
</template>
```

`web/src/components/chat/ChatRawPanel.vue`：

```vue
<script setup name="ChatRawPanel">
import { ref } from 'vue';

const props = defineProps({
  raw: { type: String, default: '' },
  payload: { type: Object, default: null },
});
const copied = ref(false);

function copy() {
  const t = JSON.stringify(props.payload || {}, null, 2) + '\n\n' + (props.raw || '');
  navigator.clipboard?.writeText(t);
  copied.value = true;
  setTimeout(() => (copied.value = false), 1200);
}
</script>

<template>
  <div class="mt-2 rounded border border-slate-800 p-2 text-xs">
    <div class="mb-1 flex items-center justify-between">
      <b class="uppercase tracking-wide text-slate-400">原文</b>
      <button class="text-slate-500 hover:text-slate-300" @click="copy">{{ copied ? '已复制' : '复制' }}</button>
    </div>
    <pre class="max-h-40 overflow-auto rounded bg-slate-950 p-2 font-mono text-[11px] leading-snug text-sky-300">{{ payload ? JSON.stringify(payload, null, 2) : '—' }}</pre>
    <pre v-if="raw" class="mt-1 max-h-32 overflow-auto rounded bg-slate-950 p-2 font-mono text-[11px] leading-snug text-sky-300">{{ raw }}</pre>
  </div>
</template>
```

`web/src/components/chat/ChatHistoryList.vue`：

```vue
<script setup name="ChatHistoryList">
const props = defineProps({
  sessions: { type: Array, default: () => [] },
  activeId: { type: String, default: '' },
  degraded: { type: Boolean, default: false },
});
const emit = defineEmits(['select', 'new']);
</script>

<template>
  <div class="w-40 shrink-0 overflow-y-auto border-r border-slate-800 bg-slate-900/60 p-2">
    <div class="mb-2 flex items-center justify-between">
      <b class="text-xs uppercase tracking-wide text-slate-400">历史（本机）</b>
      <button class="text-xs text-blue-400 hover:text-blue-300" @click="emit('new')">新建</button>
    </div>
    <div
      v-for="s in sessions"
      :key="s.id"
      :class="[
        'mb-1 cursor-pointer truncate rounded px-2 py-1 text-xs',
        s.id === activeId ? 'bg-slate-800 text-slate-100' : 'text-slate-500 hover:bg-slate-800/60',
      ]"
      :title="`${s.title} · ${s.model} · ${s.createdAt}`"
      @click="emit('select', s.id)"
    >
      {{ s.title }}
    </div>
    <div v-if="!sessions.length" class="text-xs text-slate-600">暂无历史</div>
    <div v-if="degraded" class="mt-2 rounded bg-amber-500/10 p-2 text-[11px] text-amber-400">
      本地存储已满，已清理最旧会话的图片以腾出空间。
    </div>
  </div>
</template>
```

- [ ] **Step 7: 视图 `views/chat/index.vue`（三栏骨架）**

新建 `web/src/views/chat/index.vue`：

```vue
<script setup name="chat">
import { computed, onMounted, ref } from 'vue';
import { listModels } from '@/api/models';
import type { ModelInfo } from '@/api/types';
import { flattenGroups, pickRunnableModels } from '@/utils/chatModels';
import { useChatStore } from '@/stores/chat';
import ChatMessage from '@/components/chat/ChatMessage.vue';
import ChatComposer from '@/components/chat/ChatComposer.vue';
import ChatParamsPanel from '@/components/chat/ChatParamsPanel.vue';
import ChatStatsPanel from '@/components/chat/ChatStatsPanel.vue';
import ChatRawPanel from '@/components/chat/ChatRawPanel.vue';
import ChatHistoryList from '@/components/chat/ChatHistoryList.vue';

const chat = useChatStore();
const models = ref<ModelInfo[]>([]);
const loading = ref(false);
const lastPayload = ref(null);

const runnable = computed(() => pickRunnableModels(models.value));
const lastAssistant = computed(() => {
  const list = chat.messages;
  for (let i = list.length - 1; i >= 0; i -= 1) if (list[i].role === 'assistant') return list[i];
  return null;
});

async function refresh() {
  loading.value = true;
  try {
    models.value = flattenGroups(await listModels());
    if (!chat.model && runnable.value.length) chat.setModel(runnable.value[0].name);
  } finally {
    loading.value = false;
  }
}

async function onSend(text, images) {
  lastPayload.value = { model: chat.active?.model, route_mode: chat.routeMode, messages: [{ role: 'user', content: text }] };
  await chat.send(text, images);
}

onMounted(refresh);
</script>

<template>
  <div class="flex h-full min-h-0">
    <ChatHistoryList
      :sessions="chat.sessions"
      :active-id="chat.activeId"
      :degraded="chat.storageDegraded"
      @select="chat.selectSession"
      @new="chat.newSession"
    />

    <div class="flex min-w-0 flex-1 flex-col">
      <!-- 顶栏：模型选择 + 走网关开关 -->
      <div class="flex items-center gap-2 border-b border-slate-800 bg-slate-900 px-3 py-2 text-sm">
        <select
          :value="chat.model"
          class="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100"
          @change="chat.setModel(($event.target).value)"
        >
          <option v-if="!runnable.length" value="">（无已启动且健康的模型）</option>
          <option v-for="m in runnable" :key="m.name" :value="m.name">
            {{ m.name }} · {{ m.engine }}{{ m.vision ? ' · 视觉' : '' }}
          </option>
        </select>
        <button
          :class="[
            'rounded-full border px-2 py-1 text-xs',
            chat.routeMode === 'gateway'
              ? 'border-blue-500 bg-blue-600/15 text-blue-300'
              : 'border-slate-700 text-slate-400',
          ]"
          :title="chat.routeMode === 'gateway' ? '复现网关家族路由/上下文切换' : '选谁打谁'"
          @click="chat.routeMode = chat.routeMode === 'gateway' ? 'direct' : 'gateway'"
        >
          走网关：{{ chat.routeMode === 'gateway' ? '开' : '关' }}
        </button>
        <button class="ml-auto text-xs text-slate-400 hover:text-slate-200" :disabled="loading" @click="refresh">
          {{ loading ? '刷新中…' : '刷新模型' }}
        </button>
      </div>

      <!-- 对话流 -->
      <div class="flex flex-1 flex-col gap-2 overflow-y-auto p-3">
        <div v-if="!chat.active" class="m-auto text-sm text-slate-500">点右上角「新建」开始对话</div>
        <ChatMessage v-for="m in chat.messages" :key="m.id" :msg="m" />
      </div>

      <ChatComposer :streaming="chat.streaming" @send="onSend" @stop="chat.stop()" />
    </div>

    <!-- 调试面板 -->
    <div class="w-72 shrink-0 overflow-y-auto border-l border-slate-800 bg-slate-900/60 p-2 text-slate-300">
      <ChatParamsPanel />
      <ChatStatsPanel :msg="lastAssistant" />
      <ChatRawPanel :raw="chat.rawText" :payload="lastPayload" />
    </div>
  </div>
</template>
```

- [ ] **Step 8: 类型检查与单测全绿**

Run: `cd web; npx vue-tsc --noEmit; npm test`
Expected: 无类型错误；vitest 全 PASS（含既有 `client.test.ts` / `router.test.ts` / `sse.test.ts`）。

- [ ] **Step 9: 提交**

```bash
git add web/src/utils/chatModels.ts web/src/utils/chatModels.test.ts web/src/views/chat web/src/components/chat
git commit -m "feat(web): add AI chat debugger view with three-pane layout and debug panels"
```

---

### Task 7: 接入导航与路由

**Files:**
- Modify: `web/src/components/layout/Sidebar.vue`（`menus` L13-26 + 图标分支 L58-85）
- Modify: `web/src/router/index.ts`（Layout `children` L30-120）
- Test: `web/src/router/router.test.ts`（追加一条断言）

**Interfaces:**
- Consumes: Task 6 的 `@/views/chat/index.vue`（组件 name 必须是 `chat`）。
- Produces: 路由 `/chat`，`route.name === 'chat'`。

- [ ] **Step 1: 写路由失败测试**

在 `web/src/router/router.test.ts` 内追加（沿用该文件已有的 router 导入方式；若无 router 实例导入，则用 `routes` 源断言）：

```ts
it('/chat 挂在 Layout 下且 name=chat', () => {
  const layout = routes.find((r) => r.path === '/');
  const chat = layout?.children?.find((c) => c.path === 'chat');
  expect(chat).toBeTruthy();
  expect(chat?.name).toBe('chat');
  expect(chat?.meta?.title).toBe('AI 对话');
});
```

> `routes` 未从 `@/router` 导出时，先把 `router/index.ts` 的 `const routes` 改为 `export const routes`（既有用例不受影响），再跑测试。

- [ ] **Step 2: 运行确认失败**

Run: `cd web; npm test -- router`
Expected: FAIL — 找不到 `chat` 子路由

- [ ] **Step 3: 加路由**

`web/src/router/index.ts`：`const routes` 改为 `export const routes`；在 Layout `children` 内 `settings` 之后插入：

```ts
      {
        // AI 对话（模型调试台）：管理面 Bearer 鉴权，同 Layout 内其它资源路由
        path: 'chat',
        name: 'chat',
        component: () => import('@/views/chat/index.vue'),
        meta: { title: 'AI 对话' },
      },
```

- [ ] **Step 4: 加导航项与图标**

`Sidebar.vue` 的 `menus` 中，在 `{ to: '/services', … }` 之后插入（放在服务/模型附近，符合"调试"心智）：

```ts
  { to: '/chat', label: 'AI 对话', icon: 'chat' },
```

在图标区 `<!-- probe -->` 分支之前插入一段：

```html
          <!-- chat：对话气泡 + 火花（调试台） -->
          <template v-else-if="m.icon === 'chat'"><svg class="size-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.7 8.7 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 8.5-8.5 8.38 8.38 0 0 1 8.5 8.5z" /><path d="M12 8.5l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9z" /></svg></template>
```

- [ ] **Step 5: 运行确认通过**

Run: `cd web; npm test -- router; npx vue-tsc --noEmit`
Expected: PASS 且无类型错误

- [ ] **Step 6: 提交**

```bash
git add web/src/components/layout/Sidebar.vue web/src/router/index.ts web/src/router/router.test.ts
git commit -m "feat(web): register AI chat debugger route and sidebar entry"
```

---

### Task 8: 端到端验证 + 问题沉淀

**Files:**
- Create: `docs/known-pitfalls/frontend/ai-chat-debugger.md`（若过程中踩坑）
- Modify: `docs/known-pitfalls/README.md`（追加索引条目）

- [ ] **Step 1: 全量后端测试**

Run: `python -m pytest tests -q`
Expected: 全 PASS（重点确认 `test_gateway*.py`、`test_audit.py`、`test_accounts_gateway.py` 无回归）。

- [ ] **Step 2: 全量前端测试 + 构建**

Run: `cd web; npm test; npm run build`
Expected: vitest 全 PASS；`vite build` 成功产出 `../dist`。

- [ ] **Step 3: 手工验证清单（起 webui 后逐项过）**

启动：`python -m modelctl.core.webui.server`（或既有 `modelctl webui` 命令），浏览器登录后进 `/chat`。

- [ ] 4.1 模型下拉只出现 `state=running && health=healthy` 的模型；未启动的模型不出现。
- [ ] 4.2 关掉全部模型后下拉显示占位文案；此时直接构造请求（curl 带 Bearer）→ `409 model_not_running`。
- [ ] 4.3 发一条消息：正文逐字流式出现；带 thinking 的模型（如 qwen3.8）reasoning 进折叠区、正文不被思考过程占满。
- [ ] 4.4 右栏统计出现 TTFT / 总耗时 / tok/s / prompt·completion；「实际落到」= 所选模型（direct 模式）。
- [ ] 4.5 打开「走网关」，若该模型属于家族且有上下文切换规则，右栏「路由原因」显示 `group_route` / `context_switch`，落点为切换后的 profile 名。
- [ ] 4.6 点「停止」：流中断、气泡保留已收内容并标「已中断」；后端日志无残留上游连接告警。
- [ ] 4.7 给纯文本模型发图：右栏出现上游 4xx 原文（如 vLLM 的 image 数量限制），且「实际落到」仍显示目标模型。
- [ ] 4.8 刷新页面：历史会话仍在（localStorage）；发若干张大图后触发降级提示。
- [ ] 4.9 时间显示为 `YYYY-MM-DD HH:mm:ss`；UI 无真实 ID 泄漏。

- [ ] **Step 4: 按 CLAUDE.md 规范沉淀问题（仅在确有踩坑时）**

把过程中定位到的非显然问题写入 `docs/known-pitfalls/frontend/ai-chat-debugger.md`，每个问题一个 `## <标题>` 条目（根因 / 解决方案 / 代码片段），并在 `docs/known-pitfalls/README.md` 索引表加一行（标题、分类、日期、一句话描述）。若本节无踩坑，跳过 Step 4 并在提交信息里注明无坑沉淀。

- [ ] **Step 5: 提交**

```bash
git add docs/known-pitfalls
git commit -m "docs(known-pitfalls): record AI chat debugger findings"
```

---

## Self-Review

**1. Spec coverage**

| Spec 章节 | 实现 Task |
| --- | --- |
| §3 架构与双 route_mode | Task 1（抽取）+ Task 3（端点分流） |
| §4.1 `POST /admin/api/chat/completions`（鉴权/白名单/409/413/SSE/落点头） | Task 3 |
| §4.1 模型选择器复用 `/models` + `vision` | Task 2 |
| §4.2 `prepare_openai_upstream` + `app.state` | Task 1 |
| §5 文件结构与组件划分 | Task 6 |
| §5 依赖（markdown-it/dompurify/highlight.js） | Task 4 |
| §5.1 流式解析与统计（畸形帧、usage、reasoning、TTFT） | Task 5 |
| §5.2 停止与错误分层 | Task 5（store）+ Task 6（UI 红块/已中断） |
| §5.3 历史与容量降级 | Task 5（`persist`）+ Task 6（`ChatHistoryList` 提示） |
| §6 多模态软徽章、不禁用 | Task 2（后端字段）+ Task 6（下拉 `· 视觉`） |
| §7 测试计划 | Task 1/2/3/4/5/6 各自 TDD 步骤 |
| §8 风险（proxy 等价性、重渲染、`stream_options` 兼容） | Task 1 Step 6 回归；Task 3 `include_usage` 开关；Task 6 `ChatMessage` 用 `computed` 缓存渲染 |
| 导航与路由接入 | Task 7 |

无遗漏项。

**2. Placeholder scan**

已复核：全文无 TBD / TODO / "适当处理" / "类似 Task N" / 未定义符号引用。Task 8 Step 4 明确「无踩坑则跳过」，属有意可选项而非占位。

**3. Type consistency**

- `prepare_openai_upstream(registry, groups, group_cache, default_model, context_rules, body, path)` 七参：Task 1 定义、Task 3 两处调用（direct 用 `{model: target}, {}, None, None, {}`，gateway 用 `app.state` 五件套）参数顺序一致。
- `PreparedUpstream.{target, body, headers, url, route_reason}`：Task 1 定义，Task 3 读 `prepared.target.name / .route_reason / .url / .body / .headers` 一致。
- `app.state.gateway_read_timeout`：Task 1 Step 7 挂载，Task 3 用 `getattr(state, 'gateway_read_timeout', 600.0)` 读取。
- store 暴露名：Task 5 定义 `sessions/activeId/active/messages/model/routeMode/params/streaming/rawText/storageDegraded/setModel/newSession/selectSession/send/stop/persist`；Task 6 视图与 Task 7 用到的名字逐一核对无出入。
- `ChatMsg` 字段（Task 5 定义）与 Task 6 `ChatMessage.vue` / `ChatStatsPanel.vue` 读取的 `ttftMs/totalMs/usage/routedTo/routeReason/reasoning/error.raw/interrupted/images` 一致。
- `pickRunnableModels` / `flattenGroups`（Task 6 Step 3）与 Step 7 视图 import 路径 `@/utils/chatModels` 一致。
