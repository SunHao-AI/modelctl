/**
 * SSE 订阅工具：模型启动日志尾随（/admin/api/models/{name}/log/stream）。
 *
 * 后端 admin_models._sse_log_stream 只发**命名事件**（EventSource 必须 addEventListener 按名接收）：
 *   event: log       data: {"line": "..."}   —— 初始尾部 200 行 + 之后每 2s 轮询到的新行
 *   event: heartbeat data: {}                —— 约 10s 一次的保活帧，忽略即可
 *
 * EventSource 不能带 Authorization 头；后端端点用 require_auth_or_query，
 * 必须经 ?key= query 携带 token，否则 401（与 tasks.ts 同款写法）。
 *
 * 本模块把它封装为带 close() 的句柄；调用方在 onBeforeUnmount 时 close。
 */

import { useAuthStore } from '@/stores/auth';

/** 模型日志 SSE log 事件体（后端 data 为纯 JSON，无 type 字段包装） */
export interface LogSseEvent {
  /** 单行文本（不含换行） */
  line: string;
}

/** 单个日志订阅句柄 */
export interface LogStreamHandle {
  /** 关闭 EventSource 并清理监听器 */
  close(): void;
}

/** 回调集合 */
export interface LogStreamHooks {
  /** EventSource 连接建立（open 事件），状态由它驱动而非首行日志 */
  onOpen?: () => void;
  /** 收到 event: log 帧 */
  onLine?: (evt: LogSseEvent) => void;
  /** done 时关闭流（后端日志流通常没有 done，这里仅为对称 API） */
  onDone?: () => void;
  /** EventSource 错误（401 会被 401 拦截，这里处理其它） */
  onError?: (err: Event) => void;
}

/**
 * 打开模型日志 SSE 流。
 *
 * @param url   完整 SSE 路径（如 getModelLogStreamUrl 生成的 /admin/api/models/{name}/log/stream）
 * @param hooks 回调
 * @returns 句柄（{ close() }），必须在 onBeforeUnmount 调用 close()。
 */
export function openModelLogStream(url: string, hooks: LogStreamHooks = {}): LogStreamHandle {
  // url 已是调用方生成的完整路径，这里不再包裹，只追加 ?key= 鉴权参数
  const token = useAuthStore().token;
  const es = new EventSource(`${url}?key=${encodeURIComponent(token)}`);

  /** log 事件的 data 已是纯 JSON 字符串，直接解析；解析异常走 onError */
  const onLog: EventListener = (e) => {
    const raw = (e as MessageEvent).data as string | undefined;
    if (typeof raw !== 'string' || !raw) return;
    try {
      hooks.onLine?.(JSON.parse(raw) as LogSseEvent);
    } catch {
      hooks.onError?.(new Error(`Malformed SSE data: ${raw}`) as unknown as Event);
    }
  };

  const onOpen: EventListener = () => hooks.onOpen?.();
  // heartbeat 仅保活，显式注册避免落入默认 message 之外的未处理路径
  const onHeartbeat: EventListener = () => undefined;
  const onErr: EventListener = (e) => hooks.onError?.(e);

  const listeners: Array<[string, EventListener]> = [
    ['open', onOpen],
    ['log', onLog],
    ['heartbeat', onHeartbeat],
    ['error', onErr],
  ];
  for (const [name, fn] of listeners) es.addEventListener(name, fn);

  return {
    close() {
      try {
        for (const [name, fn] of listeners) es.removeEventListener(name, fn);
        es.close();
      } catch {
        // 已 close，忽略
      }
    },
  };
}
