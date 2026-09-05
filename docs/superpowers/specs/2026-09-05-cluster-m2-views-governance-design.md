# modelctl 集群 M2 设计 —— 视图 · 治理 · 备份（中心+前端闭环）

- 日期：2026-09-05
- 上游：`2026-09-03-modelctl-cluster-design.md`（总 spec）、M1 计划账本（`.superpowers/sdd/2026-09-04-cluster-m1-declarative-goals/progress.md`，13/13 闭环，HEAD=517b15f）
- 交付切法：**单里程碑单计划**（brainstorm 定版方案 A）
- 硬约束：**worker 面零 diff、WS 协议不升版**（brainstorm 定版；rollup/审计反向帧 → M3）。
  精确定义：`wsproto.py` 仅允许 §5 T2-② 一处消费侧加固、`agent.py`/`reconcile.py` 零
  diff；`sync.py` 本地写盘逻辑属 M1 已开放的消费侧实现细节（T8 允许动，见 §5 论证）。

## 0. 背景与范围裁定

总 spec §12 的 M2 交付边界行**原文截断**（L926 以 " + " 结尾、文件即止，且无 P2 行）。
本 spec 依据总 spec 正文碎片（§4.2/§9.3/§9.4/§9.6/§9.7/§9.8/§10/§11）+ M1 账本
"留 M2"清单，重新补全 M2 定义，并**修复总 spec §12 截断行**（本 spec 落盘时一并改写
M2 行为完整表述、补 P2 行）。

### 0.1 范围内（六面）

1. **事件流**：中心 events 表 M1 已在写入，缺展示面——定版 `GET /cluster/events`
   响应契约 + CLI `cluster events` + 前端事件流视图。
2. **前端集群视图全可操作**（brainstorm 定版）：`ClusterGoalsView`（目标矩阵 +
   批量下发两段式 + 行内动作）+ 节点详情页（goals/事件流/治理按钮）+ SettingsView
   集群块。
3. **token / 节点治理**：禁用/启用、节点 token 轮换、主动踢除、节点退役；hello/join-check
   拒绝 disabled。
4. **备份**：`cluster backup --to` / `cluster restore --from` + dashboard 备份下载
   （brainstorm 定版：手动，无周期自动化）。
5. **`--cluster` 聚合 flag**：`status` / `list` 两命令走中心聚合视图。
6. **M1 留 M2 小项波**：见 §5。

### 0.2 范围外（M3 及以后，明确不做的理由）

| 项 | 理由 |
|---|---|
| metrics_summary 帧 + `metrics_rollups` 读写 + `cluster stats` | 需改 worker + WS v3，违反本里程碑硬约束 |
| `audit.query` 反向帧、远程节点日志查看 | 同上（总 spec §8.3 拉式立场不变） |
| dashboard 实时推送（WS/SSE 通道） | 事件流轮询（10s）已满足运维时效；推送通道牵动认证与连接治理，独立评估 |
| `_drift_seen` 漂移去重落库 | 重复事件仅噪音不误导，M3 事件治理一并做 |
| SettingsView 远程写 cluster 配置（interval/lease/role 等） | 远程改 `CLUSTER_ROLE`/`CLUSTER_CENTER_URL` 可自断控制面；本期只读展示，写入仍走 .env + 重启 |
| 周期自动备份轮转 | brainstorm 定版排除（过度设计）；运维 crontab 调 `cluster backup` 即可 |

### 0.3 对总 spec 的两处有据偏离

1. **`probe --cluster` 不做**（总 spec §9.7 列了 status/list/probe 三个）：中心无法主动
   探测 worker 端口——worker 只出站、中心不反连（总 spec §8.3），聚合无数据源。
   `--cluster` 只加在 `status`、`list` 两命令上。
2. **goals 单条 GET 豁免**已在 M1 终审定版（总 spec §6.5 已补豁免块），M2 前端沿用
   `GET /cluster/goals?node_id=` 与 `GET /cluster/nodes/{id}`（响应已内嵌
   node/goals/model_states 三段）。

## 1. 数据与协议（零迁移、零新表）

- **不加表、不加列、不动 `wsproto`**。nodes 表 `disabled` 列（M0 建表已有，
  `NODE_STATUSES` 含 `disabled`）在 M2 首次接通写路径。
- 事件 kind 词表定版（现状 11 种 + M2 新增 7 种），`store.py` 增模块级 `EVENT_KINDS`
  常量 + `append_event` 断言守卫（M1 `_verify_state_vocabulary` 同款 fail-fast 模式）：
  - 既有：`node.join`、`node.join_check`、`node.sync`、`node.model_action`、
    `goal.create`、`goal.update`、`goal.delete`、`goal.retry`、`goal.drift`、
    `action.result`、`token.rotate`
  - 新增：`node.disable`、`node.enable`、`node.kick`、`node.retire`、`db.backup`、
    `db.restore`（CLI restore 在替换完成后**向恢复后的新库**追加此事件——语义为
    "本库系某时刻自备份恢复而来"，写旧库无意义、丢弃则审计断链）、
    `goal.sync_overflow`（§2.4）
- 敏感字段纪律不变：token 明文永不入 events payload / 日志（总 spec §11.4）。

## 2. 后端契约（中心侧；薄层原则：消毒入参→调 core→消毒出参）

### 2.1 治理端点（admin_cluster.py 增量）

| 端点 | 语义 | 关键契约 |
|---|---|---|
| `POST /cluster/nodes/{id}/disable` | 禁用节点 | `store.set_node_disabled(true)`；若在线 → 等同一次 kick；hello/join-check 此后拒绝（401 `节点已禁用`）；goal 台账**不动**（禁用≠撤销声明，重新启用后 reconciler 按既有 revision 收敛） |
| `POST /cluster/nodes/{id}/enable` | 解除禁用 | status 回落由下一次 hello/心跳自然决定 |
| `POST /cluster/nodes/{id}/rotate-token` | 节点 token 轮换 | 调既有 `store.rotate_node_token`；**响应返回新 token 明文一次**（同 join-check 先例）+ 提示"请在该节点 .env 更新后重启"；连带 kick（旧 token 即刻失效） |
| `POST /cluster/nodes/{id}/kick` | 主动断连 | `conns.revoke(node_id)`（新增：摘除当前 epoch）→ WS 循环下一帧 `is_current` 失败 → error 帧 + close；worker 指数退避重连（最坏 30s 回来，届时若未禁用即恢复）——kick 是**一次性**断连，用于强制刷新身份/配置场景 |
| `DELETE /cluster/nodes/{id}` | 节点退役 | 前置：先 kick；删除 nodes 行 + 该节点全部 model_states + 该节点全部 goals（**连带撤销声明**，否则快照里指向幽灵节点的 goal 永挂 PENDING）；有 goals 被连带删除时响应带 `removed_goals: n` 计数；**不删** events 历史（审计留痕）。终审"ghost 节点孤儿 model_states 无删除入口"由此闭环 |
| `GET /cluster/backup` | 热备下载 | `sqlite3` backup API 到临时文件 → `FileResponse` 附件（文件名 `modelctl-cluster-<YYYYMMDD-HHMMSS>.db`）+ `X-Backup-Sha256` 响应头；写 `db.backup` 事件（不记内容只记动作）；含 token/join_token 明文——访问权限=`require_auth`（管理员域，与总 spec §11 信任模型一致） |

`NodeRegistry`（或 store 薄封装）承载 disable/enable/retire 的复合写；REST 只做参数消毒。
disabled 在 `handle_hello` 的检查置于 token 校验**之后**、upsert **之前**（先认身份再谈
解禁，防未授权探测禁用态）。

### 2.2 events 读端点定版（改造 M0 遗留 `GET /cluster/events`）

- 响应行形状：`{ts: "<YYYY-MM-DD HH:mm:ss>", node_id, goal_id, kind, text}`——
  `ts` 用 `_fmt_ts` 后端格式化（单端格式化纪律）；`text` 为 kind+payload 的**后端拼装**
  一句话展示文本（如 `goal.update（intent→stop；操作者 api）`），前端不再拼 payload。
- 参数：`node_id`（可空）、`limit`（1..1000，默认 100）；新增 `kind` 过滤（可空，
  必须 ∈ EVENT_KINDS，非法 400）。
- 拼装函数 `event_text(row) -> str` 落 `store.py` 或新 `events.py`（实现者按内聚判断，
  spec 只钉"中心单端、CLI 与前端消费同一响应"）。

### 2.3 profile 目录读端点（goal 新建弹窗数据源）

`GET /cluster/profiles` → `{profiles: [{name, engine, version}]}`：中心 `MODELS_DIR`
扫 YAML（复用 `profiles.py` 读取器，只读不落敏感内容）；同名多引擎**逐条列出**
（name 相同 engine 不同），供前端 engine 下拉联动，歧义判断仍由 `POST /goals` 的
gate/`_resolve` 负责（前端只呈现，不重复实现选边逻辑）。

### 2.4 snapshot 尺寸封顶（终审 A-4/T2-①）

`GoalService.snapshot_for` 生成后总量 > `MAX_SNAPSHOT_BYTES`（默认 8 MiB，env
`CLUSTER_MAX_SNAPSHOT_BYTES` 可调，floor 64 KiB）→ **不下发**该节点 sync 段 +
写 `goal.sync_overflow` 事件（入 EVENT_KINDS 第 17 种）+ logger.error。不静默截断
（截断快照=半套声明，比不下发更危险）。

### 2.5 core 新模块 `core/cluster/backup.py`

- `create_backup(dest: Path) -> dict{sha256, bytes}`：sqlite backup API（热备不阻写）；
  dest 父目录不存在则报错，已存在同名文件拒绝覆盖（`--force` 例外，CLI 侧）。
- `verify_backup(src: Path) -> tuple[bool, str]`：可打开 + `PRAGMA integrity_check` +
  必备表集合（nodes/goals/model_states/events/meta）齐备 + `meta.join_token` 存在性
  **仅告警不硬失败**（老备份可能无）。
- `restore_backup(src: Path) -> None`：校验通过后——当前库先自动就近备份
  `<db>.pre-restore.<ts>.bak` → 文件替换。前置条件：中心 webui **未运行**（检测失败即
  拒执并提示），故 restore 仅 CLI 可达、无 REST 端点（在线替换自身运行库无可靠语义）。

## 3. CLI 增量（`modelctl cluster …`，全部经中心 REST，restore 除外）

| 命令 | 说明 |
|---|---|
| `cluster events [--node ID] [--kind K] [--limit N]` | 渲染 §2.2 响应；表格走 `display_width` 对齐 |
| `cluster node disable/enable/rotate-token/kick --node <id>` | 对应四个治理端点；`rotate-token` 成功打印新 token + 更新指引 |
| `cluster node retire --node <id>` | DELETE；带 goals 时二次确认文案含 `removed_goals` 数 |
| `cluster backup --to <path> [--force]` | 走 `GET /cluster/backup` 下载 + 落盘后本地 sha256 复核对账 |
| `cluster restore --from <path>` | **不走 REST**：直接调 `backup.restore_backup`（中心须已停机）；执行前交互确认（`--yes` 跳过） |
| `status --cluster` / `list --cluster` | 全局 flag：改走中心（`GET /cluster/nodes` + 各节点 goals 聚合），按 node 分组表格；solo/中心不可达时报错退 2，不回退本机视图（静默回退=假报数据源）；`probe --cluster` **不提供**（§0.3） |

退出码语义沿用 M1：HTTP≠200→2；成功 0。`goal remove` 式"部分失败"文案维持终审
T12-② 裁决不改。

## 4. 前端增量（Vue 3 `<script setup>` + Element Plus，CLAUDE.md 全套规范）

### 4.1 `ClusterGoalsView.vue`（新路由 `cluster-goals`，菜单"集群目标"）

- **目标矩阵**（总 spec §9.4 的 M2 务实版）：行=节点、列=profile、单元格=
  声明（intent@revision）vs 实际（stage/reason/gpu/port）+ diff 色标（StatusBadge
  扩色）；数据源 `GET /cluster/goals` + `GET /cluster/nodes` 前端 join。
  **列数失控对策**：profile 数 > 8 时降级为"节点筛选 + 列表"形态（矩阵保留 ≤8 列），
  避免横向滚动地狱。
- **批量下发抽屉**（两段式，永不盲发）：profile 下拉（`GET /cluster/profiles`，同名
  多引擎联动 engine 选边下拉）→ 节点多选/`--all` → intent/`--create`/env_overlay
  （JSON 文本域 + 校验）→ **预览**（`dry_run:true` 渲染 gate 逐项 verdict 表）→
  确认按钮（预览成功才点亮）正式提交。
- 行内动作：stop（PUT intent=stop）/ remove（DELETE, ConfirmDialog）/ retry / sync；
  失败展示 `detail` 原文。
- 筛选器：diff 状态（all/收敛中/失败/drift）、节点、profile。

### 4.2 `ClusterNodeDetailView.vue`（路由 `cluster-nodes/:id`）

总 spec §9.3 详情的 M2 子集：基础信息卡（host/hostname/engines/capacity_text/
last_seen/lease）+ goals 表 + model_states 表 + 事件流（本节点过滤，10s 轮询，
SseLogViewer 视觉复用但**数据源是 REST 轮询**非 SSE）+ 治理按钮组：重新 sync、
禁用/启用、轮换 token（成功弹窗展示一次 token + 复制按钮）、踢除、退役（红字输入
node_id 确认，展示将连带删除的 goals 数）。

### 4.3 SettingsView 集群块（总 spec §9.6 的只读版）

角色/中心地址/interval/lease 只读展示（**定版：新增 `GET /cluster/settings` 只读
端点**，返回角色/中心地址/interval/lease 现值——不污染 `GET /cluster/status` 语义、
不新增任何写端点）+ join token 脱敏 + 轮换按钮
（复用既有 rotate 端点）+ **备份下载按钮**（§2.1 backup 端点）。

### 4.4 横切

`cluster.ts` 扩全部新 API；node_id 直显（CLAUDE.md 例外裁决）；时间/尺寸等后端已
格式化字段前端零二次加工；`:deep()`/BEM/`repeatSubmit:false`（备份下载等）沿用；
`vue-tsc` 0 错为验收线。

## 5. M1 留 M2 波（顺带项，逐条钉）

| 项 | 处置 |
|---|---|
| 终审 A-3：`model_verb` 端点展示名不归一 | 补 `_resolve_names`（与 set/remove 同口径）+ 钉 |
| 终审 A-1：`NodeRegistry` 并发契约隐式 | 类 docstring 显式声明"单事件循环前提"+ 违例场景注释 |
| 终审 A-4/T2-①：ack 无尺寸封顶 | §2.4 |
| T7-③：ghost 节点孤儿 model_states | §2.1 retire 端点闭环 |
| T1-④：`SELECT *`（store 各处） | 改显式列清单（纯规范债，随波清偿） |
| T2-②：`_opt_str_list` 单条长度上限 | wsproto 加单条 512 截断（协议面仅消费侧加固，**不算升版**：v1/v2 帧形状不变） |
| T7-②：`_drift_seen` 落库 | 明确推 M3（§0.2） |
| T8：换 engine 孤儿旧 YAML | sync prune 扩展：entries 覆盖时旧 path≠新 path → 删旧文件（worker 侧**逻辑**改动——违反硬约束？否：改动仅在 `sync.py` 消费快照的本地文件管理，不动协议/帧形状。brainstorm 硬约束精确定义为"协议与帧形状零变更、Agent 状态机零变更"，`sync.py` 本地写盘属 M1 已开放的消费侧实现细节；若实现者认为牵动过大可单独降级为"记 drift 事件不删"） |

## 6. 验收（M2 关闭判据）

1. **测试网**：M1 基线 15 文件 401 + M0 81 全绿不回退；M2 新增测试全绿——治理/备份/
   events/profiles 端点、CLI 新命令、backup restore 往返（临时库真备份→损坏→恢复→
   数据对拍）、EVENT_KINDS 词表守卫、snapshot 封顶、T8 孤儿清理；overlay 纪律不变。
2. **前端**：`vue-tsc --noEmit` 0 错 + `npm run build` 绿。
3. **端到端场景**（人工冒烟，单机中心 + 至少 1 worker）：dashboard 完成"新建 goal
   （gate 预览→提交）→ 观察矩阵收敛 READY → 事件流可见全程 → 轮换 token（旧连接立断）
   → 禁用（重连被拒）→ 启用回归 → 备份下载 sha 对账"，全程不开终端。
4. **文档**：README 增 10.7（治理/备份/聚合 flag 命令表）；总 spec §12 M2 截断行修复
   + P2 行补全；known-pitfalls 按规范沉淀。
5. **硬约束核验**：`git diff --stat` 对 `wsproto.py` 仅允许 T2-② 一处消费侧加固，
   `agent.py`/`reconcile.py` 零 diff。

## 7. 计划切分预告（writing-plans 输入）

预计 10-12 Task、纵切依赖排序：治理 core+端点 → 治理 CLI → events 定版+CLI →
backup core+CLI → profiles 端点 → `--cluster` 聚合 → deferred 波（A-3/A-1/T1-④/T2-②/T8）
→ 前端 API 层 → GoalsView → 节点详情 → Settings 块+备份按钮 → 文档+终审波。
账本目录：`.superpowers/sdd/2026-09-05-cluster-m2-views-governance/`。
