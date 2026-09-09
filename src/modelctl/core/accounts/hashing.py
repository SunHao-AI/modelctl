#!/usr/bin/env python3
# ===============================================================================
# @File   : src/modelctl/core/accounts/hashing.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/9 10:35
# @Desc   : bcrypt 密码 + API Key 生成的哈希与脱敏
# ===============================================================================

"""core/accounts/hashing.py — 密码 / API Key 的哈希与脱敏工具。

**依赖延迟导入**：`bcrypt` 只装到 gateway venv（`gateway/pyproject.toml`）。主包
（`pyproject.toml` [project.urls] 之外的其他用法）可能没有该 wheel；只要调用
`hash_password`/`verify_password` 才会 import `bcrypt`，其余函数（Key 相关）纯 stdlib。

## 关键约定

1. **API Key 前缀固定 `sk-mctl-`**，随机段用 `secrets.token_urlsafe(K)` 生成。
   长度约束：`token_urlsafe(24)` = 32 个 URL-safe 字符（24 字节熵），是"至少且
   不易碰撞"的下限；测试断言 `>=32` 防回归前把熵砍了。
2. **`hash_api_key` 用 `hashlib.sha256(utf-8)`**，确定性 64 hex。理由：
     - SHA-256 不可逆强度足够（Key 是高熵随机串，无字典内破解风险）；
     - DB 存 sha256 便于前端 `X-Api-Key` 运算后一次比对，无额外性能影响；
     - 相比 bcrypt：Key 是机器发不出、无字典意义，bcrypt 的高成本对其无意义，
       反而拖累每次鉴权高频调用。
3. **`key_prefix(key)`**：UI 展示位。策略对齐 `cluster/store.mask_tail` 的脱敏
   口径——但调参：末 4 位 + 中间 `***`。短 key（末段长度 ≤ 4）整段 `***` 防
   "末 4 位 = 全长"泄漏。**去除 `sk-mctl-` 头**：UI 已给出 `sk-mctl-` 前缀
   段，去重后仅面貌随机段。
"""

from __future__ import annotations

import hashlib
import secrets

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_API_KEY_PREFIX = "sk-mctl-"
# `secrets.token_urlsafe(n)` 生成 ceil(8n/3) 字符。n=24 → 32 字符。
_API_KEY_ENTROPY_BYTES = 24
# 脱敏保留末 4 位
_MASK_ENCRYPTED_TAIL = 4
_MASK_ENCRYPTED_BODY = "***"


# ---------------------------------------------------------------------------
# 密码（bcrypt）
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    """给定明文密码返回 bcrypt 产物（长度 60，`$2b$`/`$2y$`/`$2a$` 采样盐）。

    抛异常条件：`plain` 必须非空 str，非 str 抛 `TypeError`（避免 C 层
    `memoryview` 报错透到业务层）。
    """
    if not isinstance(plain, str) or not plain:
        raise ValueError("plain must be a non-empty string")
    # 延迟导入：主包未装 bcrypt 时至少让其它函数可用
    import bcrypt

    # bcrypt 输入最长 72 字节；超出部分会被静默截断（密码学意义上安全，
    # 语义上误导）—— 明文化截断在后端总是密码策略层由前端负责
    raw = plain.encode("utf-8", errors="ignore")[:72]
    hashed = bcrypt.hashpw(raw, bcrypt.gensalt(rounds=12))
    return hashed.decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文与既定 bcrypt 哈希。错密码/空值/非 str 均安全返回 False。"""
    if not isinstance(plain, str) or not isinstance(hashed, str):
        return False
    if not plain or not hashed:
        return False
    import bcrypt

    raw = plain.encode("utf-8", errors="ignore")[:72]
    try:
        return bcrypt.checkpw(raw, hashed.encode("ascii"))
    except (ValueError, TypeError):
        # 非 bcrypt 串 / 编码异常 → 不匹配
        return False


# ---------------------------------------------------------------------------
# API Key
# ---------------------------------------------------------------------------

def generate_api_key() -> str:
    """生成一把新 API Key：`sk-mctl-` + `secrets.token_urlsafe(24)` = 32 字符段。

    熵来源：`secrets`（OS CSPRNG，`/dev/urandom` 或 `CryptGenRandom`），
    与 `random`（Mersenne Twister）位级区分。
    """
    return f"{_API_KEY_PREFIX}{secrets.token_urlsafe(_API_KEY_ENTROPY_BYTES)}"


def hash_api_key(key: str) -> str:
    """API Key 的 sha256 十六进制摘要（用于 DB 存储与鉴权比对）。

    确定性 + 无法逆推（在 Key 熵保证下）。空串有确定 hash，但业务上
    不应将空 key 落库（`store.create_key` 别如此调用）。
    """
    if key is None:
        raise ValueError("key must be a str")
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


def key_prefix(key: str | None) -> str:
    """脱敏展示串：`sk-mctl-***xxxx`（末 4 位）；短 key 或无 key → `***`。

    规则（对齐 `cluster/store.mask_tail` 语义，仅参数调优）：
    - 去除 `sk-mctl-` 前缀后剩余段 ≤ 末段长度 → 整段 `***`。防 `sk-mctl-ab`
      的 "末 4 位 = 全长"泄漏。
    - 正常：`sk-mctl-` + `***` + 末 4 位。
    - None / 空 / 非 str → `***`。
    """
    if not isinstance(key, str) or not key:
        return _MASK_ENCRYPTED_BODY
    body = key.removeprefix(_API_KEY_PREFIX)
    if len(body) <= _MASK_ENCRYPTED_TAIL:
        return _MASK_ENCRYPTED_BODY
    return f"{_API_KEY_PREFIX}{_MASK_ENCRYPTED_BODY}{body[-_MASK_ENCRYPTED_TAIL:]}"
