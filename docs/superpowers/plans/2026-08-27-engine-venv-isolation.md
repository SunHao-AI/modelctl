# 引擎独立虚拟环境隔离 — 实施计划

> 依据：`docs/superpowers/specs/2026-08-27-engine-venv-isolation-design.md`（commit 9272a57，已获用户批准）。

## Goal

为每个托管推理引擎（vllm、sglang）创建并管理独立虚拟环境，消除 torch / flashinfer 等依赖在 vllm 与 sglang 间的版本互斥。modelctl 主环境退化为纯控制面，新增 `modelctl env` 命令族负责环境的创建 / 查看 / 卸载。

## Architecture

在本计划落定后形成如下结构：

```
envs/
├── vllm/
│   ├── pyproject.toml
│   ├── .python-version
│   └── uv.lock
└── sglang/
    ├── pyproject.toml
    ├── .python-version
    └── uv.lock
.venvs/                     # 实际虚拟环境实体（.gitignore）
├── vllm/
└── sglang/
src/modelctl/core/envs.py   # 新模块：环境路径 / 探测 / setup / remove / status
```

关键链路（数据流不变）：

- `build_command()` 仍返回 `(command, extra_env)`；
- vllm 首元素由 PATH 中的 `vllm` 改为 `envs.engine_bin("vllm", "vllm")`；
- sglang 首元素由 `sys.executable` 改为 `envs.engine_python("sglang")`；
- `extra_env` 追加 `VIRTUAL_ENV` 与前置 venv `bin`（Windows: `Scripts`）的 `PATH`；
- `check_requirements()` 第一步由 PATH 探测改为 `ensure_env(engine)`；
- `core/process.py` 的 `{**os.environ, **extra_env}` 合并无需改动。

## Tech Stack

- Python 3.12+ / uv 作为环境管理器；
- `UV_PROJECT_ENVIRONMENT` 重定向 venv 实体到项目根 `.venvs/<engine>/`；
- Windows `Scripts` 与 Linux `bin` 平台差异集中在 `core/envs.py`；
- 现有适配器模式（EngineAdapter）+ 中央编排（process.start_detached）维持不变。

## Global Constraints

- 新模块导入期禁止执行外部命令；路径/状态逻辑纯函数式，`setup()` 中才调用 `subprocess`；
- 全部单元测试 mock `subprocess` 与路径解析，Windows 开发机可完整跑通；
- PowerShell 不支持 `&&`，命令用分号 `;` 分隔；
- 计划内所有代码/命令、commit message 均用中文注释与说明。

---

## Task 1：新增 `core/envs.py`（函数骨架 + 错误分支，TDD）

### Step 1.1 写失败测试 `tests/test_core_envs.py`

新增文件 `tests/test_core_envs.py`，覆盖以下断言（先全部失败）：

Consumes:
- `from modelctl.core.envs import MANAGED_ENGINES, ENVS_ROOT, VENV_ROOTS, has_env, ensure_env, engine_python, engine_bin, setup, remove, status, EngineEnvError`
- `from modelctl.core.envfile import PROJECT_ROOT`

Produces:
- 断言 `MANAGED_ENGINES == ("vllm", "sglang")`；
- 断言 `ENVS_ROOT == PROJECT_ROOT / "envs"`；`VENV_ROOTS == PROJECT_ROOT / ".venvs"`；
- `engine_bin("vllm", "vllm")` 在注入 `os.name == "nt"` 的 monkeypatch 下返回 `<.venvs/vllm>/Scripts/vllm.exe`；
- `engine_bin("vllm", "vllm")` 在 Linux 形态（`os.name != "nt"`）下返回 `<.venvs/vllm>/bin/vllm`；
- `engine_python("sglang")` Windows 下返回 `<.venvs/sglang>/Scripts/python.exe`，Linux 下返回 `<.venvs/sglang>/bin/python`；
- `has_env("vllm")` 在 `.venvs/vllm` 不存在时返回 `False`；创建该目录 + 对应 python 可执行文件后返回 `True`（用 `tmp_path` + monkeypatch `VENV_ROOTS`）；
- `ensure_env("vllm")` 在环境存在时返回 venv 根 Path；
- `ensure_env("vllm")` 在环境缺失时抛 `EngineEnvError`，且消息包含 `modelctl env setup vllm`；
- `engine_python` / `engine_bin` 在引擎不在 `MANAGED_ENGINES` 时抛 `ValueError`；
- `status()` 无外部命令依赖：对不存在引擎返回 `{"exists": False}`；对存在引擎读出 `pyvenv.cfg` 中的 `version` 字段与 `site-packages/*.dist-info/METADATA` 中的引擎包版本。

### Step 1.2 运行测试验证失败

```
uv run pytest tests/test_core_envs.py -q
```
确认因模块缺失而全部失败（收集错误）。

### Step 1.3 最小实现 `src/modelctl/core/envs.py`

在新文件实现：

```python
"""core/envs.py — 托管引擎专用虚拟环境管理。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from modelctl.core.envfile import PROJECT_ROOT

MANAGED_ENGINES = ("vllm", "sglang")
ENVS_ROOT = PROJECT_ROOT / "envs"
VENV_ROOT = PROJECT_ROOT / ".venvs"


class EngineEnvError(RuntimeError):
    """引擎专用环境缺失或不可用。"""


def _is_windows() -> bool:
    return os.name == "nt"


def venv_bin_dir(engine: str) -> Path:
    return VENV_ROOT / engine / ("Scripts" if _is_windows() else "bin")


def engine_python(engine: str) -> Path:
    if engine not in MANAGED_ENGINES:
        raise ValueError(f"非托管引擎：{engine}")
    return venv_bin_dir(engine) / "python.exe"


def engine_bin(engine: str, name: str) -> Path:
    if engine not in MANAGED_ENGINES:
        raise ValueError(f"非托管引擎：{engine}")
    exe = name + (".exe" if _is_windows() else "")
    return venv_bin_dir(engine) / exe


def has_env(engine: str) -> bool:
    py = engine_python(engine)
    return py.is_file()


def ensure_env(engine: str) -> Path:
    if engine not in MANAGED_ENGINES:
        raise ValueError(f"非托管引擎：{engine}")
    if not has_env(engine):
        raise EngineEnvError(
            f"引擎 {engine} 的专用环境未创建，请先执行：modelctl env setup {engine}"
        )
    return VENV_ROOT / engine
```

> 说明：`engine_python` 的 Windows/Linux 分支由 `venv_bin_dir` 统一处理；`has_env`/`ensure_env` 只做纯路径判断，不执行外部命令。

### Step 1.4 运行测试验证通过

```
uv run pytest tests/test_core_envs.py -q
```

### Step 1.5 提交

```
git add src/modelctl/core/envs.py tests/test_core_envs.py
git commit -m "feat: 新增 core/envs.py 引擎专用虚拟环境路径/探测骨架" -m "TDD：has_env/ensure_env/engine_python/engine_bin 纯路径逻辑，导入期不执行外部命令"
```

---

## Task 2：`envs.py` 补全 `setup / remove / status`（外部命令层）

### Step 2.1 写失败测试（追加到 `tests/test_core_envs.py`）

Consumes:
- `from modelctl.core import envs as envs_mod`

Produces:
- `setup("vllm")`：monkeypatch 替换 `envs_mod.subprocess.run`（记录调用）并替换 `envs_mod.subprocess.PIPE` 为哨兵；macOS/Linux 下断言调用了 `["uv", "sync", "--project", str(ENVS_ROOT / "vllm")]` 且环境变量含 `UV_PROJECT_ENVIRONMENT=str(VENV_ROOT / "vllm")`；返回值为模拟退出码。
- `setup("unknown")` 抛 `ValueError`。
- `remove("vllm")`：monkeypatch 替换 `shutil.rmtree`，断言其被调用且参数为 `VENV_ROOT / "vllm"`。
- `status()`：在新建的 `tmp_path/.venvs/vllm` 下写 `pyvenv.cfg`（含 `version = 3.12.1`）与 `Lib/site-packages/vllm-0.27.0.dist-info/METADATA`（含 `Name: vllm` / `Version: 0.27.0`），monkeypatch `envs_mod.VENV_ROOT`，断言返回 `{"vllm": {"exists": True, "python": "3.12.1", "packages": {"vllm": "0.27.0"}}}`。

### Step 2.2 运行测试验证失败

```
uv run pytest tests/test_core_envs.py -q
```

### Step 2.3 实现 `setup / remove / status`

在 `core/envs.py` 追加：

```python
def setup(engine: str) -> int:
    if engine not in MANAGED_ENGINES:
        raise ValueError(f"非托管引擎：{engine}")
    exe = shutil.which("uv")
    if exe is None:
        raise EngineEnvError("未找到 uv，请先安装：pip install uv")
    env = {
        **os.environ,
        "UV_PROJECT_ENVIRONMENT": str(VENV_ROOT / engine),
    }
    proc = subprocess.run(
        [exe, "sync", "--project", str(ENVS_ROOT / engine)],
        env=env,
    )
    return proc.returncode


def remove(engine: str) -> None:
    if engine not in MANAGED_ENGINES:
        raise ValueError(f"非托管引擎：{engine}")
    shutil.rmtree(VENV_ROOT / engine, ignore_errors=True)


def status() -> dict[str, dict]:
    result: dict[str, dict] = {}
    for engine in MANAGED_ENGINES:
        root = VENV_ROOT / engine
        entry: dict = {"exists": False}
        if not has_env(engine):
            result[engine] = entry
            continue
        entry["exists"] = True
        python = _read_pyvenv_version(root)
        if python:
            entry["python"] = python
        packages = _read_installed_packages(root)
        if packages:
            entry["packages"] = packages
        result[engine] = entry
    return result
```

补两个内部函数：

```python
def _read_pyvenv_version(root: Path) -> str:
    cfg = root / "pyvenv.cfg"
    if not cfg.is_file():
        return ""
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            return line.split("=", 1)[1].strip()
    return ""


def _read_installed_packages(root: Path) -> dict[str, str]:
    sp = root / ("Lib/site-packages" if _is_windows() else "lib/python*/site-packages")
    sp_dirs = list(root.glob("lib/python*/site-packages")) if not _is_windows() else [root / "Lib/site-packages"]
    result: dict[str, str] = {}
    for sp in sp_dirs:
        if not sp.is_dir():
            continue
        for meta in sp.glob("*.dist-info/METADATA"):
            name = version = ""
            for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Name:") and not name:
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("Version:") and not version:
                    version = line.split(":", 1)[1].strip()
                if name and version:
                    break
            if name and version:
                result[name] = version
    return result
```

> 说明：`status()` 扫描 site-packages 时优先匹配 `lib/python*/site-packages`（Linux），Windows 固定 `Lib/site-packages`；不依赖 venv 内 pip（uv venv 默认无 pip）。

### Step 2.4 运行测试验证通过

```
uv run pytest tests/test_core_envs.py -q
```

### Step 2.5 提交

```
git add src/modelctl/core/envs.py tests/test_core_envs.py
git commit -m "feat: envs.py 补全 setup/remove/status" -m "setup 经 uv sync --project 并重定向 UV_PROJECT_ENVIRONMENT；status 读 pyvenv.cfg 与 dist-info，不依赖 venv 内 pip"
```

---

## Task 3：新增引擎子项目 `envs/vllm/` 与 `envs/sglang/`

### Step 3.1 复用根 `uv.toml` 的 index 配置

从根 [uv.toml](file:///d:/WorkPlace/Pycharm/modelctl/uv.toml) 迁移 `/vllm/pyproject.toml` 所需的 index 声明：阿里 default + pytorch-cu13 explicit（`https://download.pytorch.org/whl/cu130`）+ pypi.org 兜底。该配置通过子项目内 `[[tool.uv.index]]` 或根级 `uv.toml` 的 `[[index]]` 注入均可——为保持子项目自包含可复现，直接在子项目 `pyproject.toml` 的 `[tool.uv]` 段声明（uv 的 `[tool.uv.index]` 等价于根 `[[index]]`）。

### Step 3.2 写 `envs/vllm/pyproject.toml`

```toml
[project]
name = "modelctl-venv-vllm"
version = "0.1.0"
description = "vLLM 引擎专属虚拟环境（由 modelctl env setup 管理）"
requires-python = ">=3.12"
dependencies = [
    "vllm>=0.27,<0.28",
]

[tool.uv]
# torch cu13 explicit index（从项目根 uv.toml 迁移，保持子项目自包含、uv sync 可复现）
[[tool.uv.index]]
name = "pytorch-cu13"
url = "https://download.pytorch.org/whl/cu130"
explicit = true
```

### Step 3.3 写 `envs/vllm/.python-version`

```
3.12
```

> 说明：若实施时 `uv sync` 报 Python 版本与 vllm 元数据冲突（如 vllm 要求 `>=3.12,<3.14`），以引擎约束为准改此文件；真实机器上 `uv sync` 会按 `.python-version` 自动下载解释器。

### Step 3.4 同法写 `envs/sglang/pyproject.toml`

```toml
[project]
name = "modelctl-venv-sglang"
version = "0.1.0"
description = "SGLang 引擎专属虚拟环境（由 modelctl env setup 管理）"
requires-python = ">=3.12"
dependencies = [
    "sglang[all]>=0.5.9,<0.6",
]
```

### Step 3.5 写 `envs/sglang/.python-version`

```
3.12
```

### Step 3.6 生成 `uv.lock` 并入库（留待部署机执行）

> 注意：`uv.lock` 需要真实解析（含网络下载与平台 wheel 选择），本机 / CI 无法确定最终平台（Linux + cu13 wheel）。因此在开发机上生成可提交的 `uv.lock` 前需先在目标部署机（Linux + 对应 CUDA）跑一次：
>
> ```
> uv sync --project envs/vllm --no-dev
> uv sync --project envs/sglang --no-dev
> ```
>
> 生成的 `envs/*/uv.lock` 与 `.venvs/*` 分别入库 / 由 `.gitignore` 忽略。若无法在部署机生成（例如当前环境仅 Windows），则将「生成并提交 uv.lock」标记为待部署机执行的后置任务，`envs/*/pyproject.toml` 保持可解析状态。

### Step 3.7 提交

```
git add envs/vllm/pyproject.toml envs/vllm/.python-version envs/sglang/pyproject.toml envs/sglang/.python-version
git commit -m "feat: 新增 vllm/sglang 引擎子项目脚手架" -m "每引擎独立 pyproject + .python-version，索引配置自包含；uv.lock 待部署机生成入库"
```

---

## Task 4：CLI `env` 命令族（parser 注册 + 分发 + 三命令）

### Step 4.1 写失败测试 `tests/test_cli_env.py`

新增 `tests/test_cli_env.py`，覆盖 `build_parser` 对 `env` 子命令的注册与分发：

Consumes:
- `from modelctl.cli import build_parser, main`
- `from modelctl.core.envs import MANAGED_ENGINES`

Produces:
- `build_parser().parse_args(["env", "setup", "vllm"])` 得到 `command == "env"`、`action == "setup"`、`engine == "vllm"`；
- `main(["env", "list"])` 在 monkeypatch `modelctl.cli._cmd_env_list`（返回 0）下调用成功；
- `main(["env", "remove", "sglang"])` 在 monkeypatch `_cmd_env_remove` 下调用成功；
- `main(["env", "setup", "bogus"])` 因 engine 不在 `MANAGED_ENGINES` 抛 `SystemExit`（argparse `choices` 校验）。

### Step 4.2 运行测试验证失败

```
uv run pytest tests/test_cli_env.py -q
```

### Step 4.3 在 `build_parser()` 注册 `env` 子命令

在 [cli.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/cli.py) 的 `build_parser()` 中、`nginx-snippet` 之后追加：

```python
    ep = sub.add_parser("env", help="引擎专用虚拟环境管理（vllm / sglang）")
    ep.add_argument("action", choices=["setup", "list", "remove"])
    ep.add_argument("engine", nargs="?", default=None)
```

### Step 4.4 实现 `_cmd_env_setup / _cmd_env_list / _cmd_env_remove`

在 `cli.py` 中新增：

```python
def _cmd_env_setup(args, models_dir: Path | None, caps) -> int:
    if args.engine is None:
        logger.error("请指定引擎：modelctl env setup <engine>")
        return 2
    try:
        code = envs_setup(args.engine)
    except EngineEnvError as exc:
        logger.error(str(exc))
        return 2
    if code != 0:
        logger.error(f"env setup {args.engine} 失败（退出码 {code}），请检查 uv 输出后重试")
        return code
    logger.info(f"{args.engine} 环境安装完成")
    return 0


def _cmd_env_list(args, models_dir: Path | None, caps) -> int:
    states = envs_status()
    print("托管引擎环境：")
    for engine in MANAGED_ENGINES:
        st = states.get(engine, {"exists": False})
        if st["exists"]:
            detail = f"python {st.get('python', '?')}"
            if st.get("packages"):
                detail += "；" + "、".join(f"{k} {v}" for k, v in st["packages"].items())
            print(f"  {engine}: 已创建（{detail}）")
        else:
            print(f"  {engine}: 未创建（执行 modelctl env setup {engine}）")
    print("ollama / llamacpp / unsloth：原生或官方安装器，无需托管")
    return 0


def _cmd_env_remove(args, models_dir: Path | None, caps) -> int:
    if args.engine is None:
        logger.error("请指定引擎：modelctl env remove <engine>")
        return 2
    try:
        envs_remove(args.engine)
    except ValueError as exc:
        logger.error(str(exc))
        return 2
    logger.info(f"{args.engine} 环境已移除")
    return 0
```

在 `cli.py` 顶部引入已有导入基础上补充：

```python
from modelctl.core.envs import (
    MANAGED_ENGINES,
    EngineEnvError,
    remove as envs_remove,
    setup as envs_setup,
    status as envs_status,
)
```

### Step 4.5 在 `main()` 分发

在 [cli.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/cli.py) 的 `main()` 中、`nginx-snippet` 分支之后追加：

```python
        if args.command == "env":
            if args.action == "setup":
                return _cmd_env_setup(args, models_dir, caps)
            if args.action == "list":
                return _cmd_env_list(args, models_dir, caps)
            return _cmd_env_remove(args, models_dir, caps)
```

### Step 4.6 运行测试验证通过

```
uv run pytest tests/test_cli_env.py -q
```

### Step 4.7 提交

```
git add src/modelctl/cli.py tests/test_cli_env.py
git commit -m "feat: 新增 modelctl env 命令族" -m "setup/list/remove 三子命令，engine 受限 MANAGED_ENGINES 校验，错误 exit 2"
```

---

## Task 5：`vllm.py` / `sglang.py` 适配器改造（venv 路径 + 环境注入）

### Step 5.1 更新既有 vllm 测试断言（使其失效再修）

在 [tests/test_engines_vllm.py](file:///d:/WorkPlace/Pycharm/modelctl/tests/test_engines_vllm.py) 中：

- `test_vllm_command` 的 `assert cmd[:3] == ["vllm", "serve", "Qwen/Qwen3-32B"]` 改为解析 venv 路径后断言（首选 `monkeypatch` `envs.VENV_ROOT / "vllm"` 指针，再 `cmd[0].endswith("vllm")` 或等值判断）。

修改后写法示例：

```python
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    monkeypatch.setattr("modelctl.core.envs.VENV_ROOT", tmp_path / ".venvs")
    p = _write(... same ...)
    a = get_adapter("vllm")(p, CAPS8)
    a.check_requirements()
    cmd, env = a.build_command()
    assert str(cmd[0]).endswith(("vllm.exe", "vllm"))  # 指向 venv 内可执行文件
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venvs" / "vllm")
    assert f"{tmp_path / '.venvs' / 'vllm' / 'bin'}" in env["PATH"] or f"{tmp_path / '.venvs' / 'vllm' / 'Scripts'}" in env["PATH"]
```

同理在 `test_sglang_command` 中加入对首元素指向 venv 解释器的断言（`cmd[0].endswith("python")`）、`VIRTUAL_ENV` / `PATH` 注入断言。

### Step 5.2 运行测试验证失效

```
uv run pytest tests/test_engines_vllm.py tests/test_engines_sglang.py -q
```
确认 vllm / sglang 命令断言因旧逻辑而失败（此时适配器未改）。

### Step 5.3 改造 `vllm.py`

- 顶部 import 追加：`from modelctl.core.envs import ensure_env, engine_bin, VENV_ROOT`；
- `check_requirements()` 第一行改为 `ensure_env("vllm")`（替换原 `if not self.caps.binaries.get("vllm"): raise ...`）；
- `build_command()` 首元素改 `["vllm", ...]` → `[str(engine_bin("vllm", "vllm")), ...]`；
- env 构造后追加 venv 注入：

```python
        env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        # 注入引擎专用 venv：让 vllm 子进程显式感知虚拟环境
        env["VIRTUAL_ENV"] = str(VENV_ROOT / "vllm")
        env["PATH"] = str(engine_bin("vllm", "vllm").parent) + os.pathsep + env.get("PATH", os.environ["PATH"])
        return cmd, env
```

> 说明：`PATH` 前置逻辑用 `engine_bin("vllm", "vllm").parent`（即 venv 的 Scripts 或 bin 目录），与 Windows/Linux 语义天然一致。

### Step 5.4 改造 `sglang.py`

- 顶部 import 追加：`from modelctl.core.envs import ensure_env, engine_python, VENV_ROOT`，并移除不再使用的 `import sys`（仅当确认无其他引用时）；
- `check_requirements()` 第一行改为 `ensure_env("sglang")`；
- `build_command()` 首元素 `sys.executable` → `str(engine_python("sglang"))`；
- env 构造后追加 `VIRTUAL_ENV` 与 `PATH` 前置（同 vllm 逻辑）。

### Step 5.5 运行全部引擎测试验证通过

```
uv run pytest tests/test_engines_vllm.py tests/test_engines_sglang.py -q
```

### Step 5.6 检查并提交

```
git add src/modelctl/engines/vllm.py src/modelctl/engines/sglang.py tests/test_engines_vllm.py tests/test_engines_sglang.py
git commit -m "feat: vllm/sglang 适配器改用引擎专用 venv" -m "check_requirements 改 ensure_env；build_command 首元素解析 venv 路径并注入 VIRTUAL_ENV/PATH"
```

> 注：`tests/test_engines_sglang.py` 若不存在则仅提交 vllm 相关；确认实际文件名（当前工程已见 `tests/test_engines_vllm.py`，sglang 用例混于其中）。

---

## Task 6：`capabilities.py` 探测与提示语更新

### Step 6.1 写失败测试（追加到 `tests/test_core_capabilities.py` 或新建）

Consumes:
- `from modelctl.core.capabilities import probe, which_binaries, binary_paths, ENGINE_INSTALL_HINTS`
- `from modelctl.core.envs import has_env`

Produces:
- `which_binaries(["vllm"])` / `binary_paths(["vllm"])` 对托管引擎优先用 `has_env("vllm")` 判定：monkeypatch `has_env` 返回 `True` 时 `binaries["vllm"] is True`、`binary_paths["vllm"]` 指向 venv 内路径；返回 `False` 时 `binaries["vllm"] is False`；
- `ENGINE_INSTALL_HINTS["vllm"]` 与 `["sglang"]` 包含 `modelctl env setup`；
- 非托管引擎（ollama / unsloth / llamacpp）仍走 `shutil.which` 逻辑（monkeypatch `shutil.which` 验证）。

### Step 6.2 运行测试验证失败

```
uv run pytest tests/test_core_capabilities.py -q
```

### Step 6.3 改造 `capabilities.py`

- `ENGINE_INSTALL_HINTS` 中 vllm / sglang 提示语改为：
  - `"vllm": "，建议执行：modelctl env setup vllm"`
  - `"sglang": '，建议执行：modelctl env setup sglang（与 vllm 依赖互斥，需独立虚拟环境）'`
- 新增 `_managed_binary_path(engine, name)` 帮助函数：托管引擎调用 `engine_bin(engine, name)`，当 `has_env(engine)` 时返回路径、否则返回 `None`；
- `which_binaries` / `binary_paths` 对托管引擎改为：若 `engine in MANAGED_ENGINES` 则 `path = engine_bin(...) if has_env(...) else None`，非托管引擎维持 `shutil.which`；
- 在 `probe()` 中使 `binaries` / `binary_paths` 以 `which_binaries` / `binary_paths` 为基础，托管项被覆盖。

### Step 6.4 运行测试验证通过

```
uv run pytest tests/test_core_capabilities.py -q
```

### Step 6.5 提交

```
git add src/modelctl/core/capabilities.py tests/test_core_capabilities.py
git commit -m "feat: capabilities 探测与提示语接入引擎专用 venv" -m "vllm/sglang 用 has_env 判定与 venv 路径覆盖，提示语指向 env setup"
```

---

## Task 7：`compat.py` 兼容性检查指向引擎 venv

### Step 7.1 改动点

在 [compat.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/compat.py) 的 `run_compat_checks()` 中，把 `EnvSpec.from_env()`（无参，扫描当前解释器）改为扫描目标引擎 venv 的 site-packages。

`base.py` 中 `run_compat_checks` 现有：

```python
        env = getattr(self, "_compat_env", None)
        if env is None:
            env = EnvSpec.from_env()
            self._compat_env = env
```

改为：

```python
        from modelctl.core.envs import VENV_ROOT, has_env

        env = getattr(self, "_compat_env", None)
        if env is None:
            sp = self._engine_site_packages()
            env = EnvSpec.from_env(site_packages=sp)
            self._compat_env = env
```

在 `EngineAdapter` 增加私有方法 `_engine_site_packages()`（推断 venv site-packages）：
- 对托管引擎（vllm / sglang）：若 `has_env(engine)`，返回 `VENV_ROOT / engine / <site-packages>`（Windows `Lib/site-packages`，Linux `lib/python*/site-packages` 取首个存在者）；环境未建时返回 `None`（后续 `EnvSpec.from_env(None)` 回退到当前解释器，属降级，不会误报）；
- 非托管引擎返回 `None`。

### Step 7.2 验证

```
uv run pytest tests/ -q
```
确认无回归（重点：compat 相关测试、引擎测试仍通过）。

### Step 7.3 提交

```
git add src/modelctl/engines/base.py src/modelctl/core/compat.py src/modelctl/core/envs.py
git commit -m "fix: 兼容性检查改扫描目标引擎 venv" -m "EnvSpec.from_env 传引擎 venv site-packages，未建环境时降级到当前解释器"
```

---

## Task 8：主项目清理（删 vllm extra / .gitignore / README）

### Step 8.1 移除 `pyproject.toml` 的 vllm extra

在 [pyproject.toml](file:///d:/WorkPlace/Pycharm/modelctl/pyproject.toml) 中删除：

```toml
# 推理引擎栈：vLLM（torch==2.13.0/cu13 由 vllm 元数据强约束自动带入）
vllm = ["vllm==0.27.*"]
```

### Step 8.2 `.gitignore` 新增 `.venvs/`

追加一行（若已有 `.venv` 相关则并入）：

```
.venvs/
```

### Step 8.3 README 更新

在 [README.md](file:///d:/WorkPlace/Pycharm/modelctl/README.md)：
- 「安装」节（L61-66）由 `uv sync --extra dev` 改为 `uv sync --extra dev`（不变），但在「快速开始 → 1. 安装依赖」中把 vllm / sglang 的按需安装改为 `modelctl env setup vllm` / `modelctl env setup sglang`；
- 删除「安装必需依赖」（L72-73）提到 `--extra vllm` 的相关内容；
- 目录结构章节（L22-27）的 `envs/` 与 `.venvs/` 补充说明。

### Step 8.4 验证

```
uv run pytest tests/ -q
```

### Step 8.5 提交

```
git add pyproject.toml .gitignore README.md
git commit -m "chore: 移除 vllm extra，托管引擎改走 env setup" -m "pyproject 删 vllm=""==0.27.*"" 及注释；.gitignore 忽略 .venvs/；README 更新安装说明"
```

---

## 全量验证

```
uv run pytest tests/ -q
```
全部通过后，执行：

```
git add -A
git commit -m "chore: 引擎独立虚拟环境隔离功能完成"
```

## 部署机手动集成验收（无需自动化）

```
modelctl env setup vllm
modelctl env setup sglang
modelctl env list
modelctl start <vllm-profile>
modelctl start <sglang-profile>
modelctl probe
```

预期：两个引擎分别在 `.venvs/vllm`、`.venvs/sglang` 创建；`env list` 展示 python 版本与包；两个引擎模型可先后启动、健康检查通过、`stop` 正常；`probe` 显示 venv 内二进制路径。

---

## 自查清单（计划完成前逐项确认）

- [x] **Spec coverage**：本计划的 Task 1-8 完整覆盖设计规格 §2 总体结构、§3 core/envs.py 接口、§4 CLI 命令族、§5 启动链路、§6 探测与兼容、§7 错误处理、§8 测试策略、§9 兼容迁移。非目标（§10）未被纳入。
- [x] **Placeholder scan**：文中无 `TODO` / `<...>` 占位符；唯一待定项「生成并提交 uv.lock」已显式标注为部署机执行的 3 步指令，非占位符。
- [x] **Type consistency**：`envs.py` 接口签名在 Task 1/2 中定义并被 Task 4/5/6/7 引用；`ensure_env`/`engine_bin`/`engine_python`/`VENV_ROOT`/`MANAGED_ENGINES` 命名与规格 §3 一致；无未定义引用。

---

## 执行方式选择

计划完成后，请选择其中一种执行方式：

1. **Subagent-Driven（推荐）**：将上述独立任务逐个交给子代理实现，你可在每步结束后审查并推进；适合任务间存在依赖（Task 1→2→3 顺序等）但可分段执行。
2. **Inline Execution**：你在当前会话内按 Task 顺序直接实现全部改动，我全程自动验证并提交。
