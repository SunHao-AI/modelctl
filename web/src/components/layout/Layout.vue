<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import Sidebar from './Sidebar.vue';
import Header from './Header.vue';
import { useTasksStore } from '@/stores/tasks';

// 布局容器：left sidebar + top header + main router-view
const route = useRoute();
const pageTitle = computed(() => (route.meta?.title as string) ?? 'modelctl');

// 移动端抽屉导航（<768px）；桌面常驻侧栏的 hidden md:flex 契约不动
const mobileNavOpen = ref(false);
// 换页即收起抽屉，否则点完菜单抽屉一直挡着内容
watch(() => route.fullPath, () => (mobileNavOpen.value = false));

// 全局任务层：挂载即回填历史 + 重挂活动任务 SSE（刷新/切页恢复）
const tasksStore = useTasksStore();
onMounted(() => {
  void tasksStore.bootstrap().catch(() => {
    /* 列表拉取失败不阻塞页面渲染；降级依赖后续 SSE 事件 */
  });
});
// Layout 卸载（登出跳 /login）即释放全部 SSE/轮询；重新登录后 bootstrap 重建
onBeforeUnmount(() => tasksStore.reset());
</script>

<template>
  <div class="relative flex h-full min-h-screen">
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
