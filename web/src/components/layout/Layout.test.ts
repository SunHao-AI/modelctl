/**
 * Layout 单元测试（QA 修复批次 A）：
 *  - QA-A-17：懒加载路由切换期间必须出现全局顶部进度条；
 *    beforeEach 起、afterEach 落（淡出 ≈400ms 后从 DOM 移除）。
 *  - 桌面侧栏 e2e 契约：hidden md:flex 类保持在场。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { createMemoryHistory, createRouter, type Router } from 'vue-router';
import { h, type Component } from 'vue';

vi.mock('@/stores/tasks', () => ({
  useTasksStore: () => ({ bootstrap: () => Promise.resolve(), reset: () => undefined }),
}));

import Layout from './Layout.vue';

const Stub = { render: () => h('div') };
const View = { render: () => h('span', 'view') };

let router: Router;
let resolveSlow: (c: Component) => void;

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: Layout, children: [{ path: '', component: View }] },
      { path: '/slow', component: () => new Promise<Component>((r) => (resolveSlow = r)) },
    ],
  });
}

function mountLayout() {
  return mount(Layout, { global: { plugins: [router], stubs: { Sidebar: Stub, Header: Stub } } });
}

beforeEach(async () => {
  setActivePinia(createPinia());
  vi.useFakeTimers();
  router = makeRouter();
  router.push('/');
  await router.isReady();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('QA-A-17 路由级进度条', () => {
  it('chunk 拉取期间进度条在场，导航完成淡出后移除', async () => {
    const wrapper = mountLayout();
    expect(wrapper.find('[aria-hidden="true"]').exists()).toBe(false);

    // 触发懒加载导航：chunk promise 未回前进度条已亮起
    const nav = router.push('/slow');
    await flushPromises();
    const bar = wrapper.find('[aria-hidden="true"]');
    expect(bar.exists()).toBe(true);
    expect(bar.attributes('class')).toContain('pointer-events-none');

    // chunk 到达 → 导航完成 → 进度条先冲 100% 再淡出
    resolveSlow(View);
    await nav;
    await flushPromises();
    expect(wrapper.find('[aria-hidden="true"]').exists()).toBe(true);

    vi.advanceTimersByTime(500);
    await flushPromises();
    expect(wrapper.find('[aria-hidden="true"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it('桌面侧栏 hidden md:flex e2e 契约保持', () => {
    const wrapper = mountLayout();
    const sidebarStub = wrapper.findComponent(Stub);
    expect(sidebarStub.attributes('class') ?? wrapper.html()).toContain('md:flex');
    wrapper.unmount();
  });
});
