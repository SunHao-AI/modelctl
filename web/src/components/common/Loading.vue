<script setup lang="ts">
/** 全站唯一 spinner。其它组件不得再内联复制 spinner SVG。 */
withDefaults(defineProps<{ label?: string; inline?: boolean }>(), { label: '加载中…', inline: false });
</script>

<template>
  <div :class="inline ? 'inline-flex items-center gap-2' : 'flex items-center justify-center gap-2 py-8'">
    <svg class="size-5 shrink-0 animate-spin text-accent" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" opacity="0.22" />
      <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" stroke-width="3" stroke-linecap="round" />
    </svg>
    <span v-if="label" class="text-sm text-label2">{{ label }}</span>
  </div>
</template>
