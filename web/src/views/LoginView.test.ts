/**
 * LoginView 单元测试（QA 修复批次 A）：
 *  - QA-A-01：后端 401 错误体是 { error: { code, message } }（无 detail），
 *    页面必须透出后端中文 message；且 e2e 契约选择器 p.text-red-400 不变。
 *  - QA-A-09：页脚版本来自 GET /health（免鉴权），拉不到只显示 modelctl。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const login = vi.fn();
const health = vi.fn();

vi.mock('@/api/auth', () => ({
  login: (k: string) => login(k),
  logout: () => Promise.resolve({ ok: true }),
}));
vi.mock('@/api/services', () => ({
  health: () => health(),
}));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ replace: vi.fn() }),
}));

import LoginView from './LoginView.vue';

async function mountWithBadKey(rejectWith: unknown) {
  login.mockRejectedValue(rejectWith);
  health.mockResolvedValue({ ok: true, version: '0.3.0', uptime_s: 1, default_model: '', gateway_port: 8000 });
  const wrapper = mount(LoginView);
  await flushPromises();
  await wrapper.find('#api-key').setValue('wrong-key-123');
  await wrapper.find('form').trigger('submit');
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  login.mockReset();
  health.mockReset();
});

describe('QA-A-01 登录错误提取', () => {
  it('后端 { error: { message } } 形状：透出中文「认证失败」且保留 p.text-red-400 契约', async () => {
    const wrapper = await mountWithBadKey({
      response: { status: 401, data: { error: { code: 'auth', message: '认证失败' } } },
      message: 'Request failed with status code 401',
    });
    const p = wrapper.find('p.text-red-400');
    expect(p.exists()).toBe(true);
    expect(p.text()).toBe('认证失败');
  });

  it('兼容 { detail: { message } } 形状（其它端点契约）', async () => {
    const wrapper = await mountWithBadKey({
      response: { status: 401, data: { detail: { code: 'auth', message: '密钥无效' } } },
      message: 'Request failed with status code 401',
    });
    expect(wrapper.find('p.text-red-400').text()).toBe('密钥无效');
  });

  it('无响应体（网络错误）：回退 axios message，再兜底固定文案', async () => {
    const wrapper = await mountWithBadKey({ message: 'Network Error' });
    expect(wrapper.find('p.text-red-400').text()).toBe('Network Error');

    const wrapper2 = await mountWithBadKey(new Error('boom'));
    expect(wrapper2.find('p.text-red-400').text()).toBe('boom');
  });
});

describe('QA-A-09 页脚版本', () => {
  it('health 可达：页脚显示后端真实版本', async () => {
    login.mockResolvedValue({ ok: true });
    health.mockResolvedValue({ ok: true, version: '0.3.0', uptime_s: 1, default_model: '', gateway_port: 8000 });
    const wrapper = mount(LoginView);
    await flushPromises();
    expect(wrapper.text()).toContain('modelctl v0.3.0');
    expect(wrapper.text()).not.toContain('v0.1.0');
  });

  it('health 失败：页脚只显示 modelctl，不带版本号', async () => {
    login.mockResolvedValue({ ok: true });
    health.mockRejectedValue(new Error('offline'));
    const wrapper = mount(LoginView);
    await flushPromises();
    const footer = wrapper.findAll('p').at(-1)!;
    expect(footer.text()).toBe('modelctl');
  });
});
