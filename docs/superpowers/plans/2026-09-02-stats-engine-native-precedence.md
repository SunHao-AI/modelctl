# stats 引擎内置接口优先 + 兜底最后 + 60s 轮询 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 stats 模块的 token 指标（速率/首 Token 耗时）严格遵循"引擎内置接口优先、stats 兜底最后"：vLLM 补齐 `/metrics` TTFT 直方图均值（`sum/count`）采集，llamacpp 等无内置 TTFT 接口的引擎可按字段缺口触发假请求兜底（受新开关 `USAGE_BENCH_TTFT_ONLY` 控制），并把轮询默认间隔降到 60s。

**Architecture:** `parse_metrics` 新增可选 `ttft_ms` 键，裸 gauge 未命中时对 key 为 `ttft_ms` 的候选名走新增的 `_hist_mean`（`name_sum/name_count`）；`_poll_once` 把 gauge ttft 写入快照，`snapshot()` 取消对 `ttft_ms` 的无条件覆写（native 优先、gauge 补齐）；`_build_target_payload` 的 bench gate 由"三字段全 0"改为"按字段缺口 + `bench_ttft_only` 开关"；`run_server` 与 `.env.example` 轮询默认值 5/10 → 60。

**Tech Stack:** Python 3.12、纯标准库（`re` / `urllib`）、pytest（monkeypatch）

## Global Constraints

- 主依赖零新增：只用标准库（T7 已确认 `re` 等均在用）。
- 遵循现有代码风格：文件头已有 `@File/@Author` 块，中文注释、行内注释说明"为什么"。
- **现有测试基本全保持通过**。已知例外：`tests/test_stats.py::test_build_target_payload_no_bench_when_native_rate_present` 与 `tests/test_stats_native.py::test_snapshot_native_empty_leaves_base_values`，需在 T5/T6 按新 gate 语义更新（详见对应 Task）。
- TDD：每个 Task 先写失败测试再实现；一个失败测试对应一个最小实现。
- `rate_source` 取值集合冻结为 `{native, engine_gauge, window_diff, bench, none}`，不新增复合值。
- `/api/usage` 输出字段集合不变（cc-switch 零改动）。
- `USAGE_BENCH_FALLBACK` 永远优先于 `USAGE_BENCH_TTFT_ONLY`（兜底总开关）。
- PowerShell 环境：命令分隔用 `;`，不要用 `&&`。

## File Map

| 文件 | 责任 | 改动幅度 |
|---|---|---|
| `src/modelctl/core/stats.py` | 指标解析、采集循环、payload 组装 | T1/T2/T3/T4/T5/T7 主战场 |
| `src/modelctl/engines/vllm.py` | vLLM 引擎声明 | T2 加一键 |
| `.env.example` | 部署配置样例 | T7 改行 + 新增行 |
| `tests/test_stats.py` | stats 主测试 | T1/T3/T4/T5/T7 |
| `tests/test_stats_native.py` | native 滑窗测试（T3 改 fixture、T5 更新一处） | 小 |
| `tests/test_engine_native_metrics.py` | 引擎声明测试 | T2 加一个用例 |

---

### Task 1: `_hist_mean` + `parse_metrics` 支持 `ttft_ms` 键

**Files:**
- Modify: `src/modelctl/core/stats.py:183-200`（`parse_metrics` 及其前新增 `_hist_mean`）
- Test: `tests/test_stats.py`（顶部 import 追加 `_hist_mean`；文件末尾追加用例）

**Interfaces:**
- Produces: `def _hist_mean(text: str, name: str) -> float`；`parse_metrics(text, mapping) -> dict` 返回值恒含 `"ttft_ms": float`（默认 0.0，仅当 mapping 声明 `ttft_ms` 键且命中时非 0）。

- [ ] **Step 1: 写失败测试**

`tests/test_stats.py` 顶部 import 行改为：

```python
from modelctl.core.stats import _hist_mean, build_usage_payload, parse_metrics
```

文件末尾追加：

```python
class _TF:
    def __init__(self, **kw):
        d = {
            1: dict(
                prompt_total=["llamacpp:prompt_tokens_total"],
                predicted_total=["llamacpp:tokens_predicted_total"],
                prompt_rate=["llamacpp:prompt_tokens_seconds"],
                predicted_rate=["llamacpp:predicted_tokens_seconds"],
            )
        }
        d.update(kw)
        return d


TEXT_HIST = (
    "vllm:time_to_first_token_seconds_sum 2.5\n"
    "vllm:time_to_first_token_seconds_count 10\n"
    "vllm:time_to_first_token_seconds_bucket{le=\"0.1\"} 0\n"
    "vllm:time_to_first_token_seconds_bucket{le=\"+Inf\"} 10\n"
)


def test_hist_mean_divides_sum_by_count():
    assert _hist_mean(TEXT_HIST, "vllm:time_to_first_token_seconds") == 0.25


def test_hist_mean_zero_count_returns_zero():
    text = "m_sum 1.0\nm_count 0\n"
    assert _hist_mean(text, "m") == 0.0


def test_hist_mean_missing_sum_returns_zero():
    text = "m_count 4\nm_bucket{le=\"+Inf\"} 4\n"
    assert _hist_mean(text, "m") == 0.0


def test_parse_metrics_ttft_from_histogram_when_no_bare_gauge():
    got = parse_metrics(TEXT_HIST, _TF(ttft_ms=["vllm:time_to_first_token_seconds"]))
    assert got["ttft_ms"] == 0.25


def test_parse_metrics_ttft_prefers_bare_gauge():
    text = "vllm:time_to_first_token_seconds 0.3\n" + TEXT_HIST
    got = parse_metrics(text, _TF(ttft_ms=["vllm:time_to_first_token_seconds"]))
    assert got["ttft_ms"] == 0.3


def test_parse_metrics_without_ttft_key_defaults_zero():
    got = parse_metrics(TEXT_HIST, _TF())
    assert got["ttft_ms"] == 0.0
    # 现有四键不受 ttft_ms 逻辑影响
    assert got["prompt_rate"] == 0.0
    assert got["predicted_rate"] == 0.0
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_stats.py -q`

Expected: 6 个新用例 FAIL（`ImportError: cannot import name '_hist_mean'`，其余 `KeyError: 'ttft_ms'`）。

- [ ] **Step 3: 最小实现**

`src/modelctl/core/stats.py`，在 `def parse_metrics(...)` 之前（`_build_patterns` 之后）新增：

```python
def _hist_mean(text: str, name: str) -> float:
    """Prometheus 直方图均值：name_sum / name_count。

    任一缺失或 count <= 0 返回 0.0。用于 vLLM time_to_first_token_seconds
    这类只有 Histogram、没有现成均值 gauge 的引擎内置指标。
    """
    sum_m = re.match(rf"^{re.escape(name)}_sum\s+([-+0-9.eE]+)\s*$", text, re.MULTILINE)
    cnt_m = re.match(rf"^{re.escape(name)}_count\s+([-+0-9.eE]+)\s*$", text, re.MULTILINE)
    if not sum_m or not cnt_m:
        return 0.0
    try:
        cnt = float(cnt_m.group(1))
    except ValueError:
        return 0.0
    if cnt <= 0:
        return 0.0
    return float(sum_m.group(1)) / cnt
```

`parse_metrics` 整体替换为：

```python
def parse_metrics(text: str, mapping: dict[str, list[str]]) -> dict[str, float]:
    """解析 Prometheus 文本，返回指标名映射对应的数值。

    已知四个键（prompt_total / predicted_total / prompt_rate / predicted_rate）
    取 gauge 裸值（候选名第一个命中）。可选键 ttft_ms 额外支持直方图：
    候选名裸名未命中时用 <name>_sum / <name>_count 相除得均值（引擎内置 TTFT 直方图，
    如 vllm:time_to_first_token_seconds）。未声明 ttft_ms 键恒得 0.0。
    """
    result = {
        "prompt_total": 0.0,
        "predicted_total": 0.0,
        "prompt_rate": 0.0,
        "predicted_rate": 0.0,
        "ttft_ms": 0.0,
    }
    patterns = _build_patterns({k: v for k, v in mapping.items() if k in result})
    for key, key_patterns in patterns.items():
        for pattern in key_patterns:
            m = pattern.search(text)
            if m:
                try:
                    result[key] = float(m.group(1))
                except ValueError:
                    pass
                break
    if "ttft_ms" in mapping and result["ttft_ms"] == 0.0:
        for name in mapping["ttft_ms"]:
            val = _hist_mean(text, name)
            if val:
                result["ttft_ms"] = val
                break
    return result
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_stats.py -q`

Expected: 全部 PASS（含 6 个新用例 + 现有用例）。

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/stats.py tests/test_stats.py
git commit -m "feat(stats): parse_ttft_histogram_mean_and_metrics_key"
```

---

### Task 2: vLLM 声明 TTFT 直方图

**Files:**
- Modify: `src/modelctl/engines/vllm.py:229-243`
- Test: `tests/test_engine_native_metrics.py`（末尾追加）

**Interfaces:**
- Consumes: Task 1 的 `parse_metrics`（`ttft_ms` 键）
- Produces: `VllmAdapter.metrics_mapping()` 返回值含 `"ttft_ms": ["vllm:time_to_first_token_seconds"]`

- [ ] **Step 1: 写失败测试**

`tests/test_engine_native_metrics.py` 末尾追加：

```python
def test_vllm_metrics_mapping_declares_ttft_histogram():
    profile = _make_vllm_profile({})
    adapter = VllmAdapter(profile, Capabilities())
    assert adapter.metrics_mapping()["ttft_ms"] == ["vllm:time_to_first_token_seconds"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_engine_native_metrics.py -q`

Expected: 新用例 FAIL（`KeyError: 'ttft_ms'`）。

- [ ] **Step 3: 最小实现**

`src/modelctl/engines/vllm.py` 的 `metrics_mapping` 返回 dict 追加：

```python
    def metrics_mapping(self) -> dict[str, list[str]]:
        return {
            "prompt_total": ["vllm:prompt_tokens_total"],
            "predicted_total": ["vllm:generation_tokens_total"],
            # 实时速率 gauge：vLLM 自带（内部滑动窗口），客户端直连模型端口（绕过网关）时
            # 也能统计到真实吞吐；缺失/为 0 时 stats 退化为窗口差分
            "prompt_rate": [
                "vllm:prompt_tokens_seconds",
                "vllm:avg_prompt_throughput_toks_per_sec",
            ],
            "predicted_rate": [
                "vllm:generation_tokens_seconds",
                "vllm:avg_generation_throughput_toks_per_sec",
            ],
            # 首 Token 耗时：Histogram，无现成均值 gauge；stats.parse_metrics 以 sum/count 取均值
            "ttft_ms": ["vllm:time_to_first_token_seconds"],
        }
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_engine_native_metrics.py tests/test_stats.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/engines/vllm.py tests/test_engine_native_metrics.py
git commit -m "feat(vllm): declare ttft_histogram_in_metrics_mapping"
```

---

### Task 3: `_poll_once` 写入 gauge TTFT + `snapshot()` 取消无条件覆写

**Files:**
- Modify: `src/modelctl/core/stats.py:590-600`（_poll_once 快照构造）、`src/modelctl/core/stats.py:605-625`（snapshot）
- Test: `tests/test_stats.py`（末尾追加）；`tests/test_stats_native.py:20-34`（`_make_collector` fixture 加 `bench_ttft_only` 透传，防 T4 改签名后崩）

**Interfaces:**
- Consumes: Task 1 的 `parse_metrics` 返回 `ttft_ms`
- Produces: `UsageCollector._snapshot["ttft_ms"]` 可反映 gauge 值；`snapshot()["ttft_ms"] = native or gauge`（P95 仍只来自 native）

- [ ] **Step 1: 写失败测试**

`tests/test_stats.py` 末尾追加：

```python
def test_snapshot_keeps_gauge_ttft_when_native_window_empty(monkeypatch):
    """gauge 提供 ttft 而 native 滑窗为空时，snapshot 应保留 gauge 值（不再被归零）。"""
    import urllib.request

    from modelctl.core import stats as stats_mod
    from modelctl.core.stats import UsageCollector

    text = (
        "vllm:prompt_tokens_total 10\n"
        "vllm:generation_tokens_total 20\n"
        "vllm:prompt_tokens_seconds 0.0\n"
        "vllm:predicted_tokens_seconds 0.0\n"
        "vllm:time_to_first_token_seconds_sum 2.5\n"
        "vllm:time_to_first_token_seconds_count 10\n"
    )

    class _Resp:
        def read(self):
            return text.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: _Resp())
    mapping = {
        "prompt_total": ["vllm:prompt_tokens_total"],
        "predicted_total": ["vllm:generation_tokens_total"],
        "prompt_rate": ["vllm:prompt_tokens_seconds"],
        "predicted_rate": ["vllm:predicted_tokens_seconds"],
        "ttft_ms": ["vllm:time_to_first_token_seconds"],
    }
    c = UsageCollector(
        name="t",
        base_url="http://127.0.0.1:8000",
        poll_interval=999,
        api_key=None,
        data_dir=Path("data/cache"),
        mapping=mapping,
    )
    c._poll_once()
    snap = c.snapshot()
    assert snap["ttft_ms"] == 0.25
    assert snap["ttft_ms_p95"] == 0.0
```

`tests/test_stats.py` 顶部确认已 `from pathlib import Path`；若无则追加。

`tests/test_stats_native.py` 的 `_make_collector` 改为透传（现在加，避免 T4 引入新入参后崩）：

```python
def _make_collector(tmp_path: Path, **kw) -> "UsageCollector":
    """构造 on-demand 模式 UsageCollector（poll_interval=999 不轮询）。"""
    from modelctl.core.stats import UsageCollector
    collector = UsageCollector(
        name="t",
        base_url="http://1.1.1.1",
        poll_interval=999,
        api_key=None,
        data_dir=tmp_path,
        mode="on-demand",
        mapping=kw.pop("mapping", {}),
        native_mapping=kw.pop("native_mapping", None),
        bench_fallback=kw.pop("bench_fallback", True),
        bench_ttft_only=kw.pop("bench_ttft_only", True),
    )
    return collector
```

> 注意：此刻 `UsageCollector.__init__` 尚无 `bench_ttft_only` 形参，test_stats_native.py 会在 T4 实现后才全绿；T3 的 Step 4 只跑 `tests/test_stats.py`。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_stats.py -q`

Expected: `test_snapshot_keeps_gauge_ttft_when_native_window_empty` FAIL（现 `snapshot()["ttft_ms"] == 0.0`，因为 `_poll_once` 写死 0.0 且 `snapshot` 无条件覆写）。

- [ ] **Step 3: 最小实现**

`src/modelctl/core/stats.py` `_poll_once` 内（约 590-600）快照构造：

```python
        with self._lock:
            self._snapshot = {
                "ok": True,
                "error": None,
                "prompt_total": new_prompt,
                "predicted_total": new_predicted,
                "prompt_rate": metrics["prompt_rate"],
                "predicted_rate": metrics["predicted_rate"],
                "ttft_ms": metrics.get("ttft_ms", 0.0),  # gauge 档 TTFT（直方图 sum/count）
                "ttft_ms_p95": 0.0,                       # P95 仅来自 native 滑窗（gauge 层无分位）
                "rate_source": source,
            }
```

同上函数内 `rate_source` 的 `source` 计算 **不改**（保持只判速率来源，避免把混合场景误标 `engine_gauge`）。

`snapshot()` 内（约 605-625）替换两行：

```python
        native_row = self._compute_native_row()
        base["prompt_rate"] = native_row["prompt_rate"] or base.get("prompt_rate") or 0.0
        base["predicted_rate"] = native_row["predicted_rate"] or base.get("predicted_rate") or 0.0
        # ttft 档 native > gauge：滑窗为空不再把 gauge 的 TTFT 归零
        base["ttft_ms"] = native_row["ttft_ms"] or base.get("ttft_ms") or 0.0
        base["ttft_ms_p95"] = native_row["ttft_ms_p95"]
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_stats.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/stats.py tests/test_stats.py tests/test_stats_native.py
git commit -m "feat(stats): gauge_ttft_to_snapshot_and_native_first"
```

---

### Task 4: `UsageCollector.__init__` 新增 `bench_ttft_only`

**Files:**
- Modify: `src/modelctl/core/stats.py:335-387`（`__init__`）
- Test: `tests/test_stats.py`（末尾追加）

**Interfaces:**
- Produces: `UsageCollector.__init__(..., bench_fallback: bool = True, bench_ttft_only: bool = True)`；属性 `self.bench_ttft_only: bool`（env `USAGE_BENCH_TTFT_ONLY` 优先）

- [ ] **Step 1: 写失败测试**

`tests/test_stats.py` 末尾追加：

```python
def test_bench_ttft_only_env_read_default_true(monkeypatch, tmp_path):
    from modelctl.core.stats import UsageCollector

    monkeypatch.delenv("USAGE_BENCH_TTFT_ONLY", raising=False)
    c = UsageCollector(name="t", base_url="http://1.1.1.1", poll_interval=999,
                       api_key=None, data_dir=tmp_path, mode="on-demand",
                       mapping={"prompt_total": ["m"]})
    assert c.bench_ttft_only is True


def test_bench_ttft_only_env_read_false(monkeypatch, tmp_path):
    from modelctl.core.stats import UsageCollector

    monkeypatch.setenv("USAGE_BENCH_TTFT_ONLY", "false")
    c = UsageCollector(name="t", base_url="http://1.1.1.1", poll_interval=999,
                       api_key=None, data_dir=tmp_path, mode="on-demand",
                       mapping={"prompt_total": ["m"]}, bench_ttft_only=True)
    assert c.bench_ttft_only is False  # env 优先于构造入参
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_stats.py -q`

Expected: 两用例 FAIL（`TypeError: __init__() got an unexpected keyword argument 'bench_ttft_only'` 或 `AttributeError`）。

- [ ] **Step 3: 最小实现**

`src/modelctl/core/stats.py` `__init__` 签名追加形参：

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
        bench_ttft_only: bool = True,
    ) -> None:
```

`bench_fallback` 读取块（约 355-358）之后追加同构块：

```python
        if "USAGE_BENCH_TTFT_ONLY" in os.environ:
            self.bench_ttft_only = _parse_env_bool(os.environ["USAGE_BENCH_TTFT_ONLY"])
        else:
            self.bench_ttft_only = bench_ttft_only
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_stats.py tests/test_stats_native.py -q`

Expected: 全部 PASS（native 的 `_make_collector` 已在 T3 透传 `bench_ttft_only`，此刻起生效）。

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/stats.py tests/test_stats.py
git commit -m "feat(stats): add_bench_ttft_only_switch"
```

---

### Task 5: `_build_target_payload` gate 改按字段缺口

**Files:**
- Modify: `src/modelctl/core/stats.py:679-723`（`_build_target_payload`）
- Test: `tests/test_stats.py`（末尾追加 2 个新用例 + 更新 1 个既有用例）

**Interfaces:**
- Consumes: Task 1–4 全部（`parse_metrics` ttft、snapshot gauge ttft、`bench_ttft_only` 属性）
- Produces: bench 触发条件 = `bench_fallback_enabled AND missing_any AND (not rates_ok OR bench_ttft_only)`；回填只覆盖为 0 字段；`rate_source` 只要 bench 回填过且开关打开就标 `bench`

- [ ] **Step 1: 写失败测试**

`tests/test_stats.py` 末尾追加：

```python
def _mk_target(name: str) -> "StatsTarget":
    from modelctl.core.stats import StatsTarget

    return StatsTarget(
        name=name,
        data_dir=None,
        metrics_url=f"http://127.0.0.1:8000/{name}",
        mapping={"prompt_total": ["m"], "predicted_total": ["m"]},
        usage_cfg={"price_in": 1.0, "price_out": 2.0},
        bench_url=f"http://127.0.0.1:8000/{name}/v1/chat/completions",
    )


def _run_payload(collector_snap: dict, bench_ret, cfg: dict):
    """统一 runner：Fake collector + _bench_cached 注入 + 调用 _build_target_payload。"""
    import time
    from unittest.mock import MagicMock
    from modelctl.core import stats as stats_mod
    from modelctl.core.stats import UsageHandler

    name = cfg["name"]
    target = _mk_target(name)
    FakeCollector = MagicMock()
    FakeCollector.get_snapshot.return_value = collector_snap
    FakeCollector.bench_fallback = cfg.get("bench_fallback", True)
    FakeCollector.bench_ttft_only = cfg.get("bench_ttft_only", True)
    UsageHandler.targets = [target]
    UsageHandler.collectors = {name: FakeCollector}
    UsageHandler.start_time = time.time()
    cache = stats_mod._BENCH_CACHE
    cache.pop(name, None)
    try:
        calls = []

        def cb(t):
            calls.append(t)
            return bench_ret

        stats_mod._bench_cached = cb
        payload = UsageHandler._build_target_payload(UsageHandler, target)
        return {"payload": payload, "calls": calls}
    finally:
        del stats_mod._bench_cached


def test_build_target_payload_bench_only_fills_ttft_when_rates_from_gauge(monkeypatch):
    """gauge 已给速率但 ttft_ms=0：bench 触发却只补 ttft，速率保持 gauge 值。"""
    from modelctl.core import stats as stats_mod

    capture = {}

    def fake_cb(tgt):
        capture["called"] = True
        return (999.0, 999.0, 150)  # 速率字段不应被 999 覆盖

    monkeypatch.setattr(stats_mod, "_bench_cached", fake_cb)
    snap = {
        "ok": True, "error": None,
        "prompt_total": 100.0, "predicted_total": 200.0,
        "prompt_rate": 100.0, "predicted_rate": 50.0,   # gauge 已给双侧速率
        "ttft_ms": 0.0, "ttft_ms_p95": 0.0,
        "rate_source": "engine_gauge",
    }
    r = _run_payload(snap, (999.0, 999.0, 150), {"name": "r1", "bench_ttft_only": True})
    assert capture["called"] is True
    assert r["payload"]["ttft_ms"] == 150.0
    assert r["payload"]["prompt_rate"] == 100.0
    assert r["payload"]["predicted_rate"] == 50.0
    assert r["payload"]["rate_source"] == "bench"


def test_build_target_payload_skips_bench_when_only_ttft_missing_and_switch_off(monkeypatch):
    """同上但 bench_ttft_only=False：不缺速率也不缺→满足"全 0 才 bench"，ttft 缺≠全 0 → 不 bench。"""
    from modelctl.core import stats as stats_mod

    def _should_not_call(t):
        raise AssertionError("should not call bench")

    monkeypatch.setattr(stats_mod, "_bench_cached", _should_not_call)
    snap = {
        "ok": True, "error": None,
        "prompt_total": 100.0, "predicted_total": 200.0,
        "prompt_rate": 100.0, "predicted_rate": 50.0,
        "ttft_ms": 0.0, "ttft_ms_p95": 0.0,
        "rate_source": "engine_gauge",
    }
    r = _run_payload(snap, (999.0, 999.0, 150),
                     {"name": "r2", "bench_ttft_only": False})
    assert r["payload"]["ttft_ms"] is None  # 不回填 → payload 不透传 ttft
    assert r["payload"]["prompt_rate"] == 100.0
```

同时**修改既有用例** `test_build_target_payload_no_bench_when_native_rate_present`（约 396 行），因为新 gate 下"单侧速率为 0"现在**允许** bench。原用例断言"不 bench"与新 gate 不符。改为双侧速率都为 0 才能保留原语义（验证"全 0 + fallback=True 时 benchmark 触发"），而"单侧非 0 时不 bench"由新两条用例覆盖：

把 `mock_collector.get_snapshot.return_value` 的 `prompt_rate` / `predicted_rate` 都设 0：

```python
    mock_collector.get_snapshot.return_value = {
        "ok": True,
        "error": None,
        "prompt_total": 100.0,
        "predicted_total": 50.0,
        "prompt_rate": 0.0,
        "predicted_rate": 0.0,
    }
    mock_collector.bench_fallback = False  # 改为总开关关闭，以与既有 docstring"任意 native 已有值→不 bench"语义一致
```

并在该用例断言里追加：

```python
    assert payload["prompt_rate"] == 0.0
    assert payload["predicted_rate"] == 0.0
    assert "rate_source" not in payload
```

> 若既有用例已存在类似断言则现缺陷则微调，只保证最终**双语义**同时被覆盖：
> (1) `bench_fallback=False` 时永不 bench（旧语义，仍是该用例的新目标）；
> (2) `bench_ttft_only=False` 且速率有值时不 bench（新语义，见上面新用例 2）。

**追加修改 `tests/test_stats_native.py::test_build_target_payload_skips_bench_when_native_ttft_present`**（其目录在 `test_stats_native.py` 内，与 `test_stats.py` 分离但同属本 Task 修改面）：其 collector snapshot 为 `prompt_rate=0, predicted_rate=0, ttft_ms=123`，新 gate 下 `missing_any=True` 且 `not rates_ok=True` → 允许 bench，与既有用例"断言不调 bench"冲突。按新语义修正断言方向——bench 现在**应被调**，但 ttft_ms 已有 123（`stats.py:709-710` 只覆盖为 0 的字段），断言 `payload["ttft_ms"] == 123.0`（不被 bench 的 999 覆盖）即可：

```python
def test_build_target_payload_runs_bench_but_keeps_native_ttft(monkeypatch):
    """原 test_build_target_payload_skips_bench_when_native_ttft_present 在新 gate 下的等价改写。

    native 已给 ttft_ms=123（速率仍缺）→ 允许 bench 补速率；ttft_ms 不被 bench 的 999 覆盖。
    """
    import modelctl.core.stats as S
    from unittest.mock import MagicMock, Mock, patch
    fake_collector = MagicMock()
    fake_collector.bench_fallback = True
    fake_collector.bench_ttft_only = True
    fake_collector.get_snapshot.return_value = {
        "ok": True,
        "prompt_total": 100.0,
        "predicted_total": 200.0,
        "prompt_rate": 0.0,
        "predicted_rate": 0.0,
        "ttft_ms": 123.0,
        "ttft_ms_p95": 210.0,
        "rate_source": "native",
    }
    handler = Mock()
    handler.collectors = {"m1": fake_collector}
    handler.start_time = 0.0
    tgt = _make_handler_target("m1")
    try:
        with patch.object(S, "_bench_cached") as m_bench:
            m_bench.return_value = (999.0, 999.0, 150)
            payload = S.UsageHandler._build_target_payload(handler, tgt)
        m_bench.assert_called_once()
        assert payload.get("ttft_ms") == 123.0      # bench 调用不覆盖 native ttft
        assert payload.get("prompt_rate") == 999.0   # 速率字段回填
        assert payload.get("rate_source") == "bench"
    finally:
        handler.collectors = {}
        handler.targets = []
```

并删除旧函数体 `def test_build_target_payload_skips_bench_when_native_ttft_present():`（由新函数替代）。该用例改前后：
- 旧：`assert m_bench.assert_not_called()` + `assert payload["ttft_ms"] == 123.0`
- 新：`assert m_bench.assert_called_once()` + `assert payload["ttft_ms"] == 123.0`（核心意图"native ttft 不被覆写"保留）

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_stats.py -q`

Expected: `test_build_target_payload_bench_only_fills_ttft_when_rates_from_gauge` FAIL（现行门是 `native_has_any` → 不调 `_bench_cached`）；`test_build_target_payload_skips_bench_when_only_ttft_missing_and_switch_off` 现"意外 PASS"（因为现行 `native_has_any` 也跳过了它）——但该用例的真正断言点在 Task 本体实现完成后再复核；修正既有用例后 `test_build_target_payload_no_bench_when_native_rate_present` 期望变得更宽松，此时亦应失败直到新 gate 实现。

- [ ] **Step 3: 最小实现**

`src/modelctl/core/stats.py` `_build_target_payload`，替换原来的 `native_has_any` / `should_bench` 段：

```python
        prompt_r = tokens.get("prompt_rate") or 0
        predicted_r = tokens.get("predicted_rate") or 0
        ttft = tokens.get("ttft_ms") or 0
        missing_any = (prompt_r == 0 or predicted_r == 0 or ttft == 0)
        rates_ok = prompt_r > 0 and predicted_r > 0
        bench_fallback_enabled = getattr(collector, "bench_fallback", True) is True
        bench_ttft_only = getattr(collector, "bench_ttft_only", True) is True
        should_bench = (
            bench_fallback_enabled
            and missing_any
            and (not rates_ok or bench_ttft_only)
        )
        if should_bench:
            bench = _bench_cached(target)
            if bench is not None:
                if tokens.get("prompt_rate", 0.0) == 0:
                    tokens["prompt_rate"] = bench[0]
                if tokens.get("predicted_rate", 0.0) == 0:
                    tokens["predicted_rate"] = bench[1]
                if tokens.get("ttft_ms", 0.0) == 0:
                    tokens["ttft_ms"] = float(bench[2])
            tokens["rate_source"] = "bench"
```

**不变**的部分：下面 `payload = build_usage_payload(...)` 之后的透传段（`if tokens.get("ttft_ms") > 0: payload["ttft_ms"] = tokens["ttft_ms"]` 等）与原实现一致，别动。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_stats.py tests/test_stats_native.py -q`

Expected: 全部 PASS。若 `tests/test_stats_native.py::test_snapshot_native_empty_leaves_base_values` 因特意为 base `prompt_rate` 设成非 0 而 `bench_ttft_only` 默认 True 触发 bench，检查它的上游是否要 mock `_bench_cached` 为 no-op 或改 `bench_ttft_only=False`；**别因既有断言与修 bug 定向对立而静默改它**——把业务要求 dual 写入 spec 并在 plan 修订中修正，而不是抹掉既有测试。本 Task 修完后应立即跑 `uv run pytest tests/ -q` 全量回归。

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/stats.py tests/test_stats.py tests/test_stats_native.py
git commit -m "feat(stats): bench_gate_by_missing_field"
```

---

### Task 6: 更新 `tests/test_stats_native.py::test_snapshot_native_empty_leaves_base_values`

> 本 Task 只在 T5 全量回归发现它失败时启用；否则跳过并在 PR 描述中注明。

**Files:**
- Modify: `tests/test_stats_native.py`（该用例局部）

**Interfaces:**
- Consumes: T5 的 gate 语义

**Step 1:** 运行 `uv run pytest tests/test_stats_native.py::test_snapshot_native_empty_leaves_base_values -q`。
- 若 **PASS**：跳到 Step 3 直接 commit（即确认无需改）。
- 若 **FAIL**：继续 Step 2。

**Step 2:** 在该用例内，找到 `collector._snapshot` 初始化块（约 116-122 行），在往 `_snapshot` 赋值时给 collector 加一句让 bench 视角的字段全 0：

```python
    collector._snapshot = {
        ...(原有字段)...
        "prompt_rate": 0.0,
        "predicted_rate": 0.0,
    }
```

使得该用例仍然在断言"snapshot 不动 base 值"而不是"bench 触发"。若断言仍失败，把该用例的 `collector.record_tokens(...)` 参数改成 0（或删掉该行），因为其 core 语义在于 snapshot 幂等性而非 tokens 入账。

**Step 3:** Commit（如改动）

```bash
git add tests/test_stats_native.py
git commit -m "test(native): keep_snapshot_empty_test_valid_under_new_bench_gate"
```

---

### Task 7: 轮询默认值改为 60 + `.env.example` + `run_server` 注入新开关

**Files:**
- Modify: `src/modelctl/core/stats.py:831-870`（`run_server` 内部：`run_server` 的 docstring 默认值改成 60、`poll_interval = float(os.environ.get("USAGE_POLL_INTERVAL", "60"))`、`UsageCollector(...)` 调用追加 `bench_ttft_only=_parse_env_bool(os.environ.get("USAGE_BENCH_TTFT_ONLY"), True)`）
- Modify: `.env.example:51-58`
- Test: `tests/test_stats.py`（末尾追加）

**Interfaces:**
- Consumes: Task 4 的 `bench_ttft_only` 形参
- Produces: 无 env 时 `poll_interval == 60`；`UsageCollector.bench_ttft_only` 从 env 默认 True

- [ ] **Step 1: 写失败测试**

`tests/test_stats.py` 末尾追加：

```python
def test_run_server_poll_interval_default_sixty_and_pass_ttft_only(tmp_path, monkeypatch):
    """默认 60s，同时把 USAGE_BENCH_TTFT_ONLY 传进 collector（True 默认）。"""
    from modelctl.core import stats as stats_mod
    from modelctl.core.stats import StatsTarget

    captured: dict = {}

    class FakeCollector:
        def start(self): pass
        def stop(self): pass

    class FakeServer:
        def __init__(self, *a, **k): pass
        def serve_forever(self): raise KeyboardInterrupt
        def server_close(self): pass

    def fps(*args, **kwargs):
        captured["poll_interval"] = args[2]
        captured["bench_ttft_only"] = kwargs.get("bench_ttft_only")
        return FakeCollector()

    monkeypatch.setenv("USAGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("USAGE_POLL_INTERVAL", raising=False)
    monkeypatch.delenv("USAGE_BENCH_TTFT_ONLY", raising=False)
    monkeypatch.setattr(stats_mod, "UsageCollector", fps)
    monkeypatch.setattr(stats_mod, "ThreadingHTTPServer", FakeServer)

    target = StatsTarget(
        name="x", data_dir=tmp_path,
        metrics_url="http://127.0.0.1:18888/metrics",
        mapping={"prompt_total": ["m"]},
    )
    stats_mod.run_server(targets=[target])
    assert captured["poll_interval"] == 60
    assert captured["bench_ttft_only"] is True
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_stats.py::test_run_server_poll_interval_default_sixty_and_pass_ttft_only -q`

Expected: FAIL（现默认 5 且未透传 `bench_ttft_only`）。

- [ ] **Step 3: 最小实现**

`src/modelctl/core/stats.py` `run_server` 内：

```python
    poll_interval = float(os.environ.get("USAGE_POLL_INTERVAL", "60"))
    ...
    for target in targets:
        if target.mapping is not None:
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
                bench_ttft_only=_parse_env_bool(os.environ.get("USAGE_BENCH_TTFT_ONLY"), True),
            )
            collector.start()
            collectors[target.name] = collector
```

`run_server` 的 docstring 那行 `USAGE_POLL_INTERVAL（默认 5）` 改为 `（默认 60）`。

`.env.example` 修改：

```bash
# 轮询间隔（秒）；仅 poll 模式生效；默认 60 以降低引擎 /metrics 压力
USAGE_POLL_INTERVAL=60
```

紧随 `USAGE_BENCH_FALLBACK=true` 之后新增：

```bash
# 仅为补 TTFT 而测速：true=gauge 有速率但引擎无内置 TTFT 接口（如 llama.cpp）时，
# 仍按 USAGE_BENCH_FALLBACK 的节流发假请求补首 Token 耗时；false=仅速率与 TTFT 全 0 时才测速
USAGE_BENCH_TTFT_ONLY=true
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_stats.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/stats.py .env.example tests/test_stats.py
git commit -m "feat(stats): poll_default_60s_and_wire_bench_ttft_only_env"
```

---

### Task 8: 全量回归

**Files:**
- N/A（无新代码）

**Interfaces:**
- Consumes: T1–T7 全部

- [ ] **Step 1: 全量测试**

Run: `uv run pytest tests/ -q`

Expected: 全部 PASS（新增 12 用例 + 全部既有用例）。

- [ ] **Step 2: 若有失败**

- 若只有 `test_snapshot_native_empty_leaves_base_values` 失败：按 Task 6 修正后重跑。
- 若既有 `test_build_target_payload_*` 多处失败：逐个确认是"既有用例语义已被 T5 取代"还是"修 bug 的回归"，前者删/改并更新 spec §5.2 的"必须保持通过"清单；后者修复实现。

- [ ] **Step 3: Commit（无代码）/ 结束**

不新提交。plan 执行完毕，由用户决定是否触发 `subagent-driven-development` 或 `finishing-a-development-branch` 走集成/合流。

---

## Self-Review

**Spec coverage（§5.1 一一对应）：**
- 用例 1–3 → T1 Step 1 `test_hist_mean_*`
- 用例 4–6 → T1 Step 1 `test_parse_metrics_*`
- 用例 7 → T2 Step 1 `test_vllm_metrics_mapping_declares_ttft_histogram`
- 用例 8 → T3 Step 1 `test_snapshot_keeps_gauge_ttft_when_native_window_empty`
- 用例 9–10 → T5 Step 1 `test_build_target_payload_bench_only_fills_ttft_*` / `..._switch_off`
- 用例 11 → T4 Step 1 `test_bench_ttft_only_env_read_default_true/..._false`
- 用例 12 → T7 Step 1 `test_run_server_poll_interval_default_sixty_and_pass_ttft_only`
- §6 验收 1–2 → 集成冒烟（本 plan 之外，实现后单独跑）
- §6 验收 3–4 → T5 双用例 + `bench_fallback=False` 分支（`bench_fallback_enabled` 是 `should_bench` 的第一合取项，代码层面优先级已保）

**Placeholder scan:** 无 "TBD" / "TODO" / "implement later" / "appropriate error handling"。唯一特殊行是 T6 里"If PASS: 直接 commit"——这不是占位符，是条件分支步骤。

**Type consistency check:**
- `_hist_mean(text: str, name: str) -> float`（T1）在 T1 内部 `parse_metrics` 中调用，签名一致。
- `parse_metrics` 返回 dict 含 `"ttft_ms"`——T3 的 `_poll_once` 用 `metrics.get("ttft_ms", 0.0)`，兼容旧 fixture（无 ttft 键的 mapping 也 OK）。
- `UsageCollector.__init__(..., bench_ttft_only: bool = True)`（T4）与 T3 的 `_make_collector` fixture 透传一致。
- `UsageHandler._build_target_payload` 里 `getattr(collector, "bench_ttft_only", True)`——与 `MagicMock` 的旧 getter 行为一致（`MagicMock` 对 `.bench_ttft_only` 会返回 auto-mock 值，非 True）。这一点我在 T5 Step 1 的 `_run_payload` 里显式设 `FakeCollector.bench_ttft_only = cfg.get("bench_ttft_only", True)`，避免 auto-mock 值污染断言。
- 新 switch 触发门槛 `not rates_ok or bench_ttft_only`：当 `bench_fallback=False` 时，`bench_fallback_enabled=False`，整个 `and` 短路为 False，总开关优先 ✓。
- 优先级 spec §2 中"每字段独立按档取第一个非 0"与实现一致：`_poll_once` 先把 gauge 值写入 base、`snapshot()` 再按 native 覆盖、`_build_target_payload` 再按"仍为 0 才回填"补 bench，天然形成档 1→2→3→4 顺序。

**发现的不一致（已在 plan 中修复）：**
- `tests/test_stats.py::test_build_target_payload_no_bench_when_native_rate_present` 与 `tests/test_stats_native.py::test_build_target_payload_skips_bench_when_native_ttft_present` 在 T5 新 gate 下原"不调 bench"断言不再成立——均已按新语义改写（见 T5 Step 1 内两个代码块，分别修改 snapshot 并断言"ttft 不被 bench 覆盖"）。
- `tests/test_stats_native.py::test_snapshot_native_empty_leaves_base_values` 的 base `prompt_rate=20.0, predicted_rate=10.0` 在 T5 新 gate 下（`rates_ok=True`、`ttft_ms=0`、`bench_ttft_only=True`）会触发 bench。若 T5 全量回归失败，按 T6 修 base `prompt_rate`/`predicted_rate` 为 0（或把该 fixture 的 `bench_ttft_only` 设 False），使该用例继续断言"screenshot/幂等性"原意图。

## Execution Handoff

**Plan 已保存到 `docs/superpowers/plans/2026-09-02-stats-engine-native-precedence.md`，两个执行选项：**

**1. Subagent-Driven（推荐）** — 每 Task 派新 subagent，两阶 review，快速迭代。
**2. Inline Execution** — 用 `executing-plans` 在当前会话批量执行 + 检查点。

选哪一个？
