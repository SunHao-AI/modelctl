# stats 模块对齐 vLLM 原生 per-request metrics 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 stats 的 `/api/usage` 与 `modelctl status` 在 vLLM 开启 `--enable-per-request-metrics` + `--enable-force-include-usage` 时**默认**展示 vLLM 原生的 per-request 指标（decode 速率 / prompt 速率 / TTFT P50+P95，window 口径 `max(60s, 20 请求)`），并以 4 档优先级 `native → engine_gauge → window_diff → bench` 兜底；新增 `USAGE_BENCH_FALLBACK` 开关。

**Architecture:** 网关在 `_sse_stream` finally / 非流式分支拿到 vLLM 原生 `metrics` 后**额外**喂给 `UsageCollector.record_native_metrics`（方案 A：网关为单一接缝）；collector 维护 60s/20 请求滑窗并在 `snapshot()` 内合并 4 档结果；bench 仅在 native 全 0 且 `USAGE_BENCH_FALLBACK=true` 时执行并回写 `rate_source=bench`；`/api/usage` JSON 追加 `ttft_ms` / `ttft_ms_p95` / `rate_source` 三字段（现有字段不变）。

**Tech Stack:** Python 3.12、标准库 + 现有栈（loguru / pytest / httpx.MockTransport）

## Global Constraints

- Python 3.12；**不引入新第三方依赖**
- 现有 `uv run pytest tests/ -q` **必须保持全绿**（不重命名任何现有测试名）
- TDD：每 task 先写失败测试，再最小实现
- 本计划沿用"不自动 commit"策略（commit 步骤均**不执行**，改动留工作区由用户统一提交）
- `/api/usage` **现行字段语义不因本次改动被改写**——只追加 `ttft_ms` / `ttft_ms_p95` / `rate_source` 三字段
- 非 vLLM 引擎启动 / `modelctl status` / `benchmark_rates` 的**行为必须零变化**（`native_metrics_mapping` 默认 `None` 短路路径）
- 网关内部 `record_native_metrics` 钩子**必须静默失败隔离**（`hasattr` guard + `try/except` 且**绝不**中断 SSE / HTTP 响应）
- `MIN_VLLM_PER_REQUEST = (0, 13, 0)` **保留现有且不动**
- `.env.example` 新增 `USAGE_BENCH_FALLBACK=true`（默认 true 保持现状向后兼容）
- 窗口口径 `max(60s, 20 请求)`（两项都不超过），不引入 P99 等多分位数
- vLLM `check_requirements` 只加 **warning**（非 `RequirementError`），不停启

**参考 spec：** `docs/superpowers/specs/2026-09-01-stats-vllm-native-metrics-design.md`

---

### Task 1: `engines/base.py` — `native_metrics_mapping` 抽象钩子

**Files:**
- Modify: `src/modelctl/engines/base.py`（`EngineAdapter.metrics_mapping` 抽象之后）
- Test: `tests/test_engine_native_metrics.py`（**新建**）

**Interfaces:**
- Consumes: 无
- Produces: `EngineAdapter.native_metrics_mapping(self) -> dict[str, str] | None` — 默认 `None`

- [ ] **Step 1: 写失败测试（新文件）**

新建 `tests/test_engine_native_metrics.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engines/*/native_metrics_mapping 基类默认值检查。纯引擎层，无网络。"""

from __future__ import annotations

from unittest.mock import MagicMock


def test_base_native_metrics_mapping_default_none():
    """非 vLLM 引擎继承默认 None；确保 stats 侧短路不 crash。"""
    from modelctl.core.capabilities import Capabilities
    from modelctl.engines.ollama import OllamaAdapter

    profile = MagicMock()
    profile.name = "test-ollama"
    profile.engine_config = {}
    profile.port = 11434
    profile.api_key = None
    adapter = OllamaAdapter(profile, Capabilities())
    assert adapter.native_metrics_mapping() is None
```

- [ ] **Step 2: 运行并验证失败**

```bash
uv run pytest tests/test_engine_native_metrics.py::test_base_native_metrics_mapping_default_none -v
```
Expected: **FAIL**（`AttributeError: 'OllamaAdapter' object has no attribute 'native_metrics_mapping'`）

- [ ] **Step 3: 写最小实现**

编辑 `src/modelctl/engines/base.py`，在 `EngineAdapter.metrics_mapping` 抽象方法（现 行 53-54）**之后**追加普通方法（不 `@abstractmethod`）：

```python
    def native_metrics_mapping(self) -> dict[str, str] | None:
        """per-request 原生指标字段名映射（网关喂 stats collector 时用）。

        键固定为 {rate, ttft_ms, gen_time_ms, prompt_tokens, completion_tokens}，
        值为该引擎 SSE 末块 / 响应根级 "metrics" 对象中真实字段名。
        默认 None 表示该引擎不提供 per-request 原生指标（stats 侧短路）。
        """
        return None
```

- [ ] **Step 4: 运行并验证通过**

```bash
uv run pytest tests/test_engine_native_metrics.py -v
```
Expected: **PASS**

- [ ] **Step 5: 回归**

```bash
uv run pytest tests/ -q
```
Expected: **全绿**

---

### Task 2: `engines/vllm.py` — 实现 `native_metrics_mapping` + 组合 flag warning

**Files:**
- Modify: `src/modelctl/engines/vllm.py:39-102`（`check_requirements` 追加 warning 段）、`src/modelctl/engines/vllm.py`（`metrics_mapping` 附近新增 override）
- Test: `tests/test_engine_native_metrics.py`（追加）

**Interfaces:**
- Consumes: `EngineAdapter.native_metrics_mapping`（Task 1 产出）
- Produces:
  - `VllmAdapter.native_metrics_mapping(self) -> dict[str, str]` — vLLM 5 字段映射
  - `VllmAdapter.check_requirements` 组合 flag warning：`per_request=true` 且 `force=false` 时追加 `self.warnings` 一条字符串（非 `RequirementError`）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_engine_native_metrics.py`：

```python
def test_vllm_native_metrics_mapping_returns_vllm_fields():
    from unittest.mock import MagicMock
    from modelctl.core.capabilities import Capabilities
    from modelctl.engines.vllm import VllmAdapter

    profile = MagicMock()
    profile.name = "qwen3.8"
    profile.engine_config = {}
    profile.port = 8000
    profile.api_key = None
    adapter = VllmAdapter(profile, Capabilities())
    mapping = adapter.native_metrics_mapping()
    assert mapping is not None
    assert mapping["rate"] == "tokens_per_second"
    assert mapping["ttft_ms"] == "time_to_first_token_ms"
    assert mapping["gen_time_ms"] == "generation_time_ms"
    assert mapping["prompt_tokens"] == "num_prompt_tokens"
    assert mapping["completion_tokens"] == "num_generation_tokens"


def test_vllm_warns_when_per_request_metrics_on_but_force_off():
    """enable_per_request_metrics=true 且 enable_force_include_usage=false 时 add warning。"""
    from unittest.mock import MagicMock, patch
    from modelctl.core.capabilities import Capabilities
    from modelctl.engines import vllm as vllm_mod
    from modelctl.engines.vllm import VllmAdapter

    profile = MagicMock()
    profile.name = "qwen3.8"
    profile.engine_config = {
        "model": "/nonexistent/path",
        "enable_per_request_metrics": True,
        "enable_force_include_usage": False,
    }
    profile.port = 8000
    profile.api_key = None
    adapter = VllmAdapter(profile, Capabilities())

    m_envs = MagicMock()
    m_envs.ensure_env.return_value = None
    m_envs.vllm_version.return_value = (0, 14, 0)
    m_envs.VENV_ROOT = MagicMock(name="VENV_ROOT")
    m_envs.engine_bin.return_value = MagicMock(name="engine_bin")
    with patch.object(vllm_mod, "envs", m_envs), \
         patch.object(adapter, "selected_gpus", return_value=None), \
         patch.object(adapter, "run_compat_checks"), \
         patch.object(adapter, "_check_vram_advisory"):
        try:
            adapter.check_requirements()
        except Exception:
            pass  # 允许 RequirementError，只断言 warning
    join = "\n".join(adapter.warnings)
    assert "enable_force_include_usage" in join, f"应追加组合 flag warning，实际={adapter.warnings}"


def test_vllm_no_warning_when_both_flags_on():
    """两 flag 均开时不加 warning（本次目标状态）。"""
    from unittest.mock import MagicMock, patch
    from modelctl.core.capabilities import Capabilities
    from modelctl.engines import vllm as vllm_mod
    from modelctl.engines.vllm import VllmAdapter

    profile = MagicMock()
    profile.name = "qwen3.8"
    profile.engine_config = {
        "model": "/nonexistent/path",
        "enable_per_request_metrics": True,
        "enable_force_include_usage": True,
    }
    profile.port = 8000
    profile.api_key = None
    adapter = VllmAdapter(profile, Capabilities())
    m_envs = MagicMock()
    m_envs.ensure_env.return_value = None
    m_envs.vllm_version.return_value = (0, 14, 0)
    m_envs.VENV_ROOT = MagicMock()
    m_envs.engine_bin.return_value = MagicMock()
    with patch.object(vllm_mod, "envs", m_envs), \
         patch.object(adapter, "selected_gpus", return_value=None), \
         patch.object(adapter, "run_compat_checks"), \
         patch.object(adapter, "_check_vram_advisory"):
        try:
            adapter.check_requirements()
        except Exception:
            pass
    assert all("enable_force_include_usage" not in w for w in adapter.warnings)
```

- [ ] **Step 2: 运行并验证失败**

```bash
uv run pytest tests/test_engine_native_metrics.py -v
```
Expected: **3 个 FAIL**（override 未实现 → AttributeError / warning 未加 / warning 逻辑分支缺失）

- [ ] **Step 3: 写最小实现**

编辑 `src/modelctl/engines/vllm.py`：

3a. 在 `metrics_mapping`（现 行 207-221）**之前**追加：

```python
    def native_metrics_mapping(self) -> dict[str, str]:
        """per-request 原生指标字段名映射（vLLM ≥ 0.13 双 flag 均开时 SSE 末块 / 响应根级）。"""
        return {
            "rate": "tokens_per_second",
            "ttft_ms": "time_to_first_token_ms",
            "gen_time_ms": "generation_time_ms",
            "prompt_tokens": "num_prompt_tokens",
            "completion_tokens": "num_generation_tokens",
        }
```

3b. 在 `check_requirements` 中追加 warning。位置选定：`MAX pre_start 后、共享 GPU 段之前`（两处共用，便于 docker / venv 分支都走）。

追加到**行 100 `self.run_compat_checks()` 之前**（共享代码路径覆盖两个 runtime）：

```python
        # 组合 flag 语义警告：per_request 开但 force 关，流式 usage 会漏中间块
        per_request_on = bool(cfg.get("enable_per_request_metrics"))
        force_on = bool(cfg.get("enable_force_include_usage"))
        if per_request_on and not force_on:
            self.warnings.append(
                f"{self.profile.name}：enable_per_request_metrics=true 但 enable_force_include_usage=false，"
                "流式中间块缺 usage 会使 stats.record_tokens 仅末块入账；建议同时开启"
            )
```

- [ ] **Step 4: 运行并验证通过**

```bash
uv run pytest tests/test_engine_native_metrics.py -v
```
Expected: **PASS**（3 个用例）

- [ ] **Step 5: 回归**

```bash
uv run pytest tests/ -q
```
Expected: **全绿**

---

### Task 3: `core/stats.py` — `_parse_env_bool` + `_NativeSample` + `_percentile`

**Files:**
- Modify: `src/modelctl/core/stats.py`（在 `USAGE_PORT = 5002` 之后、`_fmt_tokens` 之前）
- Test: `tests/test_stats_native.py`（**新建**）

**Interfaces:**
- Consumes: 无
- Produces:
  - `_parse_env_bool(value: str | None, default: bool = True) -> bool`
  - `class _NativeSample`（`@dataclass`）— 字段 `ts: float`, `tokens_per_second: float`, `prompt_inflight_rate: float`, `ttft_ms: float`, `ttft_s: float`
  - `_percentile(values: list[float], p: float) -> float | None`（线性插值）

- [ ] **Step 1: 写失败测试（新文件）**

新建 `tests/test_stats_native.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/stats.py：_parse_env_bool / _NativeSample / _percentile 纯函数测试。"""

from __future__ import annotations


def test_parse_env_bool_true_values():
    from modelctl.core.stats import _parse_env_bool
    for raw in ("1", "true", "YES", "on", " True "):
        assert _parse_env_bool(raw) is True


def test_parse_env_bool_false_values():
    from modelctl.core.stats import _parse_env_bool
    for raw in ("0", "false", "No", "off"):
        assert _parse_env_bool(raw) is False


def test_parse_env_bool_default_on_none_empty_or_unknown():
    from modelctl.core.stats import _parse_env_bool
    assert _parse_env_bool(None) is True
    assert _parse_env_bool("") is True
    assert _parse_env_bool("   ") is True
    assert _parse_env_bool("xxx") is True
    assert _parse_env_bool(None, default=False) is False
    assert _parse_env_bool("", default=False) is False


def test_percentile_empty_returns_none():
    from modelctl.core.stats import _percentile
    assert _percentile([], 50) is None


def test_percentile_single_element_returns_itself():
    from modelctl.core.stats import _percentile
    assert _percentile([42.0], 50) == 42.0
    assert _percentile([42.0], 95) == 42.0
    assert _percentile([42.0], 0) == 42.0


def test_percentile_p50_matches_median_on_even_length():
    from modelctl.core.stats import _percentile
    values = [float(i) for i in range(1, 11)]  # 1..10
    assert abs(_percentile(values, 50) - 5.5) < 1e-9


def test_percentile_p95_linear_interpolation():
    from modelctl.core.stats import _percentile
    values = [float(i) for i in range(1, 11)]  # 1..10
    # idx = 9 * 0.95 = 8.55 → s[lo]*(1-frac) + s[hi]*frac = 9.0*0.45 + 10.0*0.55 = 9.55
    assert abs(_percentile(values, 95) - 9.55) < 1e-9


def test_percentile_sorted_input():
    from modelctl.core.stats import _percentile
    # 乱序输入应自动排序，结果同有序
    shuffled = [10.0, 1.0, 9.0, 5.0, 3.0]
    ordered = [1.0, 3.0, 5.0, 9.0, 10.0]
    assert _percentile(shuffled, 50) == _percentile(ordered, 50)


def test_native_sample_dataclass_field_layout():
    from modelctl.core.stats import _NativeSample
    s = _NativeSample(ts=1.0, tokens_per_second=10.0, prompt_inflight_rate=5.0,
                      ttft_ms=200.0, ttft_s=0.2)
    assert s.ts == 1.0
    assert s.tokens_per_second == 10.0
    assert s.prompt_inflight_rate == 5.0
    assert s.ttft_ms == 200.0
    assert s.ttft_s == 0.2
```

- [ ] **Step 2: 运行并验证失败**

```bash
uv run pytest tests/test_stats_native.py -v
```
Expected: **FAIL**（3 个符号 ImportError）

- [ ] **Step 3: 写最小实现**

编辑 `src/modelctl/core/stats.py`，在 `USAGE_PORT = 5002`（行 46）**之后**、`def _fmt_tokens`（行 49）**之前**插入：

```python
def _parse_env_bool(value: str | None, default: bool = True) -> bool:
    """env 开关解析：{"1","true","yes","on"} → True；{"0","false","no","off"} → False。
    空串/None/未知字符串回退到 default（保持现状行为，避免误关）。"""
    if value is None or value.strip() == "":
        return default
    low = value.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    return default


@dataclass
class _NativeSample:
    """vLLM per-request 原生指标单样本（60s/20 请求滑窗口径）。"""
    ts: float                    # time.monotonic 入账时
    tokens_per_second: float     # vLLM 原生 decode 速率（仅 decode 段）
    prompt_inflight_rate: float  # num_prompt_tokens / ttft_s（与 vLLM avg_prompt gauge 同量纲）
    ttft_ms: float               # time_to_first_token_ms
    ttft_s: float                # 同上（秒）


def _percentile(values: list[float], p: float) -> float | None:
    """线性插值法百分位；空列表返回 None；单元素 P50/P95 都返回该元素。"""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * (p / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac
```

> `@dataclass` 已在文件顶部 import 完毕（行 40 `from dataclasses import dataclass, field`），无需再加 import。

- [ ] **Step 4: 运行并验证通过**

```bash
uv run pytest tests/test_stats_native.py -v
```
Expected: **PASS**（9 个用例）

- [ ] **Step 5: 回归**

```bash
uv run pytest tests/ -q
```
Expected: **全绿**

---

### Task 4: `UsageCollector.record_native_metrics` + `_compute_native_row`

**Files:**
- Modify: `src/modelctl/core/stats.py`（`UsageCollector` 类 行 281-475）
- Test: `tests/test_stats_native.py`（追加）

**Interfaces:**
- Consumes: `_NativeSample` / `_percentile` / `_parse_env_bool`（Task 3）
- Produces:
  - `UsageCollector.__init__` 新增 kwargs：`native_mapping: dict[str, str] | None = None`、`bench_fallback: bool = True`
  - 实例字段：`self.native_mapping`、`self.bench_fallback: bool`、`self._native_window: list[_NativeSample]`、`self._native_window_ttl: float = 60.0`、`self._native_window_cap: int = 20`
  - `_snapshot` dict 新增 key：`ttft_ms`（float）、`ttft_ms_p95`（float）、`rate_source`（str, 默认 `"none"`）
  - `record_native_metrics(self, metric_dict: dict | None) -> None`（静默失败）
  - `_compute_native_row(self) -> dict`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_stats_native.py`：

```python
import tempfile
from pathlib import Path


def _make_collector(tmp_path, **kw):
    from modelctl.core.stats import UsageCollector
    return UsageCollector(
        name="t",
        base_url="http://127.0.0.1:1/m",
        poll_interval=999,
        api_key=None,
        data_dir=tmp_path,
        mode="on-demand",
        mapping=kw.pop("mapping", {}),
        native_mapping=kw.pop("native_mapping", None),
        bench_fallback=kw.pop("bench_fallback", True),
    )


VLLM_NATIVE = {
    "rate": "tokens_per_second",
    "ttft_ms": "time_to_first_token_ms",
    "gen_time_ms": "generation_time_ms",
    "prompt_tokens": "num_prompt_tokens",
    "completion_tokens": "num_generation_tokens",
}


def test_record_native_metrics_updates_snapshot_and_p50(tmp_path):
    c = _make_collector(tmp_path, native_mapping=VLLM_NATIVE)
    clock = {"t": 1000.0}
    c._monotonic = lambda: clock["t"]
    for i in range(20):
        c.record_native_metrics({
            "tokens_per_second": 10.0 + i,
            "time_to_first_token_ms": 100.0 + i,
            "num_prompt_tokens": 32,
        })
    snap = c.snapshot()
    # P50 of [10..29]：19.5（idx=9.5 → 19.0*0.5 + 20.0*0.5）
    # P50 of [100..119]：109.5（idx=9.5 → 109.0*0.5 + 110.0*0.5）
    # P95 of [100..119]：≈118.05
    assert abs(snap["predicted_rate"] - 19.5) < 1e-6, snap["predicted_rate"]
    assert abs(snap["ttft_ms"] - 109.5) < 1e-6, snap["ttft_ms"]
    assert snap["ttft_ms_p95"] > snap["ttft_ms"]
    assert snap["rate_source"] == "native"


def test_record_native_metrics_window_cap_trims_oldest(tmp_path):
    c = _make_collector(tmp_path, native_mapping=VLLM_NATIVE)
    clock = {"t": 1000.0}
    c._monotonic = lambda: clock["t"]
    for _ in range(25):
        c.record_native_metrics({"tokens_per_second": 50.0,
                                 "time_to_first_token_ms": 100.0,
                                 "num_prompt_tokens": 32})
    assert len(c._native_window) == 20


def test_record_native_metrics_ttl_trims_old(tmp_path):
    c = _make_collector(tmp_path, native_mapping=VLLM_NATIVE)
    c._native_window_ttl = 10.0
    clock = {"t": 0.0}
    c._monotonic = lambda: clock["t"]
    for _ in range(5):
        c.record_native_metrics({"tokens_per_second": 10.0,
                                 "time_to_first_token_ms": 100.0,
                                 "num_prompt_tokens": 32})
        clock["t"] += 1.0
    assert len(c._native_window) == 5
    clock["t"] = 1000.0
    c.record_native_metrics({"tokens_per_second": 10.0,
                             "time_to_first_token_ms": 100.0,
                             "num_prompt_tokens": 32})
    assert len(c._native_window) == 1
    assert c._native_window[0].ts == 1000.0


def test_record_native_metrics_invalid_input_still_ok(tmp_path):
    c = _make_collector(tmp_path, native_mapping=VLLM_NATIVE)
    c._monotonic = lambda: 0.0
    c.record_native_metrics(None)
    c.record_native_metrics({})
    c.record_native_metrics({"tokens_per_second": "abc", "time_to_first_token_ms": 100})
    c.record_native_metrics({"tokens_per_second": -1.0, "time_to_first_token_ms": 100,
                             "num_prompt_tokens": 32})
    assert len(c._native_window) == 0
    assert c.snapshot().get("ttft_ms", 0) == 0


def test_record_native_metrics_when_mapping_none_is_noop(tmp_path):
    c = _make_collector(tmp_path)  # native_mapping=None 默认
    c._monotonic = lambda: 0.0
    c.record_native_metrics({"tokens_per_second": 100.0, "time_to_first_token_ms": 200.0})
    assert len(c._native_window) == 0
    assert c.snapshot().get("ttft_ms", 0) == 0


def test_bench_fallback_default_true_without_env(tmp_path, monkeypatch):
    import os
    monkeypatch.delenv("USAGE_BENCH_FALLBACK", raising=False)
    c = _make_collector(tmp_path)
    assert c.bench_fallback is True


def test_bench_fallback_false_when_env_false(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("USAGE_BENCH_FALLBACK", "false")
    c = _make_collector(tmp_path)
    assert c.bench_fallback is False


def test_bench_fallback_explicit_param_overrides_when_env_set(tmp_path, monkeypatch):
    import os
    # env 优先于 kwargs（避免调用方误设）
    monkeypatch.setenv("USAGE_BENCH_FALLBACK", "false")
    c = _make_collector(tmp_path, bench_fallback=True)
    assert c.bench_fallback is False


def test_snapshot_initial_no_native_has_none_source(tmp_path):
    c = _make_collector(tmp_path, native_mapping=VLLM_NATIVE)
    # 尚未 record；_poll_once 未跑（on-demand + 未调 get_snapshot 时 _snapshot 仍是初始）
    # 直接 snapshot() 返回"全 0 & 4 字段初值"
    snap = c.snapshot()
    assert snap["ttft_ms"] == 0.0
    assert snap["ttft_ms_p95"] == 0.0
    assert snap["rate_source"] == "none"
```

- [ ] **Step 2: 运行并验证失败**

```bash
uv run pytest tests/test_stats_native.py -v
```
Expected: **FAIL**（`UsageCollector(...)` TypeError：`unexpected keyword argument 'native_mapping'`）

- [ ] **Step 3: 写最小实现**

编辑 `src/modelctl/core/stats.py` 中 `class UsageCollector`：

3a. `__init__` 签名与主体替换（现 行 288-327）：

```python
    def __init__(
        self,
        name: str,
        base_url: str,
        poll_interval: float,
        api_key: str | None,
        data_dir: Path,
        mode: str = "poll",
        mapping: dict[str, list[str]] | None = None,
        native_mapping: dict[str, str] | None = None,
        bench_fallback: bool = True,
    ) -> None:
        self.name = name
        self.data_dir = data_dir
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self.api_key = api_key
        self.mode = mode
        self.mapping = mapping or {}
        self.native_mapping = native_mapping
        # bench_fallback 优先 env（避免调用方误传覆盖用户关）
        self.bench_fallback = (
            _parse_env_bool(os.environ.get("USAGE_BENCH_FALLBACK"))
            if "USAGE_BENCH_FALLBACK" in os.environ else bench_fallback
        )
        self._lock = threading.Lock()
        self._monotonic = time.monotonic
        self._snapshot: dict[str, object] = {
            "ok": False,
            "error": None,
            "prompt_total": 0.0,
            "predicted_total": 0.0,
            "prompt_rate": 0.0,
            "predicted_rate": 0.0,
            "ttft_ms": 0.0,
            "ttft_ms_p95": 0.0,
            "rate_source": "none",
        }
        self._last = {"time": None, "predicted_total": 0.0}
        self._rate_window: list[tuple[float, float, float]] = []
        self._window_size = 10
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True) if mode == "poll" else None
        self._native_window: list[_NativeSample] = []
        self._native_window_ttl = 60.0
        self._native_window_cap = 20
        persisted_prompt, persisted_predicted = self._load_persisted()
        self._baseline = {
            "prompt_total": persisted_prompt,
            "predicted_total": persisted_predicted,
        }
        self._snapshot["prompt_total"] = persisted_prompt
        self._snapshot["predicted_total"] = persisted_predicted
```

3b. `record_tokens` 方法末尾（现 行 381）**之前**关键一行加：

```python
            # rate_source：native 未覆盖时，本行作为 window_diff 提示（供 snapshot 叠加后仍 stale 也能查）
            if prompt_rate > 0 and self._snapshot["rate_source"] in ("none",):
                self._snapshot["rate_source"] = "window_diff"
```

（放在 `self._persist(new_prompt, new_predicted)` **之前**；语义：token 差分可用 → 更新 source。若不更新也不影响功能，但会为后续 snapshot 无数据 state 提供兜底 label。）

3c. 新增两个方法（放 `record_tokens` 之后、`_compute_window_rate` **之前**，代码保持紧凑）：

```python
    def record_native_metrics(self, metric_dict: dict | None) -> None:
        """网关按每请求喂入 vLLM 原生 per-request 指标对象。

        - self.native_mapping 为 None（非 vLLM 引擎）或 metric_dict 非 dict 时静默返回；
        - 关键字段缺失 / 非数值 / 非正 直接跳过（不推入窗口）；
        - 双约束裁剪：时龄 > ttl 或 容量 > cap 都弹最早；
        - 方法不含任何 I/O / HTTP，仅锁内内存写；失败自动被 handler 隔离。
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
            while self._native_window and (
                now - self._native_window[0].ts > self._native_window_ttl
                or len(self._native_window) > self._native_window_cap
            ):
                self._native_window.pop(0)

    def _compute_native_row(self) -> dict:
        """基于 native 滑窗算 P50/P95；空滑窗返回全 0 + has_any=False。

        返回值结构：
        {"ttft_ms": float, "ttft_ms_p95": float,
         "prompt_rate": float, "predicted_rate": float, "has_any": bool}
        """
        with self._lock:
            samples = list(self._native_window)
        if not samples:
            return {"ttft_ms": 0.0, "ttft_ms_p95": 0.0,
                    "prompt_rate": 0.0, "predicted_rate": 0.0, "has_any": False}
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

3d. `_poll_once` 在 `metrics["prompt_rate"] = prompt_rate` / `metrics["predicted_rate"] = predicted_rate`（现 行 457-458）**之后**、`with self._lock:`（行 460）**之前**加：

```python
        # rate_source：gauge 优先，否则 window_diff
        source = "none"
        if metrics["prompt_rate"] > 0 or metrics["predicted_rate"] > 0:
            source = "engine_gauge"
        elif prompt_rate > 0 or predicted_rate > 0:
            source = "window_diff"
```

`with self._lock: self._snapshot = {...}` 里追加 3 项：`"ttft_ms": 0.0, "ttft_ms_p95": 0.0, "rate_source": source`（4 字段由 poll 首次就要带，避免 snapshot() 内 join 时 KeyError）。

3e. **重写 `snapshot()`**（现 行 473-475）：

```python
    def snapshot(self) -> dict:
        """返回用量快照（含 native 合并后的速率与 TTFT / rate_source）。

        关键字段（现有）：ok / error / prompt_total / predicted_total / prompt_rate / predicted_rate。
        新追加（spec 定义）：ttft_ms / ttft_ms_p95 / rate_source（native 首命中，否则保持 _snapshot 原值）。
        本方法**无副作用**，可反复调用。
        """
        with self._lock:
            base = dict(self._snapshot)
        native_row = self._compute_native_row()
        # 速率：native 优先，回退到已有 gauge / window_diff 值
        base["prompt_rate"] = native_row["prompt_rate"] or base.get("prompt_rate") or 0.0
        base["predicted_rate"] = native_row["predicted_rate"] or base.get("predicted_rate") or 0.0
        # TTFT 仅来源于 native_row（其他档 2/3 不产 TTFT）
        base["ttft_ms"] = native_row["ttft_ms"]
        base["ttft_ms_p95"] = native_row["ttft_ms_p95"]
        # rate_source：native 首命中，否则保持 _snapshot 初值 / poll 写入值
        if base.get("rate_source") == "none" and native_row["has_any"] and (
            native_row["prompt_rate"] or native_row["predicted_rate"] or native_row["ttft_ms"]
        ):
            base["rate_source"] = "native"
        return base
```

- [ ] **Step 4: 运行并验证通过**

```bash
uv run pytest tests/test_stats_native.py -v
```
Expected: **PASS**（9 + 9 = 18 个用例，前 9 是 Task 3，新 9 是 Task 4）

- [ ] **Step 5: 回归**

```bash
uv run pytest tests/ -q
```
Expected: **全绿**（重点：`tests/test_stats.py` 现有 `test_usage_collector_*` 用例仍通过——snapshot 新加字段不会破坏旧读法）

---

### Task 5: `UsageHandler._build_target_payload` bench gate + `build_usage_payload` extra

**Files:**
- Modify: `src/modelctl/core/stats.py`（`build_usage_payload` 行 171-207、`UsageHandler._build_target_payload` 行 529-553）
- Test: `tests/test_stats_native.py`（追加）

**Interfaces:**
- Consumes: Task 4 的 `collector.snapshot()["ttft_ms" | "rate_source"]` / `collector.bench_fallback`
- Produces:
  - `build_usage_payload` 在 `payload["extra"]` 末尾追加 `"| 首 Token P50 = xxx ms（P95 = yyy ms）"`（仅 `ttft_ms > 0` 时）
  - `_build_target_payload` bench gate：`native_has_any or not bench_fallback` 时跳过 `_bench_cached`；否则 bench 覆盖后回写 `rate_source=bench` + `ttft_ms=float(bench[2])`
  - `/api/usage` JSON 追加 `ttft_ms` / `ttft_ms_p95` / `rate_source` 三字段（仅非空/非零）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_stats_native.py`：

```python
def test_build_usage_payload_extra_includes_ttft_when_nonzero():
    from modelctl.core.stats import build_usage_payload
    tokens = {
        "prompt_total": 100.0, "predicted_total": 200.0,
        "prompt_rate": 10.0, "predicted_rate": 20.0,
        "ttft_ms": 123.0, "ttft_ms_p95": 210.0,
    }
    payload = build_usage_payload(tokens, {}, 0.0, 1.0)
    assert "首 Token P50 = 123 ms（P95 = 210 ms）" in payload["extra"]
    assert payload["prompt_rate"] == 10.0


def test_build_usage_payload_extra_no_ttft_when_zero():
    from modelctl.core.stats import build_usage_payload
    tokens = {"prompt_total": 0.0, "predicted_total": 0.0,
              "prompt_rate": 0.0, "predicted_rate": 0.0}
    payload = build_usage_payload(tokens, {}, 0.0, 1.0)
    assert "首 Token P50" not in payload["extra"]


def test_build_usage_payload_ttft_p95_optional_when_zero():
    from modelctl.core.stats import build_usage_payload
    tokens = {"prompt_total": 0.0, "predicted_total": 0.0,
              "prompt_rate": 0.0, "predicted_rate": 0.0,
              "ttft_ms": 50.0}
    p = build_usage_payload(tokens, {}, 0.0, 1.0)
    assert "首 Token P50 = 50 ms" in p["extra"]
    assert "P95" not in p["extra"]


def _make_handler_target(name, **kw):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    return SimpleNamespace(
        name=name, aliases=[], data_dir=MagicMock(),
        metrics_url="x", mapping={}, usage_cfg={}, api_key=None,
        bench_url="http://127.0.0.1:1/x", bench_model=name, **kw,
    )


def test_build_target_payload_skips_bench_when_native_ttft_present():
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch
    from modelctl.core import stats as S

    fake_collector = MagicMock()
    fake_collector.bench_fallback = True
    fake_collector.get_snapshot.return_value = {
        "ok": True, "prompt_total": 100.0, "predicted_total": 200.0,
        "prompt_rate": 0.0, "predicted_rate": 0.0,
        "ttft_ms": 123.0, "ttft_ms_p95": 210.0, "rate_source": "native",
    }
    S.UsageHandler.collectors = {"m1": fake_collector}
    S.UsageHandler.targets = [_make_handler_target("m1")]
    target = S.UsageHandler.targets[0]
    with patch.object(S, "_bench_cached") as m_bench:
        m_bench.return_value = (1.0, 2.0, 3)
        payload = S.UsageHandler._build_target_payload(target)
        m_bench.assert_not_called()
        assert payload.get("ttft_ms") == 123.0
        assert payload.get("rate_source") == "native"
    S.UsageHandler.targets = []
    S.UsageHandler.collectors = {}


def test_build_target_payload_runs_bench_when_all_zero_and_switch_on():
    from unittest.mock import MagicMock, patch
    from modelctl.core import stats as S

    fake_collector = MagicMock()
    fake_collector.bench_fallback = True
    fake_collector.get_snapshot.return_value = {
        "ok": True, "prompt_total": 0.0, "predicted_total": 0.0,
        "prompt_rate": 0.0, "predicted_rate": 0.0,
        "ttft_ms": 0.0, "ttft_ms_p95": 0.0, "rate_source": "none",
    }
    S.UsageHandler.collectors = {"m2": fake_collector}
    S.UsageHandler.targets = [_make_handler_target("m2")]
    target = S.UsageHandler.targets[0]
    with patch.object(S, "_bench_cached", return_value=(1.5, 2.5, 9)) as m_bench:
        payload = S.UsageHandler._build_target_payload(target)
        m_bench.assert_called_once()
        assert payload["prompt_rate"] == 1.5
        assert payload["predicted_rate"] == 2.5
        assert payload.get("rate_source") == "bench"
        assert payload.get("ttft_ms") == 9.0
    S.UsageHandler.targets = []
    S.UsageHandler.collectors = {}


def test_build_target_payload_skips_bench_when_switch_off():
    from unittest.mock import MagicMock, patch
    from modelctl.core import stats as S

    fake_collector = MagicMock()
    fake_collector.bench_fallback = False
    fake_collector.get_snapshot.return_value = {
        "ok": True, "prompt_total": 0.0, "predicted_total": 0.0,
        "prompt_rate": 0.0, "predicted_rate": 0.0,
        "ttft_ms": 0.0, "ttft_ms_p95": 0.0, "rate_source": "none",
    }
    S.UsageHandler.collectors = {"m3": fake_collector}
    S.UsageHandler.targets = [_make_handler_target("m3")]
    target = S.UsageHandler.targets[0]
    with patch.object(S, "_bench_cached") as m_bench:
        payload = S.UsageHandler._build_target_payload(target)
        m_bench.assert_not_called()
        assert payload.get("rate_source") in ("none", None)
        assert payload.get("ttft_ms") in (None, 0.0)
    S.UsageHandler.targets = []
    S.UsageHandler.collectors = {}
```

> 注：`_make_handler_target` 用的 `MagicMock` 在测试文件顶部 import（追加 `from unittest.mock import MagicMock` 到 import 段）。

- [ ] **Step 2: 运行并验证失败**

```bash
uv run pytest tests/test_stats_native.py -k "build_usage_payload_ttft or build_target_payload" -v
```
Expected: **FAIL**（extra 无 TTFT 段；handler 无 gate → bench 会调用；`payload["ttft_ms"]` 未透传）

- [ ] **Step 3: 写最小实现**

编辑 `src/modelctl/core/stats.py`：

3a. `build_usage_payload`（现 行 171-207）里的 `extra` 段替换：

```python
    ttft_ms_val = tokens.get("ttft_ms") or 0.0
    ttft_p95_val = tokens.get("ttft_ms_p95") or 0.0
    ttft_suffix = ""
    if ttft_ms_val > 0:
        p95_str = f"（P95 = {round(ttft_p95_val)} ms）" if ttft_p95_val > 0 else ""
        ttft_suffix = f"| 首 Token P50 = {round(ttft_ms_val)} ms{p95_str}"
    extra = (
        f"累计 {_fmt_tokens(prompt + predicted)} toks"
        f"（输入 {_fmt_tokens(prompt)}/输出 {_fmt_tokens(predicted)}）"
        f"| 输入速率 {prompt_rate:.1f} tok/s"
        f"| 输出速率 {predicted_rate:.1f} tok/s"
        + ttft_suffix
    )
```

原 `extra = (...)` **删除**，全部用新块替代。

3b. `UsageHandler._build_target_payload`（现 行 529-553）**整体替换**：

```python
    def _build_target_payload(self, target: StatsTarget) -> dict:
        """按 target 构造 /api/usage 单模型响应。

        字段：isValid / used / unit / planName / extra / prompt_rate / predicted_rate /
              total / remaining / model；追加 ttft_ms / ttft_ms_p95 / rate_source（仅非空）。
        兜底：native 任一非 0 → 跳过 bench（避免伪造请求覆盖真实数据）。
        """
        if target.mapping is None:
            return {"error": "该引擎不支持精确统计"}
        collector = self.collectors.get(target.name)
        if collector is None:
            return {"error": "该引擎不支持精确统计"}
        snap = collector.get_snapshot()
        if not snap["ok"]:
            return {"isValid": False,
                    "invalidMessage": f"{target.name} 不可用：{snap['error'] or '未知错误'}"}
        tokens = dict(snap)
        # 4 字段全 0 且 bench_fallback 允许时才 bench
        native_has_any = (
            (tokens.get("prompt_rate") or 0) > 0
            or (tokens.get("predicted_rate") or 0) > 0
            or (tokens.get("ttft_ms") or 0) > 0
        )
        bench_fallback_enabled = getattr(collector, "bench_fallback", True) is True
        should_bench = (
            not native_has_any
            and bench_fallback_enabled
            and (tokens.get("prompt_rate", 0.0) == 0
                 or tokens.get("predicted_rate", 0.0) == 0)
        )
        if should_bench:
            bench = _bench_cached(target)
            if bench is not None:
                if tokens.get("prompt_rate", 0.0) == 0:
                    tokens["prompt_rate"] = bench[0]
                if tokens.get("predicted_rate", 0.0) == 0:
                    tokens["predicted_rate"] = bench[1]
                if (tokens.get("ttft_ms") or 0) == 0:
                    tokens["ttft_ms"] = float(bench[2])
                tokens["rate_source"] = "bench"
        payload = build_usage_payload(tokens, target.usage_cfg, self.start_time, time.time())
        payload["model"] = target.name
        payload["planName"] = f"{target.name} 本地部署"
        # 3 新字段透传（仅非 0/非空时加入）
        if (tokens.get("ttft_ms") or 0) > 0:
            payload["ttft_ms"] = tokens["ttft_ms"]
            if (tokens.get("ttft_ms_p95") or 0) > 0:
                payload["ttft_ms_p95"] = tokens["ttft_ms_p95"]
        if tokens.get("rate_source"):
            payload["rate_source"] = tokens["rate_source"]
        return payload
```

- [ ] **Step 4: 运行并验证通过**

```bash
uv run pytest tests/test_stats_native.py -k "build_usage_payload_ttft or build_target_payload" -v
```
Expected: **PASS**（6 个用例）

- [ ] **Step 5: 回归（重点）**

现有 `tests/test_stats.py::test_build_target_payload_benchmarks_when_idle` **必须保持通过**——语义是 "全 0 + 开关默认 True → bench 调用"。新 gate 前提：`native_has_any=False`，`bench_fallback_enabled=True`，速率 0 → `should_bench=True`，等价旧行为。

```bash
uv run pytest tests/ -q
```
Expected: **全绿**

---

### Task 6: `StatsTarget` / `run_server` / `_targets_from_profiles` 注入 `native_mapping`

**Files:**
- Modify: `src/modelctl/core/stats.py`（`StatsTarget` 行 246-259、`run_server` 行 680-695、`_targets_from_profiles` 行 717-741）
- Test: `tests/test_stats_native.py`（追加）

**Interfaces:**
- Produces:
  - `StatsTarget` dataclass 加 `native_mapping: dict[str, str] | None = None`（**最后位**，向后兼容）
  - `_targets_from_profiles(data_dir)` 内部为每个 adapter 调 `native_metrics_mapping()` 填
  - `run_server` 的 `UsageCollector(...)` 构造透传 `native_mapping=target.native_mapping` 与 `bench_fallback=_parse_env_bool(os.environ["USAGE_BENCH_FALLBACK"])`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_stats_native.py`：

```python
def test_targets_from_profiles_includes_native_mapping_for_vllm(tmp_path, monkeypatch):
    """vLLM profile：StatsTarget.native_mapping 应来自 adapter.native_metrics_mapping()。"""
    from unittest.mock import MagicMock, patch
    from modelctl.core import stats as S

    fake_profile = MagicMock()
    fake_profile.name = "qwen3.8"
    fake_profile.engine = "vllm"
    fake_profile.port = 8000
    fake_profile.api_key = None
    fake_profile.aliases = ["qwen3.8-vllm"]
    fake_profile.usage = {"price_in": 1.0, "price_out": 2.0}

    fake_adapter = MagicMock()
    fake_adapter.metrics_mapping.return_value = {
        "prompt_total": ["vllm:prompt_tokens_total"],
        "predicted_total": ["vllm:generation_tokens_total"],
        "prompt_rate": [], "predicted_rate": [],
    }
    fake_adapter.native_metrics_mapping.return_value = {
        "rate": "tokens_per_second",
        "ttft_ms": "time_to_first_token_ms",
        "gen_time_ms": "generation_time_ms",
        "prompt_tokens": "num_prompt_tokens",
        "completion_tokens": "num_generation_tokens",
    }
    fake_adapter.upstream_model_name.return_value = "qwen3.8"

    with patch("modelctl.core.profile.list_profiles", return_value=[fake_profile]), \
         patch("modelctl.engines.get_adapter") as m_ga:
        m_ga.return_value = lambda p, c: fake_adapter
        targets = S._targets_from_profiles(tmp_path)
    assert len(targets) == 1
    t = targets[0]
    assert t.native_mapping is not None
    assert t.native_mapping["rate"] == "tokens_per_second"


def test_targets_from_profiles_native_mapping_none_for_unsupported_engine(tmp_path):
    """非 vLLM 引擎：native_mapping 为 None（向后兼容路径）。"""
    from unittest.mock import MagicMock, patch
    from modelctl.core import stats as S

    fake_profile = MagicMock()
    fake_profile.name = "q"
    fake_profile.engine = "ollama"
    fake_profile.port = 11434
    fake_profile.api_key = None
    fake_profile.aliases = []
    fake_profile.usage = {}

    fake_adapter = MagicMock()
    fake_adapter.metrics_mapping.return_value = {
        "prompt_total": ["llama_prompt_tokens_total"],
        "predicted_total": ["llama_generation_tokens_total"],
        "prompt_rate": [], "predicted_rate": [],
    }
    fake_adapter.native_metrics_mapping.return_value = None
    fake_adapter.upstream_model_name.return_value = "q"

    with patch("modelctl.core.profile.list_profiles", return_value=[fake_profile]), \
         patch("modelctl.engines.get_adapter") as m_ga:
        m_ga.return_value = lambda p, c: fake_adapter
        targets = S._targets_from_profiles(tmp_path)
    assert targets[0].native_mapping is None


- [ ] **Step 1: 写失败测试**

追加到 `tests/test_stats_native.py`：

```python
def test_targets_from_profiles_includes_native_mapping_for_vllm(tmp_path, monkeypatch):
    """vLLM profile：StatsTarget.native_mapping 由 adapter.native_metrics_mapping() 填充。"""
    from unittest.mock import MagicMock, patch
    from modelctl.core import stats as S

    fake_profile = MagicMock()
    fake_profile.name = "qwen3.8"
    fake_profile.engine = "vllm"
    fake_profile.port = 8000
    fake_profile.api_key = None
    fake_profile.aliases = ["qwen3.8-vllm"]
    fake_profile.usage = {"price_in": 1.0, "price_out": 2.0}

    fake_adapter = MagicMock()
    fake_adapter.metrics_mapping.return_value = {
        "prompt_total": ["vllm:prompt_tokens_total"],
        "predicted_total": ["vllm:generation_tokens_total"],
        "prompt_rate": [], "predicted_rate": [],
    }
    fake_adapter.native_metrics_mapping.return_value = dict(VLLM_NATIVE)
    fake_adapter.upstream_model_name.return_value = "qwen3.8"

    with patch("modelctl.core.profile.list_profiles", return_value=[fake_profile]), \
         patch("modelctl.engines.get_adapter") as m_ga:
        m_ga.return_value = lambda p, c: fake_adapter
        targets = S._targets_from_profiles(tmp_path)
    assert len(targets) == 1
    assert targets[0].native_mapping is not None
    assert targets[0].native_mapping["rate"] == "tokens_per_second"


def test_targets_from_profiles_native_mapping_none_for_unsupported_engine(tmp_path):
    """非 vLLM 引擎：native_mapping 为 None（向后兼容路径）。"""
    from unittest.mock import MagicMock, patch
    from modelctl.core import stats as S

    fake_profile = MagicMock()
    fake_profile.name = "q-ollama"
    fake_profile.engine = "ollama"
    fake_profile.port = 11434
    fake_profile.api_key = None
    fake_profile.aliases = []
    fake_profile.usage = {}

    fake_adapter = MagicMock()
    fake_adapter.metrics_mapping.return_value = {
        "prompt_total": ["ollama_prompt_tokens_total"],
        "predicted_total": ["llama_generation_tokens_total"],
        "prompt_rate": [], "predicted_rate": [],
    }
    fake_adapter.native_metrics_mapping.return_value = None
    fake_adapter.upstream_model_name.return_value = "q"

    with patch("modelctl.core.profile.list_profiles", return_value=[fake_profile]), \
         patch("modelctl.engines.get_adapter") as m_ga:
        m_ga.return_value = lambda p, c: fake_adapter
        targets = S._targets_from_profiles(tmp_path)
    assert targets[0].native_mapping is None
```

> **注**：不测 `run_server` 本身的注入——太深（要起 server / 线程），且 Task 4 已直接测试 `UsageCollector(..., native_mapping=..)` constructor signature，Task 6 保证的是 **target → collector 传参的最后一公里**（在代码 diff 中可见）。

- [ ] **Step 2: 运行并验证失败**

```bash
uv run pytest tests/test_stats_native.py -k "targets_from_profiles" -v
```
Expected: **FAIL**（`StatsTarget` 无 `native_mapping` 字段——`AttributeError`）

- [ ] **Step 3: 写最小实现**

编辑 `src/modelctl/core/stats.py`：

3a. `StatsTarget` dataclass 追加字段（现 行 246-259，追加为**最后一个**字段保持向后兼容）：

```python
@dataclass
class StatsTarget:
    """单个模型的用量统计目标。mapping 为 None 表示该引擎不支持精确统计。"""
    name: str
    data_dir: Path
    metrics_url: str
    mapping: dict[str, list[str]] | None
    usage_cfg: dict = field(default_factory=dict)
    api_key: str | None = None
    aliases: list[str] = field(default_factory=list)
    # 主动测速配置（bench_url 为 None = 窗口无流量时不做兜底测速）
    bench_url: str | None = None
    bench_model: str | None = None
    # per-request 原生指标字段映射（仅 vLLM 双 flag 均开才非 None；其他引擎 None）
    native_mapping: dict[str, str] | None = None
```

3b. `run_server` 中的 `UsageCollector(...)`（现 行 685-693）追加 kwargs：

```python
            collector = UsageCollector(
                target.name,
                target.metrics_url.removesuffix("/metrics"),
                poll_interval,
                target.api_key,
                target.data_dir,
                mode=mode,
                mapping=target.mapping,
                native_mapping=target.native_mapping,
                bench_fallback=_parse_env_bool(os.environ.get("USAGE_BENCH_FALLBACK")),
            )
```

3c. `_targets_from_profiles`（现 行 717-741）**改造**取 adapter 的 native 并透传：

```python
def _targets_from_profiles(data_dir: Path) -> list[StatsTarget]:
    """从 models/*.yaml 构造统计目标（供独立运行 / 后台化）。"""
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import list_profiles
    from modelctl.engines import get_adapter

    targets: list[StatsTarget] = []
    for profile in list_profiles():
        adapter = get_adapter(profile.engine)(profile, Capabilities())
        try:
            native_mapping = adapter.native_metrics_mapping()
        except (NotImplementedError, AttributeError):
            native_mapping = None
        targets.append(
            StatsTarget(
                name=profile.name,
                data_dir=data_dir,
                metrics_url=f"http://127.0.0.1:{profile.port}/metrics",
                mapping=adapter.metrics_mapping(),
                usage_cfg=profile.usage,
                api_key=profile.api_key,
                aliases=profile.aliases,
                bench_url=f"http://127.0.0.1:{profile.port}/v1/chat/completions",
                bench_model=adapter.upstream_model_name(),
                native_mapping=native_mapping,
            )
        )
    return targets
```

- [ ] **Step 4: 运行并验证通过**

```bash
uv run pytest tests/test_stats_native.py -k "targets_from_profiles" -v
```
Expected: **PASS**（2 个用例）

- [ ] **Step 5: 回归**

```bash
uv run pytest tests/ -q
```
Expected: **全绿**（关键：`tests/test_modelctl.py` 现有 `modelctl list.status` / `list.gateways` 的 stats 集成也不应受 StatsTarget 新字段默认值影响）

---

### Task 7: 网关 SSE + 非流式挂钩 `record_native_metrics`

**Files:**
- Modify: `src/modelctl/core/gateway.py`（`_sse_stream` finally 块 行 892-901、非流式分支 行 919 之后）
- Test: `tests/test_gateway.py`（追加；复用现有 `_run` / `_post` / `create_app` / `GatewayModel` fixture 模式）

**Interfaces:**
- Consumes: `UsageCollector.record_native_metrics`（Task 4 产出）
- Produces: 两处新钩子。SSE finally（在审计 write 之前、`await client.aclose()` 之前）+ 非流式（在 `record_tokens` 之后、审计 `record` 之前）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_gateway.py`：

```python
def test_proxy_sse_hooks_stats_collector_record_native_metrics(tmp_path):
    """SSE 末块含 metrics 时应调用 collector.record_native_metrics（一次）。"""
    from modelctl.core.gateway import GatewayModel, create_app, GatewayModel
    from modelctl.core.stats import UsageCollector
    from pathlib import Path
    import tempfile

    c_dir = Path(tempfile.mkdtemp(prefix="gw-native-"))
    collector = UsageCollector(
        name="q", base_url="http://127.0.0.1:1", poll_interval=999,
        api_key=None, data_dir=c_dir, mode="on-demand",
        mapping={},
        native_mapping={
            "rate": "tokens_per_second",
            "ttft_ms": "time_to_first_token_ms",
            "gen_time_ms": "generation_time_ms",
            "prompt_tokens": "num_prompt_tokens",
            "completion_tokens": "num_generation_tokens",
        },
    )

    sse_bytes = (
        b'data: {"choices":[{"delta":{"content":"a"}}], '
        b'"usage":{"prompt_tokens":32,"completion_tokens":1}}\n\n'
        b'data: {"choices":[], "usage":{"prompt_tokens":32,"completion_tokens":3}, '
        b'"metrics":{"tokens_per_second":100.0,"time_to_first_token_ms":50.0,'
        b'"num_prompt_tokens":32,"num_generation_tokens":3}}\n\n'
        b'data: [DONE]\n\n'
    )

    def upstream(request):
        return httpx.Response(
            200,
            stream=httpx.ByteStream(sse_bytes),
            headers={"content-type": "text/event-stream"},
        )

    m = GatewayModel("q", "vllm", "http://127.0.0.1:1", "q", None,
                     "http://127.0.0.1:1/", collector=collector)
    reg = {"q": m}
    app = create_app(reg, default_model="q", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions",
                      json={"model": "q", "stream": True, "messages": []}))
    assert resp.status_code == 200
    # 断言：collector 收到 native 样本
    assert len(collector._native_window) == 1
    sample = collector._native_window[0]
    assert sample.tokens_per_second == 100.0
    assert sample.ttft_ms == 50.0


def test_proxy_non_stream_hooks_stats_collector_record_native_metrics(tmp_path):
    """非流式响应根级含 metrics 时应调用 collector.record_native_metrics。"""
    from modelctl.core.gateway import GatewayModel, create_app
    from modelctl.core.stats import UsageCollector
    from pathlib import Path
    import tempfile

    c_dir = Path(tempfile.mkdtemp(prefix="gw-native-ns-"))
    collector = UsageCollector(
        name="q", base_url="http://127.0.0.1:1", poll_interval=999,
        api_key=None, data_dir=c_dir, mode="on-demand",
        mapping={},
        native_mapping={
            "rate": "tokens_per_second",
            "ttft_ms": "time_to_first_token_ms",
            "gen_time_ms": "generation_time_ms",
            "prompt_tokens": "num_prompt_tokens",
            "completion_tokens": "num_generation_tokens",
        },
    )

    body = {
        "id": "msg", "object": "chat.completion",
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 32, "completion_tokens": 3},
        "metrics": {"tokens_per_second": 80.0, "time_to_first_token_ms": 40.0,
                     "num_prompt_tokens": 32, "num_generation_tokens": 3},
    }

    def upstream(request):
        return httpx.Response(200, json=body)

    m = GatewayModel("q", "vllm", "http://127.0.0.1:1", "q", None,
                     "http://127.0.0.1:1/", collector=collector)
    reg = {"q": m}
    app = create_app(reg, default_model="q", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions",
                      json={"model": "q", "stream": False, "messages": []}))
    assert resp.status_code == 200
    assert len(collector._native_window) == 1
    assert collector._native_window[0].tokens_per_second == 80.0


def test_proxy_sse_without_metrics_and_unsupported_engine_is_noop(tmp_path):
    """非 vLLM 引擎（native_mapping=None）：record_native_metrics 静默 noop。"""
    from modelctl.core.gateway import GatewayModel, create_app
    from modelctl.core.stats import UsageCollector
    from pathlib import Path
    import tempfile

    c_dir = Path(tempfile.mkdtemp(prefix="gw-native-skip-"))
    collector = UsageCollector(
        name="ollama-x", base_url="http://127.0.0.1:1", poll_interval=999,
        api_key=None, data_dir=c_dir, mode="on-demand",
        mapping={}, native_mapping=None,  # ← ollama llamacpp 等
    )

    sse_bytes = b'data: {"choices":[{"delta":{"content":"a"}}]}\n\ndata: [DONE]\n\n'

    def upstream(request):
        return httpx.Response(200, stream=httpx.ByteStream(sse_bytes),
                              headers={"content-type": "text/event-stream"})

    m = GatewayModel("ollama-x", "ollama", "http://127.0.0.1:1", "ollama-x", None,
                     "http://127.0.0.1:1/", collector=collector)
    reg = {"ollama-x": m}
    app = create_app(reg, default_model="ollama-x", transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions",
                      json={"model": "ollama-x", "stream": True, "messages": []}))
    assert resp.status_code == 200
    assert len(collector._native_window) == 0  # native 无
```

> **实现注记**：`GatewayModel` 数据类需要 `collector` 参数。若现有 `GatewayModel` 已含 `collector: UsageCollector | None = None` 字段**直接复用**；若没有，参考 `rector` 行 442-460 附近的构造一并补齐（`collector: "UsageCollector | Any" = field(default=None)`）。实现者先 Read `src/modelctl/core/gateway.py` 50-120 行确认。

- [ ] **Step 2: 运行并验证失败**

```bash
uv run pytest tests/test_gateway.py -k "hooks_stats_collector_record_native or unsupported_engine_is_noop" -v
```
Expected: **FAIL**（`_sse_stream` 和流式分支还没调 `record_native_metrics`；`collector` 可能也不是 GatewayModel 字段）

- [ ] **Step 3: 写最小实现**

编辑 `src/modelctl/core/gateway.py`：

3a. 打开文件顶部定位 `class GatewayModel`。若 `collector` 字段已存在则跳过；若不存在，在 dataclass 主体里追加：

```python
    # Token 计数收集器；构建 / proxy 时由各路径根据 engine 判定注入
    # 类型 Annotation 用字符串避免 import 时循环依赖
    collector: "UsageCollector | None" = None
```

**注意**：`from __future__ import annotations` 已在顶部 import；`UsageCollector` 用字符串前向引用**没有实际 import 依赖**（内建 dataclass 可延迟求值）。若代码风格不允许前向引用可改 `Type["UsageCollector"] | None = None`，或最简单——**不加新字段，直接靠 `GatewayModel.__init__` 剩余接受 `**kw` 并 setattr**（现有 `ModelInfo` / `GatewayModel` 是否支持）**先 Read** 实际 `dataclass` 主体决定。**首选**：加 dataclass 字段（保持显式）。

3b. `get_collector`（`src/modelctl/core/gateway.py` 行 253-274）**追加 kwargs**：

```python
def get_collector(profile: Profile, adapter: EngineAdapter, data_dir: Path) -> "UsageCollector | None":
    """按引擎用量能力创建收集器：metrics_mapping 非 None 且其 token 计数器可轮询（非恒 0）。
    ...（原 docstring 保留）"""
    from modelctl.core.process import cache_dir
    from modelctl.core.stats import _parse_env_bool  # 顶部通常已有，可复用

    mapping = adapter.metrics_mapping()
    if mapping is None:
        return None
    data = data_dir or cache_dir()
    try:
        native_mapping = adapter.native_metrics_mapping()
    except (NotImplementedError, AttributeError):
        native_mapping = None
    return UsageCollector(
        profile.name,
        f"http://127.0.0.1:{profile.port}",
        5.0,
        profile.api_key,
        data,
        mode="on-demand",
        mapping=mapping,
        native_mapping=native_mapping,
        bench_fallback=_parse_env_bool(os.environ.get("USAGE_BENCH_FALLBACK")),
    )
```

> **注**：`gateway.py` 顶部未直接 `from modelctl.core.stats import _parse_env_bool`；按现有 lazy import 风格在函数内从 `modelctl.core.stats._parse_env_bool` import 一次即可。不要写 `from modelctl.core import stats as S` 全 import。

3c. `_sse_stream` 内加挂钩。定位锚点：行 892-900 的 `self_audit_log.record(...)` 调用块后、行 901 `await client.aclose()` 前。追加：

```python
                        # 新增：把 native 样本喂 stats collector（仅 vLLM 非 None mapping 才有效）
                        if collector is not None and seen_metrics is not None:
                            try:
                                collector.record_native_metrics(seen_metrics)
                            except Exception as exc:  # noqa: BLE001 — stats 静默，不中断 SSE
                                logger.warning(f"stats 记录 native metrics 异常（SSE 不中断）: {exc}")
```

> 位置：紧贴 `except Exception as exc: logger.warning(f"审计写盘异常...");`（行 899-900）**之后**、`await client.aclose()`（行 901）**之前**。

3d. 非流式分支加挂钩。定位锚点：行 908-917 是 `record_tokens` 分支（body line 916 `target.collector.record_tokens(prompt, completion)` 之后、行 920-921 `_snap_after = ...` 之前）追加：

```python
                # 新增：非流式 metrics 挂钩
                if _native is not None and target.collector is not None:
                    try:
                        target.collector.record_native_metrics(_native)
                    except Exception as exc:  # noqa: BLE001 — stats 静默
                        logger.warning(f"stats 记录 native metrics 异常（非流式不中断）: {exc}")
```

> **变量名注意**：非流式分支现有代码里 `_native` 是 939 行以后**审计段**才定义的。要么把挂钩移到 **行 917 之前**（那样 `_native` 还没赋值）——需先提取：**改造**：`target.collector` 拿到 `record_tokens` 后从 `upstream.content` 取一次 `json.keys` 抽 `metrics`。**简化**：直接把挂钩移到 existing 审计 try 块内一起（行 934 之后、行 949 之前），紧贴 `record_tokens` 也可以。**决定**：挂钩在 existing 审计 try 块内、`self_audit_log.record(...)` **之前**：

```python
                        # 新增：非流式 metrics 挂钩（复用已解析的 _native）
                        if _native is not None and target.collector is not None:
                            try:
                                target.collector.record_native_metrics(_native)
                            except Exception as exc:  # noqa: BLE001 — stats 静默
                                logger.warning(f"stats 记录 native metrics 异常（非流式不中断）: {exc}")
```

追加在 `_finish` 赋值段（行 948-950）**之后**、`self_audit_log.record(_build_audit_entry(...))`（现有行 962）**之前**。

3c/3d 的 hooks 都是**无 import**（仅调用 `collector` 上已有方法）+ **try/except 静默**——绝不中断 SSE / HTTP 响应。

- [ ] **Step 4: 运行并验证通过**

```bash
uv run pytest tests/test_gateway.py -k "hooks_stats_collector_record_native or unsupported_engine_is_noop" -v
```
Expected: **PASS**（3 个用例）

- [ ] **Step 5: 回归（重点）**

现 `tests/test_gateway.py` 全部 proxy_sse / proxy_non_stream 用例**必须保持通过**——新挂钩不影响转发行为、且当 `collector=None` 时 `if collector is not None` 短路。

```bash
uv run pytest tests/ -q
```
Expected: **全绿**

---

### Task 8: `cli.py._token_rate_data` 升级透传 native ttft 与 USAGE_BENCH_FALLBACK 开关

**Files:**
- Modify: `src/modelctl/cli.py:387-446`（`_token_rate_data` 函数体）
- Test: `tests/test_cli_native_ttft.py`（**新建**，或追加到 `tests/test_modelctl.py` 若已内联）

**Interfaces:**
- Consumes: `/api/usage` 响应 JSON 追加的 `ttft_ms` / `ttft_ms_p95` / `rate_source` 三字段（Task 5 产出）
- Produces:
  - `_token_rate_data(profile, caps)` 返回值新增：
    - 当 stats 有效 + ttft_ms > 0 时 → `ttft_ms: int`（透传）、`source: "stats"`
    - 当 bench_fallback 关闭且 stats 无效时 → `source: None`（不发起 served 请求）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_cli_native_ttft.py`：

```python
#!/usr/bin/env python3
"""cli.py._token_rate_data native ttft 透传 + env 开关门控。"""

from __future__ import annotations

import urllib.request
from unittest.mock import MagicMock, patch


def _fake_urlopen_response(data: dict):
    import json
    class _Resp:
        def __init__(self, payload: bytes):
            self._payload = payload
        def read(self) -> bytes:
            return self._payload
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    return _Resp(json.dumps(data).encode("utf-8"))


def test_token_rate_data_passes_through_native_ttft(monkeypatch, tmp_path):
    """stats 返回 ttft_ms>0 → 透传到 status 展示，不发 served 假请求。"""
    import os
    monkeypatch.setenv("USAGE_PORT", "39999")
    monkeypatch.delenv("USAGE_BENCH_FALLBACK", raising=False)

    fake_data = {
        "isValid": True,
        "prompt_rate": 12.0,
        "predicted_rate": 15.0,
        "ttft_ms": 123,
        "ttft_ms_p95": 240,
        "rate_source": "native",
    }

    with patch("urllib.request.urlopen", return_value=_fake_urlopen_response(fake_data)) as m_urlopen, \
         patch("modelctl.cli.get_adapter") as m_ga, \
         patch("modelctl.cli._benchmark_token_rate") as m_bench:
        m_ga.return_value = MagicMock()
        m_bench.return_value = (999.9, 999.9, 999)  # 不应被调用
        from modelctl.cli import _token_rate_data
        result = _token_rate_data(MagicMock(name="profile", name="q"), MagicMock(name="caps"))

    assert result["source"] == "stats"
    assert result["prompt_rate"] == 12.0
    assert result["predicted_rate"] == 15.0
    assert result["ttft_ms"] == 123  # ≠ None
    m_bench.assert_not_called()
    m_urlopen.call_count == 1  # 只查 stats，不发假请求


def test_token_rate_data_bench_fallback_false_skips_bench_call(monkeypatch):
    """开关假+stats 0 → 不发 served 请求（rate 全 None）。"""
    import os
    monkeypatch.setenv("USAGE_PORT", "39999")
    monkeypatch.setenv("USAGE_BENCH_FALLBACK", "false")

    fake_data = {
        "isValid": True,
        "prompt_rate": 0.0, "predicted_rate": 0.0,
        "ttft_ms": 0.0, "rate_source": "none",
    }

    with patch("urllib.request.urlopen", return_value=_fake_urlopen_response(fake_data)), \
         patch("modelctl.cli.get_adapter") as m_ga, \
         patch("modelctl.cli._benchmark_token_rate") as m_bench:
        m_ga.return_value = MagicMock()
        m_bench.return_value = (10.0, 20.0, 100)  # 不应被调用
        from modelctl.cli import _token_rate_data
        result = _token_rate_data(MagicMock(name="profile", name="q"), MagicMock(name="caps"))

    assert result["source"] is None
    assert result["prompt_rate"] is None
    m_bench.assert_not_called()


def test_token_rate_data_default_bench_when_stats_zero(monkeypatch):
    """开关未设（保持现状 default True）+ stats 0 → 仍会走 served 假请求（向后兼容）。"""
    import os
    monkeypatch.setenv("USAGE_PORT", "39999")
    monkeypatch.delenv("USAGE_BENCH_FALLBACK", raising=False)

    fake_data = {"isValid": True, "prompt_rate": 0.0, "predicted_rate": 0.0,
                 "ttft_ms": 0.0, "rate_source": "none"}

    with patch("urllib.request.urlopen", return_value=_fake_urlopen_response(fake_data)), \
         patch("modelctl.cli.get_adapter") as m_ga, \
         patch("modelctl.cli._benchmark_token_rate", return_value=(9.9, 11.1, 88)) as m_bench:
        m_ga.return_value = MagicMock()
        from modelctl.cli import _token_rate_data
        result = _token_rate_data(MagicMock(name="profile", name="q"), MagicMock(name="caps"))

    assert result["source"] == "bench"
    assert result["prompt_rate"] == 9.9
    assert result["ttft_ms"] == 88
    m_bench.assert_called_once()
```

- [ ] **Step 2: 运行并验证失败**

```bash
uv run pytest tests/test_cli_native_ttft.py -v
```
Expected: **FAIL**（现在 `_token_rate_data` 不读 `ttft_ms`；不 gate `USAGE_BENCH_FALLBACK`）

- [ ] **Step 3: 写最小实现**

编辑 `src/modelctl/cli.py`，**整体替换** `_token_rate_data`（现 行 387-422）：

```python
def _token_rate_data(profile, caps) -> dict:
    """Token 速率 data：stats 优先（含 vLLM per-request native）；stats 无效且 usage bench
    兜底允许时才主动测。返回 {"prompt_rate","predicted_rate","ttft_ms","source"}。

    - stats 有效 ttft_ms>0 时原生透传（不发给模型端口）
    - 开关 USAGE_BENCH_FALLBACK=false 时**不发** served 假请求——全 0 返回（rate=None）
    - 开关未设时默认 True 保持现状：stats 0 时照老逻辑测
    """
    port = int(os.environ.get("USAGE_PORT", "5002"))
    url = f"http://127.0.0.1:{port}/api/usage?model={profile.name}"
    data: dict = {}
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError, json.JSONDecodeError):
        data = {}

    # 场景 1：stats 有效且速率非 0——优先透传含 native ttft
    if isinstance(data, dict) and data.get("isValid"):
        prompt_rate = data.get("prompt_rate")
        predicted_rate = data.get("predicted_rate")
        if isinstance(prompt_rate, (int, float)) and isinstance(predicted_rate, (int, float)) \
                and (prompt_rate > 0 or predicted_rate > 0):
            native_ttft = data.get("ttft_ms")
            ttft_out = int(native_ttft) if isinstance(native_ttft, (int, float)) and native_ttft > 0 else None
            return {
                "prompt_rate": float(prompt_rate),
                "predicted_rate": float(predicted_rate),
                "ttft_ms": ttft_out,
                "source": "stats",
            }

    # 场景 2：stats 无效/速率 0 → 主动测速（受 USAGE_BENCH_FALLBACK 控制）
    if _env_bool_default_true("USAGE_BENCH_FALLBACK") is False:
        return {"prompt_rate": None, "predicted_rate": None, "ttft_ms": None, "source": None}
    try:
        adapter = get_adapter(profile.engine)(profile, caps)
    except Exception:  # noqa: BLE001 — 构造 adapter 失败不阻断
        adapter = None
    try:
        result = _benchmark_token_rate(adapter)
    except Exception:  # noqa: BLE001 — 测速失败不阻断 status
        result = None
    if result is None:
        return {"prompt_rate": None, "predicted_rate": None, "ttft_ms": None, "source": None}
    prompt_rate, predicted_rate, ttft_ms = result
    return {"prompt_rate": prompt_rate, "predicted_rate": predicted_rate,
            "ttft_ms": ttft_ms, "source": "bench"}


def _env_bool_default_true(name: str) -> bool:
    """env bool：已存在时按 "1"/"true"/"yes"/"on" 判定；未设时默认 True（保持现状）。

    与 stats._parse_env_bool 一致但需避免 cli 反向依赖 core.stats 内部函数
    （cli 层轻量，实现内联一个）。
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return True
    low = raw.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    return True
```

- [ ] **Step 4: 运行并验证通过**

```bash
uv run pytest tests/test_cli_native_ttft.py -v
```
Expected: **PASS**（3 个用例）

- [ ] **Step 5: 回归**

```bash
uv run pytest tests/ -q
```
Expected: **全绿**（关键：`tests/test_modelctl.py` 中 `_token_rate_data` 现有断言——若已存在 `test_token_rate_stats_preferred` 类 test 保持通过；rate 全 0 时 `source=None` 的语义要与现有 `list`/`status` 的 `-` 显示对齐，无新行为）

---

### Task 9: `.env.example` 追加 `USAGE_BENCH_FALLBACK`

**Files:**
- Modify: `.env.example`（USAGE 段末尾、`USAGE_DATA_DIR` 之后）
- Test: 无（config-only，无单测；下面的 diff 由 user 运行 e2e 冒烟回归）

**Interfaces:**
- Consumes: 无
- Produces: `.env.example` 新行 `USAGE_BENCH_FALLBACK=true`；docs 更新说明

- [ ] **Step 1: 现有 `.env.example` 内 USAGE 段确定插入位**

编辑 `.env.example`，在现有 `USAGE_DATA_DIR=/raid5/sh/code/modelctl/data/usage-data`（行 54）**之后**追加：

```bash
# per-request 兜底开关：true=保留"全 0 时主动伪造短请求 benchmark"（默认）
# false=关闭兜底，要求 vLLM 开启 --enable-per-request-metrics + --enable-force-include-usage
USAGE_BENCH_FALLBACK=true
```

- [ ] **Step 2: 验证**

无需跑测试；直接 Read 文件确认插入位置正确（位于 USAGE 段内、下一段 `# ---------- 统一网关` 之前）。

- [ ] **Step 3: 提交说明**

改动留工作区（不 commit）。用户同步一份说明到 `README.md` / `docs/`——现有 `docs/usage-stats-*.md` 若有 USAGE 环境变量小节可同步。**本次不强制**，留 user 判断。

---

### Task 10: 全量回归 + 集成冒烟运行清单

**Files:**
- Test: 无新增代码（纯验收步骤）
- Verify: e2e 冒烟清单

**Interfaces:**
- Consumes: Task 1-9 全部产出
- Produces: 交付验收说明、e2e 清单（写入 spec 的 DoD 已覆盖）

- [ ] **Step 1: 全量 pytest**

```bash
uv run pytest tests/ -q
```
Expected: **全绿**。若有新增失败：先 diff 与 spec §5.3 对照确认是否为已知 fallback path。

- [ ] **Step 2: 集成冒烟——vLLM 本地起服务**

前置条件：`envs/vllm` 环境就绪、`models/vllm/qwen3.8.yaml` 已配 `enable_per_request_metrics: true` + `enable_force_include_usage: true`（见 spec 背景）。

```bash
# 起服务
modelctl start qwen3.8

# 一次真实请求打满 native 样本
curl -s http://127.0.0.1:5003/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8","messages":[{"role":"user","content":"hi"}],"stream":true}'

# 查 /api/usage
curl -s 'http://127.0.0.1:5002/api/usage?model=qwen3.8' | python -m json.tool
```

断言：
- `prompt_rate` / `predicted_rate` > 0
- `ttft_ms` > 0
- `rate_source == "native"`
- `extra` 串末尾含 "首 Token P50 = xxx ms"

- [ ] **Step 3: 开关关路径**

```bash
export USAGE_BENCH_FALLBACK=false
modelctl status qwen3.8   # 应看到 "首 Token 耗时" 行没有 "（实测）" 后缀
```

验证 vLLM access log 中**无** `"hi"` 字串的 served 假请求（对应 `collector_diff_prompt=1`）。

- [ ] **Step 4: 4 档回退层高帽有效**

先杀 qwen3.8（或把请求 import 全部 proxy 到网关以外直连 vLLM），`modelctl status qwen3.8`：
- 若 vLLM 未开 `--enable-per-request-metrics`：`rate_source` 应为 `engine_gauge` 或 `window_diff`（`avg_generation_throughput` gauge 有值时）
- 否则回退到 `bench`（若 `USAGE_BENCH_FALLBACK=true`）

- [ ] **Step 5: 非 vLLM 引擎不受影响代理现有路径**

```bash
modelctl start qwen3.5-light    # ollama / llamacpp 成员
modelctl status qwen3.5-light
```
断言：
- `rate_source` **不出现** `"native"` 或 `"ttft_ms"` 字段（非 vLLM 引擎）
- 计算口径与本次改造前行为一致（`prompt_rate>0` 时 source=`engine_gauge` 或 `window_diff`）

- [ ] **Step 6: 数据最终交付面**

`git diff --stat` 确认改动范围与 Global Constraints 一致（**不引入新第三方依赖**）。

```
src/modelctl/engines/base.py
src/modelctl/engines/vllm.py
src/modelctl/core/stats.py
src/modelctl/core/gateway.py
src/modelctl/cli.py
.env.example
tests/test_engine_native_metrics.py
tests/test_stats_native.py
tests/test_gateway.py
tests/test_cli_native_ttft.py
```

共 10 个文件（1 个 .env 配置 + 4 个源码 + 4 个测试 + 1 e2e）。

- [ ] **Step 7: 提交说明**

改动留工作区。用户统一 commit 时建议信息：

```
feat(stats): 对齐 vLLM 原生 per-request metrics + USAGE_BENCH_FALLBACK 开关

- 网关 SSE / 非流式响应把 vLLM 原生 metrics 喂给 stats collector
- /api/usage 追加 ttft_ms / ttft_ms_p95 / rate_source 三字段
- vLLM adapter 实现 native_metrics_mapping；非 vLLM 默认 None 保持零变化
- USAGE_BENCH_FALLBACK=false 时全 0 不再发起 served 假请求
- STATUS 优先透传 stats 的原生 ttft（避免硬测速）
```