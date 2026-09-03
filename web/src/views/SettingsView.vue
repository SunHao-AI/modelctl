<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { health } from '@/api/services';
import { useAuthStore } from '@/stores/auth';

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
/** 主题标识（暂存；不做实际主题切换，仅占位） */
const darkTheme = ref<boolean>(true);

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

onMounted(fetchVersion);
</script>

<template>
  <div class="space-y-4">
    <!-- 版本信息 -->
    <section class="card">
      <h3 class="mb-3 text-sm font-semibold text-slate-100">版本</h3>
      <div class="flex items-center gap-4">
        <div>
          <span class="text-slate-400">modelctl 后端版本</span>
          <div class="font-mono text-slate-100">{{ version || '加载中…' }}</div>
        </div>
        <button class="btn-ghost" :disabled="!version" @click="fetchVersion">刷新</button>
        <span v-if="lastFetchAt" class="text-xs text-slate-500">上次拉取：{{ lastFetchAt }}</span>
      </div>
    </section>

    <!-- 主题（占位） -->
    <section class="card">
      <h3 class="mb-3 text-sm font-semibold text-slate-100">主题</h3>
      <label class="inline-flex items-center gap-2 text-sm text-slate-300">
        <input
          type="checkbox"
          v-model="darkTheme"
          class="size-4 accent-blue-500"
        />
        深色模式（占位，当前简历固定为深色）
      </label>
    </section>

    <!-- 后端端点 -->
    <section class="card">
      <h3 class="mb-3 text-sm font-semibold text-slate-100">后端端点</h3>
      <p class="text-sm text-slate-300">
        管理 API 前缀：
        <span class="font-mono text-slate-100">/admin/api</span>
      </p>
      <p class="mt-1 text-xs text-slate-500">
        任务 SSE：<span class="font-mono">/admin/api/tasks/&#123;task_id&#125;/stream</span>
      </p>
      <p class="mt-1 text-xs text-slate-500">
        日志 SSE：<span class="font-mono">/admin/api/models/&#123;name&#125;/log/stream</span>
      </p>
    </section>

    <!-- 清除 token -->
    <section class="card">
      <h3 class="mb-3 text-sm font-semibold text-slate-100">登录</h3>
      <p class="mb-3 text-xs text-slate-500">
        后端无会话概念；「清除 token」仅清空本地 localStorage 的 API Key，并跳回登录页。
      </p>
      <div class="flex items-center gap-3">
        <button class="btn-danger" :disabled="clearBusy" @click="onClearToken">清除本地 token</button>
        <span v-if="clearMessage" class="text-xs text-slate-400">{{ clearMessage }}</span>
      </div>
    </section>
  </div>
</template>
