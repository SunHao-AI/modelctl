import client, { dataOf } from './client';

export interface LoginResult {
  ok: boolean;
  message?: string;
}

/**
 * 使用 API Key 登录 / 校验。
 * 后端约定（admin_auth.require_auth）：
 *   POST /admin/api/login  body { api_key?: string; key?: string }
 *   失败时 HTTP 401 + { detail: { code, message } }；成功时 { ok: true }
 * 返回：LoginResult 与后端响应结构；前端在 onSubmit 里判断 res.ok
 */
export function login(apiKey: string): Promise<LoginResult> {
  return dataOf<LoginResult>(client.post('/login', { api_key: apiKey }));
}

/** 退出：纯前端清 token（后端无会话概念） */
export function logout(): Promise<LoginResult> {
  return Promise.resolve({ ok: true });
}
