import { useAuthStore } from '@/stores/auth';
import router from '@/router';

export interface ChatUsage {
  prompt_tokens: number;
  completion_tokens: number;
}

export interface ChatMeta {
  routedTo: string;
  routeReason: string;
  status: number;
}

export interface ChatHandlers {
  onDelta(t: string): void;
  onReasoning(t: string): void;
  onUsage(u: ChatUsage): void;
  onMeta(m: ChatMeta): void;
  onDone(): void;
  onError(e: { message: string; raw?: string }): void;
}

/**
 * 逐帧消费 SSE：按空行切帧 → 取 `data:` 行 → JSON.parse。
 * 畸形帧不中断（上游各引擎的流格式差异正是调试台要看见的东西），交 onRaw 累积。
 */
async function consumeSse(res: Response, h: ChatHandlers, onRaw: (s: string) => void): Promise<void> {
  const reader = res.body!.getReader();
  const dec = new TextDecoder();
  let buf = '';

  // 单帧解析：返回 true 表示命中 [DONE]，调用方应立即结束消费。
  const handleFrame = (frame: string): boolean => {
    for (const line of frame.split('\n')) {
      const s = line.trim();
      if (!s.startsWith('data:')) continue;
      const data = s.slice(5).trim();
      if (data === '[DONE]') {
        h.onDone();
        return true;
      }
      try {
        const obj = JSON.parse(data);
        const delta = obj.choices?.[0]?.delta;
        if (delta?.content) h.onDelta(delta.content);
        // vLLM/SGLang 的思考通道两种字段名都出现过
        const reasoning = delta?.reasoning ?? delta?.reasoning_content;
        if (reasoning) h.onReasoning(reasoning);
        // include_usage 的帧 choices 为空：只取 usage，绝不当正文
        if (obj.usage) h.onUsage(obj.usage);
      } catch {
        onRaw(data);
      }
    }
    return false;
  };

  // 空行分隔兼容 `\n\n` / `\r\n\r\n` 及混合形式。必须对**累积后的 buf** 用正则匹配：
  // 若只对单块做 `\r\n`→`\n` 替换，块边界正好落在 `\r` 与 `\n` 之间时会漏掉分隔符。
  const delim = /\r?\n\r?\n/;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let m: RegExpExecArray | null;
    while ((m = delim.exec(buf))) {
      const frame = buf.slice(0, m.index);
      buf = buf.slice(m.index + m[0].length);
      if (handleFrame(frame)) return;
    }
  }
  // 上游可能以「最后一帧 + EOF」收尾而没有终止空行，残留 buf 也要当一帧解析。
  if (buf && handleFrame(buf)) return;
  h.onDone();
}

/**
 * 对话流式请求。**必须 fetch 而非 axios**：要读 ReadableStream 边到边渲染，
 * 并读响应头 X-Chat-Routed-To。代价是绕过 client.ts 的 401 拦截器，
 * 故此处自己复刻同样的清 token + 跳登录语义。
 */
export async function streamChatCompletions(
  payload: Record<string, unknown>,
  h: ChatHandlers,
  signal: AbortSignal,
  onRaw: (s: string) => void,
): Promise<void> {
  const auth = useAuthStore();
  let res: Response;
  try {
    res = await fetch('/admin/api/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${auth.token}` },
      body: JSON.stringify(payload),
      signal,
    });
  } catch (e) {
    if ((e as Error).name === 'AbortError') {
      h.onDone();
      return;
    }
    h.onError({ message: `网络中断：${e}` });
    return;
  }

  if (res.status === 401) {
    auth.clear();
    const cur = router.currentRoute.value;
    if (cur.path !== '/login') router.push({ path: '/login', query: { redirect: cur.fullPath } });
    h.onError({ message: '登录态失效' });
    return;
  }

  h.onMeta({
    routedTo: res.headers.get('x-chat-routed-to') || '',
    routeReason: res.headers.get('x-chat-route-reason') || '',
    status: res.status,
  });

  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => '');
    h.onError({ message: `HTTP ${res.status}`, raw: text });
    return;
  }
  try {
    await consumeSse(res, h, onRaw);
  } catch (e) {
    if ((e as Error).name === 'AbortError') {
      // stop() 在 fetch 已返回后才 abort：reader.read() 抛 AbortError，属正常中断收尾，
      // 不能让异常冒出去拒绝 store.send() 的 promise。
      h.onDone();
      return;
    }
    // 其余意外错误上报给调用方，而不是让 send() 的 promise 被拒绝。
    h.onError({ message: `流式解析中断：${e}` });
  }
}
