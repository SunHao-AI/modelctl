# Vue 3 computed 依赖冻结：驱动它的 tick 提前停摆使状态永久卡终态

> 本文件为 Web UI 任务中心（2026-09-07）沉淀问题的聚合入口；原始单文件已并入本文件归档以保留溯源信息。

## 时间驱动 computed 的依赖（now ref）被自己的 watch 提前停表 —— 按钮永久卡 ✔/✗ 且永久 disabled

- **日期**：2026-09-07　**分类**：前端 / 响应式

### 根因

`TaskButton.vue` 的 `phase` computed 用 `now.value - finishedAt < 2000` 实现"任务完成后 2s 显示 ✔/✗，之后回 idle"。驱动 `now` 的是一个 500ms tick，其存活条件由 `watch(getter)` 决定，而 getter 最初只把 `status ∈ {queued, running}` 当活跃：

1. store `finalize` 瞬间 status 翻到终态 → getter 变 false → **pre-flush 微任务里立刻 `clearInterval`**，下一次 tick 不再执行，`now` 冻结在 finalize 前 ≤500ms 的值；
2. 冻结的 `now` 距 `finishedAt` 恒 < 2000 → `within` 永真 → phase 恒为 ok/fail；
3. `phase` 是 computed，其依赖（now / record / submitting）**此后全部不再变化** → 缓存永不失效 → 按钮永久禁点，只有刷新才能解锁。

本质是**自指死锁**：computed 依赖 `now`，`now` 靠 tick，tick 的存活判据却依赖"phase 所描述的状态离开活跃"——finalize 恰好触发停表，而"结果窗到期回 idle"这一步本身就还需要 tick 再推 `now` 才能被计算出来。tick 死在了它自己负责收尾的场景之前。

### 解决方案

**getter 必须继续读依赖，把 tick 存活期延长到"活动期 + 结果窗"**——凡 computed 的求值语义依赖时间推进，驱动时间的 ticker 就必须存活到该时间窗真正结束：

1. watch getter 终态分支改为 `!!r.finishedAt && now.value - finishedAt < 2000`：终态后 getter 仍读 `now.value`，tick 存活、每 500ms 推 `now`；
2. 某次 tick 使 `now - finishedAt ≥ 2000`：同一轮依赖更新里 getter 变 false → `clearInterval` 停表，同时 phase 的 `within` 变 false → 回 idle。**停表与 phase 回 idle 由同一次 tick 原子达成**，不存在"phase 还差一次 tick"的死角；
3. 停表后 `now` 冻结在 ≥2000ms 处，`within` 恒 false，phase 稳定 idle，无常驻定时器泄漏。

通用规则：

- computed 的依赖集合由**最近一次求值路径**决定；让 computed"随时间恢复"的条件，必须有活的响应式源（tick/事件）持续供给，且该源的启停判据不能把恢复所需的最后几次驱动掐掉。
- "到期自动停"类 watch，判据里**必须包含自己的输出所依赖的字段**（本例 getter 不读 `now` 就永远意识不到自己该在 2s 后停）。
- watch 回调默认 pre-flush，会在同一轮里抢在 computed 重算前执行副作用——设计"停表 vs 重算"顺序时按微任务时序推演，别按直觉顺序。

### 代码示例

```ts
// ❌ 反例：终态即停表 —— now 冻结在 finishedAt 前，within 永真，phase 卡 ok/fail
watch(
  () => {
    const r = record.value;
    return !!r && (r.status === 'queued' || r.status === 'running');
  },
  (active) => { active ? startTick() : stopTick() },  // finalize 当轮就 clearInterval
  { immediate: true },
);

// ✅ 正例：web/src/components/common/TaskButton.vue —— 结果窗内 getter 继续读 now，窗毕原子停表
const now = ref(Date.now());
let tickId: number | undefined;
watch(
  () => {
    const r = record.value;
    if (!r) return false;
    if (r.status === 'queued' || r.status === 'running') return true;
    // 结果窗：终态后 2s 内保持 tick 推进 now，到期自动停表 → phase 回 idle
    return !!r.finishedAt && now.value - new Date(r.finishedAt).getTime() < 2000;
  },
  (active) => {
    if (active && tickId === undefined) {
      tickId = window.setInterval(() => (now.value = Date.now()), 500);
    } else if (!active && tickId !== undefined) {
      clearInterval(tickId);
      tickId = undefined;
    }
  },
  { immediate: true },
);
```
