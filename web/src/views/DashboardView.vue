<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import dayjs from 'dayjs';
import { health, overview } from '@/api/services';
import type { EngineBinary, OverviewResponse } from '@/api/types';
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

/**
 * 上一次轮询是否还在飞（防重叠：3s setInterval 在后端还没回来时再次进入会
 * 发起第二条同 URL 请求；两条同 URL 请求共用浏览器保活池时，浏览器可能
 * abort 旧连接 → 控制台 net::ERR_ABORTED（前端 axios 表现为 ERR_CANCELED
 * / ECONNABORTED）。后端慢一拍时本帧自然顺延而不堆积，UI 保留旧 data。
 */
let pending: Promise<void> | null = null;

/**
 * /health 的 uptime_s（QA-A-12）：overview 的 uptime_s 恒 null 是后端遗留
 * （admin_probe.py 注释「留空由前端覆盖」），这里并行拉免鉴权且极快（~15ms）
 * 的 /health 覆盖系统卡「可用时间」。
 */
const healthUptime = ref<number | null>(null);

function refreshHealth(): void {
  void health()
    .then((r) => (healthUptime.value = r.uptime_s ?? null))
    .catch(() => undefined); // 静默：保留旧值，fmtUptime 兜底「未知」
}

function refresh(): Promise<void> {
  if (pending) return pending;
  pending = (async () => {
    try {
      data.value = await overview();
    } catch (err: unknown) {
      const e = err as { message?: string; code?: string; isCancel?: boolean };
      // axios 取消/中断：isCancel（取消令牌）+ ECONNABORTED + "aborted/canceled" 文案
      const isAbort =
        !!e?.isCancel ||
        e?.code === 'ECONNABORTED' ||
        /aborted|canceled|cancelled/i.test(e?.message ?? '');
      if (isAbort) return; // 静默：UI 无影响（保留旧 data），避免误报警
      if (typeof console !== 'undefined') console.warn('overview 失败:', e);
      errMsg.value = e?.message || '后端返回异常';
    } finally {
      pending = null;
    }
  })();
  return pending;
}

onMounted(() => {
  refresh();
  refreshHealth();
  timer = window.setInterval(() => {
    void refresh();
    refreshHealth();
  }, 3000);
});
onBeforeUnmount(() => {
  if (timer !== undefined) clearInterval(timer);
});

/** 运行中的模型数 */
const runningCount = computed(() => {
  if (!data.value) return 0;
  return data.value.models.filter((m) => m.state === 'running').length;
});

/** uptime 秒 → 可读字符串（优先 /health 的 uptime_s，overview 恒 null 时兜底） */
function fmtUptime(): string {
  const u = healthUptime.value ?? data.value?.uptime_s;
  if (u === null || u === undefined) return '未知';
  return Math.round(u) >= 0 ? `${Math.round(u)}s` : '未知';
}

/** 探测时间（后端带偏移的 ISO）→ 浏览器本地 YYYY-MM-DD HH:mm:ss */
function fmtProbedAt(): string {
  const v = data.value?.probed_at;
  return v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '未知';
}

/** 引擎可达性来源：venv 已装 / 仅 docker 旁路 / 不可达 */
type EngineRuntime = 'venv' | 'docker' | 'none';

function engineRuntime(b: EngineBinary): EngineRuntime {
  if (b.runtime === 'venv' || b.available) return 'venv';
  if (b.runtime === 'docker' || b.reachable) return 'docker';
  return 'none';
}

/** 排序权重：venv 已装 > 仅 docker 旁路 > 不可达（同档按名字） */
const RUNTIME_ORDER: Record<EngineRuntime, number> = { venv: 0, docker: 1, none: 2 };

/** 悬浮说明：说清 ✓ 是 venv 还是仅 docker 旁路，避免与环境页「未安装」口径混淆 */
function engineTitle(b: EngineBinary): string {
  const rt = engineRuntime(b);
  if (rt === 'venv') return b.path ? `venv 已安装：${b.path}` : 'venv 已安装';
  if (rt === 'docker') return 'venv 未安装；docker 环境就绪，可在模型 yaml 配 docker_image 走容器';
  return b.docker_capable
    ? '不可用：venv 未安装，且 docker 环境未就绪'
    : '不可用：该引擎不支持 docker 运行时，需 modelctl env setup';
}

/** 按「可达来源 → name」排序的引擎二进制列表 */
const engineBinaries = computed<EngineBinary[]>(() => {
  if (!data.value) return [];
  return [...(data.value.hardware.engine_binaries ?? [])].sort((a, b) => {
    const ra = engineRuntime(a);
    const rb = engineRuntime(b);
    return ra === rb ? a.name.localeCompare(b.name) : RUNTIME_ORDER[ra] - RUNTIME_ORDER[rb];
  });
});
</script>

<template>
  <div class="space-y-4">
    <!-- 顶部 4 卡片 -->
    <div class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
      <!-- 硬件 -->
      <section class="card transition-transform duration-200 hover:-translate-y-0.5" style="box-shadow: var(--shadow-m)">
        <h3 class="card-title">硬件</h3>
        <div v-if="data?.hardware" class="space-y-2 text-sm">
          <div class="flex items-center justify-between">
            <span class="text-label2">GPU 数</span>
            <span class="num text-[26px] leading-tight font-semibold tracking-[-.028em] text-label">{{ data.hardware.gpu_count }}</span>
          </div>
          <div class="flex items-center justify-between">
            <span class="text-label2">总显存</span>
            <span class="num text-[26px] leading-tight font-semibold tracking-[-.028em] text-label">{{ data.hardware.total_vram_gb }} GB</span>
          </div>
          <div class="flex items-start justify-between gap-2">
            <span class="shrink-0 text-label2">型号</span>
            <span class="truncate font-mono text-label" :title="data.hardware.gpu_name || '未探测'">
              {{ data.hardware.gpu_name || '未探测' }}
            </span>
          </div>
        </div>
        <div v-else class="text-sm text-label3">加载中…</div>
      </section>

      <!-- 服务（gateway + stats） -->
      <section class="card transition-transform duration-200 hover:-translate-y-0.5" style="box-shadow: var(--shadow-m)">
        <h3 class="card-title">服务</h3>
        <div v-if="data?.services" class="space-y-2 text-sm">
          <div class="flex items-center justify-between">
            <span class="text-label2">gateway:<span class="num">:{{ data.services.gateway.port }}</span></span>
            <StatusBadge :state="data.services.gateway.state" />
          </div>
          <div class="flex items-center justify-between">
            <span class="text-label2">stats:<span class="num">:{{ data.services.stats.port }}</span></span>
            <StatusBadge :state="data.services.stats.state" />
          </div>
        </div>
        <div v-else class="text-sm text-label3">加载中…</div>
      </section>

      <!-- 模型 -->
      <section class="card transition-transform duration-200 hover:-translate-y-0.5" style="box-shadow: var(--shadow-m)">
        <h3 class="card-title">模型</h3>
        <div v-if="data" class="space-y-2 text-sm">
          <div class="flex items-center justify-between">
            <span class="text-label2">总数</span>
            <span class="num text-[26px] leading-tight font-semibold tracking-[-.028em] text-label">{{ data.model_count }}</span>
          </div>
          <div class="flex items-center justify-between">
            <span class="text-label2">运行中</span>
            <span class="num text-[26px] leading-tight font-semibold tracking-[-.028em] text-ok">{{ runningCount }}</span>
          </div>
          <div class="flex items-start justify-between gap-2">
            <span class="shrink-0 text-label2">默认</span>
            <span class="truncate font-mono text-label" :title="data.default_model || '未配置'">
              {{ data.default_model || '未配置' }}
            </span>
          </div>
        </div>
      </section>

      <!-- 系统 -->
      <section class="card transition-transform duration-200 hover:-translate-y-0.5" style="box-shadow: var(--shadow-m)">
        <h3 class="card-title">系统</h3>
        <div v-if="data" class="space-y-2 text-sm">
          <div class="flex items-center justify-between">
            <span class="text-label2">版本</span>
            <span class="num text-label">{{ data.version || '不可用' }}</span>
          </div>
          <div class="flex items-center justify-between">
            <span class="text-label2">可用时间</span>
            <span class="num text-label">{{ fmtUptime() }}</span>
          </div>
          <div class="flex items-center justify-between">
            <span class="text-label2">探测于</span>
            <span class="num text-label">{{ fmtProbedAt() }}</span>
          </div>
        </div>
      </section>
    </div>

    <!-- 引擎二进制 -->
    <section class="card">
      <h3 class="card-title">引擎二进制</h3>
      <div v-if="engineBinaries.length" class="grid grid-cols-1 gap-2 sm:grid-cols-2 md:grid-cols-2 lg:grid-cols-4">
        <div
          v-for="b in engineBinaries"
          :key="b.name"
          :class="[
            'flex items-center justify-between gap-2 rounded-ctl border px-3 py-2 text-sm',
            engineRuntime(b) === 'venv'
              ? 'border-ok-line bg-ok-bg'
              : engineRuntime(b) === 'docker'
                ? 'border-warn-line bg-warn-bg'
                : 'border-danger-line bg-danger-bg',
          ]"
          :title="engineTitle(b)"
        >
          <span class="font-mono text-label">{{ b.name }}</span>
          <!-- venv 已装：✓；仅 docker 旁路：docker 标记；不可达：✗ -->
          <span
            v-if="engineRuntime(b) === 'venv'"
            class="text-ok"
          >✓</span>
          <span
            v-else-if="engineRuntime(b) === 'docker'"
            class="rounded-full border border-sep px-1.5 py-0.5 text-[10px] font-medium text-label3"
          >docker</span>
          <span v-else class="text-danger">✗</span>
        </div>
      </div>
      <div v-else class="text-sm text-label3">尚无数据</div>
      <!-- 图例：区分「venv 已装」与「仅 docker 旁路（venv 未安装）」 -->
      <div class="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-label3">
        <span class="inline-flex items-center gap-1"><span class="text-ok">✓</span>venv 已安装</span>
        <span class="inline-flex items-center gap-1">
          <span class="rounded-full border border-sep px-1 py-px text-[10px] text-label3">docker</span>
          venv 未装，可走 docker 旁路
        </span>
        <span class="inline-flex items-center gap-1"><span class="text-danger">✗</span>不可用</span>
      </div>
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
