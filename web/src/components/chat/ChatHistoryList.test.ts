import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import ChatHistoryList from './ChatHistoryList.vue';
import type { ChatSession } from '@/stores/chat';

const sessions: ChatSession[] = [
  { id: 'a', title: '会话A', model: 'm1', createdAt: '2026-09-12 10:00:00', messages: [] },
  { id: 'b', title: '会话B', model: 'm2', createdAt: '2026-09-12 10:01:00', messages: [] },
];

describe('ChatHistoryList', () => {
  it('渲染每个会话标题；activeId 命中的条目高亮', () => {
    const w = mount(ChatHistoryList, { props: { sessions, activeId: 'b' } });
    const items = w.findAll('.cursor-pointer'); // 会话条目是带 cursor-pointer 的 div（非 button）
    expect(items).toHaveLength(2);
    expect(items[1].classes()).toContain('bg-slate-800');
    expect(items[0].classes()).not.toContain('bg-slate-800');
    expect(items[0].text()).toBe('会话A');
  });

  it('条目 title 带上 model 与 createdAt，便于区分同名会话', () => {
    const items = mount(ChatHistoryList, { props: { sessions } }).findAll('.cursor-pointer');
    expect(items[0].attributes('title')).toContain('m1');
    expect(items[0].attributes('title')).toContain('2026-09-12 10:00:00');
  });

  it('空态显示"暂无历史"，非空不显示', () => {
    expect(mount(ChatHistoryList, { props: { sessions: [] } }).text()).toContain('暂无历史');
    expect(mount(ChatHistoryList, { props: { sessions } }).text()).not.toContain('暂无历史');
  });

  it('degraded=true 显示容量降级提示', () => {
    const w = mount(ChatHistoryList, { props: { sessions: [], degraded: true } });
    expect(w.text()).toContain('本地存储已满');
  });

  it('点击条目 emit select(id)；点击"新建"emit new', async () => {
    const w = mount(ChatHistoryList, { props: { sessions } });
    await w.findAll('.cursor-pointer')[0].trigger('click');
    expect(w.emitted('select')?.[0]).toEqual(['a']);
    await w.get('button').trigger('click');
    expect(w.emitted('new')).toHaveLength(1);
  });
});
