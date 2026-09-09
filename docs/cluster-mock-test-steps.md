# modelctl 集群 · 单机 mock 两节点功能测试手册

> 目标：在本机（`D:\WorkPlace\Pycharm\modelctl`，`127.0.0.1`）**同时模拟 center + worker 两个 modelctl 节点**，跑通 M0–M2 全链路（节点注册/心跳、goal 下发、profile 同步、模型远程启停/停止/重试、故障注入、节点退役）。
>
> 哪些用代码（自动）验证，哪些需要人工肉眼验证，每步都标了。

## 0. 架构与端口表

| 项 | center A（本机开发目录） | worker B（mock 副本） |
| --- | --- | --- |
| 源码目录 | `D:\WorkPlace\Pycharm\modelctl` | `D:\WorkPlace\Pycharm\modelctl-mock-worker` |
| `PROJECT_ROOT` | （由 `envfile.PROJECT_ROOT` 推导，不可覆盖） | 同左推导，独立 |
| `data/` | `<src>/data`（已有） | `<dst>/data`（新） |
| `CLUSTER_ROLE` | `both` | `worker` |
| `CLUSTER_NODE_ID` | 自带（center 无需注册 self；不下发 goal） | `w-mock-01` |
| `CLUSTER_LAN` | `lan-a` | `lan-a` |
| webui | `127.0.0.1:4173` | `127.0.0.1:4183` |
| 网关 | `127.0.0.1:5003`（默认，本轮不跑） | `127.0.0.1:5005` |
| 模型端口 | `18897`（llamacpp）/ `8108`（vllm；本轮不跑） | `18911`（vllm，yaml 已 patch）；llmacpp 副本 `18910` 备用 |
| 引擎 | — | vllm `qwen2.5-0.5b`（6GB 显存 `gpu_memory_utilization=0.62`） |
| llamacpp 副本 | — | 同步打了 port patch，如需切换可走 |

> **为什么必须两份源码目录**：`envfile.PROJECT_ROOT = Path(__file__).resolve().parents[3]` 由
> `python -c "import modelctl"` 的实际安装路径推导，**不能**用 `MODELCTL_PROJECT_ROOT`
> 环境变量覆盖；同一份 `src/` 装在两个 venv 中会指向同一个 `PROJECT_ROOT`，
> 导致 `data/cache/cluster-meta.db`（SQLite 台账）和 `data/cache/pid` 文件
> **被两个 webui 进程读写同一份**，测试作废。

## 1. 一键准备（人工一次；幂等）

```powershell
cd D:\WorkPlace\Pycharm\modelctl
powershell -ExecutionPolicy Bypass -File docs\cluster-mock\worker_copy.ps1
```

产物核对：

```powershell
dir D:\WorkPlace\Pycharm\modelctl-mock-worker\.env
dir D:\WorkPlace\Pycharm\modelctl-mock-worker\models\\llamacpp\qwen2.5-0.5b.yaml
dir D:\WorkPlace\Pycharm\modelctl-mock-worker\models\vllm\qwen2.5-0.5b.yaml
dir D:\WorkPlace\Pycharm\modelctl-mock-worker\venv\Scripts\python.exe
```

**人工验证**：`Get-Content D:\WorkPlace\Pycharm\modelctl-mock-worker\models\vllm\qwen2.5-0.5b.yaml | Select-String "port:|gpu_memory_utilization:"` 应见 `port: 18911` 与 `gpu_memory_utilization: 0.62`。

**代码验证**：

```powershell
D:\WorkPlace\Pycharm\modelctl-mock-worker\venv\Scripts\python.exe -c "import modelctl,sys; print(modelctl.__file__)"
```

应输出 `D:\WorkPlace\Pycharm\modelctl-mock-worker\src\modelctl\__init__.py`，**不是** center 的路径。

## 2. T0 — 前置：环境自检（代码）

```powershell
# 两个 venv 都用各自 venv 里的 modelctl，互不串
D:\WorkPlace\Pycharm\modelctl\.venv\Scripts\python.exe -m modelctl --help | Select-Object -First 3
D:\WorkPlace\Pycharm\modelctl-mock-worker\venv\Scripts\python.exe -m modelctl --help | Select-Object -First 3
```

**验证点（三行都"OK"）**：
1. 两个 python 都能 `import modelctl`；
2. `modelctl --version` 一致；
3. 检查 **不**冲突端口：

```powershell
# 期望：无 system.process 监听 4173 / 4183 / 18910 / 18911
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 4173, 4183, 18910, 18911 } | Format-Table -AutoSize
```

> 如果本机刚才还在跑旧 webui：`modelctl webui stop` 或 `Stop-Process -Name python -Force` 后再开新 webui。

## 3. T1 — center 启用控制面（人工 + 代码）

```powershell
cd D:\WorkPlace\Pycharm\modelctl
. venv\Scripts\Activate.ps1   # 可选：沿 script 路径用绝对路径也行
modelctl webui start
modelctl cluster init
```

`cluster init` 应输出 `join token: <TOKEN_A>`（如已存在则复用）。记下这个 token。

**代码验证**：

```powershell
modelctl cluster join-token   # 仅中心本机可直读台账；应输出同一 token
modelctl cluster status       # 应见 role=both，nodes=0
```

**人工验证（任一）**：
- 浏览器 `http://localhost:4173/`，登录后选"节点"页，看到 `both`/center 自身条目的状态 + 机载模型列表（若 center 自跑）。
- 或 webshell：`curl http://127.0.0.1:4173/admin/api/cluster/nodes` 应返回 200；否则：`401`（key 不对）、`403`（CLUSTER_ROLE 不是 both）、`404`（webui 没起）。

## 4. T2 — worker 加入（代码）

```powershell
cd D:\WorkPlace\Pycharm\modelctl-mock-worker
. venv\Scripts\Activate.ps1   # 可选
modelctl cluster join --center http://127.0.0.1:4173 --token <TOKEN_A> --node-id w-mock-01 --lan lan-a
```

**人工验证**：
- 输出应为：预检通过 → 已写入 `CLUSTER_CENTER_URL`/`CLUSTER_NODE_ID`/`CLUSTER_LAN` 到 `.env`，中心签发 `CLUSTER_NODE_TOKEN`（响应里一次性回显，自己截屏）。
- 若报 `token mismatch`：说明 center `cluster join-token --rotate` 过——重新看 token。

**代码验证**：worker webui 启动一次。

```powershell
modelctl webui start            # 4183 起
curl.exe -s http://127.0.0.1:4183/admin/api/health     # 200 {"ok": true, "version": "2.8.1"}
```

## 5. T3 — 节点在线检查（代码，自动）

```powershell
# 切回 center 终端
modelctl cluster nodes
```

**期望输出**：表头 + 一行；`node_id` 列 = `w-mock-01`，`状态` 列 = **online**（三叶草也来自 REST 中心已格式化、CLI 不二次加工），`port`=18910，`since_seen_s` 与 `lease_left_s` 数值合理（默认 `CLUSTER_HEARTBEAT_INTERVAL_S=10` / `LEASE_S=90`：seent_s≈0~10、lease_left≈90）。

**代码断言**（一行 PowerShell 锁定）：

```powershell
$r = (modelctl cluster nodes --json 2>$null) | ConvertFrom-Json
$mine = $r.nodes | Where-Object { $_.node_id -eq "w-mock-01" }
if (-not $mine -or $mine.status -ne "online") { Write-Error "T3 FAILED" } else { "T3 OK" }
```

**人工验证**：浏览器 `http://localhost:4173/` → "节点" 页，`w-mock-01` 卡片 online，`hostname=127.0.0.1`。注意：`-i "online"` 也在 `modelctl cluster nodes` 输出里能抓到同一信号（不是 RPC name 不同——前端 `cluster.ts` 的 `listNodesValid`/`getProbe`/`listGoal` 与 CLI 走同一 REST）。

## 6. T4 — goal 下发（代码）

```powershell
# center 侧
modelctl cluster goal set qwen2.5-0.5b-vllm --node w-mock-01 --create
modelctl cluster goal list --node w-mock-01
```

**期望**：
1. 第一次 `set` 创建 goal，gate 报告行：候选 `w-mock-01` → 通过。
2. `list` 里看到 stage 在 **`PROFILE_SYNCED`** 或更早（`PENDING_PROFILE_SYNC` 刚诞生），reason 为空。

> stage 推不推进由 worker 端 reconciler 一拍一拍来（设计文档 §A2）；`goal list` 只是看时刻，所以 stage 可能出现"还在推进"的中间态（PROFILE_SYNCED→RUNTIME_OK 等 worker 下载模型/镜像/启动容器）。

> 如果显示 `REJECTED` 或 stage 卡 `PENDING_PROFILE_SYNC`：worker 没起 webui 或 WS 通道没通；回 T2 检查 `curl /admin/api/health`。

## 7. T5 — 模型实际拉起（代码 + 人工等模型）

持续刷 stage 到 `READY`：

```powershell
# 每 5~10s 看一次，直到 stage=READY
modelctl cluster goal list --node w-mock-01
```

stage 顺序应该依次经过：
`PENDING_PROFILE_SYNC → PROFILE_SYNCED → RUNTIME_OK → STARTING → READY`

如果直接跳到 `FAILED`：
- 看 `reason` 列（如 `abandoned by previous launch` / `no healthy candidates` 等）；
- 再看 `modelctl cluster events --node w-mock-01 --limit 30` 拿事件流；
- 或 `modelctl cluster logs --node w-mock-01 <profile> --tail 200`（设计文档 §4.4：拉 worker 端 `_launch_stream`，docker 容器日志也在 WS 里回传，不必 SSH）；
- 或 直接看 worker 的 `data\logs\launch-*.log` / `docker logs <container>`。

**代码断言**：

```powershell
$modelIsUp = curl.exe -s http://127.0.0.1:18911/health
if ($modelIsUp -notmatch '"status":\s*"ok"') { Write-Error "T5 model not healthy yet" } else { "T5 model healthy" }
```

**人工验证**：
- 浏览器 `http://localhost:4173/` → "模型" 页，若 center 配置了网关，应见 `qwen2.5-0.5b` 一行（在 worker w-mock-01:18910 上跑）；
- 不归入 GET /v1（本轮 gateway 不跑），只看 webshell 管理面。

## 8. T6 — 远程启停（代码）

### T6a. 停

```powershell
modelctl cluster stop qwen2.5-0.5b-vllm --node w-mock-01
modelctl cluster goal list --node w-mock-01   # intent=stop, stage 应变 TO_DETERMINE 或停后回 RUNNING-FREE
```

模型 `18911/health` 应 500/超 时。

### T6b. 启

```powershell
modelctl cluster launch qwen2.5-0.5b-vllm --node w-mock-01
modelctl cluster goal list --node w-mock-01   # intent 回到 start, stage 推进
```

### T6c. 删（断托管，撤文件 + 停模型 + 删台账）

```powershell
modelctl cluster goal remove qwen2.5-0.5b-vllm --node w-mock-01
modelctl cluster goal list --node w-mock-01   # 应空
```

`worker` 侧 `models/vllm/qwen2.5-0.5b.yaml` 应被 reconciler 剪掉（reconciler 在 goal 删的下拍 prune 中心授权的 profile 文件）。

> 验证文件删除：**worker 侧**看 `D:\WorkPlace\Pycharm\modelctl-mock-worker\models\vllm\qwen2.5-0.5b.yaml`。

## 9. T7 — 故障注入：worker webui 掉线 → 中心判 stale → offline

### T7a. 杀 worker webui

```powershell
# worker 侧（终端 2）
D:\WorkPlace\Pycharm\modelctl-mock-worker\venv\Scripts\modelctl.exe webui stop
```

或 `Stop-Process -Name python -Id <worker-webui-pid> -Force`。

### T7b. 中心侧看心跳状态演化

中心 `CLUSTER_HEARTBEAT_INTERVAL_S` 默认 10s、`CLUSTER_LEASE_S` 90s、stale 阈值 3×lease=270s。
连续刷 `modelctl cluster nodes`：

```powershell
# 每 30s 间隔看一次，~45s 配合 top5
modelctl cluster nodes
# ...
```

- 前 90s（lease 未过期）：仍 `online`（worker 没在发心跳，但 lease 还有效地覆盖 last_seen 推后期内）。
- 90~270s：`stale`（lease 过期但 last_seen < 3×lease）。
- `>` 270s：`offline`。

**代码断言**（在 3 分钟后再跑）：

```powershell
$r = (modelctl cluster nodes | Out-String | ForEach-Object { ConvertFrom-Json $_ })
($r.node_status | Where-Object { $_.node_id -eq "w-mock-01" }).status
# 期望："offline"
```

### T7c. 恢复 worker，验 adopt（不重试启动）

```powershell
# worker 侧
modelctl webui start
```

worker reconciler 启动时按 §A3（adopt on startup）：扫描 `*.pid` 活进程 + 健康目标 → 直接置为 running，**不**重走 start 状态机，避免双跑。

center 再看：

```powershell
modelctl cluster nodes            # 应回到 online
modelctl cluster goal list        # stage 应直接 READY（adopt 跳过 STARTING）
```

> 如果 goal 重新经历 PENDING_PROFILE_SYNC → PROFILE_SYNCED 再到 STARTING：说明 adopt 没生效（环境异常）——查 worker `data\cache\*.pid` 是否健康。

## 10. T8 — 节点治理（代码）

### T8a. 禁用（goal 不动，但 hello/join 拒）

```powershell
modelctl cluster node disable  --node w-mock-01
# 视觉上立即：worker 下一次 hello 或重连被拒，节点标 disabled
modelctl cluster nodes          # 状态应 disabled（不是 online）
modelctl cluster node enable   --node w-mock-01   # 解除；状态由下次心跳自然决定
```

### T8b. 轮换 token

```powershell
modelctl cluster join-token --rotate-node w-mock-01
# 新 token 回显一次；把 worker .env 的 CLUSTER_NODE_TOKEN 改成新值，重启 worker webui
D:\WorkPlace\Pycharm\modelctl-mock-worker\venv\Scripts\modelctl.exe webui restart
```

**代码断言**：`Get-Content D:\WorkPlace\Pycharm\modelctl-mock-worker\.env | Select-String "CLUSTER_NODE_TOKEN="` 应见新值（**只手动改一次**—— worker 不会自改 center 给的响应后 token）。

### T8c. 踢除（断 WS，worker 自动重连）

```powershell
modelctl cluster node kick --node w-mock-01
```

**人工/代码验证**：worker webui 日志应见 "ws drop, backoff ... reconnect 5s"；类似 10~30s 后回到 online（`modelctl cluster nodes`）。

### T8d. 退役（删除节点 + 级联删 goal）

```powershell
modelctl cluster node retire --node w-mock-01 --yes
modelctl cluster nodes
modelctl cluster goal list
```

**人工验证**：
- center 台账里 `w-mock-01` 消失；
- 该节点所有 goal 被级联撤销（worker 端 reconciler 下拍 prune + 停模型）。

> T8d 之后，本组 mock 节点套件一次结束；可回 T4 重新 join 一个 lifecycle 跑。

## 11. T9 — 跨机复刻（可选；真实两电脑 LAN 链路）

把本轮单机 mock 升成两台真机：

1. 在**第二台 Windows** 上 `git clone` modelctl 或整目录拷（同版本）；
2. 在其开发目录 `.env` 改：
   ```
   CLUSTER_ROLE=worker
   CLUSTER_CENTER_URL=http://<第一台_IP>:4173
   CLUSTER_NODE_ID=w-<LAN 内的真正编号>
   CLUSTER_LAN=lan-a
   API_KEY=<同 center>
   UNSLOTH_API_KEY=<同 center>
   WEBUI_PORT=4173       # 第二台也走 4173，本机对本机而言没冲突
   MODEL_ROOT/<实际盘符>\models
   ```
3. `envsetup docker` 装 docker（如未装）；
4. 中心 `.env` 的 `WEBUI_HOST=0.0.0.0`（让第二台能从 LAN 访问 4173）；
5. 第二台 `modelctl cluster join --center http://<第一台_IP>:4173 --token <TOKEN> --node-id <id> --lan lan-a`；
6. 中心 `modelctl cluster nodes` 应见第二台 `online`；
7. 后续走 T4–T8 同款路径。

**安全提醒**：管理 API **没有细粒度鉴权**（只有 Bearer API_KEY），WEBUI_HOST=0.0.0.0 时 LAN 内任何拿到 key 的客户端都能操作所有节点。仅在内网 / OAuth 网关后放行。

## 12. 单元测试 / pytest 不替代的部分

集群分布式测试 **没有单测**能完整覆盖（设计文档 §A1：错误注入等不能在同一进程里既当 worker 又当 center 自证——只能 e2e mock）。
本路径已涵盖端到端：注册 → 心跳 → 状态推进 → 模型启停 → 故障恢复 → 治理 → 退役。

如有必要，可在 `tests/` 下用 FastAPI TestClient 起 mock center（`CLUSTER_ROLE=control-plane`）+ fixture 模拟 WS client 的 PR；但本轮交付以真实可观测的 e2e 为先。

## 13. 快速失败诊断表

| 现象 | 第一反应 |
| --- | --- |
| `cluster join` 报 `token mismatch` | center 侧 `cluster join-token` 看的不是 token A，或 center 侧 `CLUSTER_ROLE` 不是 both/control-plane |
| worker `webui start` 起不来 | 看 `D:\WorkPlace\Pycharm\modelctl-mock-worker\data\logs\modelctl.log`；端口冲突（4183/5005/18910）用 `Get-NetTCPConnection -State Listen` 验 |
| `cluster nodes` 看不到 worker | worker .env 的 `CLUSTER_NODE_TOKEN` 是否被 pop（rotate-token 后未更新？）；worker webui 是否在跑；`Get-Content -Task data\logs\open-webui-4183.log 2>$null` |
| worker 节点 online 但 goal stage 停在 `PENDING_PROFILE_SYNC` | worker webui WS 注册没成功——看 worker `logs/` 和 center `logs/` 同时段 profile.push 事件；或 worker `CLUSTER_NODE_TOKEN` 空导致 token 链断 |
| goal `FAILED` + `reason=abandoned by previous launch` | center 侧同 profile 失活 period 内有新 launch 抢占（§SettleGate 单实例——旧进程心跳被判废弃；看 `events` 流定位） |
| worker `models/vllm/qwen2.5-0.5b.yaml` 不见了，goal 仍在 | 中心 `goal remove` 已生效但 worker reconciler 还在跑；等一拍 |
| 第二台 LAN 网络源 IP 中心看不到 | center `WEBUI_HOST=127.0.0.1` 改 `0.0.0.0` + 防火墙放行 4173 |

## 14. 回退（回到 solo）

```powershell
# center 改回
Set-Content D:\WorkPlace\Pycharm\modelctl\.env -Value ((Get-Content D:\WorkPlace\Pycharm\modelctl\.env) -replace "^CLUSTER_ROLE=both","CLUSTER_ROLE=solo" -replace "^CLUSTER_LAN=lan-a","") -Encoding UTF8

# worker 删除
Remove-Item -Recurse -Force D:\WorkPlace\Pycharm\modelctl-mock-worker
```

--- 

**收尾检查清单**

- [ ] 两份源码各对应自己的 `PROJECT_ROOT`（venv `python -c "import modelctl; print(modelctl.__file__)"`）
- [ ] center 4173 / worker 4183 各监听起来
- [ ] worker .env 三项 CLUSTER_* 正确 + CLUSTER_NODE_TOKEN 由 join 写回
- [ ] worker webui 侧 `models/vllm/qwen2.5-0.5b.yaml` 是 reconciler 推下来的（**非**本地 patch）
- [ ] stage 推进 PENDING → PROFILE_SYNCED → RUNTIME_OK → STARTING → READY 全跑过
- [ ] `modelctl cluster nodes` 见 online + 三叶草；T7 故障注入 270s 后变 offline
- [ ] worker webui stop 后 center 自动标 offline + recover 后 adopt 不重 STARTING
- [ ] retire 后 cluster 验空、worker 本机无残留 *.pid
