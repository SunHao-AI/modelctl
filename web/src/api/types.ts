/**
 * 后端 /admin/api 响应类型定义
 * 字段口径以 src/modelctl/core/webui/*.py 真正实现为准，避免凭空 mock。
 */

/** 同步动作（stop / remove 等）的通用响应 */
export interface ActionResponse {
  /** 是否成功（status != "error"） */
  ok: boolean;
  /** 后端附带说明（组件名 / 详情） */
  detail?: string;
}

/** 异步操作（start / restart / setup 等）提交后返回的句柄 */
export interface TaskRef {
  /** 任务 id（task-xxxxxxxx） */
  task_id: string;
  /** SSE 订阅地址：/admin/api/tasks/{task_id}/stream */
  stream_url: string;
}

/** 模型状态：运行中 / 已停止 */
export type ModelState = 'running' | 'stopped';
/** 模型健康：健康 / 不健康 / 未探测 / 未知 */
export type ModelHealth = 'healthy' | 'unhealthy' | 'unknown' | null;

/** 模型摘要（列表项 / 总览用） */
export interface ModelInfo {
  /** 模型名（唯一） */
  name: string;
  /** 家族 group（可能为空） */
  group: string | null;
  /** 推理引擎（vllm/sglang/unsloth/ollama/tensorrt_llm/...） */
  engine: string;
  /** 变体 / 用途标签（可空） */
  variant: string | null;
  /** 监听端口（0 = 动态） */
  port: number;
  /** 别名列表 */
  aliases: string[];
  /** 进程状态：running / stopped */
  state: ModelState;
  /** 健康：healthy / unhealthy / unknown / null */
  health: ModelHealth;
  /** 速率统计（暂未实现，恒为 null） */
  rates: null;
  /** 脱敏后的 API Key（***xxxx），null 表示未配置 */
  api_key_masked: string | null;
  /** 进程 PID，未运行则 null */
  pid: number | null;
  /** 启动日志路径，可空 */
  log_path: string | null;
}

/** 模型详情（含 engine 配置与 profile 字段） */
export interface ModelDetail extends ModelInfo {
  /** 引擎配置（含 engine_config 原始结构，可能含 api_key 脱敏值） */
  engine_config: Record<string, unknown>;
  /** 模型路径（engine_config.model） */
  model_path: string | null;
  /** 多轮 tool_call 上限 */
  tool_call_rounds: number | null;
  /** 最大输出 token */
  max_output_tokens: number | null;
  /** 用途说明 */
  usage: string | null;
  /** 是否禁用 thinking */
  thinking_disabled: boolean | null;
}

/** 模型列表分组 */
export interface ModelsGroup {
  /** 分组名（未声明 group 的聚成 "(其它)"） */
  group: string;
  /** 组内模型列表 */
  models: ModelInfo[];
}
/** GET /models 响应 */
export interface ModelsListResponse {
  groups: ModelsGroup[];
  /** 默认模型名（来自 GATEWAY_DEFAULT_MODEL） */
  default_model: string;
}

/** 日志拉取响应 */
export interface GetLog {
  /** 日志文件路径 */
  path: string;
  /** 最近 N 行（最新行在最后） */
  lines: string[];
}

/** YAML 响应 */
export interface YamlResponse {
  /** YAML 路径 */
  path: string;
  /** 完整 YAML 文本 */
  content: string;
}

/** 健康检查 */
export interface HealthInfo {
  ok: boolean;
  version: string;
  /** 后端进程存活秒数 */
  uptime_s: number;
  /** 默认模型 */
  default_model: string;
  /** 网关端口 */
  gateway_port: number;
}

/** 服务三态状态 */
export type ServiceState = 'running' | 'stopped' | 'error';

/** 服务名（stats / gateway） */
export type ServiceKey = 'stats' | 'gateway';

/** 单个服务信息（stats / gateway） */
export interface ServiceInfo {
  state: ServiceState;
  port: number;
  /** 后端状态说明（如 PID / 占用端口的进程名） */
  detail?: string;
}

/** 家人路由成员 */
export interface FamilyMember {
  /** 模型名 */
  name: string;
  /** 引擎 */
  engine: string;
  /** 引擎优先级（小者优先） */
  priority: number;
  /** 当前是否 running */
  running: boolean;
}

/** GET /services 响应 */
export interface ServicesResponse {
  /** stats 端口（USAGE_PORT） */
  stats: ServiceInfo;
  /** gateway 端口（GATEWAY_PORT） */
  gateway: ServiceInfo;
  /** 家族路由（group → 成员） */
  family_routing: Record<string, FamilyMember[]>;
  /** 默认模型名 */
  default_model: string;
}

/** 一键状态汇总 */
export interface AllStatusResponse {
  components: Array<{
    /** 组件名（stats/gateway 之外的还有 a:t-api/<model> 等） */
    component: string;
    /** ok / skipped / error */
    status: string;
    detail: string;
  }>;
}

/** 一键启停的可选参数 */
export interface AllActionOpts {
  /** 指定模型（缺省走 GATEWAY_DEFAULT_MODEL） */
  model?: string;
  /** 等待健康超时秒 */
  timeout?: number;
  /** GPU 列表（逗号串） */
  gpus?: string;
}

/** GET /overview 响应（3s 轮询聚合） */
export interface OverviewResponse {
  version: string;
  /** 后端运行秒数（可能为 null，由前端用本地时钟覆盖） */
  uptime_s: number | null;
  default_model: string;
  gateway_port: number;
  /** 模型总数 */
  model_count: number;
  /** 硬件 */
  hardware: {
    gpu_count: number;
    gpu_name: string;
    total_vram_gb: number;
    /** 引擎二进制：engine → "available"|"missing" */
    engine_binaries: Record<string, 'available' | 'missing'>;
  };
  /** 全部模型 */
  models: ModelInfo[];
  /** 服务信息（stats / gateway） */
  services: {
    stats: ServiceInfo;
    gateway: ServiceInfo;
  };
  /** 探测时间 ISO 字符串 */
  probed_at: string;
}

/** GPU 二进制 */
export interface EngineBinary {
  /** 引擎名（vllm / sglang / ...） */
  name: string;
  /** 是否可用 */
  available: boolean;
  /** 绝对路径（venv 内或 PATH），无则 null */
  path: string | null;
}

/** GPU 锁条目 */
export interface GpuLock {
  gpu_index: number;
  owner: string;
}

/** GET /probe 完整体检响应 */
export interface ProbeResponse {
  /** GPU 数 */
  gpu_count: number;
  /** GPU 型号 */
  gpu_name: string;
  /** 显存总量（MB） */
  vram_total_mb: number;
  /** 显存总量（GB，已四舍五入到 1 位） */
  vram_total_gb: number;
  /** 每卡空闲显存（MB 数组） */
  vram_free_mb: number[];
  /** CUDA 驱动版本 */
  cuda_driver: string;
  /** 计算能力（CC）版本 */
  compute_capability: string;
  /** GPU 锁列表 */
  gpu_locks: GpuLock[];
  /** 引擎二进制列表 */
  engine_binaries: EngineBinary[];
  /** 环境变量（值为脱敏后的字符串） */
  env_vars: {
    HF_HOME: string;
    MODEL_ROOT: string;
    MODELSCOPE_CACHE: string;
    LOG_DIR: string;
    API_KEY: string;
  };
  /** 关键路径 */
  paths: {
    project_root: string;
    cache_dir: string;
    models_dir: string;
  };
  /** modelctl 版本 */
  version: string;
}

/** 环境 target 信息（get /envs） */
export interface EnvTarget {
  /** 环境名（如 "vllm"、"gateway"） */
  name: string;
  /** 是否已安装 */
  installed: boolean;
  /** 描述文本（python 版本 / 包数量等） */
  detail: string;
  /** 当前运行平台是否支持建托管 venv（托管引擎仅 Linux；gateway 恒 true） */
  platform_supported: boolean;
  /** 适配器是否支持 docker_image 运行时（UI 据此决定能否走 Docker 旁路） */
  docker_supported: boolean;
}

/** Docker 环境就绪探测（PATH 级，无子进程） */
export interface DockerEnv {
  ready: boolean;
  /** 缺失项描述（docker CLI / toolkit），ready=true 时为空数组 */
  missing: string[];
  /** 缺失时的统一安装指引文案 */
  guide: string;
}

/** 单引擎 Docker 旁路指引（支持时带 steps，否则仅 note） */
export interface DockerBypassEntry {
  name: string;
  docker_supported: boolean;
  /** 官方镜像示例（如 vllm/vllm-openai:<tag>） */
  image_example?: string;
  /** yaml 字段路径（如 vllm.docker_image） */
  yaml_field_path?: string;
  /** 仓库内可直接参考的示例 yaml 路径 */
  example_yaml?: string;
  /** 三步操作指引 */
  steps?: string[];
  /** 不支持时的说明文案 */
  note?: string;
}

/** Docker 完整诊断（GET /envs/docker/diagnose） */
export interface DockerDiagnose {
  /** 当前诊断平台：linux / windows（Task 3 后端新增顶层字段） */
  platform: "linux" | "windows";
  checks: Array<{
    key: string;
    label: string;
    ok: boolean;
    detail: string;
    /** Windows 分支附带的修复提示；Linux 分支恒为 null */
    hint?: string | null;
  }>;
  /** 可复制到部署机 root shell 的安装脚本（Windows 分支为空字符串） */
  instructions: string;
}

/**
 * Docker 一键安装 SSE 事件（Task 1 后端 core/sse_stage_event.py 权威定义）
 * 所有阶段事件统一通过 task.event("stage", ev.to_sse_dict()) 广播。
 */
export type DockerSSEEventType = "stage" | "log" | "error" | "complete";

/**
 * 安装阶段：与后端 windows_setup.STAGES（12 值）严格对齐 + "unknown" 兜底
 * （`GET /envs/docker/install/{task_id}` 中 `task.detail` 未回写时后端返 "unknown"，
 * 前端渲染分支需容）。前端只消费字面量进行 UI 分派。
 */
export type DockerSSEStage =
  | "detect_winget"
  | "detect_wsl2"
  | "winget_running"
  | "need_UAC"
  | "winget_done"
  | "write_daemon_json"
  | "test_docker_version"
  | "test_gpus"
  | "post_install_plan"
  | "already_installed"
  | "done"
  | "error"
  | "unknown";

/** 阶段 B 用户引导步骤（payload.steps 的单项；action 枚举固定 4 值 + null） */
export interface PostInstallStep {
  /** 后端下发的稳定 id（与 style key 匹配） */
  id: string;
  /** 步骤标题（用户可见） */
  label: string;
  /** 可触发的系统动作；null 表示只读提示（如 "打开 Docker Desktop 并核对"） */
  action: "restart" | "open_desktop" | "verify" | null;
  /** CLI 命令提示（设计中：给运维参考复制用） */
  cmd_hint: string | null;
  /** 可选步骤标记（UI 加 "(可选)" 徽标） */
  optional?: boolean;
}

/** Docker 一键安装 SSE 单事件（data 载荷） */
export interface DockerSSEEvent {
  type: DockerSSEEventType;
  stage: DockerSSEStage;
  /** 对人看的消息文本（前端直接展示，不做二次格式化） */
  message: string;
  /** 后端已格式化的 YYYY-MM-DD HH:mm:ss，直接展示 */
  ts: string;
  /** 错误分类码（error 帧专属；后端 `core/sse_stage_event.py` 锁定 int，未必有） */
  code?: number;
  /**
   * - type=complete 且 stage=post_install_plan → { steps: PostInstallStep[] }
   * - type=error → { tail: string[] }（winget 失败最后 50 行）
   */
  payload?: { steps: PostInstallStep[] } | { tail: string[] };
}

/** POST /envs/docker/install 响应（202） */
export interface DockerInstallStartResponse {
  task_id: string;
  /** 完整 SSE 订阅地址（/admin/api/envs/docker/install/{task_id}/events） */
  events: string;
  os: "linux" | "windows";
  /** 5 分钟窗口内已有活跃 task，后端复用原 id 并显式标记 */
  already_running?: boolean;
}

/** 非托管引擎（ollama / unsloth / llamacpp）安装情况，仅用于说明，不可 setup/remove */
export interface UnmanagedTarget {
  name: string;
  /** 是否已安装（PATH 二进制或编译产物存在） */
  installed: boolean;
  /** 探测到的可执行文件路径，未安装为 null */
  path: string | null;
  /** 安装命令 */
  install_hint: string;
}

/** 审计日志条目（JSONL 一行，字段按网关 logs 实际产生的 schema） */
export interface AuditEntry {
  /** ISO 时间戳（顶层 time / ts 之一） */
  time?: string;
  ts?: string;
  timestamp?: string;
  /** 级别（info / warn / error） */
  level?: string;
  /** 模型名 / 客户端模型 */
  model?: string;
  /** 请求 endpoint（如 /v1/chat/completions） */
  endpoint?: string;
  /** HTTP 方法 */
  method?: string;
  /** HTTP 状态码 */
  status?: number;
  status_code?: number;
  /** 响应字节数 */
  size?: number;
  /** 请求耗时（ms 或 s，按后端口径） */
  cost?: number;
  /** 描述 / 摘要 */
  message?: string;
  /** 错误信息（仅失败请求） */
  error?: string;
  /** 其余自由字段 */
  [key: string]: unknown;
}

/** 审计查询参数 */
export interface AuditQueryParams {
  /** 相对时间：10m/1h/6h/24h/7d/30d */
  since?: string;
  /** 返回上限 */
  limit?: number;
  /** 级别过滤（all/info/warn/error） */
  level?: string;
  /** 关键字（在可索引字段上做 LIKE） */
  keyword?: string;
}

/** GET /audit 响应 */
export interface AuditListResponse {
  /** 实际生效的 since（已解析为本地时区 ISO） */
  since: string;
  /** 条目（新在前） */
  entries: AuditEntry[];
  /** 总条数（过滤后未截断） */
  total: number;
  /** 错误数 */
  error_count: number;
}

/** 审计日聚合 */
export interface AuditDayCount {
  /** 日期 YYYY-MM-DD */
  date: string;
  /** 总请求 */
  total: number;
  /** 错误请求 */
  error: number;
}

/** GET /audit/stats 响应 */
export interface AuditStatsResponse {
  total: number;
  /** 按日期聚合 */
  by_day: AuditDayCount[];
  /** 按模型聚合 */
  by_model: Record<string, number>;
}

/** POST /audit/cleanup 响应 */
export interface AuditCleanupResponse {
  ok: boolean;
  /** 删除的条数 */
  removed: number;
  /** 释放的字节数 */
  freed_bytes: number;
}

/** GET /nginx-snippet 响应 */
export interface NginxSnippetResponse {
  ok: boolean;
  /** 直接粘贴到 nginx 配置中的内容 */
  snippet: string;
}

/** GET /config/static 响应 */
export interface StaticConfigResponse {
  version: string;
  default_model: string;
  /** 网关端口 */
  port: number;
  /** 关键路径 */
  paths: Record<string, string>;
}

/** 任务状态（后端 TaskStatus） */
export type TaskStatus = 'queued' | 'running' | 'success' | 'skipped' | 'error';

/** 单条任务详情（GET /tasks/{id} 与 GET /tasks 列表项同构） */
export interface TaskInfo {
  /** 任务 id（task-xxxxxxxx） */
  id: string;
  /** 任务种类：model_start|service_start|env_setup|trtllm_build|all_start|... */
  kind: string;
  /** 动作：start|stop|restart|setup|remove|build|... */
  action: string;
  /** 目标（profile / service / env 名） */
  target: string;
  status: TaskStatus;
  /** 退出码；0=成功 */
  exit_code: number;
  /** ISO 时间戳；未开始为空串 */
  started_at: string;
  finished_at: string | null;
  /** 详情（进度描述 / 错误信息） */
  detail: string | null;
  /** 失败分类码（venv_missing|docker_missing）；未分类时后端不携带 */
  code?: string;
  /** 分类码关联的引擎名（修复动作定位 env target / 环境页 focus） */
  engine?: string;
  /** 内存环形日志（后端最多 500 行） */
  logs: string[];
}

/** 启动阶段名（与后端 STAGES 严格一致） */
export type StartupStageName = 'preflight' | 'prepare_env' | 'launch' | 'loading' | 'health';

/** 单阶段快照 */
export interface StartupStage {
  stage: StartupStageName;
  status: 'pending' | 'running' | 'done' | 'error';
  label: string;
  /** 0–1；null 表示不确定态（条纹动画） */
  pct: number | null;
  /** 预估剩余秒；null 表示首次运行无预估 */
  etaSeconds: number | null;
  error: string | null;
  startedAt: string | null;
  finishedAt: string | null;
}

/** GET /models/{name}/startup 响应 */
export interface StartupSnapshot {
  profile: string;
  engine: string;
  runtime: 'docker' | 'venv' | string;
  updatedAt: string;
  stages: StartupStage[];
  knownStages: StartupStageName[];
}
