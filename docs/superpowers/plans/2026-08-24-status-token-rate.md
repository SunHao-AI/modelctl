# status 主动测速 + 首 Token 耗时 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `modelctl status <name>` 的 Token 速率优先用 stats 数据，无效/为 0 时主动对模型发一次短流式请求实测（prefill/decode 分开），并新增「首 Token 耗时」信息。

**Architecture:** cli.py 新增 `_benchmark_token_rate(adapter)`（纯标准库 urllib 流式 POST + 手写 SSE 解析，测量 TTFT/总耗时/真实 token 计数），新增 `_token_rate_data(profile, caps)`（stats 优先 → 测速 fallback），`_cmd_status` 展示速率与 TTFT。无新依赖。

**Tech Stack:** Python 3.12、标准库（urllib / json / time / io）、pytest（monkeypatch）

## Global Constraints

- Python 3.12；主依赖无 openai/httpx，**测速只用标准库**（urllib + io.TextIOWrapper）
- 遵循现有代码风格：中文注释、loguru
- 现有测试必须保持通过（`uv run pytest tests/ -q`）
- TDD：每个任务先写失败测试，再实现
- stats 有效且 `prompt_rate > 0 或 predicted_rate > 0` 时**必须用 stats**（不测速，零开销）
- 测速超时 10s；失败/空响应返回 None → 显示 `输入 -，输出 -（测速失败）`
- TTFT = 首个非 `[DONE]` 的 `data:` 事件到达时刻（reasoning 模型为思考首 token 时刻）
- 输入速率 = prompt_tokens / TTFT；输出速率 = completion_tokens / (总耗时 − TTFT)（decode_s <= 0 时输出速率 0）
- 本计划沿用「不自动 commit」策略（commit 步骤均不执行，改动留工作区由用户统一提交）

---

### Task 1: `_benchmark_token_rate` 测速函数

**Files:**
- Modify: `src/modelctl/cli.py`（import 区第 21-26 行加 `import time`；新增函数放在 `_live_token_rate_text` 之前）
- Test: `tests/test_modelctl.py`

**Interfaces:**
- Consumes: `EngineAdapter`（`profile.port`、`upstream_model_name()`、`upstream_api_key()`）、`time`、`urllib.request`、`json`
- Produces: `_benchmark_token_rate(adapter) -> tuple[float, float, int] | None`（(input_rate, output_rate, ttft_ms)，失败/超时返回 None）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_modelctl.py`）

```python
def test_benchmark_token_rate_parses_sse(monkeypatch):
    """流式 SSE 响应：TTFT=0.5s、prompt=10、completion=5 → 输入 20 tok/s、输出 10 tok/s、TTFT 500ms。"""
    import io

    import modelctl.cli as cli

    sse = (
        b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
        b'data: {"id":"x","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
        b"data: [DONE]\n\n"
    )

    class _FakeResp:
        def __init__(self, data):
            self._buf = io.BytesIO(data)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def readable(self):
            return True

        def read(self, *a, **k):
            return self._buf.read(*a, **k)

    ticks = iter([1.0, 1.5, 2.0])  # t_start=1.0 → t_ttft=1.5 → t_end=2.0
    monkeypatch.setattr(cli.time, "perf_counter", lambda: next(ticks))

    def fake_urlopen(req, timeout=10):
        return _FakeResp(sse)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    from types import SimpleNamespace

    adapter = SimpleNamespace(
        profile=SimpleNamespace(port=8101),
        upstream_model_name=lambda: "m",
        upstream_api_key=lambda: None,
    )
    result = cli._benchmark_token_rate(adapter)
    assert result == (20.0, 10.0, 500)


def test_benchmark_token_rate_timeout_returns_none(monkeypatch):
    import modelctl.cli as cli

    def fake_urlopen(req, timeout=10):
        raise TimeoutError("timeout")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    from types import SimpleNamespace

    adapter = SimpleNamespace(
        profile=SimpleNamespace(port=8101),
        upstream_model_name=lambda: "m",
        upstream_api_key=lambda: None,
    )
    assert cli._benchmark_token_rate(adapter) is None


def test_benchmark_token_rate_empty_stream_returns_none(monkeypatch):
    import io

    import modelctl.cli as cli

    class _FakeResp:
        def __init__(self, data):
            self._buf = io.BytesIO(data)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def readable(self):
            return True

        def read(self, *a, **k):
            return self._buf.read(*a, **k)

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=10: _FakeResp(b""))
    from types import SimpleNamespace

    adapter = SimpleNamespace(
        profile=SimpleNamespace(port=8101),
        upstream_model_name=lambda: "m",
        upstream_api_key=lambda: None,
    )
    assert cli._benchmark_token_rate(adapter) is None  # 无任何 chunk → 无 TTFT
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_modelctl.py::test_benchmark_token_rate_parses_sse -q`
Expected: FAIL（`AttributeError: module 'modelctl.cli' has no attribute '_benchmark_token_rate'`）

- [ ] **Step 3: 实现**

`src/modelctl/cli.py`：

3a. import 区（第 26 行 `from pathlib import Path` 之前）加：

```python
import time
```

3b. 在 `_live_token_rate_text` 之前新增：

```python
def _benchmark_token_rate(adapter) -> tuple[float, float, int] | None:
    """主动测速：发一次短流式请求，返回 (input_rate, output_rate, ttft_ms)。

    输入速率 = prompt_tokens / TTFT（prefill）；输出速率 = completion_tokens / (总耗时 - TTFT)（decode）。
    TTFT 取首个非 [DONE] 的 data: 事件到达时刻（reasoning 模型为思考首 token 时刻）。
    请求失败 / 超时 / 空响应返回 None。
    """
    import io

    profile = adapter.profile
    body = json.dumps({
        "model": adapter.upstream_model_name(),
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 64,
        "stream": True,
        "stream_options": {"include_usage": True},
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = adapter.upstream_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = f"http://127.0.0.1:{profile.port}/v1/chat/completions"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    t_start = time.perf_counter()
    t_ttft: float | None = None
    t_end: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            reader = io.TextIOWrapper(resp, encoding="utf-8", errors="replace")
            for line in reader:
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                if t_ttft is None:
                    t_ttft = time.perf_counter()
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                usage = data.get("usage") if isinstance(data, dict) else None
                if isinstance(usage, dict):
                    if isinstance(usage.get("prompt_tokens"), int):
                        prompt_tokens = usage["prompt_tokens"]
                    if isinstance(usage.get("completion_tokens"), int):
                        completion_tokens = usage["completion_tokens"]
            t_end = time.perf_counter()
    except (OSError, ValueError):
        return None
    if t_ttft is None or t_end is None:
        return None
    ttft_s = t_ttft - t_start
    if ttft_s <= 0:
        return None
    prompt_tokens = prompt_tokens if prompt_tokens is not None else len("hi") // 4
    completion_tokens = completion_tokens if completion_tokens is not None else 1
    input_rate = round(prompt_tokens / ttft_s, 1)
    decode_s = (t_end - t_start) - ttft_s
    output_rate = round(completion_tokens / decode_s, 1) if decode_s > 0 else 0.0
    return input_rate, output_rate, round(ttft_s * 1000)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_modelctl.py -q`
Expected: PASS（含既有测试与新增 3 个）

- [ ] **Step 5: Commit**

**不执行**（本计划沿用不自动 commit 策略）。

---

### Task 2: `_token_rate_data` stats 优先 + 测速 fallback

**Files:**
- Modify: `src/modelctl/cli.py`（新增 `_token_rate_data`，放在 `_live_token_rate_text` 之后）
- Test: `tests/test_modelctl.py`

**Interfaces:**
- Consumes: `_benchmark_token_rate`（Task 1）、`get_adapter`、`os.environ.get("USAGE_PORT", "5002")`、`urllib.request`
- Produces: `_token_rate_data(profile, caps) -> dict`，返回 `{"prompt_rate": float|None, "predicted_rate": float|None, "ttft_ms": int|None, "source": "stats"|"bench"|None}`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_modelctl.py`）

```python
def test_token_rate_data_uses_stats_when_valid(monkeypatch):
    """stats 有效且速率 > 0 → 用 stats，不测速。"""
    import io
    import json

    import modelctl.cli as cli

    class _FakeResp:
        def __init__(self, data):
            self._buf = io.BytesIO(data)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a, **k):
            return self._buf.read(*a, **k)

    def fake_urlopen(req, timeout=None):
        body = json.dumps({"isValid": True, "prompt_rate": 12.5, "predicted_rate": 33.3}).encode()
        return _FakeResp(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(cli, "_benchmark_token_rate", lambda adapter: (9.9, 9.9, 100))
    from types import SimpleNamespace

    caps = SimpleNamespace()
    profile = SimpleNamespace(name="m")
    data = cli._token_rate_data(profile, caps)
    assert data == {"prompt_rate": 12.5, "predicted_rate": 33.3, "ttft_ms": None, "source": "stats"}


def test_token_rate_data_benchmarks_when_stats_zero(monkeypatch):
    """stats 速率为 0 → 主动测速。"""
    import io
    import json

    import modelctl.cli as cli

    class _FakeResp:
        def __init__(self, data):
            self._buf = io.BytesIO(data)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a, **k):
            return self._buf.read(*a, **k)

    def fake_urlopen(req, timeout=None):
        body = json.dumps({"isValid": True, "prompt_rate": 0.0, "predicted_rate": 0.0}).encode()
        return _FakeResp(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(cli, "_benchmark_token_rate", lambda adapter: (5.0, 8.0, 300))
    from types import SimpleNamespace

    caps = SimpleNamespace()
    profile = SimpleNamespace(name="m")
    data = cli._token_rate_data(profile, caps)
    assert data == {"prompt_rate": 5.0, "predicted_rate": 8.0, "ttft_ms": 300, "source": "bench"}


def test_token_rate_data_benchmarks_when_stats_unavailable(monkeypatch):
    """stats 服务不可用（连接失败）→ 主动测速。"""
    import modelctl.cli as cli

    def fake_urlopen(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(cli, "_benchmark_token_rate", lambda adapter: (5.0, 8.0, 300))
    from types import SimpleNamespace

    caps = SimpleNamespace()
    profile = SimpleNamespace(name="m")
    data = cli._token_rate_data(profile, caps)
    assert data["source"] == "bench" and data["ttft_ms"] == 300
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_modelctl.py::test_token_rate_data_uses_stats_when_valid -q`
Expected: FAIL（`AttributeError: module 'modelctl.cli' has no attribute '_token_rate_data'`）

- [ ] **Step 3: 实现**

`src/modelctl/cli.py`，在 `_live_token_rate_text` 之后新增：

```python
def _token_rate_data(profile, caps) -> dict:
    """Token 速率数据：stats 优先，无效/为 0 时主动测速。

    返回 {"prompt_rate": float|None, "predicted_rate": float|None,
          "ttft_ms": int|None, "source": "stats"|"bench"|None}。
    """
    port = int(os.environ.get("USAGE_PORT", "5002"))
    url = f"http://127.0.0.1:{port}/api/usage?model={profile.name}"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError, json.JSONDecodeError):
        data = {}
    if isinstance(data, dict) and data.get("isValid"):
        prompt_rate = data.get("prompt_rate")
        predicted_rate = data.get("predicted_rate")
        if (
            isinstance(prompt_rate, (int, float))
            and isinstance(predicted_rate, (int, float))
            and (prompt_rate > 0 or predicted_rate > 0)
        ):
            return {
                "prompt_rate": float(prompt_rate),
                "predicted_rate": float(predicted_rate),
                "ttft_ms": None,
                "source": "stats",
            }
    # stats 无效/速率为 0 → 主动测速
    try:
        adapter = get_adapter(profile.engine)(profile, caps)
        result = _benchmark_token_rate(adapter)
    except Exception:  # noqa: BLE001 —— 测速失败不阻塞 status 输出
        result = None
    if result is None:
        return {"prompt_rate": None, "predicted_rate": None, "ttft_ms": None, "source": None}
    prompt_rate, predicted_rate, ttft_ms = result
    return {"prompt_rate": prompt_rate, "predicted_rate": predicted_rate, "ttft_ms": ttft_ms, "source": "bench"}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_modelctl.py -q`
Expected: PASS（含既有测试与新增 3 个）

- [ ] **Step 5: Commit**

**不执行**。

---

### Task 3: `_cmd_status` 展示速率来源与首 Token 耗时

**Files:**
- Modify: `src/modelctl/cli.py`（`_cmd_status` 第 310 行附近）
- Test: `tests/test_modelctl.py`

**Interfaces:**
- Consumes: `_token_rate_data(profile, caps)`（Task 2）
- Produces: status 单模型输出含「Token 速率」行（来源标注）与「首 Token 耗时」行

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_modelctl.py`）

```python
def test_status_output_shows_benchmark_rates_and_ttft(monkeypatch, capsys):
    """status 单模型：速率行带（实测）标注，且显示首 Token 耗时。"""
    from types import SimpleNamespace

    import modelctl.cli as cli

    monkeypatch.setattr(
        cli,
        "_token_rate_data",
        lambda profile, caps: {"prompt_rate": 20.0, "predicted_rate": 10.0, "ttft_ms": 500, "source": "bench"},
    )
    monkeypatch.setattr(
        cli,
        "list_profiles",
        lambda models_dir=None: [SimpleNamespace(name="qwen3.8-vllm", engine="vllm", port=8101)],
    )
    monkeypatch.setattr(cli, "_instance_state", lambda name: "已停止")  # 已停止 → 跳过健康检查（get_adapter）
    monkeypatch.setattr(
        cli,
        "_agent_config_info",
        lambda profile: {
            "context_length": 262144,
            "input_context": 253952,
            "output_context": 8192,
            "tool_call_rounds": "-",
            "vision": "是",
            "temperature": "-",
            "top_p": "-",
            "top_k": "-",
        },
    )
    monkeypatch.setattr(cli, "_price_rate_text", lambda profile: "输入 0.5 元/千token，输出 1 元/千token")

    class _Args:
        name = "qwen3.8-vllm"

    cli._cmd_status(_Args(), None, object())
    out = capsys.readouterr().out
    assert "输入 20.0 tok/s，输出 10.0 tok/s（实测）" in out
    assert "首 Token 耗时：500 ms" in out
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_modelctl.py::test_status_output_shows_benchmark_rates_and_ttft -q`
Expected: FAIL（输出中无 `首 Token 耗时` 行，断言失败）

- [ ] **Step 3: 实现**

`src/modelctl/cli.py`，`_cmd_status` 中（第 309-310 行附近）替换：

```python
        print(f"  Token 计费：{_price_rate_text(profiles[0])}")
        rate = _token_rate_data(profiles[0], caps)
        if rate["source"] is None:
            rate_text = "输入 -，输出 -（测速失败）"
        else:
            rate_text = f"输入 {rate['prompt_rate']:.1f} tok/s，输出 {rate['predicted_rate']:.1f} tok/s"
            if rate["source"] == "bench":
                rate_text += "（实测）"
        print(f"  Token 速率：{rate_text}")
        ttft = rate["ttft_ms"]
        print(f"  首 Token 耗时：{ttft} ms" if ttft is not None else "  首 Token 耗时：-")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_modelctl.py -q`
Expected: PASS（含既有测试与新增 1 个）

- [ ] **Step 5: 全量回归 + Commit（不执行 commit）**

Run: `uv run pytest tests/ -q`
Expected: PASS

**不执行 commit**（沿用本计划策略）。

---

## 自审记录

- **Spec 覆盖**：§2.2 `_benchmark_token_rate`（Task 1）→ §2.3 `_token_rate_data` stats 优先/fallback（Task 2）→ §2.4 输出格式（Task 3）→ §3 错误处理（测速失败/空响应在 Task 1 测试；stats 无效在 Task 2 测试）→ §4 测试计划 6 项（Task 1 覆盖 1/4/5、Task 2 覆盖 1/2/3、Task 3 覆盖 6）。
- **占位符**：无 TBD/TODO；每步含完整代码与验证命令。
- **类型一致性**：`_benchmark_token_rate(adapter) -> tuple[float, float, int] | None`；`_token_rate_data(profile, caps) -> dict`（4 键）；`_cmd_status` 消费 `rate["prompt_rate"]`/`rate["predicted_rate"]`/`rate["ttft_ms"]`/`rate["source"]`——Task 2 的返回结构与 Task 3 的消费字段一致。
- **注意**：Task 1 测试用 monkeypatch 控制 `time.perf_counter` 保证速率断言稳定；`import time` 在 Task 1 加入。
