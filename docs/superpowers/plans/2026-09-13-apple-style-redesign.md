# Apple 化视觉重构 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `web/`（Vue 3 + UnoCSS 运维控制台）的视觉语言重构为苹果系统级观感，并首次落地真正生效的浅色/深色双主题。

**Architecture:** 新增一层 CSS 变量设计令牌（`:root[data-theme]` 两套值），UnoCSS `theme.colors` 全部指向这些变量，视图层只写语义工具类；主题由 Pinia store 写 `<html data-theme>` 驱动；同时给旧 Tailwind 色家族建立「整家族别名」，使未迁移的类名自动落到语义色，让 35 个文件的迁移可分批进行且漏改不致错色。

**Tech Stack:** Vue 3.5 `<script setup>` + TypeScript、UnoCSS 66（presetWind3）、Pinia、@vueuse/core、lucide-vue-next（新增）、Vitest、Playwright。

设计文档：`docs/superpowers/specs/2026-09-13-apple-style-redesign-design.md`
可交互原型：`.superpowers/brainstorm/28100-1789269150/content/prototype.html`

## Global Constraints

对**每个任务**隐式生效，值逐字来自设计文档与 `CLAUDE.md`。

- 不改路由结构、API 调用、Pinia 数据流；不引入组件库；不删改业务逻辑。
- UI 一律显示虚拟 ID，不显示真实 ID（例外：集群视图直接显示 `node_id`）。
- 所有时间格式 `YYYY-MM-DD HH:mm:ss`。
- CSS 类名遵循 BEM；改子组件内部样式必须用 `:deep()`。
- **测试红线（改错即红）**：`#api-key` 保留；`p.text-red-400` 保留（标签必须是 `<p>`、类名含 `text-red-400`）；`ChatHistoryList` 条目必须是 `div.cursor-pointer` 且 active 态含 `bg-slate-800`、非 active 不含；「新建」必须是该组件模板第一个 `<button>`；`<aside>`/`<nav>`/`<h1>`/`<h2>` 语义标签保留；侧栏项必须是真实 `<a>`（`router-link`）且含文案「体检」；侧栏 `hidden md:flex` 的 768px 断点保留；任何视口横向溢出 ≤1px。
- **逻辑红线**：一次性密钥弹窗必须 `v-if`（禁止 `v-show`）；`TaskButton` 的 watch getter 内对 `now.value` 的读取不得删除；Dashboard 3s 轮询的 `pending` 防重叠 + abort 静默判定不动；`chat/index.vue` 的 `defineOptions({ name: 'chat' })` 不动；凡「回车即提交」的输入必须保留 `if (e.isComposing || e.keyCode === 229) return`；`api/sse.ts` 一行不改；`AccountsView` 的 `accounts_disabled` 503 引导条分派保留；`EnvsView` 的 `id="env-row-{name}"` 与 `id="docker-bypass"` 锚点保留。
- 代码面（YAML 编辑器 / SSE 日志 / JSON 展开）两主题**恒深**。
- `backdrop-filter` 只用于侧栏 / 顶栏 / 抽屉 / 弹窗遮罩四类固定元素，卡片不加模糊（性能红线）。
- 每批结束跑（工作目录 `d:\Workplace\modelctl-1\web`）：`npm run typecheck` 然后 `npm run test`，全绿才提交。

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `web/src/styles/tokens.css` | 新建 | 设计令牌唯一真源 |
| `web/src/styles/tokens.test.ts` · `tokens.alias.test.ts` | 新建 | 令牌对称性 + 别名生效（迁移安全网） |
| `web/src/test/setup.ts` | 新建 | 补 jsdom 缺失的 `matchMedia` |
| `web/unocss.config.ts` | 重写 | 语义色/圆角/阴影 → 变量；旧色家族别名；shortcuts |
| `web/src/styles/global.css` | 重写 | body 基线、`.glass`、`.num`、光斑、转场、滚动条 |
| `web/index.html` | 改 | `data-theme` + 防 FOUC 内联脚本 |
| `web/src/stores/theme.ts` | 新建 | 三态主题 + 持久化 + 写 `data-theme` |
| `web/src/components/common/ThemeSwitch.vue` | 新建 | 三段 Segmented Control |
| `web/src/components/common/DataTable.vue` | 新建 | 精修表格容器（包裹式） |
| `web/src/components/layout/{Layout,Header,Sidebar}.vue` | 改 | 材质、移动导航抽屉、lucide 图标、分组 |
| `web/src/components/common/*`（6） | 改 | 语义色；`Loading` 收敛为唯一 spinner |
| `web/src/components/chat/*`（6） | 改 | 语义色 + markdown 排版 |
| `web/src/views/**`（17） | 改 | 语义色；5 个卡片范式、6 个套 `DataTable` |
| `web/src/utils/toast.ts` | 改 | `KIND_CLASS` 换语义色（键名不动） |

任务顺序即依赖顺序：令牌层 → 主题 → 壳 → 通用组件 → 视图（卡片 / 表格 / 专项）→ 全局校验 → 沉淀。

---

## 任务 1：设计令牌层

**Files:**
- Create: `web/src/styles/tokens.css`
- Create: `web/src/styles/tokens.test.ts`
- Create: `web/src/styles/tokens.alias.test.ts`
- Modify: `web/unocss.config.ts`（全文重写）
- Modify: `web/src/styles/global.css`（全文重写）
- Modify: `web/src/main.ts`
- Modify: `web/index.html`

**Interfaces:**
- Consumes: 无
- Produces:
  - CSS 变量：`--canvas` `--canvas-tint` `--chrome` `--surface-2/3/4` `--label` `--label-2` `--label-3` `--separator` `--separator-soft` `--accent` `--accent-hover` `--on-accent` `--accent-bg` `--accent-line` `--ok/-bg/-line` `--warn/-bg/-line` `--danger/-bg/-line` `--info` `--muted` `--shadow-s/m/l` `--inset-hl` `--seg-bg` `--seg-knob` `--blur` `--code-bg` `--code-fg` `--code-line` `--r-s/m/l/xl` `--ease` `--font` `--mono`
  - 语义工具类：`bg-canvas` `bg-surface2/3/4` `text-label/label2/label3` `border-sep` `bg-accent` `text-accent-fg` `bg-accent-bg` `border-accent-line` `text-ok` `bg-ok-bg` `border-ok-line`（warn/danger 同构）`text-info` `bg-muted` `bg-code-bg` `text-code-fg` `rounded-ctl/card/panel/sheet` `shadow-s/m/l`
  - 原生工具类：`.glass` `.num` `.code-surface` `.stonedot`
  - localStorage 键：`modelctl_theme`

- [ ] **步骤 1：写令牌完整性测试（先失败）**

创建 `web/src/styles/tokens.test.ts`：

```ts
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
```

- [ ] **步骤 2：跑测试确认失败**

Run: `cd d:\Workplace\modelctl-1\web && npx vitest run src/styles/tokens.test.ts`
Expected: FAIL —— `ENOENT ... tokens.css`

- [ ] **步骤 3：创建 `web/src/styles/tokens.css`**

```css
/* 设计令牌唯一真源。语义命名，业务代码禁止引用具体色值。
   浅色 = iOS 留白（靠阴影分层）；深色 = macOS 毛玻璃（靠半透明 + 内高光分层）。 */

:root[data-theme="light"] {
  --canvas: #f2f3f7;
  --canvas-tint: radial-gradient(120% 80% at 100% 0%, rgba(0,113,227,.06) 0%, transparent 55%);
  --chrome: rgba(255,255,255,.72);
  --surface-2: #ffffff;
  --surface-3: #f4f5f8;
  --surface-4: #eceef3;
  --label: #0f1219;
  --label-2: #525b6b;
  --label-3: #8790a0;
  --separator: rgba(0,0,0,.075);
  --separator-soft: rgba(0,0,0,.045);
  --accent: #0071e3;
  --accent-hover: #0077ed;
  --on-accent: #ffffff;
  --accent-bg: rgba(0,113,227,.10);
  --accent-line: rgba(0,113,227,.22);
  --ok: #0a8a4a;      --ok-bg: rgba(10,138,74,.09);     --ok-line: rgba(10,138,74,.20);
  --warn: #a15c07;    --warn-bg: rgba(161,92,7,.09);    --warn-line: rgba(161,92,7,.20);
  --danger: #d0331f;  --danger-bg: rgba(208,51,31,.08); --danger-line: rgba(208,51,31,.20);
  --info: #0a6ed1;
  --muted: #c2c8d4;
  --shadow-s: 0 1px 2px rgba(16,24,40,.05);
  --shadow-m: 0 1px 2px rgba(16,24,40,.05), 0 10px 26px -14px rgba(16,24,40,.22);
  --shadow-l: 0 2px 6px rgba(16,24,40,.07), 0 24px 60px -24px rgba(16,24,40,.30);
  --inset-hl: none;
  --seg-bg: rgba(120,126,140,.14);
  --seg-knob: #ffffff;
  --blur: saturate(180%) blur(20px);
}

:root[data-theme="dark"] {
  --canvas: #0a0c11;
  --canvas-tint: radial-gradient(110% 75% at 12% -8%, rgba(59,130,246,.16) 0%, transparent 55%),
                 radial-gradient(90% 70% at 96% -4%, rgba(16,185,129,.10) 0%, transparent 52%);
  --chrome: rgba(26,30,40,.60);
  --surface-2: rgba(255,255,255,.055);
  --surface-3: rgba(255,255,255,.085);
  --surface-4: rgba(255,255,255,.12);
  --label: #f1f4f9;
  --label-2: #a9b3c4;
  --label-3: #78829a;
  --separator: rgba(255,255,255,.085);
  --separator-soft: rgba(255,255,255,.05);
  --accent: #4c9bff;
  --accent-hover: #66abff;
  --on-accent: #04101f;
  --accent-bg: rgba(76,155,255,.14);
  --accent-line: rgba(76,155,255,.26);
  --ok: #3ddc97;      --ok-bg: rgba(61,220,151,.11);    --ok-line: rgba(61,220,151,.22);
  --warn: #f5c451;    --warn-bg: rgba(245,196,81,.11);  --warn-line: rgba(245,196,81,.22);
  --danger: #ff7a85;  --danger-bg: rgba(255,122,133,.10); --danger-line: rgba(255,122,133,.22);
  --info: #6ab8ff;
  --muted: #4c5771;
  --shadow-s: 0 2px 8px rgba(0,0,0,.28);
  --shadow-m: 0 4px 14px rgba(0,0,0,.34), inset 0 1px 0 rgba(255,255,255,.06);
  --shadow-l: 0 18px 50px -18px rgba(0,0,0,.70), inset 0 1px 0 rgba(255,255,255,.07);
  --inset-hl: inset 0 1px 0 rgba(255,255,255,.07);
  --seg-bg: rgba(255,255,255,.085);
  --seg-knob: rgba(255,255,255,.96);
  --blur: saturate(180%) blur(22px);
}

/* 不随主题变：代码面恒深（设计文档 D7）+ 形状 / 动效 / 字体 */
:root {
  --code-bg: #0d1117;
  --code-fg: #c9d4e3;
  --code-line: rgba(255,255,255,.07);
  --r-s: 7px;
  --r-m: 11px;
  --r-l: 15px;
  --r-xl: 20px;
  --ease: cubic-bezier(.32,.72,0,1);
  --font: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  --mono: ui-monospace, "SF Mono", JetBrains Mono, Menlo, Consolas, monospace;
}
```

- [ ] **步骤 4：跑测试确认通过**

Run: `cd d:\Workplace\modelctl-1\web && npx vitest run src/styles/tokens.test.ts`
Expected: PASS（3 用例）

- [ ] **步骤 5：重写 `web/unocss.config.ts`**

```ts
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
    card: 'bg-surface2 border border-sep rounded-card shadow-m',
    'btn-base':
      'inline-flex items-center justify-center gap-2 rounded-ctl px-4 py-2 text-sm font-medium transition-colors cursor-pointer select-none disabled:opacity-40 disabled:cursor-not-allowed',
    'btn-primary': 'btn-base bg-accent text-accent-fg hover:bg-accent-hover shadow-btn',
    'btn-danger': 'btn-base bg-danger-bg text-danger border border-danger-line',
    'btn-ghost': 'btn-base bg-surface3 text-label2 hover:bg-surface4 hover:text-label',
    'input-base':
      'w-full rounded-ctl bg-surface2 border border-sep px-3 py-2 text-sm text-label placeholder-label3 outline-none focus:border-accent focus:shadow-focus',
    'label-base': 'block text-sm font-medium text-label2 mb-1',
    // color-mix / inset 写成 arbitrary value 到处转义，收进 shortcut 最干净
    'shadow-btn': 'shadow-[0_2px_7px_-1px_rgba(0,113,227,0.42),inset_0_1px_0_rgba(255,255,255,0.16)]',
    'shadow-focus': 'shadow-[0_0_0_3.5px_color-mix(in_srgb,var(--accent)_17%,transparent)]',
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
```

> 圆角键名刻意避开 Tailwind 已有语义：`rounded-s` / `rounded-l` 在 preset 里已是「起始侧 / 左侧圆角」，占用会静默失效。

- [ ] **步骤 6：重写 `web/src/styles/global.css`**

```css
/* 全局基线：全部吃 tokens.css 的语义令牌，不出现具体色值 */
*,
*::before,
*::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

html,
body,
#app {
  height: 100%;
}

body {
  background-color: var(--canvas);
  color: var(--label);
  font-family: var(--font);
  font-size: 14px;
  line-height: 1.47;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  transition: background-color 0.42s var(--ease), color 0.42s var(--ease);
}

/* 背景光斑：浅色极淡、深色蓝绿双光斑。固定层，不参与交互。 */
body::before {
  content: '';
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background-image: var(--canvas-tint);
}

/* 材质层：唯一使用 backdrop-filter 的类。
   只给侧栏 / 顶栏 / 抽屉 / 弹窗遮罩四类固定元素；卡片不加模糊（性能红线）。 */
.glass {
  background-color: var(--chrome);
  -webkit-backdrop-filter: var(--blur);
  backdrop-filter: var(--blur);
}

@supports not (backdrop-filter: blur(1px)) {
  .glass {
    background-color: var(--canvas);
  }
}

/* 数字 / 时间等宽，避免表格与 KPI 跳动 */
.num {
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
}

/* 代码面恒深 */
.code-surface {
  background-color: var(--code-bg);
  color: var(--code-fg);
  font-family: var(--mono);
}

/* 路由切换：淡入 + 极小位移 */
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s var(--ease), transform 0.2s var(--ease);
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
  transform: translateY(4px);
}

a {
  color: inherit;
  text-decoration: none;
}

button {
  font-family: inherit;
  cursor: pointer;
}

input,
select,
textarea {
  font-family: inherit;
}

/* 细滚动条：透明 border 撑出内缩 */
::-webkit-scrollbar {
  width: 10px;
  height: 10px;
}
::-webkit-scrollbar-thumb {
  background: var(--surface-4);
  border-radius: 99px;
  border: 3px solid transparent;
  background-clip: content-box;
}
::-webkit-scrollbar-track {
  background: transparent;
}

/* 31px 方形图标按钮（顶栏通用） */
.iconbtn {
  position: relative;
  display: grid;
  place-items: center;
  width: 31px;
  height: 31px;
  border-radius: var(--r-s);
  border: 0.5px solid var(--separator);
  background: var(--surface-2);
  color: var(--label-2);
  transition: background 0.18s var(--ease), color 0.18s var(--ease);
}
.iconbtn:hover {
  background: var(--surface-3);
  color: var(--label);
}

:focus-visible {
  outline: none;
  border-radius: var(--r-s);
  box-shadow: 0 0 0 3.5px color-mix(in srgb, var(--accent) 24%, transparent);
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    transition-duration: 0.01ms !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
  }
}
```

- [ ] **步骤 7：接进 `main.ts` 与 `index.html`**

`web/src/main.ts` 第 5-6 行替换（tokens 先于 global）：

```ts
import 'uno.css';
import './styles/tokens.css';
import './styles/global.css';
```

`web/index.html` 整体替换：

```html
<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="description" content="modelctl 模型管控 Web UI" />
    <title>modelctl Web UI</title>
    <script>
      // 早于样式生效敲定主题，避免浅色用户首屏闪一下深色。
      // 与 stores/theme.ts 共用同一 localStorage 键；缺省 = 跟随系统。
      (function () {
        var m = localStorage.getItem('modelctl_theme');
        var dark = m ? m === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
        document.documentElement.dataset.theme = dark ? 'dark' : 'light';
      })();
    </script>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="./src/main.ts"></script>
  </body>
</html>
```

- [ ] **步骤 8：把「别名表生效」写成常驻测试（替代 build + grep）**

这一步是整套迁移方案的地基：如果 `theme.colors` 与 presetWind3 是浅合并（整块覆盖），漏改的旧类名会渲染成旧 Tailwind 色板，浅色主题下满屏刺眼。与其每次 build 再 grep 产物，不如直接驱动 UnoCSS 生成器断言。

**已实测结论**（本计划编写时在本仓库跑过）：`theme.colors` 与 preset 是**深合并** —— 只列出的档位被覆盖，其余仍可用；`bg-slate-800 → var(--surface-3)`、`text-red-400 → var(--danger)`、`bg-emerald-600 → var(--ok)`、`text-amber-300 → var(--warn)`、`bg-code-bg → var(--code-bg)`、`bg-black/40 → rgb(0 0 0 / 0.4)` 全部生成且无旧 hex。所以下面的测试应当直接通过；若它红了，说明 UnoCSS 版本行为变了，按测试末尾的兜底方案处理。

创建 `web/src/styles/tokens.alias.test.ts`：

```ts
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
  const { css } = await uno.generate(classes.join(' '), { preflight: false });
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
```

> 兜底方案（仅当 `bg-slate-300` 那条红了，即浅合并）：删掉 `unocss.config.ts` 里的 `red`/`rose`/`emerald`/`amber`/`blue`/`sky`/`slate` 七个别名块，改在 `tokens.css` 末尾补两条测试钉住的原语 `.text-red-400{color:var(--danger)}` 与 `.bg-slate-800{background-color:var(--surface-3)}`，并把本测试的 `LEGACY` 收缩为这两条 + 一句注释说明原因；同时**任务 8/9/10 的替换必须做到零残留**（不再有别名兜底），任务 11 步骤 1 的 grep 从"允许少量残留"改为"必须为空"。

- [ ] **步骤 9：跑别名测试**

Run: `cd d:\Workplace\modelctl-1\web && npx vitest run src/styles`
Expected: PASS（语义 30 用例 + 别名 12 用例 + 深合并 1 用例）

- [ ] **步骤 10：全量测试 + 提交**

Run: `cd d:\Workplace\modelctl-1\web && npm run typecheck && npm run test`
Expected: 全绿

```bash
cd d:\Workplace\modelctl-1
git add web/src/styles/tokens.css web/src/styles/tokens.test.ts web/src/styles/tokens.alias.test.ts web/src/styles/global.css web/unocss.config.ts web/src/main.ts web/index.html
git commit -m "feat(web): 设计令牌层 —— CSS 变量 + UnoCSS 语义色 + 旧色家族别名"
```

---

## 任务 2：主题 store

**Files:**
- Create: `web/src/test/setup.ts`
- Modify: `web/vite.config.ts`（`test` 块加 `setupFiles`）
- Create: `web/src/stores/theme.ts`
- Create: `web/src/stores/theme.test.ts`
- Modify: `web/src/main.ts`

**Interfaces:**
- Consumes: 任务 1 的 `data-theme` 契约与 `localStorage['modelctl_theme']`
- Produces:
  - `type ThemeMode = 'auto' | 'light' | 'dark'`、`type ResolvedTheme = 'light' | 'dark'`
  - `function resolveTheme(mode: ThemeMode, systemPrefersDark: boolean): ResolvedTheme`
  - `const THEME_STORAGE_KEY = 'modelctl_theme'`
  - `useThemeStore()` → `{ mode, resolved, systemDark, setMode, apply }`
  - 测试全局钩子 `globalThis.__setSystemDark(v: boolean)`（仅测试环境存在）

- [ ] **步骤 0：补 jsdom 缺失的 `matchMedia`（本任务必须先做，否则测试全部假绿）**

**已实测确认**：本项目 vitest 跑 `environment: 'jsdom'`（`vite.config.ts:46`），jsdom 25 **不实现 `window.matchMedia`**，实测值为 `undefined`。`usePreferredDark()` 依赖 `window.matchMedia`，缺失时它会静默退化 —— `systemDark` 恒为 `false`，「跟随系统 → 深色」这条分支永远测不到，测试仍显示通过（假绿）。同理 `index.html` 的内联脚本在 jsdom 里也会 `matchMedia is not a function`。

创建 `web/src/test/setup.ts`：

```ts
/**
 * jsdom 不实现 window.matchMedia（实测 typeof === 'undefined'）。
 * usePreferredDark / @vueuse 依赖它，缺失时会静默退化成「系统恒浅色」，
 * 让主题测试假绿。这里补一个可被测试驱动的极简实现。
 */
type MqListener = (e: { matches: boolean }) => void;

const listeners = new Set<MqListener>();
let systemDark = false;

function install() {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      media: query,
      get matches() {
        return systemDark;
      },
      onchange: null,
      addEventListener: (_: string, cb: MqListener) => listeners.add(cb),
      removeEventListener: (_: string, cb: MqListener) => listeners.delete(cb),
      addListener: (cb: MqListener) => listeners.add(cb),
      removeListener: (cb: MqListener) => listeners.delete(cb),
      dispatchEvent: () => true,
    }),
  });
}

install();

/** 测试专用：切换"系统偏好"并派发 change，驱动 usePreferredDark 重算 */
(globalThis as unknown as { __setSystemDark: (v: boolean) => void }).__setSystemDark = (v: boolean) => {
  systemDark = v;
  for (const cb of listeners) cb({ matches: v });
};
```

在 `web/vite.config.ts` 的 `test` 块内（`environment: 'jsdom'` 之后）加一行：

```ts
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
```

验证桩生效（临时探针，跑完即删）：

```powershell
cd d:\Workplace\modelctl-1\web
"import { describe, expect, it } from 'vitest';`ndescribe('p', () => it('mm', () => { console.log('MM=' + typeof window.matchMedia); expect(typeof window.matchMedia).toBe('function'); }));" | Out-File -Encoding utf8 src\__probe.test.ts
npx vitest run src/__probe.test.ts
Remove-Item src\__probe.test.ts
```

Expected：输出 `MM=function` 且用例通过。若仍是 `undefined`，说明 `setupFiles` 路径不对（相对 `web/` 而非仓库根），修正后重跑。

- [ ] **步骤 1：写失败测试**

创建 `web/src/stores/theme.test.ts`：

```ts
/**
 * 主题 store 三态语义：
 *  - 'auto' 是**默认且唯一缺省态**，跟随系统，且**不写** localStorage
 *  - 只有显式选 light/dark 才持久化；选回 auto 必须 removeItem
 *  （若 auto 也写盘，「跟随系统」的意图就被固化成当下值，之后改系统偏好不再跟随）
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { resolveTheme, useThemeStore, THEME_STORAGE_KEY } from './theme';

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  setActivePinia(createPinia());
});

describe('resolveTheme 纯函数', () => {
  it('auto：系统深色→dark，系统浅色→light', () => {
    expect(resolveTheme('auto', true)).toBe('dark');
    expect(resolveTheme('auto', false)).toBe('light');
  });
  it('light/dark：无视系统偏好', () => {
    for (const sys of [true, false]) {
      expect(resolveTheme('light', sys)).toBe('light');
      expect(resolveTheme('dark', sys)).toBe('dark');
    }
  });
});

describe('useThemeStore', () => {
  it('无持久化记录时 mode 为 auto', () => {
    expect(useThemeStore().mode).toBe('auto');
  });

  it('localStorage 合法值会被 hydrate', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'light');
    setActivePinia(createPinia());
    expect(useThemeStore().mode).toBe('light');
  });

  it('localStorage 非法值回落 auto（手改坏数据不炸）', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'neon');
    setActivePinia(createPinia());
    expect(useThemeStore().mode).toBe('auto');
  });

  it('setMode("light") 写盘并把 data-theme 落到 <html>', () => {
    useThemeStore().setMode('light');
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');
    expect(document.documentElement.dataset.theme).toBe('light');
  });

  it('setMode("auto") 清盘（保留跟随系统的意图）', () => {
    const t = useThemeStore();
    t.setMode('dark');
    t.setMode('auto');
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
  });

  it('apply() 按 resolved 落 data-theme', () => {
    const t = useThemeStore();
    t.setMode('dark');
    t.apply();
    expect(document.documentElement.dataset.theme).toBe('dark');
  });

  /**
   * 这两条依赖 src/test/setup.ts 的 matchMedia 桩（jsdom 原生没有 matchMedia，
   * 不装桩则 systemDark 恒 false，下面第一条会以错误的方式"通过"）。
   */
  it('auto + 系统深色：resolved 为 dark 且 apply 落 dark', () => {
    const setSystemDark = (globalThis as unknown as { __setSystemDark: (v: boolean) => void }).__setSystemDark;
    expect(setSystemDark, 'matchMedia 桩未生效，检查 vite.config.ts 的 setupFiles').toBeTypeOf('function');
    setSystemDark(true);
    const t = useThemeStore();
    t.setMode('auto');
    expect(t.resolved).toBe('dark');
  });

  it('auto：系统偏好翻转会驱动 resolved 重算（响应式，非一次性读取）', async () => {
    const { nextTick } = await import('vue');
    const setSystemDark = (globalThis as unknown as { __setSystemDark: (v: boolean) => void }).__setSystemDark;
    setSystemDark(false);
    const t = useThemeStore();
    t.setMode('auto');
    expect(t.resolved).toBe('light');
    setSystemDark(true);
    await nextTick();
    expect(t.resolved).toBe('dark');
  });
});
```

- [ ] **步骤 2：跑测试确认失败**

Run: `cd d:\Workplace\modelctl-1\web && npx vitest run src/stores/theme.test.ts`
Expected: FAIL —— `Failed to resolve import "./theme"`

- [ ] **步骤 3：创建 `web/src/stores/theme.ts`**

```ts
import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import { usePreferredDark } from '@vueuse/core';

/** 仅存**手动**选择；缺省（无记录）即 'auto' = 跟随系统 */
export const THEME_STORAGE_KEY = 'modelctl_theme';

export type ThemeMode = 'auto' | 'light' | 'dark';
export type ResolvedTheme = 'light' | 'dark';

const MODES: readonly ThemeMode[] = ['auto', 'light', 'dark'];

/** 纯函数：三态真值表，单测直接覆盖，不依赖 DOM */
export function resolveTheme(mode: ThemeMode, systemPrefersDark: boolean): ResolvedTheme {
  if (mode === 'auto') return systemPrefersDark ? 'dark' : 'light';
  return mode;
}

function readStored(): ThemeMode {
  const raw = localStorage.getItem(THEME_STORAGE_KEY);
  return MODES.includes(raw as ThemeMode) ? (raw as ThemeMode) : 'auto';
}

/**
 * 主题 store。
 * 必须用 usePreferredDark()：裸 matchMedia(...).matches 非响应式，
 * computed 里读它不会在系统换肤时重算（设计文档 §4.1）。
 */
export const useThemeStore = defineStore('theme', () => {
  const mode = ref<ThemeMode>(readStored());
  const systemDark = usePreferredDark();

  const resolved = computed<ResolvedTheme>(() => resolveTheme(mode.value, systemDark.value));

  function apply() {
    document.documentElement.dataset.theme = resolved.value;
  }

  function setMode(next: ThemeMode) {
    if (!MODES.includes(next)) return;
    mode.value = next;
    if (next === 'auto') localStorage.removeItem(THEME_STORAGE_KEY);
    else localStorage.setItem(THEME_STORAGE_KEY, next);
    apply();
  }

  return { mode, resolved, systemDark, setMode, apply };
});
```

- [ ] **步骤 4：跑测试确认通过**

Run: `cd d:\Workplace\modelctl-1\web && npx vitest run src/stores/theme.test.ts`
Expected: PASS（10 用例）。若最后两条报 `matchMedia 桩未生效` → 回步骤 0 检查 `setupFiles` 路径。

- [ ] **步骤 5：`main.ts` 挂载前应用主题**

`web/src/main.ts` 整体替换：

```ts
import { createApp } from 'vue';
import { createPinia } from 'pinia';
import App from './App.vue';
import router from './router';
import { useThemeStore } from './stores/theme';
import 'uno.css';
import './styles/tokens.css';
import './styles/global.css';

const app = createApp(App);
app.use(createPinia());
// 与 index.html 内联脚本结果对齐（内联脚本可能因隐私模式读不到 localStorage）
useThemeStore().apply();
app.use(router);
app.mount('#app');
```

- [ ] **步骤 6：全量测试 + 提交**

Run: `cd d:\Workplace\modelctl-1\web && npm run typecheck && npm run test`

```bash
cd d:\Workplace\modelctl-1
git add web/src/test/setup.ts web/vite.config.ts web/src/stores/theme.ts web/src/stores/theme.test.ts web/src/main.ts
git commit -m "feat(web): 三态主题 store（跟随系统/浅色/深色）并接管 data-theme"
```

---

## 任务 3：ThemeSwitch + 替换设置页占位开关

**Files:**
- Create: `web/src/components/common/ThemeSwitch.vue`
- Create: `web/src/components/common/ThemeSwitch.test.ts`
- Modify: `web/src/views/SettingsView.vue`（删 `darkTheme` 占位 → 插 `<ThemeSwitch/>`）

**Interfaces:**
- Consumes: 任务 2 的 `useThemeStore()` → `{ mode, resolved, setMode }`
- Produces: `<ThemeSwitch />`，无 props / 无 emits；渲染 `role="radiogroup"` + 三个 `role="radio"`（顺序：浅色 / 深色 / 跟随系统）

- [ ] **步骤 1：装 lucide（本任务起需要图标）**

Run: `cd d:\Workplace\modelctl-1\web && npm i lucide-vue-next`
Expected: `package.json` dependencies 出现 `"lucide-vue-next"`

- [ ] **步骤 2：写失败测试**

创建 `web/src/components/common/ThemeSwitch.test.ts`：

```ts
/**
 * ThemeSwitch 本身无逻辑，但「三态 + 点击驱动 store」是主题功能唯一可在前端
 * 验证的部分：若点「浅色」没调 setMode('light')，用户切换就没有任何效果。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const setMode = vi.fn();
vi.mock('@/stores/theme', () => ({
  useThemeStore: () => ({ mode: 'auto', resolved: 'dark', setMode }),
}));

import ThemeSwitch from './ThemeSwitch.vue';

beforeEach(() => {
  setActivePinia(createPinia());
  setMode.mockReset();
});

it('容器是 radiogroup', () => {
  expect(mount(ThemeSwitch).get('[role="radiogroup"]').exists()).toBe(true);
});

it('渲染三态 radio，顺序为 浅色 / 深色 / 跟随系统', () => {
  const radios = mount(ThemeSwitch).findAll('[role="radio"]');
  expect(radios).toHaveLength(3);
  expect(radios.map((r) => r.text())).toEqual(['浅色', '深色', '跟随系统']);
});

it('点击「浅色」调 setMode("light")', async () => {
  await mount(ThemeSwitch).findAll('[role="radio"]')[0].trigger('click');
  expect(setMode).toHaveBeenCalledWith('light');
});

it('点击「跟随系统」调 setMode("auto")', async () => {
  await mount(ThemeSwitch).findAll('[role="radio"]')[2].trigger('click');
  expect(setMode).toHaveBeenCalledWith('auto');
});

it('当前 mode 对应的 radio aria-checked=true', () => {
  const radios = mount(ThemeSwitch).findAll('[role="radio"]');
  expect(radios[2].attributes('aria-checked')).toBe('true');
  expect(radios[0].attributes('aria-checked')).toBe('false');
});
```

- [ ] **步骤 3：跑测试确认失败**

Run: `cd d:\Workplace\modelctl-1\web && npx vitest run src/components/common/ThemeSwitch.test.ts`
Expected: FAIL —— `Failed to resolve import "./ThemeSwitch.vue"`

- [ ] **步骤 4：创建 `web/src/components/common/ThemeSwitch.vue`**

```vue
<script setup lang="ts">
import { Monitor, Moon, Sun, type LucideIcon } from 'lucide-vue-next';
import { useThemeStore, type ThemeMode } from '@/stores/theme';

/**
 * 外观切换：苹果三段 Segmented Control。
 * 无 props —— 主题全站唯一真源是 store。
 */
const theme = useThemeStore();

const OPTIONS: { value: ThemeMode; label: string; icon: LucideIcon }[] = [
  { value: 'light', label: '浅色', icon: Sun },
  { value: 'dark', label: '深色', icon: Moon },
  { value: 'auto', label: '跟随系统', icon: Monitor },
];
</script>

<template>
  <div
    role="radiogroup"
    aria-label="外观"
    class="inline-flex gap-px rounded-ctl p-0.5"
    style="background: var(--seg-bg); box-shadow: inset 0 0 0 0.5px var(--separator)"
  >
    <button
      v-for="o in OPTIONS"
      :key="o.value"
      type="button"
      role="radio"
      :aria-checked="theme.mode === o.value"
      :class="[
        'inline-flex items-center gap-1.5 rounded-ctl px-3 py-1.5 text-xs transition-all duration-200',
        theme.mode === o.value ? 'font-semibold text-label' : 'font-medium text-label2 hover:text-label',
      ]"
      :style="
        theme.mode === o.value
          ? 'background: var(--seg-knob); box-shadow: 0 1px 3px rgba(0,0,0,.22), 0 0 0 .5px var(--separator)'
          : ''
      "
      @click="theme.setMode(o.value)"
    >
      <component :is="o.icon" :size="13" :stroke-width="1.9" />
      {{ o.label }}
    </button>
  </div>
</template>
```

- [ ] **步骤 5：跑测试确认通过**

Run: `cd d:\Workplace\modelctl-1\web && npx vitest run src/components/common/ThemeSwitch.test.ts`
Expected: PASS（5 用例）

- [ ] **步骤 6：替换 SettingsView 的占位开关**

`web/src/views/SettingsView.vue` 四处改动：

1. import 区追加：

```ts
import ThemeSwitch from '@/components/common/ThemeSwitch.vue';
import { useThemeStore } from '@/stores/theme';
```

2. 删除第 27-28 行的占位声明：

```ts
/** 主题标识（暂存；不做实际主题切换，仅占位） */
const darkTheme = ref<boolean>(true);
```

在其原位置改为：

```ts
const theme = useThemeStore();
```

3. 把第 121-132 行「主题（占位）」整个 `<section>` 替换为：

```html
    <!-- 外观 -->
    <section class="card">
      <h3 class="mb-3 text-sm font-semibold text-label">外观</h3>
      <div class="flex flex-wrap items-center gap-3">
        <ThemeSwitch />
        <span class="text-xs text-label3">当前生效：{{ theme.resolved === 'dark' ? '深色' : '浅色' }}</span>
      </div>
      <p class="mt-2 text-xs text-label3">
        「跟随系统」随系统深浅色偏好实时切换；手动选择会记住并覆盖系统设置。
      </p>
    </section>
```

> `ref` 在该文件其它位置仍在用，`import { onMounted, ref }` 保持不动。

- [ ] **步骤 7：全量测试 + 提交**

Run: `cd d:\Workplace\modelctl-1\web && npm run typecheck && npm run test`

```bash
cd d:\Workplace\modelctl-1
git add web/package.json web/package-lock.json web/src/components/common/ThemeSwitch.vue web/src/components/common/ThemeSwitch.test.ts web/src/views/SettingsView.vue
git commit -m "feat(web): 外观切换生效 —— ThemeSwitch 替换设置页占位开关"
```

---

## 任务 4：侧栏（lucide 图标 + 语义分组 + 材质）

**Files:**
- Modify: `web/src/components/layout/Sidebar.vue`（全文重写）

**Interfaces:**
- Consumes: 任务 1 的 `.glass` / 语义类；任务 3 安装的 `lucide-vue-next`
- Produces: `<aside>` 内含 `<nav>`，菜单项仍是 `router-link` 渲染的真实 `<a>`；`menus` 每项新增 `group: '概览' | '运维' | '系统'`；13 项的 `to` / `label` / 顺序完全不变

- [ ] **步骤 1：确认 13 个图标名都存在于 lucide**

```powershell
cd d:\Workplace\modelctl-1\web
node -e "const l=require('lucide-vue-next');['LayoutDashboard','Boxes','Server','MessageSquareSparkle','Layers','Activity','Target','Network','ScrollText','ShieldCheck','UserRound','Database','Settings'].forEach(n=>console.log(n,typeof l[n]))"
```

Expected: 13 行全部非 `undefined`。若某项 `undefined`，用下表备用名替换（语义不变）：

| 菜单 | 主选 | 备用 |
|---|---|---|
| 仪表板 | `LayoutDashboard` | `LayoutGrid` |
| 模型 | `Boxes` | `Package` |
| 服务 | `Server` | `ServerCog` |
| AI 对话 | `MessageSquareSparkle` | `MessageSquare` |
| 环境 | `Layers` | `FolderTree` |
| 体检 | `Activity` | `HeartPulse` |
| 集群目标 | `Target` | `Crosshair` |
| 集群 | `Network` | `Cpu` |
| 审计 | `ScrollText` | `FileText` |
| 账号管理 | `ShieldCheck` | `Shield` |
| 我的账号 | `UserRound` | `User` |
| 配置 | `Database` | `ScrollText` |
| 设置 | `Settings` | `SlidersHorizontal` |

- [ ] **步骤 2：重写 `Sidebar.vue`**

```vue
<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import {
  Activity, Boxes, Cube, Database, Layers, LayoutDashboard, MessageSquareSparkle,
  Network, ScrollText, Server, Settings, ShieldCheck, Target, UserRound,
  type LucideIcon,
} from 'lucide-vue-next';

const route = useRoute();

type Group = '概览' | '运维' | '系统';

interface MenuItem {
  to: string;
  label: string;
  icon: LucideIcon;
  group: Group;
}

/** 侧边菜单：与 router 路由一一对应。项 / 顺序 / 路径 / 文案不得改动（e2e 依赖）。 */
const menus: MenuItem[] = [
  { to: '/dashboard', label: '仪表板', icon: LayoutDashboard, group: '概览' },
  { to: '/models', label: '模型', icon: Boxes, group: '概览' },
  { to: '/services', label: '服务', icon: Server, group: '概览' },
  { to: '/chat', label: 'AI 对话', icon: MessageSquareSparkle, group: '概览' },
  { to: '/envs', label: '环境', icon: Layers, group: '运维' },
  { to: '/probe', label: '体检', icon: Activity, group: '运维' },
  { to: '/cluster/goals', label: '集群目标', icon: Target, group: '运维' },
  { to: '/cluster/nodes', label: '集群', icon: Network, group: '运维' },
  { to: '/audit', label: '审计', icon: ScrollText, group: '系统' },
  { to: '/accounts', label: '账号管理', icon: ShieldCheck, group: '系统' },
  { to: '/account/self', label: '我的账号', icon: UserRound, group: '系统' },
  { to: '/config', label: '配置', icon: Database, group: '系统' },
  { to: '/settings', label: '设置', icon: Settings, group: '系统' },
];

const GROUPS: Group[] = ['概览', '运维', '系统'];

/** 按分组切分，组内保持 menus 原顺序 */
const sections = computed(() =>
  GROUPS.map((g) => ({ group: g, items: menus.filter((m) => m.group === g) })).filter((s) => s.items.length),
);

// 通过前缀匹配判定当前激活项（精确优先）
function isActive(item: MenuItem) {
  return route.path === item.to || route.path.startsWith(`${item.to}/`);
}
</script>

<template>
  <aside class="glass flex h-full w-56 flex-col border-r border-sep">
    <!-- Logo -->
    <div class="flex items-center gap-2.5 px-4 pb-4 pt-4">
      <span
        class="grid size-[22px] shrink-0 place-items-center rounded-[7px] text-white"
        style="background: linear-gradient(160deg, var(--accent), #0a4fb0); box-shadow: 0 2px 7px rgba(0,113,227,.38)"
      >
        <Cube :size="12" :stroke-width="2.6" />
      </span>
      <span class="text-[14.5px] font-semibold tracking-[-.015em] text-label">modelctl</span>
    </div>

    <!-- 菜单（分组渲染，项序与旧版一致） -->
    <nav class="flex-1 overflow-y-auto px-2 pb-3">
      <template v-for="sec in sections" :key="sec.group">
        <div class="px-2.5 pb-1.5 pt-3 text-[10.5px] font-semibold uppercase tracking-[.075em] text-label3">
          {{ sec.group }}
        </div>
        <router-link
          v-for="m in sec.items"
          :key="m.to"
          :to="m.to"
          :class="[
            'nav-item relative flex items-center gap-2.5 rounded-ctl px-2.5 py-[7px] text-[13.5px] transition-all duration-150',
            isActive(m) ? 'nav-item-active font-medium text-label' : 'text-label2 hover:bg-surface3 hover:text-label',
          ]"
        >
          <component :is="m.icon" :size="16" :stroke-width="1.75" class="shrink-0 opacity-90" />
          <span class="truncate">{{ m.label }}</span>
        </router-link>
      </template>
    </nav>

    <!-- 底部 small 标签 -->
    <div class="border-t border-sep-soft px-4 py-3 text-[11px] text-label3">modelctl chainweb</div>
  </aside>
</template>

<style scoped>
/* 选中项左侧 3px accent 竖条：画在容器外沿，等价旧版 border-l-2 的视觉位置，
   但不占 padding，避免文字右移 2px */
.nav-item-active {
  background: var(--surface-3);
  box-shadow: var(--inset-hl);
}
.nav-item-active::before {
  content: '';
  position: absolute;
  left: -8px;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 15px;
  border-radius: 0 3px 3px 0;
  background: var(--accent);
}
</style>
```

- [ ] **步骤 3：静态校验四条 e2e 契约未破**

```powershell
cd d:\Workplace\modelctl-1\web
node -e "const s=require('fs').readFileSync('src/components/layout/Sidebar.vue','utf8');
console.log('aside:',/<aside/.test(s));
console.log('nav:',/<nav/.test(s));
console.log('one-router-link:',(s.match(/<router-link/g)||[]).length===1);
console.log('probe-label:',/label: '体检'/.test(s));
console.log('w-56:',/w-56/.test(s));
console.log('no-legacy-svg:',!/<svg class=\"size-5\"/.test(s));"
```

Expected: 全 `true`。

- [ ] **步骤 4：全量测试 + 提交**

Run: `cd d:\Workplace\modelctl-1\web && npm run typecheck && npm run test`

```bash
cd d:\Workplace\modelctl-1
git add web/src/components/layout/Sidebar.vue
git commit -m "feat(web): 侧栏换 lucide 图标 + 语义分组与毛玻璃材质"
```

---

## 任务 5：布局壳（Layout 移动导航 + Header）

**Files:**
- Modify: `web/src/components/layout/Layout.vue`（全文重写）
- Modify: `web/src/components/layout/Header.vue`（全文重写）

**Interfaces:**
- Consumes: 任务 1 的 `.glass` / `.iconbtn` / 语义类；任务 3 的 `ThemeSwitch`；任务 4 的 `Sidebar`
- Produces: `Header.vue` 新增可选 prop `onToggleNav?: () => void`；`Layout.vue` 内部 `mobileNavOpen` 本地状态

- [ ] **步骤 1：重写 `Layout.vue`**

```vue
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import Sidebar from './Sidebar.vue';
import Header from './Header.vue';
import { useTasksStore } from '@/stores/tasks';

// 布局容器：left sidebar + top header + main router-view
const route = useRoute();
const pageTitle = computed(() => (route.meta?.title as string) ?? 'modelctl');

// 移动端抽屉导航（<768px）；桌面常驻侧栏的 hidden md:flex 契约不动
const mobileNavOpen = ref(false);
// 换页即收起抽屉，否则点完菜单抽屉一直挡着内容
watch(() => route.fullPath, () => (mobileNavOpen.value = false));

// 全局任务层：挂载即回填历史 + 重挂活动任务 SSE（刷新/切页恢复）
const tasksStore = useTasksStore();
onMounted(() => {
  void tasksStore.bootstrap().catch(() => {
    /* 列表拉取失败不阻塞页面渲染；降级依赖后续 SSE 事件 */
  });
});
// Layout 卸载（登出跳 /login）即释放全部 SSE/轮询；重新登录后 bootstrap 重建
onBeforeUnmount(() => tasksStore.reset());
</script>

<template>
  <div class="relative flex h-full min-h-screen">
    <!-- 桌面常驻侧栏（e2e 契约：aside/nav + hidden md:flex 断点） -->
    <Sidebar class="hidden md:flex md:flex-col" />

    <!-- 移动端抽屉：仅 <768px 出现，fixed 层不参与桌面布局 -->
    <div v-if="mobileNavOpen" class="fixed inset-0 z-40 md:hidden">
      <div class="absolute inset-0 bg-black/40" @click="mobileNavOpen = false" />
      <Sidebar class="glass absolute inset-y-0 left-0 z-50 flex w-56 flex-col border-r border-sep" />
    </div>

    <!-- 主区域 -->
    <div class="flex min-h-screen flex-1 flex-col overflow-hidden">
      <Header :title="pageTitle" :on-toggle-nav="() => (mobileNavOpen = !mobileNavOpen)" />
      <!-- 路由出口（relative 让内容盖在 body::before 光斑之上） -->
      <main class="relative flex-1 overflow-auto p-4 md:p-6">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>
    </div>
  </div>
</template>
```

变化点：删根容器的 `bg-[#0f172a] text-slate-100`（由 `body` 令牌承担）；`main` 加 `relative`；`p-4 md:p-6` 与 `transition name="fade" mode="out-in"` 不动。

- [ ] **步骤 2：重写 `Header.vue`**

脚本逻辑（health、maskedKey、onLogout、TaskDrawer 引用、`tasksStore`）**全部原样保留**，只追加 prop、theme store、头像计算，模板换语义类 + 汉堡接真。

```vue
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ListChecks, Menu } from 'lucide-vue-next';
import { useAuthStore } from '@/stores/auth';
import { health } from '@/api/services';
import TaskDrawer from '@/components/common/TaskDrawer.vue';
import ThemeSwitch from '@/components/common/ThemeSwitch.vue';
import { useTasksStore } from '@/stores/tasks';
import { useThemeStore } from '@/stores/theme';

const props = defineProps<{
  /** 页面标题 */
  title?: string;
  /** 移动端抽屉导航开关（桌面端不渲染按钮） */
  onToggleNav?: () => void;
}>();

const auth = useAuthStore();
const router = useRouter();
const theme = useThemeStore();

// 全局任务抽屉：入口按钮 + 运行中数量角标
const tasksStore = useTasksStore();
const drawerRef = ref<InstanceType<typeof TaskDrawer> | null>(null);
const runningCount = computed(() => tasksStore.runningCount);

// 后端健康状态
type HealthState = 'loading' | 'ok' | 'bad';
const healthState = ref<HealthState>('loading');

async function refresh() {
  try {
    const res = await health();
    healthState.value = res.ok ? 'ok' : 'bad';
  } catch {
    healthState.value = 'bad';
  }
}

onMounted(() => {
  refresh();
});

/** 脱敏的 apiKey 前缀：显示前 6 位 + … */
const maskedKey = () => {
  const k = auth.token || '';
  if (!k) return '未登录';
  return k.length <= 6 ? '***' : `${k.slice(0, 6)}…`;
};

/** 身份首字：账号面优先显示账号名首字，否则管理面统一「运维」 */
const who = computed(() => {
  const p = auth.accountProfile;
  return p ? (p.display_name || p.username).slice(0, 1).toUpperCase() : '运维';
});

function onLogout() {
  // 先关全部 SSE/轮询并清空任务记录，再清凭据（避免残留请求带旧 token 打后端）
  tasksStore.reset();
  auth.clear();
  router.push({ path: '/login' });
}
</script>

<template>
  <header class="glass z-10 flex items-center justify-between gap-3 border-b border-sep px-4 py-3 md:px-6">
    <!-- 左侧：汉堡（仅移动端）+ 标题 -->
    <div class="flex min-w-0 items-center gap-2.5">
      <button class="iconbtn md:hidden" type="button" aria-label="打开导航" @click="props.onToggleNav?.()">
        <Menu :size="15" :stroke-width="1.9" />
      </button>
      <h1 class="truncate text-lg font-semibold tracking-[-.022em] text-label md:text-xl">
        {{ props.title || 'modelctl' }}
      </h1>
    </div>

    <!-- 右侧：主题 + 状态 + 身份 + 任务 + 退出 -->
    <div class="flex items-center gap-2.5">
      <ThemeSwitch class="hidden lg:inline-flex" />

      <!-- 后端状态 -->
      <span
        :class="[
          'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium',
          healthState === 'loading' && 'border-sep bg-surface3 text-label2',
          healthState === 'ok' && 'border-ok-line bg-ok-bg text-ok',
          healthState === 'bad' && 'border-danger-line bg-danger-bg text-danger',
        ]"
      >
        <span
          :class="[
            'stonedot',
            healthState === 'loading' && 'bg-muted',
            healthState === 'ok' && 'animate-pulse bg-ok',
            healthState === 'bad' && 'bg-danger',
          ]"
        />
        {{ healthState === 'loading' ? '检测中' : healthState === 'ok' ? '后端正常' : '后端异常' }}
      </span>

      <!-- 脱敏 apiKey 前缀 -->
      <span class="hidden text-xs text-label3 md:inline-flex" style="font-family: var(--mono)">
        {{ maskedKey() }}
      </span>

      <!-- 任务抽屉入口 -->
      <button class="iconbtn" type="button" title="后台任务" @click="drawerRef?.toggle()">
        <ListChecks :size="15" :stroke-width="1.9" />
        <span
          v-if="runningCount > 0"
          class="num absolute -right-1 -top-1 grid h-[15px] min-w-[15px] place-items-center rounded-full px-1 text-[9.5px] font-bold"
          style="background: var(--accent); color: var(--on-accent); box-shadow: 0 0 0 2.5px var(--canvas)"
        >
          {{ runningCount }}
        </span>
      </button>

      <!-- 身份头像 -->
      <div
        class="grid size-[29px] shrink-0 place-items-center rounded-full text-[11.5px] font-semibold text-white"
        style="background: linear-gradient(150deg, #8e9bb5, #5b6980)"
      >
        {{ who }}
      </div>

      <!-- 退出登录 -->
      <button class="btn-ghost !py-1.5 text-xs" @click="onLogout">退出登录</button>
    </div>
  </header>

  <!-- 全局任务抽屉（含 toast 宿主）：与 header 同级，fragment 根 -->
  <TaskDrawer ref="drawerRef" />
</template>
```

> 原文件第 53-54 行的注释掉的 `setInterval` todo 一并删除（它引用的 `refresh` 仍在用，删掉无副作用）。`header` 加 `z-10` 使其盖在 `body::before` 光斑之上。

- [ ] **步骤 3：静态校验响应式契约**

```powershell
cd d:\Workplace\modelctl-1\web
node -e "const l=require('fs').readFileSync('src/components/layout/Layout.vue','utf8');
const h=require('fs').readFileSync('src/components/layout/Header.vue','utf8');
console.log('md:flex-breakpoint:',/hidden md:flex md:flex-col/.test(l));
console.log('drawer-md-hidden:',/fixed inset-0 z-40 md:hidden/.test(l));
console.log('main-padding:',/p-4 md:p-6/.test(l));
console.log('no-hard-bg:',!/bg-\[#0f172a\]|text-slate-100/.test(l));
console.log('h1-kept:',/<h1/.test(h));
console.log('burger-md-hidden:',/iconbtn md:hidden/.test(h));"
```

Expected: 全 `true`。

- [ ] **步骤 4：全量测试 + 提交**

Run: `cd d:\Workplace\modelctl-1\web && npm run typecheck && npm run test`

```bash
cd d:\Workplace\modelctl-1
git add web/src/components/layout/Layout.vue web/src/components/layout/Header.vue
git commit -m "feat(web): 布局壳换材质 + 移动端抽屉导航可用 + 顶栏外观切换"
```

---

## 任务 6：通用组件语义化

**Files:**
- Create: `web/src/components/common/DataTable.vue`
- Modify: `web/src/components/common/StatusBadge.vue`
- Modify: `web/src/components/common/Loading.vue`
- Modify: `web/src/components/common/ConfirmDialog.vue`
- Modify: `web/src/components/common/TaskDrawer.vue`
- Modify: `web/src/components/common/TaskButton.vue`
- Modify: `web/src/components/common/SseLogViewer.vue`
- Modify: `web/src/utils/toast.ts`

**Interfaces:**
- Consumes: 任务 1 的语义类、`.glass`、`.stonedot`
- Produces: `<DataTable>` 包裹式组件（仅默认插槽，无 props）；`Loading` 新增可选 prop `label?: string`、`inline?: boolean`

- [ ] **步骤 1：创建 `DataTable.vue`（包裹式，各页保留自己的 `<table>`）**

```vue
<script setup lang="ts">
/**
 * 精修表格容器：外层卡片 + 统一表头/行/悬停样式。
 * 包裹式而非配置式 —— 各视图保留自己的 <table> 标记与列定义，零逻辑改动。
 */
</script>

<template>
  <div class="overflow-hidden rounded-card border border-sep bg-surface2" style="box-shadow: var(--shadow-s)">
    <div class="overflow-x-auto">
      <slot />
    </div>
  </div>
</template>

<style scoped>
/* :slotted() 只匹配插槽的**顶层**节点（这里是 <table>），其后代必须写在括号外面。
   写成 :slotted(tbody tr) 不会命中 —— tbody 不是被直接插入插槽的节点。 */
:slotted(table) {
  width: 100%;
  border-collapse: collapse;
}
:slotted(table) th {
  text-align: left;
  font-size: 11px;
  font-weight: 620;
  letter-spacing: 0.055em;
  text-transform: uppercase;
  color: var(--label-3);
  padding: 10px 14px;
  background: var(--surface-3);
  border-bottom: 0.5px solid var(--separator);
  white-space: nowrap;
}
:slotted(table) td {
  padding: 11px 14px;
  font-size: 13px;
  color: var(--label);
  border-bottom: 0.5px solid var(--separator-soft);
}
:slotted(table) tbody tr {
  transition: background 0.14s var(--ease);
}
:slotted(table) tbody tr:hover {
  background: var(--surface-3);
}
:slotted(table) tbody tr:last-child td {
  border-bottom: 0;
}
</style>
```

> `<th>` / `<td>` 上各页残留的 padding / 对齐工具类会与 `:slotted()` 竞争。`:slotted()` 编译后带属性选择器，权重高于 UnoCSS 单类工具类，因此本块默认胜出。数字列需要右对齐时直接用 `text-right`（本块未设 `text-align`，不会冲突）。

- [ ] **步骤 2：`StatusBadge.vue` —— 只换 `STYLE_MAP` 的值与模板类名**

`STYLE_MAP`（第 38-87 行）整体替换为语义版。**8 个键、`text`、`isErr` 全部保持原值**：

```ts
const STYLE_MAP: Record<string, Style> = {
  running: { cls: 'bg-ok-bg text-ok border border-ok-line', text: '运行中', dot: 'bg-ok', isErr: false },
  stopped: { cls: 'bg-surface3 text-label2 border border-sep', text: '已停止', dot: 'bg-muted', isErr: false },
  skipped: { cls: 'bg-surface3 text-label2 border border-sep', text: '已跳过', dot: 'bg-muted', isErr: false },
  starting: { cls: 'bg-accent-bg text-accent border border-accent-line', text: '启动中', dot: 'bg-accent', isErr: false },
  stopping: { cls: 'bg-warn-bg text-warn border border-warn-line', text: '停止中', dot: 'bg-warn', isErr: false },
  queued: { cls: 'bg-surface3 text-label2 border border-sep', text: '排队中', dot: 'bg-muted', isErr: false },
  unknown: { cls: 'bg-surface3 text-label3 border border-sep', text: '未知', dot: 'bg-muted', isErr: false },
  error: { cls: 'bg-danger-bg text-danger border border-danger-line', text: '异常', dot: 'bg-danger', isErr: true },
};
```

模板（第 103-114 行）替换为：

```vue
<template>
  <span :class="['inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium', style.cls]">
    <span
      :class="['stonedot', style.dot, style.isErr ? '' : 'animate-pulse']"
      :aria-hidden="true"
    />
    <span>
      {{ style.text }}
      <span v-if="healthySuffix" class="text-ok">{{ healthySuffix }}</span>
    </span>
  </span>
</template>
```

> 原模板异常时用 `text-red-300` 再上一次色；新方案容器 `style.cls` 已是 `text-danger`，子 span 无需重复上色。「· 健康」副标恒定 `text-ok`。

- [ ] **步骤 3：`Loading.vue` 收敛为唯一 spinner 实现**

先读现状（16 行），改为支持内联与自定义文案，供其它 4 处复制的 spinner 复用：

```vue
<script setup lang="ts">
/** 全站唯一 spinner。其它组件不得再内联复制 spinner SVG。 */
withDefaults(defineProps<{ label?: string; inline?: boolean }>(), { label: '加载中…', inline: false });
</script>

<template>
  <div :class="inline ? 'inline-flex items-center gap-2' : 'flex items-center justify-center gap-2 py-8'">
    <svg class="size-5 shrink-0 animate-spin text-accent" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" opacity="0.22" />
      <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" stroke-width="3" stroke-linecap="round" />
    </svg>
    <span v-if="label" class="text-sm text-label2">{{ label }}</span>
  </div>
</template>
```

- [ ] **步骤 4：`ConfirmDialog.vue` / `TaskDrawer.vue` / `TaskButton.vue` / `SseLogViewer.vue` / `toast.ts` 换语义类**

按下表逐条替换（只改类名字符串，**不动任何逻辑、props、状态机、DOM 结构**）：

| 文件 | 旧类名 | 新类名 |
|---|---|---|
| `ConfirmDialog.vue` | `bg-black/60 backdrop-blur-sm` | `bg-black/40 backdrop-blur-sm` |
| `ConfirmDialog.vue` | `max-w-md rounded-lg border border-slate-700 bg-slate-900 shadow-xl` | `max-w-md rounded-panel border border-sep bg-surface2 shadow-l` |
| `ConfirmDialog.vue` | `text-red-300`（danger 标题） | `text-danger` |
| `ConfirmDialog.vue` | 内联 spinner SVG | `<Loading inline label="" />` + `import Loading from './Loading.vue'` |
| `TaskDrawer.vue` | `bg-black/50`（遮罩） | `bg-black/40` |
| `TaskDrawer.vue` | `border-l border-slate-700 bg-slate-900 shadow-2xl` | `glass border-l border-sep` |
| `TaskDrawer.vue` | `divide-slate-800` | `divide-sep-soft` |
| `TaskDrawer.vue` | `bg-[#0b1120]` | `bg-code-bg text-code-fg` |
| `TaskDrawer.vue` | `statusDot()` 里的 `bg-amber-400 / bg-blue-400 / bg-emerald-400 / bg-red-400 / bg-slate-400` | `bg-warn / bg-info / bg-ok / bg-danger / bg-muted` |
| `TaskButton.vue` | `min-w-24` | 保持不动 |
| `TaskButton.vue` | 内联 spinner SVG | `<Loading inline label="" />`（`btn-base` 已含 flex 对齐） |
| `SseLogViewer.vue` | `bg-[#0b1120] … text-slate-300` | `bg-code-bg text-code-fg`（其余 `px-4 py-3 font-mono text-xs leading-6 whitespace-pre-wrap break-all` 保留） |
| `SseLogViewer.vue` | `bg-amber-400 / bg-emerald-400 / bg-red-400` | `bg-warn / bg-ok / bg-danger` |
| `toast.ts` | `KIND_CLASS.success` = `border-emerald-500/40 bg-emerald-600/15 text-emerald-200` | `border-ok-line bg-ok-bg text-ok` |
| `toast.ts` | `KIND_CLASS.error` = `border-red-500/40 bg-red-600/15 text-red-200` | `border-danger-line bg-danger-bg text-danger` |
| `toast.ts` | `KIND_CLASS.warning` = `border-amber-500/40 bg-amber-600/15 text-amber-200` | `border-warn-line bg-warn-bg text-warn` |

> `toast.ts` 的 **`KIND_CLASS` 三个键名（`success` / `error` / `warning`）必须保持**，`utils/toast.test.ts` 依赖。

- [ ] **步骤 5：确认 spinner 已收敛**

用 Grep 工具：pattern `animate-spin`，path `d:\Workplace\modelctl-1\web\src`，`output_mode: files_with_matches`。
Expected（本任务后）：`Loading.vue` + 任务 10 才处理的 `LoginView.vue` / `DockerInstallPanel.vue`（这两处此处允许暂时残留）。
若 `ConfirmDialog.vue` 或 `TaskButton.vue` 仍命中，说明步骤 4 的 spinner 替换漏了，补上再继续。

- [ ] **步骤 6：全量测试 + 提交**

Run: `cd d:\Workplace\modelctl-1\web && npm run typecheck && npm run test`
Expected: 全绿 —— 特别确认 `toast.test.ts` 与 `ChatHistoryList.test.ts` 未红。

```bash
cd d:\Workplace\modelctl-1
git add web/src/components/common web/src/utils/toast.ts
git commit -m "feat(web): 通用组件语义化 + DataTable 精修表格容器 + spinner 收敛"
```

---

## 任务 7：聊天区语义化

**Files:**
- Modify: `web/src/components/chat/ChatHistoryList.vue`
- Modify: `web/src/components/chat/ChatMessage.vue`
- Modify: `web/src/components/chat/ChatComposer.vue`
- Modify: `web/src/components/chat/ChatParamsPanel.vue`
- Modify: `web/src/components/chat/ChatRawPanel.vue`
- Modify: `web/src/components/chat/ChatStatsPanel.vue`
- Modify: `web/src/views/chat/index.vue`

**Interfaces:**
- Consumes: 任务 1 语义类、`.code-surface`
- Produces: 无对外接口变化（props / emits 全部不变）

- [ ] **步骤 1：`ChatHistoryList.vue` —— 最高危，只改「非契约」类名**

**绝对不改**：条目的 `cursor-pointer` 类、active 态的 `bg-slate-800` 类、条目用 `div` 而非 `button`、「新建」是模板第一个 `<button>`、文案「暂无历史」「本地存储已满」。

允许改：容器 `border-r border-slate-800 bg-slate-900/60` → `border-r border-sep bg-surface2`；非 active 文字 `text-slate-400` → `text-label2`；hover `hover:bg-slate-800 hover:text-blue-300` → `hover:bg-surface3 hover:text-label`；`text-slate-500` / `text-slate-600` → `text-label3`；`text-blue-400` → `text-accent`；`bg-amber-500/10 text-amber-400` → `bg-warn-bg text-warn`。

> 注意容器 `border-slate-800` 可改，但**条目上的 `bg-slate-800` 不可改**。二者同名不同位置，务必区分。

- [ ] **步骤 2：立刻单独验证该组件**

Run: `cd d:\Workplace\modelctl-1\web && npx vitest run src/components/chat/ChatHistoryList.test.ts`
Expected: PASS（4 用例）。这是本次重构最容易踩雷的一处，必须单独先验证。

- [ ] **步骤 3：`ChatMessage.vue` —— 气泡与 markdown**

气泡：user `self-end max-w-[75%] bg-blue-600 text-slate-50` → `self-end max-w-[75%] rounded-panel rounded-br-sm bg-accent text-accent-fg px-3.5 py-2.5`；assistant `self-start max-w-[90%] bg-slate-800` → `self-start max-w-[90%] rounded-panel rounded-bl-sm bg-surface3 text-label px-3.5 py-2.5`。
错误块 `border-l-2 border-red-500 bg-red-500/10` → `border-l-2 border-danger bg-danger-bg`；`text-red-300` → `text-danger`；`text-amber-400` → `text-warn`；`text-slate-400/500` → `text-label2` / `text-label3`。

`<style scoped>` 内 9 处 `:deep()` 换令牌（**选择器结构一个不动，只换色值**）：

```css
.chat-md :deep(pre.hljs) {
  background: var(--code-bg);
  color: var(--code-fg);
  border: 0.5px solid var(--code-line);
  border-radius: var(--r-m);
  padding: 10px 12px;
  overflow-x: auto;
  font-size: 12px;
  line-height: 1.65;
}
.chat-md :deep(p:last-child) { margin-bottom: 0; }
.chat-md :deep(code:not(.hljs code)) {
  background: var(--surface-4);
  color: var(--label);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 12px;
}
.chat-md :deep(th),
.chat-md :deep(td) { border: 0.5px solid var(--separator); padding: 5px 9px; }
.chat-md :deep(th) { background: var(--surface-3); }
```

> 上面只列出**需要改色值**的 5 处；其余 4 处（`p`、`ul/ol`、`table` 等）若原本不含色值则原样保留。

- [ ] **步骤 4：其余 4 个 chat 子组件 + `views/chat/index.vue`**

`ChatComposer.vue`：textarea `border-slate-700 bg-slate-950` → `border-sep bg-surface2 text-label` + `focus:border-accent`；发送钮 `rounded-full bg-blue-600` → `rounded-full bg-accent text-accent-fg hover:bg-accent-hover`；停止 `bg-red-900 text-red-200` → `bg-danger-bg text-danger border border-danger-line`；`border-slate-800` → `border-sep`；`text-slate-200` / `text-slate-400` → `text-label` / `text-label2`。**IME 守卫不动。**

`ChatParamsPanel.vue`：`border-slate-700` / `border-slate-800` → `border-sep`；`text-slate-400` / `text-slate-500` → `text-label2` / `text-label3`；`bg-slate-950` → `bg-code-bg text-code-fg`；`accent-blue-500` → `accent-accent`。原生 `range` 滑块无需自定义样式，`accent-accent` 已让它变语义蓝。

`ChatRawPanel.vue`：`bg-slate-950` → `bg-code-bg`；`text-sky-300` → `text-info`；`hover:text-slate-300` → `hover:text-label`；其余按对照表。
`ChatStatsPanel.vue`：`border-slate-800` → `border-sep-soft`；`text-slate-200` → `text-label`、`text-slate-400` → `text-label2`、`text-slate-500` / `text-slate-600` → `text-label3`；数值 span 追加 `num`。

`views/chat/index.vue`：`bg-slate-900` / `bg-slate-900/60` → `bg-surface2` / `bg-surface3`；tab 选中 `border-blue-500 text-blue-300 bg-blue-600/15` → `border-accent text-accent bg-accent-bg`；未选中 `text-slate-400 hover:text-slate-200` → `text-label2 hover:text-label`。**`defineOptions({ name: 'chat' })` 不动。**

- [ ] **步骤 5：全量测试 + 提交**

Run: `cd d:\Workplace\modelctl-1\web && npm run typecheck && npm run test`
Expected: 全绿 —— `ChatComposer.test.ts`（IME + 回车发送）、`ChatMessage.test.ts`、`ChatStatsPanel.test.ts`、`ChatHistoryList.test.ts` 全过。

```bash
cd d:\Workplace\modelctl-1
git add web/src/components/chat web/src/views/chat
git commit -m "feat(web): 聊天区语义化（气泡、markdown 排版、历史列表材质）"
```

---

## 任务 8：卡片范式视图（5 个）

**Files:**
- Modify: `web/src/views/DashboardView.vue`
- Modify: `web/src/views/ClusterNodesView.vue`
- Modify: `web/src/views/ClusterGoalsView.vue`
- Modify: `web/src/views/ClusterNodeDetailView.vue`
- Modify: `web/src/views/EnvsView.vue`

**Interfaces:**
- Consumes: 任务 1 语义类、`.num`、`.stonedot`
- Produces: 无对外接口变化

- [ ] **步骤 1：本组通用替换对照表**

| 旧 | 新 |
|---|---|
| `card` shortcut | 保持 `card`（任务 1 已语义化） |
| `bg-slate-900` / `bg-slate-900/70` | `bg-surface2` |
| `bg-slate-950` | `bg-code-bg text-code-fg`（代码/日志面）或 `bg-canvas` |
| `bg-slate-800/30\|40\|50\|60` | `bg-surface3` |
| `bg-slate-700` / `bg-slate-600/15\|40` / `bg-slate-500/15` | `bg-surface4` |
| `border-slate-600/700/800`（含任意 `/xx`） | `border-sep` |
| `text-slate-100` / `text-slate-200` | `text-label` |
| `text-slate-300` / `text-slate-400` | `text-label2` |
| `text-slate-500` / `text-slate-600` | `text-label3` |
| `text-blue-400` / `border-blue-500(/xx)` / `bg-blue-500/xx` | `text-accent` / `border-accent-line` / `bg-accent-bg` |
| `text-emerald-300` / `text-emerald-400` / `border-emerald-500/xx` / `bg-emerald-500/xx` | `text-ok` / `text-ok` / `border-ok-line` / `bg-ok-bg` |
| `text-amber-200/300/400` / `border-amber-500\|700\|800(/xx)` / `bg-amber-500/xx` / `bg-amber-900/30` / `bg-amber-950/40` | `text-warn` / `border-warn-line` / `bg-warn-bg` |
| `text-red-300` / `text-rose-300` / `text-rose-400` / `border-red-500/*` / `border-rose-500\|800` / `bg-red-500/xx` / `bg-rose-500/xx` / `bg-rose-900/30` | `text-danger` / `border-danger-line` / `bg-danger-bg` |
| `text-red-400` | **保持原样**（别名指向 `--danger`） |
| `ring-1 ring-amber-400/60` | `ring-2 ring-warn` |
| `accent-blue-500` | `accent-accent` |
| `rounded-md` / `rounded-lg` | `rounded-ctl` |
| `rounded-xl` | `rounded-card` |
| 内联 `:style` 的柱高 / 进度宽 | **保留**（动态值），色名换 `var(--ok)` / `var(--warn)` / `var(--danger)` |
| 数字 / 时间 / 大小 | 追加 `num` 类 |

- [ ] **步骤 2：`DashboardView.vue`**

1. 按对照表全量替换。
2. 统计卡数值元素加 `num` + `text-[26px] font-semibold tracking-[-.028em] leading-tight`。
3. 引擎三态卡：`border-emerald-500/30 bg-emerald-600/5` → `border-ok-line bg-ok-bg`；`border-red-500/30 bg-red-600/5` → `border-danger-line bg-danger-bg`；`border-amber-500/30\|40 bg-amber-600/5\|10` → `border-warn-line bg-warn-bg`；中性态 `border-sep bg-surface2`。
4. `docker` 角标补 `border border-sep rounded-full px-1.5 text-label3`。
5. 统计卡加 `hover:-translate-y-0.5 transition-transform duration-200`，容器加 `style="box-shadow: var(--shadow-m)"`。
6. **3s 轮询 + `pending` 防重叠 + abort 静默三条件，一行不动。**

- [ ] **步骤 3：`ClusterNodesView.vue` —— 表格改卡片网格**

先读文件确认 `STATUS_STYLE` 现有键名与文案，**只换值，不增删键、不改文案**，值统一成 chip 范式：

```ts
/** 节点状态 → chip 类（卡片范式：状态在卡片头以 chip 呈现） */
const STATUS_STYLE: Record<string, string> = {
  // 就绪 / 在线
  ready: 'border-ok-line bg-ok-bg text-ok',
  // 降级
  degraded: 'border-warn-line bg-warn-bg text-warn',
  // 不可达 / 离线
  unreachable: 'border-danger-line bg-danger-bg text-danger',
  // 兜底
  unknown: 'border-sep bg-surface3 text-label2',
};
```

模板：`<table>` 换成卡片网格，保持跳详情的 `<router-link>`（真实 `<a>`）语义：

```vue
<div class="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
  <router-link
    v-for="n in nodes"
    :key="n.node_id"
    :to="`/cluster/nodes/${encodeURIComponent(n.node_id)}`"
    class="block rounded-card border border-sep bg-surface2 p-3.5 transition-all duration-200 hover:-translate-y-px"
    style="box-shadow: var(--shadow-s)"
  >
    <div class="flex items-start gap-2">
      <div class="min-w-0 flex-1">
        <div class="truncate text-[13px] font-medium text-label" style="font-family: var(--mono)">{{ n.node_id }}</div>
        <div class="num mt-0.5 truncate text-[11.5px] text-label3" style="font-family: var(--mono)">{{ n.lan_id || '-' }}</div>
      </div>
      <span
        :class="[
          'inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium',
          STATUS_STYLE[n.status] ?? STATUS_STYLE.unknown,
        ]"
      >
        <span class="stonedot bg-current" />{{ statusText(n.status) }}
      </span>
    </div>
    <!-- 原表格的其余列逐条搬进下方 meta 行，每列一项、一列不丢：
         <div class="mt-2.5 flex flex-wrap gap-x-3.5 gap-y-1">
           <span class="text-[11.5px] text-label3">标签
             <b class="num font-medium text-label2">值</b>
           </span>
         </div> -->
  </router-link>
</div>
```

> `:to` 的路径拼接、`statusText()`、字段名均以现有文件为准 —— 上面是**结构模板**，字段要按本页原有列一一搬运，一列不丢；时间维持 `YYYY-MM-DD HH:mm:ss`；空列表空态沿用现有文案。

- [ ] **步骤 4：`ClusterGoalsView.vue`**

- `STAGE_STYLE` 常量表按对照表换值（键与文案不动）。
- 列表项换成步骤 3 的卡片容器类（保留原有点击/展开行为）。
- 详情抽屉：遮罩 `bg-black/60` → `bg-black/40`；面板 `bg-slate-900` → `glass border-l border-sep`（`max-w-lg` 与右滑定位不动）。

- [ ] **步骤 5：`ClusterNodeDetailView.vue`**

退役确认模态：遮罩 `bg-slate-950 backdrop-blur-sm` → `bg-black/40 backdrop-blur-sm`；面板补 `rounded-panel border border-sep bg-surface2 shadow-l`；事件流 `bg-slate-950 font-mono` → `bg-code-bg text-code-fg`；`border-slate-900` → `border-sep-soft`；其余按对照表。

- [ ] **步骤 6：`EnvsView.vue`（本页保留表格，不转卡片）**

- 日志 `pre` 的 `bg-[#0b1120]` → `bg-code-bg text-code-fg`。
- focus 高亮 `ring-1 ring-amber-400/60` → `ring-2 ring-warn`。
- **`id="env-row-{name}"` 与 `id="docker-bypass"` 锚点原样保留**；`DockerInstallPanel` 挂载点不动。
- 按对照表换语义类（可顺手用 `<DataTable>` 包住本页 `<table>`，但本页非必须，保持简单）。

- [ ] **步骤 7：全量测试 + 提交**

Run: `cd d:\Workplace\modelctl-1\web && npm run typecheck && npm run test`

```bash
cd d:\Workplace\modelctl-1
git add web/src/views/DashboardView.vue web/src/views/ClusterNodesView.vue web/src/views/ClusterGoalsView.vue web/src/views/ClusterNodeDetailView.vue web/src/views/EnvsView.vue
git commit -m "feat(web): 卡片范式视图语义化（仪表板、集群节点/目标/详情、环境）"
```

---

## 任务 9：表格范式视图（6 个，接入 DataTable）

**Files:**
- Modify: `web/src/views/ModelsListView.vue`
- Modify: `web/src/views/ServicesMatrixView.vue`
- Modify: `web/src/views/AuditLogView.vue`
- Modify: `web/src/views/ProbeView.vue`
- Modify: `web/src/views/accounts/AccountsView.vue`
- Modify: `web/src/views/accounts/AccountSelfView.vue`

**Interfaces:**
- Consumes: 任务 6 的 `<DataTable>`；任务 1 语义类
- Produces: 无对外接口变化

- [ ] **步骤 1：每页统一动作（对 6 个文件各执行一遍）**

1. 加 `import DataTable from '@/components/common/DataTable.vue';`
2. 用 `<DataTable>` 包住现有 `<table>`，**删掉表格外层原有的手写容器类**（如 `overflow-hidden rounded-xl border border-slate-700/60 bg-slate-900`）—— 这些已由 `DataTable` 提供。若外层还兼有横向滚动职责，改用 `DataTable` 内置的 `overflow-x-auto`。
3. 删掉 `<th>` / `<td>` 上重复的 `bg-slate-800/40`、`text-xs uppercase tracking-wider text-slate-400`、`border-b border-slate-800/40`、`hover:bg-slate-800`、`px-*/py-*`（`DataTable` 已统一）；**保留** `text-right`、`min-w-*`、`font-mono`、条件着色类。
4. 非表格元素（筛选器、按钮、统计行、徽标、空态）按任务 8 步骤 1 的对照表换语义类。
5. 数字 / 时间 / 大小列加 `num`。
6. 代码 / `pre` 块 `bg-[#0b1120]` → `bg-code-bg text-code-fg`。
7. 列宽用 `min-width` 而非 `width`（`CLAUDE.md` 约定）；长文本 `truncate` + `:title` 兜完整内容。

- [ ] **步骤 2：`AuditLogView.vue` 专属**

- 每日柱状图的内联 `:style` 高度计算**保留**，颜色改 `style="background: var(--accent)"`（总请求柱）与 `style="background: var(--danger)"`（错误柱）；轨道底色 `bg-slate-600/15` → `bg-surface4`。
- `levelStyle()` 返回值换语义串（键与文案不动）。
- JSON 展开 `bg-[#0b1120]` → `bg-code-bg text-code-fg`。
- `text-red-400` 保持（本页用于错误计数，别名已指向 `--danger`）。

- [ ] **步骤 3：`AccountsView.vue` / `AccountSelfView.vue` 专属（安全红线）**

- tab 选中 `border-b-2 border-blue-500 text-blue-300` → `border-b-2 border-accent text-accent`。
- **一次性 Key 弹窗（`AccountsView.vue:590` / `AccountSelfView.vue:530`）**：`v-if`、`z-[60]`、`fixed inset-0`、遮罩 `bg-black/80` 四者**全部不动**（安全场景需要高遮罩；`v-show` 会残留 DOM 即密钥泄漏）；只把面板 `bg-slate-900` → `bg-surface2 rounded-panel border border-sep shadow-l`。
- `accounts_disabled` 503 引导条的 code 分派逻辑不动，只把 warning 色换成 `border-warn-line bg-warn-bg text-warn`。
- 两个文件里所有 `Teleport` 模态逐个套用面板语义类。

- [ ] **步骤 4：全量测试 + 提交**

Run: `cd d:\Workplace\modelctl-1\web && npm run typecheck && npm run test`

```bash
cd d:\Workplace\modelctl-1
git add web/src/views/ModelsListView.vue web/src/views/ServicesMatrixView.vue web/src/views/AuditLogView.vue web/src/views/ProbeView.vue web/src/views/accounts
git commit -m "feat(web): 表格范式视图接入 DataTable 并语义化"
```

---

## 任务 10：专项视图（登录 / 模型详情 / 配置 / Docker / 启动进度）

**Files:**
- Modify: `web/src/views/LoginView.vue`
- Modify: `web/src/views/accounts/AccountSelfLoginView.vue`
- Modify: `web/src/views/ModelDetailView.vue`
- Modify: `web/src/views/ConfigView.vue`
- Modify: `web/src/components/docker/DockerInstallPanel.vue`
- Modify: `web/src/components/startup/StartupProgressCard.vue`

**Interfaces:**
- Consumes: 任务 1 语义类、`.code-surface`；任务 6 的 `<Loading inline>`
- Produces: 无对外接口变化

- [ ] **步骤 1：`LoginView.vue`（e2e 最高危，先做且单独验证）**

保留：`id="api-key"`、按钮文案「登录」（e2e 用 `exact: true`）、错误段 `<p class="... text-red-400">`、空 Key 时按钮 `disabled`。

改动：
1. 根容器 `bg-[#0f172a]` → 删除（`body` 已提供 `--canvas` + 光斑）；改为 `relative flex min-h-screen items-center justify-center p-4`。
2. 背景双光斑装饰（`size-96 rounded-full bg-blue-600/10 blur-3xl` 与 `bg-emerald-600/10`）→ **删除**，由 `body::before` 的 `--canvas-tint` 统一提供（避免双重光斑，并省掉两层 blur）。
3. 卡片 `card !p-6` 保留，追加 `w-full max-w-sm` 与 `style="box-shadow: var(--shadow-l)"`。
4. 错误段确认形态为 `<p class="text-xs text-red-400">{{ error }}</p>`。
5. 登录中 spinner SVG → `<Loading inline label="" />` + `import Loading from '@/components/common/Loading.vue'`。
6. `text-slate-100` → `text-label`；`text-slate-400` → `text-label2`；`text-slate-500` / `text-slate-600` → `text-label3`；`text-blue-500` → `text-accent`。

- [ ] **步骤 2：静态校验登录页契约**

```powershell
cd d:\Workplace\modelctl-1\web
node -e "const s=require('fs').readFileSync('src/views/LoginView.vue','utf8');
console.log('api-key:',/id=\"api-key\"/.test(s));
console.log('p-text-red-400:',/<p[\s\S]{0,80}?text-red-400/.test(s));
console.log('login-btn:',/登录/.test(s));
console.log('no-hard-bg:',!/bg-\[#0f172a\]/.test(s));"
```

Expected: 全 `true`。若后端可用（默认 4173），额外跑 `npx playwright test e2e/smoke.spec.ts --project=chromium-desktop`；不可用则明确记录「e2e 未跑」。

- [ ] **步骤 3：`AccountSelfLoginView.vue`**

与步骤 1 同构：删 `bg-[#0f172a]`、删双光斑、卡片加 `max-w-sm` + `shadow-l`、`text-emerald-500` → `text-ok`、`text-slate-*` 按对照表；`text-red-400` 保持。

- [ ] **步骤 4：`ModelDetailView.vue`**

- tab 选中 `border-b-2 border-blue-500 text-blue-300` → `border-b-2 border-accent text-accent`；未选中 `text-slate-400 hover:text-slate-200` → `text-label2 hover:text-label`。
- YAML 编辑器（恒深）：`min-h-[460px] max-h-[640px] bg-[#0b1120] font-mono text-xs` → `code-surface min-h-[460px] max-h-[640px] rounded-card border border-code-line p-3 text-xs`。
- `accent-emerald-500` → `accent-ok`；`border-amber-600/40` → `border-warn-line`；`text-amber-200` → `text-warn`；`bg-amber-600/10` → `bg-warn-bg`；`bg-blue-600/15` → `bg-accent-bg`；`bg-slate-700` → `bg-surface4`；其余按对照表。
- 工作日志 tab 的 `SseLogViewer` 已在任务 6 处理，此处不动。

- [ ] **步骤 5：`ConfigView.vue`**

两处 YAML `pre` 的 `bg-[#0b1120]` → `code-surface rounded-card border border-code-line`（保留原有 `overflow-auto` / `p-4` / `text-xs`）；`text-red-400` 保持；`text-slate-100` → `text-label`、`text-slate-300`/`400` → `text-label2`、`text-slate-500` → `text-label3`。

- [ ] **步骤 6：`DockerInstallPanel.vue`**

**只换类名**：5 态状态机、4 按钮矩阵、UAC `Teleport` + `Transition` 结构、Linux 降级 `.card` 提示块全部不动。

| 旧 | 新 |
|---|---|
| `bg-emerald-600/15 border-emerald-500/30 text-emerald-300` | `bg-ok-bg border-ok-line text-ok` |
| `bg-red-600/10\|15 border-red-500/30 text-red-300` | `bg-danger-bg border-danger-line text-danger` |
| 步骤条 `text-emerald-300` / `text-emerald-400/70` / `text-slate-500` | `text-ok` / `text-ok opacity-70` / `text-label3` |
| `bg-emerald-400` / `bg-emerald-400/70` / `bg-blue-400` / `bg-red-400` | `bg-ok` / `bg-ok opacity-70` / `bg-info` / `bg-danger` |
| 阶段 B 卡 `rounded-md border-slate-700/60 bg-slate-900/60` | `rounded-ctl border border-sep bg-surface3` |
| 行内 code `bg-[#0b1120] text-[11px]` | `bg-code-bg text-code-fg text-[11px]` |
| UAC 遮罩 `bg-black/60 backdrop-blur-sm` | `bg-black/40 backdrop-blur-sm` |
| UAC 面板 `border-amber-500/30` + `bg-slate-900` | `border-warn-line` + `bg-surface2 rounded-panel shadow-l` |
| `border-slate-800/40` / `bg-slate-600` / `bg-slate-700/60` / `hover:bg-slate-800` | `border-sep-soft` / `bg-surface4` / `bg-surface4` / `hover:bg-surface3` |
| `text-slate-200/300/400/500/600` | `text-label` / `text-label2` / `text-label2` / `text-label3` / `text-label3` |
| spinner SVG | `<Loading inline label="" />` |

- [ ] **步骤 7：`StartupProgressCard.vue`**

保留 BEM 块名 `startup-card` / `__track` / `__steps` / `__stripes` 与 `@media (prefers-reduced-motion: reduce)` 降级；条纹渐变硬编码色换变量：

```scss
.startup-card__stripes {
  background-image: repeating-linear-gradient(
    45deg,
    color-mix(in srgb, var(--ok) 45%, transparent) 0 6px,
    color-mix(in srgb, var(--ok) 20%, transparent) 6px 12px
  );
}
```

轨道底色换 `var(--surface-4)`；进度条宽度的内联 `:style` 保留；模板内 slate / emerald / red 类按任务 8 对照表替换。

- [ ] **步骤 8：全量测试 + 提交**

Run: `cd d:\Workplace\modelctl-1\web && npm run typecheck && npm run test`

```bash
cd d:\Workplace\modelctl-1
git add web/src/views/LoginView.vue web/src/views/accounts/AccountSelfLoginView.vue web/src/views/ModelDetailView.vue web/src/views/ConfigView.vue web/src/components/docker web/src/components/startup
git commit -m "feat(web): 专项视图语义化（登录、模型详情、配置、Docker 安装、启动进度）"
```

---

## 任务 11：全局校验与视觉自查

**Files:**
- 无新增（发现遗漏时回改前面任务的文件）

**Interfaces:**
- Consumes: 前面全部任务
- Produces: 通过全部校验的最终状态

- [ ] **步骤 1：确认旧色类名已按别名表收敛**

Grep 工具，pattern `(bg|text|border|ring|divide|accent)-(slate|blue|emerald|red|amber|sky|rose|orange|yellow|green|teal|cyan|indigo|violet)-\d{2,3}(/\d+)?`，path `d:\Workplace\modelctl-1\web\src`，`output_mode: content`，`-n: true`。

Expected：只允许两类刻意保留项 —— `text-red-400`（e2e 垫片）、`ChatHistoryList.vue` 条目上的 `bg-slate-800`（单测垫片）。出现其它项 → 回对应任务补替换。

- [ ] **步骤 2：确认无硬编码 hex 工具类残留**

Grep 工具，pattern `bg-\[#|text-\[#|border-\[#`，path `d:\Workplace\modelctl-1\web\src`。
Expected：无输出。（允许例外：Logo / 头像的装饰性 `linear-gradient` 写在 `style` 属性里，不是工具类。）

- [ ] **步骤 3：确认 spinner 单一来源**

Grep 工具，pattern `animate-spin`，path `d:\Workplace\modelctl-1\web\src`，`output_mode: files_with_matches`。
Expected：仅 `web/src/components/common/Loading.vue`。

- [ ] **步骤 4：确认语义令牌真的被消费（无整套死令牌）**

```powershell
cd d:\Workplace\modelctl-1\web
npm run build
cd d:\Workplace\modelctl-1
$css = Get-ChildItem dist\assets\*.css | Get-Content -Raw
foreach ($v in '--chrome','--surface-2','--surface-3','--label-2','--ok-bg','--danger-line','--warn-bg','--code-bg','--inset-hl','--seg-knob','--muted','--accent-bg') {
  "$v => " + ([regex]::Matches($css, [regex]::Escape("var($v)")).Count)
}
```

Expected：每项 ≥ 1。计数为 0 说明对应语义类从未被使用，检查替换时是否写错了类名。

- [ ] **步骤 5：起预览服务**

```powershell
cd d:\Workplace\modelctl-1\web
npx vite preview --port 4179
```

`blocking: false` 启动，保持进程运行。

- [ ] **步骤 6：两主题 × 多页面截图自查**

用 Puppeteer MCP：`puppeteer_navigate` 到 `http://localhost:4179/`，再用 `puppeteer_evaluate` 切主题并截图。切主题脚本：

```js
// puppeteer_evaluate
() => { document.documentElement.dataset.theme = 'light'; return document.documentElement.dataset.theme; }
```

必查清单：

| # | 路由 | 主题 | 重点核对 |
|---|---|---|---|
| 1 | `/login` | light | 卡片阴影、无双重光斑、`text-red-400` 处显示为语义红 |
| 2 | `/login` | dark | 光斑协调、卡片内高光 |
| 3 | `/dashboard` | light | KPI 数字等宽、引擎卡三态色、卡片边界（浅色靠阴影不靠描边） |
| 4 | `/dashboard` | dark | 同上（深色靠半透明 + 内高光） |
| 5 | `/models` | light | 表头 uppercase 字距、无斑马线时是否仍易扫读 |
| 6 | `/cluster/nodes` | light | 卡片网格布局、chip 状态色 |
| 7 | `/chat` | dark | 气泡、markdown 代码块恒深 |
| 8 | `/audit` | light | **代码面恒深**是否协调、柱状图配色 |
| 9 | 任意页 | 视口 375px | 汉堡唤出抽屉导航、无横向溢出 |

每张截图后量化检查横向溢出（与 Playwright 断言同源）：

```js
// puppeteer_evaluate
() => {
  const d = document.documentElement;
  return { overflow: d.scrollWidth - d.clientWidth, clientWidth: d.clientWidth };
}
```

Expected：`overflow <= 1`。若 > 1，定位元素：

```js
// puppeteer_evaluate
() => {
  const limit = document.documentElement.clientWidth;
  return [...document.querySelectorAll('*')]
    .filter((el) => el.getBoundingClientRect().right > limit + 1)
    .slice(0, 8)
    .map((el) => `${el.tagName}.${String(el.className).slice(0, 60)} right=${Math.round(el.getBoundingClientRect().right)}`);
}
```

修复手段：容器加 `min-w-0`、长文本加 `truncate`、固定 `w-*` 换成 `max-w-*` + `flex-1`。

- [ ] **步骤 7：与原型逐张比对**

打开 `.superpowers/brainstorm/28100-1789269150/content/prototype.html`，逐项核对：侧栏分组标题字号/字距、选中项 accent 竖条、卡片圆角与阴影强度、表头字距、状态徽标是否已去掉满屏彩底、按钮危险态是否已是柔和描边红。差异逐项回改对应文件，改完重跑步骤 4 的构建与令牌计数。

- [ ] **步骤 8：最终验证**

```powershell
cd d:\Workplace\modelctl-1\web
npm run typecheck
npm run test
npm run build
npm run test:e2e
```

`test:e2e` 需要后端（`modelctl webui`，默认 4173）可用。若后端未起：明确记录「e2e 未跑（后端不可用）」，**不得声称通过**。

- [ ] **步骤 9：按 `CLAUDE.md` 规范沉淀经验**

在 `docs/known-pitfalls/frontend/` 下新建 `apple-redesign-tokens.md`，并在 `docs/known-pitfalls/README.md` 索引登记以下条目（每条含根因 / 方案 / 代码示例）：

1. `:slotted()` 只匹配插槽**顶层**节点，后代选择器必须写在括号外（`:slotted(table) tbody tr`，而非 `:slotted(tbody tr)`）。
2. UnoCSS 变量色不可配 `/opacity` 修饰符；半透明档位必须由 `-bg` / `-line` 令牌显式提供。
3. 裸 `matchMedia().matches` 非响应式；主题必须走 `usePreferredDark()`，否则「跟随系统」不随系统变。
4. 旧色家族整族别名映射 = 分批迁移的安全网；含 `red-400` / `slate-800` 作为测试垫片的取舍原因与副作用边界。
5. `rounded-s` / `rounded-l` / `rounded-m` 与 presetWind3 内置语义冲突，自定义圆角令牌必须换名（`ctl` / `card` / `panel` / `sheet`）。
6. 两主题变量集合必须对称，用 `tokens.test.ts` 在 CI 兜住「漏一个变量导致浅色下残留深色块」。

```bash
cd d:\Workplace\modelctl-1
git add docs/known-pitfalls
git commit -m "docs(known-pitfalls): 沉淀 Apple 化重构中的令牌与主题陷阱"
```

---

## 附录 A：现存旧色类名清单（任务 1 别名表依据）

扫描 `web/src` 全量 `.vue` / `.ts` / `.css` 得到，用于校验别名表覆盖完整：

- **blue**：`blue-100` `blue-300` `blue-400` `blue-500` `blue-600` `blue-700`（含 `accent-blue-500`）
- **emerald**：`emerald-300` `emerald-400` `emerald-500` `emerald-600`
- **amber**：`amber-200` `amber-300` `amber-400` `amber-500` `amber-600` `amber-700` `amber-800` `amber-900` `amber-950`
- **red**：`red-200` `red-300` `red-400` `red-500` `red-600` `red-800` `red-900`
- **rose**：`rose-300` `rose-400` `rose-500` `rose-800` `rose-900`
- **sky**：`sky-300`
- **slate**：`slate-50` `slate-100` `slate-200` `slate-300` `slate-400` `slate-500` `slate-600` `slate-700` `slate-800` `slate-900` `slate-950`
- **硬编码 hex**：`#0f172a`（3 处）、`#0b1120`（8 处）

别名表刻意**不覆盖** `slate-50/100/200/300`（正文文字档）：这四档必须由替换任务显式改成 `text-label` / `text-label2`，若兜进别名会让浅色主题下正文对比度失控。任务 11 步骤 1 的 grep 正是用来抓这类漏改。
