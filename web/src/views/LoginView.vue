<script setup lang="ts">
import { onBeforeMount, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useAuthStore } from '@/stores/auth';
import { login, logout } from '@/api/auth';
import { health } from '@/api/services';
import Loading from '@/components/common/Loading.vue';

const auth = useAuthStore();
const router = useRouter();
const route = useRoute();

const apiKey = ref('');
const submitting = ref(false);
const error = ref('');
/** 后端版本（QA-A-09：不再硬编码 v0.1.0）；health 拉取失败则只显示 modelctl */
const version = ref('');

/** 已鉴权用户进入 /login 时，直接重定向回主应用 */
onBeforeMount(() => {
  if (auth.isLoggedIn) {
    const redirect = (route.query.redirect as string) || '/';
    router.replace(redirect);
    return;
  }
  // /health 免鉴权，登录前即可取版本号；失败静默降级为不带版本
  void health()
    .then((r) => (version.value = r.version || ''))
    .catch(() => undefined);
});

/**
 * 从登录异常提取用户可见文案（QA-A-01）。
 * 后端 401 错误体形状是 { error: { code, message } }（无 detail）；
 * 按序取 error.message → detail.message（兼容其它形状）→ e.message。
 */
function loginErrorMessage(e: unknown): string {
  const err = e as {
    response?: { data?: { error?: { message?: string }; detail?: { message?: string } } };
    message?: string;
  };
  return (
    err?.response?.data?.error?.message ??
    err?.response?.data?.detail?.message ??
    err?.message ??
    '网络错误，无法连接后端'
  );
}

async function onSubmit() {
  const key = apiKey.value.trim();
  if (!key) {
    error.value = '请填入 API Key';
    return;
  }
  submitting.value = true;
  error.value = '';
  try {
    const res = await login(key);
    if (res.ok) {
      auth.persistToken(key);
      const redirect = (route.query.redirect as string) || '/';
      router.replace(redirect);
      return;
    }
    error.value = res.message || '登录失败，请检查 API Key';
  } catch (e) {
    // 后端 401 响应体：{ error: { code, message } }（无 detail；QA-A-01）
    error.value = loginErrorMessage(e);
  } finally {
    submitting.value = false;
  }
}

async function onLogout() {
  auth.clear();
  await logout();
}
</script>

<template>
  <div class="relative flex min-h-screen items-center justify-center p-4">
    <div class="relative w-full max-w-sm">
      <!-- Logo & 标题 -->
      <div class="flex flex-col items-center mb-8">
        <svg class="size-12 text-accent mb-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7h6l2-2h10v14H3z" /><path d="M3 14h4" /><path d="M3 17h7" /></svg>
        <h1 class="text-2xl font-bold text-label">modelctl</h1>
        <p class="text-sm text-label2 mt-1">模型 / 服务 / 配置 一站式管控</p>
      </div>

      <!-- 登录卡片 -->
      <div class="card !p-6 w-full max-w-sm" style="box-shadow: var(--shadow-l)">
        <h2 class="text-lg font-semibold text-label mb-1">使用 API Key 登录</h2>
        <p class="text-xs text-label2 mb-5">后端将校验 Key 的有效性与权限</p>

        <form @submit.prevent="onSubmit">
          <label class="label-base" for="api-key">API Key</label>
          <input
            id="api-key"
            v-model="apiKey"
            type="password"
            :disabled="submitting"
            class="input-base !font-mono"
            placeholder="sk-xxxxxxxxxxxx"
            autocomplete="off"
            autofocus
            spellcheck="false"
          />

          <!-- 错误提示 -->
          <p v-if="error" class="mt-2 text-xs text-red-400">{{ error }}</p>

          <button
            type="submit"
            class="btn-primary w-full mt-5"
            :disabled="submitting || !apiKey.trim()"
          >
            <Loading v-if="submitting" inline label="" />
            {{ submitting ? '校验中…' : '登录' }}
          </button>
        </form>

        <!-- 已登录快捷退出 -->
        <button class="mt-4 text-xs text-label3 hover:text-label2 underline underline-offset-2" @click="onLogout">
          已登录？点击清除本地令牌
        </button>
      </div>

      <p class="mt-6 text-center text-xs text-label3">modelctl{{ version ? ` v${version}` : '' }}</p>
    </div>
  </div>
</template>
