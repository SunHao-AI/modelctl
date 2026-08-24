# status 主动测速 + 首 Token 耗时设计

- 日期：2026-08-24
- 状态：已确认（用户逐节评审通过）
- 关联：`modelctl status <name>` 的 Token 速率显示（`cli.py._live_token_rate_text`）

## 1. 背景与目标

### 问题

`modelctl status qwen3.8-vllm` 显示 `Token 速率：输入 0.0 tok/s，输出 0.0 tok/s`，不合理。

根因：速率来自用量统计服务（stats，端口 5002）的窗口采样（[stats.py](stats.py) `_compute_window_rate`）：窗口内不足 2 个采样点、或采样间隔内 token 计数无增量（模型刚启动/无请求）时，速率为 0.0。

### 目标

1. 速率显示**优先使用 stats 实时数据**；stats 无数据或速率为 0 时，**主动对模型发一次短请求实测**。
2. 新增**首 Token 耗时（TTFT）**信息。
3. 测速不引入新依赖（主依赖无 openai/httpx），不阻塞 status 输出（失败降级为 `-`）。

### 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 测速方式 | stats 优先（速率 > 0 时用 stats）；stats 无效/为 0 时主动测速 |
| 测速请求 | 轻量：短 prompt（"hi"）+ `max_tokens=64` + `stream=true` + `stream_options.include_usage` |
| 速率定义 | 输入速率 = `prompt_tokens / TTFT`（prefill）；输出速率 = `completion_tokens / (总耗时 − TTFT)`（decode） |
| TTFT | 首个流式 chunk 到达耗时（对 reasoning 模型为思考内容开始时间） |
| 超时 | 整体 10s；失败返回 None，显示 `-` |
| 依赖 | 纯标准库（urllib + SSE 手动解析），无新依赖 |

## 2. 总体设计

### 2.1 数据流

```
modelctl status <name>
        │
        ▼
_live_token_rate_text(profile)
  ├─ 1. 查 stats：GET 127.0.0.1:5002/api/usage?model=<name>
  │      ├─ isValid 且 prompt_rate>0 或 predicted_rate>0 → 用 stats 值（来源 stats）
  │      └─ 否则 → 步骤 2
  ├─ 2. 主动测速 _benchmark_token_rate(adapter)
  │      ├─ 成功 → (prompt_rate, predicted_rate, ttft_ms)（来源 实测）
  │      └─ 失败/超时 → None → "输入 -，输出 -（测速失败）"
  │
  ▼
输出行（来源不同，追加到速率行后的首 Token 耗时行）：
  Token 速率：输入 X.X tok/s，输出 X.X tok/s（实测）
  首 Token 耗时：XXX ms
```

### 2.2 `_benchmark_token_rate(adapter) -> tuple[float, float, int] | None`

- 构造请求：
  - URL：`http://127.0.0.1:{profile.port}/v1/chat/completions`
  - headers：`Content-Type: application/json`；有 api_key 时 `Authorization: Bearer <upstream_api_key>`
  - body：`{"model": upstream_model_name, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64, "stream": true, "stream_options": {"include_usage": true}}`
- 测量：
  - `t_start` 于请求发出前
  - 逐行解析 SSE（`data: ` 前缀行），**首个非 `[DONE]` 的 `data:` 事件**到达时刻记录为 `t_ttft`（无论 delta 内容是否为空；对 reasoning 模型即思考首 token 到达时刻）
  - 流结束（`data: [DONE]` 或连接关闭）记录 `t_end`
  - 从带 `usage` 的 chunk 提取 `prompt_tokens` / `completion_tokens`（缺失时按 `prompt 字符/4`、`completion 字符/4 or 1` 估算）
- 计算：
  - `ttft_s = t_ttft - t_start`（若无任何 chunk 则返回 None）
  - `input_rate = prompt_tokens / ttft_s`
  - `decode_s = (t_end - t_start) - ttft_s`；`output_rate = completion_tokens / decode_s`（decode_s <= 0 时输出速率置 0）
- 超时：`urllib.request.urlopen(url, data=..., timeout=10)`；任何异常 → 返回 None
- SSE 解析用 `io.TextIOWrapper` / 逐行 readline，限制单行长度避免超大行

### 2.3 `_live_token_rate_text` 增强

现有逻辑（查 stats）保留，改动点：

- stats 返回 `isValid=True` 且 `prompt_rate > 0 或 predicted_rate > 0` → 返回现有格式（来源 stats，无 TTFT）
- 否则 → 调用 `_benchmark_token_rate`：
  - 成功 → 返回 `"输入 X.X tok/s，输出 X.X tok/s（实测）"`，并携带 TTFT
  - 失败 → 返回 `"输入 -，输出 -（测速失败）"`
- 返回结构需要向调用方传递 TTFT 与来源，因此把文本生成与数据分离：

```python
def _token_rate_data(profile) -> dict:
    """返回 {"prompt_rate": float|None, "predicted_rate": float|None, "ttft_ms": int|None, "source": "stats"|"bench"|None}"""
```

`_cmd_status` 展示：

```
Token 速率：输入 X.X tok/s，输出 X.X tok/s（实测 | stats）
首 Token 耗时：XXX ms（-）
```

（来源标注仅当实测时显示"（实测）"；stats 时不标注。）

### 2.4 输出格式

```
Token 速率：输入 123.4 tok/s，输出 45.6 tok/s（实测）
首 Token 耗时：456 ms
```

- stats 来源：`Token 速率：输入 123.4 tok/s，输出 45.6 tok/s`（无来源标注）；`首 Token 耗时：-`
- 测速失败：`Token 速率：输入 -，输出 -（测速失败）`；`首 Token 耗时：-`
- 模型未运行/健康检查失败：维持现状（不测速）

## 3. 错误处理

| 场景 | 行为 |
|---|---|
| stats 有效且速率 > 0 | 用 stats，不测速（零开销） |
| stats 无数据/速率为 0 | 主动测速一次 |
| 测速请求超时（10s）/连接失败/非 2xx | 返回 None → `输入 -，输出 -（测速失败）` |
| 流中无任何 chunk（空响应） | 返回 None（无 TTFT 无法计算输入速率） |
| 模型未运行 | `_cmd_status` 已先判定状态，运行中才测速（沿用现状） |
| SSE 行解析异常 | 单行失败跳过，整体异常走失败降级 |

## 4. 测试计划

新增 `tests/test_cli.py`（若不存在则建）或复用既有 CLI 测试文件：

1. `test_token_rate_data_uses_stats_when_valid`：mock stats 返回 `isValid=True, prompt_rate=10, predicted_rate=20` → 用 stats，不调用测速
2. `test_token_rate_data_benchmarks_when_stats_zero`：mock stats 返回 `isValid=True, prompt_rate=0, predicted_rate=0` → 调用测速
3. `test_token_rate_data_benchmarks_when_stats_unavailable`：mock stats 请求抛 OSError → 调用测速
4. `test_benchmark_parses_sse_and_computes_rates`：mock `urllib.request.urlopen` 返回带 SSE 数据的假响应（首 chunk 时间可控、usage 带 token 计数）→ 断言 prefill/decode 速率与 TTFT
5. `test_benchmark_timeout_returns_none`：mock urlopen 抛超时 → None
6. `test_status_output_shows_ttft`：端到端（mock 测速返回固定值）→ 输出含 `首 Token 耗时`

## 5. 兼容性与风险

- **向后兼容**：stats 有数据时输出格式不变；新增行仅 `首 Token 耗时`
- **无新依赖**：urllib + 手写 SSE 解析（标准库）
- **status 耗时**：仅在 stats 无数据时触发测速（最多 10s 超时），通常 2-5s
- **reasoning 模型**：TTFT 含思考内容开始时间，文档注明语义
- **并发**：status 为 CLI 单次命令，无并发问题
