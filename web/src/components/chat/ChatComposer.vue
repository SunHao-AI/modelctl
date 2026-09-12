<script setup lang="ts">
import { ref } from 'vue';

const props = defineProps({ streaming: { type: Boolean, default: false } });
const emit = defineEmits(['send', 'stop']);

const text = ref('');
const images = ref<string[]>([]); // dataURL[]
const MAX_IMAGES = 4;

/** 压到最长边 1280 / JPEG 0.82（≈200-400 KB），否则 base64 很快撑爆 localStorage 与 24MB 上限。 */
function compress(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, 1280 / Math.max(img.width, img.height));
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      const ctx = canvas.getContext('2d');
      if (!ctx) {
        URL.revokeObjectURL(url);
        reject(new Error('图片解码失败'));
        return;
      }
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(url);
      resolve(canvas.toDataURL('image/jpeg', 0.82));
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('图片解码失败')); };
    img.src = url;
  });
}

async function addFiles(list: (File | null)[]) {
  const files = list.filter((f): f is File => !!f && f.type.startsWith('image/'));
  for (const f of files) {
    if (images.value.length >= MAX_IMAGES) break;
    try {
      images.value.push(await compress(f));
    } catch {
      /* 单张解码失败不影响其余 */
    }
  }
}

function onPaste(e: ClipboardEvent) {
  const items = Array.from(e.clipboardData?.items || []);
  addFiles(items.map((it) => it.getAsFile()));
}

function onPick(e: Event) {
  const input = e.target as HTMLInputElement;
  addFiles(Array.from(input.files || []));
  input.value = '';
}

function submit() {
  if (props.streaming) return;
  if (!text.value && !images.value.length) return;
  emit('send', text.value, images.value);
  text.value = '';
  images.value = [];
}

/** 中文/日文输入法用回车确认候选时 key 仍是 'Enter'，此时不得发送（keyCode 229 为兼容兜底）。 */
function onEnter(e: KeyboardEvent) {
  if (e.isComposing || e.keyCode === 229) return;
  e.preventDefault();
  submit();
}
</script>

<template>
  <div class="border-t border-slate-800 bg-slate-900 p-2">
    <div v-if="images.length" class="mb-1 flex gap-1">
      <div v-for="(d, i) in images" :key="i" class="relative">
        <img :src="d" class="h-14 rounded" alt="待发送图片" />
        <button
          class="absolute -top-1 -right-1 flex size-4 items-center justify-center rounded-full bg-slate-700 text-xs text-slate-200"
          @click="images.splice(i, 1)"
        >
          ×
        </button>
      </div>
    </div>

    <textarea
      v-model="text"
      rows="2"
      class="w-full resize-none rounded border border-slate-700 bg-slate-950 p-2 text-sm"
      placeholder="输入消息，可直接粘贴图片…"
      @paste="onPaste"
      @keydown.enter.exact="onEnter"
    ></textarea>

    <div class="mt-1 flex items-center justify-end gap-2">
      <label class="mr-auto cursor-pointer rounded-full border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:text-slate-200">
        图片
        <input type="file" accept="image/*" multiple class="hidden" @change="onPick" />
      </label>
      <button
        v-if="streaming"
        class="rounded-full bg-red-900 px-3 py-1 text-sm text-red-200"
        @click="emit('stop')"
      >
        停止
      </button>
      <button
        v-else
        class="rounded-full bg-blue-600 px-3 py-1 text-sm text-white"
        @click="submit"
      >
        发送
      </button>
    </div>
  </div>
</template>
