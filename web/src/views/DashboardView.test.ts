/**
 * DashboardView 单元测试（QA 修复批次 A）：
 *  - QA-A-12：overview 的 uptime_s 恒 null（后端遗留），系统卡「可用时间」
 *    必须改由免鉴权的 GET /health 的 uptime_s 覆盖。
 */
import { beforeEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

const overview = vi.fn();
const health = vi.fn();

vi.mock('@/api/services', () => ({
  overview: () => overview(),
  health: () => health(),
}));

import DashboardView from './DashboardView.vue';

const OVERVIEW = {
  version: '0.3.0',
  uptime_s: null,
  default_model: 'qwen3.8',
  gateway_port: 8000,
  model_count: 52,
  models: [],
  hardware: { gpu_count: 1, total_vram_gb: 6, gpu_name: 'GTX 1060', engine_binaries: [] },
  services: {
    gateway: { state: 'running', port: 8000 },
    stats: { state: 'running', port: 9090 },
  },
  probed_at: '2026-09-13T22:16:18+08:00',
};

beforeEach(() => {
  overview.mockReset();
  health.mockReset();
  overview.mockResolvedValue(OVERVIEW);
});

it('overview.uptime_s=null 时用 /health 的 uptime_s 渲染「可用时间」（QA-A-12）', async () => {
  health.mockResolvedValue({ ok: true, version: '0.3.0', uptime_s: 4323.9, default_model: '', gateway_port: 8000 });
  const wrapper = mount(DashboardView);
  await flushPromises();
  expect(health).toHaveBeenCalled();
  expect(wrapper.text()).toContain('4324s');
  expect(wrapper.text()).not.toContain('可用时间 未知');
  wrapper.unmount();
});

it('/health 失败时保留 overview 兜底（仍为 null → 「未知」，不抛错）', async () => {
  health.mockRejectedValue(new Error('offline'));
  const wrapper = mount(DashboardView);
  await flushPromises();
  expect(wrapper.text()).toContain('未知');
  wrapper.unmount();
});
