# Apple 化重构：令牌与主题陷阱（前端）

> 2026-09-13 新建主题：Apple 化视觉重构（tokens.css 双主题 + UnoCSS 语义映射）过程中踩过的令牌 / 主题 / 样式层叠陷阱。
> 涉及文件：`web/src/styles/tokens.css`、`web/src/styles/global.css`、`web/unocss.config.ts`、`web/src/stores/theme.ts`。

## `:slotted()` 只匹配插槽顶层节点，后代选择器必须写在括号外

- **日期**：2026-09-13
- **症状**：DataTable 的 `<slot />` 里由使用方插入 `<table>`，scoped 样式 `:slotted(tbody tr):hover { background }` 怎么写都不生效，行悬停无高亮。
- **根因**：`:slotted(X)` 的选择器只匹配**直接插入插槽的顶层节点**。插槽里插入的是 `<table>`，`tbody`/`tr` 是它的后代而不是插槽节点本身，所以 `:slotted(tbody tr)` 恒不命中。
- **解决方案**：把顶层节点写进括号，后代写在括号外——`:slotted(table) tbody tr`。

```css
/* 错：tbody 不是插槽节点，恒不命中 */
:slotted(tbody tr):hover { background: var(--surface-3); }
/* 对：括号锁定插槽顶层 <table>，后代写在括号外 */
:slotted(table) tbody tr:hover { background: var(--surface-3); }
```

- **证据**：`web/src/components/common/DataTable.vue:17-49`（`th`/`td`/`tbody tr` 全部以 `:slotted(table) ` 为前缀，并在注释里钉死该规则）。
- **教训**：Vue scoped slot 样式的选择器边界是"插槽节点"而非"DOM 树任意位置"；后代规则一律写成 `:slotted(顶层) 后代` 两段式。

## UnoCSS 变量色不可配 `/opacity` 修饰符，半透明档位必须由令牌显式提供

- **日期**：2026-09-13
- **症状**：`bg-accent/10` 这类"语义色 + 透明度"写法在旧 WebKit（及部分内嵌 WebView）上静默失效——背景直接不渲染，且不报任何错。
- **根因**：主题色全部是 `var(--xxx)` 变量色。变量色配 `/20` 修饰符时 UnoCSS 只能生成 `color-mix(in oklab, var(--x) 20%, transparent)`，旧 WebKit 不支持 `color-mix` 时整条声明被丢弃（渐进增强在这里反而是静默失效）。
- **解决方案**：半透明档位不用修饰符表达，由令牌体系显式提供 `-bg`（约 8-14%）/ `-line`（约 20-26%）两档：`bg-accent-bg`、`border-danger-line`。设计稿里每出现一处"某色 10%"，落一个语义令牌。

```ts
// unocss.config.ts —— 每个语义色固定三件套：实色 / -bg / -line
ok: { DEFAULT: P('ok'), bg: P('ok-bg'), line: P('ok-line') },
danger: { DEFAULT: P('danger'), bg: P('danger-bg'), line: P('danger-line') },
// 错：bg-ok/10、border-danger/20 —— 变量色 + 修饰符 = 旧内核静默失效
// 对：bg-ok-bg、border-danger-line
```

- **证据**：`web/unocss.config.ts:6-8`（注释即决策记录）、`:47-50`（三件套结构）；`web/src/styles/tokens.css:21-23` / `:53-55`（两主题各自的 `-bg`/`-line` 值）。
- **教训**：以 CSS 变量为色值的体系里，透明度是**设计令牌**而不是语法糖；凡是"想给变量色打个折"的需求，都该落成一个新的语义令牌并让双主题各自给值。

## 裸 `matchMedia().matches` 非响应式，「跟随系统」必须走 `usePreferredDark()`

- **日期**：2026-09-13
- **症状**：主题模式选「跟随系统」后切换 macOS/Windows 深浅色偏好，页面配色纹丝不动，刷新才生效。
- **根因**：`matchMedia('(prefers-color-scheme: dark)').matches` 是一次性读值。computed getter 里读它，响应式系统里没有任何依赖被注册，系统换肤时 computed 永不重算。
- **解决方案**：用 VueUse 的 `usePreferredDark()`（内部 `addEventListener('change')` 驱动 ref），computed 依赖它的 `.value` 即可随系统实时切换；三态真值表抽成纯函数 `resolveTheme()` 便于单测。

```ts
// 错：一次性快照，computed 不会在系统换肤时重算
const isDark = computed(() => matchMedia('(prefers-color-scheme: dark)').matches);
// 对：VueUse 监听 change 事件，systemDark 是真响应式源
const systemDark = usePreferredDark();
const resolved = computed(() => resolveTheme(mode.value, systemDark.value));
```

- **证据**：`web/src/stores/theme.ts:24-33`（注释写明"裸 matchMedia(...).matches 非响应式"）；防 FOUC 的另一半在 `web/index.html` 内联脚本（挂载前先定 `data-theme`）。
- **教训**：浏览器 media query 都是"查询即快照"，凡要跟着系统/视口实时变的，必须显式订阅 change 事件；封装层（VueUse）的意义就是把订阅接进响应式图。

## 旧色家族整族别名映射 = 分批迁移的安全网；`red-400`/`slate-800` 是刻意保留的测试垫片

- **日期**：2026-09-13
- **症状**：重构起步时全库有几十个旧色类名（`unocss.config.ts` 注释按当时口径记为 55 个），不可能一次改完；漏改的 `text-slate-300`、`bg-amber-950` 在浅色主题下渲染成旧 Tailwind 色板的深灰/暗黄，与语义主题严重打架。
- **根因**：`presetWind3` 默认自带 Tailwind 色板，只要还有文件引用旧类名，就会生成旧色值。
- **解决方案**：在 `theme.colors` 里把**整个旧色家族**覆盖成语义变量（`red/rose → --danger`、`emerald → --ok`、`amber → --warn`、`blue → --accent`、`slate → 表面/文字系`）。此后漏改类名自动落到语义色，迁移可以安全分批；配合 `tokens.alias.test.ts` 用 `createGenerator` 在 CI 里断言每个别名的生成结果。
- **副作用边界（垫片，按类别豁免）**：① `red-400` **这一档位整体豁免、不限文件**——它是全站错误提示文字惯例（`views` 下 12 个文件共 20 处），且 e2e 用 `p.text-red-400` 做选择器（`web/e2e/smoke.spec.ts:33`）；② `bg-slate-800` **只在 `ChatHistoryList` 的条目类名上点位豁免**（`ChatHistoryList.test.ts` 断言选中态类名）；③ 除此之外，`slate/blue/emerald/amber/rose/sky` 等档位与 `bg-[#…]` 这类 arbitrary 色值一律视为残留，校验 grep 该报就报。

```ts
// unocss.config.ts —— 家族级覆盖，而非逐类名替换
red: ramp('danger', [200, 300, 400, 500, 600, 800, 900]),
slate: { ...ramp('muted', [400, 500]), 800: P('surface-3'), 900: P('surface-2'), 950: P('code-bg') },
```

- **证据**：`web/unocss.config.ts:8-11`（取舍注释）与 `:55-69`（别名表）；`web/src/styles/tokens.alias.test.ts`（43 条用例：30 语义 + 12 旧色别名 + 1 深合并，其中 `:68` 钉住 `text-red-400 → var(--danger)`）；`web/e2e/smoke.spec.ts:33` 用 `p.text-red-400` 做断言选择器。浏览器实测：该元素 computed `color: rgb(208,51,31)` = `var(--danger)`（任务 11 computed 探针）。
- **教训**：大规模样式迁移的正确姿势是"改映射层，不改使用点"——别名表让**每一次提交都处于可发布状态**；但要给校验脚本写清垫片豁免口径（**按类别/点位豁免，而不是按文件写死数量**），否则要么把惯例用法当残留误杀，要么把真残留当垫片放过。

## `rounded-s` / `rounded-l` / `rounded-m` 与 presetWind3 内置语义冲突，自定义圆角令牌必须换名

- **日期**：2026-09-13
- **症状**：给 `theme.borderRadius` 配了 `s/m/l/xl` 四档，`rounded-s` 却渲染成 `0.125rem`（内置值），`rounded-l` 渲染成 **border-top-left + border-bottom-left** 的"左侧圆角"而非自定义档位。
- **根因**：presetWind3 把 `rounded-s/l/r/t/b…` 定义为**物理方位**缩写（side/left/right/top/bottom），`rounded-m` 之类也与内置键位冲突；`theme.borderRadius` 的自定义键与内置键同名时被内置语义压制（且方位类工具类是规则生成的，不是查表）。
- **解决方案**：自定义档位换成语义命名 `ctl / card / panel / sheet`（控件/卡片/面板/浮层），与方位词表零交集。

```ts
// 错：与内置方位/档位词冲突
borderRadius: { s: P('r-s'), m: P('r-m'), l: P('r-l'), xl: P('r-xl') }
// 对：语义命名，避开 s/l/r/t/b/m 词表
borderRadius: { ctl: P('r-s'), card: P('r-m'), panel: P('r-l'), sheet: P('r-xl') }
```

- **证据**：`web/unocss.config.ts:71`（ctl/card/panel/sheet）；产物 CSS 中 `.rounded-s-*` 仍为内置方位值。
- **教训**：往预设的 `theme` 里加键前，先查该预设的**保留字表**（方位、断点、状态缩写都是雷区）；命名令牌用业务语义而非单字母，既避冲突又自解释。

## 两主题变量集合必须对称，用 `tokens.test.ts` 在 CI 兜住「漏一个变量」

- **日期**：2026-09-13
- **症状**：给 dark 加了 `--foo` 忘了 light，浅色主题下所有引用 `var(--foo)` 的属性退化为未定义——某块区域在浅色下仍是深色，肉眼极难定位到"少声明一个变量"。
- **根因**：双主题各自独立声明变量表，人工维护必然漂移；CSS 对未定义变量的静默容错放大了问题。
- **解决方案**：`tokens.test.ts` 用正则解析 tokens.css 的两个主题块，断言 ① 语义令牌全集在两主题都存在 ② 两主题变量集合**完全相等**（Set 对比）③ 不随主题变的令牌（代码面/圆角/字体/`--seg-knob-label`）必须落在公共 `:root` 块。

```ts
it('两主题变量集合完全对称', () => {
  expect(new Set(varsIn('dark'))).toEqual(new Set(varsIn('light')));
});
```

- **证据**：`web/src/styles/tokens.test.ts:31-33`（对称断言）、`:35-40`（`:root` 恒定令牌断言）。
- **教训**：成对结构（双主题、双端点、双写路径）的对称性不要靠 review，落成一条集合相等断言；新增"只该存在于一侧"的变量会被这条测试直接拦下，逼你回答"它到底随不随主题变"。

## UnoCSS 无 preflight 时 `border-*` 工具类全部不渲染；补 border 基线须连带 UA 按钮底复位

- **日期**：2026-09-13（任务 11 视觉自查发现）
- **症状**：① 所有 `border border-sep`、`.card`、`.input-base` 的 computed `border-style` 恒为 `none`（宽度 1px、颜色正确，就是看不见）；② 未显式给背景的 `button`（`ThemeSwitch` 分段钮、文字按钮）露出 UA 默认 `buttonface` 灰底与黑色立体边框，深色主题下尤其刺眼。重构前的老版本同样如此，只是旧设计几乎不用发丝线边框，Apple 化后成为一等视觉元素才暴露。
- **根因**：presetWind3 只有在开启 preflight 时才注入 Tailwind 的两条基线：`*{border-style:solid;border-width:0}` 与 `button{background-color:transparent;background-image:none}`。本项目的 UnoCSS 集成没有带上这两条；而 `border-sep` 之类工具类只声明 `border-width` + `border-color`，样式缺省值 `border-style: none` 使边框整体不可见。
- **解决方案**：在 global.css 的既有通配 reset 里补两行（等价 Tailwind preflight），并显式复位 button 底色。通配符特异度 (0,0,0) 低于任何工具类 (0,1,0)，`border-0`、`border-dashed`、`.iconbtn` 的 `border: 0.5px …` 都能正常覆盖；`button{background-color:transparent}` 特异度 (0,0,1)，任何 `bg-*` 类可覆盖。

```css
/* global.css —— 等价 Tailwind preflight 的最小集 */
*, *::before, *::after {
  box-sizing: border-box;
  border-width: 0;
  border-style: solid; /* 缺了这行：border-* 工具类只给宽度/颜色，全部不渲染 */
  margin: 0;
  padding: 0;
}
button { background-color: transparent; background-image: none; } /* 否则露 UA 灰底 */
```

- **证据**：修复前浏览器探针 `.card/.input-base/.btn-danger` computed 全部 `{"w":"0px","st":"none"}`，修复后全部 `1px solid` + 语义色（`.card` = `1px solid rgba(0,0,0,.075)`、`.btn-danger` = `1px solid rgba(208,51,31,.2)`）；修复前截图里 ThemeSwitch/退出登录带 UA 黑框，修复后消失（`shots/01-04`，截图在任务工作区，未入库）。
- **教训**：utility-first 框架的**基线层（preflight/reset）与工具类层是配套契约**；关掉或丢失基线时，工具类"生成了但不生效"，静态 grep CSS 产物完全看不出来——只有 computed style 探针能抓到。引入任何 CSS 框架时，先 diff 它默认注入的 reset 清单。

## 组件根节点自带 display 工具类，会压掉宿主传入的响应式隐藏类

- **日期**：2026-09-13（任务 11 视觉自查发现）
- **症状**：375px 视口下侧栏关不掉、永远盖住内容；主题切换器 `hidden lg:inline-flex` 完全不生效，把移动端顶栏挤爆；全局类 `.iconbtn` 写死 `display:grid` 把 `md:hidden` 顶掉，桌面端多出幽灵按钮。
- **根因**：三层叠加——① UnoCSS 产物按类名字典序排布，`.flex`/`.inline-flex`（8.5k 字符处）排在 `.hidden`（6.4k 处）**之后**，同特异度后者胜出，组件根自带的 `flex` 压掉宿主 merge 进来的 `hidden`；② `global.css` 在 `uno.css` **之后**导入，`.iconbtn` 的 `display` 压掉一切响应式工具类；③ Vue 透传 class 到子组件根，冲突发生在最不可见的地方。
- **解决方案**：确立契约——**组件根节点不写 display 工具类，显隐完全交给宿主传入的类**；需要响应式隐藏自带布局类的组件时，外面包一层 `<span class="hidden lg:inline-flex">`。全局类（`.iconbtn`）不声明 `display`，需要居中的调用点在模板上补 `grid place-items-center`。

```vue
<!-- 错：组件根自带 flex/inline-flex，宿主的 hidden 永远压不过它 -->
<aside class="glass flex h-full w-56 flex-col" />
<ThemeSwitch class="hidden lg:inline-flex" />
<!-- 对：根上不写 display；宿主用包一层的方式控制显隐 -->
<aside class="glass h-full w-56 flex-col" />   <!-- Layout: class="hidden md:flex" -->
<span class="hidden lg:inline-flex"><ThemeSwitch /></span>
```

- **证据**：产物 CSS 位置探针（`dist/assets/index-*.css`，28325 字节）`.hidden{` @6423 < `.flex{` @8543 < `.inline-flex{` @8562；修复前 375px 截图侧栏常驻（`shots/11-dashboard-375-light.png` 旧版），修复后 computed 契约 `M375 aside=none / M1440 aside=flex / burger md:hidden 生效`。代码侧可验证：`Sidebar.vue` 根节点无 display 类、`Layout.vue` 用 `hidden md:flex`、`Header.vue` 给 ThemeSwitch 包 `span.hidden.lg:inline-flex`、`.iconbtn` 不声明 display。
- **教训**：utility 层的"优先级"是**产物顺序**而不是选择器直觉；组件库与 utility 共存的规矩只有一条——组件根不声明与宿主可能冲突的布局属性，显隐是宿主的权利。跨层叠层（uno → global.css）引入的全局类同理：只声明 utility 不管的属性。

## 双主题的旋钮/徽章底色若两主题同为近白，其上文字必须用恒定令牌而非 `--label`

- **日期**：2026-09-13（任务 11 视觉自查发现）
- **症状**：深色主题下 ThemeSwitch 选中的分段按钮"白底白字"——`跟随系统` 四个字完全看不见（截图 `04-dashboard-dark.png` 修复前）。
- **根因**：分段控件旋钮底色两主题刻意同为近白（light `#ffffff`、dark `rgba(255,255,255,.96)`，这是苹果 segmented control 的跨主题惯例），而按钮文字跟随 `text-label` → 深色主题下 `--label` 是近白 `#f1f4f9`，白底白字。原型 HTML 里 `.seg button[aria-selected]` 同时改 `background` 和 `color`，实现时只搬了 background。
- **解决方案**：新增**不随主题变**的 `--seg-knob-label: #0f1219`（放公共 `:root` 块，与 `--code-bg` 同区），选中态内联样式同时写 `background: var(--seg-knob); color: var(--seg-knob-label)`；并把它加进 `tokens.test.ts` 的 `:root` 恒定令牌清单防漏。

```css
:root { /* 两主题 seg 旋钮都是近白，旋钮文字必须恒深 */ --seg-knob-label: #0f1219; }
```

- **证据**：修复前 dark 选中态 computed `{color: rgb(241,244,249)（= var(--label)）, bg: rgba(255,255,255,.96)}`；修复后实测 `color: rgb(15,18,25)（= var(--seg-knob-label)）`、未选中钮仍为 `rgb(169,179,196)（= var(--label-2)）`；深色截图 `04-dashboard-dark.png` 旋钮文字清晰（截图在任务工作区，未入库）。`--seg-knob-label` 由 `tokens.test.ts:35-40` 钉在 `:root` 恒定令牌清单里。
- **教训**：双主题对称测试只能保证"变量都存在"，保证不了"**前景/背景配对关系**在两主题下都成立"。凡两主题同色的装饰底（近白旋钮、恒深代码面），其上的前景色必须引用恒定令牌，禁止用随主题翻转的 `--label`/`--canvas`。
