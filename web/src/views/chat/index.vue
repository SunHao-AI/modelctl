<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { listModels } from '@/api/models';
import type { ModelInfo } from '@/api/types';
import { flattenGroups, pickRunnableModels } from '@/utils/chatModels';
import { useChatStore } from '@/stores/chat';
import ChatMessage from '@/components/chat/ChatMessage.vue';
import ChatComposer from '@/components/chat/ChatComposer.vue';
import ChatParamsPanel from '@/components/chat/ChatParamsPanel.vue';
import ChatStatsPanel from '@/components/chat/ChatStatsPanel.vue';
import ChatRawPanel from '@/components/chat/ChatRawPanel.vue';
import ChatHistoryList from '@/components/chat/ChatHistoryList.vue';

// 组件名与 route.name 一致以配 keep-alive（Task 7 注册 /chat → name: 'chat'）
defineOptions({ name: 'chat' });

const chat = useChatStore();
const models = ref<ModelInfo[]>([]);
const loading = ref(false);
const lastPayload = ref<Record<string, unknown> | null>(null);

const runnable = computed(() => pickRunnableModels(models.value));
const lastAssistant = computed(() => {
  const list = chat.messages;
  for (let i = list.length - 1; i >= 0; i -= 1) if (list[i].role === 'assistant') return list[i];
  return null;
});

async function refresh() {
  loading.value = true;
  try {
    models.value = flattenGroups(await listModels());
    if (!chat.model && runnable.value.length) chat.setModel(runnable.value[0].name);
  } finally {
    loading.value = false;
  }
}

function onModelChange(e: Event) {
  const target = e.target as HTMLSelectElement;
  chat.setModel(target.value);
}

async function onSend(text: string, images: string[]) {
  lastPayload.value = { model: chat.active?.model, route_mode: chat.routeMode, messages: [{ role: 'user', content: text }] };
  await chat.send(text, images);
}

onMounted(refresh);
</script>

<template>
  <div class="flex h-full min-h-0">
    <ChatHistoryList
      :sessions="chat.sessions"
      :active-id="chat.activeId"
      :degraded="chat.storageDegraded"
      @select="chat.selectSession"
      @new="chat.newSession"
    />

    <div class="flex min-w-0 flex-1 flex-col">
      <!-- 顶栏：模型选择 + 走网关开关 -->
      <div class="flex items-center gap-2 border-b border-slate-800 bg-slate-900 px-3 py-2 text-sm">
        <select
          :value="chat.model"
          class="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100"
          @change="onModelChange"
        >
          <option v-if="!runnable.length" value="">（无已启动且健康的模型）</option>
          <option v-for="m in runnable" :key="m.name" :value="m.name">
            {{ m.name }} · {{ m.engine }}{{ m.vision ? ' · 视觉' : '' }}
          </option>
        </select>
        <button
          :class="[
            'rounded-full border px-2 py-1 text-xs',
            chat.routeMode === 'gateway'
              ? 'border-blue-500 bg-blue-600/15 text-blue-300'
              : 'border-slate-700 text-slate-400',
          ]"
          :title="chat.routeMode === 'gateway' ? '复现网关家族路由/上下文切换' : '选谁打谁'"
          @click="chat.routeMode = chat.routeMode === 'gateway' ? 'direct' : 'gateway'"
        >
          走网关：{{ chat.routeMode === 'gateway' ? '开' : '关' }}
        </button>
        <button class="ml-auto text-xs text-slate-400 hover:text-slate-200" :disabled="loading" @click="refresh">
          {{ loading ? '刷新中…' : '刷新模型' }}
        </button>
      </div>

      <!-- 对话流 -->
      <div class="flex flex-1 flex-col gap-2 overflow-y-auto p-3">
        <div v-if="!chat.active" class="m-auto text-sm text-slate-500">点左栏「新建」开始对话</div>
        <ChatMessage v-for="m in chat.messages" :key="m.id" :msg="m" />
      </div>

      <ChatComposer :streaming="chat.streaming" @send="onSend" @stop="chat.stop()" />
    </div>

    <!-- 调试面板 -->
    <div class="w-72 shrink-0 overflow-y-auto border-l border-slate-800 bg-slate-900/60 p-2 text-slate-300">
      <ChatParamsPanel />
      <ChatStatsPanel :msg="lastAssistant" />
      <ChatRawPanel :raw="chat.rawText" :payload="lastPayload" />
    </div>
  </div>
</template>
