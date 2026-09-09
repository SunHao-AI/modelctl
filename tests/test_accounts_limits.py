#!/usr/bin/env python3
# ===============================================================================
# @File   : tests/test_accounts_limits.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/9 11:00
# @Desc   : accounts/limits.py — LimitGuard（并发/RPM/TPM/预算）TDD 契约
# ===============================================================================

"""TDD 契约（对应 Task 4 计划）：

## UserPolicy
- 全 8 字段；`0`/`None` 一律视为"不限制"（适配器层验证）。
- **数据类不是策略体**：守卫读字段决定放行/抛错。

## UsageLimitError
- 继承 `Exception`。
- 属性：`status` 恒 429；`type` ∈ {`budget_exceeded`, `concurrency_exceeded`,
  `rate_limit_exceeded`}；`message` 中文消息；`retry_after`（秒，可无）。

## LimitGuard（进程内存态，锁内原子化）
### 并发
- `acquire(user_id, limit)`：`limit=None/0` 或当前 `running < limit` → True，
  running+1；否则抛 `UsageLimitError(type=concurrency_exceeded, retry_after≈1)`。
- `release(user_id)`：running-1（下限 0；多次 release 不爆负）。
- 同 user_id 反复 acquire → 到达 limit 即抛错。

### RPM（固定 60s 窗口）
- `check_rpm(user_id, limit, now)`：窗口以 `now` 为锚，跨窗口重置；达到 limit →
  抛 `rate_limit_exceeded`（`retry_after` 为下一窗口起点 - now，防止 0）。
- 时钟**由调用方注入**（`now` 参数），本测试全程零 sleep。窗口时长固定 60s
  但通过 `_clock_offset` 概念调整——不，简化为：以 `now` 直接为时间戳，内部
  记 `_rpm_state[user_id] = (window_start, count)`，同 window 只重置 count。
- `limit=None/0` → 无限制，直接 return。

### TPM（预占 + 实际回补）
- `check_tpm_reserve(user_id, limit, est_total, now)`：`est_total <= 剩余窗口余额`
  → 直接把 est 计入窗口已用；否则抛 `rate_limit_exceeded(retry_after 窗口剩余秒)`。
- `add_tpm_actual(user_id, actual, now)`：把实际值补偿进窗口——若 est=0 则 actual
  全部计入；否则以 actual 覆盖窗口内本次的预估（**先 -est + actual**）。为
  简化：可以把 est 与 actual 记录到 once，结算事件到来时 add_tpm_actual(user_id,
  actual) 内部知道 est=上次 check_tpm_reserve 的值来补偿。**因此 check_tpm_reserve
  必须把 est 记录到内部**（`_tpm_est[user_id, request_id?]`—**改用 uid 一一对应，
  实际网关每 uid 同时只允许一条 in-flight 到 1 …… 不对，并发就可能 N 条**）。
  → 更简单契约：`check_tpm_reserve` 与 `add_tpm_actual` **同参数具名 `request_id`**
  关联（`None` 用 object() 且不允许同 uid 同 request 并发）。

  但这样会破坏 Task 6 简洁性——改用**同一 (uid) 单在窗口内"预占聚合"模式**：
    `check_tpm_reserve` 把 est 计入窗口已用 + 记 `_tpm_pending[uid] += est`
    `add_tpm_actual` 从 `_tpm_pending[uid] -= est_known`（按 settle 参数传入 est）
    再 `window_used += (actual - est)`
  但 settle 需要知道 est，Task 5 里可透传。**结论：`add_tpm_actual(user_id,
  est, actual, now)` 三参数**：正好匹配 "先撤预占 + 加实际"。测试遵守此签名。

### 预算
- `check_budget(policy)`：`policy.token_budget` None/0 → return（不限）；
  否则 `policy.budget_consumed < token_budget` → True，否则抛
  `budget_exceeded`（无 `retry_after`）。
- `reset_budget_if_needed(policy, store=None, now=None)` 语义：
  `policy.budget_reset_at` 不为 None 且 `now >= reset_at` → 在 policy 上把
  `budget_consumed=0` + `budget_reset_at += budget_period`；**若 store 非 None
  则调用 `store.reset_budget(user_id, now=now)` 进行持久化**。
- `now` 参数便于测试控时；**`monotonic` 不用于预算**（`real now` 才有语义），
  **`monotonic` 只用于 RPM/TPM 窗口防时钟跳变**——但因为 TDD 契约需要显式
  注入，我们把 `now` 参数就当单调秒表，由 Task 6 传 `time.monotonic()`/
  `time.time()` 取决于场景（预算偏 `time.time()`；RPM/TPM 偏 `time.monotonic()`
  防时钟回拨）。**因此内部两个 clock**：`_monotonic`（默认 `time.monotonic`）
  与 `_wall`（默认 `time.time`），都可注入。测试注入 `_monotonic()` 来控制窗口。

### 0 / None 视为不限（模块级辅助 `is_unlimited(limit)`）
- `is_unlimited(limit) -> True` 当 `limit is None or limit <= 0`。

## 护栏
- `LimitGuard` 有多线程安全（内部 `_lock` 保护所有三组计数字典）。测试用
  `threading.Barrier` 做 10 并发 acq → 只有 5 成功（limit=5）的并发高压小用例。
"""

from __future__ import annotations

import threading

import pytest


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

POLICY_FIELDS = [
    "concurrency_limit",
    "rpm_limit",
    "tpm_limit",
    "token_budget",
    "budget_period",
    "budget_reset_at",
    "budget_consumed",
    "retention_days",
]


@pytest.fixture
def clock():
    """可推进的 monotonically-increasing 时钟（仅供 RPM/TPM/预算）；1 次 +1s。"""
    import time
    import types

    class _FakeClock:
        def __init__(self, start: float) -> None:
            self._now = float(start)

        def monotonic(self) -> float:
            return self._now

        def time(self) -> float:
            return self._now

        def advance(self, seconds: float) -> None:
            self._now += float(seconds)

    return _FakeClock(start=1000.0)


@pytest.fixture
def guard(clock) -> "object":
    from modelctl.core.accounts import limits

    g = limits.LimitGuard()
    # 测试内统一注入单 clock（同一 fake 承载 monotonic 与 wall，因为它们不
    # 需实际语义区分；测试不会回拨 +1s 前进即可）
    g._mono = clock.monotonic
    g._wall = clock.time
    return g


def _policy(**kw):
    from modelctl.core.accounts.limits import UserPolicy

    defaults: dict = {f: None for f in POLICY_FIELDS}
    defaults["budget_period"] = 3600.0
    defaults.update(kw)
    return UserPolicy(**defaults)


# ---------------------------------------------------------------------------
# UserPolicy / UsageLimitError
# ---------------------------------------------------------------------------

def test_user_policy_fields_and_defaults() -> None:
    from modelctl.core.accounts.limits import UserPolicy

    p = UserPolicy(
        concurrency_limit=5,
        rpm_limit=60,
        tpm_limit=10000,
        token_budget=100000,
        budget_period=86400,
        budget_reset_at=2000.0,
        budget_consumed=10,
        retention_days=7,
    )
    assert p.concurrency_limit == 5
    assert p.rpm_limit == 60
    assert p.tpm_limit == 10000
    assert p.token_budget == 100000
    assert p.budget_period == 86400
    assert p.budget_reset_at == 2000.0
    assert p.budget_consumed == 10
    assert p.retention_days == 7

    # 缺省/None 均可
    p2 = UserPolicy()
    assert p2.concurrency_limit is None


def test_is_unlimited_none_zero_negative() -> None:
    from modelctl.core.accounts.limits import is_unlimited

    assert is_unlimited(None) is True
    assert is_unlimited(0) is True
    assert is_unlimited(-1) is True
    assert is_unlimited(1) is False
    assert is_unlimited(10) is False


def test_usage_limit_error_carries_fields() -> None:
    from modelctl.core.accounts.limits import UsageLimitError

    e = UsageLimitError(
        status=429,
        type="rate_limit_exceeded",
        message="RPM 已达上限",
        retry_after=37.5,
    )
    assert isinstance(e, Exception)
    assert e.status == 429
    assert e.type == "rate_limit_exceeded"
    assert "RPM" in e.message
    assert e.retry_after == 37.5


# ---------------------------------------------------------------------------
# 并发 acquire / release
# ---------------------------------------------------------------------------

def test_concurrency_acquire_release_basic(guard, clock) -> None:
    from modelctl.core.accounts.limits import UsageLimitError

    ok = guard.acquire("u1", 2)
    assert ok is True
    guard.acquire("u1", 2)
    # 满 2 后再 acquire 应抛
    with pytest.raises(UsageLimitError) as ei:
        guard.acquire("u1", 2)
    assert ei.value.type == "concurrency_exceeded"
    assert ei.value.status == 429

    # 释放一个即可再次 acquire
    guard.release("u1")
    assert guard.acquire("u1", 2) is True
    guard.release("u1")
    guard.release("u1")


def test_concurrency_multiple_users_independent(guard) -> None:
    from modelctl.core.accounts.limits import UsageLimitError

    guard.acquire("a", 1)
    # a 满但 b 独立
    assert guard.acquire("b", 1) is True
    with pytest.raises(UsageLimitError):
        guard.acquire("a", 1)


def test_concurrency_release_below_zero_is_clamped(guard) -> None:
    # release 无释放保护：底层 count 不应 < 0
    guard.release("ghost")
    assert guard.acquire("ghost", 1) is True
    guard.release("ghost")


def test_concurrency_unlimited_passes_always(guard) -> None:
    for i in range(50):
        assert guard.acquire("u", 0) is True
        assert guard.acquire("v", None) is True


def test_concurrency_thread_safety(guard) -> None:
    """10 线程 race acquire，limit=5 ⇒ 恰好 5 成功。"""
    from modelctl.core.accounts.limits import UsageLimitError

    barrier = threading.Barrier(10)
    ok = []
    rejected = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            guard.acquire("r", 5)
            with lock:
                ok.append(True)
        except UsageLimitError:
            with lock:
                rejected.append(True)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(ok) == 5
    assert len(rejected) == 5


# ---------------------------------------------------------------------------
# RPM 固定窗口
# ---------------------------------------------------------------------------

def test_rpm_within_window_allows_under_limit(guard, clock) -> None:
    from modelctl.core.accounts.limits import UsageLimitError

    # per-uid sliding：以首个请求 1000.0 为窗口起点，60s 内累计
    guard.check_rpm("u", 3, now=1000.0)
    guard.check_rpm("u", 3, now=1020.0)
    guard.check_rpm("u", 3, now=1050.0)  # 第 3 次仍在窗口内
    # 第 4 次达 limit 抛错
    with pytest.raises(UsageLimitError) as ei:
        guard.check_rpm("u", 3, now=1059.0)
    assert ei.value.type == "rate_limit_exceeded"
    assert ei.value.status == 429
    # retry_after 应 ≤ 60 且 > 0（窗口还剩 1s，防御下限 1s）
    assert 0 < (ei.value.retry_after or 0) <= 60


def test_rpm_window_rollover_lets_new_window_pass(guard, clock) -> None:
    from modelctl.core.accounts.limits import UsageLimitError

    # 打满（3/3）：1000 / 1010 / 1030 都在窗口 1000-1060 内
    for t in (1000.0, 1010.0, 1030.0):
        guard.check_rpm("u", 3, now=t)
    with pytest.raises(UsageLimitError):
        guard.check_rpm("u", 3, now=1059.5)
    # 跨过 60s（1062 ≥ 1000 + 60）→ 新窗口起点 1062.0 重新 3 次可放行
    guard.check_rpm("u", 3, now=1062.0)
    guard.check_rpm("u", 3, now=1070.0)
    guard.check_rpm("u", 3, now=1100.0)
    # 新窗口用满，下一次抛
    with pytest.raises(UsageLimitError):
        guard.check_rpm("u", 3, now=1110.0)


def test_rpm_unlimited_skips(guard) -> None:
    for i in range(100):
        guard.check_rpm("u", None, now=float(1000 + i))
        guard.check_rpm("u", 0, now=float(2000 + i))


def test_rpm_retry_after_points_to_next_window(guard) -> None:
    from modelctl.core.accounts.limits import UsageLimitError

    # 窗口锚 1000，60s 后到 1060
    guard.check_rpm("u", 1, now=1000.0)
    with pytest.raises(UsageLimitError) as ei:
        guard.check_rpm("u", 1, now=1030.0)
    # 应等到 1060，剩余 30s
    assert ei.value.retry_after == pytest.approx(30.0, abs=0.5)


# ---------------------------------------------------------------------------
# TPM 预占 + 实际回补
# ---------------------------------------------------------------------------

def test_tpm_reserve_simple(guard) -> None:
    from modelctl.core.accounts.limits import UsageLimitError

    guard.check_tpm_reserve("u", 1000, est_total=400, now=1000.0, request_id="r1")
    guard.check_tpm_reserve("u", 1000, est_total=500, now=1010.0, request_id="r2")
    # 第 3 次 200 应超 400+500=900 → 剩 100 < 200
    with pytest.raises(UsageLimitError):
        guard.check_tpm_reserve("u", 1000, est_total=200, now=1020.0, request_id="r3")


def test_tpm_actual_settle_frees_window(guard) -> None:
    """est 预占 + actual 结算后，窗口已用 = sum(actual)。"""
    from modelctl.core.accounts.limits import UsageLimitError

    # est_r1=400, est_r2=500（合计 900）实际 r1=40, r2=50
    guard.check_tpm_reserve("u", 1000, est_total=400, now=1000.0, request_id="r1")
    guard.check_tpm_reserve("u", 1000, est_total=500, now=1010.0, request_id="r2")
    guard.add_tpm_actual("u", est=400, actual=40, now=1020.0, request_id="r1")
    guard.add_tpm_actual("u", est=500, actual=50, now=1030.0, request_id="r2")

    # 结算后窗口已用 = 40+50=90。再 reserve 800 应能放行（90+800<1000）
    guard.check_tpm_reserve("u", 1000, est_total=800, now=1040.0, request_id="r3")
    # 剩余 100-400=… 合计 90+800=890 < 1000 → 通过；再加 200 又超
    with pytest.raises(UsageLimitError):
        guard.check_tpm_reserve("u", 1000, est_total=200, now=1050.0, request_id="r4")


def test_tpm_actual_overshoots_still_frees_more_than_just_est(guard) -> None:
    """actual > est 时，窗口新增 (actual - est)。"""
    from modelctl.core.accounts.limits import UsageLimitError

    guard.check_tpm_reserve("u", 100, est_total=50, now=1000.0, request_id="r1")
    guard.add_tpm_actual("u", est=50, actual=80, now=1020.0, request_id="r1")
    # 已用 = 50 - 50 + 80 = 80，剩 20 < 50
    with pytest.raises(UsageLimitError):
        guard.check_tpm_reserve("u", 100, est_total=50, now=1040.0, request_id="r2")


def test_tpm_settle_without_prior_reserve_logs_no_error(guard) -> None:
    """请求重试/超时导致 settle 时发现 est 未登记：会计 0 est，仅加 actual。"""
    from modelctl.core.accounts.limits import UsageLimitError

    # 无 r1 预占直接 settle
    guard.add_tpm_actual("u", est=100, actual=50, now=1000.0, request_id="r1")
    # 因为无 est 登记，视作 actual=50 直接进窗口（est=0），余量 = 200-50=150
    guard.check_tpm_reserve("u", 200, est_total=100, now=1010.0, request_id="r2")
    with pytest.raises(UsageLimitError):
        guard.check_tpm_reserve("u", 200, est_total=100, now=1020.0, request_id="r3")


def test_tpm_unlimited_passes(guard) -> None:
    for i in range(20):
        guard.check_tpm_reserve("u", 0, est_total=10_000, now=float(1000 + i), request_id=f"r{i}")
    guard.add_tpm_actual("u", est=10_000, actual=20_000, now=2000.0, request_id="rr")


# ---------------------------------------------------------------------------
# 预算（惰性重置）
# ---------------------------------------------------------------------------

def test_check_budget_within_passes(guard) -> None:
    p = _policy(token_budget=1000, budget_consumed=999)
    guard.check_budget(p)  # 不抛


def test_check_budget_at_capacity_raises(guard) -> None:
    from modelctl.core.accounts.limits import UsageLimitError

    p = _policy(token_budget=1000, budget_consumed=1000)
    with pytest.raises(UsageLimitError) as ei:
        guard.check_budget(p)
    assert ei.value.type == "budget_exceeded"
    assert ei.value.retry_after is None
    assert "预算" in ei.value.message or "budget" in ei.value.message.lower()


def test_check_budget_unlimited(guard) -> None:
    guard.check_budget(_policy(token_budget=None, budget_consumed=999999999))
    guard.check_budget(_policy(token_budget=0, budget_consumed=-42))


def test_reset_budget_if_needed_rolls_when_due(guard, clock) -> None:
    p = _policy(
        token_budget=1000,
        budget_consumed=800,
        budget_period=3600.0,
        budget_reset_at=1500.0,
    )
    # now < reset_at → 不重置
    guard.reset_budget_if_needed(p, now=1499.0)
    assert p.budget_consumed == 800
    assert p.budget_reset_at == 1500.0
    # now == reset_at → 重置 + 推进 period
    guard.reset_budget_if_needed(p, now=1500.0)
    assert p.budget_consumed == 0
    assert p.budget_reset_at == pytest.approx(1500.0 + 3600.0)


def test_reset_budget_if_needed_persists_to_store(guard, clock) -> None:
    """到期时 store.reset_budget(user_id, now) 恰好调用一次；未到期/每期一次。"""

    calls: list[tuple] = []

    class _FakeStore:
        def reset_budget(self, user_id: int, *, next_reset_at: float, now: float) -> None:
            calls.append((user_id, next_reset_at, now))

    store = _FakeStore()

    p = _policy(
        token_budget=500,
        budget_consumed=400,
        budget_period=600.0,
        budget_reset_at=2000.0,
    )

    # 到期前：不重置不写库
    guard.reset_budget_if_needed(p, now=1990.0, store=store, user_id=7)
    assert calls == []
    assert p.budget_consumed == 400

    # 到期：内存消费归零、时钟推进 period、store 写一次
    guard.reset_budget_if_needed(p, now=2000.0, store=store, user_id=7)
    assert calls == [(7, 2600.0, 2000.0)]
    assert p.budget_consumed == 0
    assert p.budget_reset_at == pytest.approx(2600.0)

    # 同一 period 内未到期（`reset_at` 已推进到 2600）：不重复触发
    guard.reset_budget_if_needed(p, now=2599.0, store=store, user_id=7)
    assert calls == [(7, 2600.0, 2000.0)]

    # 下一个重置时（now>=2600）又触发一次
    guard.reset_budget_if_needed(p, now=2600.0, store=store, user_id=7)
    assert calls == [(7, 2600.0, 2000.0), (7, 3200.0, 2600.0)]
    assert p.budget_reset_at == pytest.approx(3200.0)


def test_no_budget_period_means_no_reset(guard) -> None:
    """budget_period=None/0 → 预算永不自动重置；guard 不搬动时钟也不改字段。"""
    p = _policy(token_budget=100, budget_consumed=100, budget_period=None,
                budget_reset_at=1000.0)
    guard.reset_budget_if_needed(p, now=9000.0)
    assert p.budget_consumed == 100


def test_budget_window_overflow_still_blocks_even_after_reset_miss(guard) -> None:
    """budget_consumed 到达/超过 budget 视为阻断（不消耗不推进 = 拒绝）。"""
    from modelctl.core.accounts.limits import UsageLimitError

    p = _policy(
        token_budget=100, budget_consumed=250, budget_period=3600.0,
        budget_reset_at=2_000_000.0,
    )
    with pytest.raises(UsageLimitError):
        guard.check_budget(p)
