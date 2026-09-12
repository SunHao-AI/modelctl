<script setup lang="ts">
import { computed, type PropType } from 'vue';
import { renderMarkdown } from '@/utils/markdown';
import type { ChatMsg } from '@/stores/chat';

const props = defineProps({ msg: { type: Object as PropType<ChatMsg>, required: true } });

const html = computed(() => renderMarkdown(props.msg.content));
const stats = computed(() => {
  const m = props.msg;
  const out: string[] = [];
  if (m.ttftMs != null) out.push(`TTFT ${(m.ttftMs / 1000).toFixed(2)}s`);
  if (m.totalMs != null) out.push(`总 ${(m.totalMs / 1000).toFixed(1)}s`);
  if (m.usage) out.push(`${m.usage.prompt_tokens}/${m.usage.completion_tokens} tok`);
  if (m.routedTo) out.push(`落到 ${m.routedTo}${m.routeReason ? `（${m.routeReason}）` : ''}`);
  return out;
});
</script>

<template>
  <div
    :class="[
      'rounded-lg px-3 py-2 text-sm leading-relaxed',
      msg.role === 'user' ? 'self-end max-w-[75%] bg-blue-600 text-slate-50' : 'self-start max-w-[90%] bg-slate-800',
    ]"
  >
    <div v-if="msg.images && msg.images.length" class="mb-1 flex gap-1">
      <img v-for="(d, i) in msg.images" :key="i" :src="d" class="h-16 rounded" alt="附件" />
    </div>

    <details v-if="msg.reasoning" class="mb-1 text-slate-400">
      <summary class="cursor-pointer select-none">思考过程</summary>
      <div class="mt-1 whitespace-pre-wrap border-l-2 border-slate-600 pl-2">{{ msg.reasoning }}</div>
    </details>

    <div v-if="msg.error" class="mb-1 rounded border-l-2 border-red-500 bg-red-500/10 p-2 text-red-300">
      {{ msg.error.message }}
    </div>
    <div
      v-if="msg.error && msg.error.raw"
      class="mb-1 break-all rounded bg-slate-950 p-2 font-mono text-xs whitespace-pre-wrap"
    >
      {{ msg.error.raw }}
    </div>

    <!-- 助手正文是上游 markdown（不可信），renderMarkdown 内已 DOMPurify 消毒 -->
    <div v-if="msg.role === 'assistant'" class="chat-md" v-html="html"></div>
    <div v-else class="whitespace-pre-wrap">{{ msg.content }}</div>

    <div
      v-if="stats.length || msg.interrupted"
      class="mt-1 flex flex-wrap gap-2 border-t border-dashed border-slate-600 pt-1 text-xs text-slate-500"
    >
      <span v-for="s in stats" :key="s">{{ s }}</span>
      <span v-if="msg.interrupted" class="text-amber-400">已中断</span>
    </div>
  </div>
</template>

<style scoped>
/* v-html 注入的节点不带 scoped 属性，必须 :deep() 才能命中 */
.chat-md :deep(pre.hljs) {
  padding: 0.5rem 0.75rem;
  border-radius: 0.375rem;
  overflow-x: auto;
  margin: 0.25rem 0;
}
.chat-md :deep(p) { margin: 0.25rem 0; }
.chat-md :deep(ul), .chat-md :deep(ol) { padding-left: 1.25rem; margin: 0.25rem 0; }
.chat-md :deep(code:not(.hljs code)) {
  background: rgb(2 6 23 / 0.6);
  padding: 0.05rem 0.25rem;
  border-radius: 0.25rem;
}
.chat-md :deep(table) { border-collapse: collapse; margin: 0.25rem 0; }
.chat-md :deep(th), .chat-md :deep(td) { border: 1px solid rgb(51 65 85); padding: 0.2rem 0.4rem; }
</style>
