/**
 * ClusterGoalsView 单元测试（QA 修复批次 A）：
 *  - QA-A-18：solo 角色下 cluster 端点稳定 404；判定 disabled 后必须停止 5s 轮询。
 *  - QA-B-14：正文不得再渲染重复页题「集群目标」。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

const listClusterGoals = vi.fn();
const getClusterNodes = vi.fn();
const listClusterProfiles = vi.fn();

vi.mock('@/api/cluster', () => ({
  createClusterGoal: vi.fn(),
  deleteClusterGoal: vi.fn(),
  forceNodeSync: vi.fn(),
  retryGoal: vi.fn(),
  updateClusterGoal: vi.fn(),
  getClusterNodes: () => getClusterNodes(),
  listClusterGoals: () => listClusterGoals(),
  listClusterProfiles: () => listClusterProfiles(),
}));
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: {}, query: {} }),
  useRouter: () => ({ push: vi.fn() }),
}));

import ClusterGoalsView from './ClusterGoalsView.vue';

const notFound = Object.assign(new Error('Request failed with status code 404'), {
  response: { status: 404 },
});

beforeEach(() => {
  listClusterGoals.mockReset();
  getClusterNodes.mockReset();
  listClusterProfiles.mockReset();
  listClusterProfiles.mockResolvedValue({ profiles: [] });
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('QA-A-18 solo 404 后停止轮询（goals 页）', () => {
  it('首轮 404 → 引导文案 + 15s 内不再发第 2 次 goals/nodes 轮询请求', async () => {
    listClusterGoals.mockRejectedValue(notFound);
    getClusterNodes.mockRejectedValue(notFound);
    const wrapper = mount(ClusterGoalsView);
    await flushPromises();
    expect(wrapper.text()).toContain('当前节点未启用集群角色');
    expect(listClusterGoals).toHaveBeenCalledTimes(1);
    expect(getClusterNodes).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(15_000);
    await flushPromises();
    expect(listClusterGoals).toHaveBeenCalledTimes(1);
    expect(getClusterNodes).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });

  it('正文不再渲染重复页题「集群目标」（B-14）', async () => {
    listClusterGoals.mockRejectedValue(notFound);
    getClusterNodes.mockRejectedValue(notFound);
    const wrapper = mount(ClusterGoalsView);
    await flushPromises();
    expect(wrapper.find('h1').exists()).toBe(false);
    wrapper.unmount();
  });
});
