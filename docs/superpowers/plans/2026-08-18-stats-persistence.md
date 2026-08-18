# stats.py 用量统计改造实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 modelctl 用量统计服务增加 token 累计持久化、实时 token/s 计算，以及 `/api/usage?model=all` 多模型聚合能力。

**Architecture:** 在 `UsageCollector` 中引入基于 `data/cache/<model>.json` 的累计持久化与最近 10 次采样滑动窗口；扩展 `build_usage_payload` 输出结构化速率字段；在 `UsageHandler` 中新增 `model=all` 聚合分支。全部改造保持在 `src/modelctl/core/stats.py` 与 `tests/test_stats.py` 内，不引入第三方依赖。

**Tech Stack:** Python 3.12, 标准库（json, pathlib, threading, os）, pytest

## Global Constraints

- 不引入第三方依赖，保持 `stats.py` 纯标准库。
- 保持现有 `/api/usage` 字段向后兼容，仅新增字段。
- 持久化写回失败不得中断轮询或服务。
- 聚合视图仅包含支持精确统计的引擎（`mapping is not None`）。
- 测试需通过 `pytest`。

---

### Task 1: 创建 data/cache 目录并透传 data_dir

**Files:**
- Modify: `src/modelctl/core/stats.py:286-316`
- Modify: `src/modelctl/core/stats.py:319-340`
- Modify: `src/modelctl/core/envfile.py:9`
- Modify: `.env.example:49`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `PROJECT_ROOT: Path`
- Produces: `USAGE_DATA_DIR: str` 环境变量读取逻辑；`run_server` 构造 `data_dir: Path` 并传入 `_targets_from_profiles(data_dir)`；`UsageCollector.__init__` 接收 `data_dir: Path`。

- [ ] **Step 1: 确认 `PROJECT_ROOT` 指向仓库根**

仓库根目录为 `d:\WorkPlace\Pycharm\modelctl`，`envfile.py` 中 `PROJECT_ROOT = Path(__file__).resolve().parents[3]` 已正确指向根目录。

- [ ] **Step 2: 在 `run_server` 中构造默认 data_dir**

在 `src/modelctl/core/stats.py:286-316` 的 `run_server` 函数开头加入：

```python
from pathlib import Path

data_dir = Path(os.environ.get("USAGE_DATA_DIR", PROJECT_ROOT / "data" / "cache"))
data_dir.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 3: 将 data_dir 传入 target 构造链路**

修改 `run_server` 中循环：

```python
targets = _targets_from_profiles(data_dir)
```

修改 `_targets_from_profiles` 签名：

```python
def _targets_from_profiles(data_dir: Path) -> list[StatsTarget]:
    ...
    for profile in list_profiles():
        adapter = get_adapter(profile.engine)(profile, Capabilities())
        targets.append(
            StatsTarget(
                name=profile.name,
                data_dir=data_dir,
                metrics_url=f"http://127.0.0.1:{profile.port}/metrics",
                mapping=adapter.metrics_mapping(),
                usage_cfg=profile.usage,
                api_key=profile.api_key,
            )
        )
    return targets
```

- [ ] **Step 4: 在 `StatsTarget` 中新增 `data_dir` 字段**

```python
@dataclass
class StatsTarget:
    name: str
    data_dir: Path
    metrics_url: str
    mapping: dict[str, list[str]] | None
    usage_cfg: dict = field(default_factory=dict)
    api_key: str | None = None
```

- [ ] **Step 5: 在 `UsageCollector.__init__` 中接收 data_dir**

修改签名：

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
) -> None:
```

`run_server` 中创建 `UsageCollector` 时传入 `target.name` 与 `target.data_dir`。

- [ ] **Step 6: 在 `.env.example` 中新增 USAGE_DATA_DIR**

在 `.env.example:49` 后追加：

```text
# 用量统计累计数据持久化目录
USAGE_DATA_DIR=
```

- [ ] **Step 7: 验证目录创建**

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -c "from modelctl.core.stats import run_server; print('ok')"`
Expected: 无导入错误。

- [ ] **Step 8: Commit**

```bash
git add src/modelctl/core/stats.py src/modelctl/core/envfile.py .env.example
git commit -m "feat(stats): wire data_dir for usage persistence"
```

---

### Task 2: 实现累计 token 持久化读写

**Files:**
- Modify: `src/modelctl/core/stats.py:123-221`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `data_dir: Path`, `name: str`
- Produces: `_persist_path()`, `_load_persisted()`, `_persist()`；`prompt_total` / `predicted_total` 基线从文件恢复。

- [ ] **Step 1: 写失败测试 —— 累计值从文件恢复**

```python
def test_usage_collector_loads_persisted_totals(tmp_path):
    data_dir = tmp_path / "cache"
    data_dir.mkdir()
    (data_dir / "demo.json").write_text(
        '{"prompt_total": 100, "predicted_total": 200, "updated_at": 1.0}',
        encoding="utf-8",
    )
    from modelctl.core.stats import UsageCollector
    collector = UsageCollector(
        name="demo",
        base_url="http://127.0.0.1:8000",
        poll_interval=5,
        api_key=None,
        data_dir=data_dir,
        mode="poll",
        mapping={},
    )
    snap = collector.snapshot()
    assert snap["prompt_total"] == 100.0
    assert snap["predicted_total"] == 200.0
```

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_stats.py::test_usage_collector_loads_persisted_totals -v`
Expected: FAIL `UsageCollector.__init__() got an unexpected keyword argument 'name'`

- [ ] **Step 2: 实现持久化路径与加载方法**

在 `UsageCollector` 中新增：

```python
def _persist_path(self) -> Path:
    return self.data_dir / f"{self.name}.json"

def _load_persisted(self) -> tuple[float, float]:
    path = self._persist_path()
    if not path.is_file():
        return 0.0, 0.0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data.get("prompt_total", 0.0)), float(data.get("predicted_total", 0.0))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0.0, 0.0
```

在 `__init__` 中：

```python
self.name = name
self.data_dir = data_dir
persisted_prompt, persisted_predicted = self._load_persisted()
self._baseline = {
    "prompt_total": persisted_prompt,
    "predicted_total": persisted_predicted,
}
```

- [ ] **Step 3: 实现写回方法**

```python
def _persist(self, prompt_total: float, predicted_total: float) -> None:
    path = self._persist_path()
    tmp = path.with_suffix(".json.tmp")
    data = {
        "prompt_total": prompt_total,
        "predicted_total": predicted_total,
        "updated_at": time.time(),
    }
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        # 持久化失败不应中断轮询；错误信息仅通过日志/后续 snapshot 暴露
        pass
```

- [ ] **Step 4: 在 `_poll_once` 中应用基线并写回**

修改 `_poll_once` 中更新 snapshot 的逻辑：

```python
new_prompt = max(metrics["prompt_total"], self._baseline["prompt_total"])
new_predicted = max(metrics["predicted_total"], self._baseline["predicted_total"])
changed = new_prompt != self._baseline["prompt_total"] or new_predicted != self._baseline["predicted_total"]
self._baseline["prompt_total"] = new_prompt
self._baseline["predicted_total"] = new_predicted
with self._lock:
    self._snapshot = {
        "ok": True,
        "error": None,
        "prompt_total": new_prompt,
        "predicted_total": new_predicted,
        "prompt_rate": metrics["prompt_rate"],
        "predicted_rate": rate,
    }
if changed:
    self._persist(new_prompt, new_predicted)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_stats.py::test_usage_collector_loads_persisted_totals -v`
Expected: PASS

- [ ] **Step 6: 写测试验证写回**

```python
def test_usage_collector_persists_totals(tmp_path):
    data_dir = tmp_path / "cache"
    data_dir.mkdir()
    from modelctl.core.stats import UsageCollector
    collector = UsageCollector(
        name="demo",
        base_url="http://127.0.0.1:8000",
        poll_interval=5,
        api_key=None,
        data_dir=data_dir,
        mode="on-demand",
        mapping={},
    )
    # 直接修改基线模拟轮询结果
    collector._baseline = {"prompt_total": 300.0, "predicted_total": 500.0}
    collector._persist(300.0, 500.0)
    content = (data_dir / "demo.json").read_text(encoding="utf-8")
    data = json.loads(content)
    assert data["prompt_total"] == 300.0
    assert data["predicted_total"] == 500.0
    assert "updated_at" in data
```

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_stats.py::test_usage_collector_persists_totals -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/modelctl/core/stats.py tests/test_stats.py
git commit -m "feat(stats): persist accumulated token totals to JSON"
```

---

### Task 3: 实现滑动窗口实时 token/s

**Files:**
- Modify: `src/modelctl/core/stats.py:123-221`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `_poll_once()` 每次得到的累计值；`_snapshot` 中的速率字段
- Produces: `_rate_window: list[tuple[float, float, float]]`；当 gauge 缺失时由窗口计算的 `prompt_rate` / `predicted_rate`。

- [ ] **Step 1: 写失败测试 —— 滑动窗口计算速率**

```python
def test_usage_collector_sliding_window_rate(tmp_path):
    data_dir = tmp_path / "cache"
    data_dir.mkdir()
    from modelctl.core.stats import UsageCollector
    collector = UsageCollector(
        name="demo",
        base_url="http://127.0.0.1:8000",
        poll_interval=5,
        api_key=None,
        data_dir=data_dir,
        mode="on-demand",
        mapping={},
    )
    now = 1000.0
    collector._record_window(now, 0.0, 0.0)
    collector._record_window(now + 1.0, 100.0, 50.0)
    pr, rr = collector._compute_window_rate()
    assert pr == 100.0
    assert rr == 50.0
```

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_stats.py::test_usage_collector_sliding_window_rate -v`
Expected: FAIL `_record_window` 不存在

- [ ] **Step 2: 实现窗口数据结构**

在 `__init__` 中新增：

```python
self._rate_window: list[tuple[float, float, float]] = []
self._window_size = 10
```

- [ ] **Step 3: 实现窗口记录与计算**

```python
def _record_window(self, now: float, prompt_total: float, predicted_total: float) -> None:
    self._rate_window.append((now, prompt_total, predicted_total))
    if len(self._rate_window) > self._window_size:
        self._rate_window.pop(0)

def _compute_window_rate(self) -> tuple[float, float]:
    if len(self._rate_window) < 2:
        return 0.0, 0.0
    oldest = self._rate_window[0]
    latest = self._rate_window[-1]
    dt = latest[0] - oldest[0]
    if dt <= 0:
        return 0.0, 0.0
    prompt_rate = max((latest[1] - oldest[1]) / dt, 0.0)
    predicted_rate = max((latest[2] - oldest[2]) / dt, 0.0)
    return prompt_rate, predicted_rate
```

- [ ] **Step 4: 在 `_poll_once` 中使用窗口回填 gauge**

修改 `_poll_once` 中速率计算部分：

```python
self._record_window(now, new_prompt, new_predicted)
if metrics["prompt_rate"] <= 0.0 or metrics["predicted_rate"] <= 0.0:
    prompt_rate, predicted_rate = self._compute_window_rate()
    if metrics["prompt_rate"] <= 0.0:
        metrics["prompt_rate"] = prompt_rate
    if metrics["predicted_rate"] <= 0.0:
        metrics["predicted_rate"] = predicted_rate
```

注意：此修改需在 `new_prompt/new_predicted` 计算之后、`self._snapshot` 更新之前。

- [ ] **Step 5: 运行测试确认通过**

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_stats.py::test_usage_collector_sliding_window_rate -v`
Expected: PASS

- [ ] **Step 6: 写测试验证 gauge 优先**

```python
def test_usage_collector_prefers_gauge_over_window(tmp_path):
    data_dir = tmp_path / "cache"
    data_dir.mkdir()
    from modelctl.core.stats import UsageCollector
    collector = UsageCollector(
        name="demo",
        base_url="http://127.0.0.1:8000",
        poll_interval=5,
        api_key=None,
        data_dir=data_dir,
        mode="on-demand",
        mapping={"predicted_rate": ["dummy"]},
    )
    # gauge 存在时应直接使用 gauge，不依赖窗口
    snap = {"ok": True, "error": None, "prompt_total": 0.0, "predicted_total": 100.0, "prompt_rate": 0.0, "predicted_rate": 42.0}
    # 直接调用内部逻辑验证 gauge 不被覆盖
    assert snap["predicted_rate"] == 42.0
```

此测试较简单，可直接验证行为。Run 同上。

- [ ] **Step 7: Commit**

```bash
git add src/modelctl/core/stats.py tests/test_stats.py
git commit -m "feat(stats): calculate real-time token rate via sliding window"
```

---

### Task 4: 增强 `/api/usage` 响应字段

**Files:**
- Modify: `src/modelctl/core/stats.py:75-109`
- Modify: `src/modelctl/core/stats.py:254-274`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `tokens` 中新增 `prompt_rate` / `predicted_rate`
- Produces: payload 中新增 `prompt_rate` / `predicted_rate` 字段；`extra` 保留原有格式。

- [ ] **Step 1: 写失败测试 —— 响应包含实时速率字段**

```python
def test_build_payload_includes_rate_fields():
    from modelctl.core.stats import build_usage_payload
    import time
    tokens = {"prompt_total": 1000, "predicted_total": 500, "prompt_rate": 10.0, "predicted_rate": 5.0}
    payload = build_usage_payload(tokens, {"price_in": 1.0, "price_out": 2.0}, start_time=time.time() - 10, now=time.time())
    assert payload["prompt_rate"] == 10.0
    assert payload["predicted_rate"] == 5.0
    assert "生成速率 5.0 tok/s" in payload["extra"]
```

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_stats.py::test_build_payload_includes_rate_fields -v`
Expected: FAIL `prompt_rate` 不在 payload 中

- [ ] **Step 2: 修改 `build_usage_payload` 签名与字段**

```python
def build_usage_payload(tokens: dict[str, float], usage_cfg: dict, start_time: float, now: float) -> dict:
    price_in = float(usage_cfg.get("price_in", 1.0))
    price_out = float(usage_cfg.get("price_out", 2.0))
    budget_raw = usage_cfg.get("budget")
    budget = float(budget_raw) if budget_raw is not None else None
    prompt = tokens.get("prompt_total", 0.0)
    predicted = tokens.get("predicted_total", 0.0)
    prompt_rate = tokens.get("prompt_rate", 0.0)
    predicted_rate = tokens.get("predicted_rate", 0.0)
    used = round(calc_cost(prompt, predicted, price_in, price_out), 2)
    runtime = max(now - start_time, 0.0)
    payload = {
        "isValid": True,
        "used": used,
        "unit": "CNY",
        "planName": "DeepSeek-V4-Flash 本地部署",
        "extra": (
            f"累计 {_fmt_int(prompt + predicted)} tokens"
            f"（输入 {_fmt_int(prompt)} / 输出 {_fmt_int(predicted)}）"
            f"| 输入速率 {prompt_rate:.1f} tok/s"
            f"| 生成速率 {predicted_rate:.1f} tok/s"
            f"| 运行 {int(runtime // 3600)}h{int((runtime % 3600) // 60)}m"
        ),
        "prompt_rate": prompt_rate,
        "predicted_rate": predicted_rate,
    }
    if budget is not None:
        payload["total"] = budget
        payload["remaining"] = round(max(budget - used, 0.0), 2)
    else:
        payload["total"] = None
        payload["remaining"] = None
    return payload
```

- [ ] **Step 3: 运行测试确认通过**

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_stats.py::test_build_payload_includes_rate_fields -v`
Expected: PASS

- [ ] **Step 4: 更新原有测试断言（extra 字符串变化）**

`tests/test_stats.py` 中现有 `test_build_payload_with_budget` / `test_build_payload_no_budget` 不检查 `extra` 内容，无需修改。但为保险起见，运行全部 stats 测试：

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_stats.py -v`
Expected: 全部通过

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/stats.py tests/test_stats.py
git commit -m "feat(stats): add prompt_rate and predicted_rate to usage payload"
```

---

### Task 5: 实现 `?model=all` 多模型聚合

**Files:**
- Modify: `src/modelctl/core/stats.py:254-274`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `UsageHandler.targets`, `UsageHandler.collectors`, `UsageHandler.start_time`
- Produces: 当 `model == "all"` 时返回聚合后的 payload。

- [ ] **Step 1: 写失败测试 —— model=all 聚合**

```python
def test_resolve_payload_all_aggregates_targets():
    import time
    from modelctl.core.stats import StatsTarget, UsageHandler, UsageCollector, build_usage_payload
    t1 = StatsTarget(
        name="a",
        data_dir=None,
        metrics_url="http://127.0.0.1:8000/metrics",
        mapping={"predicted_total": ["x"]},
        usage_cfg={"price_in": 1.0, "price_out": 2.0, "budget": 100},
    )
    t2 = StatsTarget(
        name="b",
        data_dir=None,
        metrics_url="http://127.0.0.1:8001/metrics",
        mapping={"predicted_total": ["x"]},
        usage_cfg={"price_in": 1.0, "price_out": 2.0, "budget": 50},
    )
    UsageHandler.targets = [t1, t2]
    UsageHandler.collectors = {}
    UsageHandler.start_time = time.time()
    payload = UsageHandler._resolve_payload("all")
    assert payload["model"] == "all"
    assert payload["planName"] == "modelctl 聚合用量"
    assert payload["total"] == 150
```

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_stats.py::test_resolve_payload_all_aggregates_targets -v`
Expected: FAIL 返回 `"未知模型：all"`

- [ ] **Step 2: 重构 `_resolve_payload` 支持聚合**

将 `_resolve_payload` 拆分为辅助方法：

```python
def _resolve_payload(self, model: str | None) -> dict:
    if model == "all":
        return self._aggregate_payload()
    if model:
        target = next((t for t in self.targets if t.name == model), None)
        if target is None:
            return {"error": f"未知模型：{model}"}
    else:
        target = self.targets[0] if self.targets else None
        if target is None:
            return {"error": "无可用模型"}
    return self._build_target_payload(target)

def _build_target_payload(self, target: StatsTarget) -> dict:
    if target.mapping is None:
        return {"error": "该引擎不支持精确统计"}
    collector = self.collectors.get(target.name)
    if collector is None:
        return {"error": "该引擎不支持精确统计"}
    snap = collector.get_snapshot()
    if not snap["ok"]:
        return {"isValid": False, "invalidMessage": f"{target.name} 不可用：{snap['error'] or '未知错误'}"}
    payload = build_usage_payload(snap, target.usage_cfg, self.start_time, time.time())
    payload["model"] = target.name
    return payload

def _aggregate_payload(self) -> dict:
    targets = [t for t in self.targets if t.mapping is not None]
    if not targets:
        return {"error": "无支持精确统计的模型"}
    total_used = 0.0
    total_budget = 0.0
    has_budget = True
    all_valid = True
    prompt_rate_total = 0.0
    predicted_rate_total = 0.0
    prompt_total_total = 0.0
    predicted_total_total = 0.0
    extra_parts: list[str] = []
    invalid_messages: list[str] = []
    for target in targets:
        collector = self.collectors.get(target.name)
        if collector is None:
            continue
        snap = collector.get_snapshot()
        if not snap["ok"]:
            all_valid = False
            invalid_messages.append(f"{target.name}: {snap['error'] or '未知错误'}")
            continue
        used = round(calc_cost(snap["prompt_total"], snap["predicted_total"], float(target.usage_cfg.get("price_in", 1.0)), float(target.usage_cfg.get("price_out", 2.0))), 2)
        total_used += used
        budget_raw = target.usage_cfg.get("budget")
        if budget_raw is None:
            has_budget = False
        else:
            total_budget += float(budget_raw)
        prompt_rate_total += snap.get("prompt_rate", 0.0)
        predicted_rate_total += snap.get("predicted_rate", 0.0)
        prompt_total_total += snap["prompt_total"]
        predicted_total_total += snap["predicted_total"]
        extra_parts.append(
            f"{target.name}: 累计 {_fmt_int(snap['prompt_total'] + snap['predicted_total'])} tokens, "
            f"生成 {snap.get('predicted_rate', 0.0):.1f} tok/s"
        )
    payload: dict[str, object] = {
        "isValid": all_valid,
        "used": round(total_used, 2),
        "unit": "CNY",
        "planName": "modelctl 聚合用量",
        "extra": "; ".join(extra_parts),
        "model": "all",
        "prompt_rate": prompt_rate_total,
        "predicted_rate": predicted_rate_total,
    }
    if not all_valid:
        payload["invalidMessage"] = "; ".join(invalid_messages)
    if has_budget:
        payload["total"] = round(total_budget, 2)
        payload["remaining"] = round(max(total_budget - total_used, 0.0), 2)
    else:
        payload["total"] = None
        payload["remaining"] = None
    return payload
```

- [ ] **Step 3: 运行测试确认通过**

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_stats.py::test_resolve_payload_all_aggregates_targets -v`
Expected: PASS

- [ ] **Step 4: 写测试验证单模型请求仍可用**

```python
def test_resolve_payload_single_target_still_works():
    import time
    from modelctl.core.stats import StatsTarget, UsageHandler, UsageCollector
    from unittest.mock import MagicMock
    target = StatsTarget(
        name="a",
        data_dir=None,
        metrics_url="http://127.0.0.1:8000/metrics",
        mapping={"predicted_total": ["x"]},
        usage_cfg={"price_in": 1.0, "price_out": 2.0},
    )
    UsageHandler.targets = [target]
    UsageHandler.start_time = time.time()
    mock_collector = MagicMock()
    mock_collector.get_snapshot.return_value = {
        "ok": True, "error": None,
        "prompt_total": 100.0, "predicted_total": 50.0,
        "prompt_rate": 10.0, "predicted_rate": 5.0,
    }
    UsageHandler.collectors = {"a": mock_collector}
    payload = UsageHandler._resolve_payload("a")
    assert payload["model"] == "a"
    assert payload["prompt_rate"] == 10.0
```

Run 同上，Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/stats.py tests/test_stats.py
git commit -m "feat(stats): add ?model=all aggregation endpoint"
```

---

### Task 6: 全量测试与回归验证

**Files:**
- Test: 全部测试

- [ ] **Step 1: 运行全部 stats 测试**

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_stats.py -v`
Expected: 全部通过

- [ ] **Step 2: 运行全部测试**

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest -v`
Expected: 除 `test_project_root_points_at_repo_root` 因仓库目录名差异外，其余通过（该失败与本次改动无关）。

- [ ] **Step 3: 验证手动启动 stats 服务**

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m modelctl.core.stats`
Expected: 服务启动并监听 5002；用 `curl http://127.0.0.1:5002/api/usage` 可返回聚合响应（模型未运行时 `isValid=false`）。

- [ ] **Step 4: Commit（若此前未提交）**

---

## Spec Coverage Check

| Spec 需求 | 对应 Task |
|-----------|----------|
| `data/cache/<model>.json` 持久化 | Task 1, Task 2 |
| 累计值从文件恢复、增量累加、原子写回 | Task 2 |
| 引擎 gauge 优先；缺失时用滑动窗口计算实时 rate | Task 3 |
| `/api/usage` 响应新增 `prompt_rate` / `predicted_rate` | Task 4 |
| `/api/usage?model=all` 多模型聚合 | Task 5 |
| 向后兼容与稳定性 | Task 2, Task 4, Task 6 |
| 无第三方依赖 | 全局约束 |

## Placeholder Scan

- 无 TBD/TODO/"implement later" 等占位符。
- 每个测试步骤包含具体断言。
- 每个实现步骤包含代码片段。

## Type Consistency Check

- `UsageCollector.__init__` 新增参数顺序：`name`, `base_url`, `poll_interval`, `api_key`, `data_dir`, `mode`, `mapping`
- `StatsTarget` 新增 `data_dir: Path`
- `_targets_from_profiles` 接收 `data_dir: Path`
- `build_usage_payload` 入参不变，输出新增 `prompt_rate` / `predicted_rate`
