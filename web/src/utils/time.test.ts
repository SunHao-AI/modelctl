/**
 * utils/time.ts 单元测试：时间格式必须与 CLAUDE.md 约定（YYYY-MM-DD HH:mm:ss）
 * 逐字一致，NULL/undefined 归一为 '-'（后端 epoch 秒为 REAL 且可空）。
 */
import { describe, expect, it } from 'vitest';
import { fmtEpoch, fmtTokens } from './time';

describe('fmtEpoch', () => {
  it('epoch 秒按本地时区格式化为 YYYY-MM-DD HH:mm:ss', () => {
    // 1_700_000_000 = 2023-11-15 06:13:20 UTC；本地时区偏移不同但形状一致
    expect(fmtEpoch(1_700_000_000)).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });

  it('epoch 0 正常格式化（不当作空值）', () => {
    expect(fmtEpoch(0)).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });

  it('null / undefined 归一为 "-"', () => {
    expect(fmtEpoch(null)).toBe('-');
    expect(fmtEpoch(undefined)).toBe('-');
  });

  it('小数秒 epoch 不抛错（后端 REAL 列可能带小数）', () => {
    expect(fmtEpoch(1_700_000_000.5)).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });
});

describe('fmtTokens', () => {
  it('千分位分组（en-US）', () => {
    expect(fmtTokens(1234567)).toBe('1,234,567');
  });

  it('0 展示为 "0" 而非占位符', () => {
    expect(fmtTokens(0)).toBe('0');
  });

  it('null / undefined 归一为 "-"', () => {
    expect(fmtTokens(null)).toBe('-');
    expect(fmtTokens(undefined)).toBe('-');
  });
});
