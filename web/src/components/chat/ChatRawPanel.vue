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
  <div class="mt-2 rounded border border-sep p-2 text-xs">
    <div class="mb-1 flex items-center justify-between">
      <b class="uppercase tracking-wide text-label2">原文</b>
      <button class="text-label3 hover:text-label" @click="copy">{{ copied ? '已复制' : '复制' }}</button>
    </div>
    <pre class="max-h-40 overflow-auto rounded bg-code-bg p-2 font-mono text-[11px] leading-snug text-code-fg">{{ payload ? JSON.stringify(payload, null, 2) : '—' }}</pre>
    <pre v-if="raw" class="mt-1 max-h-32 overflow-auto rounded bg-code-bg p-2 font-mono text-[11px] leading-snug text-code-fg">{{ raw }}</pre>
  </div>
</template>
