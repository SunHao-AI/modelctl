import { defineConfig, presetWind3, transformerDirectives, transformerVariantGroup } from 'unocss';

/**
 * 语义色一律指向 tokens.css 的 CSS 变量 —— 一套工具类、两套主题值。
 *
 * 两个刻意取舍（详见设计文档 §3.2）：
 * 1) 半透明档位由 -bg / -line 变量固定，**不用** `/20` 透明度修饰符：
 *    变量色配修饰符需 color-mix，旧 WebKit 静默失效。
 * 2) 旧 Tailwind 色家族整族映射到语义变量。全库现存 55 个旧色类名，逐处替换必漏；
 *    有家族别名后漏改的类名自动落到语义色而非旧色板，迁移可安全分批。
 *    其中 slate-800 / red-400 同时充当单测与 e2e 的兼容垫片。
 */
const P = (v: string) => `var(--${v})`;
const ramp = (tok: string, shades: number[]) =>
  Object.fromEntries(shades.map((s) => [String(s), P(tok)]));

export default defineConfig({
  presets: [presetWind3()],
  transformers: [transformerDirectives(), transformerVariantGroup()],
  shortcuts: {
    // p-4 不可省：与重构前 card 语义一致，30+ 处裸用 class="card" 的内边距全靠它；
    // 特例页用 !p-0 / !p-4 / !p-6 覆盖。
    card: 'bg-surface2 border border-sep rounded-card shadow-m p-4',
    'btn-base':
      'inline-flex items-center justify-center gap-2 rounded-ctl px-4 py-2 text-sm font-medium transition-colors cursor-pointer select-none disabled:opacity-60 disabled:cursor-not-allowed',
    'btn-primary': 'btn-base bg-accent text-accent-fg hover:bg-accent-hover shadow-btn',
    'btn-danger': 'btn-base bg-danger-bg text-danger border border-danger-line',
    'btn-ghost': 'btn-base bg-surface3 text-label2 hover:bg-surface4 hover:text-label',
    'input-base':
      'w-full rounded-ctl bg-surface2 border border-sep px-3 py-2 text-sm text-label placeholder-label3 outline-none focus:border-accent focus:shadow-focus',
    'label-base': 'block text-sm font-medium text-label2 mb-1',
    // 卡片级标题统一规范（QA B-07/B-13）：14px/600/label + 卡题→内容 12px 净距
    'card-title': 'text-sm font-semibold text-label mb-3',
    // color-mix / inset 写成 arbitrary value 到处转义，收进 shortcut 最干净
    'shadow-btn': 'shadow-[0_2px_7px_-1px_rgba(0,113,227,0.42),inset_0_1px_0_rgba(255,255,255,0.16)]',
    // QA B-11：与 global.css :focus-visible 光环同参同轨（3.5px / accent 24%）。
    // 必须走 [box-shadow:] 直写形态：shadow-[…] 会在尾部追加未定义的
    // var(--un-shadow-color)，整条 box-shadow 在计算期失效 → 焦点只剩 1px 边框。
    'shadow-focus': '[box-shadow:0_0_0_3.5px_color-mix(in_srgb,var(--accent)_24%,transparent)]',
    // 状态圆点（替代旧 bg-X-400 + 满屏 animate-pulse）
    stonedot: 'inline-block size-1.5 rounded-full shrink-0',
  },
  theme: {
    colors: {
      canvas: P('canvas'),
      surface2: P('surface-2'),
      surface3: P('surface-3'),
      surface4: P('surface-4'),
      label: P('label'),
      label2: P('label-2'),
      label3: P('label-3'),
      sep: P('separator'),
      'sep-soft': P('separator-soft'),
      accent: { DEFAULT: P('accent'), hover: P('accent-hover'), fg: P('on-accent'), bg: P('accent-bg'), line: P('accent-line') },
      ok: { DEFAULT: P('ok'), bg: P('ok-bg'), line: P('ok-line') },
      warn: { DEFAULT: P('warn'), bg: P('warn-bg'), line: P('warn-line') },
      danger: { DEFAULT: P('danger'), bg: P('danger-bg'), line: P('danger-line') },
      info: P('info'),
      muted: P('muted'),
      code: { bg: P('code-bg'), fg: P('code-fg'), line: P('code-line') },

      // —— 旧色家族别名（red 与 rose 都指向 --danger 是有意的：现存代码两套混用）——
      red: ramp('danger', [200, 300, 400, 500, 600, 800, 900]),
      rose: ramp('danger', [300, 400, 500, 800, 900]),
      emerald: ramp('ok', [300, 400, 500, 600]),
      amber: ramp('warn', [200, 300, 400, 500, 700, 800, 900, 950]),
      blue: ramp('accent', [100, 300, 400, 500, 600, 700]),
      sky: ramp('info', [300]),
      slate: {
        ...ramp('muted', [400, 500]),
        600: P('surface-4'),
        700: P('surface-4'),
        800: P('surface-3'),
        900: P('surface-2'),
        950: P('code-bg'),
      },
    },
    borderRadius: { ctl: P('r-s'), card: P('r-m'), panel: P('r-l'), sheet: P('r-xl') },
    boxShadow: { s: P('shadow-s'), m: P('shadow-m'), l: P('shadow-l') },
  },
});
