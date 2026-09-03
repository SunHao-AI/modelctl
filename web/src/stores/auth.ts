import { computed, ref } from 'vue';
import { defineStore } from 'pinia';

/** 本地存 API Key 的 key（modelctl 独立命名空间） */
const TOKEN_KEY = 'modelctl_token';

/**
 * 鉴权 store：保存后端校验后的 API Key
 *
 * 后端约定（admin_auth.require_auth）：
 *   - 鉴权方式：所有 /admin/api/*（除 /login、/health）都依赖
 *     `Authorization: Bearer <API_KEY>`
 *   - 后端不签发任何登录态，"登录态" 完全由 localStorage 持久化
 *   - 退出（logout）= 仅清本地 localStorage + 内存 token；后端无任何会话概念
 */
export const useAuthStore = defineStore('auth', () => {
  /** accessToken（= API Key），从 localStorage hydrate */
  const token = ref<string>(localStorage.getItem(TOKEN_KEY) || '');

  /** apiKey：与 token 等价（后端语义统一） */
  const apiKey = computed(() => token.value);

  /** 是否已登录（token 非空即视为登录） */
  const isLoggedIn = computed(() => token.value.trim() !== '');

  /** 持久化后端校验过的 API Key；空串等价于 logout */
  function persistToken(t: string) {
    const v = t ?? '';
    token.value = v;
    if (v) {
      localStorage.setItem(TOKEN_KEY, v);
    } else {
      localStorage.removeItem(TOKEN_KEY);
    }
  }

  /** 退出：仅清本地（token + localStorage） */
  function clear() {
    token.value = '';
    localStorage.removeItem(TOKEN_KEY);
  }

  return {
    token,
    apiKey,
    isLoggedIn,
    persistToken,
    clear,
  };
});
