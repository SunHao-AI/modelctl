# SSE / 事件解析

> 原始单文件已并入本文件归档（2026-09-11 新建主题：SSE 帧解析与可选调用求值语义）。

## 可选链调用 `hooks.onLine?.(JSON.parse(raw))` 吞掉畸形帧的 onError（BUG-FE-02）

- **日期**：2026-09-11
- **症状**：`web/src/api/sse.ts` 的 log 帧处理器写成 `try { hooks.onLine?.(JSON.parse(raw)) } catch { hooks.onError?.(...) }`。单测里只注册 `onError` 不注册 `onLine` 时，发送非法 JSON 帧 `'{oops'`，onError 永不触发——畸形帧被静默吞掉。
- **根因**：JS 可选调用 `f?.(args)` 在 `f` 为 nullish 时**跳过整个调用表达式，包括参数求值**。`hooks.onLine` 未注册 → `JSON.parse(raw)` 根本不执行 → try 块正常走完 → catch 永不进入。语义上"没人消费就不用解析"看似合理，但它同时短路了**解析失败才有的错误通道**，两个语义被一个 `?.` 绑死了。
- **修复**：解析与分发拆两步——先 `JSON.parse` 进局部变量（异常走 catch → onError 并 return），再 `hooks.onLine?.(parsed)` 分发。

```ts
// 错：onLine 缺省时 JSON.parse 被跳过，catch 形同虚设
try {
  hooks.onLine?.(JSON.parse(raw) as LogSseEvent);
} catch {
  hooks.onError?.(new Error(`Malformed SSE data: ${raw}`));
}

// 对：解析无条件执行，分发才可选
let parsed: LogSseEvent;
try {
  parsed = JSON.parse(raw) as LogSseEvent;
} catch {
  hooks.onError?.(new Error(`Malformed SSE data: ${raw}`));
  return;
}
hooks.onLine?.(parsed);
```

- **教训**：
  1. `f?.( expensive() )` 里**永远不要放有副作用/有异常通道的参数表达式**——求值被短路是规范行为，不是 bug，但几乎总是埋雷。
  2. 写"解析失败走错误回调"这类安全网时，测试必须**只注册错误回调、不注册成功回调**跑一遍；两个回调一起注册会让短路缺陷隐身。
  3. 定位过程用了临时插桩（`try` 前后 + `catch` 内三路 console.log），直接看到 `try ok` 而非 `catch`，一次锁定求值短路；对"catch 没进去"类问题，插桩 try 尾部是区分"未执行"与"未抛错"的最短路径。
- **回归钉**：`web/src/api/sse.test.ts` → `事件分发 > log 帧非法 JSON → onError 而非静默`（只传 `{ onError }` 的 hooks 形状）。
