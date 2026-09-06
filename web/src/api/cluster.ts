import client, { dataOf, downloadBlob } from './client';

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
  capacity: Record<string, number> | null;
  capacity_text: string;
}

export interface ClusterStatus {
  role: string;
  is_center: boolean;
  nodes_total: number;
  nodes_online: number;
}

/** goal 视图行：与 admin_cluster._goal_view 逐键对齐（刻意不含 profile_yaml） */
export interface GoalView {
  goal_id: string;
  node_id: string;
  profile: string;
  engine: string;
  intent: string;
  stage: string;
  reason: string;
  error_class: string;
  target_role: string;
  profile_version: string;
  state: string;
  gpu: number[] | null;
  port: number | null;
  pid: number | null;
  created_at: string;
  updated_at: string;
  age_s: number | null;
}

/** model_states 原样行（worker 回流直存）：updated_at 是 epoch 秒且后端未格式化，前端是展示端 */
export interface ModelState {
  node_id: string;
  profile: string;
  state: string;
  gpu: number[] | null;
  port: number | null;
  pid: number | null;
  reason: string | null;
  endpoint_url: string | null;
  endpoint_ready: boolean | null;
  engine_version: string | null;
  gpu_util: number | null;
  metrics_p50_ms: number | null;
  last_probe_ms: number | null;
  error_class: string | null;
  updated_at: number | null;
}

/** GET /cluster/events 定版行：ts/text 均由后端单端拼装，前端零加工 */
export interface EventRow {
  ts: string;
  node_id: string;
  goal_id: string;
  kind: string;
  text: string;
}

/** GET /cluster/profiles 目录行：同名多引擎逐条（选边在 POST gate，前端只联动） */
export interface ProfileCatalogEntry {
  name: string;
  engine: string;
  display_name: string;
  version: string;
}

/** GET /cluster/settings 只读现值（零写端点；join token 仅脱敏） */
export interface ClusterSettings {
  role: string;
  center_url: string;
  heartbeat_interval_s: number;
  lease_s: number;
  reconcile_interval_s: number;
  max_snapshot_bytes: number;
  join_token_mask: string;
}

/** GET /cluster/nodes/{id}：台账视图 + goals + model_states 三段 */
export interface NodeDetail {
  node: NodeView;
  goals: GoalView[];
  model_states: ModelState[];
}

/** POST /cluster/goals 载荷（与 _GoalCreateBody 对齐；engine 空串=不选边） */
export interface GoalCreatePayload {
  profile: string;
  node_ids?: string[] | null;
  all_nodes?: boolean;
  intent?: string;
  create?: boolean;
  params?: Record<string, unknown> | null;
  env_overlay?: Record<string, string> | null;
  gpus?: string;
  engine?: string;
  lan_allow?: string[] | null;
  runtime_ref?: string | null;
  target_role?: string;
  dry_run?: boolean;
}

/** 创建响应：gate 的 skip 是 200+report 不是 HTTP 错误；report 是逐节点定版文本 */
export interface GoalCreateResult {
  created: number;
  skipped: number;
  errors: number;
  report: string;
  reason: string;
  goals: GoalView[];
}

/** PUT 载荷（与 _GoalUpdateBody 对齐：无 profile/node_ids——换目标=create+remove 两步） */
export interface GoalUpdatePayload {
  intent?: string;
  params?: Record<string, unknown>;
  env_overlay?: Record<string, string>;
  placement?: Record<string, unknown>;
  runtime_ref?: string;
  target_role?: string;
  profile_version?: string;
}

/** DELETE goal：removed/missing 是 goal_id 字符串列表（remove_goals 的聚合口径） */
export interface DeleteGoalResult {
  removed: string[];
  missing: string[];
}

export interface QueuedResult {
  queued: boolean;
}
export interface KickedResult {
  kicked: boolean;
}
/** rotate-token 一次性返回新明文（同 join-check 先例），界面只展示、不落任何存储 */
export interface RotateTokenResult {
  node_token: string;
  kicked: boolean;
  hint: string;
}
export interface RetireResult {
  removed: boolean;
  removed_goals: number;
}

export function getClusterStatus() {
  return dataOf(client.get<ClusterStatus>('/cluster/status'));
}

export function getClusterNodes() {
  return dataOf(client.get<{ nodes: NodeView[] }>('/cluster/nodes'));
}

export function getClusterNodeDetail(nodeId: string) {
  return dataOf(client.get<NodeDetail>(`/cluster/nodes/${encodeURIComponent(nodeId)}`));
}

export function listClusterGoals(params: { node_id?: string; profile?: string } = {}) {
  return dataOf(client.get<{ goals: GoalView[] }>('/cluster/goals', { params }));
}

export function createClusterGoal(payload: GoalCreatePayload) {
  return dataOf(client.post<GoalCreateResult>('/cluster/goals', payload));
}

export function updateClusterGoal(goalId: string, payload: GoalUpdatePayload) {
  return dataOf(client.put<{ goal: GoalView }>(`/cluster/goals/${encodeURIComponent(goalId)}`, payload));
}

export function deleteClusterGoal(goalId: string) {
  return dataOf(client.delete<DeleteGoalResult>(`/cluster/goals/${encodeURIComponent(goalId)}`));
}

export function retryGoal(goalId: string) {
  return dataOf(client.post<QueuedResult>(`/cluster/goals/${encodeURIComponent(goalId)}/retry`));
}

export function forceNodeSync(nodeId: string) {
  return dataOf(client.post<QueuedResult>(`/cluster/nodes/${encodeURIComponent(nodeId)}/sync`));
}

export function nodeModelVerb(nodeId: string, profile: string, verb: 'start' | 'stop' | 'restart') {
  return dataOf(
    client.post<QueuedResult>(
      `/cluster/nodes/${encodeURIComponent(nodeId)}/model/${encodeURIComponent(profile)}/${verb}`,
    ),
  );
}

export function disableNode(nodeId: string) {
  return dataOf(client.post<KickedResult>(`/cluster/nodes/${encodeURIComponent(nodeId)}/disable`));
}

export function enableNode(nodeId: string) {
  return dataOf(client.post<{ ok: boolean }>(`/cluster/nodes/${encodeURIComponent(nodeId)}/enable`));
}

export function rotateNodeToken(nodeId: string) {
  return dataOf(client.post<RotateTokenResult>(`/cluster/nodes/${encodeURIComponent(nodeId)}/rotate-token`));
}

export function kickNode(nodeId: string) {
  return dataOf(client.post<KickedResult>(`/cluster/nodes/${encodeURIComponent(nodeId)}/kick`));
}

export function retireNode(nodeId: string) {
  return dataOf(client.delete<RetireResult>(`/cluster/nodes/${encodeURIComponent(nodeId)}`));
}

export function listClusterEvents(params: { node_id?: string; kind?: string; limit?: number } = {}) {
  return dataOf(client.get<{ events: EventRow[] }>('/cluster/events', { params }));
}

export function listClusterProfiles() {
  return dataOf(client.get<{ profiles: ProfileCatalogEntry[] }>('/cluster/profiles'));
}

export function getClusterSettings() {
  return dataOf(client.get<ClusterSettings>('/cluster/settings'));
}

export function rotateJoinToken() {
  return dataOf(client.post<{ join_token: string }>('/cluster/join-tokens/rotate'));
}

/** 备份下载：后端附件名已带时间戳，这里只兜底；返回实际落盘文件名 */
export function downloadClusterBackup() {
  return downloadBlob('/cluster/backup', 'modelctl-cluster.db');
}
