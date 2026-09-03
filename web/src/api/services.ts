import client, { dataOf } from './client';
import type {
  ActionResponse,
  AllActionOpts,
  AllStatusResponse,
  HealthInfo,
  OverviewResponse,
  ProbeResponse,
  ServiceInfo,
  ServiceKey as _ServiceKey,
  ServicesResponse,
  TaskRef,
} from './types';

/**
 * 服务名（stats / gateway）—— 从 types 转发，免污染 types.ts
 */
export type ServiceKey = _ServiceKey;
/** 服务动作：start / stop / restart */
export type ServiceAction = 'start' | 'stop' | 'restart';

/** 健康检查（前端进入登录页前可探测）。 */
export function health(): Promise<HealthInfo> {
  return dataOf<HealthInfo>(client.get('/health'));
}

/** 总览聚合（3s 轮询一次）。 */
export function overview(): Promise<OverviewResponse> {
  return dataOf<OverviewResponse>(client.get('/overview'));
}

/** 服务列表（stats + gateway + 家族路由预览）。 */
export function servicesInfo(): Promise<ServicesResponse> {
  return dataOf<ServicesResponse>(client.get('/services'));
}

/** 一键启停状态汇总。 */
export function allStatus(): Promise<AllStatusResponse> {
  return dataOf<AllStatusResponse>(client.get('/all/status'));
}

/**
 * 服务动作：
 *  - stop：同步，返回 {ok, detail}
 *  - start / restart：异步，返回 202 + {task_id, stream_url}
 */
export async function serviceAction(
  svc: ServiceKey,
  action: ServiceAction,
): Promise<ActionResponse | TaskRef> {
  const res = action === 'stop'
    ? dataOf<ActionResponse>(client.post(`/services/${svc}/stop`))
    : dataOf<TaskRef>(client.post(`/services/${svc}/${action}`));
  return res;
}

/** 一键启动（异步 202 + task_id）。model / timeout / gpus 走 query。 */
export function allStart(opts?: AllActionOpts): Promise<TaskRef> {
  return dataOf<TaskRef>(
    client.post('/all/start', null, {
      params: { model: opts?.model, timeout: opts?.timeout, gpus: opts?.gpus },
    }),
  );
}

/** 一键停止（同步）。 */
export function allStop(): Promise<{ ok: boolean; stopped?: string[]; errors?: Array<{ component: string; detail: string }> }> {
  return dataOf<{ ok: boolean; stopped?: string[]; errors?: Array<{ component: string; detail: string }> }>(
    client.post('/all/stop'),
  );
}

/** 一键重启（异步 202 + task_id）。 */
export function allRestart(opts?: AllActionOpts): Promise<TaskRef> {
  return dataOf<TaskRef>(
    client.post('/all/restart', null, {
      params: { model: opts?.model, timeout: opts?.timeout, gpus: opts?.gpus },
    }),
  );
}

/** 完整硬件体检。 */
export function probe(): Promise<ProbeResponse> {
  return dataOf<ProbeResponse>(client.get('/probe'));
}

/** 仅返回服务信息（stats / gateway 单项）。 */
export function singleServiceInfo(svc: ServiceKey): Promise<ServiceInfo> {
  return dataOf<ServicesResponse>(client.get('/services')).then((d) => d[svc]);
}
