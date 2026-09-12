/**
 * api/client.ts 拦截器单元测试：
 *   请求拦截器 —— 有 token 必须注入 `Authorization: Bearer <token>`（后端
 *     require_auth 只认这个头）；无 token 不得注入空头。
 *   响应拦截器 —— 401 是唯一触发「清 token + 跳 /login」的状态码；其它错误
 *     （403/500/网络）原样 reject，绝不出登录态（后端瞬时故障不该踢人下线）。
 * 走自定义 axios adapter，真实执行拦截器链，不发网络请求。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios';
import { createPinia, setActivePinia } from 'pinia';
import client, { dataOf } from './client';
import { useAuthStore } from '@/stores/auth';

// axios 的 adapter 收到的是拦截器链处理后的 InternalAxiosRequestConfig
// （headers 必为 AxiosHeaders），用 AxiosRequestConfig 会让 vue-tsc 报 TS2322/TS2345。
type Adapter = (config: InternalAxiosRequestConfig) => Promise<AxiosResponse>;

const originalAdapter = client.defaults.adapter;
let lastConfig: InternalAxiosRequestConfig | undefined;

function ok(data: unknown, headers: Record<string, string> = {}): Adapter {
  return async (config) => {
    lastConfig = config;
    return { data, status: 200, statusText: 'OK', headers, config };
  };
}

function fail(status: number): Adapter {
  return async (config) => {
    lastConfig = config;
    const response = { data: { detail: { code: 'auth', message: 'x' } }, status, statusText: '', headers: {}, config };
    throw new AxiosError('http error', 'ERR_BAD_REQUEST', config, {}, response);
  };
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  window.history.replaceState({}, '', '/dashboard');
  lastConfig = undefined;
});

afterEach(() => {
  client.defaults.adapter = originalAdapter;
});

describe('请求拦截器：Bearer 注入', () => {
  it('已登录：Authorization 头为 Bearer <token>', async () => {
    const auth = useAuthStore();
    auth.persistToken('sk-mctl-test');
    client.defaults.adapter = ok({ ok: true });
    await client.get('/probe');
    const headers = lastConfig?.headers as { get?(k: string): unknown; Authorization?: string };
    const value = typeof headers?.get === 'function' ? headers.get('Authorization') : headers?.Authorization;
    expect(value).toBe('Bearer sk-mctl-test');
  });

  it('未登录：不注入 Authorization', async () => {
    client.defaults.adapter = ok({ ok: true });
    await client.get('/health');
    const headers = lastConfig?.headers as { get?(k: string): unknown; Authorization?: string };
    const value = typeof headers?.get === 'function' ? headers.get('Authorization') : headers?.Authorization;
    expect(value ?? undefined).toBeFalsy();
  });
});

describe('响应拦截器：401 处理', () => {
  it('401 清除本地 token 并跳 /login（带 redirect）', async () => {
    const auth = useAuthStore();
    auth.persistToken('sk-mctl-test');
    client.defaults.adapter = fail(401);
    await expect(client.get('/models')).rejects.toBeInstanceOf(AxiosError);
    expect(auth.isLoggedIn).toBe(false);
    // 路由已跳登录页且带回跳参数
    const { default: router } = await import('@/router');
    await router.isReady();
    expect(router.currentRoute.value.path).toBe('/login');
    expect(router.currentRoute.value.query.redirect).toBeDefined();
  });

  it('403 不清 token（越权≠登录失效）', async () => {
    const auth = useAuthStore();
    auth.persistToken('sk-mctl-test');
    client.defaults.adapter = fail(403);
    await expect(client.get('/admin-only')).rejects.toBeInstanceOf(AxiosError);
    expect(auth.isLoggedIn).toBe(true);
  });

  it('500 原样 reject 且保留登录态', async () => {
    const auth = useAuthStore();
    auth.persistToken('sk-mctl-test');
    client.defaults.adapter = fail(500);
    await expect(client.get('/probe')).rejects.toBeInstanceOf(AxiosError);
    expect(auth.isLoggedIn).toBe(true);
  });
});

describe('dataOf', () => {
  it('解包 axios 响应体的 data 字段', async () => {
    expect(await dataOf(Promise.resolve({ data: 42 }))).toBe(42);
  });
});
