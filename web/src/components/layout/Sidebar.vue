<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import {
  Activity, Box, Boxes, Database, Layers, LayoutDashboard, MessageSquare,
  Network, ScrollText, Server, Settings, ShieldCheck, Target, UserRound,
  type LucideIcon,
} from 'lucide-vue-next';

const route = useRoute();

type Group = '概览' | '运维' | '系统';

interface MenuItem {
  to: string;
  label: string;
  icon: LucideIcon;
  group: Group;
}

/** 侧边菜单：与 router 路由一一对应。项 / 顺序 / 路径 / 文案不得改动（e2e 依赖）。 */
const menus: MenuItem[] = [
  { to: '/dashboard', label: '仪表板', icon: LayoutDashboard, group: '概览' },
  { to: '/models', label: '模型', icon: Boxes, group: '概览' },
  { to: '/services', label: '服务', icon: Server, group: '概览' },
  { to: '/chat', label: 'AI 对话', icon: MessageSquare, group: '概览' },
  { to: '/envs', label: '环境', icon: Layers, group: '运维' },
  { to: '/probe', label: '体检', icon: Activity, group: '运维' },
  { to: '/cluster/goals', label: '集群目标', icon: Target, group: '运维' },
  { to: '/cluster/nodes', label: '集群', icon: Network, group: '运维' },
  { to: '/audit', label: '审计', icon: ScrollText, group: '系统' },
  { to: '/accounts', label: '账号管理', icon: ShieldCheck, group: '系统' },
  { to: '/account/self', label: '我的账号', icon: UserRound, group: '系统' },
  { to: '/config', label: '配置', icon: Database, group: '系统' },
  { to: '/settings', label: '设置', icon: Settings, group: '系统' },
];

const GROUPS: Group[] = ['概览', '运维', '系统'];

/** 按分组切分，组内保持 menus 原顺序 */
const sections = computed(() =>
  GROUPS.map((g) => ({ group: g, items: menus.filter((m) => m.group === g) })).filter((s) => s.items.length),
);

// 通过前缀匹配判定当前激活项（精确优先）
function isActive(item: MenuItem) {
  return route.path === item.to || route.path.startsWith(`${item.to}/`);
}
// 根上不写 display 工具类：UnoCSS 产物里 .flex 排在 .hidden 之后，
// 会和 Layout 传入的 `hidden md:flex` 打架（<768px 侧栏关不掉）。display 全交给外部类。
</script>

<template>
  <aside class="glass h-full w-56 flex-col border-r border-sep">
    <!-- Logo -->
    <div class="flex items-center gap-2.5 px-4 pb-4 pt-4">
      <span
        class="grid size-[22px] shrink-0 place-items-center rounded-[7px] text-white"
        style="background: linear-gradient(160deg, var(--accent), #0a4fb0); box-shadow: 0 2px 7px rgba(0,113,227,.38)"
      >
        <Box :size="12" :stroke-width="2.6" />
      </span>
      <span class="text-[14.5px] font-semibold tracking-[-.015em] text-label">modelctl</span>
    </div>

    <!-- 菜单（分组渲染，项序与旧版一致） -->
    <nav class="flex-1 overflow-y-auto px-2 pb-3">
      <template v-for="sec in sections" :key="sec.group">
        <div class="px-2.5 pb-1.5 pt-3 text-[10.5px] font-semibold uppercase tracking-[.075em] text-label3">
          {{ sec.group }}
        </div>
        <router-link
          v-for="m in sec.items"
          :key="m.to"
          :to="m.to"
          :class="[
            'nav-item relative flex items-center gap-2.5 rounded-ctl px-2.5 py-[7px] text-[13.5px] transition-all duration-150',
            isActive(m) ? 'nav-item-active font-medium text-label' : 'text-label2 hover:bg-surface3 hover:text-label',
          ]"
        >
          <component :is="m.icon" :size="16" :stroke-width="1.75" class="shrink-0 opacity-90" />
          <span class="truncate">{{ m.label }}</span>
        </router-link>
      </template>
    </nav>

    <!-- 底部 small 标签 -->
    <div class="border-t border-sep-soft px-4 py-3 text-[11px] text-label3">modelctl chainweb</div>
  </aside>
</template>

<style scoped>
/* 选中项左侧 3px accent 竖条：画在容器外沿，等价旧版 border-l-2 的视觉位置，
   但不占 padding，避免文字右移 2px */
.nav-item-active {
  background: var(--surface-3);
  box-shadow: var(--inset-hl);
}
.nav-item-active::before {
  content: '';
  position: absolute;
  left: -8px;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 15px;
  border-radius: 0 3px 3px 0;
  background: var(--accent);
}
</style>
