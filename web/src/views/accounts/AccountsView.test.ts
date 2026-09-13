/**
 * AccountsView 单元测试（QA 修复批次 A）：
 *  - QA-A-15：后端 503 accounts_disabled 时，红条与黄条互斥（只留引导块），
 *    「新建账号」按钮必须禁用。
 *  - QA-B-14：正文不再渲染重复页题「账号管理」。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

const listAccounts = vi.fn();

vi.mock('@/api/accounts', () => ({
  listAccounts: () => listAccounts(),
  createAccount: vi.fn(),
  updateAccount: vi.fn(),
  deleteAccount: vi.fn(),
  listAccountKeys: vi.fn(),
  createAccountKey: vi.fn(),
  updateAccountKey: vi.fn(),
  deleteAccountKey: vi.fn(),
  getAccountUsage: vi.fn(),
}));

import AccountsView from './AccountsView.vue';

const disabled503 = Object.assign(new Error('Request failed with status code 503'), {
  response: {
    status: 503,
    data: { detail: { code: 'accounts_disabled', message: '账号体系未启用（.env 设 ACCOUNTS_ENABLED=true）' } },
  },
});

beforeEach(() => {
  listAccounts.mockReset();
});

describe('QA-A-15 accounts_disabled 引导态', () => {
  it('红条与黄条互斥：只显示黄色引导块；「新建账号」禁用；正文无重复页题', async () => {
    listAccounts.mockRejectedValue(disabled503);
    const wrapper = mount(AccountsView);
    await flushPromises();

    const html = wrapper.html();
    // 黄色引导块在场
    expect(html).toContain('bg-warn-bg');
    expect(wrapper.text()).toContain('如需启用，请在后端 .env 中设');
    // 红条不在场（errMsg 被 !isAccountsDisabled 互斥掉）
    expect(html).not.toContain('bg-danger-bg px-3 py-2 text-sm text-danger');

    const create = wrapper.findAll('button').find((b) => b.text().includes('新建账号'))!;
    expect(create.attributes('disabled')).toBeDefined();

    // B-14：正文不渲染 h2「账号管理」
    expect(wrapper.find('h2').exists()).toBe(false);
  });

  it('正常态：账号列表渲染，「新建账号」可用', async () => {
    listAccounts.mockResolvedValue([]);
    const wrapper = mount(AccountsView);
    await flushPromises();
    expect(wrapper.text()).toContain('暂无账号');
    const create = wrapper.findAll('button').find((b) => b.text().includes('新建账号'))!;
    expect(create.attributes('disabled')).toBeUndefined();
  });
});
