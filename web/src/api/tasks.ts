/**
 * SSE 订阅工具：通用任务流（/admin/api/tasks/{id}/stream）。
 *
 * 后端 admin_tasks.Task.event() 推送事件，典型形态：
 *   data: {"type": "status"|"log"|"error"|"done", "data": ...}
 * 本模块把 EventSource 包裹为带 close() 的句柄，调用方在 mount 钩子里
 * 创建，在 beforeUnmount 里 close 即可。
 */

/** 任务 SSE 数据类型（覆盖后端 admin_tasks.Task 的广播事件） */
export interface TaskSseEvent {
  /** 事件类型 */
  type: 'status' | 'log' | 'error' | 'done';
  /** 携带数据（log 含 line / done 含 status + exit_code + message 等） */
  data: unknown;
}

/** 单个任务订阅句柄 */
export interface TaskStreamHandle {
  /** 关闭 EventSource 并清理监听器 */
  close(): void;
}

/** 回调集合 */
export interface TaskStreamHooks {
  /** 收到任意 type / data */
  onData?: (evt: TaskSseEvent) => void;
  /** 解析失败 / 网络错误 */
  onError?: (err: Event) => void;
  /** 收到 type === "done" 时调用（一般在这时关流） */
  onDone?: (evt: TaskSseEvent) => void;
}

/**
 * 打开任务 SSE 流。
 *
 * @param taskId 后端返回的 task_id（task-xxxxxxxx）
 * @param hooks  回调集合
 * @returns 句柄（{ close() }），必须在 onBeforeUnmount 调用 close()。
 */
export function openTaskStream(
  taskId: string,
  hooks: TaskStreamHooks = {},
): TaskStreamHandle {
  // 注意：EventSource 不带 Authorization header；
  // 若后端依赖 401 拦截，前端应保留 token query 或本地代理。这里按
  // admin_tasks.py 的约定不强制鉴权（任务流是「内部通道」，同一线程
  // 已有 require_auth 门槛）。
  const url = `/admin/api/tasks/${encodeURIComponent(taskId)}/stream`;
  const es = new EventSource(url);

  /** 解析 SSE 行（data: {...} 一行 JSON） */
  function dispatch(raw: string): void {
    // 仅处理 data 行；其余行忽略
    if (!raw.startsWith('data:')) return;
    const body = raw.slice(5).trim();
    if (!body) return;
    let evt: TaskSseEvent;
    try {
      evt = JSON.parse(body) as TaskSseEvent;
    } catch {
      hooks.onError?.(new Event(`Malformed SSE data: ${body}`));
      return;
    }
    if (!evt || typeof evt !== 'object') {
      hooks.onError?.(new Event('Empty SSE payload'));
      return;
    }
    if (evt.type === 'done') {
      hooks.onDone?.(evt);
    } else {
      hooks.onData?.(evt);
    }
  }

  // 后端 task stream 不发 `event:` 标签（全部走默认 message），消息首为 data 行。
  // 用单一 message 监听兜底；如果未来后端加上 event: 标签，再按名字拆分。
  const onMsg = (e: MessageEvent) => dispatch(String(e.data));
  const onErr = (e: Event) => hooks.onError?.(e);
  es.addEventListener('message', onMsg);
  es.addEventListener('error', onErr);

  return {
    close() {
      try {
        es.removeEventListener('message', onMsg);
        es.removeEventListener('error', onErr);
        es.close();
      } catch {
        // 已 close，忽略
      }
    },
  };
}
