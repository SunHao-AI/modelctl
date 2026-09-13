/**
 * ClusterNodesView 单元测试（QA 修复批次 A）：
 *  - QA-A-18：solo 角色下 /cluster/status 稳定 404；页面判定 disabled 后必须
 *    停止 5s 轮询（不再刷控制台 404）。
 *  - QA-B-14：正文不得再渲染重复页题「集群节点」（Header 是唯一页题源）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

const getClusterStatus = vi.fn();
const getClusterNodes = vi.fn();

vi.mock('@/api/cluster', () => ({
  getClusterStatus: () => getClusterStatus(),
  getClusterNodes: () => getClusterNodes(),
}));
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: {} }),
  useRouter: () => ({ push: vi.fn() }),
}));

import ClusterNodesView from './ClusterNodesView.vue';

const notFound = Object.assign(new Error('Request failed with status code 404'), {
  response: { status: 404 },
});

beforeEach(() => {
  getClusterStatus.mockReset();
  getClusterNodes.mockReset();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('QA-A-18 solo 404 后停止轮询', () => {
  it('首轮 404 → 显示引导文案；其后 15s 内不再发第 2 次请求', async () => {
    getClusterStatus.mockRejectedValue(notFound);
    const wrapper = mount(ClusterNodesView);
    await flushPromises();
    expect(wrapper.text()).toContain('当前节点未启用集群角色');
    expect(getClusterStatus).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(15_000);
    await flushPromises();
    expect(getClusterStatus).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });

  it('正文不再渲染重复页题「集群节点」（B-14）', async () => {
    getClusterStatus.mockRejectedValue(notFound);
    const wrapper = mount(ClusterNodesView);
    await flushPromises();
    expect(wrapper.find('h1').exists()).toBe(false);
    wrapper.unmount();
  });

  it('非 404 错误仍保持轮询（不误停）', async () => {
    getClusterStatus.mockRejectedValue(Object.assign(new Error('boom'), { response: { status: 500 } }));
    const wrapper = mount(ClusterNodesView);
    await flushPromises();
    // 逐 tick 推进并 flush：pending 去重语义下，同一请求未落地的 tick 会被吞（预期防重叠）
    vi.advanceTimersByTime(5_000);
    await flushPromises();
    vi.advanceTimersByTime(5_000);
    await flushPromises();
    expect(getClusterStatus).toHaveBeenCalledTimes(3); // 首次 + 2 个 5s tick
    wrapper.unmount();
  });
});
