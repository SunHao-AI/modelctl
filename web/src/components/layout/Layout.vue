<script setup lang="ts">
import { computed, onMounted } from 'vue';
import { useRoute } from 'vue-router';
import Sidebar from './Sidebar.vue';
import Header from './Header.vue';
import { useTasksStore } from '@/stores/tasks';

// 布局容器：left sidebar + top header + main router-view
const route = useRoute();
const pageTitle = computed(() => (route.meta?.title as string) ?? 'modelctl');

// 全局任务层：挂载即回填历史 + 重挂活动任务 SSE（刷新/切页恢复）
const tasksStore = useTasksStore();
onMounted(() => {
  void tasksStore.bootstrap().catch(() => {
    /* 列表拉取失败不阻塞页面渲染；降级依赖后续 SSE 事件 */
  });
});
</script>

<template>
  <div class="flex h-full min-h-screen bg-[#0f172a] text-slate-100">
    <!-- 左侧 side bar（移动端可折叠） -->
    <Sidebar class="hidden md:flex md:flex-col" />
    <!-- 主区域 -->
    <div class="flex flex-1 flex-col min-h-screen overflow-hidden">
      <Header :title="pageTitle" />
      <!-- 路由出口 -->
      <main class="flex-1 overflow-auto p-4 md:p-6">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>
    </div>
  </div>
</template>
