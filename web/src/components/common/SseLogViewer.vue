<script setup lang="ts">
import { nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { openModelLogStream } from '@/api/sse';
import type { LogSseEvent } from '@/api/sse';

/**
 * SSE 日志查看器（暗色终端风）
 *
 * - 通过 url（/admin/api/models/{name}/log/stream）订阅 EventSource
 * - 监听 data: {type: "line"|"tail", line} 事件，写入 lines ref
 * - autoFollow=true 时新行到达自动滚到底
 * - 「复制全部」：navigator.clipboard.writeText(lines 拼接)
 * - 顶部状态：连接中 / 已连接 / 已断开
 * - onBeforeUnmount 时 close SSE
 *
 * 用法（ModelDetailView work log tab）：
 *  1. 先用 getModelLog(name, N) 拉尾部 N 行填充 initial prop
 *  2. 再把 url 传入本组件
 */
const props = withDefaults(
  defineProps<{
    /** EventSource URL（已是完整 /admin/api/... 路径） */
    url: string;
    /** 新行到达时是否自动滚到底（默认 true，用户手动滚上去会临时关闭） */
    autoFollow?: boolean;
    /** 初始拉取的尾部行数（语义说明，实际预填充走 initial） */
    tailLines?: number;
    /** 预填充的初始行（来自 getModelLog 的 lines 字段） */
    initial?: string[];
  }>(),
  {
    autoFollow: true,
    tailLines: 200,
    initial: () => [],
  },
);

const lines = reactive<string[]>([...(props.initial ?? [])]);
const state = ref<'connecting' | 'open' | 'closed'>('connecting');
const copied = ref(false);
const box = ref<HTMLElement | null>(null);
/** 跟随底部开关：先取 prop 默认值，后续由 scroll 检测 / 跟随按钮维护 */
const autoFollow = ref<boolean>(props.autoFollow);

/** 订阅句柄（onBeforeUnmount 兜底关） */
let handle: { close(): void } | null = null;

/** 收到一个新行：append 到 lines 末尾（限制长度免爆），跟随则滚到底 */
function push(evt: LogSseEvent) {
  if (state.value === 'connecting') state.value = 'open';
  lines.push(evt.line);
  // 限制最多保留 4000 行
  if (lines.length > 4000) {
    lines.splice(0, lines.length - 4000);
  }
  if (autoFollow.value) {
    nextTick(scrollBottom);
  }
}

/** 滚到底 */
function scrollBottom() {
  const el = box.value;
  if (el) el.scrollTop = el.scrollHeight;
}

/** 滚动检测：用户手动滚到顶则临时关跟随（再滚回底自动跟随） */
function onScroll() {
  const el = box.value;
  if (!el) return;
  // 与底 5px 以内视为跟随
  autoFollow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 5;
}

/** 打开 SSE（一次只开一个） */
function open() {
  if (state.value === 'open') return;
  state.value = 'connecting';
  handle = openModelLogStream(props.url, {
    onLine: push,
    onError: () => {
      // EventSource 内部会自动重试 3 次，不在此主动关
      if (state.value !== 'closed') state.value = 'closed';
    },
    onDone: () => {
      state.value = 'closed';
    },
  });
}

/** 关闭 SSE */
function closeStream() {
  handle?.close();
  handle = null;
  state.value = 'closed';
}

/** 复制全部到剪贴板 */
async function onCopy() {
  try {
    await navigator.clipboard.writeText(lines.join('\n'));
    copied.value = true;
    setTimeout(() => (copied.value = false), 1500);
  } catch (err) {
    console.warn('复制日志失败:', err);
  }
}

onBeforeUnmount(closeStream);

// url 变更时重开
watch(
  () => props.url,
  (u, old) => {
    if (u === old) return;
    closeStream();
    // 重置 lines
    lines.length = 0;
    for (const l of props.initial ?? []) lines.push(l);
    open();
  },
);
</script>

<template>
  <div class="card !p-0">
    <!-- 顶部状态条 -->
    <div class="flex items-center justify-between border-b border-slate-800 px-3 py-2">
      <div class="flex items-center gap-2 text-xs">
        <span
          :class="[
            'size-2 rounded-full',
            state === 'connecting' && 'bg-amber-400 animate-pulse',
            state === 'open' && 'bg-emerald-400',
            state === 'closed' && 'bg-red-400',
          ]"
        />
        <span class="text-slate-300">
          {{ state === 'connecting' ? '连接中…' : state === 'open' ? '已连接' : '已断开' }}
        </span>
      </div>
      <div class="flex items-center gap-2">
        <button
          class="rounded-md px-2 py-1 text-xs text-slate-300 hover:bg-slate-800 hover:text-slate-100 transition-colors"
          :title="autoFollow ? '已自动跟随，点击关闭' : '点击恢复跟随底部'"
          @click="autoFollow = !autoFollow"
        >
          {{ autoFollow ? '跟随  ON' : '跟随 OFF' }}
        </button>
        <!-- 复制全部 -->
        <button class="btn-ghost !py-1 !px-2 text-xs" @click="onCopy">
          <svg class="size-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
          {{ copied ? '已复制' : '复制全部' }}
        </button>
      </div>
    </div>
    <!-- 终端 body（暗色，monospace） -->
    <pre
      ref="box"
      class="max-h-96 overflow-y-auto bg-[#0b1120] px-4 py-3 font-mono text-xs leading-6 text-slate-300 whitespace-pre-wrap break-all"
      @scroll.passive="onScroll"
    >
{{ lines.length === 0 ? '（暂无日志）' : lines.join('\n') }}
    </pre>
  </div>
</template>
