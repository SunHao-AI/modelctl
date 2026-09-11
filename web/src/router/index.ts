import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router';
import { useAuthStore } from '@/stores/auth';

// 登录页（无需鉴权）
const LoginView = () => import('@/views/LoginView.vue');
const AccountSelfLoginView = () => import('@/views/accounts/AccountSelfLoginView.vue');
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
    // 账号面板登录（公开）——与管理面 /login 完全独立的入口；401 只会从这里
    // reject 回（accountClient 拦截器跳这里，不是跳 /login）
    path: '/account/login',
    name: 'account-login',
    component: AccountSelfLoginView,
    meta: { title: '账号登录', public: true, accountRoute: true },
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
        path: 'cluster/goals',
        name: 'cluster-goals',
        component: () => import('@/views/ClusterGoalsView.vue'),
        meta: { title: '集群目标' },
      },
      {
        path: 'cluster/nodes',
        name: 'cluster-nodes',
        component: () => import('@/views/ClusterNodesView.vue'),
        meta: { title: '集群节点' },
      },
      {
        path: 'cluster/nodes/:id',
        name: 'cluster-node-detail',
        component: () => import('@/views/ClusterNodeDetailView.vue'),
        meta: { title: '节点详情' },
      },
      {
        // 管理面板：走 Bearer API_KEY（现有 client.ts）
        path: 'accounts',
        name: 'accounts',
        component: () => import('@/views/accounts/AccountsView.vue'),
        meta: { title: '账号管理' },
      },
      {
        // 账号自助面板：走 Bearer 账号 JWT（accountClient.ts）。
        // 前置条件：管理员 API Key 已登录（在 Layout 内），AND 有 accountToken；
        // 后者由 meta.accountAuth 触发 → 缺 JWT 时跳 /account/login
        path: 'account/self',
        name: 'account-self',
        component: () => import('@/views/accounts/AccountSelfView.vue'),
        meta: { title: '我的账号', accountAuth: true },
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

// 路由守卫：
//   Layer 1 —— 管理面（API_KEY）：所有 Layout 子路由默认需要 isLoggedIn
//   Layer 2 —— 账号面（JWT）：`meta.accountAuth === true` 的子路由额外需
//     isAccountLoggedIn；缺 JWT 时跳 /account/login（保留 redirect 供登录后回跳）
//
// 顺序不能换：先 public（/login、/account/login 放行）→ 再 accountAuth 优先
// （viewport 在 Layout 内）→ 再通用 isLoggedIn。若把 accountAuth 检查放后，
// 未带 JWT 但有 API_KEY 也能进 Layout 后才会被 accountAuth 弹走，用户体验
// 上还是对的（Layout 只壳，弹跳短）——但把 accountAuth 检查提前更保险，
// 避免 Layout 挂载/卸载的 SSE 副作用（tasksStore.bootstrap()）被无谓触发。
router.beforeEach((to) => {
  const auth = useAuthStore();

  // public：放行（已登录进 /login 时 view 会自行 replace 到主页；
  // 已带 JWT 进 /account/login 时同理）
  if (to.meta?.public) {
    return true;
  }

  // Layer 1：accountAuth 优先（Layout 内）
  if (to.meta?.accountAuth) {
    if (!auth.isAccountLoggedIn) {
      return { path: '/account/login', query: { redirect: to.fullPath } };
    }
  }

  // Layer 2：Layout 内默认要管理面日志
  //   meta.accountRoute 属于公开的账号登录路由（已被 public 放行），这里不会命中
  if (!auth.isLoggedIn) {
    // 若从 accountAuth 被弹走时可能会误"""抓回 /login：其实 accountAuth 分支
    // 已经先把没 JWT 的丢了，到这里的都是需要管理面日志的资源路径
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
