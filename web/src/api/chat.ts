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
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split('\n')) {
        const s = line.trim();
        if (!s.startsWith('data:')) continue;
        const data = s.slice(5).trim();
        if (data === '[DONE]') {
          h.onDone();
          return;
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
    }
  }
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
  await consumeSse(res, h, onRaw);
}
