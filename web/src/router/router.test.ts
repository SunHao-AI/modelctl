/**
 * 路由守卫单元测试：前端鉴权分两层——
 *   Layer 0：meta.public（/login、/account/login）无条件放行
 *   Layer 1：meta.accountAuth 需账号 JWT，缺失跳 /account/login（带 redirect）
 *   Layer 2：Layout 内其余路由需管理面 API_KEY，缺失跳 /login（带 redirect）
 * 兜底路由把未知路径重定向 /login（fail-closed，不给未鉴权内容留入口）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { routes } from './index';

// 视图组件是懒加载的，其 import 链会触达 axios 客户端；守卫测试只关心跳转决策，
// 统一打桩 client 避免副作用（拦截器会真的调 router/localStorage）。
vi.mock('@/api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), interceptors: {} },
  dataOf: (p: Promise<{ data: unknown }>) => p.then((r) => r.data),
  downloadBlob: vi.fn(),
}));

async function nav(path: string) {
  const { default: router } = await import('./index');
  await router.push(path).catch(() => undefined);
  await router.isReady();
  return router.currentRoute.value;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  // 每个用例重新配置 history 起点，避免上一用例的 currentRoute 串场
  window.history.replaceState({}, '', '/');
  vi.resetModules();
});

describe('Layer 0：public 路由放行', () => {
  it('未登录可直达 /login', async () => {
    expect((await nav('/login')).path).toBe('/login');
  });

  it('未登录可直达 /account/login', async () => {
    expect((await nav('/account/login')).path).toBe('/account/login');
  });
});

describe('Layer 2：管理面鉴权', () => {
  it('未登录访问 /dashboard 弹回 /login 且带 redirect', async () => {
    const route = await nav('/dashboard');
    expect(route.path).toBe('/login');
    expect(route.query.redirect).toBe('/dashboard');
  });

  it('未登录访问带参路径 /models/foo 时 redirect 保留完整 fullPath', async () => {
    const route = await nav('/models/foo?page=2');
    expect(route.path).toBe('/login');
    expect(route.query.redirect).toBe('/models/foo?page=2');
  });

  it('已登录可进 /dashboard', async () => {
    localStorage.setItem('modelctl_token', 'sk-mctl-abc');
    expect((await nav('/dashboard')).path).toBe('/dashboard');
  });

  it('未知路径兜底重定向 /login（fail-closed）', async () => {
    const route = await nav('/no/such/page');
    expect(route.path).toBe('/login');
  });
});

describe('Layer 1：账号面 JWT', () => {
  it('有 API_KEY 但无 JWT 访问 /account/self → 跳 /account/login', async () => {
    localStorage.setItem('modelctl_token', 'sk-mctl-abc');
    const route = await nav('/account/self');
    expect(route.path).toBe('/account/login');
    expect(route.query.redirect).toBe('/account/self');
  });

  it('API_KEY + JWT 齐备才放行 /account/self', async () => {
    localStorage.setItem('modelctl_token', 'sk-mctl-abc');
    localStorage.setItem('modelctl_account_token', 'jwt-1');
    expect((await nav('/account/self')).path).toBe('/account/self');
  });

  it('只有 JWT 没有 API_KEY 也进不去（双层缺一不可）', async () => {
    localStorage.setItem('modelctl_account_token', 'jwt-1');
    const route = await nav('/account/self');
    // accountAuth 检查在前：缺 JWT 场景跳账号登录；此处 JWT 有而 API_KEY 无，
    // 由 Layer 2 弹回管理面登录
    expect(route.path).toBe('/login');
  });
});

it('/chat 挂在 Layout 下且 name=chat', () => {
  const layout = routes.find((r) => r.path === '/');
  const chat = layout?.children?.find((c) => c.path === 'chat');
  expect(chat).toBeTruthy();
  expect(chat?.name).toBe('chat');
  expect(chat?.meta?.title).toBe('AI 对话');
});
