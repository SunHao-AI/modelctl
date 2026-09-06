<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { AxiosError } from 'axios';
import dayjs from 'dayjs';
import {
  disableNode,
  enableNode,
  forceNodeSync,
  getClusterNodeDetail,
  kickNode,
  listClusterEvents,
  retireNode,
  rotateNodeToken,
  type EventRow,
  type NodeDetail,
} from '@/api/cluster';

/**
 * 节点详情（M2，spec §4.2）：基础信息 + goals/model_states + 事件流（10s 轮询）
 * + 治理按钮组（sync / disable|enable / rotate-token / kick / retire）。
 */
const route = useRoute();
const router = useRouter();
const nodeId = computed(() => String(route.params.id || ''));

const detail = ref<NodeDetail | null>(null);
const events = ref<EventRow[]>([]);
const notFound = ref('');
const error = ref('');
const actionError = ref('');
const busy = ref(false);
let timer: number | undefined;

const STATUS_STYLE: Record<string, string> = {
  online: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  stale: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  offline: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
  disabled: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
};

function errText(e: unknown): string {
  const ax = e as AxiosError<{ detail?: string }>;
  return ax.response?.data?.detail || ax.message || String(e);
}

function fmtEpoch(v: number | null): string {
  return v === null ? '-' : dayjs.unix(v).format('YYYY-MM-DD HH:mm:ss');
}

async function refresh() {
  try {
    detail.value = await getClusterNodeDetail(nodeId.value);
    events.value = (await listClusterEvents({ node_id: nodeId.value, limit: 100 })).events;
    notFound.value = '';
    error.value = '';
  } catch (e) {
    const st = (e as AxiosError).response?.status;
    if (st === 404) notFound.value = `节点 ${nodeId.value} 不存在（或已退役）`;
    else error.value = errText(e);
  }
}

async function runAction(fn: () => Promise<unknown>) {
  if (busy.value) return;
  busy.value = true;
  actionError.value = '';
  try {
    await fn();
    await refresh();
  } catch (e) {
    actionError.value = errText(e);
  } finally {
    busy.value = false;
  }
}

// ---------------- 治理动作 ----------------
const rotateResult = ref<{ token: string; hint: string } | null>(null);
const copyTip = ref('');

function onRotate() {
  void runAction(async () => {
    const out = await rotateNodeToken(nodeId.value);
    rotateResult.value = { token: out.node_token, hint: out.hint };
  });
}

async function copyToken() {
  if (!rotateResult.value) return;
  try {
    await navigator.clipboard.writeText(rotateResult.value.token);
    copyTip.value = '已复制到剪贴板';
  } catch {
    copyTip.value = '剪贴板不可用，请手选复制';
  }
}

const retireOpen = ref(false);
const retireInput = ref('');
const retireOk = computed(() => retireInput.value === nodeId.value && !!nodeId.value);

function onRetireConfirm() {
  if (!retireOk.value) return;
  void runAction(async () => {
    await retireNode(nodeId.value);
    retireOpen.value = false;
    await router.push('/cluster/nodes');
  });
}

onMounted(() => {
  refresh();
  timer = window.setInterval(refresh, 10_000);
});
onBeforeUnmount(() => window.clearInterval(timer));
</script>

<template>
  <div class="p-6">
    <div class="mb-4 flex items-center justify-between">
      <div class="flex items-center gap-3">
        <button class="btn-ghost" @click="router.push('/cluster/nodes')">← 节点列表</button>
        <h1 class="font-mono text-lg font-semibold text-slate-100">{{ nodeId }}</h1>
        <span
          v-if="detail"
          class="rounded border px-2 py-0.5 text-xs"
          :class="STATUS_STYLE[detail.node.status] || STATUS_STYLE.offline"
        >{{ detail.node.status }}</span>
      </div>
      <div v-if="detail" class="flex flex-wrap items-center justify-end gap-2">
        <button class="btn-ghost" :disabled="busy" @click="runAction(() => forceNodeSync(nodeId))">重新 sync</button>
        <button
          v-if="detail.node.status !== 'disabled'"
          class="btn-ghost"
          :disabled="busy"
          @click="runAction(() => disableNode(nodeId))"
        >禁用</button>
        <button v-else class="btn-ghost" :disabled="busy" @click="runAction(() => enableNode(nodeId))">启用</button>
        <button class="btn-ghost" :disabled="busy" @click="onRotate">轮换 token</button>
        <button class="btn-ghost" :disabled="busy" @click="runAction(() => kickNode(nodeId))">踢除</button>
        <button class="btn-danger" :disabled="busy" @click="retireInput = ''; retireOpen = true">退役</button>
      </div>
    </div>

    <div v-if="notFound" class="rounded-lg border border-slate-700 bg-slate-800/50 p-6 text-sm text-slate-400">
      {{ notFound }}
    </div>

    <template v-else>
      <div v-if="error" class="mb-4 rounded-lg border border-rose-800 bg-rose-900/30 p-4 text-sm text-rose-300">{{ error }}</div>
      <div v-if="actionError" class="mb-4 rounded-lg border border-amber-800 bg-amber-900/30 p-4 text-sm text-amber-300">{{ actionError }}</div>

      <!-- 一次性 token 面板：只活在组件内存里，关闭即丢，绝不落任何存储 -->
      <div v-if="rotateResult" class="mb-4 rounded-lg border border-amber-700 bg-amber-950/40 p-4 text-sm">
        <div class="mb-2 font-semibold text-amber-300">新节点令牌（仅此一次显示，关闭后无法找回）</div>
        <div class="flex items-center gap-3">
          <code class="flex-1 select-all break-all rounded bg-slate-950 p-2 font-mono text-xs text-amber-200">{{ rotateResult.token }}</code>
          <button class="btn-ghost shrink-0" @click="copyToken">复制</button>
          <button class="btn-ghost shrink-0" @click="rotateResult = null; copyTip = ''">关闭</button>
        </div>
        <div class="mt-2 text-xs text-slate-400">{{ rotateResult.hint }}</div>
        <div v-if="copyTip" class="mt-1 text-xs text-slate-500">{{ copyTip }}</div>
      </div>

      <!-- 基础信息卡 -->
      <section v-if="detail" class="card mb-4">
        <h3 class="mb-3 text-sm font-semibold text-slate-100">基础信息</h3>
        <dl class="grid grid-cols-2 gap-x-8 gap-y-2 text-sm md:grid-cols-3">
          <div><dt class="text-slate-500">LAN</dt><dd class="text-slate-200">{{ detail.node.lan_id || '-' }}</dd></div>
          <div><dt class="text-slate-500">角色</dt><dd class="text-slate-200">{{ detail.node.role }}</dd></div>
          <div><dt class="text-slate-500">主机</dt><dd class="text-slate-200">{{ detail.node.hostname || '-' }}（{{ detail.node.host_ip || '-' }}）</dd></div>
          <div><dt class="text-slate-500">容量</dt><dd class="text-slate-200">{{ detail.node.capacity_text || '-' }}</dd></div>
          <div><dt class="text-slate-500">令牌</dt><dd class="font-mono text-slate-400">{{ detail.node.token_mask }}</dd></div>
          <div><dt class="text-slate-500">最后心跳</dt><dd class="text-slate-200">{{ detail.node.since_seen_s === null ? '-' : detail.node.since_seen_s.toFixed(0) + 's 前' }}</dd></div>
          <div><dt class="text-slate-500">租约剩余</dt><dd class="text-slate-200">{{ detail.node.lease_left_s === null ? '-' : Math.max(detail.node.lease_left_s, 0).toFixed(0) + 's' }}</dd></div>
          <div class="col-span-2">
            <dt class="text-slate-500">引擎</dt>
            <dd class="text-slate-200">
              <span v-if="!detail.node.engines">-</span>
              <span v-for="(ver, eng) in detail.node.engines || {}" :key="eng" class="mr-3">{{ eng }}: {{ ver || '未知' }}</span>
            </dd>
          </div>
        </dl>
      </section>

      <!-- goals 表 -->
      <section v-if="detail" class="card mb-4">
        <h3 class="mb-3 text-sm font-semibold text-slate-100">托管目标（{{ detail.goals.length }}）</h3>
        <table class="w-full text-left text-sm">
          <thead class="text-slate-400">
            <tr class="border-b border-slate-700">
              <th class="py-2 pr-4">profile</th><th class="py-2 pr-4">engine</th>
              <th class="py-2 pr-4">intent</th><th class="py-2 pr-4">stage</th>
              <th class="py-2 pr-4">实际</th><th class="py-2">gpu/端口</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="g in detail.goals" :key="g.goal_id" class="border-b border-slate-800 text-slate-200">
              <td class="py-2 pr-4 font-mono">{{ g.profile }}</td>
              <td class="py-2 pr-4 text-slate-400">{{ g.engine }}</td>
              <td class="py-2 pr-4">{{ g.intent }}</td>
              <td class="py-2 pr-4">
                {{ g.stage }}
                <div v-if="g.reason" class="max-w-72 truncate text-xs text-slate-500" :title="g.reason">{{ g.reason }}</div>
              </td>
              <td class="py-2 pr-4 text-slate-400">{{ g.state || '-' }}</td>
              <td class="py-2">{{ g.gpu?.join(',') || '-' }}<span v-if="g.port"> :{{ g.port }}</span></td>
            </tr>
            <tr v-if="!detail.goals.length"><td colspan="6" class="py-4 text-center text-slate-500">该节点暂无托管目标</td></tr>
          </tbody>
        </table>
      </section>

      <!-- model_states 表 -->
      <section v-if="detail" class="card mb-4">
        <h3 class="mb-3 text-sm font-semibold text-slate-100">运行事实（worker 回流）</h3>
        <table class="w-full text-left text-sm">
          <thead class="text-slate-400">
            <tr class="border-b border-slate-700">
              <th class="py-2 pr-4">profile</th><th class="py-2 pr-4">state</th>
              <th class="py-2 pr-4">gpu</th><th class="py-2 pr-4">端口</th>
              <th class="py-2 pr-4">endpoint</th><th class="py-2">更新于</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in detail.model_states" :key="m.profile" class="border-b border-slate-800 text-slate-200">
              <td class="py-2 pr-4 font-mono">{{ m.profile }}</td>
              <td class="py-2 pr-4">{{ m.state }}</td>
              <td class="py-2 pr-4 text-slate-400">{{ m.gpu?.join(',') || '-' }}</td>
              <td class="py-2 pr-4 text-slate-400">{{ m.port ?? '-' }}</td>
              <td class="py-2 pr-4 font-mono text-xs text-slate-400">{{ m.endpoint_url || '-' }}</td>
              <td class="py-2 text-slate-400">{{ fmtEpoch(m.updated_at) }}</td>
            </tr>
            <tr v-if="!detail.model_states.length"><td colspan="6" class="py-4 text-center text-slate-500">该节点当前无在跑模型上报</td></tr>
          </tbody>
        </table>
      </section>

      <!-- 事件流（REST 10s 轮询；后端已拼好 text，前端零加工） -->
      <section class="card">
        <h3 class="mb-3 text-sm font-semibold text-slate-100">事件流（最近 100 条 · 10s 轮询）</h3>
        <div class="max-h-96 overflow-y-auto rounded bg-slate-950 p-3 font-mono text-xs">
          <div v-for="(e, i) in events" :key="i" class="flex gap-3 border-b border-slate-900 py-1">
            <span class="shrink-0 text-slate-500">{{ e.ts }}</span>
            <span class="shrink-0 text-blue-400">{{ e.kind }}</span>
            <span class="break-all text-slate-300">{{ e.text }}</span>
          </div>
          <div v-if="!events.length" class="py-4 text-center text-slate-500">暂无事件</div>
        </div>
      </section>
    </template>

    <!-- 退役确认：自绘模态（ConfirmDialog 无输入插槽），输入 node_id 完全匹配才点亮红按钮 -->
    <div v-if="retireOpen" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm" @click.self="retireOpen = false">
      <div class="w-full max-w-md rounded-lg border border-slate-700 bg-slate-900 shadow-xl">
        <div class="border-b border-slate-800 px-5 py-3">
          <h3 class="text-base font-semibold text-red-300">退役节点 {{ nodeId }}</h3>
        </div>
        <div class="space-y-3 px-5 py-4 text-sm text-slate-300">
          <p>将从台账删除该节点及其全部运行事实，并连带撤销 <b class="text-red-300">{{ detail?.goals.length ?? 0 }}</b> 个托管 goal（worker 下拍剪文件、停模型）。事件历史保留。</p>
          <label class="block">
            <span class="text-xs text-slate-500">输入节点 ID <code class="font-mono text-slate-300">{{ nodeId }}</code> 以确认</span>
            <input
              v-model="retireInput"
              class="mt-1 w-full rounded border border-slate-600 bg-slate-950 px-3 py-1.5 font-mono text-sm text-slate-100"
            />
          </label>
        </div>
        <div class="flex justify-end gap-3 border-t border-slate-800 px-5 py-3">
          <button class="btn-ghost" :disabled="busy" @click="retireOpen = false">取消</button>
          <button class="btn-danger" :disabled="!retireOk || busy" @click="onRetireConfirm">确认退役</button>
        </div>
      </div>
    </div>
  </div>
</template>
