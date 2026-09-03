<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { overview } from '@/api/services';
import type { OverviewResponse } from '@/api/types';
import StatusBadge from '@/components/common/StatusBadge.vue';

/**
 * 仪表板：3s 轮询 /admin/api/overview
 * 顶部 4 大卡片：硬件 / 服务 / 模型 / 系统
 * 中部 Grid（mobile 1 列 / desktop 4 列）：引擎二进制
 * 底部：模型列表 + 服务状态 router-link
 */
const data = ref<OverviewResponse | null>(null);
const errMsg = ref('');

let timer: number | undefined;

async function refresh() {
  try {
    const res = await overview();
    data.value = res;
  } catch (err) {
    console.warn('overview 失败:', err);
    errMsg.value = (err as { message?: string })?.message || '后端返回异常';
  }
}

onMounted(() => {
  refresh();
  timer = window.setInterval(refresh, 3000);
});
onBeforeUnmount(() => {
  if (timer !== undefined) clearInterval(timer);
});

/** 运行中的模型数 */
const runningCount = computed(() => {
  if (!data.value) return 0;
  return data.value.models.filter((m) => m.state === 'running').length;
});

/** uptime 秒 → 可读字符串 */
function fmtUptime(): string {
  const u = data.value?.uptime_s;
  if (u === null || u === undefined) return '未知';
  return Math.round(u) >= 0 ? `${Math.round(u)}s` : '未知';
}

/** 按 name 排序的引擎二进制列表 */
const engineBinaries = computed<Array<{ name: string; state: 'available' | 'missing' }>>(() => {
  if (!data.value) return [];
  return Object.entries(data.value.hardware.engine_binaries ?? {})
    .map(([name, state]) => ({ name, state }))
    .sort((a, b) => (a.state === b.state ? a.name.localeCompare(b.name) : a.state === 'available' ? -1 : 1));
});
</script>

<template>
  <div class="space-y-4">
    <!-- 顶部 4 卡片 -->
    <div class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
      <!-- 硬件 -->
      <section class="card">
        <h3 class="mb-3 text-xs font-medium uppercase tracking-wider text-slate-400">硬件</h3>
        <div v-if="data?.hardware" class="space-y-2 text-sm">
          <div class="flex items-center justify-between">
            <span class="text-slate-400">GPU 数</span>
            <span class="font-mono text-slate-100">{{ data.hardware.gpu_count }}</span>
          </div>
          <div class="flex items-center justify-between">
            <span class="text-slate-400">总显存</span>
            <span class="font-mono text-slate-100">{{ data.hardware.total_vram_gb }} GB</span>
          </div>
          <div class="flex items-start justify-between gap-2">
            <span class="text-slate-400 shrink-0">型号</span>
            <span class="truncate font-mono text-slate-100" :title="data.hardware.gpu_name || '未探测'">
              {{ data.hardware.gpu_name || '未探测' }}
            </span>
          </div>
        </div>
        <div v-else class="text-sm text-slate-500">加载中…</div>
      </section>

      <!-- 服务（gateway + stats） -->
      <section class="card">
        <h3 class="mb-3 text-xs font-medium uppercase tracking-wider text-slate-400">服务</h3>
        <div v-if="data?.services" class="space-y-2 text-sm">
          <div class="flex items-center justify-between">
            <span class="text-slate-400">gateway:<span class="font-mono">:{{ data.services.gateway.port }}</span></span>
            <StatusBadge :state="data.services.gateway.state" />
          </div>
          <div class="flex items-center justify-between">
            <span class="text-slate-400">stats:<span class="font-mono">:{{ data.services.stats.port }}</span></span>
            <StatusBadge :state="data.services.stats.state" />
          </div>
        </div>
        <div v-else class="text-sm text-slate-500">加载中…</div>
      </section>

      <!-- 模型 -->
      <section class="card">
        <h3 class="mb-3 text-xs font-medium uppercase tracking-wider text-slate-400">模型</h3>
        <div v-if="data" class="space-y-2 text-sm">
          <div class="flex items-center justify-between">
            <span class="text-slate-400">总数</span>
            <span class="font-mono text-slate-100">{{ data.model_count }}</span>
          </div>
          <div class="flex items-center justify-between">
            <span class="text-slate-400">运行中</span>
            <span class="font-mono text-emerald-300">{{ runningCount }}</span>
          </div>
          <div class="flex items-start justify-between gap-2">
            <span class="text-slate-400 shrink-0">默认</span>
            <span class="truncate font-mono text-slate-100" :title="data.default_model || '未配置'">
              {{ data.default_model || '未配置' }}
            </span>
          </div>
        </div>
      </section>

      <!-- 系统 -->
      <section class="card">
        <h3 class="mb-3 text-xs font-medium uppercase tracking-wider text-slate-400">系统</h3>
        <div v-if="data" class="space-y-2 text-sm">
          <div class="flex items-center justify-between">
            <span class="text-slate-400">版本</span>
            <span class="font-mono text-slate-100">{{ data.version || '不可用' }}</span>
          </div>
          <div class="flex items-center justify-between">
            <span class="text-slate-400">可用时间</span>
            <span class="font-mono text-slate-100">{{ fmtUptime() }}</span>
          </div>
          <div class="flex items-center justify-between">
            <span class="text-slate-400">探测于</span>
            <span class="font-mono text-slate-100">{{ data.probed_at }}</span>
          </div>
        </div>
      </section>
    </div>

    <!-- 引擎二进制 -->
    <section class="card">
      <h3 class="mb-3 text-xs font-medium uppercase tracking-wider text-slate-400">引擎二进制</h3>
      <div v-if="engineBinaries.length" class="grid grid-cols-1 gap-2 sm:grid-cols-2 md:grid-cols-2 lg:grid-cols-4">
        <div
          v-for="b in engineBinaries"
          :key="b.name"
          :class="[
            'flex items-center justify-between rounded-lg border px-3 py-2 text-sm',
            b.state === 'available'
              ? 'border-emerald-500/30 bg-emerald-600/5'
              : 'border-red-500/30 bg-red-600/5',
          ]"
        >
          <span class="font-mono text-slate-100">{{ b.name }}</span>
          <span
            :class="b.state === 'available' ? 'text-emerald-300' : 'text-red-300'"
          >
            {{ b.state === 'available' ? '✓' : '✗' }}
          </span>
        </div>
      </div>
      <div v-else class="text-sm text-slate-500">尚无数据</div>
    </section>

    <!-- 错误提示 -->
    <p v-if="errMsg" class="text-sm text-red-400">{{ errMsg }}</p>

    <!-- 快捷入口 -->
    <div class="flex items-center gap-4">
      <router-link class="btn-ghost" :to="{ name: 'models-list' }">
        打开模型列表
        <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14" /><path d="M12 5l7 7-7 7" /></svg>
      </router-link>
      <router-link class="btn-ghost" :to="{ name: 'services-matrix' }">
        服务状态
        <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14" /><path d="M12 5l7 7-7 7" /></svg>
      </router-link>
    </div>
  </div>
</template>
