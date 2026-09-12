/**
 * utils/toast.ts 单元测试：自动消失（setTimeout 摘除）、duration=0 常驻、
 * dismiss 幂等。toast 是任务终态唯一用户反馈通道，条目泄漏会堆满屏幕。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { toast, useToastItems } from './toast';

const { items } = useToastItems();

beforeEach(() => {
  vi.useFakeTimers();
  items.value.splice(0, items.value.length);
});

describe('toast 条目生命周期', () => {
  it('success 入列后 3s 自动消失', () => {
    toast.success('done');
    expect(items.value).toHaveLength(1);
    vi.advanceTimersByTime(3000);
    expect(items.value).toHaveLength(0);
  });

  it('error 缺省驻留 4s（比 success 长，留出读错误的时间）', () => {
    toast.error('boom');
    vi.advanceTimersByTime(2999);
    expect(items.value).toHaveLength(1);
    vi.advanceTimersByTime(1001);
    expect(items.value).toHaveLength(0);
  });

  it('duration=0 常驻，直到手动 dismiss', () => {
    toast.warning('stale', 0);
    vi.advanceTimersByTime(60_000);
    expect(items.value).toHaveLength(1);
    const id = items.value[0].id;
    toast.dismiss(id);
    expect(items.value).toHaveLength(0);
  });

  it('dismiss 未知 id 幂等（不误删他人）', () => {
    toast.success('a');
    toast.dismiss(999_999);
    expect(items.value).toHaveLength(1);
  });

  it('多条消息 id 递增且 kind 正确', () => {
    toast.success('a');
    toast.error('b');
    toast.warning('c');
    expect(items.value.map((t) => t.kind)).toEqual(['success', 'error', 'warning']);
    const ids = items.value.map((t) => t.id);
    expect(new Set(ids).size).toBe(3);
  });
});
