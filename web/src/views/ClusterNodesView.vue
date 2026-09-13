<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { getClusterNodes, getClusterStatus, type NodeView, type ClusterStatus } from '@/api/cluster';
import { AxiosError } from 'axios';

const status = ref<ClusterStatus | null>(null);
const nodes = ref<NodeView[]>([]);
const disabled = ref(false); // 404 = 未启用集群角色（solo/worker）
const error = ref('');
let timer: number | undefined;

/** 节点状态 → chip 类（卡片范式：状态在卡片头以 chip 呈现） */
const STATUS_STYLE: Record<string, string> = {
  online: 'border-ok-line bg-ok-bg text-ok',
  stale: 'border-warn-line bg-warn-bg text-warn',
  offline: 'border-sep bg-surface3 text-label2',
  disabled: 'border-danger-line bg-danger-bg text-danger',
};

// 轮询防重叠：pending 期间新的 tick 直接返回，避免同样的同 URL 请求被浏览器
// keep-alive 池 abort（见 docs/known-pitfalls/frontend/polling-overlap-err-aborted.md）
let pending: Promise<void> | null = null;

function refresh(): Promise<void> {
  if (pending) return pending;
  pending = (async () => {
    try {
      status.value = await getClusterStatus();
      nodes.value = (await getClusterNodes()).nodes;
      disabled.value = false;
      error.value = '';
    } catch (e) {
      const err = e as AxiosError & { isCancel?: boolean };
      // 静默 abort：UI 无影响，保留旧 data
      if (
        !!err?.isCancel ||
        err?.code === 'ECONNABORTED' ||
        /aborted|canceled|cancelled/i.test(err?.message ?? '')
      ) {
        return;
      }
      if (err?.response?.status === 404) {
        disabled.value = true;
        return;
      }
      error.value = (e as Error).message;
    } finally {
      pending = null;
    }
  })();
  return pending;
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
      <h1 class="text-lg font-semibold text-label">集群节点</h1>
      <div v-if="status" class="num text-sm text-label2">
        角色 {{ status.role }} · {{ status.nodes_online }}/{{ status.nodes_total }} online
      </div>
    </div>

    <div v-if="disabled" class="rounded-ctl border border-sep bg-surface3 p-6 text-sm text-label2">
      当前节点未启用集群角色。中心机请在 .env 设置 CLUSTER_ROLE=both 后重启 webui，
      并执行 <code class="text-accent">modelctl cluster init</code>。
    </div>

    <div v-else-if="error" class="rounded-ctl border border-danger-line bg-danger-bg p-4 text-sm text-danger">
      {{ error }}
    </div>

    <!-- 卡片网格：原表格 8 列（节点/LAN/角色/容量/状态/最后心跳/租约剩余/主机）逐列搬入，一列不丢 -->
    <div v-else class="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
      <router-link
        v-for="n in nodes"
        :key="n.node_id"
        :to="`/cluster/nodes/${encodeURIComponent(n.node_id)}`"
        class="block rounded-card border border-sep bg-surface2 p-3.5 transition-all duration-200 hover:-translate-y-px"
        style="box-shadow: var(--shadow-s)"
      >
        <div class="flex items-start gap-2">
          <div class="min-w-0 flex-1">
            <div class="truncate text-[13px] font-medium text-label" style="font-family: var(--mono)">{{ n.node_id }}</div>
            <div class="num mt-0.5 truncate text-[11.5px] text-label3" style="font-family: var(--mono)">{{ n.lan_id || '-' }}</div>
          </div>
          <span
            :class="[
              'inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium',
              STATUS_STYLE[n.status] || STATUS_STYLE.offline,
            ]"
          >
            <span class="stonedot bg-current" />{{ n.status }}
          </span>
        </div>
        <div class="mt-2.5 flex flex-wrap gap-x-3.5 gap-y-1">
          <span class="text-[11.5px] text-label3">角色
            <b class="font-medium text-label2">{{ n.role }}</b>
          </span>
          <span class="text-[11.5px] text-label3">容量
            <b class="num font-medium text-label2">{{ n.capacity_text }}</b>
          </span>
          <span class="text-[11.5px] text-label3">最后心跳
            <b class="num font-medium text-label2">{{ fmtAge(n.since_seen_s) }}</b>
          </span>
          <span class="text-[11.5px] text-label3">租约剩余
            <b class="num font-medium text-label2">{{ n.lease_left_s === null ? '-' : fmtAge(Math.max(n.lease_left_s, 0)) }}</b>
          </span>
          <span class="text-[11.5px] text-label3">主机
            <b class="num font-medium text-label2">{{ n.hostname || n.host_ip || '-' }}</b>
          </span>
        </div>
      </router-link>

      <div v-if="!nodes.length" class="col-span-full rounded-ctl border border-sep bg-surface3 p-6 text-center text-sm text-label3">
        暂无节点，等待 worker join…
      </div>
    </div>
  </div>
</template>
