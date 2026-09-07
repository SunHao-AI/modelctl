<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { dockerDiagnose, envRemove, envSetup, envTargets } from '@/api/envs';
import type { DockerBypassEntry, DockerDiagnose, DockerEnv, EnvTarget, UnmanagedTarget } from '@/api/types';
import TaskButton from '@/components/common/TaskButton.vue';
import ConfirmDialog from '@/components/common/ConfirmDialog.vue';

const route = useRoute();
const router = useRouter();
/** ?focus= 定位目标名（4s 后清除高亮） */
const focusName = ref('');

/**
 * 环境管理：拉取 targets（venv 安装状态），Setup 走 TaskButton（异步 28min+），
 * Remove 用普通按钮 + ConfirmDialog
 */
const targets = ref<EnvTarget[]>([]);
/** 非托管引擎（原生/官方安装器），只在表格外做说明，不提供 Setup/Remove */
const unmanaged = ref<UnmanagedTarget[]>([]);
const errMsg = ref('');
const notice = ref('');
const loading = ref(true);

/** 待确认移除的 target 名 */
const pendingRemove = ref<string | null>(null);
const removeBusy = ref(false);

/** Docker 环境就绪探测（随列表加载返回） */
const dockerEnv = ref<DockerEnv | null>(null);
/** 逐引擎 Docker 旁路指引 */
const dockerBypass = ref<DockerBypassEntry[]>([]);
/** 诊断面板：展开态 / 结果（懒加载一次）/ 错误 / 加载态 */
const diagOpen = ref(false);
const diagData = ref<DockerDiagnose | null>(null);
const diagErr = ref('');
const diagBusy = ref(false);
/** 复制反馈的当前 key（'inst' 或引擎名） */
const copiedKey = ref('');

async function load() {
  loading.value = true;
  try {
    const r = await envTargets();
    targets.value = r.targets ?? [];
    unmanaged.value = r.unmanaged ?? [];
    dockerEnv.value = r.docker_env ?? null;
    dockerBypass.value = r.docker_bypass ?? [];
    errMsg.value = '';
  } catch (err) {
    console.warn('envTargets 失败:', err);
    errMsg.value = (err as { message?: string })?.message || '环境列表加载失败';
  } finally {
    loading.value = false;
  }
}

async function doRemove(target: string) {
  removeBusy.value = true;
  notice.value = '';
  try {
    const r = await envRemove(target);
    notice.value = `${target}：${r.detail || (r.ok ? '已移除' : '移除失败')}`;
    void load();
  } catch (err) {
    notice.value = (err as { message?: string })?.message || '移除失败';
  } finally {
    removeBusy.value = false;
    pendingRemove.value = null;
  }
}

/** Setup 失败提示：平台限制类错误附旁路区块引导语 */
function onSetupError(name: string, msg: string) {
  notice.value = `${name} setup 失败：${msg}`;
  if (msg.includes('docker 镜像绕过')) {
    notice.value += '——旁路步骤见下方「Docker 旁路」区块';
  }
}

/** 展开/收起完整诊断；首次展开懒加载一次 */
async function onDiagnose() {
  diagOpen.value = !diagOpen.value;
  if (!diagOpen.value || diagData.value || diagBusy.value) return;
  diagBusy.value = true;
  diagErr.value = '';
  try {
    diagData.value = await dockerDiagnose();
  } catch (err) {
    console.warn('dockerDiagnose 失败:', err);
    diagErr.value = (err as { message?: string })?.message || '诊断失败';
  } finally {
    diagBusy.value = false;
  }
}

/** 复制任意文本（ConfigView copySnippet 同款 1.5s 反馈） */
async function copyText(key: string, text: string) {
  try {
    await navigator.clipboard.writeText(text);
    copiedKey.value = key;
    setTimeout(() => (copiedKey.value = ''), 1500);
  } catch (err) {
    console.warn('复制失败:', err);
  }
}

/**
 * focus 是否需要额外高亮 Docker 旁路区块（规格 §4.4/§6）：
 * 1) 纯 docker 语境 focus —— focus 名不在任何受管 target 行中；
 * 2) focus 命中的引擎 platform_supported === false —— 托管 venv 装不了，只能走旁路。
 */
function focusNeedsBypass(): boolean {
  const name = focusName.value;
  if (!name) return false;
  const hit = targets.value.find((t) => t.name === name);
  if (!hit) return true;
  return hit.platform_supported === false;
}

onMounted(async () => {
  await load();
  const f = route.query.focus;
  if (typeof f !== 'string' || !f) return;
  // 用后即清：刷新/前进后退不再重复滚动
  void router.replace({ query: {} });
  focusName.value = f;
  await nextTick();
  // 引擎行优先；查不到行（如纯 docker 语境）退回旁路区块
  const el = document.getElementById(`env-row-${f}`) ?? document.getElementById('docker-bypass');
  el?.scrollIntoView({ block: 'center' });
  window.setTimeout(() => {
    if (focusName.value === f) focusName.value = '';
  }, 4000);
});
</script>

<template>
  <div class="space-y-4">
    <!-- 工具条 -->
    <div class="flex items-center justify-between">
      <p class="text-sm text-slate-400">
        共 {{ targets.length }} 个受管目标（managed engine + gateway 共用仓）
      </p>
      <button class="btn-ghost" :disabled="loading" @click="load">刷新</button>
    </div>

    <p v-if="errMsg" class="text-sm text-red-400">{{ errMsg }}</p>
    <p v-if="notice" class="text-sm text-emerald-300">{{ notice }}</p>

    <!-- 表格 -->
    <section class="card !p-0 overflow-x-auto">
      <table v-if="targets.length" class="w-full text-sm">
        <thead class="bg-slate-800/40 text-left text-xs text-slate-400 uppercase tracking-wider">
          <tr>
            <th class="px-3 py-2">目标</th>
            <th class="px-3 py-2">状态</th>
            <th class="px-3 py-2">说明</th>
            <th class="px-3 py-2 text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="t in targets"
            :id="`env-row-${t.name}`"
            :key="t.name"
            class="border-b border-slate-800/40 transition-colors"
            :class="focusName === t.name ? 'bg-amber-500/10' : ''"
          >
            <td class="px-3 py-2 font-mono text-slate-100">{{ t.name }}</td>
            <td class="px-3 py-2">
              <span
                class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs"
                :class="t.installed
                  ? 'bg-emerald-600/15 text-emerald-300 border border-emerald-500/30'
                  : 'bg-slate-600/15 text-slate-400 border border-slate-500/30'"
              >
                <span class="size-1.5 rounded-full" :class="t.installed ? 'bg-emerald-400' : 'bg-slate-500'" />
                {{ t.installed ? '已安装' : '未安装' }}
              </span>
            </td>
            <td class="px-3 py-2 text-xs text-slate-400">{{ t.detail }}</td>
            <td class="px-3 py-2 text-right">
              <div class="flex items-center justify-end gap-2">
                <!-- 未安装可用 Setup（任务流，长耗时） -->
                <TaskButton
                  v-if="!t.installed"
                  label="Setup"
                  variant="primary"
                  :target="t.name"
                  :disabled="!t.platform_supported"
                  :disabled-reason="
                    t.platform_supported ? undefined : '托管 venv 仅支持 Linux 部署机，可走下方「Docker 旁路」区块'
                  "
                  :task-target="() => envSetup(t.name)"
                  @success="() => load()"
                  @error="(msg) => onSetupError(t.name, msg)"
                />
                <!-- 已安装可用 Remove -->
                <button
                  v-else
                  class="btn-ghost !py-1 !px-2 text-xs"
                  :disabled="removeBusy"
                  @click="pendingRemove = t.name"
                >移除</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else-if="!loading" class="p-6 text-sm text-slate-500">尚无受管目标</div>
      <div v-else class="p-6 text-sm text-slate-500">加载中…</div>
    </section>

    <!-- Docker 旁路：托管 venv 仅支持 Linux，已支持引擎可改用官方 docker 镜像 -->
    <!-- focus 命中非托管引擎（platform 不支持）或纯 docker 语境 focus 时高亮本区块 -->
    <section
      v-if="dockerBypass.length"
      id="docker-bypass"
      class="card space-y-3 transition-shadow"
      :class="focusNeedsBypass() ? 'ring-1 ring-amber-400/60' : ''"
    >
      <div class="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 class="text-sm font-medium text-slate-200">Docker 旁路</h2>
          <p class="mt-0.5 text-xs text-slate-500">
            托管 venv 仅支持 Linux；下列引擎可改用官方 docker 镜像绕过 venv（编辑模型 yaml 后仍由 modelctl 启停）
          </p>
        </div>
        <div class="flex items-center gap-2">
          <span
            class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs"
            :class="dockerEnv?.ready
              ? 'bg-emerald-600/15 text-emerald-300 border border-emerald-500/30'
              : 'bg-red-600/15 text-red-300 border border-red-500/30'"
            :title="dockerEnv && !dockerEnv.ready ? dockerEnv.missing.join('；') : undefined"
          >
            <span class="size-1.5 rounded-full" :class="dockerEnv?.ready ? 'bg-emerald-400' : 'bg-red-400'" />
            {{ dockerEnv?.ready ? 'Docker 环境就绪' : 'Docker 环境缺失' }}
          </span>
          <button class="btn-ghost !py-1 !px-2 text-xs" :disabled="diagBusy" @click="onDiagnose">
            {{ diagBusy ? '诊断中…' : diagOpen ? '收起诊断' : '完整诊断' }}
          </button>
        </div>
      </div>

      <p v-if="dockerEnv && !dockerEnv.ready" class="text-xs text-slate-500">{{ dockerEnv.guide }}</p>

      <!-- 完整诊断：首次展开懒加载一次（后端含子进程探测） -->
      <div v-if="diagOpen" class="space-y-1.5 border-t border-slate-800/40 pt-3">
        <p v-if="diagErr" class="text-xs text-red-400">{{ diagErr }}</p>
        <template v-else-if="diagData">
          <div v-for="c in diagData.checks" :key="c.key" class="flex flex-wrap items-center gap-x-2 text-xs">
            <span class="size-1.5 rounded-full" :class="c.ok ? 'bg-emerald-400' : 'bg-red-400'" />
            <span class="text-slate-300">{{ c.label }}</span>
            <span v-if="!c.ok" class="break-all text-slate-500">{{ c.detail }}</span>
          </div>
          <div class="mt-2">
            <div class="mb-1 flex items-center justify-between">
              <span class="text-xs text-slate-500">
                安装脚本（复制到部署机 root shell；WebUI 只展示不执行）
              </span>
              <button class="btn-ghost !py-1 !px-2 text-xs" @click="copyText('inst', diagData.instructions)">
                {{ copiedKey === 'inst' ? '已复制' : '复制脚本' }}
              </button>
            </div>
            <pre
              class="max-h-72 overflow-auto bg-[#0b1120] p-3 font-mono text-xs leading-6 whitespace-pre text-slate-300"
            >{{ diagData.instructions }}</pre>
          </div>
        </template>
        <p v-else class="text-xs text-slate-500">诊断中…</p>
      </div>

      <!-- 逐引擎指引 -->
      <div class="space-y-2">
        <div v-for="b in dockerBypass" :key="b.name" class="space-y-1.5 border-t border-slate-800/40 pt-2.5">
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
            <span class="font-mono text-slate-300">{{ b.name }}</span>
            <span
              class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5"
              :class="b.docker_supported
                ? 'bg-emerald-600/15 text-emerald-300 border border-emerald-500/30'
                : 'bg-slate-600/15 text-slate-400 border border-slate-500/30'"
            >
              <span class="size-1.5 rounded-full" :class="b.docker_supported ? 'bg-emerald-400' : 'bg-slate-500'" />
              {{ b.docker_supported ? '支持 docker 运行时' : '暂不支持 docker' }}
            </span>
          </div>
          <p v-if="!b.docker_supported" class="text-xs text-slate-500">{{ b.note }}</p>
          <template v-else>
            <div class="flex flex-wrap items-center gap-2 text-xs">
              <span class="text-slate-500">yaml 片段</span>
              <code class="break-all text-slate-300">{{ b.yaml_field_path }}: {{ b.image_example }}</code>
              <button
                class="btn-ghost !py-0.5 !px-2 text-xs"
                @click="copyText(b.name, `${b.yaml_field_path}: ${b.image_example}`)"
              >
                {{ copiedKey === b.name ? '已复制' : '复制' }}
              </button>
              <span class="text-slate-600">·</span>
              <span class="break-all text-slate-500">示例 <code class="text-slate-400">{{ b.example_yaml }}</code></span>
            </div>
            <ol class="ml-4 list-decimal space-y-0.5 text-xs text-slate-400">
              <li v-for="(s, i) in b.steps" :key="i" class="break-all">{{ s }}</li>
            </ol>
          </template>
        </div>
      </div>
    </section>

    <!-- 非托管引擎说明：原生二进制 / 官方安装器 / 源码编译，不建 venv 故不在上表 -->
    <section v-if="unmanaged.length" class="card space-y-2">
      <div class="flex items-baseline justify-between">
        <h2 class="text-sm font-medium text-slate-200">非托管引擎</h2>
        <span class="text-xs text-slate-500">原生或官方安装器，无需托管 venv</span>
      </div>
      <div v-for="u in unmanaged" :key="u.name" class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span class="font-mono text-slate-300">{{ u.name }}</span>
        <span
          class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5"
          :class="u.installed
            ? 'bg-emerald-600/15 text-emerald-300 border border-emerald-500/30'
            : 'bg-slate-600/15 text-slate-400 border border-slate-500/30'"
        >
          <span class="size-1.5 rounded-full" :class="u.installed ? 'bg-emerald-400' : 'bg-slate-500'" />
          {{ u.installed ? '已安装' : '未安装' }}
        </span>
        <span v-if="u.installed" class="break-all text-slate-500">{{ u.path }}</span>
        <code v-else class="break-all whitespace-pre-line text-slate-400">{{ u.install_hint }}</code>
      </div>
    </section>

    <!-- 移除确认 -->
    <ConfirmDialog
      :open="pendingRemove !== null"
      :title="`移除环境 ${pendingRemove ?? ''}`"
      :message="`确认移除 ${pendingRemove ?? ''}？底层 rmtree 删除目录，操作不可撤销。`"
      danger
      :loading="removeBusy"
      confirm-text="移除"
      @confirm="doRemove(pendingRemove!)"
      @cancel="pendingRemove = null"
    />
  </div>
</template>
