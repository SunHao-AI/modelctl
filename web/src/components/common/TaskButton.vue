<script setup lang="ts">
import { ref } from 'vue';
import { onBeforeUnmount } from 'vue';
import { openTaskStream } from '@/api/tasks';
import type { TaskRef } from '@/api/types';

/**
 * 任务式按钮：点击 → 调 target() 拿 TaskRef → 订阅 SSE stream → 完成/失败
 *
 * 用法（模型/服务/环境）：
 *   <TaskButton
 *     label="启动"
 *     variant="primary"
 *     :task-target="() => startModel(name)"
 *     @success="onRefresh"
 *     @error="onError"
 *   />
 *
 * 按钮文本随状态切换：
 *   - idle:        label
 *   - submitting:  提交中…
 *   - running:     执行中…
 *   - ok:          ✔ 完成
 *   - fail:        ✗ 失败
 * 完成 / 失败 2s 后回到 idle；失败会在 2s 内调用 onError；成功调用 onSuccess。
 *
 * 兜底：
 *   - 未拿到 task_ref / 后端返回结构异常 → 失败
 *   - SSE 5 分钟未完成 → 超时失败
 */
const props = withDefaults(
  defineProps<{
    /** 按钮默认文本 */
    label: string;
    /** 按钮风格：primary / danger / ghost */
    variant?: 'primary' | 'danger' | 'ghost';
    /** 任务目标：点击后返回 TaskRef */
    taskTarget: () => Promise<TaskRef>;
    /** 成功回调（detail 来自后端 done 事件 / task 详情） */
    onSuccess?: (detail?: string) => void;
    /** 失败回调：message 来自后端 error 事件 / 网络异常 */
    onError?: (message: string) => void;
  }>(),
  {
    variant: 'primary',
  },
);

const emit = defineEmits<{
  (e: 'success', detail?: string): void;
  (e: 'error', message: string): void;
}>();

type Phase = 'idle' | 'submitting' | 'running' | 'ok' | 'fail';
const text = ref<string>(props.label);
const phase = ref<Phase>('idle');

/** SSE 句柄；onBeforeUnmount 兜底 close */
let streamHandle: { close(): void } | null = null;
/** 5 分钟超时 timer */
let timeoutId: number | undefined;
/** 2s 复位 timer */
let resetId: number | undefined;

/** 终态：ok / fail。统一关闭 stream 并延时复位 */
function finalize(finalPhase: 'ok' | 'fail', message?: string, detail?: string) {
  // 重复 finalize 忽略
  if (phase.value === finalPhase) return;
  phase.value = finalPhase;
  text.value = finalPhase === 'ok' ? '✔ 完成' : '✗ 失败';
  // 关闭 stream + 定时器
  try {
    streamHandle?.close();
  } catch {
    /* ignore */
  }
  streamHandle = null;
  if (timeoutId !== undefined) {
    clearTimeout(timeoutId);
    timeoutId = undefined;
  }
  if (finalPhase === 'ok') {
    try {
      props.onSuccess?.(detail);
      emit('success', detail);
    } catch (err) {
      console.warn('onSuccess 回调异常:', err);
    }
  } else {
    try {
      props.onError?.(message || '');
      emit('error', message || '');
    } catch (err) {
      console.warn('onError 回调异常:', err);
    }
  }
  // 2s 复位回 idle
  if (resetId !== undefined) clearTimeout(resetId);
  resetId = window.setTimeout(() => {
    phase.value = 'idle';
    text.value = props.label;
    resetId = undefined;
  }, 2000);
}

/** 解析 SSE 任务事件并推进状态 */
function handleSseDone(evt: {
  type: 'status' | 'log' | 'error' | 'done';
  data: unknown;
}) {
  if (phase.value === 'idle' || phase.value === 'ok' || phase.value === 'fail') return;
  const d = evt.data as
    | { status?: string; exit_code?: number; message?: string; detail?: string }
    | undefined;
  if (evt.type === 'done') {
    if (d?.status === 'success') {
      finalize('ok', undefined, d?.detail || d?.message);
    } else if (d?.status === 'error') {
      // 后端 done 事件带 status="error" 时按失败处理
      finalize('fail', d?.message || d?.detail || '执行失败');
    } else {
      // done 但无 status（旧版兼容）
      finalize('ok', undefined, d?.detail || d?.message);
    }
  } else if (evt.type === 'error') {
    finalize('fail', d?.message || d?.detail || '执行失败');
  } else if (evt.type === 'status' || evt.type === 'log') {
    // 收到第一个 status/log 视为真正开工
    if (phase.value === 'submitting') {
      phase.value = 'running';
      text.value = '执行中…';
    }
  }
}

/** 点击主入口 */
async function onClick() {
  if (phase.value !== 'idle') return;
  phase.value = 'submitting';
  text.value = '提交中…';

  let taskRef: TaskRef;
  try {
    taskRef = await props.taskTarget();
  } catch (err) {
    finalize('fail', (err as { message?: string })?.message || '提交失败');
    return;
  }
  // 后端返回结构损失
  if (!taskRef?.task_id) {
    finalize('fail', '后端未返回 task_id');
    return;
  }

  phase.value = 'running';
  text.value = '执行中…';

  try {
    streamHandle = openTaskStream(taskRef.task_id, {
      onData: handleSseDone,
      onDone: handleSseDone,
      onError: (err) => {
        console.warn('SSE 错误:', err?.type, err);
        // 不立即 fail：浏览器自动会重试；靠 5 分钟超时兜底
      },
    });
  } catch (err) {
    finalize('fail', (err as { message?: string })?.message || 'SSE 订阅失败');
    return;
  }

  // 5 分钟兜底
  if (timeoutId !== undefined) clearTimeout(timeoutId);
  timeoutId = window.setTimeout(() => {
    if (phase.value === 'submitting' || phase.value === 'running') {
      finalize('fail', '执行超过 300s 未完成（超时）');
    }
  }, 5 * 60 * 1000);
}

onBeforeUnmount(() => {
  try {
    streamHandle?.close();
  } catch {
    /* ignore */
  }
  streamHandle = null;
  if (timeoutId !== undefined) clearTimeout(timeoutId);
  if (resetId !== undefined) clearTimeout(resetId);
});
</script>

<template>
  <button
    :class="[
      phase === 'fail' ? 'btn-danger' : variant === 'primary' ? 'btn-primary' : variant === 'danger' ? 'btn-danger' : 'btn-ghost',
      'min-w-24',
    ]"
    :disabled="phase !== 'idle'"
    :title="text"
    @click="onClick"
  >
    <!-- 等待 spinner -->
    <svg
      v-if="phase === 'submitting' || phase === 'running'"
      class="size-3.5 animate-spin"
      viewBox="0 0 24 24"
      fill="none"
    >
      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" />
    </svg>
    <span>{{ text }}</span>
  </button>
</template>
