<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { AxiosError } from 'axios';
import {
  listAccounts, createAccount, updateAccount, deleteAccount,
  listAccountKeys, createAccountKey, updateAccountKey, deleteAccountKey,
  getAccountUsage,
} from '@/api/accounts';
import type { AccountUser, AccountKey, AccountUsage } from '@/api/accounts';
import ConfirmDialog from '@/components/common/ConfirmDialog.vue';
import DataTable from '@/components/common/DataTable.vue';
import { fmtEpoch, fmtTokens } from '@/utils/time';

/**
 * 账号管理面板
 *
 * 端点走 `client.ts`（Bearer API_KEY）——与账号面板 JWT 双信任域：管理面只用
 * 现有 API Key；账号 JWT 在这里**不**注入（不同 axios 实例）。
 *
 * accounts 未启用：所有端点 503 { code: 'accounts_disabled' }，本视图只
 * 展示一个友好的 503 空态，并对 `accounts_disabled` 给"联系管理员启用"引导。
 */

const errMsg = ref('');
const notice = ref('');
const loading = ref(false);
const accounts = ref<AccountUser[]>([]);
const pendingDelete = ref<AccountUser | null>(null);
const delBusy = ref(false);

// 详情面板（Key 用量侧）
const openAccount = ref<AccountUser | null>(null);
const openUsage = ref<AccountUsage | null>(null);
const openKeys = ref<AccountKey[]>([]);
const openLoading = ref(false);
const issuedKey = ref<string | null>(null);
const issuedKeyCtx = ref<{ id: number; prefix: string } | null>(null);
const pendingKeyOp = ref<{ kind: 'status' | 'delete'; key: AccountKey; status?: string } | null>(null);
const keyOpBusy = ref(false);

// 新建/编辑账号表单
const showForm = ref(false);
const editing = ref<AccountUser | null>(null);
const form = ref<{
  username: string; password: string; display_name: string; is_admin: boolean;
  concurrency_limit: number | ''; rpm_limit: number | ''; tpm_limit: number | '';
  token_budget: number | ''; budget_period: string; retention_days: number | '';
  status: 'active' | 'disabled';
}>({
  username: '', password: '', display_name: '', is_admin: false,
  concurrency_limit: '', rpm_limit: '', tpm_limit: '',
  token_budget: '', budget_period: 'day', retention_days: '', status: 'active',
});
const formErr = ref('');
const formBusy = ref(false);

// 签发 Key 表单
const showIssueKey = ref(false);
const issueKeyName = ref('');
const issueKeyDays = ref<number | ''>('');
const issueKeyErr = ref('');
const issueKeyBusy = ref(false);

const isAccountsDisabled = ref(false);

function pickDetail(e: unknown): { code?: string; message?: string; http?: number } {
  const ax = e as AxiosError<{ detail?: unknown }>;
  const d = ax.response?.data?.detail;
  if (d && typeof d === 'object' && 'code' in d) {
    return { code: (d as { code: string }).code, message: (d as { message?: string }).message, http: ax.response?.status };
  }
  return { http: ax.response?.status, message: typeof d === 'string' ? d : undefined };
}

function toastError(e: unknown, fallback: string) {
  const d = pickDetail(e);
  errMsg.value = d.message || fallback;
  notice.value = '';
  if (d.code === 'accounts_disabled') {
    isAccountsDisabled.value = true;
    errMsg.value = '账号体系未启用（后端 .env 需设 ACCOUNTS_ENABLED=true）';
  }
}

async function load() {
  loading.value = true;
  isAccountsDisabled.value = false;
  errMsg.value = '';
  try {
    accounts.value = await listAccounts();
  } catch (e) {
    toastError(e, '账号列表加载失败');
  } finally {
    loading.value = false;
  }
}

async function doDelete() {
  const acc = pendingDelete.value;
  if (!acc) return;
  delBusy.value = true;
  errMsg.value = '';
  try {
    await deleteAccount(acc.id);
    notice.value = `账号 ${acc.username} 已删除（含级联 keys/usage/sessions）`;
    await load();
  } catch (e) {
    toastError(e, '删除账号失败');
  } finally {
    delBusy.value = false;
    pendingDelete.value = null;
  }
}

async function openDetail(acc: AccountUser) {
  openAccount.value = acc;
  openUsage.value = null;
  openKeys.value = [];
  openLoading.value = true;
  errMsg.value = '';
  try {
    const [u, ks] = await Promise.all([getAccountUsage(acc.id), listAccountKeys(acc.id)]);
    openUsage.value = u;
    openKeys.value = ks;
  } catch (e) {
    toastError(e, '账号详情加载失败');
    openAccount.value = null;
  } finally {
    openLoading.value = false;
  }
}

function closeDetail() {
  openAccount.value = null;
  openUsage.value = null;
  openKeys.value = [];
}

// 表单
function resetForm(acc: AccountUser | null) {
  if (acc) {
    form.value = {
      username: acc.username,
      password: '', // 密码不更新同端点（后端 admin 无 password 改）
      display_name: acc.display_name || '',
      is_admin: !!acc.is_admin,
      concurrency_limit: acc.concurrency_limit ?? '',
      rpm_limit: acc.rpm_limit ?? '',
      tpm_limit: acc.tpm_limit ?? '',
      token_budget: acc.token_budget ?? '',
      budget_period: acc.budget_period || 'day',
      retention_days: acc.retention_days ?? '',
      status: (acc.status as 'active' | 'disabled') || 'active',
    };
  } else {
    form.value = {
      username: '', password: '', display_name: '', is_admin: false,
      concurrency_limit: '', rpm_limit: '', tpm_limit: '',
      token_budget: '', budget_period: 'day', retention_days: '', status: 'active',
    };
  }
  formErr.value = '';
}

function openCreate() {
  editing.value = null;
  resetForm(null);
  showForm.value = true;
}

function openEdit(acc: AccountUser) {
  editing.value = acc;
  resetForm(acc);
  showForm.value = true;
}

function intOrNull(v: number | ''): number | null {
  return v === '' ? null : Number(v);
}

async function onFormSubmit() {
  const f = form.value;
  if (f.username.length > 64) {
    formErr.value = 'username 过长';
    return;
  }
  const isCreate = !editing.value;
  if (isCreate && !f.password) {
    formErr.value = '新建时必须填 password';
    return;
  }
  formBusy.value = true;
  formErr.value = '';
  try {
    if (isCreate) {
      await createAccount({
        username: f.username,
        password: f.password,
        display_name: f.display_name,
        is_admin: f.is_admin,
        concurrency_limit: intOrNull(f.concurrency_limit),
        rpm_limit: intOrNull(f.rpm_limit),
        tpm_limit: intOrNull(f.tpm_limit),
        token_budget: intOrNull(f.token_budget),
        budget_period: f.budget_period,
        retention_days: intOrNull(f.retention_days),
      });
      notice.value = `账号 ${f.username} 已创建`;
    } else if (editing.value) {
      await updateAccount(editing.value.id, {
        display_name: f.display_name,
        is_admin: f.is_admin,
        status: f.status,
        concurrency_limit: intOrNull(f.concurrency_limit),
        rpm_limit: intOrNull(f.rpm_limit),
        tpm_limit: intOrNull(f.tpm_limit),
        token_budget: intOrNull(f.token_budget),
        budget_period: f.budget_period,
        retention_days: intOrNull(f.retention_days),
      });
      notice.value = `账号 ${editing.value.username} 已更新`;
    }
    showForm.value = false;
    await load();
  } catch (e) {
    const d = pickDetail(e);
    formErr.value = d.message || (e as { message?: string })?.message || '保存失败';
  } finally {
    formBusy.value = false;
  }
}

// 签发 Key
const currentUser = computed(() => openAccount.value);
function openIssueKey() {
  if (!currentUser.value) return;
  issueKeyName.value = '';
  issueKeyDays.value = '';
  issueKeyErr.value = '';
  showIssueKey.value = true;
}

async function onIssueKey() {
  const acc = currentUser.value;
  if (!acc) return;
  const days = issueKeyDays.value === '' ? null : Number(issueKeyDays.value);
  if (days !== null && (!Number.isFinite(days) || days < 0)) {
    issueKeyErr.value = '有效期必须为非负数字（天）';
    return;
  }
  issueKeyBusy.value = true;
  issueKeyErr.value = '';
  try {
    const r = await createAccountKey(acc.id, { name: issueKeyName.value, expires_in_days: days });
    issuedKey.value = r.key || '';
    issuedKeyCtx.value = { id: r.id, prefix: r.key_prefix || String(r.id) };
    showIssueKey.value = false;
    issueKeyName.value = '';
    notice.value = `Key 已签发（明文仅此次可见）`;
    const ks = await listAccountKeys(acc.id);
    openKeys.value = ks;
  } catch (e) {
    issueKeyErr.value = pickDetail(e).message || (e as { message?: string })?.message || '签发失败';
  } finally {
    issueKeyBusy.value = false;
  }
}

async function doKeyOp(op: typeof pendingKeyOp.value) {
  const acc = currentUser.value;
  if (!acc || !op) return;
  keyOpBusy.value = true;
  errMsg.value = '';
  try {
    if (op.kind === 'status' && op.status) {
      await updateAccountKey(acc.id, op.key.id, op.status);
      notice.value = `Key ${op.key.key_prefix || op.key.id} → ${op.status}`;
    } else if (op.kind === 'delete') {
      await deleteAccountKey(acc.id, op.key.id);
      notice.value = `Key ${op.key.key_prefix || op.key.id} 已删除`;
    }
    const ks = await listAccountKeys(acc.id);
    openKeys.value = ks;
    if (op.key.id === (openAccount.value?.id as number | undefined)) {
      // do nothing openAccount.id is account vs key — intentionally
    }
  } catch (e) {
    toastError(e, 'Key 操作失败');
  } finally {
    keyOpBusy.value = false;
    pendingKeyOp.value = null;
  }
}

onMounted(load);
</script>

<template>
  <div class="max-w-5xl mx-auto pb-12">
    <div class="flex items-center justify-between mb-5">
      <div>
        <h2 class="text-xl font-semibold text-label">账号管理</h2>
        <p class="text-xs text-label3 mt-0.5">管理用户账号 / 签发 API Key / 查看用量</p>
      </div>
      <button class="btn-primary !py-1.5 text-xs" @click="openCreate">
        <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
        新建账号
      </button>
    </div>

    <div v-if="errMsg" class="mb-3 rounded-ctl border border-danger-line bg-danger-bg px-3 py-2 text-sm text-danger">{{ errMsg }}</div>
    <div v-if="isAccountsDisabled" class="mb-3 rounded-ctl border border-warn-line bg-warn-bg px-4 py-3 text-sm text-warn">
      <p class="text-label font-medium mb-1">账号体系未启用</p>
      <p class="text-xs">
        如需启用，请在后端 .env 中设 <code class="font-mono text-warn">ACCOUNTS_ENABLED=true</code> 重启服务，
        并首次使用 <code class="font-mono">modelctl account create</code> 建首个账号。
      </p>
    </div>
    <div v-if="notice" class="mb-3 rounded-ctl border border-ok-line bg-ok-bg px-3 py-2 text-sm text-ok">{{ notice }}</div>

    <!-- 表格 -->
    <DataTable>
      <table class="min-w-[64rem] text-sm">
        <thead>
          <tr>
            <th class="min-w-32">账号</th>
            <th class="min-w-24">显示名</th>
            <th class="min-w-16">角色</th>
            <th class="min-w-20">状态</th>
            <th class="text-right">并发</th>
            <th class="text-right">RPM</th>
            <th class="text-right">TPM</th>
            <th class="text-right">Token 预算</th>
            <th class="min-w-36">创建</th>
            <th class="min-w-36 text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading"><td colspan="10" class="!py-6 text-center text-label3">加载中…</td></tr>
          <tr v-else-if="accounts.length === 0 && !isAccountsDisabled"><td colspan="10" class="!py-6 text-center text-label3">暂无账号</td></tr>
          <tr v-for="a in accounts" :key="a.id">
            <td class="font-mono text-xs">
              <button class="max-w-40 truncate text-left hover:text-accent hover:underline align-middle" :title="a.username" @click="openDetail(a)">{{ a.username }}</button>
            </td>
            <td class="max-w-40 truncate" :title="a.display_name || ''">{{ a.display_name || '-' }}</td>
            <td>
              <span v-if="a.is_admin" class="rounded-ctl border border-warn-line bg-warn-bg px-1.5 py-0.5 text-xs text-warn">管理员</span>
              <span v-else class="text-xs text-label3">用户</span>
            </td>
            <td>
              <span :class="[
                'inline-flex items-center rounded-full px-2 py-0.5 text-xs border',
                a.status === 'active' && 'border-ok-line bg-ok-bg text-ok',
                a.status === 'disabled' && 'border-danger-line bg-danger-bg text-danger',
                !['active','disabled'].includes(a.status) && 'border-sep bg-surface3 text-label2',
              ]">{{ a.status }}</span>
            </td>
            <td class="num text-right font-mono text-xs">{{ a.concurrency_limit ?? '-' }}</td>
            <td class="num text-right font-mono text-xs">{{ a.rpm_limit ?? '-' }}</td>
            <td class="num text-right font-mono text-xs">{{ a.tpm_limit ?? '-' }}</td>
            <td class="num text-right font-mono text-xs">{{ a.token_budget === null ? '无' : fmtTokens(a.token_budget) }}</td>
            <td class="num text-xs text-label3">{{ fmtEpoch(a.created_at) }}</td>
            <td class="text-right whitespace-nowrap">
              <button class="btn-ghost !py-0.5 !px-2 text-xs mr-1" @click="openDetail(a)">详情</button>
              <button class="btn-ghost !py-0.5 !px-2 text-xs mr-1" @click="openEdit(a)">编辑</button>
              <button class="btn-ghost !py-0.5 !px-2 text-xs text-danger" @click="pendingDelete = a">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </DataTable>

    <!-- 详情弹窗 -->
    <Teleport to="body">
      <div v-if="openAccount" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur p-4" @click.self="closeDetail">
        <div class="w-full max-w-3xl h-[85vh] rounded-panel border border-sep bg-surface2 shadow-l flex flex-col">
          <div class="flex items-center justify-between border-b border-sep px-5 py-3">
            <h3 class="text-base font-semibold text-label">
              账号 <span class="font-mono">{{ openAccount.username }}</span>
            </h3>
            <div class="flex items-center gap-2">
              <button class="btn-ghost !py-1.5 text-xs" :disabled="!openAccount" @click="openIssueKey()">
                <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
                签发 Key
              </button>
              <button class="text-label3 hover:text-label text-xl leading-none" @click="closeDetail">×</button>
            </div>
          </div>

          <div class="flex-1 overflow-y-auto px-5 py-4 space-y-5">
            <p v-if="openLoading" class="text-label3 text-sm">加载…</p>
            <template v-else>
              <!-- 用量 -->
              <section>
                <h4 class="text-sm font-semibold text-label mb-2">用量 / 预算</h4>
                <div v-if="openUsage" class="grid grid-cols-2 md:grid-cols-4 gap-3">
                  <div class="rounded-ctl bg-surface3 border border-sep p-2 text-xs">
                    <div class="text-label3">请求数</div>
                    <div class="num font-mono text-label my-0.5">{{ fmtTokens(openUsage.requests) }}</div>
                  </div>
                  <div class="rounded-ctl bg-surface3 border border-sep p-2 text-xs">
                    <div class="text-label3">Prompt Tokens</div>
                    <div class="num font-mono text-label my-0.5">{{ fmtTokens(openUsage.prompt_tokens) }}</div>
                  </div>
                  <div class="rounded-ctl bg-surface3 border border-sep p-2 text-xs">
                    <div class="text-label3">Total Tokens</div>
                    <div class="num font-mono text-label my-0.5">{{ fmtTokens(openUsage.total_tokens) }}</div>
                  </div>
                  <div class="rounded-ctl bg-surface3 border border-sep p-2 text-xs">
                    <div class="text-label3">预算</div>
                    <div class="num font-mono text-label my-0.5">
                      {{ openUsage.token_budget === null ? '无限额' : `${fmtTokens(openUsage.budget_consumed)} / ${fmtTokens(openUsage.token_budget)}` }}
                    </div>
                    <div class="text-[11px] text-label3">周期：{{ openUsage.budget_period || '—' }} · 重置：{{ fmtEpoch(openUsage.budget_reset_at) }}</div>
                  </div>
                </div>
              </section>

              <!-- Keys -->
              <section>
                <h4 class="text-sm font-semibold text-label mb-2">API Keys（{{ openKeys.length }}）</h4>
                <DataTable>
                  <table class="min-w-[40rem] text-xs">
                    <thead>
                      <tr>
                        <th class="min-w-24">前缀</th>
                        <th class="min-w-24">名称</th>
                        <th class="min-w-20">状态</th>
                        <th class="min-w-36">创建</th>
                        <th class="min-w-36">过期</th>
                        <th class="min-w-32 text-right">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-if="openKeys.length === 0">
                        <td colspan="6" class="text-center text-label3">暂无 Key</td>
                      </tr>
                      <tr v-for="k in openKeys" :key="k.id">
                        <td class="font-mono">{{ k.key_prefix || '-' }}</td>
                        <td class="max-w-40 truncate" :title="k.name || ''">{{ k.name || '-' }}</td>
                        <td>
                          <span :class="[
                            'inline-flex items-center rounded-full px-2 py-0.5 text-[11px] border',
                            k.status === 'active' && 'border-ok-line bg-ok-bg text-ok',
                            k.status === 'disabled' && 'border-warn-line bg-warn-bg text-warn',
                            k.status === 'revoked' && 'border-danger-line bg-danger-bg text-danger',
                          ]">{{ k.status }}</span>
                        </td>
                        <td class="num text-label3">{{ fmtEpoch(k.created_at) }}</td>
                        <td class="num text-label3">{{ fmtEpoch(k.expires_at) === '-' ? '永不' : fmtEpoch(k.expires_at) }}</td>
                        <td class="text-right whitespace-nowrap">
                          <button v-if="k.status === 'active'" class="text-xs text-label2 hover:text-warn mr-2" @click="pendingKeyOp = { kind: 'status', key: k, status: 'disabled' }">禁用</button>
                          <button v-else-if="k.status === 'disabled'" class="text-xs text-label2 hover:text-ok mr-2" @click="pendingKeyOp = { kind: 'status', key: k, status: 'active' }">启用</button>
                          <button v-if="k.status !== 'revoked'" class="text-xs text-label2 hover:text-warn mr-2" @click="pendingKeyOp = { kind: 'status', key: k, status: 'revoked' }">吊销</button>
                          <button class="text-xs text-label2 hover:text-danger" @click="pendingKeyOp = { kind: 'delete', key: k, status: undefined }">删除</button>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </DataTable>
              </section>
            </template>
          </div>

          <div class="border-t border-sep px-5 py-3">
            <button class="btn-ghost text-xs" @click="closeDetail">关闭</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 新建/编辑账号表单 -->
    <Teleport to="body">
      <div v-if="showForm" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur p-4" @click.self="showForm = false">
        <div class="w-full max-w-lg rounded-panel border border-sep bg-surface2 shadow-l">
          <div class="flex items-center justify-between border-b border-sep px-5 py-3">
            <h3 class="text-base font-semibold text-label">{{ editing ? `编辑账号 ${editing.username}` : '新建账号' }}</h3>
            <button class="text-label3 hover:text-label text-xl leading-none" @click="showForm = false">×</button>
          </div>
          <form class="px-5 py-4 space-y-3" @submit.prevent="onFormSubmit">
            <div>
              <label class="label-base">Username <span v-if="!editing" class="text-red-400">*</span></label>
              <input v-model="form.username" class="input-base" :disabled="!!editing" placeholder="unique username" />
            </div>
            <div v-if="!editing">
              <label class="label-base">Password <span class="text-red-400">*</span></label>
              <input v-model="form.password" type="password" class="input-base" placeholder="至少 8 位" autocomplete="new-password" />
            </div>
            <div>
              <label class="label-base">显示名</label>
              <input v-model="form.display_name" class="input-base" placeholder="可选，例如：张三" />
            </div>
            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="label-base">并发上限</label>
                <input v-model="form.concurrency_limit" type="number" min="0" class="input-base" placeholder="不限制" />
              </div>
              <div>
                <label class="label-base">RPM</label>
                <input v-model="form.rpm_limit" type="number" min="0" class="input-base" placeholder="不限制" />
              </div>
              <div>
                <label class="label-base">TPM</label>
                <input v-model="form.tpm_limit" type="number" min="0" class="input-base" placeholder="不限制" />
              </div>
              <div>
                <label class="label-base">Token 预算</label>
                <input v-model="form.token_budget" type="number" min="0" class="input-base" placeholder="无限额" />
              </div>
              <div>
                <label class="label-base">预算周期</label>
                <select v-model="form.budget_period" class="input-base">
                  <option value="day">day</option>
                  <option value="week">week</option>
                  <option value="month">month</option>
                  <option value="year">year</option>
                  <option value="lifetime">lifetime</option>
                </select>
              </div>
              <div>
                <label class="label-base">保留天数</label>
                <input v-model="form.retention_days" type="number" min="0" class="input-base" placeholder="默认策略" />
              </div>
            </div>
            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="label-base">管理员</label>
                <label class="mt-1 inline-flex items-center gap-2 text-sm text-label2">
                  <input v-model="form.is_admin" type="checkbox" class="size-4" />
                  授予 is_admin
                </label>
              </div>
              <div>
                <label class="label-base">状态</label>
                <select v-model="form.status" class="input-base">
                  <option value="active">active</option>
                  <option value="disabled">disabled</option>
                </select>
              </div>
            </div>
            <p v-if="formErr" class="text-xs text-red-400">{{ formErr }}</p>
          </form>
          <div class="flex items-center justify-end gap-3 border-t border-sep px-5 py-3">
            <button class="btn-ghost" :disabled="formBusy" @click="showForm = false">取消</button>
            <button class="btn-primary" :disabled="formBusy" @click="onFormSubmit">
              <svg v-if="formBusy" class="size-4 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" /><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" />
              </svg>
              {{ editing ? '保存' : '创建' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 签发 Key 弹窗 -->
    <Teleport to="body">
      <div v-if="showIssueKey" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur p-4" @click.self="showIssueKey = false">
        <div class="w-full max-w-md rounded-panel border border-sep bg-surface2 shadow-l">
          <div class="flex items-center justify-between border-b border-sep px-5 py-3">
            <h3 class="text-base font-semibold text-label">签发 Key（{{ currentUser?.username }}）</h3>
            <button class="text-label3 hover:text-label text-xl leading-none" @click="showIssueKey = false">×</button>
          </div>
          <form class="px-5 py-4 space-y-3" @submit.prevent="onIssueKey">
            <div>
              <label class="label-base">名称</label>
              <input v-model="issueKeyName" class="input-base" placeholder="例如：生产环境" />
            </div>
            <div>
              <label class="label-base">有效期（天，留空 = 永不过期）</label>
              <input v-model="issueKeyDays" type="number" min="0" step="1" class="input-base" placeholder="例如：30" />
            </div>
            <p v-if="issueKeyErr" class="text-xs text-red-400">{{ issueKeyErr }}</p>
            <p class="text-xs text-warn">⚠ Key 明文仅签发瞬间展示一次，请提醒用户妥善保存。</p>
          </form>
          <div class="flex items-center justify-end gap-3 border-t border-sep px-5 py-3">
            <button class="btn-ghost" :disabled="issueKeyBusy" @click="showIssueKey = false">取消</button>
            <button class="btn-primary" :disabled="issueKeyBusy" @click="onIssueKey">
              <svg v-if="issueKeyBusy" class="size-4 animate-spin" viewBox="0 0 24 24" fill="none">
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
        <div class="w-full max-w-xl rounded-panel border border-warn-line bg-surface2 shadow-l">
          <div class="flex items-center justify-between border-b border-sep px-5 py-3">
            <h3 class="text-base font-semibold text-warn">Key 已签发 — 请立即交给用户</h3>
            <button class="text-label3 hover:text-label text-xl leading-none" @click="issuedKey = null">×</button>
          </div>
          <div class="px-5 py-4">
            <p class="text-xs text-warn mb-3">明文仅此次可见，关闭窗口后将无法再查看（后端仅存 hash）。</p>
            <div class="rounded-ctl code-surface border border-code-line p-3 font-mono text-sm break-all select-all">{{ issuedKey }}</div>
            <p v-if="issuedKeyCtx" class="mt-3 text-xs text-label3">
              Key ID：{{ issuedKeyCtx.id }} · 前缀：{{ issuedKeyCtx.prefix }}
            </p>
          </div>
          <div class="flex items-center justify-end gap-3 border-t border-sep px-5 py-3">
            <button class="btn-danger" @click="issuedKey = null">已交付，关闭</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 通用确认弹窗 -->
    <ConfirmDialog
      :open="!!pendingDelete"
      :danger="true"
      title="删除账号"
      :message="`确定删除账号 ${pendingDelete?.username} 及其所有 Key / 用量 / 会话？\n该操作不可恢复（级联清理）。`"
      :loading="delBusy"
      @confirm="doDelete()"
      @cancel="pendingDelete = null"
    />

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
  </div>
</template>
