<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import Sidebar from './Sidebar.vue';
import Header from './Header.vue';
import { useTasksStore } from '@/stores/tasks';

// 布局容器：left sidebar + top header + main router-view
const route = useRoute();
const router = useRouter();
const pageTitle = computed(() => (route.meta?.title as string) ?? 'modelctl');

// 移动端抽屉导航（<768px）；桌面常驻侧栏的 hidden md:flex 契约不动
const mobileNavOpen = ref(false);
// 换页即收起抽屉，否则点完菜单抽屉一直挡着内容
watch(() => route.fullPath, () => (mobileNavOpen.value = false));

/**
 * 路由级 pending 反馈（QA-A-17）：懒加载 chunk 拉取期间顶部 2px 进度条。
 * beforeEach 起、afterEach/onError 落；纯 CSS transition，不引新依赖。
 */
const barOn = ref(false);
const barWidth = ref('0%');
const barFading = ref(false);
let barTimers: number[] = [];
let unguard: Array<() => void> = [];

function navStart(): void {
  barTimers.forEach((t) => window.clearTimeout(t));
  barTimers = [];
  barOn.value = true;
  barFading.value = false;
  barWidth.value = '10%';
  barTimers.push(
    window.setTimeout(() => {
      if (!barFading.value) barWidth.value = '78%';
    }, 40),
  );
}

function navDone(): void {
  if (!barOn.value) return;
  barWidth.value = '100%';
  barTimers.push(window.setTimeout(() => (barFading.value = true), 200));
  barTimers.push(
    window.setTimeout(() => {
      barOn.value = false;
      barFading.value = false;
      barWidth.value = '0%';
    }, 400),
  );
}

// 全局任务层：挂载即回填历史 + 重挂活动任务 SSE（刷新/切页恢复）
const tasksStore = useTasksStore();
onMounted(() => {
  unguard.push(router.beforeEach(() => void navStart()));
  unguard.push(router.afterEach(() => navDone()));
  router.onError(() => navDone()); // onError 不提供注销句柄，随实例销毁自然回收
  void tasksStore.bootstrap().catch(() => {
    /* 列表拉取失败不阻塞页面渲染；降级依赖后续 SSE 事件 */
  });
});
// Layout 卸载（登出跳 /login）即释放全部 SSE/轮询；重新登录后 bootstrap 重建
onBeforeUnmount(() => {
  unguard.forEach((f) => f());
  unguard = [];
  barTimers.forEach((t) => window.clearTimeout(t));
  barTimers = [];
  tasksStore.reset();
});
</script>

<template>
  <div class="relative flex h-full min-h-screen">
    <!-- 路由切换进度条（QA-A-17）：fixed 顶层，pointer-events-none 不拦交互 -->
    <div
      v-if="barOn"
      aria-hidden="true"
      class="pointer-events-none fixed left-0 top-0 z-[70] h-[2px] bg-accent"
      :style="{
        width: barWidth,
        opacity: barFading ? 0 : 1,
        transition: 'width 260ms var(--ease), opacity 180ms linear',
      }"
    />
    <!-- 桌面常驻侧栏（e2e 契约：aside/nav + hidden md:flex 断点） -->
    <Sidebar class="hidden md:flex md:flex-col" />

    <!-- 移动端抽屉：仅 <768px 出现，fixed 层不参与桌面布局 -->
    <div v-if="mobileNavOpen" class="fixed inset-0 z-40 md:hidden">
      <div class="absolute inset-0 bg-black/40" @click="mobileNavOpen = false" />
      <Sidebar class="glass absolute inset-y-0 left-0 z-50 flex w-56 flex-col border-r border-sep" />
    </div>

    <!-- 主区域 -->
    <div class="flex min-h-screen flex-1 flex-col overflow-hidden">
      <Header :title="pageTitle" :on-toggle-nav="() => (mobileNavOpen = !mobileNavOpen)" />
      <!-- 路由出口（relative 让内容盖在 body::before 光斑之上） -->
      <main class="relative flex-1 overflow-auto p-4 md:p-6">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>
    </div>
  </div>
</template>
