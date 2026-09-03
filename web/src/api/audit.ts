import client, { dataOf } from './client';
import type {
  AuditCleanupResponse,
  AuditListResponse,
  AuditQueryParams,
  AuditStatsResponse,
} from './types';

/** 审计列表查询。 */
export function auditList(params?: AuditQueryParams): Promise<AuditListResponse> {
  return dataOf<AuditListResponse>(
    client.get('/audit', {
      params: {
        since: params?.since,
        limit: params?.limit,
        level: params?.level,
        keyword: params?.keyword,
      },
    }),
  );
}

/**
 * 审计统计：total / by_day / by_model。
 * since 与列表同源（10m / 1h / 6h / 24h / 7d / 30d）。
 */
export function auditStats(since?: string): Promise<AuditStatsResponse> {
  return dataOf<AuditStatsResponse>(
    client.get('/audit/stats', { params: { since } }),
  );
}

/** 清理 N 天前的审计记录。 */
export function auditCleanup(days = 30): Promise<AuditCleanupResponse> {
  return dataOf<AuditCleanupResponse>(client.post('/audit/cleanup', null, { params: { days } }));
}

/** 取审计目录的绝对路径（用于前端展示）。 */
export function auditPath(): Promise<{ path: string }> {
  return dataOf<{ path: string }>(client.get('/audit/path'));
}
