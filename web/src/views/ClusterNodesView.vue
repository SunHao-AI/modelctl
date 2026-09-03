<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { getClusterNodes, getClusterStatus, type NodeView, type ClusterStatus } from '@/api/cluster';
import { AxiosError } from 'axios';

const status = ref<ClusterStatus | null>(null);
const nodes = ref<NodeView[]>([]);
const disabled = ref(false); // 404 = 未启用集群角色（solo/worker）
const error = ref('');
let timer: number | undefined;

const STATUS_STYLE: Record<string, string> = {
  online: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  stale: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  offline: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
  disabled: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
};

async function refresh() {
  try {
    status.value = await getClusterStatus();
    nodes.value = (await getClusterNodes()).nodes;
    disabled.value = false;
    error.value = '';
  } catch (e) {
    if ((e as AxiosError).response?.status === 404) {
      disabled.value = true;
      return;
    }
    error.value = (e as Error).message;
  }
}

function fmtAge(s: number | null): string {
  return s === null ? '-' : s < 60 ? `${s.toFixed(0)}s` : `${(s / 60).toFixed(1)}m`;
}

onMounted(() => {
  refresh();
  timer = window.setInterval(refresh, 5000);
});
onBeforeUnmount(() => window.clearInterval(timer));
</script>

<template>
  <div class="p-6">
    <div class="mb-4 flex items-center justify-between">
      <h1 class="text-lg font-semibold text-slate-100">集群节点</h1>
      <div v-if="status" class="text-sm text-slate-400">
        角色 {{ status.role }} · {{ status.nodes_online }}/{{ status.nodes_total }} online
      </div>
    </div>

    <div v-if="disabled" class="rounded-lg border border-slate-700 bg-slate-800/50 p-6 text-sm text-slate-400">
      当前节点未启用集群角色。中心机请在 .env 设置 CLUSTER_ROLE=both 后重启 webui，
      并执行 <code class="text-blue-400">modelctl cluster init</code>。
    </div>

    <div v-else-if="error" class="rounded-lg border border-rose-800 bg-rose-900/30 p-4 text-sm text-rose-300">
      {{ error }}
    </div>

    <table v-else class="w-full text-left text-sm">
      <thead class="text-slate-400">
        <tr class="border-b border-slate-700">
          <th class="py-2 pr-4">节点</th>
          <th class="py-2 pr-4">LAN</th>
          <th class="py-2 pr-4">角色</th>
          <th class="py-2 pr-4">状态</th>
          <th class="py-2 pr-4">最后心跳</th>
          <th class="py-2 pr-4">租约剩余</th>
          <th class="py-2">主机</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="n in nodes" :key="n.node_id" class="border-b border-slate-800 text-slate-200">
          <td class="py-2 pr-4 font-mono">{{ n.node_id }}</td>
          <td class="py-2 pr-4 text-slate-400">{{ n.lan_id || '-' }}</td>
          <td class="py-2 pr-4">{{ n.role }}</td>
          <td class="py-2 pr-4">
            <span class="rounded border px-2 py-0.5 text-xs" :class="STATUS_STYLE[n.status] || STATUS_STYLE.offline">
              {{ n.status }}
            </span>
          </td>
          <td class="py-2 pr-4">{{ fmtAge(n.since_seen_s) }}</td>
          <td class="py-2 pr-4">{{ n.lease_left_s === null ? '-' : fmtAge(Math.max(n.lease_left_s, 0)) }}</td>
          <td class="py-2 text-slate-400">{{ n.hostname || n.host_ip || '-' }}</td>
        </tr>
        <tr v-if="!nodes.length">
          <td colspan="7" class="py-6 text-center text-slate-500">暂无节点，等待 worker join…</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
