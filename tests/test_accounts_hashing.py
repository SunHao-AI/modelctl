#!/usr/bin/env python3
# ===============================================================================
# @File   : tests/test_accounts_hashing.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/9 10:30
# @Desc   : accounts/hashing.py — bcrypt 密码与 API Key 生成/脱敏
# ===============================================================================

"""TDD 契约（对应 Task 3 计划）：

- `hash_password` / `verify_password`：bcrypt 往返；错密返回 False；哈希串不等于明文。
- `generate_api_key`：前缀 `sk-mctl-`，字符集全 ASCII 可打印，长度稳定（≥ 32 随机段），
  高熵下连续调用不重复（跑 100 次去重）。
- `hash_api_key`：sha256 十六进制，确定性与长度 64。
- `key_prefix`：`sk-mctl-` 前段保留 + 中段 `***` + 末 4 位；短 key（≤ 末段长度）
  一律脱敏为 `***`（避免末 4 位 = 全长泄漏）。
"""

from __future__ import annotations

import importlib.util
import re

import pytest

# bcrypt 只装在 gateway venv；主 venv 中缺失时用 skip 保护付费函数测试。
# 这是**依赖布局决策**（见 gateway/pyproject.toml 与 CLAUDE.md "依赖管理"
# 规则），不是可选项——主包 import 不应因缺 bcrypt 而崩。
_need_bcrypt = importlib.util.find_spec("bcrypt") is None
skip_bcrypt = pytest.mark.skipif(_need_bcrypt, reason="bcrypt 依赖在 gateway venv 内，主 venv 跳过")


# ---------------------------------------------------------------------------
# hash_password / verify_password
# ---------------------------------------------------------------------------

@skip_bcrypt
def test_hash_password_returns_bcrypt_like_and_verifies() -> None:
    from modelctl.core.accounts.hashing import hash_password, verify_password

    plain = "S3cr3t!密码"
    hashed = hash_password(plain)
    # bcrypt 产物必然长 60 字符，且非明文
    assert isinstance(hashed, str)
    assert hashed != plain
    assert len(hashed) == 60

    assert verify_password(plain, hashed) is True


@skip_bcrypt
def test_verify_password_wrong_password_is_false() -> None:
    from modelctl.core.accounts.hashing import hash_password, verify_password

    hashed = hash_password("right-password")
    assert verify_password("wrong-password", hashed) is False


@skip_bcrypt
def test_verify_password_empty_plain_or_hashed_is_false() -> None:
    from modelctl.core.accounts.hashing import verify_password

    # 空密码或非 bcrypt 串必须安全返回 False 而非抛异常
    assert verify_password("", "$2b$12$xxxxxxxxxxxxxxxxxxx") is False
    assert verify_password("abc", "not-a-bcrypt-hash") is False
    assert verify_password("abc", "") is False


@skip_bcrypt
def test_hash_password_has_sufficient_salt() -> None:
    """同一明文两次哈希应产生不同盐值，防止彩虹表比对。"""
    from modelctl.core.accounts.hashing import hash_password

    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    # bcrypt 前缀 `$2b$`/`$2y$`/`$2a$` + 成本 4 位（BCPBKDF 允许范围）
    assert re.match(r"^\$2[aby]\$\d{2}\$", a)


# ---------------------------------------------------------------------------
# hash_api_key / key_prefix
# ---------------------------------------------------------------------------

def test_hash_api_key_is_deterministic_sha256() -> None:
    from modelctl.core.accounts.hashing import hash_api_key

    k = "sk-mctl-0123456789abcdef"
    h = hash_api_key(k)
    assert isinstance(h, str)
    assert len(h) == 64
    # sha256 hex 且确定
    assert re.fullmatch(r"[0-9a-f]{64}", h)
    assert hash_api_key(k) == h
    # 不同 key 不同 hash（生日碰撞理论概率 ~2^-128）
    assert hash_api_key("sk-mctl-9999999999999999") != h


def test_key_prefix_masks_middle_and_keeps_tail() -> None:
    from modelctl.core.accounts.hashing import key_prefix

    k = "sk-mctl-abcdef0123456789XYZW"
    p = key_prefix(k)
    # 结构: `sk-mctl-` 头 + `***` 脱敏段 + 末 4 位（"WVXY"/"XYZW" 等由 key 决定）
    assert p.startswith("sk-mctl-")
    assert p.endswith("XYZW")
    assert "***" in p
    # 完整明文（去掉 prefix 后）不出现在脱敏串里
    random_part = k.removeprefix("sk-mctl-")
    assert random_part not in p


def test_key_prefix_short_key_returns_mask_only() -> None:
    """短 key（去掉前缀后 ≤ 末段长度）完全脱敏成 `***`，避免末 4 = 全文。"""
    from modelctl.core.accounts.hashing import key_prefix

    assert key_prefix("sk-mctl-ab") == "***"
    assert key_prefix("sk-mctl-abc") == "***"
    assert key_prefix("sk-mctl-abcd") == "***"
    # 无 `sk-mctl-` 前缀的 key（防越权仅信播种记录）
    assert key_prefix("") == "***"
    assert key_prefix(None) == "***"  # type: ignore[arg-type]


def test_key_prefix_standard_key_shape() -> None:
    """对常规 `generate_api_key` 产物，前缀形态固定：`sk-mctl-***<4>`。"""
    from modelctl.core.accounts.hashing import generate_api_key, key_prefix

    k = generate_api_key()
    p = key_prefix(k)
    # 末段必须是 4 个字符
    assert p.endswith(k[-4:])
    # 去掉前缀后长度等于 `***` (3) + 4=7
    assert len(p.removeprefix("sk-mctl-")) == 7


# ---------------------------------------------------------------------------
# generate_api_key
# ---------------------------------------------------------------------------

_API_KEY_RE = re.compile(r"^sk-mctl-[A-Za-z0-9_-]+$")


def test_generate_api_key_shape_and_prefix() -> None:
    from modelctl.core.accounts.hashing import generate_api_key

    k = generate_api_key()
    assert isinstance(k, str)
    assert k.startswith("sk-mctl-")
    assert _API_KEY_RE.match(k)
    # 随机段长度可预测：至少 32 字符（24 字节 token_urlsafe 高熵下限）
    random_part = k.removeprefix("sk-mctl-")
    assert len(random_part) >= 32


def test_generate_api_key_unique_in_100_calls() -> None:
    """100 次生成的 key 完全无碰撞（24 字节熵空间留给读者）。"""
    from modelctl.core.accounts.hashing import generate_api_key

    keys = {generate_api_key() for _ in range(100)}
    assert len(keys) == 100


def test_hash_password_available_at_import_stub() -> None:
    """供 gate：bcrypt 未装的运行环境主包 import 也不应崩（延迟导入）。"""
    # 不强制模拟缺失依赖，仅断言入口函数存在且可 import 模块本身
    import modelctl.core.accounts.hashing as h

    assert callable(h.hash_password)
    assert callable(h.verify_password)
    assert callable(h.generate_api_key)
    assert callable(h.hash_api_key)
    assert callable(h.key_prefix)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
