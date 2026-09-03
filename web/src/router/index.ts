import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router';
import { useAuthStore } from '@/stores/auth';

// 登录页（无需鉴权）
const LoginView = () => import('@/views/LoginView.vue');
// 带 Layout 的容器路由（需鉴权）
const Layout = () => import('@/components/layout/Layout.vue');

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: LoginView,
    meta: { title: '登录', public: true },
  },
  {
    path: '/',
    component: Layout,
    meta: { title: '主应用' },
    redirect: '/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/DashboardView.vue'),
        meta: { title: '仪表板' },
      },
      {
        path: 'models',
        name: 'models-list',
        component: () => import('@/views/ModelsListView.vue'),
        meta: { title: '模型' },
      },
      {
        path: 'models/:name',
        name: 'models-detail',
        component: () => import('@/views/ModelDetailView.vue'),
        meta: { title: '模型详情' },
        props: true,
      },
      {
        path: 'services',
        name: 'services-matrix',
        component: () => import('@/views/ServicesMatrixView.vue'),
        meta: { title: '服务' },
      },
      {
        path: 'envs',
        name: 'envs',
        component: () => import('@/views/EnvsView.vue'),
        meta: { title: '环境' },
      },
      {
        path: 'probe',
        name: 'probe',
        component: () => import('@/views/ProbeView.vue'),
        meta: { title: '体检' },
      },
      {
        path: 'audit',
        name: 'audit',
        component: () => import('@/views/AuditLogView.vue'),
        meta: { title: '审计' },
      },
      {
        path: 'config',
        name: 'config',
        component: () => import('@/views/ConfigView.vue'),
        meta: { title: '配置' },
      },
      {
        path: 'settings',
        name: 'settings',
        component: () => import('@/views/SettingsView.vue'),
        meta: { title: '设置' },
      },
      {
        path: 'cluster/nodes',
        name: 'cluster-nodes',
        component: () => import('@/views/ClusterNodesView.vue'),
        meta: { title: '集群节点' },
      },
    ],
  },
  {
    // 兜底：任何未匹配路径均按资源路径回登录或 404
    path: '/:pathMatch(.*)*',
    redirect: '/login',
  },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

// 全局守卫：未登录访问受保护页面 -> 跳转 /login
router.beforeEach((to) => {
  const auth = useAuthStore();
  if (to.meta?.public) {
    // 已登录用户访问 /login 时直接进主页
    if (to.name === 'login' && auth.isLoggedIn) {
      return { path: '/' };
    }
    return true;
  }
  if (!auth.isLoggedIn) {
    return { path: '/login', query: { redirect: to.fullPath } };
  }
  return true;
});

router.afterEach((to) => {
  if (typeof to.meta?.title === 'string') {
    document.title = `${to.meta.title} · modelctl Web UI`;
  }
});

export default router;
