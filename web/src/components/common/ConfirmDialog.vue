<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue';
import { X } from 'lucide-vue-next';
import Loading from './Loading.vue';

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
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
      @click.self="close"
    >
      <div
        class="w-full max-w-md rounded-panel border border-sep bg-surface2 shadow-l"
        role="dialog"
        aria-modal="true"
      >
        <!-- 标题 -->
        <div class="flex items-center justify-between border-b border-sep px-5 py-3">
          <h3 :class="['text-base font-semibold', props.danger ? 'text-danger' : 'text-label']">
            {{ props.title }}
          </h3>
          <button
            class="grid size-8 shrink-0 place-items-center rounded-ctl text-label3 transition-colors hover:bg-surface3 hover:text-label"
            aria-label="关闭"
            @click="close"
          >
            <X :size="18" :stroke-width="1.9" />
          </button>
        </div>
        <!-- 主体 -->
        <div class="px-5 py-4">
          <p class="text-sm text-label2 whitespace-pre-line">{{ props.message }}</p>
        </div>
        <!-- 按钮 -->
        <div class="flex items-center justify-end gap-3 border-t border-sep px-5 py-3">
          <button class="btn-ghost" :disabled="props.loading" @click="close">{{ props.cancelText }}</button>
          <button
            :class="props.danger ? 'btn-danger' : 'btn-primary'"
            :disabled="props.loading"
            @click="emit('confirm')"
          >
            <Loading v-if="props.loading" inline label="" />
            {{ props.confirmText }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
