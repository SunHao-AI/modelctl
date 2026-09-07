# WebUI 启动失败提供环境创建入口 —— 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 模型启动因"环境缺失/Docker 未就绪"失败时，任务抽屉的失败卡片提供可点击修复入口（Linux 一键创建环境；其他平台跳转环境页定位指引）。

**Architecture:** 后端把启动失败分类为结构化错误码（`venv_missing`/`docker_missing`）附在任务对象上，经 SSE `done` 事件与 `to_dict()` 透传；前端 store 记录 code/engine，TaskDrawer 失败卡片按码渲染动作，EnvsView 支持 `?focus=` 定位高亮。

**Tech Stack:** FastAPI + pytest（后端）；Vue 3 `<script setup>` + TS + Pinia + Tailwind（前端 `web/`，无 vitest，类型验证靠 `vue-tsc --noEmit` via `npm run build`）。

**Spec:** `docs/superpowers/specs/2026-09-07-webui-env-setup-action-design.md`

## Global Constraints

- PowerShell：语句分隔用 `;`，**不支持 `&&`**；多行 commit message 用 `git commit -F <文件>`（不支持 heredoc）。
- 后端测试统一在仓库根 `d:\WorkPlace\Pycharm\modelctl` 执行，且带 `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"`。
- 前端命令在 `d:\WorkPlace\Pycharm\modelctl\web` 执行（`npm run build` = `vue-tsc --noEmit && vite build`，已核实）。
- 注释/docstring 用中文；文件头 4 行注释块保持既有格式。
- `Task.error()` 新参数必须关键字可选（`*, code=None, engine=None`），既有调用点零改动。
- `to_dict()`/done 事件中 `code`/`engine` **仅在非 None 时出现**（向后兼容既有断言）。
- **顺序陷阱（spec §4.1）**：`("Docker 环境未就绪", "docker_missing")` 规则必须插在 `("环境未就绪", "venv_missing")` **之前**，否则被后者抢先命中，Windows 用户会被引导去建 Windows 上不可用的 venv。
- 提交信息风格：`feat(scope): 中文描述` / `test(scope): …`。

---

### Task 1: reconcile 分类基础设施（docker_missing 规则 + classify_start_failure）

**Files:**
- Modify: `src/modelctl/core/cluster/reconcile.py`（`_ERROR_RULES` L126-149、`classify_error` L152-162）
- Test: `tests/test_cluster_reconcile.py`（既有参数表 L56-83 同文件追加）

**Interfaces:**
- Consumes: 无（纯新增）
- Produces: `classify_start_failure(detail: str, engine: str) -> tuple[str | None, str | None]`——Task 3 消费；`_ERROR_RULES` 新增 `("Docker 环境未就绪", "docker_missing")`——`classify_error` 自动获得新码。

- [ ] **Step 1: 写失败测试**

在 `tests/test_cluster_reconcile.py` 的 `test_classify_error_falls_back_to_runtime_capability`（L81-83）之后追加：

```python
def test_classify_error_docker_missing_wins_over_venv():
    """'Docker 环境未就绪' 同时含 '环境未就绪'，docker_missing 规则必须先命中
    （否则 Windows 用户会被引导去建 Windows 不可用的托管 venv，spec §4.1）。"""
    detail = (
        "docker_image 已配置但 Docker 环境未就绪：docker 命令不在 PATH；"
        "nvidia-smi 不在 PATH / nvidia-container-toolkit 未就绪"
        "——执行 `modelctl env setup docker` 查看安装指引"
    )
    assert classify_error(detail) == "docker_missing"


#: 启动失败分类：仅"WebUI 可给修复入口"的码才返回 (code, engine)
@pytest.mark.parametrize("detail,engine,expected", [
    ("vllm 的专用环境未创建，请先执行：modelctl env setup vllm", "vllm", ("venv_missing", "vllm")),
    ("引擎 ollama 的二进制在 PATH 中找不到", "ollama", ("venv_missing", "ollama")),
    ("llamacpp 未安装", "llamacpp", ("venv_missing", "llamacpp")),
    ("docker_image 已配置但 Docker 环境未就绪：docker 命令不在 PATH", "vllm", ("docker_missing", "vllm")),
    # 不可操作码与未命中一律 (None, None)：绝不在端口冲突/OOM 上挂"创建环境"按钮
    ("端口 8101 已被占用（nginx:80）", "vllm", (None, None)),
    ("显存不足：需要 80GiB，实际 40GiB", "vllm", (None, None)),
    ("", "vllm", (None, None)),
    ("完全没见过的报错", "vllm", (None, None)),
])
def test_classify_start_failure(detail, engine, expected):
    assert classify_start_failure(detail, engine) == expected
```

并把该文件顶部（L29 附近）对 reconcile 的 import 补上 `classify_start_failure`（与 `classify_error` 同一 from-import）。

- [ ] **Step 2: 运行确认失败**

```powershell
$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_cluster_reconcile.py -q -k "docker_missing or classify_start_failure" -p no:cacheprovider
```
预期：FAIL（`classify_start_failure` 未定义 / docker 串被判成 `venv_missing`）。

- [ ] **Step 3: 实现**

`reconcile.py`：在 `("PATH 中找不到", "venv_missing"),` 与 `("未安装", "venv_missing"),` 之间插入 `("Docker 环境未就绪", "docker_missing"),`，并替换表头注释为：

```python
#: 有序子串规则表：先具体后笼统。`[gpu_lock]` 带方括号前缀（gpu_lock.py 的报错格式），
#: 必须早于 `gpu_list`（profile 字段名），否则"卡位被占"会被误判成"profile 写错"。
#: 同理 `Docker 环境未就绪`（docker_setup 统一文案）同时包含 `环境未就绪`，必须早于
#: 该 venv 规则，否则配了 docker_image 但 docker 缺失的失败会被判成 venv_missing，
#: 把 Windows 用户引导去建 Windows 上不可用的托管 venv。
```

在 `classify_error` 之后追加：

```python
#: 启动失败中"WebUI 能给出修复入口"的错误码（与前端 TaskDrawer 的修复动作一一对应）
_ACTIONABLE_CODES = frozenset({"docker_missing", "venv_missing"})


def classify_start_failure(detail: str, engine: str) -> tuple[str | None, str | None]:
    """模型启动失败详情 → (错误码, 相关引擎)；不可归类时 (None, None)。

    与 classify_error 共享 _ERROR_RULES（单一关键字源），但语义相反：classify_error
    给集群中心做兜底归类（未知→runtime_capability），本函数只在"命中首个规则且该码
    可操作"时才给码——宁缺毋滥，绝不在端口冲突/OOM 等失败上挂出误导性的"创建环境"
    按钮。engine 由调用方从 profile.engine 直传，不从 detail 猜测。
    """
    text = detail or ""
    for needle, code in _ERROR_RULES:
        if needle in text:
            return (code, engine) if code in _ACTIONABLE_CODES else (None, None)
    return (None, None)
```

- [ ] **Step 4: 运行确认通过（含既有回归）**

```powershell
$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_cluster_reconcile.py -q -p no:cacheprovider
```
预期：全部 PASS（L72-75 的两条"顺序守卫"多用例不得变红）。

- [ ] **Step 5: Commit**

```powershell
git add src/modelctl/core/cluster/reconcile.py tests/test_cluster_reconcile.py
git commit -m "feat(reconcile): 新增 docker_missing 规则与 classify_start_failure 可操作码分类"
```

---

### Task 2: Task 携带错误码（admin_tasks）

**Files:**
- Modify: `src/modelctl/core/webui/admin_tasks.py`（`Task` L42-140）
- Test: `tests/test_admin_tasks.py`（末尾追加）

**Interfaces:**
- Consumes: 无
- Produces: `Task.code: str | None`、`Task.engine: str | None`；`Task.error(exit_code=1, message="", *, code=None, engine=None)`；`to_dict()` 与 done 事件在非 None 时含 `"code"`/`"engine"`——Task 3/4/5 依赖。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.filterwarnings(
    r"ignore:There is no current event loop:DeprecationWarning:modelctl\.core\.webui\.admin_tasks"
)
def test_task_error_carries_code_and_engine():
    """失败任务携带分类码：to_dict 与 done 事件都要带上（抽屉据此渲染修复动作）。"""
    from modelctl.core.webui.admin_tasks import Task

    task = Task(id="task-c1", kind="model_start", action="start", target="m1")
    q = task.subscribe()
    task.error(exit_code=1, message="vllm 的专用环境未创建", code="venv_missing", engine="vllm")
    d = task.to_dict()
    assert d["code"] == "venv_missing"
    assert d["engine"] == "vllm"
    payloads = []
    while not q.empty():
        payloads.append(q.get_nowait())
    done = [p for p in payloads if "event: done" in p]
    assert done and '"code": "venv_missing"' in done[0] and '"engine": "vllm"' in done[0], payloads


def test_task_error_without_code_omits_fields():
    """无分类码时两键必须缺席（旧前端/既有断言的形状不变）。"""
    from modelctl.core.webui.admin_tasks import Task

    task = Task(id="task-c2", kind="model_start", action="start", target="m1")
    task.error(exit_code=1, message="端口 8101 已被占用")
    d = task.to_dict()
    assert "code" not in d
    assert "engine" not in d
```

- [ ] **Step 2: 运行确认失败**

```powershell
$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_admin_tasks.py -q -k "code or omits" -p no:cacheprovider
```
预期：FAIL（`error()` 不接受 code → TypeError）。

- [ ] **Step 3: 实现**

`Task` dataclass 在 `detail: str | None = None` 与 `logs: ...` 之间插入：

```python
    # 失败分类码（venv_missing|docker_missing|...），仅命中分类规则的失败任务携带
    code: str | None = None
    engine: str | None = None
```

`to_dict()` 改为条件加键：

```python
    def to_dict(self) -> dict:
        """对外序列化；排除 _subscribers 内部队列；code/engine 仅携带时出现。"""
        data = {
            "id": self.id,
            "kind": self.kind,
            "action": self.action,
            "target": self.target,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "detail": self.detail,
            "logs": self.logs,
        }
        if self.code is not None:
            data["code"] = self.code
        if self.engine is not None:
            data["engine"] = self.engine
        return data
```

`error()` 整体替换：

```python
    def error(self, exit_code: int = 1, message: str = "", *, code: str | None = None, engine: str | None = None) -> None:
        """标记任务失败并广播 done 事件。exit_code: 2=配置错误, 1=运行错误。

        code/engine 为可选失败分类码（reconcile.classify_start_failure 产出），
        非 None 时随 done 事件与 to_dict 透传给前端渲染修复动作。
        """
        self.status = "error"
        self.exit_code = exit_code
        self.detail = message or self.detail
        self.code = code
        self.engine = engine
        self.finished_at = _now_iso()
        payload = {"status": "error", "exit_code": exit_code, "message": self.detail, "task_id": self.id}
        if code is not None:
            payload["code"] = code
        if engine is not None:
            payload["engine"] = engine
        self.event("done", payload)
```

- [ ] **Step 4: 运行确认通过**

```powershell
$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_admin_tasks.py -q -p no:cacheprovider
```
预期：全部 PASS（含既有 3 条）。

- [ ] **Step 5: Commit**

```powershell
git add src/modelctl/core/webui/admin_tasks.py tests/test_admin_tasks.py
git commit -m "feat(webui): Task 携带失败分类码 code/engine，done 事件与 to_dict 条件透传"
```

---

### Task 3: 启动/重启失败接线（admin_models）

**Files:**
- Modify: `src/modelctl/core/webui/admin_models.py`（`_do_start` L136-173、`_do_restart` L176-211 内共 6 处 `task.error(...)`）
- Test: `tests/test_admin_models_classify.py`（新建）

**Interfaces:**
- Consumes: `classify_start_failure(detail, engine)`（Task 1）、`Task.error(..., code=, engine=)`（Task 2）
- Produces: 启动/重启任务失败时 `task.code/engine` 就绪——`GET /tasks/{id}` 与 SSE done 自动带出（Task 2 保证），前端 Task 4/5 消费。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_admin_models_classify.py`（文件头注释块仿 `tests/test_admin_tasks.py` L1-10，`@File` 改本文件名、`@Desc` "启动失败分类接线测试"）：

```python
"""_do_start/_do_restart 失败路径的分类码接线测试（不拉真引擎，全部 monkeypatch）。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from modelctl.core.webui import admin_models as am  # noqa: E402
from modelctl.core.webui.admin_tasks import Task  # noqa: E402


def _profile(engine: str = "vllm") -> SimpleNamespace:
    """_do_start 只用到 profile.name / profile.engine，鸭子类型即可。"""
    return SimpleNamespace(name=f"qwen-{engine}", engine=engine)


def test_do_start_exception_venv_missing_attaches_code(monkeypatch):
    import modelctl.core.all_service as all_service
    from modelctl.core.envs import EngineEnvError

    def boom(profile, caps, timeout):
        raise EngineEnvError("vllm 的专用环境未创建，请先执行：modelctl env setup vllm")

    monkeypatch.setattr(all_service, "start_profile", boom)
    task = Task(id="task-s1", kind="model_start", action="start", target="qwen-vllm")
    asyncio.run(am._do_start(_profile(), None, 1.0, task, None))
    assert task.status == "error"
    assert task.code == "venv_missing"
    assert task.engine == "vllm"


def test_do_start_error_result_unactionable_no_code(monkeypatch):
    import modelctl.core.all_service as all_service
    from modelctl.core.all_service import ComponentResult

    def fail(profile, caps, timeout):
        return ComponentResult("model:x", "error", "引擎进程提前退出")

    monkeypatch.setattr(all_service, "start_profile", fail)
    task = Task(id="task-s2", kind="model_start", action="start", target="qwen-vllm")
    asyncio.run(am._do_start(_profile(), None, 1.0, task, None))
    assert task.status == "error"
    assert task.code is None and task.engine is None


def test_do_start_requirement_error_keeps_exit_code_2(monkeypatch):
    """RequirementError（如端口占用）仍 exit_code=2，且不可操作码不挂分类。"""
    import modelctl.core.all_service as all_service
    from modelctl.engines.base import RequirementError

    def boom(profile, caps, timeout):
        raise RequirementError("端口 8101 已被占用（nginx:80）")

    monkeypatch.setattr(all_service, "start_profile", boom)
    task = Task(id="task-s3", kind="model_start", action="start", target="qwen-vllm")
    asyncio.run(am._do_start(_profile(), None, 1.0, task, None))
    assert task.exit_code == 2
    assert task.code is None and task.engine is None


def test_do_restart_exception_venv_missing_attaches_code(monkeypatch):
    import modelctl.core.all_service as all_service
    from modelctl.core.envs import EngineEnvError

    def boom(profile, caps, timeout):
        raise EngineEnvError("vllm 的专用环境未创建，请先执行：modelctl env setup vllm")

    monkeypatch.setattr(all_service, "restart_profile", boom)
    task = Task(id="task-s4", kind="model_restart", action="restart", target="qwen-vllm")
    asyncio.run(am._do_restart(_profile(), None, 1.0, task, None))
    assert task.code == "venv_missing" and task.engine == "vllm"
```

> 注意：`_do_start` 内部是 `from modelctl.core.all_service import start_profile`（函数内 import），因此 monkeypatch `all_service.start_profile` 模块属性即可生效。

- [ ] **Step 2: 运行确认失败**

```powershell
$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_admin_models_classify.py -q -p no:cacheprovider
```
预期：前两条 FAIL（`task.code` 为 None——尚未接线）。

- [ ] **Step 3: 实现**

`admin_models.py` 在 `_do_start` 定义之前插入模块级辅助：

```python
def _fail_task(task, exit_code: int, message: str, engine: str) -> None:
    """失败收尾统一入口：附启动失败分类码，任务抽屉据此渲染修复动作。

    分类只认 venv_missing/docker_missing（reconcile.classify_start_failure），
    其余失败 code/engine 均为 None——宁缺毋滥。
    """
    from modelctl.core.cluster.reconcile import classify_start_failure

    code, eng = classify_start_failure(message, engine)
    task.error(exit_code=exit_code, message=message, code=code, engine=eng)
```

`_do_start` 与 `_do_restart` 内共 6 处 `task.error(...)` 全部改为 `_fail_task(task, ...)`（两函数各 3 处：result error 分支 `(1, result.detail)`、RequirementError 分支 `(2, str(exc))`、else 与 ImportError 分支 `(1, str(exc))`；`profile.engine` 作第 4 参）。其余行一律不动。

- [ ] **Step 4: 运行确认通过 + 回归**

```powershell
$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests/test_admin_models_classify.py tests/test_admin_tasks.py -q -p no:cacheprovider
```
预期：全部 PASS。

- [ ] **Step 5: Commit**

```powershell
git add src/modelctl/core/webui/admin_models.py tests/test_admin_models_classify.py
git commit -m "feat(webui): 启动/重启失败任务附带分类码（venv_missing/docker_missing）"
```

---

### Task 4: 前端类型与 store 透传

**Files:**
- Modify: `web/src/api/types.ts`（`TaskInfo` L413-432）
- Modify: `web/src/api/tasks.ts`（`TaskDoneEvent` L28-33）
- Modify: `web/src/stores/tasks.ts`（`TaskRecord` L39-57、`toRecord` L64-80、`onDone` L233-240、轮询终态分支 L173-175）

**Interfaces:**
- Consumes: Task 2 的 done payload / to_dict 中的 `code`/`engine` 键
- Produces: `TaskRecord.code?: string`、`TaskRecord.engine?: string`——Task 5 消费；SSE、降级轮询、刷新 bootstrap（toRecord）三路恢复后均在位。

- [ ] **Step 1: types.ts —— `TaskInfo` 在 `logs: string[];` 之前插入**

```ts
  /** 失败分类码（venv_missing|docker_missing）；未分类时后端不携带 */
  code?: string;
  /** 分类码关联的引擎名（修复动作定位 env target / 环境页 focus） */
  engine?: string;
```

- [ ] **Step 2: api/tasks.ts —— `TaskDoneEvent` 替换为**

```ts
/** SSE done 事件体 */
export interface TaskDoneEvent {
  status: string;
  exit_code: number;
  message?: string;
  task_id: string;
  /** 失败分类码（后端 Task.error 携带；success 时无） */
  code?: string;
  /** 分类码关联引擎名 */
  engine?: string;
}
```

- [ ] **Step 3: stores/tasks.ts —— 四处改动**

`TaskRecord` 的 `exitCode: number;` 之后插入：

```ts
  /** 失败分类码（与后端 Task.code 同名；修复动作渲染依据） */
  code?: string;
  /** 分类码关联引擎名 */
  engine?: string;
```

`toRecord()` 的 `exitCode: t.exit_code,` 之后插入（bootstrap 恢复路径）：

```ts
    code: t.code,
    engine: t.engine,
```

`attachStream` 的 `onDone` 改为（success 时后端不带 → 覆盖为 undefined，语义正确）：

```ts
      onDone: (evt) => {
        t.lastEventAt = Date.now();
        t.code = evt.code;
        t.engine = evt.engine;
        if (evt.status === 'success' || evt.status === 'skipped') {
          finalize(t, evt.status as TaskStatus, t.detail, evt.exit_code);
        } else {
          finalize(t, 'error', evt.message || t.detail || '执行失败', evt.exit_code);
        }
      },
```

`startPolling` 的终态 else 分支改为（finalize 之前写入）：

```ts
        } else {
          cur.code = info.code;
          cur.engine = info.engine;
          finalize(cur, info.status, info.detail ?? '', info.exit_code);
        }
```

- [ ] **Step 4: 类型验证**

```powershell
cd d:\WorkPlace\Pycharm\modelctl\web; npm run typecheck
```
预期：0 错误。

- [ ] **Step 5: Commit**

```powershell
cd d:\WorkPlace\Pycharm\modelctl; git add web/src/api/types.ts web/src/api/tasks.ts web/src/stores/tasks.ts
git commit -m "feat(web): 任务记录透传失败分类码 code/engine（SSE/轮询/刷新三路）"
```

---

### Task 5: TaskDrawer 失败卡片修复动作

**Files:**
- Modify: `web/src/components/common/TaskDrawer.vue`

**Interfaces:**
- Consumes: `TaskRecord.code/engine`（Task 4）、`envSetup(target): Promise<TaskRef>`、`envTargets(): Promise<EnvTargetsResponse>`（`@/api/envs` 既有）、`tasksStore.track()`（既有）
- Produces: `venv_missing`+Linux → [创建环境]（提交 env_setup 任务进抽屉跟踪）；`venv_missing`+非 Linux / `docker_missing` / 平台未知 → [去环境页]（`/envs?focus=<engine>`，Task 6 消费）。

- [ ] **Step 1: script 增加逻辑**

import 区追加：

```ts
import { useRouter } from 'vue-router';
import { envSetup, envTargets } from '@/api/envs';
import { toast } from '@/utils/toast';
```

`const tasksStore = useTasksStore();` 之后追加：

```ts
const router = useRouter();

/** engine → 本平台可建托管 venv（懒加载一次 /envs；失败留空 → 保守"去环境页"） */
const platformMap = ref<Record<string, boolean>>({});
let platformRequested = false;
async function ensurePlatformMap() {
  if (platformRequested) return;
  platformRequested = true;
  try {
    const r = await envTargets();
    const m: Record<string, boolean> = {};
    for (const t of r.targets ?? []) m[t.name] = t.platform_supported;
    platformMap.value = m;
  } catch {
    /* 留空：fixAction 保守降级为 goto（跳转后由环境页展示真相） */
  }
}

/** 本会话已提交过"创建环境"的引擎（防连点；409 由后端 target 互斥兜底） */
const setupSubmitted = ref(new Set<string>());

/** 失败卡片修复动作：'' = 无 | 'setup' = 一键创建环境 | 'goto' = 跳环境页 */
function fixAction(t: TaskRecord): '' | 'setup' | 'goto' {
  if (t.status !== 'error' || !t.code) return '';
  if (t.code === 'docker_missing') return 'goto';
  if (t.code !== 'venv_missing' || !t.engine) return '';
  if (!(t.engine in platformMap.value)) {
    // 平台信息未到：先给"去环境页"，懒加载完成后响应式重算为 [创建环境]
    void ensurePlatformMap();
    return 'goto';
  }
  return platformMap.value[t.engine] ? 'setup' : 'goto';
}

function gotoEnvs(t: TaskRecord) {
  if (t.engine) void router.push({ path: '/envs', query: { focus: t.engine } });
  else void router.push('/envs');
}

async function onCreateEnv(t: TaskRecord) {
  const engine = t.engine;
  if (!engine || setupSubmitted.value.has(engine)) return;
  try {
    const refVal = await envSetup(engine);
    setupSubmitted.value.add(engine);
    // 复用全局任务链路：新 env_setup 任务进抽屉跟踪
    tasksStore.track(refVal, { target: engine, retryFn: () => envSetup(engine) });
  } catch (err) {
    if ((err as { response?: { status?: number } }).response?.status === 409) {
      toast.warning('该环境已有任务在执行中');
    } else {
      toast.error((err as { message?: string })?.message || '提交创建环境任务失败');
    }
  }
}
```

- [ ] **Step 2: template 在"操作行"容器之后、日志 `<pre>` 之前插入**

```html
          <!-- 修复动作：后端分类码驱动（仅 venv_missing/docker_missing 渲染，spec §4.4） -->
          <div v-if="fixAction(t)" class="mt-2 flex flex-wrap items-center gap-2">
            <template v-if="fixAction(t) === 'setup'">
              <button
                class="btn-primary !min-w-0 !px-2 !py-1 text-xs"
                :disabled="setupSubmitted.has(t.engine ?? '')"
                @click="onCreateEnv(t)"
              >
                {{ setupSubmitted.has(t.engine ?? '') ? '创建中…' : '创建环境' }}
              </button>
              <span class="text-xs text-slate-500">环境创建成功后，回到模型详情页点击启动</span>
            </template>
            <button v-else class="btn-ghost !px-2 !py-1 text-xs" @click="gotoEnvs(t)">去环境页</button>
          </div>
```

- [ ] **Step 3: 类型验证**

```powershell
cd d:\WorkPlace\Pycharm\modelctl\web; npm run typecheck
```
预期：0 错误。

- [ ] **Step 4: Commit**

```powershell
cd d:\WorkPlace\Pycharm\modelctl; git add web/src/components/common/TaskDrawer.vue
git commit -m "feat(web): 任务抽屉失败卡片按分类码渲染[创建环境]/[去环境页]修复动作"
```

---

### Task 6: EnvsView 支持 ?focus= 定位高亮

**Files:**
- Modify: `web/src/views/EnvsView.vue`（script 末尾 `onMounted(load)` L102、表格 `<tr>` L130、Docker 旁路 `<section>` L177）

**Interfaces:**
- Consumes: Task 5 跳转携带的 `route.query.focus`（engine/target 名）
- Produces: 滚动定位到目标引擎行（存在时）或 Docker 旁路区块，4s 高亮后自动清除；query 用后即清，刷新不重复滚动。

- [ ] **Step 1: script 改动**

```ts
import { nextTick, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
```

`const targets = ref<EnvTarget[]>([]);` 之前追加：

```ts
const route = useRoute();
const router = useRouter();
/** ?focus= 定位目标名（4s 后清除高亮） */
const focusName = ref('');
```

`onMounted(load);` 替换为：

```ts
onMounted(async () => {
  await load();
  const f = route.query.focus;
  if (typeof f !== 'string' || !f) return;
  // 用后即清：刷新/前进后退不再重复滚动
  void router.replace({ query: {} });
  focusName.value = f;
  await nextTick();
  // 引擎行优先；查不到行（如纯 docker 语境）退回旁路区块
  const el = document.getElementById(`env-row-${f}`) ?? document.getElementById('docker-bypass');
  el?.scrollIntoView({ block: 'center' });
  window.setTimeout(() => {
    if (focusName.value === f) focusName.value = '';
  }, 4000);
});
```

- [ ] **Step 2: template 改动**

`<tr v-for="t in targets" :key="t.name" class="border-b border-slate-800/40">` 改为：

```html
          <tr
            v-for="t in targets"
            :id="`env-row-${t.name}`"
            :key="t.name"
            class="border-b border-slate-800/40 transition-colors"
            :class="focusName === t.name ? 'bg-amber-500/10' : ''"
          >
```

`<section v-if="dockerBypass.length" class="card space-y-3">` 改为：

```html
    <section
      v-if="dockerBypass.length"
      id="docker-bypass"
      class="card space-y-3 transition-shadow"
      :class="focusName && !targets.some((t) => t.name === focusName) ? 'ring-1 ring-amber-400/60' : ''"
    >
```

- [ ] **Step 3: 构建验证（含 vite 全量）**

```powershell
cd d:\WorkPlace\Pycharm\modelctl\web; npm run build
```
预期：无错误。

- [ ] **Step 4: Commit**

```powershell
cd d:\WorkPlace\Pycharm\modelctl; git add web/src/views/EnvsView.vue
git commit -m "feat(web): 环境页支持 ?focus= 定位引擎行/旁路区块并短时高亮"
```

---

### Task 7: 全量验证与知识沉淀

**Files:**
- Modify: `docs/known-pitfalls/backend/`（按 CLAUDE.md 渐进式披露规范，归入既有主题文件或新建）

**Interfaces:**
- Consumes: 全部前序 Task
- Produces: 回归证据 + pitfalls 沉淀

- [ ] **Step 1: 后端全量回归**

```powershell
$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; python -m pytest tests -q -p no:cacheprovider
```
预期：0 新增失败（总数 = 主干通过数 + 新增用例数；208 真机相关跳过项与主干一致）。

- [ ] **Step 2: ruff（触碰文件与主干基线比不新增）**

```powershell
ruff check src/modelctl/core/cluster/reconcile.py src/modelctl/core/webui/admin_tasks.py src/modelctl/core/webui/admin_models.py tests/test_admin_models_classify.py tests/test_cluster_reconcile.py tests/test_admin_tasks.py
```

- [ ] **Step 3: 手工冒烟（webui 实机；执行人无法覆盖时逐项移交用户）**

1. 对 venv 不存在的 profile 点"启动" → 失败 toast 文案不变；任务抽屉失败卡片：Windows 显示 [去环境页]（点 → `/envs?focus=<engine>` 定位高亮）；Linux 显示 [创建环境]（点 → 抽屉出现 env_setup 任务，成功后回详情页可启动）
2. 制造端口冲突失败 → 失败卡片**无**任何修复按钮
3. 刷新页面 → 失败任务与修复按钮仍在（toRecord 路径）
4. Windows 上配 `docker_image` 且 docker 缺失的 profile → 启动失败 → 点 [去环境页] → 定位到 Docker 旁路区块
5. `docker_missing` 分类经 `classify_start_failure` 返回 `docker_missing`（单测已锁），Windows 环境页 Setup 按钮禁用提示正常

- [ ] **Step 4: 知识沉淀**

`docs/known-pitfalls/backend/` 对应主题文件追加条目（根因：失败提示指向 CLI 而 WebUI 已有同能力；解法：后端结构化分类码贯通，勿在前端正则解析错误文案），`docs/known-pitfalls/README.md` 索引加一行。

- [ ] **Step 5: Commit**

```powershell
git add docs/known-pitfalls
git commit -m "docs(pitfalls): 失败提示须指向可操作入口——错误分类码贯通任务事件"
```
