# 集群治理与备份（主题聚合）

> 本文件为 M2 期间沉淀问题的聚合入口；原始单文件已并入本文件归档以保留溯源信息。

## goal 快照超尺寸时"截断下发"会让 worker 剪掉服役文件

- **日期**：2026-09-06　**分类**：后端 / 集群下发
- **根因**：sync 快照是"全量声明"语义——worker 的 prune 分支把"快照里没有"当作"中心已撤销"，会删本地 YAML、停对应模型。若因体积上限对快照做截断下发，被截掉的 goal 在 worker 视角等价于被撤销，服役中的模型会被静默剪掉。
- **解决方案**：超限**整段不下发**（ack 不带 sync 段、不写 `last_goal_sync_sha`），写 `goal.sync_overflow` 事件 + `logger.error`；worker 保持旧快照继续服役，运维在 dashboard/事件流看到溢出后拆分或清理 goal。上限 `CLUSTER_MAX_SNAPSHOT_BYTES`（默认 8 MiB，floor 64 KiB 防误配把 sync 整体禁掉）。
- **测试钉**：`tests/test_cluster_goals.py::test_snapshot_overflow_*`（灌大 `profile_yaml` 越限，断言 ack 无 sync 段 + 事件计数切片只含 `goal.sync_overflow`）。

## 事件 kind 词表守卫若 fail-fast，worker 一条未知 kind 就把 WS 循环打成 500

- **日期**：2026-09-06　**分类**：后端 / 集群治理
- **根因**：WS event 帧的 kind 是 worker 自由字符串（M0 契约"未知消息一律不回显不拒绝"）。若在 `append_event` 对词表外 kind 硬抛，一条恶意/旧版 worker 的未知 kind 就能掀掉整条长连接。
- **解决方案**：守卫降级为 `logger.warning`（kind 经 `[:64]` 消毒）后照常入库；fail-fast 断言只用于导入期自检（中心自有埋点必须全在 `EVENT_KINDS` 内）与测试钉。`event_text` 兜底分支保证词表外 kind 也有合法展示、永不抛。
- **同族裁决**：`status/list --cluster` 中心不可达时报错退 2，**绝不静默回退本机视图**——数据源静默切换会让用户拿本机数字当集群全貌做运维决策。`--cluster` 分发短路于 `caps = probe()` 之前（纯中心机可能无本地引擎环境）。

## 在线替换自身运行库：restore 若走 REST 等于让进程抽掉自己的地基

- **日期**：2026-09-06　**分类**：后端 / 集群备份
- **根因**：中心 webui 运行中直接 `os.replace` 自己的 SQLite 文件，已打开的连接与 WAL 状态未定义；"恢复成功"的响应甚至可能由即将被替换的旧库处理链路发出。
- **解决方案**：restore 不提供 REST，仅 CLI 直调 `backup.restore_backup`；前置 `process.is_running(WEBUI_INSTANCE)` 检测，webui 在跑即拒执。替换前先自动就近备份 `<db>.pre-restore.<ts>.bak`。备份侧用 sqlite3 backup API（热备不阻写），`GET /cluster/backup` 经 `BackgroundTask` 清理临时文件；CLI 下载后本地 sha256 与 `X-Backup-Sha256` 对账，不符退 2 并删除坏文件。
