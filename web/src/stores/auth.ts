import { computed, ref } from 'vue';
import { defineStore } from 'pinia';

/** 本地存 API Key 的 key（modelctl 独立命名空间） */
const TOKEN_KEY = 'modelctl_token';
/** 本地存账号 JWT 的 key（自账号面板登录签发；与管理面 API_KEY 完全独立） */
const ACCOUNT_TOKEN_KEY = 'modelctl_account_token';
/** 账号面板 profile 持久化（user 摘要）——登录一次就缓存，避免每次进自助面板再打 login */
const ACCOUNT_PROFILE_KEY = 'modelctl_account_profile';

export interface AccountProfile {
  id: number;
  username: string;
  display_name: string;
  is_admin: boolean;
}

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

  /** 账号面板 JWT（自检用 require_account 的 Bearer）；与管理面 API_KEY 完全独立 */
  const accountToken = ref<string>(localStorage.getItem(ACCOUNT_TOKEN_KEY) || '');
  const accountProfile = ref<AccountProfile | null>(
    (() => {
      try {
        const raw = localStorage.getItem(ACCOUNT_PROFILE_KEY);
        return raw ? (JSON.parse(raw) as AccountProfile) : null;
      } catch {
        return null;
      }
    })(),
  );

  function setAccountSession(t: string, profile: AccountProfile) {
    accountToken.value = t;
    accountProfile.value = profile;
    if (t) {
      localStorage.setItem(ACCOUNT_TOKEN_KEY, t);
      localStorage.setItem(ACCOUNT_PROFILE_KEY, JSON.stringify(profile));
    } else {
      localStorage.removeItem(ACCOUNT_TOKEN_KEY);
      localStorage.removeItem(ACCOUNT_PROFILE_KEY);
    }
  }

  function clearAccountSession() {
    accountToken.value = '';
    accountProfile.value = null;
    localStorage.removeItem(ACCOUNT_TOKEN_KEY);
    localStorage.removeItem(ACCOUNT_PROFILE_KEY);
  }

  const isAccountLoggedIn = computed(() => accountToken.value.trim() !== '');

  return {
    token,
    apiKey,
    isLoggedIn,
    persistToken,
    clear,
    accountToken,
    accountProfile,
    isAccountLoggedIn,
    setAccountSession,
    clearAccountSession,
  };
});
