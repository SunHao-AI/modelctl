# modelctl 集群 · 单机 mock 两节点功能测试（按终端划分）

> 目标：在本机（`D:\WorkPlace\Pycharm\modelctl`，`127.0.0.1`）**同时模拟 center + worker 两个 modelctl 节点**，跑通 M0–M2 全链路（节点注册、goal 下发、profile 同步、模型远程启停、故障注入、节点治理）。
>
> **本文档完全按终端组织**。每个 step 顶部都用 `T-center` / `T-worker` / `Both` 标明应在哪个窗口里执行；step 之间的命令可跨窗口交叉——**你只需在这些窗口间切换**。

## 0. 终端拓扑

| 终端 | 路径 | 角色 | webui | 典型用途 |
| --- | --- | --- | --- | --- |
| **T-center** | `D:\WorkPlace\Pycharm\modelctl` | `CLUSTER_ROLE=both`（中心控制面；不向自己注册为节点，见下方说明） | `127.0.0.1:4173` | 集群 init、goal 下发、nodes / status / events / backup 等中心侧操作 |
| **T-worker** | `D:\WorkPlace\Pycharm\modelctl\mock-worker` | `CLUSTER_ROLE=worker`（`CLUSTER_NODE_ID=w-mock-01`） | `127.0.0.1:4183` | 加入集群、模型被拉起位置（`18911/vllm` 或 `18910/llamacpp`） |
| *(可选)* **T-browser** | 浏览器 — | — | — | 登录 4173 webui 看"节点"页、看 SSE 事件流 |

**端口约定**：

| 端口 | T-center 占用 | T-worker 占用 |
| --- | --- | --- |
| 4173 | webui（admin REST + SSE） | — |
| 4183 | — | webui（worker 本地管理面） |
| 18911 | — | vllm `qwen2.5-0.5b`（默认 yaml；`models/vllm/qwen2.5-0.5b.yaml`） |
| 18910 | — | llamacpp（备用副本） |
| 5005 | — | worker 网关（默认，本轮不跑） |

> ⚠️ **18911/18910 已经同时是 center 自己 yaml 的端口**：center 的 `models/vllm/qwen2.5-0.5b.yaml` 现在写的就是 `port: 18911`、`models/llamacpp/qwen2.5-0.5b.yaml` 写的就是 `port: 18910`。这意味着：
> - `worker_copy.ps1` 里 `8108 → 18911` / `18897 → 18910` 的端口 patch **已成 no-op**（源值已等于目标值），别再指望它做隔离；
> - center（`CLUSTER_ROLE=both`）**不要本地跑同名 profile**，否则与 worker 抢同一端口，`all_service` 的 `port_in_use` 前置拦截会点名占用者并直接失败；
> - 需要真正双跑同名 profile 时，改 **center 的 yaml**（sync 会把它推到 worker，两侧仍同端口，互斥关系不变）或换 profile。

> **两份源码目录的强约束**：`envfile.PROJECT_ROOT = Path(__file__).resolve().parents[3]` 由 `python -c "import modelctl"` 实际安装路径推导，不可被 env 变量覆盖；**同一份 `src/` 装两个 venv 会让 SQLite 台账 + pid 文件被两个 webui 进程读写同一份**——测试作废。worker 必须用 `worker_copy.ps1` 生成的独立项目副本 + 独立 venv。

> **profile 下发小陷阱（已踩过）**：center 用 `profiles.load(name)` 读自己 `models/<engine>/<stem>.yaml` 原样存进 goal（`goals.py._write_goal(profile_yaml=source["yaml"], profile_sha=source["sha"])`），worker 收到后**直接用这份 yaml 覆盖本地文件**——这意味着：
> - 想改 worker 端运行端口 / 显存参数，**改 center 的 yaml**（中心是 profile 的"单一事实源"）；
> - 改 worker 本地 yaml 只会被下一拍 sync 覆盖回去，**无效**。
>
> **Windows 桌面 vllm 冷启动需显式 `CLUSTER_START_TIMEOUT_S=1800`**：
> - Worker 默认 `CLUSTER_START_TIMEOUT_S=300`（[config.py L88](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/cluster/config.py#L88-L90)），与 `all_service.START_TIMEOUT_DOCKER=1800` **不同源**——worker reconciler 用前者，导致 vllm docker 在 WSL2/FUSE+nvidia 7.5 上 >5 min 的权重加载直接被判 `startup_timeout`。
> - 修：worker `.env` 显式 `CLUSTER_START_TIMEOUT_S=1800`（已 sunk 进 [worker_copy.ps1 L105](file:///d:/WorkPlace/Pycharm/modelctl/docs/cluster-mock/worker_copy.ps1#L105-L108)），worker webui **restart** 后生效；`all_service.default_start_timeout` 路径无变化（仍按 `is_docker_runtime` 给 1800）。
> - ⚠️ **历史 `.env` 不会被脚本纠正**：`worker_copy.ps1` 每次 `Remove-Item` 重建整个副本（含已下载的 `data\models\*` 权重），所以只想改超时**别重跑脚本**，直接改 `mock-worker\.env`。曾因手工留档把该项停在 `600`，Windows 冷启动仍被判 `startup_timeout`——核对：
>   ```powershell
>   Select-String -Path D:\WorkPlace\Pycharm\modelctl\mock-worker\.env -Pattern "^CLUSTER_START_TIMEOUT_S="
>   # 非 1800 时：
>   (Get-Content D:\WorkPlace\Pycharm\modelctl\mock-worker\.env) -replace "^CLUSTER_START_TIMEOUT_S=.*","CLUSTER_START_TIMEOUT_S=1800" | Set-Content D:\WorkPlace\Pycharm\modelctl\mock-worker\.env -Encoding UTF8
>   ```
>
> **placement gate 拒绝常见原因**：
> - `machine_not_ready`（worker 本机 `.venvs/<engine>` 没建）：sglang / tokenspeed / aphrodite / lmdeploy / tensorrt_llm 等——worker 必须先 `modelctl env setup <engine>`（worker 用**自己**的 venv，center 的 venv 不算）。
> - `docker_image 非空` 的引擎（vllm / tensorrt_llm / tokenspeed）走 docker 旁路绕过 venv 检查，**不需要** `env setup`；本地 `docker start+pull` 完即可。
> - `llamacpp` 特殊：不在 MANAGED_ENGINES，需**源码编译**——PATH 有 `llama-server` 或 `LLAMACPP_SOURCE_DIR` 下有 `<src>/build/bin/llama-server`；missing 时 `dry-run` 直接 `[dry-run] SKIP <node> engine llamacpp 在 <node> 上不可用`。

## 1. 一次性准备（Both；幂等）

```powershell
cd D:\WorkPlace\Pycharm\modelctl
powershell -ExecutionPolicy Bypass -File docs\cluster-mock\worker_copy.ps1
```

产物核对（**Both**，不需要任何 venv 激活）：

```powershell
dir D:\WorkPlace\Pycharm\modelctl\mock-worker\.env
dir D:\WorkPlace\Pycharm\modelctl\mock-worker\models\vllm\qwen2.5-0.5b.yaml
dir D:\WorkPlace\Pycharm\modelctl\mock-worker\models\llamacpp\qwen2.5-0.5b.yaml
dir D:\WorkPlace\Pycharm\modelctl\mock-worker\venv\Scripts\python.exe
```

代码侧核对（**Both**）：

```powershell
# 期望：输出 …\modelctl\mock-worker\src\modelctl\__init__.py（不是 center 路径）
D:\WorkPlace\Pycharm\modelctl\mock-worker\venv\Scripts\python.exe -c "import modelctl,sys; print(modelctl.__file__)"
```

**入口自检（Both）**：

```powershell
# 期望：无监听 4173/4183/18910/18911
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 4173, 4183, 18910, 18911 } | Format-Table -AutoSize
# 如有残留：
modelctl webui stop     # 或在对应 venv 里
Stop-Process -Name python -Force
```

## 2. center 侧：启用控制面 + 初始化集群（T-center）

```powershell
cd D:\WorkPlace\Pycharm\modelctl
modelctl webui start                     # 4173 listen
modelctl cluster init                    # 输出 join token: JT-XXXX
```

记下打印的 `TOKEN_A`。

代码核对（**T-center**）：

```powershell
modelctl cluster join-token              # 同一 token，直读台账
modelctl cluster status                  # 角色: both  中心: True  节点: 0 online / N total
```

人工核对（**T-browser** 任选其一）：

- `http://localhost:4173/` → 登录 → 节点页，此步**列表为空属正常**（见 §0：center 不向自己注册）。第一条记录要等 §3 worker join 后出现。

## 3. worker 侧：加入 + 起 webui（T-worker）

```powershell
cd D:\WorkPlace\Pycharm\modelctl\mock-worker
modelctl cluster join --center http://127.0.0.1:4173 --token <TOKEN_A> --node-id w-mock-01 --lan lan-a
```

**预期**：预检通过 → 写入 `.env`（`CLUSTER_CENTER_URL`、`CLUSTER_NODE_ID`、`CLUSTER_LAN`、`CLUSTER_NODE_TOKEN`，**token 一次性回显请随手记下**）。

**常见报错**：`token mismatch` → 中心做 `cluster join-token --rotate` 过；先 `cluster join-token` 重新获取 token 再来一遍 join。

继续：

```powershell
modelctl webui start                     # 4183 listen
curl.exe -s http://127.0.0.1:4183/admin/api/health     # 200 {"ok": true, "version": "..."}
```

## 4. 节点在线核验（Both）

在 T-worker 起完 webui、**等 5~10 秒心跳**后：

```powershell
# —— T-center：看节点状态
modelctl cluster nodes
# 期望：w-mock-01 状态列 = online，hostname=127.0.0.1，port 列对应 goal 端口
```

```powershell
# —— T-worker：自检自己视角
modelctl cluster status
# 期望：role=worker, 自身 running
```

```powershell
# —— T-worker：worker 侧 jobs 看 profile sync 有没有把 yaml 推下来
modelctl status --port 18911    # 暂未 ready 没关系，能看到引擎 chunk 行即可
```

**代码断言（T-center）**：

```powershell
$r = (modelctl cluster nodes --json 2>$null) | ConvertFrom-Json
$mine = $r.nodes | Where-Object { $_.node_id -eq "w-mock-01" }
if (-not $mine -or $mine.status -ne "online") { Write-Error "T4 FAILED: $mine.status" } else { "T4 OK: $mine.status" }
```

## 5. goal 下发（Both：先 dry-run 再实际 set）

> 中心侧候选已被 `worker_copy` 写入 `CLUSTER_NODE_ID=w-mock-01`，所以**所有 goal 子命令在 T-center 执行**（实际下发的 REST 请求到 4173 由中心代理，CLI 无需切终端）。**T-worker 在这一步只是被动接收并自愈**。

### 5a. dry-run 验 placement gate（T-center）

```powershell
modelctl cluster goal set qwen2.5-0.5b --node w-mock-01 --engine vllm --dry-run
```

- 期望：报告行 "候选 w-mock-01 → 通过"。
- **不会**写台账（`goal list` 仍空）。

### 5b. 实际下发（T-center）

```powershell
modelctl cluster goal set qwen2.5-0.5b --node w-mock-01 --engine vllm --create
modelctl cluster goal list --node w-mock-01
```

- 第一次 set 创建 goal；立刻 `list` 时 stage 通常是 `PENDING_PROFILE_SYNC`（worker 还没回拍 sync 完成回调）。

### 5c. 持续观察 stage 推进（Both）

worker 端 reconciler 一拍一拍推，**T-worker** 侧查本地状态最快：

```powershell
# 看 worker 本地 stage（不经网络，0 延迟）
Get-Content D:\WorkPlace\Pycharm\modelctl\mock-worker\data\cache\cluster-reconcile.json -Raw | ConvertFrom-Json |
    Select-Object -ExpandProperty goals |
    ForEach-Object { "{0:,-24} {1:,-22} = {2}" -f $_.profile_name, $_.stage, $_.profile_sha }
```

中心侧（**T-center**）走 REST：

```powershell
modelctl cluster goal list --node w-mock-01
# 或带 reason：
modelctl cluster events --node w-mock-01 --limit 30
```

**stage 顺序应该**：

```
PENDING_PROFILE_SYNC → PROFILE_SYNCED → RUNTIME_OK → STARTING → READY
                                                  (或 FAILED：startup_timeout / launch_failed / ...)
```

### 5d. 健康检查（T-center 或 T-worker）

```powershell
curl.exe -s http://127.0.0.1:18911/    # vllm 容器（worker 跑）
# 例如返回 200 HTML 说明容器活着；/v1/models 返回可正确结构化 JSON
```

**自动化断言（T-center）**：

```powershell
$h = curl.exe -s http://127.0.0.1:18911/v1/models
if ($h -notmatch '"id"') { Write-Error "T5 model not healthy" } else { "T5 healthy" }
```

### 5e. 故障排查（stage 卡住 / FAILED）

- 看 reason：
  ```powershell
  modelctl cluster goal list --node w-mock-01 --json | ConvertFrom-Json | ForEach-Object { "{0:,-8} {1}" -f $_.stage, $_.reason }
  ```
- 看事件流：
  ```powershell
  modelctl cluster events --node w-mock-01 --kind launch --limit 50
  ```
- 看 worker 本地日志：
  ```powershell
  # T-worker
  Get-Content D:\WorkPlace\Pycharm\modelctl\mock-worker\data\logs\launch-*.log -Tail 100
  # 或 docker 容器日志
  docker logs --tail 100 (docker ps --filter name=qwen2.5-0.5b --format "{{.ID}}")
  ```

> **cause 表**：
> - `startup_timeout`（最常见）：分两种子原因——
>   - **a. 超时不够长**（Windows Docker Desktop 冷启动 5-30min）：worker `.env` 升 `CLUSTER_START_TIMEOUT_S=1800` + `webui restart`。**remove+set** 重发新模式（FAILED 是终态，retry 不重置 状态机）。
>   - **b. 权重复写/小卡规**：profile `--enforce-eager` + `gpu_memory_utilization` 在 6GB 卡上；改 `model: <本地权重目录>` 或降 `max_model_len`/`gpu_memory_utilization` 后 **remove+set**。
> - `image_missing`：docker 镜像未拉；worker 侧 `docker pull` 完再 `modelctl cluster sync --node w-mock-01`。
> - `workspace_full`：`<MODEL_ROOT>` 剩余 <5GB；清理旧镜像/模型。
> - `profile_not_found`：center `goal list --profile qwen2.5-0.5b` 看 sha；center yaml 路径改过但 intent 未重新 set。

## 6. 远程启停（**cloud-level API**，CLI：`launch` / `stop` / `remove`）

> 设计文档 §4.2 把这三者统一为三个入口：`goal set --intent=...` 是"真身"，`launch`/`stop` 是别名（`cli.py` L282-285）。**没有** `goal stop` 这种嵌套——一字不差的命令在下面这几行。

### 6a. 停（T-center）

```powershell
modelctl cluster stop qwen2.5-0.5b --node w-mock-01
# 或等价的
modelctl cluster goal set qwen2.5-0.5b --node w-mock-01 --intent stop
```

校验（**Both**）：

```powershell
modelctl cluster goal list --node w-mock-01   # intent=stop, stage 可能会跳到 TO_DETERMINE 等同步态
# T-worker
curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:18911/    # 期望非 200 / 超时
```

### 6b. 启（T-center）

```powershell
modelctl cluster launch qwen2.5-0.5b --node w-mock-01
# 或
modelctl cluster goal set qwen2.5-0.5b --node w-mock-01 --intent start
```

观察 stage 从 `TO_DETERMINE`推回 `PENDING_PROFILE_SYNC → ... → READY`。

### 6c. 撤托管（delete goal，删文件 + 停模型 + 删台账，T-center）

```powershell
modelctl cluster goal remove qwen2.5-0.5b --node w-mock-01
modelctl cluster goal list --node w-mock-01     # 空
```

**T-worker 校验文件已剪枝**（reconciler 这一拍下来会删掉被 prune 的 yaml）：

```powershell
Test-Path D:\WorkPlace\Pycharm\modelctl\mock-worker\models\vllm\qwen2.5-0.5b.yaml
# 期望：False
```

## 7. 故障注入：worker webui 掉线 → center 判 stale → offline

> 注释：这是测**心跳机制**，**不需要**touch goal 台账；center 保持 goal 不变。

### 7a. 杀 worker webui（T-worker）

```powershell
modelctl webui stop
# 或更暴力
Stop-Process -Name python -Force    # 在同工作目录用它前先 modelctl status 看 pid
```

### 7b. 中心侧等心跳自愈（T-center，每 30s 一次，约 4-5 次）

默认 `CLUSTER_HEARTBEAT_INTERVAL_S=10` / `CLUSTER_LEASE_S=90`，stale 阈值 = 3×lease = 270s：

| 经过时间 | 期望 status |
| --- | --- |
| 0~90s（lease 还有效） | `online` |
| 90~270s | `stale` |
| `> 270s` | `offline` |

```powershell
modelctl cluster nodes     # 重复执行，直到看到 offline
```

**代码断言**（在 3 分钟后跑）：

```powershell
$r = (modelctl cluster nodes --json 2>$null) | ConvertFrom-Json
($r.nodes | Where-Object { $_.node_id -eq "w-mock-01" }).status   # 期望 "offline"
```

### 7c. 恢复 worker + 验 adopt（T-worker + T-center）

```powershell
# T-worker
modelctl webui start
```

worker reconciler 启动时按 §A3（adopt on startup）：扫 `*.pid` 活进程 + 健康目标 → **直接**标记 running，不重走 STARTING 阶段。

```powershell
# T-center
modelctl cluster nodes               # 应回 online
modelctl cluster goal list --node w-mock-01
# 期望：stage 直接回到 READY（不是 PENDING_PROFILE_SYNC / STARTING）
```

> 如果 goal 又重新走 PENDING → PROFILE_SYNCED → STARTING：说明 adopt 未生效——查 `D:\WorkPlace\Pycharm\modelctl\mock-worker\data\cache\*.pid` 进程是否还在。

## 8. 节点治理（T-center）

### 8a. 禁用 + 解除

```powershell
modelctl cluster node disable  --node w-mock-01
modelctl cluster nodes          # 状态列 = disabled
modelctl cluster node enable   --node w-mock-01
```

> 禁用只拒 hello/join，**goal 台账不动**——所以 disable 期间 stage 不变，worker 重连后自然恢复。

### 8b. 轮换节点 token

```powershell
modelctl cluster join-token --rotate-node w-mock-01     # 打印新 token 一次
# T-worker：把新 token 写到 .env CLUSTER_NODE_TOKEN=... 然后
D:\WorkPlace\Pycharm\modelctl\mock-worker\venv\Scripts\modelctl.exe webui restart
```

代码断言（**T-worker**）：

```powershell
Get-Content D:\WorkPlace\Pycharm\modelctl\mock-worker\.env | Select-String "CLUSTER_NODE_TOKEN="
# 新值手工写过 → OK
```

> ⚠️ worker **不会**自改 token（设计文档 §2.1：rotate 响应一次性回显，必须人工 update `.env` 后重启）。

### 8c. Kick（断 WS，worker 自动指数退避重连）

```powershell
modelctl cluster node kick --node w-mock-01
```

worker 日志（**T-worker**，约 5-30s 后回 online）：

```powershell
Get-Content D:\WorkPlace\Pycharm\modelctl\mock-worker\data\logs\modelctl.log -Tail 50 | Select-String "kick|backoff|reconnect"
modelctl cluster nodes       # T-center 验 online 已恢复
```

### 8d. 退役（级联删 goal + 删节点；**二次确认**）

```powershell
modelctl cluster node retire --node w-mock-01 --yes
modelctl cluster nodes        # w-mock-01 没了
modelctl cluster goal list    # 该节点 goal 已级联删除
```

**T-worker 校验**（应该不再收到 sync，文件已 prune）：

```powershell
Test-Path D:\WorkPlace\Pycharm\modelctl\mock-worker\models\vllm\qwen2.5-0.5b.yaml     # 期望 False
```

> T8d 之后，重新跑一轮：回 §2 重新 `cluster init`（或复用 token）→ §3 重 join。

## 9. 跨机复刻（可选；真实两电脑）

把 mock 升成两台 Windows：

1. 第二台 clone/拷贝 modelctl 仓库（同版本）；
2. 第二台 `.env`：
   ```
   CLUSTER_ROLE=worker
   CLUSTER_CENTER_URL=http://<第一台_IP>:4173
   CLUSTER_NODE_ID=w-<LAN 内真编号>
   CLUSTER_LAN=lan-a
   WEBUI_PORT=4173          # 第二台对自己讲，无冲突
   MODEL_ROOT=D:\models
   ```
3. 第二台 `modelctl env setup docker --run`（如未装）；
4. **第一台**（center）`WEBUI_HOST=0.0.0.0`，防火墙放行 4173；
5. 第二台 `modelctl cluster join --center http://<第一台_IP>:4173 --token <TOKEN> --node-id <id> --lan lan-a`；
6. 第一台 `modelctl cluster nodes` 见第二台 online；
7. 后续 §4–§8 同款路径。

**安全提醒**：管理 API 仅 Bearer API_KEY，**无细粒度鉴权**；`WEBUI_HOST=0.0.0.0` 时 LAN 内任何拿到 key 的客户端都能操作所有节点——生产走 OAuth 网关或反向代理收紧。

## 10. 快速失败诊断表

| 现象 | 终端 | 第一反应 |
| --- | --- | --- |
| `cluster join` 报 `token mismatch` | T-worker ↔ T-center | T-center 先 `cluster join-token` 复看 token |
| worker `webui start` 起不来 | T-worker | `Get-Content D:\WorkPlace\Pycharm\modelctl\mock-worker\data\logs\modelctl.log -Tail 100`；端口冲突 `Get-NetTCPConnection -State Listen` |
| `cluster nodes` 看不到 worker | T-center | ① worker `.env` 的 `CLUSTER_NODE_TOKEN` 是否被 rotate 后未更新；② worker webui 是否跑着 |
| worker online 但 goal stage 卡 `PENDING_PROFILE_SYNC` | Both | T-worker 看 `data\logs\profile*.log` / T-center 看 `cluster events --kind profile`；token 链断 |
| goal `FAILED` + `reason=abandoned by previous launch` | T-center | T-center `cluster events --kind launch` 定位抢占；§SettleGate 单实例 |
| `modelctl cluster node` 命令报 `invalid choice: 'kick'` 或 `invalid choice: 'stop'`（针对 goal） | T-center | **没有** `goal stop`，是 `cluster stop`（§6a）；`node` 子命令只有 disable/enable/rotate-token/kick/retire（§8） |
| worker YAML 改了 port 不生效 | Both | **必须改 center 的 yaml**（§0 已说明：node 上文件由 sync 覆盖）；改完 `goal remove` + `goal set`（FAILED 是终态） |
| 第二台 LAN 看不到 center | T-worker(2) | center `.env` 的 `WEBUI_HOST` 改 `0.0.0.0` + 防火墙 4173 |

## 11. 完工检查清单

- [ ] 两份源码 `python -c "import modelctl"` 各自带完整 `PROJECT_ROOT`
- [ ] 4173 / 4183 监听 OK（`Get-NetTCPConnection`）
- [ ] worker `.env` 三项 `CLUSTER_*` 完整 + `CLUSTER_NODE_TOKEN` 由 join 写回
- [ ] worker 侧 `models/vllm/qwen2.5-0.5b.yaml` 是 reconciler 推下来的（非本地 patch 痕迹）
- [ ] stage 全跑过：`PENDING_PROFILE_SYNC → PROFILE_SYNCED → RUNTIME_OK → STARTING → READY`
- [ ] `cluster nodes` 见 online；stale/offline 三态按 §7b 时间表演化
- [ ] retire 后 `cluster nodes` 空、worker 本机无残留 `*.pid`、`models/vllm/*.yaml` 已 prune

## 12. 回退（回 solo）

```powershell
# T-center
(Get-Content D:\WorkPlace\Pycharm\modelctl\.env) |
  ForEach-Object { $_ -replace "^CLUSTER_ROLE=both", "CLUSTER_ROLE=solo" } |
  Set-Content D:\WorkPlace\Pycharm\modelctl\.env -Encoding UTF8

# Both：删除 worker 整副本（含 .env、venv、models、data）
Remove-Item -Recurse -Force D:\WorkPlace\Pycharm\modelctl\mock-worker
```

---

## Appendix A：CLI 命令速查（按终端聚合）

### T-center 常用

```powershell
modelctl cluster init                     # 一次性；建 SQLite + join token
modelctl cluster join-token               # 看 join token（中心本机）
modelctl cluster join-token --rotate      # 轮换 join token
modelctl cluster join-token --rotate-node w-mock-01
modelctl cluster nodes                    # 节点列表
modelctl cluster status                   # 摘要（角色 / 节点计数）
modelctl cluster goal set <profile> --node <id> --create [--engine vllm] [--dry-run]
modelctl cluster goal list [--node <id>] [--profile <name>] [--json]
modelctl cluster goal remove <profile> --node <id>
modelctl cluster goal retry <profile> --node <id>          # 只推 FAILED 状态机
modelctl cluster stop <profile> --node <id>                # = goal set --intent stop
modelctl cluster launch <profile> --node <id>              # = goal set --intent start
modelctl cluster sync --node <id> | --all                   # 强制全量 profile sync
modelctl cluster events --node <id> --kind launch --limit 50
modelctl cluster node disable|enable|rotate-token|kick|retire --node <id> [--yes]
modelctl cluster backup --to backup.tar                   # 热备份台账
modelctl cluster restore --from backup.tar --yes          # 须先停 center
modelctl status --cluster                               # 聚合视图（仅 center 可忽看）
modelctl llm-map --node w-mock-01                         # 节点视角的 LLM url 表
```

### T-worker 常用

```powershell
modelctl cluster join --center http://127.0.0.1:4173 --token <tok> --node-id w-mock-01 --lan lan-a
modelctl webui start|stop|restart
modelctl status --port 18911    # 看 goal 在不在跑、chunks 推没推到
modelctl cluster status         # 自身角色视角
```

### Both 都能跑（中心只影响走 REST；worker 也是同一个 REST 镜像）

`cluster nodes`、`cluster events`、`cluster goal list` 对 center 和 worker 都能调——CLI 不区分，关键是**有 CLUSTER_API_KEY**（worker 经 join 写入 .env）。
