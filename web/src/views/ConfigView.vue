<script setup lang="ts">
import { ref } from 'vue';
import { nginxSnippet, staticConfig } from '@/api/config';
import type { StaticConfigResponse } from '@/api/types';
import Loading from '@/components/common/Loading.vue';

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
const copyErr = ref('');
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
      <h3 class="mb-3 text-sm font-semibold text-label">nginx 路由片段</h3>
      <p class="mb-3 text-xs text-label3">
        后端 <code class="font-mono text-label2">build_llm_map(profiles, node, host, port)</code> 会生成 nginx
        <code class="font-mono text-label2">map</code> 块；直接粘贴到 nginx 配置中使用。
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
          <Loading v-if="genBusy" inline label="" />
          {{ genBusy ? '生成中…' : '生成片段' }}
        </button>
      </div>

      <p v-if="snippetErr" class="mt-3 text-xs text-red-400">{{ snippetErr }}</p>

      <div v-if="snippet" class="mt-3">
        <div class="mb-1 flex items-center justify-between">
          <span class="text-xs text-label3">已生成片段（{{ snippet.length }} 字符）</span>
          <button class="btn-ghost !py-1 !px-2 text-xs" :class="copyErr ? 'text-red-400' : ''" @click="copySnippet">
            {{ copied ? '已复制' : copyErr ? '复制失败' : '复制' }}
          </button>
        </div>
        <pre class="code-surface max-h-96 overflow-auto rounded-card border border-code-line p-3 text-xs leading-6 whitespace-pre">{{ snippet }}</pre>
      </div>
    </section>

    <!-- 后端静态配置 -->
    <section class="card">
      <div class="mb-3 flex items-center justify-between">
        <div class="mr-3">
          <h3 class="text-sm font-semibold text-label">后端静态配置</h3>
          <p class="mt-1 text-xs text-label3">来自 <code class="font-mono text-label2">GET /admin/api/config/static</code></p>
        </div>
        <button class="btn-ghost" :disabled="staticBusy" @click="onShowStatic">
          <Loading v-if="staticBusy" inline label="" />
          {{ staticBusy ? '加载中…' : '查看' }}
        </button>
      </div>
      <p v-if="staticErr" class="text-xs text-red-400">{{ staticErr }}</p>
      <pre v-else-if="staticCfg" class="code-surface max-h-96 overflow-auto rounded-card border border-code-line p-3 text-xs leading-6 whitespace-pre">{{ JSON.stringify(staticCfg, null, 2) }}</pre>
      <p v-else class="py-4 text-sm text-label3">点击「查看」拉取</p>
    </section>
  </div>
</template>
