import { describe, expect, it } from 'vitest';
import {
  BAR_GAP_PX,
  CHART_HEIGHT_PX,
  CHART_TOP_PAD_PX,
  auditBarHeights,
} from './auditChart';

describe('auditBarHeights（QA B-03 柱高上限）', () => {
  it('空数据返回空数组', () => {
    expect(auditBarHeights([])).toEqual([]);
  });

  it('全 0 数据不产生 NaN，柱高为 0', () => {
    expect(auditBarHeights([{ total: 0, error: 0 }])).toEqual([{ totalPx: 0, errorPx: 0 }]);
  });

  it('最高柱（total=max 且 error=max）合计+间隙也不越出容器（旧实现 60+24=84px 溢出 20px）', () => {
    const bars = auditBarHeights([{ total: 100, error: 100 }]);
    const stack = bars[0].totalPx + bars[0].errorPx + BAR_GAP_PX;
    expect(stack).toBeLessThanOrEqual(CHART_HEIGHT_PX - CHART_TOP_PAD_PX);
  });

  it('任意随机数据下所有柱均不越出容器', () => {
    const days = Array.from({ length: 14 }, (_, i) => ({
      total: (i * 37) % 211,
      error: (i * 53) % 97,
    }));
    for (const b of auditBarHeights(days)) {
      expect(b.totalPx + b.errorPx + BAR_GAP_PX).toBeLessThanOrEqual(CHART_HEIGHT_PX - CHART_TOP_PAD_PX);
    }
  });

  it('日间高度保持比例可比（同一比例系数，不逐柱独立归一）', () => {
    const bars = auditBarHeights([
      { total: 10, error: 0 },
      { total: 5, error: 0 },
    ]);
    expect(bars[0].totalPx).toBeGreaterThan(bars[1].totalPx);
    expect(bars[1].totalPx / bars[0].totalPx).toBeCloseTo(0.5, 1);
  });

  it('error 段不会超过 total 段（total≥error 时权重生效）', () => {
    const bars = auditBarHeights([{ total: 50, error: 50 }]);
    expect(bars[0].errorPx).toBeLessThan(bars[0].totalPx);
  });
});
