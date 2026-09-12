<script setup lang="ts">
import { computed, type PropType } from 'vue';
import type { ChatMsg } from '@/stores/chat';

const props = defineProps({ msg: { type: Object as PropType<ChatMsg | null>, default: null } });

const tps = computed(() => {
  const m = props.msg;
  if (!m || !m.usage || !m.totalMs || m.totalMs <= 0) return null;
  return (m.usage.completion_tokens / (m.totalMs / 1000)).toFixed(1);
});
</script>

<template>
  <div class="rounded border border-slate-800 p-2 text-xs">
    <b class="mb-1 block uppercase tracking-wide text-slate-400">统计 / 落点</b>
    <template v-if="msg">
      <div class="flex justify-between"><span class="text-slate-500">实际落到</span><span class="text-slate-200">{{ msg.routedTo || '-' }}</span></div>
      <div class="flex justify-between"><span class="text-slate-500">路由原因</span><span class="text-slate-200">{{ msg.routeReason || '直连命中' }}</span></div>
      <div class="flex justify-between"><span class="text-slate-500">TTFT</span><span class="text-slate-200">{{ msg.ttftMs != null ? (msg.ttftMs / 1000).toFixed(2) + 's' : '-' }}</span></div>
      <div class="flex justify-between"><span class="text-slate-500">总耗时</span><span class="text-slate-200">{{ msg.totalMs != null ? (msg.totalMs / 1000).toFixed(1) + 's' : '-' }}</span></div>
      <div class="flex justify-between"><span class="text-slate-500">tok/s</span><span class="text-slate-200">{{ tps ?? '-' }}</span></div>
      <div class="flex justify-between"><span class="text-slate-500">prompt / completion</span><span class="text-slate-200">{{ msg.usage ? `${msg.usage.prompt_tokens}/${msg.usage.completion_tokens}` : '无 usage' }}</span></div>
    </template>
    <div v-else class="text-slate-600">发送后显示</div>
  </div>
</template>
