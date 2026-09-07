<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue';
import { toast } from '@/utils/toast';
import { useTasksStore } from '@/stores/tasks';
import type { TaskRef } from '@/api/types';

/**
 * 任务式按钮：点击 → 调 taskTarget() 拿 TaskRef → 交给 tasksStore 跟踪。
 *
 * 状态完全由 store 派生（跨视图/刷新存活）：
 *   - idle:        无该 target 的任务记录
 *   - submitting:  本地瞬时态（已点尚未拿到 task_ref）
 *   - running:     activityFor(target).status ∈ {queued, running}
 *   - ok:          最近任务终态 success/skipped（完成后 2s 内显示，随后回 idle）
 *   - fail:        最近任务终态 error（同上）
 *
 * 用法：
 *   <TaskButton label="启动" :target="name" :task-target="() => startModel(name)" @success="onRefresh" />
 */
const props = withDefaults(
  defineProps<{
    /** 按钮默认文本 */
    label: string;
    /** 目标名：profile / service / env（store 匹配键） */
    target: string;
    /** 按钮风格 */
    variant?: 'primary' | 'danger' | 'ghost';
    /** 任务目标：点击后返回 TaskRef */
    taskTarget: () => Promise<TaskRef>;
    /** 成功回调（store 终态时触发） */
    onSuccess?: (detail?: string) => void;
    /** 失败回调 */
    onError?: (message: string) => void;
  }>(),
  { variant: 'primary' },
);

const emit = defineEmits<{
  (e: 'success', detail?: string): void;
  (e: 'error', message: string): void;
}>();

const tasksStore = useTasksStore();

/** 该 target 的活动/最近任务 */
const record = computed(() => tasksStore.activityFor(props.target));

/** 本地瞬时提交态（响应式，computed 才能感知） */
const submitting = ref(false);

/**
 * 当前时间戳：tick 存活到"活动期 + 结果窗"（终态后 2s 内继续推进 now），
 * 结果窗到期 getter 转 false 自动停表，phase 回 idle，避免常驻定时器。
 */
const now = ref(Date.now());
let tickId: number | undefined;
watch(
  () => {
    const r = record.value;
    if (!r) return false;
    if (r.status === 'queued' || r.status === 'running') return true;
    // 结果窗：终态后 2s 内保持 tick 推进 now，到期自动停表 → phase 回 idle
    return !!r.finishedAt && now.value - new Date(r.finishedAt).getTime() < 2000;
  },
  (active) => {
    if (active && tickId === undefined) {
      tickId = window.setInterval(() => (now.value = Date.now()), 500);
    } else if (!active && tickId !== undefined) {
      clearInterval(tickId);
      tickId = undefined;
    }
  },
  { immediate: true },
);
onScopeDispose(() => {
  if (tickId !== undefined) clearInterval(tickId);
});

type Phase = 'idle' | 'submitting' | 'running' | 'ok' | 'fail';
const phase = computed<Phase>(() => {
  if (submitting.value) return 'submitting';
  const r = record.value;
  if (!r) return 'idle';
  if (r.status === 'queued' || r.status === 'running') return 'running';
  // 终态：完成后 2s 内显示结果，否则回 idle
  if (!r.finishedAt) return 'idle';
  const within = now.value - new Date(r.finishedAt).getTime() < 2000;
  if (!within) return 'idle';
  return r.status === 'error' ? 'fail' : 'ok';
});

const text = computed(() => {
  switch (phase.value) {
    case 'submitting':
      return '提交中…';
    case 'running':
      return '执行中…';
    case 'ok':
      return '✔ 完成';
    case 'fail':
      return '✗ 失败';
    default:
      return props.label;
  }
});

/** 提交：调 taskTarget → track 到 store（store 终态时回调本组件 onSuccess/onError） */
async function onClick() {
  if (phase.value !== 'idle') return;
  submitting.value = true;
  let refVal: TaskRef;
  try {
    refVal = await props.taskTarget();
  } catch (err) {
    submitting.value = false;
    const msg = (err as { message?: string })?.message || '提交失败';
    // 409 = 已有同 target 任务在跑
    if ((err as { response?: { status?: number } }).response?.status === 409) {
      toast.warning('该目标已有任务在执行中');
    } else {
      toast.error(msg);
    }
    props.onError?.(msg);
    emit('error', msg);
    return;
  }
  submitting.value = false;
  if (!refVal?.task_id) {
    const msg = '后端未返回 task_id';
    toast.error(msg);
    props.onError?.(msg);
    emit('error', msg);
    return;
  }
  tasksStore.track(refVal, {
    target: props.target,
    retryFn: props.taskTarget,
    onSuccess: (detail) => {
      props.onSuccess?.(detail);
      emit('success', detail);
    },
    onError: (message) => {
      props.onError?.(message);
      emit('error', message);
    },
  });
}
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
