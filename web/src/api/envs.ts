import client, { dataOf } from './client';
import type {
  ActionResponse,
  DockerBypassEntry,
  DockerDiagnose,
  DockerEnv,
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

/** Docker 环境完整诊断（只读，按需触发；内含 15s 级子进程探测，勿在列表加载时调用）。 */
export function dockerDiagnose(): Promise<DockerDiagnose> {
  return dataOf<DockerDiagnose>(client.get('/envs/docker/diagnose'));
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
