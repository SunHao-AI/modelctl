import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import ChatComposer from './ChatComposer.vue';

/**
 * 派发一次真实的 keydown(Enter)，并强制 isComposing —— 中文/日文输入法用回车
 * 确认候选时，keydown 的 key 同样是 'Enter'，但 isComposing 为 true（多数浏览器
 * keyCode 还是 229）。jsdom 不保证 KeyboardEventInit.isComposing 生效，故用
 * Object.defineProperty 在实例上写死，确保测的是组件自身的 IME 守卫而非环境差异。
 */
function pressEnter(el: HTMLTextAreaElement, isComposing: boolean) {
  const ev = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
  Object.defineProperty(ev, 'isComposing', { value: isComposing, configurable: true });
  el.dispatchEvent(ev);
}

describe('ChatComposer 输入法（IME）回车处理', () => {
  it('候选确认回车（isComposing=true）不发送消息', async () => {
    const wrapper = mount(ChatComposer);
    const textarea = wrapper.find('textarea');
    await textarea.setValue('你好');

    pressEnter(textarea.element as HTMLTextAreaElement, true);

    expect(wrapper.emitted('send')).toBeUndefined();
  });

  it('普通回车（isComposing=false）发送一次并携带文本', async () => {
    const wrapper = mount(ChatComposer);
    const textarea = wrapper.find('textarea');
    await textarea.setValue('你好');

    pressEnter(textarea.element as HTMLTextAreaElement, false);

    const emitted = wrapper.emitted('send');
    expect(emitted).toHaveLength(1);
    expect(emitted?.[0]).toEqual(['你好', []]);
  });
});
