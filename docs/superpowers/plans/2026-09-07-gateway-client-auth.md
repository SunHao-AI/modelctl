# 网关与 nginx 数据面客户端鉴权 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 modelctl 网关 `/v1*` 全部端点与 nginx 数据面加上强制客户端鉴权（fail-closed），并配套限流、真实 IP、TLS 与引擎 key 下发补齐。

**Architecture:** 三层纵深——nginx（TLS + 限流 + map 常量凭据比对，覆盖网关与模型直连两条路径）→ 网关 `:5003`（处理器首行调用 `verify_client`，OpenAI 错误信封 401）→ 引擎原生 `--api-key`。客户端 key 只用于准入，验完即丢，上游认证仍用 profile key。

**Tech Stack:** Python 3.10+ / FastAPI / httpx / pytest；nginx 1.x（map + limit_req + ssl）；openssl。

**Spec:** `docs/superpowers/specs/2026-09-07-gateway-client-auth-design.md`

## Global Constraints

- 严禁对任何数据库执行 DDL；本计划无数据库改动。
- 后端遵循 PEP 8，统一 try-except + logger 记录异常。
- 时间格式 `YYYY-MM-DD HH:mm:ss`；审计 `ts` 沿用现有 ISO8601 毫秒格式，不改。
- 凡可能含 CJK 的终端/日志输出，用 `display_width` + `pad_width` 对齐，禁 `len()`/`f"{x:<N}"`。本计划的日志均为 ASCII 键值对，若新增含中文的对齐输出须遵守此条。
- 每次修复 UI/样式或修 Bug 后，须按渐进式披露沉淀到 `docs/known-pitfalls/`（Task 10）。
- 新密钥**不得复用**已在日志中明文泄露的 `fly@@see`。
- 密钥明文**绝不写入审计日志**，只记结果标签。
- 测试命令：`python -m pytest tests/<file> -v`（PowerShell，不支持 `&&`，多命令用 `;`）。

---

### Task 1: 网关凭据校验函数

**Files:**
- Modify: `src/modelctl/core/gateway.py`（在 `GATEWAY_PORT = 5003` 常量区之后、`GatewayModel` 之前插入）
- Test: `tests/test_gateway.py`（文件末尾追加）

**Interfaces:**
- Consumes: 无（本任务自包含）
- Produces:
  - `GATEWAY_CLIENT_KEY_ENV: str = "GATEWAY_CLIENT_API_KEY"`
  - `client_api_key() -> str`（读 env，首次触发 `load_env`；未配置返回 `""`）
  - `verify_client(request) -> str`（通过返回 `"ok"`；失败返回 `"missing"` / `"invalid"` / `"unconfigured"` 之一，**不抛异常**，由调用方据此构造 401）
  - `AUTH_OK: str = "ok"`
  - `auth_error_response(label: str) -> "JSONResponse"`（401 + OpenAI 错误信封）
  - `client_ip_of(request) -> str`

- [ ] **Step 1: 写失败测试**

在 `tests/test_gateway.py` 末尾追加（该文件用 pytest-asyncio 自动异步模式，测试函数直接 `async def`；同步测试也可用 `_run(...)`）：

```python
# ---------- 客户端鉴权（GATEWAY_CLIENT_API_KEY，fail-closed） ----------

_CLIENT_KEY = "sk-test-client-key-9f3a"


def test_verify_client_bearer_ok(monkeypatch):
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", _CLIENT_KEY)
    req = MagicMock()
    req.headers = {"authorization": f"Bearer {_CLIENT_KEY}"}
    assert verify_client(req) == "ok"


def test_verify_client_xapikey_ok(monkeypatch):
    """Anthropic 客户端（Trae CN 内置 Claude SDK）只带 x-api-key，必须认。"""
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", _CLIENT_KEY)
    req = MagicMock()
    req.headers = {"x-api-key": _CLIENT_KEY}
    assert verify_client(req) == "ok"


def test_verify_client_missing(monkeypatch):
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", _CLIENT_KEY)
    req = MagicMock()
    req.headers = {}
    assert verify_client(req) == "missing"


def test_verify_client_invalid(monkeypatch):
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", _CLIENT_KEY)
    req = MagicMock()
    req.headers = {"authorization": "Bearer wrong-key"}
    assert verify_client(req) == "invalid"


def test_verify_client_non_ascii_rejected(monkeypatch):
    """compare_digest 对非 ASCII str 抛 TypeError，必须干净拒绝而非 500。"""
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", _CLIENT_KEY)
    req = MagicMock()
    req.headers = {"authorization": "Bearer 密钥"}
    assert verify_client(req) == "invalid"


def test_verify_client_fail_closed_when_unconfigured(monkeypatch):
    """fail-closed：服务端未配 key 时一律拒绝，不得放行匿名请求。

    用 setenv("") 而非 delenv：load_env 是 setdefault 语义，若本地 .env 恰好配了
    该键，delenv 后首次 client_api_key() 会把它读回来，用例将随环境随机失败。
    """
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", "")
    monkeypatch.setenv("API_KEY", _CLIENT_KEY)  # 管理面 key 不得被复用
    req = MagicMock()
    req.headers = {"authorization": f"Bearer {_CLIENT_KEY}"}
    assert verify_client(req) == "unconfigured"


def test_client_ip_prefers_xff_then_xrealip_then_socket():
    req = MagicMock()
    req.headers = {"x-forwarded-for": "1.2.3.4, 10.0.0.1"}
    req.client = None
    assert client_ip_of(req) == "1.2.3.4"
    req.headers = {"x-real-ip": "5.6.7.8"}
    assert client_ip_of(req) == "5.6.7.8"
    req.headers = {}
    req.client = MagicMock(host="9.9.9.9")
    assert client_ip_of(req) == "9.9.9.9"
```

同时在该文件顶部的 `from modelctl.core.gateway import (...)` 块中补上 `client_api_key, client_ip_of, verify_client`（保持字母序，`MagicMock` 已导入）。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_gateway.py -k "verify_client or client_ip" -v`
Expected: FAIL — `ImportError: cannot import name 'verify_client'`

- [ ] **Step 3: 实现**

在 `src/modelctl/core/gateway.py` 顶部 import 区补 `import hmac`（与既有 `import json`/`import os` 同组）。

在 `GATEWAY_PORT = 5003` 所在常量块末尾（`ENGINE_PRIORITY` 之后）插入：

```python
# ---------- 数据面客户端鉴权（详见 spec 2026-09-07-gateway-client-auth） ----------
# 与管理面 API_KEY 严格隔离：API_KEY 能改配置/启停模型，绝不可下发给外部推理客户端。
GATEWAY_CLIENT_KEY_ENV = "GATEWAY_CLIENT_API_KEY"

# 校验结果标签：写入审计的 auth 字段，只记结果，绝不记 key 值或片段
AUTH_OK = "ok"
AUTH_MISSING = "missing"
AUTH_INVALID = "invalid"
AUTH_UNCONFIGURED = "unconfigured"

# .env 懒加载标记：webui 同 app 挂载 /v1 时不保证已 load_env（与 webui.admin_auth 同范式）
_client_key_env_loaded: bool = False


def client_api_key() -> str:
    """网关客户端密钥；未配置/为空返回 ""（调用方须按 fail-closed 处理）。"""
    global _client_key_env_loaded
    if not _client_key_env_loaded:
        try:
            load_env()
        except Exception:  # noqa: BLE001 — 加载失败走"未配置"分支，保持 401 路径
            pass
        _client_key_env_loaded = True
    return os.environ.get(GATEWAY_CLIENT_KEY_ENV) or ""


def verify_client(request) -> str:
    """数据面准入校验：通过返回 AUTH_OK，否则返回失败标签（不抛异常）。

    双通道嗅探（CLAUDE.md「嗅探请求头，不能强要求 Bearer」首次落地）：
    Authorization: Bearer <key> 或 x-api-key: <key> 任一命中即通过——Anthropic
    协议客户端（Trae CN 内置 Claude SDK）只带 x-api-key，只认 Bearer 会打挂 /v1/messages。
    比较用 hmac.compare_digest 恒定时间防时序泄露，bytes 形式使非 ASCII 输入干净拒绝。
    """
    expected = client_api_key()
    if not expected:
        return AUTH_UNCONFIGURED
    auth = request.headers.get("authorization") or ""
    candidate = ""
    if auth.lower().startswith("bearer "):
        candidate = auth[7:].strip()
    if not candidate:
        candidate = (request.headers.get("x-api-key") or "").strip()
    if not candidate:
        return AUTH_MISSING
    try:
        ok = hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
    except TypeError:  # 防御：非 ASCII 等异常输入一律视为不匹配
        ok = False
    return AUTH_OK if ok else AUTH_INVALID


def auth_error_response(label: str):
    """401 响应：OpenAI 错误信封，保证 OpenAI/Anthropic SDK 能正常解析并抛客户端异常。"""
    from fastapi.responses import JSONResponse

    message = {
        AUTH_UNCONFIGURED: "gateway client API key not configured",
        AUTH_MISSING: "missing API key: send 'Authorization: Bearer <key>' or 'x-api-key: <key>'",
    }.get(label, "invalid API key")
    return JSONResponse(status_code=401, content={"error": {"message": message, "type": "authentication_error"}})


def client_ip_of(request) -> str:
    """真实来源 IP：nginx 已补 X-Real-IP / X-Forwarded-For，无前置代理时退回 socket。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or ""
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_gateway.py -k "verify_client or client_ip" -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add src/modelctl/core/gateway.py tests/test_gateway.py
git commit -m "feat(gateway): 新增数据面客户端凭据校验函数（fail-closed，双通道嗅探）"
```

---

### Task 2: 三个 `/v1` 处理器接入鉴权

**Files:**
- Modify: `src/modelctl/core/gateway.py`（`list_models` L526、`anthropic_proxy` L565、`proxy` L764）
- Modify: `tests/test_gateway.py`（辅助函数 `_post` / `_get` / `_post_headers` + 新增鉴权用例）

**Interfaces:**
- Consumes: Task 1 的 `verify_client` / `auth_error_response` / `AUTH_OK`
- Produces: `/v1*` 全部端点在凭据非法时返回 401；`create_app` 无签名变更

**注意：本任务是破坏性变更**——裸 `POST /v1` 连通性探测从 200 变 401（spec §9 裁决 2 已确认）。

- [ ] **Step 1: 先改测试辅助层并改写受影响的既有用例**

`tests/test_gateway.py` 中三个辅助函数默认注入合法 key（`_CLIENT_KEY` 已在 Task 1 定义）。整体替换：

```python
def _auth_headers() -> dict:
    """默认客户端凭据：/v1* 已强制鉴权，测试统一带合法 key。"""
    return {"Authorization": f"Bearer {_CLIENT_KEY}"}


async def _post(app, path: str, json: dict | None = None, headers: dict | None = None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers or _auth_headers()
    ) as client:
        return await client.post(path, json=json or {})


async def _post_headers(app, path: str, json: dict | None = None, headers: dict | None = None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers or {}
    ) as client:
        return await client.post(path, json=json or {})


async def _get(app, path: str, headers: dict | None = None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers or _auth_headers()
    ) as client:
        return await client.get(path)
```

删除原文件 L77-79 处旧版 `_get` 定义（已上移合并，避免重复定义覆盖）。

再给 autouse fixture 补上客户端 key，使全部既有用例无需逐个改动即可通过：

```python
@pytest.fixture(autouse=True)
def _isolate_cache_dir(monkeypatch, tmp_path):
    """Test 全局隔离 PID 命名空间 + 注入网关客户端 key：避免 is_running_any 走假 PID 命中，
    同时使 /v1* 用例默认携带合法凭据（fail-closed 后无 key 一律 401）。"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", _CLIENT_KEY)
```

`_CLIENT_KEY` 常量在 Task 1 位于文件末尾，需**上移到 `_isolate_cache_dir` 之前**（fixture 引用它）。

`test_bare_v1_returns_ok_not_redirect` 语义变更，整体替换（原断言"裸 /v1 返回 200"仍成立，但现在是带 key 的 200；额外断言无 key 时 401）：

```python
def test_bare_v1_returns_ok_not_redirect():
    """裸 /v1（无尾斜杠）不得 307 重定向：FastAPI redirect_slashes 的 Location 是
    根相对路径 /v1/，经 B 机 nginx 前缀路由后客户端跟随会丢 /<node>/llm 前缀落空。
    裸 /v1 视为连通性探测，带合法凭据返回 200；无凭据一律 401（fail-closed）。"""
    reg = {"qwen3.8": GatewayModel("qwen3.8", "ollama", "http://upstream", "qwen3.8:27b", None, "http://upstream/")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    for path in ("/v1", "/v1/"):
        resp = _run(_post(app, path))
        assert resp.status_code == 200
        assert resp.headers.get("location") is None  # 不重定向
        assert resp.json()["status"] == "ok"
        # 行为变更点：无凭据的连通性探测不再放行（通知依赖 baseUrl 探测的客户端）
        assert _run(_post_headers(app, path, headers={})).status_code == 401
```

- [ ] **Step 2: 写新增鉴权用例（追加到文件末尾）**

```python
def test_v1_chat_completions_requires_key():
    reg = {"qwen3.8": GatewayModel("qwen3.8", "ollama", "http://upstream", "qwen3.8:27b", None, "http://upstream/")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    resp = _run(_post_headers(app, "/v1/chat/completions", json={"messages": []}, headers={}))
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"


def test_v1_wrong_key_rejected():
    reg = {"qwen3.8": GatewayModel("qwen3.8", "ollama", "http://upstream", "qwen3.8:27b", None, "http://upstream/")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    resp = _run(_post_headers(app, "/v1/chat/completions", json={"messages": []},
                              headers={"Authorization": "Bearer nope"}))
    assert resp.status_code == 401
    assert "invalid API key" in resp.json()["error"]["message"]


def test_v1_models_requires_key():
    reg = {"qwen3.8": GatewayModel("qwen3.8", "ollama", "http://upstream", "qwen3.8:27b", None, "http://upstream/")}
    app = create_app(reg, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert _run(_get(app, "/v1/models", headers={})).status_code == 401


def test_v1_messages_accepts_x_api_key(monkeypatch):
    """Anthropic 客户端只带 x-api-key：准入通过，且上游收到的仍是 profile key（覆盖逻辑不变）。"""
    captured = {}

    def upstream(request):
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"id": "1", "type": "message", "content": []})

    reg = {"qwen3.8": GatewayModel("qwen3.8", "vllm", "http://upstream", "qwen3.8", "profile-key-1", "http://upstream/")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(upstream))
    resp = _run(_post_headers(app, "/v1/messages", json={"model": "qwen3.8", "messages": [], "max_tokens": 8},
                              headers={"x-api-key": _CLIENT_KEY}))
    assert resp.status_code == 200
    assert captured["headers"].get("authorization") == "Bearer profile-key-1"
    assert captured["headers"].get("x-api-key") == "profile-key-1"


def test_v1_all_endpoints_fail_closed_when_unconfigured(monkeypatch):
    """服务端未配 key：即使带管理面 API_KEY 也全部 401，message 指向网关未配置。"""
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", "")  # 非 delenv，理由见 Task 1 同名说明
    reg = {"qwen3.8": GatewayModel("qwen3.8", "ollama", "http://upstream", "qwen3.8:27b", None, "http://upstream/")}
    app = create_app(reg, default_model="qwen3.8", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    for path, method in (("/v1/models", "GET"), ("/v1/chat/completions", "POST"), ("/v1", "POST")):
        call = _get if method == "GET" else _post
        resp = _run(call(app, path, headers={"Authorization": f"Bearer {_CLIENT_KEY}"}))
        assert resp.status_code == 401, path
        assert "not configured" in resp.json()["error"]["message"]
```

- [ ] **Step 3: 运行确认失败**

Run: `python -m pytest tests/test_gateway.py -k "requires_key or wrong_key or accepts_x_api or unconfigured or bare_v1" -v`
Expected: FAIL — 新增用例得到 200 而非 401（`verify_client` 已存在但尚未接入处理器）

- [ ] **Step 4: 接入三个处理器**

`src/modelctl/core/gateway.py`：

`list_models`（L526 起）签名加 `request: Request`，函数体首行校验：

```python
    @app.get("/v1/models")
    async def list_models(request: Request) -> dict | "JSONResponse":
        label = verify_client(request)
        if label != AUTH_OK:
            return auth_error_response(label)
        # 注册表同时含 name 与 alias 两个 key（指向同一 GatewayModel），须按 name 去重
```

`anthropic_proxy`（L565 起）函数体首行（`try: body = await request.json()` 之前）插入：

```python
        label = verify_client(request)
        if label != AUTH_OK:
            logger.warning(f"Anthropic 请求被拒 auth={label} ip={client_ip_of(request)}")
            return auth_error_response(label)
```

`proxy`（L765 起）在 `if not path:` 之前插入（注意：必须在裸 `/v1` 短路返回**之前**，否则裸探测仍匿名可通）：

```python
    async def proxy(request: Request, path: str = ""):
        label = verify_client(request)
        if label != AUTH_OK:
            logger.warning(f"网关请求被拒 path=/v1/{path} auth={label} ip={client_ip_of(request)}")
            return auth_error_response(label)
        if not path:
            return JSONResponse(status_code=200, content={"status": "ok"})
```

同文件把 `proxy` / `list_models` 内既有的 `f"auth={'Authorization' in request.headers} ..."` 埋点（L591、L787）改为记录结果标签：`auth={label}`（`anthropic_proxy` 用其自身变量），使日志能区分通过与被拒。

- [ ] **Step 5: 跑全量网关测试**

Run: `python -m pytest tests/test_gateway.py tests/test_gateway_context_switch.py tests/test_audit.py -v`
Expected: 全部 PASS。若 `test_audit.py` 有用例走 `/v1` 且未带 key，按 Task 2 Step 1 同法给其请求补 `Authorization` 头（该文件用 `client.post(...)`，加 `headers={"Authorization": f"Bearer {key}"}` 并在用例内 `monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", key)`）。

- [ ] **Step 6: 提交**

```bash
git add src/modelctl/core/gateway.py tests/test_gateway.py tests/test_audit.py
git commit -m "feat(gateway): /v1 全部端点强制客户端鉴权（fail-closed，401 OpenAI 信封）"
```

---

### Task 3: 审计字段 `auth` / `client_ip`

**Files:**
- Modify: `src/modelctl/core/gateway.py`（`_build_audit_entry` L213-265 + 4 处调用点 L685/L733/L949/L1026）
- Modify: `src/modelctl/core/webui/admin_audit.py`（过滤链 L142-188）
- Test: `tests/test_audit.py`

**Interfaces:**
- Consumes: Task 1 的 `AUTH_OK` 等标签、`client_ip_of`
- Produces: 审计条目新增 `auth: str`、`client_ip: str`；被拒请求同样落审计

- [ ] **Step 1: 写失败测试**

`tests/test_audit.py` 复用该文件已有的 `_new_audit_log`、`_reg_one()`、`_first_audit_rec(tmp_path)`（读 `<tmp_path>/audit/modelctl-*.jsonl` 最后一行）与 `create_app`，末尾追加。注意该文件顶部未导入 `asyncio` / `httpx` / `create_app` / `_new_audit_log`，需一并补上（`_reg_one` 与 `create_app` 已在文件内定义/导入，按实际位置取用）：

```python
def test_audit_records_rejected_request_with_auth_and_ip(tmp_path, monkeypatch):
    """被拒请求同样落审计；auth 只记标签，明文 key 绝不入审计。"""
    key = "sk-audit-key-123"
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", key)
    audit = _new_audit_log(Path(tmp_path / "audit"))
    app = create_app(
        _reg_one(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": []})),
        audit_log=audit,
    )

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # 不带凭据 → 401，必须落审计
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "q", "messages": [{"role": "user", "content": "hi"}]},
                headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
            )
            assert resp.status_code == 401

    asyncio.run(_go())
    rec = _first_audit_rec(tmp_path)
    assert rec["status_code"] == 401
    assert rec["auth"] == "missing"
    assert rec["client_ip"] == "203.0.113.9"
    assert rec["error"] == "auth_missing"
    assert key not in json.dumps(rec, ensure_ascii=False)  # 明文 key 绝不入审计
    audit.destroy()


def test_audit_passed_request_auth_label_is_ok(tmp_path, monkeypatch):
    """通过校验的请求 auth=ok，client_ip 无代理头时取 socket 或空串（不得缺键）。"""
    key = "sk-audit-key-123"
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", key)
    audit = _new_audit_log(Path(tmp_path / "audit"))
    app = create_app(
        _reg_one(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": []})),
        audit_log=audit,
    )

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "q", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {key}", "X-Real-IP": "198.51.100.4"},
            )
            assert resp.status_code == 200

    asyncio.run(_go())
    rec = _first_audit_rec(tmp_path)
    assert rec["auth"] == "ok"
    assert rec["client_ip"] == "198.51.100.4"
    assert key not in json.dumps(rec, ensure_ascii=False)
    audit.destroy()
```

注意：`GET /v1/models` 不落审计（非推理端点），本任务不改变这一点——只保证 401 的推理请求可追溯。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_audit.py::test_audit_records_rejected_request_with_auth_and_ip tests/test_audit.py::test_audit_passed_request_auth_label_is_ok -v`
Expected: FAIL — 条目无 `auth` 键（`KeyError: 'auth'`）

- [ ] **Step 3: 扩展 `_build_audit_entry`**

签名新增两个关键字参数（放在 `input_char_len` 之后、两个 collector 参数之前，保持必填在前）：

```python
    input_char_len: int,
    auth: str = AUTH_OK,
    client_ip: str = "",
    collector_diff_prompt: int = 0,
    collector_diff_completion: int = 0,
) -> dict:
```

返回 dict 在 `"status_code": status_code,` 之前插入两行：

```python
        "auth": auth,
        "client_ip": client_ip,
```

docstring 补一句：`auth：准入校验结果标签（ok/missing/invalid/unconfigured），绝不记录 key 值；client_ip：nginx 透传的真实来源 IP。`

- [ ] **Step 4: 4 处调用点补参 + 被拒请求落审计**

4 处 `self_audit_log.record(_build_audit_entry(...))` 均补 `auth=label, client_ip=client_ip_of(request),`（`proxy` 与 `anthropic_proxy` 各自作用域已有 `label` 变量，来自 Task 2）。

`proxy` 在鉴权失败分支补落审计（`anthropic_proxy` 同法）。审计此时尚无 target，模型字段用空串：

```python
        if label != AUTH_OK:
            logger.warning(f"网关请求被拒 path=/v1/{path} auth={label} ip={client_ip_of(request)}")
            audit_log.record(_build_audit_entry(
                model_name="", profile_name="", profile_engine="",
                path=path or "v1", stream=False,
                native_metrics=None, usage=None, gateway_metrics=None,
                status_code=401, error=f"auth_{label}", finish_reason=None,
                input_char_len=0, auth=label, client_ip=client_ip_of(request),
            ))
            return auth_error_response(label)
```

注意此处用闭包内的 `audit_log`（`create_app` L492 已定型的局部变量），而非 `target.audit_log`。

- [ ] **Step 5: admin_audit 过滤支持**

`src/modelctl/core/webui/admin_audit.py` 的过滤函数（L142-188 一带）在现有 `status_code` / `error` 判定后追加：

```python
        if auth and entry.get("auth") != auth:
            continue
        if client_ip and entry.get("client_ip") != client_ip:
            continue
```

对应查询端点新增两个可选 query 参数 `auth: str = ""`、`client_ip: str = ""`，透传到过滤函数。

- [ ] **Step 6: 运行确认通过**

Run: `python -m pytest tests/test_audit.py tests/test_gateway.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add src/modelctl/core/gateway.py src/modelctl/core/webui/admin_audit.py tests/test_audit.py
git commit -m "feat(audit): 审计新增 auth/client_ip 字段并记录被拒请求"
```

---

### Task 4: 配置项与 `all_service` 适配

**Files:**
- Modify: `.env.example`（网关段 L82-95）
- Modify: `src/modelctl/core/all_service.py`（`start_gateway` L247-251、`status_gateway` L350-355）
- Test: `tests/test_all_service.py`

**Interfaces:**
- Consumes: Task 1 的 `GATEWAY_CLIENT_KEY_ENV` / `client_api_key`
- Produces: `modelctl status` 在网关开启鉴权后仍报告正常；未配 key 时有明确 warning

- [ ] **Step 1: 写失败测试**

`tests/test_all_service.py` 末尾追加（沿用文件既有 import；`ComponentResult` / `status_gateway` 若未导入则补上）：

```python
def test_status_gateway_sends_client_key(monkeypatch):
    """fail-closed 后 /v1/models 需要凭据，status_gateway 必须带 key，否则误报"无响应"。"""
    seen = {}

    def fake_wait_health(url, timeout, api_key=None, alive_check=None):
        seen["url"] = url
        seen["api_key"] = api_key
        return True

    monkeypatch.setattr("modelctl.core.all_service.is_running", lambda name: True)
    monkeypatch.setattr("modelctl.core.all_service.wait_health", fake_wait_health)
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", "sk-status-key")
    result = status_gateway()
    assert "/v1/models" in seen["url"]
    assert seen["api_key"] == "sk-status-key"
    assert "正常" in result.detail


def test_status_gateway_unconfigured_key_reports_401_expected(monkeypatch):
    """未配 key 时网关必然 401：status 需明确提示"未配置客户端密钥"，而非笼统的"无响应"。"""
    monkeypatch.setattr("modelctl.core.all_service.is_running", lambda name: True)
    monkeypatch.setattr("modelctl.core.all_service.wait_health", lambda *a, **k: False)
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", "")  # 非 delenv，理由见 Task 1 同名说明
    result = status_gateway()
    assert "GATEWAY_CLIENT_API_KEY" in result.detail
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_all_service.py -k status_gateway -v`
Expected: FAIL — `seen["api_key"] is None`

- [ ] **Step 3: 改 `status_gateway`**

`src/modelctl/core/all_service.py` L350-355 整体替换：

```python
def status_gateway() -> ComponentResult:
    port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
    if is_running("llm-gateway"):
        from modelctl.core.gateway import client_api_key

        key = client_api_key()
        if not key:
            return ComponentResult(
                "gateway", "ok",
                "运行中，但未配置 GATEWAY_CLIENT_API_KEY——fail-closed，全部 /v1 请求返回 401",
            )
        ok = wait_health(f"http://127.0.0.1:{port}/v1/models", 3.0, key)
        return ComponentResult("gateway", "ok", "运行中，/v1/models " + ("正常" if ok else "无响应"))
    return ComponentResult("gateway", "ok", "已停止")
```

- [ ] **Step 4: 改 `start_gateway` 启动提醒**

`start_gateway` 中 `logger.info(f"网关已启动（PID {pid}），监听端口 {port}")` **之前**插入：

```python
    from modelctl.core.gateway import GATEWAY_CLIENT_KEY_ENV, client_api_key

    if not client_api_key():
        logger.warning(
            f"{GATEWAY_CLIENT_KEY_ENV} 未配置：网关将以 fail-closed 运行，全部 /v1 请求返回 401。"
            f"请在 .env 设置该密钥后重启网关。"
        )
```

- [ ] **Step 5: 补 `.env.example`**

`.env.example` 网关段（`GATEWAY_READ_TIMEOUT` 之后）追加：

```ini
# 网关数据面客户端密钥：外部调用方访问 /v1* 与 nginx 模型直连路径的唯一凭据。
# 与管理面 API_KEY 严格隔离——API_KEY 可改配置/启停模型，绝不可下发给外部客户端。
# 留空 ⇒ fail-closed，网关 /v1* 全部返回 401（预期行为，非故障）。
# 生成建议：python -c "import secrets;print('sk-'+secrets.token_urlsafe(32))"
GATEWAY_CLIENT_API_KEY=
```

- [ ] **Step 6: 运行确认通过**

Run: `python -m pytest tests/test_all_service.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add .env.example src/modelctl/core/all_service.py tests/test_all_service.py
git commit -m "feat(service): 网关状态/启动适配客户端鉴权密钥（未配置时明确告警）"
```

---

### Task 5: `nginx-snippet` 输出凭据校验 map

**Files:**
- Modify: `src/modelctl/core/nginx_snippet.py`
- Modify: `src/modelctl/cli.py`（parser L159-161、`_cmd_nginx_snippet` L986-989）
- Test: `tests/test_nginx_snippet.py`

**Interfaces:**
- Consumes: Task 1 的 `GATEWAY_CLIENT_KEY_ENV`
- Produces: `build_client_auth_map(client_key: str, *, bearer_var: str = "$http_authorization") -> str`；`build_llm_map` 签名不变

- [ ] **Step 1: 写失败测试**

`tests/test_nginx_snippet.py` 末尾追加：

```python
def test_build_client_auth_map_gateway_group_only_client_key():
    """网关 location 的白名单必须只含 client_key：与网关自身校验口径完全一致，
    否则 profile key 过了 nginx 却被网关 401，表现为无法解释的配置矛盾。"""
    out = build_client_auth_map("sk-abc123", extra_keys=["profile-key-1"])
    gw = out.split("map $http_authorization $llm_bearer_gw")[1].split("map $http_x_api_key $llm_xkey_gw")[0]
    assert '"Bearer sk-abc123" 1;' in gw
    assert "profile-key-1" not in gw


def test_build_client_auth_map_all_group_includes_profile_keys():
    """直连/用量 location 的白名单需同时含两把 key：直连 location 不改写 Authorization，
    引擎只认 profile key；只放行 client_key 会让既有直连客户端全断。"""
    out = build_client_auth_map("sk-abc123", extra_keys=["profile-key-1"])
    assert '"Bearer profile-key-1" 1;' in out
    assert '"profile-key-1" 1;' in out


def test_build_client_auth_map_emits_reject_pairs():
    out = build_client_auth_map("sk-abc123")
    assert 'map "$llm_bearer_gw$llm_xkey_gw" $llm_reject' in out
    assert 'map "$llm_bearer_all$llm_xkey_all" $llm_reject_all' in out
    assert 'default 1;' in out
    assert out.count('"11" 0;') == 2  # 两组各一个放行组合表


def test_build_client_auth_map_dedups_and_skips_empty_extra_keys():
    out = build_client_auth_map("sk-abc123", extra_keys=["sk-abc123", "", "  "])
    assert out.count('"Bearer sk-abc123" 1;') == 2  # gw 与 all 各一次，不因重复而多写


def test_build_client_auth_map_rejects_empty_key():
    with pytest.raises(ProfileError):
        build_client_auth_map("")


def test_build_client_auth_map_rejects_quote_in_key():
    """key 含双引号会破坏 nginx 配置解析，必须拒绝而非静默产出错误配置。"""
    with pytest.raises(ProfileError):
        build_client_auth_map('sk-bad"key')
```

顶部 import 补 `build_client_auth_map`（`ProfileError` / `pytest` 该文件应已有，缺则补）。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_nginx_snippet.py -k client_auth -v`
Expected: FAIL — `ImportError: cannot import name 'build_client_auth_map'`

- [ ] **Step 3: 实现生成器**

`src/modelctl/core/nginx_snippet.py` 末尾追加：

```python
def build_client_auth_map(client_key: str, extra_keys: Iterable[str] = ()) -> str:
    """生成客户端凭据校验 map 片段（nginx http 块），供 B 机 include。

    产出两组白名单，与两条路径的下游校验能力精确对齐：
      $llm_reject      网关 location 用——只认 GATEWAY_CLIENT_API_KEY，与网关自身
                       verify_client 口径完全一致（网关只认这一把，多放会表现为
                       "过了 nginx 却被网关 401" 的配置矛盾）。
      $llm_reject_all  模型直连 / 用量 location 用——额外放行各 profile api_key，
                       因为直连不改写 Authorization，vLLM 等引擎只认 profile key；
                       只放行 client_key 会让既有直连客户端全断。

    双通道与网关一致：Authorization: Bearer <key> 或 x-api-key: <key> 任一命中即放行。

    产物含明文密钥：上传后须 chmod 600，且严禁入库。
    """
    key = (client_key or "").strip()
    if not key:
        raise ProfileError("客户端密钥为空，无法生成 nginx 鉴权片段")
    extra: list[str] = []
    for item in extra_keys:
        e = (item or "").strip()
        if e and e != key and e not in extra:
            extra.append(e)
    for k in [key, *extra]:
        if '"' in k or "\n" in k or "\\" in k:
            raise ProfileError(f"密钥含双引号/反斜杠/换行，nginx map 值不安全：{k[:4]}***")

    def _maps(suffix: str, keys: list[str]) -> list[str]:
        lines = []
        for var, out, prefix in (
            ("$http_authorization", f"$llm_bearer_{suffix}", "Bearer "),
            ("$http_x_api_key", f"$llm_xkey_{suffix}", ""),
        ):
            lines.append(f"map {var} {out} {{")
            lines.append("    default 0;")
            lines += [f'    "{prefix}{k}" 1;' for k in keys]
            lines.append("}")
        reject = "$llm_reject" if suffix == "gw" else "$llm_reject_all"
        lines += [
            f'map "$llm_bearer_{suffix}$llm_xkey_{suffix}" {reject} {{',
            '    "11" 0;',
            '    "10" 0;',
            '    "01" 0;',
            "    default 1;",
            "}",
        ]
        return lines

    lines = [
        "# ---- 客户端凭据校验（modelctl nginx-snippet 生成，勿手改）----",
        "# 产物含明文密钥：chmod 600，严禁入库",
        "# $llm_reject=网关 location 专用（仅 client key）；$llm_reject_all=直连/用量 location 用（含 profile key）",
    ]
    lines += _maps("gw", [key])
    lines += _maps("all", [key, *extra])
    return "\n".join(lines) + "\n"
```

顶部若无 `Iterable`，在 `from typing import ...` 补（该文件已有 `ProfileError` 定义/导入，按实际位置取用）。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_nginx_snippet.py -v`
Expected: 全部 PASS

- [ ] **Step 5: CLI 接入 `--client-key`**

`src/modelctl/cli.py` L161 后新增参数：

```python
    ns.add_argument("--client-key", default=None,
                    help="数据面客户端密钥（默认取 .env 的 GATEWAY_CLIENT_API_KEY）；为空则不生成鉴权 map 并告警")
    ns.add_argument("--no-auth", action="store_true",
                    help="不生成客户端鉴权 map（仅用于本机调试，公网禁用）")
```

`_cmd_nginx_snippet`（L986-989）整体替换：

```python
def _cmd_nginx_snippet(args, models_dir) -> int:
    gateway_port = int(os.environ.get("GATEWAY_PORT", "5003"))
    profiles = list_profiles(models_dir)
    print(build_llm_map(profiles, args.node, args.host, gateway_port), end="")
    if args.no_auth:
        logger.warning("--no-auth：已跳过客户端鉴权片段，公网暴露将使模型可被匿名调用")
        return 0
    key = args.client_key or os.environ.get("GATEWAY_CLIENT_API_KEY", "")
    if not key:
        logger.warning(
            "GATEWAY_CLIENT_API_KEY 未配置且未传 --client-key：未生成鉴权片段，"
            "nginx 数据面将匿名可访问"
        )
        return 0
    print(build_client_auth_map(key, [p.api_key for p in profiles]), end="")
    return 0
```

`cli.py` 的 `from modelctl.core.nginx_snippet import build_llm_map` 补 `build_client_auth_map`（若为函数内延迟导入则同处补）。

- [ ] **Step 6: 冒烟验证**

Run: `$env:GATEWAY_CLIENT_API_KEY="sk-demo-key"; python -m modelctl nginx-snippet --node 208 --host 192.168.77.208`
Expected: 先输出 `map $uri $llm_model_target {...}`，随后 6 段 map；`$llm_bearer_gw` 内只有 `"Bearer sk-demo-key" 1;`，`$llm_bearer_all` 内另含各 profile key 行

- [ ] **Step 7: 提交**

```bash
git add src/modelctl/core/nginx_snippet.py src/modelctl/cli.py tests/test_nginx_snippet.py
git commit -m "feat(nginx): nginx-snippet 生成数据面客户端凭据校验 map（网关/直连分组）"
```

---

### Task 6: nginx 参考配置（TLS + 限流 + IP 头 + 401）

**Files:**
- Modify: `docs/nginx/llm-routing.example.conf`（整体重写，64 行 → 完整版）
- Create: `docs/nginx/tls-setup.md`（自建 CA 操作文档）

**Interfaces:**
- Consumes: Task 5 生成的 `$llm_reject`
- Produces: 可直接抄用的 B 机配置；CA/证书生成步骤文档

- [ ] **Step 1: 重写 example conf**

`docs/nginx/llm-routing.example.conf` 整体替换为下列内容（保留原有 4 条 location 顺序与注释要点，叠加 TLS / 限流 / 凭据 / IP 头）：

```nginx
# ================= LLM 多模型路由（B 机 nginx 参考配置） =================
# 使用步骤：
#   1. C 机执行：modelctl nginx-snippet --node 210 --host 192.168.77.210
#      输出含两段：map $uri $llm_model_target（路由表） + 凭据校验 map（$llm_reject）
#      上传 B 机 /etc/nginx/conf.d/ 并 chmod 600（产物含明文密钥，严禁入库）
#   2. 将下方 http 块与 locations 并入配置；http 块的 zone 全局唯一，多 server 复用
#   3. 按 docs/nginx/tls-setup.md 生成证书，替换 ssl_certificate 路径
#   4. nginx -t && systemctl reload nginx
#
# 鉴权口径（详见 docs/superpowers/specs/2026-09-07-gateway-client-auth-design.md）：
#   网关路径 /<node>/llm/v1/... 与模型直连路径 /<node>/llm/<模型>/v1/... 共用同一把
#   GATEWAY_CLIENT_API_KEY；客户端可带 Authorization: Bearer <key> 或 x-api-key: <key>。
#   网关自身还会二次校验（纵深防御），引擎侧 key 由网关代填，客户端无需关心。

# ---------- http 块：限流 zone（全局唯一，多 server 复用）----------
# limit_req 只管新建请求速率，SSE 长会话不占该速率但占并发，故两者必须同时配。
limit_req_zone  $binary_remote_addr zone=llm_req:10m  rate=30r/m;
limit_conn_zone $binary_remote_addr zone=llm_conn:10m;
limit_req_status  429;
limit_conn_status 429;

# ---------- http 块：路由表 + 凭据 map（由 modelctl nginx-snippet 生成，勿手写）----------
# map $uri $llm_model_target { ... }
# 网关 location 专用（只含 GATEWAY_CLIENT_API_KEY，与网关自身校验口径一致）：
#   $llm_reject
# 模型直连 / 用量 location 专用（额外含各 profile api_key）：
#   $llm_reject_all

server {
    listen 5000 ssl;
    ssl_certificate     /etc/nginx/tls/server.crt;
    ssl_certificate_key /etc/nginx/tls/server.key;   # chmod 600
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # ================= LLM 多模型路由 =================
    # 顺序即优先级：旧用量 > 按模型用量 > 网关 > 模型直连
    #（v1 必须优先于模型名匹配，否则 /llm/v1/... 会被误判为模型名）
    # 每个 location 统一：凭据校验 → 限流 → 透传真实 IP → SSE 不缓冲
    # 凭据变量选择：走网关（5003）的用 $llm_reject，直连引擎端口的用 $llm_reject_all

    # 1) 旧用量统计（兼容旧 cc-switch 卡片）—— 指向 5002 stats，非网关，用 all 组
    location ~ ^/(\d+)/llm/v1/api/usage(.*)$ {
        if ($llm_reject_all) {
            default_type application/json;
            return 401 '{"error":{"message":"invalid API key","type":"authentication_error"}}';
        }
        limit_req  zone=llm_req burst=60 nodelay;
        limit_conn llm_conn 16;
        proxy_pass http://192.168.77.$1:5002/api/usage$2;
    }

    # 2) 按模型用量统计（stats 服务已支持 ?model= 路由）—— 同上，用 all 组
    location ~ ^/(?<node_id>\d+)/llm/(?<model_name>[^/]+)/v1/api/usage$ {
        if ($llm_reject_all) {
            default_type application/json;
            return 401 '{"error":{"message":"invalid API key","type":"authentication_error"}}';
        }
        limit_req  zone=llm_req burst=60 nodelay;
        limit_conn llm_conn 16;
        proxy_pass http://192.168.77.$node_id:5002/api/usage?model=$model_name;
    }

    # 3) 网关（model 参数场景 + 旧地址兼容），关闭缓冲保证 SSE 流式
    #    v1(?:/.*)? 同时匹配 /llm/v1 与 /llm/v1/...——裸 v1（无尾斜杠）也必须走网关，
    #    否则 /llm/v1 会被规则 4 误判为模型名 v1（map 无 v1 条目 → 404/掉入 location / → 502）
    #    $llm_reject：只放行 GATEWAY_CLIENT_API_KEY，与网关 verify_client 完全同口径
    location ~ ^/(?<node_id>\d+)/llm/(?<llm_rest>v1(?:/.*)?)$ {
        if ($llm_reject) {
            default_type application/json;
            return 401 '{"error":{"message":"invalid API key","type":"authentication_error"}}';
        }
        limit_req  zone=llm_req burst=60 nodelay;
        limit_conn llm_conn 16;
        proxy_pass http://192.168.77.$node_id:5003/$llm_rest;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header X-Real-IP       $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header X-Accel-Buffering no;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    # 4) 模型直连（map 注册表命中端口；未知模型 404）
    #    必须在网关规则之后声明（v1 优先匹配），否则 /llm/v1/... 会被误判为模型名 v1 走直连
    #    $llm_reject_all：本 location 不改写 Authorization，引擎（vLLM/SGLang 等）只认
    #    profile api_key，故白名单必须含它；Ollama/TensorRT-LLM 引擎侧无鉴权，
    #    这一层是它们唯一的准入闸门
    location ~ ^/(?<node_id>\d+)/llm/(?<model_name>[^/]+)/(?<llm_rest>.*)$ {
        if ($llm_reject_all) {
            default_type application/json;
            return 401 '{"error":{"message":"invalid API key","type":"authentication_error"}}';
        }
        if ($llm_model_target = "") {
            return 404;
        }
        limit_req  zone=llm_req burst=60 nodelay;
        limit_conn llm_conn 16;
        rewrite ^/\d+/llm/[^/]+/(.*)$ /$1 break;
        proxy_pass $llm_model_target;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header X-Real-IP       $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

- [ ] **Step 2: 写 TLS 操作文档**

Create `docs/nginx/tls-setup.md`，内容包含：为何裸 IP 签不了 Let's Encrypt（LE 只签域名）、下方生成命令、客户端信任 CA 的方法（Windows / Linux / macOS / `curl -k` 降级）、5000 端口从 http 直接切 https 的停机说明与客户端 `base_url` 改造点。

```bash
# 1) 根 CA（10 年；ca.key 离线保管，绝不上传任何机器）
openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
  -keyout ca.key -out ca.crt -subj "/CN=modelctl-internal-CA"

# 2) 服务端 CSR
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr \
  -subj "/CN=llm.modelctl.internal"

# 3) 签发服务端证书（SAN 必须含实际访问的 IP/域名，否则客户端校验失败）
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 365 -sha256 \
  -extfile <(printf "subjectAltName=IP:36.156.121.146,DNS:llm.modelctl.internal") \
  -out server.crt

# 4) 部署到 B 机
scp server.crt server.key root@B机IP:/etc/nginx/tls/
ssh root@B机IP 'chmod 600 /etc/nginx/tls/server.key && nginx -t && systemctl reload nginx'
```

文档必须明确标注：**本步骤为人工操作，Agent 不代跑**（spec §9 裁决 3）。

- [ ] **Step 3: 语法自检**

Run（若本机有 nginx）: `nginx -t -c "$PWD/docs/nginx/llm-routing.example.conf"`
Expected: 无配置语法错误。本机无 nginx 时跳过，改由 B 机执行 `nginx -t` 验证，并在 PR 描述注明未本机验证。

- [ ] **Step 4: 提交**

```bash
git add docs/nginx/llm-routing.example.conf docs/nginx/tls-setup.md
git commit -m "docs(nginx): 数据面参考配置加 TLS/限流/凭据校验/真实 IP"
```

---

### Task 7: 引擎侧 key 下发补齐

**Files:**
- Modify: `src/modelctl/engines/sglang.py`（`build_command` L93-107）
- Modify: `src/modelctl/engines/tokenspeed.py`（docker 分支 L111-115）
- Test: `tests/test_engines_sglang.py`、`tests/test_engines_tokenspeed.py`

**Interfaces:**
- Consumes: `EngineAdapter.api_key_args()`（`engines/base.py` L139-140，返回 `["--api-key", <key>]`）
- Produces: SGLang 与 TokenSpeed-docker 后端启用原生 key 校验

前置核实结论（勿重复调研）：SGLang 官方支持 `--api-key`（[Server Arguments → API related](https://docs.sglang.io/advanced_features/server_arguments.html)）；TensorRT-LLM 与 Ollama **官方无此能力**，本任务不改这两个文件，它们依赖 Task 2/5 的两层兜底。

- [ ] **Step 1: 写失败测试**

`tests/test_engines_sglang.py` 复用该文件已有的 `_write(tmp_path, text)`（写 yaml 并 `load_profile`）、`CAPS8`、`_stub_venv`（从 `tests.test_engines_vllm` 导入），末尾追加：

```python
def test_sglang_command_includes_api_key(tmp_path, monkeypatch):
    """SGLang 支持 --api-key；此前未下发，导致直连引擎端口可匿名推理。"""
    _stub_venv(tmp_path, monkeypatch, "sglang")
    p = _write(tmp_path, "name: s\nengine: sglang\nport: 30000\napi_key: sk-eng-key\nsglang:\n  model: /models/s\n")
    a = get_adapter("sglang")(p, CAPS8)
    cmd, _env = a.build_command()
    assert "--api-key" in cmd
    assert cmd[cmd.index("--api-key") + 1] == "sk-eng-key"
```

`tests/test_engines_tokenspeed.py` 已有 `test_tokenspeed_docker_command`（L43 起，profile 内含 `api_key: sk-test`，走 docker 分支），在其后追加同口径用例；venv 分支作为对照已有行为，一并断言防回归：

```python
def test_tokenspeed_docker_command_includes_api_key(tmp_path, monkeypatch):
    """docker 分支此前漏发 --api-key（venv 分支 L134 已有），容器暴露端口可匿名推理。"""
    model_dir = tmp_path / "models" / "Qwen3.5-397B-A17B"
    model_dir.mkdir(parents=True)
    p = _write(
        tmp_path,
        f"name: q\nengine: tokenspeed\nport: 8150\napi_key: sk-test\ntokenspeed:\n"
        f"  model: {model_dir}\n  tensor_parallel_size: 8\n"
        f"  docker_image: lightseekorg/tokenspeed:latest\n",
    )
    a = get_adapter("tokenspeed")(p, CAPS8)
    cmd, _env = a.build_command()
    assert cmd[0] == "docker"
    assert "--api-key" in cmd
    assert cmd[cmd.index("--api-key") + 1] == "sk-test"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_engines_sglang.py tests/test_engines_tokenspeed.py -k api_key -v`
Expected: FAIL — cmd 中无 `--api-key`

- [ ] **Step 3: 实现**

`sglang.py` 在 `cmd += shlex.split(...extra_args)` 之前（即 L112-113 处）插入：

```python
        cmd += self.api_key_args()
```

`tokenspeed.py` docker 分支，在 `cmd += ["--max-model-len", ...]` 之后、`cmd += extra` 之前插入：

```python
            cmd += self.api_key_args()
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_engines_sglang.py tests/test_engines_tokenspeed.py tests/test_engines_tensorrt_llm.py tests/test_engines_ollama.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 真机回归（关键，不可跳过）**

spec §5 指出 `--api-key` 是否连带门控非 OpenAI 端点存在歧义，必须实测。在 208 节点：

```bash
modelctl stop sglang-<name>
modelctl start sglang-<name>
modelctl status <name>        # 必须正常，不得报"未运行"
modelctl list                 # 状态列正常
```

已知风险点：`all_service.status_model`（L460）用 `wait_health("http://127.0.0.1:<port>", 3.0)` **不带 key** 请求根路径。若 status 误报，改该行为：

```python
        ok = wait_health(f"http://127.0.0.1:{profile.port}", 3.0, api_key_of(profile))
```

其中 `api_key_of(profile)` 取 `profile.api_key`（若该处无现成取值则 `getattr(profile, "api_key", None)`）。

任一回退无法收敛时，**单独回滚本 Task**（`git revert`），Task 1-6 的网关与 nginx 加固不受影响。

- [ ] **Step 6: 提交**

```bash
git add src/modelctl/engines/sglang.py src/modelctl/engines/tokenspeed.py tests/test_engines_sglang.py tests/test_engines_tokenspeed.py
git commit -m "feat(engines): SGLang 与 TokenSpeed docker 分支补齐 --api-key 下发"
```

---

### Task 8: 测试指南文档重写

**Files:**
- Modify: `docs/nginx/测试指南.md`（架构图 L13-18、鉴权说明 L57-68、curl 矩阵 L177-215、排错表 L220-265、SDK 示例 L238-252）

**Interfaces:**
- Consumes: 前 7 个 Task 的全部行为
- Produces: 与实现一致的运维文档

- [ ] **Step 1: 改鉴权说明段**

原 L179-181 的「统一网关：客户端**无需**携带」必须改为：

```markdown
> - **所有 /v1 路径都需要凭据**，可带 `Authorization: Bearer <key>` 或 `x-api-key: <key>`。
> - **网关路径**（`/<node>/llm/v1/...`）：带 `GATEWAY_CLIENT_API_KEY`。该 key 仅用于准入，
>   网关会改用后端 profile key 认证上游，客户端无需知道 profile key。
> - **模型直连路径**（`/<node>/llm/<模型>/v1/...`）：带该模型的 `api_key`（profile key）。
>   nginx 不改写 Authorization，引擎直接校验该值，因此必须用 profile key 而非客户端 key。
> - nginx 白名单**同时包含** `GATEWAY_CLIENT_API_KEY` 与全部 profile `api_key`，
>   两条路径各有可用组合，既有客户端无需改造。
> - 缺凭据 / key 不在白名单 → 401 `{"error":{"type":"authentication_error"}}`；命中限流 → 429。
```

两条路径的凭据分工必须讲清，这是运维最容易踩的点：

| 客户端携带 | 网关路径 `/<n>/llm/v1/...` | 直连路径 `/<n>/llm/<模型>/v1/...` |
|---|---|---|
| `GATEWAY_CLIENT_API_KEY` | **200**（nginx `$llm_reject` 放行 + 网关准入通过） | **200**（nginx `$llm_reject_all` 放行；引擎侧无 key 的 Ollama 等直接服务）—— 注：vLLM 等有 key 的引擎会 401 |
| profile `api_key` | **401**（nginx `$llm_reject` 只含客户端 key，直接拒绝） | **200** |

> 两组白名单的分工：`$llm_reject` 只含 `GATEWAY_CLIENT_API_KEY`，与网关 `verify_client` 同口径；`$llm_reject_all` 额外含各 profile `api_key`，因为直连 location 不改写 `Authorization`，vLLM/SGLang 等引擎只认 profile key。带客户端 key 走直连路径，对有 key 的引擎会 401（引擎不认）——想让两条路径一把 key 通吃，可把 `GATEWAY_CLIENT_API_KEY` 配成与 profile `api_key` 相同值（会失去数据面/管理面隔离，不推荐）。

- [ ] **Step 2: curl 矩阵全量补 key 与 https**

L183-215 的 8 条校验矩阵统一：`http://` → `https://`；每条补 `-H "Authorization: Bearer $CLIENT_KEY"`；开头加变量定义：

```bash
export CLIENT_KEY="<GATEWAY_CLIENT_API_KEY 的值>"
export B=<B机地址>
export NODE=208
```

新增两条：

```bash
# ⑨ 缺凭据 → 401（网关路径）
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://$B:5000/$NODE/llm/v1/chat/completions \
  -H 'Content-Type: application/json' -d '{"messages":[{"role":"user","content":"hi"}]}'

# ⑩ x-api-key 通道同样可用
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://$B:5000/$NODE/llm/v1/chat/completions \
  -H "x-api-key: $CLIENT_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"你好"}]}'
```

- [ ] **Step 3: 排错表补两行**

| 现象 | 根因 | 处置 |
|---|---|---|
| `401 authentication_error` | 未带 key / key 不匹配 / 服务端未配 `GATEWAY_CLIENT_API_KEY` | 看网关日志 `auth=missing\|invalid\|unconfigured` 区分三档；`modelctl status` 会直接提示"未配置 GATEWAY_CLIENT_API_KEY" |
| `429` | 命中 `limit_req`（30r/m burst=60）或 `limit_conn`（16） | 单 IP 退避重试；确属多人共用出口 IP，按 `llm-routing.example.conf` 注释调高 zone 速率 |

- [ ] **Step 4: SDK 示例更新**

L238-252 的 OpenAI SDK 示例改为 `https://` + `api_key=<CLIENT_KEY>`，并注明未信任自建 CA 时须 `httpx.Client(verify="<ca.crt 路径>")`。

- [ ] **Step 5: 自检并提交**

通读全文，确认无残留「无需 key」「http://」的旧表述；确认矩阵每条命令可直接复制执行。

```bash
git add docs/nginx/测试指南.md
git commit -m "docs(nginx): 测试指南同步鉴权/限流/TLS 行为"
```

---

### Task 9: 陷阱知识库沉淀

**Files:**
- Modify: `docs/known-pitfalls/README.md`（摘要索引）
- Modify/Create: `docs/known-pitfalls/backend/网关鉴权.md`

**Interfaces:**
- Consumes: 无
- Produces: 符合 CLAUDE.md 渐进式披露规范的知识条目

- [ ] **Step 1: 摘要层加索引行**

`docs/known-pitfalls/README.md` 索引表追加一行（分类 `backend`，日期 `2026-09-07`，一句话描述）：

```markdown
| 网关 /v1 端点匿名可白嫖 GPU | backend | 2026-09-07 | 数据面无任何凭据校验且公网 http 暴露，须三层加固：nginx map 常量比对 + 网关 fail-closed + 引擎 api-key |
```

- [ ] **Step 2: 详情文件**

按该目录既有主题聚合文件的格式（文件头「原始单文件已并入本文件归档」说明 + `## <问题标题>` 条目），在 `docs/known-pitfalls/backend/` 下建 `网关鉴权.md`，记录四条：

1. **网关覆盖客户端 Authorization 导致"带了 key 也不校验"** — 根因（`gateway.py` 用 profile key 无条件覆盖）+ 解决（准入与上游认证分离，客户端 key 验完即丢）。
2. **`hmac.compare_digest` 对非 ASCII str 抛 TypeError** — 表现为 500 而非 401；解决：两侧 `.encode("utf-8")` 后比较并 try-except（同 `cluster/tokens.py` 范式）。
3. **SSE 场景不可用 BaseHTTPMiddleware 做鉴权** — 网关手工管理 httpx 客户端生命周期（`gateway.py` L830-832），中间件层有提前切断流的风险；解决：处理器首行显式调用校验。
4. **Ollama / TensorRT-LLM 引擎侧无鉴权能力** — 官方确认（Ollama 本地 API 无需认证；trtllm-serve 无 `--api-key`）；解决：只能靠 nginx map + 网关两层兜底，勿误以为加了引擎层就安全。

- [ ] **Step 3: 提交**

```bash
git add docs/known-pitfalls/
git commit -m "docs(pitfalls): 沉淀网关鉴权三层加固的四条陷阱"
```

---

### Task 10: 全量验证与发布说明

**Files:**
- 无新增（验证 + 可选 `docs/TODO.md` 勾销）

- [ ] **Step 1: 全量测试**

Run: `python -m pytest tests/ -q`
Expected: 全部 PASS，无新增 failure。若有失败，回到对应 Task 修复，禁止跳过或改断言迁就实现。

- [ ] **Step 2: 端到端人工验证清单（208 节点）**

逐项执行并记录结果，任何一项不符回到对应 Task：

```bash
# 网关本机（带 key / 不带 key）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5003/v1/models
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $CLIENT_KEY" http://127.0.0.1:5003/v1/models
# 期望：401 / 200

# 经 B 机（401、200、流式、429）
curl -sk -o /dev/null -w '%{http_code}\n' https://36.156.121.146:5000/208/llm/v1/models
curl -sk -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $CLIENT_KEY" https://36.156.121.146:5000/208/llm/v1/models
# 期望：401 / 200；再连续请求 70+ 次应出现 429

# 审计中无明文 key
grep -c "$CLIENT_KEY" data/audit/*.jsonl || echo "OK: 审计无明文 key"
```

- [ ] **Step 3: 发布说明（PR 描述必须包含）**

- 破坏性变更 1：`/v1*` 全部端点强制 `GATEWAY_CLIENT_API_KEY`，未配置则**全部 401**。上线前必须在 `.env` 配好新 key（生成命令见 `.env.example` 注释）并重启网关。
- 破坏性变更 2：裸 `POST /v1` 连通性探测从 200 → 401。依赖 baseUrl 探测的客户端（hertz 等）需带 key，或改探 `GET /v1/models`。
- 破坏性变更 3：5000 端口由 http 直接切 https（不做双监听）。所有客户端 `base_url` 改 `https://`，并信任自建 CA 或显式关闭校验。
- 密钥策略：新 `GATEWAY_CLIENT_API_KEY` 必须另生成，**不得复用** `fly@@see`（已在日志明文泄露）；建议一并轮换 `API_KEY`，注意它是各引擎 `api_key: ${API_KEY}` 的插值源，改动后需重启全部模型进程并重新登录 webui。
- 已知残留：nginx 直连路径的 key 口径待 Task 8 实测确认（见该 Task Step 1 注意事项）。
