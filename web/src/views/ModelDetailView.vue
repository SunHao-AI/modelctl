<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { startModel, stopModel, restartModel, startModelUi, stopModelUi, getModel, getModelLog, getModelYaml, getModelLogStreamUrl } from '@/api/models';
import type { ModelDetail, YamlResponse } from '@/api/types';
import StatusBadge from '@/components/common/StatusBadge.vue';
import TaskButton from '@/components/common/TaskButton.vue';
import SseLogViewer from '@/components/common/SseLogViewer.vue';
import ConfirmDialog from '@/components/common/ConfirmDialog.vue';

/**
 * 模型详情：上部分览（状态/引擎/端口/PID/api_key/操作按钮），下部分 tab（工作日志 SSE / YAML / 配置），5s 轮询刷新
 */
const route = useRoute();
const router = useRouter();
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
const tab = ref<TabKey>('log');
/** 日志预填充行数 */
const logTail = ref(200);
const logInitial = ref<string[]>([]);
const yaml = ref<YamlResponse | null>(null);
const yamlErr = ref('');
let timer: number | undefined;

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
/** 拉取 YAML */
async function refreshYaml() {
  yamlErr.value = '';
  try {
    yaml.value = await getModelYaml(name.value);
  } catch (err) {
    console.warn('getModelYaml 失败:', err);
    yamlErr.value = (err as { message?: string })?.message || 'YAML 读取失败';
  }
}
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
  timer = window.setInterval(() => void refresh(), 5000);
});
onBeforeUnmount(() => {
  if (timer !== undefined) clearInterval(timer);
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
          <TaskButton label="启动" variant="primary" :task-target="() => startModel(name)" @success="() => refresh()" />
          <button class="btn-danger" :disabled="stopBusy || detail.state === 'stopped'" @click.stop="stopConfirm = true">{{ stopBusy ? '停止中…' : '停止' }}</button>
          <TaskButton label="重启" variant="ghost" :task-target="() => restartModel(name)" @success="() => refresh()" />
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
      <!-- YAML tab（拉取一次） -->
      <div v-else-if="tab === 'yaml'" class="space-y-3 p-3">
        <div v-if="yaml" class="flex items-center justify-between text-xs">
          <span class="font-mono text-slate-500">{{ yaml.path }}</span>
          <button class="btn-ghost !py-1 !px-2" @click="copyText(yaml.content)">复制</button>
        </div>
        <pre v-if="yaml" class="max-h-96 overflow-auto bg-[#0b1120] p-3 font-mono text-xs leading-6 text-slate-300 whitespace-pre-wrap">{{ yaml.content }}</pre>
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
