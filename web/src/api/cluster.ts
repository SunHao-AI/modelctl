import client, { dataOf } from './client';

/** 与后端 admin_cluster.node_view 对齐（node_token 永不下发，仅 mask） */
export interface NodeView {
  node_id: string;
  lan_id: string | null;
  role: string;
  host_ip: string | null;
  hostname: string | null;
  engines: Record<string, string | null> | null;
  status: 'online' | 'stale' | 'offline' | 'disabled';
  token_mask: string;
  since_seen_s: number | null;
  lease_left_s: number | null;
}

export interface ClusterStatus {
  role: string;
  is_center: boolean;
  nodes_total: number;
  nodes_online: number;
}

export function getClusterStatus() {
  return dataOf(client.get<ClusterStatus>('/cluster/status'));
}

export function getClusterNodes() {
  return dataOf(client.get<{ nodes: NodeView[] }>('/cluster/nodes'));
}
