# modelctl Web 控制台 Apple 化视觉重构 · 设计文档

日期：2026-09-13
范围：`web/`（Vue 3 + UnoCSS 前端）
状态：已与用户逐节确认，待审阅

---

## 1. 目标与范围

把当前"扁平硬边 + 满屏半透明彩色 pill + 硬编码 hex"的运维台观感，重构为苹果系统级的优雅视觉语言，并首次引入**真正生效的浅色/深色双主题**。

**范围内**

- 设计令牌层（CSS 变量）从零建立并落地到 UnoCSS
- 布局壳：侧栏、顶栏、主区
- 全部 17 个视图 + 17 个组件的视觉呈现
- 主题切换机制（当前 SettingsView 的深色开关是纯占位，不生效）
- 图标体系统一（换成 lucide）

**范围外（明确不做）**

- 不改路由结构、API 调用、数据流、store 逻辑
- 不重排信息架构：13 项菜单只做视觉分组，增删/改名不做
- 不引入组件库（项目现在无 Element Plus，保持无）
- 不拆分子系统、不做后端改动

**成功标准**

1. 视图层零硬编码色值（无 `slate-*`、`blue-600`、`#0f172a`、`#0b1120` 字面量残留）
2. 主题切换瞬时全站生效、刷新不闪白、默认跟随系统
3. 全套 vitest + Playwright（5 project）全绿
4. 同一页面在浅/深两主题下均达到原型稿的观感水准

---

## 2. 已确认的设计决策

| # | 决策 | 选定 |
|---|---|---|
| D1 | 重构深度 | 全局视觉语言刷新（不动 IA 与功能） |
| D2 | 视觉方向 | **双主题**：深色走 macOS 毛玻璃层级，浅色走 iOS 留白 |
| D3 | 主题策略 | **三态：跟随系统 / 浅色 / 深色**，默认 `auto`，手动选择才持久化 |
| D4 | 图标 | 引入 `lucide-vue-next`，替换 Sidebar 里 13 段手写 path |
| D5 | 数据密集页 | **按页分配**：实体少→卡片网格；字段密→精修表格 |
| D6 | 令牌实现 | CSS 变量 + UnoCSS `theme.colors` 映射到 `var(--…)` |
| D7 | 代码面 | YAML 编辑器 / SSE 日志 / JSON 展开**两主题恒深**，不做浅色语法配色 |

原型稿（可交互，含主题切换）：
`.superpowers/brainstorm/28100-1789269150/content/prototype.html`

---

## 3. 令牌层

### 3.1 新增 `web/src/styles/tokens.css`

唯一真源，两组变量 + 一组不参与换肤的常量。

```css
:root[data-theme="light"] {
  --canvas: #f2f3f7;
  --canvas-tint: radial-gradient(120% 80% at 100% 0%, rgba(0,113,227,.06) 0%, transparent 55%);
  --chrome: rgba(255,255,255,.72);
  --surface-2: #ffffff;  --surface-3: #f4f5f8;  --surface-4: #eceef3;
  --label: #0f1219;  --label-2: #525b6b;  --label-3: #8790a0;
  --separator: rgba(0,0,0,.075);  --separator-soft: rgba(0,0,0,.045);
  --accent: #0071e3;  --accent-hover: #0077ed;  --on-accent: #ffffff;
  --ok:#0a8a4a;     --ok-bg:rgba(10,138,74,.09);   --ok-line:rgba(10,138,74,.20);
  --warn:#a15c07;   --warn-bg:rgba(161,92,7,.09);  --warn-line:rgba(161,92,7,.20);
  --danger:#d0331f; --danger-bg:rgba(208,51,31,.08); --danger-line:rgba(208,51,31,.20);
  --info:#0a6ed1;
  --shadow-s: 0 1px 2px rgba(16,24,40,.05);
  --shadow-m: 0 1px 2px rgba(16,24,40,.05), 0 10px 26px -14px rgba(16,24,40,.22);
  --shadow-l: 0 2px 6px rgba(16,24,40,.07), 0 24px 60px -24px rgba(16,24,40,.30);
  --inset-hl: none;
  --seg-bg: rgba(120,126,140,.14);  --seg-knob:#ffffff;
  --blur: saturate(180%) blur(20px);
}

:root[data-theme="dark"] {
  --canvas: #0a0c11;
  --canvas-tint: radial-gradient(110% 75% at 12% -8%, rgba(59,130,246,.16) 0%, transparent 55%),
                 radial-gradient(90% 70% at 96% -4%, rgba(16,185,129,.10) 0%, transparent 52%);
  --chrome: rgba(26,30,40,.60);
  --surface-2: rgba(255,255,255,.055); --surface-3: rgba(255,255,255,.085); --surface-4: rgba(255,255,255,.12);
  --label:#f1f4f9; --label-2:#a9b3c4; --label-3:#78829a;
  --separator: rgba(255,255,255,.085); --separator-soft: rgba(255,255,255,.05);
  --accent:#4c9bff; --accent-hover:#66abff; --on-accent:#04101f;
  --ok:#3ddc97;   --ok-bg:rgba(61,220,151,.11);  --ok-line:rgba(61,220,151,.22);
  --warn:#f5c451; --warn-bg:rgba(245,196,81,.11); --warn-line:rgba(245,196,81,.22);
  --danger:#ff7a85; --danger-bg:rgba(255,122,133,.10); --danger-line:rgba(255,122,133,.22);
  --info:#6ab8ff;
  --shadow-s: 0 2px 8px rgba(0,0,0,.28);
  --shadow-m: 0 4px 14px rgba(0,0,0,.34), inset 0 1px 0 rgba(255,255,255,.06);
  --shadow-l: 0 18px 50px -18px rgba(0,0,0,.70), inset 0 1px 0 rgba(255,255,255,.07);
  --inset-hl: inset 0 1px 0 rgba(255,255,255,.07);
  --seg-bg: rgba(255,255,255,.085); --seg-knob: rgba(255,255,255,.96);
  --blur: saturate(180%) blur(22px);
}

/* 代码面恒深（D7）：放在 :root，不随 data-theme 变 */
:root {
  --code-bg:#0d1117; --code-fg:#c9d4e3; --code-line:rgba(255,255,255,.07);
  --r-s:7px; --r-m:11px; --r-l:15px; --r-xl:20px;
  --ease: cubic-bezier(.32,.72,0,1);
  --font: -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  --mono: ui-monospace,"SF Mono",JetBrains Mono,Menlo,Consolas,monospace;
}
```

令牌命名刻意用 `surface-2/3/4`（而非原 `surface.0/1/2`）——`0` 这一级现在由 `--canvas` 承担，避免"背景到底是 canvas 还是 surface-0"的歧义。

### 3.2 改 `web/unocss.config.ts`

`theme.colors` / `theme.borderRadius` / `theme.boxShadow` 指向变量。

**圆角键名刻意避开 Tailwind 已有语义**：`rounded-s` / `rounded-l` 在 preset 里已是「起始侧/左侧圆角」，`rounded-m` 也会被当成无效值——若直接占用会静默失效。故采用无语义冲突的名字：`rounded-ctl`(7px 控件) / `rounded-card`(11px) / `rounded-panel`(15px) / `rounded-sheet`(20px 浮层)。

```ts
theme: {
  colors: {
    canvas: 'var(--canvas)',
    surface2: 'var(--surface-2)', surface3: 'var(--surface-3)', surface4: 'var(--surface-4)',
    label: 'var(--label)', label2: 'var(--label-2)', label3: 'var(--label-3)',
    sep: 'var(--separator)', 'sep-soft': 'var(--separator-soft)',
    accent: { DEFAULT: 'var(--accent)', hover: 'var(--accent-hover)', fg: 'var(--on-accent)' },
    ok:     { DEFAULT: 'var(--ok)',     bg: 'var(--ok-bg)',     line: 'var(--ok-line)' },
    warn:   { DEFAULT: 'var(--warn)',   bg: 'var(--warn-bg)',   line: 'var(--warn-line)' },
    danger: { DEFAULT: 'var(--danger)', bg: 'var(--danger-bg)', line: 'var(--danger-line)' },
    info: 'var(--info)',
    code: { bg: 'var(--code-bg)', fg: 'var(--code-fg)', line: 'var(--code-line)' },
    // §6.3 兼容垫片：把测试钉住的两个旧色名重映射成语义值
    red: { 400: 'var(--danger)' },
    slate: { 800: 'var(--surface-3)' },
  },
  borderRadius: { ctl: 'var(--r-s)', card: 'var(--r-m)', panel: 'var(--r-l)', sheet: 'var(--r-xl)' },
  boxShadow: { s: 'var(--shadow-s)', m: 'var(--shadow-m)', l: 'var(--shadow-l)' },
}
```

`slate-800` 被顶掉后，preset 其余 `slate-*` 仍可用（`theme.colors` 与预设深合并），所以第 5–7 批迁移期间的残余旧类名仍能渲染，不会中途出现黑块。`red: { 400 }` 同理只覆盖 400 一档。

shortcuts 重写为语义版（键名保持不变，值全换）：

```ts
'card':      'bg-surface2 border border-sep rounded-card shadow-m',
'btn-base':  'inline-flex items-center justify-center gap-2 rounded-ctl px-4 py-2 text-sm font-medium transition-colors cursor-pointer select-none disabled:opacity-40 disabled:cursor-not-allowed',
'btn-primary':'btn-base bg-accent text-accent-fg hover:bg-accent-hover shadow-btn',
'btn-danger': 'btn-base bg-danger-bg text-danger border border-danger-line',
'btn-ghost':  'btn-base bg-surface3 text-label2 hover:bg-surface4 hover:text-label',
'input-base': 'w-full rounded-ctl bg-surface2 border border-sep px-3 py-2 text-label placeholder-label3 outline-none focus:border-accent focus:shadow-focus',
'label-base': 'block text-sm font-medium text-label2 mb-1',
```

配套新增两条阴影 shortcut（含 `color-mix`/`inset`，写成 arbitrary value 会因空格和括号到处转义，收进 shortcut 最干净）：

```ts
'shadow-btn':  'shadow-[0_2px_7px_-1px_rgba(0,113,227,0.42),inset_0_1px_0_rgba(255,255,255,0.16)]',
'shadow-focus':'shadow-[0_0_0_3.5px_color-mix(in_srgb,var(--accent)_17%,transparent)]',
```

**毛玻璃材质不进 `theme.colors`，单独一条 `.glass` 工具类**：`--blur` 是 `saturate() blur()` 整条 filter 串，而 UnoCSS 没有 `backdrop-filter` 的 theme 槽位，`backdrop-blur-[var(--blur)]` 会被当成 blur 半径解析并静默失效。所以 `--chrome` / `--blur` 两个变量**只被 `global.css` 里这一条原生类消费**（`theme.colors` 中不设 `chrome`）：

```css
/* global.css —— 唯一材质类，避免在 30 个文件里重复 backdrop-filter 长串 */
.glass {
  background-color: var(--chrome);
  -webkit-backdrop-filter: var(--blur);
  backdrop-filter: var(--blur);
}
```

侧栏 / 顶栏 / 抽屉面板 / 移动导航统一写 `class="glass"`。

注：`btn-danger` 由"实心红"改为"柔和描边红"，与原型一致；`disabled:opacity-40` 是苹果的中性禁用观感（用 Tailwind 已有档位，不写 `42` 这类非标值）。

### 3.3 改 `web/src/styles/global.css`

- 删除 `body` 的 `background-color:#0f172a` / `color:#e2e8f0` / 字体栈，改吃 `--canvas` `--label` `--font`
- 加 `body::before` 承载 `--canvas-tint` 光斑（`position:fixed;inset:0;pointer-events:none`），浅色下极淡、深色下蓝绿双光斑
- 滚动条：`background-clip:content-box` + 3px 透明 border 做细滚动条，色用 `--surface-4`
- 路由转场：`.fade-*` 由 `opacity .18s ease` 升级为 `opacity .2s var(--ease)` + `translateY(4px)`
- 数字统一：新增 `.num { font-variant-numeric: tabular-nums }`，所有时间/大小/计数处使用
- 全局 `@media (prefers-reduced-motion: reduce)` 关动效
- 加 `transition: background-color/color .42s var(--ease)` 让换肤有苹果式的柔和过渡

`main.ts` 在 `import 'uno.css'` 之后插入 `import './styles/tokens.css'`（变量必须在工具类之前可见；实际两者都是全局表，顺序只影响同权重覆盖，此处不冲突，按此顺序固化以免后续误改）。

---

## 4. 主题机制

### 4.1 `web/src/stores/theme.ts`（新增，Pinia）

```ts
type ThemeMode = 'auto' | 'light' | 'dark'
const KEY = 'modelctl_theme'          // 仅存手动选择；缺省即 'auto'

const mode = ref<ThemeMode>(localStorage.getItem(KEY) as ThemeMode | null ?? 'auto')
const systemDark = usePreferredDark()               // @vueuse/core，已装依赖，自带 change 监听
const resolved = computed<'light' | 'dark'>(() =>
  mode.value === 'auto' ? (systemDark.value ? 'dark' : 'light') : mode.value)

function apply() { document.documentElement.dataset.theme = resolved.value }
watch(resolved, apply, { immediate: true })          // mode 变 / 系统偏好变都会让 resolved 变

function setMode(next: ThemeMode) {
  mode.value = next
  next === 'auto' ? localStorage.removeItem(KEY) : localStorage.setItem(KEY, next)
}
```

- **必须用 `usePreferredDark()` 而不是裸 `matchMedia`**：裸 `matchMedia(...).matches` 不是响应式的，`computed` 里读它不会在系统换肤时重算，`watch(resolved, …)` 也就永不触发。`@vueuse/core` 已是项目依赖，直接用它。
- 只有用户显式点 `light`/`dark` 才写 `localStorage`；选回 `auto` 则 `removeItem`。
- 导出纯函数 `resolveTheme(mode, systemPrefersDark)` 供单测直接覆盖三态真值表（不依赖 DOM）。

### 4.2 防首屏闪白

`web/index.html`：

- `<html lang="zh-CN" class="dark">` → `<html lang="zh-CN" data-theme="dark">`（默认深色，与旧版观感一致）
- 在 `<head>` 内联一段 5 行脚本：读 `localStorage['modelctl_theme']`，为 `light` 时把 `data-theme` 改成 `light`；`auto` 时按 `matchMedia` 判定。早于 CSS 生效，避免 FOUC。
- 不引入任何外链字体（`-apple-system` / `Segoe UI Variable` 已在栈内，实际渲染即系统 SF/Segoe，零网络开销）

`main.ts` 在 `app.mount()` **之前**调用 `useThemeStore().apply()`，与内联脚本结果对齐。

### 4.3 切换入口

新增 `components/common/ThemeSwitch.vue` —— 三段 Segmented Control（浅色 / 深色 / 跟随系统），`role="radiogroup"` + `aria-checked`，激活段用 `--seg-knob` + 阴影滑动。

挂载两处：

1. `SettingsView` —— **移除现有那个只存值不生效的深色占位开关**，换成 `<ThemeSwitch/>`；原字段不再写入设置（后端无对应字段，本就未生效）
2. `Header.vue` 右侧图标按钮区 —— 用一个 `ThemeSwitch` 的紧凑图标变体（或直接放 `ThemeSwitch`），便于随手切换

---

## 5. 布局壳

### 5.1 `Sidebar.vue`

- 容器：`class="glass border-r border-sep"`，宽度仍 `w-56`（**不动**，e2e 依赖 `aside`/`nav` 与 `hidden md:flex`）
- **语义标签不变**：`<aside>` 内含 `<nav>`，菜单项仍是 `<router-link>` 渲染的真实 `<a>`（e2e 用 `getByRole('link', {name:/体检|probe/i})`）
- 菜单**仍是一个扁平 `menus` 数组**，只是新增 `group` 字段用于渲染分组标题；项、顺序、路径、`meta.title` 全部不变：
  - 概览：仪表板、模型、服务、AI 对话
  - 运维：环境、体检、集群、集群目标
  - 系统：审计日志、账号管理、我的账号、配置、设置
- 选中态：`bg-surface3 text-label font-medium` + 左侧 3px `--accent` 竖条（用伪元素画在容器外沿 `-left-3`，保持与旧版 `border-l-2` 相同的视觉位置）
- 图标：13 段手写 `<path>` → `lucide-vue-next` 组件，用 `<component :is="ICONS[m.icon]">` 分派，集中一个 `iconMap`。`size=16` / `stroke-width=1.75`
- Logo：21px 圆角方块 + `--accent` 渐变 + `0 2px 7px rgba(0,113,227,.38)` 投影

### 5.2 `Header.vue`

- `class="glass border-b border-sep"`（原 `bg-slate-900/60`）
- 标题 `h1/h2`：`text-xl font-semibold tracking-[-.022em] text-label`（**保留现有标签层级**，e2e 断言 `h1, h2`）
- 健康徽标 → `.pill` 语义版：`bg-ok-bg text-ok border-ok-line` + `--ok` 圆点带 3px 光晕
- apiKey 前缀 → `text-label3` + `--mono`
- 任务入口 → 31px 方形 `iconbtn`，角标 `--accent` 底 + `box-shadow:0 0 0 2.5px var(--canvas)` 形成描边
- **原汉堡按钮（当前是无功能占位）改为真实功能**：`md:hidden`，点击切换一个 `mobileNavOpen` 状态，驱动 `Sidebar` 以抽屉形式滑出（遮罩 `bg-black/42` + 面板走 `--chrome`）。这顺带修掉"移动端折叠后完全无法导航"的实际缺陷，且不动 768px 断点契约——桌面仍是 `hidden md:flex` 的常驻侧栏，移动抽屉是额外的 `fixed` 层。
- 右侧新增头像（当前登录身份首字，admin 显示"运维"、account 显示账号名首字）

### 5.3 `Layout.vue`

- 根容器去掉 `bg-[#0f172a] text-slate-100`，改由 `body` 的令牌承担
- `main` 内边距 `p-4 md:p-6` **保持不变**（响应式契约）
- 保留 `transition name="fade" mode="out-in"`

---

## 6. 组件与视图

### 6.1 通用组件

| 组件 | 改造 |
|---|---|
| `StatusBadge.vue` | 视觉改「彩色圆点 + 同色文字」（表格范式）；`STYLE_MAP` 的 **8 个键、`includes('error')` 兜底、「· 健康」副标全部保留**，值换成 `text-ok` / `bg-ok-bg border-ok-line` 等语义串 |
| `TaskDrawer.vue` | 遮罩 `bg-black/42`，面板 `class="glass"` + 左缘分隔线；任务行状态点换 `--info/--warn/--ok/--danger`；**toast 宿主与 `KIND_CLASS` 的三键（success/error/warning）不动**，值换语义色 |
| `ConfirmDialog.vue` | 面板 `bg-surface2 rounded-panel border-sep shadow-l`；`role="dialog"`、`aria-modal`、Esc 关闭、danger 标题色全保留 |
| `Loading.vue` | 语义色 + `--accent`；**成为唯一 spinner 实现**，`ConfirmDialog`/`TaskButton`/`LoginView`/`DockerInstallPanel` 中复制的 4 份 spinner SVG 删除、改用本组件 |
| `TaskButton.vue` | 只改类分派串的色名；**五相位状态机与 `computed-tick-timing` 记录的 tick 逻辑（L59-94，watch getter 内读 `now.value`）一行不动** |
| `SseLogViewer.vue` | 恒深终端 `bg-code-bg text-code-fg`；连接态三色点走语义；`api/sse.ts` 一行不动 |
| `DataTable.vue`（**新增**） | `table` + 表头（`bg-surface3`、11px、`letter-spacing:.055em`、uppercase）+ 行（`border-b sep-soft`、`hover:bg-surface3`）三件套集中定义，9 个表格页复用。列定义仍由各页自己写 `<template #cell>` 具名插槽，不抽象成配置式组件（YAGNI） |
| `ThemeSwitch.vue`（**新增**） | §4.3 |
| `StartupProgressCard.vue` | BEM 块保留（含 `prefers-reduced-motion` 降级），条纹渐变换成语义色变量 |
| `ChatMessage.vue` | 气泡改 `bg-accent text-accent-fg` / `bg-surface3`；9 处 `:deep()` 色值换语义；markdown 排版微调（表格分隔线用 `--separator`） |
| `ChatComposer.vue` | 输入框走 `input-base`；发送钮 `--accent` 圆形 |
| `ChatHistoryList.vue` | 材质换语义色，但 **`.cursor-pointer` 与 active 态的 `bg-slate-800` 类名必须保留**（见下"兼容垫片"） |
| `DockerInstallPanel.vue` | 5 态状态机与按钮矩阵不动，只换色名与卡片材质；UAC 弹窗换 `--chrome` + `--warn-line` 边框 |

### 6.2 视图按范式分配

**卡片网格（实体少、重状态扫描）**
`DashboardView`（KPI 4 卡 + 引擎卡网格）、`ClusterNodesView`、`ClusterGoalsView`、`ClusterNodeDetailView`、`EnvsView`

**精修表格（字段密、需横向对比）**
`ModelsListView`、`ServicesMatrixView`、`AuditLogView`、`AccountsView`、`AccountSelfView`、`ProbeView`

**专项**
- `LoginView` / `AccountSelfLoginView`：`--canvas-tint` 光斑 + `card`，`id="api-key"` 与"登录"按钮文案不动
- `chat/index.vue`：三栏改语义面，`defineOptions({ name: 'chat' })` **必须保留**（keep-alive）
- `ModelDetailView`：tab 下划线换 `--accent`；YAML 编辑器恒深
- `ConfigView` / `AuditLogView` 的 JSON/pre：`bg-[#0b1120]` → `bg-code-bg`
- `SettingsView`：表单走 `input-base` + `ThemeSwitch`
- `AccountSelfLoginView`：同 LoginView

**批量替换规则**（脚本化机械替换 + 人工逐文件复核）

| 旧 | 新 |
|---|---|
| `bg-slate-900`（不透明卡面） | `bg-surface2` |
| `bg-slate-900/60`、`bg-slate-900/80`（侧栏/顶栏） | `class="glass"` |
| `bg-slate-800` | `bg-surface3`（**例外见下**） |
| `bg-slate-700` | `bg-surface4` |
| `bg-[#0f172a]` | `bg-canvas`（多数场景可直接删，由 `body` 承担） |
| `bg-[#0b1120]` / `bg-slate-950` | `bg-code-bg` |
| `text-slate-100/200` | `text-label` |
| `text-slate-300/400` | `text-label2` |
| `text-slate-500` | `text-label3` |
| `border-slate-700(/60)` / `border-slate-800` | `border-sep` |
| `blue-600` / `blue-500` | `accent` / `accent-hover` |
| `bg-blue-600/20` 等半透明彩底 | `bg-ok-bg` / `bg-warn-bg` / `bg-danger-bg` / `bg-surface3`（按语义择一） |
| `emerald-*` | `ok` / `ok-bg` / `ok-line` |
| `amber-*` | `warn` / `warn-bg` / `warn-line` |
| `red-*` | `danger` / `danger-bg` / `danger-line`（**`text-red-400` 例外**） |
| `rounded-xl`（卡片） | `rounded-card` |
| `rounded-lg`（按钮/输入） | `rounded-ctl` |
| 弹窗 / 抽屉面板 | `rounded-panel` |
| `shadow-lg` | `shadow-m` |
| 内联 `:style` 的柱高/进度宽 | 保留（仍是动态值），只换配色来源 |

**两处故意不改（§6.3 垫片依赖）**：`ChatHistoryList.vue` 的 `bg-slate-800`（测试按类名断言）与 `LoginView.vue` / `AccountSelfLoginView.vue` 的 `text-red-400`（e2e 按类名断言）。它们经 §3.2 垫片已被重映射为语义值，视觉上与其他语义面/语义色一致。

### 6.3 测试兼容垫片（关键约束）

三条硬红线，采用「**保留旧类名 + 重定义其值**」而非改测试，把回归面压到零：

1. `smoke.spec.ts` 断言 `p.text-red-400` → 用 §3.2 的 `theme.colors.red = { 400: 'var(--danger)' }` 把该类名重映射成语义危险色。`LoginView` / `AccountSelfLoginView` 的错误段继续写 `text-red-400`，标签仍是 `<p>`。
2. `ChatHistoryList.test.ts` 断言 active 条目 `classes()` 含 `bg-slate-800` → 用 §3.2 的 `theme.colors.slate = { 800: 'var(--surface-3)' }` 重映射。active/inactive 的差异仍然成立（inactive 不带这个类）。

   **迁移第一批就要验证深合并**：写一个临时断言（或 `uno.config` 生成后 grep 产物 CSS），确认 `theme.colors` 与 presetWind3 内置色板是深合并 —— 即 `bg-slate-700`、`bg-red-500` 在迁移期仍能生成。若实测为浅合并（整块覆盖），则改为**只在 `tokens.css` 里补两条同名原语**：`.text-red-400{color:var(--danger)}` / `.bg-slate-800{background-color:var(--surface-3)}`，并在第 8 批清理时把其余 `slate-*` 残留全部替换干净（不依赖内置色板）。两种做法对视图层代码完全同形，切换成本仅在 §3.2。
3. `ChatHistoryList` 条目继续用 `<div class="cursor-pointer">`（不换 `<button>`），且"新建"仍是模板中第一个 `<button>`。

其余必须原样保留的契约：`aside`/`nav`/`h1`/`h2` 语义标签、真实 `<a>` 链接与"体检"文案、`#api-key`、768px `hidden md:flex`、登录按钮 disabled 语义、`localStorage['modelctl_token']`、一次性密钥弹窗的 `v-if`（**禁止改 `v-show`**，DOM 残留即密钥泄漏）、`AccountsView` 的 `accounts_disabled` 503 引导条分派、`EnvsView` 的 `id="env-row-{name}"` 与 `id="docker-bypass"` 锚点、IME 回车守卫（`e.isComposing || e.keyCode === 229`）、Dashboard 3s 轮询的 `pending` 防重叠。

新增依赖：`npm i lucide-vue-next`（devDependencies 无新增）。

---

## 7. 执行顺序与验证

每批独立可编译、可回滚：

1. 令牌层：`tokens.css` + `unocss.config.ts` + `global.css` + `index.html` 内联脚本
2. 主题机制：`stores/theme.ts` + `ThemeSwitch.vue` + `SettingsView` 占位开关替换
3. 布局壳：`Sidebar`（含 lucide + 分组）+ `Header` + `Layout`
4. 通用组件 + `DataTable.vue`
5. 卡片范式视图（5 个）
6. 表格范式视图（6 个）
7. 专项视图（登录、聊天、模型详情、配置、设置、Docker、Startup）
8. 清理：删残余硬编码色、删 4 份重复 spinner、全库 grep 校验
9. 验证 + 按 CLAUDE.md 规范沉淀 `docs/known-pitfalls/frontend/`

**每批结束跑**：`npm run typecheck` && `npm run test`

**最终验证**：`npm run build` && `npm run test`；`npm run test:e2e` 需后端可用，若后端未起则明确说明未跑而非声称通过。

**视觉自查**：起 vite dev，用 puppeteer MCP 分别在 `data-theme=light` / `dark` 下截登录页、Dashboard、模型表格、聊天、集群卡片共 10 张，与原型稿逐张比对；重点核对浅色主题下代码面恒深是否协调、精修表格无斑马线后的可读性。

**性能红线**：`backdrop-filter` 只用于侧栏、顶栏、抽屉/弹窗遮罩这三类大面积固定元素，不给每张卡片加模糊（原型里卡片的"毛玻璃感"由半透明 + `--inset-hl` 内高光模拟，实测开销近零）。

---

## 8. 风险

| 风险 | 处置 |
|---|---|
| 半透明 surface 叠加深色 canvas，浅色主题下边界感不足 | 浅色下 `--surface-2` 用不透明纯白 + `--shadow-s`，靠阴影而非描边分层（已写进令牌值） |
| 精修表格去掉斑马线后长表扫读费力 | 保留 `hover:bg-surface3` + 行分隔线 `--separator-soft`；若自查发现仍费力，给 `DataTable` 加可选 `striped` prop（**不默认开启**） |
| 主题切换时 `canvas-tint` 渐变过渡在部分浏览器不动画 | 渐变元素只 `transition: background`，接受降级为瞬时切换；主体色仍平滑 |
| `lucide-vue-next` 与现有 13 个图标语义对不齐 | `iconMap` 集中一处，逐项核对；找不到完全对应的用形状最接近的线性图标，不换图标语义 |
| 视口横向溢出（Playwright 硬断言 ≤1px） | 每批改完立刻在 375px / 412px / 桌面三档自查；新增长内容一律 `min-w-0` + `truncate` + 原生 `title` 属性（项目无组件库，不引入 tooltip 组件） |
| `color-mix()` / `backdrop-filter` 兼容性 | 两者均需 Safari 16.4+ / Chrome 111+。项目 Playwright 跑 webkit/chromium/firefox 三引擎，若旧版 WebKit 不支持 `color-mix` 会导致聚焦环消失（非致命）；对 `.glass` 补 `@supports not (backdrop-filter: blur(1px)) { .glass{ background-color: var(--canvas) } }` 降级为不透明 |
| 双主题使验收面翻倍 | 每批只自查改动页的两主题截图，最后一批做全量 |
