<script setup lang="ts">
import { onBeforeMount, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useAuthStore } from '@/stores/auth';
import { logout, login } from '@/api/auth';

const auth = useAuthStore();
const router = useRouter();
const route = useRoute();

const apiKey = ref('');
const submitting = ref(false);
const error = ref('');

/** 已鉴权用户进入 /login 时，直接重定向回主应用 */
onBeforeMount(() => {
  if (auth.isLoggedIn) {
    const redirect = (route.query.redirect as string) || '/';
    router.replace(redirect);
  }
});

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
    // 后端 401 响应体：{ detail: { code, message } }
    const detail = (e as { response?: { data?: { detail?: { message?: string } } } })?.response?.data?.detail;
    error.value = detail?.message || (e as { message?: string })?.message || '网络错误，无法连接后端';
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
  <div class="min-h-screen flex items-center justify-center p-4 bg-[#0f172a]">
    <!-- 背景装饰 -->
    <div class="pointer-events-none absolute inset-0 overflow-hidden">
      <div class="absolute -top-32 -right-32 size-96 rounded-full bg-blue-600/10 blur-3xl" />
      <div class="absolute -bottom-32 -left-32 size-96 rounded-full bg-emerald-600/10 blur-3xl" />
    </div>

    <div class="relative w-full max-w-sm">
      <!-- Logo & 标题 -->
      <div class="flex flex-col items-center mb-8">
        <svg class="size-12 text-blue-500 mb-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7h6l2-2h10v14H3z" /><path d="M3 14h4" /><path d="M3 17h7" /></svg>
        <h1 class="text-2xl font-bold text-slate-100">modelctl</h1>
        <p class="text-sm text-slate-400 mt-1">模型 / 服务 / 配置 一站式管控</p>
      </div>

      <!-- 登录卡片 -->
      <div class="card !p-6">
        <h2 class="text-lg font-semibold text-slate-100 mb-1">使用 API Key 登录</h2>
        <p class="text-xs text-slate-400 mb-5">后端将校验 Key 的有效性与权限</p>

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
            <svg v-if="submitting" class="size-4 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" />
            </svg>
            {{ submitting ? '校验中…' : '登录' }}
          </button>
        </form>

        <!-- 已登录快捷退出 -->
        <button class="mt-4 text-xs text-slate-500 hover:text-slate-300 underline underline-offset-2" @click="onLogout">
          已登录？点击清除本地令牌
        </button>
      </div>

      <p class="mt-6 text-center text-xs text-slate-600">modelctl v0.1.0</p>
    </div>
  </div>
</template>
