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
    // 401 = 鉴权失效：清 token 回登录页（仅 handle 401；其它错误包括 abort
    // 直接 reject 交调用方自行判断——axios 内部对 CanceledError 已做 isCancel 标记）
    const status = err.response?.status;
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

/**
 * Blob 下载（备份导出等二进制端点）：复用同一 axios 实例 ⇒ Bearer 注入与
 * 401 跳登录零重复。文件名优先取 Content-Disposition（后端已带时间戳），
 * 落盘动作走临时 <a download>，随后立即回收 ObjectURL。
 */
export async function downloadBlob(path: string, fallbackFilename: string): Promise<string> {
  const res = await client.get(path, { responseType: 'blob', timeout: 120_000 });
  const disp = String(res.headers['content-disposition'] || '');
  const match = /filename="?([^";]+)"?/i.exec(disp);
  const filename = match?.[1] || fallbackFilename;
  const url = URL.createObjectURL(res.data as Blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  return filename;
}

/** 解包响应体（返回 res.data），统一各 api 模块取值 */
export function dataOf<T>(p: Promise<{ data: T }>): Promise<T> {
  return p.then((r) => r.data);
}

export default client;
