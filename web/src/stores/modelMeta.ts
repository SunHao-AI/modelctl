import { ref, computed } from 'vue';
import { defineStore } from 'pinia';

/**
 * 模型元数据 store（示例）：保存模型 ViewModels（用于列表展示 / 详情读取）
 * 后续可按需扩展字段
 */
export const useModelMetaStore = defineStore('modelMeta', () => {
  const viewModels = ref<Record<string, unknown>>({});
  const loading = ref(false);

  const viewModelsReady = computed(() => Object.keys(viewModels.value).length > 0);

  function setViewModels(m: Record<string, unknown>) {
    viewModels.value = m;
  }

  function appendViewModel(name: string, meta: unknown) {
    viewModels.value = { ...viewModels.value, [name]: meta };
  }

  function clear() {
    viewModels.value = {};
  }

  function setLoading(v: boolean) {
    loading.value = v;
  }

  return {
    viewModels,
    loading,
    viewModelsReady,
    setViewModels,
    appendViewModel,
    clear,
    setLoading,
  };
});
