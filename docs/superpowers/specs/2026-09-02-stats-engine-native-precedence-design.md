# stats 模块"引擎内置接口优先 + 兜底最后"与轮询降频设计

- 日期：2026-09-02
- 状态：已确认（用户逐节评审通过）
- 关联：[stats.py](stats.py)、[engines/vllm.py](engines/vllm.py)、[engines/llamacpp.py](engines/llamacpp.py)、`.env.example`
- 前置 spec：`2026-09-01-stats-vllm-native-metrics-design.md`（per-request native 链路）、`2026-08-24-status-token-rate-design.md`（主动测速）、`2026-08-18-stats-persistence-design.md`（持久化）

## 1. 背景与目标

### 规范

stats 模块在 token 相关信息（速率、首 Token 响应时间）的获取上必须遵循：

> **优先走各引擎内置的专用接口**；只有该引擎不提供此类接口时，才由 stats 模块的兜底链路采集。

同时把 stats 轮询频率从 10 秒一次降为 60 秒一次，以降低系统资源占用。

### 现状与偏差

2026-09-01 的 spec 已落地档 1（vLLM per-request native 经网关喂 `record_native_metrics`），但仍有三处偏离规范：

1. **gauge 层没有 TTFT 通道**。vLLM `/metrics` 上本来就有 `vllm:time_to_first_token_seconds` 直方图（引擎内置专用接口），但 `metrics_mapping()` 未声明、`parse_metrics` 也不会解析直方图。
2. **`snapshot()` 无条件用 native 滑窗覆写 `ttft_ms`**（`stats.py:613`）。native 滑窗为空即归 0，等于把任何 gauge 来源的 TTFT 强制清零——即使将来补了 gauge 解析也拿不到值。
3. **bench 兜底的 gate 是"三字段任一非 0 就整体跳过"**（`stats.py:691-701`）。llamacpp 有吞吐 gauge、永远没有 TTFT，于是 `native_has_any` 恒为真 → 兜底永不触发 → `首 Token 耗时` 恒为 `-`。结果反而是"有流量时信息更少"。

### 轮询频率现状

| 位置 | 当前值 |
|---|---|
| `stats.py:848` 代码默认 | `5` |
| `stats.py:835` 文档串 | 写 `5` |
| `.env.example:52` | `10` |
| 用户 `.env` | 未设置该键 |

### 目标

1. 补齐 gauge 层 TTFT 采集，使 vLLM 的四个字段（`prompt_rate` / `predicted_rate` / `ttft_ms` / `ttft_ms_p95`）在**不依赖假请求**的前提下有值。
2. 兜底链路严格降级为"最后一档"，且按字段缺口独立触发，让无内置 TTFT 接口的引擎（llamacpp）也能拿到首 Token 耗时。
3. 轮询默认间隔 60 秒（代码默认值与 `.env.example` 同步）。

### 非目标

- 不改 `/api/usage` 现有字段语义（三字段 `ttft_ms` / `ttft_ms_p95` / `rate_source` 早已存在，本次只是让它们更多场景有值）。
- 不改滑窗参数（`_window_size=10`、`_native_window_ttl=60`、`_native_window_cap=20`）与 `poll_interval` 的联动。
- 不为 sglang / aphrodite / lmdeploy / tensorrt_llm / tokenspeed 补 TTFT 直方图映射（各自的指标名未在本机核验，凭猜写会静默取到 0，留 Phase 2）。
- 不改 ollama / unsloth（`metrics_mapping()` 返回 `None`，`_build_target_payload` 现即返回"该引擎不支持精确统计"）。
- 不改 gateway 的 `record_native_metrics` 挂钩、audit 链路、`benchmark_rates` 计算口径。

### 已确认的关键决策（brainstorming 阶段用户确认）

| 决策点 | 结论 |
|---|---|
| 内置接口口径 | **两者都要**：per-request native（档 1）优先 + `/metrics` gauge（档 2）补齐 |
| 无内置 TTFT 的引擎 | **保留 bench 假请求兜底**（现状方向），不按"显示 `-`"处理 |
| vLLM TTFT 直方图口径 | **均值 `sum/count`**，不做 bucket 线性插值算 P50/P95 |
| 60s 落地方式 | **代码默认值 + `.env.example` 同步改为 60**，不动用户 `.env`，滑窗参数不联动 |
| 有 gauge 速率但无 TTFT 的引擎是否仍发假请求 | **加开关控制**（新增 `USAGE_BENCH_TTFT_ONLY`，默认 `true`） |

## 2. 取值分层（规范落地形态）

四个字段**各自独立**按下列顺序取第一个非 0 值：

| # | `rate_source` | 来源 | 引擎内置接口 | 覆盖字段 |
|---|---|---|---|---|
| 1 | `native` | 网关从响应体解析的 per-request `metrics`（vLLM 双 flag） | ✅ | 四字段全 |
| 2 | `engine_gauge` | 轮询 `/metrics`：吞吐 gauge + TTFT 直方图 `sum/count` 均值 | ✅ | 速率 + `ttft_ms`（无 P95） |
| 3 | `window_diff` | 累计计数器滑窗差分 | ❌ 兜底 | 仅速率 |
| 4 | `bench` | `benchmark_rates` 假请求，受 `USAGE_BENCH_FALLBACK` 控制 | ❌ 兜底 | 仅填仍为 0 的字段 |

各引擎落位：

| 引擎 | 档 1 | 档 2 | 实际效果 |
|---|---|---|---|
| vllm | ✅（双 flag 开启时） | ✅ 速率 + TTFT | 内置接口覆盖全部字段，仅零流量冷启动才可能走档 4 |
| llamacpp | ❌ | ✅ 仅速率 | 速率走内置 gauge；TTFT 无内置接口 → 走档 4 兜底 |
| sglang / aphrodite / lmdeploy / tensorrt_llm / tokenspeed | ❌ | ✅ 仅速率（现状） | 与 llamacpp 同路径，Phase 2 再补各自 TTFT |
| ollama / unsloth | ❌ | ❌（`mapping is None`） | 维持"该引擎不支持精确统计"，本次不动 |

## 3. 详细设计

### 3.1 `core/stats.py` — 直方图均值解析

在 `parse_metrics` 附近新增模块级私有助手：

```python
def _hist_mean(text: str, name: str) -> float:
    """Prometheus 直方图均值：name_sum / name_count。

    任一缺失或 count <= 0 返回 0.0。用于 vLLM time_to_first_token_seconds
    这类只有 Histogram、没有现成均值 gauge 的内置指标。
    """
```

`_build_patterns` 不改。裸名（gauge）正则保持优先命中，仅在裸名未命中时才尝试 `<name>_sum` / `<name>_count` 两条独立正则——避免直方图场景误命中同名 gauge。

### 3.2 `core/stats.py` — `parse_metrics` 支持 `ttft_ms` 键

- 返回值新增 `"ttft_ms": 0.0` 默认项。
- 解析每个键时：先按现有裸名逻辑取 gauge 值；若该键候选名全部未命中，且 mapping 键为 `ttft_ms`，则对每个候选名调 `_hist_mean` 取第一个非 0 结果。
- 现有四个键（`prompt_total` / `predicted_total` / `prompt_rate` / `predicted_rate`）行为完全不变；未声明 `ttft_ms` 键的引擎恒得 0.0。

> **口径说明**：直方图均值 ≠ P50。因此 gauge 档来源的 TTFT 只写入 `ttft_ms`，**不写** `ttft_ms_p95`。`build_usage_payload` 已有的"P95 为 0 时不输出 P95 段"逻辑（`stats.py:229`）天然适配，`extra` 串显示为 `首 Token P50 = 123 ms`——此时该值是均值而非严格 P50，属已知展示口径近似，不做额外标注（避免 `extra` 文案分叉）。

### 3.3 `engines/vllm.py` — 声明 TTFT 直方图

`metrics_mapping()` 追加一个键：

```python
"ttft_ms": ["vllm:time_to_first_token_seconds"],
```

`native_metrics_mapping()` 不动。`llamacpp.py` **显式不加** `ttft_ms` 键——llama.cpp 官方 `/metrics` 只暴露 `prompt_tokens_total` / `tokens_predicted_total` / `prompt_tokens_seconds` / `predicted_tokens_seconds`，无任何 TTFT/延迟指标。

### 3.4 `core/stats.py` — `_poll_once` 写入 gauge TTFT

`_poll_once` 内构造 `_snapshot` 时（`stats.py:590-600`），把 `metrics["ttft_ms"]` 写入而非硬编码 `0.0`。轮询失败分支（`stats.py:549-559`）保持写 `0.0`。

`rate_source` 判定（`stats.py:584-588`）**不纳入** `ttft_ms`——该标签按现有语义只反映**速率**来源，避免把"速率来自 window_diff、TTFT 来自 gauge"这类正常混合场景误标为 `engine_gauge`。

### 3.5 `core/stats.py` — `snapshot()` 取消无条件覆写

`stats.py:613` 改为：

```python
base["ttft_ms"] = native_row["ttft_ms"] or (base.get("ttft_ms") or 0.0)
base["ttft_ms_p95"] = native_row["ttft_ms_p95"]
```

即 `ttft_ms` 遵循 native 优先、gauge 补齐；`ttft_ms_p95` 仍只由 native 提供（档 2 无分位数据）。`prompt_rate` / `predicted_rate` 两行合并逻辑（`stats.py:611-612`）已是 `native or gauge/window` 形态，不改。

### 3.6 `core/stats.py` — bench gate 改为按字段缺口

`UsageHandler._build_target_payload`（`stats.py:689-711`）现有 `native_has_any` 整体 gate 替换为：

```python
missing_any = (
    (tokens.get("prompt_rate") or 0) == 0
    or (tokens.get("predicted_rate") or 0) == 0
    or (tokens.get("ttft_ms") or 0) == 0
)
rates_ok = (tokens.get("prompt_rate") or 0) > 0 and (tokens.get("predicted_rate") or 0) > 0
should_bench = bench_fallback_enabled and missing_any and (
    not rates_ok or bench_ttft_only
)
```

真值表（与用户确认的开关语义一致）：

| `USAGE_BENCH_FALLBACK` | `USAGE_BENCH_TTFT_ONLY` | 行为 |
|---|---|---|
| `false` | 任意 | 完全不发假请求（总开关优先，现有语义不变） |
| `true` | `true`（默认） | 缺速率**或**缺 TTFT 任一即测速 → llamacpp 有流量时也能拿到 TTFT |
| `true` | `false` | 仅当速率与 TTFT **全为 0** 时才测速（即改造前行为） |

回填逻辑不变：只覆盖仍为 0 的字段（`stats.py:705-710`），因此 llamacpp 有 gauge 速率时，假测速只补 `ttft_ms`，不会污染速率。`rate_source` 仍按"bench 一旦回填任一字段就整体标 `bench`"（`stats.py:711`），不新增复合标签值。

**两处相对改造前的行为变化**（均为规范要求的"兜底应可用"）：

- `rates_ok` 要求 `prompt_rate` 与 `predicted_rate` **同时**非 0。单侧为 0 通常是该侧 gauge 缺失（如 vLLM 未开 `--enable-metrics` 时两个吞吐 gauge 皆无），而非真实无流量，故视为缺口、允许兜底补测。
- native 有 `ttft_ms` 但两档速率均为 0 时，改造前因 `native_has_any` 而跳过 bench、界面显示 `0.0 tok/s`；改造后允许兜底补速率。`ttft_ms` 已有值，bench 不会覆写它。

### 3.7 `core/stats.py` — 新开关读取与传递

`UsageCollector.__init__` 增加：

```python
if "USAGE_BENCH_TTFT_ONLY" in os.environ:
    self.bench_ttft_only = _parse_env_bool(os.environ["USAGE_BENCH_TTFT_ONLY"])
else:
    self.bench_ttft_only = bench_ttft_only
```

形态与现有 `bench_fallback`（`stats.py:355-358`）完全一致：env 优先、否则用构造入参、`__init__` 读一次不热读。构造入参 `bench_ttft_only: bool = True`。

`UsageHandler._build_target_payload` 通过 `getattr(collector, "bench_ttft_only", True)` 读取，与现有 `getattr(collector, "bench_fallback", True)`（`stats.py:696`）同构——保证测试里 `MagicMock` 的旧 collector 不受影响。

`run_server`（`stats.py:855-865`）与 `gateway.get_collector`（`gateway.py:277-303`）两处 `UsageCollector(...)` 构造点均显式传入。

### 3.8 轮询降频

| 位置 | 改动 |
|---|---|
| `stats.py:848` | `os.environ.get("USAGE_POLL_INTERVAL", "5")` → `"60"` |
| `stats.py:835` 文档串 | `USAGE_POLL_INTERVAL（默认 5）` → `（默认 60）` |
| `.env.example:51-52` | `USAGE_POLL_INTERVAL=10` → `=60`，注释补充"降低引擎侧压力" |

用户 `.env` 未设该键，改动后自动生效 60s，无需迁移。

### 3.9 `.env.example`

`USAGE_BENCH_FALLBACK` 段之后新增：

```bash
# 仅为补 TTFT 而测速：true=gauge 有速率但引擎无内置 TTFT 接口（如 llama.cpp）时，
# 仍按 USAGE_BENCH_FALLBACK 的节流发假请求补首 Token 耗时；false=仅速率与 TTFT 全 0 时才测速
USAGE_BENCH_TTFT_ONLY=true
```

## 4. 已知副作用（显式接受，不静默处理）

1. **档 3 `window_diff` 变钝**：`_window_size=10` 个采样点差分，60s 轮询下窗口从 ~100s 拉长到 ~600s 均值。缓解事实：vLLM 与 llamacpp 的速率都有档 1/档 2 覆盖，档 3 实际只在"gauge 恒 0 但网关有累计"的 vLLM 未开 `--enable-metrics` 场景兜底。
2. **llamacpp 在有真实流量时每 30s 仍发一次假测速**（`_BENCH_TTL=30`），占用一份推理资源；不需要此行为可设 `USAGE_BENCH_TTFT_ONLY=false`。
3. **gauge 档的 `ttft_ms` 是直方图均值而非 P50**，`extra` 文案仍写"P50"。仅影响 vLLM 未开 per-request flag 的场景；要消除需引入 bucket 插值，本期不做。

## 5. 测试计划

### 5.1 新增（`tests/test_stats.py` / `tests/test_stats_native.py`）

1. `test_hist_mean_divides_sum_by_count`：标准直方图文本 → `sum/count`。
2. `test_hist_mean_zero_count_returns_zero`：`_count 0` → `0.0`。
3. `test_hist_mean_missing_sum_returns_zero`：只有 `_bucket` 行 → `0.0`。
4. `test_parse_metrics_ttft_from_histogram_when_no_gauge`：mapping 含 `ttft_ms: ["vllm:time_to_first_token_seconds"]`，文本只有 `_sum`/`_count` → 得均值。
5. `test_parse_metrics_ttft_prefers_bare_gauge`：同名裸 gauge 与 `_sum` 并存 → 取裸 gauge。
6. `test_parse_metrics_without_ttft_key_defaults_zero`：mapping 无 `ttft_ms` 键 → 返回 0.0 且现有四键不变。
7. `test_vllm_metrics_mapping_declares_ttft_histogram`：`VllmAdapter.metrics_mapping()["ttft_ms"] == ["vllm:time_to_first_token_seconds"]`。
8. `test_snapshot_keeps_gauge_ttft_when_native_window_empty`：`_poll_once` 拿到 gauge ttft、native 滑窗为空 → `snapshot()["ttft_ms"] > 0`（回归 §3.5）。
9. `test_build_target_payload_bench_only_fills_ttft_when_rates_from_gauge`：速率非 0、`ttft_ms=0`、开关默认 → 调 `_bench_cached`，速率字段不被 bench 覆写。
10. `test_build_target_payload_skips_bench_when_only_ttft_missing_and_switch_off`：同上快照但 `bench_ttft_only=False` → `_bench_cached` 不被调用。
11. `test_bench_ttft_only_env_read_default_true` / `test_bench_ttft_only_env_read_false`。
12. `test_run_server_poll_interval_default_sixty`：未设 env 时 `run_server` 取 60。

### 5.2 回归必须保持通过

- `test_build_target_payload_skips_bench_when_native_ttft_present`（native 有 TTFT → 不 bench）
- `test_build_target_payload_runs_bench_when_all_zero_and_switch_on`（全 0 → bench，四字段回填）
- `test_build_target_payload_skips_bench_when_switch_off`（`bench_fallback=False` → 不 bench）
- `test_poll_once_prefers_persisted_gateway_totals` 等现有窗口差分/持久化用例
- native 滑窗全部用例（`tests/test_stats_native.py`）
- `pytest tests/ -q` 全绿

### 5.3 集成冒烟（`envs/vllm` 环境）

- 启 `qwen3.8-flash-next-vllm`（仅开 `--enable-metrics`、**不开** per-request 双 flag）：发一次真实请求后 `curl :5002/api/usage?model=qwen3.8-flash-next-vllm` 断言 `ttft_ms > 0` 且 `rate_source != "bench"`（证明 TTFT 来自内置直方图）。
- 启 llamacpp profile：有流量时断言 `ttft_ms > 0` 且 `extra` 内速率与 gauge 值一致（bench 只补 TTFT）；设 `USAGE_BENCH_TTFT_ONLY=false` 重启后断言上游 access log 无 `hi` 请求且 `ttft_ms` 缺失。
- 观察 stats 服务日志确认 `后台每 60s 轮询`。

## 6. 验收标准

1. vLLM 仅开 `--enable-metrics`（不开 per-request 双 flag）时，`/api/usage` 的 `ttft_ms` 有值且来自 `/metrics` 直方图，非假请求。
2. llamacpp 有流量时 `ttft_ms` 有值（来自兜底），`prompt_rate` / `predicted_rate` 仍等于引擎 gauge 值。
3. `USAGE_BENCH_TTFT_ONLY=false` 时 llamacpp 有流量场景上游收不到假测速请求。
4. `USAGE_BENCH_FALLBACK=false` 优先级高于 `USAGE_BENCH_TTFT_ONLY`（任何配置组合都不发假请求）。
5. `USAGE_POLL_INTERVAL` 未配置时后台轮询间隔为 60s（日志可见）。
6. `/api/usage` 字段集合与改造前完全一致（cc-switch 零改动）；`rate_source` 取值集合仍为 `{native, engine_gauge, window_diff, bench, none}`。
7. 新增 12 项单元测试 + 现有 `pytest tests/ -q` 全绿。

## 7. 改动清单

| 文件 | 改动 |
|---|---|
| `src/modelctl/core/stats.py` | `_hist_mean` 新增；`parse_metrics` 支持 `ttft_ms`；`_poll_once` 写 gauge ttft；`snapshot()` 取消无条件覆写；`UsageCollector.__init__` 新增 `bench_ttft_only`；`_build_target_payload` gate 改按字段；`run_server` 默认 60 + 注入新开关；模块与函数文档串 |
| `src/modelctl/engines/vllm.py` | `metrics_mapping()` 追加 `ttft_ms` 直方图名 |
| `.env.example` | `USAGE_POLL_INTERVAL=60`；新增 `USAGE_BENCH_TTFT_ONLY=true` |
| `tests/test_stats.py`、`tests/test_stats_native.py` | §5.1 的 12 项 |
| **不改** | `engines/llamacpp.py`（无 TTFT 指标）、其余 5 个引擎、`gateway.py` 除构造点传参外的逻辑、`audit.py`、`cli.py`、`benchmark_rates` |

## 8. 给 writing-plans 的节点

建议拆为 7 个 task，按序执行（TDD：每个 task 先写失败测试）：

1. `core/stats.py`：`_hist_mean` + `parse_metrics` 的 `ttft_ms` 键（含 §5.1 用例 1-6）
2. `engines/vllm.py`：`metrics_mapping` 追加 `ttft_ms`（用例 7）
3. `core/stats.py`：`_poll_once` 写入 gauge ttft + `snapshot()` 取消无条件覆写（用例 8）
4. `core/stats.py`：`UsageCollector.__init__` 的 `bench_ttft_only`（用例 11）
5. `core/stats.py`：`_build_target_payload` gate 改按字段（用例 9-10）
6. `core/stats.py` + `.env.example`：轮询默认 60 + 新增开关条目（用例 12）
7. 回归 `pytest tests/ -q` + §5.3 集成冒烟
