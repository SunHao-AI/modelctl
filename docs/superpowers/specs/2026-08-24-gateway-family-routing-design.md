# 网关家族路由（group 自动选择运行实例）设计

- 日期：2026-08-24
- 状态：已确认（用户逐节评审通过）
- 关联：`modelctl.core.gateway`（网关按 body.model 分发）

## 1. 背景与目标

### 问题

同一模型族存在多个引擎版本（vLLM / sglang / unsloth / ollama / llama.cpp），各版本是独立的 profile：

| 家族 | 成员（name） |
|---|---|
| qwen3.8 | qwen3.8-vllm / qwen3.8-llamacpp / qwen3.8-llamacpp-high / qwen3.8-llamacpp-light / qwen3.8-ollama / qwen3.8-sglang / qwen3.8-unsloth / qwen3.8-vllm-light |
| deepseek-v4-flash | deepseek-v4-flash-vllm / -vllm-light / -vllm-high / -vllm-pp / -llamacpp / -llamacpp-light / -llamacpp-high / -ollama / -sglang / -unsloth |
| kimi-k2.5 | kimi-k2.5-vllm / -llamacpp / -ollama / -sglang / -unsloth |
| qwen3-coder | qwen3-coder-llamacpp / -ollama / -unsloth |

当前网关 `resolve_model` 只做 name/alias **精确匹配**，不看运行状态：

- 裸名 `qwen3.8` 是 llamacpp 版 profile 的 alias（`alias: qwen3.8`），若 llamacpp 服务未启动而 vLLM 已启动，请求 `model=qwen3.8` 仍被路由到 llamacpp 端口 → **502 后端不可达**。
- 客户端（如 cc-switch）无法用裸名 `qwen3.8` 访问"当前实际在跑的那个 qwen3.8"。

### 目标

1. 客户端请求家族裸名（如 `model=qwen3.8`）时，网关自动路由到**当前运行中且健康**的家族成员。
2. 同时多个成员运行时，按**引擎优先级**选择（vllm > sglang > unsloth > ollama > llamacpp）。
3. 家族内无任何成员运行时，返回 404（明确告知未部署）。
4. `/v1/models` 展示家族逻辑名（有成员运行时），客户端可直接选中。
5. 机制对**所有多引擎模型族通用**（qwen3.8 / deepseek-v4-flash / kimi-k2.5 / qwen3-coder）。

### 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 适用范围 | 通用所有多引擎模型族 |
| 家族识别 | yaml 顶层显式 `group: <家族名>` 字段 |
| 多实例选择 | 引擎优先级（vllm > sglang > unsloth > ollama > llamacpp），同引擎取扫描顺序第一个健康的 |
| 无成员运行 | 返回 404 model not found |
| /v1/models | 展示家族逻辑名（组内有健康成员时） |

## 2. 总体设计

核心思路：**转发逻辑零改动**。家族解析只在"选目标模型"阶段进行——把请求的裸名解析为**具体运行成员**的 `GatewayModel`，后续转发、上游 model 名改写、鉴权全部沿用现有代码。

```
请求 model=qwen3.8
        │
        ▼
resolve_model()
  ├─ 1. group 优先：groups["qwen3.8"] 存在？
  │     └─ 按引擎优先级遍历成员，返回第一个 (is_running && is_model_healthy) 的 GatewayModel
  │           └─ 无成员健康 → 404
  ├─ 2. 精确匹配：registry["qwen3.8"]（name/alias）
  └─ 3. 回退 default_model（同样先 group 后精确）
        │
        ▼
转发到具体成员（body["model"] 改写为该成员的 upstream_model）
```

### 2.1 Profile：新增 `group` 字段

[profile.py](profile.py) 的 `Profile` dataclass 新增：

```python
group: str | None = None
```

解析顶层可选字段 `group`（非空字符串，缺省 None）。不参与 name/alias 冲突校验（group 是"家族名"，可与成员名不同）。

### 2.2 网关：家族索引 `build_groups`

新增函数，从 `models/*.yaml` 读取各 profile 的 `group` 字段，构建家族索引：

```python
ENGINE_PRIORITY = {"vllm": 0, "sglang": 1, "unsloth": 2, "ollama": 3, "llamacpp": 4}

def build_groups(models_dir=None) -> dict[str, list[GatewayModel]]:
    """group 名 -> 按引擎优先级排序的成员 GatewayModel 列表（同引擎按扫描顺序）。"""
```

- 未声明 `group` 的 profile 不进入任何家族（行为与现状一致）。
- 组内排序：`ENGINE_PRIORITY.get(engine, 99)` 升序，同优先级保持 `list_profiles` 顺序。
- 与 `build_registry` 相互独立：registry 仍是 name/alias → 具体模型；groups 是 group → 成员列表。

### 2.3 解析：`resolve_model` 支持家族

```python
def resolve_model(registry, body_model, default_model, groups=None) -> GatewayModel | None:
```

解析顺序（body_model 与 default_model 各走一遍）：

1. **group 优先**：`body_model in groups` → 家族解析，返回第一个健康成员；**组内无健康成员 → 直接返回 `None`（404），不回退 default_model**（与"无成员时返回 404"决策一致）。
2. **精确匹配**：`body_model in registry`（name/alias）。
3. 未命中 registry → 回退 `default_model`（同样的 group → 精确顺序）。
4. 全部失败 → `None` → 网关返回 404 `model not found`。

> 注意：命中 group 但无健康成员时不回退 default（避免跨模型意外响应）；只有 body_model 完全不在 registry/groups 时才回退 default。

家族解析辅助：

```python
def _resolve_group(groups, name) -> GatewayModel | None:
    for m in groups.get(name, []):
        if is_running(m.name) and is_model_healthy(m):
            return m
    return None
```

### 2.4 展示：`/v1/models` 含家族逻辑名

`list_models` 在现有"具体成员（健康过滤）"基础上，追加家族逻辑名：

- 遍历 `groups`，组内有健康成员（`is_running && is_model_healthy` 任一命中）→ 追加 `{"id": group_name, ...}`。
- 无健康成员的组不展示。
- 家族逻辑名 `id` 与具体成员 id 不冲突（成员 id 是 name，group 是裸名；若存在 profile name 恰等于 group 名，按"先具体后家族"去重，具体成员优先）。

### 2.5 `create_app` 注入

```python
def create_app(registry=None, default_model=None, read_timeout=600.0,
               transport=None, context_rules=None, groups=None):
    ...
    registry = registry if registry is not None else build_registry()
    groups = groups if groups is not None else build_groups()
```

- 测试可注入 `groups`（与 registry 注入方式一致）。
- `GATEWAY_DEFAULT_MODEL=qwen3.8` 语义不变：default_model 走同样的 group → 精确解析，行为与显式请求一致。

### 2.6 与现有机制的兼容

| 场景 | 行为 |
|---|---|
| 请求精确成员名 `qwen3.8-vllm` | 精确匹配 registry，不变 |
| 请求别名 `qwen3.8-high` | 精确匹配，不变（llamacpp alias，未启动则 502，与现状一致） |
| 裸名 `qwen3.8` 同时是 llamacpp alias 与 group 名 | **group 优先**，走家族路由（解决原 502 问题）；nginx 路径直连仍用 llamacpp alias，互不影响 |
| 上下文切换（context_rules） | 在家族解析之后执行（[gateway.py 现有逻辑](gateway.py)）；切换目标仍按现有 registry 精确匹配，本期不扩展（现有规则 target 均为具体成员名） |
| 未声明 group 的模型 | 不进入 groups，行为完全不变 |

## 3. yaml 改动清单

以下文件顶层新增 `group: <家族名>`（位置：name 之后、engine 之前，与现有字段风格一致）：

**qwen3.8 家族（`group: qwen3.8`）**：models/{llamacpp,ollama,sglang,unsloth,vllm}/qwen3.8*.yaml 共 8 个（含 llamacpp/qwen3.8.yaml、vllm/qwen3.8.yaml、vllm/qwen3.8-light.yaml）。

**deepseek-v4-flash 家族（`group: deepseek-v4-flash`）**：models/llamacpp/deepseek-v4-flash*.yaml（3）、models/vllm/deepseek-v4-flash*.yaml（4）、models/ollama/deepseek-v4-flash.yaml、models/sglang/deepseek-v4-flash.yaml、models/unsloth/deepseek-v4-flash.yaml，共 10 个。

**kimi-k2.5 家族（`group: kimi-k2.5`）**：models/{llamacpp,vllm,ollama,sglang,unsloth}/kimi-k2.5.yaml，共 5 个。

**qwen3-coder 家族（`group: qwen3-coder`）**：models/{llamacpp,ollama,unsloth}/qwen3-coder.yaml，共 3 个。

> 说明：不修改各 profile 现有 `alias`（nginx 路径直连依赖 alias 短名，保持不变）；裸名与 group 名重复时由网关"group 优先"规则处理。

## 4. 错误处理

| 场景 | 行为 |
|---|---|
| 家族内无成员运行 | 404 `model not found: <name>`（沿用现有 404 逻辑） |
| 成员进程活着但端口不通（is_model_healthy False） | 视为不可用，跳过选下一个成员 |
| group 字段非法（空字符串/非字符串） | profile 解析时忽略该字段并告警，不报错（不阻塞网关启动） |
| 请求精确成员名但该成员未启动 | 502（与现状一致，客户端应改用家族裸名） |

## 5. 测试计划

新增/调整 `tests/test_gateway.py`：

1. `test_build_groups`：两个同 group 不同引擎的 profile → 按引擎优先级排序；未声明 group 的不出现。
2. `test_resolve_model_group_prefers_running`：mock `is_running`/`is_model_healthy`，高优先级成员不健康时选低优先级健康成员。
3. `test_resolve_model_group_none_running`：组内全不健康 → None。
4. `test_resolve_model_group_fallback_default`：body_model 未知、default_model 是 group 名 → 家族解析。
5. `test_list_models_group_shown`：组内有健康成员 → /v1/models 含家族逻辑名；全不健康 → 不含。
6. `test_proxy_group_routes_to_running_member`：端到端，请求 `model=qwen3.8` → 转发到运行成员，body.model 改写为该成员 upstream_model。

`tests/test_profile.py`（若存在）：`group` 字段解析（缺省 None / 合法字符串 / 空值忽略）。

## 6. 兼容性与风险

- **向后兼容**：未加 group 的模型行为不变；`build_registry`/`resolve_model` 保持原签名兼容（`groups` 缺省 None）。
- **优先级顺序依赖**：引擎优先级为硬编码常量；新增引擎类型需同步维护（default 99 兜底）。
- **多次健康探测**：家族解析与 /v1/models 均调用 `is_model_healthy`（单次 2s 超时探测），组内成员多时会串行探测。当前每族最多 10 个成员，量级可接受；若后续族内成员过多，可改为并发探测。
- **文档同步**：`.env.example` 中 `GATEWAY_DEFAULT_MODEL` 注释需更新（裸名 qwen3.8 现走家族路由，不再必然 502）。
