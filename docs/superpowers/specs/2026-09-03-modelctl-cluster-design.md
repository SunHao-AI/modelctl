# modelctl Cluster 分布式管理面设计文档

> 日期: 2026-09-03 | 状态: 已评审（待实现）
> 目标: 在不改动单机推理行为的前提下，为 modelctl 增加一个跨局域网的分布式控制面。每台 GPU 服务器在同一个局域网（M 个 LAN）中以现有 webui 进程常驻，向中心节点（节点 A）注册形成集群，通过一个 Web UI + CLI 统一管理 N 台服务器的模型下发、启停、监控与审计聚合。

---

## 1. 需求分析

### 1.1 背景

modelctl 当前的能力是单机的：CLI / webui 管理本机上的模型 profile、引擎 venv、网关路由、用量统计与审计。它已有 `NODE_ID` / `NODE_HOST`（用于生成 nginx 多节点路由），但没有跨机集群能力。用户需求：把 N 台 GPU 服务器（分布在 M 个局域网，N ≥ M）纳入一个控制平面，通过 **一个 Web UI + 一套 CLI** 集中管理。

**关键决策（在头脑风暴阶段已确认）：**
1. **单中心控制平面**：机器 A 是唯一的控制中心，所有 worker 向它注册。
2. **跨网可达**：所有 server 对中心 A 都可路由，中心可直连所有 worker（无需代理层 / VPN 基建）。
3. **常驻 agent 形态**：每台 worker 跑现有 webui 进程（不部署额外组件），用 `cluster join` 风格登记到中心（PyTorch torchrun 风格的"启动命令 + 注册主节点"，但更简单——无 rank、无 rendezvous、worker 重启后无需中心重新广播）。
4. **声明式目标状态 + 自动同步**：中心定义"集群应跑什么"，worker 本机可执行、可重启，配置由中心下发。
5. **范围：仅管理面**。推理流量（数据面）仍走现有 nginx 路由直连 worker，不跨网拆分单个请求；"同一模型多 LAN 复制/负载均衡"留 P2。

### 1.2 目标用户

- 运维多 GPU 服务器 / 多 LAN 的工程师
- 希望把分散的推理节点纳入统一管理的团队
- 熟悉单机 modelctl 但需要批量下发同一 profile 到多机的人

### 1.3 功能需求

| 能力 | 说明 |
|---|---|
| 节点注册 / 下线 | worker 用 `modelctl cluster join` 一次性登记到中心；断线自动重连 |
| 声明式目标（goal） | 中心记录 `(node, profile)` 的目标状态；`goal set --all` 批量下发 |
| Profile 同步 | 中心把 profile YAML 下发到 worker 的 `models/`，原子写入 + sha 漂移检测 |
| 远程启停 / 重启 | 中心通过 WS 下发 `model.start/stop/restart`，worker 端 reconciler 执行 |
| 心跳与状态聚合 | worker 周期上报模型状态 + GPU/主机指标；中心聚合展示 |
| 事件 / 审计聚合 | 跨节点事件流与审计日志可在中心 dashboard / CLI 查看 |
| 集群视图 Web UI | 节点视图 + 跨机目标矩阵 + 事件流 + 集群设置 |
| 集群 CLI | `modelctl cluster ...` 子命令 + 现有命令加 `--cluster` 聚合模式 |

### 1.4 非功能需求

- **零破坏**：`CLUSTER_ROLE=solo`（默认）下行为与现状完全一致，现有部署零影响。
- **控制面故障不中断推理面**：中心宕机 / 断网时，worker 推理进程持续运行；只失去控制面。
- **幂等**：所有中心→worker 指令幂等，重放安全。
- **密钥安全**：API_KEY 等敏感项永不下发到 worker 明文 profile / env；只下发白名单 env 覆盖。
- **低依赖**：中心状态用单文件 SQLite（Python `sqlite3`），不引入额外中间件。

---

## 2. 总体架构与角色

**单中心、同构、单进程。** 同一个 `modelctl` 二进制，在 A 机上以"控制面实例"启动，在各 worker 上以"工作节点实例"启动。中心不跑推理流量（数据面直连 worker）。

```
                    ┌─────────────────────────────────────────┐
 浏览器 / CLI        │           机器 A（控制面）               │
 ────────────────── │  modelctl webui (cluster 角色)          │
                    │  ├─ 本地模型 / venv / 推理端口（可随意） │
                    │  ├─ /admin/api/...   （现有管理面）      │
                    │  ├─ /admin/cluster/* （节点/任务/状态）  │
                    │  ├─ /ws/cluster      （worker 通道）    │
                    │  └─ SQLite: nodes / goals / states /    │
                    │                  events / audit         │
                    └──────────────┬──────────────────────────┘
                                   │ WebSocket（每 worker 一条）
                    ┌──────────────┼──────────────────┐
                    │              │                  │
            ┌───────▼──────┐  ┌───▼────────┐   ┌─────▼────────┐
            │ worker 1     │  │ worker 2   │   │ worker N     │
            │ modelctl webui│  │ ...        │   │ ...          │
            │ 本地模型调度  │  │            │   │              │
            │ 推理端口 810x │  │            │   │              │
            └──────────────┘  └────────────┘   └──────────────┘
                                   ▲
        用户 / nginx ──────────────┘（数据面，/v1、端口直连，不经 A）
```

**角色（role）：** 节点可以是 `control-plane` / `worker` / `both`（A 自己也跑模型）。role 由 `.env` 的 `CLUSTER_ROLE` 决定。中心所有状态在 A 的本地 SQLite；worker 不把 goal/state 写进中心。中心节点也能 `modelctl start qwen3.8-vllm` 本地跑推理（`both` role），与集群管理并存。

**最关键的边界决策：** 集群协议**只管控制面**——profile 分发、模型启停、健康状态聚合、审计聚合、任务回传。**推理流量零流经 A**：nginx 规则照旧直连 worker 端口，A 的 Web UI 上"请求测试"走透传（与现有 webui 一致）。

---

## 3. 进程与角色模型（单一二进制）

一个 `modelctl` 进程同时承载三个面：数据面（网关 `/v1`）、管理面（`/admin/api`）、集群面（`/admin/cluster` + `/ws/cluster`）。三者共用同一端口（现有 webui 端口 4173），由 `.env` 的 `CLUSTER_ROLE` 决定是否挂集群路由。**worker 节点 = 不开集群面的 webui 进程**（`CLUSTER_ROLE=worker`），中心 = `both`。

### 3.1 CLUSTER_ROLE 矩阵

| CLUSTER_ROLE | 本地模型调度 | /admin/cluster/* | /ws/cluster | 节点台账 SQLite |
|---|---|---|---|---|
| `solo`（默认，向后兼容） | ✅ | ❌ 404 | ❌ | — |
| `worker` | ✅ | ❌ 404 | ❌（出站只发 WS） | ❌ |
| `control-plane` / `both` | ✅ | ✅ | ✅ | ✅ |

`solo` 不产生任何行为变化，现有部署零影响。

### 3.2 cluster 子命令（CLI）

```bash
# 中心（A）侧
modelctl cluster init                        # 初始化中心：生成 cluster token，建 SQLite
modelctl cluster status                      # 节点、模型目标状态汇总
modelctl cluster nodes                       # 列出 worker（连/断/最后心跳/目标 diff）
modelctl cluster goal list                   # 所有 (node, profile) 目标
modelctl cluster goal set <profile> --node <id> [--create]
modelctl cluster goal set qwen3.8-vllm --all --create   # 批量下发到所有 worker
modelctl cluster goal remove <profile> --node <id>
modelctl cluster sync --node <id>            # 手动触发一次 profile 全量同步（也可 --all）
modelctl cluster launch <profile> --node <id> [--create]   # = goal set + start
modelctl cluster stop <profile> --node <id>
modelctl cluster join-token --rotate         # 生成/轮换 worker 加入凭证
modelctl cluster events --node <id> --limit 100
modelctl cluster backup --to <path>
modelctl cluster restore --from <path>
```

### 3.3 节点侧（worker）

```bash
modelctl cluster join --center <A>:4173 --token <join-token> --node-id <id> --lan <lan-id>
#  作用：校验集群后把 CLUSTER_CENTER_URL / CLUSTER_NODE_ID / CLUSTER_LAN 写入本机 .env
#  worker 进程启动时（webui 起来后）后台建立到中心的 WebSocket，持续心跳
```

`join` 只写本地 `.env` 三行 + 触发 webui 重载，**不安装任何新的进程 / systemd 单元**——worker 仍跑现有 `modelctl webui start`（systemd）。即"N 台 server 单命令启动" = 现有 webui 启动命令 + 一次性 join。

### 3.4 角色判定

进程启动时读 `CLUSTER_ROLE`：
- `both` / `control-plane` → 挂中心路由 + 启动节点监听器 + 调度循环
- `worker` → 不挂集群路由，webui 起来后 spawn 一个 `WorkerAgent` 线程维持 WS

### 3.5 关键不变量

1. 中心故障不中断 worker 推理（worker 本地 loop 持续 reconcile 最后 goal，WS 断线只影响控制面）。
2. worker 离线时中心查看该节点显示 `stale`，但对其模型的本地起停不受影响。
3. `solo` 模式下 `cluster *` 子命令统一报 "cluster 未启用" 并提示 `modelctl cluster init`。

---

## 4. 注册与心跳协议（WebSocket，拉式 join）

worker 不做"中心反向 push"，**worker 主动连中心**（`/ws/cluster`），带上 join token 注册。中心被动接受。这保证任何 NAT/防火墙下 worker 都能起来（worker 出站已可达——保留出站可达优先的健壮性）。

### 4.1 WebSocket 协议（JSON over WS，一行一条）

握手（worker → center）：

```json
{"t":"hello","v":1,"node_id":"w-210","lan":"lan-2","key":"<join-token|node_token>","meta":{"host_ip":"192.168.77.210","engines":{"vllm":"0.9.1","sglang":null},"api_key_sha":"ab12cd...","hostname":"w210"}}
```

中心验证 key（hmac.compare_digest 对 join token 或已登记节点的 node_token）后回：

```json
{"t":"welcome","node_token":"<该节点专属 token>","interval_s":10,"push":{"profiles":["qwen3.8-vllm"],"started":[...],"stopped":[...]}}
```

之后双向长连，worker 周期发心跳（默认 10s），中心按需下行指令。

### 4.2 消息类型（t 字段）

| 方向 | t | 用途 |
|---|---|---|
| worker→center | `hello` | 注册（一次性） |
| center→worker | `welcome` | 注册成功 + 当前全量 goal 状态 |
| worker→center | `heartbeat` | 周期；携带 `{profiles: {name:{state,health,gpu:[..],port,updated_at}}, gpu:{used_mb,total_mb,fan,util}, host:{cpu_pct,mem_pct,load}}` |
| worker→center | `event` | 离散事件：模型 up/down、venv ready、probe 完成（用于审计聚合） |
| worker→center | `log_tail` | 响应 log-tail 请求时回 N 行 |
| center→worker | `goal.sync` | 下发 profile YAML 全量（`{name, yaml, sha256}`） |
| center→worker | `model.start` / `model.stop` / `model.restart` | 控制面起停 |
| center→worker | `status.query` / `log.tail` / `probe.run` | 主动查询 |
| center→worker | `token.rotate` | 节点 token 轮换 |

### 4.3 鉴权与凭据链

- 中心 `cluster init` 生成 `CLUSTER_JOIN_TOKEN`（一次性，仅用于新节点 join 校验）。
- worker join 成功后，中心为每个节点签发**节点专属 `node_token`**，后续该 worker 重连只用 node_token（不再需要 join token），可单独吊销某一节点。
- 节点 token 存中心 SQLite；忘记则 `cluster join-token --rotate-node <id>`。

### 4.4 断线 / 重连 / 防抖

- worker 本地 WorkerAgent 维护指数退避（1s→2s→…→30s）重连；每次重连重发 hello 用 node_token（幂等，中心回当前 goal，worker 据此 reconcile 本地）。
- 中心侧：worker 心跳超时（3×interval）标记节点 `stale`；9×interval 标记 `offline`；不删除记录。
- 同一 node_id 同时只允许一条 WS：新连接到来时踢旧连接（同一 worker 重启新进程）。
- 中心→worker 指令是**幂等**的：start 时若已 up 直接 ack；stop 时若已 down 直接 ack。

### 4.5 中心可靠性

- 中心进程崩溃：worker 不受影响（心跳收不到响应只是退避重连）；重开后 worker 自动 hello 回来，状态恢复。
- worker 推理被外部杀掉：webui 进程存活→下一次 heartbeat 上报 `state=down`，中心标记 stale model；webui 被杀→worker 节点 `stale/offline`，推理自然断（符合预期）。
- 中心记录所有节点最后已知 goal，重启后从 SQLite 加载，不丢失目标状态。

---

## 5. 声明式目标状态与 profile 同步

**Goal（目标）= 中心记录在 SQLite 里"应该在哪个节点跑哪个 profile"的声明**，worker 端的 `models/` 是同步产物 + 执行层。Goal 是唯一 source of truth。

### 5.1 Goal 记录结构

```
goals(
  id            TEXT PK          # 全局唯一，形如 "qwen3.8-vllm@w-210"
  node_id       TEXT             # 目标节点 worker 的 cluster node_id
  profile       TEXT             # 模型标识符 (name)
  engine        TEXT             # 引擎子目录，如 "vllm"
  profile_yaml  TEXT             # 下发的完整 profile YAML 原文
  profile_sha   TEXT             # sha256（漂移检测 + sync 幂等）
  intent        TEXT             # start | stop（默认 start）
  params        JSON             # 引擎参数覆盖（可选，如 gpu_list、tp）
  env_overlay   JSON             # .env 片段要覆盖到 worker 的字段（仅白名单 key）
  created_by    TEXT
  created_at    TEXT
)
```

`goal set qwen3.8-vllm --all` → 对每个在线 worker 生成一条 `(node_id, profile)` goal。

### 5.2 Worker 端数据结构（同步产物）

```
worker 机
├── models/                          # 现有目录，profile 原地
│   ├── vllm/
│   │   └── qwen3.8.yaml            # 由 sync 写入
│   │       └─ 头部注入注释：# managed-by: modelctl-cluster goal_id=qwen3.8-vllm@w-210
│   └── ...
├── data/cache/
│   ├── cluster-goals.json           # 缓存本节点所有 goal（含源 + sha256）
│   └── cluster-sync-marker.json     # 上次 sync 时间 + goal 集合 hash
```

### 5.3 同步语义

- **写文件原子**：`models/<engine>/<name>.yaml` 用临时文件 + rename 写入；写入前做一个 `.master` 备份（与现有下载回写一致）。
- **漂移检测**：worker 端 heartbeat 前扫一遍 `data/cache/cluster-goals.json` 列出的每个 profile 的当前 sha256，与 goal 的 sha 比较；不一致 → 上报 drift 事件（worker 本地被改过或被删）。中心 dashboard 标黄。
- **不覆盖**本地自定义：worker 上手动 `modelctl start` 一个新 profile（中心不知道）不算 drift；删掉了中心管理的 profile 才标 stale。
- **幂等**：sync 以 goal_id 为键，重复 sync 不重写未变内容（sha 相同则 skip）。

### 5.4 环境变量处理

worker 本地 `.env` 的敏感项（API_KEY、MODEL_ROOT）**不从中心下发**。profile YAML 里的 `${API_KEY}` 模板在 worker 本地解析（与现有 envfile 机制一致），密钥不随 sync 落明文。中心 dashboard 显示该 goal 对应 worker 是否已配置所需 env 项（worker 心跳带 `env_ok: {profile: bool}`）。

### 5.5 控制面 API（/admin/cluster/*）

```
GET    /admin/cluster/nodes               # 节点列表 + 状态
GET    /admin/cluster/nodes/{id}          # 单节点详情（含本节点 goals 列表）
POST   /admin/cluster/goals               # 创建 goal {profile, node_id|all, params, env_overlay}
GET    /admin/cluster/goals?node_id=&profile=
PUT    /admin/cluster/goals/{id}          # 更新 params
DELETE /admin/cluster/goals/{id}          # 删除 → 下发动作: 删本地 profile + 停模型
POST   /admin/cluster/nodes/{id}/sync     # 强制全量 sync
POST   /admin/cluster/nodes/{id}/model/{name}/start|stop|restart
GET    /admin/cluster/nodes/{id}/log?tail=200
GET    /admin/cluster/audit?from=&to=&node_id=   # 跨节点审计聚合
POST   /admin/cluster/join-tokens         # 轮换 join token
GET    /admin/cluster/export              # 全部 goal + 节点状态的 JSON 导出
```

所有 `/admin/cluster/*` 过现有 Bearer（API_KEY）鉴权，访问权限不变。

---

## 6. 启动流程与模型状态机

**原则：worker 自持 reconcile 循环，与中心解耦。** 中心只投递 intent（goal + start/stop 指令），worker 端常驻 reconciler 负责把本地状态逼向"最后已知"状态。中心断线 / 宕机，worker 推理不受影响。

### 6.1 Worker 端 reconciler（webui 进程内一个线程）

每 5s 一轮（也是 heartbeat 的触发源）：

```
loop:
  1. 读取本地 data/cache/cluster-goals.json（中心最后一次 goal.sync 写入）
  2. 对每个 (profile: intent):
       读本地 model state（process.is_pid_alive + health 探测）
       配方:
         - intent=start  且 state=unknown/down → 调用现有 start 入口
         - intent=stop   且 state=up           → 调用现有 stop
         - intent=start  且 state=up           → skip
       start/stop 的实际执行仍走原有 modelctl core（cli.py 内 _cmd_start），
       reconciler 只决定"去不去调"，不重写执行逻辑
  3. 比较"实际 state"与 goal 声明，记录 drift 标记
  4. 发起 heartbeat（含 profiles / gpu 聚合 / events）
  5. 若收到中心新指令:
       goal.sync            → 原地更新 models/<engine>/<name>.yaml（后续 re-reconcile）
       model.start/stop/restart → 调整本地 intent 表（不立即执行，下轮 loop 生效）
       status.query / probe.run → 立即执行
       log.tail             → 立即响应
  6. sleep(5)
```

关键点：reconciler 只调度"去调现有 start/stop"，所有重算力走现有引擎 adapter。**worker 的本地行为与单机模式完全一致**，集群模式是单机之上的调度层，不替换不破坏。

### 6.2 模型状态机（worker 端，与中心展示一致）

```
  unknown  ──(probe ok, pid alive)──▶  up
     │       ▲
     │stop   │(health ok after start)
     ▼       │
   down  ◀──(manual start ok)──  starting  ◀──(start command)──  unknown
     │        ▲
     │(start cmd)
     ▼
  stopping
```

- `unknown`：还没收到过 intent 或 probe 中
- `starting`：reconciler 已发 start，等 health 回来（走现有 wait_health，超时 300s）
- `up`：health OK，推理可用
- `stopping`：stop 发出，等 PID 消失
- `down`：已停止 / 健康检查失败
- `error`：start 失败（能力探测失败 / venv 缺失 / gpu_lock 抢占失败），附 failure reason

**错误不自动重试**：reconciler 看到 error → 停止对该 goal 的调度（避免无限重试刷爆日志）；下次 goal 变更（同步/手动 start）或节点重启时重新尝试。中心 dashboard 上 `error` 节点标红 + 提供"重试"按钮（本质是触发一次 start 指令）。

### 6.3 与现有本地操作兼容

- worker 上手工 `modelctl start`（不经中心）：reconciler 探测到 state=up、intent=stop → 会把它停下（reconcile 到 goal）。有意行为。用户想让 worker 自治：`cluster goal remove` 后再本地 start，reconciler 不再管理该 profile。
- 中心 stop 一个本地没启动的模型：worker 收到 stop 指令但 state=down，直接 ack skip（幂等）。
- goal=start 但 worker 缺 venv：现有 start 报错，reconciler 标 error，dashboard 提示"需要先 `modelctl env setup <engine>`"。

### 6.4 跨 LAN 同一请求

本设计（仅管理面）**不做跨网复制调度**。`goal set --all` 只是"在所有 worker 上都跑"，请求仍由 nginx 直连某一台（用户/客户端通过 nginx 路由到具体 node-id 决定）。后续如需复制/负载均衡，在 nginx 层按 node-id 做 upstream 即可，数据面零改动。

### 6.5 中心 dashboard / CLI 的模型视图

每个 `(node, profile)` 一行：

```
node       profile           intent  state     gpu        port   age      events
w-210      qwen3.8-vllm      start   up        [0,1,2,3]  8101   2d3h     -
w-210      qwen3.8-llamacpp  stop    down      -          -      -        -
w-211      deepseek-v4-flash start   starting  [0,1]      18888  45s      venv ready
w-212      kimi-k2.5-sglang  start   error     -          -      12m      gpu_lock: 已被占用
```

---

## 7. 故障处理与恢复

**核心立场：控制面故障永远不中断推理面。** 推理面跑在 worker 本地 webui + 引擎进程里，是长驻进程；集群是 scheduler/监控层，不是数据路径。

### 7.1 故障场景与恢复矩阵

| 故障 | 现象 | 恢复路径 | 数据面影响 |
|---|---|---|---|
| worker 模型 OOM/崩溃 | 引擎进程死，webui 活着 | reconciler 探测 state=down、intent=start → **不自动重启**（避免 OOM loop），标 error + 事件上报；中心 dashboard 标红 + 手动"重试" | 该模型不可用，其他模型不受影响 |
| worker webui 进程死 | 引擎进程可能还在（detached）；WS 断 | 节点心跳超时 → 中心标 `stale`（3×interval）→ `offline`（9×interval）；webui 由 systemd 重启后重新 join → 中心收 hello → 状态恢复，reconciler 对仍 running 的引擎进程做 "adopt"（只读 pid，不重复 start） | 无（引擎 detached） |
| worker 整台机器宕机 | 节点 offline | 中心标 offline；单机故障不影响其他节点；用户按 nginx 路由感知单节点 404 | 该 LAN 内切流到其它 LAN 需用户改 nginx |
| 中心进程宕机 | 所有 WS 断；所有 worker stale→offline | 中心 systemd 自动拉起；worker 指数退避重连 → hello → 中心从 SQLite 恢复 goal/节点 → 全量重放；worker reconciler 期间按本地最后 goal 继续 reconcile | **零**（worker 独立） |
| 中心 SQLite 损坏/删除 | 节点列表 + goal 全丢 | 用备份恢复；或 `cluster init` 重建 + 所有 worker 重新 join（重新 sync）。worker 端 `cluster-goals.json` 还在，推理持续 | **零** |
| join token 泄露/实体攻击 | 任意公网 worker 可注入 | `cluster join-token --rotate`（一次性换发）；节点专属 token 可单独 `--rotate-node <id>` 吊销；中心可标 `node.disabled=true` 拒绝对节点所有指令 | **零** |
| 跨网到某 LAN 不通 | 节点 stale | 同"中心宕机"路径；该 LAN 推理仍可用；中心 dashboard 标红。网络恢复后自动重连 | 该 LAN 推理可用；中心失去该 LAN 控制 |
| LAN 网络抖动 | 心跳丢包 | WS 自带 TCP 重传；worker 指数退避重连；3×interval 阈值吸收抖动 | 无 |

### 7.2 一致性保证（幂等 + 最后 goal 语义）

- **所有 center→worker 指令幂等**（start 已 up → ack；stop 已 down → ack；sync 同 sha → skip），重放安全。
- **worker reconciler 以本地 intent 表为唯一调度源**，intent 表从中心 goal.sync 或 model.start/stop 指令更新，worker 崩溃重启时从 `cluster-goals.json` 重建，本地不依赖中心在线。
- **heartbeat 带"events 队列"**：worker 本地事件先入队（内存环形 1000），WS 重连后先 flush 未确认 events 再恢复周期心跳，避免事件丢失。
- **审计日志独立**：每 worker 本地 audit（现有 `AUDIT_DIR`），中心 dashboard 聚合视图只读 worker 持久化日志（`modelctl audit` 远程调用），中心宕机不丢审计。

### 7.3 明确不做（MVP 范围外）

- 不做自动重启策略（防止 OOM loop）、不做 LAN 自动切流（nginx 层的事）、不做中心主备（MVP 内 1 中心足够，P2 再议）。
- 做：dashboard 红色节点 + 事件流；`cluster status` CLI 持续输出。

### 7.4 测试

- 单元测试：state machine 转移、幂等指令、reconcile 配方表、token 校验。
- 集成测试（fake center / fake worker 在进程内）：节点注册/心跳/断线/重连/重新 join 后状态恢复。
- 故障注入测试：`SIGKILL` worker webui、删中心 SQLite、断网 90s 后 reconcile，断言推理不中断。

---

## 8. Web UI 与 CLI 用户体验

**原则：复用现有 Vue 前端结构（Layout/Sidebar/route 模块），新增 2 个路由面板 + 1 个 API 模块 + 现有 Dashboard 增加"节点"列。** 不新做工程，不改 Vite 配置。

### 8.1 路由变更（web/src/router/index.ts）

```
/admin              Dashboard（现有；表格加 1 列 "node"）
/models             模型列表（现有；加 "节点" 筛选 + "节点" 显示列）
/cluster/nodes      新增  ← 集群节点视图
/cluster/goals      新增  ← 跨机目标状态矩阵 + goal 管理
/system             现有 Settings（新增 Join Token 块）
```

### 8.2 新增导航项（Sidebar.vue）

"集群" 下挂 `节点` 与 `目标` 两个子项；solo 角色下隐藏整组（根据 `/admin/cluster/status` 是否 404 判定）。

### 8.3 ClusterNodesView.vue

```
┌─ 节点列表（表格）─────────────────────────────────────────────┐
│ 节点    LAN     角色   状态    心跳   已建连   GPU 占用    模型数 │
│ w-210   lan-2   worker online  3s     2d3h     384/384G    4    │
│ w-211   lan-2   worker online  4s     1h2m     48/384G     1    │
│ w-212   lan-5   worker stale   45s    -        -           2    │
└──────────────────────────────────────────────────────────────┘
  点进去 → /cluster/nodes/:id → 详情面板:
  ・基础信息: host_ip / hostname / engine 版本表 / API_KEY 可用性
  ・GPU: 每卡 used_mb/total_mb/fan%/util%/温度
  ・模型矩阵: profile / intent / state / gpu / port / age
    → 每个模型可 [启动]/[停止]/[跟踪日志]
  ・事件流: 最近 100 条 event（SseLogViewer 复用）
  ・"重新 sync" / "禁用节点" / "轮换该节点 token" 按钮
```

### 8.4 ClusterGoalsView.vue

```
┌─ 目标状态矩阵 ──────────────────────────────────────────────┐
│ 节点  ┬ qwen3.8-vllm  ┬ deepseek-v4-flash ┬ kimi-k2.5-sglang │
├───────┼───────────────┼──────────────────┼──────────────────
│ w-210 │ ● up [0-3]    │ ○ down           │ ○ down
        │ intent=start  │ intent=stop      │ intent=start
│ w-211 │ ● up [0-1]    │ ◐ starting [0-1] │ -（未声明）
│ w-212 │ -             │ ● up [0-3]       │ ✕ error: gpu_lock
└──────────────────────────────────────────────────────────────┘
  ✕ 点击弹事件详情。
```

顶部工具栏：
- "批量下发"：选 profile → 选 `--all`/指定节点 → (可选 env_overlay JSON) → 提交
- "全集群清单 JSON 导出" 按钮（`GET /admin/cluster/export`）
- "目标不一致"报告：列出"声明了 goal 但 worker 上 profile 文件被改过/被删"的 (node, profile)

### 8.5 Dashboard 增量（现有页）

`modelctl list` 风格表格加 1 列 `node`：所在节点的 cluster node_id（solo 下显示 `-`）；筛选器 `node`。

### 8.6 SettingsView 增量（新增"集群"块）

```
[node_id]  [CLUSTER_ROLE ▾ (solo/worker/both)]
[CLUSTER_CENTER_URL]  [CLUSTER_LAN]
[Join Token 显示(脱敏)]  [轮换] [节点 Token 轮换表]
```

写 `PUT /admin/config`（扩展现有 admin_config API，加 cluster 字段）。

### 8.7 CLI UX

- 所有 `modelctl cluster *` 子命令独立（见 3.2）。
- **加 `--cluster` 全局 flag**：让现有 `modelctl status/list/probe` 自动走中心聚合视图（不传则看本机；传则拼合 `/admin/cluster/nodes` 多节点视图）。保留老 CLI 习惯。
- 终端彩色复用现有 `colors.py`：online 绿、stale 黄、offline 红；表格对齐复用 CJK 对齐工具。

### 8.8 组件复用清单

| 复用 | 用途 |
|---|---|
| `SseLogViewer.vue` | 节点事件流 / log.tail 实时看 |
| `TaskButton.vue` | 批量下发起停 |
| `ConfirmDialog.vue` | 删 goal / 禁用节点 / 取消 Token |
| `StatusBadge.vue` | 扩展颜色集（online/stale/offline/error） |
| `stores/auth` + `client.ts` | 扩展 `cluster.ts` API 模块（`/admin/cluster/*`） |

### 8.9 部署示例

```bash
# 一次性部署 4 台机
ssh w210 "cd modelctl && modelctl cluster join --center a210:4173 --token $JT --node-id w-210 --lan lan-2"
ssh w211 "...node-id w-211..."
ssh w212 "...node-id w-212 --lan lan-5"
# 中心侧：
modelctl cluster goal set qwen3.8-vllm --all --create
modelctl cluster goal set deepseek-v4-flash --node w-210 --create
modelctl cluster status -f
```

验收：浏览器开 `https://a210:4173/cluster/nodes`，能看到 4 台机器、模型矩阵、每个节点 GPU 实时数据。

---

## 9. 安全与鉴权

### 9.1 威胁模型（按可信层级）

- **可信**：中心 A 机进程 + 持有 `node_token` 已合法 join 的 worker webui 进程。
- **半可信**：持有 `API_KEY`（webui admin token）的用户/进程——等同管理员，可读写所有 goal，与今天 webui 行为一致。
- **不可信**：网络上其他机器、持有 `join_token` 但未 join 的进程、过期/被 revoke 的 token。

目标：只靠"一个正确的 WS 连接"无法做"中心管理面"以外的事情。MVP 不引入 mTLS（所有 server 单向可达 A，后续 P2 再议）。

### 9.2 凭证链

| 名称 | 生成时机 | 寿命 | 用途 |
|---|---|---|---|
| `API_KEY`（既有） | .env 已有 | 持久 | 现有 webui 管理面所有 Bearer；不动 |
| `CLUSTER_JOIN_TOKEN` | `cluster init` 生成 | 持久，可 rotate | 未 join 的 worker 首次 hello 校验；rotate 后旧值失效 |
| `node_token`（每节点） | join 成功分配 | 持久，单节点可 rotate | 该节点重连时鉴权；独立吊销（不动其他节点） |
| `WS session_id` | 每条连接握手成功 | 单连接 | 仅用于 audit/trace，不作权限 |

**规则：**
1. Bearer（API_KEY）仅用于建立信任的初始阶段；**worker 后续重连只用 node_token**（避免网络广播 API_KEY）。
2. `node_token` 在 SQLite 单节点记录；`cluster join-token --rotate-node <id>` 立即使旧 token 失效，该节点重连需 join_token 重新 hello。
3. `join_token` rotate 后所有未 join 请求拒绝；已 join 节点不受影响（他们有 node_token）。
4. 中心 dashboard 显示各 node token 的 last_seen。

### 9.3 传输层

- WS 默认 **HTTPS/WSS**（center 侧）；仅内网可设 `CLUSTER_WS_INSECURE=1` 走裸 WS（MVP 可行，稳定后升级 WSS）。
- WS 消息体序列化前做 `node_id + seq` 去重 + `nonce` 防重放（MVP 保 `node_id + seq` 即可）。
- 敏感字段（API_KEY、node_token、join_token）不出现在审计 log；center log 只出现 node_id。

### 9.4 授权粒度

MVP 不做 RBAC，只分 2 类：
- **operator（持有 API_KEY）**：可执行所有 `/admin/cluster/*`，可 rotate token / disable 节点 / 删 SQLite 备份。
- **worker（持有 node_token）**：只可通过 WS 与 center 通信；不能直发 HTTP（节点对 center 只有 WS 一条通道）。

更细粒度（谁能启动哪台节点/哪个 model）留 P2。

### 9.5 不入明文的敏感项

- **API_KEY / MODELSCOPE_TOKEN** 等永不下发到 worker profile YAML。profile 里 `${API_KEY}` 占位符由 worker 本地 envfile 解析。
- `env_overlay`（goal 里的 env 覆盖）**只允许覆盖白名单 key**：`MODEL_ROOT`、`MODELSCOPE_CACHE`、`HF_HOME`、`OLLAMA_MODELS`、`LOG_DIR`、`AUDIT_DIR`、`MODELCTL_GPUS`。禁止写入 `API_KEY`/`*_API_KEY`（防止把密钥当配置下发到明文文件）。WebUI 表单直接禁用这些 key 的输入。
- worker 本地 sync 产物（models/、cluster-goals.json）**不入 git**，.gitignore 加 `data/cache/cluster-*` + 用文件头注释 `# managed-by: modelctl-cluster` 标记，不单独建目录。

### 9.6 中心自身的安全边界

- 中心 SQLite 在 `data/cache/cluster-meta.db`（`.gitignore`），权限 0600。
- 中心要求 `WEBUI_HOST=0.0.0.0` 才能从 LAN 访问——由用户手动改，不进默认配置。若 `WEBUI_HOST=127.0.0.1` 则 LAN 其他机器无法接入（与现有 webui 默认隔离一致）。提醒：集群模式必须显式改成 0.0.0.0 + 防火墙暴露 4173。
- 中心 SQLite 备份：`modelctl cluster backup --to file:///path`（MVP 支持 file，对象存储留 P2）。

### 9.7 审计

- 集群面关键操作进本地审计（与现有 AUDIT_DIR 同款 loguru JSONL）：
  - `cluster.init` / `cluster.role_change`
  - `node.join` / `node.rejoin` / `node.disable` / `node.enable`
  - `token.rotate`（join / 单节点）
  - `goal.create` / `goal.update` / `goal.delete`
  - `model.start` / `model.stop` / `model.restart`
  - `node.sync`
- 审计条目带 `operator=<API_KEY 指纹>`、`node_id`、`timestamp`、`goal_id`（若有）。
- worker 本地 audit 不 mix，worker 自己的 audit 照旧。中心 dashboard "审计聚合" 实际是遍历 online 节点调 `log.tail`/`stats query`，中心不落全量事件（MVP 内如此，P2 可加 `events.stream` 全量上报）。

---

## 10. 数据模型（SQLite schema）

中心所有持久化状态在 `data/cache/cluster-meta.db`（`.gitignore`，0600）。用 Python 原生 `sqlite3`（轻量、无硬依赖），单文件，无额外服务。

### 10.1 表结构（DDL）

```sql
-- 节点注册台账。join 成功后插入，永不删除（offline 仅标状态）
CREATE TABLE IF NOT EXISTS nodes (
  node_id     TEXT PRIMARY KEY,
  node_token  TEXT NOT NULL,
  lan_id      TEXT,
  role        TEXT NOT NULL DEFAULT 'worker',
  host_ip     TEXT,
  hostname    TEXT,
  engines     JSON,
  created_at  TEXT,
  last_seen   TEXT,
  status      TEXT NOT NULL DEFAULT 'offline',   -- online|stale|offline|disabled
  disabled    INTEGER NOT NULL DEFAULT 0
);

-- 声明式目标。goal 的 source of truth
CREATE TABLE IF NOT EXISTS goals (
  goal_id      TEXT PRIMARY KEY,      -- "<profile>@@<node_id>"
  node_id      TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
  profile      TEXT NOT NULL,
  engine       TEXT NOT NULL,
  profile_yaml TEXT NOT NULL,
  profile_sha  TEXT NOT NULL,
  intent       TEXT NOT NULL DEFAULT 'start',
  params       JSON,
  env_overlay  JSON,
  created_by   TEXT,
  created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_goals_node ON goals(node_id);
CREATE INDEX IF NOT EXISTS idx_goals_profile ON goals(profile);

-- 模型状态快照。每次 heartbeat 全量覆盖该 (node, profile)
CREATE TABLE IF NOT EXISTS model_states (
  node_id  TEXT NOT NULL,
  profile  TEXT NOT NULL,
  state    TEXT NOT NULL,
  gpu      TEXT,
  port     INTEGER,
  pid      INTEGER,
  reason   TEXT,
  updated_at TEXT,
  PRIMARY KEY (node_id, profile)
);

-- 事件流（retention 约束总量）
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  node_id    TEXT,
  kind       TEXT NOT NULL,
  payload    JSON
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_node ON events(node_id, ts);

-- 凭证轮换审计（不含明文 token）
CREATE TABLE IF NOT EXISTS token_ops (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT,
  op         TEXT,
  node_id    TEXT,
  operator   TEXT
);

-- 集群面操作审计（中心独立存一份供 backup）
CREATE TABLE IF NOT EXISTS audit (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT,
  operator   TEXT,
  action     TEXT NOT NULL,
  detail     JSON
);
```

### 10.2 关键约定

- **`nodes` 不删**：节点下线只更 `status=offline/disabled`。goal 的 node 引用 FK ON DELETE CASCADE 避免悬空。
- **`model_states` 仅覆盖**：heartbeat 只 update 已存在 key 的行；新 key 是 insert。worker offline 超 9×interval 后 dashboard 不读旧 model_states（展示 `offline`）。
- **retention**：`events` 保留 `CLUSTER_EVENT_RETENTION_DAYS`（默认 30）；应用启动时 `DELETE FROM events WHERE ts < ?`；`audit` 默认 90 天。
- **不存明文 token**：`node_token` 存明文（短），中心走 hmac 比对。未来升级 mTLS 可改 radix 指纹方案。
- **单文件备份** = `modelctl cluster backup`（sqlite3 Online Backup，不锁读不写）。默认 to `data/cache/cluster-meta.db.<utc-stamp>`。

### 10.3 中心 DB 损坏恢复

- 备份：`modelctl cluster backup --to /backup/` + 手工 `git push`。
- 恢复：`modelctl cluster restore --from /backup/xxx.db`（原子 rename：备份旧 DB → 复制恢复 → 重启 webui）。
- 最坏情况（备份全损）：`cluster init` 重建 + 每个 worker `cluster join` 重新下发；推理进程仍在跑（detached），`cluster status` 能看到，对运行中的引擎做 "adopt"（只查 pid/health 不重启）。

### 10.4 与现有 webui 本地状态的隔离

- 现有单机逻辑继续用本地文件（`data/cache/*.pid`、`*.lock`）。`cluster-meta.db` **只属于 center**。
- worker 上不出现在 `cluster-meta.db`；worker 节点只有 `.env` 的 CLUSTER_* key + `data/cache/cluster-goals.json` + `data/cache/cluster-reconcile.json`（intent 表 + 上次 sync 时间）。

### 10.5 API/CLI 映射

| 表 | CLI | HTTP |
|---|---|---|
| nodes | `cluster nodes`、`cluster join-token ...` | `GET /admin/cluster/nodes*`、`POST /admin/cluster/join-tokens` |
| goals | `cluster goal *` | `GET/POST/PUT/DELETE /admin/cluster/goals*` |
| model_states | `cluster status` | 嵌入 `GET /admin/cluster/nodes/{id}` |
| events / audit | `cluster events?node=&limit=` | `GET /admin/cluster/events*` |
| token_ops | （无） | （无，只内部写） |

---

## 11. 分阶段交付（MVP → P2，供实现计划切分参考）

> 本节为交付边界说明，不是功能承诺；落地计划由 writing-plans 细化。

- **M0（基础）**：CLUSTER_ROLE 矩阵 + `cluster init/join` + WS 协议（hello/heartbeat）+ nodes/model_states 表 + 中心 dashboard 复用、`cluster status/nodes` CLI。**验收**：A 机看到 1 个 worker online，心跳数据正确，solo 模式零影响。
- **M1（目标状态）**：goals 表 + `goal set/sync` + profile 同步（原子写 + drift）+ 远程启停 + intent reconciler + `cluster launch/stop`。**验收**：`goal set qwen3.8-vllm --node w-210 --create` 后 w-210 上模型健康就绪。
- **M2（聚类视图）**：ClusterNodesView / ClusterGoalsView vue + 事件流 + `--cluster` 聚合 flag + 审计聚合 + token 轮换 + backup/restore。
- **P2（MVP 外）**：跨网复制/负载均衡（nginx upstream）、中心主备、mTLS、RBAC 细粒度、对象存储备份、`events.stream` 全量上报。

---

## 12. 风险与权衡

| 风险 | 缓解 |
|---|---|
| 中心单点故障 | MVP 接受单中心（控制面故障不伤推理面）；P2 主备 |
| WS 长连在 LAN 间抖动 | 指数退避重连 + 3×interval 阈值 + events 离线缓冲 |
| worker 本地 model 被手改后意图混淆 | drift 检测 + dashboard 标黄 + `cluster sync` 可一键强制覆写 |
| 密钥泄露面 | 只 node_token 上 WS，API_KEY 永不下发；join_token 一次一换 |
| SQLite 不可用于高并发 | MVP 控制面 QPS 低（心跳 10s/节点 + 事件），单文件足够 |
| Vue 新增路由破坏现有布局 | 复用 Layout/Sidebar/现有组件，不引入新依赖 |

---

## 13. 验收标准（DoD）

- [ ] `CLUSTER_ROLE=solo` 的现有用户运行 `modelctl` 全部既有命令，行为与改动前完全一致（回归测试通过）。
- [ ] A 机 + 2 个 worker（任意 LAN）跑通 `init` → `join` → `goal set --all --create` → 模型健康就绪 → `cluster status` 全绿。
- [ ] kill -9 中心 webui：所有 worker 推理不中断；中心重启后 worker 自动 hello 恢复，goal 状态不丢。
- [ ] kill -9 worker webui：该模型若在跑，引擎 detached 仍服务；webui 重启后 adopt 现有进程，不重复 start。
- [ ] `goal set` 一个缺 venv 的 worker：该 (node, profile) 标 error，其他节点不受影响，dashboard 可一键重试。
- [ ] `token.rotate` 后旧 worker 重连被拒；`--rotate-node` 只吊销指定节点。
- [ ] 中心 SQLite 删除后 `cluster restore` 从备份恢复，节点/goal 全量回滚。
- [ ] webui `/cluster/nodes`、`/cluster/goals` 在 solo 模式下正确隐藏；center 模式下展示全量数据。
