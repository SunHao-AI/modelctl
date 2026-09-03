<script setup lang="ts">
defineProps<{
  /** 加载提示文字 */
  text?: string;
}>();
</script>

<template>
  <div class="flex items-center justify-center gap-2 p-8">
    <svg class="size-5 animate-spin text-blue-400" viewBox="0 0 24 24" fill="none">
      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" />
    </svg>
    <span class="text-sm text-slate-400">{{ text || '加载中…' }}</span>
  </div>
</template>
