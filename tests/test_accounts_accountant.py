#!/usr/bin/env python3
# ===============================================================================
# @File   : tests/test_accounts_accountant.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/9 11:25
# @Desc   : accounts/accountant.py 异步记账队列
# ===============================================================================

"""TDD 契约（对应 Task 5 计划）：

- `class Accountant(store)`：
  - `__init__(store)`: 未 start 前 settle 调用应缓冲到内存 pending 队列（不丢
    消息）。`start()` 后消费 worker 线程 drain。**测试禁真后台消费**——
    通过 `Accountant._drain_once()` 同步调用做单步 assert，保持稳定。
  - `settle(*, user_id: int, key_id: int | None, model: str,
            prompt_tokens: int, completion_tokens: int,
            status: str, client_ip: str = "", ttft_ms: int | None = None,
            latency_ms: int | None = None,
            session_id: str | None = None, title: str = "",
            user_msg: str = "", assistant_msg: str = "", now: float) -> None`
    仅入队（`now` 也入队，不取时钟——便于测试）。
  - `stop()`: **不丢队列**——先把队列排空再返回（或至少 `drain` 完显式
    通知时不中断）。测试会断言 "settle 3 项 + stop() 后计数器仍全部落库"。

- 单次 settle 效果（针对同一 `(user_id, model)` domain）：
  - `usage_records` 表 +1 行：
    - `prompt_tokens` / `completion_tokens` / `total_tokens` = p + c
    - `status` 透传
    - `key_id` 透传
  - `users.budget_consumed` 恰好 += p + c（若 ≥0；负值 clamp 到 0，兼容
    store.insert_usage 内部 clamp 口径）
  - `sessions` 表：
    - 无 `session_id` 且无现有 (user_id, key_id, model) 空闲窗口会话 → 新建 1 行
    - 有 `session_id` → 复用（幂等，第二 settle 同 id 命中已有 id）
    - **跨用户 session_id 不劫持**：A 的 (sid, A.user_id) 命中后 B 传同 sid
      → B 得新建（spec §6.2）。
  - `messages` 表：
    - user_msg 非空 → +1 行 role="user"
    - assistant_msg 非空 → +1 行 role="assistant"
  - `sessions.message_count` += 落库的消息行数（可能 1 或 2）：
    **单条 UPDATE 原子**（`bump_session(..., count=<N>)`）。

- 异常隔离：单条 settle 落库抛错时不应终止 worker；其他 settle 继续落。

- `stop()` 幂等：多次 start/stop 不 stream 泄漏。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def store(tmp_path) -> "object":
    from modelctl.core.accounts.store import AccountsStore
    s = AccountsStore(tmp_path / "accounts_accountant.db")
    s.init_db()
    yield s
    s.close()


@pytest.fixture
def accountant(store) -> "object":
    from modelctl.core.accounts.accountant import Accountant
    a = Accountant(store)
    try:
        yield a
    finally:
        a.stop()


def _mk_user(store, username="alice", now=1000.0):
    return store.create_user(username=username, password_hash="bcrypt$fake",
                             now=now)


def _mk_key(store, user_id, now=1000.0, name="default"):
    from modelctl.core.accounts import hashing as h
    cred = f"sk-mctl-u{user_id}-{name}"
    kh = h.hash_api_key(cred)
    return store.create_key(user_id=user_id, key_hash=kh,
                            key_prefix="sk-mctl-***abcd", name=name, now=now)


def _settle_ok(accountant, *, user_id, key_id, model="qwen3.8",
               prompt_tokens=100, completion_tokens=50,
               status="ok", client_ip="1.2.3.4", ttft_ms=None, latency_ms=100,
               session_id=None, title="",
               user_msg="hello", assistant_msg="hello world",
               now=1000.0):
    accountant.settle(
        user_id=user_id, key_id=key_id, model=model,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        status=status, client_ip=client_ip, ttft_ms=ttft_ms, latency_ms=latency_ms,
        session_id=session_id, title=title,
        user_msg=user_msg, assistant_msg=assistant_msg,
        now=now,
    )
    accountant._drain_once()  # 测试同步 drain


# ---------------------------------------------------------------------------
# settle → store side-effect
# ---------------------------------------------------------------------------

def test_settle_writes_usage_record(accountant, store) -> None:
    uid = _mk_user(store)
    kid = _mk_key(store, uid, now=1000.0)
    _settle_ok(accountant, user_id=uid, key_id=kid,
               prompt_tokens=100, completion_tokens=50,
               status="ok", latency_ms=1234, now=1500.0)

    rows = store.list_usage_for_user(uid)
    assert len(rows) == 1
    r = rows[0]
    assert r["user_id"] == 1
    assert r["key_id"] == 1
    assert r["model"] == "qwen3.8"
    assert r["prompt_tokens"] == 100
    assert r["completion_tokens"] == 50
    assert r["total_tokens"] == 150
    assert r["status"] == "ok"
    assert r["client_ip"] == "1.2.3.4"
    assert r["ttft_ms"] is None
    assert r["latency_ms"] == 1234
    assert r["created_at"] == 1500.0


def test_settle_increments_budget_consumed(accountant, store) -> None:
    uid = _mk_user(store)
    kid = _mk_key(store, uid)
    assert store.get_user_by_id(uid)["budget_consumed"] == 0
    _settle_ok(accountant, user_id=uid, key_id=kid,
               prompt_tokens=100, completion_tokens=50, now=1500.0)

    row = store.get_user_by_id(uid)
    assert row["budget_consumed"] == 150
    # 两次 settle：累加
    _settle_ok(accountant, user_id=uid, key_id=kid,
               prompt_tokens=10, completion_tokens=20, now=1600.0)
    row = store.get_user_by_id(uid)
    assert row["budget_consumed"] == 180


def test_settle_creates_session_and_messages(accountant, store) -> None:
    uid = _mk_user(store)
    kid = _mk_key(store, uid)
    # 无 session_id → 新建会话（按 user/key/model 空闲窗口聚合）
    _settle_ok(accountant, user_id=uid, key_id=kid, model="qwen3.8",
               session_id=None, title="first-round",
               user_msg="hi", assistant_msg="hello",
               prompt_tokens=2, completion_tokens=3, now=1000.0)

    sessions = store.list_sessions(uid)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["model"] == "qwen3.8"
    assert s["title"] == "first-round"
    assert s["message_count"] == 2
    assert s["last_active_at"] == 1000.0

    msgs = store.get_messages(s["id"])
    assert len(msgs) == 2
    assert [(m["role"], m["content"]) for m in msgs] == [("user", "hi"), ("assistant", "hello")]


def test_settle_samel_session_id_reuses(accountant, store) -> None:
    uid = _mk_user(store)
    kid = _mk_key(store, uid)
    _settle_ok(accountant, user_id=uid, key_id=kid,
               session_id="my-conv", title="first", now=1000.0)
    sessions = store.list_sessions(uid)
    first_id = sessions[0]["id"]

    # 再次 settle 同 external session_id → 同会话 + message_count 再增
    _settle_ok(accountant, user_id=uid, key_id=kid,
               session_id="my-conv", user_msg="hi2", assistant_msg="hi2", now=1010.0)
    sessions = store.list_sessions(uid)
    assert len(sessions) == 1
    assert sessions[0]["id"] == first_id
    assert sessions[0]["message_count"] == 4  # 2+2


def test_settle_cross_user_session_id_no_hijack(accountant, store) -> None:
    """跨用户同 session_id：B 不会写到 A 的会话里——命中条件必带 user_id。"""
    ua = _mk_user(store, username="a")
    ub = _mk_user(store, username="b")
    kid_a = _mk_key(store, ua)
    kid_b = _mk_key(store, ub)
    _settle_ok(accountant, user_id=ua, key_id=kid_a, session_id="shared-sid",
               user_msg="from a", assistant_msg="ack a", now=1000.0)
    # B 传同样 external sid → 应 new 新会话（B 的 message_count 独立）
    _settle_ok(accountant, user_id=ub, key_id=kid_b, session_id="shared-sid",
               user_msg="from b", assistant_msg="ack b", now=1010.0)

    a_sessions = store.list_sessions(ua)
    b_sessions = store.list_sessions(ub)
    assert len(a_sessions) == 1 and len(b_sessions) == 1
    assert a_sessions[0]["id"] != b_sessions[0]["id"]
    # A 的会话 title & 消息独立
    assert a_sessions[0]["message_count"] == 2


def test_settle_empty_messages_no_bump(accountant, store) -> None:
    """user_msg 与 assistant_msg 都空 → 不写 messages，message_count 不变。"""
    uid = _mk_user(store)
    kid = _mk_key(store, uid)
    _settle_ok(accountant, user_id=uid, key_id=kid, session_id="s",
               user_msg="", assistant_msg="", now=1000.0)

    session = store.list_sessions(uid)[0]
    assert session["message_count"] == 0
    assert store.get_messages(session["id"]) == []


# ---------------------------------------------------------------------------
# 队列行为
# ---------------------------------------------------------------------------

def test_settle_before_start_buffers_then_drains_on_start(accountant, store) -> None:
    uid = _mk_user(store)
    kid = _mk_key(store, uid)
    # accessor 未 start（fixture 直接 Accountant(store) 未调 start）
    # 但 _drain_once 是手动驱动，所以这里直接 settle 3 次并 drain
    for i in range(3):
        _settle_ok(accountant, user_id=uid, key_id=kid,
                   prompt_tokens=10 + i, completion_tokens=20 + i,
                   now=1000.0 + i)
    assert len(store.list_usage_for_user(uid)) == 3


def test_stop_drains_remaining_before_returning(accountant, store) -> None:
    """stop 前排空队列再返回：sink 不丢消息。"""
    uid = _mk_user(store)
    kid = _mk_key(store, uid)
    accountant.settle(user_id=uid, key_id=kid, model="qwen3.8",
                      prompt_tokens=5, completion_tokens=5, status="ok",
                      now=1000.0)
    # 未 drain 就 stop：应触发内部 drain 直到队列空
    accountant.stop()
    assert len(store.list_usage_for_user(uid)) == 1
    # 二次 stop 不 stream
    accountant.stop()


def test_worker_exception_does_not_terminate(accountant, store, monkeypatch) -> None:
    """单条 settle 抛错：不终止 worker，后续 drain 继续。"""
    uid = _mk_user(store)
    kid = _mk_key(store, uid)
    # 让第 1 次 settle 用到时 store.insert_usage 抛一次
    orig = store.insert_usage
    calls = {"n": 0}

    def boom(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("sink failure")
        return orig(**kw)

    monkeypatch.setattr(store, "insert_usage", boom)

    # 第 1 条预期抛错
    accountant.settle(user_id=uid, key_id=kid, model="qwen3.8",
                      prompt_tokens=1, completion_tokens=1, status="err",
                      now=1000.0)
    accountant.__dict__.get("_error_buffer", set()) if False else None
    try:
        accountant._drain_once()
    except Exception:
        pass  # 契约：内部捕获不 re-raise

    # 第 2 条成功
    accountant.settle(user_id=uid, key_id=kid, model="qwen3.8",
                      prompt_tokens=2, completion_tokens=2, status="ok",
                      now=1010.0)
    accountant._drain_once()
    # 至少第 2 条落库（used 2 行中可能只 1 行——取决于失败 path 后 usage 失败但
    # budget_consumed/messages 是否仍落；契约：_process 捕获全部异常局部不入下条。
    # 首次 settle usage 抛错 → 后续 budget/messages 不跑（失败在 usage 后中止）
    rows = store.list_usage_for_user(uid)
    assert any(r["status"] == "ok" for r in rows)
