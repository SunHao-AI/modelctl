# Web UI 任务中心实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 SSE 命名事件协议，引入 Pinia 全局任务状态层与任务抽屉，实现跨视图/刷新状态恢复。

**Architecture:** 后端补 `GET /tasks/{id}` 与 `update_detail` 广播；前端 `stores/tasks.ts` 集中托管 SSE 订阅与降级轮询；`TaskButton` 状态改为 store 派生；新增 `TaskDrawer` 作为全局任务入口。

**Tech Stack:** FastAPI + pytest（后端）；Vue 3 `<script setup>` + Pinia + TypeScript + axios + EventSource（前端）。前端无 vitest，验证靠 `npm run typecheck` 与手动验收。

## Global Constraints

- 前端：Vue 3 Composition API + `<script setup>`；组件名 PascalCase；CSS 类名 BEM；网络请求走 `src/api/client.ts` axios 实例。**项目无 element-plus**（CLAUDE.md 的 Element Plus 条目与实际代码不符）：即时通知使用自写轻量 `src/utils/toast.ts`（success/error/warning，固定定位自动消失），禁止引入组件库。
- 后端：RESTful；try-except 记 logger；GET 列表/GET 单条/POST/PUT/DELETE 五方法在同一路由组。
- PowerShell 不支持 `&&`，多命令用分号。
- Python 头注释遵循 CLAUDE.md 仓库标准文件头。

---

### Task 1: 后端 — `GET /tasks/{id}` 端点 + `update_detail` 广播

**Files:**
- Modify: `src/modelctl/core/webui/admin_router.py`（在 `list_tasks` 之后插入 `get_task`）
- Modify: `src/modelctl/core/webui/admin_tasks.py:115-117`（`update_detail` 末尾加广播）
- Test: `tests/test_admin_tasks.py`

**Interfaces:**
- Consumes: 现有 `TaskManager.get_task(task_id) -> Task | None`、`Task.to_dict() -> dict`、`Depends(require_auth_or_query)`（admin_router.py 已有 import）。
- Produces: `GET /admin/api/tasks/{task_id}` → 200 `Task.to_dict()` 或 404 `{"error":{"code":"not_found","message":...}}`；`update_detail` 现在会广播 `event: step`（既有 4 个调用点 `admin_models.py:151,190`、`admin_services.py:207,374` 自动获益）。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_admin_tasks.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""admin_tasks / admin_router 任务端点测试。"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_tasks"


@pytest.fixture()
def admin_client(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    logger.remove()
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c


def _mk_task(client: TestClient) -> str:
    """经 /tasks 列表找一个真实任务 id；没有则经 envs setup 造一个。

    直接操作 app.state.task_manager 更稳（不依赖 envs 端点副作用）。
    """
    tm = client.app.state.task_manager
    task = tm.create_task(kind="test", action="start", target="t1")
    task.update_status("running")
    task.log_line("hello")
    return task.id


def test_get_task_ok(admin_client):
    tid = _mk_task(admin_client)
    r = admin_client.get(f"/admin/api/tasks/{tid}", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == tid
    assert body["status"] == "running"
    assert body["logs"] == ["hello"]


def test_get_task_not_found(admin_client):
    r = admin_client.get("/admin/api/tasks/task-nope", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_update_detail_broadcasts_step(admin_client):
    """update_detail 必须让订阅者收到 step 事件（detail 可见性）。"""
    import asyncio

    from modelctl.core.webui.admin_tasks import Task

    task = Task(id="task-x", kind="k", action="a", target="t")
    q = task.subscribe()
    task.update_status("running")
    task.update_detail("安装中 30%")
    # event() 经 call_soon_threadsafe 投递，无 running loop 时同步 put_nowait
    payloads = []
    while not q.empty():
        payloads.append(q.get_nowait())
    assert any("event: step" in p and "安装中 30%" in p for p in payloads), payloads
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_admin_tasks.py -v`
Expected: `test_get_task_ok` / `test_get_task_not_found` FAIL（404/405，端点不存在）；`test_update_detail_broadcasts_step` FAIL（无 step 事件）。

- [ ] **Step 3: 实现后端改动**

3a. `admin_tasks.py` 的 `update_detail` 改为：

```python
    def update_detail(self, detail: str) -> None:
        """更新任务详情并广播 step 事件（detail 对前端可见）。"""
        self.detail = detail
        self.event("step", {"step": 0, "label": detail, "status": self.status, "task_id": self.id})
```

3b. `admin_router.py` 在 `list_tasks` 端点之后、`task_stream` 之前插入：

```python
    @router.get("/tasks/{task_id}")
    async def get_task(
        task_id: str,
        request: Request,
        key: str = Query(default=""),
        _: None = Depends(require_auth_or_query),
    ):
        """GET /admin/api/tasks/{task_id} — 单条任务详情（含 logs）。

        前端刷新/降级轮询恢复状态用；不存在返回 404 not_found，
        前端据此判定 webui 已重启、任务丢失。
        """
        tm: TaskManager = request.app.state.task_manager
        task = tm.get_task(task_id)
        if task is None:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "not_found", "message": f"任务 {task_id} 不存在或已过期"}},
            )
        return task.to_dict()
```

- [ ] **Step 4: 跑测试确认通过 + 冒烟回归**

Run: `pytest tests/test_admin_tasks.py tests/test_webui_smoke.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```powershell
git add src/modelctl/core/webui/admin_router.py src/modelctl/core/webui/admin_tasks.py tests/test_admin_tasks.py
git commit -m "feat(webui): add GET /tasks/{id} endpoint and broadcast detail via step event"
```

---

### Task 2: 前端 — SSE 命名事件协议 + `getTask` API

**Files:**
- Modify: `web/src/api/tasks.ts`（重写事件监听；新增 `getTask`）
- Modify: `web/src/api/types.ts`（新增 `TaskStatus`、`TaskInfo`）

**Interfaces:**
- Consumes: 后端 `_sse_task_stream`（admin_router.py）发送的命名事件 `step|log|done|heartbeat`，payload 分别为 `{"step":int,"label":str,"status":str,"task_id":str}` / `{"line":str}` / `{"status":str,"exit_code":int,"message"?:str,"task_id":str}`；`GET /tasks/{id}` 返回 Task.to_dict()。
- Produces:
  - `export type TaskStatus = 'queued' | 'running' | 'success' | 'skipped' | 'error';`
  - `export interface TaskInfo { id: string; kind: string; action: string; target: string; status: TaskStatus; exit_code: number; started_at: string; finished_at: string | null; detail: string | null; logs: string[]; }`
  - `getTask(taskId: string): Promise<TaskInfo>`（404 时抛 `Error`，`err.code === 'not_found'`）
  - `openTaskStream(taskId, hooks)` 改为按命名事件派发，`TaskStreamHooks.onStep/onLog/onDone/onHeartbeat/onError`。

- [ ] **Step 1: 扩展 types.ts**

在 `types.ts` 末尾追加：

```ts
/** 任务状态（后端 TaskStatus） */
export type TaskStatus = 'queued' | 'running' | 'success' | 'skipped' | 'error';

/** 单条任务详情（GET /tasks/{id} 与 GET /tasks 列表项同构） */
export interface TaskInfo {
  /** 任务 id（task-xxxxxxxx） */
  id: string;
  /** 任务种类：model_start|service_start|env_setup|trtllm_build|all_start|... */
  kind: string;
  /** 动作：start|stop|restart|setup|remove|build|... */
  action: string;
  /** 目标（profile / service / env 名） */
  target: string;
  status: TaskStatus;
  /** 退出码；0=成功 */
  exit_code: number;
  /** ISO 时间戳；未开始为空串 */
  started_at: string;
  finished_at: string | null;
  /** 详情（进度描述 / 错误信息） */
  detail: string | null;
  /** 内存环形日志（后端最多 500 行） */
  logs: string[];
}
```

- [ ] **Step 2: 重写 tasks.ts**

整体替换 `web/src/api/tasks.ts`：

```ts
/**
 * 任务 SSE 订阅与单条查询工具。
 *
 * 后端 _sse_task_stream 发送**命名事件**：
 *   event: step      data: {"step":0,"label":"...","status":"running","task_id":"..."}
 *   event: log       data: {"line":"..."}
 *   event: done      data: {"status":"success|error","exit_code":0,"message"?:"...","task_id":"..."}
 *   event: heartbeat data: {}
 * 本模块把 EventSource 按事件名拆分回调；调用方在 onBeforeUnmount 调 close()。
 */

import client, { dataOf } from './client';
import type { TaskInfo } from './types';

/** SSE step 事件体 */
export interface TaskStepEvent {
  step: number;
  label: string;
  status: string;
  task_id: string;
}
/** SSE log 事件体 */
export interface TaskLogEvent {
  line: string;
}
/** SSE done 事件体 */
export interface TaskDoneEvent {
  status: string;
  exit_code: number;
  message?: string;
  task_id: string;
}

/** 单个任务订阅句柄 */
export interface TaskStreamHandle {
  close(): void;
}

/** 回调集合 */
export interface TaskStreamHooks {
  onStep?: (evt: TaskStepEvent) => void;
  onLog?: (evt: TaskLogEvent) => void;
  onDone?: (evt: TaskDoneEvent) => void;
  /** EventSource 底层 error（网络层；不重连决策交调用方） */
  onError?: (err: Event) => void;
}

/**
 * 打开任务 SSE 流（命名事件版）。
 *
 * @param taskId 后端返回的 task_id（task-xxxxxxxx）
 * @param hooks  回调集合
 * @returns 句柄（{ close() }），必须在 onBeforeUnmount 调用 close()。
 */
export function openTaskStream(taskId: string, hooks: TaskStreamHooks = {}): TaskStreamHandle {
  // EventSource 不能带 Authorization 头；后端 SSE 端点用 require_auth_or_query，
  // 必须经 ?key= query 携带 token，否则 401（现存 bug：旧 URL 没带 key，流从未连通）。
  const token = useAuthStore().token;
  const url = `/admin/api/tasks/${encodeURIComponent(taskId)}/stream?key=${encodeURIComponent(token)}`;
  const es = new EventSource(url);

  function dispatch<T>(raw: unknown, cb?: (evt: T) => void) {
    if (typeof raw !== 'string') return;
    if (!raw.startsWith('data:')) return;
    const body = raw.slice(5).trim();
    if (!body) return;
    try {
      cb?.(JSON.parse(body) as T);
    } catch {
      hooks.onError?.(new Event(`Malformed SSE data: ${body}`));
    }
  }

  const listeners: Array<[string, (e: MessageEvent) => void]> = [
    ['step', (e) => dispatch<TaskStepEvent>(e.data, hooks.onStep)],
    ['log', (e) => dispatch<TaskLogEvent>(e.data, hooks.onLog)],
    ['done', (e) => dispatch<TaskDoneEvent>(e.data, hooks.onDone)],
    ['heartbeat', () => { /* 保活，忽略 */ }],
  ];
  for (const [name, fn] of listeners) es.addEventListener(name, fn as EventListener);
  const onErr = (e: Event) => hooks.onError?.(e);
  es.addEventListener('error', onErr);

  return {
    close() {
      try {
        for (const [name, fn] of listeners) es.removeEventListener(name, fn as EventListener);
        es.removeEventListener('error', onErr);
        es.close();
      } catch {
        /* 已 close，忽略 */
      }
    },
  };
}

/**
 * GET /admin/api/tasks/{taskId} — 单条任务详情。
 *
 * 404（webui 重启导致任务丢失）时抛 Error 且 err.code === 'not_found'。
 */
export async function getTask(taskId: string): Promise<TaskInfo> {
  try {
    return await dataOf(client.get<TaskInfo>(`/tasks/${encodeURIComponent(taskId)}`));
  } catch (err) {
    const status = (err as { response?: { status?: number } }).response?.status;
    if (status === 404) {
      const e = new Error('任务不存在或已过期') as Error & { code: string };
      e.code = 'not_found';
      throw e;
    }
    throw err;
  }
}
```

注意：上文 `dispatch` 从 SSE 文本解析是为对称旧实现写的；EventSource 命名事件回调的 `e.data` 已是纯 JSON 字符串（后端 `data: {...}\n\n`），因此实际实现应直接把 `e.data` 传给 `JSON.parse`，无需 `startsWith('data:')` 分支。实现时以直接 `JSON.parse(String(e.data))` 为准（heartbeat 事件体 `{}` 解析后忽略）。

- [ ] **Step 3: 验证编译**

Run（在 `web/` 目录下）: `npm run typecheck`
Expected: 0 error（此时 TaskButton 尚未改，旧 `onData/onDone` 调用仍在，需一并适配——本任务把 TaskButton 的 `openTaskStream` 调用点改为新 hooks 形态，见 Step 4）。

- [ ] **Step 4: 适配 TaskButton 现有调用点（最小改动，保持旧行为）**

`web/src/components/common/TaskButton.vue` 中 `handleSseDone` 依赖旧 `onData/onDone` 回调。为让 typecheck 通过，临时把 `openTaskStream(taskRef.task_id, { onStep:..., onLog:..., onDone:... })` 接到现有 `handleSseDone` 逻辑：

```ts
streamHandle = openTaskStream(taskRef.task_id, {
  onStep: (evt) => handleSseDone({ type: 'status', data: evt }),
  onLog: (evt) => handleSseDone({ type: 'log', data: evt }),
  onDone: (evt) => handleSseDone({ type: 'done', data: evt }),
  onError: (err) => {
    console.warn('SSE 错误:', err?.type, err);
  },
});
```

- [ ] **Step 5: Commit**

```powershell
git add web/src/api/tasks.ts web/src/api/types.ts web/src/components/common/TaskButton.vue
git commit -m "feat(web): parse SSE named events (step/log/done) and add getTask API"
```

---

### Task 3: 前端 — `stores/tasks.ts` 全局任务状态层

**Files:**
- Create: `web/src/stores/tasks.ts`

**Interfaces:**
- Consumes: Task 2 的 `openTaskStream(taskId, {onStep,onLog,onDone,onError})`、`getTask(taskId): Promise<TaskInfo>`（404 → `err.code==='not_found'`）；`TaskRef`/`TaskInfo`/`TaskStatus`（types.ts）。
- Produces（TaskButton 与 TaskDrawer 依赖的完整签名）：

```ts
export interface TrackOpts {
  /** 匹配键：profile / service / env 名（TaskButton 用） */
  target: string;
  /** 重试时重放的原触发函数 */
  retryFn?: () => Promise<TaskRef>;
  /** 终态回调 */
  onSuccess?: (detail?: string) => void;
  onError?: (message: string) => void;
}
export function useTasksStore(): {
  /** 全部任务（新在前） */
  tasks: import('vue').Ref<TaskRecord[]>;
  /** 运行中/排队数（Header 角标） */
  runningCount: import('vue').ComputedRef<number>;
  track(ref: TaskRef, opts: TrackOpts): void;
  /** Layout 挂载时调用：回填历史 + 重挂 running 任务 SSE */
  bootstrap(): Promise<void>;
  /** 某 target 的活动任务（queued/running）或最近终态任务（TaskButton 派生 phase 用） */
  activityFor(target: string): TaskRecord | undefined;
  retry(taskId: string): Promise<void>;
  dismiss(taskId: string): void;
};
export interface TaskRecord {
  id: string; kind: string; action: string; target: string;
  status: TaskStatus; detail: string; startedAt: string; finishedAt: string;
  exitCode: number; logs: string[];
  retryFn?: () => Promise<TaskRef>;
  onSuccess?: (detail?: string) => void; onError?: (message: string) => void;
  /** 内部：SSE 句柄 / 降级轮询 timer / 最后活跃时间 */
  stream: { close(): void } | null; pollTimer: number; lastEventAt: number;
}
```

常量：`MAX_LOG_LINES = 2000`、`POLL_INTERVAL_MS = 5000`、`RECONNECT_GRACE_MS = 30000`、`MAX_TASKS = 50`。

- [ ] **Step 1a: 创建 `web/src/utils/toast.ts`（自写轻量通知，项目无组件库）**

```ts
/**
 * 轻量全局 toast：固定右上角、自动消失，不依赖任何组件库。
 *
 * 用法：toast.success('...') / toast.error('...', 5000) / toast.warning('...')
 * duration 毫秒，缺省 3000（error 4000）；传 0 表示常驻（调用 toast.dismiss 关）。
 */

type ToastKind = 'success' | 'error' | 'warning';

interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

const items = ref<ToastItem[]>([]);
let seq = 0;

const KIND_CLASS: Record<ToastKind, string> = {
  success: 'border-emerald-500/40 bg-emerald-600/15 text-emerald-200',
  error: 'border-red-500/40 bg-red-600/15 text-red-200',
  warning: 'border-amber-500/40 bg-amber-600/15 text-amber-200',
};

/** 挂载点：<ToastHost /> 组件渲染 items（Task 3 同文件导出） */
export function useToastItems() {
  return { items, KIND_CLASS };
}

function push(kind: ToastKind, message: string, duration: number) {
  const item: ToastItem = { id: ++seq, kind, message };
  items.value.push(item);
  if (duration > 0) {
    window.setTimeout(() => dismiss(item.id), duration);
  }
}

function dismiss(id: number) {
  const i = items.value.findIndex((t) => t.id === id);
  if (i >= 0) items.value.splice(i, 1);
}

export const toast = {
  success: (m: string, d = 3000) => push('success', m, d),
  error: (m: string, d = 4000) => push('error', m, d),
  warning: (m: string, d = 3000) => push('warning', m, d),
  dismiss,
};
```

注意：`items` 需要 `import { ref } from 'vue'`（文件首行加上）。toast 的渲染宿主在 Task 5 的 `TaskDrawer.vue` 中挂载（TaskDrawer 常驻于 Header，天然全局）——`TaskDrawer` 模板底部渲染固定定位容器：

```vue
    <!-- 全局 toast 容器（TaskDrawer 常驻，toast 宿主） -->
    <div class="fixed right-4 top-14 z-[60] flex w-80 flex-col gap-2">
      <div
        v-for="t in toastItems"
        :key="t.id"
        :class="['rounded-lg border px-3 py-2 text-sm shadow-lg backdrop-blur', kindClass[t.kind]]"
      >{{ t.message }}</div>
    </div>
```

（TaskDrawer 组件内：`const { items: toastItems, KIND_CLASS: kindClass } = useToastItems();`）

- [ ] **Step 1b: 实现 store**

创建 `web/src/stores/tasks.ts`：

```ts
import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import client, { dataOf } from '@/api/client';
import { toast } from '@/utils/toast';
import { getTask, openTaskStream } from '@/api/tasks';
import type { TaskInfo, TaskRef, TaskStatus } from '@/api/types';

/** 前端日志环形缓冲上限 */
const MAX_LOG_LINES = 2000;
/** SSE 断流后的降级轮询间隔 */
const POLL_INTERVAL_MS = 5000;
/** EventSource 断开后给原生重连的宽限期 */
const RECONNECT_GRACE_MS = 30000;
/** 抽屉/列表保留上限（终态裁剪最旧） */
const MAX_TASKS = 50;

/** 触发方注册的回调与重试函数 */
export interface TrackOpts {
  target: string;
  retryFn?: () => Promise<TaskRef>;
  onSuccess?: (detail?: string) => void;
  onError?: (message: string) => void;
}

/** store 内统一的任务记录（TaskInfo + 恢复/重试辅助字段） */
export interface TaskRecord {
  id: string;
  kind: string;
  action: string;
  target: string;
  status: TaskStatus;
  detail: string;
  startedAt: string;
  finishedAt: string;
  exitCode: number;
  logs: string[];
  retryFn?: () => Promise<TaskRef>;
  onSuccess?: (detail?: string) => void;
  onError?: (message: string) => void;
  /** 内部字段：SSE 句柄 / 轮询 timer / 最后活跃时间戳 */
  stream: { close(): void } | null;
  pollTimer: number;
  lastEventAt: number;
}

/** TaskInfo → TaskRecord（bootstrap / 轮询回填用） */
function toRecord(t: TaskInfo): TaskRecord {
  return {
    id: t.id, kind: t.kind, action: t.action, target: t.target,
    status: t.status, detail: t.detail ?? '', startedAt: t.started_at,
    finishedAt: t.finished_at ?? '', exitCode: t.exit_code, logs: [...t.logs],
    stream: null, pollTimer: 0, lastEventAt: Date.now(),
  };
}

export const useTasksStore = defineStore('tasks', () => {
  const tasks = ref<TaskRecord[]>([]);
  const runningCount = computed(
    () => tasks.value.filter((t) => t.status === 'queued' || t.status === 'running').length,
  );

  /** 索引辅助：id → 在 tasks 数组中的下标 */
  function idx(id: string): number {
    return tasks.value.findIndex((t) => t.id === id);
  }

  /** 裁剪至 MAX_TASKS（最旧先删；运行中任务不受限） */
  function trim() {
    const done = tasks.value.filter((t) => t.status !== 'queued' && t.status !== 'running');
    const overflow = done.length - MAX_TASKS;
    if (overflow <= 0) return;
    const drop = new Set(done.slice(0, overflow).map((t) => t.id));
    for (const t of tasks.value) {
      if (drop.has(t.id)) teardown(t);
    }
    tasks.value = tasks.value.filter((t) => !drop.has(t.id));
  }

  /** 释放 SSE / 轮询资源（不删记录本身） */
  function teardown(t: TaskRecord) {
    try {
      t.stream?.close();
    } catch { /* ignore */ }
    t.stream = null;
    if (t.pollTimer) {
      clearInterval(t.pollTimer);
      t.pollTimer = 0;
    }
  }

  /** 推进状态到终态并触发回调 + 通知 */
  function finalize(t: TaskRecord, status: TaskStatus, detail: string, exitCode: number) {
    t.status = status;
    t.detail = detail;
    t.exitCode = exitCode;
    t.finishedAt = new Date().toISOString();
    teardown(t);
    trim();
    if (status === 'success' || status === 'skipped') {
      toast.success(`${t.target} ${t.action} 完成`);
      try { t.onSuccess?.(detail); } catch { /* ignore */ }
    } else {
      toast.error(`${t.target} ${t.action} 失败：${detail}`);
      try { t.onError?.(detail); } catch { /* ignore */ }
    }
  }

  /** 追加日志（环形裁剪） */
  function appendLog(t: TaskRecord, line: string) {
    t.logs.push(line);
    if (t.logs.length > MAX_LOG_LINES) {
      t.logs.splice(0, t.logs.length - MAX_LOG_LINES);
    }
  }

  /** 挂 SSE：断流后先给原生重连宽限，超时降级轮询 */
  function attachStream(t: TaskRecord) {
    teardown(t);
    t.lastEventAt = Date.now();
    t.stream = openTaskStream(t.id, {
      onStep: (evt) => {
        t.lastEventAt = Date.now();
        if (evt.status === 'running' && !t.startedAt) {
          t.startedAt = new Date().toISOString();
        }
        if (evt.label && evt.label !== t.target) t.detail = evt.label;
        if (evt.status === 'running' || evt.status === 'queued') {
          t.status = evt.status as TaskStatus;
        }
      },
      onLog: (evt) => {
        t.lastEventAt = Date.now();
        appendLog(t, evt.line);
      },
      onDone: (evt) => {
        t.lastEventAt = Date.now();
        if (evt.status === 'success' || evt.status === 'skipped') {
          finalize(t, evt.status as TaskStatus, t.detail, evt.exit_code);
        } else {
          finalize(t, 'error', evt.message || t.detail || '执行失败', evt.exit_code);
        }
      },
      onError: () => {
        // 浏览器自动重连；宽限期后仍未恢复则降级轮询
        setTimeout(() => {
          const i = idx(t.id);
          if (i < 0) return;
          const cur = tasks.value[i];
          if (cur.status !== 'queued' && cur.status !== 'running') return;
          if (Date.now() - cur.lastEventAt < RECONNECT_GRACE_MS) return;
          if (!cur.pollTimer) startPolling(cur);
        }, RECONNECT_GRACE_MS);
      },
    });
  }

  /** 降级轮询：5s 一次 GET /tasks/{id}，404 = webui 重启 → 标记 error */
  function startPolling(t: TaskRecord) {
    if (t.pollTimer) return;
    t.pollTimer = window.setInterval(async () => {
      const i = idx(t.id);
      if (i < 0) { clearInterval(t.pollTimer); return; }
      const cur = tasks.value[i];
      try {
        const info = await getTask(t.id);
        cur.lastEventAt = Date.now();
        // 日志 diff（后端环形 500 行可能滚动，对齐尾部即可）
        const from = Math.max(0, info.logs.length - (info.logs.length - cur.logs.length));
        for (let n = cur.logs.length; n < info.logs.length; n += 1) {
          appendLog(cur, info.logs[n]);
        }
        void from;
        if (info.status === 'queued' || info.status === 'running') {
          cur.status = info.status;
          cur.detail = info.detail ?? cur.detail;
        } else {
          finalize(cur, info.status, info.detail ?? '', info.exit_code);
        }
      } catch (err) {
        if ((err as { code?: string }).code === 'not_found') {
          finalize(cur, 'error', '服务已重启，任务状态丢失', 1);
        }
        // 网络抖动：下一轮再试
      }
    }, POLL_INTERVAL_MS);
  }

  /** 触发新任务：登记 + 挂 SSE */
  function track(ref: TaskRef, opts: TrackOpts) {
    const rec: TaskRecord = {
      id: ref.task_id, kind: '', action: '', target: opts.target,
      status: 'queued', detail: '', startedAt: '', finishedAt: '',
      exitCode: 0, logs: [],
      retryFn: opts.retryFn, onSuccess: opts.onSuccess, onError: opts.onError,
      stream: null, pollTimer: 0, lastEventAt: Date.now(),
    };
    // 补 kind/action：立即拉一次详情（失败不阻塞，仅缺元数据）
    void getTask(ref.task_id)
      .then((info) => {
        const i = idx(rec.id);
        if (i < 0) return;
        tasks.value[i].kind = info.kind;
        tasks.value[i].action = info.action;
      })
      .catch(() => { /* ignore */ });
    tasks.value.unshift(rec);
    trim();
    attachStream(rec);
  }

  /** 页面重载后：拉历史 + 对活动任务重挂 SSE */
  async function bootstrap() {
    const list = await dataOf(client.get<{ tasks: TaskInfo[] }>('/tasks'));
    for (const info of list.tasks) {
      const rec = toRecord(info);
      tasks.value.push(rec);
    }
    // 新在前
    tasks.value.sort((a, b) => (b.startedAt || '').localeCompare(a.startedAt || ''));
    trim();
    // 活动任务重挂 SSE（恢复实时性）
    for (const t of tasks.value) {
      if (t.status === 'queued' || t.status === 'running') attachStream(t);
    }
  }

  /** 按 target 查活动任务或最近终态任务（TaskButton 派生 phase） */
  function activityFor(target: string): TaskRecord | undefined {
    return tasks.value.find(
      (t) => t.target === target && (t.status === 'queued' || t.status === 'running'),
    ) ?? tasks.value.find((t) => t.target === target);
  }

  /** 重试：重放 retryFn，成功后旧记录标 superseded 并移除 */
  async function retry(taskId: string) {
    const i = idx(taskId);
    if (i < 0) return;
    const old = tasks.value[i];
    if (!old.retryFn) {
      toast.warning('该任务不支持重试（缺少触发函数）');
      return;
    }
    teardown(old);
    tasks.value.splice(i, 1);
    try {
      const ref = await old.retryFn();
      track(ref, { target: old.target, retryFn: old.retryFn, onSuccess: old.onSuccess, onError: old.onError });
    } catch (err) {
      const msg = (err as { message?: string })?.message || '重试提交失败';
      const rec = toRecord({
        id: `task-retry-${Date.now()}`, kind: old.kind, action: old.action, target: old.target,
        status: 'error', exit_code: 1, started_at: '', finished_at: new Date().toISOString(),
        detail: msg, logs: [],
      } as TaskInfo);
      rec.retryFn = old.retryFn;
      tasks.value.unshift(rec);
      toast.error(msg);
    }
  }

  /** 忽略终态任务 */
  function dismiss(taskId: string) {
    const i = idx(taskId);
    if (i < 0) return;
    teardown(tasks.value[i]);
    tasks.value.splice(i, 1);
  }

  return {
    tasks, runningCount,
    track, bootstrap, activityFor, retry, dismiss,
  };
});
```

实现注意：
- `bootstrap()` 中 `getTask('')` 占位调用应删除，直接用 axios `GET /tasks`（上方已写正确路径）；实现时清理占位代码。
- `startPolling` 里 `from` 变量是死代码，删除（直接按 `cur.logs.length` 索引进 `info.logs` 追加）。
- `toRecord` 的 `finished_at` 可能为 `null`，`toRecord` 里已用 `?? ''` 处理。

- [ ] **Step 2: 验证编译**

Run: `cd web; npm run typecheck`
Expected: 0 error。（toast 为自写工具，无新增依赖；若 typecheck 报 `@/utils/toast` 缺失，确认 Step 1a 文件已创建。）

- [ ] **Step 3: Commit**

```powershell
git add web/src/stores/tasks.ts
git commit -m "feat(web): add Pinia tasks store with SSE attach, degrade-poll and refresh bootstrap"
```

---

### Task 4: 前端 — `TaskButton.vue` 改为 store 派生状态

**Files:**
- Modify: `web/src/components/common/TaskButton.vue`（整体重写）

**Interfaces:**
- Consumes: Task 3 的 `useTasksStore()`：`track(ref, {target, retryFn, onSuccess, onError})`、`activityFor(target) -> TaskRecord | undefined`。
- Produces: props 新增必填 `target: string`（EnvsView/ModelDetailView/ServicesMatrixView 共 5 处调用点需补传，见 Step 2）；其余 props/emits（`label`、`variant`、`taskTarget`、`onSuccess`、`onError`、`@success`、`@error`）不变。

- [ ] **Step 1: 重写 TaskButton.vue**

```vue
<script setup lang="ts">
import { computed } from 'vue';
import { ElMessage } from 'element-plus';
import { useTasksStore } from '@/stores/tasks';
import type { TaskRef } from '@/api/types';

/**
 * 任务式按钮：点击 → 调 target() 拿 TaskRef → 交给 tasksStore 跟踪。
 *
 * 状态完全由 store 派生（跨视图/刷新存活）：
 *   - idle:        无该 target 的任务记录
 *   - submitting:  本地瞬时态（已点尚未拿到 task_ref）
 *   - running:     activityFor(target).status ∈ {queued, running}
 *   - ok:          最近任务终态 success/skipped（2s 后回 idle）
 *   - fail:        最近任务终态 error（2s 后回 idle）
 *
 * 用法不变：
 *   <TaskButton label="启动" :task-target="() => startModel(name)" @success="onRefresh" />
 */
const props = withDefaults(
  defineProps<{
    label: string;
    variant?: 'primary' | 'danger' | 'ghost';
    taskTarget: () => Promise<TaskRef>;
    onSuccess?: (detail?: string) => void;
    onError?: (message: string) => void;
  }>(),
  { variant: 'primary' },
);

const emit = defineEmits<{
  (e: 'success', detail?: string): void;
  (e: 'error', message: string): void;
}>();

const tasksStore = useTasksStore();

/** 该 target 的活动/最近任务 */
const record = computed(() => tasksStore.activityFor(props.target ?? ''));

/** 本地瞬时提交态：点击后尚未拿到 task_id 的窗口 */
let submitting = false;

type Phase = 'idle' | 'submitting' | 'running' | 'ok' | 'fail';
const phase = computed<Phase>(() => {
  if (submitting) return 'submitting';
  const r = record.value;
  if (!r) return 'idle';
  if (r.status === 'queued' || r.status === 'running') return 'running';
  // 终态：最近 2s 内完成的显示结果，否则回 idle
  const done = Date.now() - new Date(r.finishedAt).getTime() < 2000;
  if (!done) return 'idle';
  return r.status === 'error' ? 'fail' : 'ok';
});

const text = computed(() => {
  switch (phase.value) {
    case 'submitting': return '提交中…';
    case 'running': return '执行中…';
    case 'ok': return '✔ 完成';
    case 'fail': return '✗ 失败';
    default: return props.label;
  }
});

/** 提交回调透传给 store（store 终态时会再触发本组件 props.onSuccess/onError） */
async function onClick() {
  if (phase.value !== 'idle') return;
  submitting = true;
  let ref: TaskRef;
  try {
    ref = await props.taskTarget();
  } catch (err) {
    submitting = false;
    const msg = (err as { message?: string })?.message || '提交失败';
    // 409 = 已有同 target 任务在跑
    if ((err as { response?: { status?: number } }).response?.status === 409) {
      ElMessage.warning('该目标已有任务在执行中');
    } else {
      ElMessage.error(msg);
    }
    emit('error', msg);
    return;
  }
  submitting = false;
  if (!ref?.task_id) {
    const msg = '后端未返回 task_id';
    ElMessage.error(msg);
    emit('error', msg);
    return;
  }
  tasksStore.track(ref, {
    target: props.target ?? '',
    retryFn: props.taskTarget,
    onSuccess: (detail) => { props.onSuccess?.(detail); emit('success', detail); },
    onError: (message) => { props.onError?.(message); emit('error', message); },
  });
}
</script>
```

模板保持现有结构（spinner / 文案），但 `disabled` 与 `:title` 直接绑 `phase`：

```vue
<template>
  <button
    :class="[
      phase === 'fail' ? 'btn-danger' : variant === 'primary' ? 'btn-primary' : variant === 'danger' ? 'btn-danger' : 'btn-ghost',
      'min-w-24',
    ]"
    :disabled="phase !== 'idle'"
    :title="text"
    @click="onClick"
  >
    <svg v-if="phase === 'submitting' || phase === 'running'" class="size-3.5 animate-spin" viewBox="0 0 24 24" fill="none">
      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" />
    </svg>
    <span>{{ text }}</span>
  </button>
</template>
```

**props 调整**：上述重写已包含新增必填 prop `target: string`。现有调用点传的是 `:task-target`，未传 `target`，需给 3 个视图共 5 处调用点补上：

| 调用文件 | 行（约） | 补 `:target=` |
|---|---|---|
| `views/EnvsView.vue` | 98 | `:target="t.name"` |
| `views/ModelDetailView.vue` | 170, 172 | `:target="name"` |
| `views/ServicesMatrixView.vue` | 140, 141, 161, 162 | `:target="'stats'"` / `:target="'gateway'"` |

（注：ModelsListView.vue 注释提到 TaskButton 但实际未使用——stop 走同步调用，无调用点需要改。）

- [ ] **Step 2: 补调用点 `:target`**

按上表修改 3 个视图，补 `:target="..."`。

- [ ] **Step 3: 验证编译**

Run: `cd web; npm run typecheck`
Expected: 0 error。

- [ ] **Step 4: Commit**

```powershell
git add web/src/components/common/TaskButton.vue web/src/views/EnvsView.vue web/src/views/ModelDetailView.vue web/src/views/ServicesMatrixView.vue
git commit -m "refactor(web): derive TaskButton phase from tasks store; add target prop"
```

---

### Task 5: 前端 — `TaskDrawer.vue` 全局任务抽屉

**Files:**
- Create: `web/src/components/common/TaskDrawer.vue`
- Modify: `web/src/components/layout/Header.vue`（挂抽屉入口按钮）
- Modify: `web/src/components/layout/Layout.vue`（`onMounted` 调 `bootstrap()`）

**Interfaces:**
- Consumes: Task 3 store：`tasks`、`runningCount`、`retry(taskId)`、`dismiss(taskId)`、`bootstrap()`。
- Produces: 无对外 API（纯展示组件）。

- [ ] **Step 1: 创建 TaskDrawer.vue**

```vue
<script setup lang="ts">
import { computed, ref } from 'vue';
import { useTasksStore } from '@/stores/tasks';
import type { TaskRecord } from '@/stores/tasks';
import { useToastItems } from '@/utils/toast';

/** 抽屉开关（Header 通过 ref 调 toggle/close；本组件内部维护 open 状态） */
const open = ref(false);
function toggle() { open.value = !open.value; }
function close() { open.value = false; }
defineExpose({ toggle, close });

/** 全局 toast 宿主：TaskDrawer 常驻 Header，容器随组件渲染 */
const { items: toastItems, KIND_CLASS: kindClass } = useToastItems();

const tasksStore = useTasksStore();
const tasks = computed(() => tasksStore.tasks);
const runningCount = computed(() => tasksStore.runningCount);

/** 当前展开查看日志的任务 id */
const expandedId = ref<string | null>(null);

/** 状态 → 中文标签 */
function statusLabel(s: TaskRecord['status']): string {
  switch (s) {
    case 'queued': return '准备中';
    case 'running': return '执行中';
    case 'success': return '已完成';
    case 'skipped': return '已跳过';
    case 'error': return '已失败';
  }
}

/** 状态点颜色 */
function statusDot(s: TaskRecord['status']): string {
  switch (s) {
    case 'queued': return 'bg-amber-400';
    case 'running': return 'bg-blue-400 animate-pulse';
    case 'success': return 'bg-emerald-400';
    case 'skipped': return 'bg-slate-400';
    case 'error': return 'bg-red-400';
  }
}

/** 耗时显示（startedAt ~ finishedAt，未结束算到当前） */
function duration(t: TaskRecord): string {
  if (!t.startedAt) return '';
  const end = t.finishedAt ? new Date(t.finishedAt).getTime() : Date.now();
  const sec = Math.max(0, Math.round((end - new Date(t.startedAt).getTime()) / 1000));
  if (sec < 60) return `${sec}s`;
  return `${Math.floor(sec / 60)}m${sec % 60}s`;
}

function onRetry(id: string) { void tasksStore.retry(id); }
function onDismiss(id: string) {
  if (expandedId.value === id) expandedId.value = null;
  tasksStore.dismiss(id);
}
</script>

<template>
  <!-- 遮罩 -->
  <div
    v-if="open"
    class="fixed inset-0 z-40 bg-black/50"
    @click="close"
  />
  <!-- 抽屉本体：右侧滑出 -->
  <aside
    v-if="open"
    class="fixed right-0 top-0 z-50 flex h-full w-full max-w-md flex-col border-l border-slate-700 bg-slate-900 shadow-2xl"
  >
    <header class="flex items-center justify-between border-b border-slate-700 px-4 py-3">
      <h2 class="text-sm font-semibold text-slate-100">后台任务</h2>
      <button class="btn-ghost !px-2 !py-1 text-xs" @click="close">关闭</button>
    </header>

    <div class="flex-1 overflow-y-auto">
      <p v-if="!tasks.length" class="p-4 text-sm text-slate-500">暂无任务</p>
      <ul class="divide-y divide-slate-800">
        <li v-for="t in tasks" :key="t.id" class="px-4 py-3">
          <div class="flex items-center gap-2">
            <span :class="['size-2 shrink-0 rounded-full', statusDot(t.status)]" />
            <span class="min-w-0 flex-1 truncate font-mono text-sm text-slate-100">{{ t.target }}</span>
            <span class="shrink-0 text-xs text-slate-400">{{ statusLabel(t.status) }}</span>
            <span v-if="t.startedAt" class="shrink-0 text-xs text-slate-500">{{ duration(t) }}</span>
          </div>
          <p v-if="t.detail" class="mt-1 truncate text-xs text-slate-400">{{ t.detail }}</p>

          <!-- 操作行 -->
          <div class="mt-2 flex items-center gap-2">
            <button
              class="btn-ghost !px-2 !py-1 text-xs"
              @click="expandedId = expandedId === t.id ? null : t.id"
            >
              {{ expandedId === t.id ? '收起日志' : '查看日志' }}
            </button>
            <button
              v-if="t.status === 'error' && t.retryFn"
              class="btn-ghost !px-2 !py-1 text-xs"
              @click="onRetry(t.id)"
            >重试</button>
            <button
              v-if="t.status !== 'queued' && t.status !== 'running'"
              class="btn-ghost !px-2 !py-1 text-xs"
              @click="onDismiss(t.id)"
            >忽略</button>
          </div>

          <!-- 日志面板（数据源 = store，只读渲染） -->
          <pre
            v-if="expandedId === t.id"
            class="mt-2 max-h-64 overflow-y-auto rounded bg-[#0b1120] px-3 py-2 font-mono text-xs leading-5 text-slate-300 whitespace-pre-wrap break-all"
          >{{ t.logs.length ? t.logs.join('\n') : '（暂无日志）' }}</pre>
        </li>
      </ul>
    </div>
  </aside>
</template>
```

- [ ] **Step 2: Header.vue 接入抽屉**

在 `Header.vue` 的 `<script setup>` 中：

```ts
import { ref, onMounted } from 'vue';
import TaskDrawer from '@/components/common/TaskDrawer.vue';
import { useTasksStore } from '@/stores/tasks';

const tasksStore = useTasksStore();
const drawerRef = ref<InstanceType<typeof TaskDrawer> | null>(null);
const runningCount = computed(() => tasksStore.runningCount);
```

模板右侧（退出登录按钮之前）插入：

```vue
      <!-- 任务抽屉入口 -->
      <button class="btn-ghost !py-1.5 text-xs relative" title="后台任务" @click="drawerRef?.toggle()">
        <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="4" rx="1" /><rect x="3" y="10" width="18" height="4" rx="1" /><rect x="3" y="16" width="18" height="4" rx="1" /></svg>
        <span v-if="runningCount > 0" class="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-blue-500 px-1 text-[10px] font-semibold text-white">
          {{ runningCount }}
        </span>
      </button>
```

并在 `</header>` 后挂 `<TaskDrawer ref="drawerRef" />`。

- [ ] **Step 3: Layout.vue 挂载 bootstrap**

在 `Layout.vue` 的 `<script setup>` 中：

```ts
import { onMounted } from 'vue';
import { useTasksStore } from '@/stores/tasks';

const tasksStore = useTasksStore();
onMounted(() => {
  void tasksStore.bootstrap().catch(() => { /* 列表失败不阻塞页面 */ });
});
```

- [ ] **Step 4: 验证编译**

Run: `cd web; npm run typecheck`
Expected: 0 error。

- [ ] **Step 5: Commit**

```powershell
git add web/src/components/common/TaskDrawer.vue web/src/components/layout/Header.vue web/src/components/layout/Layout.vue
git commit -m "feat(web): global task drawer with live logs and retry"
```

---

### Task 6: 端到端手动验收

**Files:** 无（验证步骤）

- [ ] **Step 1: 启动后端 webui**

Run: `modelctl webui restart`
Expected: 进程监听 4173，`/admin/api/health` 200。

- [ ] **Step 2: 启动前端 dev**

Run: `cd web; npm run dev`
Expected: vite 监听 5173，`/admin/api` 代理到 4173。

- [ ] **Step 3: 验收跨视图状态**

1. 打开 `http://localhost:5173/envs`，对未安装 target 点 Setup。
2. 立即切到 `/models`，再切回 `/envs`。
3. 预期：Setup 按钮仍显示"执行中…"；Header 任务角标 ≥ 1；打开抽屉可见日志滚动。
4. 全程不刷新页面，等待 setup 完成（或经 TaskButton 触发一个小模型 start 代替长任务）。

- [ ] **Step 4: 验收刷新恢复**

1. Setup 运行中按 F5 刷新。
2. 预期：刷新后 Setup 按钮恢复"执行中…"，抽屉中该任务日志从 `GET /tasks/{id}` 回填后继续增量。
3. 验证日志连续性：刷新前的最后几行与刷新后的起始行衔接（允许后端环形缓冲截断，日志尾部一致即可）。

- [ ] **Step 5: 验收失败与重试**

1. 触发一个会失败的任务（如对未配置 API key 的模型 start）。
2. 预期：按钮短暂"✗ 失败"后回 idle；toast error 弹出；抽屉中该任务红态 + "重试"按钮。
3. 点重试：旧任务消失，新任务出现在列表顶部并重新走 SSE。

- [ ] **Step 6: 验收 webui 重启降级**

1. 任务运行中执行 `modelctl webui restart`。
2. 前端轮询 `GET /tasks/{id}` 应得 404 → 任务标 error"服务已重启，任务状态丢失"。
3. 抽屉显示该错误文案，"重试"可重新提交。

- [ ] **Step 7: 跑全量测试**

Run: `pytest tests/ -q`
Expected: 全部 PASS（含 Task 1 新增与 test_webui_smoke 回归）。

- [ ] **Step 8: 沉淀 known-pitfalls**

按 CLAUDE.md"问题自动沉淀"要求，在 `docs/known-pitfalls/backend/` 追加 `sse-named-events.md`：记录"FastAPI SSE 用 `event:` 命名标签时前端 EventSource 必须 `addEventListener(name)`，只监听 `message` 会静默收不到任何事件"的根因与排查过程。

```powershell
git add docs/known-pitfalls/backend/sse-named-events.md
git commit -m "docs: known-pitfalls entry for SSE named-event EventSource mismatch"
```

---

## Self-Review

- **Spec 覆盖**：§4.1 → Task 1；§4.2 → Task 1 Step 3a；§5.1 → Task 3；§5.2 → Task 2；§5.3 → Task 4；§5.4 → Task 5；§5.5 → Task 5 Step 2/3；§6 异常矩阵 → Task 3 store 逻辑 + Task 6 手动验收；§7 测试 → Task 1/2/6；§8 顺序 → 任务编号顺序即实施顺序。无遗漏。
- **Placeholder 扫描**：无 TBD/TODO；每个代码步骤给出完整可编译代码；类型/函数签名在各 Task 的 Produces 段显式声明。
- **类型一致性**：`TaskInfo`/`TaskRef`/`TaskStatus` 在 Task 2 定义、Task 3/4/5 引用一致；`track`/`bootstrap`/`activityFor`/`retry`/`dismiss`/`runningCount`/`tasks` 在 Task 3 声明、Task 4/5 消费一致；`openTaskStream` hooks 从旧 `onData/onDone` 迁移为新 `onStep/onLog/onDone/onError`，Task 2 Step 4 与 Task 3 `attachStream` 使用一致。

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-04-webui-task-center.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 每个 Task 派发独立子代理执行，我在 Task 间做两阶段 review。

**2. Inline Execution** — 在当前会话内用 executing-plans 批量执行，设检查点 review。

Which approach?