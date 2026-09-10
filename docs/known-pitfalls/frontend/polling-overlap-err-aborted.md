# 前端轮询与浏览器保活治理（vue / axios / keep-alive）

> 本文件聚合"前端 polling / ERR_ABORTED 治理"主题。

## 前端 axios 30s 默认超时 + 3s 轮询重叠 → 抑制策略

- **日期**：2026-09-09　**分类**：前端 / 网络

### 现象

前端 `DashboardView.vue` 用 `setInterval` 每 3s 拉一次 `/admin/api/overview`。后端慢（> 3s）时，**同一 tick 不到一拍**，浏览器在同一保活池里发起第二条同 URL 请求；浏览器/浏览器扩展可能在第二条发出时 abort 老连接的 `xhr`，控制台出现 `net::ERR_ABORTED`。前端 axios 表现为 `CanceledError`（`isCancel=true` 或 `code=ECONNABORTED`）。

这并非"实际请求被截断"——axios 的 abort 拦截器**只处理 `CanceledError`**（响应拦截器中 401 分支外的所有 reject 都原样上抛），调用方 `try/catch` 自然接住，但**控制台噪音**会持续刷——尤其在 dashboard / models / services 三页同时挂轮询时。

### 根因

1. **axios 默认 timeout=30_000** 不是问题，是**间隔 < 后端 wall** 才暴露问题。`DashboardView` 3s poll × 后端 11s overview 墙钟 → 4 个 in-flight 请求并发同 URL → 保活池 hit-limit。
2. **axios 对 aborted 的可见性**：`CanceledError` 有 `isCancel=true` 标志且 axios 在 `client.ts` 的响应拦截器里**没有显式过滤**，所有 reject 都按 `Promise.reject(err)` 上抛。调用方必须自行识别"abort vs 真错误"。
3. **浏览器 disk cache 干扰**：vite 的 hashless asset URL（`dist/assets/index-BXjbuTLd.js` 入口 hash 只一次，**模块级 chunk 的 URL 名按内容 hash** 但 **import 入口不变**）。硬刷新前若 disk cache 持有旧 chunk 副本，dev 期间看到的是旧代码的 abort 噪声——项目入库 dist/ 后旧 chunk 已不存在，但**浏览器仍按 URL 缓存命中**。
4. **uvicorn `timeout_keep_alive` 默认 5s**（uvicorn 0.52.4，见 `.venvs/gateway/Lib/site-packages/uvicorn/config.py:229`）：浏览器 keep-alive 习惯更宽松——**Chrome 60s / Firefox 115s / EDGE 120s**（handwiki.org）；窗口未对齐时服务端先 close（FIN 已发但客户端可能还没收到 RST），客户端下一条请求命中的 socket 已被 reset → 浏览器层 `ERR_ABORTED`。Web 仓变更已在 `server.py` 加 `timeout_keep_alive=15` 缓解（见 [backend/overview双进程64-worker-keepalive.md](../backend/overview双进程64-worker-keepalive.md)），**但那组修复只服务于 overview 这条聚合端点**——任何会发"长耗时"长轮询的页面 + 后端 wall > 浏览器保活前缀 的 URL，都要自己防重叠（`pending`/`suppressedByClose`）。

### 解决方案

1. **`DashboardView.vue` 防重叠 pending promise**（[DashboardView.vue](file:///d:/WorkPlace/Pycharm/modelctl/web/src/views/DashboardView.vue) 第 17-55 行）：
   - 用模块级 `let pending: Promise<void> | null = null`；`refresh()` 进函数时若 `pending` 非空**直接 return**，不发起第二次同 URL 请求，本帧自然顺延、UI 保留旧 data。
   - `setInterval` 3s 命中堆积时 UI 不会崩溃，只是**帧率降到后端 wall 的倒数**（11s 后端 → dashboard 卡片约 0.09Hz 更新，超出人类感知）。
   - **静默 abort**：catch 块里 `/aborted|canceled|cancelled/i.test(e?.message ?? '') || e?.code === 'ECONNABORTED' || e?.isCancel` 三条件命中 → 直接 `return`（不写 errMsg，不 console.warn），避免误报警。

2. **`client.ts` 默认 timeout 30s 保持，但注释 alert 谁调用方**（[client.ts](file:///d:/WorkPlace/Pycharm/modelctl/web/src/api/client.ts) 第 5-13 行）：对慢于 3s 的端点调用方**必须**显式做 overlap 防护，不写注释 = 后续谁加 3s poll 谁再踩一次坑。

3. **disk cache 硬刷口诀**：`browser_navigate` 到 `http://127.0.0.1:4173/?_=1788966230#/`（query 强制 track 级 cache bypass）+ `browser_evaluate({script: 'localStorage.clear(); sessionStorage.clear()'})` 双清，能彻底忽略 hash 级 cache。这只能一次性验证；线上还是要靠"旧 chunk 已被 dist/ 替换 + 用户硬刷新"兜住。

4. **大轮询 / 大响应后端的兜底**：后端加 LRU / wave / 大池（见 backend 镜像条目）的目标，**和前端 pending 防护是正交的两层**：
   - 后端 wall 减到 ≤3s → 浏览器无论如何不会堆叠。
   - 前端 pending 防护 → 后端就算慢了，UI 也不崩、console 也不炸。
   两层各自兜住一半，**不要**只修其中一层而宣称"已修"。

### 代码示例

```ts
// web/src/views/DashboardView.vue
let pending: Promise<void> | null = null;

function refresh(): Promise<void> {
  if (pending) return pending;          // 防重叠
  pending = (async () => {
    try {
      data.value = await overview();
    } catch (err: unknown) {
      const e = err as { message?: string; code?: string; isCancel?: boolean };
      const isAbort =
        !!e?.isCancel ||
        e?.code === 'ECONNABORTED' ||
        /aborted|canceled|cancelled/i.test(e?.message ?? '');
      if (isAbort) return;              // 静默：UI 无影响，保留旧 data
      if (typeof console !== 'undefined') console.warn('overview 失败:', e);
      errMsg.value = e?.message || '后端返回异常';
    } finally {
      pending = null;
    }
  })();
  return pending;
}

onMounted(() => {
  refresh();
  timer = window.setInterval(() => void refresh(), 3000);
});
onBeforeUnmount(() => {
  if (timer !== undefined) clearInterval(timer);
});
```

### 验证

- 后端 wall=11s 实测：浏览器 console 仍会报 0-2 条 `net::ERR_ABORTED`（来自 disk cache 旧 chunk + Firefox 当前 keep-alive 窗口被服务端 close）；
- 硬刷新 + `?_=xxxx` bypass 后：所有响应 200、UI 4 卡片正常、模型列表 51 行全渲染、模型详情 + SSE 日志流正常；
- **tsc 验证**：`npx vue-tsc --noEmit` 通过；**功能验证**：dashboard 4 卡片 / 模型列表 / 模型详情 / 工作日志 全部正确。

### 边界 / 待补

- 后端 overview 后端 wall 进一步压到 ~2s 时，前端 `pending` 防护**仍然有价值**（避免未来 100+ profile 时回归）。
- `setInterval` 在浏览器 tab 切走时会降频（Chrome / Firefox 都如此），**回来那一拍**会集中打回堆积——`pending` 防护此时正好帮上忙。
- 大模型返回（如 GET /admin/api/backup）走 `downloadBlob` 用 `timeout: 120_000`，**与 overview 不同口径**，别忘了注释里标一下。
- 若未来引入 SSE（`EventSource`），SSE 的 `onerror` 不会走 axios 拦截器，需要**单独** `addEventListener('message' + name)` 配对（见 [backend/sse-named-events.md](../backend/sse-named-events.md)），与 `pending` 防护不冲突但需各自独立。
