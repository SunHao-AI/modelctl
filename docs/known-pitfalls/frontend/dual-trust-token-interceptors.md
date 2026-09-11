# 前端双信任域 token 拦截器（主题聚合）

> WebUI 同时承载两个互不相通的信任域：管理面 `API_KEY`（Bearer，长期）与账号面 `JWT`（Bearer，`ACCOUNTS_ENABLED=true` 才启用）。若复用同一个 axios 实例按 `URL 前缀分派`，`/api/account/*` 的 401 会误清 admin token、把 admin 用户也弹去登录页；路由守卫顺序颠倒则会让持有时效 JWT 但 API_KEY 无效的用户被跳到错误的登录页。规范参考：多账号体系 spec `docs/superpowers/specs/2026-09-08-accounts-api-keys-design.md`。

## 复用同一个 axios 实例按 baseURL 分派 401 处理——两个信任域 401 会互相串

- **日期**：2026-09-11　**分类**：前端 / 双信任域
- **现象**：`/api/account/*` 请求拿到 401（JWT 过期/密码修改），若拦截器按"全局 401 = 清 admin + 跳 /login"处理，用户明明登录在账号自助面板，却被弹到 admin 登录页；反过来 admin token 过期，账号面 self 面板也丢会话。两信任域本就不该共享会话生命周期——admin 的 `API_KEY` 是长期凭据（用户手工管理），account 的 JWT 是短期会话（登录即发、`exp` 到点即过期），过期语义、恢复路径、目标登录页全不同。
- **根因**：把"401 = 未登录"当成单一信号来源回写 `store.token`；一个 axios 实例的响应拦截器无法按 baseURL 隔离状态，同 `instance.interceptors.response` 里两条 401 分支必然竞争同一个 `clearSession()`。
- **解决方案**：物理拆两个 axios 实例，各自独立 baseURL、独立请求头注入、独立 401 处置：`web/src/api/client.ts` (`baseURL: '/admin/api'`，401 → `clearSession` + 跳 `/login`)；`web/src/api/accountClient.ts` (`baseURL: '/api/account'`，401 → `clearAccountSession` + 跳 `/account/login`)。auth store 拆两条独立 ref（`token` / `accountToken`、`profile` / `accountProfile`），绝不共享 `setSession` 语义。
- **通用教训**：`Bearer` 只是 request 头名，不代表同一凭据体系。**凭据生命周期不同必然信任域不同**，前端会话隔离原则按"信任域"而非"协议/URL 前缀"划；同类情形：管理面 + 版本迁移"旧 token 兼容期"、OAuth + Local Account 双通道登录，都不应用一个 store token 字段两处挂。

## 路由守卫顺序敏感——`public → accountAuth → isLoggedIn` 颠序必段错

- **日期**：2026-09-11　**分类**：前端 / 双信任域
- **现象**：`/account/self` 路由 meta 挂 `accountAuth: true`，`beforeEach` 若写成"先 `!auth.isLoggedIn` 就跳 `/login`"，持 API_KEY 有效但 JWT 未登录的用户访问会被跳到 admin `/login`——他根本没有 admin token（浏览器里同源 localStorage 但 admin 从未登录），只会环跳死锁。真正的三段判据必须按"public → accountAuth → isLoggedIn"守护者序：先看公开页、再看 **该路由所属信任域** 登否、兜底才落到主仓。
- **解决方案**（`web/src/router/index.ts` `beforeEach`）：
  ```ts
  if (to.meta?.public) return true;
  if (to.meta?.accountAuth) {
    if (!auth.isAccountLoggedIn) {
      return { path: '/account/login', query: { redirect: to.fullPath } };
    }
  }
  if (!auth.isLoggedIn) {
    return { path: '/login', query: { redirect: to.fullPath } };
  }
  return true;
  ```
  关键：`accountAuth` 分支**结束前不 return**，让 admin 判定继续持；只有 `accountAuth` 且已登才放行，否则显式跳到 **目标信任域的登录页**（而非混跳）。
- **通用教训**：多信任域共存时，路由级"该域的 GUI 登否"判据必须先于"同一仓主仓登否"判据；两条分支的共同前置（`public`）放在最顶，其余分支按 **specific → generic**（域专属 → 全局）。测试钉：mock `auth.isAccountLoggedIn=true / isLoggedIn=false` 访问 `/account/self` 断言目标为 `/account/self` 而非 `/login`，反向亦然。

## 503 `accounts_disabled` 要在 UI 层做"缺依赖"引导，不是"报错"

- **日期**：2026-09-11　**分类**：前端 / 双信任域
- **现象**：`ACCOUNTS_ENABLED=false` 时后端 `/admin/api/accounts` 族返回 503 `{"code":"accounts_disabled","message":"..."}`；前端若按普通业务错误弹 `ElMessage.error(detail.message)`，用户看到"账号体系未启用"字样后不知道该干啥。管理台本来就是所有管理员入口的门面，这类"环境缺配置"错误应给出 **可执行下一步**，不是单纯复述服务端 message。
- **解决方案**：`AccountsView.vue` 顶部检测 `pickDetail(e).code === 'accounts_disabled'`，切换为引导条（`el-alert type="warning"`）：展示 message + 明确要求的 `.env` 项 `ACCOUNTS_ENABLED=true`，替代整个列表体。`ACCOUNTS_ENABLED=true` 时同样响应里 code 非 `accounts_disabled` 才落地为常规列表/详情流程。
- **通用教训**：区分「业务错误」（有可执行修复路径，落地为 alert）与「故障错误」（服务端异常，落地为 message）。503 类"环境未就绪"属于前者，前端 `code` 分派表比字符串匹配可靠；后端注入 `{"code": "..."} `信封的式已在 [网关鉴权](../backend/网关鉴权.md#hmac.compare_digest-对非-ascii-str-抛typeerror——须以-utf-8-bytes-比较) 与 [错误分类与修复引导](../backend/错误分类与修复引导.md) 沉淀同型机制。

## Vue Router 401 redirect 死锁——`/account/login` 本身不能被 `accountAuth` 拦

- **日期**：2026-09-11　**分类**：前端 / 双信任域
- **现象**：`/account/login` 页面自身由 `public: true` 放行。若路由表里给 `/account/login` 也挂了 `accountAuth: true`（例如编辑时想 Sig "登录页也要认证以保护 title"），死锁链：accountToken 过期 → self 面板 401 → 跳 `/account/login?redirect=...` → 守卫发现 `accountAuth` 未登 → 再跳 `/account/login?redirect=/account/login?redirect=...` 递归。URL 里的 `query.redirect` 层层嵌套，`router.replace` 3 次后 Chrome 报"VRouter detected redirect cycle"而停在空页。
- **解决方案**：登录类公开页必须**同时**满足 `public: true`，且 **不挂** 任何信任域 guard meta；守卫里 `if (to.meta?.public) return true` 是最早分水岭，任何后续 `accountAuth / userAuth` 分支都因 `public` 先返回而不可达。
- **通用教训**："公开页 = 不判据任何登录态"是硬约束，登录页/注册页/忘记密码页在 meta 上只保留 `{ title, public: true }`，不叠加任何 `<xxx>Auth` 字段，尤其是**上游 guard 已 true 时下游 guard 短路**——写测试时也要覆盖"公开页 meta 只含 public"这条正向 + 反向（加 `accountAuth` → 3 次跳后断言 URL 未变化）。

## 一次性 Key 明文 modal 用 `v-if` 防文案粘到下一页

- **日期**：2026-09-11　**分类**：前端 / 双信任域
- **现象**：签发 Key 成功后，独立 modal（z-[60]）里展示明文 + "已保存关闭"按钮，切换会话/Key 页签都走同一 `AccountSelfView`。若明文 modal 用 `v-show` 或仅把 `issuedKey` 置空但 `displayedKey` 仍绑 `state`，键盘切换页签后 modal DOM 未销毁，浏览器 `select-all` + 复制（浏览器剪贴板 API 侧） 会带上隐藏明文；Safari/iOS 更甚——`document.hidden` 下仍可以粘贴。此外多个 tabs 各自一次签发，若用单个全局 ref 会被后来的签发覆盖，前一个 modal 已"关闭"但明文实际是后一个，用户以为复制了先前的。
- **解决方案**：
  - 弹窗用 `v-if="issuedKey"` 而非 `v-show`，关闭时**销毁 DOM**；
  - 明文与元数据拆两个 ref（`issuedKey: string | null` / `issuedKeyMeta: { name, expires_at } | null`），签发动作同步 set、关闭同步 set null，两条 ref 在同一响应式 flush 中写；
  - modal 节点内 `select-all` 只对**当次签发**生效：文案+按钮+复制都是 `v-if` 内 DOM，DOM 不在片就没法选。
- **通用教训**：一次性敏感串（key / OTP / secret）DOM 展示原则：**文案需保证在用户主动"关闭/隐藏"操作之前必被丢弃**，`v-if` 是 Vue 里破产最少的语义——`v-show` 只是 CSS hidden，DOM 常驻 + `innerText` 可枚举 + 剪贴板可跨成；独立 modal z-index 也要显式大于 `ConfirmDialog`（z-50），否则 401 时历史弹窗会盖住新提示。记忆点：**Secret 显示时长 == 用户决策时长**。
