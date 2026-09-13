// @vitest-environment node
/**
 * 令牌完整性：两套主题的变量集合必须完全一致 —— 少一个变量，另一主题下该属性
 * 会退化成「未定义」，表现为浅色主题下某块区域仍是深色（最难查的一类视觉 bug）。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const css = readFileSync(fileURLToPath(new URL('./tokens.css', import.meta.url)), 'utf8');

/** 取某个 :root[data-theme="x"] 块内声明的所有 --变量名 */
function varsIn(theme: 'light' | 'dark'): string[] {
  const block = css.match(new RegExp(`:root\\[data-theme="${theme}"\\]\\s*\\{([\\s\\S]*?)\\}`))?.[1] ?? '';
  return [...block.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]);
}

const SEMANTIC = ['--canvas', '--canvas-tint', '--chrome', '--surface-2', '--surface-3', '--surface-4',
  '--label', '--label-2', '--label-3', '--separator', '--separator-soft',
  '--accent', '--accent-hover', '--on-accent', '--accent-bg', '--accent-line',
  '--ok', '--ok-bg', '--ok-line', '--warn', '--warn-bg', '--warn-line',
  '--danger', '--danger-bg', '--danger-line', '--info', '--muted',
  '--shadow-s', '--shadow-m', '--shadow-l', '--inset-hl', '--seg-bg', '--seg-knob', '--blur'];

describe('tokens.css', () => {
  it.each(['light', 'dark'] as const)('%s 主题声明了全部语义令牌', (t) => {
    const vars = varsIn(t);
    for (const v of SEMANTIC) expect(vars, `${t} 缺少 ${v}`).toContain(v);
  });

  it('两主题变量集合完全对称', () => {
    expect(new Set(varsIn('dark'))).toEqual(new Set(varsIn('light')));
  });

  it('代码面与形状令牌在 :root（不随主题变）', () => {
    const rootBlock = css.match(/:root\s*\{([\s\S]*?)\}/)?.[1] ?? '';
    for (const v of ['--code-bg', '--code-fg', '--code-line', '--r-s', '--r-m', '--r-l', '--r-xl', '--ease', '--font', '--mono']) {
      expect(rootBlock, `缺少 ${v}`).toContain(`${v}:`);
    }
  });
});
