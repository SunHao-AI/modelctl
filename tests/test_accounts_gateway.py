#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_accounts_gateway.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/9 14:00
# @Desc   : gateway 账号 Key 数据面接入（限额/会话/结算）
# ===============================================================================

"""Task 6 TDD 契约：gateway 在 ACCOUNTS_ENABLED 开启时的行为。

分两组：

- **回归护栏**（accounts_enabled=False / 未传 accounts_store）：
  沿用现有 verify_client 单钥 fail-closed，零回归。这一组通过 `test_gateway.py`
  覆盖，此处只针对 `create_app` 新参数默认 `accounts_enabled=None` 走 env 的
  回退路径断言一次。

- **启用分支**（accounts_enabled=True + 注入 AccountsStore）：
  - 无效 Key（不存在 / 禁用）→ 401 `invalid_api_key`
  - 预算耗尽 → 429 `budget_exceeded`
  - 并发超限 → 429 `concurrency_exceeded` + Retry-After
  - RPM 超限 → 429 `rate_limit_exceeded`
  - TPM 预占溢出 → 429 `rate_limit_exceeded`
  - 有效 Key + 有效上游 → 200，且并发槽在**流结束后**释放（可再次请求）
  - `X-Session-Id` 请求头落到 sessions 表并复用
  - 结束时 `accountant.settle` 落库：usage_records + budget_consumed 累加 +
    session/messages 写入；`limit_guard.add_tpm_actual` 结算
  - 审计记录 `_build_audit_entry` 里包含 `user_id` / `key_id`（401 走 None）
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from modelctl.core.accounts.hashing import hash_api_key
from modelctl.core.accounts.store import AccountsStore
from modelctl.core.gateway import GatewayModel, create_app

# 一个受控的 epoch 时基（够大到不会和 0 边界混淆）
_NOW = 1_700_000_000.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _mk_store(tmp_path) -> AccountsStore:
    s = AccountsStore(tmp_path / "accounts.db")
    s.init_db()
    return s


def _mk_user_key(
    store: AccountsStore,
    *,
    username: str = "alice",
    policy: dict | None = None,
    password_hash: str = "bcrypt$fake",
) -> tuple[int, str]:
    """建号 + 签发一把 active Key；返回 (user_id, 明文 key)。"""
    uid = store.create_user(
        username=username,
        password_hash=password_hash,
        now=_NOW,
        **({} if policy is None else policy),
    )
    # 用短前缀保证 key 在测试里可读；hashing.hash_api_key 直接 sha256
    from modelctl.core.accounts.hashing import key_prefix
    cred = f"sk-mctl-{username}-test-key-0001"
    kh = hash_api_key(cred)
    kp = key_prefix(cred)
    store.create_key(
        user_id=uid,
        key_hash=kh,
        key_prefix=kp,
        name="test",
        now=_NOW,
    )
    return uid, cred


def _post_headers(app, path: str, *, json_body: dict | None = None,
                 headers: dict | None = None):
    async def _call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=headers or {},
        ) as client:
            return await client.post(path, json=json_body or {})
    return _run(_call())


def _get_headers(app, path: str, *, headers: dict | None = None):
    async def _call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=headers or {},
        ) as client:
            return await client.get(path)
    return _run(_call())


def _drain_accountant(app) -> None:
    """任务结束、ASGI 生命周期未走 lifespan（httpx.ASGITransport 默认不触发）时，
    手动触发 `Accountant._drain_once()` 同步完成全部 settle 落库；等价于
    uvicorn 关闭时 `lifespan` yield 之后调用的 `stop()` 语义。"""
    acc = getattr(app.state, "accountant", None)
    if acc is not None:
        acc._drain_once()


def _reg_one() -> dict:
    return {
        "qwen3.8": GatewayModel(
            "qwen3.8", "ollama", "http://upstream", "qwen3.8:27b",
            None, "http://upstream/",
        ),
    }


def _upstream_ok_200(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/messages":
        return httpx.Response(200, json={
            "id": "msg-1", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })
    return httpx.Response(200, json={
        "id": "chatcmpl-1", "object": "chat.completion",
        "choices": [{"message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })


def _noop_audit():
    """与 `core.audit.NoopAuditLog` 等价：显式导入会在 gateway 未装时炸；
    这里手工写一份最小 stub 更稳。"""
    class _A:
        def record(self, entry):
            self.entries.append(entry)
            return True
        def ensure_cleanup_thread(self):
            pass
        def destroy(self):
            pass
        def __init__(self):
            self.entries = []
    a = _A()
    return a


# ---------------------------------------------------------------------------
# 回归护栏：不启用 accounts → 现有 verify_client 行为
# ---------------------------------------------------------------------------

def test_accounts_disabled_zero_regression(tmp_path, monkeypatch):
    """不启用 accounts：`accounts_enabled=False` + 客户端 GATEWAY_CLIENT_API_KEY，
    请求 /v1/chat/completions 使用合法 key 必须 200、无 key 401、错 key 401。
    这保证 `create_app` 新参数默认不改变既有行为。"""
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", "mgt-key")
    monkeypatch.delenv("ACCOUNTS_ENABLED", raising=False)
    audit = _noop_audit()
    app = create_app(
        _reg_one(), default_model="qwen3.8",
        transport=httpx.MockTransport(_upstream_ok_200),
        audit_log=audit,
        accounts_enabled=False,  # 显式关闭（应与 None + env 未设等价）
    )
    ok = _post_headers(app, "/v1/chat/completions",
                       json_body={"model": "qwen3.8", "messages": []},
                       headers={"Authorization": "Bearer mgt-key"})
    assert ok.status_code == 200
    missing = _post_headers(app, "/v1/chat/completions",
                            json_body={"model": "qwen3.8", "messages": []},
                            headers={})
    assert missing.status_code == 401
    wrong = _post_headers(app, "/v1/chat/completions",
                          json_body={"model": "qwen3.8", "messages": []},
                          headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401


# ---------------------------------------------------------------------------
# accounts_enabled=True —— Key 识别
# ---------------------------------------------------------------------------

def test_enabled_invalid_key_401(tmp_path, monkeypatch):
    """未签发 / 错 Key → 401 `invalid_api_key`；审计里 user_id=None key_id=None。"""
    monkeypatch.delenv("GATEWAY_CLIENT_API_KEY", raising=False)
    monkeypatch.delenv("ACCOUNTS_ENABLED", raising=False)
    store = _mk_store(tmp_path)
    audit = _noop_audit()
    try:
        app = create_app(
            _reg_one(), default_model="qwen3.8",
            transport=httpx.MockTransport(_upstream_ok_200),
            audit_log=audit,
            accounts_enabled=True,
            accounts_store=store,
        )
        resp = _post_headers(app, "/v1/chat/completions",
                             json_body={"model": "qwen3.8", "messages": []},
                             headers={"Authorization": "Bearer does-not-exist"})
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["type"] == "invalid_api_key"
        # 401 也应该落审计
        assert audit.entries, "被拒请求必须落审计"
        last = audit.entries[-1]
        assert last.get("user_id") is None
        assert last.get("key_id") is None
    finally:
        store.close()


def test_enabled_disabled_key_401(tmp_path, monkeypatch):
    """Key 被禁用 → 401 `invalid_api_key`（不走 403，语义同"未签发"）。"""
    monkeypatch.delenv("GATEWAY_CLIENT_API_KEY", raising=False)
    store = _mk_store(tmp_path)
    audit = _noop_audit()
    try:
        uid, cred = _mk_user_key(store, username="alice")
        key = store.get_key_by_hash(hash_api_key(cred))
        store.set_key_status(key["id"], "disabled")
        app = create_app(
            _reg_one(), default_model="qwen3.8",
            transport=httpx.MockTransport(_upstream_ok_200),
            audit_log=audit,
            accounts_enabled=True,
            accounts_store=store,
        )
        resp = _post_headers(app, "/v1/chat/completions",
                             json_body={"model": "qwen3.8", "messages": []},
                             headers={"Authorization": f"Bearer {cred}"})
        assert resp.status_code == 401
        assert resp.json()["error"]["type"] == "invalid_api_key"
    finally:
        store.close()


def test_enabled_valid_key_passthrough(tmp_path, monkeypatch):
    """有效 Key → 200 透传，审计条目带 user_id / key_id；usage_rows 落库。"""
    monkeypatch.delenv("GATEWAY_CLIENT_API_KEY", raising=False)
    store = _mk_store(tmp_path)
    audit = _noop_audit()
    try:
        uid, cred = _mk_user_key(store, username="alice")
        app = create_app(
            _reg_one(), default_model="qwen3.8",
            transport=httpx.MockTransport(_upstream_ok_200),
            audit_log=audit,
            accounts_enabled=True,
            accounts_store=store,
        )
        resp = _post_headers(app, "/v1/chat/completions",
                             json_body={"model": "qwen3.8",
                                       "messages": [{"role": "user", "content": "hi"}]},
                             headers={"Authorization": f"Bearer {cred}"})
        assert resp.status_code == 200
        # 审计条目应含 user_id / key_id
        assert audit.entries, "成功请求也应落审计"
        last = audit.entries[-1]
        assert last.get("user_id") == uid
        assert last.get("key_id") is not None
        # httpx.ASGITransport 不触发 lifespan → 手动 drain；等价于 stop() 的末次 sweep
        _drain_accountant(app)
        # settle 应该已 drain（app 生命周期结束后 stop()）：至少 usage 表有一行
        with store._lock:  # 直接专家读取：测试专用
            rows = store._db().execute(
                "SELECT user_id, prompt_tokens, completion_tokens FROM usage_records"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == uid
        assert rows[0][1] == 10
        assert rows[0][2] == 5
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 四道限流
# ---------------------------------------------------------------------------

def test_enabled_budget_exceeded_429(tmp_path, monkeypatch):
    """预算耗尽 → 429 `budget_exceeded`。"""
    monkeypatch.delenv("GATEWAY_CLIENT_API_KEY", raising=False)
    store = _mk_store(tmp_path)
    audit = _noop_audit()
    try:
        uid, cred = _mk_user_key(
            store, username="alice",
            policy={"token_budget": 100, "budget_period": "day"},
        )
        # 直接推 budget_consumed 到满额
        store.increment_budget_consumed(uid, 100, now=_NOW)
        app = create_app(
            _reg_one(), default_model="qwen3.8",
            transport=httpx.MockTransport(_upstream_ok_200),
            audit_log=audit,
            accounts_enabled=True,
            accounts_store=store,
        )
        resp = _post_headers(app, "/v1/chat/completions",
                             json_body={"model": "qwen3.8", "messages": []},
                             headers={"Authorization": f"Bearer {cred}"})
        assert resp.status_code == 429
        body = resp.json()
        assert body["error"]["type"] == "budget_exceeded"
        # 预算耗尽不推进并发（acquire 没走过）
        guard = app.state.limit_guard
        with guard._lock:
            assert guard._concurrency.get(uid) in (None, 0)
    finally:
        store.close()


def test_enabled_concurrency_exceeded_429(tmp_path, monkeypatch):
    """并发满 → 429 `concurrency_exceeded` + Retry-After 头。"""
    monkeypatch.delenv("GATEWAY_CLIENT_API_KEY", raising=False)
    store = _mk_store(tmp_path)
    audit = _noop_audit()
    try:
        uid, cred = _mk_user_key(
            store, username="alice",
            policy={"concurrency_limit": 1},
        )
        app = create_app(
            _reg_one(), default_model="qwen3.8",
            transport=httpx.MockTransport(_upstream_ok_200),
            audit_log=audit,
            accounts_enabled=True,
            accounts_store=store,
        )
        # 手动占一个并发槽模拟另一请求还在跑
        app.state.limit_guard.acquire(uid, 1)
        resp = _post_headers(app, "/v1/chat/completions",
                             json_body={"model": "qwen3.8", "messages": []},
                             headers={"Authorization": f"Bearer {cred}"})
        assert resp.status_code == 429
        body = resp.json()
        assert body["error"]["type"] == "concurrency_exceeded"
        assert "retry-after" in {k.lower() for k in resp.headers}
    finally:
        store.close()


def test_enabled_rpm_exceeded_429(tmp_path, monkeypatch):
    """RPM 满 → 429 `rate_limit_exceeded`（第二次同窗内拒绝；窗口锚首个请求）。"""
    monkeypatch.delenv("GATEWAY_CLIENT_API_KEY", raising=False)
    store = _mk_store(tmp_path)
    audit = _noop_audit()
    try:
        uid, cred = _mk_user_key(
            store, username="alice",
            policy={"rpm_limit": 1},
        )
        app = create_app(
            _reg_one(), default_model="qwen3.8",
            transport=httpx.MockTransport(_upstream_ok_200),
            audit_log=audit,
            accounts_enabled=True,
            accounts_store=store,
        )
        r1 = _post_headers(app, "/v1/chat/completions",
                           json_body={"model": "qwen3.8", "messages": []},
                           headers={"Authorization": f"Bearer {cred}"})
        assert r1.status_code == 200
        r2 = _post_headers(app, "/v1/chat/completions",
                           json_body={"model": "qwen3.8", "messages": []},
                           headers={"Authorization": f"Bearer {cred}"})
        assert r2.status_code == 429
        assert r2.json()["error"]["type"] == "rate_limit_exceeded"
    finally:
        store.close()


def test_enabled_tpm_exceeded_429(tmp_path, monkeypatch):
    """TPM 预占溢出 → 429 `rate_limit_exceeded`。
    est_total = estimate_prompt_tokens(messages) * 4（对数级放大 upper bound）+ max_tokens / 4；
    最小化配置：sender messages 够长使得任何私有 est 都 > tpm_limit。"""
    monkeypatch.delenv("GATEWAY_CLIENT_API_KEY", raising=False)
    store = _mk_store(tmp_path)
    audit = _noop_audit()
    try:
        uid, cred = _mk_user_key(
            store, username="alice",
            policy={"tpm_limit": 10},
        )
        app = create_app(
            _reg_one(), default_model="qwen3.8",
            transport=httpx.MockTransport(_upstream_ok_200),
            audit_log=audit,
            accounts_enabled=True,
            accounts_store=store,
        )
        # 大约 est = (prompt_chars // 4) * 4 = prompt_chars；构造 100 字符消息
        # est ≈ 100 > tpm_limit 10 → 预占就溢出
        long_text = "x" * 400  # est ≈ 400 > 10
        resp = _post_headers(app, "/v1/chat/completions",
                             json_body={"model": "qwen3.8",
                                       "messages": [{"role": "user", "content": long_text}]},
                             headers={"Authorization": f"Bearer {cred}"})
        assert resp.status_code == 429
        assert resp.json()["error"]["type"] == "rate_limit_exceeded"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# SSE 流：并发槽在流结束后释放
# ---------------------------------------------------------------------------

def test_enabled_streaming_releases_concurrency(tmp_path, monkeypatch):
    """并发=1 时：一次 SSE 请求跑完，下一个请求应能拿到槽（不 429）——
    证明 release 在流结束 finally 里执行。"""
    monkeypatch.delenv("GATEWAY_CLIENT_API_KEY", raising=False)
    store = _mk_store(tmp_path)
    audit = _noop_audit()
    try:
        uid, cred = _mk_user_key(
            store, username="alice",
            policy={"concurrency_limit": 1},
        )
        app = create_app(
            _reg_one(), default_model="qwen3.8",
            transport=httpx.MockTransport(lambda r: _upstream_sse_raw()),
            audit_log=audit,
            accounts_enabled=True,
            accounts_store=store,
        )
        async def one():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"Authorization": f"Bearer {cred}"},
            ) as client:
                async with client.stream("POST", "/v1/chat/completions",
                                        json={"model": "qwen3.8",
                                              "messages": [{"role": "user", "content": "hi"}],
                                              "stream": True}) as resp:
                    assert resp.status_code == 200
                    _ = b"".join([c async for c in resp.aiter_bytes()])
        _run(one())
        # 流结束后再打：不应该是 429（release 已跑）
        r2 = _post_headers(app, "/v1/chat/completions",
                           json_body={"model": "qwen3.8", "messages": []},
                           headers={"Authorization": f"Bearer {cred}"})
        assert r2.status_code == 200
    finally:
        store.close()


def _upstream_sse_raw() -> httpx.Response:
    chunks = [
        b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"he"}}]}\n\n',
        b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"llo"}}]}\n\n',
        b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n',
        b"data: [DONE]\n\n",
    ]
    buf = b"".join(chunks)
    return httpx.Response(200,
                          headers={"content-type": "text/event-stream"},
                          content=buf)


# ---------------------------------------------------------------------------
# 会话归属 + settle 落库
# ---------------------------------------------------------------------------

def test_enabled_session_id_from_header(tmp_path, monkeypatch):
    """两请求同一 X-Session-Id：只建一次 session（sessions 表里只有一行 uid/session_key）。"""
    monkeypatch.delenv("GATEWAY_CLIENT_API_KEY", raising=False)
    store = _mk_store(tmp_path)
    audit = _noop_audit()
    try:
        uid, cred = _mk_user_key(store, username="alice")
        app = create_app(
            _reg_one(), default_model="qwen3.8",
            transport=httpx.MockTransport(_upstream_ok_200),
            audit_log=audit,
            accounts_enabled=True,
            accounts_store=store,
        )
        hdr = {"Authorization": f"Bearer {cred}", "X-Session-Id": "sess-42"}
        body = {"model": "qwen3.8", "messages": [{"role": "user", "content": "hi"}]}
        r1 = _post_headers(app, "/v1/chat/completions", json_body=body, headers=hdr)
        assert r1.status_code == 200
        r2 = _post_headers(app, "/v1/chat/completions", json_body=body, headers=hdr)
        assert r2.status_code == 200
        _drain_accountant(app)
        with store._lock:
            sessions = store._db().execute(
                "SELECT id, session_key, user_id, message_count FROM sessions"
            ).fetchall()
        assert len(sessions) == 1
        row = sessions[0]
        assert row[1] == "sess-42"
        assert row[2] == uid
        # 两次请求每次 user+assistant 各 1 → 4 条
        assert row[3] == 4
    finally:
        store.close()


def test_enabled_settle_increments_budget_consumed(tmp_path, monkeypatch):
    """settle 应把 p+c 累加进 users.budget_consumed，两次同源请求累加两次。"""
    monkeypatch.delenv("GATEWAY_CLIENT_API_KEY", raising=False)
    store = _mk_store(tmp_path)
    audit = _noop_audit()
    try:
        uid, cred = _mk_user_key(store, username="alice")
        app = create_app(
            _reg_one(), default_model="qwen3.8",
            transport=httpx.MockTransport(_upstream_ok_200),
            audit_log=audit,
            accounts_enabled=True,
            accounts_store=store,
        )
        hdr = {"Authorization": f"Bearer {cred}"}
        body = {"model": "qwen3.8", "messages": [{"role": "user", "content": "hi"}]}
        r1 = _post_headers(app, "/v1/chat/completions", json_body=body, headers=hdr)
        assert r1.status_code == 200
        r2 = _post_headers(app, "/v1/chat/completions", json_body=body, headers=hdr)
        assert r2.status_code == 200
        _drain_accountant(app)
        row = store.get_user_by_id(uid)
        # 两次请求各 p=10 c=5 → 30
        assert row["budget_consumed"] == 30
    finally:
        store.close()


# ---------------------------------------------------------------------------
# _build_audit_entry 可扩展
# ---------------------------------------------------------------------------

def test_build_audit_entry_accepts_user_key_ids(tmp_path):
    """扩展 `_build_audit_entry` 带默认值 user_id / key_id，两者均可省略也
    可显式传入；返回 dict 里两个字段都存在（border 兼容旧调用/旧 JSONL）。"""
    from modelctl.core.gateway import _build_audit_entry
    e_default = _build_audit_entry(
        model_name="m", profile_name="m", profile_engine="vllm", path="chat",
        stream=False, native_metrics=None, usage=None, gateway_metrics=None,
        status_code=200, error=None, finish_reason=None, input_char_len=10,
    )
    assert "user_id" in e_default
    assert "key_id" in e_default
    assert e_default["user_id"] is None
    assert e_default["key_id"] is None

    e_set = _build_audit_entry(
        model_name="m", profile_name="m", profile_engine="vllm", path="chat",
        stream=False, native_metrics=None, usage=None, gateway_metrics=None,
        status_code=200, error=None, finish_reason=None, input_char_len=10,
        user_id=42, key_id=7,
    )
    assert e_set["user_id"] == 42
    assert e_set["key_id"] == 7


# ---------------------------------------------------------------------------
# accounts_gate 直接调用（可选回归：0 依赖模块函数名）
# ---------------------------------------------------------------------------

def test_accounts_gate_disabled_uses_legacy_flow(tmp_path, monkeypatch):
    """`accounts_gate` 在 accounts_enabled=False 时不挂 request.state.identity，
    走 legacy verify_client。此用例简单点：仅确认 request.state 有该属性通道。"""
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", "mgt")
    monkeypatch.delenv("ACCOUNTS_ENABLED", raising=False)
    audit = _noop_audit()
    app = create_app(
        _reg_one(), default_model="qwen3.8",
        transport=httpx.MockTransport(_upstream_ok_200),
        audit_log=audit,
        accounts_enabled=False,
    )
    # 走一次合法请求 → 不应把 identity 挂 request.state
    resp = _post_headers(app, "/v1/chat/completions",
                         json_body={"model": "qwen3.8", "messages": []},
                         headers={"Authorization": "Bearer mgt"})
    assert resp.status_code == 200
