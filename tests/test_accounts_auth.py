#!/usr/bin/env python3
# ===============================================================================
# @File   : tests/test_accounts_auth.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/9 11:20
# @Desc   : accounts/auth.py 双通道凭据解析与身份组装
# ===============================================================================

"""TDD 契约（对应 Task 5 计划）：

- `extract_credential(request) -> str`：
  - `Authorization: Bearer <key>` 优先（与 gateway `verify_client` 一致）。
  - 回退到 `x-api-key`（Anthropic Claude SDK 只带这个头）。
  - 均零值 → `""`。
  - 值两端 strip；`Bearer  abc ` → `"abc"`。
  - Bearer 大小写不敏感（`bEARer` 或 `BEARer`）。
- `@dataclass AccountIdentity`：
  - 字段含 `user_id`, `key_id`, `key_prefix`, `username`, `display_name`, `is_admin`, `policy`
  - `policy` 是 `limits.UserPolicy`（8 字段填充完毕）
- `resolve_account(store, credential, *, now) -> AccountIdentity | None`：
  - 空串 / 空白 → `None`（不查库）。
  - `hash_api_key(credential)` 未命中 → `None`（未签发 Key）。
  - Key 状态非 `active` → `None`。
  - Key `expires_at` 过期 → `None`。
  - 用户 `status` 非 `active`（`disabled`/`locked`）→ `None`。
  - 有效 Key + 有效用户 → 返回 `AccountIdentity`，`policy` 字段来源于 users 行。
  - 命中时 **`store.touch_key_last_used(key_id, now=...)`** 恰好调用一次（记录 Key
    最近命中时刻；不阻塞主链路，但 hawk 时间必须注入）。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import pytest


@dataclass
class _FakeRequest:
    """极简单的 duck-typed Request：只暴露 `headers` dict。"""

    headers: dict


@pytest.fixture
def store(tmp_path) -> "object":
    from modelctl.core.accounts.store import AccountsStore
    s = AccountsStore(tmp_path / "accounts_auth.db")
    s.init_db()
    yield s
    s.close()


def _mk_user_with_key(store, *, username: str = "alice",
                      user_status: str = "active",
                      key_status: str = "active",
                      expires_at: float | None = None,
                      policy_limits: dict | None = None,
                      now: float = 1000.0):
    pw = "bcrypt$fake"
    limits = policy_limits or {}
    uid = store.create_user(username=username, password_hash=pw, now=now,
                            **limits)
    if user_status != "active":
        store.set_user_status(uid, user_status, now=now)
    kh = "hash-of-" + username
    prefix = "sk-mctl-***abcd"
    kid = store.create_key(user_id=uid, key_hash=kh, key_prefix=prefix,
                           name="test", expires_at=expires_at, now=now)
    if key_status != "active":
        store.set_key_status(kid, key_status)
    return uid, kid


# ---------------------------------------------------------------------------
# extract_credential
# ---------------------------------------------------------------------------

def test_extract_credential_bearer_first() -> None:
    from modelctl.core.accounts.auth import extract_credential

    r = _FakeRequest({"authorization": "Bearer  abc-123  ",
                      "x-api-key": "other-key"})
    assert extract_credential(r) == "abc-123"


def test_extract_credential_x_api_key_fallback() -> None:
    from modelctl.core.accounts.auth import extract_credential

    r = _FakeRequest({"x-api-key": "ck-456"})
    assert extract_credential(r) == "ck-456"


def test_extract_credential_bearer_case_insensitive() -> None:
    from modelctl.core.accounts.auth import extract_credential

    assert extract_credential(_FakeRequest({"authorization": "bEARer x"})) == "x"
    assert extract_credential(_FakeRequest({"authorization": "BEARer y"})) == "y"
    assert extract_credential(_FakeRequest({"authorization": "bearer z"})) == "z"


def test_extract_credential_empty_returns_empty_string() -> None:
    from modelctl.core.accounts.auth import extract_credential

    assert extract_credential(_FakeRequest({})) == ""
    assert extract_credential(_FakeRequest({"authorization": "Bearer"})) == ""  # 无 token
    assert extract_credential(_FakeRequest({"authorization": "Bearer   "})) == ""


def test_extract_credential_strips_trailing_space_in_raw() -> None:
    from modelctl.core.accounts.auth import extract_credential

    assert extract_credential(_FakeRequest({"x-api-key": "  keyA  "})) == "keyA"


# ---------------------------------------------------------------------------
# resolve_account
# ---------------------------------------------------------------------------

def test_resolve_account_empty_credential_returns_none(store) -> None:
    from modelctl.core.accounts.auth import resolve_account

    assert resolve_account(store, "", now=1000.0) is None
    assert resolve_account(store, "   ", now=1000.0) is None


def test_resolve_account_unknown_key_returns_none(store) -> None:
    from modelctl.core.accounts.auth import resolve_account

    _mk_user_with_key(store, username="u1")
    # credential 打了 hash 后再查库，陌生 credential → 陌生 hash → miss
    assert resolve_account(store, "sk-mctl-nonexistent", now=1000.0) is None


def test_resolve_account_valid_key_returns_identity(store) -> None:
    from modelctl.core.accounts import limits
    from modelctl.core.accounts.auth import resolve_account

    uid, kid = _mk_user_with_key(
        store, username="alice",
        policy_limits={"concurrency_limit": 3, "rpm_limit": 60,
                       "tpm_limit": 1000, "token_budget": 50000,
                       "budget_period": "day", "retention_days": 7},
        now=1000.0,
    )
    # 手工组装：需要 credential key 的 hash == store 里密钥的 hash
    # 简化：直接调内部 hash_api_key，通过 store key hash 反推 credential —— 不可行
    # 改用：从 create_key 内部拿到 hash，然后造 credential 使 sha256(key_hash)==hash？
    # sha256 单射不可逆。改为：auth.resolve_account 内部实际流程是
    #   cred → hash_api_key(cred) → store.get_key_by_hash
    # 测试不必强求明文 credential，只要 hash(external_input) == store 内即可
    # 但我们不知道 store 用的 hash_input 是什么（test 中 _mk_user_with_key 内
    # 我把 key_hash 假定为 "hash-of-alice"，不是有意义的 sha256）。
    # 解法：让 resolve_account 走相同 hash_api_key 路径，所以测试需要造
    # 一个 credential 而 hash_api_key(credential) .equals. store 里 key_hash。
    # 用 store.create_key 时报 hash 是 **sha256(credential)，所以测试：
    #   credential = 固定字符串
    #   key_hash = hashing.hash_api_key(credential)
    #   store.create_key(key_hash=key_hash)
    # 但 _mk_user_with_key 里我写的 key_hash = "hash-of-alice"（无 sha256）
    # → 因此 helper 得接受 key_hash 参数；更新本测试：
    from modelctl.core.accounts.hashing import hash_api_key
    from modelctl.core.accounts.store import AccountsStore  # just type

    import time
    real_now = 1000.0
    already_uid = uid
    # 再建一个 Key 用有效 hash
    cred = "sk-mctl-real-credential"
    from modelctl.core.accounts import hashing as h
    kh = h.hash_api_key(cred)
    kid2 = store.create_key(user_id=already_uid, key_hash=kh,
                            key_prefix="sk-mctl-***xxxx", name="real",
                            now=real_now)
    identity = resolve_account(store, cred, now=real_now + 1)
    assert identity is not None
    assert identity.user_id == uid
    assert identity.key_id == kid2
    assert identity.username == "alice"
    assert isinstance(identity.policy, limits.UserPolicy)
    assert identity.policy.concurrency_limit == 3
    assert identity.policy.rpm_limit == 60
    assert identity.policy.tpm_limit == 1000
    assert identity.policy.token_budget == 50000
    # is_admin 默认 False
    assert identity.is_admin is False


def test_resolve_account_disabled_key_returns_none(store) -> None:
    from modelctl.core.accounts import hashing as h
    from modelctl.core.accounts.auth import resolve_account

    # 新建账号，用真实 hashing 签一个 Key，然后处置为 revoked；
    # resolve 时 hash 命中 Key 但 status != active → None
    uid = store.create_user(username="bob", password_hash="bcrypt$fake", now=1000.0)
    cred = "sk-mctl-bob-cred"
    kh = h.hash_api_key(cred)
    kid = store.create_key(user_id=uid, key_hash=kh, key_prefix="sk-mctl-***bbbb",
                           name="b-key", now=1000.0)
    assert store.set_key_status(kid, "revoked")
    assert resolve_account(store, cred, now=1200.0) is None


def test_resolve_account_expired_key_returns_none(store) -> None:
    from modelctl.core.accounts import hashing as h
    from modelctl.core.accounts.auth import resolve_account

    # 建 Key（expires_at=999.0 已过期）
    uid, _ = _mk_user_with_key(store, username="carol",
                               expires_at=999.0, now=1000.0)
    cred = "sk-mctl-carol-cred"
    kh = h.hash_api_key(cred)
    store.create_key(user_id=uid, key_hash=kh, key_prefix="sk-mctl-***cccc",
                     name="expired", expires_at=999.0, now=1000.0)
    # 已到 expires_at=999，now=1001 判定为过期
    assert resolve_account(store, cred, now=1001.0) is None
    # 未到 expires_at 时正常
    resolved_before = resolve_account(store, cred, now=998.0)
    assert resolved_before is not None


def test_resolve_account_disabled_user_returns_none(store) -> None:
    from modelctl.core.accounts import hashing as h
    from modelctl.core.accounts.auth import resolve_account

    uid, _ = _mk_user_with_key(store, username="dora",
                               user_status="disabled", now=1000.0)
    cred = "sk-mctl-dora-cred"
    kh = h.hash_api_key(cred)
    store.create_key(user_id=uid, key_hash=kh, key_prefix="sk-mctl-***dddd",
                     name="d-key", now=1000.0)
    assert resolve_account(store, cred, now=1000.0) is None


def test_resolve_account_valid_key_touches_last_used(store) -> None:
    from modelctl.core.accounts import hashing as h
    from modelctl.core.accounts.auth import resolve_account

    uid, kid = _mk_user_with_key(store, username="eve", now=1000.0)
    cred = "sk-mctl-eve-cred"
    kh = h.hash_api_key(cred)
    store.create_key(user_id=uid, key_hash=kh, key_prefix="sk-mctl-***eeee",
                     name="e-key", now=1000.0)
    resolved = resolve_account(store, cred, now=1234.0)
    assert resolved is not None
    k = store.get_key_by_id(resolved.key_id)
    # 从有效身份解析过程中 touch 了这个 Key
    assert k["last_used_at"] == 1234.0
