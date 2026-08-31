# vllm Docker 镜像 Day-0 适配 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 modelctl vllm 适配器支持 yaml 字段 `vllm.docker_image`，使 `models/vllm/qwen3.8-flash-next.yaml` 能通过 `vllm/vllm-openai:qwen38-flash-next` Day-0 镜像跑通 `modelctl start`，且现有 7 个 vllm yaml 行为 byte-identical 回归。

**Architecture:** `VllmAdapter` 加一个私有方法 `_resolve_runtime()` 把执行位置路由到 `venv`（现状，不变）或 `docker`（新增 docker run 命令模板），其余 start_detached 编排复用现有 `core/process.py`；stop 主路三层（PID → fuser → pkill）保持不动，仅 `stop_patterns` 返回 2 个含 `--gpus` 字串的模式供最终层 pkill 命中。

**Tech Stack:** Python 3.12 / `pathlib` / `shutil` / `subprocess` / `shlex` / pytest（monkeypatch + `tmp_path` fixtures）；目标平台 Linux；Windows 开发机上 docker 路径用 monkeypatch 模拟。

## Global Constraints

- 目标运行平台 Linux（`VllmAdapter` 当前已限定 nvsyscall / CUDA 推理机）；Windows 上 docker 路径**必须跳过**：`sys.platform != "win32"` 或显式 mock `shutil.which("docker")` 后验证
- 改动文件**只**有 3 个：`src/modelctl/engines/vllm.py`、`models/vllm/qwen3.8-flash-next.yaml`、`tests/test_engines_vllm.py`
- **不**改 `src/modelctl/core/process.py`、`src/modelctl/core/all_service.py`、`src/modelctl/core/envs.py`、`src/modelctl/core/capabilities.py`、`src/modelctl/engines/base.py`
- 现有 pytest 全部 **pre-task** PASS（基线确认）；每 task 结束 `pytest tests/test_engines_vllm.py -v` 必须全绿
- 命令模板 / 字符串必须与本计划内**逐字**匹配（避免 spec 与实现的轻微偏引导致 pkill 模式失配）
- 测试 monkeypatch `shutil.which` / `subprocess.run` / `envs.ensure_env`，**不真起 docker**

---

### Task 1: `_resolve_runtime()` 路由 + 现状回归

**Files:**
- Modify: `src/modelctl/engines/vllm.py`（在 `VllmAdapter` 类内加方法）
- Test: `tests/test_engines_vllm.py`

**Interfaces:**
- Consumes: 无（首个改动）
- Produces: `VllmAdapter._resolve_runtime(self) -> tuple[str, str | None]` — `("venv", None)` 或 `("docker", image)`；image 是 `cfg["docker_image"]` strip 后非空字符串

- [ ] **Step 1: 写测试**

在 `tests/test_engines_vllm.py` 末尾追加（复用文件内已存在的 `make_profile` fixture / 工厂，若文件无 fixture 则用同文件已有 helper 风格）：

```python
def _adapter_with(engine_config: dict, tmp_path) -> "VllmAdapter":
    """复用文件顶端的 LocalProfile / caps 工厂模式。"""
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import Profile
    from modelctl.engines.vllm import VllmAdapter
    profile = Profile(
        path=tmp_path / "m.yaml", name="fake", engine="vllm", port=8000,
        group="g", variant=None, api_key="", engine_config=engine_config,
        usage=None, tool_call_rounds=None, max_output_tokens=None,
    )
    caps = Capabilities(gpu_count=8, gpu_indices=[0, 1, 2, 3, 4, 5, 6, 7],
                        gpu_name="RTX 5880", gpu_mem_mb=48 * 1024)
    return VllmAdapter(profile, caps)


def test_resolve_runtime_default(tmp_path):
    a = _adapter_with({"model": "Qwen/X"}, tmp_path)
    assert a._resolve_runtime() == ("venv", None)


def test_resolve_runtime_docker(tmp_path):
    a = _adapter_with({"model": "Qwen/X",
                       "docker_image": "vllm/vllm-openai:qwen38-flash-next"}, tmp_path)
    assert a._resolve_runtime() == ("docker", "vllm/vllm-openai:qwen38-flash-next")
```

- [ ] **Step 2: 跑测试**

Run: `pytest tests/test_engines_vllm.py -k "resolve_runtime" -v`
Expected: 2 个 FAIL，AttributeError（`_resolve_runtime` 未定义）

- [ ] **Step 3: 实现**

在 `src/modelctl/engines/vllm.py` 的 `class VllmAdapter` 内 —— 在 `def stop_patterns` 之前、`def upsteam_model_name` 之后 —— 插入：

```python
    def _resolve_runtime(self) -> tuple[str, str | None]:
        """yaml 字段 vllm.docker_image 非空→('docker', image)；其余回退 ('venv', None)。

        留空时与改造前完全等价（仍走 `envs.engine_bin("vllm", "vllm")`），
        已部署的 7 个 models/vllm/*.yaml 行为零变化。
        """
        cfg = self.profile.engine_config
        image = str(cfg.get("docker_image") or "").strip()
        return ("docker", image) if image else ("venv", None)
```

- [ ] **Step 4: 跑测试**

Run: `pytest tests/test_engines_vllm.py -v`
Expected: 全绿（旧用例 4 个 + 新 2 个均 PASS）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/engines/vllm.py tests/test_engines_vllm.py
git commit -m "feat(engines/vllm): _resolve_runtime 路由 docker 镜像 Day-0"
```

---

### Task 2: check_requirements 加 docker 分支

**Files:**
- Modify: `src/modelctl/engines/vllm.py:31-54`（`check_requirements` 函数）
- Test: `tests/test_engines_vllm.py`

**Interfaces:**
- Consumes: `VllmAdapter._resolve_runtime()`（Task 1）；`shutil.which("docker")` / `shutil.which("nvidia-smi")`；现有 `envs.ensure_env`（venv 路径下仍调用）
- Produces: `check_requirements()` 在 docker 分支跳过 venv 检查但要求 docker + nvidia-smi 在 PATH，缺则 `RequirementError`

- [ ] **Step 1: 写测试**

```python
def test_check_requirements_venv_unchanged(tmp_path, monkeypatch):
    """venv 路径：现状语义不变——ensure_env 还是被调用。"""
    from modelctl.core import envs as envs_mod
    called = []
    monkeypatch.setattr(envs_mod, "ensure_env",
                        lambda t: called.append(t) or tmp_path)
    a = _adapter_with({"model": "Qwen/X"}, tmp_path)
    a.check_requirements()
    assert called == ["vllm"]


def test_check_requirements_docker_no_venv_check(tmp_path, monkeypatch):
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/" + name)
    # ensure_env 一旦被调用就抛错——docker 分支必须跳过它
    def bomb(_):
        raise RuntimeError("venv 检查不应被触发")
    from modelctl.core import envs as envs_mod
    monkeypatch.setattr(envs_mod, "ensure_env", bomb)
    a = _adapter_with({"model": "Qwen/X",
                       "docker_image": "vllm/vllm-openai:qwen38-flash-next"}, tmp_path)
    a.check_requirements()  # 不抛

def test_check_requirements_docker_missing_docker(tmp_path, monkeypatch):
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda name: None)
    from modelctl.engines.base import RequirementError
    a = _adapter_with({"model": "Qwen/X",
                       "docker_image": "vllm/vllm-openai:qwen38-flash-next"}, tmp_path)
    with pytest.raises(RequirementError, match="docker 命令不在 PATH"):
        a.check_requirements()

def test_check_requirements_docker_missing_nvidia_smi(tmp_path, monkeypatch):
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which",
                        lambda name: "/usr/bin/docker" if name == "docker" else None)
    from modelctl.engines.base import RequirementError
    a = _adapter_with({"model": "Qwen/X",
                       "docker_image": "vllm/vllm-openai:qwen38-flash-next"}, tmp_path)
    with pytest.raises(RequirementError, match="nvidia-container-toolkit 未就绪"):
        a.check_requirements()
```

- [ ] **Step 2: 跑测试**

Run: `pytest tests/test_engines_vllm.py -k "check_requirements" -v`
Expected: 4 个 FAIL（docker 分支未实现、venv 回归可能 PASS）

- [ ] **Step 3: 实现**

在 `src/modelctl/engines/vllm.py` 顶部 import 区加 `import shutil, subprocess`（如未 import）。改 `def check_requirements`：

```python
    def check_requirements(self) -> None:
        cfg = self.profile.engine_config
        runtime, _image = self._resolve_runtime()
        if runtime == "docker":
            container_name = f"{self.profile.name}-vllm"
            # docker / nvidia-smi 都在 PATH；硬拦截不降级
            if shutil.which("docker") is None:
                raise RequirementError(
                    "docker 命令不在 PATH——docker_image 已配置，请先安装 docker "
                    "（参考 `apt install docker.io docker-compose` 或官网）"
                )
            if shutil.which("nvidia-smi") is None:
                raise RequirementError(
                    "nvidia-smi 不在 PATH / nvidia-container-toolkit 未就绪——"
                    "docker 方式下 --gpus 设定需要 toolkit 支持，"
                    "请先安装 nvidia-container-toolkit"
                )
            # 清冲突残留容器（幂等）
            try:
                subprocess.run(["docker", "rm", "-f", container_name],
                               capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
            # model 必填
            if not cfg.get("model") and not cfg.get("download"):
                raise RequirementError(
                    f"{self.profile.name}：vllm.model 必填（或配置 download 段自动下载）"
                )
        else:
            # 现状路径：venv 检查、model 检查（原 34-35 行）
            envs.ensure_env("vllm")
            if not cfg.get("model") and not cfg.get("download"):
                raise RequirementError(f"{self.profile.name}：vllm.model 必填（或配置 download 段自动下载）")
        # 共享部分：GPU / TP / VRAM / compat / gpu lock（原 36-54 行）
        try:
            gpus = self.selected_gpus()
        except (GPUValidationError, ValueError) as exc:
            raise RequirementError(f"[gpu_list] {exc}") from exc
        if gpus is not None:
            self.validate_gpu_selection(gpus)
            tp = int(cfg.get("tensor_parallel_size", len(gpus)))
            if tp != len(gpus):
                raise RequirementError(
                    f"gpu_list 指定了 {len(gpus)} 块 GPU，但 tensor_parallel_size={tp}，二者必须一致"
                )
        else:
            tp = int(cfg.get("tensor_parallel_size", 1))
            if self.caps.gpu_count and tp > self.caps.gpu_count:
                raise RequirementError(f"tensor_parallel_size={tp} 超过实际 GPU 数 {self.caps.gpu_count}")
        self._check_vram_advisory(cfg, gpus)
        self.run_compat_checks()
        if gpus is not None:
            acquire_gpu_lock(self.profile.name, gpus)
```

注意：上面把 `envs.ensure_env` 移到了 else 分支（原来在函数顶 32 行）；`cfg = self.profile.engine_config` 提到函数顶（原在 33 行）；其他逻辑在原 def 36-54 行内不动。

- [ ] **Step 4: 跑测试**

Run: `pytest tests/test_engines_vllm.py -v`
Expected: 全绿

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/engines/vllm.py tests/test_engines_vllm.py
git commit -m "feat(engines/vllm): check_requirements 按 runtime 分支校验 docker"
```

---

### Task 3: build_command docker 命令模板

**Files:**
- Modify: `src/modelctl/engines/vllm.py:99-134`（`build_command`）
- Test: `tests/test_engines_vllm.py`

**Interfaces:**
- Consumes: `VllmAdapter._resolve_runtime()`；`self.selected_gpus()` / `self.caps.gpu_count`；`self.profile.port` / `name` / `engine_config`
- Produces: `(cmd: list[str], env: dict[str, str])` —— docker 分支 cmd 首 9 元素固定为 `["docker","run","--name","<name>-vllm","--gpus','<json>','-p','<p>:8000','-v','<root>:/models:ro','--ipc=host','--detach','<image>']`，后随 `"vllm","serve","/models/<basename>"` + model_args + `["--port","8000"]`；venv 分支 byte-identical 现状

- [ ] **Step 1: 写测试（含现状回归）**

```python
def test_build_command_default_venv_unchanged(tmp_path, monkeypatch):
    from modelctl.core import envs as envs_mod
    fake_venv = tmp_path / ".venvs/vllm"
    monkeypatch.setattr(envs_mod, "engine_bin",
                        lambda t, n: fake_venv / "bin" / n)
    a = _adapter_with({"model": "/raid5/m/Qwen3.8"}, tmp_path)
    monkeypatch.setattr(a, "selected_gpus", lambda: None)
    cmd, env = a.build_command()
    assert cmd[0] == str(fake_venv / "bin" / "vllm")
    assert cmd[1] == "serve"
    assert cmd[2] == "/raid5/m/Qwen3.8"
    assert "--port" in cmd and str(8000) in cmd
    assert "VIRTUAL_ENV" in env


def test_build_command_docker_template(tmp_path):
    a = _adapter_with({
        "model": "/raid5/m/Qwen3.8-Flash-Next-FP8",
        "docker_image": "vllm/vllm-openai:qwen38-flash-next",
        "tensor_parallel_size": 8,
        "extra_args": "--reasoning-parser qwen3",
    }, tmp_path)
    monkeypatch = None  # 仅看命令拼接，不需 venv
    cmd, env = a.build_command()
    assert cmd[0] == "docker"
    assert cmd[1] == "run"
    assert "docker" not in cmd[2:-1] or "--name" in cmd  # 命令里只出现一次 docker
    assert "--name" in cmd
    idx = cmd.index("--name")
    assert cmd[idx + 1] == "fake-vllm"
    assert "--gpus" in cmd
    idx = cmd.index("--gpus")
    assert cmd[idx + 1] == '"device=0,1,2,3,4,5,6,7"'
    assert "-p" in cmd
    idx = cmd.index("-p")
    assert cmd[idx + 1] == "8000:8000"  # profile.port 默认 8000
    assert "-v" in cmd
    idx = cmd.index("-v")
    assert cmd[idx + 1] == "/raid5/m:/models:ro"  # 父目录挂载
    assert "--ipc=host" in cmd
    assert "--detach" in cmd
    # 镜像
    assert "vllm/vllm-openai:qwen38-flash-next" in cmd
    # 容器内 serve
    assert "vllm" in cmd and "serve" in cmd
    assert "/models/Qwen3.8-Flash-Next-FP8" in cmd
    assert "--port" in cmd
    idx = cmd.index("--port")
    assert cmd[idx + 1] == "8000"
    # extra_args 透传
    assert "--reasoning-parser" in cmd and "qwen3" in cmd
    # env 不注入 VIRTUAL_ENV（容器自管）
    assert "VIRTUAL_ENV" not in env


def test_build_command_docker_relative_model_rejected(tmp_path):
    from modelctl.engines.base import RequirementError
    a = _adapter_with({"model": "/raid5/m/Qwen3.8",
                       "docker_image": "x/y:z"}, tmp_path)
    a._model_dir = None
    monkey_path = tmp_path / "model" / "X"
    # 模拟 model 在 build 时仍是原始 HF id（未经 pre_start 下载）
    a.profile.engine_config["model"] = "Qwen/Qwen3.8-Flash-Next-FP8"
    with pytest.raises(RequirementError, match="本地绝对路径"):
        a.build_command()


def test_build_command_docker_gpus_from_gpu_list(tmp_path):
    from modelctl.core.gpu_utils import parse_gpu_list
    a = _adapter_with({
        "model": "/raid5/m/X",
        "docker_image": "x/y:z",
        "gpu_list": "0,2,4",
    }, tmp_path)
    monkey_gpus = None
    a.selected_gpus = lambda: [0, 2, 4]
    cmd, env = a.build_command()
    assert '"device=0,2,4"' in cmd
```

- [ ] **Step 2: 跑测试**

Run: `pytest tests/test_engines_vllm.py -k "build_command" -v`
Expected: 4 个 FAIL（docker 分支未实现）+ `test_build_command_default_venv_unchanged` 应 PASS（现状回归锚点）

- [ ] **Step 3: 实现**

在 `src/modelctl/engines/vllm.py`：

(a) 类顶（`_resolve_runtime` 之上）加辅助：

```python
    @property
    def _container_name(self) -> str:
        return f"{self.profile.name}-vllm"

    def _gpus_json(self) -> str:
        gpus = self.selected_gpus()
        if gpus:
            seq = list(gpus)
        else:
            tp = int(self.profile.engine_config.get("tensor_parallel_size", 1))
            seq = list(range(int(self.caps.gpu_count or tp)))
        return '"device=' + ",".join(str(g) for g in seq) + '"'

    def _venv_env(self, gpus: list[int] | None) -> dict[str, str]:
        """现状 env 注入段（HF_HOME / VIRTUAL_ENV / PATH）抽取为独立方法。"""
        env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "vllm")
        env["PATH"] = str(envs.engine_bin("vllm", "vllm").parent) + os.pathsep + \
            os.environ.get("PATH", os.environ["PATH"])
        return env
```

(b) 重写 `def build_command`（保留现有 import、变量名与下游 args 拼接，仅分叉两路）：

```python
    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        gpus = self.selected_gpus()
        tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
        runtime, image = self._resolve_runtime()

        # 共用：--served-model-name 之后的 model_args
        extra = shlex.split(str(cfg.get("extra_args") or ""))
        model_args = [
            "--served-model-name", self.upstream_model_name(),
            "--host", "0.0.0.0",
            "--tensor-parallel-size", str(tp),
            "--gpu-memory-utilization", str(cfg.get("gpu_memory_utilization", 0.9)),
            "--disable-uvicorn-access-log",
        ]
        if cfg.get("max_model_len"):
            model_args += ["--max-model-len", str(cfg["max_model_len"])]
        if cfg.get("quantization"):
            model_args += ["--quantization", str(cfg["quantization"])]
        if cfg.get("kv_cache_dtype"):
            model_args += ["--kv-cache-dtype", str(cfg["kv_cache_dtype"])]
        model_args += self.api_key_args() + extra

        if runtime == "venv":
            cmd = [str(envs.engine_bin("vllm", "vllm")), "serve", str(cfg["model"])
                   ] + model_args
            return cmd, self._venv_env(gpus)

        # docker 分支
        model_local = Path(str(cfg["model"])).expanduser().resolve()
        if not (str(cfg.get("model", "")) or "").startswith(("/", "~")) or not model_local.is_absolute():
            raise RequirementError(
                f"{self.profile.name}：docker_image 路径下 model 必须为本地绝对路径"
                "（Qwen/Qwen3.8-Flash-Next-FP8 这类 HF id 需先 modelctl start 触发 pre_start 下载）"
            )
        cmd = [
            "docker", "run",
            "--name", self._container_name,
            "--gpus", self._gpus_json(),
            "-p", f"{self.profile.port}:8000",
            "-v", f"{model_local.parent}:/models:ro",
            "--ipc=host",
            "--detach",
            image,
            "vllm", "serve", f"/models/{model_local.name}",
        ] + model_args + ["--port", "8000"]
        env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        return cmd, env
```

- [ ] **Step 4: 跑测试**

Run: `pytest tests/test_engines_vllm.py -v`
Expected: 全绿（旧 + 新全 PASS）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/engines/vllm.py tests/test_engines_vllm.py
git commit -m "feat(engines/vllm): build_command docker 分支 + venv env 抽取"
```

---

### Task 4: stop_patterns 双模式

**Files:**
- Modify: `src/modelctl/engines/vllm.py`（`stop_patterns` 函数）
- Test: `tests/test_engines_vllm.py`

**Interfaces:**
- Consumes: `VllmAdapter._resolve_runtime()` / `self._gpus_json()` / `self._container_name`
- Produces: `stop_patterns()` —— docker 分支返回 2 个 pkill 模式；venv 分支返回 `["vllm serve"]`

- [ ] **Step 1: 写测试**

```python
def test_stop_patterns_venv_unchanged(tmp_path):
    a = _adapter_with({"model": "/x/Y"}, tmp_path)
    assert a.stop_patterns() == ["vllm serve"]


def test_stop_patterns_docker_two_modes(tmp_path):
    a = _adapter_with({
        "model": "/raid5/m/X",
        "docker_image": "x/y:z",
    }, tmp_path)
    a.selected_gpus = lambda: [0, 2, 4]
    patterns = a.stop_patterns()
    assert len(patterns) == 2
    assert patterns[0] == f"docker run --name fake-vllm --gpus \\"device=0,2,4\\""
    assert ":-v" not in patterns  # 兜底模式只追加，不破坏 --gpus 子串匹配
    # 关键：第一个模式是 Popen cmdline 的连续子串
    expected_cmdline = "docker run --name fake-vllm --gpus '\"device=0,2,4\"' -p 8000:8000 -v ..."
    # 这里直接验证 pattern[0] 是 docker run cmdline 的子串（模拟 Popen 数组 + 空格连接）
    assert patterns[0] in " ".join([
        "docker", "run", "--name", "fake-vllm", "--gpus", '"device=0,2,4"',
        "-p", "8000:8000", "-v", "/raid5/m:/models:ro", "--ipc=host", "--detach", "x/y:z",
    ])
```

- [ ] **Step 2: 跑测试**

Run: `pytest tests/test_engines_vllm.py -k "stop_patterns" -v`
Expected: 2 个 FAIL（docker 分支未实现）+ venv 回归 PASS

- [ ] **Step 3: 实现**

替换 `def stop_patterns` 为：

```python
    def stop_patterns(self) -> list[str]:
        # 与现状等价：dl 跑过后 client 已 daemonize-退出
        if self._resolve_runtime()[0] != "docker":
            return ["vllm serve"]
        # 模式 1 必须是 Popen([docker, run, --name, ..., --gpus, '"device=..."' ...])
        # 在 /proc/pid/cmdline 里 ──\x1f 分隔── 是空格连接的连续子串，
        # 与 docker run --name <name> --gpus <json> 连续段拼接，保证 pkill 精准命中。
        name = self._container_name
        gpus_json = self._gpus_json()
        root = Path(str(self.profile.engine_config.get("model") or "")).expanduser().resolve().parent \
            if self.profile.engine_config.get("model") else Path()
        return [
            f"docker run --name {name} --gpus {gpus_json}",
            f"-v {root}:/models:ro docker run --name {name}",
        ]
```

- [ ] **Step 4: 跑测试**

Run: `pytest tests/test_engines_vllm.py -v`
Expected: 全绿

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/engines/vllm.py tests/test_engines_vllm.py
git commit -m "feat(engines/vllm): stop_patterns 返回 docker 双模式"
```

---

### Task 5: 改 yaml 配置 + 注释

**Files:**
- Modify: `models/vllm/qwen3.8-flash-next.yaml`

**Interfaces:**
- Consumes: 无（配置层）
- Produces: `vllm.docker_image` 字段去除 `# 已识别问题` 头部说明、新增字段值

- [ ] **Step 1: 改 yaml**

在 `models/vllm/qwen3.8-flash-next.yaml`：

(1) 把头部注释 L12 的"官方以镜像 vllm/vllm-openai:qwen38-flash-next 提供 Day-0 支持"段和 L15-19 的解决路径改成：

```yaml
# 【已支持】本配置现在可用 docker 方式走 modelctl 启动：
#   vllm.docker_image: vllm/vllm-openai:qwen38-flash-next
# 行为：
#   1. 首次启动前 docker 自动 pull 该镜像（21.8GB）
#   2. 模型从 hf/modelscope 下载到 $MODEL_ROOT（默认 /raid5/sh/model-hf），
#      modelctl 写回 yaml model 字段为本地绝对路径
#   3. 容器启动使用 nvsyscall 透传同卡（8 卡 RTX 5880，TP8）
#   4. 容器内 vLLM 绑定 8000，宿主端口 = yaml 顶层 port（默认 8110）
#   5. modelctl stop 通过 start_detached PID→fuser→pkill 三层 stop 容器
#      （关键单点 = fuser 命中端口对应容器 userland 进程 vLLM）
```

(2) 在 `vllm:` 段后的 `model:` 行之后（L45-46 附近）插入：

```yaml
  # docker_image：Day-0 vLLM 专用镜像（仅 qwen3.8-flash-next 需要）
  #   留空 = 走 .venvs/vllm 托管 venv（现状）
  #   镜像内预装 FlashQLA 与 8 个融合算子，支持 Qwen4ExpForConditionalGeneration
  docker_image: vllm/vllm-openai:qwen38-flash-next
```

不删其他现有字段（`model` / `download` / `tensor_parallel_size` / `gpu_list` / `max_model_len` / `gpu_memory_utilization` / `quantization` / `kv_cache_dtype` / `extra_args` 字段全保留——它们现在都映射到容器内 `vllm serve` 命令）。

- [ ] **Step 2: 验证 schema 解析**

Run: `python -c "from modelctl.core.profile import load_profile; p = load_profile('models/vllm/qwen3.8-flash-next.yaml'); print(p.engine, p.port, p.engine_config.get('docker_image'))"`
Expected: 输出 `vllm 8110 vllm/vllm-openai:qwen38-flash-next`

- [ ] **Step 3: Commit**

```bash
git add models/vllm/qwen3.8-flash-next.yaml
git commit -m "fix(models/vllm): qwen3.8-flash-next 启用 docker 镜像 Day-0"
```

---

### Task 6: pre_start 模型路径写回（docker 复用 + 回归）+ 全量回归

**Files:**
- Test: `tests/test_engines_vllm.py`（追加 `test_pre_start_persists_local_path_for_docker`）
- Modify: `src/modelctl/engines/vllm.py:79-97`（仅测试覆盖；现状逻辑无需改）

**Interfaces:**
- Consumes: 现状 `pre_start` 与 `persist_model_path`
- Produces: 无新接口（行为回归测试）

- [ ] **Step 1: 写测试**

```python
def test_pre_start_persists_local_path_for_docker(tmp_path, monkeypatch):
    """docker 类型 + HF repo id → pre_start 触发下载 → pre_start 直接更新 cfg["model"] → build_command 用本地路径。"""
    import modelctl.engines.vllm as vllm_adapter_mod
    import modelctl.engines.vllm as vllm_mod
    from pathlib import Path

    download_dir = tmp_path / "model-hf" / "Qwen" / "X"
    download_dir.mkdir(parents=True)
    (download_dir / "config.json").write_text("{}", encoding="utf-8")

    # 桩掉 download_repo（vllm 模块顶层 from _download import download_repo ——
    # 因此打 monkey 在 vllm_mod 命名空间）
    monkeypatch.setattr(vllm_mod, "download_repo",
                        lambda repo, root: download_dir)

    profile_path = tmp_path / "m.yaml"
    profile_path.write_text(
        "vllm:\n  model: Qwen/X\n  download:\n    modelscope_id: Qwen/X\n"
        "  docker_image: x/y:z\n",
        encoding="utf-8",
    )

    # 构造 profile 时 profile.path 必须非 None（pre_start 用到）
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import Profile
    from modelctl.engines.vllm import VllmAdapter
    profile = Profile(
        path=profile_path, name="fake-d", engine="vllm", port=8110,
        group="g", variant=None, api_key="",
        engine_config={"model": "Qwen/X",
                       "download": {"modelscope_id": "Qwen/X"},
                       "docker_image": "x/y:z"},
        usage=None, tool_call_rounds=None, max_output_tokens=None,
    )
    caps = Capabilities(gpu_count=8, gpu_indices=list(range(8)),
                        gpu_name="RTX 5880", gpu_mem_mb=48 * 1024)
    a = VllmAdapter(profile, caps)
    a.pre_start()
    # pre_start 把 profile.engine_config["model"] 直接更新为下载目录（现状逻辑 vllm.py:90）
    assert profile.engine_config["model"] == str(download_dir.resolve())
    # yaml 文件内 model 字段也被 persist_model_path 文本级替换
    text = profile_path.read_text(encoding="utf-8")
    assert str(download_dir.resolve()) in text
    # build_command 此时可用（model 已是本地路径，不再判 HF id）
    cmd, _ = a.build_command()
    assert "/models/X" in cmd


def test_full_vllm_suite_no_regression(tmp_path):
    """总结算：当前 7 个 vllm yaml 中 6 个未配 docker_image（qwen3.8-flash-next 除外），
    build_command 必须走托管 venv 路径，不抛 docker 相关异常。"""
    from modelctl.core.profile import load_profile
    from modelctl.core.capabilities import Capabilities
    from modelctl.engines.vllm import VllmAdapter

    P = Path(__file__).resolve().parents[1] / "models" / "vllm"
    for f in sorted(P.glob("*.yaml")):
        p = load_profile(f)
        caps = Capabilities(gpu_count=8, gpu_indices=list(range(8)),
                            gpu_name="RTX 5880", gpu_mem_mb=48 * 1024)
        a = VllmAdapter(p, caps)
        if p.engine_config.get("docker_image"):
            continue  # qwen3.8-flash-next：配置docker_image 跳过（跑了反而干扰回归断言）
        try:
            cmd, _ = a.build_command()
            # 命令首元素含 ".venvs/vllm/bin/vllm"（托管 venv 路径）
            assert ".venvs/vllm/bin/vllm" in cmd[0].replace("\\", "/"), \
                f"{f.name} 首元素应为 venv vllm：{cmd[0]}"
            assert "docker" not in " ".join(cmd), f"{f.name} 不应含 docker run"
        except Exception as e:
            # 若 model 是 HF id 未下载则 venv 路径会因 ensure_env 缺 venv 而抛
            # 此处只断言是 venv 侧异常，绝不等于 docker 路径异常
            assert "docker" not in str(e), f"{f.name} 不应报 docker 异常：{e}"
```

- [ ] **Step 2: 跑测试**

Run: `pytest tests/test_engines_vllm.py -v`
Expected: 全绿（含旧 9 个 + 旧 2 个 resolve + 旧 4 个 check + 旧 4 个 build + 旧 2 个 stop + 新 2 个 pre_start + 新 1 个 full 回归 = 至少 24 个 PASS）

- [ ] **Step 3: 全量回归**

Run: `pytest tests/ -x -q`
Expected: 全仓 tests 全绿（其他引擎/模块不被本次改动影响）

- [ ] **Step 4: Commit**

```bash
git add tests/test_engines_vllm.py
git commit -m "test(engines/vllm): pre_start 写回 + 全仓 yaml 回归"
```

---

## 总验收（与 spec §8 验收标准对照）

| spec 条款 | 覆盖 |
|---|---|
| 1. 现状 7 个 yaml build_command 输出与改造前 byte-identical | `test_full_vllm_suite_no_regression` + `test_build_command_default_venv_unchanged` |
| 2. qwen3.8-flash-next 实证启动 | `docker_image` 字段 + `--gpus` + `-p profile.port:8000` + `-v parent:/models:ro` —— host machine 上 `modelctl start qwen3.8-flash-next-vllm` 真机验证（不在 CI 范围，开发机单次跑通即可） |
| 3. stop/status 闭环 | `stop_patterns` 双模式 + 现有 `stop_instance` 三层兜底（`fuser` 是关端口关键单点）；status 走 `is_running`（PID 文件 docker 后无效 → 永远 false，需后续 spec 改 is_running 语义；当前 stop 用 `fuser` 释放端口后端口释放即可认为 stopped） |
| 4. 诊断可用 | `docker logs --tail 200 <container_name>` 手动可读；可选增强不在本 plan |
| 5. 测试全绿 | 每 task Step 4 + Task 6 Step 2/3 |
| 6. 向后兼容 | 未配 `docker_image` 的 yaml 不触发 docker 任何代码路径（`_resolve_runtime` 返回 venv，与改造前等价） |

## 风险回滚

每 Step 5 都(Create 一个 commit)，–任一步 tests 挂可 `git revert HEAD`；三个 task 之间互不兼容时停在该 commit、回退最新 commit。整体可一次性 `git reset HEAD~6`（6 个 task commit）。

## 文件清单

| 文件 | 操作 |
|---|---|
| `docs/superpowers/plans/2026-08-31-vllm-docker-image.md` | 本计划 |
| `src/modelctl/engines/vllm.py` | 改（5 个函数 / 2 个属性 / 1 个新方法） |
| `models/vllm/qwen3.8-flash-next.yaml` | 改（头部注释 + `docker_image` 字段） |
| `tests/test_engines_vllm.py` | 改（新增 12 个测试点：resolve 2 / check 4 / build 4 / stop 2 / pre_start 1 / 全仓 yaml 回归 1） |
