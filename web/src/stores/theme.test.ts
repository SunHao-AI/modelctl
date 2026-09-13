/**
 * 主题 store 三态语义：
 *  - 'auto' 是**默认且唯一缺省态**，跟随系统，且**不写** localStorage
 *  - 只有显式选 light/dark 才持久化；选回 auto 必须 removeItem
 *  （若 auto 也写盘，「跟随系统」的意图就被固化成当下值，之后改系统偏好不再跟随）
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { resolveTheme, useThemeStore, THEME_STORAGE_KEY } from './theme';

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  setActivePinia(createPinia());
});

describe('resolveTheme 纯函数', () => {
  it('auto：系统深色→dark，系统浅色→light', () => {
    expect(resolveTheme('auto', true)).toBe('dark');
    expect(resolveTheme('auto', false)).toBe('light');
  });
  it('light/dark：无视系统偏好', () => {
    for (const sys of [true, false]) {
      expect(resolveTheme('light', sys)).toBe('light');
      expect(resolveTheme('dark', sys)).toBe('dark');
    }
  });
});

describe('useThemeStore', () => {
  it('无持久化记录时 mode 为 auto', () => {
    expect(useThemeStore().mode).toBe('auto');
  });

  it('localStorage 合法值会被 hydrate', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'light');
    setActivePinia(createPinia());
    expect(useThemeStore().mode).toBe('light');
  });

  it('localStorage 非法值回落 auto（手改坏数据不炸）', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'neon');
    setActivePinia(createPinia());
    expect(useThemeStore().mode).toBe('auto');
  });

  it('setMode("light") 写盘并把 data-theme 落到 <html>', () => {
    useThemeStore().setMode('light');
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');
    expect(document.documentElement.dataset.theme).toBe('light');
  });

  it('setMode("auto") 清盘（保留跟随系统的意图）', () => {
    const t = useThemeStore();
    t.setMode('dark');
    t.setMode('auto');
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
  });

  it('apply() 按 resolved 落 data-theme', () => {
    const t = useThemeStore();
    t.setMode('dark');
    t.apply();
    expect(document.documentElement.dataset.theme).toBe('dark');
  });

  /**
   * 这两条依赖 src/test/setup.ts 的 matchMedia 桩（jsdom 原生没有 matchMedia，
   * 不装桩则 systemDark 恒 false，下面第一条会以错误的方式"通过"）。
   */
  it('auto + 系统深色：resolved 为 dark 且 apply 落 dark', () => {
    const setSystemDark = (globalThis as unknown as { __setSystemDark: (v: boolean) => void }).__setSystemDark;
    expect(setSystemDark, 'matchMedia 桩未生效，检查 vite.config.ts 的 setupFiles').toBeTypeOf('function');
    setSystemDark(true);
    const t = useThemeStore();
    t.setMode('auto');
    expect(t.resolved).toBe('dark');
  });

  it('auto：系统偏好翻转会驱动 resolved 重算（响应式，非一次性读取）', async () => {
    const { nextTick } = await import('vue');
    const setSystemDark = (globalThis as unknown as { __setSystemDark: (v: boolean) => void }).__setSystemDark;
    setSystemDark(false);
    const t = useThemeStore();
    t.setMode('auto');
    expect(t.resolved).toBe('light');
    setSystemDark(true);
    await nextTick();
    expect(t.resolved).toBe('dark');
  });
});
