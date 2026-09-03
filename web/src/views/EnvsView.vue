<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { envRemove, envSetup, envTargets } from '@/api/envs';
import type { EnvTarget, UnmanagedTarget } from '@/api/types';
import TaskButton from '@/components/common/TaskButton.vue';
import ConfirmDialog from '@/components/common/ConfirmDialog.vue';

/**
 * 环境管理：拉取 targets（venv 安装状态），Setup 走 TaskButton（异步 28min+），
 * Remove 用普通按钮 + ConfirmDialog
 */
const targets = ref<EnvTarget[]>([]);
/** 非托管引擎（原生/官方安装器），只在表格外做说明，不提供 Setup/Remove */
const unmanaged = ref<UnmanagedTarget[]>([]);
const errMsg = ref('');
const notice = ref('');
const loading = ref(true);

/** 待确认移除的 target 名 */
const pendingRemove = ref<string | null>(null);
const removeBusy = ref(false);

async function load() {
  loading.value = true;
  try {
    const r = await envTargets();
    targets.value = r.targets ?? [];
    unmanaged.value = r.unmanaged ?? [];
    errMsg.value = '';
  } catch (err) {
    console.warn('envTargets 失败:', err);
    errMsg.value = (err as { message?: string })?.message || '环境列表加载失败';
  } finally {
    loading.value = false;
  }
}

async function doRemove(target: string) {
  removeBusy.value = true;
  notice.value = '';
  try {
    const r = await envRemove(target);
    notice.value = `${target}：${r.detail || (r.ok ? '已移除' : '移除失败')}`;
    void load();
  } catch (err) {
    notice.value = (err as { message?: string })?.message || '移除失败';
  } finally {
    removeBusy.value = false;
    pendingRemove.value = null;
  }
}

onMounted(load);
</script>

<template>
  <div class="space-y-4">
    <!-- 工具条 -->
    <div class="flex items-center justify-between">
      <p class="text-sm text-slate-400">
        共 {{ targets.length }} 个受管目标（managed engine + gateway 共用仓）
      </p>
      <button class="btn-ghost" :disabled="loading" @click="load">刷新</button>
    </div>

    <p v-if="errMsg" class="text-sm text-red-400">{{ errMsg }}</p>
    <p v-if="notice" class="text-sm text-emerald-300">{{ notice }}</p>

    <!-- 表格 -->
    <section class="card !p-0 overflow-x-auto">
      <table v-if="targets.length" class="w-full text-sm">
        <thead class="bg-slate-800/40 text-left text-xs text-slate-400 uppercase tracking-wider">
          <tr>
            <th class="px-3 py-2">目标</th>
            <th class="px-3 py-2">状态</th>
            <th class="px-3 py-2">说明</th>
            <th class="px-3 py-2 text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in targets" :key="t.name" class="border-b border-slate-800/40">
            <td class="px-3 py-2 font-mono text-slate-100">{{ t.name }}</td>
            <td class="px-3 py-2">
              <span
                class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs"
                :class="t.installed
                  ? 'bg-emerald-600/15 text-emerald-300 border border-emerald-500/30'
                  : 'bg-slate-600/15 text-slate-400 border border-slate-500/30'"
              >
                <span class="size-1.5 rounded-full" :class="t.installed ? 'bg-emerald-400' : 'bg-slate-500'" />
                {{ t.installed ? '已安装' : '未安装' }}
              </span>
            </td>
            <td class="px-3 py-2 text-xs text-slate-400">{{ t.detail }}</td>
            <td class="px-3 py-2 text-right">
              <div class="flex items-center justify-end gap-2">
                <!-- 未安装可用 Setup（任务流，长耗时） -->
                <TaskButton
                  v-if="!t.installed"
                  label="Setup"
                  variant="primary"
                  :task-target="() => envSetup(t.name)"
                  @success="() => load()"
                  @error="(msg) => (notice = `${t.name} setup 失败：${msg}`)"
                />
                <!-- 已安装可用 Remove -->
                <button
                  v-else
                  class="btn-ghost !py-1 !px-2 text-xs"
                  :disabled="removeBusy"
                  @click="pendingRemove = t.name"
                >移除</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else-if="!loading" class="p-6 text-sm text-slate-500">尚无受管目标</div>
      <div v-else class="p-6 text-sm text-slate-500">加载中…</div>
    </section>

    <!-- 非托管引擎说明：原生二进制 / 官方安装器 / 源码编译，不建 venv 故不在上表 -->
    <section v-if="unmanaged.length" class="card space-y-2">
      <div class="flex items-baseline justify-between">
        <h2 class="text-sm font-medium text-slate-200">非托管引擎</h2>
        <span class="text-xs text-slate-500">原生或官方安装器，无需托管 venv</span>
      </div>
      <div v-for="u in unmanaged" :key="u.name" class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span class="font-mono text-slate-300">{{ u.name }}</span>
        <span
          class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5"
          :class="u.installed
            ? 'bg-emerald-600/15 text-emerald-300 border border-emerald-500/30'
            : 'bg-slate-600/15 text-slate-400 border border-slate-500/30'"
        >
          <span class="size-1.5 rounded-full" :class="u.installed ? 'bg-emerald-400' : 'bg-slate-500'" />
          {{ u.installed ? '已安装' : '未安装' }}
        </span>
        <span v-if="u.installed" class="break-all text-slate-500">{{ u.path }}</span>
        <code v-else class="break-all whitespace-pre-line text-slate-400">{{ u.install_hint }}</code>
      </div>
    </section>

    <!-- 移除确认 -->
    <ConfirmDialog
      :open="pendingRemove !== null"
      :title="`移除环境 ${pendingRemove ?? ''}`"
      :message="`确认移除 ${pendingRemove ?? ''}？底层 rmtree 删除目录，操作不可撤销。`"
      danger
      :loading="removeBusy"
      confirm-text="移除"
      @confirm="doRemove(pendingRemove!)"
      @cancel="pendingRemove = null"
    />
  </div>
</template>
