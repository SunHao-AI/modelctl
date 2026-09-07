<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRouter } from 'vue-router';
import { useTasksStore } from '@/stores/tasks';
import type { TaskRecord } from '@/stores/tasks';
import { useToastItems, toast } from '@/utils/toast';
import { envSetup, envTargets } from '@/api/envs';

/**
 * 全局任务抽屉：Header 经 ref 调 toggle/close 打开。
 * 列表数据源 = tasksStore.tasks（跨视图/刷新存活）；日志只读渲染 store 缓冲。
 * 同时作为全局 toast 渲染宿主（本组件常驻 Header）。
 */
const open = ref(false);
function toggle() {
  open.value = !open.value;
}
function close() {
  open.value = false;
}
defineExpose({ toggle, close });

/** 全局 toast 宿主 */
const { items: toastItems, KIND_CLASS: kindClass } = useToastItems();

const tasksStore = useTasksStore();
const tasks = computed(() => tasksStore.tasks);

const router = useRouter();

/** engine → 本平台可建托管 venv（懒加载一次 /envs；失败留空 → 保守"去环境页"） */
const platformMap = ref<Record<string, boolean>>({});
let platformRequested = false;
async function ensurePlatformMap() {
  if (platformRequested) return;
  platformRequested = true;
  try {
    const r = await envTargets();
    const m: Record<string, boolean> = {};
    for (const t of r.targets ?? []) m[t.name] = t.platform_supported;
    platformMap.value = m;
  } catch {
    /* 留空：fixAction 保守降级为 goto（跳转后由环境页展示真相） */
  }
}

/** 本会话已提交过"创建环境"的引擎（防连点；409 由后端 target 互斥兜底） */
const setupSubmitted = ref(new Set<string>());

/** 失败卡片修复动作：'' = 无 | 'setup' = 一键创建环境 | 'goto' = 跳环境页 */
function fixAction(t: TaskRecord): '' | 'setup' | 'goto' {
  if (t.status !== 'error' || !t.code) return '';
  if (t.code === 'docker_missing') return 'goto';
  if (t.code !== 'venv_missing' || !t.engine) return '';
  if (!(t.engine in platformMap.value)) {
    // 平台信息未到：先给"去环境页"，懒加载完成后响应式重算为 [创建环境]
    void ensurePlatformMap();
    return 'goto';
  }
  return platformMap.value[t.engine] ? 'setup' : 'goto';
}

function gotoEnvs(t: TaskRecord) {
  if (t.engine) void router.push({ path: '/envs', query: { focus: t.engine } });
  else void router.push('/envs');
}

async function onCreateEnv(t: TaskRecord) {
  const engine = t.engine;
  if (!engine || setupSubmitted.value.has(engine)) return;
  try {
    const refVal = await envSetup(engine);
    setupSubmitted.value.add(engine);
    // 复用全局任务链路：新 env_setup 任务进抽屉跟踪
    tasksStore.track(refVal, { target: engine, retryFn: () => envSetup(engine) });
  } catch (err) {
    if ((err as { response?: { status?: number } }).response?.status === 409) {
      toast.warning('该环境已有任务在执行中');
    } else {
      toast.error((err as { message?: string })?.message || '提交创建环境任务失败');
    }
  }
}

/** 当前展开查看日志的任务 id（至多一个） */
const expandedId = ref<string | null>(null);

/** 状态 → 中文标签 */
function statusLabel(s: TaskRecord['status']): string {
  switch (s) {
    case 'queued':
      return '准备中';
    case 'running':
      return '执行中';
    case 'success':
      return '已完成';
    case 'skipped':
      return '已跳过';
    case 'error':
      return '已失败';
  }
}

/** 状态点颜色 */
function statusDot(s: TaskRecord['status']): string {
  switch (s) {
    case 'queued':
      return 'bg-amber-400';
    case 'running':
      return 'bg-blue-400 animate-pulse';
    case 'success':
      return 'bg-emerald-400';
    case 'skipped':
      return 'bg-slate-400';
    case 'error':
      return 'bg-red-400';
  }
}

/** 耗时显示（startedAt ~ finishedAt；未结束算到当前） */
function duration(t: TaskRecord): string {
  if (!t.startedAt) return '';
  const start = new Date(t.startedAt).getTime();
  const end = t.finishedAt ? new Date(t.finishedAt).getTime() : Date.now();
  const sec = Math.max(0, Math.round((end - start) / 1000));
  if (sec < 60) return `${sec}s`;
  return `${Math.floor(sec / 60)}m${sec % 60}s`;
}

function onRetry(id: string) {
  void tasksStore.retry(id);
}
function onDismiss(id: string) {
  if (expandedId.value === id) expandedId.value = null;
  tasksStore.dismiss(id);
}
</script>

<template>
  <!-- 遮罩 -->
  <div v-if="open" class="fixed inset-0 z-40 bg-black/50" @click="close" />

  <!-- 抽屉本体：右侧滑出 -->
  <aside
    v-if="open"
    class="fixed right-0 top-0 z-50 flex h-full w-full max-w-md flex-col border-l border-slate-700 bg-slate-900 shadow-2xl"
  >
    <header class="flex items-center justify-between border-b border-slate-700 px-4 py-3">
      <h2 class="text-sm font-semibold text-slate-100">后台任务</h2>
      <button class="btn-ghost !px-2 !py-1 text-xs" @click="close">关闭</button>
    </header>

    <div class="flex-1 overflow-y-auto">
      <p v-if="!tasks.length" class="p-4 text-sm text-slate-500">暂无任务</p>
      <ul class="divide-y divide-slate-800">
        <li v-for="t in tasks" :key="t.id" class="px-4 py-3">
          <div class="flex items-center gap-2">
            <span :class="['size-2 shrink-0 rounded-full', statusDot(t.status)]" />
            <span class="min-w-0 flex-1 truncate font-mono text-sm text-slate-100">{{ t.target }}</span>
            <span class="shrink-0 text-xs text-slate-400">{{ statusLabel(t.status) }}</span>
            <span v-if="t.startedAt" class="shrink-0 text-xs text-slate-500">{{ duration(t) }}</span>
          </div>
          <p v-if="t.detail" class="mt-1 truncate text-xs text-slate-400">{{ t.detail }}</p>

          <!-- 操作行 -->
          <div class="mt-2 flex items-center gap-2">
            <button class="btn-ghost !px-2 !py-1 text-xs" @click="expandedId = expandedId === t.id ? null : t.id">
              {{ expandedId === t.id ? '收起日志' : '查看日志' }}
            </button>
            <button v-if="t.status === 'error' && t.retryFn" class="btn-ghost !px-2 !py-1 text-xs" @click="onRetry(t.id)">
              重试
            </button>
            <button v-if="t.status !== 'queued' && t.status !== 'running'" class="btn-ghost !px-2 !py-1 text-xs" @click="onDismiss(t.id)">
              忽略
            </button>
          </div>

          <!-- 修复动作：后端分类码驱动（仅 venv_missing/docker_missing 渲染，spec §4.4） -->
          <div v-if="fixAction(t)" class="mt-2 flex flex-wrap items-center gap-2">
            <template v-if="fixAction(t) === 'setup'">
              <button
                class="btn-primary !min-w-0 !px-2 !py-1 text-xs"
                :disabled="setupSubmitted.has(t.engine ?? '')"
                @click="onCreateEnv(t)"
              >
                {{ setupSubmitted.has(t.engine ?? '') ? '创建中…' : '创建环境' }}
              </button>
              <span class="text-xs text-slate-500">环境创建成功后，回到模型详情页点击启动</span>
            </template>
            <button v-else class="btn-ghost !px-2 !py-1 text-xs" @click="gotoEnvs(t)">去环境页</button>
          </div>

          <!-- 日志面板（数据源 = store，只读渲染） -->
          <pre
            v-if="expandedId === t.id"
            class="mt-2 max-h-64 overflow-y-auto rounded bg-[#0b1120] px-3 py-2 font-mono text-xs leading-5 text-slate-300 whitespace-pre-wrap break-all"
          >{{ t.logs.length ? t.logs.join('\n') : '（暂无日志）' }}</pre>
        </li>
      </ul>
    </div>
  </aside>

  <!-- 全局 toast 容器：与抽屉平级（多根节点），抽屉关闭时 toast 依然可见 -->
  <div class="fixed right-4 top-14 z-[60] flex w-80 flex-col gap-2">
    <div
      v-for="tt in toastItems"
      :key="tt.id"
      :class="['rounded-lg border px-3 py-2 text-sm shadow-lg backdrop-blur', kindClass[tt.kind]]"
    >
      {{ tt.message }}
    </div>
  </div>
</template>
