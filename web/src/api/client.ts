import axios, { AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios';
import router from '@/router';
import { useAuthStore } from '@/stores/auth';

/**
 * Axios 客户端：baseURL '/admin/api'
 * 请求拦截器：注入 Authorization: Bearer {token}
 * 响应拦截器：401 时清除 token 并跳转 /login
 */
const client: AxiosInstance = axios.create({
  baseURL: '/admin/api',
  timeout: 30_000,
});

client.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const auth = useAuthStore();
  if (auth.token) {
    config.headers.set('Authorization', `Bearer ${auth.token}`);
  }
  return config;
});

client.interceptors.response.use(
  (res) => res,
  (err: AxiosError<{ message?: string; code?: string }>) => {
    const status = err.response?.status;
    // 401 = 鉴权失效：清 token 回登录页
    if (status === 401) {
      const auth = useAuthStore();
      auth.clear();
      const current = router.currentRoute.value;
      if (current.path !== '/login') {
        router.push({ path: '/login', query: { redirect: current.fullPath } });
      }
    }
    return Promise.reject(err);
  },
);

/** 解包响应体（返回 res.data），统一各 api 模块取值 */
export function dataOf<T>(p: Promise<{ data: T }>): Promise<T> {
  return p.then((r) => r.data);
}

export default client;
