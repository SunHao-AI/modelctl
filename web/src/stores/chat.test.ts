import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';

function fakeFetch(chunks: string[], headers: Record<string, string> = {}) {
  let i = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(c) { if (i < chunks.length) c.enqueue(new TextEncoder().encode(chunks[i++])); else c.close(); },
  });
  return vi.fn(async () => ({
    ok: true, status: 200, body,
    headers: new Headers({ 'x-chat-routed-to': 'a', 'x-chat-route-reason': 'group_route', ...headers }),
  }));
}

const SSE_OK = [
  'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
  'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
  'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
  'data: [DONE]\n\n',
];

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
});

it('逐块拼接 content，usage 帧不当正文', async () => {
  global.fetch = fakeFetch(SSE_OK) as never;
  const { useChatStore } = await import('./chat');
  const s = useChatStore();
  s.setModel('a');
  await s.send('hi', []);
  const last = s.messages[s.messages.length - 1];
  expect(last.role).toBe('assistant');
  expect(last.content).toBe('Hello');
  expect(last.usage).toEqual({ prompt_tokens: 5, completion_tokens: 2 });
  expect(last.routedTo).toBe('a');
  expect(s.streaming).toBe(false);
});

it('reasoning 与 content 分区', async () => {
  global.fetch = fakeFetch([
    'data: {"choices":[{"delta":{"reasoning_content":"想一想"}}]}\n\n',
    'data: {"choices":[{"delta":{"content":"答"}}]}\n\n',
    'data: [DONE]\n\n',
  ]) as never;
  const { useChatStore } = await import('./chat');
  const s = useChatStore();
  s.setModel('a');
  await s.send('q', []);
  const last = s.messages[s.messages.length - 1];
  expect(last.reasoning).toBe('想一想');
  expect(last.content).toBe('答');
});

it('畸形帧不中断流，原文进 rawText', async () => {
  global.fetch = fakeFetch([
    'data: not-json\n\n',
    'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
    'data: [DONE]\n\n',
  ]) as never;
  const { useChatStore } = await import('./chat');
  const s = useChatStore();
  s.setModel('a');
  await s.send('hi', []);
  expect(s.messages[s.messages.length - 1].content).toBe('ok');
  expect(s.rawText).toContain('not-json');
});

it('HTTP 400 保留状态码与上游原文', async () => {
  global.fetch = vi.fn(async () => ({
    ok: false, status: 400, body: null,
    headers: new Headers({ 'x-chat-routed-to': 'a' }),
    text: async () => '{"error":{"message":"max 0 images"}}',
  })) as never;
  const { useChatStore } = await import('./chat');
  const s = useChatStore();
  s.setModel('a');
  await s.send('hi', ['data:image/jpeg;base64,AAA']);
  const last = s.messages[s.messages.length - 1];
  expect(last.error?.message).toContain('400');
  expect(last.error?.raw).toContain('max 0 images');
});

it('会话持久化到 localStorage 并可回读', async () => {
  global.fetch = fakeFetch(SSE_OK) as never;
  const { useChatStore, LS_KEY } = await import('./chat');
  const s = useChatStore();
  s.setModel('a');
  await s.send('你好', []);
  const saved = JSON.parse(localStorage.getItem(LS_KEY) || '[]');
  expect(saved).toHaveLength(1);
  expect(saved[0].title).toBe('你好');
});

it('超限时先剥最旧会话图片，仍超再丢整会话', async () => {
  const { useChatStore, LS_KEY, LS_CAP } = await import('./chat');
  const s = useChatStore();
  s.setModel('a');
  s.newSession();
  s.active!.messages.push({ id: 'm1', role: 'user', content: 'old', reasoning: '', images: ['data:image/jpeg;base64,' + 'A'.repeat(LS_CAP / 2)] });
  s.newSession();
  s.active!.messages.push({ id: 'm2', role: 'user', content: 'new', reasoning: '', images: ['data:image/jpeg;base64,' + 'B'.repeat(LS_CAP / 2)] });
  s.persist();
  const saved = JSON.parse(localStorage.getItem(LS_KEY) || '[]');
  expect(s.storageDegraded).toBe(true);
  expect(saved.length).toBeLessThanOrEqual(2);
  expect(saved.every((x: { messages: { images: unknown[] }[] }) =>
    x.messages.every((m) => Array.isArray(m.images)))).toBe(true);
});
