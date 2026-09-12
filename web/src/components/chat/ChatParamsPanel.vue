<script setup lang="ts">
import { useChatStore } from '@/stores/chat';

const chat = useChatStore();
</script>

<template>
  <div class="mb-2 rounded border border-slate-800 p-2">
    <b class="mb-1 block text-xs uppercase tracking-wide text-slate-400">推理参数（下次发送生效）</b>
    <label class="text-xs text-slate-500">temperature {{ chat.params.temperature.toFixed(2) }}</label>
    <input v-model.number="chat.params.temperature" type="range" min="0" max="2" step="0.05" class="mb-1 w-full" />
    <label class="text-xs text-slate-500">top_p {{ chat.params.top_p.toFixed(2) }}</label>
    <input v-model.number="chat.params.top_p" type="range" min="0" max="1" step="0.05" class="mb-1 w-full" />
    <label class="text-xs text-slate-500">max_tokens</label>
    <input v-model.number="chat.params.max_tokens" type="number" min="1" class="mb-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
    <label class="mt-1 block text-xs text-slate-500">system prompt</label>
    <textarea v-model="chat.params.system" rows="2" class="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm"></textarea>
    <label class="mt-1 flex items-center gap-1 text-xs text-slate-500">
      <input v-model="chat.params.includeUsage" type="checkbox" />
      请求 usage（个别引擎不认 stream_options 时可关）
    </label>
  </div>
</template>
