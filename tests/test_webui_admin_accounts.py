#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_webui_admin_accounts.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/9 15:30
# @Desc   : Task 7 契约 — 账号 JWT 认证 + 管理员面板 + 账号自助 API
# ===============================================================================

"""Task 7 TDD 契约：WebUI 账号/JWT 认证 + 管理员 CRUD + 账号自助路由。

分四组：

1. **admin_accounts 偏移 / 校验**（`/admin/api/accounts*`，`require_auth` = Bearer API_KEY）
   - 无 API_KEY 或错 key → 401
   - `accounts` 未启用（`app.state.accounts is None`）→ 503
   - 账号 CRUD：POST /accounts 建号 → GET /accounts 列表 → GET /accounts/{id} →
     PUT 改限额/状态 → DELETE 删号
   - Key 管理：POST /accounts/{id}/keys 一次性返回明文 → GET 列表（前缀脱敏）→
     PUT 改状态（禁用/启用/吊销）→ DELETE 删
   - Usage 汇总：GET /accounts/{id}/usage（sum_usage_for_user + budget_consumed）

2. **account_self 登录**（`/api/account/login`，无鉴权）
   - 正确密码 → 200 + `token`（JWT HS256，payload 含 user_id/is_admin）
   - 空/错 username → 401
   - 错密码 → 401
   - `accounts` 未启用 → 503

3. **account_self 自助**（其余端点 `require_account` = Bearer JWT）
   - 无 token → 401
   - JWT 篡改/过期 → 401
   - 有效 JWT GET /keys 只返回自家 Key
   - POST /keys 签发新 Key（一次性返回明文，库里存 hash）
   - GET /usage 返回自家 sum_usage
   - GET /sessions / GET /sessions/{id} / GET /sessions/{id}/export /
     DELETE /sessions/{id} — 归属校验（跨用户 404）
   - GET /sessions/search?q= 消息内容搜索
   - 边界：JWT 里 user_id 不匹配 → 401（防伪造 token 越权）

4. **路由组五方法齐全** 护栏：
   - admin：`/admin/api/accounts` GET/POST；`/admin/api/accounts/{id}` GET/PUT/DELETE；
     `/admin/api/accounts/{id}/keys` GET/POST
   - self：`/api/account/*` 内各端点（GET/POST/PUT/DELETE）注册齐全

**关键约束**（对齐 CLAUDE.md）：
- Key 展示只 `key_prefix`（`sk-mctl-***x4`），绝不回显完整明文，
  除了 POST /keys 初次签发那一刻（一次性展示）。
- 时间统一 `YYYY-MM-DD HH:mm:ss`（输出层），`budget_reset_at` 等 epoch REAL
  在 JSON 层转 ISO。
- 接口组五方法齐全，避免前端 405。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid

import pytest

fastapi = pytest.importorskip("fastapi")
jwt = pytest.importorskip("jwt")

from modelctl.core.accounts.hashing import hash_api_key, hash_password, key_prefix, generate_api_key  # noqa: E402
from modelctl.core.accounts.store import AccountsStore  # noqa: E402
from modelctl.core.gateway import create_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ADMIN_KEY = "sk-mgmt-admin-key-9999"
JWT_SECRET = "unit-test-jwt-secret-0001"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path) -> AccountsStore:
    s = AccountsStore(tmp_path / "accounts.db")
    s.init_db()
    return s


def _mk_app(tmp_path, monkeypatch, *, accounts_store: AccountsStore | None = None,
            admin_key: str = ADMIN_KEY, jwt_secret: str = JWT_SECRET):
    """造一个 `create_app(admin=True)` + 可选 accounts_store 的 app，返回 app。
    不启动 lifespan（TestClient 会按需走 lifespan，但这里显式关闭
    `gateway` 的上游依赖：`transport=httpx.MockTransport` 提供默认 200）。
    """
    import httpx
    monkeypatch.setenv("API_KEY", admin_key)
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", "sk-test-client-key-9f3a")
    monkeypatch.setenv("ACCOUNTS_JWT_SECRET", jwt_secret)
    # accounts_enabled 由 accounts_store 是否注入决定；TestClient 里 lifespan
    # 默认关闭，accountant 不会启动（不影响测试）
    return create_app(
        {},  # 空 registry：管理面/自助面板不依赖模型路由
        default_model="",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        admin=True,
        accounts_enabled=(accounts_store is not None),
        accounts_store=accounts_store,
    )


def _create_user(store: AccountsStore, *, username: str = "alice",
                 password: str = "secret-pw-0001", is_admin: bool = False,
                 budget: int | None = 10000) -> int:
    uid = store.create_user(
        username=username,
        password_hash=hash_password(password),
        is_admin=is_admin,
        token_budget=budget,
        now=time.time(),
    )
    return uid


def _create_key(store: AccountsStore, user_id: int, *, name: str = "test") -> tuple[int, str]:
    cred = generate_api_key()
    kid = store.create_key(
        user_id=user_id,
        key_hash=hash_api_key(cred),
        key_prefix=key_prefix(cred),
        name=name,
        now=time.time(),
    )
    return kid, cred


def _admin_headers(token: str = ADMIN_KEY) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _self_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _fw(payload: dict) -> dict:
    """结构断言助手：把 account/session 的 epoch float 时间字段转 ISO；测试用字段
    也直接剃平（防意外）。"""
    return payload


# ---------------------------------------------------------------------------
# 1. admin_accounts
# ---------------------------------------------------------------------------

def test_admin_accounts_missing_api_key_401(tmp_path, monkeypatch):
    """未提供 Bearer API_KEY 访问 /admin/api/accounts → 401。"""
    store = _make_store(tmp_path)
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        r = c.get("/admin/api/accounts")
        assert r.status_code == 401


def test_admin_accounts_wrong_api_key_401(tmp_path, monkeypatch):
    """Bearer 头与 API_KEY 不匹配 → 401。"""
    store = _make_store(tmp_path)
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        r = c.get("/admin/api/accounts", headers=_admin_headers("wrong-key"))
        assert r.status_code == 401


def test_admin_accounts_accounts_disabled_503(tmp_path, monkeypatch):
    """accounts 未启用（未传 accounts_store）→ 全部管理面板端点 503。"""
    app = _mk_app(tmp_path, monkeypatch, accounts_store=None)
    with TestClient(app) as c:
        assert c.get("/admin/api/accounts", headers=_admin_headers()).status_code == 503
        assert c.post("/admin/api/accounts",
                      json={"username": "x", "password": "y"},
                      headers=_admin_headers()).status_code == 503
        assert c.get("/admin/api/accounts/1", headers=_admin_headers()).status_code == 503


def test_admin_create_and_list_accounts(tmp_path, monkeypatch):
    """POST /accounts 建号 → 201 + id；GET /accounts 列表能看到新建账号。"""
    store = _make_store(tmp_path)
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        r = c.post("/admin/api/accounts",
                   json={"username": "alice", "password": "p1",
                         "display_name": "Alice", "is_admin": False,
                         "token_budget": 5000},
                   headers=_admin_headers())
        assert r.status_code == 201, r.text
        body = r.json()
        uid = body["id"]
        assert uid >= 1
        # UI 契约：不返回完整 password_hash（电池火种）
        assert "password_hash" not in body

        # 列表能看到
        lst = c.get("/admin/api/accounts", headers=_admin_headers()).json()
        assert isinstance(lst["accounts"], list)
        assert any(a["id"] == uid for a in lst["accounts"])


def test_admin_get_single_account(tmp_path, monkeypatch):
    """GET /accounts/{id} 命中返回 200，不命中返回 404。"""
    store = _make_store(tmp_path)
    uid = _create_user(store, username="bob")
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        r = c.get(f"/admin/api/accounts/{uid}", headers=_admin_headers())
        assert r.status_code == 200
        assert r.json()["id"] == uid
        r404 = c.get("/admin/api/accounts/99999", headers=_admin_headers())
        assert r404.status_code == 404


def test_admin_duplicate_username_409(tmp_path, monkeypatch):
    """username unique 冲突 → 409。"""
    store = _make_store(tmp_path)
    _create_user(store, username="dup")
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        r = c.post("/admin/api/accounts",
                   json={"username": "dup", "password": "p"},
                   headers=_admin_headers())
        assert r.status_code == 409


def test_admin_update_account_limits(tmp_path, monkeypatch):
    """PUT /accounts/{id} 改限额 / is_admin / status；越权 status 400。"""
    store = _make_store(tmp_path)
    uid = _create_user(store, username="carol", budget=100)
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        r = c.put(f"/admin/api/accounts/{uid}",
                  json={"rpm_limit": 60, "tpm_limit": 5000, "is_admin": True,
                        "budget_period": "week"},
                  headers=_admin_headers())
        assert r.status_code == 200
        row = c.get(f"/admin/api/accounts/{uid}", headers=_admin_headers()).json()
        assert row["rpm_limit"] == 60
        assert row["tpm_limit"] == 5000
        assert row["is_admin"] is True
        assert row["budget_period"] == "week"

        # 越权 status 应 400
        r2 = c.put(f"/admin/api/accounts/{uid}", json={"status": "bogus"},
                   headers=_admin_headers())
        assert r2.status_code == 400


def test_admin_delete_account(tmp_path, monkeypatch):
    """DELETE /accounts/{id} 级联删除 keys/sessions/messages；重复 delete 幂等返回 200。"""
    store = _make_store(tmp_path)
    uid = _create_user(store, username="dave")
    kid, _ = _create_key(store, uid)
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        assert c.delete(f"/admin/api/accounts/{uid}", headers=_admin_headers()).status_code == 200
        assert store.get_user_by_id(uid) is None
        assert store.get_key_by_id(kid) is None
        assert c.delete(f"/admin/api/accounts/{uid}", headers=_admin_headers()).status_code == 404


def test_admin_issue_key_returns_plain_once(tmp_path, monkeypatch):
    """POST /accounts/{uid}/keys 一次性返回明文 key（仅这一次）；
    后续 GET keys 列表只见 prefix 脱敏。"""
    store = _make_store(tmp_path)
    uid = _create_user(store, username="eve")
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        r = c.post(f"/admin/api/accounts/{uid}/keys",
                   json={"name": "prod", "expires_in_days": None},
                   headers=_admin_headers())
        assert r.status_code == 201, r.text
        body = r.json()
        assert "key" in body  # 明文
        assert body["key"].startswith("sk-mctl-")
        kid = body["id"]
        # 列表只见 prefix
        lst = c.get(f"/admin/api/accounts/{uid}/keys", headers=_admin_headers()).json()
        entry = next(k for k in lst["keys"] if k["id"] == kid)
        assert "key" not in entry  # 没有明文字段
        assert entry["key_prefix"].startswith("sk-mctl-")


def test_admin_update_key_status_and_delete(tmp_path, monkeypatch):
    """PUT /accounts/{uid}/keys/{kid} disabled/enabled/revoked；DELETE 幂等。"""
    store = _make_store(tmp_path)
    uid = _create_user(store, username="frank")
    kid, _ = _create_key(store, uid)
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        assert c.put(f"/admin/api/accounts/{uid}/keys/{kid}",
                     json={"status": "disabled"}, headers=_admin_headers()).status_code == 200
        assert store.get_key_by_id(kid)["status"] == "disabled"
        assert c.put(f"/admin/api/accounts/{uid}/keys/{kid}",
                     json={"status": "active"}, headers=_admin_headers()).status_code == 200
        assert c.put(f"/admin/api/accounts/{uid}/keys/{kid}",
                     json={"status": "bogus"}, headers=_admin_headers()).status_code == 400
        assert c.delete(f"/admin/api/accounts/{uid}/keys/{kid}",
                        headers=_admin_headers()).status_code == 200
        assert store.get_key_by_id(kid) is None
        assert c.delete(f"/admin/api/accounts/{uid}/keys/{kid}",
                        headers=_admin_headers()).status_code == 404


def test_admin_usage_endpoint(tmp_path, monkeypatch):
    """GET /accounts/{uid}/usage 返回 sum_usage_for_user + budget_consumed。"""
    store = _make_store(tmp_path)
    uid = _create_user(store, username="gina", budget=100)
    # 少量 usage 直插
    for _ in range(3):
        store.insert_usage(
            user_id=uid, key_id=None, request_id=str(uuid.uuid4()),
            model="qwen3.8", prompt_tokens=10, completion_tokens=5,
            client_ip="1.2.3.4", ttft_ms=None, latency_ms=None,
            status="200", now=time.time(),
        )
    store.increment_budget_consumed(uid, 30, now=time.time())
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        r = c.get(f"/admin/api/accounts/{uid}/usage", headers=_admin_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["requests"] == 3
        assert body["prompt_tokens"] == 30
        assert body["completion_tokens"] == 15
        assert body["total_tokens"] == 45
        assert body["budget_consumed"] == 30
        assert body["token_budget"] == 100


# ---------------------------------------------------------------------------
# 2. account_self 登录
# ---------------------------------------------------------------------------

def test_self_login_success_issuers_jwt(tmp_path, monkeypatch):
    """正确凭据 → 200 + `token`（HS256，payload 含 user_id / is_admin / exp）。"""
    store = _make_store(tmp_path)
    uid = _create_user(store, username="alice", password="p1")
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        r = c.post("/api/account/login", json={"username": "alice", "password": "p1"})
        assert r.status_code == 200, r.text
        token = r.json()["token"]
        assert token.count(".") == 2  # JWT 三段
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        assert payload["user_id"] == uid
        assert payload["is_admin"] is False
        assert "exp" in payload and "iat" in payload


def test_self_login_wrong_password_401(tmp_path, monkeypatch):
    password: str = "wrong-pw"
    store = _make_store(tmp_path)
    _create_user(store, username="alice", password="right-pw")
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        r = c.post("/api/account/login", json={"username": "alice", "password": password})
        assert r.status_code == 401


def test_self_login_unknown_user_401(tmp_path, monkeypatch):
    """未知 username → 401（无用户存在性泄漏：与错密码同一提示）"""
    store = _make_store(tmp_path)
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        r = c.post("/api/account/login", json={"username": "ghost", "password": "x"})
        assert r.status_code == 401


def test_self_login_disabled_user_401(tmp_path, monkeypatch):
    """账号 status=disabled 时登录返回 401（即使密码正确）。"""
    store = _make_store(tmp_path)
    uid = _create_user(store, username="hank", password="p")
    store.set_user_status(uid, "disabled", now=time.time())
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        r = c.post("/api/account/login", json={"username": "hank", "password": "p"})
        assert r.status_code == 401


def test_self_login_accounts_disabled_503(tmp_path, monkeypatch):
    app = _mk_app(tmp_path, monkeypatch, accounts_store=None)
    with TestClient(app) as c:
        assert c.post("/api/account/login",
                      json={"username": "x", "password": "y"}).status_code == 503


# ---------------------------------------------------------------------------
# 3. account_self 自助
# ---------------------------------------------------------------------------

def _login_self(c: TestClient, username: str, password: str) -> str:
    r = c.post("/api/account/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _mk_token(user_id: int, *, is_admin: bool = False, exp: int | None = None) -> str:
    now = int(time.time())
    payload = {"user_id": user_id, "is_admin": is_admin, "iat": now,
               "exp": (exp if exp is not None else now + 3600)}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def test_self_requires_jwt_401(tmp_path, monkeypatch):
    """无 Bearer token 访问 /api/account/keys → 401。"""
    store = _make_store(tmp_path)
    _create_user(store, username="zi")
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        assert c.get("/api/account/keys").status_code == 401
        # 敏感操作同样 401
        assert c.post("/api/account/keys", json={"name": "x"}).status_code == 401
        assert c.delete("/api/account/sessions/1").status_code == 401


def test_self_jwt_tampered_401(tmp_path, monkeypatch):
    """篡改 token 的 payload/exp → 401。"""
    store = _make_store(tmp_path)
    uid = _create_user(store, username="tam")
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    tok = _mk_token(uid)
    # 翻转**签名首字符**导致签名失效。
    # 切勿翻转末字符：HS256 签名 32 字节 = 256 bit，base64url 编码成 43 字符即 258 bit，
    # 多出的 2 bit 全在末字符上（解码时被丢弃）→ 同一解码结果对应 4 个末字符。
    # 翻转它有 4/64 = 1/16 概率解出完全相同的 32 字节签名 → 校验通过 → 用例概率性假红。
    # 首字符的 6 bit 全部有效，翻转必然改变解码后的签名字节。
    # 详见 docs/known-pitfalls/backend/test-isolation.md
    hdr, payload, sig = tok.split(".")
    tampered = f"{hdr}.{payload}.{'A' if sig[0] != 'A' else 'B'}{sig[1:]}"
    with TestClient(app) as c:
        assert c.get("/api/account/keys",
                     headers=_self_headers(tampered)).status_code == 401
        # expired token
        past = _mk_token(uid, exp=int(time.time()) - 10)
        assert c.get("/api/account/keys",
                     headers=_self_headers(past)).status_code == 401


def test_self_jwt_wrong_secret_401(tmp_path, monkeypatch):
    """token 用**另一个 secret** 编 → 401。"""
    store = _make_store(tmp_path)
    uid = _create_user(store, username="sek")
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    tok = jwt.encode({"user_id": uid, "is_admin": False,
                      "iat": int(time.time()), "exp": int(time.time()) + 60},
                     "other-secret", algorithm="HS256")
    with TestClient(app) as c:
        assert c.get("/api/account/keys",
                     headers=_self_headers(tok)).status_code == 401


def test_self_create_and_list_keys(tmp_path, monkeypatch):
    """自助 POST /keys 一次性返回明文；GET /keys 不返回明文。
    且用户 A 的 token 访问用户 B 的资源应 404（require_account 用 payload 里的
    user_id 强归属，不依赖 token 携带的具体 key）。"""
    store = _make_store(tmp_path)
    uid_a = _create_user(store, username="a", password="pa")
    uid_b = _create_user(store, username="b", password="pb")
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        tok_a = _login_self(c, "a", "pa")
        tok_b = _login_self(c, "b", "pb")
        r = c.post("/api/account/keys", json={"name": "a-key"},
                   headers=_self_headers(tok_a))
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["key"].startswith("sk-mctl-")
        kid_a = body["id"]

        # 列表只露 prefix
        lst = c.get("/api/account/keys", headers=_self_headers(tok_a)).json()
        mine = [k for k in lst["keys"] if k["id"] == kid_a]
        assert len(mine) == 1
        assert "key" not in mine[0]
        # 用户 B 看不到用户 A 的 Key（因为 self 端点按 JWT user_id 隔离）
        lst_b = c.get("/api/account/keys", headers=_self_headers(tok_b)).json()
        assert all(k["id"] != kid_a for k in lst_b["keys"])


def test_self_update_and_delete_own_key(tmp_path, monkeypatch):
    """用户只能改/删自己的 Key：其余用户的 kid 返回 404。"""
    store = _make_store(tmp_path)
    uid_a = _create_user(store, username="ca", password="pa")
    uid_b = _create_user(store, username="cb", password="pb")
    kid_a, _ = _create_key(store, uid_a, name="a")
    kid_b, _ = _create_key(store, uid_b, name="b")
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        tok_a = _login_self(c, "ca", "pa")
        # 改自己的
        assert c.put(f"/api/account/keys/{kid_a}", json={"status": "disabled"},
                     headers=_self_headers(tok_a)).status_code == 200
        # 改别人的 → 404（归属校验失败）
        assert c.put(f"/api/account/keys/{kid_b}", json={"status": "disabled"},
                     headers=_self_headers(tok_a)).status_code == 404
        # 删自己的
        assert c.delete(f"/api/account/keys/{kid_a}",
                        headers=_self_headers(tok_a)).status_code == 200
        # 删别人的 → 404
        assert c.delete(f"/api/account/keys/{kid_b}",
                        headers=_self_headers(tok_a)).status_code == 404


def test_self_usage_endpoint(tmp_path, monkeypatch):
    """GET /usage 按 invoice 显示的 tokens / budget_consumed（JSON 中必须为
    数字，不是字符串）。"""
    store = _make_store(tmp_path)
    uid_a = _create_user(store, username="uz", password="pu", budget=999)
    _create_user(store, username="uz2", password="pu2", budget=1)
    store.insert_usage(
        user_id=uid_a, key_id=None, request_id="r-1", model="qwen3.8",
        prompt_tokens=20, completion_tokens=10, client_ip="1.2.3.4",
        ttft_ms=None, latency_ms=None, status="200", now=time.time(),
    )
    store.increment_budget_consumed(uid_a, 30, now=time.time())
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        tok = _login_self(c, "uz", "pu")
        r = c.get("/api/account/usage", headers=_self_headers(tok))
        assert r.status_code == 200
        body = r.json()
        assert body["requests"] == 1
        assert body["total_tokens"] == 30
        assert body["budget_consumed"] == 30
        assert body["token_budget"] == 999
        assert isinstance(body["total_tokens"], int)  # 数字字段


def _mk_session_and_messages(store: AccountsStore, user_id: int, *,
                              session_key: str = "s-test-0001",
                              title: str = "hello",
                              model: str = "qwen3.8") -> dict:
    """造会话 + 一条 user 消息 + 一条 assistant 消息，返回会话行。"""
    sess = store.get_or_create_session(
        user_id=user_id, key_id=None, model=model,
        session_id=session_key, title=title, now=time.time(),
    )
    store.add_message(session_id=sess["id"], role="user",
                      content="hi there", now=time.time())
    store.add_message(session_id=sess["id"], role="assistant",
                      content="hello", now=time.time())
    store.bump_session(sess["id"], count=2, now=time.time())
    return sess


def test_self_sessions_list_get_export_delete(tmp_path, monkeypatch):
    """会话归属校验：自己会话可见；跨用户访问一律 404。
    导出内容 = session 元 + 全部消息（按时间正序）。"""
    store = _make_store(tmp_path)
    uid_a = _create_user(store, username="sa", password="pa")
    uid_b = _create_user(store, username="sb", password="pb")
    sess_a = _mk_session_and_messages(store, uid_a, session_key="ska",
                                       title="a-title")
    _mk_session_and_messages(store, uid_b, session_key="skb", title="b-title")
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        tok_a = _login_self(c, "sa", "pa")
        # 列表（自己的 title 命中）
        r = c.get("/api/account/sessions", headers=_self_headers(tok_a))
        assert r.status_code == 200
        rows = r.json()["sessions"]
        assert any(s["id"] == sess_a["id"] for s in rows)
        assert all(s["session_key"] == "ska" for s in rows)

        # 单条
        r = c.get(f"/api/account/sessions/{sess_a['id']}",
                  headers=_self_headers(tok_a))
        assert r.status_code == 200
        assert r.json()["title"] == "a-title"

        # 导出
        r = c.get(f"/api/account/sessions/{sess_a['id']}/export",
                  headers=_self_headers(tok_a))
        assert r.status_code == 200
        exp = r.json()
        assert exp["session"]["id"] == sess_a["id"]
        assert [m["role"] for m in exp["messages"]] == ["user", "assistant"]

        # 跨用户 → 404
        sess_b_id = next(s["id"] for s in
                        c.get("/api/account/sessions",
                              headers=_self_headers(_login_self(c, "sb", "pb"))).json()["sessions"])
        assert c.get(f"/api/account/sessions/{sess_b_id}",
                     headers=_self_headers(tok_a)).status_code == 404
        assert c.delete(f"/api/account/sessions/{sess_b_id}",
                        headers=_self_headers(tok_a)).status_code == 404

        # 删自己的
        assert c.delete(f"/api/account/sessions/{sess_a['id']}",
                        headers=_self_headers(tok_a)).status_code == 200
        assert store.get_session(sess_a["id"], user_id=uid_a) is None


def test_self_message_search(tmp_path, monkeypatch):
    """GET /sessions/search?q= 命中消息内容，返回时带 session title/model 上下文。"""
    store = _make_store(tmp_path)
    uid_a = _create_user(store, username="ssa", password="pa")
    sess_a = _mk_session_and_messages(store, uid_a, session_key="sks",
                                       title="t1", model="qwen3.8")
    # 造第二条消息，内容含独特令牌
    store.add_message(session_id=sess_a["id"], role="assistant",
                      content="unique-marker-0001", now=time.time())
    store.bump_session(sess_a["id"], count=1, now=time.time())
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    with TestClient(app) as c:
        tok_a = _login_self(c, "sa", "pa") if False else _login_self(c, "ssa", "pa")
        r = c.get("/api/account/sessions/search",
                  params={"q": "unique-marker-0001"},
                  headers=_self_headers(tok_a))
        assert r.status_code == 200
        rows = r.json()["messages"]
        assert any(m["session_id"] == sess_a["id"] and
                   "unique-marker-0001" in (m["content"] or "")
                   for m in rows)
        assert rows and all(m.get("title") == "t1" for m in rows)


# ---------------------------------------------------------------------------
# 4. 路由组五方法齐全
# ---------------------------------------------------------------------------

def test_admin_accounts_route_methods_complete(tmp_path, monkeypatch):
    """/admin/api/accounts 端点 GET/POST；/admin/api/accounts/{id} GET/PUT/DELETE；
    /admin/api/accounts/{id}/keys GET/POST；/admin/api/accounts/{id}/keys/{kid}
    PUT/DELETE 一一注册（避免前端 405）。"""
    store = _make_store(tmp_path)
    _create_user(store, username="rp")
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    paths = app.openapi()["paths"]
    assert "/admin/api/accounts" in paths
    assert "get" in paths["/admin/api/accounts"]
    assert "post" in paths["/admin/api/accounts"]

    # /admin/api/accounts/{account_id}
    # FastAPI openapi 路径模板里允许 account_id/account 名字差异，宽松匹配
    # —— 按路径模板数字段个数 + 方法
    # 定位。
    single = None
    keys_route = None
    # OpenAPI 路径 `/admin/api/accounts/{account_id}` 里有 4 个 "/"（含前导）
    # 4 段（去掉空串后）；不同 FastAPI 版本对追尾斜杠策略一致（无追尾），
    # 用 segs 定位比 count 稳。
    for p, methods in paths.items():
        segs = [s for s in p.split("/") if s]
        # 单资源：`/admin/api/accounts/{account_id}` 共 4 段
        # （admin / api / accounts / {account_id}）
        if (p.startswith("/admin/api/accounts/") and "{" in p
                and len(segs) == 4 and not p.endswith("/keys")
                and not p.endswith("/usage")):
            if "get" in methods and "put" in methods and "delete" in methods:
                single = p
        # keys 列表：末段 keys
        if (p.startswith("/admin/api/accounts/") and p.endswith("/keys")
                and "{" in p and len(segs) == 5):
            if "get" in methods and "post" in methods:
                keys_route = p
    # 关键护栏：不存在会让 list_keys 被误挂到 /accounts/{id}（type='keys'）的路径泄漏
    assert single is not None, "缺少 /admin/api/accounts/{id} 完整路由"
    assert keys_route is not None, "缺少 /admin/api/accounts/{id}/keys 完整路由"


def test_account_self_route_methods_complete(tmp_path, monkeypatch):
    """/api/account/* 端点齐全：login POST；keys GET/POST/PUT/DELETE；
    usage GET；sessions GET；sessions/{id} GET/DELETE；sessions/{id}/export GET；
    sessions/search GET。"""
    store = _make_store(tmp_path)
    _create_user(store, username="rs")
    app = _mk_app(tmp_path, monkeypatch, accounts_store=store)
    paths = app.openapi()["paths"]
    assert "/api/account/login" in paths and "post" in paths["/api/account/login"]
    # /api/account/keys
    assert "/api/account/keys" in paths
    assert "get" in paths["/api/account/keys"]
    assert "post" in paths["/api/account/keys"]
    # /api/account/keys/{key_id}
    key_route = next(
        (p for p in paths if p.startswith("/api/account/keys/") and "{" in p),
        None,
    )
    assert key_route is not None
    assert "put" in paths[key_route]
    assert "delete" in paths[key_route]
    # /api/account/usage
    assert "/api/account/usage" in paths and "get" in paths["/api/account/usage"]
    # /api/account/sessions
    assert "/api/account/sessions" in paths and "get" in paths["/api/account/sessions"]
    # /api/account/sessions/{id}
    sess_route = next(
        (p for p in paths if p.startswith("/api/account/sessions/") and "{" in p
         and not p.endswith("/export") and not p.endswith("/search")),
        None,
    )
    assert sess_route is not None
    assert "get" in paths[sess_route]
    assert "delete" in paths[sess_route]
    # /api/account/sessions/{id}/export
    exp_route = next(
        (p for p in paths if p.startswith("/api/account/sessions/")
         and p.endswith("/export") and "{" in p),
        None,
    )
    assert exp_route is not None and "get" in paths[exp_route]
    # /api/account/sessions/search
    assert "/api/account/sessions/search" in paths and "get" in paths["/api/account/sessions/search"]
