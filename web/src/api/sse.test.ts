/**
 * api/sse.ts 单元测试：模型日志 SSE 封装的关键行为——
 *   1. EventSource 无法带 header，token 必须以 ?key= query 携带（后端
 *      require_auth_or_query 的降级通道），且要 URL 编码。
 *   2. log 帧 JSON 解析失败 → onError（不是静默吞掉）。
 *   3. close() 之后的 error 事件必须被抑制（浏览器对主动 close 也 emit
 *      error/net::ERR_ABORTED，上抛会造成 console 误报）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useAuthStore } from '@/stores/auth';
import { openModelLogStream } from './sse';

/** 可编程的 EventSource 桩：记录 URL、手动派发命名事件 */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  closed = false;
  private handlers = new Map<string, EventListener[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(name: string, fn: EventListener) {
    const list = this.handlers.get(name) ?? [];
    list.push(fn);
    this.handlers.set(name, list);
  }

  removeEventListener(name: string, fn: EventListener) {
    const list = this.handlers.get(name) ?? [];
    this.handlers.set(name, list.filter((f) => f !== fn));
  }

  close() {
    this.closed = true;
  }

  emit(name: string, data?: unknown) {
    const evt = { data } as unknown as Event;
    for (const fn of this.handlers.get(name) ?? []) fn(evt);
  }
}

function lastEs(): FakeEventSource {
  return FakeEventSource.instances[FakeEventSource.instances.length - 1];
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
});

describe('鉴权 query 注入', () => {
  it('token 以 ?key= 追加且经 URL 编码', () => {
    const auth = useAuthStore();
    auth.persistToken('a b+c&d');
    openModelLogStream('/admin/api/models/m/log/stream');
    expect(lastEs().url).toBe('/admin/api/models/m/log/stream?key=a%20b%2Bc%26d');
  });

  it('未登录也建流（key= 空串，由后端 401 决定成败）', () => {
    openModelLogStream('/admin/api/models/m/log/stream');
    expect(lastEs().url).toBe('/admin/api/models/m/log/stream?key=');
  });
});

describe('事件分发', () => {
  it('log 帧 JSON 解析后交给 onLine', () => {
    const onLine = vi.fn();
    openModelLogStream('/x', { onLine });
    lastEs().emit('log', '{"line":"hello"}');
    expect(onLine).toHaveBeenCalledWith({ line: 'hello' });
  });

  it('log 帧非法 JSON → onError 而非静默', () => {
    const onError = vi.fn();
    openModelLogStream('/x', { onError });
    lastEs().emit('log', '{oops');
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it('空 data 的 log 帧直接忽略', () => {
    const onLine = vi.fn();
    openModelLogStream('/x', { onLine });
    lastEs().emit('log', '');
    expect(onLine).not.toHaveBeenCalled();
  });

  it('stopped 帧解析 reason 并回调', () => {
    const onStopped = vi.fn();
    openModelLogStream('/x', { onStopped });
    lastEs().emit('stopped', '{"reason":"container removed"}');
    expect(onStopped).toHaveBeenCalledWith('container removed');
  });

  it('stopped 帧非 JSON 时原文作 reason', () => {
    const onStopped = vi.fn();
    openModelLogStream('/x', { onStopped });
    lastEs().emit('stopped', 'gone');
    expect(onStopped).toHaveBeenCalledWith('gone');
  });

  it('heartbeat 帧不触发任何回调', () => {
    const onLine = vi.fn();
    openModelLogStream('/x', { onLine });
    lastEs().emit('heartbeat', '{}');
    expect(onLine).not.toHaveBeenCalled();
  });
});

describe('close() 语义', () => {
  it('close 后 EventSource.close() 被调用', () => {
    const h = openModelLogStream('/x');
    h.close();
    expect(lastEs().closed).toBe(true);
  });

  it('close 后的 error 事件被抑制，不再上抛', () => {
    const onError = vi.fn();
    const h = openModelLogStream('/x', { onError });
    h.close();
    lastEs().emit('error');
    expect(onError).not.toHaveBeenCalled();
  });

  it('close 前的 error 正常上抛（真实网络故障要能感知）', () => {
    const onError = vi.fn();
    openModelLogStream('/x', { onError });
    lastEs().emit('error');
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it('close 后再收 log 帧不再分发（监听器已摘除）', () => {
    const onLine = vi.fn();
    const h = openModelLogStream('/x', { onLine });
    h.close();
    lastEs().emit('log', '{"line":"late"}');
    expect(onLine).not.toHaveBeenCalled();
  });
});
