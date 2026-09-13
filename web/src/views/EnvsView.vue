<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { dockerDiagnose, envRemove, envSetup, envTargets } from '@/api/envs';
import type { DockerBypassEntry, DockerDiagnose, DockerEnv, EnvTarget, UnmanagedTarget } from '@/api/types';
import TaskButton from '@/components/common/TaskButton.vue';
import ConfirmDialog from '@/components/common/ConfirmDialog.vue';
import DataTable from '@/components/common/DataTable.vue';
import DockerInstallPanel from '@/components/docker/DockerInstallPanel.vue';
import { toast } from '@/utils/toast';

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
/** 在飞诊断 promise：页面入口平台探测与手动「完整诊断」共用，避免重复慢请求 */
let diagInflight: Promise<void> | null = null;
/** 复制反馈的当前 key（'inst' 或引擎名） */
const copiedKey = ref('');

/**
 * 宿主平台（QA-A-02）：不再在诊断未跑时假装 'linux'（会把 Windows 主机
 * 误判成 Linux 并隐藏「一键安装」入口）。诊断结果到达前返回 null，
 * 模板用「正在检测平台…」占位；DockerInstallPanel 只认 linux/windows，不动它。
 */
const hostPlatform = computed(() => diagData.value?.platform ?? null);

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

/** 展开/收起完整诊断；首次展开懒加载一次（与入口平台探测共用 inflight） */
async function onDiagnose() {
  diagOpen.value = !diagOpen.value;
  if (!diagOpen.value || diagData.value) return;
  await runDiagnose();
}

/**
 * 实际执行诊断（QA-A-02）：进入页面即异步拉一次，用其 platform 字段决定
 * DockerInstallPanel 的分支；该接口含子进程探测本身慢，结果只落 diagData，
 * 面板在结果到达前显示「正在检测平台…」占位。
 */
function runDiagnose(): Promise<void> {
  if (diagInflight) return diagInflight;
  diagBusy.value = true;
  diagErr.value = '';
  diagInflight = (async () => {
    try {
      diagData.value = await dockerDiagnose();
    } catch (err) {
      console.warn('dockerDiagnose 失败:', err);
      // QA-A-07：失败必须可见（模板红字渲染 diagErr），按钮保持可点以重试
      diagErr.value = (err as { message?: string })?.message || '诊断失败';
    } finally {
      diagBusy.value = false;
      diagInflight = null;
    }
  })();
  return diagInflight;
}

/** 复制任意文本（ConfigView copySnippet 同款 1.5s 反馈；失败 toast 可见化，QA-A-16） */
async function copyText(key: string, text: string) {
  try {
    await navigator.clipboard.writeText(text);
    copiedKey.value = key;
    setTimeout(() => (copiedKey.value = ''), 1500);
  } catch (err) {
    console.warn('复制失败:', err);
    toast.error('复制失败：浏览器拒绝了剪贴板写入');
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
  // 进入页面即异步探测宿主平台（QA-A-02：诊断接口慢，结果到达前面板显示占位）
  void runDiagnose();
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

    <!-- 表格（B-15：并入 DataTable 统一表格容器，用法同 ModelsListView） -->
    <DataTable v-if="targets.length">
      <table class="min-w-[42rem] text-sm">
        <thead>
          <tr>
            <th>目标</th>
            <th>状态</th>
            <th>说明</th>
            <th class="text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="t in targets"
            :id="`env-row-${t.name}`"
            :key="t.name"
            :class="focusName === t.name ? 'bg-warn-bg' : ''"
          >
            <td class="font-mono text-label">{{ t.name }}</td>
            <td>
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
            <td class="text-xs text-label2">{{ t.detail }}</td>
            <td class="text-right">
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
    </DataTable>
    <div v-else-if="!loading" class="card p-6 text-sm text-label3">尚无受管目标</div>
    <div v-else class="card p-6 text-sm text-label3">加载中…</div>

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
          <h2 class="mb-3 text-sm font-semibold text-label">Docker 旁路</h2>
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

      <!-- 诊断失败可见化（QA-A-07）：面板收起时（含入口平台探测失败）也要在按钮下方看到原因 -->
      <p v-if="diagErr && !diagOpen" class="text-xs text-red-400">
        {{ diagErr }}（可点「完整诊断」重试）
      </p>

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
         platform 取自 diagData.platform（QA-A-02：诊断未回时不再假装 'linux' 误判平台，
         外层 v-if 分流：未知 → 占位卡片；面板组件本身不动（只认 linux/windows）。 -->
    <section v-if="hostPlatform === null" class="card">
      <h2 class="text-sm font-semibold text-label">Docker 一键安装（Windows-only）</h2>
      <p class="mt-1 text-xs text-label3">
        {{ diagErr ? '平台检测失败，原因见上方「Docker 旁路」区块的红色提示；修复后可点「完整诊断」重试。' : '正在检测平台…' }}
      </p>
    </section>
    <DockerInstallPanel v-else :platform="hostPlatform" />

    <!-- 非托管引擎说明：原生二进制 / 官方安装器 / 源码编译，不建 venv 故不在上表 -->
    <section v-if="unmanaged.length" class="card space-y-2">
      <div class="flex items-baseline justify-between">
        <h2 class="mb-3 text-sm font-semibold text-label">非托管引擎</h2>
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
