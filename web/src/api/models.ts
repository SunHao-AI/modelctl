import client, { dataOf } from './client';
import type {
  ActionResponse,
  GetLog,
  ModelDetail,
  ModelsListResponse,
  TaskRef,
  YamlResponse,
} from './types';

/** 异步操作（start / restart）参数：超时秒 + GPU 列表（逗号串） */
export interface ModelActionOpts {
  /** 等待健康超时秒数（1 ~ 3600，默认 600） */
  timeout?: number;
  /** GPU 列表（逗号串，例如 "0,1"） */
  gpus?: string;
}

/** 列出所有模型（按 group 聚合） + 默认模型名。 */
export function listModels(): Promise<ModelsListResponse> {
  return dataOf<ModelsListResponse>(client.get('/models'));
}

/** 取模型详情（含 engine 配置）。 */
export function getModel(name: string): Promise<ModelDetail> {
  return dataOf<ModelDetail>(client.get(`/models/${encodeURIComponent(name)}`));
}

/** 同步停止模型（快速，不走任务流）。 */
export function stopModel(name: string): Promise<ActionResponse> {
  return dataOf<ActionResponse>(client.post(`/models/${encodeURIComponent(name)}/stop`));
}

/** 启动模型（异步 202 + task_id + stream_url）。 */
export function startModel(name: string, opts?: ModelActionOpts): Promise<TaskRef> {
  return dataOf<TaskRef>(
    client.post(`/models/${encodeURIComponent(name)}/start`, null, {
      params: { timeout: opts?.timeout, gpus: opts?.gpus },
    }),
  );
}

/** 重启模型（异步 202 + task_id + stream_url）。 */
export function restartModel(name: string, opts?: ModelActionOpts): Promise<TaskRef> {
  return dataOf<TaskRef>(
    client.post(`/models/${encodeURIComponent(name)}/restart`, null, {
      params: { timeout: opts?.timeout, gpus: opts?.gpus },
    }),
  );
}

/** 读取模型启动日志尾部 N 行。 */
export function getModelLog(name: string, lines = 200): Promise<GetLog> {
  return dataOf<GetLog>(
    client.get(`/models/${encodeURIComponent(name)}/log`, { params: { lines } }),
  );
}

/** 模型日志 SSE 地址（前端用 EventSource 订阅）。 */
export function getModelLogStreamUrl(name: string): string {
  return `/admin/api/models/${encodeURIComponent(name)}/log/stream`;
}

/** 取模型 YAML 文本（含 path）。 */
export function getModelYaml(name: string): Promise<YamlResponse> {
  return dataOf<YamlResponse>(client.get(`/models/${encodeURIComponent(name)}/yaml`));
}

/** 启动 Unsloth Web 管理控制台（仅 engine=unsloth）。 */
export function startModelUi(name: string): Promise<{ ok: boolean; detail?: string }> {
  return dataOf<{ ok: boolean; detail?: string }>(
    client.post(`/models/${encodeURIComponent(name)}/ui/start`),
  );
}

/** 停止 Unsloth Web 管理控制台（仅 engine=unsloth）。 */
export function stopModelUi(name: string): Promise<{ ok: boolean; detail?: string }> {
  return dataOf<{ ok: boolean; detail?: string }>(
    client.post(`/models/${encodeURIComponent(name)}/ui/stop`),
  );
}
