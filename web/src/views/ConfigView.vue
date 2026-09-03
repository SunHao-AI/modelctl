<script setup lang="ts">
import { ref } from 'vue';
import { nginxSnippet, staticConfig } from '@/api/config';
import type { StaticConfigResponse } from '@/api/types';

/**
 * 配置：
 *  - 上方 nginx-snippet：node + host 输入 + 生成按钮
 *  - 下方 snippet 等宽展示 + 复制按钮
 *  - 「查看后端静态配置」按钮 + staticConfig JSON 等宽展示
 */
const node = ref('');
const host = ref('');
const snippet = ref('');
const snippetErr = ref('');
const staticCfg = ref<StaticConfigResponse | null>(null);
const staticErr = ref('');
const copied = ref(false);
const genBusy = ref(false);
const staticBusy = ref(false);

async function onGenerate() {
  if (!node.value.trim() || !host.value.trim()) {
    snippetErr.value = '请填入节点前缀域名或 IP';
    return;
  }
  genBusy.value = true;
  snippetErr.value = '';
  snippet.value = '';
  try {
    const r = await nginxSnippet(node.value, host.value);
    snippet.value = r.snippet ?? '';
  } catch (err) {
    console.warn('nginxSnippet 失败:', err);
    snippetErr.value = (err as { message?: string })?.message || '片段生成失败';
  } finally {
    genBusy.value = false;
  }
}

async function onShowStatic() {
  staticBusy.value = true;
  staticErr.value = '';
  staticCfg.value = null;
  try {
    staticCfg.value = await staticConfig();
  } catch (err) {
    console.warn('staticConfig 失败:', err);
    staticErr.value = (err as { message?: string })?.message || '静态配置读取失败';
  } finally {
    staticBusy.value = false;
  }
}

/** 复制 snippet */
async function copySnippet() {
  try {
    await navigator.clipboard.writeText(snippet.value);
    copied.value = true;
    setTimeout(() => (copied.value = false), 1500);
  } catch (err) {
    console.warn('复制 snippet 失败:', err);
  }
}
</script>

<template>
  <div class="space-y-4">
    <!-- nginx snippet 生成器 -->
    <section class="card">
      <h3 class="mb-3 text-sm font-semibold text-slate-100">nginx 路由片段</h3>
      <p class="mb-3 text-xs text-slate-500">
        后端 <code class="font-mono text-slate-400">build_llm_map(profiles, node, host, port)</code> 会生成 nginx
        <code class="font-mono text-slate-400">map</code> 块；直接粘贴到 nginx 配置中使用。
      </p>
      <div class="flex flex-wrap items-end gap-3">
        <div>
          <label class="label-base" for="node">节点前缀</label>
          <input
            id="node"
            v-model="node"
            class="input-base !w-40"
            placeholder="210"
            @keyup.enter="onGenerate"
          />
        </div>
        <div>
          <label class="label-base" for="host">节点 IP / 域名</label>
          <input
            id="host"
            v-model="host"
            class="input-base !w-48"
            placeholder="10.0.0.210"
            @keyup.enter="onGenerate"
          />
        </div>
        <button class="btn-primary" :disabled="genBusy" @click="onGenerate">
          <svg v-if="genBusy" class="size-4 animate-spin" viewBox="0 0 24 24" fill="none">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" />
          </svg>
          {{ genBusy ? '生成中…' : '生成片段' }}
        </button>
      </div>

      <p v-if="snippetErr" class="mt-3 text-xs text-red-400">{{ snippetErr }}</p>

      <div v-if="snippet" class="mt-3">
        <div class="mb-1 flex items-center justify-between">
          <span class="text-xs text-slate-500">已生成片段（{{ snippet.length }} 字符）</span>
          <button class="btn-ghost !py-1 !px-2 text-xs" @click="copySnippet">
            {{ copied ? '已复制' : '复制' }}
          </button>
        </div>
        <pre class="max-h-96 overflow-auto bg-[#0b1120] p-3 font-mono text-xs leading-6 text-slate-300 whitespace-pre">{{ snippet }}</pre>
      </div>
    </section>

    <!-- 后端静态配置 -->
    <section class="card">
      <div class="mb-3 flex items-center justify-between">
        <div>
          <h3 class="text-sm font-semibold text-slate-100">后端静态配置</h3>
          <p class="mt-1 text-xs text-slate-500">来自 <code class="font-mono text-slate-400">GET /admin/api/config/static</code></p>
        </div>
        <button class="btn-ghost" :disabled="staticBusy" @click="onShowStatic">
          <svg v-if="staticBusy" class="size-4 animate-spin" viewBox="0 0 24 24" fill="none">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" />
          </svg>
          {{ staticBusy ? '加载中…' : '查看' }}
        </button>
      </div>
      <p v-if="staticErr" class="text-xs text-red-400">{{ staticErr }}</p>
      <pre v-else-if="staticCfg" class="max-h-96 overflow-auto bg-[#0b1120] p-3 font-mono text-xs leading-6 text-slate-300 whitespace-pre">{{ JSON.stringify(staticCfg, null, 2) }}</pre>
      <p v-else class="py-4 text-sm text-slate-500">点击「查看」拉取</p>
    </section>
  </div>
</template>
