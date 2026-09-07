# Vue 3 深代理：闭包捕获 raw 对象导致写入不触发响应式

> 本文件为 Web UI 任务中心（2026-09-07）沉淀问题的聚合入口；原始单文件已并入本文件归档以保留溯源信息。

## `ref<T[]>` 深代理陷阱：把 raw 对象交给闭包长期持有，属性写入全部丢失响应式

- **日期**：2026-09-07　**分类**：前端 / 响应式

### 根因

`tasks.value.unshift(rec)` 之后，Vue 在数组内部存储的是 **raw** `rec`；组件经 `store.tasks` 读到的是深代理 `reactive(rec)`。依赖追踪只认**代理路径**上的读写：

- `tasks.value[i].xxx` —— 经 `get` 拦截返回同一 proxy 实例，写它触发 `set` 拦截 → 依赖重算；
- 闭包直接捕获入组**之前**的 raw `rec`，SSE 回调里 `t.status = ...` / `t.logs.push(...)` 全部落在 raw 目标对象上，**绕过所有拦截器** → UI 永不刷新。

Pinia tasks store 的 `track()` 最初写的是 `attachStream(rec)`，SSE 的 onStep/onLog/onDone 在主链路上不刷新按钮/角标/抽屉；而 `bootstrap()` 里 `for (const t of tasks.value) attachStream(t)` 取的是代理，行为正确——同一个 `attachStream` 形成**两条链路响应式分裂**，"有时好有时坏"最难排查。

### 解决方案

**入列后回读代理再传出去**。`const proxy = tasks.value[idx(rec.id)]` 是一次代理路径读取，返回的 proxy 与组件将来经 `store.tasks` 读到的是**同一实例**（`reactive` 对同一 raw 有 WeakMap 缓存，恒返回同一代理）。此后闭包内所有写操作（status/detail/logs.push/teardown 清句柄）都命中拦截器。

通用规则：

1. 放进 `ref<T[]>` / `reactive` 容器的对象，**入列后一律按 key/index 回读**再交给长期持有者（回调闭包、订阅句柄、timer 闭包）；不要把入列前的原始字面量传出去。
2. 长期闭包内每次动数据前**重新经代理路径取**（如轮询回调里 `tasks.value[idx(id)]`），天然免疫"数组被 splice/重排后引用失效"。
3. 症状识别：写入"没报错、数据确实变了（raw 上能读到）、但 computed/模板就是不动"，先怀疑拿的是 raw。可用 `toRaw(t) !== t` 断言传进去的是代理。

### 代码示例

```ts
// ❌ 反例：unshift 后把 raw rec 交给 attachStream，SSE 回调写 raw 不触发更新
tasks.value.unshift(rec);
trim();
attachStream(rec);

// ✅ 正例：web/src/stores/tasks.ts —— 回读代理再传入
tasks.value.unshift(rec);
trim();
// 必须把数组里的响应式代理交给 attachStream：ref<TaskRecord[]> 是深代理，
// 组件读到的是 reactive(rec)，若闭包捕获 raw rec 则写入不触发依赖更新
const proxy = tasks.value[idx(rec.id)];
if (proxy) attachStream(proxy);
```

```ts
// ✅ 轮询/宽限期回调内部同理：先经代理路径重取，再写入
const i = idx(t.id);
if (i < 0) return;
const cur = tasks.value[i];   // 代理版本
cur.lastEventAt = Date.now(); // 命中 set 拦截 → runningCount/视图重算
```
