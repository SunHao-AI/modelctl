/**
 * jsdom 不实现 window.matchMedia（实测 typeof === 'undefined'）。
 * usePreferredDark / @vueuse 依赖它，缺失时会静默退化成「系统恒浅色」，
 * 让主题测试假绿。这里补一个可被测试驱动的极简实现。
 */
type MqListener = (e: { matches: boolean }) => void;

const listeners = new Set<MqListener>();
let systemDark = false;

function install() {
  // setupFiles 对所有测试文件生效，包括 `@vitest-environment node` 的
  //（如 tokens*.test.ts），那里没有 window——只在有 DOM 的环境装桩。
  if (typeof window === 'undefined') return;
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      media: query,
      get matches() {
        return systemDark;
      },
      onchange: null,
      addEventListener: (_: string, cb: MqListener) => listeners.add(cb),
      removeEventListener: (_: string, cb: MqListener) => listeners.delete(cb),
      addListener: (cb: MqListener) => listeners.add(cb),
      removeListener: (cb: MqListener) => listeners.delete(cb),
      dispatchEvent: () => true,
    }),
  });
}

install();

/** 测试专用：切换"系统偏好"并派发 change，驱动 usePreferredDark 重算 */
(globalThis as unknown as { __setSystemDark: (v: boolean) => void }).__setSystemDark = (v: boolean) => {
  systemDark = v;
  for (const cb of listeners) cb({ matches: v });
};