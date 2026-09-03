import { defineConfig, presetWind3, transformerDirectives, transformerVariantGroup } from 'unocss';

export default defineConfig({
  presets: [presetWind3()],
  transformers: [transformerDirectives(), transformerVariantGroup()],
  shortcuts: {
    // 常用布局工具类，统一风格
    'card': 'bg-slate-900 border border-slate-700/60 rounded-xl p-4 shadow-lg',
    'btn-base': 'inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-colors cursor-pointer select-none disabled:opacity-50 disabled:cursor-not-allowed',
    'btn-primary': 'btn-base bg-blue-600 hover:bg-blue-500 text-white',
    'btn-danger': 'btn-base bg-red-600 hover:bg-red-500 text-white',
    'btn-ghost': 'btn-base bg-slate-800 hover:bg-slate-700 text-slate-200',
    'input-base': 'w-full rounded-lg bg-slate-800 border border-slate-700 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-blue-500',
    'label-base': 'block text-sm font-medium text-slate-300 mb-1',
  },
  theme: {
    colors: {
      brand: {
        50: '#eff6ff',
        100: '#dbeafe',
        500: '#3b82f6',
        600: '#2563eb',
        700: '#1d4ed8',
      },
      surface: {
        0: '#0f172a',
        1: '#1e293b',
        2: '#334155',
      },
    },
  },
});
