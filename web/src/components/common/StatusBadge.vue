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
  /** 是否异常态（副标「健康」恒为绿色） */
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
  running: { cls: 'bg-ok-bg text-ok border border-ok-line', text: '运行中', dot: 'bg-ok', isErr: false },
  stopped: { cls: 'bg-surface3 text-label2 border border-sep', text: '已停止', dot: 'bg-muted', isErr: false },
  skipped: { cls: 'bg-surface3 text-label2 border border-sep', text: '已跳过', dot: 'bg-muted', isErr: false },
  starting: { cls: 'bg-accent-bg text-accent border border-accent-line', text: '启动中', dot: 'bg-accent', isErr: false },
  stopping: { cls: 'bg-warn-bg text-warn border border-warn-line', text: '停止中', dot: 'bg-warn', isErr: false },
  queued: { cls: 'bg-surface3 text-label2 border border-sep', text: '排队中', dot: 'bg-muted', isErr: false },
  unknown: { cls: 'bg-surface3 text-label3 border border-sep', text: '未知', dot: 'bg-muted', isErr: false },
  error: { cls: 'bg-danger-bg text-danger border border-danger-line', text: '异常', dot: 'bg-danger', isErr: true },
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
      :class="['stonedot', style.dot, style.isErr ? '' : 'animate-pulse']"
      :aria-hidden="true"
    />
    <span>
      {{ style.text }}
      <span v-if="healthySuffix" class="text-ok">{{ healthySuffix }}</span>
    </span>
  </span>
</template>
