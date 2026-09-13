<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { listModels, stopModel } from '@/api/models';
import type { ModelInfo, ModelsGroup } from '@/api/types';
import StatusBadge from '@/components/common/StatusBadge.vue';
import ConfirmDialog from '@/components/common/ConfirmDialog.vue';
import DataTable from '@/components/common/DataTable.vue';

/**
 * 模型列表：拉 groups + 默认模型高亮；行内启停走 TaskButton（start/restart）/
 * 同步调用（stop）
 */
const router = useRouter();

const groups = ref<ModelsGroup[]>([]);
const defaultModel = ref('');
const errMsg = ref('');
const loading = ref(true);

/** 平铺模型（带 group），供表格渲染 */
const flat = computed<Array<ModelInfo & { group: string }>>(() => {
  const out: Array<ModelInfo & { group: string }> = [];
  for (const g of groups.value) {
    for (const m of g.models) out.push({ ...m, group: g.group });
  }
  return out;
});

/** 待确认停止的模型名 */
const pendingStop = ref<ModelInfo | null>(null);
/** 同步 stop 的提交中状态 */
const stopBusy = ref<boolean>(false);
/** 完成后提示 */
const notice = ref('');

async function load() {
  loading.value = true;
  try {
    const res = await listModels();
    groups.value = res.groups ?? [];
    defaultModel.value = res.default_model ?? '';
    errMsg.value = '';
  } catch (err) {
    console.warn('listModels 失败:', err);
    errMsg.value = (err as { message?: string })?.message || '模型列表加载失败';
  } finally {
    loading.value = false;
  }
}

async function doStop(name: string) {
  stopBusy.value = true;
  notice.value = '';
  try {
    const r = await stopModel(name);
    notice.value = `已请求停止 ${name}：${r.detail || (r.ok ? 'OK' : 'FAIL')}`;
  } catch (err) {
    console.warn('stopModel 失败:', err);
    notice.value = (err as { message?: string })?.message || '停止请求失败';
  } finally {
    stopBusy.value = false;
    pendingStop.value = null;
    // stop 同步返回，手动刷新一次
    void load();
  }
}

function isDefault(m: ModelInfo): boolean {
  return !!defaultModel.value && defaultModel.value === m.name;
}

function goDetail(m: ModelInfo) {
  router.push({ name: 'models-detail', params: { name: m.name } });
}

onMounted(load);
</script>

<template>
  <div class="space-y-4">
    <!-- 工具条 -->
    <div class="flex items-center justify-between">
      <p class="text-sm text-label3">
        <span class="text-label2">共 {{ flat.length }} 个模型 · {{ groups.length }} 个家族</span>
        <span v-if="defaultModel" class="ml-2 text-ok">默认：{{ defaultModel }}</span>
      </p>
      <button class="btn-ghost" :disabled="loading" @click="load">刷新</button>
    </div>

    <!-- 错误提示 -->
    <p v-if="errMsg" class="text-sm text-red-400">{{ errMsg }}</p>
    <p v-if="notice" class="text-sm text-ok">{{ notice }}</p>

    <!-- 表格 -->
    <DataTable v-if="flat.length">
      <table class="min-w-[52rem] text-sm">
        <thead>
          <tr>
            <th>状态</th>
            <th class="min-w-56">模型</th>
            <th>家族</th>
            <th>引擎</th>
            <th class="text-right">端口</th>
            <th>健康</th>
            <th class="text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="m in flat"
            :key="m.name"
            class="cursor-pointer"
            :class="isDefault(m) ? 'bg-ok-bg' : ''"
            @click="goDetail(m)"
          >
            <td><StatusBadge :state="m.state" /></td>
            <td>
              <div class="flex items-center gap-2">
                <span class="max-w-72 truncate font-medium text-label" :title="m.name">{{ m.name }}</span>
                <span
                  v-if="isDefault(m)"
                  class="shrink-0 rounded-full border border-ok-line bg-ok-bg px-1.5 py-0.5 text-[10px] text-ok"
                >默认</span>
              </div>
              <div v-if="m.aliases.length" class="mt-0.5 max-w-72 truncate font-mono text-xs text-label3" :title="m.aliases.join(', ')">
                aliases: {{ m.aliases.join(', ') }}
              </div>
            </td>
            <td class="text-label2">{{ m.group }}</td>
            <td class="font-mono text-label2">{{ m.engine }}</td>
            <td class="num text-right font-mono text-label2">{{ m.port || '—' }}</td>
            <td>
              <span
                class="text-xs"
                :class="
                  m.health === 'healthy'
                    ? 'text-ok'
                    : m.health === 'unhealthy'
                      ? 'text-danger'
                      : 'text-label3'
                "
              >
                {{ m.health || '—' }}
              </span>
            </td>
            <!-- 操作：stop 用确认，start/restart 用详情（详见 ModelDetailView） -->
            <td class="text-right whitespace-nowrap" @click.stop>
              <div class="flex items-center justify-end gap-2">
                <button
                  v-if="m.state === 'running'"
                  class="btn-ghost !py-1 !px-2 text-xs"
                  :disabled="stopBusy"
                  @click="pendingStop = m"
                >停止</button>
                <button
                  v-else
                  class="btn-ghost !py-1 !px-2 text-xs"
                  disabled
                  title="详见模型详情页（启动走任务流）"
                >启动</button>
                <router-link
                  class="btn-ghost !py-1 !px-2 text-xs"
                  :to="{ name: 'models-detail', params: { name: m.name } }"
                >详情</router-link>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </DataTable>
    <div v-else-if="!loading" class="card p-6 text-sm text-label3">尚无模型</div>
    <div v-else class="card p-6 text-sm text-label3">加载中…</div>

    <!-- 停止确认对话框 -->
    <ConfirmDialog
      :open="pendingStop !== null"
      :title="`停止模型 ${pendingStop?.name ?? ''}`"
      :message="`确认停止模型 ${pendingStop?.name ?? ''}？该操作会终止该模型的运行进程。`"
      danger
      :loading="stopBusy"
      confirm-text="停止"
      @confirm="doStop(pendingStop!.name)"
      @cancel="pendingStop = null"
    />
  </div>
</template>
