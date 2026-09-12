/**
 * E2E 冒烟 + 鉴权主链路（生产拓扑：uvicorn 同源 API + SPA）。
 *
 * 与单测的分工：router.test.ts 用 jsdom 验证守卫「决策逻辑」，本文件验证
 * 真实浏览器里「后端 401 → 前端行为」的闭环——包括后端响应形状（detail.code）
 * 与前端解析是否对齐这类跨端契约，只有起真后端才测得到。
 *
 * 后端由 playwright.config.ts 的 webServer 拉起，密钥固定：
 *   API_KEY = e2e_admin_key_0123456789
 */
import { expect, test } from '@playwright/test';

const ADMIN_KEY = 'e2e_admin_key_0123456789';

test('登录页渲染：标题、API Key 输入框、登录按钮齐全', async ({ page }) => {
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: 'modelctl' })).toBeVisible();
  await expect(page.locator('#api-key')).toBeVisible();
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeVisible();
});

test('空 Key 提交前端拦截：不发请求，提示必填', async ({ page }) => {
  await page.goto('/login');
  // 空串时按钮 disabled（:disabled="submitting || !apiKey.trim()"）
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeDisabled();
});

test('错误 Key 登录：后端 401，错误文案回显且不出登录页', async ({ page }) => {
  await page.goto('/login');
  await page.locator('#api-key').fill('wrong-key-000000');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  // LoginView 把后端 detail.message（或兜底文案）渲染在输入框下方
  await expect(page.locator('p.text-red-400')).toBeVisible({ timeout: 10_000 });
  expect(page.url()).toContain('/login');
  // 登录失败绝不写 token
  const token = await page.evaluate(() => localStorage.getItem('modelctl_token'));
  expect(token).toBeFalsy();
});

test('正确 Key 登录：进入仪表板且 token 持久化（刷新保持登录）', async ({ page }) => {
  await page.goto('/login');
  await page.locator('#api-key').fill(ADMIN_KEY);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard/, { timeout: 10_000 });
  const token = await page.evaluate(() => localStorage.getItem('modelctl_token'));
  expect(token).toBe(ADMIN_KEY);

  // 刷新（SPA 深链由后端 SPA fallback 回 index.html）后仍停在仪表板
  await page.reload();
  await expect(page).toHaveURL(/\/dashboard/);
});

test('未登录深链 /models 被弹回 /login 且 redirect 带回原路径', async ({ page }) => {
  await page.goto('/models');
  await expect(page).toHaveURL(/\/login/);
  // vue-router 的 query 序列化不编码 '/'，直接解析 query 而非匹配编码串
  const url = new URL(page.url());
  expect(url.searchParams.get('redirect')).toBe('/models');
});

test('SPA history 深链直接访问返回 HTML 而非 API 404（生产挂载契约）', async ({ request }) => {
  // 前端路由路径：须回 index.html（HTML），不是 API 的 404 JSON
  const spa = await request.get('/models/foo/bar');
  expect(spa.status()).toBe(200);
  expect(spa.headers()['content-type']).toContain('text/html');

  // API 前缀：未命中路由必须回 404 JSON，绝不能回 SPA 页面
  const api = await request.get('/admin/api/no/such/endpoint');
  expect(api.status()).toBe(404);
  expect(api.headers()['content-type']).toContain('json');

  const data = await request.get('/v1/no/such/endpoint');
  expect(data.status()).toBe(404);
  expect(data.headers()['content-type']).toContain('json');
});

test('管理 API fail-closed：无凭据 401 且响应形状为 code=auth', async ({ request }) => {
  const res = await request.get('/admin/api/probe');
  expect(res.status()).toBe(401);
  const body = await res.json();
  expect(String(JSON.stringify(body))).toContain('auth');
});

test('已登录页签标题按路由 meta 更新（afterEach 契约）', async ({ page }) => {
  await page.goto('/login');
  await page.locator('#api-key').fill(ADMIN_KEY);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard/, { timeout: 10_000 });
  await expect(page).toHaveTitle(/仪表板/);
});
