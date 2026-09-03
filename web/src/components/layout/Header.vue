<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { useAuthStore } from '@/stores/auth';
import { health } from '@/api/services';

const props = defineProps<{
  /** 页面标题 */
  title?: string;
}>();

const auth = useAuthStore();
const router = useRouter();

// 后端健康状态
type HealthState = 'loading' | 'ok' | 'bad';
const healthState = ref<HealthState>('loading');

async function refresh() {
  try {
    const res = await health();
    healthState.value = res.ok ? 'ok' : 'bad';
  } catch {
    healthState.value = 'bad';
  }
}

onMounted(() => {
  refresh();
});

/** 脱敏的 apiKey 前缀：显示前 6 位 + *** */
const maskedKey = () => {
  const k = auth.token || '';
  if (!k) return '未登录';
  return k.length <= 6 ? '***' : `${k.slice(0, 6)}…`;
};

function onLogout() {
  auth.clear();
  router.push({ path: '/login' });
}

// todo: 5s 定期轮询删除（暂保留占位，避免 lint 报未使用）
// setInterval(refresh, 30_000);
</script>

<template>
  <header class="flex items-center justify-between gap-4 border-b border-slate-800 bg-slate-900/60 px-4 py-3 md:px-6">
    <!-- 左侧：标题 -->
    <div class="flex items-center gap-3 min-w-0">
      <!-- 移动端折叠按钮占位（后续可接 Sidebar） -->
      <button class="md:hidden btn-ghost !px-2 !py-1">
        <svg class="size-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="12" x2="21" y2="12" /><line x1="3" y1="18" x2="21" y2="18" /></svg>
      </button>
      <h1 class="text-lg md:text-xl font-semibold text-slate-100 truncate">{{ props.title || 'modelctl' }}</h1>
    </div>

    <!-- 右侧：状态 + 用户 + 退出 -->
    <div class="flex items-center gap-3">
      <!-- 后端状态 badge -->
      <span
        :class="[
          'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium',
          healthState === 'loading' && 'bg-slate-700 text-slate-300',
          healthState === 'ok' && 'bg-emerald-600/20 text-emerald-300 border border-emerald-500/30',
          healthState === 'bad' && 'bg-red-600/20 text-red-300 border border-red-500/30',
        ]"
      >
        <span :class="[
          'size-1.5 rounded-full',
          healthState === 'loading' && 'bg-slate-400',
          healthState === 'ok' && 'bg-emerald-400 animate-pulse',
          healthState === 'bad' && 'bg-red-400',
        ]" />
        {{ healthState === 'loading' ? '检测中' : healthState === 'ok' ? '后端正常' : '后端异常' }}
      </span>

      <!-- 用户脱敏 apiKey 前缀 -->
      <span class="hidden md:inline-flex items-center gap-1.5 text-xs text-slate-400 font-mono">{{ maskedKey() }}</span>

      <!-- 退出登录 -->
      <button class="btn-ghost !py-1.5 text-xs" @click="onLogout">退出登录</button>
    </div>
  </header>
</template>
