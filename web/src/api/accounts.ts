import client, { dataOf } from './client';

/**
 * 管理面账号 / Key / 用量 API（走 `client.ts`，Bearer API_KEY）。
 *
 * 后端契约：`admin_accounts.py` → 挂在 `/admin/api` 前缀下；accounts 未启用
 * 时后端一律 503 `{ code: 'accounts_disabled' }`。
 *
 * 类型与后端 `_user_public / _key_public` 白名单对齐；时间列后端不格式化
 * （REAL epoch 秒），前端用 formatTime 展示。
 */

export interface AccountUser {
  id: number;
  username: string;
  display_name: string;
  is_admin: boolean;
  status: 'active' | 'disabled' | string;
  concurrency_limit: number | null;
  rpm_limit: number | null;
  tpm_limit: number | null;
  token_budget: number | null;
  budget_consumed: number;
  budget_period: string | null;
  budget_reset_at: number | null;
  retention_days: number | null;
  created_at: number | null;
  updated_at: number | null;
}

export interface AccountKey {
  id: number;
  user_id: number;
  key_prefix: string;
  name: string;
  status: 'active' | 'disabled' | 'revoked' | string;
  expires_at: number | null;
  last_used_at: number | null;
  created_at: number | null;
  /** 仅在 POST /accounts/{id}/keys 201 响应中出现（一次性） */
  key?: string;
}

export interface AccountUsage {
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  budget_consumed: number;
  token_budget: number | null;
  budget_period: string | null;
  budget_reset_at: number | null;
}

export interface CreateAccountPayload {
  username: string;
  password: string;
  display_name?: string;
  is_admin?: boolean;
  concurrency_limit?: number | null;
  rpm_limit?: number | null;
  tpm_limit?: number | null;
  token_budget?: number | null;
  budget_period?: string;
  retention_days?: number | null;
}

export function listAccounts(): Promise<AccountUser[]> {
  return dataOf<{ accounts: AccountUser[] }>(client.get('/accounts')).then((r) => r.accounts);
}

export function createAccount(payload: CreateAccountPayload): Promise<AccountUser> {
  return dataOf<AccountUser>(client.post('/accounts', payload));
}

export function getAccount(id: number): Promise<AccountUser> {
  return dataOf<AccountUser>(client.get(`/accounts/${id}`));
}

export function updateAccount(
  id: number,
  payload: Partial<CreateAccountPayload> & {
    status?: 'active' | 'disabled' | string;
    budget_reset_at?: number | null;
  },
): Promise<AccountUser> {
  return dataOf<AccountUser>(client.put(`/accounts/${id}`, payload));
}

/** 幂等：首删 200 + snapshot，重复删 404 */
export function deleteAccount(id: number): Promise<{ id: number; username: string }> {
  return dataOf<{ id: number; username: string }>(client.delete(`/accounts/${id}`));
}

export function listAccountKeys(accountId: number): Promise<AccountKey[]> {
  return dataOf<{ keys: AccountKey[] }>(
    client.get(`/accounts/${accountId}/keys`),
  ).then((r) => r.keys);
}

/** 一次性返回明文 key —— 前端在弹窗抄录后必须"仅显示一次"提示后收起 */
export function createAccountKey(
  accountId: number,
  payload: { name?: string; expires_in_days?: number | null },
): Promise<AccountKey> {
  return dataOf<AccountKey>(client.post(`/accounts/${accountId}/keys`, payload));
}

export function updateAccountKey(
  accountId: number,
  keyId: number,
  status: 'active' | 'disabled' | 'revoked' | string,
): Promise<AccountKey> {
  return dataOf<AccountKey>(client.put(`/accounts/${accountId}/keys/${keyId}`, { status }));
}

/** 硬删（重复删 404） */
export function deleteAccountKey(accountId: number, keyId: number): Promise<{ id: number }> {
  return dataOf<{ id: number }>(client.delete(`/accounts/${accountId}/keys/${keyId}`));
}

export function getAccountUsage(
  accountId: number,
  since?: number,
): Promise<AccountUsage> {
  const params: Record<string, number> = {};
  if (since !== undefined) params.since = since;
  return dataOf<AccountUsage>(client.get(`/accounts/${accountId}/usage`, { params }));
}
