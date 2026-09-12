import { defineConfig, loadEnv } from 'vite';
import vue from '@vitejs/plugin-vue';
import UnoCSS from 'unocss/vite';
import { fileURLToPath, URL } from 'node:url';

/** dev 期后端（modelctl webui）端口：与后端共用仓库根 .env 的 WEBUI_PORT，避免两处真值。 */
const BACKEND_DEFAULT_PORT = 4173;

export default defineConfig(({ mode }) => {
  // 读仓库根（web/ 的上一级）的 .env，只取 WEBUI_ 前缀——不注入前端代码，仅供配置使用
  const env = loadEnv(mode, fileURLToPath(new URL('..', import.meta.url)), 'WEBUI_');
  const backendPort = env.WEBUI_PORT || String(BACKEND_DEFAULT_PORT);

  return {
    plugins: [
      vue(),
      UnoCSS(),
    ],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      port: 5173,
      host: '0.0.0.0',
      proxy: {
        '/admin/api': {
          target: `http://127.0.0.1:${backendPort}`,
          changeOrigin: true,
        },
        // 账号自助面板（JWT，与管理面 API_KEY 双信任域）走独立 baseURL '/api/account'
        '/api/account': {
          target: `http://127.0.0.1:${backendPort}`,
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: '../dist',
      emptyOutDir: true,
    },
    // vitest 配置（`npm test` → vitest run）：单元测试跑在 jsdom（localStorage /
    // EventSource 等浏览器 API 可用），只收 src/**\/*.test.ts，不碰 e2e/（Playwright 独立跑）。
    test: {
      environment: 'jsdom',
      include: ['src/**/*.test.ts'],
      clearMocks: true,
      // 并行 worker 冷启动时，路由守卫测试的懒加载视图（dynamic import → esbuild
      // 现场 transform）会顶穿默认 5s 超时；守卫断言本身 <100ms，放宽到 30s 只兜冷启动。
      testTimeout: 30_000,
      coverage: {
        provider: 'v8',
        include: ['src/**/*.{ts,vue}'],
        exclude: ['src/**/*.test.ts', 'src/env.d.ts', 'src/main.ts'],
        reporter: ['text', 'html'],
        reportsDirectory: '../build/coverage-web',
      },
    },
  };
});
