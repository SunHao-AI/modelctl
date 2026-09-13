<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ListChecks, LogOut, Menu } from 'lucide-vue-next';
import { useAuthStore } from '@/stores/auth';
import { health } from '@/api/services';
import TaskDrawer from '@/components/common/TaskDrawer.vue';
import ThemeSwitch from '@/components/common/ThemeSwitch.vue';
import { useTasksStore } from '@/stores/tasks';

const props = defineProps<{
  /** 页面标题 */
  title?: string;
  /** 移动端抽屉导航开关（桌面端不渲染按钮） */
  onToggleNav?: () => void;
}>();

const auth = useAuthStore();
const router = useRouter();

// 全局任务抽屉：入口按钮 + 运行中数量角标
const tasksStore = useTasksStore();
const drawerRef = ref<InstanceType<typeof TaskDrawer> | null>(null);
const runningCount = computed(() => tasksStore.runningCount);

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

/** 脱敏的 apiKey 前缀：显示前 6 位 + … */
const maskedKey = () => {
  const k = auth.token || '';
  if (!k) return '未登录';
  return k.length <= 6 ? '***' : `${k.slice(0, 6)}…`;
};

/** 身份首字：账号面优先显示账号名首字，否则管理面统一「运维」 */
const who = computed(() => {
  const p = auth.accountProfile;
  return p ? (p.display_name || p.username).slice(0, 1).toUpperCase() : '运维';
});

function onLogout() {
  // 先关全部 SSE/轮询并清空任务记录，再清凭据（避免残留请求带旧 token 打后端）
  tasksStore.reset();
  auth.clear();
  router.push({ path: '/login' });
}
</script>

<template>
  <header class="glass z-10 flex items-center justify-between gap-3 border-b border-sep px-4 py-3 md:px-6">
    <!-- 左侧：汉堡（仅移动端）+ 标题 -->
    <div class="flex min-w-0 items-center gap-2.5">
      <button class="iconbtn grid place-items-center md:hidden" type="button" aria-label="打开导航" @click="props.onToggleNav?.()">
        <Menu :size="15" :stroke-width="1.9" />
      </button>
      <h1 class="truncate text-lg font-semibold tracking-[-.022em] text-label md:text-xl" :title="props.title || 'modelctl'">
        {{ props.title || 'modelctl' }}
      </h1>
    </div>

    <!-- 右侧：主题 + 状态 + 身份 + 任务 + 退出 -->
    <div class="flex items-center gap-2.5">
      <!-- 包一层再响应式隐藏：ThemeSwitch 根节点自带 inline-flex，直接传
           `hidden lg:inline-flex` 会被 UnoCSS 产物里排后的 .inline-flex 压掉 -->
      <span class="hidden lg:inline-flex">
        <ThemeSwitch />
      </span>

      <!-- 后端状态（QA B-05：窄屏收成"仅圆点"，文案隐藏但不换行） -->
      <span
        :class="[
          'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-1 text-xs font-medium',
          healthState === 'loading' && 'border-sep bg-surface3 text-label2',
          healthState === 'ok' && 'border-ok-line bg-ok-bg text-ok',
          healthState === 'bad' && 'border-danger-line bg-danger-bg text-danger',
        ]"
      >
        <span
          :class="[
            'stonedot',
            healthState === 'loading' && 'bg-muted',
            healthState === 'ok' && 'animate-pulse bg-ok',
            healthState === 'bad' && 'bg-danger',
          ]"
        />
        <span class="max-md:hidden">{{ healthState === 'loading' ? '检测中' : healthState === 'ok' ? '后端正常' : '后端异常' }}</span>
      </span>

      <!-- 脱敏 apiKey 前缀 -->
      <span class="hidden text-xs text-label3 md:inline-flex" style="font-family: var(--mono)">
        {{ maskedKey() }}
      </span>

      <!-- 任务抽屉入口 -->
      <button class="iconbtn grid place-items-center" type="button" title="后台任务" @click="drawerRef?.toggle()">
        <ListChecks :size="15" :stroke-width="1.9" />
        <span
          v-if="runningCount > 0"
          class="num absolute -right-1 -top-1 grid h-[15px] min-w-[15px] place-items-center rounded-full px-1 text-[9.5px] font-bold"
          style="background: var(--accent); color: var(--on-accent); box-shadow: 0 0 0 2.5px var(--canvas)"
        >
          {{ runningCount }}
        </span>
      </button>

      <!-- 身份头像（QA B-08：两端均压至 #5b6980 及更深，白字全段 ≥4.5:1） -->
      <div
        class="grid size-[29px] shrink-0 place-items-center rounded-full text-[11.5px] font-semibold text-white"
        style="background: linear-gradient(150deg, #5b6980, #3f4a5e)"
      >
        {{ who }}
      </div>

      <!-- 退出登录（QA B-05：<md 收成图标钮；aria-label 保底可访问名仍为「退出登录」） -->
      <button
        class="btn-ghost !py-1.5 text-xs max-md:!px-2.5"
        aria-label="退出登录"
        title="退出登录"
        @click="onLogout"
      >
        <span class="whitespace-nowrap max-md:hidden">退出登录</span>
        <LogOut class="md:hidden" :size="14" :stroke-width="1.9" />
      </button>
    </div>
  </header>

  <!-- 全局任务抽屉（含 toast 宿主）：与 header 同级，fragment 根 -->
  <TaskDrawer ref="drawerRef" />
</template>
