# 网关家族路由（group 自动选择运行实例）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 网关支持 yaml 顶层 `group` 字段，请求家族裸名（如 `model=qwen3.8`）时自动路由到当前运行中且健康的家族成员（按引擎优先级），`/v1/models` 展示家族逻辑名。

**Architecture:** Profile 增加 `group` 字段；网关新增 `build_groups()` 构建家族索引（按引擎优先级排序）；`resolve_model()` 家族解析优先、精确匹配次之、最后回退默认模型；`create_app` 在未注入 registry 时自动构建 groups。转发逻辑零改动（解析结果为具体成员的 `GatewayModel`）。

**Tech Stack:** Python 3.12、FastAPI、httpx、pytest、PyYAML、loguru

## Global Constraints

- Python 3.12（`from __future__ import annotations` 可用，但 gateway.py 例外：见其文件头注释）
- 遵循现有代码风格：中文注释、loguru 日志
- 现有测试必须保持通过（`uv run pytest tests/ -q`）
- TDD：每个任务先写失败测试，再实现
- 引擎优先级常量：`{"vllm": 0, "sglang": 1, "unsloth": 2, "ollama": 3, "llamacpp": 4}`，未知引擎 99
- 家族无健康成员 → 404，**不回退 default_model**（设计文档 §2.3 明确）
- 不修改任何 profile 现有 `alias` 字段（nginx 路径直连依赖它）
- `build_registry` / `resolve_model` 保持向后兼容（groups 缺省 None / 注入 registry 时 groups 默认为空）

---

### Task 1: Profile 支持 group 字段

**Files:**
- Modify: `src/modelctl/core/profile.py`（Profile dataclass 第 25-36 行；`_to_profile` 第 86-97 行）
- Test: `tests/test_profile.py`

**Interfaces:**
- Consumes: 无（新字段）
- Produces: `Profile.group: str | None`（顶层可选字段 `group`，非空字符串；非法时告警忽略为 None）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_profile.py`）

```python
def test_group_field(tmp_path):
    d = _write(tmp_path, "name: demo\ngroup: qwen3.8\nengine: ollama\nport: 11434\n")
    p = load_profile("demo", d)
    assert p.group == "qwen3.8"


def test_group_missing_defaults_none(tmp_path):
    d = _write(tmp_path, "name: demo\nengine: ollama\nport: 11434\n")
    p = load_profile("demo", d)
    assert p.group is None


def test_group_invalid_ignored_with_warning(tmp_path):
    d = _write(tmp_path, "name: demo\ngroup: \"\"\nengine: ollama\nport: 11434\n")
    p = load_profile("demo", d)
    assert p.group is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_profile.py::test_group_field -q`
Expected: FAIL（`AttributeError: 'Profile' object has no attribute 'group'`）

- [ ] **Step 3: 实现**

`src/modelctl/core/profile.py`：

```python
@dataclass
class Profile:
    name: str
    engine: str
    port: int
    api_key: str | None = None
    engine_config: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    group: str | None = None
    path: Path | None = None
    tool_call_rounds: int | None = None
    max_output_tokens: int | None = None
```

`_to_profile` 中，在 `aliases = _parse_aliases(raw, src)` 之后加：

```python
    group = raw.get("group")
    if group is not None and (not isinstance(group, str) or not group.strip()):
        logger.warning(f"{src}：group 必须是非空字符串，已忽略")
        group = None
```

`return Profile(...)` 中在 `aliases=aliases,` 之后加 `group=group,`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_profile.py -q`
Expected: PASS（全部，含既有测试）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/profile.py tests/test_profile.py
git commit -m "feat: Profile 支持 group 字段（家族路由用）"
```

---

### Task 2: 网关家族索引 build_groups

**Files:**
- Modify: `src/modelctl/core/gateway.py`（模块常量区第 32 行 `GATEWAY_PORT` 附近；`build_registry` 之后）
- Test: `tests/test_gateway.py`

**Interfaces:**
- Consumes: `Profile.group`（Task 1）、`list_profiles`、`get_adapter`、`GatewayModel`、`Capabilities`
- Produces: `ENGINE_PRIORITY: dict[str, int]`；`build_groups(models_dir: Path | None = None, host: str = "127.0.0.1") -> dict[str, list[GatewayModel]]`（group 名 → 按引擎优先级升序的成员列表）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_gateway.py`）

```python
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
    assert list(groups) == ["qwen3.8"]  # 未声明 group 的不进入家族
    assert [m.name for m in groups["qwen3.8"]] == ["qwen3.8-vllm", "qwen3.8-llamacpp"]  # vllm 优先
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_gateway.py::test_build_groups_sorted_by_engine_priority -q`
Expected: FAIL（`ImportError: cannot import name 'build_groups'`）

- [ ] **Step 3: 实现**

`src/modelctl/core/gateway.py`，`GATEWAY_PORT` 之后加：

```python
# 家族路由引擎优先级（数值越小越优先）；未知引擎兜底 99
ENGINE_PRIORITY = {"vllm": 0, "sglang": 1, "unsloth": 2, "ollama": 3, "llamacpp": 4}
```

`build_registry` 之后加：

```python
def build_groups(models_dir: Path | None = None, host: str = "127.0.0.1") -> dict[str, list[GatewayModel]]:
    """group 名 -> 按引擎优先级排序的成员 GatewayModel 列表（同引擎保持扫描顺序）。

    未声明 group 的 profile 不进入任何家族；组内排序见 ENGINE_PRIORITY。
    """
    groups: dict[str, list[GatewayModel]] = {}
    for profile in list_profiles(models_dir):
        if not profile.group:
            continue
        adapter = get_adapter(profile.engine)(profile, Capabilities())
        model = GatewayModel(
            name=profile.name,
            engine=profile.engine,
            backend_url=f"http://{host}:{profile.port}",
            upstream_model=adapter.upstream_model_name(),
            api_key=profile.api_key,
            health_url=adapter.health_url(),
            aliases=profile.aliases,
            adapter=adapter,
        )
        groups.setdefault(profile.group, []).append(model)
    for members in groups.values():
        members.sort(key=lambda m: ENGINE_PRIORITY.get(m.engine, 99))
    return groups
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_gateway.py -q`
Expected: PASS（含既有测试）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/gateway.py tests/test_gateway.py
git commit -m "feat: 网关 build_groups 家族索引（按引擎优先级排序）"
```

---

### Task 3: resolve_model 家族解析

**Files:**
- Modify: `src/modelctl/core/gateway.py`（`resolve_model` 第 171-179 行）
- Test: `tests/test_gateway.py`

**Interfaces:**
- Consumes: `build_groups` 产出的 groups 结构（Task 2）、`is_running`、`is_model_healthy`（均已在 gateway.py）
- Produces: `resolve_model(registry, body_model, default_model, groups=None) -> GatewayModel | None`；`_resolve_group(groups, name) -> GatewayModel | None`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_gateway.py`）

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_gateway.py::test_resolve_model_group_prefers_running_member -q`
Expected: FAIL（`TypeError: resolve_model() got an unexpected keyword argument 'groups'`）

- [ ] **Step 3: 实现**

`src/modelctl/core/gateway.py`，替换原 `resolve_model`：

```python
def _resolve_group(groups: dict[str, list[GatewayModel]], name: str) -> GatewayModel | None:
    """家族解析：按引擎优先级顺序返回第一个运行中且健康的成员；无则 None。"""
    for m in groups.get(name, []):
        if is_running(m.name) and is_model_healthy(m):
            return m
    return None


def resolve_model(
    registry: dict[str, GatewayModel],
    body_model: str | None,
    default_model: str | None,
    groups: dict[str, list[GatewayModel]] | None = None,
) -> GatewayModel | None:
    """按 body.model 解析目标模型；支持家族（group）路由。

    顺序：body_model 命中 group（家族解析，无健康成员即 None 不回退）→
    body_model 精确匹配 name/alias → 回退 default_model（同样 group 优先）→ None。
    groups 为 None（未启用家族路由）时行为与旧版一致。
    """
    if groups and body_model and body_model in groups:
        return _resolve_group(groups, body_model)
    if body_model and body_model in registry:
        return registry[body_model]
    if groups and default_model and default_model in groups:
        return _resolve_group(groups, default_model)
    if default_model and default_model in registry:
        return registry[default_model]
    return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_gateway.py -q`
Expected: PASS（含既有 `test_resolve_model`）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/gateway.py tests/test_gateway.py
git commit -m "feat: resolve_model 家族解析（group 优先，无健康成员返回 None）"
```

---

### Task 4: create_app 集成 groups + /v1/models 展示家族逻辑名

**Files:**
- Modify: `src/modelctl/core/gateway.py`（`create_app` 第 196-215 行；`list_models` 第 217-247 行；proxy 中 `resolve_model` 调用第 265 行）
- Test: `tests/test_gateway.py`

**Interfaces:**
- Consumes: `build_groups`（Task 2）、`resolve_model(..., groups=)`（Task 3）
- Produces: `create_app(..., groups: dict[str, list[GatewayModel]] | None = None)`（注入 registry 时 groups 缺省为空 dict；未注入时自动 build_groups）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_gateway.py`）

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_gateway.py::test_proxy_group_routes_to_running_member -q`
Expected: FAIL（`TypeError: create_app() got an unexpected keyword argument 'groups'`）

- [ ] **Step 3: 实现**

`src/modelctl/core/gateway.py`：

3a. `create_app` 签名与 groups 构建：

```python
def create_app(
    registry: dict[str, GatewayModel] | None = None,
    default_model: str | None = None,
    read_timeout: float = 600.0,
    transport=None,
    context_rules: dict[str, list[ContextSwitchRule]] | None = None,
    groups: dict[str, list[GatewayModel]] | None = None,
):
    """构建 FastAPI 网关应用（transport 供测试注入 httpx.MockTransport）。

    环境变量：GATEWAY_DEFAULT_MODEL（默认模型，缺省/未知 model 回退目标）；
    GATEWAY_CONTEXT_SWITCH（JSON 上下文切换规则，见 load_context_switch_rules）。
    groups：家族索引（group -> 成员列表）；调用方注入 registry 时缺省为空 dict，
    未注入时自动从 models/*.yaml 构建。
    """
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    if registry is None:
        registry = build_registry()
        if groups is None:
            groups = build_groups()
    else:
        groups = groups or {}
    default_model = default_model or os.environ.get("GATEWAY_DEFAULT_MODEL")
    context_rules = context_rules if context_rules is not None else load_context_switch_rules(_env_context_rules())
    app = FastAPI(title="modelctl gateway", docs_url="/docs", openapi_url="/openapi.json")
```

3b. `list_models`：在 `results` 计算之后、`return` 之前追加家族逻辑名：

```python
        results = await asyncio.gather(*(asyncio.to_thread(_available, m) for m in models))
        data = [
            {
                "id": m.aliases[0] if m.aliases else m.name,
                "object": "model",
                "created": 0,
                "owned_by": "modelctl",
            }
            for m, ok in zip(models, results, strict=False)
            if ok
        ]
        # 家族逻辑名：组内有健康成员时展示（id=group 名，与具体成员 id 去重）
        existing_ids = {item["id"] for item in data}

        def _group_healthy(members: list[GatewayModel]) -> bool:
            return any(is_running(m.name) and is_model_healthy(m) for m in members)

        for group_name, members in groups.items():
            if group_name in existing_ids:
                continue
            if await asyncio.to_thread(_group_healthy, members):
                data.append({"id": group_name, "object": "model", "created": 0, "owned_by": "modelctl"})
        return {"object": "list", "data": data}
```

3c. proxy 中 `resolve_model` 调用（第 265 行附近）改为传 groups：

```python
        target = resolve_model(registry, body.get("model"), default_model, groups)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_gateway.py -q`
Expected: PASS（含全部既有测试与新增 3 个）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/gateway.py tests/test_gateway.py
git commit -m "feat: 网关集成家族路由（/v1/models 展示逻辑名，proxy 家族解析）"
```

---

### Task 5: 26 个 yaml 增加 group 字段

**Files:**
- Modify: 下述 26 个 yaml（每个在 `name:` 行之后插入 `group: <家族名>` 行）

**Interfaces:**
- Consumes: Profile.group 解析（Task 1）
- Produces: 各家族在网关注册表/家族索引中生效

- [ ] **Step 1: 按清单修改**

对每个文件，在 `name:` 行后插入 `group: <家族名>`（保留原有 `alias` 等字段不动）：

**qwen3.8（`group: qwen3.8`）**
- `models/llamacpp/qwen3.8.yaml`（`name:` 第 4 行）
- `models/llamacpp/qwen3.8-high.yaml`（`name:` 第 4 行）
- `models/llamacpp/qwen3.8-light.yaml`（`name:` 第 4 行）
- `models/ollama/qwen3.8.yaml`（`name:` 第 4 行）
- `models/sglang/qwen3.8.yaml`（`name:` 第 4 行）
- `models/unsloth/qwen3.8.yaml`（`name:` 第 4 行）
- `models/vllm/qwen3.8.yaml`（`name:` 第 16 行）
- `models/vllm/qwen3.8-light.yaml`（`name:` 第 5 行）

**deepseek-v4-flash（`group: deepseek-v4-flash`）**
- `models/llamacpp/deepseek-v4-flash.yaml`（`name:` 第 4 行）
- `models/llamacpp/deepseek-v4-flash-light.yaml`（`name:` 第 4 行）
- `models/llamacpp/deepseek-v4-flash-high.yaml`（`name:` 第 4 行）
- `models/vllm/deepseek-v4-flash.yaml`（`name:` 第 7 行）
- `models/vllm/deepseek-v4-flash-light.yaml`（`name:` 第 4 行）
- `models/vllm/deepseek-v4-flash-high.yaml`（`name:` 第 4 行）
- `models/vllm/deepseek-v4-flash-pp.yaml`（`name:` 第 8 行）
- `models/ollama/deepseek-v4-flash.yaml`（`name:` 第 4 行）
- `models/sglang/deepseek-v4-flash.yaml`（`name:` 第 4 行）
- `models/unsloth/deepseek-v4-flash.yaml`（`name:` 第 4 行）

**kimi-k2.5（`group: kimi-k2.5`）**
- `models/llamacpp/kimi-k2.5.yaml`（`name:` 第 7 行）
- `models/vllm/kimi-k2.5.yaml`（`name:` 第 11 行）
- `models/ollama/kimi-k2.5.yaml`（`name:` 第 4 行）
- `models/sglang/kimi-k2.5.yaml`（`name:` 第 7 行）
- `models/unsloth/kimi-k2.5.yaml`（`name:` 第 7 行）

**qwen3-coder（`group: qwen3-coder`）**
- `models/llamacpp/qwen3-coder.yaml`（`name:` 第 7 行）
- `models/ollama/qwen3-coder.yaml`（`name:` 第 6 行）
- `models/unsloth/qwen3-coder.yaml`（`name:` 第 5 行）

示例（`models/vllm/qwen3.8.yaml`）：

```yaml
name: qwen3.8-vllm
group: qwen3.8
engine: vllm
```

- [ ] **Step 2: 验证 group 解析**

Run: `uv run python -c "from modelctl.core.profile import list_profiles; from collections import Counter; print(Counter(p.group for p in list_profiles()))"`
Expected: `Counter({'qwen3.8': 8, 'deepseek-v4-flash': 10, 'kimi-k2.5': 5, 'qwen3-coder': 3, None: 余量})`

- [ ] **Step 3: 验证 build_groups 与网关集成**

Run: `uv run python -c "from modelctl.core.gateway import build_groups; g=build_groups(); print({k:[m.name for m in v] for k,v in g.items()})"`
Expected: 4 个家族，每族成员按引擎优先级排序（vllm 在前，llamacpp 在后）

- [ ] **Step 4: 跑全量测试确认无回归**

Run: `uv run pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add models/
git commit -m "feat: 为 qwen3.8/deepseek-v4-flash/kimi-k2.5/qwen3-coder 家族 profile 增加 group 字段"
```

---

### Task 6: .env.example 注释更新

**Files:**
- Modify: `.env.example`（第 63-67 行）

**Interfaces:**
- Consumes: 无
- Produces: 文档准确性（说明裸名 qwen3.8 现走家族路由）

- [ ] **Step 1: 更新注释**

将 `.env.example` 中 `GATEWAY_DEFAULT_MODEL` 段（第 63-67 行）改为：

```yaml
# 未知/缺省 model 的回退模型；同时是一键启停（modelctl all）默认模型标识：
# 填 profile 的 name / alias / 家族 group 名（如 qwen3.8-vllm / qwen3.8）。
# 裸名 qwen3.8 是家族逻辑名：网关按引擎优先级自动路由到当前运行中的 qwen3.8 成员；
# 家族内无任何成员运行时返回 404。该项未设置时代码实际回退 deepseek-v4-flash。
GATEWAY_DEFAULT_MODEL=qwen3.8
```

（保留默认值 `qwen3.8`——家族路由下它不再必然 502，而是路由到运行中的 qwen3.8 成员。）

- [ ] **Step 2: 验证**

Run: `uv run pytest tests/test_all_service.py tests/test_gateway.py -q`
Expected: PASS（确认 GATEWAY_DEFAULT_MODEL 相关逻辑无回归）

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs: 更新 GATEWAY_DEFAULT_MODEL 注释（裸名走家族路由）"
```

---

## 自审记录

- **Spec 覆盖**：group 字段（Task 1）→ build_groups + 引擎优先级（Task 2）→ resolve_model 家族解析 + 404 不回退（Task 3）→ create_app 注入 + /v1/models 展示（Task 4）→ yaml 26 文件（Task 5）→ .env.example 文档（Task 6）。全部 spec 章节有对应任务。
- **占位符**：无 TBD/TODO；每步含完整代码与验证命令。
- **类型一致性**：`build_groups` 返回 `dict[str, list[GatewayModel]]`；`resolve_model` 第 4 参数 `groups`；`create_app` 关键字 `groups`；`_resolve_group(groups, name)` 一致。
