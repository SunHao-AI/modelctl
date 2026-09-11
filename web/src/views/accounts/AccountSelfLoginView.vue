<script setup lang="ts">
import { onBeforeMount, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useAuthStore } from '@/stores/auth';
import { accountLogin } from '@/api/accountSelf';

// 账号面板登录页 — 独立于管理面 /login：
//   - 独立表单 → 独立 token（JWT） → 独立跳转（/account/self）
//   - 401 语义：后端 login 弱命题（未知用户/错密码/禁用账号同 shape）
const auth = useAuthStore();
const router = useRouter();
const route = useRoute();

const username = ref('');
const password = ref('');
const submitting = ref(false);
const error = ref('');

onBeforeMount(() => {
  if (auth.isAccountLoggedIn) {
    const redirect = (route.query.redirect as string) || '/account/self';
    router.replace(redirect);
  }
});

async function onSubmit() {
  const u = username.value.trim();
  if (!u || !password.value) {
    error.value = '请输入用户名和密码';
    return;
  }
  submitting.value = true;
  error.value = '';
  try {
    const res = await accountLogin(u, password.value);
    auth.setAccountSession(res.token, res.user);
    const redirect = (route.query.redirect as string) || '/account/self';
    router.replace(redirect);
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: { message?: string } | string } } })
      ?.response?.data?.detail;
    if (typeof detail === 'string') {
      error.value = detail;
    } else {
      error.value = detail?.message || (e as { message?: string })?.message || '网络错误';
    }
  } finally {
    submitting.value = false;
  }
}

function onClear() {
  auth.clearAccountSession();
  error.value = '已清除本地账号会话';
}
</script>

<template>
  <div class="min-h-screen flex items-center justify-center p-4 bg-[#0f172a]">
    <div class="pointer-events-none absolute inset-0 overflow-hidden">
      <div class="absolute -top-32 -right-32 size-96 rounded-full bg-emerald-600/10 blur-3xl" />
      <div class="absolute -bottom-32 -left-32 size-96 rounded-full bg-blue-600/10 blur-3xl" />
    </div>

    <div class="relative w-full max-w-sm">
      <div class="flex flex-col items-center mb-8">
        <svg class="size-12 text-emerald-500 mb-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
          <circle cx="12" cy="7" r="4" />
        </svg>
        <h1 class="text-2xl font-bold text-slate-100">账号面板</h1>
        <p class="text-sm text-slate-400 mt-1">以账号身份登录（使用用户名/密码）</p>
      </div>

      <div class="card !p-6">
        <h2 class="text-lg font-semibold text-slate-100 mb-1">账号登录</h2>
        <p class="text-xs text-slate-400 mb-5">与"管理面板（API Key）"是独立凭据</p>

        <form @submit.prevent="onSubmit">
          <label class="label-base" for="account-username">用户名</label>
          <input
            id="account-username" v-model="username" type="text" :disabled="submitting"
            class="input-base" placeholder="username" autocomplete="off" autofocus spellcheck="false"
          />

          <label class="label-base mt-3" for="account-password">密码</label>
          <input
            id="account-password" v-model="password" type="password" :disabled="submitting"
            class="input-base" placeholder="••••••••" autocomplete="current-password"
          />

          <p v-if="error" class="mt-3 text-xs text-red-400">{{ error }}</p>

          <button
            type="submit" class="btn-primary w-full mt-5"
            :disabled="submitting || !username.trim() || !password"
          >
            <svg v-if="submitting" class="size-4 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" />
            </svg>
            {{ submitting ? '登录中…' : '登录' }}
          </button>
        </form>

        <div class="mt-4 flex items-center justify-between text-xs">
          <router-link to="/login" class="text-slate-500 hover:text-slate-300 underline underline-offset-2">
            ← 返回 API Key 登录
          </router-link>
          <button class="text-slate-500 hover:text-slate-300 underline underline-offset-2" @click="onClear">
            清除本地会话
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
