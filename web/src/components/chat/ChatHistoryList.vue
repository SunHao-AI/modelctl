<script setup lang="ts">
import type { PropType } from 'vue';
import type { ChatSession } from '@/stores/chat';

defineProps({
  sessions: { type: Array as PropType<ChatSession[]>, default: () => [] },
  activeId: { type: String, default: '' },
  degraded: { type: Boolean, default: false },
});
const emit = defineEmits(['select', 'new']);
</script>

<template>
  <div class="w-40 shrink-0 overflow-y-auto border-r border-sep bg-surface2 p-2">
    <div class="mb-2 flex items-center justify-between">
      <b class="text-xs uppercase tracking-wide text-label2">历史（本机）</b>
      <button class="text-xs text-accent hover:text-accent-hover" @click="emit('new')">新建</button>
    </div>
    <div
      v-for="s in sessions"
      :key="s.id"
      :class="[
        'mb-1 cursor-pointer truncate rounded px-2 py-1 text-xs',
        s.id === activeId ? 'bg-slate-800 text-label' : 'text-label3 hover:bg-surface3 hover:text-label',
      ]"
      :title="`${s.title} · ${s.model} · ${s.createdAt}`"
      @click="emit('select', s.id)"
    >
      {{ s.title }}
    </div>
    <div v-if="!sessions.length" class="text-xs text-label3">暂无历史</div>
    <div v-if="degraded" class="mt-2 rounded bg-warn-bg p-2 text-[11px] text-warn">
      本地存储已满，已清理最旧会话的图片以腾出空间。
    </div>
  </div>
</template>
