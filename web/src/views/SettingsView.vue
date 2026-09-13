<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { health } from '@/api/services';
import { useAuthStore } from '@/stores/auth';
import ThemeSwitch from '@/components/common/ThemeSwitch.vue';
import { useThemeStore } from '@/stores/theme';
import { AxiosError } from 'axios';
import {
  downloadClusterBackup,
  getClusterSettings,
  rotateJoinToken,
  type ClusterSettings,
} from '@/api/cluster';

/**
 * 设置：
 *  - 版本展示（从 /admin/api/health 获取；它本身无 auth 约束，返回 version）
 *  - 后端端点显示（/admin/api）
 *  - 清除本地 token 按钮
 */
const auth = useAuthStore();
const router = useRouter();

const version = ref('');
const lastFetchAt = ref('');
const clearMessage = ref('');
const clearBusy = ref(false);
const theme = useThemeStore();

/** 拉取版本（health 无鉴权，本页挂载时主动调用） */
async function fetchVersion() {
  lastFetchAt.value = new Date().toLocaleString('zh-CN', { hour12: false });
  try {
    const r = await health();
    version.value = r.version || '未知';
  } catch (err) {
    console.warn('health 失败:', err);
    version.value = '（后端不可达）';
  }
}

/** 清除本地 token */
function onClearToken() {
  if (!auth.isLoggedIn) {
    clearMessage.value = '当前未登录，无需清除';
    return;
  }
  auth.clear();
  clearMessage.value = '已清除本地 token';
  clearBusy.value = true;
  setTimeout(() => {
    clearBusy.value = false;
    router.replace({ path: '/login' });
  }, 600);
}

// ---------------- 集群块（M2，只读 + 轮换 + 备份） ----------------
const settings = ref<ClusterSettings | null>(null);
const clusterOff = ref(false); // 404 = 非中心角色，整块降级为一行提示
const joinTokenOnce = ref(''); // 一次性明文：只存内存，离开页面即丢
const clusterBusy = ref(false);
const backupTip = ref('');
const clusterError = ref('');

async function fetchSettings() {
  try {
    settings.value = await getClusterSettings();
    clusterOff.value = false;
  } catch (e) {
    if ((e as AxiosError).response?.status === 404) clusterOff.value = true;
    else clusterError.value = (e as Error).message;
  }
}

async function onRotateJoin() {
  if (clusterBusy.value) return;
  clusterBusy.value = true;
  clusterError.value = '';
  try {
    joinTokenOnce.value = (await rotateJoinToken()).join_token;
    try { settings.value = await getClusterSettings(); } catch { /* 掩码刷新失败不影响一次性令牌展示 */ }
  } catch (e) {
    clusterError.value = (e as AxiosError<{ detail?: string }>).response?.data?.detail || (e as Error).message;
  } finally {
    clusterBusy.value = false;
  }
}

async function onBackup() {
  if (clusterBusy.value) return;
  clusterBusy.value = true;
  clusterError.value = '';
  backupTip.value = '';
  try {
    backupTip.value = `已下载 ${await downloadClusterBackup()}（sha256 见响应头 X-Backup-Sha256，可用 cluster backup --to 的本地复核口径对账）`;
  } catch (e) {
    clusterError.value = (e as AxiosError<{ detail?: string }>).response?.data?.detail || (e as Error).message;
  } finally {
    clusterBusy.value = false;
  }
}

onMounted(() => { fetchVersion(); fetchSettings(); });
</script>

<template>
  <div class="space-y-4">
    <!-- 版本信息 -->
    <section class="card">
      <h3 class="mb-3 text-sm font-semibold text-label">版本</h3>
      <div class="flex items-center gap-4">
        <div>
          <span class="text-label2">modelctl 后端版本</span>
          <div class="font-mono text-label">{{ version || '加载中…' }}</div>
        </div>
        <button class="btn-ghost" :disabled="!version" @click="fetchVersion">刷新</button>
        <span v-if="lastFetchAt" class="text-xs text-label3">上次拉取：{{ lastFetchAt }}</span>
      </div>
    </section>

    <!-- 外观 -->
    <section class="card">
      <h3 class="mb-3 text-sm font-semibold text-label">外观</h3>
      <div class="flex flex-wrap items-center gap-3">
        <ThemeSwitch />
        <span class="text-xs text-label3">当前生效：{{ theme.resolved === 'dark' ? '深色' : '浅色' }}</span>
      </div>
      <p class="mt-2 text-xs text-label3">
        「跟随系统」随系统深浅色偏好实时切换；手动选择会记住并覆盖系统设置。
      </p>
    </section>

    <!-- 后端端点 -->
    <section class="card">
      <h3 class="mb-3 text-sm font-semibold text-label">后端端点</h3>
      <p class="text-sm text-label2">
        管理 API 前缀：
        <span class="font-mono text-label">/admin/api</span>
      </p>
      <p class="mt-1 text-xs text-label3">
        任务 SSE：<span class="font-mono">/admin/api/tasks/&#123;task_id&#125;/stream</span>
      </p>
      <p class="mt-1 text-xs text-label3">
        日志 SSE：<span class="font-mono">/admin/api/models/&#123;name&#125;/log/stream</span>
      </p>
    </section>

    <!-- 集群（M2：只读 + join token 轮换 + 备份下载） -->
    <section class="card">
      <h3 class="mb-3 text-sm font-semibold text-label">集群</h3>
      <p v-if="clusterOff" class="text-xs text-label3">
        当前节点未启用集群角色（solo/worker），无集群配置。
      </p>
      <template v-else>
        <dl v-if="settings" class="grid grid-cols-2 gap-x-8 gap-y-2 text-sm md:grid-cols-3">
          <div><dt class="text-label3">角色</dt><dd class="text-label">{{ settings.role }}</dd></div>
          <div><dt class="text-label3">中心地址</dt><dd class="font-mono text-label">{{ settings.center_url || '-' }}</dd></div>
          <div><dt class="text-label3">心跳间隔</dt><dd class="text-label">{{ settings.heartbeat_interval_s }}s</dd></div>
          <div><dt class="text-label3">租约时长</dt><dd class="text-label">{{ settings.lease_s }}s</dd></div>
          <div><dt class="text-label3">reconcile 周期</dt><dd class="text-label">{{ settings.reconcile_interval_s }}s</dd></div>
          <div><dt class="text-label3">快照上限</dt><dd class="text-label">{{ settings.max_snapshot_bytes }} 字节</dd></div>
          <div><dt class="text-label3">join token</dt><dd class="font-mono text-label2">{{ settings.join_token_mask }}</dd></div>
        </dl>
        <span v-else-if="!clusterError" class="text-xs text-label3">配置加载中…</span>
        <p class="mt-2 text-xs text-label3">
          以上为进程启动时读取的 .env 现值，改配置请在中心机改 <span class="font-mono">.env</span> 后重启 webui（远程写可自断控制面，刻意不提供）。
        </p>
        <div class="mt-3 flex items-center gap-3">
          <button class="btn-danger" :disabled="clusterBusy" @click="onRotateJoin">轮换 join token</button>
          <button class="btn-ghost" :disabled="clusterBusy" @click="onBackup">下载数据库备份</button>
        </div>
        <div v-if="joinTokenOnce" class="mt-3 rounded-ctl border border-warn-line bg-warn-bg p-3 text-sm">
          <div class="mb-1 text-xs font-semibold text-warn">新 join token（仅此一次显示）——旧 token 立即失效，所有未加入节点须改用新 token</div>
          <code class="select-all break-all font-mono text-xs text-warn">{{ joinTokenOnce }}</code>
        </div>
        <div v-if="backupTip" class="mt-2 text-xs text-ok">{{ backupTip }}</div>
        <div v-if="clusterError" class="mt-2 text-xs text-danger">{{ clusterError }}</div>
      </template>
    </section>

    <!-- 清除 token -->
    <section class="card">
      <h3 class="mb-3 text-sm font-semibold text-label">登录</h3>
      <p class="mb-3 text-xs text-label3">
        后端无会话概念；「清除 token」仅清空本地 localStorage 的 API Key，并跳回登录页。
      </p>
      <div class="flex items-center gap-3">
        <button class="btn-danger" :disabled="clearBusy" @click="onClearToken">清除本地 token</button>
        <span v-if="clearMessage" class="text-xs text-label2">{{ clearMessage }}</span>
      </div>
    </section>
  </div>
</template>
