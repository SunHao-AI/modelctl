#!/usr/bin/env python3
# ===============================================================================
# @File   : src/modelctl/core/accounts/auth.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/9 11:40
# @Desc   : 数据面凭据提取与 Key → 账号身份解析
# ===============================================================================

"""core/accounts/auth.py — 网关数据面每一请求调用一次：credential → 身份快照。

只有两个对外符号：

- `extract_credential(request)`：从 HTTP 头里取出**明文 credential**（不查库、
  不做任何校验），与 `gateway.verify_client` 的双通道嗅探完全一致：
  `Authorization: Bearer <key>` 优先，回退到 `x-api-key`（Anthropic Claude
  SDK 只带这个头）。返回 `""` 表示客户端没发凭证。
- `resolve_account(store, credential, *, now)`：把明文 credential 走
  `hash_api_key` 摘要 → 命中 `api_keys` → join `users` → 组装
  `AccountIdentity`（含 `limits.UserPolicy`）。任何一步 miss / 状态非法 /
  过期都静默返回 `None`——不把业务细节泄漏到异常，网关上层把 None 转成 401。

设计要点（改动前必读）：

1. **两层拆分**：`extract_credential` 不依赖 store，`resolve_account` 不依赖
   request——每层都可独立单测。测试伪造 `_FakeRequest(headers=dict)` 不需要
   拉起 FastAPI，伪造 store 也无需真库。
2. **`now` 一律由调用方注入**（不 import time）：与 store/limits 同口径，让
   Key 过期窗口与 last_used 的推进都是确定性单测；生产调用点用
   `time.time()`。
3. **`policy` 是快照不是引用**：`resolve_account` 返回后 store 里同一 uid 的
   额度可能被管理员改——快照避免"限流判定时读到一半"的口径分裂。`limit
   guard` 每次判定时才拉新 row 做惰性预算重置，见 Task 6 契约。
4. **`touch_key_last_used` 在 resolve 命中时**恰好调用一次：它走独立 SQL，
   与用户查询不见锁竞争（同库但不同表 + 独立 autocommit）；写失败不吞但
   也不阻塞主链路——退化为 "last_used_at 未刷新"，不影响本次放行。
5. **`is_admin` 只在参与 monkey 时用处**（Task 7 管理路由会区分权限）。数
   据面本身不消费该字段——放进快照是给上层透传，别在 `auth` 层做 admin
   判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from modelctl.core.accounts.hashing import hash_api_key, key_prefix
from modelctl.core.accounts.limits import UserPolicy


@dataclass
class AccountIdentity:
    """一次请求通过 Key 认证后的账号身份快照。

    字段与 `users` / `api_keys` 联合行对齐 + 一个 `UserPolicy`（Task 6 限流
    判定按此过 `LimitGuard`）。`policy` 用 `field(default_factory=...)`
    避免可变 / None 默认歧义（dataclass 硬约束），`resolve_account` 里
    必然以实值覆盖，未初始化时视为"上层漏拼"（防签名静默退化）。
    """

    user_id: int
    key_id: int
    key_prefix: str
    username: str
    display_name: str
    is_admin: bool
    policy: UserPolicy = field(default_factory=UserPolicy)
    #: 明文 credential 的脱敏展示串（`sk-mctl-***xxxx` 或 `***`）。
    #: 用于错误回执 / 日志（避免直接回显完整 Key）。
    key_mask: str = ""

__all__ = ["AccountIdentity", "extract_credential", "resolve_account"]


def extract_credential(request: Any) -> str:
    """从 HTTP 请求头提取明文 credential（不查库、不校验）。

    双通道嗅探与 `gateway.verify_client` 完全一致：

    - `Authorization: Bearer <key>` 优先。大小写不敏感（`bEARer` / `BEARer`
      / `bearer` 都识别）；`Bearer` 与 token 之间的空白容忍，token 两端 strip。
    - 回退到 `x-api-key`（Anthropic Claude SDK 只带这个头）。
    - 均无 → `""`。空串把 "没凭证" 与 "发了空串" 归一口径。

    `request` 是 duck-typed：只要求 `request.headers.get(name) -> str | None`
    ——Starlette `Request` / httpx `Request` / 测试 `_FakeRequest` 都满足。
    """
    headers = getattr(request, "headers", {}) or {}
    candidate = ""
    auth = (headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        candidate = auth[len("bearer "):].strip()
    if not candidate:
        candidate = (headers.get("x-api-key") or "").strip()
    return candidate


def resolve_account(store: Any, credential: str, *, now: float) -> AccountIdentity | None:
    """Key credential → 账号身份快照；任何一步 miss / 非法 / 过期 → None。

    判定顺序（**故意**先 Key 后 user，让错误 Key 不泄漏任何用户存在性）：

    1. `credential` 空 / 空白 → None（不查库）。
    2. `hash_api_key(credential)` → `store.get_key_by_hash`；miss → None。
    3. Key 状态非 `active` → None（disabled / revoked 都走这里）。
    4. Key `expires_at` 非 None 且 `expires_at < now` → None（严格 "expires_at
       时刻之后才拒绝"；语义即"到 expires_at 那刻仍可用，超过即失效"。与
       数据库写入的秒级 float 边界行为一致：`expires_at = 999.0`、`now = 999.0`
       → 仍可访问；`now = 1000.0` → 过期）。
    5. `store.get_user_by_id(k["user_id"])`；miss → None（脏数据：孤儿 Key）。
    6. user `status != "active"` → None。
    7. 命中 → 组装 `AccountIdentity` 并用 `store.touch_key_last_used(key_id,
       now=now)` 刷新最近命中时刻。

    `now` 由调用方注入（Task 6 数据面用 `time.time()`；测试用固定值验证过期
    边界）。本函数不上抛业务异常——JSONDecodeError 级别的意外也不会阻塞
    网关（`verify_client` 走这个路径，出错必须 fail-close 到 401）。
    """
    if not credential or not credential.strip():
        return None
    key_hash = hash_api_key(credential)
    key = store.get_key_by_hash(key_hash)
    if key is None:
        return None
    if key.get("status") != "active":
        return None
    expires_at = key.get("expires_at")
    if expires_at is not None and float(expires_at) < now:
        return None
    user = store.get_user_by_id(int(key["user_id"]))
    if user is None:
        return None
    if user.get("status") != "active":
        return None

    policy = UserPolicy(
        concurrency_limit=user.get("concurrency_limit"),
        rpm_limit=user.get("rpm_limit"),
        tpm_limit=user.get("tpm_limit"),
        token_budget=user.get("token_budget"),
        budget_period=_period_to_seconds(user.get("budget_period")),
        budget_reset_at=user.get("budget_reset_at"),
        budget_consumed=int(user.get("budget_consumed") or 0),
        retention_days=user.get("retention_days"),
    )
    try:
        store.touch_key_last_used(int(key["id"]), now=now)
    except Exception:  # pragma: no cover - 库故障降级为 last_used 未刷新
        # 主链路不阻断：last_used_at 只是展示 / 审计字段，不影响放行判定。
        # 但仍需可观测，加 logger 供运维追踪（Task 9 把 accounts 域 logger
        # 独立出来时改走模块 logger）。
        import logging
        logging.getLogger(__name__).warning(
            "touch_key_last_used failed for key_id=%s user_id=%s",
            key.get("id"), user.get("id"), exc_info=True,
        )
    identity = AccountIdentity(
        user_id=int(user["id"]),
        key_id=int(key["id"]),
        key_prefix=key.get("key_prefix") or "",
        username=user.get("username") or "",
        display_name=user.get("display_name") or "",
        is_admin=bool(user.get("is_admin")),
        policy=policy,
        key_mask=key_prefix(credential),
    )
    return identity


def _period_to_seconds(period: str | None) -> float | None:
    """`users.budget_period` 字符串 → 秒（`limits.UserPolicy.budget_period` 类型口径）。

    store 里 `budget_period` 存的是短字符串（`day` / `week` / `month` 等），
    `limits` 里 `budget_period` 是 float 秒；本函数把 unit 语义翻译成秒，凭
    据层不重复限流逻辑。未知 / 空字符串视为"无限周期"（None）——这是**有意的**
    宽松行为：`token_budget` 非 None 但 `budget_period` 不可解析时不会崩，反而
    回到 "无重置" 语义，让管理员显式配错也仅表现为"预算从未清零"而不是 5xx。
    """
    if not period:
        return None
    text = str(period).strip().lower()
    if text in ("d", "day", "days", "1d"):
        return 86400.0
    if text in ("h", "hour", "hours", "1h"):
        return 3600.0
    if text in ("w", "week", "weeks", "1w"):
        return 7 * 86400.0
    if text in ("m", "month", "months", "1m"):
        # 与 `retention_days` 语义保持一致的口径：30 天一个预算周期
        return 30 * 86400.0
    if text in ("y", "year", "years", "1y"):
        return 365 * 86400.0
    return None
