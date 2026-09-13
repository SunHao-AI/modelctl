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

/** 节点状态 → chip 类（与 ClusterNodesView 卡片范式一致） */
const STATUS_STYLE: Record<string, string> = {
  online: 'border-ok-line bg-ok-bg text-ok',
  stale: 'border-warn-line bg-warn-bg text-warn',
  offline: 'border-sep bg-surface3 text-label2',
  disabled: 'border-danger-line bg-danger-bg text-danger',
};

function errText(e: unknown): string {
  const ax = e as AxiosError<{ detail?: string }>;
  return ax.response?.data?.detail || ax.message || String(e);
}

function fmtEpoch(v: number | null): string {
  return v === null ? '-' : dayjs.unix(v).format('YYYY-MM-DD HH:mm:ss');
}

/** 轮询请求序号 + 节点快照：晚到的过期响应/错误直接丢弃，防串节点渲染 */
let refreshSeq = 0;

// 轮询防重叠：pending 期间新的 tick 直接返回，避免同 URL 请求被浏览器
// keep-alive 池 abort（见 docs/known-pitfalls/frontend/polling-overlap-err-aborted.md）
let pending: Promise<void> | null = null;

function refresh(): Promise<void> {
  if (pending) return pending;
  pending = (async () => {
    const seq = ++refreshSeq;
    const target = nodeId.value;
    try {
      const d = await getClusterNodeDetail(target);
      const ev = (await listClusterEvents({ node_id: target, limit: 100 })).events;
      if (seq !== refreshSeq || nodeId.value !== target) return; // 过期响应：已切节点或有更新的轮询
      detail.value = d;
      events.value = ev;
      notFound.value = '';
      error.value = '';
    } catch (e) {
      if (seq !== refreshSeq || nodeId.value !== target) return;
      const err = e as AxiosError & { isCancel?: boolean };
      // 静默 abort：UI 无影响，保留旧 data
      if (
        !!err?.isCancel ||
        err?.code === 'ECONNABORTED' ||
        /aborted|canceled|cancelled/i.test(err?.message ?? '')
      ) {
        return;
      }
      const st = err?.response?.status;
      if (st === 404) notFound.value = `节点 ${target} 不存在（或已退役）`;
      else error.value = errText(e);
    } finally {
      pending = null;
    }
  })();
  return pending;
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
        <h1 class="font-mono text-lg font-semibold text-label">{{ nodeId }}</h1>
        <span
          v-if="detail"
          class="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium"
          :class="STATUS_STYLE[detail.node.status] || STATUS_STYLE.offline"
        ><span class="stonedot bg-current" />{{ detail.node.status }}</span>
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

    <div v-if="notFound" class="rounded-ctl border border-sep bg-surface3 p-6 text-sm text-label2">
      {{ notFound }}
    </div>

    <template v-else>
      <div v-if="error" class="mb-4 rounded-ctl border border-danger-line bg-danger-bg p-4 text-sm text-danger">{{ error }}</div>
      <div v-if="actionError" class="mb-4 rounded-ctl border border-warn-line bg-warn-bg p-4 text-sm text-warn">{{ actionError }}</div>

      <!-- 一次性 token 面板：只活在组件内存里，关闭即丢，绝不落任何存储 -->
      <div v-if="rotateResult" class="mb-4 rounded-ctl border border-warn-line bg-warn-bg p-4 text-sm">
        <div class="mb-2 font-semibold text-warn">新节点令牌（仅此一次显示，关闭后无法找回）</div>
        <div class="flex items-center gap-3">
          <code class="flex-1 select-all break-all rounded-ctl bg-code-bg p-2 font-mono text-xs text-warn">{{ rotateResult.token }}</code>
          <button class="btn-ghost shrink-0" @click="copyToken">复制</button>
          <button class="btn-ghost shrink-0" @click="rotateResult = null; copyTip = ''">关闭</button>
        </div>
        <div class="mt-2 text-xs text-label2">{{ rotateResult.hint }}</div>
        <div v-if="copyTip" class="mt-1 text-xs text-label3">{{ copyTip }}</div>
      </div>

      <!-- 基础信息卡 -->
      <section v-if="detail" class="card mb-4">
        <h3 class="mb-3 text-sm font-semibold text-label">基础信息</h3>
        <dl class="grid grid-cols-2 gap-x-8 gap-y-2 text-sm md:grid-cols-3">
          <div><dt class="text-label3">LAN</dt><dd class="num text-label">{{ detail.node.lan_id || '-' }}</dd></div>
          <div><dt class="text-label3">角色</dt><dd class="text-label">{{ detail.node.role }}</dd></div>
          <div><dt class="text-label3">主机</dt><dd class="text-label">{{ detail.node.hostname || '-' }}（{{ detail.node.host_ip || '-' }}）</dd></div>
          <div><dt class="text-label3">容量</dt><dd class="num text-label">{{ detail.node.capacity_text || '-' }}</dd></div>
          <div><dt class="text-label3">令牌</dt><dd class="font-mono text-label2">{{ detail.node.token_mask }}</dd></div>
          <div><dt class="text-label3">最后心跳</dt><dd class="num text-label">{{ detail.node.since_seen_s === null ? '-' : detail.node.since_seen_s.toFixed(0) + 's 前' }}</dd></div>
          <div><dt class="text-label3">租约剩余</dt><dd class="num text-label">{{ detail.node.lease_left_s === null ? '-' : Math.max(detail.node.lease_left_s, 0).toFixed(0) + 's' }}</dd></div>
          <div class="col-span-2">
            <dt class="text-label3">引擎</dt>
            <dd class="text-label">
              <span v-if="!detail.node.engines">-</span>
              <span v-for="(ver, eng) in detail.node.engines || {}" :key="eng" class="mr-3">{{ eng }}: {{ ver || '未知' }}</span>
            </dd>
          </div>
        </dl>
      </section>

      <!-- goals 表 -->
      <section v-if="detail" class="card mb-4">
        <h3 class="mb-3 text-sm font-semibold text-label">托管目标（{{ detail.goals.length }}）</h3>
        <table class="w-full text-left text-sm">
          <thead class="text-label2">
            <tr class="border-b border-sep">
              <th class="py-2 pr-4">profile</th><th class="py-2 pr-4">engine</th>
              <th class="py-2 pr-4">intent</th><th class="py-2 pr-4">stage</th>
              <th class="py-2 pr-4">实际</th><th class="py-2">gpu/端口</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="g in detail.goals" :key="g.goal_id" class="border-b border-sep text-label">
              <td class="py-2 pr-4 font-mono">{{ g.profile }}</td>
              <td class="py-2 pr-4 text-label2">{{ g.engine }}</td>
              <td class="py-2 pr-4">{{ g.intent }}</td>
              <td class="py-2 pr-4">
                {{ g.stage }}
                <div v-if="g.reason" class="max-w-72 truncate text-xs text-label3" :title="g.reason">{{ g.reason }}</div>
              </td>
              <td class="py-2 pr-4 text-label2">{{ g.state || '-' }}</td>
              <td class="num py-2">{{ g.gpu?.join(',') || '-' }}<span v-if="g.port"> :{{ g.port }}</span></td>
            </tr>
            <tr v-if="!detail.goals.length"><td colspan="6" class="py-4 text-center text-label3">该节点暂无托管目标</td></tr>
          </tbody>
        </table>
      </section>

      <!-- model_states 表 -->
      <section v-if="detail" class="card mb-4">
        <h3 class="mb-3 text-sm font-semibold text-label">运行事实（worker 回流）</h3>
        <table class="w-full text-left text-sm">
          <thead class="text-label2">
            <tr class="border-b border-sep">
              <th class="py-2 pr-4">profile</th><th class="py-2 pr-4">engine</th>
              <th class="py-2 pr-4">state</th><th class="py-2 pr-4">gpu</th>
              <th class="py-2 pr-4">端口</th>
              <th class="py-2 pr-4">endpoint</th><th class="py-2">更新于</th>
            </tr>
          </thead>
          <tbody>
            <!-- 同 stem 多引擎共存时，(profile, engine) 才是唯一行键：旧 worker 心跳为 ''，
                 仍按该行键唯一。:key 用 profile|engine 拼接避免 stale vnode。 -->
            <tr
              v-for="m in detail.model_states"
              :key="`${m.profile}__${m.engine ?? ''}`"
              class="border-b border-sep text-label"
            >
              <td class="py-2 pr-4 font-mono">{{ m.profile }}</td>
              <td class="py-2 pr-4 text-label2">{{ m.engine || '-' }}</td>
              <td class="py-2 pr-4">{{ m.state }}</td>
              <td class="num py-2 pr-4 text-label2">{{ m.gpu?.join(',') || '-' }}</td>
              <td class="num py-2 pr-4 text-label2">{{ m.port ?? '-' }}</td>
              <td class="py-2 pr-4 font-mono text-xs text-label2">{{ m.endpoint_url || '-' }}</td>
              <td class="num py-2 text-label2">{{ fmtEpoch(m.updated_at) }}</td>
            </tr>
            <tr v-if="!detail.model_states.length"><td colspan="7" class="py-4 text-center text-label3">该节点当前无在跑模型上报</td></tr>
          </tbody>
        </table>
      </section>

      <!-- 事件流（REST 10s 轮询；后端已拼好 text，前端零加工） -->
      <section class="card">
        <h3 class="mb-3 text-sm font-semibold text-label">事件流（最近 100 条 · 10s 轮询）</h3>
        <div class="max-h-96 overflow-y-auto rounded-ctl bg-code-bg p-3 font-mono text-xs text-code-fg">
          <div v-for="(e, i) in events" :key="i" class="flex gap-3 border-b border-code-line py-1">
            <span class="num shrink-0 text-code-fg opacity-60">{{ e.ts }}</span>
            <span class="shrink-0 text-accent">{{ e.kind }}</span>
            <span class="break-all">{{ e.text }}</span>
          </div>
          <div v-if="!events.length" class="py-4 text-center text-code-fg opacity-60">暂无事件</div>
        </div>
      </section>
    </template>

    <!-- 退役确认：自绘模态（ConfirmDialog 无输入插槽），输入 node_id 完全匹配才点亮红按钮 -->
    <div v-if="retireOpen" class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm" @click.self="retireOpen = false">
      <div class="w-full max-w-md rounded-panel border border-sep bg-surface2 shadow-l">
        <div class="border-b border-sep-soft px-5 py-3">
          <h3 class="text-base font-semibold text-danger">退役节点 {{ nodeId }}</h3>
        </div>
        <div class="space-y-3 px-5 py-4 text-sm text-label2">
          <p>将从台账删除该节点及其全部运行事实，并连带撤销 <b class="text-danger">{{ detail?.goals.length ?? 0 }}</b> 个托管 goal（worker 下拍剪文件、停模型）。事件历史保留。</p>
          <label class="block">
            <span class="text-xs text-label3">输入节点 ID <code class="font-mono text-label2">{{ nodeId }}</code> 以确认</span>
            <input
              v-model="retireInput"
              class="mt-1 w-full rounded-ctl border border-sep bg-surface3 px-3 py-1.5 font-mono text-sm text-label"
            />
          </label>
        </div>
        <div class="flex justify-end gap-3 border-t border-sep-soft px-5 py-3">
          <button class="btn-ghost" :disabled="busy" @click="retireOpen = false">取消</button>
          <button class="btn-danger" :disabled="!retireOk || busy" @click="onRetireConfirm">确认退役</button>
        </div>
      </div>
    </div>
  </div>
</template>
