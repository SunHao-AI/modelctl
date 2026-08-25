# Qwen3.8-vLLM 兼容性问题排查指南

> 适用：vLLM 0.27.1 + Qwen3.8-27B（Qwen3.5 架构，`Qwen3_5ForConditionalGeneration`）
> 记录时间：2026-08-25
> 触发场景：vLLM 升级到 0.27.1 后，Trae CN / Claude Code 无法正常使用 qwen3.8-vllm

---

## 一、问题现象

升级 vLLM 到 0.27.1 后，两个客户端出现不同症状：

| 客户端 | 协议 | 现象 |
|---|---|---|
| Trae CN（`OpenAI Chat Completions` 格式） | `/v1/chat/completions` | 有输入无输出，一段时间后对话直接结束；请求返回 200 但前端不显示内容 |
| Claude Code（Anthropic 协议） | `/v1/messages` | 等待很久后报 `API Error: 500 Unexpected reasoning effort high. Supported types are xhigh (default), medium, and low.` |

网关日志（`logs/launch-llm-gateway.log`）显示：Trae 发 1 条消息产生 3 个重复请求（客户端自动重试）；Claude Code 的 `/v1/messages` 请求持续 500。

---

## 二、根因分析

共定位 5 个叠加问题，全部由 **vLLM 0.27.1 相对旧版本的行为变化**引入。

### 1. Qwen3.5 家族模板强制思考（Trae CN 无输出）

Qwen3.8 的 chat template 强制把 `<think>` 放入 prompt，模型总是先输出思考过程。vLLM 0.27.1 的 `--reasoning-parser qwen3` 把思考内容路由到 `delta.reasoning` 通道，**`delta.content` 长时间为空**。Trae CN 等客户端不渲染 reasoning 通道 → 表现为"有输入无输出"。

### 2. 网关破坏 SSE 流式格式（Trae CN 无输出、自动重试）

网关旧版 `_sse_stream` 按行重组 SSE 响应，**丢失了事件之间的空行分隔符**（vLLM 输出 `data: {...}\n\n`，网关却产出 `data: {...}\n`）。Trae CN 的 SSE 解析器等待空行结束事件却永远等不到 → 内容不触发 → 认为响应失败 → 重试 3 次后放弃。

> Claude Code 之所以正常，是因为它走 Anthropic 端点（`_raw_sse` 原始字节透传），SSE 格式从未被破坏。

### 3. reasoning_effort 枚举不兼容（Claude Code 500）

Qwen3.8 的 `chat_template.jinja` **只接受 `reasoning_effort ∈ {xhigh, medium, low}`**，其余值直接 `raise_exception`。

Claude Code **默认每个请求都携带 `"output_config": {"effort": "high"}`**（低/中/高/xhigh 五档的默认档），vLLM 的 Anthropic 端点把它原样传给模板 → `high` 不在枚举内 → 500。

官方 issue：[QwenLM/Qwen3.8#217](https://github.com/QwenLM/Qwen3.8/issues/217)

### 4. 客户端 API key 与后端不一致（401）

客户端配置的 key（如 `root123456`）可能与 vLLM 实际校验的 key（`--api-key`，来自 `.env API_KEY`）不一致。网关旧逻辑在 `up_key == target.api_key` 时透传客户端 key，导致 vLLM 返回 401。

### 5. Anthropic 协议端点缺失（Claude Code 404）

网关原先只提供 OpenAI 端点（`/v1/chat/completions` 等），不识别 Anthropic 的 `/v1/messages`，Trae CN / Claude Code 走 Claude 协议时返回 404。

---

## 三、修复方案

### 修复 1：服务端默认关闭思考

`models/vllm/qwen3.8.yaml` 的 `extra_args` 增加：

```yaml
extra_args: "--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder --generation-config vllm --default-chat-template-kwargs '{\"enable_thinking\": false}' --max-num-seqs 4"
```

- `--default-chat-template-kwargs '{"enable_thinking": false}'`：服务端默认关闭思考（ModelScope 官方教程推荐），无论经网关还是直连 vLLM 都直接输出正文；请求显式传 `chat_template_kwargs` 仍可覆盖。
- `--max-num-seqs 4`：官方推荐并发上限。

网关侧兜底（`src/modelctl/core/gateway.py`）：对 qwen3.8 家族（`_THINKING_DISABLED_GROUPS`）且支持 `chat_template_kwargs` 的引擎（vllm/sglang/unsloth），请求体未显式传 `chat_template_kwargs` 时注入 `{"enable_thinking": false}`。

### 修复 2：SSE 流式原始字节透传

`src/modelctl/core/gateway.py` 的 `_sse_stream` 改为：

- **原始字节透传**（`yield chunk` 保留 SSE 空行分隔与完整结构），不再按行重组改写；
- 仅**旁路解析** `data:` 行做用量统计与响应摘要日志。

```python
async def _sse_stream(upstream=upstream, client=client):
    pending = b""
    collected = {"content": [], "reasoning": [], "tool_calls": False}
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk  # 原始透传，保留 SSE 格式
            pending += chunk
            # 旁路解析 data: 行，仅做统计，不改写输出
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == b"[DONE]":
                    continue
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                _record_usage(data, seen_tokens)
                # ... 累计 content / reasoning / tool_calls 供响应摘要日志
    finally:
        if pending:
            yield pending
        logger.info(f"OpenAI 流式响应摘要 content=... reasoning_len=... tool_calls=...")
        await client.aclose()
```

### 修复 3：reasoning_effort 枚举映射

`src/modelctl/core/gateway.py` 的 `_normalize_reasoning_effort` 统一映射不兼容枚举（`high/ultra/extreme → xhigh`，`balanced → medium`，`minimal → low`），覆盖 **4 个字段位置**：

1. 顶层 `reasoning_effort`
2. `thinking.effort`（Anthropic thinking 参数）
3. `reasoning.effort`（Anthropic reasoning 参数）
4. **`output_config.effort`**（Claude Code 新版协议默认携带，关键漏网之鱼）

```python
_REASONING_EFFORT_MAP = {
    "high": "xhigh", "ultra": "xhigh", "extreme": "xhigh",
    "balanced": "medium", "minimal": "low",
}
```

OpenAI 与 Anthropic 两个端点都调用此函数。

### 修复 4：认证统一使用 profile 有效 key

`src/modelctl/core/gateway.py` 中 OpenAI / Anthropic 端点转发时：

```python
up_key = target.upstream_api_key()
if up_key:
    auth = f"Bearer {up_key}"  # 用 profile 有效 key 覆盖客户端 key
```

Anthropic 端点额外同时设置 `x-api-key` 和 `Authorization: Bearer`（vLLM 0.27.1 的 `/v1/messages` 只认后者）。

### 修复 5：新增 Anthropic `/v1/messages` 透传端点

`src/modelctl/core/gateway.py` 新增 `POST /v1/messages`：

- 按 `body.model` 路由到后端 vLLM（vLLM 0.27+ 原生支持 Anthropic 端点）
- 透传 `x-api-key` / `Authorization` / `anthropic-version` / `anthropic-beta` 头
- 流式**原始字节透传**，保留 `event:` 行（Claude SDK 依赖事件类型）
- Claude Code 新版 `thinking: {"type": "adaptive"}` 转为 vLLM 支持的 `disabled`

---

## 四、部署步骤

```bash
cd /raid5/sh/code/modelctl
# 1. 同步代码（gateway.py、models/vllm/qwen3.8.yaml、tests/）
git pull   # 或 scp 同步

# 2. 重启 vLLM（加载新的 extra_args）
modelctl stop qwen3.8-vllm
modelctl start qwen3.8-vllm

# 3. 重启网关
modelctl gateway restart
```

## 五、验证方法

```bash
# 1. 确认 vLLM 加载了新参数
grep -o "default_chat_template_kwargs[^,]*" logs/launch-qwen3.8-vllm.log | head -1

# 2. 观察网关日志（含请求元数据 + 响应摘要）
tail -f logs/launch-llm-gateway.log
```

正常特征：

| 日志 | 正常表现 |
|---|---|
| `OpenAI 流式响应摘要` | `content='你好！...'` 有正文、`reasoning_len=0`、`tool_calls=False` |
| `output_config.effort 兼容映射` | `high -> xhigh`（Claude Code 请求时出现） |
| `thinking.type adaptive -> disabled` | Claude Code 请求时出现 |
| Trae CN 发 1 条消息 | 网关**只出现 1 个请求**（不再 3 次重试） |

---

## 六、注意事项

1. **qwen3.8 默认关闭思考**（服务端 + 网关双层保障）。需要思考能力的请求显式传 `chat_template_kwargs: {"enable_thinking": true}`（OpenAI）或 `thinking: {"type": "enabled", "budget_tokens": N}`（Anthropic）即可。
2. **vLLM 再升级版本时**，优先检查三处兼容性：`reasoning_effort` 枚举、`thinking` 类型（adaptive）、Anthropic 端点认证方式。
3. **SSE 透传必须保留原始格式**，任何按行重组都可能破坏事件分隔。
4. `--kv-cache-memory` 不要手动指定：vLLM ≥0.21 默认启用 CUDA graph memory profiling，旧值在新版本下会 OOM，交由 `gpu_memory_utilization` 自动分配。

---

## 七、相关文件

| 文件 | 作用 |
|---|---|
| `src/modelctl/core/gateway.py` | `/v1/messages` 透传、effort 映射、thinking 转换、SSE 原始透传、认证统一、响应摘要日志 |
| `models/vllm/qwen3.8.yaml` | `extra_args`（默认关闭思考、`max-num-seqs 4`） |
| `tests/test_gateway.py` | 13 个新增测试覆盖上述修复 |
