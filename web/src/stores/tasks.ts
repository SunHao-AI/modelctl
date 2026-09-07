/**
 * 全局任务状态层：SSE 订阅生命周期归 store（不随视图卸载），
 * 刷新/切页后 bootstrap() 从 GET /tasks 恢复真实状态。
 *
 * - track(): 触发新任务（TaskButton 调用）
 * - bootstrap(): Layout 挂载时回填历史 + 对活动任务重挂 SSE
 * - activityFor(target): TaskButton 派生 phase
 * - 降级链：SSE 断流 → 宽限期后 5s 轮询 GET /tasks/{id} → 404 判"服务已重启"
 */

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
  /** 匹配键：profile / service / env 名（TaskButton 用） */
  target: string;
  /** 重试时重放的原触发函数 */
  retryFn?: () => Promise<TaskRef>;
  /** 终态回调 */
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

function isActive(t: TaskRecord): boolean {
  return t.status === 'queued' || t.status === 'running';
}

/** TaskInfo → TaskRecord（bootstrap / 轮询回填用） */
function toRecord(t: TaskInfo): TaskRecord {
  return {
    id: t.id,
    kind: t.kind,
    action: t.action,
    target: t.target,
    status: t.status,
    detail: t.detail ?? '',
    startedAt: t.started_at,
    finishedAt: t.finished_at ?? '',
    exitCode: t.exit_code,
    logs: [...t.logs],
    stream: null,
    pollTimer: 0,
    lastEventAt: Date.now(),
  };
}

export const useTasksStore = defineStore('tasks', () => {
  const tasks = ref<TaskRecord[]>([]);
  const runningCount = computed(() => tasks.value.filter(isActive).length);

  /** id → 在 tasks 数组中的下标（-1 = 不存在） */
  function idx(id: string): number {
    return tasks.value.findIndex((t) => t.id === id);
  }

  /** 释放 SSE / 轮询资源（不删记录本身） */
  function teardown(t: TaskRecord) {
    try {
      t.stream?.close();
    } catch {
      /* ignore */
    }
    t.stream = null;
    if (t.pollTimer) {
      clearInterval(t.pollTimer);
      t.pollTimer = 0;
    }
  }

  /** 裁剪至 MAX_TASKS（最旧终态先删；活动任务不裁） */
  function trim() {
    const done = tasks.value.filter((t) => !isActive(t));
    const overflow = done.length - MAX_TASKS;
    if (overflow <= 0) return;
    const drop = new Set(done.slice(0, overflow).map((t) => t.id));
    for (const t of tasks.value) {
      if (drop.has(t.id)) teardown(t);
    }
    tasks.value = tasks.value.filter((t) => !drop.has(t.id));
  }

  /** 追加日志（环形裁剪） */
  function appendLog(t: TaskRecord, line: string) {
    t.logs.push(line);
    if (t.logs.length > MAX_LOG_LINES) {
      t.logs.splice(0, t.logs.length - MAX_LOG_LINES);
    }
  }

  /** 推进到终态：清资源、裁剪、toast 通知、触发回调 */
  function finalize(t: TaskRecord, status: TaskStatus, detail: string, exitCode: number) {
    t.status = status;
    t.detail = detail;
    t.exitCode = exitCode;
    t.finishedAt = new Date().toISOString();
    teardown(t);
    trim();
    if (status === 'success' || status === 'skipped') {
      toast.success(`${t.target} ${t.action} 完成`);
      try {
        t.onSuccess?.(detail);
      } catch {
        /* ignore */
      }
    } else {
      toast.error(`${t.target} ${t.action} 失败：${detail}`);
      try {
        t.onError?.(detail);
      } catch {
        /* ignore */
      }
    }
  }

  /** 降级轮询：5s 一次 GET /tasks/{id}，404 = webui 重启 → 标记 error */
  function startPolling(t: TaskRecord) {
    if (t.pollTimer) return;
    t.pollTimer = window.setInterval(async () => {
      const i = idx(t.id);
      if (i < 0) {
        clearInterval(t.pollTimer);
        return;
      }
      const cur = tasks.value[i];
      try {
        const info = await getTask(t.id);
        cur.lastEventAt = Date.now();
        // 日志增量对齐：store 缓冲可能比后端环形（500 行）更长，只追加新增尾部
        for (let n = cur.logs.length; n < info.logs.length; n += 1) {
          appendLog(cur, info.logs[n]);
        }
        if (isActive(cur)) {
          cur.status = info.status;
          cur.detail = info.detail ?? cur.detail;
          if (info.started_at && !cur.startedAt) cur.startedAt = info.started_at;
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

  /** 挂 SSE：事件推进状态；断流宽限期后仍未活跃则降级轮询 */
  function attachStream(t: TaskRecord) {
    teardown(t);
    t.lastEventAt = Date.now();
    t.stream = openTaskStream(t.id, {
      onStep: (evt) => {
        t.lastEventAt = Date.now();
        if (evt.status === 'running' && !t.startedAt) {
          t.startedAt = new Date().toISOString();
        }
        // 首帧 label=target（update_status 广播），detail 帧 label=detail——
        // 仅当 label 与 target 不同才视为 detail，避免把 target 当进度文案
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
        // 浏览器原生自动重连；宽限期后仍无活跃事件则降级轮询
        window.setTimeout(() => {
          const i = idx(t.id);
          if (i < 0) return;
          const cur = tasks.value[i];
          if (!isActive(cur)) return;
          if (Date.now() - cur.lastEventAt < RECONNECT_GRACE_MS) return;
          startPolling(cur);
        }, RECONNECT_GRACE_MS);
      },
    });
  }

  /** 触发新任务：登记 + 挂 SSE；元数据（kind/action）异步补齐 */
  function track(refVal: TaskRef, opts: TrackOpts) {
    const rec: TaskRecord = {
      id: refVal.task_id,
      kind: '',
      action: '',
      target: opts.target,
      status: 'queued',
      detail: '',
      startedAt: '',
      finishedAt: '',
      exitCode: 0,
      logs: [],
      retryFn: opts.retryFn,
      onSuccess: opts.onSuccess,
      onError: opts.onError,
      stream: null,
      pollTimer: 0,
      lastEventAt: Date.now(),
    };
    // 补 kind/action：立即拉一次详情（失败不阻塞，仅缺展示元数据）
    void getTask(refVal.task_id)
      .then((info) => {
        const i = idx(rec.id);
        if (i < 0) return;
        tasks.value[i].kind = info.kind;
        tasks.value[i].action = info.action;
        if (info.started_at && !tasks.value[i].startedAt) {
          tasks.value[i].startedAt = info.started_at;
        }
      })
      .catch(() => {
        /* ignore：SSE/轮询路径不依赖元数据 */
      });
    tasks.value.unshift(rec);
    trim();
    attachStream(rec);
  }

  /** 页面重载后：拉历史 + 对活动任务重挂 SSE */
  async function bootstrap() {
    const list = await dataOf(client.get<{ tasks: TaskInfo[] }>('/tasks'));
    for (const info of list.tasks) {
      if (idx(info.id) >= 0) continue; // 防重复登记
      tasks.value.push(toRecord(info));
    }
    // 新在前（与后端 list_tasks 的 started_at 倒序一致，双保险）
    tasks.value.sort((a, b) => (b.startedAt || '').localeCompare(a.startedAt || ''));
    trim();
    for (const t of tasks.value) {
      if (isActive(t)) attachStream(t);
    }
  }

  /** 按 target 查活动任务；无活动则查最近终态（TaskButton 派生 phase） */
  function activityFor(target: string): TaskRecord | undefined {
    return (
      tasks.value.find((t) => t.target === target && isActive(t)) ??
      tasks.value.find((t) => t.target === target)
    );
  }

  /** 重试：重放 retryFn，旧记录移除、新任务入列 */
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
      const refVal = await old.retryFn();
      track(refVal, {
        target: old.target,
        retryFn: old.retryFn,
        onSuccess: old.onSuccess,
        onError: old.onError,
      });
    } catch (err) {
      const msg = (err as { message?: string })?.message || '重试提交失败';
      const rec: TaskRecord = {
        ...toRecord({
          id: `task-retry-${Date.now()}`,
          kind: old.kind,
          action: old.action,
          target: old.target,
          status: 'error',
          exit_code: 1,
          started_at: new Date().toISOString(),
          finished_at: new Date().toISOString(),
          detail: msg,
          logs: [],
        }),
        retryFn: old.retryFn,
      };
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

  return { tasks, runningCount, track, bootstrap, activityFor, retry, dismiss };
});
