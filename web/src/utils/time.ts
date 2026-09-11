import dayjs from 'dayjs';

/**
 * 统一时间格式：`YYYY-MM-DD HH:mm:ss`（项目 CLAUDE.md 约定）。
 * 后端字段是 epoch 秒（REAL），NULL 归一为 '-'。
 */
export function fmtEpoch(v: number | null | undefined): string {
  if (v === null || v === undefined) return '-';
  return dayjs.unix(v).format('YYYY-MM-DD HH:mm:ss');
}

export function fmtTokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return '-';
  return n.toLocaleString('en-US');
}
