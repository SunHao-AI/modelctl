/**
 * 兼容性专项：响应式布局断点 + 跨内核可交互性。
 *
 * 跨浏览器（chromium/firefox/webkit）与移动视口（Pixel 7 / iPhone 14）由
 * playwright.config.ts 的 projects 矩阵自动把本文件跑 5 遍——这里写的是
 * **与内核无关的布局契约**，同一断言在三种渲染引擎下必须同真。
 *
 * 断点契约（Layout.vue / Sidebar.vue）：侧栏 `hidden md:flex`，即视口 <768px
 * 折叠、≥768px 展开。这是本项目唯一的双栏/单栏切换点，也是移动端唯一会破的布局。
 */
import { expect, test } from '@playwright/test';

const ADMIN_KEY = 'e2e_admin_key_0123456789';

test.beforeEach(async ({ page }) => {
  await page.goto('/login');
  await page.locator('#api-key').fill(ADMIN_KEY);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
});

test('桌面视口：侧栏与主区域同屏（无横向溢出）', async ({ page, browserName }) => {
  test.skip(page.viewportSize()!.width < 768, '移动视口不适用桌面断言');
  // 侧栏可见（md:flex 生效）
  const sidebar = page.locator('nav, aside').first();
  await expect(sidebar).toBeVisible();
  // 页面级横向溢出是三内核排版差异最常见的破坏形态，统一断言无溢出
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow, `${browserName} 桌面视口不应有横向滚动`).toBeLessThanOrEqual(1);
});

test('移动视口：单栏布局无横向溢出，内容可滚动可达', async ({ page, browserName }) => {
  test.skip(page.viewportSize()!.width >= 768, '桌面视口不适用移动断言');
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow, `${browserName} 移动视口不应有横向滚动`).toBeLessThanOrEqual(1);
  // 标题在主区仍可见（折叠侧栏后内容不丢）
  await expect(page.locator('h1, h2').first()).toBeVisible();
});

test('跨内核基础可交互性：路由跳转与输入控件响应', async ({ page }) => {
  // 侧栏折叠时导航链接仍可经 DOM 定位（移动菜单展开逻辑不在此断言范围）
  const link = page.getByRole('link', { name: /体检|probe/i }).first();
  if (await link.isVisible().catch(() => false)) {
    await link.click();
    await expect(page).toHaveURL(/\/probe/, { timeout: 10_000 });
  } else {
    // 折叠视口下用直接导航验证同一路由在目标内核可达
    await page.goto('/probe');
    await expect(page).toHaveURL(/\/probe/, { timeout: 10_000 });
  }
});
