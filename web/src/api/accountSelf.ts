import accountClient, { accountDataOf } from './accountClient';
import type { AccountProfile } from '@/stores/auth';

/**
 * 账号自助面板 API（走 `accountClient.ts`，Bearer 账号 JWT）。
 *
 * 后端契约：`account_self.py` → `/api/account/*`；
 *   - 除 `/login` 外全部走 `require_account`（JWT）；
 *   - **归属自治**：所有端点只命中当前 JWT 携带的 user_id 资源；
 *   - 不暴露别家数据、不暴露 hash；
 *   - Key 明文仅 POST /keys 201 一次性。
 */

export interface AccountLoginResult {
  token: string;
  user: AccountProfile;
}

export interface SelfKey {
  id: number;
  user_id: number;
  key_prefix: string;
  name: string;
  status: 'active' | 'disabled' | 'revoked' | string;
  expires_at: number | null;
  last_used_at: number | null;
  created_at: number | null;
  /** 仅 POST /keys 201 一次性出现 */
  key?: string;
}

export interface SelfUsage {
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  budget_consumed: number;
  token_budget: number | null;
  budget_period: string | null;
  budget_reset_at: number | null;
}

export interface SelfSession {
  id: number;
  user_id: number;
  key_id: number | null;
  model: string | null;
  session_key: string | null;
  title: string | null;
  message_count: number;
  created_at: number | null;
  last_active_at: number | null;
}

export interface SelfMessage {
  id: number;
  session_id: number;
  role: 'system' | 'user' | 'assistant' | 'tool' | string;
  content: string;
  created_at: number | null;
  /** search 端点带会话上下文 join */
  title?: string | null;
  model?: string | null;
}

export interface SessionExport {
  session: SelfSession;
  messages: SelfMessage[];
}

/** POST /api/account/login — 成功 {token, user}；失败 401 {code:'auth', message:'用户名或密码错误'} */
export function accountLogin(username: string, password: string): Promise<AccountLoginResult> {
  return accountDataOf<AccountLoginResult>(
    accountClient.post('/login', { username, password }),
  );
}

// ---------------------------------------------------------------------------
// Key 管理
// ---------------------------------------------------------------------------

export function listKeys(): Promise<SelfKey[]> {
  return accountDataOf<{ keys: SelfKey[] }>(accountClient.get('/keys')).then((r) => r.keys);
}

/** 一次性返回明文 key（key 字段仅此一次） */
export function createKey(payload: { name?: string; expires_in_days?: number | null }): Promise<SelfKey> {
  return accountDataOf<SelfKey>(accountClient.post('/keys', payload));
}

export function updateKey(keyId: number, status: 'active' | 'disabled' | 'revoked' | string): Promise<SelfKey> {
  return accountDataOf<SelfKey>(accountClient.put(`/keys/${keyId}`, { status }));
}

/** 硬删（重复删 404） */
export function deleteKey(keyId: number): Promise<{ id: number }> {
  return accountDataOf<{ id: number }>(accountClient.delete(`/keys/${keyId}`));
}

// ---------------------------------------------------------------------------
// 用量
// ---------------------------------------------------------------------------

export function getUsage(): Promise<SelfUsage> {
  return accountDataOf<SelfUsage>(accountClient.get('/usage'));
}

// ---------------------------------------------------------------------------
// 会话
// ---------------------------------------------------------------------------

/**
 * 重要：`/sessions/search` 比 `/sessions/{id}` 优先注册（后端路由顺序决定），
 * 前端调用方无需担心——URL 类名是分开的，不存在"search 被解析为 id"问题。
 */
export function searchMessages(q: string): Promise<SelfMessage[]> {
  return accountDataOf<{ messages: SelfMessage[] }>(
    accountClient.get('/sessions/search', { params: { q } }),
  ).then((r) => r.messages);
}

export function listSessions(q?: string): Promise<SelfSession[]> {
  const params: Record<string, string> = {};
  if (q) params.q = q;
  return accountDataOf<{ sessions: SelfSession[] }>(
    accountClient.get('/sessions', { params }),
  ).then((r) => r.sessions);
}

export function getSession(sessionId: number): Promise<SelfSession> {
  return accountDataOf<SelfSession>(accountClient.get(`/sessions/${sessionId}`));
}

export function exportSession(sessionId: number): Promise<SessionExport> {
  return accountDataOf<SessionExport>(accountClient.get(`/sessions/${sessionId}/export`));
}

/** 幂等：跨用户/已删均 404 */
export function deleteSession(sessionId: number): Promise<{ id: number }> {
  return accountDataOf<{ id: number }>(accountClient.delete(`/sessions/${sessionId}`));
}
