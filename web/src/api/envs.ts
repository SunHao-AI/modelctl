import client, { dataOf } from './client';
import { useAuthStore } from '@/stores/auth';
import type {
  ActionResponse,
  DockerBypassEntry,
  DockerDiagnose,
  DockerEnv,
  DockerInstallStartResponse,
  DockerSSEStage,
  EnvTarget,
  TaskRef,
  UnmanagedTarget,
} from './types';

/** envTargets 响应（docker_* 为旁路指引与环境探测） */
export interface EnvTargetsResponse {
  targets: EnvTarget[];
  unmanaged?: UnmanagedTarget[];
  docker_env?: DockerEnv;
  docker_bypass?: DockerBypassEntry[];
}

/** 列出所有受管 venv target（managed engine + gateway）及其安装状态；unmanaged 为非托管引擎说明。 */
export function envTargets(): Promise<EnvTargetsResponse> {
  return dataOf<EnvTargetsResponse>(client.get('/envs'));
}

/**
 * Docker 环境完整诊断（只读，按需触发；内含 15s 级子进程探测，勿在列表加载时调用）。
 *
 * ``os`` 参数透传到后端 query（linux/windows 二选一）；缺省按后端 sys.platform。
 * Windows 主机上不传 os 默认走 windows 分支，Linux 上显式传 windows 会 400。
 */
export function dockerDiagnose(os?: 'linux' | 'windows'): Promise<DockerDiagnose> {
  return dataOf<DockerDiagnose>(
    client.get('/envs/docker/diagnose', os ? { params: { os } } : undefined),
  );
}

/**
 * 安装目标环境（任务流：202 + task_id）。
 * 该操作可能持续 ~28min（如 vllm / sglang 安装），
 * 由调用方订阅 SSE stream 跟踪进度。
 */
export function envSetup(target: string): Promise<TaskRef> {
  return dataOf<TaskRef>(client.post(`/envs/${encodeURIComponent(target)}/setup`));
}

/** 移除目标环境（同步）：底层 rmtree，几秒内完成。 */
export function envRemove(target: string): Promise<ActionResponse> {
  return dataOf<ActionResponse>(client.post(`/envs/${encodeURIComponent(target)}/remove`));
}

/**
 * 组装 EventSource 订阅地址：在 ``events`` 路径上追加 ``?key=<token>``。
 *
 * EventSource 浏览器 API 无法自定义 ``Authorization`` header，后端已提供
 * ``require_auth_or_query`` 的 ``?key=`` query 鉴权回退（与 tasks/models 既有
 * SSE 流同款约定，见 docs/known-pitfalls/backend/sse-named-events.md）。
 *
 * token 为空时原样返回 events 路径（会导致 401，交给调用方 onError 分支处理），
 * 不在 API 层做硬 abort，方便排查时区分"未登录"与"鉴权失败"。
 */
export function withSseAuthKey(eventsPath: string): string {
  const token = useAuthStore().token;
  if (!token) return eventsPath;
  const sep = eventsPath.includes('?') ? '&' : '?';
  return `${eventsPath}${sep}key=${encodeURIComponent(token)}`;
}

/**
 * 启动 Docker 一键安装（异步）：POST /envs/docker/install。
 *
 * - 成功返回 202 + ``{ task_id, events, os, already_running? }``；
 * - 5 分钟窗口内同用户活跃 task 直接复用原 id + ``already_running: true``；
 * - 非 Windows 主机 + ``os=windows`` → 400 platform_mismatch（浏览器跨机点按钮场景）；
 * - 用户未完成任务满 5 个 → 429 rate_limited。
 *
 * SSE 订阅不走 axios：EventSource 无法自定义 header，前端用 ``events`` 字段的
 * 完整绝对路径（已含 /admin/api/ 前缀）+ ``?key=<token>`` query 鉴权。
 *
 * 浏览器直接用 ``r.events`` 拼接 ``?key=`` 打开 EventSource（见 DockerInstallPanel）。
 */
export function startDockerInstall(body: {
  os?: 'linux' | 'windows';
  registry_mirrors?: string[];
  max_concurrent_downloads?: number;
}): Promise<DockerInstallStartResponse> {
  return dataOf<DockerInstallStartResponse>(client.post('/envs/docker/install', body));
}

/** Docker 一键安装状态只读探针（SSE 断线后前端兜底用）。 */
export function fetchDockerInstallStatus(taskId: string): Promise<{
  stage: DockerSSEStage;
  done: boolean;
  last_ts: string;
}> {
  return dataOf(
    client.get(`/envs/docker/install/${encodeURIComponent(taskId)}`),
  ) as Promise<{ stage: DockerSSEStage; done: boolean; last_ts: string }>;
}

/**
 * 阶段 B 引导的系统动作（open_desktop / restart）—— Windows-only。
 *
 * - ``open_desktop`` → 后端调 ``explorer.exe ms-settings:developers``；
 * - ``restart``      → 后端调度 ``shutdown.exe /r /t 5``（5 秒后执行）；
 * - ``verify``       → 405 永远走 GET /docker/diagnose 只读，禁止 POST 触发探测；
 * - 非 win32 主机    → 400 platform_mismatch。
 *
 * 返回值 ``executed`` 是后端实际触发的命令文本（成功时），供前端 toast 展示。
 */
export function systemActionWindows(action: 'open_desktop' | 'restart'): Promise<{
  executed: string;
  ts: string;
}> {
  return dataOf(client.post('/envs/docker/system-action', { action }));
}
