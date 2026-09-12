import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright E2E + 跨浏览器兼容性配置。
 *
 * 被测对象是**生产形态**：`python -m uvicorn ... create_app(admin=True)` 同源提供
 * 管理 API + SPA 静态产物（webui/server.mount_static），而不是 vite dev server——
 * E2E 必须验证「history 深链刷新回 index.html」「401 统一形状」这类只有生产
 * 拓扑才存在的行为。
 *
 * webServer 启动后端（仓库根 venv，需 `uv sync --extra dev --extra test`），
 * `reuseExistingServer` 让本地已起着的 webui 直接复用；CI 冷启动。
 * 前端产物需先 `npm run build`（CI 的 e2e job 保证顺序）。
 *
 * 兼容性矩阵（桌面三引擎 + 移动视口）：
 *   - chromium / firefox / webkit 覆盖三大渲染内核（= 跨浏览器）
 *   - Pixel 7 / iPhone 14 视口 + 触摸模拟覆盖移动端布局（侧栏折叠断点）
 * OS 维度由 CI matrix 承担（ubuntu + windows 双 runner，见 .github/workflows/ci.yml）。
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  // CI 上禁止 test.only 漏网；本地不限
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never', outputFolder: '../build/playwright-report' }]] : [['list']],
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  webServer: {
    // 与生产启动口径一致：`modelctl webui` 即此入口（webui 端口 4173、管理面开启）。
    // load_env 是 setdefault 语义 → 这里显式注入的 env 恒优先于开发者本地 .env。
    command: 'uv run --extra dev --extra test python -m modelctl.core.webui.server',
    cwd: '..',
    // 就绪探针用 /admin/api/health（免鉴权）；根路径 `/` 是 SPA 兜底 HTML，
    // 只能证明 dist 已挂载，不能证明管理面就绪，故不用它。
    url: 'http://127.0.0.1:4173/admin/api/health',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      API_KEY: 'e2e_admin_key_0123456789',
      GATEWAY_CLIENT_API_KEY: 'e2e_data_key_0123456789',
      ACCOUNTS_JWT_SECRET: 'e2e_jwt_secret_0123456789',
      WEBUI_PORT: '4173',
      // 钉死 solo：开发者 .env 若配了 worker/both，不能让 E2E 进程拉起集群后台线程
      CLUSTER_ROLE: 'solo',
    },
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
    { name: 'mobile-chrome', use: { ...devices['Pixel 7'] } },
    { name: 'mobile-safari', use: { ...devices['iPhone 14'] } },
  ],
});
