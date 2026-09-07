# SSE 命名事件与 EventSource 监听配对

> 本文件为 Web UI 任务中心（2026-09-07）沉淀问题的聚合入口；原始单文件已并入本文件归档以保留溯源信息。

## 后端发 `event:` 命名事件，前端只监听 `message` —— 流"连上了"却永远收不到数据

- **日期**：2026-09-07　**分类**：后端 / SSE

### 根因

后端 `admin_tasks.py` 的 `Task.event()` 与 `admin_router.py` 的 `_sse_task_stream` 发送的是**命名事件**，每帧都带 `event:` 标签：

```
event: step\ndata: {"step":0,"label":"...","status":"running","task_id":"..."}\n\n
event: log\ndata: {"line":"..."}\n\n
event: done\ndata: {"status":"success","exit_code":0,"task_id":"..."}\n\n
event: heartbeat\ndata: {}\n\n
```

而浏览器 `EventSource` 的 `onmessage`（等价 `addEventListener('message', ...)`）**只派发无名事件或 `event: message` 的帧**——凡带其它 `event:` 名的帧不会进入 `onmessage`，也不报错。旧版前端 `openTaskStream()` 只挂了 `onmessage`，于是 SSE 连接建立成功（HTTP 200、心跳在跑），回调却一次都不触发，UI 永远停在"提交中…"，且没有任何显式错误——典型的**静默失配**。

同一次排查还暴露第二个叠加 bug：`EventSource` 无法携带 `Authorization` 头，后端 SSE 端点用 `require_auth_or_query`（Bearer 失败回退 `?key=` query）。旧 URL 没拼 `?key=`，请求实际 401；但 401 只触发底层 `error` 事件，旧代码的 `onError` 仅 `console.warn`，进一步掩盖了"流从未连通"的真相。

### 解决方案

- **协议两端必须同构核对**：后端 `event: <name>` 的每个名字，前端必须 `es.addEventListener('<name>', ...)` 一一对应；`heartbeat` 保活帧也要注册空监听（不注册虽不报错，但排查时无法区分"没来"和"没接"）。
- SSE URL 必须拼 `?key=<token>`（token 取 `useAuthStore().token`，`encodeURIComponent` 编码），与 `require_auth_or_query` 的 query 回退配对。
- 排查路径：DevTools → Network → 该请求 → **EventStream** 标签页能直接看到帧的 `event:` 名——若帧在、回调不触发，即监听名不匹配；若帧都没有，查 401/代理缓冲（`X-Accel-Buffering: no`）。
- 教训：**不能以"连接 200"作为 SSE 生效的证据**；验收必须打到"回调真的触发"这一层。

### 代码示例

```ts
// ✅ 前端：按后端 event: 标签逐个注册（web/src/api/tasks.ts）
const listeners: Array<[string, EventListener]> = [
  ['step', ((e: MessageEvent) => dispatch<TaskStepEvent>(e.data, hooks.onStep)) as EventListener],
  ['log', ((e: MessageEvent) => dispatch<TaskLogEvent>(e.data, hooks.onLog)) as EventListener],
  ['done', ((e: MessageEvent) => dispatch<TaskDoneEvent>(e.data, hooks.onDone)) as EventListener],
  ['heartbeat', (() => { /* 保活帧，忽略 */ }) as EventListener],
];
for (const [name, fn] of listeners) es.addEventListener(name, fn);

// ✅ URL 带 query 鉴权（EventSource 不能带 Authorization 头）
const url = `/admin/api/tasks/${encodeURIComponent(taskId)}/stream?key=${encodeURIComponent(token)}`;
const es = new EventSource(url);
```

```python
# ✅ 后端：payload 形态与前端监听名严格配对（admin_tasks.py）
payload = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
```

```ts
// ❌ 反例：后端全是命名事件，这里只听 message —— 永不触发
es.onmessage = (e) => handle(JSON.parse(e.data));
```
