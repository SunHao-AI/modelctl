/**
 * 任务 SSE 订阅与单条查询工具。
 *
 * 后端 _sse_task_stream 发送**命名事件**（EventSource 必须 addEventListener 按名接收）：
 *   event: step      data: {"step":0,"label":"...","status":"running","task_id":"..."}
 *   event: log       data: {"line":"..."}
 *   event: done      data: {"status":"success|error","exit_code":0,"message"?:"...","task_id":"..."}
 *   event: heartbeat data: {}
 * 本模块按事件名拆分回调；调用方必须在 onBeforeUnmount 调 close()。
 */

import client, { dataOf } from './client';
import { useAuthStore } from '@/stores/auth';
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
  /** 失败分类码（后端 Task.error 携带；success 时无） */
  code?: string;
  /** 分类码关联引擎名 */
  engine?: string;
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
  /** 保活帧（约 10s 一次）：静默长任务期间调用方借此刷新"最后活跃时间" */
  onHeartbeat?: () => void;
  /** EventSource 底层 error（网络层；重连与降级决策交调用方） */
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
  // 必须经 ?key= query 携带 token，否则 401（旧实现漏带 key，流从未连通）。
  const token = useAuthStore().token;
  const url = `/admin/api/tasks/${encodeURIComponent(taskId)}/stream?key=${encodeURIComponent(token)}`;
  const es = new EventSource(url);

  /** 命名事件的 data 已是纯 JSON 字符串，直接解析；解析异常走 onError */
  function dispatch<T>(raw: string | undefined, cb?: (evt: T) => void) {
    if (typeof raw !== 'string' || !raw) return;
    try {
      cb?.(JSON.parse(raw) as T);
    } catch {
      hooks.onError?.(new Error(`Malformed SSE data: ${raw}`) as unknown as Event);
    }
  }

  const listeners: Array<[string, EventListener]> = [
    ['step', ((e: MessageEvent) => dispatch<TaskStepEvent>(e.data, hooks.onStep)) as EventListener],
    ['log', ((e: MessageEvent) => dispatch<TaskLogEvent>(e.data, hooks.onLog)) as EventListener],
    ['done', ((e: MessageEvent) => dispatch<TaskDoneEvent>(e.data, hooks.onDone)) as EventListener],
    ['heartbeat', (() => hooks.onHeartbeat?.()) as EventListener],
  ];
  for (const [name, fn] of listeners) es.addEventListener(name, fn);
  const onErr: EventListener = (e) => hooks.onError?.(e);
  es.addEventListener('error', onErr);

  return {
    close() {
      try {
        for (const [name, fn] of listeners) es.removeEventListener(name, fn);
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
 * 404（webui 重启导致任务丢失）时抛 Error 且 err.code === 'not_found'，
 * 调用方据此判定"服务已重启，任务状态丢失"。
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
