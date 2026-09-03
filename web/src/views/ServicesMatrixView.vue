<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import {
  allRestart,
  allStart,
  allStop,
  allStatus,
  serviceAction,
  servicesInfo,
} from '@/api/services';
import type {
  AllStatusResponse,
  ServiceKey,
  ServicesResponse,
  TaskRef,
} from '@/api/types';
// 取自 @/api/services 是同一类型（re-export），仅保留 types 别名
import StatusBadge from '@/components/common/StatusBadge.vue';
import TaskButton from '@/components/common/TaskButton.vue';
import ConfirmDialog from '@/components/common/ConfirmDialog.vue';

/**
 * 服务矩阵：stats / gateway 两张卡片 + 家族路由预览 + 全部启停大按钮
 */
const data = ref<ServicesResponse | null>(null);
const allStatusData = ref<AllStatusResponse | null>(null);
const errMsg = ref('');
const stopNotice = ref('');
const allBusy = ref(false);
const pendingAll = ref<'start' | 'stop' | 'restart' | null>(null);

async function load() {
  try {
    const [services, all] = await Promise.all([servicesInfo(), allStatus()]);
    data.value = services;
    allStatusData.value = all;
    errMsg.value = '';
  } catch (err) {
    console.warn('services 加载失败:', err);
    errMsg.value = (err as { message?: string })?.message || '服务状态加载失败';
  }
}

onMounted(load);

/** serviceAction 在 start/restart 时返回 TaskRef，TS 联合类型需断言 */
function taskTargetFor(svc: ServiceKey, action: 'start' | 'restart'): () => Promise<TaskRef> {
  return () => serviceAction(svc, action).then((r) => r as TaskRef);
}

/** 停止单个服务（同步） */
async function stopSVC(svc: ServiceKey) {
  stopNotice.value = '';
  const label = svc === 'gateway' ? '网关' : '统计';
  try {
    const r = await serviceAction(svc, 'stop');
    // 同步 stop 只可能返回 ActionResponse
    const a = r as { ok?: boolean; detail?: string };
    stopNotice.value = `${label}：${a.detail || (a.ok ? '已停止' : '停止未确认')}`;
  } catch (err) {
    stopNotice.value = (err as { message?: string })?.message || '停止请求失败';
  } finally {
    void load();
  }
}

/** 全家启停（带确认） */
async function doAll(kind: 'start' | 'stop' | 'restart') {
  allBusy.value = true;
  pendingAll.value = null;
  stopNotice.value = '';
  try {
    if (kind === 'stop') {
      const r = await allStop();
      const errText = (r.errors ?? []).map((e) => e.component).join(', ');
      stopNotice.value = r.ok ? '全家已停止' : `全家部分失败：${errText}`;
    } else if (kind === 'start') {
      await allStart();
      stopNotice.value = '全家启动任务已提交（2s 后自动刷新）';
      setTimeout(() => void load(), 2500);
    } else {
      await allRestart();
      stopNotice.value = '全家重启任务已提交（2s 后自动刷新）';
      setTimeout(() => void load(), 2500);
    }
  } catch (err) {
    stopNotice.value = (err as { message?: string })?.message || '全家启停失败';
  } finally {
    allBusy.value = false;
    void load();
  }
}

/** 仅刷新全家状态 */
async function refreshAllStatus() {
  try {
    allStatusData.value = await allStatus();
    stopNotice.value = '';
  } catch (err) {
    console.warn('allStatus 失败:', err);
    stopNotice.value = (err as { message?: string })?.message || '状态刷新失败';
  }
}

/** 家族路由表（按 group 排序，按 priority 排序） */
const familyRows = computed<Array<{ group: string; members: Array<{ name: string; engine: string; priority: number; running: boolean }> }>>(() => {
  if (!data.value) return [];
  const out: Array<{ group: string; members: Array<{ name: string; engine: string; priority: number; running: boolean }> }> = [];
  for (const [group, members] of Object.entries(data.value.family_routing ?? {})) {
    out.push({ group, members: [...(members ?? [])].sort((a, b) => a.priority - b.priority) });
  }
  return out.sort((a, b) => a.group.localeCompare(b.group));
});
</script>

<template>
  <div class="space-y-4">
    <!-- 错误提示 -->
    <p v-if="errMsg" class="text-sm text-red-400">{{ errMsg }}</p>
    <p v-if="stopNotice" class="text-sm text-emerald-300">{{ stopNotice }}</p>

    <!-- 上部分：stats / gateway -->
    <div class="grid grid-cols-1 gap-4 md:grid-cols-2">
      <section
        v-if="data"
        :class="['card', data.stats.state === 'running' ? 'border-emerald-500/30' : '']"
      >
        <div class="mb-3 flex items-center justify-between">
          <h3 class="text-base font-semibold text-slate-100">stats</h3>
          <StatusBadge :state="data.stats.state" />
        </div>
        <div class="space-y-1 text-sm">
          <div class="flex items-center justify-between">
            <span class="text-slate-400">端口</span>
            <span class="font-mono text-slate-100">:{{ data.stats.port }}</span>
          </div>
          <div v-if="data.stats.detail" class="break-all text-xs text-slate-400">{{ data.stats.detail }}</div>
        </div>
        <div class="mt-4 flex items-center gap-2">
          <TaskButton label="启动" variant="ghost" :task-target="taskTargetFor('stats', 'start')" @success="() => load()" />
          <TaskButton label="重启" variant="ghost" :task-target="taskTargetFor('stats', 'restart')" @success="() => load()" />
          <button class="btn-danger" :disabled="data.stats.state !== 'running'" @click="stopSVC('stats')">停止</button>
        </div>
      </section>
      <section
        v-if="data"
        :class="['card', data.gateway.state === 'running' ? 'border-emerald-500/30' : '']"
      >
        <div class="mb-3 flex items-center justify-between">
          <h3 class="text-base font-semibold text-slate-100">gateway</h3>
          <StatusBadge :state="data.gateway.state" />
        </div>
        <div class="space-y-1 text-sm">
          <div class="flex items-center justify-between">
            <span class="text-slate-400">端口</span>
            <span class="font-mono text-slate-100">:{{ data.gateway.port }}</span>
          </div>
          <div v-if="data.gateway.detail" class="break-all text-xs text-slate-400">{{ data.gateway.detail }}</div>
        </div>
        <div class="mt-4 flex items-center gap-2">
          <TaskButton label="启动" variant="ghost" :task-target="taskTargetFor('gateway', 'start')" @success="() => load()" />
          <TaskButton label="重启" variant="ghost" :task-target="taskTargetFor('gateway', 'restart')" @success="() => load()" />
          <button class="btn-danger" :disabled="data.gateway.state !== 'running'" @click="stopSVC('gateway')">停止</button>
        </div>
      </section>
      <div v-else class="card text-sm text-slate-500">加载中…</div>
    </div>

    <!-- 家族路由预览 -->
    <section class="card !p-0">
      <div class="flex items-center justify-between border-b border-slate-800 px-3 py-2">
        <h3 class="text-sm font-semibold text-slate-100">家族路由预览</h3>
        <button class="btn-ghost !py-1 !px-2 text-xs" :disabled="allBusy" @click="load">刷新</button>
      </div>
      <div v-if="familyRows.length" class="divide-y divide-slate-800/50">
        <div v-for="row in familyRows" :key="row.group" class="space-y-1 px-3 py-2">
          <div class="text-xs font-semibold uppercase tracking-wider text-blue-300">{{ row.group }}</div>
          <table class="w-full text-sm">
            <tbody>
              <tr v-for="m in row.members" :key="m.name" class="border-b border-slate-800/30">
                <td class="w-1/2 py-1">
                  <span
                    class="mr-2 inline-block size-2 rounded-full"
                    :class="m.running ? 'bg-emerald-400' : 'bg-slate-600'"
                  />
                  <span class="font-medium text-slate-100">{{ m.name }}</span>
                </td>
                <td class="w-1/4 text-right font-mono text-slate-400">{{ m.engine }}</td>
                <td class="w-1/4 text-right font-mono text-slate-500">prio={{ m.priority }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
      <div v-else class="py-6 text-sm text-slate-500">尚无家族数据</div>
    </section>

    <!-- 全家启停 -->
    <section class="card">
      <h3 class="mb-3 text-xs font-medium uppercase tracking-wider text-slate-400">一键启停</h3>
      <div class="flex flex-wrap items-center gap-3">
        <button class="btn-primary" :disabled="allBusy || pendingAll !== null" @click="pendingAll = 'start'">全家启动</button>
        <button class="btn-danger" :disabled="allBusy || pendingAll !== null" @click="pendingAll = 'stop'">全家停止</button>
        <button class="btn-danger" :disabled="allBusy || pendingAll !== null" @click="pendingAll = 'restart'">全家重启</button>
        <button class="btn-ghost" :disabled="allBusy" @click="refreshAllStatus">刷新状态</button>
      </div>
      <!-- 全家状态 chip -->
      <div v-if="allStatusData" class="mt-3 flex flex-wrap gap-2">
        <span
          v-for="c in allStatusData.components"
          :key="c.component"
          :class="[
            'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs',
            c.status === 'ok' && 'bg-emerald-600/15 text-emerald-300 border border-emerald-500/30',
            c.status === 'skipped' && 'bg-slate-600/15 text-slate-300 border border-slate-500/30',
            c.status === 'error' && 'bg-red-600/15 text-red-300 border border-red-500/30',
          ]"
        >
          <span
            class="size-1.5 rounded-full"
            :class="c.status === 'ok' ? 'bg-emerald-400' : c.status === 'skipped' ? 'bg-slate-400' : 'bg-red-400'"
          />
          {{ c.component }}
        </span>
      </div>
    </section>

    <!-- 全家确认 -->
    <ConfirmDialog
      :open="pendingAll !== null"
      :title="pendingAll === 'stop' ? '一键停止' : pendingAll === 'restart' ? '一键重启' : '一键启动'"
      :message="pendingAll === 'stop'
        ? '确认停止 stats + gateway + 全部运行中的模型？'
        : pendingAll === 'restart'
          ? '确认重启 默认模型 + gateway + stats？该操作先停止后启动。'
          : '确认启动 默认模型 + gateway + stats？'"
      danger
      :loading="allBusy"
      :confirm-text="pendingAll ?? '确认'"
      @confirm="doAll(pendingAll!)"
      @cancel="pendingAll = null"
    />
  </div>
</template>
