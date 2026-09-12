<script setup lang="ts">
import { ref, type PropType } from 'vue';

const props = defineProps({
  raw: { type: String, default: '' },
  payload: { type: Object as PropType<Record<string, unknown> | null>, default: null },
});
const copied = ref(false);

function copy() {
  const t = JSON.stringify(props.payload || {}, null, 2) + '\n\n' + (props.raw || '');
  navigator.clipboard?.writeText(t);
  copied.value = true;
  setTimeout(() => (copied.value = false), 1200);
}
</script>

<template>
  <div class="mt-2 rounded border border-slate-800 p-2 text-xs">
    <div class="mb-1 flex items-center justify-between">
      <b class="uppercase tracking-wide text-slate-400">原文</b>
      <button class="text-slate-500 hover:text-slate-300" @click="copy">{{ copied ? '已复制' : '复制' }}</button>
    </div>
    <pre class="max-h-40 overflow-auto rounded bg-slate-950 p-2 font-mono text-[11px] leading-snug text-sky-300">{{ payload ? JSON.stringify(payload, null, 2) : '—' }}</pre>
    <pre v-if="raw" class="mt-1 max-h-32 overflow-auto rounded bg-slate-950 p-2 font-mono text-[11px] leading-snug text-sky-300">{{ raw }}</pre>
  </div>
</template>
