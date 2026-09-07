/**
 * 轻量全局 toast：固定右上角、自动消失，不依赖任何组件库（项目未装 element-plus）。
 *
 * 用法：toast.success('...') / toast.error('...', 5000) / toast.warning('...')
 * duration 毫秒，缺省 3000（error 4000）；传 0 表示常驻（toast.dismiss(id) 手动关）。
 * 渲染宿主：TaskDrawer.vue（常驻 Header）经 useToastItems() 拿 items 渲染。
 */

import { ref, type Ref } from 'vue';

type ToastKind = 'success' | 'error' | 'warning';

export interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

const items = ref<ToastItem[]>([]);
let seq = 0;

const KIND_CLASS: Record<ToastKind, string> = {
  success: 'border-emerald-500/40 bg-emerald-600/15 text-emerald-200',
  error: 'border-red-500/40 bg-red-600/15 text-red-200',
  warning: 'border-amber-500/40 bg-amber-600/15 text-amber-200',
};

/** 渲染宿主用：拿到响应式 items 与样式映射 */
export function useToastItems(): { items: Ref<ToastItem[]>; KIND_CLASS: Record<ToastKind, string> } {
  return { items, KIND_CLASS };
}

function push(kind: ToastKind, message: string, duration: number) {
  const item: ToastItem = { id: ++seq, kind, message };
  items.value.push(item);
  if (duration > 0) {
    window.setTimeout(() => dismiss(item.id), duration);
  }
}

function dismiss(id: number) {
  const i = items.value.findIndex((t) => t.id === id);
  if (i >= 0) items.value.splice(i, 1);
}

export const toast = {
  success: (m: string, d = 3000) => push('success', m, d),
  error: (m: string, d = 4000) => push('error', m, d),
  warning: (m: string, d = 3000) => push('warning', m, d),
  dismiss,
};
