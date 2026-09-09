<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { startModel, stopModel, restartModel, startModelUi, stopModelUi, getModel, getModelLog, getModelYaml, getModelLogStreamUrl, getStartup } from '@/api/models';
import type { ModelDetail, StartupSnapshot, YamlResponse } from '@/api/types';
import { useTasksStore } from '@/stores/tasks';
import StatusBadge from '@/components/common/StatusBadge.vue';
import TaskButton from '@/components/common/TaskButton.vue';
import SseLogViewer from '@/components/common/SseLogViewer.vue';
import ConfirmDialog from '@/components/common/ConfirmDialog.vue';
import StartupProgressCard from '@/components/startup/StartupProgressCard.vue';

/**
 * 模型详情：上部分览（状态/引擎/端口/PID/api_key/操作按钮），下部分 tab（工作日志 SSE / YAML / 配置），5s 轮询刷新
 */
const route = useRoute();
const router = useRouter();
const tasksStore = useTasksStore();
/** 模型名（路由参数） */
const name = computed(() => String(route.params.name ?? ''));
const detail = ref<ModelDetail | null>(null);
const errMsg = ref('');
const stopNotice = ref('');
const uiNotice = ref('');
const stopBusy = ref(false);
const uiBusy = ref(false);
const stopConfirm = ref(false);
type TabKey = 'log' | 'yaml' | 'overview';
const tab = ref<TabKey>('yaml');
/** 日志预填充行数 */
const logTail = ref(200);
const logInitial = ref<string[]>([]);
const yaml = ref<YamlResponse | null>(null);
const yamlErr = ref('');
/** YAML 编辑文本（textarea v-model）。null 表示尚未拉取；非 null 时 isDirty 据此计算。 */
const yamlEdit = ref<string | null>(null);
let timer: number | undefined;
const startup = ref<StartupSnapshot | null>(null);
let startupTimer: number | undefined;
/** YAML 编辑是否偏离源文件（isDirty） */
const yamlDirty = computed(() => !!yamlEdit.value && !!yaml.value && yamlEdit.value !== yaml.value.content);

/** 拉取模型详情 */
async function refresh() {
  try {
    detail.value = await getModel(name.value);
    errMsg.value = '';
  } catch (err) {
    console.warn('getModel 失败:', err);
    errMsg.value = (err as { message?: string })?.message || '模型详情读取失败';
  }
}
/** 预填充日志尾部 */
async function refreshLog() {
  try {
    const r = await getModelLog(name.value, logTail.value);
    logInitial.value = r.lines;
  } catch (err) {
    console.warn('getModelLog 失败:', err);
  }
}
/** 拉取 YAML（并把编辑态初始化为本源内容；不覆盖用户已编辑的文本） */
async function refreshYaml() {
  yamlErr.value = '';
  try {
    const r = await getModelYaml(name.value);
    yaml.value = r;
    // 编辑态未动过（null）时按源初始化；用户已编辑时保留原文（不覆盖）
    if (yamlEdit.value === null) yamlEdit.value = r.content;
    else if (yaml.value && yaml.value.content === r.content) {
      // 源文件未变化但 yamlEdit 已被设为"与源一致的副本"——无变更
    }
  } catch (err) {
    console.warn('getModelYaml 失败:', err);
    yamlErr.value = (err as { message?: string })?.message || 'YAML 读取失败';
  }
}
/** 个性化启动：用编辑后的 YAML 文本作为 override 提交（源 yaml 文件不改）。
 *
 * 走与"启动"按钮**同一套**任务流 + tasksStore（SSE / 轮询 / 进度卡片 / 终态 toast
 * 全部复用）。`retryFn` 闭包重放 overrideFn 中的 yamlOverride——重试也保持 override 语义。
 */
function applyYamlAndStart() {
  if (yamlEdit.value === null || !yaml.value || !yamlDirty.value) return;
  const text = yamlEdit.value;
  const fn = () => startModel(name.value, { yamlOverride: text });
  void fn().then(
    (refVal) => {
      tasksStore.track(refVal, {
        target: name.value,
        retryFn: fn,
        onSuccess: () => { refresh(); void refreshStartup(); },
        onError: (msg) => { yamlNotice.value = msg; },
      });
    },
    (err) => {
      yamlNotice.value = (err as { message?: string })?.message || '个性化启动提交失败';
    },
  );
}
/** 个性化启动通知（YAML tab 顶部 banner） */
const yamlNotice = ref('');
/** 恢复源文件内容（只编辑态，不写盘） */
function resetYamlEdit() {
  if (yaml.value) yamlEdit.value = yaml.value.content;
  yamlNotice.value = '';
}
/** 拉一次启动进度快照（仅无记录 404 → 清空卡片；其它故障保留上一帧防闪烁） */
async function refreshStartup() {
  try {
    startup.value = await getStartup(name.value);
  } catch (err) {
    if ((err as { response?: { status?: number } }).response?.status === 404) {
      startup.value = null;
    }
  }
}
/** 卡片可见：启动中/停止态但进度未收尾，或失败收尾 */
const showStartup = computed(() => {
  const s = startup.value;
  if (!s) return false;
  if (s.stages.some((x) => x.status === 'error')) return true;
  if (detail.value?.state === 'running') return false;
  return s.stages.some((x) => x.status === 'running' || x.status === 'pending');
});
/** 启动进行中等价态：本模型有 queued/running 任务（后端无 starting 状态，store 派生） */
const startupTaskActive = computed(() => {
  const rec = tasksStore.activityFor(name.value);
  return !!rec && (rec.status === 'queued' || rec.status === 'running');
});
/** SSE 日志流地址（计算属性，供 SseLogViewer 使用） */
const logStreamUrl = computed(() => getModelLogStreamUrl(name.value));
/** 是否 unsloth 引擎（可开启 Unsloth Web 控制台） */
const isUnsloth = computed(() => detail.value?.engine === 'unsloth');
/** 复制到剪贴板 */
async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (err) {
    console.warn('copy 失败:', err);
  }
}
/** 停止（同步） */
async function doStop() {
  stopBusy.value = true;
  stopNotice.value = '';
  try {
    const r = await stopModel(name.value);
    stopNotice.value = r.detail || (r.ok ? '已发送停止请求' : '停止请求未确认');
  } catch (err) {
    stopNotice.value = (err as { message?: string })?.message || '停止请求失败';
  } finally {
    stopBusy.value = false;
    stopConfirm.value = false;
  }
}
/** 开启 Unsloth Web 控制台（同步） */
async function onUiStart() {
  uiBusy.value = true;
  uiNotice.value = '';
  try {
    const r = await startModelUi(name.value);
    uiNotice.value = r.detail || (r.ok ? '已启动 Web 控制台' : '启动失败');
  } catch (err) {
    uiNotice.value = (err as { message?: string })?.message || 'Web 控制台启动失败';
  } finally {
    uiBusy.value = false;
  }
}
/** 关闭 Unsloth Web 控制台（同步） */
async function onUiStop() {
  uiBusy.value = true;
  uiNotice.value = '';
  try {
    const r = await stopModelUi(name.value);
    uiNotice.value = r.detail || (r.ok ? '已停止 Web 控制台' : '停止失败');
  } catch (err) {
    uiNotice.value = (err as { message?: string })?.message || 'Web 控制台关闭失败';
  } finally {
    uiBusy.value = false;
  }
}
/** 返回模型列表 */
function backTo() {
  router.push({ name: 'models-list' });
}
onMounted(() => {
  void refresh();
  void refreshLog();
  void refreshStartup();
  void refreshYaml(); // 进入详情页自动显示 YAML（不切 tab，仅预热数据；未来切换零延迟）
  // 5s 轮询 detail + 日志预填充（log tab 当前可见时），保证 docker runtime
  // 即使 SSE 没实时增量（如 docker-in-docker / 容器已删）也能 5s 间隔刷新
  timer = window.setInterval(() => {
    void refresh();
    if (tab.value === 'log') void refreshLog();
    // yaml tab 当前可见且用户未编辑时，跟源文件同步（避免源被改动的陈旧态）
    if (tab.value === 'yaml' && !yamlDirty.value) void refreshYaml();
  }, 5000);
  // 设计 §4.8：仅启动期间 2s 轮询进度。门控：本模型有进行中的 start/restart 任务
  // （后端无 starting 状态，由 tasks store 派生）或正在展示启动卡片（含最近失败）。
  // 不满足直接跳过本轮请求，避免常驻轮询；404 清空逻辑在 refreshStartup 内保留。
  startupTimer = window.setInterval(() => {
    if (!startupTaskActive.value && !showStartup.value) return;
    void refreshStartup();
  }, 2000);
});
onBeforeUnmount(() => {
  if (timer !== undefined) clearInterval(timer);
  if (startupTimer !== undefined) clearInterval(startupTimer);
});
// 切到 yaml tab 时拉一次
watch(tab, (t) => {
  if (t === 'yaml' && !yaml.value && !yamlErr.value) void refreshYaml();
});
/** engine_config 转 key-value 列表（嵌套对象序列化为 JSON） */
function engineConfigEntries(): Array<{ key: string; value: string }> {
  if (!detail.value) return [];
  return Object.entries(detail.value.engine_config ?? {}).map(([k, v]) => ({
    key: k,
    value: typeof v === 'object' ? JSON.stringify(v) : String(v ?? ''),
  }));
}
</script>

<template>
  <div class="space-y-4">
    <!-- 返回按钮 -->
    <button class="btn-ghost" @click="backTo()">
      <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5" /><path d="M12 19l-7-7 7-7" /></svg>
      返回模型列表
    </button>
    <!-- 上部分览 -->
    <section class="card">
      <div v-if="detail" class="space-y-3">
        <div class="flex flex-wrap items-center gap-3">
          <h2 class="text-lg font-semibold text-slate-100">{{ detail.name }}</h2>
          <StatusBadge :state="detail.state" :health="detail.health" />
          <span v-if="isUnsloth" class="rounded-full bg-blue-600/15 px-2 py-0.5 text-xs text-blue-300">unsloth</span>
        </div>
        <div class="grid grid-cols-2 gap-x-8 gap-y-2 text-sm md:grid-cols-4">
          <div><span class="text-slate-400">引擎</span><div class="font-mono text-slate-100">{{ detail.engine }}</div></div>
          <div><span class="text-slate-400">端口</span><div class="font-mono text-slate-100">{{ detail.port || '—' }}</div></div>
          <div><span class="text-slate-400">PID</span><div class="font-mono text-slate-100">{{ detail.pid ?? '—' }}</div></div>
          <div>
            <span class="text-slate-400">API Key</span>
            <div class="flex items-center gap-2 font-mono text-slate-100">
              {{ detail.api_key_masked || '未配置' }}
              <button v-if="detail.api_key_masked" class="text-xs text-slate-500 hover:text-slate-300" @click="copyText(detail.api_key_masked!)">复制</button>
            </div>
          </div>
        </div>
        <!-- 操作按钮（外部写操作，stop 走 ConfirmDialog 防误触） -->
        <div class="flex flex-wrap items-center gap-3 pt-2" @click.stop>
          <TaskButton label="启动" variant="primary" :target="name" :task-target="() => startModel(name)" @success="() => { refresh(); void refreshStartup(); }" />
          <button class="btn-danger" :disabled="stopBusy || detail.state === 'stopped'" @click.stop="stopConfirm = true">{{ stopBusy ? '停止中…' : '停止' }}</button>
          <TaskButton label="重启" variant="ghost" :target="name" :task-target="() => restartModel(name)" @success="() => { refresh(); void refreshStartup(); }" />
          <!-- Unsloth Web 控制台（同步，仅 unsloth 引擎可启动） -->
          <template v-if="isUnsloth">
            <span class="mx-1 h-4 w-px bg-slate-700" />
            <button class="btn-ghost" :disabled="uiBusy" @click.stop="onUiStart">开启</button>
            <button class="btn-ghost" :disabled="uiBusy" @click.stop="onUiStop">关闭</button>
          </template>
          <button class="btn-ghost" @click.stop="refreshLog(); refresh()">刷新日志</button>
        </div>
        <p v-if="stopNotice" class="text-xs text-slate-400">{{ stopNotice }}</p>
        <p v-if="uiNotice" class="text-xs text-slate-400">{{ uiNotice }}</p>
      </div>
      <div v-else class="py-4 text-sm text-slate-500">加载中…</div>
      <p v-if="errMsg" class="pt-2 text-sm text-red-400">{{ errMsg }}</p>
    </section>
    <!-- 启动进度卡片（启动中 / 启动失败时常驻） -->
    <StartupProgressCard
      v-if="showStartup && startup"
      :snapshot="startup"
      :to-env-page="() => router.push({ name: 'envs' })"
    />
    <!-- 中部 tab：工作日志 / YAML / 配置 -->
    <section class="card !p-0">
      <div class="flex items-center gap-1 border-b border-slate-800 px-2">
        <button
          v-for="t in (['log', 'yaml', 'overview'] as const)" :key="t"
          :class="['px-4 py-2.5 text-sm transition-colors', tab === t ? 'border-b-2 border-blue-500 text-blue-300' : 'text-slate-400 hover:text-slate-200']"
          @click="tab = t"
        >{{ t === 'log' ? '工作日志' : t === 'yaml' ? 'YAML' : '配置' }}</button>
      </div>
      <!-- 工作日志 tab（SSE 实时流 + 预填充尾部） -->
      <div v-if="tab === 'log'" class="space-y-3 p-3">
        <div class="flex items-center gap-3">
          <label class="label-base !mb-0">尾部行数</label>
          <select v-model.number="logTail" class="input-base !w-28" @change="refreshLog()">
            <option :value="100">100</option><option :value="200">200</option><option :value="500">500</option><option :value="1000">1000</option>
          </select>
          <button class="btn-ghost !py-1 !px-2 text-xs" @click="refreshLog">拉取最新</button>
        </div>
        <SseLogViewer :url="logStreamUrl" :tail-lines="logTail" :initial="logInitial" />
      </div>
      <!-- YAML tab（自动加载 + 可编辑；override 不影响源文件） -->
      <div v-else-if="tab === 'yaml'" class="space-y-3 p-3">
        <div v-if="yaml" class="flex flex-wrap items-center justify-between gap-2 text-xs">
          <span class="font-mono text-slate-500">{{ yaml.path }}（源文件，仅展示；编辑不影响源）</span>
          <div class="flex items-center gap-2">
            <label class="flex items-center gap-1 text-slate-400">
              <input
                type="checkbox"
                :checked="yamlDirty"
                class="h-3.5 w-3.5 accent-emerald-500"
                :title="yamlDirty ? '编辑态偏离源文件' : '与源文件一致'"
                disabled
              >
              {{ yamlDirty ? '已修改' : '与源一致' }}
            </label>
            <button class="btn-ghost !py-1 !px-2" :disabled="!yamlDirty" :title="yamlDirty ? '恢复为源文件内容' : '当前与源一致'" @click="resetYamlEdit">重置</button>
            <button class="btn-ghost !py-1 !px-2" @click="copyText(yamlEdit || yaml.content)">复制</button>
            <button
              class="btn-primary !py-1 !px-2"
              :disabled="!yamlDirty"
              :title="yamlDirty ? '用上方编辑的 YAML 文本启动（源文件不改）' : '请先编辑 YAML'"
              @click="applyYamlAndStart"
            >应用并启动</button>
          </div>
        </div>
        <!-- 个性化启动通知 -->
        <div v-if="yamlNotice" class="rounded-md bg-amber-600/10 border border-amber-600/40 px-3 py-2 text-xs text-amber-200 whitespace-pre-wrap">{{ yamlNotice }}</div>
        <!-- 可编辑 YAML（font-mono + 等宽行高；max-h 可控；不自动换行，横向滚动看长行） -->
        <textarea
          v-if="yaml && yamlEdit !== null"
          v-model="yamlEdit"
          spellcheck="false"
          class="block w-full min-h-[460px] max-h-[640px] resize-y overflow-auto bg-[#0b1120] p-3 font-mono text-xs leading-6 text-slate-200 whitespace-pre rounded-md border border-slate-700"
        ></textarea>
        <p v-else-if="yamlErr" class="text-sm text-red-400">{{ yamlErr }}</p>
        <p v-else class="py-4 text-sm text-slate-500">加载中…</p>
      </div>
      <!-- 配置 tab（profile + engine_config 键值） -->
      <div v-else class="p-3">
        <div class="mb-3 text-xs text-slate-500">来自 GET /models/{{ name }}（engine_config + profile 字段）</div>
        <table v-if="detail" class="w-full text-sm">
          <tbody>
            <tr v-for="row in engineConfigEntries()" :key="row.key" class="border-b border-slate-800/50">
              <td class="w-48 py-2 text-slate-400">engine_config.{{ row.key }}</td>
              <td class="font-mono text-slate-200 break-all">{{ row.value || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
    <!-- 停止确认 -->
    <ConfirmDialog :open="stopConfirm" title="停止模型" :message="`确认停止模型 ${name}？`" danger :loading="stopBusy" confirm-text="停止" @confirm="doStop" @cancel="stopConfirm = false" />
  </div>
</template>
