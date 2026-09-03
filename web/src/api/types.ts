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
