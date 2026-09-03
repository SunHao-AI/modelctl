/**
 * SSE 订阅工具：模型启动日志尾随（/admin/api/models/{name}/log/stream）。
 *
 * 后端 admin_models._sse_log_stream 推送：
 *   data: {"type": "line" | "tail", "line": "..."}
 *  - line：实时新行（首次推送 200 行后开始，更新每 2s 一次）
 *  - tail：初始保留的尾部行（与 line 同一形态，仅语义不同）
 *
 * 本模块把它封装为带 close() 的句柄；调用方在 onBeforeUnmount 时 close。
 */

/** 模型日志 SSE 事件类型 */
export type LogSseType = 'line' | 'tail';

export interface LogSseEvent {
  type: LogSseType;
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
  /** 收到 line / tail */
  onLine?: (evt: LogSseEvent) => void;
  /** done 时关闭流（后端日志流通常没有 done，这里仅为对称 API） */
  onDone?: () => void;
  /** EventSource 错误（401 会被 401 拦截，这里处理其它） */
  onError?: (err: Event) => void;
}

/**
 * 打开模型日志 SSE 流。
 *
 * @param name  模型名
 * @param hooks 回调
 * @returns 句柄（{ close() }），必须在 onBeforeUnmount 调用 close()。
 */
export function openModelLogStream(name: string, hooks: LogStreamHooks = {}): LogStreamHandle {
  const url = `/admin/api/models/${encodeURIComponent(name)}/log/stream`;
  const es = new EventSource(url);

  /** 解析一行 SSE 数据（data: {...}） */
  function dispatch(raw: string): void {
    if (!raw.startsWith('data:')) return;
    const body = raw.slice(5).trim();
    if (!body) return;
    let evt: LogSseEvent;
    try {
      evt = JSON.parse(body) as LogSseEvent;
    } catch {
      hooks.onError?.(new Event(`Malformed SSE data: ${body}`));
      return;
    }
    if (!evt || !('line' in evt)) {
      hooks.onError?.(new Event('Empty SSE payload'));
      return;
    }
    hooks.onLine?.(evt);
  }

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
