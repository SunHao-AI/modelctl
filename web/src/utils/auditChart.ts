/**
 * 审计「近 14 天」柱状图的柱高纯计算（QA B-03）。
 *
 * 旧实现按 (total/max)*60px + (error/max)*24px 定值叠加，最高柱合计可达 84px，
 * 越出 h-16（64px）容器压到卡片标题。此处改为按容器实际高度归一：
 * 以「最高柱（total*W_TOTAL + error*W_ERROR 加权合计）」为基准求统一比例系数 k，
 * 任何数据下 最高柱合计 + 堆叠间隙 + 顶部余量 ≤ 容器高度。
 */

/** 图表容器高度 px（对应模板 h-16） */
export const CHART_HEIGHT_PX = 64;
/** 顶部余量 px（保证不贴容器顶） */
export const CHART_TOP_PAD_PX = 4;
/** total 段与 error 段之间的堆叠间隙 px（对应模板 gap-0.5） */
export const BAR_GAP_PX = 2;
/** total 段权重（沿用旧设计 60 : 24 的视觉比例） */
export const W_TOTAL = 60;
/** error 段权重 */
export const W_ERROR = 24;

/** 输入只依赖 total / error 两个数字 */
export interface AuditDayCounts {
  total: number;
  error: number;
}

/** 单日两段的像素高度 */
export interface AuditBarHeights {
  totalPx: number;
  errorPx: number;
}

/**
 * 计算每日柱高：所有柱共享同一比例系数 k（保持日间可比性），
 * 且保证任意数据下 `totalPx + errorPx + BAR_GAP_PX ≤ chartH - CHART_TOP_PAD_PX`。
 */
export function auditBarHeights(
  days: readonly AuditDayCounts[],
  chartH: number = CHART_HEIGHT_PX,
): AuditBarHeights[] {
  if (!days.length) return [];
  const avail = Math.max(0, chartH - CHART_TOP_PAD_PX - BAR_GAP_PX);
  const maxStack = Math.max(...days.map((d) => Math.max(0, d.total) * W_TOTAL + Math.max(0, d.error) * W_ERROR));
  if (maxStack <= 0) return days.map(() => ({ totalPx: 0, errorPx: 0 }));
  const k = avail / maxStack;
  // 向下取整到 0.1px，避免四舍五入把合计顶破上限
  const floorPx = (v: number) => Math.floor(v * 10) / 10;
  return days.map((d) => ({
    totalPx: floorPx(Math.max(0, d.total) * W_TOTAL * k),
    errorPx: floorPx(Math.max(0, d.error) * W_ERROR * k),
  }));
}
