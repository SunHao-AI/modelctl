import type { ModelInfo } from '@/api/types';

/** 可选模型 = 已启动且健康；口径与后端 `_model_summary` 的 state/health 一致。 */
export function pickRunnableModels(models: ModelInfo[]): ModelInfo[] {
  return models.filter((x) => x.state === 'running' && x.health === 'healthy');
}

/** 把 listModels 的分组响应摊平成数组。 */
export function flattenGroups(resp: { groups: { models: ModelInfo[] }[] }): ModelInfo[] {
  return resp.groups.flatMap((g) => g.models);
}
