<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { AxiosError } from 'axios';
import { useAuthStore } from '@/stores/auth';
import {
  listKeys, createKey, updateKey, deleteKey,
  getUsage, listSessions, exportSession, deleteSession, searchMessages,
} from '@/api/accountSelf';
import type { SelfKey, SelfSession, SelfUsage, SelfMessage } from '@/api/accountSelf';
import ConfirmDialog from '@/components/common/ConfirmDialog.vue';
import { fmtEpoch, fmtTokens } from '@/utils/time';

const auth = useAuthStore();
const router = useRouter();

const profile = computed(() => auth.accountProfile);
const displayName = computed(() =>
  (profile.value?.display_name || profile.value?.username || '账号'));

// ---------------------------------------------------------------------------
// Tab 切换
// ---------------------------------------------------------------------------

type TabKey = 'keys' | 'usage' | 'sessions';
const tab = ref<TabKey>('keys');

async function onLogout() {
  auth.clearAccountSession();
  router.push('/account/login');
}

// ---------------------------------------------------------------------------
// 通用错误处理：优先 detail.message, 退 detail, 再退 axios message
// ---------------------------------------------------------------------------

function errText(e: unknown): string {
  const ax = e as AxiosError<{ detail?: string | { message?: string } }>;
  const d = ax.response?.data?.detail;
  if (typeof d === 'string') return d;
  if (d && typeof d === 'object' && 'message' in d) return (d as { message: string }).message;
  return ax.message || String(e);
}

const errMsg = ref('');
const notice = ref('');
function pickDetail(e: unknown): { code?: string; message?: string; http?: number } {
  const ax = e as AxiosError<{ detail?: unknown }>;
  const d = ax.response?.data?.detail;
  if (d && typeof d === 'object' && 'code' in d) {
    const o = d as { code?: string; message?: string };
    return { code: o.code, message: o.message, http: ax.response?.status };
  }
  return { http: ax.response?.status, message: (typeof d === 'string') ? d : undefined };
}
function toastError(e: unknown, fallback: string) {
  const d = pickDetail(e);
  errMsg.value = d.message || fallback;
  notice.value = '';
  // 503 已在管理面板语义下就绪（accounts 未启用）：给出"联系管理员"引导
  if (d.code === 'accounts_disabled') {
    errMsg.value += ' —— 请联系管理员在后端启用账号体系';
  }
}

// ---------------------------------------------------------------------------
// Keys 面板
// ---------------------------------------------------------------------------

const keys = ref<SelfKey[]>([]);
const keysLoading = ref(false);
const showNewKey = ref(false);
const newKeyName = ref('');
const newKeyExpireDays = ref<number | ''>('');
const newKeyErr = ref('');
const newKeyBusy = ref(false);
/** 一次性明文：签发后弹层展示，用户"知悉"后收起；关闭后不再显示 */
const issuedKey = ref<string | null>(null);
const issuedKeyCtx = ref<{ name: string; id: number } | null>(null);

const pendingKeyOp = ref<{ kind: 'status' | 'delete'; key: SelfKey; status?: string } | null>(null);
const keyOpBusy = ref(false);

async function loadKeys() {
  keysLoading.value = true;
  errMsg.value = '';
  try {
    keys.value = await listKeys();
  } catch (e) {
    toastError(e, 'Key 列表加载失败');
  } finally {
    keysLoading.value = false;
  }
}

async function onIssue() {
  const name = newKeyName.value.trim();
  const days = newKeyExpireDays.value === '' ? null : Number(newKeyExpireDays.value);
  if (days !== null && (!Number.isFinite(days) || days < 0)) {
    newKeyErr.value = '有效期必须为非负数字（天）';
    return;
  }
  newKeyBusy.value = true;
  newKeyErr.value = '';
  try {
    const r = await createKey({ name, expires_in_days: days });
    issuedKey.value = r.key || '';
    issuedKeyCtx.value = { name: r.name || name || '(未命名)', id: r.id };
    showNewKey.value = false;
    notice.value = `Key ${r.key_prefix || r.id} 已签发（明文仅此次可见）`;
    newKeyName.value = '';
    void loadKeys();
  } catch (e) {
    newKeyErr.value = errText(e);
  } finally {
    newKeyBusy.value = false;
  }
}

function closeIssued() {
  issuedKey.value = null;
  issuedKeyCtx.value = null;
}

async function doKeyOp(op: typeof pendingKeyOp.value) {
  if (!op) return;
  keyOpBusy.value = true;
  errMsg.value = '';
  try {
    if (op.kind === 'status' && op.status) {
      await updateKey(op.key.id, op.status);
      notice.value = `Key ${op.key.key_prefix || op.key.id} → ${op.status}`;
    } else if (op.kind === 'delete') {
      await deleteKey(op.key.id);
      notice.value = `Key ${op.key.key_prefix || op.key.id} 已删除`;
    }
    void loadKeys();
  } catch (e) {
    toastError(e, 'Key 操作失败');
  } finally {
    keyOpBusy.value = false;
    pendingKeyOp.value = null;
  }
}

// ---------------------------------------------------------------------------
// 用量面板
// ---------------------------------------------------------------------------

const usage = ref<SelfUsage | null>(null);
const usageLoading = ref(false);

async function loadUsage() {
  usageLoading.value = true;
  errMsg.value = '';
  try {
    usage.value = await getUsage();
  } catch (e) {
    toastError(e, '用量加载失败');
  } finally {
    usageLoading.value = false;
  }
}

// ---------------------------------------------------------------------------
// 会话面板
// ---------------------------------------------------------------------------

const sessions = ref<SelfSession[]>([]);
const sessionsQ = ref('');
const sessionsLoading = ref(false);
const showSearch = ref(false);
const searchQ = ref('');
const searchBusy = ref(false);
const searchRes = ref<SelfMessage[] | null>(null);
const searchErr = ref('');

const openSession = ref<SelfSession | null>(null);
const messages = ref<SelfMessage[] | null>(null);
const sessionDetailBusy = ref(false);
const pendingDeleteSession = ref<SelfSession | null>(null);
const sessionDelBusy = ref(false);

async function loadSessions() {
  sessionsLoading.value = true;
  errMsg.value = '';
  try {
    sessions.value = await listSessions(sessionsQ.value.trim() || undefined);
  } catch (e) {
    toastError(e, '会话列表加载失败');
  } finally {
    sessionsLoading.value = false;
  }
}

async function onSearch() {
  const q = searchQ.value.trim();
  if (!q) {
    searchErr.value = '搜索关键词不能为空';
    return;
  }
  searchBusy.value = true;
  searchErr.value = '';
  searchRes.value = null;
  try {
    searchRes.value = await searchMessages(q);
  } catch (e) {
    searchErr.value = errText(e);
  } finally {
    searchBusy.value = false;
  }
}

async function openSessionDetail(s: SelfSession) {
  openSession.value = s;
  messages.value = null;
  sessionDetailBusy.value = true;
  errMsg.value = '';
  try {
    const r = await exportSession(s.id);
    messages.value = r.messages;
  } catch (e) {
    toastError(e, '会话详情加载失败');
    openSession.value = null;
  } finally {
    sessionDetailBusy.value = false;
  }
}

function closeSession() {
  openSession.value = null;
  messages.value = null;
}

async function doDeleteSession() {
  const s = pendingDeleteSession.value;
  if (!s) return;
  sessionDelBusy.value = true;
  errMsg.value = '';
  try {
    await deleteSession(s.id);
    notice.value = `会话 ${s.title || s.id} 已删除`;
    if (openSession.value?.id === s.id) closeSession();
    void loadSessions();
  } catch (e) {
    toastError(e, '会话删除失败');
  } finally {
    sessionDelBusy.value = false;
    pendingDeleteSession.value = null;
  }
}

// ---------------------------------------------------------------------------
// 生命周期
// ---------------------------------------------------------------------------

onMounted(() => {
  void loadKeys();
  void loadUsage();
  void loadSessions();
});

/** Tab 切换时按需刷新（避免切进来看到过老的数据） */
watch(tab, (t) => {
  if (t === 'keys') void loadKeys();
  else if (t === 'usage') void loadUsage();
  else if (t === 'sessions') void loadSessions();
});

onBeforeUnmount(() => {
  /* 纯查询视图，无 SSE/轮询需要释放 */
});
</script>

<template>
  <div class="max-w-5xl mx-auto pb-12">
    <!-- 页头 -->
    <div class="flex items-center justify-between mb-5">
      <div>
        <h2 class="text-xl font-semibold text-slate-100">我的账号</h2>
        <p class="text-xs text-slate-400 mt-0.5">
          身份：<span class="font-mono text-slate-200">{{ displayName }}</span>
          <span v-if="profile?.is_admin" class="ml-2 text-xs text-amber-300 border border-amber-400/40 rounded px-1.5 py-0.5">管理员</span>
        </p>
      </div>
      <button class="btn-ghost !py-1.5 text-xs" @click="onLogout">退出账号</button>
    </div>

    <!-- 全局提示 -->
    <div v-if="errMsg" class="mb-3 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">{{ errMsg }}</div>
    <div v-if="notice" class="mb-3 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">{{ notice }}</div>

    <!-- Tabs -->
    <nav class="flex gap-1 mb-4 border-b border-slate-800">
      <button
        v-for="t in ([['keys', 'API Keys'], ['usage', '用量'], ['sessions', '会话']] as [TabKey, string][])"
        :key="t[0]"
        :class="[
          'px-4 py-2 text-sm border-b-2 -mb-px transition-colors',
          tab === t[0] ? 'text-blue-300 border-blue-500' : 'text-slate-400 border-transparent hover:text-slate-200',
        ]"
        @click="tab = t[0]"
      >
        {{ t[1] }}
      </button>
    </nav>

    <!-- ==================== Keys Tab ==================== -->
    <section v-if="tab === 'keys'" class="space-y-4">
      <div class="flex items-center justify-between">
        <h3 class="text-base font-medium text-slate-100">API Keys</h3>
        <button class="btn-primary !py-1.5 text-xs" @click="showNewKey = true">
          <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
          签发新 Key
        </button>
      </div>

      <div class="card !p-0 overflow-hidden">
        <table class="w-full text-sm">
          <thead class="bg-slate-800/60 text-slate-400 text-xs">
            <tr>
              <th class="px-3 py-2 text-left">前缀</th>
              <th class="px-3 py-2 text-left">名称</th>
              <th class="px-3 py-2 text-left">状态</th>
              <th class="px-3 py-2 text-left">创建</th>
              <th class="px-3 py-2 text-left">过期</th>
              <th class="px-3 py-2 text-left">最近使用</th>
              <th class="px-3 py-2 text-right">操作</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-800">
            <tr v-if="keysLoading">
              <td colspan="7" class="px-3 py-6 text-center text-slate-500">加载中…</td>
            </tr>
            <tr v-else-if="keys.length === 0">
              <td colspan="7" class="px-3 py-6 text-center text-slate-500">暂无 Key</td>
            </tr>
            <tr v-for="k in keys" :key="k.id" class="hover:bg-slate-800/30">
              <td class="px-3 py-2 font-mono text-xs text-slate-200">{{ k.key_prefix || '-' }}</td>
              <td class="px-3 py-2">{{ k.name || '-' }}</td>
              <td class="px-3 py-2">
                <span :class="[
                  'inline-flex items-center rounded-full px-2 py-0.5 text-xs border',
                  k.status === 'active' && 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
                  k.status === 'disabled' && 'bg-amber-500/15 text-amber-300 border-amber-500/30',
                  k.status === 'revoked' && 'bg-red-500/15 text-red-300 border-red-500/30',
                  !['active','disabled','revoked'].includes(k.status) && 'bg-slate-700 text-slate-300 border-slate-600',
                ]">{{ k.status }}</span>
              </td>
              <td class="px-3 py-2 text-xs text-slate-400">{{ fmtEpoch(k.created_at) }}</td>
              <td class="px-3 py-2 text-xs text-slate-400">{{ fmtEpoch(k.expires_at) === '-' ? '永不过期' : fmtEpoch(k.expires_at) }}</td>
              <td class="px-3 py-2 text-xs text-slate-400">{{ fmtEpoch(k.last_used_at) }}</td>
              <td class="px-3 py-2 text-right whitespace-nowrap">
                <button
                  v-if="k.status === 'active'" class="btn-ghost !py-0.5 !px-2 text-xs mr-1"
                  @click="pendingKeyOp = { kind: 'status', key: k, status: 'disabled' }"
                >禁用</button>
                <button
                  v-else-if="k.status === 'disabled'" class="btn-ghost !py-0.5 !px-2 text-xs mr-1"
                  @click="pendingKeyOp = { kind: 'status', key: k, status: 'active' }"
                >启用</button>
                <button
                  v-if="k.status !== 'revoked'" class="btn-ghost !py-0.5 !px-2 text-xs mr-1 text-amber-300"
                  @click="pendingKeyOp = { kind: 'status', key: k, status: 'revoked' }"
                >吊销</button>
                <button class="btn-ghost !py-0.5 !px-2 text-xs text-red-300"
                  @click="pendingKeyOp = { kind: 'delete', key: k, status: undefined }"
                >删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- ==================== Usage Tab ==================== -->
    <section v-else-if="tab === 'usage'" class="space-y-4">
      <div class="card" v-if="usageLoading">
        <p class="text-slate-400 text-sm">加载中…</p>
      </div>
      <div v-else-if="usage" class="grid gap-4 md:grid-cols-2">
        <!-- 用量 4 键 -->
        <div class="card">
          <h4 class="text-sm font-semibold text-slate-100 mb-3">累计用量</h4>
          <dl class="grid grid-cols-2 gap-y-3">
            <div class="flex justify-between text-sm"><dt class="text-slate-400">请求数</dt><dd class="font-mono text-slate-200">{{ fmtTokens(usage.requests) }}</dd></div>
            <div class="flex justify-between text-sm"><dt class="text-slate-400">Prompt Tokens</dt><dd class="font-mono text-slate-200">{{ fmtTokens(usage.prompt_tokens) }}</dd></div>
            <div class="flex justify-between text-sm"><dt class="text-slate-400">Completion Tokens</dt><dd class="font-mono text-slate-200">{{ fmtTokens(usage.completion_tokens) }}</dd></div>
            <div class="flex justify-between text-sm"><dt class="text-slate-400">Total Tokens</dt><dd class="font-mono text-slate-200">{{ fmtTokens(usage.total_tokens) }}</dd></div>
          </dl>
        </div>
        <!-- 预算 -->
        <div class="card">
          <h4 class="text-sm font-semibold text-slate-100 mb-3">Token 预算</h4>
          <dl class="grid grid-cols-2 gap-y-3">
            <div class="flex justify-between text-sm">
              <dt class="text-slate-400">预算</dt>
              <dd class="font-mono text-slate-200">{{ usage.token_budget === null ? '无限额' : fmtTokens(usage.token_budget) }}</dd>
            </div>
            <div class="flex justify-between text-sm">
              <dt class="text-slate-400">已消耗</dt>
              <dd class="font-mono text-slate-200">{{ fmtTokens(usage.budget_consumed) }}</dd>
            </div>
            <div class="flex justify-between text-sm">
              <dt class="text-slate-400">周期</dt>
              <dd class="text-slate-200">{{ usage.budget_period || '—' }}</dd>
            </div>
            <div class="flex justify-between text-sm">
              <dt class="text-slate-400">重置</dt>
              <dd class="font-mono text-xs text-slate-200">{{ fmtEpoch(usage.budget_reset_at) }}</dd>
            </div>
          </dl>
        </div>
      </div>
      <div v-else class="card"><p class="text-slate-500">暂无用量数据</p></div>
    </section>

    <!-- ==================== Sessions Tab ==================== -->
    <section v-else class="space-y-4">
      <div class="flex items-center justify-between gap-2 flex-wrap">
        <div class="flex gap-2 flex-1 min-w-0">
          <input
            v-model="sessionsQ" placeholder="按标题 / 内容搜索" class="input-base max-w-xs"
            @keyup.enter="loadSessions()"
          />
          <button class="btn-ghost !py-1.5 text-xs" @click="loadSessions()">搜索会话</button>
        </div>
        <button class="btn-ghost !py-1.5 text-xs" @click="showSearch = !showSearch">
          {{ showSearch ? '关闭消息搜索' : '搜索消息' }}
        </button>
      </div>

      <!-- 消息搜索（跨会话） -->
      <div v-if="showSearch" class="card">
        <div class="flex gap-2 items-center mb-3">
          <input v-model="searchQ" class="input-base flex-1" placeholder="在历史消息里搜关键词" @keyup.enter="onSearch" />
          <button class="btn-primary !py-1.5 text-xs" :disabled="searchBusy" @click="onSearch">
            <svg v-if="searchBusy" class="size-4 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" /><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" />
            </svg>
            搜索
          </button>
        </div>
        <p v-if="searchErr" class="text-xs text-red-400 mb-2">{{ searchErr }}</p>
        <div v-if="searchRes" class="space-y-2 max-h-96 overflow-y-auto">
          <p v-if="searchRes.length === 0" class="text-xs text-slate-500">未命中任何消息</p>
          <div v-for="m in searchRes" :key="m.id" class="rounded border border-slate-800 bg-slate-800/30 p-2 text-sm">
            <div class="flex items-center gap-2 text-xs text-slate-400 mb-1">
              <span :class="[
                'rounded px-1.5 py-0.5 border',
                m.role === 'user' && 'bg-blue-500/15 text-blue-300 border-blue-500/30',
                m.role === 'assistant' && 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
                m.role === 'system' && 'bg-slate-600/40 text-slate-300 border-slate-500/30',
                !['user','assistant','system'].includes(m.role) && 'bg-amber-500/15 text-amber-300 border-amber-500/30',
              ]">{{ m.role }}</span>
              <span class="truncate max-w-[200px]" :title="m.title || ''">{{ m.title || `会话 #${m.session_id}` }}</span>
              <span class="font-mono text-[11px]">{{ fmtEpoch(m.created_at) }}</span>
            </div>
            <p class="text-slate-200 text-xs whitespace-pre-wrap break-words">{{ m.content }}</p>
            <button class="mt-2 text-xs text-blue-300 hover:underline" @click="openSessionDetail({ id: m.session_id, user_id: profile?.id ?? 0, key_id: null, model: m.model ?? null, session_key: null, title: m.title ?? null, message_count: 0, created_at: null, last_active_at: null })">
              打开该会话 →
            </button>
          </div>
        </div>
      </div>

      <!-- 会话列表 -->
      <div class="card !p-0 overflow-hidden">
        <table class="w-full text-sm">
          <thead class="bg-slate-800/60 text-slate-400 text-xs">
            <tr>
              <th class="px-3 py-2 text-left">标题</th>
              <th class="px-3 py-2 text-left">模型</th>
              <th class="px-3 py-2 text-right">消息数</th>
              <th class="px-3 py-2 text-left">首次</th>
              <th class="px-3 py-2 text-left">最近</th>
              <th class="px-3 py-2 text-right">操作</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-800">
            <tr v-if="sessionsLoading"><td colspan="6" class="px-3 py-6 text-center text-slate-500">加载中…</td></tr>
            <tr v-else-if="sessions.length === 0"><td colspan="6" class="px-3 py-6 text-center text-slate-500">会话列表为空</td></tr>
            <tr v-for="s in sessions" :key="s.id" class="hover:bg-slate-800/30">
              <td class="px-3 py-2 max-w-xs truncate" :title="s.title || ''">{{ s.title || `#${s.id}` }}</td>
              <td class="px-3 py-2 font-mono text-xs">{{ s.model || '-' }}</td>
              <td class="px-3 py-2 text-right font-mono text-xs">{{ s.message_count }}</td>
              <td class="px-3 py-2 text-xs text-slate-400">{{ fmtEpoch(s.created_at) }}</td>
              <td class="px-3 py-2 text-xs text-slate-400">{{ fmtEpoch(s.last_active_at) }}</td>
              <td class="px-3 py-2 text-right whitespace-nowrap">
                <button class="btn-ghost !py-0.5 !px-2 text-xs mr-1" @click="openSessionDetail(s)">查看</button>
                <button class="btn-ghost !py-0.5 !px-2 text-xs text-red-300" @click="pendingDeleteSession = s">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 签发新 Key 弹窗 -->
    <Teleport to="body">
      <div v-if="showNewKey" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4" @click.self="showNewKey = false">
        <div class="w-full max-w-md rounded-lg border border-slate-700 bg-slate-900 shadow-xl">
          <div class="flex items-center justify-between border-b border-slate-800 px-5 py-3">
            <h3 class="text-base font-semibold text-slate-100">签发新 Key</h3>
            <button class="text-slate-400 hover:text-slate-200 text-xl leading-none" @click="showNewKey = false">×</button>
          </div>
          <div class="px-5 py-4">
            <label class="label-base">名称</label>
            <input v-model="newKeyName" class="input-base" placeholder="例如：本地开发" />
            <label class="label-base mt-3">有效期（天，留空 = 永不过期）</label>
            <input v-model="newKeyExpireDays" type="number" min="0" step="1" class="input-base" placeholder="例如：30" />
            <p v-if="newKeyErr" class="mt-2 text-xs text-red-400">{{ newKeyErr }}</p>
            <p class="mt-3 text-xs text-amber-300/80">注意：Key 明文仅签发瞬间展示一次，请妥善保存。</p>
          </div>
          <div class="flex items-center justify-end gap-3 border-t border-slate-800 px-5 py-3">
            <button class="btn-ghost" :disabled="newKeyBusy" @click="showNewKey = false">取消</button>
            <button class="btn-primary" :disabled="newKeyBusy" @click="onIssue">
              <svg v-if="newKeyBusy" class="size-4 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" /><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" />
              </svg>
              签发
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 一次性 Key 明文弹窗 -->
    <Teleport to="body">
      <div v-if="issuedKey" class="fixed inset-0 z-[60] flex items-center justify-center bg-black/80 backdrop-blur p-4">
        <div class="w-full max-w-xl rounded-lg border border-amber-500/40 bg-slate-900 shadow-xl">
          <div class="flex items-center justify-between border-b border-slate-800 px-5 py-3">
            <h3 class="text-base font-semibold text-amber-300">Key 已签发 — 请立即保存</h3>
            <button class="text-slate-400 hover:text-slate-200 text-xl leading-none" @click="closeIssued">×</button>
          </div>
          <div class="px-5 py-4">
            <p class="text-xs text-amber-300/90 mb-3">明文仅此次可见，关闭窗口后将无法再查看（后端仅存 hash）。</p>
            <div class="rounded bg-slate-800 border border-slate-700 p-3 font-mono text-sm break-all select-all">{{ issuedKey }}</div>
            <p v-if="issuedKeyCtx" class="mt-3 text-xs text-slate-400">
              名称：{{ issuedKeyCtx.name }} · ID：{{ issuedKeyCtx.id }}
            </p>
          </div>
          <div class="flex items-center justify-end gap-3 border-t border-slate-800 px-5 py-3">
            <button class="btn-danger" @click="closeIssued">我已保存，关闭</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 会话详情 -->
    <Teleport to="body">
      <div v-if="openSession" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur p-4" @click.self="closeSession">
        <div class="w-full max-w-2xl h-[80vh] rounded-lg border border-slate-700 bg-slate-900 shadow-xl flex flex-col">
          <div class="flex items-center justify-between border-b border-slate-800 px-5 py-3">
            <h3 class="text-base font-semibold text-slate-100 truncate" :title="openSession.title || ''">
              {{ openSession.title || `会话 #${openSession.id}` }}
            </h3>
            <button class="text-slate-400 hover:text-slate-200 text-xl leading-none" @click="closeSession">×</button>
          </div>
          <div class="flex-1 overflow-y-auto px-5 py-4 space-y-3">
            <p v-if="sessionDetailBusy" class="text-slate-400 text-sm">加载消息…</p>
            <template v-else-if="messages">
              <p v-if="messages.length === 0" class="text-slate-500 text-sm text-center py-4">（无消息）</p>
              <div v-for="m in messages" :key="m.id" class="max-w-[85%]" :class="m.role === 'user' ? 'ml-auto text-right' : ''">
                <div class="text-[11px] text-slate-500 mb-0.5">{{ m.role }} · {{ fmtEpoch(m.created_at) }}</div>
                <div :class="[
                  'inline-block rounded-lg px-3 py-2 text-sm whitespace-pre-wrap break-words',
                  m.role === 'user' ? 'bg-blue-500/20 text-blue-100' : (m.role === 'assistant' ? 'bg-slate-800 text-slate-200' : 'bg-slate-800/60 text-slate-300'),
                ]">{{ m.content || '（空）' }}</div>
              </div>
            </template>
          </div>
          <div class="flex items-center justify-end gap-2 border-t border-slate-800 px-5 py-3">
            <button class="btn-ghost !py-1.5 text-xs text-red-300" @click="pendingDeleteSession = openSession; closeSession()">删除该会话</button>
            <button class="btn-ghost !py-1.5 text-xs" @click="closeSession">关闭</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 通用确认弹窗 -->
    <ConfirmDialog
      :open="!!pendingKeyOp"
      :danger="pendingKeyOp?.kind === 'delete'"
      :title="pendingKeyOp?.kind === 'delete' ? '删除 Key' : '变更 Key 状态'"
      :message="(pendingKeyOp?.kind === 'delete')
        ? `确定删除 Key #${pendingKeyOp?.key.id}（${pendingKeyOp?.key.name || pendingKeyOp?.key.key_prefix || ''}）？\n该操作不可恢复。`
        : `Key #${pendingKeyOp?.key.id} → ${pendingKeyOp?.status}。`"
      :loading="keyOpBusy"
      @confirm="doKeyOp(pendingKeyOp)"
      @cancel="pendingKeyOp = null"
    />

    <ConfirmDialog
      :open="!!pendingDeleteSession"
      :danger="true"
      title="删除会话"
      :message="`确定删除会话 ${pendingDeleteSession?.title || '#' + pendingDeleteSession?.id} 及全部消息？\n该操作不可恢复。`"
      :loading="sessionDelBusy"
      @confirm="doDeleteSession()"
      @cancel="pendingDeleteSession = null"
    />
  </div>
</template>
