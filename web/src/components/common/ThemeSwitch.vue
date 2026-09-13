<script setup lang="ts">
import { Monitor, Moon, Sun, type LucideIcon } from 'lucide-vue-next';
import { useThemeStore, type ThemeMode } from '@/stores/theme';

/**
 * 外观切换：苹果三段 Segmented Control。
 * 无 props —— 主题全站唯一真源是 store。
 */
const theme = useThemeStore();

const OPTIONS: { value: ThemeMode; label: string; icon: LucideIcon }[] = [
  { value: 'light', label: '浅色', icon: Sun },
  { value: 'dark', label: '深色', icon: Moon },
  { value: 'auto', label: '跟随系统', icon: Monitor },
];
</script>

<template>
  <div
    role="radiogroup"
    aria-label="外观"
    class="inline-flex gap-px rounded-ctl p-0.5"
    style="background: var(--seg-bg); box-shadow: inset 0 0 0 0.5px var(--separator)"
  >
    <button
      v-for="o in OPTIONS"
      :key="o.value"
      type="button"
      role="radio"
      :aria-checked="theme.mode === o.value"
      :class="[
        'inline-flex items-center gap-1.5 rounded-ctl px-3 py-1.5 text-xs transition-all duration-200',
        theme.mode === o.value ? 'font-semibold text-label' : 'font-medium text-label2 hover:text-label',
      ]"
      :style="
        theme.mode === o.value
          ? 'background: var(--seg-knob); box-shadow: 0 1px 3px rgba(0,0,0,.22), 0 0 0 .5px var(--separator)'
          : ''
      "
      @click="theme.setMode(o.value)"
    >
      <component :is="o.icon" :size="13" :stroke-width="1.9" />
      {{ o.label }}
    </button>
  </div>
</template>
