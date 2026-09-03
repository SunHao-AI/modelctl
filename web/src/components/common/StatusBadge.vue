<script setup lang="ts">
import { computed } from 'vue';

/**
 * 状态 + 健康 badge（三主态 + 副标）
 *
 *  - state=running → 绿色「运行中」
 *  - state=stopped → 灰色「已停止」
 *  - state 含 error / starting / stopping 等 → 红色「异常」/对应状态
 *  - health=healthy 时附加「健康」副标
 *  - 异常时整个 badge 文本红色
 */

interface Style {
  /** badge 背景 + 文字 + 边框 */
  cls: string;
  /** 主文本 */
  text: string;
  /** 主点颜色（用于小圆点） */
  dot: string;
  /** 是否异常态（异常时副标也会变红） */
  isErr: boolean;
}

const props = withDefaults(
  defineProps<{
    /** 服务状态：running / stopped / error / starting / stopping / skipped 等 */
    state: string;
    /** 可选健康检查：healthy / unhealthy / unknown / null */
    health?: string | null;
  }>(),
  {
    state: 'unknown',
    health: null,
  },
);

const STYLE_MAP: Record<string, Style> = {
  running: {
    cls: 'bg-emerald-600/15 text-emerald-300 border border-emerald-500/30',
    text: '运行中',
    dot: 'bg-emerald-400',
    isErr: false,
  },
  stopped: {
    cls: 'bg-slate-600/15 text-slate-300 border border-slate-500/30',
    text: '已停止',
    dot: 'bg-slate-400',
    isErr: false,
  },
  skipped: {
    cls: 'bg-slate-600/15 text-slate-300 border border-slate-500/30',
    text: '已跳过',
    dot: 'bg-slate-400',
    isErr: false,
  },
  starting: {
    cls: 'bg-blue-600/15 text-blue-300 border border-blue-500/30',
    text: '启动中',
    dot: 'bg-blue-400',
    isErr: false,
  },
  stopping: {
    cls: 'bg-amber-600/15 text-amber-300 border border-amber-500/30',
    text: '停止中',
    dot: 'bg-amber-400',
    isErr: false,
  },
  queued: {
    cls: 'bg-slate-600/15 text-slate-300 border border-slate-500/30',
    text: '排队中',
    dot: 'bg-slate-400',
    isErr: false,
  },
  unknown: {
    cls: 'bg-slate-600/15 text-slate-400 border border-slate-500/30',
    text: '未知',
    dot: 'bg-slate-500',
    isErr: false,
  },
  error: {
    cls: 'bg-red-600/15 text-red-300 border border-red-500/40',
    text: '异常',
    dot: 'bg-red-400',
    isErr: true,
  },
};

/** 取样式：先精确命中，再用 contains('error') 兜底 */
const style = computed<Style>(() => {
  const key = (props.state ?? '').toLowerCase();
  if (STYLE_MAP[key]) return STYLE_MAP[key];
  if (key.includes('error')) return STYLE_MAP.error;
  return STYLE_MAP.unknown;
});

/** 是否显示「健康」副标 */
const showHealthy = computed(() => props.health === 'healthy');
/** 健康副标（"" / "健康"） */
const healthySuffix = computed(() => (showHealthy.value ? '· 健康' : ''));
</script>

<template>
  <span :class="['inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium', style.cls]">
    <span
      :class="['size-1.5 rounded-full', style.dot, style.isErr ? '' : 'animate-pulse']"
      :aria-hidden="true"
    />
    <span :class="style.isErr ? 'text-red-300' : ''">
      {{ style.text }}
      <span v-if="healthySuffix" :class="style.isErr ? 'text-red-300' : 'text-emerald-300'">{{ healthySuffix }}</span>
    </span>
  </span>
</template>
