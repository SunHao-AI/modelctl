import client, { dataOf } from './client';
import type { NginxSnippetResponse, StaticConfigResponse } from './types';

/** 生成 nginx map 路由片段（按 node/host 参数）。 */
export function nginxSnippet(node: string, host: string): Promise<NginxSnippetResponse> {
  return dataOf<NginxSnippetResponse>(
    client.get('/nginx-snippet', { params: { node, host } }),
  );
}

/** 后端静态配置：版本 / 默认模型 / 端口 / 路径。 */
export function staticConfig(): Promise<StaticConfigResponse> {
  return dataOf<StaticConfigResponse>(client.get('/config/static'));
}
