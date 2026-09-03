# modelctl Web UI 设计文档

> 日期: 2026-09-02 | 状态: 已评审
> 目标: 为 modelctl 多模型部署启动器提供图形化管理界面，降低新手用户的使用门槛

## 1. 需求分析

### 1.1 背景

modelctl 是一个多模型 LLM 部署启动器，支持 9 种推理引擎、12 个子命令、统一网关、用量统计与审计。当前仅有 CLI 运行模式，新手用户需要学习命令行操作、理解 YAML 配置语法、手动拼参数，使用门槛高。

### 1.2 目标用户

- 初次使用 modelctl 的开发者 / 运维人员
- 已有 CLI 使用习惯但希望通过图形化界面降低操作复杂度
- 需要通过浏览器远程管理 LLM 推理服务的用户

### 1.3 功能需求（完整覆盖 12 个 CLI 子命令，含子动作共 15 项映射）

| CLI 命令 | Web UI 对应功能 | 同步/异步 |
|---|---|---|
| `start <name>` | 模型列表行内启动 / 一键启动 | 异步（最长600s） |
| `stop <name>` | 模型列表行内停止 / 一键停止 | 同步 |
| `restart <name>` | 模型列表行内重启 | 异步 |
| `status [name]` | 仪表板 / 模型列表轮询 | 同步 |
| `list` | 模型管理页分组表格 | 同步 |
| `probe` | 硬件体检页 | 同步 |
| `stats <action>` | 服务矩阵页 stats 卡片启停 | 同步 |
| `gateway <action>` | 服务矩阵页 gateway 卡片启停 | 同步 |
| `all <action>` | 一键启停页 | start/restart异步, stop同步 |
| `audit [sub]` | 审计日志页（query/stats/path/cleanup） | 同步 |
| `ui start <name>` | 模型详情页 unsloth UI 启动 | 同步 |
| `ui stop <name>` | 模型详情页 unsloth UI 停止 | 同步 |
| `env <action> [engine]` | 环境管理页 setup/list/remove | setup异步, 其余同步 |
| `nginx-snippet` | 配置中心页生成片段 | 同步 |
| `trtllm <action> <name>` | 环境管理页 trtllm build/status | build异步（28min+） |

### 1.4 非功能需求

- **认证**: 复用 `.env` 中 `API_KEY`，Bearer Token
- **响应式**: 桌面/平板/手机三档
- **深色模式**: 默认深色，专业监控风格
- **实时性**: 3s 状态轮询 + SSE 长任务/日志推送
- **一致性**: 与 CLI 行为完全一致（退出码语义、错误分类、状态判定）

## 2. 竞品分析

调研了 5 个同类产品，提取了 13 项可复用设计模式：

### 2.1 LiteLLM Proxy（最接近的管理面参考）

- **布局**: 侧边栏导航 + 内容区，分组（Dashboard/Models/API Keys/Logs/Settings）
- **表格**: 数据表格带排序、过滤、分页，行内操作按钮
- **卡片**: KPI 卡片（requests/tokens/cost/spend）
- **弹窗**: 模态框用于创建/编辑表单
- **反馈**: Toast 通知 + 加载 spinner
- **新建引导**: 空状态引导（"No API keys yet - create your first key"）
- **暗色模式**: 支持切换

### 2.2 Unsloth Studio

- **布局**: 顶部导航 + 侧栏模型列表
- **日志**: 终端风格实时日志查看器
- **模型卡片**: 量化信息、大小、上下文长度
- **API Key**: 复制按钮
- **错误处理**: GPU OOM 提示、模型未找到提示

### 2.3 vLLM / Ollama（无原生 Web UI，参考其 API 设计）

- 通过外部工具（Grafana、Open WebUI）管理
- 指标通过 Prometheus `/metrics` 暴露
- 健康检查通过 `/v1/models` 或 `/health`
- 无生命周期管理——依赖外部编排器（modelctl 正是做这件事的）

### 2.4 SD WebUI (A1111)（交互模式参考）

- **渐进式披露**: Tab 切换 + 高级参数折叠
- **Slider/Dropdown**: 数值参数用滑块，枚举参数用下拉
- **实时预览**: 参数修改后预览效果
- **快捷键**: 键盘快捷操作
- **参数预设**: 保存/加载/重命名

### 2.5 ComfyUI（复杂模型管理参考）

- **队列**: Active/Completed 任务列表
- **GPU 监控**: 实时显存使用
- **节点验证**: 红色边框标记无效节点
- **History**: 完成任务记录

### 2.6 提取的设计模式清单

| 模式 | 来源 | 应用到 modelctl Web UI |
|---|---|---|
| 侧边栏分组导航 | LiteLLM | 监控/操作/诊断三组 |
| 模型注册表格 | LiteLLM/Ollama | 按 family 分组 + 行内操作 |
| KPI 卡片 | LiteLLM/ComfyUI | GPU/运行模型/服务状态 |
| 终端风格日志 | Unsloth/vLLM | SSE 实时尾随 + 过滤 |
| 审计表格 | LiteLLM | 过滤 + 分页 + 导出 |
| 模态框表单 | LiteLLM/SD WebUI | env setup 向导 + 确认弹窗 |
| 错误横幅 | vLLM/Unsloth | 配置错误红横幅 + 修复建议 |
| 渐进式披露 | SD WebUI | 高级参数折叠 |
| GPU 解锁可视化 | modelctl 自身 | 显示占用方 + 锁状态 |
| 新手引导向导 | LiteLLM | 三步 Onboarding（环境/模型/API） |
| 移动端表格→卡片 | 通用 | <768px 表格转卡片列表 |
| 色彩语义 | 通用 | 绿=运行中, 红=异常, 灰=停止, 橙=加载中 |
| 8px 栅格 | 通用 | 统一间距系统 |

## 3. 技术架构

### 3.1 技术栈

| 层 | 技术 | 版本 |
|---|---|---|
| 前端框架 | Vue 3 + Vite + TypeScript | Vue 3.5+ / Vite 6+ |
| 组件库 | naive-ui | 2.40+ |
| 状态管理 | Pinia | 2.x |
| 图表 | ECharts (vue-echarts) | 5.x |
| HTTP | axios | 1.x |
| 图标 | @vicons/ionicons5 | 0.13+ |
| 后端框架 | FastAPI + Uvicorn | 0.110+ / 0.29+ |
| 后端HTTP | httpx | 0.27+ |
| 数据格式 | Pydantic | 2.x (FastAPI 依赖) |

### 3.2 部署架构

**单进程 FastAPI**（扩展现有 `gateway.py::create_app()`）:

```
浏览器 (Vue3 SPA, 项目根 dist/ 构建产物)
  ├── Pinia (auth / system / operations / ui)
  ├── axios 拦截器 (401→/login, 注入 Bearer)
  ├── 全局 3s 状态轮询 (GET /admin/api/overview)
  └── 按需 SSE (日志尾随 / 长任务进度)

FastAPI 单进程 (uvicorn, 复用 gateway venv)
  ├── /v1/*        原有 OpenAI 兼容代理 (不变，nginx 依赖)
  ├── /admin/api/* 新增管理 API (本设计主体)
  ├── /            前端静态文件 (项目根 dist/，SPA history 兜底 index.html)
  └── 后台线程池 (asyncio.to_thread 跑阻塞操作)
       → 每任务一个 asyncio.Queue 广播 SSE
       → 任务状态存 app.state.tasks[id]

底层复用 (零重写):
  profile.list_profiles / load_profile
  capabilities.probe / ENGINE_BINARIES
  all_service.start_profile / stop_profile / restart_profile
  process.is_running / pid_file / start_detached / stop_instance
  audit._read_audit_entries / stats_summary
  envs.setup / remove / status / known_targets
  gpu_lock.list_gpu_locks
  nginx_snippet.build_llm_map
  adapters.get_adapter / adapter.ui_spec
```

### 3.3 关键设计约束

| # | 约束 | 原因 |
|---|---|---|
| 1 | **不动 `/v1/*`** | nginx 上游依赖该路径做推理代理 |
| 2 | **stop 必须走 `all_service.stop_profile`** | 不能 pkill；需 SIGTERM→SIGKILL→fuser 兜底 + 释放 GPU 锁 |
| 3 | **区分两种错误** | `RequirementError`(exit 2) 配置错误 vs 健康超时(exit 1)，前端分色提示 |
| 4 | **冷启动最长 600s** | start/restart 必须异步任务+SSE，不能同步 POST |
| 5 | **单进程模型成立** | 全靠 `data/cache/*.pid` + `*.gpu-lock` 文件协调，无需额外 DB |
| 6 | **admin 路由前缀 `/admin/api`** | 与 `/v1/*` 隔离，避免冲突 |
| 7 | **CORS 不开** | 单进程同源；未来拆前端才加白名单 |
| 8 | **模型 CRUD v1 只读** | profile YAML 有 `${VAR}` 插值 + 9 引擎 schema 差异大，受控编辑延后 v2 |

### 3.4 目录结构（新增）

```
modelctl/
├── web/                       # ★ 前端项目（新增顶层目录）
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   ├── public/
│   │   └── favicon.svg
│   └── src/
│       ├── main.ts
│       ├── App.vue
│       ├── router/index.ts
│       ├── api/
│       │   ├── client.ts        # axios 实例 + 拦截器
│       │   ├── auth.ts          # login/health
│       │   ├── overview.ts      # overview/system/probe
│       │   ├── models.ts        # models CRUD(只读) + 操作
│       │   ├── services.ts      # services/all
│       │   ├── envs.ts          # env setup/remove
│       │   ├── audit.ts         # audit query/stats/cleanup
│       │   ├── config.ts        # nginx-snippet/config
│       │   └── tasks.ts         # tasks stream
│       ├── stores/
│       │   ├── auth.ts
│       │   ├── system.ts
│       │   ├── operations.ts
│       │   └── ui.ts
│       ├── composables/
│       │   ├── usePolling.ts    # 3s 轮询
│       │   ├── useSseTask.ts    # 任务 SSE
│       │   └── useSseLog.ts     # 日志 SSE
│       ├── components/
│       │   ├── common/
│       │   │   ├── StatusBadge.vue
│       │   │   ├── GpuCard.vue
│       │   │   ├── ModelCard.vue
│       │   │   ├── ServiceCard.vue
│       │   │   ├── OperationButton.vue
│       │   │   ├── LongTaskModal.vue
│       │   │   ├── TaskTray.vue
│       │   │   ├── LogViewer.vue
│       │   │   ├── UsageMetrics.vue
│       │   │   ├── AuditTable.vue
│       │   │   ├── StepWizard.vue
│       │   │   ├── CopyButton.vue
│       │   │   ├── Monospaced.vue
│       │   │   └── FamilyRouting.vue
│       │   └── layout/
│       │       ├── AppLayout.vue
│       │       ├── Sidebar.vue
│       │       ├── TopBar.vue
│       │       └── MobileNav.vue
│       ├── pages/
│       │   ├── Login.vue
│       │   ├── Dashboard.vue
│       │   ├── Models.vue
│       │   ├── ModelDetail.vue
│       │   ├── Services.vue
│       │   ├── Automation.vue
│       │   ├── Environments.vue
│       │   ├── Probe.vue
│       │   ├── Audit.vue
│       │   ├── Config.vue
│       │   ├── Settings.vue
│       │   └── Onboarding.vue
│       ├── theme/
│       │   └── dark.ts
│       └── types/
│           ├── index.ts         # 共享类型定义
│           └── api.ts           # API 响应类型
├── gateway/                    # 现有 gateway 子项目（扩展）
│   ├── pyproject.toml          # 不修改（已含 fastapi/uvicorn/httpx）
│   └── ...
└── src/modelctl/
    ├── cli.py                  # 新增 `webui` 子命令
    └── core/
        ├── gateway.py          # 在 create_app() 中 include admin_router
        └── webui/              # ★ 新增（管理 API 路由）
            ├── __init__.py
            ├── admin_router.py # FastAPI APIRouter
            ├── admin_auth.py   # Bearer Token 依赖
            ├── admin_models.py # 模型相关端点
            ├── admin_services.py # 服务/一键端点
            ├── admin_tasks.py  # 任务流 + SSE
            ├── admin_audit.py  # 审计端点
            ├── admin_envs.py   # 环境端点
            ├── admin_config.py # 配置端点
            └── admin_probe.py  # 体检/概览端点
```

## 4. 页面/路由设计

### 4.1 路由表

| 路由 | 页面 | 说明 |
|---|---|---|
| `/login` | Login | 单框 API Key 登录 |
| `/` | Dashboard | 仪表板（默认） |
| `/models` | Models | 模型管理（分组表格） |
| `/models/:name` | ModelDetail | 模型详情（tab: 基础/日志/用量/Agent） |
| `/services` | Services | 服务矩阵（stats/gateway/家族路由） |
| `/automation` | Automation | 一键启停 |
| `/environments` | Environments | 环境管理 |
| `/probe` | Probe | 硬件体检 |
| `/audit` | Audit | 审计日志 |
| `/config` | Config | 配置中心 |
| `/settings` | Settings | 设置/帮助 |
| `/onboarding` | Onboarding | 首次引导向导 |

### 4.2 侧边栏导航（三组）

```
监控
  ├── 仪表板        (GaugeOutline)
  ├── 模型管理      (CubeOutline, 运行数 badge)
  ├── 服务矩阵      (ServerOutline)
  └── 审计日志      (DocumentTextOutline)

操作
  ├── 一键启停      (FlashOutline)
  └── 环境管理      (GitMergeOutline)

诊断
  ├── 硬件体检      (HardwareChipOutline)
  ├── 配置中心      (SettingsOutline)
  └── 设置/帮助     (HelpCircleOutline)
```

### 4.3 各页面核心交互

#### 仪表板
- 进页拉 `/overview`，之后 3s 轮询
- 4 个 KPI 卡片 + GPU 监控列 + 模型/服务状态列 + 最近审计
- 磁贴跳详情/自动化

#### 模型管理
- 按 `group` 分组、组内按 `ENGINE_PRIORITY` 排序（与 `_cmd_list` 一致）
- 筛选（引擎/状态/family）+ 搜索
- 行内操作按钮：启动/停止/重启/详情
- 点击行 → 右侧抽屉打开详情
- **状态列只读，靠 3s 轮询驱动**

#### 模型详情
- Tab: 基础信息 / 日志(SSE) / 用量(ECharts) / Agent 配置参考
- 日志 tab 接 SSE 尾随，自动滚动，关键词过滤
- 显示 `agent_config_ref`（上下文/视觉/采样/Token 速率）
- YAML 只读 + 下载

#### 服务矩阵
- stats/gateway 两张 ServiceCard 各自启停
- 家族路由预览（读 `build_groups()`）

#### 一键启停
- 默认模型下拉（预填 `GATEWAY_DEFAULT_MODEL`）
- 超时/GPU 选择
- 启动前弹确认列将启 profile
- 任务流逐条推 model→gateway→stats
- 失败项标红

#### 环境管理
- 7 行表格（6 托管引擎 + gateway）
- 平台不支持时 disabled + tooltip
- setup 走 3 步向导（确认→进度SSE→完成）
- remove 红色二次确认

#### 硬件体检
- 五区块：GPU 概览 / 引擎二进制(带安装提示) / 托管 venv / site-packages / 关键 env
- 右上"重新体检"手动触发

#### 审计日志
- 过滤条：时间/模型/端点/限制/JSON
- 虚拟滚动表格（列对齐 `_format_audit_table`）
- 清理走 dry-run 预览再确认

#### 配置中心
- nginx 片段生成：`NODE_ID`/`NODE_HOST` 预填 → 实时预览 → 复制
- `.env` 关键变量只读展示（key 脱敏）

#### 设置/帮助
- API Key 脱敏显示 + 版本信息 + 文档链接

#### 新手引导
- 首次登录（无 localStorage token）→ 三步引导
- Step1: 环境检查（自动 probe + 缺失引擎安装提示）
- Step2: 选择并启动第一个模型
- Step3: 展示 API 端点 + curl 示例

## 5. 组件规范

### 5.1 核心组件

| 组件 | 职责 | 关键 Props |
|---|---|---|
| StatusBadge | 状态徽章（四态） | `state: running/pid_error/external/stopped` |
| GpuCard | GPU 显存条 + 占用方 | `gpu: {index, name, used, total, temp}` |
| ModelCard | 移动端模型卡片 | `model: ModelSummary` |
| ServiceCard | 服务启停卡 | `service: {name, port, state}` |
| OperationButton | 统一操作按钮 | `type: start/stop/restart`, `loading`, `disabled` |
| LongTaskModal | 长任务进度弹窗 | `task: Task`, `onClose` |
| TaskTray | 底部全局任务托盘 | `tasks: Task[]` |
| LogViewer | 终端日志查看器 | `stream: SSE`, `lines: number`, `filter: string` |
| UsageMetrics | ECharts 用量图表 | `model: string`, `data: MetricsPoint[]` |
| AuditTable | 审计表格 | `rows: AuditEntry[]`, `filters: AuditFilter` |
| StepWizard | 多步向导 | `steps: Step[]`, `current: number` |
| CopyButton | 复制按钮 | `text: string` |
| FamilyRouting | 家族路由可视化 | `groups: Group[]` |

### 5.2 色彩方案（深色默认）

| Token | 值 | 用途 |
|---|---|---|
| bg-primary | `#1E1E2E` | 主背景 |
| bg-card | `#313244` | 卡片/表格背景 |
| bg-hover | `#45475A` | 悬停 |
| primary | `#CBA6F7` | 主色调 |
| success | `#A6E3A1` | 成功/运行中 |
| warning | `#F9E2AF` | 警告/加载中 |
| error | `#F38BA8` | 错误/异常 |
| info | `#89B4FA` | 链接/按钮 |
| text-primary | `#CDD6F4` | 主文字 |
| text-secondary | `#A6ADC8` | 次级文字 |
| text-disabled | `#6C7086` | 禁用/灰 |
| border | `#585B70` | 边框 |
| divider | `#45475A` | 分割线 |

### 5.3 交互反馈规范

| 场景 | 表现 |
|---|---|
| 加载 | 按钮 loading 态 + spinner；表格 skeleton |
| 成功 | `message.success("XX 已启动")`，2s 消失 |
| 失败 | `message.error("XX 失败: {原因}")`，红色高亮 |
| 长任务 | 底部 TaskTray 进度条 + 步骤 + 可取消 |
| 确认 | 危险操作弹 `dialog.warning` + 红色确认按钮 |
| 空状态 | 友好提示 + CTA 按钮 |
| 错误横幅 | 页面顶部红横幅 + 修复建议 |
| 状态变更 | 状态点颜色渐变（0.3s transition） |

## 6. API 端点设计

前缀 `/admin/api`，除 `/login`/`/health` 外全部需要 `Authorization: Bearer <API_KEY>`。

### 6.1 认证 & 系统

| 方法 | 路径 | 请求 | 响应 | 说明 |
|---|---|---|---|---|
| POST | `/login` | `{key: string}` | `{ok: true}` / 401 | Bearer Token 校验 |
| GET | `/health` | — | `{ok, version, uptime_s, default_model}` | 健康检查 |
| GET | `/overview` | — | `OverviewResponse` | 3s 轮询聚合 |

`OverviewResponse`:
```json
{
  "gpus": [{"index": 0, "name": "NVIDIA A100", "used_vram": 4.2, "total_vram": 80, "temp": 62}],
  "gpu_locks": {"0": {"owner": "qwen3.8-vllm", "pid": 12345, "acquired_at": "10:20:01"}},
  "engines": {"vllm": "available", "sglang": "missing", ...},
  "models": [ModelSummary...],
  "services": {"stats": {"state": "running", "port": 5002}, "gateway": {"state": "running", "port": 5003}},
  "default_model": "qwen3.8",
  "probed_at": "2026-09-02T10:23:45Z"
}
```

`ModelSummary`:
```json
{
  "name": "qwen3.8-vllm",
  "group": "qwen3.8",
  "engine": "vllm",
  "variant": "",
  "port": 8101,
  "aliases": ["qwen3.8"],
  "state": "running|stopped|pid_error|external",
  "health": "healthy|unhealthy|unknown",
  "rates": {"prompt": 1234.5, "predicted": 45.6, "ttft_ms": 80, "source": "native"},
  "api_key_masked": "***3876",
  "pid": 12345,
  "log_path": "/path/to/launch-qwen3.8-vllm.log"
}
```

### 6.2 模型

| 方法 | 路径 | 请求 | 响应 | 说明 |
|---|---|---|---|---|
| GET | `/models` | — | `{groups: ModelGroup[]}` | 按 group 分组 |
| GET | `/models/{n}` | — | `ModelDetail` | 详情 + engine_config |
| POST | `/models/{n}/start` | `{timeout?: 600, gpus?: string}` | `202 {task_id, stream_url}` | 异步启动 |
| POST | `/models/{n}/stop` | — | `200 {ok, detail}` | 同步停止 |
| POST | `/models/{n}/restart` | `{timeout?: 600, gpus?: string}` | `202 {task_id}` | 异步重启 |
| GET | `/models/{n}/log` | `?lines=200` | `{lines: string[]}` | 历史日志 |
| GET | `/models/{n}/log/stream` | — | SSE | 实时尾随 |
| GET | `/models/{n}/yaml` | — | `{content: string}` | 原始 YAML |
| POST | `/models/{n}/purge-stale` | — | `{ok, detail}` | 清理残留 |
| POST | `/models/{n}/ui/start` | `{port?, host?, allow_from?}` | `{ok}` / 412 | unsloth UI 启动 |
| POST | `/models/{n}/ui/stop` | — | `{ok}` | unsloth UI 停止 |

### 6.3 服务 / 一键

| 方法 | 路径 | 请求 | 响应 | 说明 |
|---|---|---|---|---|
| GET | `/services` | — | `{stats, gateway, family_routing}` | 服务状态 |
| POST | `/services/{svc}/start` | — | `202 {task_id}` | 启动 stats/gateway |
| POST | `/services/{svc}/stop` | — | `200 {ok}` | 停止 |
| POST | `/services/{svc}/restart` | — | `202 {task_id}` | 重启 |
| POST | `/all/start` | `{model?, timeout?, gpus?}` | `202 {task_id}` | 一键启动 |
| POST | `/all/stop` | — | `200 {stopped: string[]}` | 一键停止 |
| POST | `/all/restart` | `{model?, timeout?, gpus?}` | `202 {task_id}` | 一键重启 |
| GET | `/all/status` | — | `{model, gateway, stats}` | 一键状态 |

### 6.4 环境 / 体检 / 审计 / 配置

| 方法 | 路径 | 请求 | 响应 | 说明 |
|---|---|---|---|---|
| GET | `/envs` | — | `{targets: EnvTarget[]}` | 环境状态 |
| POST | `/envs/{t}/setup` | — | `202 {task_id}` | 异步安装 |
| POST | `/envs/{t}/remove` | — | `200 {ok}` | 同步移除 |
| GET | `/probe` | — | `ProbeData` | 硬件探测 |
| GET | `/audit` | `?since&model&endpoints&limit&json` | `{entries: AuditEntry[]}` | 审计查询 |
| GET | `/audit/stats` | — | `{total, errors, by_model}` | 审计统计 |
| POST | `/audit/cleanup` | `{dry_run?: true}` | `{deleted: string[], size_mb}` | 清理 |
| GET | `/audit/path` | — | `{path: string}` | 审计目录 |
| GET | `/nginx-snippet` | `?node&host` | `{content: string}` | nginx 片段 |
| GET | `/config/static` | — | `{vars: Record<string,string>}` | .env 只读 |
| POST | `/trtllm/{n}/build` | — | `202 {task_id}` | 异步编译 |
| GET | `/trtllm/{n}/status` | — | `{built, engine_dir, files}` | 编译状态 |

### 6.5 任务

| 方法 | 路径 | 请求 | 响应 | 说明 |
|---|---|---|---|---|
| GET | `/tasks` | — | `{tasks: Task[]}` | 最近 200 条 |
| GET | `/tasks/{id}` | — | `Task` | 任务快照 |
| GET | `/tasks/{id}/stream` | — | SSE | 实时进度 |

`Task`:
```json
{
  "id": "task-1234",
  "kind": "model_start|service_start|all_start|env_setup|trtllm_build",
  "action": "start|stop|restart|setup|remove|build",
  "target": "qwen3.8-vllm|stats|gateway|all|sglang",
  "status": "queued|running|success|skipped|error",
  "exit_code": 0,
  "started_at": "2026-09-02T10:20:01Z",
  "finished_at": null,
  "detail": null,
  "logs": ["[10:20:01] 初始化 vllm serve...", "[10:22:35] 服务就绪..."]
}
```

SSE 事件格式:
```
event: step
data: {"step": 1, "label": "启动模型 qwen3.8-vllm", "status": "started"}

event: log
data: {"line": "INFO 19:20:01 初始化 vllm serve ..."}

event: step
data: {"step": 1, "label": "启动模型 qwen3.8-vllm", "status": "success", "duration_s": 154}

event: done
data: {"status": "success", "exit_code": 0}
```

### 6.6 错误码

| code | HTTP | 说明 | 对应 CLI |
|---|---|---|---|
| `auth` | 401 | 认证失败 | — |
| `not_found` | 404 | 模型/服务不存在 | ProfileError |
| `config_error` | 422 | 配置错误 | exit 2 (RequirementError) |
| `timeout` | 504 | 健康检查超时 | exit 1 |
| `early_exit` | 502 | 进程早退 | exit 1 |
| `port_in_use` | 409 | 端口占用 | — |
| `gpus_invalid` | 422 | GPU 索引无效 | RequirementError |
| `unsupported_engine` | 412 | 不支持的引擎操作 | — |
| `env_not_created` | 412 | 环境未安装 | EngineEnvError |
| `task_conflict` | 409 | 同一 profile 已有进行中任务 | — |

错误响应体:
```json
{"error": {"message": "xx", "code": "config_error", "exit_code": 2}}
```

## 7. 实时刷新策略

### 7.1 三层轮询/SSE

| 层 | 方式 | 频率 | 触发条件 |
|---|---|---|---|
| 状态轮询 | GET `/overview` | 3s | AppLayout 挂载时启动，卸载时清 |
| 长任务 SSE | GET `/tasks/{id}/stream` | 按需 | 提交长任务后打开，完成/断线关闭 |
| 日志 SSE | GET `/models/{n}/log/stream` | 按需 | 模型详情日志 tab 打开时 |

### 7.2 SSE 实现要点

- `StreamingResponse(media_type="text/event-stream")` + 异步生成器
- 每客户端独立 `asyncio.Queue`
- `to_thread` 内用 `loop.call_soon_threadsafe(q.put, ev)` 跨线程投事件
- 每 10s 推 `event: heartbeat` 保活（防 600s 冷启动被代理掐断）
- 断线 → 5s 轮询 `GET /tasks/{id}` 兜底校对；连续 3 次断线彻底停

### 7.3 轮询矩阵

| 页面 | 3s 轮询 | SSE |
|---|---|---|
| 仪表板 | ✓ | — |
| 模型列表 | ✓ | — |
| 模型详情 | ✓ | 日志 tab 开时 ✓ |
| 服务矩阵 | ✓ | — |
| 一键启停 | ✓ | 任务进行中 ✓ |
| 环境管理 | — | setup 时 ✓ |
| 硬件体检 | — | — |
| 审计日志 | — | — |
| 配置中心 | — | — |
| 设置 | — | — |

## 8. 新手引导流程

### 8.1 触发条件

首次登录（localStorage 无 `modelctl_webui_onboarded` 标记）→ 自动跳转 `/onboarding`

### 8.2 三步流程

```
Step 1: 环境检查
  - 自动执行 GET /probe
  - 展示 GPU 型号/数量/显存
  - 引擎二进制状态（✓/✗ + 安装提示）
  - 托管 venv 状态
  - [继续 →]

Step 2: 启动第一个模型
  - 展示可用模型列表（按显存预估排序）
  - 选择模型
  - 确认启动（可选 GPU/timeout）
  - 长任务进度（SSE）
  - [继续 →]

Step 3: 访问 API
  - 显示 API 端点 (http://host:5003/v1)
  - 模型名称
  - API Key 脱敏
  - curl 测试命令（可复制）
  - [开始使用 →] → 标记 onboarded，跳转仪表板
```

### 8.3 空状态引导

- 仪表板无运行模型: "暂无运行中的模型" + [▶ 启动模型] + [一键启动]
- 模型列表空: "暂无模型配置" + 指向 models/ 目录
- 审计日志空: "暂无审计记录"

## 9. 响应式设计

| 断点 | 布局 |
|---|---|
| ≥1200px | 侧边栏 240px + 内容区，表格全列 |
| 768-1199px | 侧边栏 64px（图标 only）+ 内容区，表格省略低优先级列 |
| <768px | 侧边栏隐藏→底部导航栏 5 项（仪表板/模型/一键/审计/更多），表格→卡片，KPI 4列→2列 |

- 触摸目标 ≥ 44px
- ECharts 响应式 resize
- 表格水平滚动 fallback

## 10. 安全

### 10.1 认证

- API_KEY 即 Bearer Token，无 exp
- `hmac.compare_digest(token, os.environ["API_KEY"])` 恒定时间比较
- 登录端点只做存在性校验，不签 JWT
- 登出纯前端删 localStorage
- 401 → 前端拦截器自动跳 `/login`

### 10.2 数据脱敏

- API Key 仅暴露末 4 位: `***3876`
- `.env` 敏感变量值脱敏显示
- 日志内容不做脱敏（与 CLI 一致）

### 10.3 CORS

- 单进程同源，`allow_origins=[]`
- 未来拆前端才加白名单

### 10.4 nginx 生产配置

```nginx
# B 机 nginx — Web UI
location /admin {
    proxy_pass http://127.0.0.1:4173;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_buffering off;
    proxy_read_timeout 0;
}

# 现有推理代理（不变）
location /<node>/llm/v1/ {
    proxy_pass http://NODE_HOST:5003/v1/;
    ...
}
```

## 11. 实施分阶段

| 阶段 | 内容 | 产出 |
|---|---|---|
| P0 后端骨架 | `admin_router` + 鉴权 + `/overview` + `/models`(只读) + `/tasks` 任务流 PoC（挑 `model.start` 跑通 SSE）+ 静态 mount + `modelctl webui` 子命令 | 后端 API 可 curl 验证 |
| P1 核心前端 | Vue3 项目搭建 + 登录 + 仪表板 + 模型列表(含行内操作) + 模型详情(含日志SSE) + 一键启停 | 核心功能可用 |
| P2 完整功能 | 服务矩阵 + 环境管理 + trtllm + 审计 + 体检 + 配置 + 设置 + 新手引导 | 全部 12 子命令图形化 |
| P3 打磨 | 深色主题对齐 + ECharts 用量图表 + 响应式 + 错误码全量对齐 + 交互规范完善 | 视觉/交互质量提升 |
| P4 可选(v2) | 受控 YAML 编辑 / i18n / 多节点 / RBAC / WebSocket | 后续迭代 |

## 12. 开放问题

| # | 议题 | 决策 |
|---|---|---|
| A | 模型 CRUD 范围 | v1 只读 + 行内操作 + YAML 下载；受控编辑延后 v2 |
| B | 默认模型语义 | 一键启停默认用 `GATEWAY_DEFAULT_MODEL`（族名），UI 可选具体 profile |
| C | 实时刷新 | 3s 轮询 + 按需 SSE，不用 WebSocket |
| D | 前端目录 | 新增顶层 `web/`，后端复用 gateway venv |
| E | 管理 API 位置 | `src/modelctl/core/webui/` 子包，在 `create_app()` 中 include_router |
| F | WebUI 启动方式 | `modelctl webui [--port 4173]` 子命令 |
| G | i18n | v1 全中文（复用 CLI 中文状态词），v2 再上 vue-i18n |
