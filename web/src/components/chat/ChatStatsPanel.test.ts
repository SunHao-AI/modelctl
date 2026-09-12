import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import ChatStatsPanel from './ChatStatsPanel.vue';
import type { ChatMsg } from '@/stores/chat';

const base: ChatMsg = { id: 'x', role: 'assistant', content: '', reasoning: '', images: [] };

describe('ChatStatsPanel', () => {
  it('null 消息显示"发送后显示"', () => {
    expect(mount(ChatStatsPanel, { props: { msg: null } }).text()).toContain('发送后显示');
  });

  it('totalMs=0 时 tok/s 显示"-"而非 Infinity/NaN', () => {
    const msg: ChatMsg = { ...base, usage: { prompt_tokens: 1, completion_tokens: 5 }, totalMs: 0 };
    const t = mount(ChatStatsPanel, { props: { msg } }).text();
    expect(t).not.toMatch(/Infinity|NaN/);
    expect(t).toContain('tok/s');
  });

  it('有 usage 与 totalMs 时正确算 tok/s', () => {
    const msg: ChatMsg = { ...base, usage: { prompt_tokens: 10, completion_tokens: 20 }, totalMs: 4000 };
    // 20 / (4000/1000) = 5.0
    expect(mount(ChatStatsPanel, { props: { msg } }).text()).toContain('5.0');
  });

  it('无 usage 显示"无 usage"', () => {
    expect(mount(ChatStatsPanel, { props: { msg: { ...base } } }).text()).toContain('无 usage');
  });

  it('缺 routedTo/routeReason 时回落到"-"与"直连命中"', () => {
    const t = mount(ChatStatsPanel, { props: { msg: { ...base } } }).text();
    expect(t).toContain('直连命中');
  });

  it('有 routedTo/routeReason 时原样展示落点', () => {
    const msg: ChatMsg = { ...base, routedTo: 'a', routeReason: 'group_route' };
    const t = mount(ChatStatsPanel, { props: { msg } }).text();
    expect(t).toContain('a');
    expect(t).toContain('group_route');
  });
});
