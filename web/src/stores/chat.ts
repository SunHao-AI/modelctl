import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import { streamChatCompletions, type ChatUsage } from '@/api/chat';

export interface ChatMsg {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  reasoning: string;
  images: string[];
  usage?: ChatUsage;
  ttftMs?: number;
  totalMs?: number;
  error?: { message: string; raw?: string };
  interrupted?: boolean;
  routedTo?: string;
  routeReason?: string;
}

export interface ChatSession {
  id: string;
  title: string;
  model: string;
  createdAt: string;
  messages: ChatMsg[];
}

export const LS_KEY = 'modelctl.chat.v1';
export const LS_CAP = 4 * 1024 * 1024;
const LS_MAX_SESSIONS = 50;
const TITLE_LEN = 24;

function uid(): string {
  return Math.random().toString(36).slice(2);
}

/** `YYYY-MM-DD HH:mm:ss`（sv-SE locale 天然是该形状，避免引 dayjs 只做这一件事）。 */
function now(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export const useChatStore = defineStore('chat', () => {
  const sessions = ref<ChatSession[]>([]);
  const activeId = ref('');
  const model = ref('');
  const routeMode = ref<'direct' | 'gateway'>('direct');
  const params = ref({ temperature: 0.7, top_p: 0.95, max_tokens: 2048, system: '', includeUsage: true });
  const streaming = ref(false);
  const rawText = ref('');
  const storageDegraded = ref(false);
  let controller: AbortController | null = null;

  const active = computed(() => sessions.value.find((s) => s.id === activeId.value) || null);
  const messages = computed(() => active.value?.messages ?? []);

  function hydrate() {
    try {
      sessions.value = JSON.parse(localStorage.getItem(LS_KEY) || '[]');
    } catch {
      sessions.value = [];
    }
  }

  /**
   * 容量降级顺序（图片 data URL 是体积大头）：
   * 截会话数 → 从最旧开始剥图片 → 仍超则从最旧整会话丢弃。
   */
  function persist() {
    let list = sessions.value.slice(-LS_MAX_SESSIONS);
    let str = JSON.stringify(list);
    if (str.length > LS_CAP) {
      for (const s of [...list]) {
        for (const m of s.messages) if (m.images?.length) m.images = [];
        str = JSON.stringify(list);
        if (str.length <= LS_CAP) {
          storageDegraded.value = true;
          break;
        }
      }
    }
    while (str.length > LS_CAP && list.length > 1) {
      list = list.slice(1);
      str = JSON.stringify(list);
      storageDegraded.value = true;
    }
    try {
      localStorage.setItem(LS_KEY, str);
    } catch {
      /* 隐私模式 / 配额硬失败：对话本身不该因此报错 */
    }
  }

  function setModel(m: string) {
    model.value = m;
  }

  function newSession() {
    const s: ChatSession = { id: uid(), title: '新对话', model: model.value, createdAt: now(), messages: [] };
    sessions.value.push(s);
    activeId.value = s.id;
    persist();
  }

  function selectSession(id: string) {
    activeId.value = id;
  }

  async function send(text: string, images: string[] = []) {
    if (!active.value) newSession();
    const s = active.value!;
    if (!text && !images.length) return;
    if (s.title === '新对话') s.title = (text || '图片对话').slice(0, TITLE_LEN);
    s.messages.push({ id: uid(), role: 'user', content: text, reasoning: '', images });
    const asst: ChatMsg = { id: uid(), role: 'assistant', content: '', reasoning: '', images: [] };
    s.messages.push(asst);

    streaming.value = true;
    rawText.value = '';
    controller = new AbortController();
    const t0 = performance.now();
    let first = true;
    const markFirst = () => {
      if (first) {
        asst.ttftMs = performance.now() - t0;
        first = false;
      }
    };

    const content: Array<Record<string, unknown>> = images.map((d) => ({ type: 'image_url', image_url: { url: d } }));
    if (text) content.push({ type: 'text', text });

    await streamChatCompletions(
      {
        model: s.model,
        messages: [{ role: 'user', content }],
        route_mode: routeMode.value,
        temperature: params.value.temperature,
        top_p: params.value.top_p,
        max_tokens: params.value.max_tokens,
        system: params.value.system || undefined,
        include_usage: params.value.includeUsage,
      },
      {
        onDelta: (t) => { markFirst(); asst.content += t; },
        onReasoning: (t) => { markFirst(); asst.reasoning += t; },
        onUsage: (u) => { asst.usage = u; },
        onMeta: (m) => { asst.routedTo = m.routedTo; asst.routeReason = m.routeReason; },
        onDone: () => { asst.totalMs = performance.now() - t0; streaming.value = false; controller = null; persist(); },
        onError: (e) => { asst.error = e; streaming.value = false; controller = null; persist(); },
      },
      controller.signal,
      (r) => { rawText.value += r + '\n'; },
    );
  }

  function stop() {
    controller?.abort();
    const last = messages.value[messages.value.length - 1];
    if (last?.role === 'assistant') last.interrupted = true;
    streaming.value = false;
    persist();
  }

  hydrate();

  return {
    sessions, activeId, active, messages, model, routeMode, params, streaming, rawText, storageDegraded,
    setModel, newSession, selectSession, send, stop, persist, hydrate,
  };
});
