<script setup lang="ts">
import { computed } from 'vue';
import type { StartupSnapshot, StartupStage, StartupStageName } from '@/api/types';

/**
 * 启动进度卡片：5 段时间轴 + 当前阶段进度条（确定态实条 / 不确定态条纹）+ ETA + 错误态。
 *
 * 数据源由父组件提供：发起页合并 task SSE 的 stage 帧，旁观/刷新后读 /startup 快照。
 * 时间字段（startedAt / finishedAt）后端已按 YYYY-MM-DD HH:mm:ss 格式化，前端不再加工。
 */
const props = defineProps<{
  snapshot: StartupSnapshot;
  /** docker 时错误提示引导「环境」页修 docker */
  toEnvPage?: () => void;
}>();

const ORDER: StartupStageName[] = ['preflight', 'prepare_env', 'launch', 'loading', 'health'];
const LABELS: Record<StartupStageName, string> = {
  preflight: '依赖检查',
  prepare_env: '准备环境',
  launch: '拉起进程',
  loading: '加载模型',
  health: '就绪',
};

/** 按固定顺序补齐缺失阶段（后端快照可能只含部分阶段） */
const stages = computed<StartupStage[]>(() =>
  ORDER.map(
    (n) =>
      props.snapshot.stages.find((s) => s.stage === n) ?? {
        stage: n,
        status: 'pending',
        label: LABELS[n],
        pct: null,
        etaSeconds: null,
        error: null,
        startedAt: null,
        finishedAt: null,
      },
  ),
);

const currentIndex = computed(() => {
  const run = stages.value.findIndex((s) => s.status === 'running');
  if (run >= 0) return run;
  const err = stages.value.findIndex((s) => s.status === 'error');
  if (err >= 0) return err;
  const lastDone = stages.value.reduce((acc, s, i) => (s.status === 'done' ? i : acc), -1);
  return Math.min(stages.value.length - 1, lastDone + 1);
});

const current = computed(() => stages.value[currentIndex.value]);
const failed = computed(() => stages.value.some((s) => s.status === 'error'));
const succeeded = computed(
  () => !failed.value && stages.value.every((s) => s.status === 'done'),
);

/** 当前阶段百分比（null → 条纹动画） */
const pctInt = computed(() =>
  current.value.pct === null ? null : Math.max(0, Math.min(100, Math.round(current.value.pct * 100))),
);
const etaText = computed(() => {
  const s = current.value.etaSeconds;
  if (s === null || s === undefined) return '首次运行，无预估';
  if (s < 60) return `约剩 ${s} 秒`;
  const m = Math.round(s / 60);
  return `约剩 ${m} 分钟`;
});
/** 错误文案含环境类关键词时给出跳转入口 */
const showEnvLink = computed(() =>
  failed.value && /(环境未创建|未安装|不在 PATH|Docker 环境)/.test(current.value.error ?? ''),
);

function dotClass(i: number): string {
  const st = stages.value[i].status;
  if (st === 'error') return 'bg-danger';
  if (st === 'done') return 'bg-ok opacity-70';
  if (st === 'running') return 'bg-ok animate-pulse';
  return 'bg-surface4';
}
function textClass(i: number): string {
  const st = stages.value[i].status;
  if (st === 'error') return 'text-danger';
  if (i === currentIndex.value) return 'text-ok';
  if (st === 'done') return 'text-ok opacity-70';
  return 'text-label3';
}
</script>

<template>
  <div class="startup-card card space-y-3">
    <!-- 标题 + 状态徽标 -->
    <div class="flex items-baseline justify-between">
      <div>
        <h3 class="card-title !mb-0">启动进度</h3>
        <p class="mt-0.5 text-xs text-label3">
          运行时 <span class="font-mono text-label2">{{ snapshot.runtime }}</span> ·
          引擎 <span class="font-mono text-label2">{{ snapshot.engine }}</span> ·
          更新于 {{ snapshot.updatedAt || '—' }}
        </p>
      </div>
      <span
        v-if="succeeded"
        class="inline-flex items-center gap-1.5 rounded-full border border-ok-line bg-ok-bg px-2 py-0.5 text-xs text-ok"
      ><span class="size-1.5 rounded-full bg-ok" />已就绪</span>
      <span
        v-else-if="failed"
        class="inline-flex items-center gap-1.5 rounded-full border border-danger-line bg-danger-bg px-2 py-0.5 text-xs text-danger"
      ><span class="size-1.5 rounded-full bg-danger" />启动失败</span>
    </div>

    <!-- 5 段时间轴 -->
    <ol class="startup-card__steps flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs">
      <li v-for="(s, i) in stages" :key="s.stage" :class="['flex items-center gap-1.5', textClass(i)]">
        <span :class="['size-1.5 rounded-full', dotClass(i)]" />
        <span>{{ LABELS[s.stage] }}</span>
        <span v-if="i < stages.length - 1" class="mx-1 text-label3">→</span>
      </li>
    </ol>

    <!-- 当前阶段进度条 -->
    <div class="space-y-1.5">
      <div class="flex flex-wrap items-center justify-between gap-2 text-xs">
        <span class="text-label2">{{ current.label }}</span>
        <span v-if="current.status !== 'done'" class="text-label2">
          {{ pctInt !== null ? `${pctInt}% · ${etaText}` : etaText }}
        </span>
      </div>
      <div class="startup-card__track h-1.5 w-full overflow-hidden rounded-full bg-surface4">
        <div
          v-if="pctInt !== null"
          class="h-full rounded-full bg-ok opacity-80 transition-[width] duration-500"
          :style="{ width: `${pctInt}%` }"
        />
        <div v-else class="startup-card__stripes h-full w-full" />
      </div>
    </div>

    <!-- 错误态 -->
    <div v-if="failed && current.error" class="space-y-2">
      <div class="rounded-ctl border border-danger-line bg-danger-bg px-3 py-2 text-sm leading-5 whitespace-pre-line text-danger">
        {{ current.error }}
      </div>
      <button v-if="showEnvLink && toEnvPage" class="btn-ghost !py-1 !px-2 text-xs" @click="toEnvPage">
        去环境页
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 不确定态条纹动画（BEM：块 startup-card，元素 __stripes） */
.startup-card__stripes {
  background-image: repeating-linear-gradient(
    45deg,
    color-mix(in srgb, var(--ok) 45%, transparent) 0 6px,
    color-mix(in srgb, var(--ok) 20%, transparent) 6px 12px
  );
  background-size: 16.97px 100%;
  animation: startup-card-stripes 1.1s linear infinite;
}
@keyframes startup-card-stripes {
  from {
    background-position: 0 0;
  }
  to {
    background-position: 16.97px 0;
  }
}
@media (prefers-reduced-motion: reduce) {
  .startup-card__stripes {
    animation: none;
  }
}
</style>
