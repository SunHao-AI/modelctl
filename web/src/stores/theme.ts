import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import { usePreferredDark } from '@vueuse/core';

/** 仅存**手动**选择；缺省（无记录）即 'auto' = 跟随系统 */
export const THEME_STORAGE_KEY = 'modelctl_theme';

export type ThemeMode = 'auto' | 'light' | 'dark';
export type ResolvedTheme = 'light' | 'dark';

const MODES: readonly ThemeMode[] = ['auto', 'light', 'dark'];

/** 纯函数：三态真值表，单测直接覆盖，不依赖 DOM */
export function resolveTheme(mode: ThemeMode, systemPrefersDark: boolean): ResolvedTheme {
  if (mode === 'auto') return systemPrefersDark ? 'dark' : 'light';
  return mode;
}

function readStored(): ThemeMode {
  const raw = localStorage.getItem(THEME_STORAGE_KEY);
  return MODES.includes(raw as ThemeMode) ? (raw as ThemeMode) : 'auto';
}

/**
 * 主题 store。
 * 必须用 usePreferredDark()：裸 matchMedia(...).matches 非响应式，
 * computed 里读它不会在系统换肤时重算（设计文档 §4.1）。
 */
export const useThemeStore = defineStore('theme', () => {
  const mode = ref<ThemeMode>(readStored());
  const systemDark = usePreferredDark();

  const resolved = computed<ResolvedTheme>(() => resolveTheme(mode.value, systemDark.value));

  function apply() {
    document.documentElement.dataset.theme = resolved.value;
  }

  function setMode(next: ThemeMode) {
    if (!MODES.includes(next)) return;
    mode.value = next;
    if (next === 'auto') localStorage.removeItem(THEME_STORAGE_KEY);
    else localStorage.setItem(THEME_STORAGE_KEY, next);
    apply();
  }

  return { mode, resolved, systemDark, setMode, apply };
});
