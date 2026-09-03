<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { probe } from '@/api/services';
import type { ProbeResponse } from '@/api/types';

/**
 * 体检：GET /admin/api/probe → 5 区块（GPU / GPU 锁 / 引擎二进制 / 环境变量 / 路径与版本）
 * 顶部「重新体检」按钮 + 体检时间戳显示
 */
const data = ref<ProbeResponse | null>(null);
const errMsg = ref('');
const probedAt = ref('');
const busy = ref(false);

async function load() {
  busy.value = true;
  try {
    data.value = await probe();
    probedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    errMsg.value = '';
  } catch (err) {
    console.warn('probe 失败:', err);
    errMsg.value = (err as { message?: string })?.message || '体检失败';
  } finally {
    busy.value = false;
  }
}

onMounted(load);

/** MB → GB（保留 1 位）；0 返回 '0' */
function mbToGb(mb: number): string {
  if (mb === 0) return '0';
  return (mb / 1024).toFixed(1);
}
</script>

<template>
  <div class="space-y-4">
    <!-- 顶部：重新体检 + 时间戳 -->
    <div class="flex items-center justify-between">
      <p class="text-sm text-slate-400">
        完整硬件体检（5 区块）
        <span v-if="probedAt" class="ml-2 text-slate-500">上次：{{ probedAt }}</span>
      </p>
      <button class="btn-primary" :disabled="busy" @click="load">
        <svg v-if="busy" class="size-4 animate-spin" viewBox="0 0 24 24" fill="none">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" />
        </svg>
        {{ busy ? '体检中…' : '重新体检' }}
      </button>
    </div>

    <p v-if="errMsg" class="text-sm text-red-400">{{ errMsg }}</p>

    <div v-if="data" class="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <!-- 区块 1：GPU -->
      <section class="card">
        <h3 class="mb-3 text-xs font-medium uppercase tracking-wider text-slate-400">GPU</h3>
        <div class="space-y-2 text-sm">
          <div class="grid grid-cols-2 gap-x-4 gap-y-2">
            <div><span class="text-slate-400">数量</span><div class="font-mono text-slate-100">{{ data.gpu_count }}</div></div>
            <div><span class="text-slate-400">型号</span><div class="font-mono text-slate-100">{{ data.gpu_name || '未知' }}</div></div>
            <div><span class="text-slate-400">显存总量</span><div class="font-mono text-slate-100">{{ data.vram_total_gb }} GB</div></div>
            <div><span class="text-slate-400">空闲（每卡）</span><div class="font-mono text-slate-100">{{ data.vram_free_mb.map(mbToGb).join(' / ') || '—' }} GB</div></div>
            <div><span class="text-slate-400">CUDA 驱动</span><div class="font-mono text-slate-100">{{ data.cuda_driver || '未知' }}</div></div>
            <div><span class="text-slate-400">计算能力（CC）</span><div class="font-mono text-slate-100">{{ data.compute_capability || '未知' }}</div></div>
          </div>
          <p v-if="data.vram_total_mb" class="text-xs text-slate-500">原始 MB：{{ data.vram_total_mb }}</p>
        </div>
      </section>

      <!-- 区块 2：GPU 锁 -->
      <section class="card">
        <h3 class="mb-3 text-xs font-medium uppercase tracking-wider text-slate-400">GPU 锁</h3>
        <div v-if="data.gpu_locks.length" class="flex flex-wrap gap-2">
          <span
            v-for="l in data.gpu_locks"
            :key="l.gpu_index"
            class="inline-flex items-center gap-1.5 rounded-full border border-amber-500/30 bg-amber-600/10 px-2.5 py-1 text-xs text-amber-300"
          >
            GPU {{ l.gpu_index }} ← {{ l.owner || 'unknown' }}
          </span>
        </div>
        <p v-else class="text-sm text-slate-500">无占用</p>
      </section>

      <!-- 区块 3：引擎二进制 -->
      <section class="card !p-0">
        <div class="border-b border-slate-800 px-3 py-2 text-xs font-medium uppercase tracking-wider text-slate-400">
          引擎二进制
        </div>
        <table v-if="data.engine_binaries.length" class="w-full text-sm">
          <tbody>
            <tr v-for="b in data.engine_binaries" :key="b.name" class="border-b border-slate-800/40">
              <td class="w-2/5 px-3 py-1.5">
                <span class="mr-2 inline-block size-2 rounded-full" :class="b.available ? 'bg-emerald-400' : 'bg-red-400'" />
                <span :class="b.available ? 'text-emerald-300' : 'text-red-300'">{{ b.name }}</span>
              </td>
              <td class="font-mono text-xs text-slate-500 break-all">{{ b.path || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="p-3 text-sm text-slate-500">尚无数据</p>
      </section>

      <!-- 区块 4：环境变量 -->
      <section class="card !p-0">
        <div class="border-b border-slate-800 px-3 py-2 text-xs font-medium uppercase tracking-wider text-slate-400">
          环境变量
        </div>
        <table class="w-full text-sm">
          <tbody>
            <tr v-for="([k, v]) in Object.entries(data.env_vars)" :key="k" class="border-b border-slate-800/40">
              <td class="w-2/5 px-3 py-1.5 font-mono text-slate-300">{{ k }}</td>
              <td class="font-mono text-xs text-slate-400 break-all">{{ v || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- 区块 5：路径与版本 -->
      <section class="card">
        <h3 class="mb-3 text-xs font-medium uppercase tracking-wider text-slate-400">路径与版本</h3>
        <div class="space-y-2 text-sm">
          <div><span class="text-slate-400">项目根</span><div class="font-mono text-xs text-slate-200 break-all">{{ data.paths.project_root }}</div></div>
          <div><span class="text-slate-400">缓存目录</span><div class="font-mono text-xs text-slate-200 break-all">{{ data.paths.cache_dir }}</div></div>
          <div><span class="text-slate-400">模型目录</span><div class="font-mono text-xs text-slate-200 break-all">{{ data.paths.models_dir }}</div></div>
          <div><span class="text-slate-400">版本</span><div class="font-mono text-slate-100">{{ data.version || '未知' }}</div></div>
        </div>
      </section>
    </div>

    <p v-else-if="!busy" class="text-sm text-slate-500">尚未体检</p>
    <p v-else class="text-sm text-slate-500">体检中…</p>
  </div>
</template>
