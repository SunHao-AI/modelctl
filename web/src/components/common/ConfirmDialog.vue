<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue';

const props = withDefaults(
  defineProps<{
    /** 是否显示 */
    open: boolean;
    /** 标题 */
    title?: string;
    /** 主体信息 */
    message?: string;
    /** 危险操作（红按钮） */
    danger?: boolean;
    /** 确认按钮文案 */
    confirmText?: string;
    /** 取消按钮文案 */
    cancelText?: string;
    /** 提交中（禁用交互、显示 loading） */
    loading?: boolean;
  }>(),
  {
    open: false,
    title: '确认操作',
    message: '',
    danger: false,
    confirmText: '确认',
    cancelText: '取消',
    loading: false,
  },
);

const emit = defineEmits<{
  (e: 'confirm'): void;
  (e: 'cancel'): void;
}>();

// 本地显示状态（避免父组件立即改 open 导致闪现）
const visible = ref(props.open);
watch(
  () => props.open,
  (v) => (visible.value = v),
);

function close() {
  emit('cancel');
}

// Esc 关闭
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') close();
}
onMounted(() => {
  document.addEventListener('keydown', onKeydown);
});
onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown);
});
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      @click.self="close"
    >
      <div
        class="w-full max-w-md rounded-lg border border-slate-700 bg-slate-900 shadow-xl"
        role="dialog"
        aria-modal="true"
      >
        <!-- 标题 -->
        <div class="flex items-center justify-between border-b border-slate-800 px-5 py-3">
          <h3 :class="['text-base font-semibold', props.danger ? 'text-red-300' : 'text-slate-100']">
            {{ props.title }}
          </h3>
          <button class="text-slate-400 hover:text-slate-200 text-xl leading-none" aria-label="关闭" @click="close">
            ×
          </button>
        </div>
        <!-- 主体 -->
        <div class="px-5 py-4">
          <p class="text-sm text-slate-300 whitespace-pre-line">{{ props.message }}</p>
        </div>
        <!-- 按钮 -->
        <div class="flex items-center justify-end gap-3 border-t border-slate-800 px-5 py-3">
          <button class="btn-ghost" :disabled="props.loading" @click="close">{{ props.cancelText }}</button>
          <button
            :class="props.danger ? 'btn-danger' : 'btn-primary'"
            :disabled="props.loading"
            @click="emit('confirm')"
          >
            <svg v-if="props.loading" class="size-4 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" />
            </svg>
            {{ props.confirmText }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
