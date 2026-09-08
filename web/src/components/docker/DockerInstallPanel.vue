<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { toast } from '@/utils/toast';
import {
  dockerDiagnose,
  fetchDockerInstallStatus,
  startDockerInstall,
  systemActionWindows,
  withSseAuthKey,
} from '@/api/envs';
import type {
  DockerSSEEvent,
  PostInstallStep,
} from '@/api/types';

/**
 * Docker 一键安装面板（Windows-only）—— 4 态状态机 + 4 按钮矩阵 + 阶段 B 卡片 + UAC 弹窗。
 *
 * 状态机（严格按 Task 4 spec Step 3）：
 *   idle         —— 面板初始，仅 "一键安装" 可点
 *   installing   —— SSE 流进行中，4 按钮全关
 *   need_reboot  —— 阶段 B 引导（post_install_plan 帧到达），3 按钮可点
 *   done         —— 用户 verify 通过，4 按钮全关 + "已就绪" 徽标
 *   error        —— SSE 收 type=error，"已就绪点我验证" 可点（retry）
 *
 * 4 按钮矩阵（严格按 Task 4 spec Step 3 表格）：
 *
 *   | 按钮                  | idle | installing | need_reboot | done | error    |
 *   | 一键安装              |  ✅  |   ❌       |    ❌       |  ❌  |   ❌     |
 *   | 打开 Docker Desktop   |  ❌  |   ❌       |    ✅       |  ❌  |   ❌     |
 *   | 重启计算机             |  ❌  |   ❌       |    ✅       |  ❌  |   ❌     |
 *   | 已就绪点我验证         |  ❌  |   ❌       |    ✅       |  ❌  |  ✅retry |
 *
 * SSE 消费：命名事件 `event: stage / log / done / heartbeat`（与 admin_router.
 * _sse_task_stream 约定一致，见 docs/known-pitfalls/backend/sse-named-events.md）。
 *
 * 后端契约以 src/modelctl/core/sse_stage_event.py + windows_setup.STAGES 为
 * 单一事实来源（12 值 stage 枚举）；前端只消费字面量进行 UI 分派。
 */
defineProps<{
  /** 宿主平台：linux 显示降级 alert，windows 显示完整面板 */
  platform: 'linux' | 'windows';
}>();

type Phase = 'idle' | 'installing' | 'need_reboot' | 'done' | 'error';
/**
 * EventSource 命名事件的 type 标记（与后端 _sse_task_stream 发帧的 name 严格对齐）。
 * "error" 是 EventSource 标准事件（关闭/重试前发），不同于后端业务错误帧（走 "stage" name）。
 */
type SseName = 'stage' | 'log' | 'done' | 'heartbeat' | 'message' | 'error';

const phase = ref<Phase>('idle');
/** 阶段 B 引导步骤（payload.steps） */
const steps = ref<PostInstallStep[]>([]);
/** 最近 200 行日志（超则 shift 首行，避免长安装期内存爆） */
const logs = ref<string[]>([]);
/** UAC 弹窗可见性（不中断安装） */
const uacDialogShow = ref(false);
/** 错误文案（phase=error 时展示） */
const errMsg = ref('');
/** install 任务 id（虚拟 ID，可直接展示） */
const taskId = ref('');

/** 4 段 steps 的 active 索引 */
const stepActive = computed(() => {
  switch (phase.value) {
    case 'idle':
      return 0;
    case 'installing':
      return 1;
    case 'need_reboot':
      return 2;
    case 'done':
    case 'error':
      return 3;
    default:
      return 0;
  }
});

/** 4 按钮矩阵（严格按 spec 表格实现） */
const canInstall = computed(() => phase.value === 'idle');
const canOpenDesktop = computed(() => phase.value === 'need_reboot');
const canRestart = computed(() => phase.value === 'need_reboot');
const canVerify = computed(() => phase.value === 'need_reboot' || phase.value === 'error');

/** EventSource 句柄（onBeforeUnmount 兜底 close，防网络泄漏） */
let esRef: EventSource | null = null;
/**
 * 已注册的监听器（与 esRef 同生命周期，closeSse 时逐个 removeEventListener 防重复订阅）。
 *
 * 说明：MessageEvent 是 Event 的子型，但 DOM 类型签名写的是 `EventListener = (event: Event) => void`；
 * removeEventListener 时需要传入 **同一个函数引用** 才能命中，故一个 handler 在添加与移除
 * 两处都得用 wrapper（不能 add 时用 `(e: MessageEvent) => ...`、remove 时换回裸 `(e: Event) => ...`）。
 */
type SseEntry = { name: SseName; matcher: EventListener };
let sseListeners: SseEntry[] = [];
/** fallback 5s 轮询 timer */
let statusTimer: number | undefined;

/** 清理已挂的监听器 */
function detachSseListeners() {
  if (!esRef) return;
  for (const { name, matcher } of sseListeners) {
    try {
      esRef.removeEventListener(name, matcher);
    } catch {
      // 已 close，忽略
    }
  }
  sseListeners = [];
}

/**
 * 打开 SSE：在 events URL 上拼 ?key=<token>（EventSource 无法自定义 header）。
 *
 * 命名事件（stage / log / done / message）统一走 `MessageEvent` 解析分支（后端所有结构化
 * 帧都由 _sse_task_stream 发 `event: <name>\ndata: <json>\n\n`，参见 known-pitfalls/
 * backend/sse-named-events.md）；heartbeat 是 10s 保活帧，error 是连接层错误，不需要解析。
 */
function openSse(eventsPath: string) {
  esRef?.close();
  esRef = null;
  sseListeners = [];
  const url = withSseAuthKey(eventsPath);
  const es = new EventSource(url);

  // 结构化消息帧：在 message 回调内做 `as MessageEvent` 类型收窄（运行时一定带 data）
  const dispStructHandler: EventListener = (raw) => {
    onSseEvent(raw as MessageEvent);
  };
  // EventSource 默认事件名是 "message"；这里显式覆盖，与命名帧同名处理
  const heartbeatHandler: EventListener = () => {
    // 保活帧忽略（需要排查时再打日志）
  };
  const errorHandler: EventListener = (e: Event) => onSseError(e);

  es.addEventListener('stage', dispStructHandler);
  es.addEventListener('log', dispStructHandler);
  es.addEventListener('done', dispStructHandler);
  es.addEventListener('message', dispStructHandler);
  es.addEventListener('heartbeat', heartbeatHandler);
  es.addEventListener('error', errorHandler);

  sseListeners = [
    { name: 'stage', matcher: dispStructHandler },
    { name: 'log', matcher: dispStructHandler },
    { name: 'done', matcher: dispStructHandler },
    { name: 'message', matcher: dispStructHandler },
    { name: 'heartbeat', matcher: heartbeatHandler },
    { name: 'error', matcher: errorHandler },
  ];
  esRef = es;
}

/** 关 SSE（幂等） */
function closeSse() {
  if (esRef) {
    detachSseListeners();
    try {
      esRef.close();
    } catch {
      // 已 close，忽略
    }
    esRef = null;
  }
  sseListeners = [];
  if (statusTimer !== undefined) {
    window.clearInterval(statusTimer);
    statusTimer = undefined;
  }
}

/**
 * SSE 单事件处理：按 type/stage 分派 5 个分支。
 *
 * - type=log          → 进 logs（限 200 行）
 * - type=error        → phase="error" + errMsg + tail 并入 logs
 * - stage=need_UAC    → 显 UAC 弹窗（不中断安装，continuing wait）
 * - stage=post_install_plan + payload.steps → phase="need_reboot" + 填 steps
 * - stage=done        → phase="done"
 */
function onSseEvent(e: MessageEvent) {
  let evt: DockerSSEEvent;
  try {
    evt = JSON.parse(String(e.data)) as DockerSSEEvent;
  } catch (err) {
    console.warn('DockerSSEEvent JSON 解析失败:', err, String(e.data).slice(0, 200));
    return;
  }
  // 1) error 帧
  if (evt.type === 'error') {
    phase.value = 'error';
    errMsg.value = evt.message || '安装失败';
    if (evt.payload && 'tail' in evt.payload && Array.isArray(evt.payload.tail)) {
      // 显示最后 50 行 tail
      logs.value = [...logs.value, ...evt.payload.tail].slice(-200);
    } else {
      logs.value.push(evt.message);
      if (logs.value.length > 200) logs.value.shift();
    }
    uacDialogShow.value = false;
    return;
  }
  // 2) log 帧
  if (evt.type === 'log') {
    logs.value.push(evt.message);
    if (logs.value.length > 200) logs.value.shift();
    return;
  }
  // 3) stage / complete 帧：按 stage 分派
  if (evt.stage === 'need_UAC') {
    // need_UAC 不中断安装（后端 UAC_SILENCE_SEC 监测继续等）；显弹窗
    uacDialogShow.value = true;
  } else if (
    evt.stage === 'post_install_plan' &&
    evt.payload &&
    'steps' in evt.payload &&
    Array.isArray(evt.payload.steps)
  ) {
    // 阶段 A 完成 + 阶段 B 引导步骤集合下发（后端 windows_setup L556 用 type='stage'）
    phase.value = 'need_reboot';
    steps.value = evt.payload.steps;
  } else if (evt.stage === 'done') {
    phase.value = 'done';
  }
}

function onSseError(e: Event) {
  // EventSource 自动重试 3 次；此处不 abort 面板
  console.warn('docker install SSE error:', e);
}

/** fallback 5s 轮询 GET /envs/docker/install/{id}：SSE 静默断时兜底 */
function startStatusFallback(t: string) {
  if (statusTimer !== undefined) window.clearInterval(statusTimer);
  statusTimer = window.setInterval(async () => {
    if (phase.value === 'done' || phase.value === 'error') {
      closeSse();
      return;
    }
    try {
      const s = await fetchDockerInstallStatus(t);
      if (s.done) {
        if (s.stage === 'done' && phase.value === 'installing') {
          phase.value = 'done';
        } else if (s.stage === 'error' && phase.value === 'installing') {
          phase.value = 'error';
          if (!errMsg.value) {
            errMsg.value = '安装进程已终止（SSE fallback 探测，见日志）';
          }
        }
      }
    } catch {
      // 网络抖动忽略
    }
  }, 5000);
}

/** 一键安装：调 startDockerInstall → 打开 SSE + 启动 fallback */
async function startInstall() {
  if (!canInstall.value) return;
  phase.value = 'installing';
  logs.value = [];
  steps.value = [];
  errMsg.value = '';
  taskId.value = '';
  try {
    const r = await startDockerInstall({ os: 'windows' });
    taskId.value = r.task_id;
    if (r.already_running) {
      toast.warning('5 分钟窗口内已有活跃安装任务，复用同一 task_id');
    }
    uacDialogShow.value = false;
    openSse(r.events);
    startStatusFallback(r.task_id);
  } catch (err) {
    phase.value = 'error';
    errMsg.value = (err as { message?: string })?.message || '提交 docker install 失败';
  }
}

/** 打开 Docker Desktop（阶段 B open_desktop） */
async function openDesktop() {
  if (!canOpenDesktop.value) return;
  try {
    const r = await systemActionWindows('open_desktop');
    toast.success(`已打开：${r.executed}`);
  } catch (err) {
    const msg = (err as { message?: string })?.message || '打开 Desktop 失败';
    toast.error(msg);
    if ((err as { response?: { status?: number } })?.response?.status === 400) {
      toast.error('当前 WebUI 主机非 Windows，open_desktop 仅 Windows 可用');
    }
  }
}

/** 重启计算机（阶段 B restart）—— 二次确认 5 秒 countdown */
async function doRestart() {
  if (!canRestart.value) return;
  try {
    // 项目未安装 element-plus，alert 直接用浏览器 confirm（语义等价）
    if (!window.confirm('确认 5 秒后重启本计算机？（WSL2 backend 启用需要）')) {
      return;
    }
    const r = await systemActionWindows('restart');
    // toast 仅有 success/error/warning 三种；重启排程语义上更接近"进行中提示"，用 warning 最贴近
    toast.warning(`已排程重启：${r.executed}`);
  } catch (err) {
    const msg = (err as { message?: string })?.message || '排程重启失败';
    toast.error(msg);
  }
}

/** 验证：调 dockerDiagnose('windows') 全 ok → done，否则回 need_reboot */
async function verify() {
  if (!canVerify.value) return;
  try {
    const d = await dockerDiagnose('windows');
    const allOk = d.checks.every((c) => c.ok);
    phase.value = allOk ? 'done' : 'need_reboot';
    if (allOk) toast.success('Windows Docker 环境已就绪');
    else toast.warning('诊断发现未就绪项，请继续完成阶段 B 后再验证');
  } catch (err) {
    const msg = (err as { message?: string })?.message || '验证失败';
    toast.error(msg);
    if ((err as { response?: { status?: number } })?.response?.status === 400) {
      toast.error('当前 WebUI 主机非 Windows，diagnose?os=windows 不可用');
    }
  }
}

/** 日志折叠展开态 + 自动滚到底 */
const logExpanded = ref(false);
const logBox = ref<HTMLElement | null>(null);
watch(
  () => logs.value.length,
  async () => {
    if (!logExpanded.value || !logBox.value) return;
    await nextTick();
    if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight;
  },
);

/** 关闭 UAC 弹窗（仅隐 UI，不 cancel 安装） */
function closeUac() {
  uacDialogShow.value = false;
}

onBeforeUnmount(closeSse);
</script>

<template>
  <div class="space-y-3">
    <!-- Linux 平台：降级 alert（不替操作系统） -->
    <div v-if="platform === 'linux'" class="card">
      <div class="flex items-start gap-3 text-sm">
        <span class="mt-1 size-2 rounded-full bg-blue-400" />
        <div>
          <h2 class="font-medium text-slate-200">Docker 一键安装（Windows-only）</h2>
          <p class="mt-1 text-xs leading-5 text-slate-400">
            当前 WebUI 主机是 <span class="font-mono text-slate-300">Linux</span>，Docker
            一键安装仅 Windows。Linux 可直接执行
            <code
              class="mt-1 inline-block rounded bg-[#0b1120] px-1.5 py-0.5 font-mono text-[11px] text-slate-300"
            >
              modelctl env setup docker --os=linux --run
            </code>
            或在下方「Docker 旁路」区块走官方镜像 + daemon.json 合并（诊断接口
            可通过 <code
              class="inline-block rounded bg-[#0b1120] px-1 py-0.5 font-mono text-[11px] text-slate-300"
            >?os=linux</code> 锁定平台）。
          </p>
        </div>
      </div>
    </div>

    <!-- Windows 平台：完整面板 -->
    <div v-else class="card space-y-3">
      <!-- 标题 + 状态徽标 -->
      <div class="flex items-baseline justify-between">
        <div>
          <h2 class="text-sm font-medium text-slate-200">Docker 一键安装（Windows-only）</h2>
          <p class="mt-0.5 text-xs text-slate-500">
            winget + Docker Desktop + WSL2；阶段 A 自动，阶段 B 用户引导
          </p>
        </div>
        <!-- 成功徽标（仅 done 阶段） -->
        <span
          v-if="phase === 'done'"
          class="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-600/15 px-2 py-0.5 text-xs text-emerald-300"
        >
          <span class="size-1.5 rounded-full bg-emerald-400" />
          已就绪
        </span>
        <!-- 错误徽标（仅 error 阶段） -->
        <span
          v-else-if="phase === 'error'"
          class="inline-flex items-center gap-1.5 rounded-full border border-red-500/30 bg-red-600/15 px-2 py-0.5 text-xs text-red-300"
        >
          <span class="size-1.5 rounded-full bg-red-400" />
          失败
        </span>
      </div>

      <!-- 4 段 steps：检测 → 下载安装 → 引导验证 → 完成 -->
      <ol class="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs">
        <li
          v-for="(label, i) in ['检测', '下载安装', '引导验证', '完成']"
          :key="label"
          :class="[
            'flex items-center gap-1.5',
            i === stepActive
              ? 'text-emerald-300'
              : i < stepActive
                ? 'text-emerald-400/70'
                : 'text-slate-500',
          ]"
        >
          <span
            :class="[
              'size-1.5 rounded-full',
              i === stepActive
                ? 'bg-emerald-400 animate-pulse'
                : i < stepActive
                  ? 'bg-emerald-400/70'
                  : 'bg-slate-600',
            ]"
          />
          <span>{{ label }}</span>
          <span v-if="i < 3" class="mx-1 text-slate-600">→</span>
        </li>
      </ol>

      <!-- 错误提示（error 阶段） -->
      <div
        v-if="phase === 'error' && errMsg"
        class="rounded-md border border-red-500/30 bg-red-600/10 px-3 py-2 text-sm leading-5 whitespace-pre-line text-red-300"
      >
        {{ errMsg }}
      </div>

      <!-- 阶段 B 卡片：遍历 steps，每项含 label + action 按钮 + cmd_hint code -->
      <div v-if="phase === 'need_reboot'" class="space-y-2">
        <p class="text-xs text-slate-400">
          阶段 A 已完成，请按顺序完成 Desktop 首次配置（WSL2 backend 需重启后生效）：
        </p>
        <div
          v-for="(s, idx) in steps"
          :key="s.id"
          class="rounded-md border border-slate-700/60 bg-slate-900/60 p-3"
        >
          <div class="flex flex-wrap items-center justify-between gap-2">
            <span class="text-sm text-slate-200">
              <span class="mr-2 text-slate-500">{{ idx + 1 }}.</span>
              {{ s.label }}
            </span>
            <div class="flex items-center gap-2">
              <span
                v-if="s.optional"
                class="rounded bg-slate-700/60 px-1.5 py-0.5 text-[10px] text-slate-400"
              >可选</span>
              <button
                v-if="s.action === 'open_desktop'"
                class="btn-ghost !py-1 !px-2 text-xs"
                :disabled="!canOpenDesktop"
                @click="openDesktop"
              >打开 Desktop</button>
              <button
                v-else-if="s.action === 'restart'"
                class="btn-danger !py-1 !px-2 text-xs"
                :disabled="!canRestart"
                @click="doRestart"
              >重启计算机</button>
              <button
                v-else-if="s.action === 'verify'"
                class="btn-primary !py-1 !px-2 text-xs"
                :disabled="!canVerify"
                @click="verify"
              >已就绪点我验证</button>
            </div>
          </div>
          <code
            v-if="s.cmd_hint"
            class="mt-2 block rounded bg-[#0b1120] p-2 font-mono text-[11px] leading-5 whitespace-pre-line break-all text-slate-300"
          >{{ s.cmd_hint }}</code>
        </div>
      </div>

      <!-- 4 按钮矩阵（严格按 Task 4 spec 表格） -->
      <div class="flex flex-wrap items-center gap-2 border-t border-slate-800/40 pt-3">
        <button class="btn-primary" :disabled="!canInstall" @click="startInstall">
          <svg
            v-if="phase === 'installing'"
            class="size-4 animate-spin"
            viewBox="0 0 24 24"
            fill="none"
          >
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
            <path d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" fill="currentColor" />
          </svg>
          {{ phase === 'installing' ? '安装中…' : '一键安装' }}
        </button>
        <button class="btn-ghost" :disabled="!canOpenDesktop" @click="openDesktop">
          打开 Docker Desktop
        </button>
        <button class="btn-danger" :disabled="!canRestart" @click="doRestart">
          重启计算机
        </button>
        <button class="btn-primary" :disabled="!canVerify" @click="verify">
          已就绪点我验证
        </button>
        <span class="ml-auto text-[11px] text-slate-500">
          任务 ID：{{ taskId || '—' }}
        </span>
      </div>

      <!-- 日志折叠（限制高度，避免长安装期内存爆） -->
      <div v-if="logs.length" class="rounded-md border border-slate-800/40">
        <button
          class="flex w-full items-center justify-between px-3 py-1.5 text-xs text-slate-400 hover:bg-slate-800/40"
          @click="logExpanded = !logExpanded"
        >
          <span>安装日志（{{ logs.length }} 行）</span>
          <span>{{ logExpanded ? '收起' : '展开' }}</span>
        </button>
        <pre
          v-if="logExpanded"
          ref="logBox"
          class="max-h-64 overflow-auto border-t border-slate-800/40 bg-[#0b1120] px-3 py-2 font-mono text-[11px] leading-5 whitespace-pre-wrap break-all text-slate-300"
        >{{ logs.join('\n') }}</pre>
      </div>

      <!-- UAC 弹窗：关闭按钮"不中断安装"（自定义样式，参考 ConfirmDialog 风格） -->
      <Teleport to="body">
        <Transition
          name="uac-fade"
          enter-active-class="transition duration-150"
          enter-from-class="opacity-0"
          leave-active-class="transition duration-100"
          leave-to-class="opacity-0"
        >
          <div
            v-if="uacDialogShow"
            class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
            @click.self="closeUac"
          >
            <div
              class="w-full max-w-md rounded-lg border border-amber-500/30 bg-slate-900 shadow-2xl"
              role="dialog"
              aria-modal="true"
            >
              <div class="flex items-center justify-between border-b border-slate-800 px-5 py-3">
                <h3 class="text-base font-semibold text-amber-300">需要授权</h3>
                <button
                  class="text-2xl leading-none text-slate-400 hover:text-slate-200"
                  aria-label="关闭"
                  @click="closeUac"
                >×</button>
              </div>
              <div class="px-5 py-4">
                <p class="whitespace-pre-line text-sm leading-6 text-slate-300">
                  Windows 已弹出 UAC 授权框——请在系统弹窗输入管理员密码并点"是"。
                  本安装任务未中断，授权后会自动继续。
                </p>
              </div>
              <div class="flex items-center justify-end border-t border-slate-800 px-5 py-3">
                <button class="btn-ghost" @click="closeUac">关闭（继续等待授权）</button>
              </div>
            </div>
          </div>
        </Transition>
      </Teleport>
    </div>
  </div>
</template>
