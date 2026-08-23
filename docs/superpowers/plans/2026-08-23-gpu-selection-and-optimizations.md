# GPU 指定与启动生命周期优化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 modelctl 支持显式指定模型使用的 GPU，并补齐显存预检、GPU 冲突检测、跨平台停止、健康检查退避及相关测试。

**Architecture:** 新增 `core/gpu_utils.py` 统一 GPU 列表解析/校验；`EngineAdapter` 抽象层按 profile > CLI > env 优先级解析 `gpu_list` 并注入 `CUDA_VISIBLE_DEVICES`；新增 `core/gpu_lock.py` 做轻量文件锁冲突检测；`core/process.py` 增加 Windows 兼容停止与健康检查指数退避。

**Tech Stack:** Python 3.12, pytest, pytest-mock, loguru, pathlib, subprocess, signal

## Global Constraints

- 所有文本文件使用 LF 行尾（`.gitattributes` 已配置）。
- GPU 索引格式：逗号分隔整数字符串，例如 `"0,1,2,3"`。
- 优先级：profile.gpu_list > CLI `--gpus` > `MODELCTL_GPUS` 环境变量 > 默认全部 GPU。
- 严格校验：重复、越界、与 `tensor_parallel_size` 不匹配均报错。
- 不引入新的第三方依赖。
- 每阶段结束后必须 `uv run pytest -q` 全绿。

---

## File Map

| 文件 | 责任 |
|---|---|
| `src/modelctl/core/gpu_utils.py` | GPU 列表解析、校验、优先级解析工具函数 |
| `src/modelctl/core/capabilities.py` | 新增 `gpu_indices`、按 GPU 汇总显存 helper |
| `src/modelctl/core/gpu_lock.py` | GPU 占用文件锁：获取、释放、冲突检测、残留清理 |
| `src/modelctl/engines/base.py` | `EngineAdapter` 新增 `selected_gpus/validate_gpu_selection/cuda_visible_devices` |
| `src/modelctl/engines/llamacpp.py` | 读取 `gpu_list`、注入 CUDA_VISIBLE_DEVICES、tensor-split 数量对齐 |
| `src/modelctl/engines/vllm.py` | 读取 `gpu_list`、校验/推导 tensor_parallel_size、注入环境变量 |
| `src/modelctl/engines/sglang.py` | 同 vllm |
| `src/modelctl/engines/unsloth.py` | 读取 `gpu_list`、校验 tensor_parallel 数量、注入环境变量 |
| `src/modelctl/engines/ollama.py` | 读取 `gpu_list`、注入环境变量（ollama serve 全局共享） |
| `src/modelctl/cli.py` | `start` / `all start` 增加 `--gpus` 参数 |
| `src/modelctl/core/process.py` | Windows 兼容停止、健康检查指数退避、停止后释放 GPU 锁 |
| `tests/test_gpu_utils.py` | gpu_utils 单元测试 |
| `tests/test_engines_base_gpu.py` | EngineAdapter GPU 相关方法测试 |
| `tests/test_gpu_lock.py` | gpu_lock 单元测试 |
| `tests/test_engines_llamacpp.py` | llamacpp 适配器 build_command / check_requirements 测试 |
| `tests/test_engines_vllm.py` | vllm 适配器 GPU 指定测试 |
| `tests/test_engines_sglang.py` | sglang 适配器 GPU 指定测试 |
| `tests/test_engines_unsloth.py` | unsloth 适配器 GPU 指定测试 |
| `tests/test_engines_ollama.py` | ollama 适配器 GPU 指定测试 |
| `tests/test_process.py` | 停止流程跨平台分支、健康检查退避测试 |

---

## Task 1: GPU 列表工具函数与 Capabilities 增强

**Files:**
- Create: `src/modelctl/core/gpu_utils.py`
- Modify: `src/modelctl/core/capabilities.py`
- Create tests: `tests/test_gpu_utils.py`

**Interfaces:**
- Consumes: nothing (avoid circular import with engines.base)
- Produces:
  - `parse_gpu_list(raw: str | list[int] | None) -> list[int] | None`
  - `validate_gpu_selection(gpus: list[int], available: list[int]) -> None`  # raises ValueError
  - `resolve_gpu_list(profile_value, cli_value, env_value) -> list[int] | None`
  - `GPUValidationError(ValueError)`
  - `Capabilities.gpu_indices: list[int]`
  - `Capabilities.vram_total_mb_per_gpu: list[int]`
  - `selected_vram_total_mb(caps, gpus) -> int`
  - `selected_vram_free_mb(caps, gpus) -> int`

- [ ] **Step 1: Write failing tests for gpu_utils**

```python
import pytest
from modelctl.core.gpu_utils import GPUValidationError, parse_gpu_list, validate_gpu_selection, resolve_gpu_list

def test_parse_gpu_list_string():
    assert parse_gpu_list("0,1,2") == [0, 1, 2]

def test_parse_gpu_list_list():
    assert parse_gpu_list([3, 4]) == [3, 4]

def test_parse_gpu_list_none():
    assert parse_gpu_list(None) is None

def test_parse_gpu_list_empty_string():
    assert parse_gpu_list("") is None

def test_parse_gpu_list_duplicate():
    with pytest.raises(ValueError, match="重复"):
        parse_gpu_list("0,1,1")

def test_validate_gpu_selection_ok():
    validate_gpu_selection([0, 1], [0, 1, 2, 3])

def test_validate_gpu_selection_out_of_range():
    with pytest.raises(GPUValidationError, match="超出可用范围"):
        validate_gpu_selection([0, 5], [0, 1, 2, 3])

def test_resolve_gpu_list_priority():
    assert resolve_gpu_list("0,1", "2,3", "4,5") == [0, 1]
    assert resolve_gpu_list(None, "2,3", "4,5") == [2, 3]
    assert resolve_gpu_list(None, None, "4,5") == [4, 5]
    assert resolve_gpu_list(None, None, None) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gpu_utils.py -v`
Expected: FAIL (module/functions not defined)

- [ ] **Step 3: Implement gpu_utils.py**

```python
from __future__ import annotations


class GPUValidationError(ValueError):
    """GPU 列表解析或校验失败。"""


def parse_gpu_list(raw: str | list[int] | None) -> list[int] | None:
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    if isinstance(raw, list):
        items = [int(x) for x in raw]
    else:
        parts = [p.strip() for p in str(raw).split(",") if p.strip() != ""]
        try:
            items = [int(p) for p in parts]
        except ValueError as exc:
            raise GPUValidationError(f"gpu_list 包含非整数项：{raw!r}") from exc
    if len(items) != len(set(items)):
        dup = next(x for x in items if items.count(x) > 1)
        raise GPUValidationError(f"gpu_list 存在重复 GPU 索引：{dup}")
    return items


def validate_gpu_selection(gpus: list[int], available: list[int]) -> None:
    if not gpus:
        return
    available_set = set(available)
    invalid = [g for g in gpus if g not in available_set]
    if invalid:
        raise GPUValidationError(
            f"[gpu_list] 配置的 GPU 索引 {gpus} 超出可用范围。\n"
            f"当前可用 GPU 索引：{','.join(str(g) for g in sorted(available_set))}"
        )


def resolve_gpu_list(
    profile_value: str | list[int] | None,
    cli_value: str | None,
    env_value: str | None,
) -> list[int] | None:
    for candidate in (profile_value, cli_value, env_value):
        parsed = parse_gpu_list(candidate)
        if parsed is not None:
            return parsed
    return None
```

- [ ] **Step 4: Capabilities 新增 gpu_indices 与显存 helper**

In `src/modelctl/core/capabilities.py`. IMPORTANT: do NOT change the meaning of `vram_total_mb` — it is "单卡显存"（first GPU total）, asserted by `tests/test_capabilities.py::test_probe...` and printed by `cli.py` as 单卡显存. Only ADD two new fields with defaults; existing tests construct `Capabilities(...)` positionally/keyword so defaults are required.

```python
# dataclass Capabilities — ADD these two fields (keep all existing fields unchanged):
    gpu_indices: list[int] = field(default_factory=list)
    vram_total_mb_per_gpu: list[int] = field(default_factory=list)  # 每卡总显存（MB），与 vram_free_mb 对齐

# probe(): inside the EXISTING row loop that already appends to `frees`, also collect per-GPU totals.
#   Initialize `totals: list[int] = []` right before the loop (next to `frees`).
#   Inside the loop, after the frees append block, add:
try:
    totals.append(int(parts[1]))
except ValueError:
    totals.append(0)
# After the loop, alongside `caps.vram_free_mb = frees` / `caps.gpu_count = len(frees)`:
caps.vram_total_mb_per_gpu = totals          # NEW
caps.gpu_indices = list(range(len(frees)))   # NEW
# Do NOT touch caps.vram_total_mb or caps.gpu_count assignment logic.


def selected_vram_total_mb(caps: Capabilities, gpus: list[int]) -> int:
    """按选中 GPU 索引汇总各卡总显存。"""
    return sum(
        (caps.vram_total_mb_per_gpu[g] if g < len(caps.vram_total_mb_per_gpu) else 0)
        for g in gpus
    )


def selected_vram_free_mb(caps: Capabilities, gpus: list[int]) -> int:
    """按选中 GPU 索引汇总各卡剩余显存（MB）。"""
    return sum(
        (caps.vram_free_mb[g] if g < len(caps.vram_free_mb) else 0)
        for g in gpus
    )
```

Also add a test to `tests/test_capabilities.py` verifying `gpu_indices` and `vram_total_mb_per_gpu` are populated by `probe()` from the existing multi-GPU sample input (without altering existing assertions).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_gpu_utils.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/modelctl/core/gpu_utils.py src/modelctl/core/capabilities.py tests/test_gpu_utils.py
git commit -m "feat(gpu): 新增 gpu_list 解析/校验工具与 Capabilities.gpu_indices"
```

---

## Task 2: EngineAdapter 抽象层与 CLI --gpus 参数

**Files:**
- Modify: `src/modelctl/engines/base.py`
- Modify: `src/modelctl/cli.py`

**Design note (pre-flight refinement):** Do NOT add a `cli_gpus` parameter to `EngineAdapter.__init__`. Adapters are constructed as `get_adapter(engine)(profile, caps)` inside `core/all_service.py`; threading a new param would touch every call site. Instead the CLI sets `os.environ["MODELCTL_GPUS"] = args.gpus` when `--gpus` is provided, and `selected_gpus()` reads it via the env slot. This preserves the spec priority profile > CLI > env with zero signature changes.

**Interfaces:**
- Consumes: `resolve_gpu_list`, `validate_gpu_selection`, `GPUValidationError` from `core.gpu_utils`
- Produces:
  - `EngineAdapter.selected_gpus() -> list[int] | None`  # reads cfg.gpu_list then os.environ MODELCTL_GPUS
  - `EngineAdapter.validate_gpu_selection(gpus=None) -> None`  # raises RequirementError on invalid
  - `EngineAdapter.cuda_visible_devices(gpus) -> dict[str, str]`

- [ ] **Step 1: Write failing tests for EngineAdapter GPU helpers**

```python
import pytest
from unittest.mock import MagicMock

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import Profile
from modelctl.engines.base import EngineAdapter, RequirementError

class DummyAdapter(EngineAdapter):
    def build_command(self): return [], {}
    def check_requirements(self): pass
    def metrics_mapping(self): return None

def test_profile_wins_over_env(monkeypatch):
    monkeypatch.setenv("MODELCTL_GPUS", "2,3")
    profile = Profile(name="x", engine="dummy", port=1, engine_config={"gpu_list": "0,1"})
    adapter = DummyAdapter(profile, Capabilities(gpu_indices=[0, 1, 2, 3]))
    assert adapter.selected_gpus() == [0, 1]

def test_env_fallback(monkeypatch):
    monkeypatch.setenv("MODELCTL_GPUS", "4,5")
    profile = Profile(name="x", engine="dummy", port=1, engine_config={})
    adapter = DummyAdapter(profile, Capabilities(gpu_indices=[0, 1, 2, 3, 4, 5]))
    assert adapter.selected_gpus() == [4, 5]

def test_none_when_unset(monkeypatch):
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    profile = Profile(name="x", engine="dummy", port=1, engine_config={})
    adapter = DummyAdapter(profile, Capabilities(gpu_indices=[0, 1, 2, 3]))
    assert adapter.selected_gpus() is None

def test_validate_gpu_selection_raises():
    profile = Profile(name="x", engine="dummy", port=1, engine_config={"gpu_list": "0,8"})
    adapter = DummyAdapter(profile, Capabilities(gpu_indices=[0, 1, 2, 3]))
    with pytest.raises(RequirementError, match="超出可用范围"):
        adapter.validate_gpu_selection(adapter.selected_gpus())

def test_cuda_visible_devices():
    adapter = DummyAdapter(Profile(name="x", engine="dummy", port=1), Capabilities())
    assert adapter.cuda_visible_devices([0, 2]) == {"CUDA_VISIBLE_DEVICES": "0,2"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_engines_base_gpu.py -v`
Expected: FAIL

- [ ] **Step 3: Implement EngineAdapter GPU helpers**

In `src/modelctl/engines/base.py`, keep the existing `__init__(self, profile, caps)` unchanged (do NOT add cli_gpus). Add these three methods to `EngineAdapter`:

```python
import os
from modelctl.core.gpu_utils import GPUValidationError, resolve_gpu_list, validate_gpu_selection

# inside class EngineAdapter(ABC):
def selected_gpus(self) -> list[int] | None:
    """按 profile.gpu_list > 环境变量 MODELCTL_GPUS（CLI --gpus 亦写入此变量）解析。"""
    cfg = self.profile.engine_config
    return resolve_gpu_list(cfg.get("gpu_list"), None, os.environ.get("MODELCTL_GPUS"))

def validate_gpu_selection(self, gpus: list[int] | None = None) -> None:
    gpus = gpus if gpus is not None else self.selected_gpus()
    if gpus is None:
        return
    try:
        validate_gpu_selection(gpus, self.caps.gpu_indices)
    except GPUValidationError as exc:
        raise RequirementError(str(exc)) from exc

def cuda_visible_devices(self, gpus: list[int]) -> dict[str, str]:
    return {"CUDA_VISIBLE_DEVICES": ",".join(str(g) for g in gpus)}
```

- [ ] **Step 4: Update CLI to parse --gpus and set env**

In `src/modelctl/cli.py`:
1. In the parser loop where `start`/`restart` subcommands get `--timeout`, also add `--gpus`:
   ```python
   p.add_argument("--gpus", default=None, help="逗号分隔的 GPU 索引，如 0,1,2（覆盖环境变量 MODELCTL_GPUS）")
   ```
   And on the `all` subparser (`ap`) add the same `--gpus` option so `modelctl all start --gpus ...` works.
2. In the main dispatch (where commands are routed), after parsing args and before invoking the command handler, when a model-start path runs, apply:
   ```python
   if getattr(args, "gpus", None):
       os.environ["MODELCTL_GPUS"] = args.gpus
   ```
   Place it early in `main()` right after argument parsing so both `_cmd_start` and `all start` pick it up. Ensure `import os` is present at module top.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_engines_base_gpu.py tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/modelctl/engines/base.py src/modelctl/cli.py tests/test_engines_base_gpu.py
git commit -m "feat(gpu): EngineAdapter 抽象层与 CLI --gpus 参数"
```

---

## Task 3: 各 CUDA 引擎接入 gpu_list

**Files:**
- Modify: `src/modelctl/engines/llamacpp.py`
- Modify: `src/modelctl/engines/vllm.py`
- Modify: `src/modelctl/engines/sglang.py`
- Modify: `src/modelctl/engines/unsloth.py`
- Modify: `src/modelctl/engines/ollama.py`
- Modify tests: `tests/test_engines_llamacpp.py`, `tests/test_engines_vllm.py`, `tests/test_engines_sglang.py`, `tests/test_engines_unsloth.py`, `tests/test_engines_ollama.py`

**Interfaces:**
- Consumes: `selected_gpus()`, `validate_gpu_selection()`, `cuda_visible_devices()` from base
- Produces: engine-specific `build_command` returns with `CUDA_VISIBLE_DEVICES` in env

- [ ] **Step 1: Write failing tests for each engine**

Example for vllm:

```python
from pathlib import Path
from unittest.mock import MagicMock

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import Profile
from modelctl.engines.vllm import VllmAdapter

def _adapter(cfg, cli_gpus=None):
    profile = Profile(name="qwen3.8-vllm", engine="vllm", port=8101, engine_config=cfg)
    return VllmAdapter(profile, Capabilities(gpu_indices=[0,1,2,3], binaries={"vllm": True}), cli_gpus=cli_gpus)

def test_vllm_gpu_list_sets_cuda_visible_devices():
    adapter = _adapter({"model": "/tmp/hf", "tensor_parallel_size": 2, "gpu_list": "2,3"})
    cmd, env = adapter.build_command()
    assert env == {"CUDA_VISIBLE_DEVICES": "2,3"}

def test_vllm_cli_gpus_overrides_profile():
    adapter = _adapter({"model": "/tmp/hf", "tensor_parallel_size": 2, "gpu_list": "0,1"}, cli_gpus="2,3")
    _, env = adapter.build_command()
    assert env == {"CUDA_VISIBLE_DEVICES": "0,1"}

def test_vllm_tensor_parallel_size_derived_from_gpu_list():
    adapter = _adapter({"model": "/tmp/hf", "gpu_list": "1,2"})
    cmd, _ = adapter.build_command()
    assert "--tensor-parallel-size" in cmd
    assert cmd[cmd.index("--tensor-parallel-size") + 1] == "2"
```

Write similar tests for llamacpp (tensor-split count), sglang, unsloth, ollama.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_engines_*.py -v`
Expected: FAIL

- [ ] **Step 3: Implement engine changes**

**llamacpp**

In `check_requirements`:

```python
gpus = self.selected_gpus()
if gpus is not None:
    self.validate_gpu_selection(gpus)
    gpu_count = len(gpus)
else:
    gpu_count = int(cfg.get("gpu_count", 8))
if gpu_count > self.caps.gpu_count:
    raise RequirementError(...)
```

In `build_command`:

```python
gpus = self.selected_gpus()
gpu_count = len(gpus) if gpus else int(cfg.get("gpu_count", 8))
gpu_split = ",".join(["1"] * gpu_count)
env = {...}
if gpus:
    env.update(self.cuda_visible_devices(gpus))
```

**vllm**

In `check_requirements`:

```python
gpus = self.selected_gpus()
if gpus is not None:
    self.validate_gpu_selection(gpus)
    tp = int(cfg.get("tensor_parallel_size", len(gpus)))
    if tp != len(gpus):
        raise RequirementError(f"gpu_list 指定了 {len(gpus)} 块 GPU，tensor_parallel_size={tp} 必须一致")
else:
    tp = int(cfg.get("tensor_parallel_size", 1))
if self.caps.gpu_count and tp > self.caps.gpu_count:
    raise RequirementError(...)
```

In `build_command`:

```python
gpus = self.selected_gpus()
tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
cmd = [..., "--tensor-parallel-size", str(tp), ...]
env = {...}
if gpus:
    env.update(self.cuda_visible_devices(gpus))
```

Apply analogous changes to sglang, unsloth, ollama.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_engines_*.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/engines/*.py tests/test_engines_*.py
git commit -m "feat(gpu): 为 llamacpp/vllm/sglang/unsloth/ollama 接入 gpu_list"
```

---

## Task 4: 按 GPU 显存预检与 GPU 冲突检测

**Files:**
- Create: `src/modelctl/core/gpu_lock.py`
- Modify: `src/modelctl/engines/llamacpp.py`, `src/modelctl/engines/unsloth.py`
- Modify: `src/modelctl/core/process.py` (release locks)
- Create tests: `tests/test_gpu_lock.py`

**Interfaces:**
- Consumes: `selected_gpus()` from adapters
- Produces:
  - `acquire_gpu_lock(name, gpus) -> None`
  - `release_gpu_lock(name) -> None`
  - `list_gpu_locks() -> dict[int, str]`

- [ ] **Step 1: Write failing tests for gpu_lock**

```python
import json
from pathlib import Path

import pytest

from modelctl.core.gpu_lock import acquire_gpu_lock, list_gpu_locks, release_gpu_lock


def test_acquire_and_list(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp_path)
    acquire_gpu_lock("a", [0, 1])
    assert list_gpu_locks() == {0: "a", 1: "a"}


def test_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp_path)
    acquire_gpu_lock("a", [0, 1])
    with pytest.raises(RequirementError, match="已被模型 a 占用"):
        acquire_gpu_lock("b", [1, 2])


def test_release(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp_path)
    acquire_gpu_lock("a", [0])
    release_gpu_lock("a")
    assert list_gpu_locks() == {}


def test_stale_lock_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp_path)
    # create a lock with non-existent pid
    lock = tmp_path / "stale.gpu-lock"
    lock.write_text(json.dumps({"gpus": [0], "pid": 9999999, "updated_at": 0}), encoding="utf-8")
    acquire_gpu_lock("a", [0])
    assert list_gpu_locks() == {0: "a"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gpu_lock.py -v`
Expected: FAIL

- [ ] **Step 3: Implement gpu_lock.py**

```python
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from modelctl.core.envfile import PROJECT_ROOT
from modelctl.engines.base import RequirementError

LOCK_DIR = PROJECT_ROOT / "data" / "cache"
LOCK_SUFFIX = ".gpu-lock"


def _lock_path(name: str) -> Path:
    return LOCK_DIR / f"{name}{LOCK_SUFFIX}"


def _read_lock(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "pid" in data:
            try:
                os.kill(int(data["pid"]), 0)
                return data
            except OSError:
                path.unlink(missing_ok=True)
                return None
    except (OSError, ValueError):
        return None
    return None


def list_gpu_locks() -> dict[int, str]:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[int, str] = {}
    for path in LOCK_DIR.glob(f"*{LOCK_SUFFIX}"):
        data = _read_lock(path)
        if data is None:
            continue
        name = path.name[: -len(LOCK_SUFFIX)]
        for g in data.get("gpus", []):
            result[int(g)] = name
    return result


def acquire_gpu_lock(name: str, gpus: list[int]) -> None:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    locks = list_gpu_locks()
    conflicts = {g: locks[g] for g in gpus if g in locks and locks[g] != name}
    if conflicts:
        detail = "; ".join(f"GPU {g} 已被模型 {n} 占用" for g, n in sorted(conflicts.items()))
        raise RequirementError(f"[gpu_lock] {detail}。请先停止占用模型，或更换 gpu_list。")
    lock = _lock_path(name)
    lock.write_text(
        json.dumps({"gpus": gpus, "pid": os.getpid(), "updated_at": time.time()}, ensure_ascii=False),
        encoding="utf-8",
    )


def release_gpu_lock(name: str) -> None:
    _lock_path(name).unlink(missing_ok=True)
```

- [ ] **Step 4: Wire locks into adapters and process stop**

In llamacpp/unsloth `check_requirements` after GPU validation:

```python
from modelctl.core.gpu_lock import acquire_gpu_lock
gpus = self.selected_gpus()
if gpus is not None:
    self.validate_gpu_selection(gpus)
    acquire_gpu_lock(self.profile.name, gpus)
```

In `core/process.py` `stop_instance`:

```python
try:
    from modelctl.core.gpu_lock import release_gpu_lock
    release_gpu_lock(name)
except Exception:
    pass
```

- [ ] **Step 5: Update per-GPU vram checks**

In llamacpp `check_requirements`:

```python
from modelctl.core.capabilities import selected_vram_free_mb
if self._model and self._model.is_file():
    need_mb = self._model.stat().st_size / 1024 / 1024 * 1.1
    gpus = self.selected_gpus()
    free_mb = selected_vram_free_mb(self.caps, gpus) if gpus else free_vram_total_mb(self.caps)
    if need_mb > free_mb:
        raise RequirementError(...)
```

Apply analogous change to unsloth `_check_vram`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_gpu_lock.py tests/test_engines_llamacpp.py tests/test_engines_unsloth.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/modelctl/core/gpu_lock.py src/modelctl/core/process.py src/modelctl/engines/llamacpp.py src/modelctl/engines/unsloth.py tests/test_gpu_lock.py
git commit -m "feat(gpu): 按 GPU 显存预检与 GPU 冲突文件锁"
```

---

## Task 5: 进程停止跨平台与健康检查指数退避

**Files:**
- Modify: `src/modelctl/core/process.py`
- Modify tests: `tests/test_process.py`

**Interfaces:**
- Consumes: existing `stop_instance` and `wait_health`
- Produces: cross-platform `stop_instance`, exponential backoff `wait_health`

- [ ] **Step 1: Write failing tests for process helpers**

```python
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from modelctl.core.process import wait_health


def test_wait_health_exponential_backoff():
    call_times = []

    def slow_health(*args, **kwargs):
        call_times.append(time.time())
        return len(call_times) >= 3

    with patch("modelctl.core.process.urllib.request.urlopen", side_effect=slow_health):
        start = time.time()
        assert wait_health("http://localhost/health", timeout=10) is True
        deltas = [call_times[i] - (call_times[i-1] if i > 0 else start) for i in range(len(call_times))]
        assert deltas[1] >= 0.9  # first interval ~1s
        assert deltas[2] >= 1.8  # second interval ~2s
```

Also add tests for `_is_posix` platform branching in `stop_instance` using monkeypatch.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_process.py -v`
Expected: FAIL

- [ ] **Step 3: Implement changes in process.py**

For `wait_health`:

```python
interval = 1.0
while time.time() < deadline:
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            if 200 <= resp.status < 300:
                return True
    except (urllib.error.URLError, OSError):
        pass
    remaining = deadline - time.time()
    if remaining <= 0:
        break
    time.sleep(min(interval, remaining))
    interval = min(interval * 2, 5.0)
return False
```

For `stop_instance`, make POSIX-only calls conditional:

```python
import sys

if sys.platform != "win32":
    subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
    for pat in patterns:
        subprocess.run(["pkill", "-f", pat], capture_output=True)
else:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
```

Also handle `os.killpg` AttributeError gracefully.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_process.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/process.py tests/test_process.py
git commit -m "fix(process): Windows 兼容停止与健康检查指数退避"
```

---

## Task 6: 全量回归与文档更新

**Files:**
- Modify: `README.md`
- Modify: example profiles under `models/` if needed

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -q`
Expected: ALL PASS

- [ ] **Step 2: Run lint/type check**

Run:

```bash
uv run ruff check src tests
uv run mypy src
```

Expected: no errors

- [ ] **Step 3: Update README**

Add a section explaining:

```yaml
llamacpp:
  gpu_list: "0,1,2,3"  # 指定使用 GPU 0-3
```

And CLI examples:

```bash
modelctl start deepseek-v4-flash-llamacpp --gpus 0,1,2,3
MODELCTL_GPUS=4,5 modelctl start qwen3.8-vllm
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: 补充 gpu_list / --gpus / MODELCTL_GPUS 使用说明"
```

---

## Self-Review Checklist

- [ ] Spec coverage: each requirement in `docs/superpowers/specs/2026-08-23-gpu-selection-and-optimizations-design.md` has a task.
- [ ] No placeholders: no TBD/TODO/fill-in-details steps.
- [ ] Type consistency: `selected_gpus()`, `validate_gpu_selection()`, `cuda_visible_devices()` signatures match across tasks.
- [ ] Testability: each task ends with running tests.
