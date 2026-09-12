/**
 * stores/auth.ts 单元测试：管理面 token 与账号面 JWT 是**两个独立信任域**，
 * 持久化 key 不重叠、clear 互不影响——这条不变式如果被改坏，等于"退出管理面
 * 登录会顺带踢掉账号会话"（或更糟：两者共用一个 key 导致越权展示）。
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useAuthStore } from './auth';

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
});

describe('管理面 token（API_KEY）', () => {
  it('初始未登录：localStorage 为空时 token 空串、isLoggedIn false', () => {
    const auth = useAuthStore();
    expect(auth.token).toBe('');
    expect(auth.isLoggedIn).toBe(false);
  });

  it('persistToken 同时写内存与 localStorage，且 apiKey 与 token 等价', () => {
    const auth = useAuthStore();
    auth.persistToken('sk-mctl-abc');
    expect(auth.isLoggedIn).toBe(true);
    expect(auth.apiKey).toBe('sk-mctl-abc');
    expect(localStorage.getItem('modelctl_token')).toBe('sk-mctl-abc');
  });

  it('persistToken("") 等价 logout：两处都清', () => {
    const auth = useAuthStore();
    auth.persistToken('sk-mctl-abc');
    auth.persistToken('');
    expect(auth.isLoggedIn).toBe(false);
    expect(localStorage.getItem('modelctl_token')).toBeNull();
  });

  it('persistToken(null) 被兜底为空串（后端异常返回 null 不炸）', () => {
    const auth = useAuthStore();
    auth.persistToken(null as unknown as string);
    expect(auth.isLoggedIn).toBe(false);
  });

  it('clear() 只清管理面，不动账号面会话', () => {
    const auth = useAuthStore();
    auth.persistToken('sk-mctl-abc');
    auth.setAccountSession('jwt-1', { id: 1, username: 'u', display_name: 'U', is_admin: false });
    auth.clear();
    expect(auth.isLoggedIn).toBe(false);
    expect(auth.isAccountLoggedIn).toBe(true);
  });

  it('store 新建时从 localStorage hydrate（刷新页面保持登录）', () => {
    localStorage.setItem('modelctl_token', 'persisted-key');
    setActivePinia(createPinia()); // 重建 pinia 模拟页面刷新
    const auth = useAuthStore();
    expect(auth.isLoggedIn).toBe(true);
    expect(auth.token).toBe('persisted-key');
  });

  it('纯空白 token 不算登录（trim 判据）', () => {
    const auth = useAuthStore();
    auth.persistToken('   ');
    expect(auth.isLoggedIn).toBe(false);
  });
});

describe('账号面 JWT（与管理面完全独立）', () => {
  it('setAccountSession 持久化 JWT + profile，isAccountLoggedIn 变 true', () => {
    const auth = useAuthStore();
    auth.setAccountSession('jwt-1', { id: 2, username: 'bob', display_name: 'Bob', is_admin: true });
    expect(auth.isAccountLoggedIn).toBe(true);
    expect(localStorage.getItem('modelctl_account_token')).toBe('jwt-1');
    expect(JSON.parse(localStorage.getItem('modelctl_account_profile') || '{}').username).toBe('bob');
  });

  it('账号会话不写管理面 key（信任域隔离）', () => {
    const auth = useAuthStore();
    auth.setAccountSession('jwt-1', { id: 2, username: 'bob', display_name: '', is_admin: false });
    expect(localStorage.getItem('modelctl_token')).toBeNull();
    expect(auth.isLoggedIn).toBe(false);
  });

  it('clearAccountSession 清两处且不碰管理面 token', () => {
    const auth = useAuthStore();
    auth.persistToken('sk-mctl-abc');
    auth.setAccountSession('jwt-1', { id: 2, username: 'bob', display_name: '', is_admin: false });
    auth.clearAccountSession();
    expect(auth.isAccountLoggedIn).toBe(false);
    expect(auth.accountProfile).toBeNull();
    expect(localStorage.getItem('modelctl_account_token')).toBeNull();
    expect(localStorage.getItem('modelctl_account_profile')).toBeNull();
    expect(auth.isLoggedIn).toBe(true);
  });

  it('损坏的 profile JSON 降级为 null 而非抛错（hydrated 容错）', () => {
    localStorage.setItem('modelctl_account_profile', '{not-json');
    setActivePinia(createPinia());
    const auth = useAuthStore();
    expect(auth.accountProfile).toBeNull();
  });
});
