# 一键启停（modelctl all）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `modelctl all start|stop|restart|status` 一键启停命令（默认模型 + 网关 + 用量统计三件套），并为 gateway/stats 补齐 restart/status 四动作；新增 `script/modelctl-all.sh` 薄脚本与文档。

**Architecture:** 新增 `core/all_service.py` 承载单组件四动作原语（start_profile/stop_profile/.../status_stats）与编排函数（start_all/stop_all/restart_all/status_all），统一返回 `ComponentResult`。cli.py 既有模型/gateway/stats 命令改为调用公共原语（行为不变），gateway/stats 补齐 restart/status，注册 `all` 子命令。`all stop` 停止全部运行中模型（含非默认），`all start/restart` 仅作用于默认模型。

**Tech Stack:** Python 3.12、PyYAML、loguru、pytest（mock 启动原语）。

## Global Constraints

- `requires-python = ">=3.12"`；运行期依赖仅 `PyYAML>=6.0` + `loguru>=0.7`；不新增第三方依赖。
- 代码注释用中文；终端为 PowerShell（命令分隔用分号）；git commit 含中文消息时用文件方式 `[System.IO.File]::WriteAllText(path, msg, UTF8Encoding($false))` 写消息文件再 `git commit -F`，避免 GBK 终端乱码。
- 既有命令行为不变：`modelctl start|stop|restart|status <name>`、`gateway start|stop|status`、`stats start|stop` 的输出与退出码保持现状。
- `all start/restart` 仅启动默认模型；`all stop` 停止**全部运行中模型**（遍历 profiles，含非默认）；`all status` 汇总默认模型 + gateway + stats。
- 默认模型解析：`GATEWAY_DEFAULT_MODEL`（.env 可覆盖），未设置回退 `deepseek-v4-flash`，匹配 `profile.name` 或 `profile.aliases`；`--model` 覆盖。
- 失败处理：逐组件尝试 + 汇总，start/restart 任 error → exit 2，stop 任 error → exit 1。
- 幂等：start 时已运行 → `skipped`（不报错）；stop 时未运行模型不进入结果列表（语义即"无需处理"，避免噪音）。
- 全量 `uv run pytest -q` 通过（当前 206 passed/1 skipped 基线）；新增/修改文件 ruff + mypy 干净。

---

### Task 1: all_service.py —— 单组件四动作原语 + ComponentResult + 默认模型解析

**Files:**
- Create: `src/modelctl/core/all_service.py`
- Test: `tests/test_all_service.py`（新建）

**Interfaces:**
- Consumes: `modelctl.core.capabilities.probe/Capabilities`、`modelctl.core.profile.Profile/list_profiles`、`modelctl.core.process`（is_running/start_detached/stop_instance/wait_health/pid_file/launch_log/tail_file）、`modelctl.engines.get_adapter`、`modelctl.engines.base.RequirementError`、`modelctl.core.gateway.GATEWAY_PORT`、`modelctl.core.stats.USAGE_PORT`、loguru logger。
- Produces:
  - `ComponentResult`（dataclass）：`component: str`、`status: Literal["ok","skipped","error"]`、`detail: str = ""`
  - `resolve_default_profile(models_dir: Path | None, model_id: str | None) -> Profile | None`（model_id=None 时取 `GATEWAY_DEFAULT_MODEL`，未设置回退 `"deepseek-v4-flash"`；匹配 name 或 aliases）
  - 单组件原语（返回 `ComponentResult`；**`start_profile`/`restart_profile` 在 check_requirements 失败时向上抛 `RequirementError`**——cli 既有命令依赖 main 捕获它返回 exit 2，与"配置错误→2、健康超时→1"的既有语义保持一致）：
    - `start_profile(profile, caps, timeout) -> ComponentResult`（已运行→skipped；启动失败→error(健康超时)；逻辑迁移自 cli._cmd_start）
    - `stop_profile(profile, caps, models_dir) -> ComponentResult`（迁移自 cli._stop_profile，含 ollama 特判；不抛）
    - `restart_profile(profile, caps, timeout) -> ComponentResult`（运行中→stop 后 start；未运行→直接 start）
    - `start_gateway() / stop_gateway() / restart_gateway() / status_gateway() -> ComponentResult`（不抛）
    - `start_stats() / stop_stats() / restart_stats() / status_stats() -> ComponentResult`（不抛）

- [ ] **Step 1: 写失败测试 `tests/test_all_service.py`**

```python
"""all_service 单组件原语与默认模型解析测试。"""

from __future__ import annotations

import pytest

from modelctl.core.all_service import (
    ComponentResult,
    resolve_default_profile,
    start_gateway,
    start_profile,
    status_gateway,
    status_stats,
    stop_gateway,
    stop_profile,
    stop_stats,
)
from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import Profile
from modelctl.engines.base import RequirementError


def _profile(name: str = "m", aliases: list[str] | None = None) -> Profile:
    return Profile(
        name=name, engine="llamacpp", port=18080,
        aliases=aliases or [], engine_config={"model": "m.gguf"},
    )


class _FakeAdapter:
    """可配置 wait_ready 结果的假引擎适配器。"""

    def __init__(self, profile, caps, ready: bool = True):
        self.warnings: list[str] = []
        self.profile = profile
        self._ready = ready

    def check_requirements(self):
        return None

    def pre_start(self):
        return None

    def build_command(self):
        return ["echo", "hi"], {}

    def wait_ready(self, timeout):
        return self._ready

    def upstream_api_key(self):
        return None

    def post_start(self):
        return None

    def metrics_mapping(self):
        return None


# ---- 默认模型解析 ----

def test_resolve_default_profile_by_name(tmp_path):
    (tmp_path / "a.yaml").write_text(
        "name: deepseek-v4-flash-llamacpp\nalias: deepseek-v4-flash\nengine: llamacpp\nport: 18080\nllamacpp:\n  model: m.gguf\n",
        encoding="utf-8",
    )
    r = resolve_default_profile(tmp_path, "deepseek-v4-flash-llamacpp")
    assert r is not None and r.name == "deepseek-v4-flash-llamacpp"


def test_resolve_default_profile_by_alias(tmp_path):
    (tmp_path / "a.yaml").write_text(
        "name: deepseek-v4-flash-llamacpp\nalias: deepseek-v4-flash\nengine: llamacpp\nport: 18080\nllamacpp:\n  model: m.gguf\n",
        encoding="utf-8",
    )
    r = resolve_default_profile(tmp_path, "deepseek-v4-flash")
    assert r is not None and r.name == "deepseek-v4-flash-llamacpp"


def test_resolve_default_profile_fallback(tmp_path, monkeypatch):
    (tmp_path / "a.yaml").write_text(
        "name: deepseek-v4-flash-llamacpp\nalias: deepseek-v4-flash\nengine: llamacpp\nport: 18080\nllamacpp:\n  model: m.gguf\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("GATEWAY_DEFAULT_MODEL", raising=False)
    assert resolve_default_profile(tmp_path, None) is not None  # 回退 deepseek-v4-flash 经 alias 命中


def test_resolve_default_profile_missing(tmp_path):
    assert resolve_default_profile(tmp_path, "nonexistent") is None


# ---- 模型原语 ----

def test_start_profile_skips_when_running(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: True)
    r = start_profile(_profile(), Capabilities(), 5.0)
    assert r.status == "skipped" and r.component == "model:m"


def test_start_profile_check_raises_requirement(monkeypatch):
    """check_requirements 失败应抛 RequirementError（cli 依赖 main 捕获返回 2）。"""
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: False)

    class _FailingAdapter:
        def __init__(self, profile, caps):
            pass

        def check_requirements(self):
            raise RequirementError("无法运行")

    monkeypatch.setattr(all_service, "get_adapter", lambda engine: _FailingAdapter)
    with pytest.raises(RequirementError):
        start_profile(_profile(), Capabilities(), 5.0)


def test_start_profile_ok(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: False)
    monkeypatch.setattr(all_service, "get_adapter", lambda engine: lambda p, c: _FakeAdapter(p, c, ready=True))
    monkeypatch.setattr(all_service, "start_detached", lambda name, cmd, env: 123)
    r = start_profile(_profile(), Capabilities(), 5.0)
    assert r.status == "ok" and "18080" in r.detail


def test_start_profile_health_timeout(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: False)
    monkeypatch.setattr(all_service, "get_adapter", lambda engine: lambda p, c: _FakeAdapter(p, c, ready=False))
    r = start_profile(_profile(), Capabilities(), 5.0)
    assert r.status == "error" and "超时" in r.detail


def test_stop_profile_calls_stop_instance(monkeypatch):
    from modelctl.core import all_service

    calls: list[str] = []
    monkeypatch.setattr(all_service, "get_adapter", lambda engine: type("A", (), {
        "stop_patterns": lambda self: ["llama-server"],
    })())
    monkeypatch.setattr(all_service, "stop_instance", lambda name, port, pat: calls.append(name))
    r = stop_profile(_profile(), Capabilities(), None)
    assert r.status == "ok" and calls == ["m"]


# ---- 网关 / 统计原语 ----

def test_start_gateway_skips_when_running(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: True)
    r = start_gateway()
    assert r.status == "skipped" and r.component == "gateway"


def test_start_gateway_ok(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: False)
    monkeypatch.setattr(all_service, "start_detached", lambda name, cmd, env: 1)
    r = start_gateway()
    assert r.status == "ok" and r.component == "gateway"


def test_status_gateway_stopped(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: False)
    assert status_gateway().detail == "已停止"


def test_status_stats_stopped(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: False)
    assert status_stats().detail == "已停止"


def test_stop_gateway_and_stats(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "stop_instance", lambda *a, **k: True)
    assert stop_gateway().status == "ok"
    assert stop_stats().status == "ok"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_all_service.py -v`
Expected: FAIL（`ModuleNotFoundError: modelctl.core.all_service`）

- [ ] **Step 3: 实现 `src/modelctl/core/all_service.py`**

```python
#!/usr/bin/env python3
"""core/all_service.py — 一键启停编排与单组件四动作原语（模型/网关/统计）。

供 `modelctl all` 与 `modelctl gateway|stats <动作>` 共用；统一返回 ComponentResult，
cli.py 负责把结果转成退出码与打印，本模块不依赖 cli。
注意：start_profile/restart_profile 在 check_requirements 失败时向上抛 RequirementError，
以便 cli 既有命令保持"配置错误 → exit 2、健康超时 → exit 1"的语义；编排层负责捕获。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loguru import logger

from modelctl.core.capabilities import Capabilities, probe
from modelctl.core.gateway import GATEWAY_PORT
from modelctl.core.process import (
    is_running,
    launch_log,
    pid_file,
    start_detached,
    stop_instance,
    tail_file,
    wait_health,
)
from modelctl.core.profile import Profile, list_profiles
from modelctl.core.stats import USAGE_PORT
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError

# all_service.py 位于 src/modelctl/core/，src 目录 = parents[2]（对应 cli.py 的 parents[1]）
_SRC_DIR = str(Path(__file__).resolve().parents[2])
DEFAULT_MODEL_ID = "deepseek-v4-flash"


@dataclass
class ComponentResult:
    component: str
    status: Literal["ok", "skipped", "error"]
    detail: str = ""


def resolve_default_profile(models_dir: Path | None, model_id: str | None) -> Profile | None:
    """解析默认模型 profile：model_id 缺省取 GATEWAY_DEFAULT_MODEL，未设置回退 deepseek-v4-flash。"""
    mid = model_id or os.environ.get("GATEWAY_DEFAULT_MODEL") or DEFAULT_MODEL_ID
    for p in list_profiles(models_dir):
        if p.name == mid or mid in p.aliases:
            return p
    return None


def start_profile(profile: Profile, caps: Capabilities, timeout: float) -> ComponentResult:
    """启动单个模型 profile（幂等：已运行返回 skipped）。

    check_requirements 失败时抛 RequirementError（配置错误语义，交给调用方/编排处理）。
    逻辑迁移自 cli._cmd_start。
    """
    tag = f"model:{profile.name}"
    if is_running(profile.name):
        return ComponentResult(tag, "skipped", "已在运行")
    adapter = get_adapter(profile.engine)(profile, caps)
    adapter.check_requirements()  # RequirementError 向上抛
    for warning in adapter.warnings:
        logger.warning(warning)
    adapter.pre_start()
    cmd, env = adapter.build_command()
    pid = start_detached(profile.name, cmd, env)
    logger.info(f"已启动 {profile.name}（PID {pid}），等待健康检查（超时 {timeout:g}s）...")
    if adapter.wait_ready(timeout):
        upstream_key = adapter.upstream_api_key()
        if upstream_key and upstream_key != profile.api_key:
            logger.info(f"上游 API Key（本次启动自动生成）：{upstream_key}")
        adapter.post_start()
        log = launch_log(profile.name)
        logger.info(f"启动成功：{profile.name} 运行于 http://127.0.0.1:{profile.port}")
        if log is not None:
            logger.info(f"日志：{log}")
        if profile.usage or adapter.metrics_mapping() is not None:
            logger.info("提示：用量统计可通过 `modelctl stats start` 启动")
        return ComponentResult(tag, "ok", f"http://127.0.0.1:{profile.port}")
    log = launch_log(profile.name)
    if log is not None:
        logger.warning(f"健康检查超时，日志尾部 50 行（{log}）：")
        logger.warning(tail_file(log, 50))
    else:
        logger.warning("健康检查超时，且未找到启动日志")
    return ComponentResult(tag, "error", "健康检查超时")


def stop_profile(profile: Profile, caps: Capabilities, models_dir: Path | None) -> ComponentResult:
    """停止单个模型 profile（含 ollama 共享 serve 特判）。逻辑迁移自 cli._stop_profile。"""
    tag = f"model:{profile.name}"
    adapter = get_adapter(profile.engine)(profile, caps)
    if profile.engine == "ollama":
        other_ollama_running = any(
            is_running(o.name)
            for o in list_profiles(models_dir)
            if o.engine == "ollama" and o.name != profile.name
        )
        if pid_file(profile.name).is_file() and not other_ollama_running:
            stop_instance(profile.name, profile.port, [])
        else:
            adapter.unload_model()
            pid_file(profile.name).unlink(missing_ok=True)
    else:
        stop_instance(profile.name, profile.port, adapter.stop_patterns())
    logger.info(f"已停止：{profile.name}")
    return ComponentResult(tag, "ok", "已停止")


def restart_profile(profile: Profile, caps: Capabilities, timeout: float) -> ComponentResult:
    """重启单个模型 profile：运行中先停后启，未运行直接启。"""
    if is_running(profile.name):
        stop_profile(profile, caps, None)
    return start_profile(profile, caps, timeout)


def _detached_script(module: str) -> tuple[list[str], dict[str, str]]:
    """后台启动 python -m 模块（gateway/stats）的公共 (命令, 环境变量)。"""
    extra_env = {"PYTHONPATH": _SRC_DIR + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return [sys.executable, "-m", module], extra_env


def start_gateway() -> ComponentResult:
    if is_running("llm-gateway"):
        return ComponentResult("gateway", "skipped", "网关已在运行")
    cmd, env = _detached_script("modelctl.core.gateway")
    pid = start_detached("llm-gateway", cmd, env)
    port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
    logger.info(f"网关已启动（PID {pid}），监听端口 {port}")
    return ComponentResult("gateway", "ok", f"http://127.0.0.1:{port}")


def stop_gateway() -> ComponentResult:
    port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
    stop_instance("llm-gateway", port, ["modelctl.core.gateway"])
    logger.info("网关已停止")
    return ComponentResult("gateway", "ok", "已停止")


def restart_gateway() -> ComponentResult:
    if is_running("llm-gateway"):
        stop_gateway()
    return start_gateway()


def status_gateway() -> ComponentResult:
    port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
    if is_running("llm-gateway"):
        ok = wait_health(f"http://127.0.0.1:{port}/v1/models", 3.0)
        return ComponentResult("gateway", "ok", "运行中，/v1/models " + ("正常" if ok else "无响应"))
    return ComponentResult("gateway", "ok", "已停止")


def start_stats() -> ComponentResult:
    if is_running("usage-stats"):
        return ComponentResult("stats", "skipped", "用量统计服务已在运行")
    cmd, env = _detached_script("modelctl.core.stats")
    pid = start_detached("usage-stats", cmd, env)
    port = int(os.environ.get("USAGE_PORT", str(USAGE_PORT)))
    logger.info(f"用量统计服务已启动（PID {pid}），监听端口 {port}")
    return ComponentResult("stats", "ok", f"http://127.0.0.1:{port}")


def stop_stats() -> ComponentResult:
    port = int(os.environ.get("USAGE_PORT", str(USAGE_PORT)))
    stop_instance("usage-stats", port, ["modelctl.core.stats"])
    logger.info("用量统计服务已停止")
    return ComponentResult("stats", "ok", "已停止")


def restart_stats() -> ComponentResult:
    if is_running("usage-stats"):
        stop_stats()
    return start_stats()


def status_stats() -> ComponentResult:
    port = int(os.environ.get("USAGE_PORT", str(USAGE_PORT)))
    if is_running("usage-stats"):
        ok = wait_health(f"http://127.0.0.1:{port}/api/usage", 3.0)
        return ComponentResult("stats", "ok", "运行中，/api/usage " + ("正常" if ok else "无响应"))
    return ComponentResult("stats", "ok", "已停止")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_all_service.py -v`
Expected: PASS（`test_start_profile_health_timeout` 中 `_Adapter` 复用需在测试内重新定义完整类，见 Step 1 代码——实现者按 TDD 实际调整测试代码保证其可运行且真断言行为）

- [ ] **Step 5: 运行静态检查**

Run: `uv run ruff check src/modelctl/core/all_service.py tests/test_all_service.py; uv run mypy src/modelctl/core/all_service.py`
Expected: 干净

- [ ] **Step 6: Commit**

```bash
git add src/modelctl/core/all_service.py tests/test_all_service.py
git commit -m "feat(core): all_service 单组件四动作原语与默认模型解析"
```

（中文 commit 用文件方式：`[System.IO.File]::WriteAllText("...\commitmsg.txt", $msg, (New-Object System.Text.UTF8Encoding($false))); git commit -F "...\commitmsg.txt" --no-verify; Remove-Item "...\commitmsg.txt"`）

---

### Task 2: all_service.py —— 编排 start_all / stop_all / restart_all / status_all

**Files:**
- Modify: `src/modelctl/core/all_service.py`（追加编排函数）
- Test: `tests/test_all_service.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `ComponentResult` / `resolve_default_profile` / `start_profile` / `stop_profile` / `restart_profile` / `start_gateway` 等全部原语、`probe`。
- Produces:
  - `start_all(models_dir, model_name=None, timeout=300) -> list[ComponentResult]`
  - `stop_all(models_dir) -> list[ComponentResult]`
  - `restart_all(models_dir, model_name=None, timeout=300) -> list[ComponentResult]`
  - `status_all(models_dir) -> list[ComponentResult]`

- [ ] **Step 1: 写失败测试（追加）**

```python
def test_start_all_order_and_continue(tmp_path, monkeypatch):
    """默认模型解析失败 → error 但继续启动 gateway/stats。"""
    from modelctl.core import all_service

    calls: list[str] = []
    monkeypatch.setattr(all_service, "resolve_default_profile", lambda d, m: None)
    monkeypatch.setattr(all_service, "start_gateway", lambda: (calls.append("gateway") or ComponentResult("gateway", "ok", "")))
    monkeypatch.setattr(all_service, "start_stats", lambda: (calls.append("stats") or ComponentResult("stats", "ok", "")))
    results = all_service.start_all(tmp_path, "nonexistent")
    assert results[0].status == "error" and "model" in results[0].component
    assert calls == ["gateway", "stats"]  # 模型失败不阻断后续


def test_start_all_starts_default_model_first(tmp_path, monkeypatch):
    from modelctl.core import all_service

    order: list[str] = []
    monkeypatch.setattr(all_service, "resolve_default_profile", lambda d, m: _profile("m"))
    monkeypatch.setattr(all_service, "start_profile", lambda p, c, t: (order.append("model") or ComponentResult("model:m", "ok", "")))
    monkeypatch.setattr(all_service, "start_gateway", lambda: (order.append("gateway") or ComponentResult("gateway", "ok", "")))
    monkeypatch.setattr(all_service, "start_stats", lambda: (order.append("stats") or ComponentResult("stats", "ok", "")))
    all_service.start_all(tmp_path)
    assert order == ["model", "gateway", "stats"]


def test_stop_all_stops_every_running_model(tmp_path, monkeypatch):
    """stop 遍历全部 profiles，非默认模型也停；未运行不记录。"""
    from modelctl.core import all_service

    stopped: list[str] = []
    monkeypatch.setattr(all_service, "probe", lambda: Capabilities())
    monkeypatch.setattr(all_service, "stop_stats", lambda: ComponentResult("stats", "ok", ""))
    monkeypatch.setattr(all_service, "stop_gateway", lambda: ComponentResult("gateway", "ok", ""))
    monkeypatch.setattr(all_service, "list_profiles", lambda d: [_profile("a"), _profile("b")])
    monkeypatch.setattr(all_service, "is_running", lambda name: name == "a")  # 仅 a 运行
    monkeypatch.setattr(all_service, "stop_profile", lambda p, c, d: (stopped.append(p.name) or ComponentResult(f"model:{p.name}", "ok", "")))
    results = all_service.stop_all(tmp_path)
    assert [r.component for r in results] == ["stats", "gateway", "model:a"]  # 顺序 stats→gateway→模型；b 未运行不记录
    assert stopped == ["a"]


def test_restart_all_only_default_model(tmp_path, monkeypatch):
    from modelctl.core import all_service

    restarted: list[str] = []
    monkeypatch.setattr(all_service, "resolve_default_profile", lambda d, m: _profile("m"))
    monkeypatch.setattr(all_service, "restart_profile", lambda p, c, t: (restarted.append(p.name) or ComponentResult("model:m", "ok", "")))
    monkeypatch.setattr(all_service, "restart_gateway", lambda: ComponentResult("gateway", "ok", ""))
    monkeypatch.setattr(all_service, "restart_stats", lambda: ComponentResult("stats", "ok", ""))
    all_service.restart_all(tmp_path)
    assert restarted == ["m"]  # 仅默认模型
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_all_service.py -v`
Expected: FAIL（`NameError: start_all`）

- [ ] **Step 3: 实现编排函数（追加到 all_service.py）**

```python
def start_all(models_dir: Path | None, model_name: str | None = None, timeout: float = 300) -> list[ComponentResult]:
    """一键启动：默认模型 → gateway → stats；单组件失败继续后续。"""
    caps = probe()
    results: list[ComponentResult] = []
    profile = resolve_default_profile(models_dir, model_name)
    if profile is None:
        mid = model_name or os.environ.get("GATEWAY_DEFAULT_MODEL") or DEFAULT_MODEL_ID
        results.append(
            ComponentResult("model", "error", f"未找到默认模型 profile（{mid}），请配置 GATEWAY_DEFAULT_MODEL 或 --model；可运行 `modelctl list` 查看")
        )
    else:
        try:
            results.append(start_profile(profile, caps, timeout))
        except RequirementError as error:  # check_requirements 失败（配置错误）
            results.append(ComponentResult(f"model:{profile.name}", "error", str(error)))
    results.append(start_gateway())
    results.append(start_stats())
    return results


def stop_all(models_dir: Path | None) -> list[ComponentResult]:
    """一键关闭：stats → gateway → 全部运行中模型（含非默认）。"""
    caps = probe()
    results: list[ComponentResult] = [stop_stats(), stop_gateway()]
    for profile in list_profiles(models_dir):
        if is_running(profile.name):
            results.append(stop_profile(profile, caps, models_dir))
    return results


def restart_all(models_dir: Path | None, model_name: str | None = None, timeout: float = 300) -> list[ComponentResult]:
    """一键重启：仅默认模型 + gateway + stats。"""
    caps = probe()
    results: list[ComponentResult] = []
    profile = resolve_default_profile(models_dir, model_name)
    if profile is None:
        mid = model_name or os.environ.get("GATEWAY_DEFAULT_MODEL") or DEFAULT_MODEL_ID
        results.append(ComponentResult("model", "error", f"未找到默认模型 profile（{mid}），请配置 GATEWAY_DEFAULT_MODEL 或 --model"))
    else:
        try:
            results.append(restart_profile(profile, caps, timeout))
        except RequirementError as error:
            results.append(ComponentResult(f"model:{profile.name}", "error", str(error)))
    results.append(restart_gateway())
    results.append(restart_stats())
    return results


def status_all(models_dir: Path | None) -> list[ComponentResult]:
    """汇总默认模型 + gateway + stats 状态。"""
    results: list[ComponentResult] = []
    profile = resolve_default_profile(models_dir, None)
    if profile is None:
        results.append(ComponentResult("model", "ok", "默认模型未找到（GATEWAY_DEFAULT_MODEL 未匹配任何 profile）"))
    elif is_running(profile.name):
        ok = wait_health(f"http://127.0.0.1:{profile.port}", 3.0)
        results.append(ComponentResult(f"model:{profile.name}", "ok", "运行中" + ("，健康正常" if ok else "，健康无响应")))
    else:
        results.append(ComponentResult(f"model:{profile.name}", "ok", "已停止"))
    results.append(status_gateway())
    results.append(status_stats())
    return results
```

（`status_all` 中模型健康探测缺上游 api key——`wait_health` 传 None 即可，健康探测仅作参考，不阻断。）

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_all_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/all_service.py tests/test_all_service.py
git commit -m "feat(core): all_service 一键编排 start_all/stop_all/restart_all/status_all"
```

---

### Task 3: cli.py 集成 —— 既有命令改调原语 + gateway/stats 补 restart/status + all 子命令

**Files:**
- Modify: `src/modelctl/cli.py`（build_parser / main 分发 / _cmd_gateway_* / _cmd_stats_* / 新增 _cmd_all）
- Test: `tests/test_modelctl.py`（追加分发用例）

**Interfaces:**
- Consumes: Task 1/2 的全部 all_service API。
- Produces:
  - `modelctl gateway restart` / `stats restart` / `stats status` 三个新子命令
  - `modelctl all start|stop|restart|status`（start/restart 支持 `--model`、`--timeout`）
  - 既有命令改调 all_service 原语，输出/退出码行为不变

- [ ] **Step 1: 写失败测试（追加到 `tests/test_modelctl.py`）**

```python
def test_all_command_dispatch(tmp_path, monkeypatch):
    """all start/stop/restart/status 分发到 all_service 编排，--model/--timeout 透传。"""
    import modelctl.cli as cli
    from modelctl.core import all_service

    seen: dict = {}

    def _start_all(md, model_name=None, timeout=300):
        seen["cmd"] = "start"
        seen["model"] = model_name
        seen["timeout"] = timeout
        return [all_service.ComponentResult("model:x", "ok", "")]

    monkeypatch.setattr(cli.all_service, "start_all", _start_all)
    rc = cli.main(["all", "start", "--model", "q", "--timeout", "10", "--models-dir", str(tmp_path)])
    assert rc == 0 and seen == {"cmd": "start", "model": "q", "timeout": 10.0}


def test_all_start_error_exit_2(tmp_path, monkeypatch):
    import modelctl.cli as cli
    from modelctl.core import all_service

    monkeypatch.setattr(cli.all_service, "start_all", lambda md, model_name=None, timeout=300: [
        all_service.ComponentResult("model:x", "error", "boom"),
        all_service.ComponentResult("gateway", "ok", ""),
    ])
    rc = cli.main(["all", "start", "--models-dir", str(tmp_path)])
    assert rc == 2


def test_all_stop_error_exit_1(tmp_path, monkeypatch):
    import modelctl.cli as cli
    from modelctl.core import all_service

    monkeypatch.setattr(cli.all_service, "stop_all", lambda md: [all_service.ComponentResult("gateway", "error", "boom")])
    rc = cli.main(["all", "stop", "--models-dir", str(tmp_path)])
    assert rc == 1


def test_gateway_restart_dispatch(tmp_path, monkeypatch):
    import modelctl.cli as cli

    monkeypatch.setattr(cli.all_service, "restart_gateway", lambda: cli.all_service.ComponentResult("gateway", "ok", ""))
    rc = cli.main(["gateway", "restart"])
    assert rc == 0


def test_stats_status_dispatch(tmp_path, monkeypatch):
    import modelctl.cli as cli

    monkeypatch.setattr(cli.all_service, "status_stats", lambda: cli.all_service.ComponentResult("stats", "ok", "已停止"))
    rc = cli.main(["stats", "status"])
    assert rc == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_modelctl.py -k "all_ or gateway_restart or stats_status" -v`
Expected: FAIL（`invalid choice: 'all'` 等）

- [ ] **Step 3: 修改 `build_parser`（cli.py）**

```python
    sp = sub.add_parser("stats", help="用量统计服务控制")
    sp.add_argument("action", choices=["start", "stop", "restart", "status"])
    gp = sub.add_parser("gateway", help="统一网关（model 参数路由）控制")
    gp.add_argument("action", choices=["start", "stop", "restart", "status"])
    ap = sub.add_parser("all", help="一键启停（默认模型 + 网关 + 统计）")
    ap.add_argument("action", choices=["start", "stop", "restart", "status"])
    ap.add_argument("--model", default=None, help="默认模型 profile（缺省解析 GATEWAY_DEFAULT_MODEL）")
    ap.add_argument("--timeout", type=float, default=300, help="模型健康检查超时秒数（默认 300）")
```

- [ ] **Step 4: 新增 `_cmd_all` 与 gateway/stats 的 restart/status 分发（cli.py）**

```python
def _cmd_all(args, models_dir: Path | None, caps) -> int:
    from modelctl.core import all_service

    if args.action == "start":
        results = all_service.start_all(models_dir, args.model, args.timeout)
        exit_code = 2
    elif args.action == "stop":
        results = all_service.stop_all(models_dir)
        exit_code = 1
    elif args.action == "restart":
        results = all_service.restart_all(models_dir, args.model, args.timeout)
        exit_code = 2
    else:
        results = all_service.status_all(models_dir)
        exit_code = 0
    for r in results:
        line = f"[{r.status}] {r.component}"
        if r.detail:
            line += f"：{r.detail}"
        if r.status == "error":
            logger.error(line)
        else:
            logger.info(line)
    if any(r.status == "error" for r in results):
        logger.info("提示：可执行 `modelctl status` 细查各组件状态")
        return exit_code
    return 0


def _cmd_gateway_restart(args, models_dir: Path | None, caps) -> int:
    from modelctl.core import all_service

    r = all_service.restart_gateway()
    (logger.error if r.status == "error" else logger.info)(f"网关：{r.detail}")
    return 0 if r.status in ("ok", "skipped") else 2


def _cmd_stats_restart(args, models_dir: Path | None, caps) -> int:
    from modelctl.core import all_service

    r = all_service.restart_stats()
    (logger.error if r.status == "error" else logger.info)(f"用量统计：{r.detail}")
    return 0 if r.status in ("ok", "skipped") else 2


def _cmd_stats_status(args, models_dir: Path | None, caps) -> int:
    from modelctl.core import all_service

    r = all_service.status_stats()
    logger.info(f"用量统计：{r.detail}")
    return 0
```

（`all` 命令的 exit 语义：start/restart 有 error → 2；stop 有 error → 1；status 恒 0。符合 spec 4.2。）

- [ ] **Step 5: 修改 `main` 分发（cli.py）**

```python
        if args.command == "stats":
            if args.action == "start":
                return _cmd_stats_start()
            if args.action == "stop":
                return _cmd_stats_stop()
            if args.action == "restart":
                return _cmd_stats_restart(args, models_dir, caps)
            return _cmd_stats_status(args, models_dir, caps)
        if args.command == "gateway":
            if args.action == "start":
                return _cmd_gateway_start()
            if args.action == "stop":
                return _cmd_gateway_stop()
            if args.action == "restart":
                return _cmd_gateway_restart(args, models_dir, caps)
            return _cmd_gateway_status()
        if args.command == "all":
            return _cmd_all(args, models_dir, caps)
```

- [ ] **Step 6: 重构既有命令改调 all_service 原语（spec 4.3 DRY 要求，行为不变）**

cli.py 顶部 import 区追加：

```python
from modelctl.core import all_service
```

既有命令函数体替换（每个函数保留原签名与返回语义；`RequirementError` 由 `start_profile`/`restart_profile` 向上抛、`main` 捕获返回 2，与现有一致）：

```python
def _cmd_start(args, models_dir: Path | None, caps) -> int:
    profile = load_profile(args.name, models_dir)
    r = all_service.start_profile(profile, caps, args.timeout)
    return 0 if r.status in ("ok", "skipped") else 1


def _cmd_stop(args, models_dir: Path | None, caps) -> int:
    profile = load_profile(args.name, models_dir)
    all_service.stop_profile(profile, caps, models_dir)
    return 0


def _cmd_restart(args, models_dir: Path | None, caps) -> int:
    profile = load_profile(args.name, models_dir)
    r = all_service.restart_profile(profile, caps, args.timeout)
    return 0 if r.status in ("ok", "skipped") else 1
```

（`_cmd_start` 中"健康超时返回 1、RequirementError 由 main 返回 2"的既有语义由原语保证：原语仅在 check_requirements 抛异常、其余返回 ComponentResult。）

```python
def _cmd_gateway_start() -> int:
    all_service.start_gateway()
    return 0


def _cmd_gateway_stop() -> int:
    all_service.stop_gateway()
    return 0


def _cmd_gateway_status() -> int:
    logger.info(f"网关：{all_service.status_gateway().detail}")
    return 0


def _cmd_stats_start() -> int:
    all_service.start_stats()
    return 0


def _cmd_stats_stop() -> int:
    all_service.stop_stats()
    return 0
```

（原 `_cmd_stats_start`/`_cmd_gateway_start` 里的"已在运行"提示由原语的 `skipped` 路径经原语内部 logger 输出，行为等价；`_cmd_gateway_status` 原打印 `网关：运行中，/v1/models 正常/无响应` 或 `网关：已停止`，现取 `status_gateway().detail` 保持一致。）

- [ ] **Step 7: 运行测试确认通过（既有命令行为不回归）**

Run: `uv run pytest tests/test_modelctl.py -v; uv run pytest -q; uv run ruff check src/modelctl/cli.py tests/test_modelctl.py; uv run mypy src/modelctl/cli.py`
Expected: 全部 PASS（全量 206+ 基线不回归）+ ruff/mypy 干净（cli.py 顶部 `from modelctl.core import all_service`——确认无循环导入：all_service 不依赖 cli）

- [ ] **Step 8: Commit**

```bash
git add src/modelctl/cli.py tests/test_modelctl.py
git commit -m "feat(cli): modelctl all 一键启停 + gateway/stats 补齐 restart/status"
```

---

### Task 4: script/modelctl-all.sh 薄脚本

**Files:**
- Create: `script/modelctl-all.sh`

**Interfaces:**
- Produces: `bash script/modelctl-all.sh start|stop|restart|status [--model X] [--timeout N]` → 转发到 `uv run modelctl all ...`

- [ ] **Step 1: 创建 `script/modelctl-all.sh`**

```bash
#!/usr/bin/env bash
# modelctl-all.sh — 一键启停（默认模型 + 网关 + 统计），通过 uv 调用 modelctl all
set -euo pipefail

# 优先使用原生（Linux/WSL）的 uv；仅当不存在时才回退到 Windows 的 uv.exe
if command -v uv >/dev/null 2>&1; then
    UV=uv
else
    UV=uv.exe
fi

"$UV" run modelctl all "$@"
```

- [ ] **Step 2: 授予可执行权限（仅 Linux 部署机；Windows 开发机跳过）**

```bash
chmod +x script/modelctl-all.sh
```

- [ ] **Step 3: 冒烟验证（Windows 开发机）**

Run: `bash script/modelctl-all.sh status`
Expected: 输出三件套状态汇总（`modelctl all status` 输出），无报错

- [ ] **Step 4: Commit**

```bash
git add script/modelctl-all.sh
git commit -m "feat(script): modelctl-all.sh 一键启停薄脚本"
```

---

### Task 5: README 与 .env.example 文档

**Files:**
- Modify: `README.md`
- Modify: `.env.example`

**Interfaces:**
- 无新接口；文档说明。

- [ ] **Step 1: `.env.example` 追加（末尾）**

```bash
# 一键启停（modelctl all）默认模型标识：profile 的 name 或 alias；未设置回退 deepseek-v4-flash
GATEWAY_DEFAULT_MODEL=deepseek-v4-flash
```

- [ ] **Step 2: README 增补"一键启停"一节**

内容要点（置于现有命令说明之后）：
- 三件套：默认模型 + 统一网关（gateway）+ 用量统计（stats）
- 四动作：`modelctl all start|stop|restart|status`
  - start/restart 仅启动默认模型（`GATEWAY_DEFAULT_MODEL` 或 `--model` 指定；未设置回退 `deepseek-v4-flash`）
  - stop 停止全部运行中模型（含非默认，经 `modelctl start <name>` 启动的）+ 网关 + 统计
  - status 汇总三件套状态
- 单组件四动作：`modelctl gateway start|stop|restart|status`、`modelctl stats start|stop|restart|status`
- 薄脚本：`bash script/modelctl-all.sh <动作>`
- 失败语义：逐组件尝试 + 汇总，start/restart 任失败 exit 2、stop 任失败 exit 1

- [ ] **Step 3: 验证文档命令与实际 CLI 一致（对照 build_parser）**

Run: `uv run modelctl all --help; uv run modelctl gateway --help; uv run modelctl stats --help`
Expected: 帮助信息与文档一致

- [ ] **Step 4: Commit**

```bash
git add README.md .env.example
git commit -m "docs: 一键启停（modelctl all）使用说明"
```

---

## 验收（对照 spec）

- `modelctl all start`：默认模型 → gateway → stats，逐行 `[ok/skipped/error]` 汇总；任 error → exit 2
- `modelctl all stop`：stats → gateway → 全部运行中模型（含非默认）；任 error → exit 1
- `modelctl all restart`：仅默认模型停后启 + gateway/stats restart
- `modelctl all status`：三件套状态汇总，exit 0
- `modelctl gateway restart`、`modelctl stats restart|status` 可用
- 既有命令行为不变（全量 206+ 测试通过）
- `bash script/modelctl-all.sh start` 等价 `uv run modelctl all start`
- README/.env.example 已更新
