# Web UI 任务中心设计（方案 A：Pinia 全局状态 + 任务抽屉）

- 日期：2026-09-04
- 状态：待审阅
- 关联：`src/modelctl/core/webui/admin_tasks.py`、`web/src/components/common/TaskButton.vue`、`web/src/api/tasks.ts`

## 1. 背景与问题

Web 控制台已具备异步任务骨架（后端 `TaskManager` + `Task`，前端 `TaskButton` + SSE），但存在四类缺陷：

1. **协议不通**：后端 `_sse_task_stream` 每条事件带 `event: step|log|done|heartbeat` 命名标签，而前端 `tasks.ts` 只监听无名 `message` 事件——`EventSource` 按命名事件派发，前端实际**收不到任何任务事件**，TaskButton 的"完成/失败"全靠 5 分钟超时兜底。
2. **状态易失**：`TaskButton` 把 SSE 句柄与 `phase` 全部放在组件本地，组件卸载（切换视图/刷新）即断流丢态，回到页面按钮复位为 idle。用户场景：安装 vllm 环境（28 分钟级）中切换界面，状态错误地回退。
3. **无全局入口**：运行中的任务只能在原触发按钮上看到，跨视图无法跟进、无法查看日志、无法重试。
4. **detail 不同步**：后端 `Task.update_detail()` 只改内存字段，不发任何事件，前端永远看不到 detail 更新。

后端任务本身已在后台线程执行、SSE 推送亦不阻塞主线程，**无需引入 Web Worker**；缺的是前端全局状态层与协议修复。

## 2. 目标与非目标

**目标**：

- 修复 SSE 命名事件协议，前端能真正收到 step/log/done。
- 任务状态由 Pinia store 全局持有：切换视图、刷新页面后从后端恢复真实状态。
- Header 提供全局任务抽屉：运行中角标、任务列表、实时日志、失败重试。
- TaskButton 状态改为从 store 派生，天然获得刷新/切页恢复能力。
- SSE 断流降级轮询；webui 重启（任务丢失）时给出明确错误提示。

**非目标**：

- 任务持久化落盘（webui 重启后历史清空属可接受，任务客观上已随进程死亡）。
- Web Worker 日志通道（当前日志量无渲染卡顿证据）。
- 新增任务类型或改动后端任务执行逻辑。

## 3. 架构与数据流

```
触发(EnvsView/ModelDetailView/ServicesMatrixView) → TaskButton.onClick
    → API 返回 TaskRef{task_id} → tasksStore.track(ref, {target, callbacks})
        ├─ store 持有 SSE 订阅（生命周期不随视图卸载）
        ├─ 事件推进任务状态：step→running、log→追加日志（环形 2000 行）、done→终态
        ├─ SSE 断流 → 降级 5s 轮询 GET /tasks/{id} 直至终态
        └─ 终态 → ElMessage 通知 + 触发注册的 onSuccess/onError

Layout.mounted → tasksStore.bootstrap()
    → GET /tasks → 回填历史任务
    → 对 queued/running 任务自动重挂 SSE（刷新恢复的关键）

Header → 角标 = running 数 → 打开 TaskDrawer
    → 列表（状态色块 + 进度时间）→ 展开某任务 → 日志面板（数据源=store 缓冲）
    → 失败任务 → 重试按钮（重放注册的 taskTarget）
```

## 4. 后端改动

### 4.1 新增 `GET /admin/api/tasks/{task_id}`

与现有 `GET /tasks` 同路由组（满足 CLAUDE.md"列表+单条"约定）。返回 `task.to_dict()`；不存在返回 404 JSON：

```json
{"error": {"code": "not_found", "message": "任务 task-xxx 不存在或已过期"}}
```

前端收到 404 → 视为"webui 已重启、任务丢失"→ 标记本地任务 `error("服务已重启，任务状态丢失")`，可重试。

### 4.2 `Task.update_detail()` 补广播

`update_detail` 末尾追加 `self.event("step", {"step": 0, "label": detail, "status": self.status, "task_id": self.id})`，使 detail 变更对前端可见（复用 step 事件，避免新增事件类型）。

### 4.3 SSE 事件协议保持不变

`_sse_task_stream` 现有 `event: step|log|done|heartbeat` 命名事件不动（前端改为按命名事件监听即可，见 §5.2）。

## 5. 前端改动

### 5.1 新增 `stores/tasks.ts`

```ts
interface TrackedTask {
  id: string;
  kind: string;           // model_start|service_start|env_setup|trtllm_build|...
  action: string;         // start|stop|restart|setup|...
  target: string;         // profile / service / env 名（TaskButton 匹配键）
  status: 'queued' | 'running' | 'success' | 'skipped' | 'error';
  detail?: string;
  startedAt?: string;     // ISO
  finishedAt?: string;
  exitCode: number;
  logs: string[];         // 环形缓冲，上限 2000 行
  // 恢复/重试所需，非响应数据：
  retryFn?: () => Promise<TaskRef>;
  callbacks?: { onSuccess?: (detail?: string) => void; onError?: (message: string) => void };
  /** SSE 断流后的轮询 timer id，0=无 */
  pollTimer: number;
}
```

Store API：

| 方法 | 语义 |
|---|---|
| `track(ref: TaskRef, opts: {target; retryFn?; callbacks?})` | 新建跟踪：注册任务、打开 SSE 订阅、登记回调 |
| `bootstrap()` | Layout 挂载时调用：`GET /tasks` 回填历史；对 queued/running 任务按 id 重挂 SSE；清理本地已无对应后端的幽灵任务 |
| `activityFor(target: string)` | 返回该 target 的活动任务（queued/running）或最近终态任务，供 TaskButton 派生 phase |
| `retry(taskId)` | 重放该任务的 `retryFn`，新任务替换旧条目 |
| `dismiss(taskId)` | 从列表移除终态任务 |
| `runningCount` | 角标用 |

事件推进规则：

- `step`：`status` 入 running 时填 `startedAt`；`label` 更新到 `detail`（若与 target 不同）。
- `log`：`logs.push(line)`，超 2000 行裁剪头部。
- `done`：写终态 + `finishedAt` + `exitCode`；调 `callbacks` + `ElMessage`（成功 info / 失败 error）。
- `heartbeat`：忽略。

**SSE 断流处理**：`openTaskStream` 的 `onError` 回调里，store 先让 EventSource 原生重连（浏览器自动），若 30s 内未恢复（无事件到达），降级启动 5s 轮询 `GET /tasks/{id}` 直至终态或 404。轮询期间日志增量来自响应 `logs` 字段的 diff（按行数对齐）。

### 5.2 改造 `api/tasks.ts`（修协议 bug）

- `openTaskStream` 改为按命名事件注册：`es.addEventListener('step'|'log'|'done'|'heartbeat', ...)`，事件体直接使用后端 JSON payload（`{step,label,status,task_id}` / `{line}` / `{status,exit_code,message?,task_id}`）。
- 新增 `getTask(taskId): Promise<Task>` —— `GET /tasks/{id}` 封装，404 抛带 `code='not_found'` 的错误。
- 删除旧的单 `message` 监听路径（后端从未发无名事件，属死代码）。

### 5.3 改造 `TaskButton.vue`

- `phase`/`text` 改为 `computed`：从 `tasksStore.activityFor(props.target)` 派生（idle/submitting/running/ok/fail 映射同现有文案）。
- `onClick` 只负责：禁重入 → 调 `taskTarget()` → 把 `TaskRef` 交给 `tasksStore.track(ref, {target, retryFn: props.taskTarget, callbacks})`。
- 不再自持 SSE 句柄/超时定时器（全部上移 store）；`onBeforeUnmount` 钩子删除。
- 视觉反馈（spinner / ✔ / ✗ 文案）保持不变。

### 5.4 新增 `components/common/TaskDrawer.vue`

- Header 加任务图标按钮：运行中数角标（`runningCount`），点击展开抽屉。
- 抽屉内容：任务列表（新在前），每条显示 `target` + 状态色块（queued=琥珀 / running=蓝脉动 / success=绿 / skipped=灰 / error=红）+ `startedAt`~`finishedAt` 耗时 + `detail`。
- 展开单条：日志面板复用 `SseLogViewer` 的终端 `<pre>` 样式，但数据源改为 store 的 `logs`（只读渲染，不再自开 EventSource）。
- 失败任务：`重试`（调 `store.retry`）+ `忽略`（`store.dismiss`）。
- 终态任务：`忽略` 按钮；历史超过 50 条自动裁剪最旧。

### 5.5 Layout / Header 接线

- `Layout.vue` `onMounted` 调 `tasksStore.bootstrap()`。
- `Header.vue` 挂 `TaskDrawer`（抽屉 fixed 右侧滑出，z-index 高于内容区）。

## 6. 异常与恢复矩阵

| 场景 | 检测 | 行为 |
|---|---|---|
| SSE 断开（网络抖动） | `onError` + 30s 无事件 | 原生重连 → 仍失败降级 5s 轮询至终态 |
| webui 重启（任务真丢） | 轮询/重连得 404 | 任务标 `error("服务已重启，任务状态丢失")`，抽屉可重试 |
| 任务失败 | done `status=error` | ElMessage error + 抽屉红态 + 日志尾部可查 |
| 同 target 重复提交 | 后端 409 | ElMessage warning"该目标已有任务在执行"；按钮保持执行中 |
| 前端刷新 | 页面重载 | `bootstrap()` 拉 `/tasks`，running 任务重挂 SSE，按钮恢复执行中 |

## 7. 测试

- **后端**：`GET /tasks/{id}` 200/404 端点测试；`update_detail` 触发 step 事件断言；现有 SSE 流测试补事件名断言。
- **前端 store**：`track`/`bootstrap`/`activityFor`/`retry` 单测；SSE 断流降级轮询路径单测（mock `getTask` 404 → 标记 error）；命名事件解析单测。
- **组件**：TaskDrawer 渲染与交互单测（展开/重试/忽略）。
- **手动验收**：
  1. env setup 启动后切到 /models 再回来 → 按钮仍"执行中"，抽屉可见日志滚动。
  2. setup 进行中刷新页面 → 角标与按钮恢复"执行中"，日志经 `/tasks/{id}` 回填后继续增量。
  3. setup 失败 → ElMessage error + 抽屉红态 + 重试按钮重放成功。

## 8. 实施顺序

1. 后端：`GET /tasks/{id}` + `update_detail` 广播 + 端点测试。
2. 前端 `api/tasks.ts`：命名事件协议 + `getTask`。
3. 前端 `stores/tasks.ts`：store 主体（track/bootstrap/降级轮询）。
4. `TaskButton.vue` 改造为 store 派生。
5. `TaskDrawer.vue` + Header/Layout 接线。
6. 测试与手动验收。

每一步独立可验证，1–2 为后续步骤的协议基础，必须先落地。
