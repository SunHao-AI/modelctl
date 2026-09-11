<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { AxiosError } from 'axios';
import ConfirmDialog from '@/components/common/ConfirmDialog.vue';
import {
  createClusterGoal,
  deleteClusterGoal,
  forceNodeSync,
  getClusterNodes,
  listClusterGoals,
  listClusterProfiles,
  retryGoal,
  updateClusterGoal,
  type GoalCreateResult,
  type GoalView,
  type NodeView,
  type ProfileCatalogEntry,
} from '@/api/cluster';

/**
 * 集群目标（M2，spec §4.1）：节点×profile 矩阵 + 两段式下发抽屉 + 行内动作。
 * 矩阵是总览（格=收敛色），动作在列表形态；profile 列 >8 强制降级列表。
 */
const router = useRouter();

const nodes = ref<NodeView[]>([]);
const goals = ref<GoalView[]>([]);
const catalog = ref<ProfileCatalogEntry[]>([]);
const disabled = ref(false);
const error = ref('');
const actionError = ref('');
let timer: number | undefined;

type Mode = 'matrix' | 'list';
const mode = ref<Mode>('matrix');
const filterState = ref<'all' | 'converged' | 'converging' | 'failed'>('all');
const filterNode = ref('');
const filterProfile = ref('');

const STAGE_STYLE: Record<string, string> = {
  converged: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  converging: 'bg-blue-500/15 text-blue-400 border-blue-500/30',
  failed: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
};
const NODE_STATUS_STYLE: Record<string, string> = {
  online: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  stale: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  offline: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
  disabled: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
};

/** Axios 错误 → 后端 detail 原文（展示纪律：失败给人看的永远是后端原话） */
function errText(e: unknown): string {
  const ax = e as AxiosError<{ detail?: string }>;
  return ax.response?.data?.detail || ax.message || String(e);
}

// 轮询防重叠：pending 期间新的 tick 直接返回，避免同 URL 请求被浏览器
// keep-alive 池 abort（见 docs/known-pitfalls/frontend/polling-overlap-err-aborted.md）
let pending: Promise<void> | null = null;

function refresh(): Promise<void> {
  if (pending) return pending;
  pending = (async () => {
    try {
      const [n, g] = await Promise.all([getClusterNodes(), listClusterGoals()]);
      nodes.value = n.nodes;
      goals.value = g.goals;
      disabled.value = false;
      error.value = '';
    } catch (e) {
      const err = e as AxiosError & { isCancel?: boolean };
      // 静默 abort：UI 无影响，保留旧 data
      if (
        !!err?.isCancel ||
        err?.code === 'ECONNABORTED' ||
        /aborted|canceled|cancelled/i.test(err?.message ?? '')
      ) {
        return;
      }
      if (err?.response?.status === 404) {
        disabled.value = true;
        return;
      }
      error.value = errText(e);
    } finally {
      pending = null;
    }
  })();
  return pending;
}

async function loadCatalog() {
  try {
    catalog.value = (await listClusterProfiles()).profiles;
  } catch {
    catalog.value = []; // 目录失败不拦主视图：抽屉打开时会重试
  }
}

function classify(g: GoalView): 'converged' | 'converging' | 'failed' {
  if (g.stage === 'FAILED' || g.error_class) return 'failed';
  const settled = g.intent === 'start' ? 'READY' : 'STOPPED';
  return g.stage === settled ? 'converged' : 'converging';
}

const matrixColumns = computed(() => {
  const names = [...new Set(goals.value.map((g) => g.profile))].sort();
  return filterProfile.value ? names.filter((p) => p === filterProfile.value) : names;
});
/** 列数失控对策（裁决 1）：>8 列强制列表形态，不给手动切回矩阵的入口 */
const effectiveMode = computed<Mode>(() => (matrixColumns.value.length > 8 ? 'list' : mode.value));

const filteredGoals = computed(() =>
  goals.value.filter((g) => {
    if (filterNode.value && g.node_id !== filterNode.value) return false;
    if (filterProfile.value && g.profile !== filterProfile.value) return false;
    if (filterState.value !== 'all' && classify(g) !== filterState.value) return false;
    return true;
  }),
);

function goalAt(nodeId: string, profile: string): GoalView | undefined {
  return goals.value.find((g) => g.node_id === nodeId && g.profile === profile);
}

// ---------------- 行内动作 ----------------
const removeTarget = ref<GoalView | null>(null);
const busy = ref(false);

async function rowStop(g: GoalView) {
  try {
    actionError.value = '';
    await updateClusterGoal(g.goal_id, { intent: 'stop' });
    await refresh();
  } catch (e) {
    actionError.value = errText(e);
  }
}

async function rowRemoveConfirm() {
  if (!removeTarget.value) return;
  busy.value = true;
  try {
    actionError.value = '';
    const out = await deleteClusterGoal(removeTarget.value.goal_id);
    if (out.missing.length) actionError.value = `撤销返回 missing：${out.missing.join(', ')}`;
    removeTarget.value = null;
    await refresh();
  } catch (e) {
    actionError.value = errText(e);
  } finally {
    busy.value = false;
  }
}

async function rowRetry(g: GoalView) {
  try {
    actionError.value = '';
    await retryGoal(g.goal_id);
    await refresh();
  } catch (e) {
    actionError.value = errText(e);
  }
}

async function rowSync(nodeId: string) {
  try {
    actionError.value = '';
    await forceNodeSync(nodeId);
  } catch (e) {
    actionError.value = errText(e);
  }
}

// ---------------- 两段式下发抽屉 ----------------
const drawer = ref(false);
const formName = ref('');
const formEngine = ref('');
const allNodes = ref(true);
const pickedNodes = ref<string[]>([]);
const intent = ref<'start' | 'stop'>('start');
const create = ref(true);
const envText = ref('');
const preview = ref<GoalCreateResult | null>(null);
/** 预览成功才点亮提交；表单任何改动经 @change/@input 调 resetPreview 复位（永不盲发） */
const previewOk = ref(false);
/** dry-run 请求序号：resetPreview/新预览递增，晚到的过期响应直接丢弃，防复活失效预览 */
let previewSeq = 0;
const drawerError = ref('');

const catalogNames = computed(() => [...new Set(catalog.value.map((p) => p.name))].sort());
const engineOptions = computed(() =>
  catalog.value.filter((p) => p.name === formName.value).map((p) => p.engine),
);
const needsEngine = computed(() => engineOptions.value.length > 1);
/** 同名多引擎才强制选边；单引擎留空串=不选边（选边歧义裁决留在 POST gate） */
const enginePayload = computed(() => (needsEngine.value ? formEngine.value : ''));

const envOverlayError = computed(() => {
  const t = envText.value.trim();
  if (!t) return '';
  try {
    const v: unknown = JSON.parse(t);
    if (typeof v !== 'object' || v === null || Array.isArray(v)) return 'env_overlay 必须是 JSON 对象';
    for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
      if (typeof val !== 'string') return `env_overlay.${k} 必须是字符串值`;
    }
    return '';
  } catch {
    return 'JSON 解析失败';
  }
});

const formValid = computed(
  () => !!formName.value && !envOverlayError.value &&
    (!needsEngine.value || !!formEngine.value) &&
    (allNodes.value || pickedNodes.value.length > 0),
);

function resetPreview() {
  previewSeq++; // 作废所有在途 dry-run：旧响应到达时 seq 校验不通过即被丢弃
  preview.value = null;
  previewOk.value = false;
}

async function openDrawer() {
  drawerError.value = '';
  drawer.value = true;
  if (!catalog.value.length) await loadCatalog();
}

function closeDrawer() {
  drawer.value = false;
  resetPreview();
}

function payload(dryRun: boolean) {
  return {
    profile: formName.value,
    engine: enginePayload.value,
    all_nodes: allNodes.value,
    node_ids: allNodes.value ? undefined : pickedNodes.value,
    intent: intent.value,
    create: create.value,
    env_overlay: envText.value.trim() ? (JSON.parse(envText.value) as Record<string, string>) : undefined,
    dry_run: dryRun,
  };
}

async function doPreview() {
  if (!formValid.value) return;
  drawerError.value = '';
  resetPreview();
  const seq = ++previewSeq;
  try {
    const out = await createClusterGoal(payload(true));
    if (seq !== previewSeq) return; // 过期响应：表单已改动或有更新的预览
    preview.value = out;
    previewOk.value = true;
  } catch (e) {
    if (seq !== previewSeq) return;
    drawerError.value = errText(e);
  }
}

async function doSubmit() {
  if (!previewOk.value) return;
  busy.value = true;
  drawerError.value = '';
  try {
    const out = await createClusterGoal(payload(false));
    closeDrawer();
    actionError.value = out.reason || '';
    await refresh();
  } catch (e) {
    drawerError.value = errText(e);
  } finally {
    busy.value = false;
  }
}

onMounted(() => {
  refresh();
  loadCatalog();
  timer = window.setInterval(refresh, 5000);
});
onBeforeUnmount(() => window.clearInterval(timer));
</script>

<template>
  <div class="p-6">
    <div class="mb-4 flex items-center justify-between">
      <h1 class="text-lg font-semibold text-slate-100">集群目标</h1>
      <div class="flex items-center gap-3">
        <span class="text-sm text-slate-400">{{ goals.length }} 个 goal · {{ nodes.length }} 节点</span>
        <button class="btn-primary" @click="openDrawer">新建下发</button>
      </div>
    </div>

    <div v-if="disabled" class="rounded-lg border border-slate-700 bg-slate-800/50 p-6 text-sm text-slate-400">
      当前节点未启用集群角色。中心机请在 .env 设置 CLUSTER_ROLE=both 后重启 webui。
    </div>

    <template v-else>
      <div v-if="error" class="mb-4 rounded-lg border border-rose-800 bg-rose-900/30 p-4 text-sm text-rose-300">
        {{ error }}
      </div>
      <div v-if="actionError" class="mb-4 rounded-lg border border-amber-800 bg-amber-900/30 p-4 text-sm text-amber-300">
        {{ actionError }}
      </div>

      <!-- 筛选器 -->
      <div class="mb-4 flex flex-wrap items-center gap-3 text-sm">
        <select v-model="filterState" class="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200">
          <option value="all">全部状态</option>
          <option value="converged">已收敛</option>
          <option value="converging">收敛中</option>
          <option value="failed">失败</option>
        </select>
        <select v-model="filterNode" class="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200">
          <option value="">全部节点</option>
          <option v-for="n in nodes" :key="n.node_id" :value="n.node_id">{{ n.node_id }}</option>
        </select>
        <select v-model="filterProfile" class="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200">
          <option value="">全部 profile</option>
          <option v-for="p in [...new Set(goals.map((g) => g.profile))].sort()" :key="p" :value="p">{{ p }}</option>
        </select>
        <button
          v-if="matrixColumns.length <= 8"
          class="btn-ghost"
          @click="mode = mode === 'matrix' ? 'list' : 'matrix'"
        >
          {{ mode === 'matrix' ? '切换列表' : '切换矩阵' }}
        </button>
        <span v-else class="text-xs text-slate-500">profile 列超过 8 个，已降级为列表形态</span>
      </div>

      <!-- 矩阵形态 -->
      <table v-if="effectiveMode === 'matrix' && filteredGoals.length" class="w-full text-left text-sm">
        <thead class="text-slate-400">
          <tr class="border-b border-slate-700">
            <th class="py-2 pr-4">节点</th>
            <th v-for="p in matrixColumns" :key="p" class="py-2 pr-4">{{ p }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="n in nodes" :key="n.node_id" class="border-b border-slate-800">
            <td class="py-2 pr-4">
              <router-link
                :to="`/cluster/nodes/${n.node_id}`"
                class="font-mono text-blue-400 hover:underline"
              >{{ n.node_id }}</router-link>
              <span class="ml-2 rounded border px-1.5 py-0.5 text-xs" :class="NODE_STATUS_STYLE[n.status] || NODE_STATUS_STYLE.offline">
                {{ n.status }}
              </span>
            </td>
            <td v-for="p in matrixColumns" :key="p" class="py-2 pr-4 align-top">
              <div
                v-if="goalAt(n.node_id, p)"
                class="cursor-pointer rounded border px-2 py-1 text-xs"
                :class="STAGE_STYLE[classify(goalAt(n.node_id, p)!)]"
                :title="`${goalAt(n.node_id, p)!.stage}${goalAt(n.node_id, p)!.reason ? ' · ' + goalAt(n.node_id, p)!.reason : ''}`"
                @click="router.push(`/cluster/nodes/${n.node_id}`)"
              >
                {{ goalAt(n.node_id, p)!.intent }} · {{ goalAt(n.node_id, p)!.stage }}
                <div v-if="goalAt(n.node_id, p)!.gpu?.length" class="text-slate-400">
                  gpu {{ goalAt(n.node_id, p)!.gpu!.join(',') }}<span v-if="goalAt(n.node_id, p)!.port"> :{{ goalAt(n.node_id, p)!.port }}</span>
                </div>
              </div>
              <span v-else class="text-xs text-slate-600">-</span>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- 列表形态（含 >8 列降级） -->
      <table v-else-if="filteredGoals.length" class="w-full text-left text-sm">
        <thead class="text-slate-400">
          <tr class="border-b border-slate-700">
            <th class="py-2 pr-4">节点</th>
            <th class="py-2 pr-4">profile</th>
            <th class="py-2 pr-4">engine</th>
            <th class="py-2 pr-4">intent</th>
            <th class="py-2 pr-4">stage</th>
            <th class="py-2 pr-4">实际</th>
            <th class="py-2 pr-4">gpu/端口</th>
            <th class="py-2 pr-4">更新时间</th>
            <th class="py-2">动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="g in filteredGoals" :key="g.goal_id" class="border-b border-slate-800 text-slate-200">
            <td class="py-2 pr-4">
              <router-link :to="`/cluster/nodes/${g.node_id}`" class="font-mono text-blue-400 hover:underline">{{ g.node_id }}</router-link>
            </td>
            <td class="py-2 pr-4 font-mono">{{ g.profile }}</td>
            <td class="py-2 pr-4 text-slate-400">{{ g.engine }}</td>
            <td class="py-2 pr-4">{{ g.intent }}</td>
            <td class="py-2 pr-4">
              <span class="rounded border px-2 py-0.5 text-xs" :class="STAGE_STYLE[classify(g)]">{{ g.stage }}</span>
              <div v-if="g.reason" class="max-w-64 truncate text-xs text-slate-500" :title="g.reason">{{ g.reason }}</div>
            </td>
            <td class="py-2 pr-4 text-slate-400">{{ g.state || '-' }}</td>
            <td class="py-2 pr-4 text-slate-400">{{ g.gpu?.join(',') || '-' }}<span v-if="g.port"> :{{ g.port }}</span></td>
            <td class="py-2 pr-4 text-slate-400">{{ g.updated_at }}</td>
            <td class="py-2 whitespace-nowrap">
              <button v-if="g.intent === 'start'" class="btn-ghost mr-1" @click="rowStop(g)">stop</button>
              <button v-if="g.stage === 'FAILED'" class="btn-ghost mr-1" @click="rowRetry(g)">retry</button>
              <button class="btn-ghost mr-1" @click="rowSync(g.node_id)">sync</button>
              <button class="btn-danger" @click="removeTarget = g">remove</button>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-else class="rounded-lg border border-slate-700 bg-slate-800/50 p-6 text-center text-sm text-slate-500">
        暂无匹配的 goal
      </div>
    </template>

    <!-- 下发抽屉（两段式：预览成功才点亮提交，永不盲发） -->
    <div v-if="drawer" class="fixed inset-0 z-40 flex justify-end bg-black/60" @click.self="closeDrawer">
      <div class="h-full w-full max-w-lg overflow-y-auto border-l border-slate-700 bg-slate-900 p-5">
        <div class="mb-4 flex items-center justify-between">
          <h2 class="text-base font-semibold text-slate-100">批量下发 goal</h2>
          <button class="text-xl text-slate-400 hover:text-slate-200" @click="closeDrawer">×</button>
        </div>

        <div class="space-y-3 text-sm">
          <label class="block">
            <span class="text-slate-400">profile</span>
            <select v-model="formName" class="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-slate-200" @change="formEngine = ''; resetPreview()">
              <option value="">请选择…</option>
              <option v-for="n in catalogNames" :key="n" :value="n">{{ n }}</option>
            </select>
          </label>

          <label v-if="needsEngine" class="block">
            <span class="text-amber-400">engine（同名 YAML 多引擎，必须选边）</span>
            <select v-model="formEngine" class="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-slate-200" @change="resetPreview">
              <option value="">请选择…</option>
              <option v-for="e in engineOptions" :key="e" :value="e">{{ e }}</option>
            </select>
          </label>

          <div>
            <label class="inline-flex items-center gap-2 text-slate-300">
              <input v-model="allNodes" type="checkbox" class="size-4 accent-blue-500" @change="resetPreview" />
              全部节点（--all）
            </label>
            <div v-if="!allNodes" class="mt-2 max-h-40 overflow-y-auto rounded border border-slate-700 bg-slate-950 p-2">
              <label v-for="n in nodes" :key="n.node_id" class="flex items-center gap-2 py-0.5 text-slate-300">
                <input v-model="pickedNodes" type="checkbox" :value="n.node_id" class="size-4 accent-blue-500" @change="resetPreview" />
                <span class="font-mono">{{ n.node_id }}</span>
                <span class="text-xs text-slate-500">{{ n.status }} · {{ n.capacity_text }}</span>
              </label>
            </div>
          </div>

          <div class="flex gap-4">
            <label class="flex-1">
              <span class="text-slate-400">intent</span>
              <select v-model="intent" class="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-slate-200" @change="resetPreview">
                <option value="start">start</option>
                <option value="stop">stop</option>
              </select>
            </label>
            <label class="mt-5 inline-flex items-center gap-2 text-slate-300">
              <input v-model="create" type="checkbox" class="size-4 accent-blue-500" @change="resetPreview" />
              允许新建（--create）
            </label>
          </div>

          <label class="block">
            <span class="text-slate-400">env_overlay（JSON 对象，可空）</span>
            <textarea
              v-model="envText"
              rows="3"
              class="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-xs text-slate-200"
              placeholder='{"MODEL_ROOT": "/mnt/nas"}'
              @input="resetPreview"
            />
            <span v-if="envOverlayError" class="text-xs text-rose-400">{{ envOverlayError }}</span>
          </label>

          <div v-if="drawerError" class="rounded border border-rose-800 bg-rose-900/30 p-3 text-xs text-rose-300">{{ drawerError }}</div>

          <!-- 预览区：gate 逐项 verdict 报告原文 -->
          <div v-if="preview" class="rounded border border-slate-700 bg-slate-950 p-3">
            <div class="mb-1 text-xs text-slate-400">
              预览（dry-run）：预计 created {{ preview.created }} · skipped {{ preview.skipped }} · errors {{ preview.errors }}
            </div>
            <pre class="max-h-56 overflow-auto whitespace-pre-wrap text-xs text-slate-300">{{ preview.report }}</pre>
          </div>

          <div class="flex justify-end gap-3 pt-2">
            <button class="btn-ghost" :disabled="!formValid || busy" @click="doPreview">预览（dry-run）</button>
            <button class="btn-primary" :disabled="!previewOk || busy" @click="doSubmit">确认提交</button>
          </div>
        </div>
      </div>
    </div>

    <ConfirmDialog
      :open="!!removeTarget"
      title="撤销 goal"
      :message="removeTarget ? `撤销 ${removeTarget.node_id} 上的 ${removeTarget.profile}？会撤 worker 本地 YAML、停模型并删台账。` : ''"
      danger
      confirm-text="撤销"
      :loading="busy"
      @confirm="rowRemoveConfirm"
      @cancel="removeTarget = null"
    />
  </div>
</template>
