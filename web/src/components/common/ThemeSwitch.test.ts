/**
 * ThemeSwitch 本身无逻辑，但「三态 + 点击驱动 store」是主题功能唯一可在前端
 * 验证的部分：若点「浅色」没调 setMode('light')，用户切换就没有任何效果。
 */
import { beforeEach, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const setMode = vi.fn();
vi.mock('@/stores/theme', () => ({
  useThemeStore: () => ({ mode: 'auto', resolved: 'dark', setMode }),
}));

import ThemeSwitch from './ThemeSwitch.vue';

beforeEach(() => {
  setActivePinia(createPinia());
  setMode.mockReset();
});

it('容器是 radiogroup', () => {
  expect(mount(ThemeSwitch).find('[role="radiogroup"]').exists()).toBe(true);
});

it('渲染三态 radio，顺序为 浅色 / 深色 / 跟随系统', () => {
  const radios = mount(ThemeSwitch).findAll('[role="radio"]');
  expect(radios).toHaveLength(3);
  expect(radios.map((r) => r.text())).toEqual(['浅色', '深色', '跟随系统']);
});

it('点击「浅色」调 setMode("light")', async () => {
  await mount(ThemeSwitch).findAll('[role="radio"]')[0].trigger('click');
  expect(setMode).toHaveBeenCalledWith('light');
});

it('点击「跟随系统」调 setMode("auto")', async () => {
  await mount(ThemeSwitch).findAll('[role="radio"]')[2].trigger('click');
  expect(setMode).toHaveBeenCalledWith('auto');
});

it('当前 mode 对应的 radio aria-checked=true', () => {
  const radios = mount(ThemeSwitch).findAll('[role="radio"]');
  expect(radios[2].attributes('aria-checked')).toBe('true');
  expect(radios[0].attributes('aria-checked')).toBe('false');
});
