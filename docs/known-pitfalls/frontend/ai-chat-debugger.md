# AI 对话调试台（前端实现陷阱）

> 原始单文件已并入本文件归档（2026-09-12 新建主题：AI 对话调试台的组件声明 / IME / SSE 帧解析 / markdown 安全）。

## `<script setup name="X">` 在本仓库是 no-op，且会丢失 TypeScript 支持

- **日期**：2026-09-12
- **症状**：按计划/CLAUDE.md 的写法给路由视图写 `<script setup name="chat">`，组件 name 既不生效（keep-alive 无法按 route.name 命中），又因为没写 `lang="ts"` 直接失去类型检查。
- **根因**：`<script setup>` 上的 `name` 属性并不是 Vue 官方编译器的能力，而是 `vite-plugin-vue-setup-extend` 提供的编译期扩展（`@vue/compiler-sfc` 的 `compileScript` 不认这个属性，产物里没有 `name`）。本仓库 `web/package.json` 未安装该插件，全仓也搜不到一处 `<script setup name=` 用法——约定与依赖脱节，写了也不报错、只是静默失效。
- **解决方案**：用官方的 `<script setup lang="ts">` + `defineOptions({ name: 'chat' })` 显式声明组件名（Vue 3.3+ 内置，无需额外插件）。

```vue
<!-- 错：name 被忽略，且丢了 TS -->
<script setup name="chat">
<!-- 对：name 与 route.name 一致以配 keep-alive，且保 TS -->
<script setup lang="ts">
defineOptions({ name: 'chat' });
```

- **证据**：`web/src/views/chat/index.vue:1`（`<script setup lang="ts">`）与 `:14-15`（`defineOptions({ name: 'chat' })`）；`web/package.json` 无 `vite-plugin-vue-setup-extend`；全仓 grep `<script setup name=` 无匹配。
- **教训**：仓库约定的语法糖要先确认对应插件/编译能力是否真的装上了，否则是"写了不报错、静默不生效"的隐形坑。

## IME（中文/日文输入法）组合态回车被当成发送

- **日期**：2026-09-12
- **症状**：使用中文输入法在文本框里用回车确认候选词时，消息被直接发了出去，发出去的是还没上屏的半截拼音/候选文本；同时 `preventDefault()` 还可能干扰输入法的候选确认。
- **根因**：模板上写的是 `@keydown.enter.exact.prevent="submit"`。输入法组合态下确认候选词同样会派发一个 `key === 'Enter'` 的 keydown，但组件没检查 `isComposing`，于是"确认候选"被误当成"发送"，并在事件上先 `preventDefault()`。
- **解决方案**：去掉模板上的 `.prevent`，改由处理函数先判定组合态再决定是否 `preventDefault()` + 发送；`keyCode === 229` 作为老浏览器的兜底判据。

```vue
<!-- 错：组合态也被当发送，且无条件 preventDefault -->
<textarea @keydown.enter.exact.prevent="submit" />
<!-- 对：先看 isComposing（keyCode 229 兜底），再拦截并发送 -->
<textarea @keydown.enter.exact="onEnter" />
```

```ts
/** 中文/日文输入法用回车确认候选时 key 仍是 'Enter'，此时不得发送（keyCode 229 为兼容兜底）。 */
function onEnter(e: KeyboardEvent) {
  if (e.isComposing || e.keyCode === 229) return;
  e.preventDefault();
  submit();
}
```

- **证据**：`web/src/components/chat/ChatComposer.vue:68-72`（`onEnter`）、`:95`（`@keydown.enter.exact="onEnter"`）；回归用例 `web/src/components/chat/ChatComposer.test.ts:17-38`。
- **教训**：凡"回车即提交"的输入框，都要显式处理 IME 组合态，不能只靠 `.exact` 修饰符。

## SSE 逐块解析时对单块做 `\r\n` → `\n` 替换会静默丢流

- **日期**：2026-09-12
- **症状**：SSE 流偶发整段消失——要么整条流被当成一帧、要么最后一帧丢失；某些上游（CRLF 换行 / 无结尾空行收尾）尤其明显。
- **根因**：不规范的实现会对**每个网络块**单独做 `\r\n` → `\n` 替换再按 `\n\n` 切帧。分隔符可能正好跨块（一块以 `\r` 结尾、下一块以 `\n` 开头），逐块替换会把跨块的 CRLF 破坏掉，从而漏判分隔符；此外若上游以"最后一帧 + EOF"收尾而没有终止空行，循环结束时残留 buffer 会被直接丢弃。
- **解决方案**：累积 buffer，用 `/\r?\n\r?\n/` 正则 `exec` 匹配并靠 `m[0].length` 前进指针；循环结束后把残留 buffer 当作最后一帧解析；`AbortError` 在 `fetch` 与 `reader.read()` 两条路径都走 `onDone()` 收尾，而不是 `onError()`。

```ts
// 必须对**累积后的 buf** 用正则匹配：块边界落在 \r 与 \n 之间时逐块替换会漏分隔符
const delim = /\r?\n\r?\n/;
for (;;) {
  const { value, done } = await reader.read();
  if (done) break;
  buf += dec.decode(value, { stream: true });
  let m: RegExpExecArray | null;
  while ((m = delim.exec(buf))) {
    const frame = buf.slice(0, m.index);
    buf = buf.slice(m.index + m[0].length);
    if (handleFrame(frame)) return;
  }
}
// 上游可能以「最后一帧 + EOF」收尾而没有终止空行，残留 buf 也要当一帧解析
if (buf && handleFrame(buf)) return;
h.onDone();
```

- **证据**：`web/src/api/chat.ts:59-76`（累积 buffer + 正则切帧 + 残留帧兜底）、`:98-105` 与 `:126-137`（两处 `AbortError` 走 `onDone()`）。
- **教训**：流式解析要把"分块"当作不可靠输入——状态必须累积在 buffer 上，任何"逐块预处理"都可能被块边界击穿；收尾帧同理。

## jsdom 下 `KeyboardEventInit.isComposing` 不可靠，IME 用例会"假通过"

- **日期**：2026-09-12
- **症状**：IME 组件的单测里用 `new KeyboardEvent('keydown', { isComposing: true })` 构造事件，断言"不发送"，但该断言并未真正测到组件的 IME 守卫——环境差异会让用例变成恒真/假通过。
- **根因**：jsdom 不保证 `KeyboardEventInit.isComposing` 会在实例上生效，事件对象的 `isComposing` 可能恒为 `false`。于是"组合态不发送"这条用例即便组件没有守卫也可能通过，失去判别力。
- **解决方案**：用 `Object.defineProperty(ev, 'isComposing', { value, configurable: true })` 在事件实例上把该字段写死，确保测的是组件自身逻辑而非环境行为。

```ts
function pressEnter(el: HTMLTextAreaElement, isComposing: boolean) {
  const ev = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
  Object.defineProperty(ev, 'isComposing', { value: isComposing, configurable: true });
  el.dispatchEvent(ev);
}
```

- **证据**：`web/src/components/chat/ChatComposer.test.ts:11-15`。
- **教训**：凡是测试依赖某个 DOM 事件 init 字段，先确认 jsdom 是否支持；不支持就用 `defineProperty` 在实例上定桩，否则护栏用例会退化成恒真。

## markdown-it 用 `html: false` 不足以拦掉 `onerror` 字面量，安全边界要落在 DOMPurify

- **日期**：2026-09-12
- **症状**：期望"渲染后的 HTML 里不出现 `onerror`/`<script`"这一安全断言，在 `html: false` 下并不能满足——被转义后的文本里 `onerror` 这类字面量仍然存在（只是不再是属性），断言会失败；而渲染 AI 输出又需要允许内联 HTML。
- **根因**：`html: false` 只是不解析内联 HTML，转义为文本并不等于"内容里没有危险字面量"；单纯靠它做安全断言是对语义的误解。
- **解决方案**：`html: true` 允许内联 HTML 渲染，把安全边界明确放在 `DOMPurify.sanitize()` 上（script、事件属性在消毒阶段被剥离），`renderMarkdown` 只对外暴露已消毒的 HTML 供 `v-html` 使用。

```ts
const md = new MarkdownIt({ html: true, linkify: true, breaks: true, /* …highlight… */ });

export function renderMarkdown(text: string): string {
  if (!text) return '';
  return DOMPurify.sanitize(md.render(text), { ADD_ATTR: ['target'] });
}
```

- **证据**：`web/src/utils/markdown.ts:13-31`（`html: true` + `DOMPurify.sanitize`）、`web/src/utils/markdown.test.ts:11-15`（`<img src=x onerror=...>` + `<script>` 被消毒）。
- **教训**：不能把"转义"当"消毒"。不可信内容渲染的统一做法是"允许 HTML + 集中消毒"，并让安全断言落在消毒实现上，而不是依赖某个开关关闭后的字面量形态。
