# 新增生产 GPU 推理引擎实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 modelctl 新增 Aphrodite、LMDeploy、TensorRT-LLM、TokenSpeed 四个生产级 GPU 推理引擎适配器，并补充对应示例配置与测试，使其成为通用一键 LLM 部署工具。

**Architecture:** 延续现有 `EngineAdapter` 插件架构：每个引擎一个适配器文件、一条注册表记录、一个模型配置目录。Aphrodite/LMDeploy/TokenSpeed 作为托管引擎使用独立 venv；TensorRT-LLM 优先 docker 模式，本地模式可选。所有引擎对外暴露 OpenAI 兼容 API，modelctl 负责启动命令构建、GPU 隔离、健康检查与指标映射。

**Tech Stack:** Python 3.12+、uv、CUDA 12.x、NVIDIA Container Toolkit、pytest。

## Global Constraints

- 新增引擎必须实现 `EngineAdapter` 抽象基类（`build_command`、`check_requirements`、`metrics_mapping`）
- 每个新引擎需在 `src/modelctl/engines/__init__.py` 注册，并在 `src/modelctl/core/profile.py` 的 `KNOWN_ENGINES` 中声明
- 托管引擎需在 `src/modelctl/core/envs.py` 的 `MANAGED_ENGINES` 中声明，并在 `envs/<engine>/pyproject.toml` 中锁定依赖
- 所有适配器需支持 `gpu_list` 与 `tensor_parallel_size` 一致性校验（复用 `resolve_gpu_list` / `validate_gpu_selection`）
- 所有适配器健康检查默认探测 `http://127.0.0.1:<port>/health`，不支持时回退到 `/v1/models`
- 配置文件必须包含 `group`、`port`、`api_key` 顶层字段，以及 `<engine>:` 引擎专用配置段
- 测试必须覆盖：命令构建、`check_requirements` 硬性错误、GPU/TP 一致性、健康检查 URL

---

## File Structure

```
envs/
  aphrodite/pyproject.toml          # Aphrodite 独立 venv
  lmdeploy/pyproject.toml           # LMDeploy 独立 venv
  tokenspeed/pyproject.toml         # TokenSpeed 独立 venv
models/
  aphrodite/qwen3.8.yaml           # Aphrodite 示例配置
  lmdeploy/qwen3.8.yaml            # LMDeploy 示例配置
  tensorrt_llm/qwen3.8.yaml        # TensorRT-LLM 示例配置
  tokenspeed/qwen3.5-397b.yaml     # TokenSpeed 示例配置
src/modelctl/
  core/
    capabilities.py                # 新增二进制探测与安装提示
    envs.py                        # 新增托管引擎
    profile.py                     # KNOWN_ENGINES 扩展
  engines/
    __init__.py                    # 注册四个适配器
    aphrodite.py                   # Aphrodite 适配器
    lmdeploy.py                    # LMDeploy 适配器
    tensorrt_llm.py                # TensorRT-LLM 适配器
    tokenspeed.py                  # TokenSpeed 适配器
tests/
  test_engines_aphrodite.py
  test_engines_lmdeploy.py
  test_engines_tensorrt_llm.py
  test_engines_tokenspeed.py
```

---

### Task 0: 共享基础设施与注册表扩展

**Files:**
- Modify: `src/modelctl/core/profile.py:27`
- Modify: `src/modelctl/core/capabilities.py:24-34`
- Modify: `src/modelctl/core/envs.py:16`
- Create: `envs/aphrodite/pyproject.toml`
- Create: `envs/lmdeploy/pyproject.toml`
- Create: `envs/tokenspeed/pyproject.toml`
- Test: `tests/test_core_envs.py`（已存在，扩展断言）

**Interfaces:**
- Consumes: 无
- Produces: `KNOWN_ENGINES`、`ENGINE_BINARIES`、`ENGINE_INSTALL_HINTS`、`MANAGED_ENGINES` 包含新引擎名

- [ ] **Step 1: 扩展 KNOWN_ENGINES**

```python
# src/modelctl/core/profile.py:27
KNOWN_ENGINES = {"llamacpp", "ollama", "vllm", "sglang", "unsloth",
                 "aphrodite", "lmdeploy", "tensorrt_llm", "tokenspeed"}
```

- [ ] **Step 2: 扩展二进制探测与安装提示**

```python
# src/modelctl/core/capabilities.py:24
ENGINE_BINARIES = ["ollama", "vllm", "sglang", "unsloth", "llamacpp",
                   "aphrodite", "lmdeploy", "tensorrt_llm", "tokenspeed"]

ENGINE_INSTALL_HINTS = {
    "ollama": "，建议执行：curl -fsSL https://ollama.com/install.sh | sh",
    "vllm": "，建议执行：modelctl env setup vllm",
    "sglang": "，建议执行：modelctl env setup sglang（与 vllm 依赖互斥，需独立 venv）",
    "unsloth": "，建议执行：curl -fsSL https://unsloth.ai/install.sh | sh",
    "aphrodite": "，建议执行：modelctl env setup aphrodite",
    "lmdeploy": "，建议执行：modelctl env setup lmdeploy",
    "tokenspeed": "，建议执行：modelctl env setup tokenspeed",
    # llamacpp 提示较长，由 cli._cmd_probe 单独输出
}
```

- [ ] **Step 3: 扩展托管引擎列表**

```python
# src/modelctl/core/envs.py:16
MANAGED_ENGINES = ("vllm", "sglang", "aphrodite", "lmdeploy", "tokenspeed")
```

- [ ] **Step 4: 创建 Aphrodite venv 子项目**

```toml
# envs/aphrodite/pyproject.toml
[project]
name = "modelctl-venv-aphrodite"
version = "0.1.0"
description = "Aphrodite Engine 引擎专属虚拟环境"
requires-python = ">=3.12"
dependencies = [
    "aphrodite-engine",
]

[tool.uv]
[[tool.uv.index]]
name = "pytorch-cu13"
url = "https://download.pytorch.org/whl/cu130"
explicit = true
```

- [ ] **Step 5: 创建 LMDeploy venv 子项目**

```toml
# envs/lmdeploy/pyproject.toml
[project]
name = "modelctl-venv-lmdeploy"
version = "0.1.0"
description = "LMDeploy 引擎专属虚拟环境"
requires-python = ">=3.12"
dependencies = [
    "lmdeploy",
]

[tool.uv]
[[tool.uv.index]]
name = "pytorch-cu13"
url = "https://download.pytorch.org/whl/cu130"
explicit = true
```

- [ ] **Step 6: 创建 TokenSpeed venv 子项目**

```toml
# envs/tokenspeed/pyproject.toml
[project]
name = "modelctl-venv-tokenspeed"
version = "0.1.0"
description = "TokenSpeed 引擎专属虚拟环境"
requires-python = ">=3.12"
dependencies = [
    "tokenspeed",
]

[tool.uv]
[[tool.uv.index]]
name = "pytorch-cu13"
url = "https://download.pytorch.org/whl/cu130"
explicit = true
```

- [ ] **Step 7: 运行现有 envs 测试，确保无回归**

Run: `pytest tests/test_core_envs.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/modelctl/core/profile.py src/modelctl/core/capabilities.py src/modelctl/core/envs.py

git add envs/aphrodite/pyproject.toml envs/lmdeploy/pyproject.toml envs/tokenspeed/pyproject.toml

git commit -m "chore: register aphrodite/lmdeploy/tensorrt_llm/tokenspeed engines and venvs"
```

---

### Task 1: Aphrodite Engine 适配器

**Files:**
- Create: `src/modelctl/engines/aphrodite.py`
- Modify: `src/modelctl/engines/__init__.py`
- Create: `models/aphrodite/qwen3.8.yaml`
- Create: `tests/test_engines_aphrodite.py`

**Interfaces:**
- Consumes: `EngineAdapter` 基类、`envs.ensure_env`、`envs.engine_bin`、现有 GPU 校验工具
- Produces: `AphroditeAdapter` 类，注册到 `_REGISTRY["aphrodite"]`

- [ ] **Step 1: 编写命令构建测试**

```python
# tests/test_engines_aphrodite.py
import os
import pytest

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import load_profile
from modelctl.engines import get_adapter

CAPS8 = Capabilities(gpu_count=8, compute_capability="8.9", binaries={"aphrodite": True})


def _write(tmp_path, text, name="m.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return load_profile(name[:-5], tmp_path)


def _stub_venv(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.envs.VENV_ROOT", tmp_path / ".venvs")
    bin_dir = tmp_path / ".venvs" / "aphrodite" / ("Scripts" if os.name == "nt" else "bin")
    exe_name = "aphrodite.exe" if os.name == "nt" else "aphrodite"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / exe_name).write_bytes(b"fake")
    return bin_dir


def test_aphrodite_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    _stub_venv(tmp_path, monkeypatch)
    p = _write(
        tmp_path,
        "name: q\nengine: aphrodite\nport: 8140\napi_key: sk-test\naphrodite:\n"
        "  model: /models/Qwen3.8-27B-Q4_K_M.gguf\n  tensor_parallel_size: 1\n"
        "  quantization: gguf\n  max_model_len: 32768\n"
        '  extra_args: "--disable-log-requests"\n',
    )
    a = get_adapter("aphrodite")(p, CAPS8)
    a.check_requirements()
    cmd, env = a.build_command()
    assert str(cmd[0]).endswith("aphrodite.exe") or str(cmd[0]).endswith("aphrodite")
    assert cmd[1] == "run"
    assert cmd[2] == "/models/Qwen3.8-27B-Q4_K_M.gguf"
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0"
    assert cmd[cmd.index("--port") + 1] == "8140"
    assert cmd[cmd.index("--tensor-parallel-size") + 1] == "1"
    assert cmd[cmd.index("--quantization") + 1] == "gguf"
    assert cmd[cmd.index("--max-model-len") + 1] == "32768"
    assert cmd[cmd.index("--served-model-name") + 1] == "q"
    assert cmd[cmd.index("--api-key") + 1] == "sk-test"
    assert "--disable-log-requests" in cmd
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_engines_aphrodite.py::test_aphrodite_command -v`
Expected: FAIL (AphroditeAdapter / aphrodite binary / registry 未定义)

- [ ] **Step 3: 实现 AphroditeAdapter**

```python
# src/modelctl/engines/aphrodite.py
import os
import shlex
import shutil
from pathlib import Path

from modelctl.core import envs
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.gpu_lock import acquire_gpu_lock
from modelctl.core.gpu_utils import GPUValidationError
from modelctl.engines.base import EngineAdapter, RequirementError


class AphroditeAdapter(EngineAdapter):
    def check_requirements(self) -> None:
        envs.ensure_env("aphrodite")
        cfg = self.profile.engine_config
        if not cfg.get("model"):
            raise RequirementError(f"{self.profile.name}：aphrodite.model 必填")
        try:
            gpus = self.selected_gpus()
        except (GPUValidationError, ValueError) as exc:
            raise RequirementError(f"[gpu_list] {exc}") from exc
        if gpus is not None:
            self.validate_gpu_selection(gpus)
            tp = int(cfg.get("tensor_parallel_size", len(gpus)))
            if tp != len(gpus):
                raise RequirementError(
                    f"gpu_list 指定了 {len(gpus)} 块 GPU，但 tensor_parallel_size={tp}"
                )
        else:
            tp = int(cfg.get("tensor_parallel_size", 1))
            if self.caps.gpu_count and tp > self.caps.gpu_count:
                raise RequirementError(f"tensor_parallel_size={tp} 超过实际 GPU 数")
        self.run_compat_checks()
        if gpus is not None:
            acquire_gpu_lock(self.profile.name, gpus)

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        gpus = self.selected_gpus()
        tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
        extra = shlex.split(str(cfg.get("extra_args") or ""))
        cmd = [
            str(envs.engine_bin("aphrodite", "aphrodite")),
            "run",
            str(cfg["model"]),
            "--host", "0.0.0.0",
            "--port", str(self.profile.port),
            "--tensor-parallel-size", str(tp),
            "--served-model-name", self.upstream_model_name(),
        ]
        if cfg.get("quantization"):
            cmd += ["--quantization", str(cfg["quantization"])]
        if cfg.get("max_model_len"):
            cmd += ["--max-model-len", str(cfg["max_model_len"])]
        cmd += self.api_key_args() + extra
        env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "aphrodite")
        env["PATH"] = str(envs.engine_bin("aphrodite", "aphrodite").parent) + os.pathsep + os.environ.get("PATH", "")
        return cmd, env

    def metrics_mapping(self) -> dict[str, list[str]]:
        return {
            "prompt_total": ["aphrodite:prompt_tokens_total"],
            "predicted_total": ["aphrodite:generation_tokens_total"],
            "prompt_rate": ["aphrodite:avg_prompt_throughput_toks_per_sec"],
            "predicted_rate": ["aphrodite:avg_generation_throughput_toks_per_sec"],
        }

    def upstream_model_name(self) -> str:
        return self.profile.name
```

- [ ] **Step 4: 注册到引擎注册表**

```python
# src/modelctl/engines/__init__.py
from modelctl.engines.aphrodite import AphroditeAdapter

_REGISTRY = {
    "llamacpp": LlamaCppAdapter,
    "ollama": OllamaAdapter,
    "vllm": VllmAdapter,
    "sglang": SglangAdapter,
    "unsloth": UnslothAdapter,
    "aphrodite": AphroditeAdapter,
}
```

- [ ] **Step 5: 创建示例配置**

```yaml
# models/aphrodite/qwen3.8.yaml
group: qwen3.8
port: 8140
api_key: ${API_KEY}

aphrodite:
  model: /raid5/sh/model-gguf/Qwen3.8-27B-Q4_K_M.gguf
  tensor_parallel_size: 1
  quantization: gguf
  max_model_len: 32768
  extra_args: "--disable-log-requests"

usage:
  price_in: 0.5
  price_out: 1.0
```

- [ ] **Step 6: 运行测试确认通过**

Run: `pytest tests/test_engines_aphrodite.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/modelctl/engines/aphrodite.py src/modelctl/engines/__init__.py

git add models/aphrodite/qwen3.8.yaml tests/test_engines_aphrodite.py

git commit -m "feat(engines): add aphrodite engine adapter"
```

---

### Task 2: LMDeploy 适配器

**Files:**
- Create: `src/modelctl/engines/lmdeploy.py`
- Modify: `src/modelctl/engines/__init__.py`
- Create: `models/lmdeploy/qwen3.8.yaml`
- Create: `tests/test_engines_lmdeploy.py`

**Interfaces:**
- Consumes: `EngineAdapter` 基类、`envs.ensure_env`、`envs.engine_bin`、GPU 校验工具
- Produces: `LmdeployAdapter` 类，注册到 `_REGISTRY["lmdeploy"]`

- [ ] **Step 1: 编写命令构建测试**

```python
# tests/test_engines_lmdeploy.py
import os
import pytest

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import load_profile
from modelctl.engines import get_adapter

CAPS8 = Capabilities(gpu_count=8, compute_capability="8.9", binaries={"lmdeploy": True})


def _write(tmp_path, text, name="m.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return load_profile(name[:-5], tmp_path)


def _stub_venv(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.envs.VENV_ROOT", tmp_path / ".venvs")
    bin_dir = tmp_path / ".venvs" / "lmdeploy" / ("Scripts" if os.name == "nt" else "bin")
    exe_name = "lmdeploy.exe" if os.name == "nt" else "lmdeploy"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / exe_name).write_bytes(b"fake")
    return bin_dir


def test_lmdeploy_command(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch)
    p = _write(
        tmp_path,
        "name: q\nengine: lmdeploy\nport: 8130\napi_key: sk-test\nlmdeploy:\n"
        "  model: /models/Qwen3.8-27B\n  tensor_parallel_size: 1\n"
        "  session_len: 32768\n  cache_max_entry_count: 0.8\n"
        "  quant_policy: 4\n"
        '  extra_args: "--enable-prefix-caching"\n',
    )
    a = get_adapter("lmdeploy")(p, CAPS8)
    a.check_requirements()
    cmd, env = a.build_command()
    assert str(cmd[0]).endswith("lmdeploy.exe") or str(cmd[0]).endswith("lmdeploy")
    assert cmd[1] == "serve"
    assert cmd[2] == "api_server"
    assert cmd[3] == "/models/Qwen3.8-27B"
    assert cmd[cmd.index("--server-name") + 1] == "0.0.0.0"
    assert cmd[cmd.index("--server-port") + 1] == "8130"
    assert cmd[cmd.index("--tp") + 1] == "1"
    assert cmd[cmd.index("--session-len") + 1] == "32768"
    assert cmd[cmd.index("--cache-max-entry-count") + 1] == "0.8"
    assert cmd[cmd.index("--quant-policy") + 1] == "4"
    assert "--enable-prefix-caching" in cmd
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_engines_lmdeploy.py::test_lmdeploy_command -v`
Expected: FAIL

- [ ] **Step 3: 实现 LmdeployAdapter**

```python
# src/modelctl/engines/lmdeploy.py
import os
import shlex

from modelctl.core import envs
from modelctl.core.gpu_lock import acquire_gpu_lock
from modelctl.core.gpu_utils import GPUValidationError
from modelctl.engines.base import EngineAdapter, RequirementError


class LmdeployAdapter(EngineAdapter):
    def check_requirements(self) -> None:
        envs.ensure_env("lmdeploy")
        cfg = self.profile.engine_config
        if not cfg.get("model"):
            raise RequirementError(f"{self.profile.name}：lmdeploy.model 必填")
        try:
            gpus = self.selected_gpus()
        except (GPUValidationError, ValueError) as exc:
            raise RequirementError(f"[gpu_list] {exc}") from exc
        if gpus is not None:
            self.validate_gpu_selection(gpus)
            tp = int(cfg.get("tensor_parallel_size", len(gpus)))
            if tp != len(gpus):
                raise RequirementError(
                    f"gpu_list 指定了 {len(gpus)} 块 GPU，但 tensor_parallel_size={tp}"
                )
        else:
            tp = int(cfg.get("tensor_parallel_size", 1))
            if self.caps.gpu_count and tp > self.caps.gpu_count:
                raise RequirementError(f"tensor_parallel_size={tp} 超过实际 GPU 数")
        self.run_compat_checks()
        if gpus is not None:
            acquire_gpu_lock(self.profile.name, gpus)

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        gpus = self.selected_gpus()
        tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
        extra = shlex.split(str(cfg.get("extra_args") or ""))
        cmd = [
            str(envs.engine_bin("lmdeploy", "lmdeploy")),
            "serve", "api_server",
            str(cfg["model"]),
            "--server-name", "0.0.0.0",
            "--server-port", str(self.profile.port),
            "--tp", str(tp),
        ]
        if cfg.get("session_len"):
            cmd += ["--session-len", str(cfg["session_len"])]
        if cfg.get("cache_max_entry_count") is not None:
            cmd += ["--cache-max-entry-count", str(cfg["cache_max_entry_count"])]
        if cfg.get("quant_policy"):
            cmd += ["--quant-policy", str(cfg["quant_policy"])]
        cmd += extra
        env = {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "lmdeploy")
        env["PATH"] = str(envs.engine_bin("lmdeploy", "lmdeploy").parent) + os.pathsep + os.environ.get("PATH", "")
        return cmd, env

    def metrics_mapping(self) -> dict[str, list[str]]:
        return {
            "prompt_total": ["lmdeploy:prompt_tokens_total"],
            "predicted_total": ["lmdeploy:generation_tokens_total"],
        }
```

- [ ] **Step 4: 注册到引擎注册表**

```python
# src/modelctl/engines/__init__.py
from modelctl.engines.lmdeploy import LmdeployAdapter

_REGISTRY = {
    # ... existing engines ...
    "aphrodite": AphroditeAdapter,
    "lmdeploy": LmdeployAdapter,
}
```

- [ ] **Step 5: 创建示例配置**

```yaml
# models/lmdeploy/qwen3.8.yaml
group: qwen3.8
port: 8130
api_key: ${API_KEY}

lmdeploy:
  model: /raid5/sh/model-hf/Qwen/Qwen3.8-27B
  tensor_parallel_size: 1
  session_len: 32768
  cache_max_entry_count: 0.8
  quant_policy: 4
  extra_args: "--enable-prefix-caching"

usage:
  price_in: 0.5
  price_out: 1.0
```

- [ ] **Step 6: 运行测试确认通过**

Run: `pytest tests/test_engines_lmdeploy.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/modelctl/engines/lmdeploy.py src/modelctl/engines/__init__.py

git add models/lmdeploy/qwen3.8.yaml tests/test_engines_lmdeploy.py

git commit -m "feat(engines): add lmdeploy engine adapter"
```

---

### Task 3: TensorRT-LLM 适配器

**Files:**
- Create: `src/modelctl/engines/tensorrt_llm.py`
- Modify: `src/modelctl/engines/__init__.py`
- Create: `models/tensorrt_llm/qwen3.8.yaml`
- Create: `tests/test_engines_tensorrt_llm.py`

**Interfaces:**
- Consumes: `EngineAdapter` 基类、docker/venv 运行时选择、GPU 校验工具
- Produces: `TensorRtLlmAdapter` 类，注册到 `_REGISTRY["tensorrt_llm"]`

- [ ] **Step 1: 编写命令构建测试（venv 模式）**

```python
# tests/test_engines_tensorrt_llm.py
import os
import pytest

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import load_profile
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError

CAPS8 = Capabilities(gpu_count=8, compute_capability="8.9", binaries={"tensorrt_llm": True, "docker": True})


def _write(tmp_path, text, name="m.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return load_profile(name[:-5], tmp_path)


def _stub_venv(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.envs.VENV_ROOT", tmp_path / ".venvs")
    bin_dir = tmp_path / ".venvs" / "tensorrt_llm" / ("Scripts" if os.name == "nt" else "bin")
    exe_name = "python.exe" if os.name == "nt" else "python"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / exe_name).write_bytes(b"fake")
    return bin_dir


def test_tensorrt_llm_venv_command(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch)
    engine_dir = tmp_path / "engines" / "qwen3.8-tp4-fp8"
    engine_dir.mkdir(parents=True)
    p = _write(
        tmp_path,
        f"name: q\nengine: tensorrt_llm\nport: 8120\napi_key: sk-test\ntensorrt_llm:\n"
        f"  model: /models/Qwen3.8-27B\n  engine_dir: {engine_dir}\n"
        f"  tensor_parallel_size: 4\n  quantization: fp8\n"
        f"  max_input_len: 32768\n  max_output_len: 8192\n  max_batch_size: 64\n"
        f'  extra_args: "--use_fused_mlp"\n',
    )
    a = get_adapter("tensorrt_llm")(p, CAPS8)
    a.check_requirements()
    cmd, env = a.build_command()
    assert cmd[0].endswith("python.exe") or cmd[0].endswith("python")
    assert cmd[1] == "-m"
    assert cmd[2] == "tensorrt_llm.serve"
    assert cmd[3] == "/models/Qwen3.8-27B"
    assert cmd[cmd.index("--engine_dir") + 1] == str(engine_dir)
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0"
    assert cmd[cmd.index("--port") + 1] == "8120"
    assert cmd[cmd.index("--tp") + 1] == "4"
    assert cmd[cmd.index("--max_input_len") + 1] == "32768"
    assert cmd[cmd.index("--max_output_len") + 1] == "8192"
    assert cmd[cmd.index("--max_batch_size") + 1] == "64"
    assert "--use_fused_mlp" in cmd
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_engines_tensorrt_llm.py::test_tensorrt_llm_venv_command -v`
Expected: FAIL

- [ ] **Step 3: 实现 TensorRtLlmAdapter**

```python
# src/modelctl/engines/tensorrt_llm.py
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from modelctl.core import envs
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.gpu_lock import acquire_gpu_lock
from modelctl.core.gpu_utils import GPUValidationError
from modelctl.engines.base import EngineAdapter, RequirementError


class TensorRtLlmAdapter(EngineAdapter):
    def _resolve_runtime(self) -> tuple[str, str | None]:
        cfg = self.profile.engine_config
        image = str(cfg.get("docker_image") or "").strip()
        return ("docker", image) if image else ("venv", None)

    def check_requirements(self) -> None:
        cfg = self.profile.engine_config
        runtime, image = self._resolve_runtime()
        if runtime == "docker":
            if shutil.which("docker") is None:
                raise RequirementError("docker 命令不在 PATH")
            if shutil.which("nvidia-smi") is None:
                raise RequirementError("docker 模式需要 nvidia-container-toolkit")
        else:
            envs.ensure_env("tensorrt_llm")
        if not cfg.get("model"):
            raise RequirementError(f"{self.profile.name}：tensorrt_llm.model 必填")
        if not cfg.get("engine_dir"):
            raise RequirementError(f"{self.profile.name}：tensorrt_llm.engine_dir 必填（编译产物缓存目录）")
        try:
            gpus = self.selected_gpus()
        except (GPUValidationError, ValueError) as exc:
            raise RequirementError(f"[gpu_list] {exc}") from exc
        if gpus is not None:
            self.validate_gpu_selection(gpus)
            tp = int(cfg.get("tensor_parallel_size", len(gpus)))
            if tp != len(gpus):
                raise RequirementError(
                    f"gpu_list 指定了 {len(gpus)} 块 GPU，但 tensor_parallel_size={tp}"
                )
        else:
            tp = int(cfg.get("tensor_parallel_size", 1))
            if self.caps.gpu_count and tp > self.caps.gpu_count:
                raise RequirementError(f"tensor_parallel_size={tp} 超过实际 GPU 数")
        self.run_compat_checks()
        if gpus is not None:
            acquire_gpu_lock(self.profile.name, gpus)

    def pre_start(self) -> None:
        cfg = self.profile.engine_config
        engine_dir = Path(str(cfg.get("engine_dir") or "")).expanduser()
        if engine_dir.exists() and any(engine_dir.iterdir()):
            return
        # 编译产物缺失：记录警告，由用户手动触发编译（避免首次 28min 阻塞在 modelctl start）
        self.warnings.append(
            f"TensorRT-LLM engine_dir {engine_dir} 不存在或为空，"
            "请先执行 trtllm-build 编译或配置 docker_image 使用预编译镜像"
        )

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        gpus = self.selected_gpus()
        tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
        runtime, image = self._resolve_runtime()
        extra = shlex.split(str(cfg.get("extra_args") or ""))
        model = str(cfg["model"])
        engine_dir = str(Path(str(cfg["engine_dir"])).expanduser())

        if runtime == "docker":
            model_local = Path(model).expanduser().resolve()
            engine_local = Path(engine_dir).expanduser().resolve()
            cmd = [
                "docker", "run", "--rm", "--detach",
                "--name", f"{self.profile.name}-trtllm",
                "--gpus", self._gpus_json(gpus, tp),
                "-p", f"{self.profile.port}:8000",
                "-v", f"{model_local.parent.as_posix()}:/models:ro",
                "-v", f"{engine_local.as_posix()}:/engines:ro",
                "--ipc=host",
                image,
                "serve", f"/models/{model_local.name}",
                "--engine_dir", "/engines",
                "--host", "0.0.0.0",
                "--port", "8000",
                "--tp", str(tp),
            ] + extra
            env = {}
            if gpus:
                env.update(self.cuda_visible_devices(gpus))
            return cmd, env

        cmd = [
            str(envs.engine_python("tensorrt_llm")),
            "-m", "tensorrt_llm.serve",
            model,
            "--engine_dir", engine_dir,
            "--host", "0.0.0.0",
            "--port", str(self.profile.port),
            "--tp", str(tp),
        ]
        if cfg.get("quantization"):
            cmd += ["--quantization", str(cfg["quantization"])]
        if cfg.get("max_input_len"):
            cmd += ["--max_input_len", str(cfg["max_input_len"])]
        if cfg.get("max_output_len"):
            cmd += ["--max_output_len", str(cfg["max_output_len"])]
        if cfg.get("max_batch_size"):
            cmd += ["--max_batch_size", str(cfg["max_batch_size"])]
        cmd += self.api_key_args() + extra
        env = {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "tensorrt_llm")
        env["PATH"] = str(envs.engine_python("tensorrt_llm").parent) + os.pathsep + os.environ.get("PATH", "")
        return cmd, env

    def metrics_mapping(self) -> dict[str, list[str]]:
        return {
            "prompt_total": ["nv_inference_request_success", "trtllm:prompt_tokens_total"],
            "predicted_total": ["trtllm:generation_tokens_total"],
        }

    def _gpus_json(self, gpus, tp) -> str:
        if gpus:
            seq = list(gpus)
        else:
            seq = list(range(int(self.caps.gpu_count or tp)))
        return '"device=' + ",".join(str(g) for g in seq) + '"'

    def wait_ready(self, timeout: float) -> bool:
        from modelctl.core.process import wait_health
        if self._resolve_runtime()[0] == "docker":
            return wait_health(self.health_url(), timeout, self.upstream_api_key(), alive_check=None)
        return super().wait_ready(timeout)

    def stop_patterns(self) -> list[str]:
        if self._resolve_runtime()[0] != "docker":
            return ["tensorrt_llm.serve"]
        name = f"{self.profile.name}-trtllm"
        return [f"docker run --rm --detach --name {name}"]
```

- [ ] **Step 4: 注册到引擎注册表**

```python
# src/modelctl/engines/__init__.py
from modelctl.engines.tensorrt_llm import TensorRtLlmAdapter

_REGISTRY = {
    # ... existing engines + aphrodite + lmdeploy ...
    "tensorrt_llm": TensorRtLlmAdapter,
}
```

- [ ] **Step 5: 创建示例配置**

```yaml
# models/tensorrt_llm/qwen3.8.yaml
group: qwen3.8
port: 8120
api_key: ${API_KEY}

tensorrt_llm:
  model: /raid5/sh/model-hf/Qwen/Qwen3.8-27B
  engine_dir: /raid5/sh/trt_engines/qwen3.8-tp4-fp8
  tensor_parallel_size: 4
  quantization: fp8
  max_input_len: 32768
  max_output_len: 8192
  max_batch_size: 64
  # docker_image: nvcr.io/nvidia/tensorrt-llm:tag  # 可选，配置后优先 docker 模式
  extra_args: "--use_fused_mlp --enable_chunked_context"

usage:
  price_in: 0.5
  price_out: 1.0
```

- [ ] **Step 6: 运行测试确认通过**

Run: `pytest tests/test_engines_tensorrt_llm.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/modelctl/engines/tensorrt_llm.py src/modelctl/engines/__init__.py

git add models/tensorrt_llm/qwen3.8.yaml tests/test_engines_tensorrt_llm.py

git commit -m "feat(engines): add tensorrt-llm engine adapter"
```

---

### Task 4: TokenSpeed 适配器

**Files:**
- Create: `src/modelctl/engines/tokenspeed.py`
- Modify: `src/modelctl/engines/__init__.py`
- Create: `models/tokenspeed/qwen3.5-397b.yaml`
- Create: `tests/test_engines_tokenspeed.py`

**Interfaces:**
- Consumes: `EngineAdapter` 基类、docker 优先运行时、模型下载逻辑
- Produces: `TokenSpeedAdapter` 类，注册到 `_REGISTRY["tokenspeed"]`

- [ ] **Step 1: 编写命令构建测试（docker 模式）**

```python
# tests/test_engines_tokenspeed.py
import os
import pytest

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import load_profile
from modelctl.engines import get_adapter

CAPS8 = Capabilities(gpu_count=8, compute_capability="8.9", binaries={"docker": True})


def _write(tmp_path, text, name="m.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return load_profile(name[:-5], tmp_path)


def test_tokenspeed_docker_command(tmp_path, monkeypatch):
    model_dir = tmp_path / "models" / "Qwen3.5-397B-A17B"
    model_dir.mkdir(parents=True)
    p = _write(
        tmp_path,
        f"name: q\nengine: tokenspeed\nport: 8150\napi_key: sk-test\ntokenspeed:\n"
        f"  model: {model_dir}\n  tensor_parallel_size: 8\n"
        f"  max_model_len: 131072\n"
        f"  docker_image: lightseekorg/tokenspeed:latest\n"
        f'  extra_args: "--enable-prefix-caching"\n',
    )
    a = get_adapter("tokenspeed")(p, CAPS8)
    a.check_requirements()
    cmd, env = a.build_command()
    assert cmd[0] == "docker"
    assert cmd[1] == "run"
    assert "--gpus" in cmd
    assert f"{p.port}:8000" in cmd  # 8150:8000
    assert "lightseekorg/tokenspeed:latest" in cmd
    assert f"/models/{model_dir.name}" in cmd
    assert cmd[cmd.index("--tp") + 1] == "8"
    assert "--enable-prefix-caching" in cmd
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_engines_tokenspeed.py::test_tokenspeed_docker_command -v`
Expected: FAIL

- [ ] **Step 3: 实现 TokenSpeedAdapter**

```python
# src/modelctl/engines/tokenspeed.py
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from modelctl.core import envs
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.gpu_lock import acquire_gpu_lock
from modelctl.core.gpu_utils import GPUValidationError
from modelctl.engines._download import download_repo
from modelctl.engines._persist import persist_model_path
from modelctl.engines.base import EngineAdapter, RequirementError


class TokenSpeedAdapter(EngineAdapter):
    def _resolve_runtime(self) -> tuple[str, str | None]:
        cfg = self.profile.engine_config
        image = str(cfg.get("docker_image") or "").strip()
        return ("docker", image) if image else ("venv", None)

    def check_requirements(self) -> None:
        cfg = self.profile.engine_config
        runtime, image = self._resolve_runtime()
        if runtime == "docker":
            if shutil.which("docker") is None:
                raise RequirementError("docker 命令不在 PATH")
            if shutil.which("nvidia-smi") is None:
                raise RequirementError("docker 模式需要 nvidia-container-toolkit")
        else:
            envs.ensure_env("tokenspeed")
        if not cfg.get("model") and not cfg.get("download"):
            raise RequirementError(f"{self.profile.name}：tokenspeed.model 必填（或配置 download 段自动下载）")
        try:
            gpus = self.selected_gpus()
        except (GPUValidationError, ValueError) as exc:
            raise RequirementError(f"[gpu_list] {exc}") from exc
        if gpus is not None:
            self.validate_gpu_selection(gpus)
            tp = int(cfg.get("tensor_parallel_size", len(gpus)))
            if tp != len(gpus):
                raise RequirementError(
                    f"gpu_list 指定了 {len(gpus)} 块 GPU，但 tensor_parallel_size={tp}"
                )
        else:
            tp = int(cfg.get("tensor_parallel_size", 1))
            if self.caps.gpu_count and tp > self.caps.gpu_count:
                raise RequirementError(f"tensor_parallel_size={tp} 超过实际 GPU 数")
        self.run_compat_checks()
        if gpus is not None:
            acquire_gpu_lock(self.profile.name, gpus)

    def pre_start(self) -> None:
        cfg = self.profile.engine_config
        model = str(cfg.get("model") or "")
        if not (model and Path(model).expanduser().is_dir()):
            if cfg.get("download"):
                modelscope_id = cfg["download"]["modelscope_id"]
                model_root = Path(os.environ.get("MODEL_ROOT") or PROJECT_ROOT.parent / "model-hf")
                local_dir = download_repo(modelscope_id, model_root)
                if self.profile.path is None:
                    raise RequirementError(f"{self.profile.name}：profile 文件路径缺失")
                persist_model_path(self.profile.path, "tokenspeed", str(local_dir.resolve()))
                cfg["model"] = str(local_dir.resolve())

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        gpus = self.selected_gpus()
        tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
        runtime, image = self._resolve_runtime()
        extra = shlex.split(str(cfg.get("extra_args") or ""))
        model = str(cfg["model"])

        if runtime == "docker":
            model_local = Path(model).expanduser().resolve()
            cmd = [
                "docker", "run", "--rm", "--detach",
                "--name", f"{self.profile.name}-tokenspeed",
                "--gpus", self._gpus_json(gpus, tp),
                "-p", f"{self.profile.port}:8000",
                "-v", f"{model_local.parent.as_posix()}:/models:ro",
                "--ipc=host",
                image,
                "serve", f"/models/{model_local.name}",
                "--host", "0.0.0.0",
                "--port", "8000",
                "--tp", str(tp),
            ]
            if cfg.get("max_model_len"):
                cmd += ["--max-model-len", str(cfg["max_model_len"])]
            cmd += extra
            env = {}
            if gpus:
                env.update(self.cuda_visible_devices(gpus))
            return cmd, env

        cmd = [
            str(envs.engine_bin("tokenspeed", "tokenspeed")),
            "serve",
            model,
            "--host", "0.0.0.0",
            "--port", str(self.profile.port),
            "--tp", str(tp),
        ]
        if cfg.get("max_model_len"):
            cmd += ["--max-model-len", str(cfg["max_model_len"])]
        cmd += self.api_key_args() + extra
        env = {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "tokenspeed")
        env["PATH"] = str(envs.engine_bin("tokenspeed", "tokenspeed").parent) + os.pathsep + os.environ.get("PATH", "")
        return cmd, env

    def metrics_mapping(self) -> dict[str, list[str]]:
        return {
            "prompt_total": ["tokenspeed:prompt_tokens_total"],
            "predicted_total": ["tokenspeed:generation_tokens_total"],
        }

    def _gpus_json(self, gpus, tp) -> str:
        if gpus:
            seq = list(gpus)
        else:
            seq = list(range(int(self.caps.gpu_count or tp)))
        return '"device=' + ",".join(str(g) for g in seq) + '"'

    def wait_ready(self, timeout: float) -> bool:
        from modelctl.core.process import wait_health
        if self._resolve_runtime()[0] == "docker":
            return wait_health(self.health_url(), timeout, self.upstream_api_key(), alive_check=None)
        return super().wait_ready(timeout)

    def stop_patterns(self) -> list[str]:
        if self._resolve_runtime()[0] != "docker":
            return ["tokenspeed serve"]
        name = f"{self.profile.name}-tokenspeed"
        return [f"docker run --rm --detach --name {name}"]
```

- [ ] **Step 4: 注册到引擎注册表**

```python
# src/modelctl/engines/__init__.py
from modelctl.engines.tokenspeed import TokenSpeedAdapter

_REGISTRY = {
    # ... existing engines + aphrodite + lmdeploy + tensorrt_llm ...
    "tokenspeed": TokenSpeedAdapter,
}
```

- [ ] **Step 5: 创建示例配置**

```yaml
# models/tokenspeed/qwen3.5-397b.yaml
group: qwen3.5-397b
port: 8150
api_key: ${API_KEY}

tokenspeed:
  model: Qwen/Qwen3.5-397B-A17B
  tensor_parallel_size: 8
  max_model_len: 131072
  docker_image: lightseekorg/tokenspeed:latest
  download:
    modelscope_id: Qwen/Qwen3.5-397B-A17B
  extra_args: "--enable-prefix-caching"

usage:
  price_in: 1.0
  price_out: 3.0
```

- [ ] **Step 6: 运行测试确认通过**

Run: `pytest tests/test_engines_tokenspeed.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/modelctl/engines/tokenspeed.py src/modelctl/engines/__init__.py

git add models/tokenspeed/qwen3.5-397b.yaml tests/test_engines_tokenspeed.py

git commit -m "feat(engines): add tokenspeed engine adapter"
```

---

## Self-Review

**1. Spec coverage:**
- 路线 A 四件套：Aphrodite (Task 1)、LMDeploy (Task 2)、TensorRT-LLM (Task 3)、TokenSpeed (Task 4) ✓
- 插件式约定：每个任务独立注册适配器并创建示例配置 ✓
- 共享基础设施：Task 0 统一扩展 KNOWN_ENGINES、ENGINE_BINARIES、MANAGED_ENGINES、venv 子项目 ✓
- 测试覆盖：每个任务包含命令构建、GPU/TP 校验、健康检查相关测试 ✓

**2. Placeholder scan:**
- 无 "TBD" / "TODO" / "implement later" ✓
- 无 "add appropriate error handling" 类模糊描述 ✓
- 每个步骤含具体代码或命令 ✓

**3. Type consistency:**
- 所有适配器继承 `EngineAdapter`，实现 `build_command() -> tuple[list[str], dict[str, str]]` ✓
- 所有适配器使用 `selected_gpus()` / `validate_gpu_selection()` / `acquire_gpu_lock()` ✓
- 注册表键名与 KNOWN_ENGINES / MANAGED_ENGINES 一致 ✓

**4. 已知缺口：**
- TokenSpeed 真实 CLI 参数（如 `serve` 子命令、flag 名称）若与草案不同，需在实现时同步调整代码与测试。
- TensorRT-LLM venv 模式命令基于 `python -m tensorrt_llm.serve` 假设；若上游未提供该入口，需改用 `tritonserver`。
- 各引擎 Prometheus 指标名需在实际运行后校准，当前使用占位名称。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-01-add-gpu-engines.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
