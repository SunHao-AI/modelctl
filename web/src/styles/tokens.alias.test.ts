// @vitest-environment node
/**
 * 语义令牌与「旧色家族别名」是否真的被 UnoCSS 生成为 CSS 变量。
 *
 * 这是整个分批迁移的安全网：别名失效 = 漏改的旧类名渲染成旧色板，
 * 浅色主题下会满屏刺眼且很难定位。所以直接在 CI 里断言生成结果，
 * 而不是靠人工 build + grep。
 */
import { createGenerator } from 'unocss';
import { describe, expect, it } from 'vitest';
import config from '../../unocss.config';

/** 生成 CSS → 把「选择器 → 声明」摊平成 Map（同声明的选择器会被合并成 .a,.b{}） */
async function generate(classes: string[]) {
  const uno = await createGenerator(config);
  const { css } = await uno.generate(classes.join(' '), { preflights: false });
  // layer 注释会被并进选择器，必须先去掉
  const clean = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const map = new Map<string, string>();
  for (const rule of clean.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    for (const one of rule[1].split(',')) map.set(one.trim(), rule[2].trim());
  }
  return map;
}

/** CSS 里 / 与 : 需要转义 */
const sel = (c: string) => '.' + c.replace(/[/]/g, '\\/').replace(/:/g, '\\:');

const SEMANTIC: [string, string][] = [
  ['bg-canvas', 'var(--canvas)'],
  ['bg-surface2', 'var(--surface-2)'],
  ['bg-surface3', 'var(--surface-3)'],
  ['bg-surface4', 'var(--surface-4)'],
  ['text-label', 'var(--label)'],
  ['text-label2', 'var(--label-2)'],
  ['text-label3', 'var(--label-3)'],
  ['border-sep', 'var(--separator)'],
  ['border-sep-soft', 'var(--separator-soft)'],
  ['bg-accent', 'var(--accent)'],
  ['text-accent-fg', 'var(--on-accent)'],
  ['bg-accent-bg', 'var(--accent-bg)'],
  ['border-accent-line', 'var(--accent-line)'],
  ['text-ok', 'var(--ok)'],
  ['bg-ok-bg', 'var(--ok-bg)'],
  ['border-ok-line', 'var(--ok-line)'],
  ['text-danger', 'var(--danger)'],
  ['bg-danger-bg', 'var(--danger-bg)'],
  ['border-danger-line', 'var(--danger-line)'],
  ['text-warn', 'var(--warn)'],
  ['bg-warn-bg', 'var(--warn-bg)'],
  ['border-warn-line', 'var(--warn-line)'],
  ['text-info', 'var(--info)'],
  ['bg-muted', 'var(--muted)'],
  ['bg-code-bg', 'var(--code-bg)'],
  ['text-code-fg', 'var(--code-fg)'],
  ['rounded-ctl', 'var(--r-s)'],
  ['rounded-card', 'var(--r-m)'],
  ['rounded-panel', 'var(--r-l)'],
  ['shadow-s', 'var(--shadow-s)'],
];

const LEGACY: [string, string][] = [
  ['bg-slate-800', 'var(--surface-3)'],
  ['bg-slate-900', 'var(--surface-2)'],
  ['bg-slate-950', 'var(--code-bg)'],
  ['bg-slate-700', 'var(--surface-4)'],
  ['text-slate-400', 'var(--muted)'],
  ['text-red-400', 'var(--danger)'],
  ['bg-red-500', 'var(--danger)'],
  ['text-rose-400', 'var(--danger)'],
  ['bg-emerald-600', 'var(--ok)'],
  ['text-amber-300', 'var(--warn)'],
  ['bg-blue-600', 'var(--accent)'],
  ['text-sky-300', 'var(--info)'],
];

const ALL = [...SEMANTIC, ...LEGACY].map(([c]) => c);

describe('UnoCSS 语义令牌', () => {
  it.each(SEMANTIC)('%s 生成为 %s', async (cls, expected) => {
    const map = await generate(ALL);
    const decl = map.get(sel(cls));
    expect(decl, `${cls} 未生成 —— 检查 theme.colors 键名`).toContain(expected);
  });
});

describe('旧色家族别名（分批迁移的安全网）', () => {
  it.each(LEGACY)('%s 生成为 %s 而非旧 hex', async (cls, expected) => {
    const map = await generate(ALL);
    const decl = map.get(sel(cls));
    expect(decl, `${cls} 未生成 —— theme.colors 可能与 preset 浅合并`).toContain(expected);
    expect(decl, `${cls} 仍是旧 hex：${decl}`).not.toMatch(/#[0-9a-fA-F]{6}/);
  });

  it('未列出的 slate 档位仍由 preset 提供（深合并，非整块覆盖）', async () => {
    const map = await generate(['bg-slate-300', 'bg-slate-100']);
    expect(map.get('.bg-slate-300'), '深合并失效').toBeDefined();
  });
});
