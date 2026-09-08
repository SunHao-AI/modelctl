#!/usr/bin/env python3
# ===============================================================================
# @File   : tests/test_accounts_store.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/8 16:25
# @Desc   : 账号库 SQLite DAO 五表读写测试
# ===============================================================================

"""`core/accounts/store.py` — 五表 DAO 行为约束。

关注点（按 spec §3 / §6 的可失败点组织）：
- schema 幂等：重复 `init_db` 不报错；`_ensure_columns` 能给旧库补列且不动既有值。
- 唯一约束：`username` / `key_hash` 撞库必须抛 IntegrityError（否则同 Key 命中两条账号）。
- 零 `SELECT *`：列清单常量与 schema 逐列一致（护栏测试反射比对）。
- 会话归属：`X-Session-Id` 命中同用户会话；**跨用户同 session_id 不得劫持**；
  空闲窗口内聚合、超窗新建。
- 时间全部由调用方注入 `now`，测试绝不 sleep。
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture()
def store(tmp_path):
    from modelctl.core.accounts.store import AccountsStore

    s = AccountsStore(tmp_path / "accounts.db")
    s.init_db()
    yield s
    s.close()


def _mk_user(store, username="alice", now=1.0, **kw):
    kw.setdefault("password_hash", "bcrypt$fake")
    return store.create_user(username=username, now=now, **kw)


def _mk_key(store, user_id, name="default", key_hash="hash-a", key_prefix="sk-mctl-***aaaa",
            now=1.0, **kw):
    return store.create_key(user_id=user_id, key_hash=key_hash, key_prefix=key_prefix,
                            name=name, now=now, **kw)


# ---------- schema ----------


def test_init_db_creates_five_tables_and_is_idempotent(tmp_path):
    from modelctl.core.accounts.store import AccountsStore

    s = AccountsStore(tmp_path / "a.db")
    s.init_db()
    s.init_db()  # 第二次不得抛
    names = {r["name"] for r in s._db().execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"users", "api_keys", "usage_records", "sessions", "messages"} <= names
    s.close()


def test_init_db_creates_db_file_with_parent_dir(tmp_path):
    from modelctl.core.accounts.store import AccountsStore

    target = tmp_path / "nested" / "deep" / "accounts.db"
    s = AccountsStore(target)
    s.init_db()
    assert target.is_file()
    s.close()


def test_ensure_columns_adds_missing_without_touching_data(tmp_path):
    """旧库缺列 → 补齐且既有行值不丢（迁移顺序：先加列带默认 → 回填 → 后加约束）。"""
    from modelctl.core.accounts.store import AccountsStore

    target = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(target))
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT)")
    conn.execute("INSERT INTO users(id, username) VALUES(1, 'old')")
    conn.commit()
    conn.close()

    s = AccountsStore(target)
    s.init_db()
    row = s.get_user_by_id(1)
    assert row["username"] == "old"
    # 补列走 schema 声明的默认值：NOT NULL DEFAULT 'active' 生效，可空列为 NULL
    assert row["status"] == "active" and row["display_name"] is None
    s.init_db()  # 再跑一次仍幂等
    s.close()


def test_explicit_column_lists_cover_full_schema(store):
    """零 SELECT * 护栏：每张表的列清单常量必须与 schema 完全一致。

    漏列会让读路径静默丢字段（面板显示空值而非报错），故用反射兜底。
    """
    from modelctl.core.accounts import store as m

    mapping = {
        "users": m._USER_COLS,
        "api_keys": m._KEY_COLS,
        "usage_records": m._USAGE_COLS,
        "sessions": m._SESSION_COLS,
        "messages": m._MESSAGE_COLS,
    }
    for table, cols in mapping.items():
        have = [r["name"] for r in store._db().execute(f"PRAGMA table_info({table})").fetchall()]
        assert tuple(have) == tuple(cols), f"{table} 列清单与 schema 不一致: {have} != {list(cols)}"


def test_source_has_no_select_star():
    """源码级护栏：store.py 内不得出现 `SELECT *`（CLAUDE.md 数据库规范）。"""
    import inspect

    from modelctl.core.accounts import store as m

    assert "SELECT *" not in inspect.getsource(m)


# ---------- users ----------


def test_create_user_returns_id_and_roundtrip(store):
    uid = _mk_user(store, "bob", display_name="Bob", concurrency_limit=3, rpm_limit=60,
                   tpm_limit=100000, token_budget=1000000, budget_period="day",
                   retention_days=30)
    row = store.get_user_by_id(uid)
    assert row["username"] == "bob" and row["display_name"] == "Bob"
    assert row["concurrency_limit"] == 3 and row["rpm_limit"] == 60
    assert row["token_budget"] == 1000000 and row["budget_period"] == "day"
    assert row["status"] == "active" and row["is_admin"] is False
    assert row["budget_consumed"] == 0 and row["budget_reset_at"] is None
    assert store.get_user_by_username("bob")["id"] == uid
    assert store.get_user_by_username("BOB") is None  # 大小写敏感（避免近似名抢占歧义）
    assert store.get_user_by_id(9999) is None


def test_username_unique_rejects_duplicate(store):
    _mk_user(store, "dup")
    with pytest.raises(sqlite3.IntegrityError):
        _mk_user(store, "dup")


def test_create_user_defaults_limit_none_means_unlimited(store):
    uid = _mk_user(store, "lim")
    row = store.get_user_by_id(uid)
    # store 只负责存；"不限制"语义由 limits 解释，这里只保证 None 不被篡改成 0
    assert row["concurrency_limit"] is None and row["token_budget"] is None


def test_list_users_sorted_by_id(store):
    a = _mk_user(store, "a")
    b = _mk_user(store, "b")
    assert [u["id"] for u in store.list_users()] == [a, b]


def test_update_user_limits_whitelist_ignores_unknown_fields(store):
    uid = _mk_user(store, "carl", rpm_limit=10)
    out = store.update_user_limits(uid, now=2.0, rpm_limit=99, is_admin=True,
                                   password_hash="hacked", username="evil")
    assert out["rpm_limit"] == 99 and out["is_admin"] is True
    assert out["password_hash"] == "bcrypt$fake" and out["username"] == "carl"
    assert out["updated_at"] == 2.0
    assert store.update_user_limits(4242, now=3.0, rpm_limit=1) is None


def test_set_user_status_validates_and_persists(store):
    uid = _mk_user(store, "dave")
    store.set_user_status(uid, "disabled", now=5.0)
    assert store.get_user_by_id(uid)["status"] == "disabled"
    with pytest.raises(ValueError):
        store.set_user_status(uid, "deleted", now=6.0)


def test_set_user_password_hash(store):
    uid = _mk_user(store, "erin")
    store.set_user_password_hash(uid, "bcrypt$new", now=6.0)
    assert store.get_user_by_username("erin")["password_hash"] == "bcrypt$new"


def test_budget_increment_and_reset(store):
    uid = _mk_user(store, "frank", token_budget=1000)
    store.increment_budget_consumed(uid, 300, now=1.0)
    store.increment_budget_consumed(uid, 250, now=2.0)
    assert store.get_user_by_id(uid)["budget_consumed"] == 550
    store.reset_budget(uid, next_reset_at=99.0, now=3.0)
    row = store.get_user_by_id(uid)
    assert row["budget_consumed"] == 0 and row["budget_reset_at"] == 99.0


# ---------- api_keys ----------


def test_create_key_roundtrip_and_lookup_by_hash(store):
    uid = _mk_user(store, "grace")
    kid = _mk_key(store, uid, name="prod", key_hash="h1", key_prefix="sk-mctl-***h1")
    row = store.get_key_by_hash("h1")
    assert row["id"] == kid and row["user_id"] == uid
    assert row["name"] == "prod" and row["status"] == "active"
    assert row["expires_at"] is None and row["last_used_at"] is None
    assert store.get_key_by_id(kid)["key_prefix"] == "sk-mctl-***h1"
    assert store.get_key_by_hash("nope") is None


def test_key_hash_unique(store):
    uid = _mk_user(store, "heidi")
    _mk_key(store, uid, key_hash="same")
    with pytest.raises(sqlite3.IntegrityError):
        _mk_key(store, uid, key_hash="same", key_prefix="other")


def test_list_keys_for_user_scoped(store):
    u1, u2 = _mk_user(store, "ivan"), _mk_user(store, "judy")
    k1 = _mk_key(store, u1, key_hash="k1")
    k2 = _mk_key(store, u1, key_hash="k2", name="ci")
    _mk_key(store, u2, key_hash="k3")
    assert [k["id"] for k in store.list_keys_for_user(u1)] == [k1, k2]


def test_key_status_whitelist_and_expiry(store):
    uid = _mk_user(store, "kate")
    kid = _mk_key(store, uid, key_hash="k9", expires_at=123.0)
    assert store.get_key_by_id(kid)["expires_at"] == 123.0
    store.set_key_status(kid, "revoked")
    assert store.get_key_by_id(kid)["status"] == "revoked"
    with pytest.raises(ValueError):
        store.set_key_status(kid, "expired")


def test_touch_key_last_used(store):
    uid = _mk_user(store, "leo")
    kid = _mk_key(store, uid, key_hash="kt")
    store.touch_key_last_used(kid, now=77.0)
    assert store.get_key_by_id(kid)["last_used_at"] == 77.0


# ---------- usage_records ----------


def test_insert_and_sum_usage(store):
    uid = _mk_user(store, "mia")
    kid = _mk_key(store, uid, key_hash="ku")
    store.insert_usage(user_id=uid, key_id=kid, request_id="r1", model="qwen",
                       prompt_tokens=10, completion_tokens=5, client_ip="1.2.3.4",
                       ttft_ms=40, latency_ms=200, status="ok", now=100.0)
    store.insert_usage(user_id=uid, key_id=kid, request_id="r2", model="qwen",
                       prompt_tokens=20, completion_tokens=8, client_ip="1.2.3.4",
                       ttft_ms=None, latency_ms=None, status="error", now=200.0)
    total = store.sum_usage_for_user(uid)
    assert total == {"requests": 2, "prompt_tokens": 30, "completion_tokens": 13,
                     "total_tokens": 43}
    recent = store.sum_usage_for_user(uid, since=150.0)
    assert recent["requests"] == 1 and recent["total_tokens"] == 28
    rows = store.list_usage_for_user(uid)
    assert [r["request_id"] for r in rows] == ["r2", "r1"]  # 时间倒序
    assert rows[1]["total_tokens"] == 15 and rows[1]["ttft_ms"] == 40


def test_usage_scoped_to_user(store):
    u1, u2 = _mk_user(store, "nina"), _mk_user(store, "oscar")
    k1, k2 = _mk_key(store, u1, key_hash="n"), _mk_key(store, u2, key_hash="o")
    store.insert_usage(user_id=u1, key_id=k1, request_id="x", model="m", prompt_tokens=1,
                       completion_tokens=1, client_ip="", ttft_ms=None, latency_ms=None,
                       status="ok", now=1.0)
    store.insert_usage(user_id=u2, key_id=k2, request_id="y", model="m", prompt_tokens=9,
                       completion_tokens=9, client_ip="", ttft_ms=None, latency_ms=None,
                       status="ok", now=1.0)
    assert store.sum_usage_for_user(u1)["total_tokens"] == 2
    assert [r["request_id"] for r in store.list_usage_for_user(u2)] == ["y"]


def test_sum_usage_empty_user_returns_zeroes(store):
    uid = _mk_user(store, "pam")
    assert store.sum_usage_for_user(uid) == {"requests": 0, "prompt_tokens": 0,
                                             "completion_tokens": 0, "total_tokens": 0}


# ---------- sessions / messages ----------


def test_session_created_by_external_id_then_reused(store):
    uid, other = _mk_user(store, "quinn"), _mk_user(store, "rita")
    kid = _mk_key(store, uid, key_hash="ks")
    s1 = store.get_or_create_session(user_id=uid, key_id=kid, model="qwen",
                                     session_id="cli-sess-1", title="你好世界", now=100.0)
    s2 = store.get_or_create_session(user_id=uid, key_id=kid, model="qwen",
                                     session_id="cli-sess-1", title="你好世界", now=160.0)
    assert s1["id"] == s2["id"]
    assert s1["title"] == "你好世界" and s1["session_key"] == "cli-sess-1"
    assert s2["last_active_at"] == 160.0
    # 同一 session_id 属于别人：必须另起会话，绝不写入他人会话（防劫持）
    s3 = store.get_or_create_session(user_id=other, key_id=99, model="qwen",
                                     session_id="cli-sess-1", title="别人的", now=170.0)
    assert s3["id"] != s1["id"] and s3["user_id"] == other
    assert store.get_session(s1["id"], user_id=other) is None  # 归属校验


def test_session_idle_window_aggregation(store):
    uid = _mk_user(store, "sam")
    kid = _mk_key(store, uid, key_hash="ki")
    a = store.get_or_create_session(user_id=uid, key_id=kid, model="qwen", now=1000.0)
    b = store.get_or_create_session(user_id=uid, key_id=kid, model="qwen", now=2799.0)
    assert a["id"] == b["id"]  # 窗口内（默认 30min）归同一会话，活跃时刻刷新到 2799
    # 空闲窗口以"最后一次活跃"起算（spec §6.1）：2799+1801 才超窗，而非距创建 1801s
    c = store.get_or_create_session(user_id=uid, key_id=kid, model="qwen", now=4600.0)
    assert c["id"] != a["id"]  # 超窗新建
    d = store.get_or_create_session(user_id=uid, key_id=kid, model="deepseek", now=4600.0)
    assert d["id"] not in (a["id"], c["id"])  # 换 model 也是新会话
    auto = store.get_session(a["id"], user_id=uid)
    assert auto["session_key"].startswith("s-")  # 无 X-Session-Id 时内部生成


def test_title_truncated_from_first_user_message(store):
    uid = _mk_user(store, "tina")
    kid = _mk_key(store, uid, key_hash="kt2")
    s = store.get_or_create_session(user_id=uid, key_id=kid, model="m",
                                    title="开" * 500, now=1.0)
    assert len(s["title"]) <= 120


def test_messages_and_message_count_and_export(store):
    uid = _mk_user(store, "uma")
    kid = _mk_key(store, uid, key_hash="km")
    s = store.get_or_create_session(user_id=uid, key_id=kid, model="qwen",
                                    session_id="s-export", title="导出会话", now=10.0)
    store.add_message(session_id=s["id"], role="user", content="今天天气", now=11.0,
                      prompt_tokens=6, completion_tokens=0)
    store.bump_session(s["id"], now=11.0)
    store.add_message(session_id=s["id"], role="assistant", content="晴", now=12.0,
                      prompt_tokens=0, completion_tokens=3)
    store.bump_session(s["id"], now=12.0)
    out = store.export_session(s["id"], user_id=uid)
    assert out["session"]["message_count"] == 2 and out["session"]["title"] == "导出会话"
    assert [m["role"] for m in out["messages"]] == ["user", "assistant"]
    assert out["messages"][0]["content"] == "今天天气"
    assert out["messages"][1]["completion_tokens"] == 3
    assert store.export_session(s["id"], user_id=999) is None


def test_list_sessions_filters_by_query(store):
    uid = _mk_user(store, "vic")
    kid = _mk_key(store, uid, key_hash="kq")
    s1 = store.get_or_create_session(user_id=uid, key_id=kid, model="m", session_id="a1",
                                     title="Python 教程", now=10.0)
    s2 = store.get_or_create_session(user_id=uid, key_id=kid, model="m", session_id="a2",
                                     title="Go 教程", now=20.0)
    store.add_message(session_id=s2["id"], role="user", content="聊聊 rust 语法", now=21.0,
                      prompt_tokens=1, completion_tokens=0)
    assert [s["id"] for s in store.list_sessions(uid)] == [s2["id"], s1["id"]]  # 活跃倒序
    assert [s["id"] for s in store.list_sessions(uid, q="Python")] == [s1["id"]]
    assert [s["id"] for s in store.list_sessions(uid, q="rust")] == [s2["id"]]  # 命中消息内容
    assert store.list_sessions(uid, q="不存在") == []


def test_search_messages_returns_session_context(store):
    uid = _mk_user(store, "wendy")
    kid = _mk_key(store, uid, key_hash="kq2")
    s = store.get_or_create_session(user_id=uid, key_id=kid, model="qwen", session_id="q1",
                                    title="标题", now=10.0)
    store.add_message(session_id=s["id"], role="assistant", content="KV cache 命中", now=11.0,
                      prompt_tokens=1, completion_tokens=2)
    hits = store.search_messages(uid, "cache")
    assert len(hits) == 1
    assert hits[0]["session_id"] == s["id"] and hits[0]["title"] == "标题"
    assert hits[0]["model"] == "qwen"
    assert store.search_messages(uid, "cache 不存在") == []


def test_delete_session_cascades_messages(store):
    uid = _mk_user(store, "xue")
    kid = _mk_key(store, uid, key_hash="kd")
    s = store.get_or_create_session(user_id=uid, key_id=kid, model="m", session_id="d1", now=1.0)
    store.add_message(session_id=s["id"], role="user", content="hi", now=2.0,
                      prompt_tokens=1, completion_tokens=0)
    assert store.delete_session(s["id"], user_id=uid) is True
    assert store.get_session(s["id"], user_id=uid) is None
    left = store._db().execute("SELECT COUNT(*) AS c FROM messages WHERE session_id=?",
                               (s["id"],)).fetchone()["c"]
    assert left == 0
    assert store.delete_session(s["id"], user_id=uid) is False  # 幂等


def test_delete_session_rejects_other_users_session(store):
    u1, u2 = _mk_user(store, "yang"), _mk_user(store, "zoe")
    kid = _mk_key(store, u1, key_hash="kd2")
    s = store.get_or_create_session(user_id=u1, key_id=kid, model="m", session_id="d2", now=1.0)
    assert store.delete_session(s["id"], user_id=u2) is False
    assert store.get_session(s["id"], user_id=u1) is not None


# ---------- 账号级联删除 ----------


def test_delete_user_cascade(store):
    uid = _mk_user(store, "amy")
    kid = _mk_key(store, uid, key_hash="kc")
    s = store.get_or_create_session(user_id=uid, key_id=kid, model="m", session_id="c1", now=1.0)
    store.add_message(session_id=s["id"], role="user", content="hi", now=2.0,
                      prompt_tokens=1, completion_tokens=0)
    store.insert_usage(user_id=uid, key_id=kid, request_id="r", model="m", prompt_tokens=1,
                       completion_tokens=1, client_ip="", ttft_ms=None, latency_ms=None,
                       status="ok", now=1.0)
    assert store.delete_user(uid)["username"] == "amy"
    assert store.get_user_by_id(uid) is None
    assert store.get_key_by_hash("kc") is None
    assert store.get_session(s["id"], user_id=uid) is None
    assert store.sum_usage_for_user(uid)["requests"] == 0
    assert store.delete_user(4242) is None
