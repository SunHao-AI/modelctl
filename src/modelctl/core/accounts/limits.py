#!/usr/bin/env python3
# ===============================================================================
# @File   : src/modelctl/core/accounts/limits.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/9 11:05
# @Desc   : 账号四道限流（并发 / RPM / TPM / 预算）
# ===============================================================================

"""core/accounts/limits.py — `LimitGuard`：并发 / RPM / TPM / 预算四道准入。

## 定位

- **纯内存对象**：本模块持有全部并发/RPM/TPM 计数器与预占（内存零 I/O），
  只在**预算到点**（惰性）时通过 `reset_budget_if_needed(policy, store, ...)`
  把 `store.reset_budget(user_id, now)` 打回去持久化一次——非到期是零写入。
- **无状态跨重启**：进程重启后 RPM/TPM 窗口清零、并发槽空、预算消费以 store 为
  单一真值源（Task 5 accountant 每次 settle 累加），下一次 `check_budget`
  从 policy 读最新 anchored 状态。计数器**必须**声明为进程内存态——
  跨进程一致性由部署口径（单网关实例）保证；多实例需要外部 Redis / DB 锁，
  不在本 spec 范围。
- **`0`/`None` 各项限额均视为不限**（与 `UserPolicy` 默认一致）。多实例
  CLI/日志输出的字段名含中文 `并发`等 → 输出层用 `display_width`+`pad_width`
  对齐（本模块错误消息本身含中文，也适用该规则；CLI 侧排版调用方处理）。

## 钟

模块级两个 clock 钩子：

- `_mono`：默认 `time.monotonic` —— RPM / TPM 固定窗口；不受系统 NTP 回拨影响。
- `_wall`：默认 `time.time` —— 预算 `budget_reset_at`（epoch 语义，落 store 里
  也是 epoch float 可比对）。

单测只需要注入 `guard._mono = fake`（与 `guard._wall = fake`），不需要类上
硬上构造器参数，以保持 API 简洁；测试见 `tests/test_accounts_limits.py`。

## TPM 预占 + 实际回补细节

一次 in-flight 请求：

```
处理器首行:  check_tpm_reserve(uid, limit, est_total, now, request_id)
            # 1. 溢出检查（若 est_total > 剩余则抛 rate_limit_exceeded）
            # 2. 窗口已用 +est_total；_tpm_pending[(uid, rid)] = est_total
流式结束:    add_tpm_actual(uid, est, actual, now, request_id)
            # 1. _tpm_used[uid] -= pending[（uid, rid）]（缺失按 0 处理：settle
            #    在异常路径下可能未预占）
            # 2. _tpm_used[uid] += actual
            # 3. _tpm_pending 移除 (uid, rid)
```

`_tpm_used` 也是固定 60s 窗口（RPM 同窗口机制，秒粒度一致）——所以 `add_
tpm_actual` 时若 `now` 已到下一窗口，先触发窗口 reset 再补齐 actual；
避免"下个窗口起步就把上个窗口超支算到新窗口"。

## 并发 vs RPM vs TPM 的次序

**Task 6 里建议顺序：预算（check_budget）→ 并发（acquire）→ RPM（check_rpm）→
TPM（check_tpm_reserve）**。理由：
- 预算是最"贵"的一致失败（一次判定即拒绝，无需向后回滚任何计数器）。
- 并发 acquire 是唯一"占资源"步骤；失败时前两步 no-op，后两步未执行，
  无需回滚。
- RPM/TPM 是纯判定；任一失败，仅需 `release(user_id)` 释放并发槽。

## 错误映射

| 场景 | type | retry_after |
| --- | --- | --- |
| 并发满 | `concurrency_exceeded` | 1（保守 hint；实际何时释放不可测）|
| RPM 满 | `rate_limit_exceeded` | 窗口剩余秒 |
| TPM 溢出 | `rate_limit_exceeded` | 窗口剩余秒 |
| 预算耗尽 | `budget_exceeded` | None |
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# RPM / TPM 固定窗口秒长。两者共享一个 60s 主 tick 保证"一分钟入门"语义。
_RPM_TPM_WINDOW_S = 60.0

# 并发满时给用户 wait 建议（秒）：并发槽只能被完成者释放，最短 hint 1s 防
# 客户端紧刷；真实释放时长不定，UI 侧提示文本不硬编码。
_CONCURRENCY_RETRY_HINT_S = 1.0


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class UserPolicy:
    """从 `users` 表同行的额度字段抽取出的策略快照。

    全部字段**可 None / ≤ 0 视为不限**——UID 政策层不做含义解释，由
    `is_unlimited` 统一判定。`budget_consumed` 单独放在
    `UserPolicy` 是因为 `reset_budget_if_needed` 需要就地改写它（同时推进
    `budget_reset_at`）；Task 6 会经此时钟从 store 拉最新 row，避免拿过期快照。
    """

    concurrency_limit: int | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    token_budget: int | None = None
    budget_period: float | None = None
    budget_reset_at: float | None = None
    budget_consumed: int = 0
    retention_days: int | None = None


def is_unlimited(limit: int | float | None) -> bool:
    """`None` / `<= 0` 一律视为"不限制"。

    一元哨兵：调用方只需 `if is_unlimited(limit): return` 跳过判定点。
    负数按 0 处理是防御编程（数据面偶有脏数据，不应因配置错误 block 全部
    流量）。
    """
    return limit is None or limit <= 0


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class UsageLimitError(Exception):
    """四道限流的统一拒绝异常，网关 envelope 5 个字段都能带上。

    属性升级为普通 instance attr（不是 frozen dataclass）：便于 Task 6 直接
    `raise` 后 `except UsageLimitError as ex` 打包响应，无需 repr 拼接。
    """

    def __init__(
        self,
        *,
        status: int,
        type: str,
        message: str,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.type = type
        self.message = message
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


class LimitGuard:
    """进程内限流守卫：并发 / RPM / TPM / 预算四道判定，纯内存计数。

    不是进程单例——Task 6 会 `create_app` 时构建一个实例挂在
    `app.state.limit_guard`；同一 FastAPI 进程内只此一份。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # 并发：{user_id: int}
        self._concurrency: dict[int, int] = {}

        # 固定 60s 窗口：RPM 独立记 (window_start, count)；TPM 独立记
        # (window_start, used)。两窗口 tick 不共用是因为 check 时机可以
        # 分别打点；窗口 start 都以"从未陈词归一到 0"的 now 打。
        self._rpm_state: dict[int, tuple[float, int]] = {}
        self._tpm_used: dict[int, tuple[float, int]] = {}
        # TPM 预占 (user_id, request_id) -> est
        self._tpm_pending: dict[tuple[int, object], int] = {}

        # 时钟（测试可替换，默认走 time）
        self._mono: Callable[[], float] = time.monotonic
        self._wall: Callable[[], float] = time.time

    # ------------------------------------------------------------------
    # UserPolicy 预算
    # ------------------------------------------------------------------

    def check_budget(self, policy: UserPolicy) -> None:
        """`budget_consumed < token_budget` 通过，否则抛
        `budget_exceeded`。`token_budget` None/0 视为不限、直接 return。"""
        if is_unlimited(policy.token_budget):
            return
        if policy.budget_consumed >= policy.token_budget:
            raise UsageLimitError(
                status=429,
                type="budget_exceeded",
                message=f"预算已耗尽（consumed={policy.budget_consumed}, "
                        f"budget={policy.token_budget}），待重置后可用",
                retry_after=None,
            )

    def reset_budget_if_needed(
        self,
        policy: UserPolicy,
        *,
        now: float | None = None,
        store: object | None = None,
        user_id: int | None = None,
    ) -> None:
        """惰性预算重置：`now >= budget_reset_at` 时把 `budget_consumed` 归零
        并推进 `budget_reset_at += budget_period`；`store` 非空则写库。

        锚点使用 `now`（`_wall` 缺省值）；不推进 budget（比如非预期分支
        budget_consumed 已经 0 或 policy 无 period 不做重置）。

        幂等：多次调用同一 now 不会二次推进（因为 `reset_at` 已在 `_wall`
        将来之前不满足）。`store.reset_budget(debetween normalized at)` 的
        具体签名：`(user_id: int, now: float)`，无返回值。
        """
        now = self._wall() if now is None else float(now)
        if is_unlimited(policy.budget_period):
            return
        if policy.budget_reset_at is None:
            # 该账号从未设定过重置点——直接消费累积不做惰性（无法计算下一次
            # 重置），交由管理员显式 touch。
            return
        if now < policy.budget_reset_at:
            return
        # 到期 → 消费清零 + 重置点推进一个 period
        new_reset_at = float(policy.budget_reset_at) + float(policy.budget_period)
        policy.budget_consumed = 0
        policy.budget_reset_at = new_reset_at
        if store is not None and user_id is not None:
            # store.reset_budget(user_id, *, next_reset_at, now) — 持久化口径
            # 由 limits 决定（本模块已知道 period），store 不再重复推导。
            store.reset_budget(user_id, next_reset_at=new_reset_at, now=now)

    # ------------------------------------------------------------------
    # 并发
    # ------------------------------------------------------------------

    def acquire(self, user_id: int, limit: int | None) -> bool:
        """占一个并发槽成功返回 True，满槽抛 `concurrency_exceeded`。

        `limit` None/0 视为无限制——不记账，直接 True。计数器下限 0
        （多次多余 release 不会下探为负导致下一次 acquire 溢出）。
        """
        if is_unlimited(limit):
            return True
        with self._lock:
            cur = self._concurrency.get(user_id, 0)
            if cur >= limit:
                raise UsageLimitError(
                    status=429,
                    type="concurrency_exceeded",
                    message=f"并发数已达上限（limit={limit}），请稍后重试",
                    retry_after=_CONCURRENCY_RETRY_HINT_S,
                )
            self._concurrency[user_id] = cur + 1
            return True

    def release(self, user_id: int) -> None:
        """释放一个并发槽；局限 0 避免负数释放。"""
        with self._lock:
            cur = self._concurrency.get(user_id, 0)
            if cur > 0:
                self._concurrency[user_id] = cur - 1
            # cur <= 0 时直接放弃：多余 release 视作噪音。

    # ------------------------------------------------------------------
    # RPM
    # ------------------------------------------------------------------

    def check_rpm(self, user_id: int, limit: int | None, *, now: float | None = None) -> None:
        """per-uid 滑动 60s 窗口，满窗内 `limit` 次则抛 `rate_limit_exceeded`。

        窗口锚：**该 uid 首个成功请求的 now** 为起点，之后 60s 内 `count`
        持续累计；60s 后再有请求自动切窗 reset count。选 per-uid 而非
        `int(now // 60 * 60)` 全局对齐的原因：用户视角"每分钟 N 次"就是
        "任意 60s 内 N 次"，全局对齐会让"1059.5 之前 60s 内已用满"与
        "60s tick 切分关系"脱钩。

        计数语义：**调用成功即 +1**；抛错时 count 保持不变（不做惩罚回滚）。
        `test_rpm_window_rollover_lets_new_window_pass` 中跨窗应在新窗口
        重新 3 次放行。
        """
        if is_unlimited(limit):
            return
        now = self._mono() if now is None else float(now)
        with self._lock:
            state = self._rpm_state.get(user_id)
            if state is None or (now - state[0]) >= _RPM_TPM_WINDOW_S:
                # 新窗口（首见 或 距上次窗口起点 >= 60s）
                window_start = now
                count = 0
            else:
                window_start, count = state
            if count >= limit:
                # 下一窗口起点 = 首次窗口起点 + 60s
                window_end = window_start + _RPM_TPM_WINDOW_S
                retry = max(window_end - now, _CONCURRENCY_RETRY_HINT_S)
                raise UsageLimitError(
                    status=429,
                    type="rate_limit_exceeded",
                    message=f"RPM 已达上限（limit={limit}/min），约 {int(retry)}s 后可重试",
                    retry_after=retry,
                )
            self._rpm_state[user_id] = (window_start, count + 1)

    # ------------------------------------------------------------------
    # TPM（预占 + 实际回补）
    # ------------------------------------------------------------------

    def check_tpm_reserve(
        self,
        user_id: int,
        limit: int | None,
        *,
        est_total: int,
        now: float | None = None,
        request_id: object,
    ) -> None:
        """预占 `est_total` 到当前 60s 窗口；溢出抛 `rate_limit_exceeded`。

        `est_total` 是**预估本请求产生的总 token**（上游 metrics 可取
        `stream_options.include_usage` 或客户端 max_tokens / 提示词长度估计）。

        固定 60s 窗口与 RPM 相同（见 `check_rpm` 说明），但独立计数器。
        预占后再调 `add_tpm_actual(user_id, est, actual, now, request_id)`
        结算。
        """
        now = self._mono() if now is None else float(now)
        if is_unlimited(limit):
            # 无限额时无需记账 pending：add_tpm_actual 也不会从此 dict 找 est
            return
        with self._lock:
            state = self._tpm_used.get(user_id)
            if state is None or (now - state[0]) >= _RPM_TPM_WINDOW_S:
                window_start = now
                used = 0
            else:
                window_start, used = state
            if used + int(est_total) > int(limit):
                # 下一窗口起点 = 首个请求起点 + 60s（与 RPM 同锚）
                window_end = window_start + _RPM_TPM_WINDOW_S
                retry = max(window_end - now, _CONCURRENCY_RETRY_HINT_S)
                raise UsageLimitError(
                    status=429,
                    type="rate_limit_exceeded",
                    message=f"TPM 预占已达上限（limit={limit}, used={used}），"
                            f"约 {int(retry)}s 后可重试",
                    retry_after=retry,
                )
            self._tpm_used[user_id] = (window_start, used + int(est_total))
            self._tpm_pending[(user_id, request_id)] = int(est_total)

    def add_tpm_actual(
        self,
        user_id: int,
        *,
        est: int,
        actual: int,
        now: float | None = None,
        request_id: object,
    ) -> None:
        """把实际 token 结算到 60s 窗口：`used -= pending[(uid, rid)]`
        然后 `used += actual`。

        **未预占兜底**：异常路径（例如 acquire 之后 check_tpm_reserve 未走
        到）让 `settle` 尝试 post-actual 时无 (uid, rid) 项；按 `est=0` 处理
        （即 `used += actual`），避免静默丢数据同时保守不欠账。
        """
        now = self._mono() if now is None else float(now)
        with self._lock:
            state = self._tpm_used.get(user_id)
            if state is None or (now - state[0]) >= _RPM_TPM_WINDOW_S:
                # 结算时才切窗：清零后 actual 落到新窗口起点上，
                # 与 check_tpm_reserve 保持一致的 per-uid sliding 语义。
                window_start = now
                used = 0
            else:
                window_start, used = state
            # 撤回预占（未登记估为 0：settle no-op）
            pending_est = self._tpm_pending.pop((user_id, request_id), 0)
            used = used - pending_est + int(actual)
            if used < 0:
                # 防御性 clamp：竞态或异常路径下的 est 追溯大于 used 时，
                # 不让窗口 taken 为负（下一窗口 reset 会自然覆盖）。
                used = 0
            self._tpm_used[user_id] = (window_start, used)
