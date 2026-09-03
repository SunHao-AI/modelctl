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
      },
    },
    build: {
      outDir: '../dist',
      emptyOutDir: true,
    },
  };
});
