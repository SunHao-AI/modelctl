# 环境页 Docker 一键安装（Windows-only）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** WebUI 环境页为 Windows 桌面用户提供 "Docker 一键安装" 路径：winget 静默装 Docker Desktop + 落 `~/.docker/daemon.json` + 5 阶段 B 引导卡片；CLI 增 `--os=linux|windows` 支持跨平台预览并硬拒跨平台执行；后端新增 `POST /envs/docker/install` + SSE + `POST /envs/docker/system-action`。

**Architecture:** 对称式双文件——新增 `src/modelctl/core/windows_setup.py` 与既有 `docker_setup.py` 平行，`docker_setup.run_install` 增 `os_hint`/`on_stage` 参数做平台分派（缺省 None 完全向后兼容）；`StageEvent` frozen dataclass（`core/sse_stage_event.py`）作 SSE 后端 ↔ windows_setup callback ↔ 前端 TS 的单一事实来源；后端 `admin_envs.py` 复用 `TaskManager`（`request.app.state.task_manager` 全局单例）；前端 `DockerInstallPanel.vue` 4 态状态机 + UAC 弹窗 + 阶段 B 卡片 + 4 按钮，接入 `EnvsView.vue`（Linux 降级为 el-alert）。

**Tech Stack:** FastAPI + SSE（`StreamingResponse`）+ pytest（`TestClient`）；Vue 3 `<script setup>` + TypeScript + Element Plus + EventSource（浏览器原生 SSE）；`npm run build`（vue-tsc + vite）做前端门禁。

**Spec:** `docs/superpowers/specs/2026-09-07-envs-docker-install-windows-only-design.md`（commit `5207f7b`，593 行）

---

## Global Constraints

- 回复与代码注释用中文；PowerShell 多命令用 `;` 分隔。
- UI 显示均为虚拟 ID；时间格式 `YYYY-MM-DD HH:mm:ss`。
- 严禁任何 DDL；只写三处：`~/.docker/daemon.json`（合并）/ `core/sse_stage_event.py`（新文件）/ 前端构建产物。
- Windows 侧**只**允许执行 `winget`/`explorer.exe`/`shutdown.exe`（`close_fds=True`）；不写注册表、不调 PATH、不 dism、不 wsl --install、不关 Defender。
- Python 测试命令固定：`$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; d:\WorkPlace\Pycharm\modelctl\.venvs\gateway\Scripts\python.exe -m pytest <target> -v`
- 前端门禁固定：`cd d:\WorkPlace\Pycharm\modelctl\web; npm run build`（要求 exit 0）
- 同一文件多处修改必须**串行**编辑。
- 每个 Task 结束 commit 一次；提交信息 conventional commits + Chinese scope。
- 已知环境噪音：全量 pytest 有 timezone/engine-docker 既有失败（非本分支回归）；只要求本计划新增及直接涉及的测试通过。
- 新建 Python 测试文件必须带仓库标准文件头（参照 `tests/test_core_docker_setup.py` 前 10 行）。
- **本机（Windows 开发机）限制**（所有 Task）：不跑真 winget / 不弹真 UAC / 不真调 shutdown / 不点真"一键安装"按钮——所有子进程用 `unittest.mock.patch` 打补丁。
- `stage` 字段 12 值枚举（三端字符串联合体）：`detect_winget | detect_wsl2 | winget_running | need_UAC | winget_done | write_daemon_json | test_docker_version | test_gpus | post_install_plan | already_installed | done | error`
- 前端**不新增本地依赖**；**不引入 e2e 框架**。
- `POST /envs/docker/install` 5 分钟去重窗 + 每用户 max 3 未完成任务。
- SSE 用户 close 不 kill winget（client-side close 语义，子进程独立跑完）。
- `windows_setup.py` 必须用 `import sys` + `sys.platform`（不能 `from sys import platform`），否则测试 monkeypatch `ws.sys.platform` 会失效。

---

### Task 1: `windows_setup.py` + `core/sse_stage_event.py`（TDD 单元层，全 mock）

**Files:**
- Create: `src/modelctl/core/sse_stage_event.py`
- Create: `src/modelctl/core/windows_setup.py`
- Create: `tests/test_core_windows_setup.py`（19 用例）
- Modify: `tests/test_core_docker_setup.py`（末尾追加 1 个 Check 字段名锚定用例）

**Interfaces:**
- Consumes: `docker_setup` 的 `DEFAULT_REGISTRY_MIRRORS / DEAD_REGISTRY_MIRRORS / DEFAULT_MAX_CONCURRENT_DOWNLOADS / resolve_registry_mirrors / split_dead_mirrors / is_dead_mirror`（import 不复制）
- Produces（后续 Task 逐字依赖）:
  - `core/sse_stage_event.py`: `StageEvent` frozen dataclass — `type:str / stage:str / message:str / ts:str / code:int|None = None / payload:dict|None = None`；`to_sse_dict() -> dict` 去掉 None 字段
  - `windows_setup.WINGET_ID = "Docker.DockerDesktop"`
  - `windows_setup.DESKTOP_EXE_CANDIDATES: tuple[Path, ...]` (Program Files + LOCALAPPDATA 候选)
  - `windows_setup.DAEMON_JSON: Path = Path.home() / ".docker" / "daemon.json"`
  - `windows_setup.UAC_SILENCE_SEC: int = 20`
  - `windows_setup.GPU_SMOKE_IMAGE = "nvidia/cuda:12.4.0-base-ubuntu22.04"`
  - `windows_setup.WSL_PROBE_TIMEOUT: int = 15`
  - `@dataclass(frozen=True) class Check: key/label/ok/detail/hint=""`（hint 是比 docker_setup.Check 多出的第 5 字段）
  - `windows_setup.STAGES: frozenset[str]`（12 值枚举）
  - `path_level_missing() -> list[str]`（which winget→which docker）
  - `diagnose() -> list[Check]`（5 项，nvidia 无则 skip）
  - `install_steps(registry_mirrors, max_downloads) -> list[tuple[str,str]]`（4 步）
  - `render_instructions(registry_mirrors, max_downloads) -> str`
  - `_merge_daemon_json(registry_mirrors, max_downloads=DEFAULT) -> bool`
  - `run_install(registry_mirrors, max_downloads, on_stage=None) -> int`（0/2/3）
  - `post_install_plan(nvidia_present=False) -> dict`（5 步 steps）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_core_windows_setup.py`，19 个用例覆盖以下：

```
1.  test_check_dataclass_fields_match_docker_setup
2.  test_path_level_missing_no_winget
3.  test_path_level_missing_no_docker
4.  test_path_level_missing_all_present
5.  test_diagnose_returns_five_checks
6.  test_diagnose_gpu_skip_when_no_nvidia_smi
7.  test_diagnose_gpu_smoke_run_called_when_nvidia_present
8.  test_install_steps_order_and_winget_cmd
9.  test_render_instructions_contains_key_lines
10. test_merge_daemon_json_creates_parent_dir
11. test_merge_daemon_json_preserves_runtime_fields
12. test_merge_daemon_json_removes_dead_mirrors
13. test_merge_daemon_json_union_keeps_user_order
14. test_merge_daemon_json_no_change_when_idempotent
15. test_run_install_rejects_non_win32
16. test_run_install_no_winget_returns_2
17. test_run_install_already_installed_branch
18. test_run_install_winget_fail_emits_error
19. test_run_install_uac_mask_emits_need_uac
20. test_post_install_plan_has_five_steps
21. test_stage_event_frozen_dataclass
```

关键 mock 模式（全部用例统一）：
- `monkeypatch.setattr(ws.sys, "platform", "win32")` 模拟 Windows
- `monkeypatch.setattr(ws.shutil, "which", lambda name: ...)` 模拟 PATH 探测
- `monkeypatch.setattr(ws.subprocess, "run", MagicMock(...))` / `monkeypatch.setattr(ws.subprocess, "Popen", MagicMock(...))`
- `monkeypatch.setattr(ws, "DAEMON_JSON", tmp_path / ".docker" / "daemon.json")` 隔离文件 IO
- `monkeypatch.setattr(ws, "DESKTOP_EXE_CANDIDATES", [tmp_exe])` 模拟 Desktop 已装

`_merge_daemon_json` 测试用 `@pytest.fixture() def fake_home_ctor` 重定向到 `tmp_path` 处理路径。

`run_install` 阶段序列断言：
- 完整 winget 分支: `detect_winget → detect_wsl2 → winget_running → winget_done → write_daemon_json → test_docker_version → [test_gpus] → post_install_plan → done`
- 已装分支: `detect_winget → already_installed → write_daemon_json → test_docker_version → [test_gpus] → post_install_plan → done`
- 失败分支: 任意阶段失败 → `error`(code=2|3, payload) 终止

`StageEvent` 契约：`frozen=True`；`code/payload` 缺省 None（`to_sse_dict()` 不输出 None 字段）。

- [ ] **Step 2: 运行确认失败**

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; d:\WorkPlace\Pycharm\modelctl\.venvs\gateway\Scripts\python.exe -m pytest tests/test_core_windows_setup.py -v`
Expected: 全 FAIL — `ModuleNotFoundError: No module named 'modelctl.core.windows_setup'` 或 `sse_stage_event`

- [ ] **Step 3: 最小实现**

**3a — `src/modelctl/core/sse_stage_event.py`（新建）**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/sse_stage_event.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : Docker 一键安装 SSE 阶段事件统一数据契约
# ===============================================================================

"""core/sse_stage_event.py — Docker 一键安装 SSE 阶段事件数据契约（单一事实来源）。"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class StageEvent:
    type: str            # "stage" | "log" | "error" | "complete"
    stage: str           # 12 值枚举之一
    message: str         # 人类可读中文；log 时是原始 stdout 一行
    ts: str              # "YYYY-MM-DD HH:mm:ss"
    code: int | None = None       # 仅 error/complete
    payload: dict | None = None   # post_install_plan: {"steps":[...]}; error: {"tail":[...]}

    def to_sse_dict(self) -> dict:
        out = {"type": self.type, "stage": self.stage, "message": self.message, "ts": self.ts}
        if self.code is not None:
            out["code"] = self.code
        if self.payload is not None:
            out["payload"] = self.payload
        return out
```

**3b — `src/modelctl/core/windows_setup.py`（新建）**

关键实现要点（实现子代理按此骨架填完整，注意以下必须遵守的设计）：

1. **文件头**：仓库标准 10 行 + docstring
2. **imports**：`import json, shutil, subprocess, sys`（注意是 `import sys` 不是 `from sys import platform`）+ `from dataclasses import dataclass` + `from pathlib import Path` + `from typing import Callable` + `from loguru import logger` + `from modelctl.core.docker_setup import DEFAULT_REGISTRY_MIRRORS, DEAD_REGISTRY_MIRRORS, DEFAULT_MAX_CONCURRENT_DOWNLOADS, resolve_registry_mirrors, split_dead_mirrors, is_dead_mirror` + `from modelctl.core.sse_stage_event import StageEvent`
3. **常量**：`WINGET_ID / DESKTOP_EXE_CANDIDATES(3 个候选路径) / DAEMON_JSON / UAC_SILENCE_SEC=20 / GPU_SMOKE_IMAGE / WSL_PROBE_TIMEOUT=15`
4. **`@dataclass(frozen=True) Check`:** 5 字段 `key/label/ok/detail/hint=""`
5. **`STAGES: frozenset[str]`** — 12 值枚举
6. **`_ts_now() -> str`**：`datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")`
7. **`path_level_missing()`**：which winget→无→`["winget"]`；有→which docker→无→`["docker"]`；都齐→`[]`
8. **`_probe_wsl2() -> tuple[int,str]`**（注意函数名是 `_probe_wsl2`，不是 `_prwsl2`）：`subprocess.run(["wsl","--version"], timeout=WSL_PROBE_TIMEOUT)` → `(proc.returncode, proc.stdout.strip())`；异常→`(1, f"probe-error: {exc}")`
9. **`_desktop_installed() -> bool`**：`any(p.is_file() for p in DESKTOP_EXE_CANDIDATES)`
10. **`_gpu_smoke() -> tuple[int,str]`**：`docker run --rm --gpus all <GPU_SMOKE_IMAGE> nvidia-smi`，timeout 120
11. **`diagnose() -> list[Check]`**：5 项（winget/wsl2/desktop/docker_cli/gpus）
12. **`install_steps()`**：4 步 tuple（`_modelctl_detect_prereqs` / `winget install ...` / `_modelctl_merge_daemon_json` / `_modelctl_verify_after_install`）
13. **`render_instructions()`**：`#` 注释开头的 PowerShell 指引（非可执行脚本，复述 install_steps 供人阅读）
14. **`_merge_daemon_json(registry_mirrors, max_downloads=DEFAULT) -> bool`**：读旧文件（`DAEMON_JSON.is_file()` 判断），`json.loads`，`split_dead_mirrors` 剔停服源，union 保序追加（用户 preferred 排前），`max_downloads>0` 时覆盖；无变化→False；有变化→`write_text(json.dumps(indent=2)+"\n")`→True
15. **`run_install(registry_mirrors, max_downloads, on_stage=None) -> int`**：
    - 内部 `emit(stage, message, etype="stage", code=None, payload=None)` 闭包：构建 `StageEvent`，`on_stage` callback 异常 catch + `logger.exception(f"on_stage callback 异常：{e_cb}")`，`logger.info` 辅助
    - **平台门禁**：`if sys.platform != "win32"` → `emit("error", f"Windows 安装路径仅 Windows 主机可 --run，当前平台 {sys.platform!r}", etype="error", code=2)` → return 2
    - 阶段序列：detect_winget → detect_wsl2 → check `_desktop_installed()` → 两分支（winget / already_installed）
    - **winget 子进程**：`subprocess.run(["winget","install","-e","--id",WINGET_ID,"--silent","--accept-package-agreements","--accept-source-agreements"], timeout=30*60, ...)`；rc!=0 → emit error(code=3, payload={"tail": last 50 lines}) → return 3
    - **UAC 20s 检测**：若 20s 无 stdout 输出 → `emit("need_UAC", "Windows 系统已弹出 UAC 授权框...")` 但仍继续等
    - write_daemon_json → test_docker_version → [test_gpus] → post_install_plan → done → return 0
16. **`post_install_plan(nvidia_present=False) -> dict`**：`{"steps": [5 步]}`，id 固定序 `[restart, open_desktop, enable_wsl2, enable_gpu, verify]`，每步字段 `{id, label, action, cmd_hint, optional}`；`enable_gpu.optional = not nvidia_present`

**注意**：代码中**禁止**以下 anti-pattern：
- ~~`if exc if False else f"..."`~~（用正常 `except Exception as e_cb:` 块）
- ~~`proc.stdout.splitlines() and tail.extend(...)`~~（用 `tail.extend([l for l in (proc.stdout or "").splitlines() if l.strip()])`）
- ~~`def _prwsl2():`~~（正确拼写是 `def _probe_wsl2():`，所有调用点同步用正确名称）

- [ ] **Step 4: 追加 Check 字段锚定用例**

在 `tests/test_core_docker_setup.py` 末尾追加：

```python
def test_check_dataclass_keeps_5_fields_match_windows_setup():
    """防未来漂移：docker_setup.Check 字段名是 windows_setup.Check 的真子集。"""
    from dataclasses import fields
    from modelctl.core import windows_setup as ws
    ds_fields = {f.name for f in fields(ds.Check)}
    ws_fields = {f.name for f in fields(ws.Check)}
    assert ds_fields <= ws_fields
```

- [ ] **Step 5: 运行确认通过**

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; d:\WorkPlace\Pycharm\modelctl\.venvs\gateway\Scripts\python.exe -m pytest tests/test_core_windows_setup.py tests/test_core_docker_setup.py -v`
Expected: 全绿（20+ 用例）

- [ ] **Step 6: Commit**

```
git add src/modelctl/core/sse_stage_event.py src/modelctl/core/windows_setup.py tests/test_core_windows_setup.py tests/test_core_docker_setup.py
git commit -m "feat(envs-docker): add windows_setup.py (winget Desktop + daemon.json merge) + StageEvent dataclass"
```

---

### Task 2: `docker_setup.run_install` dispatcher + CLI `--os` flag（TDD）

**Files:**
- Modify: `src/modelctl/core/docker_setup.py` L395-L421（`run_install` → dispatcher；原 Linux 逻辑抽为 `_install_linux`）
- Modify: `src/modelctl/cli.py` L185-L195（增 `--os` argument）+ L1021-L1052（`_cmd_env_setup_docker` 按 `--os` 分派 + 跨平台预览/拒绝）
- Modify: `tests/test_cli_env.py`（末尾追加 4 用例）
- Modify: `tests/test_core_docker_setup.py`（末尾追加 3 用例）

**Interfaces:**
- Consumes: Task 1 `windows_setup.run_install(registry_mirrors, max_downloads, on_stage)`
- Produces:
  - `docker_setup.run_install(registry_mirrors, max_downloads, os_hint=None, on_stage=None) -> int`（缺省 None 向后完全兼容）
  - `docker_setup._install_linux(registry_mirrors, max_downloads, on_stage) -> int`（私有；原 Linux 路径原样搬移）
  - CLI `--os {linux,windows}` choices，default None
  - 三种交叉拒绝的 stderr 文案（字面断言）

- [ ] **Step 1: 写失败测试**

`tests/test_core_docker_setup.py` 末尾追加 3 例：
```
- test_run_install_dispatcher_linux_default (monkeypatch platform=linux + mock _install_linux → 0)
- test_run_install_dispatcher_windows_hints_windows (monkeypatch platform=win32 + mock ws_.run_install → 0)
- test_run_install_dispatcher_windows_on_linux_reject (monkeypatch platform=linux + os_hint="windows" → return 2)
```

`tests/test_cli_env.py` 末尾追加 4 例（文件顶部补 `import sys`）：
```
- test_parser_env_setup_docker_os_flag (--os=windows → args.os=="windows"; 无 --os → None; --os=macos → SystemExit)
- test_env_setup_docker_os_linux_preview_cross_platform (Win host + --os=linux 无 --run → stdout 含 "apt-get install docker-ce"; mock run_install 未调)
- test_env_setup_docker_os_linux_run_cross_platform_rejected (Win + --os=linux --run → exit 2 + "Linux 安装路径仅 Linux 主机可 --run")
- test_env_setup_docker_os_windows_run_cross_platform_rejected (Linux CI + --os=windows --run → exit 2 + "Windows 安装路径仅 Windows 主机可 --run")
```

用平台自适配模式：`if sys.platform.startswith("win32"): assert rc==2 ...; else: assert rc in (0,2)`

- [ ] **Step 2: 运行确认失败**

Run: `$env:PYTHONPATH="..."; pytest tests/test_core_docker_setup.py tests/test_cli_env.py -v`
Expected: `_install_linux` 未定义 / `--os` 未注册 → FAIL

- [ ] **Step 3a: `docker_setup.py` dispatcher 改造**

在 `docker_setup.py` 中：

1. 文件顶部 import 区补充（若缺）：`import datetime`（用于 `_install_linux` emit 时间戳）；`from typing import Callable`（for `on_stage` 参数类型）

2. 将 L395-L421 的 `run_install` **重命名为 `_install_linux`** 并加 `on_stage` 参数：
```python
def _install_linux(
    registry_mirrors: list[str] | None = None,
    max_downloads: int | None = None,
    on_stage: Callable[..., None] | None = None,
) -> int:
    if not sys.platform.startswith("linux"):
        return 2
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return 2
    # --- 原 L403-L421 逻辑完全不变 ---
    ...
```

3. 新 `run_install` dispatcher（替代原 `run_install`）：
```python
def run_install(
    registry_mirrors=None, max_downloads=None,
    os_hint: str | None = None,
    on_stage: Callable[..., None] | None = None,
) -> int:
    hint = os_hint or ("windows" if sys.platform == "win32" else "linux")
    if hint == "windows":
        if sys.platform != "win32":
            logger.error(f"Windows 安装路径仅 Windows 主机可 --run，当前平台 {sys.platform!r}")
            return 2
        from modelctl.core import windows_setup
        return windows_setup.run_install(registry_mirrors, max_downloads, on_stage)
    if hint == "linux":
        if not sys.platform.startswith("linux"):
            logger.error(f"Linux 安装路径仅 Linux 主机可 --run，当前平台 {sys.platform!r}")
            return 2
        return _install_linux(registry_mirrors, max_downloads, on_stage)
    logger.error(f"未知 os_hint: {hint!r}")
    return 2
```

- [ ] **Step 3b: `cli.py` 增 `--os` + `_cmd_env_setup_docker` 改造**

1. L185-L195 `env setup docker` 的 `ep` 增加 `--os`：
```python
ep.add_argument("--os", choices=["linux", "windows"], default=None,
                help="目标操作系统（缺省按 sys.platform）；跨平台时可预览安装脚本")
```

2. `_cmd_env_setup_docker(args)` 头部增加平台判定逻辑：
```python
import sys
host_os = "windows" if sys.platform == "win32" else "linux"
target_os = getattr(args, "os", None) or host_os

# --- 现有 diagnose + _print_docker_checks 逻辑保留 ---
# --- 跨平台预览：target_os != host_os 且无 --run → 只 print render_instructions(target_os), return 0 ---
# --- 跨平台执行：target_os != host_os 且有 --run → stderr 精确拒绝文案 + return 2 ---
# --- 同平台执行：docker_setup.run_install(..., os_hint=target_os) ---
```

3. `_print_docker_checks` 内部读 `args.os` 若传了则调用 `windows_setup.diagnose()` / `docker_setup.diagnose()` 按平台选择（**但注意**：跨平台 diagnose 仍走本主机 check（Linux host 上不能 wsl --version），因此 preview 模式仅走 render_instructions，不调 diagnose）。

- [ ] **Step 4: 运行确认通过**

Run: `$env:PYTHONPATH="..."; pytest tests/test_core_docker_setup.py tests/test_cli_env.py -v`
Expected: 全绿（新增 7 用例 + 既有用例不 regression）

- [ ] **Step 5: Commit**

```
git add src/modelctl/core/docker_setup.py src/modelctl/cli.py tests/test_core_docker_setup.py tests/test_cli_env.py
git commit -m "feat(envs-docker): run_install dispatcher (--os linux|windows) + cross-platform preview"
```

---

### Task 3: `admin_envs.py` 后端端点（install / SSE / system-action / diagnose?os=）

**Files:**
- Modify: `src/modelctl/core/webui/admin_envs.py`（注册 5 个新端点 + 模块级 `docker_install_task_manager = TaskManager()`）
- Modify: `tests/test_webui_admin_envs.py`（新增 9 用例）

**Interfaces:**
- Consumes: Task 1 `windows_setup.run_install` + `StageEvent`；Task 2 `docker_setup.run_install` dispatcher；`TaskManager`（`admin_tasks.py`）
- Produces:
  - `GET /admin/api/envs/docker/diagnose?os=windows`（扩展：`os` query 可选，`os=windows` 且非 win32 → 400 + 引导）
  - `POST /admin/api/envs/docker/install` → 202 `{"task_id": "...", "events": "/admin/api/envs/docker/install/<task_id>/events", "os": "..."}`
  - `GET /admin/api/envs/docker/install/{task_id}/events`（SSE，10s 心跳）
  - `GET /admin/api/envs/docker/install/{task_id}`（fallback 只读 `{"stage":"...","done":bool,"last_ts":"..."}`）
  - `POST /admin/api/envs/docker/system-action`（`{"action":"open_desktop"|"restart"}`，Windows-only，verify → 405）

- [ ] **Step 1: 写失败测试**（9 用例，全部 mock）

```
1. test_install_endpoint_401_unauthed
2. test_install_endpoint_202_win32_returns_task_id (monkeypatch sys.platform="win32" + 去重窗 mock)
3. test_install_endpoint_400_linux_os_windows (monkeypatch sys.platform="linux" + body.os="windows")
4. test_install_endpoint_dedup_same_user (mock 已有活跃 task → 返回 already_running: true)
5. test_system_action_endpoint_400_non_win32
6. test_system_action_endpoint_open_desktop_win32_ok (mock subprocess.Popen → 断言参数含 "explorer.exe" + "ms-settings:developers")
7. test_system_action_endpoint_restart_win32_ok (mock Popen → 断言含 "shutdown.exe" + "/r" + "/t" + "5")
8. test_system_action_endpoint_verify_returns_405
9. test_diagnose_endpoint_os_windows_non_win32_400 (monkeypatch sys.platform="linux" + query os=windows)
```

测试模式：`TestClient(app)` 发 HTTP 请求，`monkeypatch` 平台，`mock.patch("modelctl.core.windows_setup.run_install")` 避免真跑。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现 `admin_envs.py` 5 端点**

**关键约束：所有 5 个精确路径（`/docker/diagnose?os=`、`/docker/install`、`/docker/install/{id}/events`、`/docker/install/{id}`、`/docker/system-action`）必须在 L213 `@router.post("/{target}/setup")` 之前注册**（FastAPI 按注册顺序匹配，`{target}` 会吞 `docker`）。

3a. **`GET /docker/diagnose`**（保留已有端点，扩展 query param）:
```python
@router.get("/docker/diagnose")
async def docker_diagnose(request: Request, os: str | None = None):
    target_os = os or ("windows" if sys.platform == "win32" else "linux")
    if target_os == "windows" and sys.platform != "win32":
        raise HTTPException(400, f"Windows 诊断仅 Windows 主机可执行，当前平台 {sys.platform!r}")
    if target_os == "windows":
        from modelctl.core import windows_setup
        checks = windows_setup.diagnose()
    else:
        checks = docker_setup.diagnose()
    return {"platform": target_os, "checks": checks}
```

3b. **`POST /docker/install`**（202 + task_id）:
- 模块级单例：`docker_install_task_manager = TaskManager()`
- Body validation: `{"os": "linux"|"windows" (optional), "registry_mirrors": [...], "max_concurrent_downloads": 0~8}`
- 非 win32 + `os=windows` → 400
- **5 分钟去重窗**：`_user_active_installs: dict[str, dict]` 模块级 dict（key=user_id, value={"task_id","started_at"}）；已有未过期 → 返原 task_id + `already_running: true`
- 每用户 max 3 未完成任务检查
- 创建 Task（`docker_install_task_manager.create_task(kind="docker", action="install", target=os_referenced)`）
- 后台线程 `threading.Thread(target=_run_docker_install_task, daemon=True).start()`
- 线程内调 `windows_setup.run_install(..., on_stage=lambda ev: task.event("stage", ev.to_sse_dict()))`
- 响应 202 `{"task_id": ..., "events": f"/admin/api/envs/docker/install/{task.id}/events", "os": ...}`

3c. **`GET /docker/install/{task_id}/events`**（SSE）:
```python
@router.get("/docker/install/{task_id}/events")
async def docker_install_sse(task_id: str, request: Request):
    task = docker_install_task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "install task not found")
    async def gen():
        # 先推当前 state, 再 subscribe → 10s 心跳 → done 收流
        ...  # 参照 admin_router._sse_task_stream 的 async 生成器模式
    return StreamingResponse(gen(), media_type="text/event-stream")
```

3d. **`GET /docker/install/{task_id}`**（fallback 只读）:
```python
@router.get("/docker/install/{task_id}")
async def docker_install_status(task_id: str):
    task = docker_install_task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404)
    return {"stage": task.detail.get("stage", "unknown"), "done": task.status in ("done","error"), "last_ts": task.finished_at or task.started_at}
```

3e. **`POST /docker/system-action`**:
```python
@router.post("/docker/system-action")
async def docker_system_action(request: Request):
    body = await request.json()
    action = body.get("action")
    if action not in ("open_desktop", "restart", "verify"):
        raise HTTPException(400, f"action 必须为 open_desktop|restart|verify")
    if action == "verify":
        raise HTTPException(405, "verify 请使用 GET /docker/diagnose?os=windows")
    if sys.platform != "win32":
        raise HTTPException(400, f"{action} 仅 Windows 主机可用")
    if action == "open_desktop":
        ctypes.windll.shell32.ShellExecuteW(None, "open", "explorer.exe", "ms-settings:developers", None, 1)
        executed = "explorer.exe ms-settings:developers"
    elif action == "restart":
        subprocess.Popen(["shutdown.exe", "/r", "/t", "5", "/c", "modelctl 正在重启以启用 WSL2 backend"], close_fds=True)
        executed = "shutdown.exe /r /t 5"
    return {"executed": executed, "ts": _ts_now()}
```

注意：`admin_envs.py` 需要 `import sys, threading, subprocess, time`（检查文件顶部已有哪些，缺的补上）。

- [ ] **Step 4: 运行确认通过**

Run: `$env:PYTHONPATH="..."; pytest tests/test_webui_admin_envs.py -v`
Expected: 9 新用例全绿 + 既有用例不 regression

- [ ] **Step 5: Commit**

```
git add src/modelctl/core/webui/admin_envs.py tests/test_webui_admin_envs.py
git commit -m "feat(envs-docker): POST /envs/docker/install + SSE + system-action + diagnose?os= endpoints"
```

---

### Task 4: 前端 `DockerInstallPanel.vue` + `EnvsView.vue` 接入 + 验收

**Files:**
- Modify: `web/src/api/types.ts`（新增 `DockerSSEStage / DockerSSEEvent / PostInstallStep / DockerInstallStartResponse`）
- Modify: `web/src/api/envs.ts`（新增 `startDockerInstall / fetchDockerInstallStatus / systemActionWindows` + `DockerDiagnosePayload` 补 `platform` 字段）
- Create: `web/src/components/docker/DockerInstallPanel.vue`（4 态状态机 + UAC 弹窗 + 阶段 B 卡片 + 4 按钮）
- Modify: `web/src/views/EnvsView.vue`（Docker 旁路区块下方追加 `<DockerInstallPanel>`）
- Create: `docs/known-pitfalls/backend/docker-setup-os-dispatch.md`（4 种 os/platform 交叉矩阵 + StageEvent SoT 位置 + Check 字段名锁测试锚定方式）
- Modify: `docs/known-pitfalls/README.md`（追加一行索引）

**Interfaces:**
- Consumes: Task 1-3 全部后端 API（`/docker/diagnose?os=` / `POST /docker/install` / SSE / `POST /docker/system-action`）
- Produces: 可部署的 WebUI Docker 一键安装前端

- [ ] **Step 1: `types.ts` 新增类型**

```ts
export type DockerSSEEventType = "stage" | "log" | "error" | "complete";
export type DockerSSEStage =
  | "detect_winget" | "detect_wsl2" | "winget_running" | "need_UAC"
  | "winget_done" | "write_daemon_json" | "test_docker_version"
  | "test_gpus" | "post_install_plan" | "already_installed"
  | "done" | "error";
export interface PostInstallStep {
  id: string;
  label: string;
  action: "restart" | "open_desktop" | "verify" | null;
  cmd_hint: string | null;
  optional?: boolean;
}
export interface DockerSSEEvent {
  type: DockerSSEEventType;
  stage: DockerSSEStage;
  message: string;
  ts: string;
  code?: number;
  payload?: { steps: PostInstallStep[] } | { tail: string[] };
}
export interface DockerInstallStartResponse {
  task_id: string;
  events: string;
  os: "linux" | "windows";
  already_running?: boolean;
}
```

`DockerDiagnosePayload`（已有接口）补 `platform: "linux" | "windows"` 字段。

- [ ] **Step 2: `envs.ts` 新增 3 函数**

```ts
export function startDockerInstall(body: { os?: "linux"|"windows"; registry_mirrors?: string[]; max_concurrent_downloads?: number })
  : Promise<DockerInstallStartResponse>;
export function fetchDockerInstallStatus(taskId: string): Promise<{ stage: DockerSSEStage; done: boolean; last_ts: string }>;
export function systemActionWindows(action: "open_desktop"|"restart"): Promise<{ executed: string; ts: string }>;
```

- [ ] **Step 3: `DockerInstallPanel.vue`（新建）**

4 态状态机：`phase = ref<"idle"|"installing"|"need_reboot"|"done"|"error">("idle")`

Props: `{ platform: "linux"|"windows", initialDiagnose?: any }`

方法：
- `startInstall()`: POST `/admin/api/envs/docker/install` → 拿到 `task_id` + `events` 路径 → `new EventSource(events)` 订阅 → `phase="installing"`
- `onSSEEvent(e)`: 解析 → 按 `e.type / e.stage` 分派：
  - `need_UAC` → `uacDialogShow = true`（不中断安装）
  - `post_install_plan` → `phase="need_reboot"`；从 `e.payload.steps` 设 `steps.value`
  - `done` → `phase="done"`
  - `error` → `phase="error"`
  - `log` → `logs.push(e.message)` 限 200 行（>200 shift）
- `openDesktop()`: `systemActionWindows("open_desktop")` + toast
- `doRestart()`: `ElMessageBox.confirm("确认 5 秒后重启计算机?")` → `systemActionWindows("restart")`
- `verify()`: `fetchDockerDiagnose("windows")` → 全 ok ? `phase="done"` : `phase="need_reboot"`
- `onUnmount`: `esRef.value?.close()`

模板：
- Linux 平台（`platform==="linux"`）→ 只显示 `el-alert type="info"` 降级提示
- Windows 平台：`el-steps` 4 段 + 4 按钮（一键安装 / 打开 Docker Desktop / 重启计算机 / 已就绪点我验证）+ 阶段 B 卡片 + 日志折叠 + UAC 弹窗

按钮可见性矩阵：
| 按钮 | idle | installing | need_reboot | done | error |
|---|---|---|---|---|---|
| 一键安装 | ✅ primary | ❌ | ❌ | ❌ | ❌ |
| 打开 Docker Desktop | ❌ | ❌ | ✅ | ❌ | ❌ |
| 重启计算机 | ❌ | ❌ | ✅ warning | ❌ | ❌ |
| 已就绪点我验证 | ❌ | ❌ | ✅ success | ❌ | ✅ success(retry) |

- [ ] **Step 4: `EnvsView.vue` 接入**

在 Docker 旁路区块（已有 `el-card`）下方追加：
```html
<div class="card mt-4">
  <DockerInstallPanel :platform="dockerDiag?.platform ?? 'linux'" :initial-diagnose="dockerDiag" />
</div>
```

`dockerDiag` 响应 handler 需确认包含 `platform` 字段（Task 3 新增的顶层 `platform` 字段）。

- [ ] **Step 5: 前端构建门禁**

Run: `cd d:\WorkPlace\Pycharm\modelctl\web; npm run build`
Expected: exit 0（vue-tsc 类型检查 + vite 打包均通过）

- [ ] **Step 6: CLI 实际跑（Windows 开发机）**

在本机执行以下命令验证（**不跑** `--run`）：
```
modelctl env setup docker --os=linux
→ stdout 含 "apt-get install docker-ce"（跨预览脚本）

modelctl env setup docker --os=linux --run
→ exit 2 + stderr "Linux 安装路径仅 Linux 主机可 --run"

modelctl env setup docker --os=windows
→ stdout 含 "winget install -e --id Docker.DockerDesktop"（PowerShell 指引）
```

- [ ] **Step 7: WebUI 手工 verify**

登录 admin 后打开环境页：
- Windows host：新 "Docker 一键安装" 卡片 read-state — 4 段 steps + "一键安装"按钮可见；**不真点"一键安装"**（避免 UAC + 30min 下载）
- 确认 Task 2 已交付的列表页 / Docker 旁路 / 完整诊断卡现状不变

- [ ] **Step 8: known-pitfalls 沉淀**

创建 `docs/known-pitfalls/backend/docker-setup-os-dispatch.md`：
- dispatcher 4 种 os/platform 交叉矩阵（`--os` 缺省 = 按 host / `--os` 匹配 / `--os` 不匹配×预览 / `--os` 不匹配×执行）
- `StageEvent` SoT 位置（`core/sse_stage_event.py`）+ 前端 `PostInstallStep` 对齐契约
- `windows_setup.Check` 字段名锁与 `docker_setup.Check` 的测试锚定方式（`dataclasses.fields` 比对）
- 精确路径注册顺序（`/docker/install` 必须在 `/{target}/setup` 前）
- `import sys` vs `from sys import platform` 的 monkeypatch 兼容性

在 `docs/known-pitfalls/README.md` 追加一行索引。

- [ ] **Step 9: 全量回归**

Run: `$env:PYTHONPATH="..."; pytest tests/test_core_windows_setup.py tests/test_core_docker_setup.py tests/test_cli_env.py tests/test_webui_admin_envs.py -v`
Expected: 全绿（新增全部用例 + 既有不变）

- [ ] **Step 10: Commit**

```
git add web/src/api/types.ts web/src/api/envs.ts web/src/components/docker/DockerInstallPanel.vue web/src/views/EnvsView.vue docs/known-pitfalls/backend/docker-setup-os-dispatch.md docs/known-pitfalls/README.md
git commit -m "feat(envs-docker): WebUI DockerInstallPanel 4-state machine + EnvsView 接入"
```

---

### 终审

- [ ] **F1: 4 Task 跨层 Pattern 校核**

逐项验证：
- Task1 `windows_setup.Check` 字段名 ⊇ Task1 `docker_setup.Check` 字段名（测试锁定）
- Task1 `StageEvent.to_sse_dict()` 输出 key 集合 = Task3 SSE media 输出 key 集合 = Task4 `DockerSSEEvent` TS 类型
- Task2 `run_install(os_hint="windows")` → 委托 Task1 `windows_setup.run_install`（签名一致）
- Task3 `POST /install` handler 线程内调用 `windows_setup.run_install(on_stage=task.event)` → Task1 内部 `emit` callback
- Task4 `DockerInstallPanel.vue` 的 `onSSEEvent` 分派 stage 值 ∈ Task1 `STAGES` frozenset

- [ ] **F2: Python/TS 一致性**

- 12 值 stage 枚举：Python `STAGES` frozenset = TS `DockerSSEStage` 联合（逐字比对）
- `PostInstallStep` 字段：Python `post_install_plan()["steps"][0]` keys = TS `PostInstallStep` 必填字段
- `DockerInstallStartResponse` 字段：Python 202 body keys = TS 接口

- [ ] **F3: 双端 CI**

- 后端：`pytest` 全绿（无新 regression）
- 前端：`npm run build` exit 0

- [ ] **F4: 无 regression**

- `docker_setup.run_install` 缺省调用（无 `os_hint`/`on_stage`）行为与 Task 2 之前 100% 一致
- `GET /docker/diagnose`（无 `os` query）行为与 Task 2 之前 100% 一致
- CLI `modelctl env setup docker`（无 `--os`）在 Linux host 上行为与 Task 2 之前 100% 一致

---

### Self-Review (4 项)

- [ ] **SR1: 占位符扫描**
  全文无 TBD / "TODO 待定" / 未定义引用。唯一 "TODO refactor"（spec §5.4 备选路径）已注明 "本次 TDD 优先：内联重复代码"。

- [ ] **SR2: 内一致性**
  - Global Constraints `stage` 12 值枚举 = Task1 `STAGES` frozenset = Task4 `DockerSSEStage` TS 联合（逐字比对）
  - Task2 dispatcher 拒绝文案 "Linux 安装路径仅 Linux 主机可 --run，当前平台 ..." = spec §7.1 文案 = Task2 测试断言
  - Task3 精确路径注册顺序约束与 `admin_envs.py` 现状 L213 `POST /{target}/setup` 一致

- [ ] **SR3: scope 检查**
  - macOS / 非 root Linux / 企业 GPO / registry 面板 / Docker Compose 全部不在本 plan scope（spec §14 已列后续演进）
  - Task 4 不做 e2e、不真跑 `--run`、不引入新 npm 依赖

- [ ] **SR4: 歧义检查**
  - `run_install` 签名 `(registry_mirrors=None, max_downloads=None, os_hint=None, on_stage=None)` 三方一致（docker_setup.dispatcher / windows_setup.主入口 / admin_envs.线程调用）
  - `stage` 枚举值 `need_UAC`（大写 UAC）三端一致
  - `system_action` 响应体 `{"executed": str, "ts": str}` 前后端一致
  - `DockerSSEEvent.payload` 可为 `{steps:[...]}` 或 `{tail:[...]}`（前端按 `stage` 区分，不混用）
