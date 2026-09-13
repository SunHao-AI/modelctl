/**
 * EnvsView 单元测试（QA 修复批次 A）：
 *  - QA-A-02：诊断未返回前不得把平台硬编码为 'linux'（Windows 主机曾被误判并
 *    隐藏「一键安装」入口）；结果到达前渲染「正在检测平台…」占位。
 *  - QA-A-07：诊断失败必须在页面可见（红字），而不是只留 console.warn。
 *  - QA-A-16：剪贴板写入失败必须 toast 可见反馈。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

const dockerDiagnose = vi.fn();
const envTargets = vi.fn();

// 工厂必须覆盖 @/api/envs 全部具名导出（DockerInstallPanel 也从此模块引 4 个）
vi.mock('@/api/envs', () => ({
  dockerDiagnose: () => dockerDiagnose(),
  envRemove: vi.fn(),
  envSetup: vi.fn(),
  envTargets: () => envTargets(),
  fetchDockerInstallStatus: vi.fn(() => Promise.resolve({ phase: 'idle', steps: [] })),
  startDockerInstall: vi.fn(),
  systemActionWindows: vi.fn(),
  withSseAuthKey: (p: string) => p,
}));
// TaskButton 经 @/stores/tasks → @/api/client → @/router 会拉起真实 router；整店 mock 截断
vi.mock('@/stores/tasks', () => ({
  useTasksStore: () => ({ activityFor: () => undefined, track: () => undefined }),
}));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));
const toastError = vi.fn();
vi.mock('@/utils/toast', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: (m: string) => toastError(m) },
}));

import EnvsView from './EnvsView.vue';

beforeEach(() => {
  dockerDiagnose.mockReset();
  envTargets.mockReset();
  toastError.mockReset();
  envTargets.mockResolvedValue({ targets: [], unmanaged: [], docker_env: null, docker_bypass: [] });
});

describe('QA-A-02 平台检测不再假装 Linux', () => {
  it('诊断未返回：渲染「正在检测平台…」占位，不渲染 Linux 降级文案', async () => {
    let resolveDiag: (v: unknown) => void = () => undefined;
    dockerDiagnose.mockReturnValue(new Promise((r) => (resolveDiag = r)));
    const wrapper = mount(EnvsView);
    await flushPromises();

    expect(wrapper.text()).toContain('正在检测平台…');
    expect(wrapper.text()).not.toContain('当前 WebUI 主机是');

    // 诊断返回 windows → 面板走完整分支（含「一键安装」按钮），而非 Linux 降级 alert
    resolveDiag({ platform: 'windows', checks: [], instructions: '' });
    await flushPromises();
    expect(wrapper.text()).toContain('一键安装');
    expect(wrapper.text()).not.toContain('正在检测平台…');
    wrapper.unmount();
  });

  it('进入页面即异步拉一次诊断（不再要求用户先点「完整诊断」）', async () => {
    dockerDiagnose.mockResolvedValue({ platform: 'windows', checks: [], instructions: '' });
    const wrapper = mount(EnvsView);
    await flushPromises();
    expect(dockerDiagnose).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });
});

describe('QA-A-07 诊断失败可见化', () => {
  it('诊断超时：页面渲染红色错误文案且「完整诊断」按钮仍可重试', async () => {
    dockerDiagnose.mockRejectedValue(Object.assign(new Error('timeout of 30000ms exceeded'), { code: 'ECONNABORTED' }));
    const wrapper = mount(EnvsView);
    await flushPromises();

    expect(wrapper.text()).toContain('timeout of 30000ms exceeded');
    const btn = wrapper.findAll('button').find((b) => b.text().includes('完整诊断'))!;
    expect(btn.exists()).toBe(true);
    expect(btn.attributes('disabled')).toBeUndefined();
    wrapper.unmount();
  });
});

describe('QA-A-16 复制失败可见反馈', () => {
  it('clipboard 写入被拒 → toast.error 可见反馈', async () => {
    dockerDiagnose.mockResolvedValue({ platform: 'linux', checks: [], instructions: 'echo hi' });
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('NotAllowedError')) },
      configurable: true,
    });
    const wrapper = mount(EnvsView);
    await flushPromises();

    // 展开诊断面板，点「复制脚本」
    await wrapper.findAll('button').find((b) => b.text().includes('完整诊断'))!.trigger('click');
    await flushPromises();
    await wrapper.findAll('button').find((b) => b.text().includes('复制脚本'))!.trigger('click');
    await flushPromises();

    expect(toastError).toHaveBeenCalledWith(expect.stringContaining('复制失败'));
    wrapper.unmount();
  });
});
