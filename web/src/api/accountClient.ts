import axios, { AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios';
import router from '@/router';
import { useAuthStore } from '@/stores/auth';

/**
 * 账号面独立 Axios 客户端：baseURL '/api/account'
 *
 * 与 `./client.ts`（管理面 API_KEY）物理隔离：
 *   - 请求头注入 `Bearer ${accountToken}`（JWT，非 API Key）；
 *   - 401 时清 **账号面** 会话并跳 `/account/login`（**不**跳管理面 `/login`）；
 *
 * 之所以不开单实例的两段拦截：两套 token 分属两套存储（`modelctl_token` vs
 * `modelctl_account_token`），任何"复用同一 instance 按 baseURL 分派"的做法都在
 * 语义上把两个信任域搅在一起；独立实例能确保 admin API_KEY 意外漏到 `/api/` 时
 * 后端仍然会 401（真防串域），且云端 401 分流准确。
 */
const accountClient: AxiosInstance = axios.create({
  baseURL: '/api/account',
  timeout: 30_000,
});

accountClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const auth = useAuthStore();
  if (auth.accountToken) {
    config.headers.set('Authorization', `Bearer ${auth.accountToken}`);
  }
  return config;
});

accountClient.interceptors.response.use(
  (res) => res,
  (err: AxiosError) => {
    const status = err.response?.status;
    if (status === 401) {
      const auth = useAuthStore();
      auth.clearAccountSession();
      const current = router.currentRoute.value;
      if (!current.path.startsWith('/account')) {
        router.push({ path: '/account/login', query: { redirect: current.fullPath } });
      }
    }
    return Promise.reject(err);
  },
);

/** 解包响应体（返回 res.data），与 client.ts 的 dataOf 对齐 */
export function accountDataOf<T>(p: Promise<{ data: T }>): Promise<T> {
  return p.then((r) => r.data);
}

export default accountClient;
