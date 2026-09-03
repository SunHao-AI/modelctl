<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';

const route = useRoute();

interface MenuItem {
  to: string;
  label: string;
  icon: string;
}

// 侧边菜单（与 router 路由一一对应）
const menus: MenuItem[] = [
  { to: '/dashboard', label: '仪表板', icon: 'dashboard' },
  { to: '/models', label: '模型', icon: 'models' },
  { to: '/services', label: '服务', icon: 'services' },
  { to: '/envs', label: '环境', icon: 'envs' },
  { to: '/probe', label: '体检', icon: 'probe' },
  { to: '/audit', label: '审计', icon: 'audit' },
  { to: '/config', label: '配置', icon: 'config' },
  { to: '/settings', label: '设置', icon: 'settings' },
];

// 通过前缀匹配判定当前激活项（精确优先）
// 注：<script setup> 禁止顶层 export 语句（Vue SFC 约束），此处改为模块内变量
const activeKey = computed(() => {
  const cur = route.path;
  for (const m of menus) {
    if (cur === m.to || cur.startsWith(`${m.to}/`)) return m.to;
  }
  return '';
});

function isActive(item: MenuItem) {
  return route.path === item.to || route.path.startsWith(`${item.to}/`);
}
</script>

<template>
  <aside
    class="flex h-full w-56 flex-col border-r border-slate-800 bg-slate-900/80 backdrop-blur"
  >
    <!-- Logo -->
    <div class="flex items-center gap-2 px-4 py-4 border-b border-slate-800">
      <svg class="size-6 text-blue-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7h6l2-2h10v14H3z" /></svg>
      <span class="text-base font-semibold tracking-wide">modelctl</span>
    </div>

    <!-- 菜单 -->
    <nav class="flex-1 overflow-y-auto px-2 py-3">
      <router-link
        v-for="m in menus"
        :key="m.to"
        :to="m.to"
        :class="[
          'group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors',
          isActive(m)
            ? 'bg-blue-600/20 text-blue-300 border-l-2 border-blue-500'
            : 'text-slate-300 hover:bg-slate-800 hover:text-slate-100',
        ]"
      >
        <!-- 内联 SVG 图标 -->
        <span class="size-5 flex items-center justify-center shrink-0">
          <!-- dashboard -->
          <template v-if="m.icon === 'dashboard'"><svg class="size-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="9" /><rect x="14" y="3" width="7" height="5" /><rect x="14" y="12" width="7" height="9" /><rect x="3" y="16" width="7" height="5" /></svg></template>
          <!-- models -->
          <template v-else-if="m.icon === 'models'"><svg class="size-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" /><path d="M3.27 6.96 12 12.01l8.73-5.05" /><path d="M12 22.08V12" /></svg></template>
          <!-- services -->
          <template v-else-if="m.icon === 'services'"><svg class="size-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="2" width="20" height="8" rx="2" /><rect x="2" y="14" width="20" height="8" rx="2" /><line x1="6" y1="6" x2="6.01" y2="6" /><line x1="6" y1="18" x2="6.01" y2="18" /></svg></template>
          <!-- envs -->
          <template v-else-if="m.icon === 'envs'"><svg class="size-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg></template>
          <!-- probe -->
          <template v-else-if="m.icon === 'probe'"><svg class="size-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2" /></svg></template>
          <!-- audit -->
          <template v-else-if="m.icon === 'audit'"><svg class="size-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /><line x1="16" y1="13" x2="8" y2="13" /><line x1="16" y1="17" x2="8" y2="17" /><line x1="10" y1="9" x2="8" y2="9" /></svg></template>
          <!-- config -->
          <template v-else-if="m.icon === 'config'"><svg class="size-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg></template>
          <!-- settings -->
          <template v-else-if="m.icon === 'settings'"><svg class="size-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20v-6" /><path d="M5 4h14l-2 4H7l-2-4z" /><path d="M19 8v10a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V8" /></svg></template>
          <!-- fallback -->
          <svg v-else class="size-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" /></svg>
        </span>
        <span class="truncate">{{ m.label }}</span>
      </router-link>
    </nav>

    <!-- 底部 small 标签 -->
    <div class="border-t border-slate-800 px-4 py-3 text-xs text-slate-500">
      modelctl chainweb
    </div>
  </aside>
</template>
