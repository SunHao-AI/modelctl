#!/usr/bin/env python3
# ===============================================================================
# @File   : src/modelctl/core/accounts/__init__.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/8 16:25
# @Desc   : 多账号与 API Key 体系包入口
# ===============================================================================

"""core/accounts — 多账号 / 多 API Key 体系（设计见
`docs/superpowers/specs/2026-09-08-accounts-api-keys-design.md`）。

模块划分：

- `store.py`      SQLite DAO（五表：users / api_keys / usage_records / sessions / messages）
- `hashing.py`    bcrypt 密码哈希 + `sk-mctl-` Key 生成/摘要/脱敏
- `limits.py`     `LimitGuard`：并发 / RPM / TPM / 预算四道准入检查
- `auth.py`       数据面凭据提取与 Key → 账号身份解析
- `accountant.py` 请求结束后的异步记账队列

**包级 import 必须零第三方依赖**：`bcrypt` / `PyJWT` 只装在 gateway 虚拟环境，
而本包的 `accounts_enabled()` / `accounts_db_path()` 会被主包 CLI 在无这些依赖的
进程里调用，所以重依赖一律延迟到子模块内部导入，不得出现在本文件。
"""

from __future__ import annotations

import os

from modelctl.core.paths import accounts_db_path


def accounts_enabled() -> bool:
    """账号体系总开关（`ACCOUNTS_ENABLED`）。

    默认 **False**：关闭时网关走现有 `GATEWAY_CLIENT_API_KEY` 单钥 fail-closed，
    行为与升级前完全一致（零回归）。非法值同样回退 False——开关类配置"看不清就当关"
    比"就当开"安全（后者会让无账号库的部署直接拒绝全部请求）。

    布尔口径复用 `stats._parse_env_bool`（与 USAGE_* 开关一致），函数内导入以免
    包级拉起 stats 模块。
    """
    from modelctl.core.stats import _parse_env_bool

    return _parse_env_bool(os.environ.get("ACCOUNTS_ENABLED"), default=False)


__all__ = ["accounts_db_path", "accounts_enabled"]
