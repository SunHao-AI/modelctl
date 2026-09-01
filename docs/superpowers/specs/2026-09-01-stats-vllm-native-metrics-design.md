# stats 模块对齐 vLLM 原生 per-request metrics 设计

- 日期：2026-09-01
- 状态：已确认（用户逐节评审通过）
- 关联：[stats.py](stats.py)、[gateway.py](gateway.py)、[engines/base.py](engines/base.py)、[engines/vllm.py](engines/vllm.py)、`cli.py._token_rate_data`
- 前置 spec：`2026-08-24-status-token-rate-design.md`（主动测速）、`2026-08-31-per-request-audit-design.md`（审计落盘）

## 1. 背景与目标

### 问题

`modelctl` 目前对 token 速率与 TTFT 的呈现走 [gateway.py](gateway.py) 的 `collector.record_tokens`（按真实请求 usage 累计）与 [stats.py](stats.py) 的 `_compute_window_rate`（累计差分）+ `_bench_cached`（窗口无流量时的假请求 benchmark 兜底）。这条链路只看得到**总量**，看不到 vLLM 在开启 `--enable-per-request-metrics` + `--enable-force-include-usage` 后**已经在 SSE 末块 / 非流式响应根级**给出的 per-request 原生指标：

```json
"metrics": {
  "arrival_time": 1756692345.123456,
  "first_scheduled_time": 1756692345.234567,
  "first_token_time": 1756692345.345678,
  "last_token_time": 1756692345.567890,
  "num_generation_tokens": 128,
  "num_prompt_tokens": 32,
  "time_to_first_token_ms": 150.0,
  "time_to_last_token_ms": 223.6,
  "queue_time_ms": 111.0,
  "generation_time_ms": 222.22,
  "mean_itl_ms": 1.55,
  "tokens_per_second": 575.52
}
```

### 目标

1. **默认采用 vLLM 原生 token 信息**：`/api/usage` 的速率与"首 Token 耗时"以 vLLM 提供的 per-request metrics 为**第一真相**，聚合口径与 vLLM 自己 `/metrics` 面板上给的 `vllm:avg_generation_throughput_toks_per_sec` / `vllm:time_to_first_token_seconds` Histogram 算法**保持一致**（不做二次加工）。
2. **兜底也严格对齐**：当 vLLM 原生指标缺失（vLLM < 0.13.0 / 未开 flag / 客户端绕开网关）时，退化为现有窗口差分 + 假请求 benchmark，但**伪 SDK 请求的速率计算口径**（输入速率=prompt/TTFT，输出速率=completion/(总耗时-TTFT)）保持与 vLLM per-request 语义一致（事实上已经一致）。
3. **第三方依赖不增**：本期不做 audit/jaeger 链路追踪、不做非 vLLM 引擎的 native 适配（`base.py` 默认返回 `None`）、不输出 P99 等多分位、不改 `/api/usage` 现有字段语义（只*追加* `ttft_ms` / `ttft_ms_p95` / `rate_source` 三个字段，cc-switch 不读不影响）。

### 已确认的关键决策（brainstorming 阶段用户确认）

| 决策点 | 结论 |
|---|---|
| 数据接缝 | **方案 A：网关为单一接缝**。网关在 `_sse_stream` / 非流式分支已解析 `seen_metrics`（审计已用），本次*额外*喂给 stats collector；不改 audit 链路，不让 collector 直接 HTTP poll per-request metrics |
| native 聚合口径 | **与 vLLM 保持一致**。vLLM 不聚合最近 N 个请求的 `tokens_per_second`，stat 侧也不二次加工：native 专属指标（TTFT / per-request P50）走 §3.2 的 per-request 滑窗口径；速率仍渐进沿用现状 `metrics_mapping` 的 engine gauge 路径（客户端直连可覆盖），并新增 per-request P50 作为 gauge 缺失时的中间档 |
| 窗口时长 | `max(60s, 20 请求)` 双约束（与现有 `_rate_window` 不同，原生 metrics 单独开一条） |
| 兜底保留 | **可配置开关** `USAGE_BENCH_FALLBACK`（默认 `true` 保持现状）；stats 任一 native 字段非 0 时跳过 served 假请求 benchmark；cli 也是同样逻辑 |
| TTFT 输出 | **P50 为主**（`ttft_ms`）+ **P95 为可选**（`ttft_ms_p95`）；不输出 P99 |
| 本期适配范围 | **仅严格 vLLM**。`base.py` 加 `native_metrics_mapping()` 默认 `None`，Phase 2 再扩 aphrodite/lmdeploy/sglang 等 |
| 启动参数校验 | `enable_per_request_metrics=true` 且 `enable_force_include_usage=false` 时 `check_requirements` 写**非硬拦截** warning（该 yaml 上速度/TTFT 会缺项） |

## 2. vLLM 对齐口径（参考事实）

> 已在本机 `envs/vllm` 环境实测核验，用于作为聚合语义的锚点。

### 2.1 单请求 decode 速率

vLLM 源码 `vllm/entrypoints/openai/serving_metrics.py`：

```python
tokens_per_second = num_generation_tokens / (last_token_ts - first_token_ts)
# 分子：completion_tokens（不含 prompt 不下采样）
# 分母：last_token_time - first_token_time（即纯 decode 段，不含 TTFT 与 queue）
```

**含义**：仅 decode 阶段 tokens/s；首次 forward pass（prefill）时间被排除在分母外。

### 2.2 单请求 prompt 速率（vLLM 未直接给，stats 侧推导）

vLLM 未在 per-request `metrics` 里给显式 prompt throughput 字段，但 `time_to_first_token_ms` + `num_prompt_tokens` 可直接推：

```
prompt_inflight_rate = num_prompt_tokens / max(time_to_first_token_ms / 1000.0, EPS)
```

**语义**：与 vLLM 的 prefill throughput gauge `vllm:avg_prompt_throughput_toks_per_sec` = `prompt_tokens_in_chunk / (now - last_log)` **同量纲**（都是 prefill 期间处理的 tokens/s），差别仅在分母窗口宽度（单请求 vs 滑窗），统计意义上等价。

### 2.3 速率 gauge 与窗口

`vllm:avg_generation_throughput_toks_per_sec` / `vllm:avg_prompt_throughput_toks_per_sec` 是 vLLM 内部的**全局滑窗 gauge**（内部窗口 ≈ 8-15s，随 `LoggingUtility` 刷新频率变化）。stats 侧本轮新增的 per-request 滑窗用 `max(60s, 20 请求)`（更宽），两者互为备份（见 §3.2 完整的 4 档优先级）：

- **档 2 优先**：gauge 非 0（客户端直连 vLLM 端口绕过网关也能拿到真实吞吐——这是现状 `metrics_mapping` 链路已提供的行为，保持不变）
- **档 1 补齐**：当 gauge 缺失/0、但 per-request native 信号有值时取 per-request 滑窗 P50（仅覆盖经网关的集；vLLM 0.84 缺 gauge 时的中间档）
- **档 3/4 退化**：voll-m 两档 1/2 都为 0 时退化；最终兜底 `_bench_cached`（served 端假请求）受 `USAGE_BENCH_FALLBACK` 控制

### 2.4 TTFT 分位

vLLM `vllm:time_to_first_token_seconds` Harness Histogram 的 P50 等价于"最近 N 个请求 `time_to_first_token_ms` 的中位数"。stats 侧本次在 per-request 滑窗上算 P50 与 P95 两个值（`statistics.median` + 手工 P95），**不做** vLLM Histogram 上的桶对齐、不上报到 Prometheus。

### 2.5 触发条件

| vLLM flag | 作用 |
|---|---|
| `--enable-per-request-metrics` | 在 SSE 末块 / 非流式响应根级挂 `metrics` 对象 |
| `--enable-force-include-usage` | 强制流式每块（含末块）都带 `usage`，供 stats collector 累计 token 入账；不影响 `metrics` 是否出现 |

**两 flag 独立性**：
- `enable_per_request_metrics=true` + `enable_force_include_usage=false`：`metrics` 对象存在但流式中间块没有 usage——stats 的 `collector.record_tokens` 会漏掉中间增量，仅末块入账（现网 vLLM 0.84 默认行为）。**启动时给 warning**。
- `enable_force_include_usage=true` + `enable_per_request_metrics=false`：usage 全量入账，但没有 per-request 指标，本次 native 链路不起作用，仅走旧窗口差分。
- 两者均开：本次目标状态，`metrics` + per-request 完整可用。

## 3. 总体设计

### 3.1 数据流

```
客户端任意 → 网关 5003
             │
             ├─ _sse_stream（已产出 seen_metrics）
             │       ├─ collector.record_tokens(...)     ← 现有：per-块增量入账（vLLM 0.84 必要 force flag）
             │       └─ collector.record_native_metrics(seen_metrics)  ← 新增：喂末块原生指标
             │
             └─ 非流式分支（已解析根级 metrics）
                     ├─ collector.record_tokens(prompt, completion)  ← 现有
                     └─ collector.record_native_metrics(_native)      ← 新增
                              │
                              ▼
                    UsageCollector._native_window (deque，max(60s, 20 请求))
                              │
                              ▼  _compute_native_percentiles
                    snapshot() 追加字段：
                      ttft_ms              ← per-request 滑窗 time_to_first_token_ms P50
                      ttft_ms_p95          ← per-request 滑窗 P95
                      rate_source          ← "native" | "engine_gauge" | "window_diff" | "bench" | "none"
                              │
                              ▼
                    /api/usage（现有字段不变，追加三字段；cc-switch 忽略不影响）
```

### 3.2 兜底三档优先级（每个字段各解）

对 `prompt_rate`、`predicted_rate`、`ttft_ms`、`ttft_ms_p95` 四个字段各解：

| # | 档 | 含义 |
|---|---|---|
| 1 | native | per-request 滑窗 P50（仅 vLLM 该 yaml 双 flag 均开、实际经网关请求过流量才有） |
| 2 | engine_gauge | vLLM `/metrics` 上的 rate gauge（客户端直连也能触发），缺失/0 跳过 |
| 3 | window_diff | 现有 `_compute_window_rate`（仅统计经网关或已 token 入账的流量） |
| 4 | bench | 现有 `_bench_cached` served 假请求，**仅当上述全部为 0** 且 `USAGE_BENCH_FALLBACK=true` 时调用 |

每个字段**独立判定**（避免"输入速率有值、输出速率为 0"时整体跳过——现状 bug 语义保留）。`rate_source` 字段为四字段中*实际显示为有效值的最高档*（benchmark 在 handler 层覆盖后需回写标签，见 3.4 节）。

### 3.3 滑窗结构

```python
# core/stats.py
@dataclass
class _NativeSample:
    ts: float                    # time.monotonic 入账时
    tokens_per_second: float     # vLLM 原生 decode 速率
    prompt_inflight_rate: float  # num_prompt_tokens / (ttft_s)
    ttft_ms: float               # time_to_first_token_ms
    ttft_s: float                # 同上（秒）

# 字段内嵌到 UsageCollector.__init__：
self._native_window: list[_NativeSample] = []
self._native_window_ttl = 60.0
self._native_window_cap = 20
```

**裁剪规则**（`record_native_metrics` 时）：
1. 追加 `(now, sample)`
2. 从最旧起 pop 直到满足：`now - oldest.ts <= ttl` **且** `len <= cap`（**两个约束独立都必须满足**，即窗口是 `max(ttl, 20 请求)` 表达为"两项都不超过"）

**注意**：纯按时间或纯按容量裁剪都会引入偏差（高并发时 60s 可能塞进 5000 请求；低并发时 5 分钟后 20 个请求都还在窗口里）。简单合理语义是"限制规模和时龄两项都不超过"。

### 3.4 `rate_source` 确定规则

**分两层实现**：

1. **collector.snapshot() 内部**（4 档取值缺 bench）：按 4 条规则按下顺序首命中：
   - **`native`**：per-request 滑窗 P50 至少一个非 0（档 1）。
   - **`engine_gauge`**：轮询时 gauge 值写入快照（`_poll_once` 行 456-458 链路）且非 0（档 2）。
   - **`window_diff`**：由 `record_tokens` 推入差分窗口且 `_compute_window_rate` 出非 0（档 3）。
   - **`none`**：四字段全 0。

2. **`UsageHandler._build_target_payload` 外层修正**：当 bench 覆盖了 prompt_rate / predicted_rate / ttft_ms 任一字段时，把 `rate_source` **重写为 `bench`**（覆盖 snapshot 里的 `none`）。这是唯一允许把 `none` → 其他值的 override（bench 与 native 互斥：native 任一非 0 时不会走 bench，见 4.5 gate）。

**不可能出现的组合**：
- 同时 `rate_source = native` 又有字段由 bench 填——gate 阻止 bench 在有 native 信号时运行。
- `rate_source = window_diff` 又有字段由 bench 填——handler 层会把它重写为 `bench`。
- `rate_source = engine_gauge / native` 同时字段被 bench 填——同样重写成 `bench`。

**展示价值**：`rate_source` 为非空字符串时允许 CLI 侧加"（实时）"标记（见 4.9 注记——本期不加，只保留数据）。

### 3.5 与现有"窗口差分"档（档 3）的关系

`_compute_window_rate` 使用最近 `self._window_size`（=10）个采样点的差分，本质上是"以 total 计数的增量 / 时间差"，在 vLLM force flag 下会将 prefill+decode 段均摊到统一分母——**与 per-request `tokens_per_second`（仅 decode 段）语义不一致**，但作为 档3 退化回退值可接受（这是现状行为，保持不变）。

档 4（`_bench_cached`）的 `_benchmark_rates` 计算口径**与 per-request P50 语义一致**（输入速率=prompt/TTFT、输出速率=completion/(总耗时-TTFT)），本期保持不变。

### 3.6 `build_usage_payload` 新增 extra 行

`extra` 字符串末尾追加 TTFT 信息（仅当 `ttft_ms > 0`）：

```
| 首 Token P50 = 123 ms（P95 = 210 ms）
```

`payload` dict 同步增加 `ttft_ms: int` 与 `ttft_ms_p95: int`（键存在但值为 0/None 时 cc-switch 忽略）。

## 4. 详细改动

### 4.1 `engines/base.py`

新增抽象方法（默认 `None`），与 `metrics_mapping` 并列：

```python
def native_metrics_mapping(self) -> dict[str, str] | None:
    """per-request 原生指标字段名映射。

    {
      "rate":              "tokens_per_second",
      "ttft_ms":           "time_to_first_token_ms",
      "gen_time_ms":       "generation_time_ms",
      "prompt_tokens":     "num_prompt_tokens",
      "completion_tokens": "num_generation_tokens",
    }
    返回 None 表示该引擎不提供 per-request 原生指标（stats 侧自动跳过档1）。
    """
    return None
```

> **键含义**：adapter 返回 vLLM `metrics` 对象中真实字段名；stats 侧按 5 个键映射到内部 `_NativeSample` 字段。键命名保留 `prompt_tokens` / `completion_tokens`（与 `usage` 字段同语）提高可读性，adapter 映射到 vLLM 实际字段名 `num_prompt_tokens` / `num_generation_tokens`。

### 4.2 `engines/vllm.py`

实现覆盖：

```python
def native_metrics_mapping(self) -> dict[str, str]:
    return {
        "rate": "tokens_per_second",
        "ttft_ms": "time_to_first_token_ms",
        "gen_time_ms": "generation_time_ms",
        "prompt_tokens": "num_prompt_tokens",
        "completion_tokens": "num_generation_tokens",
    }
```

`check_requirements` 追加（在 venv / docker 两个分支*都*做出，不依赖版本探测）：

```python
per_request_on = bool(cfg.get("enable_per_request_metrics"))
force_on = bool(cfg.get("enable_force_include_usage"))
if per_request_on and not force_on:
    self.warnings.append(
        f"{self.profile.name}：enable_per_request_metrics=true 但 enable_force_include_usage=false，"
        "流式中间块缺 usage 会使 stats.record_tokens 仅末块入账；建议同时开启 force flag"
    )
```

其余行为（含 `MIN_VLLM_PER_REQUEST` 版本门控）不动。

### 4.3 `core/stats.py` — `UsageCollector`

4.3.1 **新增构造入参**（保持兼容：默认 None，调用方 `run_server` 显式注入）：

```python
def __init__(
    self,
    name: str, base_url: str, poll_interval: float,
    api_key: str | None, data_dir: Path,
    mode: str = "poll",
    mapping: dict[str, list[str]] | None = None,
    native_mapping: dict[str, str] | None = None,      # ← 新增
    bench_fallback: bool = True,                        # ← 新增，env USAGE_BENCH_FALLBACK
) -> None:
    ...
    self.native_mapping = native_mapping
    self.bench_fallback = bench_fallback
    self._native_window: list[_NativeSample] = []
    self._native_window_ttl = 60.0
    self._native_window_cap = 20
```

4.3.2 **新增 `record_native_metrics` 方法**：

```python
def record_native_metrics(self, metric_dict: dict | None) -> None:
    """网关每请求喂入 per-request 原生指标对象。

    - metric_dict 为 vLLM SSE 末块 / 非流式根级的 "metrics" 对象；
    - adapter.native_metrics_mapping() 为 None 时此引擎不支持，静默返回；
    - 仅 vLLM 双 flag 均开的 yaml 会收到带 5 个字段的 dict，否则返回；
    - 非数值/缺失字段直接跳过（不推入窗口，避免 N/A 计入中位数）。
    """
    if not self.native_mapping or not isinstance(metric_dict, dict):
        return
    mapping = self.native_mapping
    try:
        tps = float(metric_dict[mapping["rate"]])
        ttft_ms = float(metric_dict[mapping["ttft_ms"]])
        prompt_tk = int(metric_dict.get(mapping["prompt_tokens"]) or 0)
    except (KeyError, TypeError, ValueError):
        return
    if tps <= 0 or ttft_ms <= 0:
        return
    ttft_s = ttft_ms / 1000.0
    # prompt 速率由 num_prompt_tokens / ttft_s 推导（语义与 vLLM avg_prompt gauge 同量纲）
    prompt_rate = (prompt_tk / ttft_s) if ttft_s > 1e-6 else 0.0
    sample = _NativeSample(
        ts=self._monotonic(),
        tokens_per_second=tps,
        prompt_inflight_rate=prompt_rate,
        ttft_ms=ttft_ms,
        ttft_s=ttft_s,
    )
    now = sample.ts
    with self._lock:
        self._native_window.append(sample)
        # 双约束裁剪：时龄 <= ttl 且 容量 <= cap
        while self._native_window and (
            now - self._native_window[0].ts > self._native_window_ttl
            or len(self._native_window) > self._native_window_cap
        ):
            self._native_window.pop(0)
```

4.3.3 **新增 percentiles 辅助**（模块级私有助手，stats.py 顶部）：

```python
def _percentile(values: list[float], p: float) -> float | None:
    """简单最近邻法百分位（vLLM Histogram 的 P50=P50 分点语义近似）；空列表返回 None。"""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * (p / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac
```

4.3.4 **`_compute_native_row` 内嵌方法**：

```python
def _compute_native_row(self) -> dict:
    """基于 per-request 滑窗算 native 行的 P50/P95。空滑窗时 has_any=False 返回四 0。"""
    with self._lock:
        samples = list(self._native_window)
    if not samples:
        return {"ttft_ms": 0.0, "ttft_ms_p95": 0.0, "prompt_rate": 0.0, "predicted_rate": 0.0,
                "has_any": False}
    tps_vals = [s.tokens_per_second for s in samples]
    prompt_vals = [s.prompt_inflight_rate for s in samples]
    ttft_vals = [s.ttft_ms for s in samples]
    predicted_p50 = _percentile(tps_vals, 50) or 0.0
    prompt_p50 = _percentile(prompt_vals, 50) or 0.0
    ttft_p50 = _percentile(ttft_vals, 50) or 0.0
    ttft_p95 = _percentile(ttft_vals, 95) or 0.0
    return {
        "ttft_ms": round(ttft_p50, 2),
        "ttft_ms_p95": round(ttft_p95, 2),
        "prompt_rate": round(prompt_p50, 2),
        "predicted_rate": round(predicted_p50, 2),
        "has_any": True,
    }
```

4.3.5 **`snapshot()` 重写**，合并三档 + 写入 4 个新字段（bench 档在 handler 层处理，collector.snapshot 内部 4 值不含 bench）：

```python
def snapshot(self) -> dict:
    with self._lock:
        base = dict(self._snapshot)
    full_row = self._compute_native_row()
    # 合并到 base：native 优先 → 否则用 base 内原 gauge/window_diff 值
    base["prompt_rate"] = full_row["prompt_rate"] or base.get("prompt_rate") or 0.0
    base["predicted_rate"] = full_row["predicted_rate"] or base.get("predicted_rate") or 0.0
    # 新增三字段
    base["ttft_ms"] = full_row["ttft_ms"]
    base["ttft_ms_p95"] = full_row["ttft_ms_p95"]
    # rate_source：按 §3.4 首命中
    if base.get("rate_source", "none") == "none":  # 尚未被 _poll_once/_record 设过时才覆写
        if full_row["has_any"] and (base.get("prompt_rate") or base.get("predicted_rate") or base.get("ttft_ms")):
            base["rate_source"] = "native"
    return base
```

> **实现注记**：
> - `rate_source` 在 `_poll_once` 中按"当前末次轮询取值是 gauge 还是 window_diff"直接写；`record_tokens` 后如果 rate 仍为 0 则翻回 `"none"`。`snapshot()` 只做叠加（native 首命中 + 保持其余），**不在 snapshot 内重算**。
> - **仅在"尚未被置过"时写 native** 是为了防止把 handler 层未来可能已写过的 `"bench"` 覆写回 `"native"`——但因 collector 不应读 bench 结果，本行实际不会触碰 `rate_source == "bench"` 情况。简化代码可直接兜底读 `base.get("rate_source", "none")`。
> - snapshot 不产生副作用（对 `_native_window` 仅读、对 `_snapshot` 也仅读），保持 `snapshot()` 幂等可被多次调用。

4.3.6 **`_bench_cached` 的 gate**（模块级，保持 `_bench_cached` 签名）：

```python
def _bench_cached(target: StatsTarget, has_native: bool = False) -> tuple[float, float, int] | None:
    if not target.bench_url:
        return None
    # 两组键都存在时跳过（仅 native 任一非 0 才跳过，避免 prompt 走 native 而 predicted 因
    # 某请求 completion 短 = 0 的情况误判）
    if has_native:
        return None
    ...
```

实际上门更自然放在 `UsageHandler._build_target_payload`（因为 gate 需读 snapshot 判断）——见 4.5 节。

4.3.7 **env 开关解析**（`__init__` 内）：

```python
self.bench_fallback = bool(_parse_env_bool(os.environ.get("USAGE_BENCH_FALLBACK", "true")))
```

`_parse_env_bool` 复用 gateway 已有工具（或 stats 内新建小函数），`"1"/"true"/"yes"/"on"`（忽略大小写）为 True。默认 True 保持现状（向后兼容）。

### 4.4 `core/gateway.py`

4.4.1 `_sse_stream` 的 `finally` 块中（在 `await client.aclose()` **之前**、审计 `record` 之后一并挂钩），静默失败隔离：

```python
            # 新增：喂 per-request 原生指标（vLLM 双 flag 均开 yaml 才有；其他引擎 native_mapping 为 None 会静默返回）
            if seen_metrics is not None and collector is not None and hasattr(collector, "record_native_metrics"):
                try:
                    collector.record_native_metrics(seen_metrics)
                except Exception as exc:  # noqa: BLE001 —— 实时 stats 不阻断 SSE
                    logger.warning(f"stats 记录 native metrics 异常（SSE 不中断）: {exc}")
```

4.4.2 非流式分支（网关第 934 行附近）：在已有 `record_tokens` 之后，审计前挂钩：

```python
                if target.collector is not None and hasattr(target.collector, "record_native_metrics"):
                    try:
                        target.collector.record_native_metrics(_native)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"stats 记录 native metrics 异常（响应不中断）: {exc}")
```

> **变量名 `_native`**：现有代码已用（行 939），保持一致。

4.4.3 **`get_collector` 注入 native_mapping**：在 `_collector = get_collector(...)` 调用处，需要把 adapter 的 `native_metrics_mapping()` 传入。核实现有 `get_collector` 签名：

```python
# gateway.py 行 254
def get_collector(profile, ...):
    mapping = adapter.metrics_mapping()
    ...
    collector = UsageCollector(..., mapping=mapping)
```

改为同时取 `native_mapping`（传 Adapter 实例即可选择）：

```python
# 外部签名新增入参
def get_collector(profile, caps, adapter, *, bench_fallback=True):
    ...
    native_mapping = adapter.native_metrics_mapping()
    collector = UsageCollector(..., mapping=mapping,
                                native_mapping=native_mapping,
                                bench_fallback=bench_fallback)
```

调用方（`_proxy` 注入 `model.collector` 的位置，gateway.py 466 行附近）传入 `adapter` 实例。**注意**：现有 `get_collector` 内部已 `from modelctl.engines import get_adapter; adapter = get_adapter(profile.engine)(profile, caps)`，可改为*直接复用*避免二次构造（adapter 上粗代表性的 check 只是 calls）。

### 4.5 `core/stats.py` — `UsageHandler._build_target_payload`

改现有 bench gate：

```python
        snap = collector.get_snapshot()
        if not snap["ok"]:
            return {...现有...}
        tokens = dict(snap)
        # "任一速率缺失（窗口无流量 / 引擎无 throughput gauge，如 vLLM 0.27+）时"
        # 现有逻辑保留；开头追加 USAGE_BENCH_FALLBACK + native gate：
        native_has_any = (
            tokens.get("prompt_rate", 0.0) > 0
            or tokens.get("predicted_rate", 0.0) > 0
            or tokens.get("ttft_ms", 0.0) > 0
        )
        if not collector.bench_fallback:
            pass  # 不回测——直接走 bench=None
        elif native_has_any:
            pass  # 任一路径拿到 per-request 信号 → skip 假请求
        else:
            bench = _bench_cached(target)  # 仅当四字段皆 0 且开关为 True
            if bench is not None:
                if tokens.get("prompt_rate", 0.0) == 0:
                    tokens["prompt_rate"] = bench[0]
                if tokens.get("predicted_rate", 0.0) == 0:
                    tokens["predicted_rate"] = bench[1]
                tokens["ttft_ms"] = tokens.get("ttft_ms", 0.0) or float(bench[2])
                tokens["rate_source"] = "bench"   # 覆盖之前"none"
        payload = build_usage_payload(tokens, target.usage_cfg, self.start_time, time.time())
        # 追加三字段透传（仅在非 0 时）
        if tokens.get("ttft_ms"):
            payload["ttft_ms"] = tokens["ttft_ms"]
            if tokens.get("ttft_ms_p95"):
                payload["ttft_ms_p95"] = tokens["ttft_ms_p95"]
        if tokens.get("rate_source"):
            payload["rate_source"] = tokens["rate_source"]
        ...
        return payload
```

> **注意**：`rate_source` 在 `bench` 覆盖后**必须覆盖**为 `"bench"`（若 native 已设过 native，但 converged 被 bench 高采abwe，取最高档而非最初源——语义与之前 3.4 节简单对齐的估算方向偏离一格，此为真实用户体验对应的"展示的是什么"即 档来源）。

### 4.6 `build_usage_payload` 追加 extra 信息

在 `extra` 末尾追加（仅 `ttft_ms > 0` 时）：

```python
    ttft_ms_val = tokens.get("ttft_ms") or 0.0
    ttft_p95_val = tokens.get("ttft_ms_p95") or 0.0
    extra_suffix = ""
    if ttft_ms_val > 0:
        p95_str = f"（P95 = {round(ttft_p95_val)} ms）" if ttft_p95_val > 0 else ""
        extra_suffix = f"| 首 Token P50 = {round(ttft_ms_val)} ms{p95_str}"
```

拼接到 `payload["extra"]` 末尾。

### 4.7 `core/stats.py` — `run_server`

在 `_targets_from_profiles` 与 `UsageCollector(...)` 构造处注入 `native_mapping`：

```python
            collector = UsageCollector(
                target.name,
                target.metrics_url.removesuffix("/metrics"),
                poll_interval,
                target.api_key,
                target.data_dir,
                mode=mode,
                mapping=target.mapping,
                native_mapping=getattr(adapter, "native_metrics_mapping", lambda: None)(),
                bench_fallback=os.environ.get("USAGE_BENCH_FALLBACK", "true").lower() in ("1", "true", "yes", "on"),
            )
```

> **注意**：`_targets_from_profiles` 已构造 `adapter`（现有代码），扩展签名透传即可（`StatsTarget` 加 `native_mapping` 字段）。

### 4.8 `.env.example`

新增：

```bash
# 保留"窗口无流量且 native 无数据时主动伪造请求基准"兜底；false 时关闭（推荐 true 保持现状）
USAGE_BENCH_FALLBACK=true
```

### 4.9 `cli.py._token_rate_data`

改升级思路：native ttft_ms 不 0 时跳过 served 假请求。

```python
def _token_rate_data(profile, caps) -> dict:
    """Token 速率数据：stats 优先（含 vLLM per-request native），无效/0 时主动测速。

    native ttft_ms 非 0 时优先使用该值展示，避免 served 假请求测速。
    测速（HTTP 直连上游端口）受 USAGE_BENCH_FALLBACK 控制（default true）。
    """
    port = int(os.environ.get("USAGE_PORT", "5002"))
    url = f"http://127.0.0.1:{port}/api/usage?model={profile.name}"
    ttft_ms = None
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError, json.JSONDecodeError):
        data = {}
    if isinstance(data, dict) and data.get("isValid"):
        prompt_rate = data.get("prompt_rate")
        predicted_rate = data.get("predicted_rate")
        if isinstance(prompt_rate, (int, float)) and isinstance(predicted_rate, (int, float)) and (prompt_rate > 0 or predicted_rate > 0):
            native_ttft = data.get("ttft_ms")
            if isinstance(native_ttft, (int, float)) and native_ttft > 0:
                ttft_ms = int(native_ttft)
            return {
                "prompt_rate": float(prompt_rate),
                "predicted_rate": float(predicted_rate),
                "ttft_ms": ttft_ms,  # ← 从 native 透传（None 时显示 "-"）
                "source": "stats",
            }
    # stats 无效/速率为 0 → 主动测速
    if os.environ.get("USAGE_BENCH_FALLBACK", "true").lower() not in ("1", "true", "yes", "on"):
        return {"prompt_rate": None, "predicted_rate": None, "ttft_ms": None, "source": None}
    try:
        adapter = get_adapter(profile.engine)(profile, caps)
    except Exception:  # noqa: BLE001
        adapter = None
    try:
        result = _benchmark_token_rate(adapter)
    except Exception:  # noqa: BLE001
        result = None
    ...
```

> **语义改变 flag**：之前 stats 有效但只有速率时不上报 ttft（显示 `-`）；本次改为"若 stats 返回 ttft_ms > 0（native 已写入）则直接展示"——vLLM 用户会看到"首 Token 耗时：123 ms（实测）"而非硬测速。**文案是否需要调整**：`_live_token_rate_text` 的来源标签保持 `"stats"`（数据来源仍是 stats 服务），是否需要加"（native）"后缀——**保持现状不加**，reasoning 已在 spec 上记录。

### 4.10 `tests/test_stats.py` 与 `tests/test_modelctl.py` 更新项

在 `tests/test_stats.py` 新增/保留：

- `test_usage_collector_records_native_metrics_p50`：20 个 synthetic samples 喂入，断言 `snapshot()["predicted_rate"]` ≈ P50；`ttft_ms` ≈ P50。
- `test_usage_collector_native_metrics_window_cap`：超过 cap 时最早样本被弹。
- `test_usage_collector_native_metrics_ttl`：手工 rewrite `sample.ts` 验证时龄 > 60s 被弹。
- `test_usage_collector_native_metrics_invalid_skipped`：None / 缺 key / 非数值样本被静默拒绝（旧 snapshot 值不变）。
- `test_usage_collector_native_metrics_missing_mapping_skip`：`native_mapping=None` 时 `record_native_metrics` 无副作用。
- `test_usage_collector_native_metrics_no_double_call_bench`：直接调 `_build_target_payload` 访问端点接口不够便利——改为测 `UsageHandler.collectors[...].bench_fallback=False` 分支——简化为**检验 `_bench_cached` 行为**：给一个 `has_native=True` 的假 target，断言返回 None。
- `test_bench_fallback_env_read_default_true`：`UsageCollector.__init__` 读 env 缺省 True。
- `test_bench_fallback_env_read_false`：env=`"false"` 时 `bench_fallback=False`。
- `test_build_usage_payload_extra_includes_ttft`：`tokens` 含 ttft_ms=123，ttft_ms_p95=210 时，`payload["extra"]` 末尾含 `"首 Token P50 = 123 ms（P95 = 210 ms）"`。
- `test_build_usage_payload_extra_no_ttft_0`：ttft_ms=0 时 extra 末尾不含 "首 Token"。

`tests/test_modelctl.py` 现有 gateway 测试保留（`_sse_stream` finally 新增 `record_native_metrics` 调用带 `hasattr` guard，旧 collector 无该方法的 test 不受影响）。

### 4.11 不应改的文件

| 文件 | 理由 |
|---|---|
| `audit.py` / `_build_audit_entry` | 已消费 `seen_metrics`，本次仅多一个观察者（stats collector）不改 audit 写盘 / schema |
| `vllm.yaml` schema / profile.py | native yaml 字段（`enable_per_request_metrics` / `enable_force_include_usage`）**已存在**于 `models/vllm/qwen3.8.yaml` 无需多次引入 |
| 其他 4 个 engine `metrics_mapping` / `native_metrics_mapping` | Phase 2 处理；本期 `base.py` 默认 None 即生效 |
| `_benchmark_rates`（stats.py 模块函数） | 已对齐 per-request 语义，保持不变 |
| `cc-switch` 消费方 | 新字段可忽略不影响（向后兼容纯增量） |

## 5. 测试计划

### 5.1 单元测试（`pytest tests/test_stats.py`）

1. **native 滑窗 P50/P95**：12 个 `tokens_per_second` 均匀样本（10,11,12,...,21），`snapshot()` 后 `predicted_rate` 应非常接近 P50（约 15.5）；`ttft_ms` 同理。
2. **window cap 裁剪**：cap=20 喂 25 个样本，断言最早 5 个被弹（直接 verify `len(collector._native_window)==20`）。
3. **TTL 裁剪**： rewrite 最旧样本 `ts` 为 `now - 61s`，下次 `record_native_metrics` 时 pop。
4. **非法输入静默拒绝**：`record_native_metrics(None)` / `record_native_metrics({})` / `record_native_metrics({"tokens_per_second": "abc"})` 不 crash 不推窗口。
5. **`native_mapping=None`**：验证拒绝路径。
6. **env 开关默认 True / False 分支**。
7. **`_percentile` 单元**：空 list → None；单元素 → 该值；两元素线性插值性。
8. **`build_usage_payload` extra 串**：ttft 非 0 时含；0 时不含。

### 5.2 集成 / e2e

- `envs/vllm` 环境下启 `qwen3.8` profile 的 vLLM（双 flag 均开 nginx）。
- `curl http://127.0.0.1:5002/api/usage?model=qwen3.8` 断言响应 JSON 包：
  - `prompt_rate` / `predicted_rate` 非 0
  - `ttft_ms` 非 0、`ttft_ms_p95` ≥ `ttft_ms`
  - `rate_source == "native"`
- `modelctl status qwen3.8` 断言输出行含 `首 Token P50 = xxxx ms` 或既有 `首 Token 耗时：xxx ms`，且**未发** served 假请求（观察 vLLM access log）。
- `USAGE_BENCH_FALLBACK=false` 重启后 `modelctl status <新 profile 未开 force>` 四字段全 `-`（不偷偷发假请求）。

### 5.3 回归保证

- 现有 `tests/test_stats.py` 下**已存在**的 `test_build_target_payload_benchmarks_when_idle` 保持通过（bench 整体链路未变，gate 仅加在"native 任一非 0 时"分支上——该 test 不含 native metrics，bench 仍被调用）。
- 现有 gateway proxy test（`tests/test_modelctl.py`）含 `collector.record_tokens` 断言的 test 保持通过（`record_native_metrics` 新勾化 with `hasattr` guard，旧 collector 无该方法的 test 不受影响）。
- 现有 `_bench_cached` 30s 节流行为保持。

## 6. 验收标准（Definition of Done）

1. `modelctl status <vllm-profile>`（双 flag 均开）在 60 秒内就能展示非 0 的 `prompt_rate` / `predicted_rate` / `ttft_ms`（无需等 `_bench_cached` 30s 节流）。
2. `/api/usage?model=<vllm-profile>` 响应 JSON 含 `ttft_ms` / `ttft_ms_p95` / `rate_source` 三字段；`rate_source in ("native", "none")` 选择 binary。
3. `USAGE_BENCH_FALLBACK=false` 部署时 `modelctl status` 不发 served 假请求（观察 vLLM access log 无 `hi` 字串请求，对应 `collector_diff_prompt=1`）。
4. `enable_per_request_metrics=true` 而 `enable_force_include_usage=false` 启动时，`modelctl start <name>` 前打印 warning 并继续（非硬拦截）。
5. 非 vLLM 引擎（sglang/aphrodite/lmdeploy/llamacpp/ollama/unsloth）启动行为**零变化**（`native_metrics_mapping` 默认 None，collector 的 `record_native_metrics` 静默返回）。
6. 新增单元测试 + 集成冒烟通过；现有 `pytest` 回归全绿。

## 7. 开放细节（实现期换确认）

- **`percentage` 函数在主体系中复用还是 `_percentile` 独立实现**：怀疑已有类似实现的 risk，实现时先 grep `statistics.quantiles` 或 `percentile` 命名，避免重复小工具。
- **`_last_gauge_active` 维护是否改为直接读 `snapshot["prompt_rate"]`**：`_poll_once` 中已把 gauge 写入 `metrics["prompt_rate"]` 到 `self._snapshot["prompt_rate"]`（行 456-458），`_last_gauge_active` 很可能不需保持，直接从 store 派生即可（简化实现）。
- **`_NativeSample` dataclass 在外面字段层能否省**：可不建议保持 dataclass——实现时可换言之用 tuple 避免多一个类声明，YAGNI 取小那条。
- **`USAGE_BENCH_FALLBACK` 读 env 的时机**：`UsageCollector.__init__` 读一次 OK（gateway 使用期不变）。**不得在 `record_native_metrics` 内重读**（避免 O(N)）。
- **`total/remaining` 影响**：新字段对 `calc_cost` 不产生影响（cost 仅看 prompt_total/predicted_total），无须验证。

## 8. 时间线与依赖

- 依赖：`2026-08-31-per-request-audit-design.md` 已实现上线，网关 `seen_metrics` 已可用。
- 不阻塞：`2026-08-11-cc-switch-usage-stats-design.md` 输出的 `/api/usage` schema 仅做*增量*扩展。
- 与 Phase 2 分接触角标：`base.native_metrics_mapping` 钩子在 Phase 2 可linux aphrodite/lmdeploy/sglang 三家实现（各自有的原生 per-request 字段已确认，需 reviewed 具体名）。

## 9. 风险与回计

| 风险 | 缓解 |
|---|---|
| 高并发下 per-request 滑窗 P50 追不上 fleet 实际 throughput（因为滑窗只覆盖"经网关"子集） | accept：vLLM Avg gauge 档 2 在 collector 无 native 信号时仍会 active，两层保险 |
| `enable_force_include_usage=false` 且 `enable_per_request_metrics=true` yaml 上"能拿到 metrics 但 stats token 计数不完整" | 启动 warning 告知；不硬拦截（用户可偏好 vLLM 0.84 默认行为） |
| 非 vLLM 引擎的 `record_native_metrics` 空调用累积开销 | O(1) 短路（`native_mapping=None` 早退），无调用足可忽略 |
| small-sample P50/P95 抖动 ≤20 请求时高方差 | accept： native ttft 展示保留 2 位小数（ms）能在视觉层 snippet；展示层无抖动指标额外增加 |

## 10. 给 writing-plans 的节点

将本 spec 拆为 10 个 task（供 `writing-plans` skill 输入）；建议次序 = 下列顺序。

1. `engines/base.py`：加 `native_metrics_mapping` 抽象方法
2. `engines/vllm.py`：实现 override + check_requirements 新增 warning
3. `core/stats.py`：新增 `_parse_env_bool`、`_NativeSample` / `_percentile`、`UsageCollector.__init__` 新入参、`record_native_metrics`、`_compute_native_row`
4. `core/stats.py`：`snapshot()` 重写、`run_server` / `_targets_from_profiles` / `StatsTarget` 注入 `native_mapping`
5. `core/stats.py`：`UsageHandler._build_target_payload` bench gate 改、`build_usage_payload` extra 串改
6. `core/gateway.py`：`_sse_stream` finally + 非流式分支 `record_native_metrics` 挂钩、`get_collector` 注入 `adapter`
7. `cli.py`：`_token_rate_data` 升级（native ttft 透传 + `USAGE_BENCH_FALLBACK` gate）
8. `.env.example`：新增 `USAGE_BENCH_FALLBACK=true`
9. `tests/test_stats.py`：新增 8-10 个 test case；回归现有用例保持全绿
10. e2e 冒烟（env in `envs/vllm` 双 flag 均开 nginx 样例）
