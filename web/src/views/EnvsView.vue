<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { dockerDiagnose, envRemove, envSetup, envTargets } from '@/api/envs';
import type { DockerBypassEntry, DockerDiagnose, DockerEnv, EnvTarget, UnmanagedTarget } from '@/api/types';
import TaskButton from '@/components/common/TaskButton.vue';
import ConfirmDialog from '@/components/common/ConfirmDialog.vue';
import DockerInstallPanel from '@/components/docker/DockerInstallPanel.vue';

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
      <p class="num text-sm text-label2">
        共 {{ targets.length }} 个受管目标（managed engine + gateway 共用仓）
      </p>
      <button class="btn-ghost" :disabled="loading" @click="load">刷新</button>
    </div>

    <p v-if="errMsg" class="text-sm text-red-400">{{ errMsg }}</p>
    <p v-if="notice" class="text-sm text-ok">{{ notice }}</p>

    <!-- 表格 -->
    <section class="card !p-0 overflow-x-auto">
      <table v-if="targets.length" class="w-full text-sm">
        <thead class="bg-surface3 text-left text-xs text-label2 uppercase tracking-wider">
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
            class="border-b border-sep transition-colors"
            :class="focusName === t.name ? 'bg-warn-bg' : ''"
          >
            <td class="px-3 py-2 font-mono text-label">{{ t.name }}</td>
            <td class="px-3 py-2">
              <span
                class="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs"
                :class="t.installed
                  ? 'bg-ok-bg text-ok border-ok-line'
                  : 'bg-surface3 text-label2 border-sep'"
              >
                <span class="stonedot" :class="t.installed ? 'bg-ok' : 'bg-muted'" />
                {{ t.installed ? '已安装' : '未安装' }}
              </span>
            </td>
            <td class="px-3 py-2 text-xs text-label2">{{ t.detail }}</td>
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
      <div v-else-if="!loading" class="p-6 text-sm text-label3">尚无受管目标</div>
      <div v-else class="p-6 text-sm text-label3">加载中…</div>
    </section>

    <!-- Docker 旁路：托管 venv 仅支持 Linux，已支持引擎可改用官方 docker 镜像 -->
    <!-- focus 命中非托管引擎（platform 不支持）或纯 docker 语境 focus 时高亮本区块； -->
    <!-- 隐藏 dockerBypass 区块时仍保留诊断按钮供 DockerInstallPanel 拉 platform。 -->
    <section
      id="docker-bypass"
      class="card space-y-3 transition-shadow"
      :class="focusNeedsBypass() ? 'ring-2 ring-warn' : ''"
    >
      <div class="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 class="text-sm font-medium text-label">Docker 旁路</h2>
          <p class="mt-0.5 text-xs text-label3">
            托管 venv 仅支持 Linux；下列引擎可改用官方 docker 镜像绕过 venv（编辑模型 yaml 后仍由 modelctl 启停）
          </p>
        </div>
        <div class="flex items-center gap-2">
          <span
            class="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs"
            :class="dockerEnv?.ready
              ? 'bg-ok-bg text-ok border-ok-line'
              : 'bg-danger-bg text-danger border-danger-line'"
            :title="dockerEnv && !dockerEnv.ready ? dockerEnv.missing.join('；') : undefined"
          >
            <span class="stonedot" :class="dockerEnv?.ready ? 'bg-ok' : 'bg-danger'" />
            {{ dockerEnv?.ready ? 'Docker 环境就绪' : 'Docker 环境缺失' }}
          </span>
          <button class="btn-ghost !py-1 !px-2 text-xs" :disabled="diagBusy" @click="onDiagnose">
            {{ diagBusy ? '诊断中…' : diagOpen ? '收起诊断' : '完整诊断' }}
          </button>
        </div>
      </div>

      <p v-if="dockerEnv && !dockerEnv.ready" class="text-xs text-label3">{{ dockerEnv.guide }}</p>

      <!-- 完整诊断：首次展开懒加载一次（后端含子进程探测） -->
      <div v-if="diagOpen" class="space-y-1.5 border-t border-sep pt-3">
        <p v-if="diagErr" class="text-xs text-red-400">{{ diagErr }}</p>
        <template v-else-if="diagData">
          <div v-for="c in diagData.checks" :key="c.key" class="flex flex-wrap items-center gap-x-2 text-xs">
            <span class="stonedot" :class="c.ok ? 'bg-ok' : 'bg-danger'" />
            <span class="text-label2">{{ c.label }}</span>
            <span v-if="!c.ok" class="break-all text-label3">{{ c.detail }}</span>
          </div>
          <div class="mt-2">
            <div class="mb-1 flex items-center justify-between">
              <span class="text-xs text-label3">
                安装脚本（复制到部署机 root shell；WebUI 只展示不执行）
              </span>
              <button class="btn-ghost !py-1 !px-2 text-xs" @click="copyText('inst', diagData.instructions)">
                {{ copiedKey === 'inst' ? '已复制' : '复制脚本' }}
              </button>
            </div>
            <pre
              class="max-h-72 overflow-auto rounded-ctl bg-code-bg p-3 font-mono text-xs leading-6 whitespace-pre text-code-fg"
            >{{ diagData.instructions }}</pre>
          </div>
        </template>
        <p v-else class="text-xs text-label3">诊断中…</p>
      </div>

      <p v-if="!dockerBypass.length" class="text-xs text-label3">
        当前无可用 Docker 镜像引擎（manifest 未加载或未声明引擎）；诊断按钮仍可用于探测 Docker 环境。
      </p>

      <!-- 逐引擎指引 -->
      <div v-if="dockerBypass.length" class="space-y-2">
        <div v-for="b in dockerBypass" :key="b.name" class="space-y-1.5 border-t border-sep pt-2.5">
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
            <span class="font-mono text-label2">{{ b.name }}</span>
            <span
              class="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5"
              :class="b.docker_supported
                ? 'bg-ok-bg text-ok border-ok-line'
                : 'bg-surface3 text-label2 border-sep'"
            >
              <span class="stonedot" :class="b.docker_supported ? 'bg-ok' : 'bg-muted'" />
              {{ b.docker_supported ? '支持 docker 运行时' : '暂不支持 docker' }}
            </span>
          </div>
          <p v-if="!b.docker_supported" class="text-xs text-label3">{{ b.note }}</p>
          <template v-else>
            <div class="flex flex-wrap items-center gap-2 text-xs">
              <span class="text-label3">yaml 片段</span>
              <code class="break-all text-label2">{{ b.yaml_field_path }}: {{ b.image_example }}</code>
              <button
                class="btn-ghost !py-0.5 !px-2 text-xs"
                @click="copyText(b.name, `${b.yaml_field_path}: ${b.image_example}`)"
              >
                {{ copiedKey === b.name ? '已复制' : '复制' }}
              </button>
              <span class="text-label3">·</span>
              <span class="break-all text-label3">示例 <code class="text-label2">{{ b.example_yaml }}</code></span>
            </div>
            <ol class="ml-4 list-decimal space-y-0.5 text-xs text-label2">
              <li v-for="(s, i) in b.steps" :key="i" class="break-all">{{ s }}</li>
            </ol>
          </template>
        </div>
      </div>
    </section>

    <!-- Docker 一键安装（Windows-only）：放在「Docker 旁路」section 之后，构成"Docker 环境补齐"入口。
         platform 取自 diagData.platform（Task 3 后端新增顶层字段；诊断未跑时默认为 linux，
         面板会降级为引导 alert 而非假装 Windows）。 -->
    <DockerInstallPanel
      :platform="diagData ? diagData.platform : 'linux'"
    />

    <!-- 非托管引擎说明：原生二进制 / 官方安装器 / 源码编译，不建 venv 故不在上表 -->
    <section v-if="unmanaged.length" class="card space-y-2">
      <div class="flex items-baseline justify-between">
        <h2 class="text-sm font-medium text-label">非托管引擎</h2>
        <span class="text-xs text-label3">原生或官方安装器，无需托管 venv</span>
      </div>
      <div v-for="u in unmanaged" :key="u.name" class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span class="font-mono text-label2">{{ u.name }}</span>
        <span
          class="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5"
          :class="u.installed
            ? 'bg-ok-bg text-ok border-ok-line'
            : 'bg-surface3 text-label2 border-sep'"
        >
          <span class="stonedot" :class="u.installed ? 'bg-ok' : 'bg-muted'" />
          {{ u.installed ? '已安装' : '未安装' }}
        </span>
        <span v-if="u.installed" class="break-all text-label3">{{ u.path }}</span>
        <code v-else class="break-all whitespace-pre-line text-label2">{{ u.install_hint }}</code>
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
