# modelctl 模型启动进度可视化（StartupProgress）设计

日期：2026-09-08
状态：草稿（等待用户评审）

## 1. 背景与目标

### 1.1 问题（2026-09-08 Windows 实测，qwen2.5-1.5b-vllm docker 路径）

启动一个 docker 运行时的 vLLM 模型，全程 UI 无任何有效反馈：

| 现象 | 根因（运行时证据） |
|---|---|
| 模型详情页工作日志永远显示「连接中…」（18 分钟无变化） | docker 路径下 `launch-<name>.log` **只有一行容器 ID**（`start_detached` 重定向 `docker run --detach` 的 stdout，容器内真实输出在 daemon 侧的 `docker logs` 里）；SSE `/log/stream` tail 的是 launch log → 连上后永远无新行 |
| 后台任务面板「（暂无日志）」，start 任务全程零进度 | `admin_models._do_start` 从 `running` 到结束才 `update_detail`，中间无 stage 事件；`docker_setup.ensure_image` 用 `subprocess.run(capture_output=True)` 把 14 分钟 `docker pull` 输出全部吞掉 |
| 用户以为卡死，反复点重试，最终「引擎进程提前退出」 | 容器 `Exit 137 / OOMKilled=false`＝外部 SIGKILL：vLLM 0.28 在 WSL2 冷启动，02:58:06 起容器 → 03:12:44 才打出 banner（14.5 分钟 import torch）→ `ModelConfig(...)` 又 6 分钟 → 03:18:41 被上一次重试的清理逻辑杀掉。**引擎并没有死，只是没有任何 UI 告诉用户它在进行哪一步、大概多久** |

三条根因指向同一个缺失：**start 流程没有阶段化的进度通道**。附带一个真雷：start 默认 `timeout=600`（`admin_models.py` / `cli.py`），而 docker + WSL2 冷启动 import 阶段就花了 878s——用户不点重试也会被健康检查超时误杀。

### 1.2 目标

| 目标 | 度量 |
|---|---|
| T1：所有引擎的 start 流程输出统一阶段进度（5 段阶段机） | task SSE 广播 + 快照文件双通道；venv / docker / ollama 全引擎 |
| T2：docker 拉镜像有真实百分比与预估剩余时间 | 解析 `docker pull` 非 TTY 逐层输出 → 聚合 pct；阶段级 EMA ETA |
| T3：docker 运行时的引擎日志进入既有日志链路 | 后台 tee `docker logs -f` → launch log；SSE / CLI `modelctl logs` / 早退日志摘录零改动全部复活 |
| T4：详情页有常驻启动进度大卡片 | `StartupProgressCard`：5 段时间轴 + 当前段进度条 + ETA chip + 错误态 |
| T5：冷启动不再被 600s 误杀 | docker runtime 默认超时 1800s（venv 维持 600s，`MODELCTL_START_TIMEOUT` 覆盖） |
| T6：修 SSE「连接中」假状态 | `SseLogViewer` 接 `EventSource.onopen`，连上即切换状态 |

### 1.3 非目标

- 运行期（ready 之后）的推理性能监控、排队/吞吐可视化。
- `trtllm build` 子命令的编译进度（build 强制 venv，独立逻辑，不纳入阶段机）。
- gateway / stats / TUI 等服务自身启动进度（本设计只管模型 profile）。
- 历史启动曲线、跨机器聚合统计。

## 2. 已确认决策（brainstorming 问答记录）

| 决策点 | 结论 |
|---|---|
| 范围 | 全量：阶段进度 + ETA + 详情页大卡片 |
| 覆盖面 | 所有引擎启动流程统一建模（docker 段有百分比，venv 段无百分比显示阶段名+耗时） |
| docker 日志归位 | 启动时后台 tee `docker logs -f` → launch log（单数据源，下游全部复活），而非仅 SSE 特判 |
| ETA 精度 | 阶段级均值 ETA（EMA 滑动估计，不做到 shard/layer 细粒度） |

## 3. 总体架构

```
all_service.start_profile(profile, caps, timeout, on_progress=None)
        │  发射 StageEvent（5 段阶段机）
        ├─ preflight    adapter.check_requirements() + 端口预检 + warnings
        ├─ prepare_env  adapter.pre_start()   ← docker: ensure_image 逐层 pct
        │                                      ← download: 模型下载文件数 pct
        ├─ launch       adapter.build_command() + start_detached()
        ├─ loading      wait_ready 等待窗口内 watcher 线程 tail 日志匹配模式表
        └─ health       wait_ready 返回 → 100%
                │
                ├─→ on_progress 回调（CLI: logger.info 一行；WebUI: task.event("stage")）
                ├─→ 快照落盘  data/cache/<name>.startup.json（每次事件原子覆写）
                └─→ 耗时统计  data/cache/startup-timing.json（engine:stage → EMA）

前端：
  发起页      task SSE（既有 _sse_task_stream，零协议改动）
  刷新/旁观页  GET /admin/api/models/{name}/startup 快照 + 2s 轮询兜底
      └─→ StartupProgressCard（详情页头部下方常驻，starting/失败时可见）
```

复用既有 `Task.event("stage", …)` 基建（与 Docker 一键安装同一通道），不新增 SSE 端点类型。

## 4. 详细设计

### 4.1 新模块 `src/modelctl/core/startup_progress.py`

```python
STAGES = ("preflight", "prepare_env", "launch", "loading", "health")
STAGE_LABELS = {"preflight": "依赖检查", "prepare_env": "准备环境",
                "launch": "拉起进程", "loading": "加载模型", "health": "就绪"}

@dataclass
class StageEvent:
    stage: str            # STAGES 之一
    status: str           # running | done | error
    label: str            # 人话文案，如 "拉取镜像 vllm/vllm-openai:latest（3/9 层）"
    pct: float | None     # 0.0–1.0；None = 不确定态（前端条纹动画）
    eta_s: int | None     # 预估剩余秒；None = 无历史样本（显示"首次运行，无预估"）
    error: str | None     # status=error 时的原因（RequirementError 双路径文案原样透传）
```

`StartupTracker`（每次 start 一个实例）职责：

1. **发射**：`emit(stage, status, *, label, pct=None, error=None)` → 补 `eta_s`（查 EMA）→ 依次调 `on_progress(StageEvent)`、覆写快照文件。
2. **快照** `data/cache/<name>.startup.json`（`cache_dir()` 已有，见 `core/paths.py`）：

   ```json
   {"profile": "qwen2.5-1.5b-vllm", "engine": "vllm", "runtime": "docker",
    "updated_at": "2026-09-08 03:18:41",
    "stages": [{"stage": "preflight", "status": "done", "label": "依赖检查",
                 "started_at": "...", "finished_at": "..."},
                {"stage": "prepare_env", "status": "running", "pct": 0.45,
                 "eta_s": 360, "label": "拉取镜像 …（4/9 层）", ...}, ...]}
   ```
   写盘用 `tmp + os.replace` 原子覆写；快照是尽力而为（写失败只 log，不影响启动）。
3. **耗时统计** `data/cache/startup-timing.json`：键 `"<engine>:<stage>"`，值 `{"ema_s": 872.5, "n": 4, "updated_at": "..."}`。stage done 时以本次实测耗时更新 EMA（α=0.4，`n<5` 时直接用算术均值）；`running` 事件查询：`eta_s = round(ema_s * (1 - pct))`（pct=None 时 `eta_s = ema_s` 剩余未知仍给量级参考）。冷启动无样本 → `eta_s=None`。读写竞争容忍最后写赢（单机单启动语义，无锁）。

阶段耗时只在 **done** 时计入统计；error / 被杀的 attempt 不落样本（避免把重试 SIGKILL 的半程耗时当基线）。

### 4.2 loading 段日志监视（`LoadingWatcher`，同模块）

`wait_ready` 等待窗口内起一个 daemon 线程，每 2s：

1. `tail` launch log 增量行（docker 路径内容来自 §4.4 tee；venv 路径进程自己重定向写入）。
2. 按 `PATTERNS[engine]`（有序 `list[(compiled_regex, label, pct)]`）匹配，命中且 pct 单调不减才 `emit`。
3. `spawned_proc` 早退（venv）或容器退出探测（docker，复用 `adapter.backend_dead()` 语义交给 wait_ready 自身，watcher 只负责进度不负责失败判定）时线程结束；`wait_ready` 返回后 `stop()`（Event 置位 + join(3)）。

`PATTERNS` 初始表（vllm / tokenspeed / tensorrt_llm 三个 docker_capable 引擎 + vllm venv 同表；其余引擎整段兜底文案）：

| 引擎 | 匹配行（vLLM 0.28 实测措辞） | label | pct |
|---|---|---|---|
| vllm | banner `vLLM API server` | 引擎进程初始化 | 0.05 |
| vllm | `Downloading shards: +(\d+)%` | 下载模型权重 | 0.05+0.45n |
| vllm | `Loading safetensors checkpoint shards: +(\d+)%` | 加载模型权重 | 0.5+0.3n |
| vllm | `Capturing CUDA graph shapes: +(\d+)%` | 捕获 CUDA graph | 0.8+0.15n |
| vllm | `Starting API server` / `Application startup complete` | 启动 HTTP 服务 | 0.97 |
| tokenspeed / tensorrt_llm | 首版仅 banner 行 + 兜底 | 引擎初始化中 | None |

兜底文案（无任何命中且 loading 已持续 > 120s）：`引擎初始化中（首次冷启动在 docker/WSL2 上可达 15 分钟）`，pct=None——**正是本次事故里用户缺失的那句话**。regex 匹配为纯字符串包含/捕获，不依赖 ANSI（docker logs 非 TTY 无颜色码；venv 路径若含 ANSI 先剥码再匹配）。

### 4.3 `docker pull` 流式解析（`docker_setup.py`）

`ensure_image(image, attempts=None, on_progress=None)` 加可选回调（不传行为完全兼容现状）：

- `subprocess.run(capture_output=True)` → `Popen(["docker","pull",image], stdout=PIPE, stderr=STDOUT, text=True)` 逐行读。无 TTY 时 docker CLI 自动输出经典逐层格式（`<digest>: Pulling fs layer / Downloading 51.2MB/1.2GB / Pull complete`），无需处理 TTY 光标动画。
- 解析器 `parse_pull_line(line) -> PullUpdate | None` 纯函数（单测 fixture 文本驱动）：维护 `{digest: state}`；总层数在首批 `Pulling fs layer` 齐后固定（60s 内未见任何层行则退化为无 pct 的 label 更新）。
- 聚合 `pct = (done层数 + Σ 进行中层字节比) / 总层数`；每层 `Extracting` 阶段计入该层 done 之前的子进度（0.5 权重）。每次 `pull` attempt 重置状态机（重试时 pct 回落是真实语义，label 带 `（第 i/N 次尝试）`）。
- 错误分类重试逻辑（`classify_pull_error` / `TRANSIENT_MARKERS`）不变：stderr 全文仍在逐行读取时收集（现在合并了 stdout+stderr，失败时取全文分类，与现状 `proc.stderr or proc.stdout` 语义一致——分类函数本身对混合文本不敏感）。
- 阶段事件：本地已有镜像（`image_present`）→ `prepare_env` 直接 done（label「镜像已就位」，秒过）；拉取中 → running + pct + ETA；完成 → done。
- `pre_start` 里 vllm/tokenspeed/trtllm 的 `ensure_image(image)` 调用点透传 `on_progress`（adapter 通过新增的 `set_progress_sink(fn)` 方法接收，由 `start_profile` 注入；不注入时 sink 为 None）。

模型下载分支（llamacpp `source_dir` 编译 / `download` 下载）：`pre_start` 现有实现内不动，阶段机把整个 `pre_start` 归入 `prepare_env`，下载子进度本期不做（pct=None + 兜底 label）。

### 4.4 docker 日志 tee（修「连接中」真内容）

docker 容器启动成功后（`is_docker_runtime()` 为 True），spawn 后台进程把容器输出续写进 launch log：

```python
cmd = adapter.log_tee_cmd()          # 引擎适配器新增钩子，docker 分支返回
                                     # ["docker", "logs", "-f", "--tail", "all", <container>]
                                     # 其它运行时返回 None（零行为变化）
subprocess.Popen(cmd, stdout=open(launch_log(name), "ab"),
                 stderr=subprocess.STDOUT, **DETACHED)
```

- 用 `--tail all`（首挂即全量续写），而非 `--tail 0`：`docker run --detach` 秒返回容器 ID，若只跟新行，tee 挂上前的数百毫秒内 banner 会漏，伤及 §4.2 loading 监视。全量重放对本 profile 无副作用——launch log 里此前只有 `start_detached` 写的一行容器 ID。唯一重复风险是同容器被外力杀后 re-tee（重启走新容器，不受影响），append-only 写入 + watcher 的 pct 单调不减保证重复行无害。
- tee PID 写 `data/cache/<name>.log-tee.pid`：
  - `start_profile` 进入即清理残留 tee（PID 存在且活着 → kill），防止双 tee 重复写；
  - `stop_profile` 在 `adapter.stop_backend()` 后 kill tee 并删 PID；
  - 容器被外力删除时 `docker logs -f` 自行退出，无孤儿风险（Windows 下 `DETACHED_PROCESS` 同款 flags 复用 `process.py` 现有常量）。
- 失败摘录兜底：`all_service` 失败分支里 `died and log 内容仍只有容器 ID 行` 时，改为直接跑 `docker logs --tail 50 <container>` 摘录（此时 tee 可能没来得及挂上/被 SIGKILL），保证「引擎进程提前退出」的日志摘录有真实内容。

### 4.5 `all_service.start_profile` 插桩

签名加尾参：`start_profile(profile, caps, timeout, on_progress=None)`（`restart_profile` 同步透传）。现有步骤与阶段对齐，**不改任何执行顺序**：

| 阶段 | 现有代码位置 | 事件 |
|---|---|---|
| preflight | 幂等 skip 判定、端口预检、`check_requirements`、warnings、kv 预检 | running → done；RequirementError → 当前阶段 error + 重抛 |
| prepare_env | `adapter.pre_start()`（docker 拉镜像经 §4.3 回调；内部再 emit 细 pct） | running（含子 pct）→ done |
| launch | `build_command` + `start_detached`（docker 路径此后立即挂 §4.4 tee） | running → done（瞬时） |
| loading | `LoadingWatcher`（§4.2）覆盖 `wait_ready` 等待窗口 | running（模式表 pct / 兜底文案）→ done |
| health | `wait_ready` 成功 + `post_start` | done（pct=1.0） |

失败路径：`wait_ready` 返回 False → `loading` 段 error，error = 「引擎进程提前退出」/「健康检查超时」（沿用现有 `ComponentResult.detail` 文案，摘录逻辑见 §4.4 兜底）。CLI 传 `on_progress=lambda ev: logger.info(ev 单行)`——logger 只写前缀，消息在调用侧拼好（遵守 CLAUDE.md 日志对齐规范，本消息为单行 KV 无表格对齐负担）。

`reconcile.py::_default_starter` 不传回调（集群收敛路径保持静默，零行为变化）。

### 4.6 启动超时自适应（T5）

新 helper `all_service.default_start_timeout(profile, caps) -> float`：

```
MODELCTL_START_TIMEOUT 环境变量 → 显式值（覆盖一切）
runtime == docker（adapter 判定）→ 1800
其余 → 600（现状值）
```

接入点（调用方未显式指定时才走自适应）：

- `admin_models.py` start/restart：`timeout: float | None = Query(default=None, ge=1, le=7200)`；None → helper。前端发起请求**不再传 timeout**，删掉写死值。
- `cli.py` start/restart：`--timeout` `default=None`，None → helper（帮助文案改「默认 docker 1800s / 其它 600s」）。

### 4.7 WebUI API

1. **stage 事件广播**：`_do_start` / restart 把 `on_progress` 绑到 `task.event("stage", ev_dict)` + `task.update_detail(f"{label} {pct%}")`——`_sse_task_stream` 透传既有事件类型，零协议改动；后台任务面板自动开始滚动显示 label。
2. **快照端点**：`GET /admin/api/models/{name}/startup`（poweruser）：读 `cache/<name>.startup.json`，不存在 → 404；存在 → camelCase 响应模型（沿用 webui 既有 Response 风格）：

   ```json
   {"profile": "…", "engine": "vllm", "runtime": "docker", "updatedAt": "…",
    "stages": [{"stage": "prepare_env", "status": "running", "label": "…",
                 "pct": 0.45, "etaSeconds": 360, "error": null,
                 "startedAt": "…", "finishedAt": null}]}
   ```

### 4.8 前端

**`web/src/components/startup/StartupProgressCard.vue`**（新组件，视觉复用 [DockerInstallPanel.vue](../../web/src/components/docker/DockerInstallPanel.vue) 的时间轴+徽标语言）：

- props：`snapshot`（§4.7 响应）；5 段横向时间轴，done=绿 / running=蓝脉冲 / error=红 / pending=灰。
- 当前段下方：`pct` 有值 → 实进度条 + `45% · 约剩 6 分钟`；`pct=null` → 条纹动画条 + label（兜底文案原样显示）；`etaSeconds=null` → `首次运行，无预估`。
- error 态：红条 + error 全文 +（docker 且错误文案含「环境未创建/未安装」时）跳转「环境」页链接。
- 展示条件：模型状态 `starting` 或最近一次 start 失败；`running`/已停止隐藏。
- 数据流：发起页复用已订阅的 task SSE（stage 事件增量合入本地 snapshot）；非发起页 / 刷新后 → 拉快照 + `starting` 期间 2s 轮询。
- 时间显示一律 `YYYY-MM-DD HH:mm:ss`（CLAUDE.md 规范），后端已格式化字段前端不二次加工。

**`SseLogViewer.vue`**：`openModelLogStream` 增 `onopen` 回调 → `state='open'`（修「连接中」假状态：EventSource 连接成功即绿，不等首行日志）；`onerror` 已有逻辑不变。

**`ModelDetailView.vue`**：头部 `<el-descriptions>` 下方挂 `<StartupProgressCard>`；工作日志 tab 内容不变（tee 落地后自动有内容）。

## 5. 错误处理汇总

| 场景 | 行为 |
|---|---|
| RequirementError（含双路径 dual_error 文案） | preflight/prepare_env error 态 + 原文透传快照与卡片 |
| `docker pull` 最终失败 | prepare_env error，error=分类后的末行错误；不吞 stderr |
| 快照/计时文件损坏或不可写 | log warning 后跳过落盘，启动流程不受影响（进度尽力而为） |
| tee 进程起不来（docker CLI 异常） | log warning，工作日志面板退化为「无内容」但启动不受影响 |
| watcher 线程任何异常 | 捕获 + log，仅失去 loading 子进度，`wait_ready` 主判定不受影响 |
| 快照存在但 profile 已删除 | 端点 404（按 profile 存在性判定优先于文件） |

## 6. 测试策略（全部 mock，不依赖真 docker）

- `test_startup_progress.py`：pull 解析器 fixture（含 `Already exists` / `Downloading`/`Extracting` 混合 / 重试重置）；`PATTERNS` vllm 日志 fixture 单调 pct 序列；EMA 冷启动无样本 / 收敛；快照原子覆写与损坏容忍。
- `test_all_service_startup_progress.py`：fake adapter + monkeypatch `ensure_image`/`start_detached`/`wait_ready` → 断言 5 段事件序列、RequirementError 停在 preflight error、wait_ready False → loading error。
- `test_docker_log_tee.py`：mock Popen → spawn 参数（`--tail 0`、append 模式）、残留清理、stop kill、非 docker runtime 不 spawn。
- `test_admin_models_startup_endpoint.py`：无快照 404、有快照 camelCase 契约。
- `docker_setup.ensure_image` 既有测试维持通过（回调缺省零行为变化）。
- 前端手动验收：模拟慢镜像（`docker pull` 大镜像 + 限速）观察百分比与 ETA；起 qwen2.5-1.5b-vllm 全程卡片阶段推进；容器 `docker kill` 后卡片红态 + 摘录有真实日志。

## 7. 影响文件清单

| 文件 | 变更 |
|---|---|
| `src/modelctl/core/startup_progress.py` | 新增：StageEvent / StartupTracker / LoadingWatcher / PATTERNS / pull 解析 |
| `src/modelctl/core/docker_setup.py` | `ensure_image` 流式化 + `on_progress` |
| `src/modelctl/core/all_service.py` | `start_profile`/`restart_profile` 加回调 + 5 段插桩 + tee 生命周期 + `default_start_timeout` |
| `src/modelctl/engines/base.py`（+ vllm/tokenspeed/trtllm） | `log_tee_cmd()` / `set_progress_sink()` 钩子 |
| `src/modelctl/core/webui/admin_models.py` | task stage 事件、`/startup` 端点、timeout=None 自适应 |
| `src/modelctl/cli.py` | `--timeout` 缺省自适应 |
| `web/src/components/startup/StartupProgressCard.vue` | 新增 |
| `web/src/views/ModelDetailView.vue` | 挂卡片 |
| `web/src/components/common/SseLogViewer.vue` | `onopen` 修假「连接中」 |
| `tests/…` | §6 所列 |
