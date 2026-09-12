import { describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { defineComponent, h, nextTick, reactive } from 'vue';
import type { ChatMsg } from '@/stores/chat';

// 用 hoisted 保证 mock 工厂在 import ChatMessage 之前就能拿到同一个 spy
const { renderMarkdown } = vi.hoisted(() => ({
  renderMarkdown: vi.fn((t: string) => `<p>${t}</p>`),
}));
vi.mock('@/utils/markdown', () => ({ renderMarkdown }));

import ChatMessage from './ChatMessage.vue';

function mkMsg(id: string, content: string): ChatMsg {
  return { id, role: 'assistant', content, reasoning: '', images: [] };
}

/**
 * design.md:162 —— 长回复不应因"只有最后一条流式消息在变"而被反复重渲染。
 * 这里模拟两条**已完成**消息，仅改动第一条的 content，断言 renderMarkdown
 * 只多跑 1 次（而不是把两条都重渲染 → 多跑 2 次）。
 */
describe('ChatMessage 渲染缓存（design.md:162）', () => {
  it('仅变更某条消息时不重渲染其它已完成消息（按 msg.id 实例隔离）', async () => {
    const msgs = reactive([mkMsg('m1', 'first'), mkMsg('m2', 'second')]);
    const Wrapper = defineComponent({
      setup() {
        return () => h('div', msgs.map((m) => h(ChatMessage, { key: m.id, msg: m })));
      },
    });

    const wrapper = mount(Wrapper);
    expect(renderMarkdown).toHaveBeenCalledTimes(2);

    msgs[0].content = 'first-changed';
    await nextTick();

    // 只有被改动的那条重新渲染 → 调用次数 +1（若两条都重渲染会是 +2）
    expect(renderMarkdown).toHaveBeenCalledTimes(3);
    expect(renderMarkdown).toHaveBeenLastCalledWith('first-changed');
    // 未变更的消息内容原样保留
    expect(wrapper.text()).toContain('second');
  });
});
