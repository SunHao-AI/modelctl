<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import dayjs from 'dayjs';
import { auditCleanup, auditList, auditPath, auditStats } from '@/api/audit';
import type { AuditEntry, AuditListResponse, AuditStatsResponse } from '@/api/types';
import ConfirmDialog from '@/components/common/ConfirmDialog.vue';
import DataTable from '@/components/common/DataTable.vue';

/**
 * 审计日志：工具条（since/level/keyword + 搜索）+ 统计卡（总量/错误数/近14天）+
 * 列表（行点击展开完整 JSON）+ 底部清理（30 天前 JSONL，ConfirmDialog 防误触）
 */
/** 下拉选项 */
const SINCE_OPTIONS = [
  { value: '10m', label: '10 分钟' },
  { value: '1h', label: '1 小时' },
  { value: '6h', label: '6 小时' },
  { value: '24h', label: '24 小时' },
  { value: '7d', label: '7 天' },
  { value: '30d', label: '30 天' },
];
const LEVEL_OPTIONS = [
  { value: 'all', label: '全部' },
  { value: 'info', label: 'info' },
  { value: 'warn', label: 'warn' },
  { value: 'error', label: 'error' },
];

/** 表单 */
const since = ref('24h');
const level = ref('all');
const keyword = ref('');
const list = ref<AuditListResponse | null>(null);
const stats = ref<AuditStatsResponse | null>(null);
const auditDir = ref('');
const errMsg = ref('');
const loading = ref(false);
/** 展开行（key = 时间戳-模型名） */
const expanded = ref<Record<string, boolean>>({});
/** 清理对话框 */
const cleanupOpen = ref(false);
const cleanupBusy = ref(false);
const cleanupResult = ref<{ removed: number; freed_bytes: number } | null>(null);

/** 搜索：并行拉取列表 / 统计 / 路径 */
async function search() {
  loading.value = true;
  try {
    const [l, s, p] = await Promise.all([
      auditList({ since: since.value || undefined, level: level.value === 'all' ? undefined : level.value, keyword: keyword.value.trim() || undefined, limit: 200 }),
      auditStats(since.value || undefined),
      auditPath(),
    ]);
    list.value = l;
    stats.value = s;
    auditDir.value = p.path ?? '';
    errMsg.value = '';
  } catch (err) {
    console.warn('audit 加载失败:', err);
    errMsg.value = (err as { message?: string })?.message || '审计日志加载失败';
  } finally {
    loading.value = false;
  }
}
onMounted(search);

/** 行 key（时间戳 + 模型名，保证唯一） */
function rowKey(e: AuditEntry, i: number): string {
  const ts = e.time ?? e.ts ?? e.timestamp ?? String(i);
  return `${ts}-${e.model ?? ''}`;
}
/** 行点击展开/收起 */
function toggle(e: AuditEntry, i: number) {
  const k = rowKey(e, i);
  expanded.value = { ...expanded.value, [k]: !expanded.value[k] };
}
/** 级别 badge 样式 */
function levelStyle(level: string | undefined): string {
  if (level === 'error') return 'border-danger-line bg-danger-bg text-danger';
  if (level === 'warn') return 'border-warn-line bg-warn-bg text-warn';
  if (level === 'info') return 'border-accent-line bg-accent-bg text-accent';
  return 'border-sep bg-surface3 text-label2';
}
/** 摘要：端点 · model · status（网关写入键是 path，endpoint 仅旧格式兼容） */
function summaryOf(e: AuditEntry): string {
  const ep = (e.path ?? e.endpoint ?? '').toString();
  const model = (e.model ?? '').toString();
  const status = e.status_code ?? e.status;
  const parts: string[] = [];
  if (ep) parts.push(ep);
  if (model) parts.push(model);
  if (status) parts.push(`${status}`);
  return parts.join(' · ') || '—';
}
/** 清理 30 天前（ConfirmDialog 确认后才执行） */
async function doCleanup() {
  cleanupBusy.value = true;
  cleanupResult.value = null;
  try {
    const r = await auditCleanup(30);
    cleanupResult.value = { removed: r.removed, freed_bytes: r.freed_bytes };
    void search();
  } catch (err) {
    console.warn('cleanup 失败:', err);
    errMsg.value = (err as { message?: string })?.message || '清理失败';
  } finally {
    cleanupBusy.value = false;
  }
}
/** 近 14 天（取 by_day 末 14 天） */
const recentDays = computed(() => (stats.value?.by_day ?? []).slice(-14));
/** 柱状图归一化最大值 */
const maxDay = computed(() => {
  const arr = recentDays.value;
  if (!arr.length) return 1;
  return Math.max(...arr.map((d) => Math.max(d.total, d.error))) || 1;
});
</script>

<template>
  <div class="space-y-4">
    <!-- 工具条 -->
    <div class="card">
      <div class="flex flex-wrap items-end gap-3">
        <div>
          <label class="label-base">时间范围</label>
          <select v-model="since" class="input-base !w-32">
            <option v-for="o in SINCE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
        </div>
        <div>
          <label class="label-base">级别</label>
          <select v-model="level" class="input-base !w-28">
            <option v-for="o in LEVEL_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
        </div>
        <div class="flex-1 min-w-48">
          <label class="label-base">关键字</label>
          <input v-model="keyword" class="input-base" placeholder="搜索 model / endpoint 等" @keyup.enter="search" />
        </div>
        <button class="btn-primary" :disabled="loading" @click="search">搜索</button>
      </div>
    </div>
    <p v-if="errMsg" class="text-sm text-red-400">{{ errMsg }}</p>
    <!-- 统计卡 -->
    <div v-if="stats" class="grid grid-cols-1 gap-4 md:grid-cols-3">
      <section class="card">
        <h3 class="mb-2 text-xs font-medium uppercase tracking-wider text-label3">总量</h3>
        <div class="num text-2xl font-semibold text-label">{{ stats.total }}</div>
      </section>
      <section class="card">
        <h3 class="mb-2 text-xs font-medium uppercase tracking-wider text-label3">错误数</h3>
        <div class="num text-2xl font-semibold text-red-400">{{ list?.error_count ?? 0 }}</div>
        <p v-if="stats.by_model && Object.keys(stats.by_model).length" class="mt-1 text-xs text-label3">涉及 {{ Object.keys(stats.by_model).length }} 个模型</p>
      </section>
      <section class="card !p-4">
        <h3 class="mb-2 text-xs font-medium uppercase tracking-wider text-label3">近 14 天</h3>
        <div v-if="recentDays.length" class="flex h-16 items-end gap-1">
          <div v-for="d in recentDays" :key="d.date" :title="`${d.date}: ${d.total} req / ${d.error} err`" class="flex flex-1 flex-col-reverse gap-0.5">
            <div class="rounded-t-sm" :style="{ height: `${(d.total / maxDay) * 60}px`, background: 'var(--accent)' }" />
            <div class="rounded-b-sm" :style="{ height: `${(d.error / maxDay) * 24}px`, background: 'var(--danger)' }" />
          </div>
        </div>
        <p v-else class="text-sm text-label3">无数据</p>
      </section>
    </div>
    <!-- 列表 -->
    <DataTable v-if="list && list.entries.length">
      <table class="min-w-[52rem] text-sm">
        <thead>
          <tr>
            <th class="min-w-32">时间</th>
            <th class="min-w-16">级别</th>
            <th class="min-w-36">模型</th>
            <th class="min-w-64">摘要</th>
            <th class="text-right">状态</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="(e, i) in list.entries" :key="rowKey(e, i)">
            <tr class="cursor-pointer" @click="toggle(e, i)">
              <td class="num font-mono text-xs text-label2">{{ dayjs(e.time ?? e.ts ?? e.timestamp ?? 0).format('MM-DD HH:mm:ss') }}</td>
              <td>
                <span :class="['inline-block rounded border px-1.5 py-0.5 text-xs', levelStyle(e.level)]">{{ e.level ?? 'log' }}</span>
              </td>
              <td class="max-w-56 truncate font-mono" :title="e.model ?? ''">{{ e.model ?? '—' }}</td>
              <td class="max-w-md truncate text-label2" :title="summaryOf(e)">{{ summaryOf(e) }}</td>
              <td class="num text-right font-mono text-xs text-label2">{{ e.status ?? e.status_code ?? '—' }}</td>
            </tr>
            <!-- 展开行：完整 JSON -->
            <tr v-if="expanded[rowKey(e, i)]">
              <td colspan="5" class="!p-0">
                <pre class="code-surface max-h-96 overflow-auto whitespace-pre-wrap p-3 text-xs leading-6">{{ JSON.stringify(e, null, 2) }}</pre>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </DataTable>
    <div v-else-if="!loading" class="card text-sm text-label3">无匹配记录</div>
    <div v-else class="card text-sm text-label3">加载中…</div>
    <!-- 底部：清理 + 路径 -->
    <section class="card flex flex-wrap items-center justify-between">
      <div class="break-all font-mono text-xs text-label3">审计目录: {{ auditDir || '—' }}</div>
      <div class="flex items-center gap-3">
        <button class="btn-ghost" @click="search">重新加载</button>
        <button class="btn-danger" @click="cleanupOpen = true">清理 30 天前</button>
      </div>
    </section>
    <!-- 清理确认 -->
    <ConfirmDialog :open="cleanupOpen" title="清理审计日志" message="确认清理 30 天前的审计 JSONL 文件？该操作不会清空今日文件。" danger :loading="cleanupBusy" confirm-text="清理" @confirm="doCleanup" @cancel="cleanupOpen = false" />
    <!-- 清理结果提示 -->
    <Transition>
      <p v-if="cleanupResult" class="num text-sm text-ok">已清理 {{ cleanupResult.removed }} 个文件，释放 {{ (cleanupResult.freed_bytes / 1024 / 1024).toFixed(1) }} MB</p>
    </Transition>
  </div>
</template>
