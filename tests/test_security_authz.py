#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_security_authz.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/11 11:30
# @Desc   : 鉴权与凭据安全红线回归测试
# ===============================================================================

"""安全红线回归：鉴权 fail-closed、凭据隔离、JWT 伪造、注入、脱敏。

定位（与既有安全测试的分工）：test_gateway.py / test_webui_admin_accounts.py 已经
覆盖了「功能正确性」视角下的鉴权（对 key 放行、错 key 拒绝）。本文件只覆盖**安全红线**
——即「如果这条约束哪天被改坏，会不会直接变成可利用漏洞」的那批不变式，因此用例都
带明确的威胁模型注释。conftest 会按 `test_security_` 前缀自动打 `security` marker，
CI 可单独立项 `-m security` 快速门禁。

威胁模型分层（本项目三把独立密钥）：
- API_KEY                → 管理面（/admin/api/*），能改配置、启停模型
- GATEWAY_CLIENT_API_KEY → 数据面（/v1*），只能推理
- ACCOUNTS_JWT_SECRET    → 账号面 JWT 签名（不回退 API_KEY）
红线核心是**三者绝不互换/绝不共用/绝不因缺配而放行**。
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

pytest.importorskip("fastapi")

pytestmark = pytest.mark.security

from fastapi.testclient import TestClient  # noqa: E402

from modelctl.core.gateway import (  # noqa: E402
    AUTH_INVALID,
    AUTH_MISSING,
    AUTH_OK,
    AUTH_UNCONFIGURED,
    create_app,
    verify_client,
)

ADMIN_KEY = "sk-admin-plane-key-0001"
CLIENT_KEY = "sk-data-plane-key-0002"
JWT_SECRET = "unit-test-jwt-secret-32-bytes-ok"


def _admin_client(monkeypatch, **extra_env):
    monkeypatch.setenv("API_KEY", ADMIN_KEY)
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", CLIENT_KEY)
    monkeypatch.setenv("ACCOUNTS_JWT_SECRET", JWT_SECRET)
    for k, v in extra_env.items():
        monkeypatch.setenv(k, v)
    return TestClient(create_app(admin=True))


# ===========================================================================
# 1. 管理面 fail-closed —— API_KEY 缺失/错配一律拒绝，无默认放行
# ===========================================================================


class TestAdminPlaneFailClosed:
    def test_no_api_key_configured_denies_every_admin_route(self, monkeypatch):
        """威胁：API_KEY 未配时若"当作空 key 比较"，空 Bearer 即可进管理面。

        期望：未配置 = 全部 401（fail-closed），与"配了但给错"同等拒绝。
        """
        monkeypatch.delenv("API_KEY", raising=False)
        with TestClient(create_app(admin=True)) as c:
            for path in ("/admin/api/overview", "/admin/api/models", "/admin/api/envs", "/admin/api/audit"):
                assert c.get(path, headers={"Authorization": "Bearer "}).status_code == 401, path

    def test_empty_bearer_token_rejected(self, monkeypatch):
        with _admin_client(monkeypatch) as c:
            assert c.get("/admin/api/overview", headers={"Authorization": "Bearer "}).status_code == 401

    def test_non_bearer_scheme_rejected(self, monkeypatch):
        """Basic/Digest 等其它 scheme 不得被当作 Bearer 误放行。"""
        with _admin_client(monkeypatch) as c:
            r = c.get("/admin/api/overview", headers={"Authorization": f"Basic {ADMIN_KEY}"})
            assert r.status_code == 401

    def test_correct_key_admitted(self, monkeypatch):
        with _admin_client(monkeypatch) as c:
            assert c.get("/admin/api/overview", headers={"Authorization": f"Bearer {ADMIN_KEY}"}).status_code == 200

    @pytest.mark.parametrize(
        "bad",
        [ADMIN_KEY[:-1], ADMIN_KEY + "x", ADMIN_KEY.replace("-", "_"), "Bearer " + ADMIN_KEY, "x" * len(ADMIN_KEY)],
    )
    def test_near_miss_keys_rejected(self, monkeypatch, bad):
        """前缀/后缀/变体的"近似正确"key 一律拒绝（比较是全串恒定时间比较）。

        注：token 两端的空白会被 Bearer 解析器 strip 掉（与网关 verify_client 的
        `.strip()` 口径一致），故"带空格的正确 key"是**合法**放行，不作为拒绝样本。
        """
        with _admin_client(monkeypatch) as c:
            assert c.get("/admin/api/overview", headers={"Authorization": f"Bearer {bad}"}).status_code == 401

    def test_non_ascii_bearer_returns_401_not_500(self, monkeypatch):
        """威胁：用非 ASCII Bearer 头触发 compare_digest 的 TypeError → 500（匿名 DoS 面）。

        回归 WEB-P2-1：修复是先把两侧编码成 UTF-8 bytes 再比较。
        """
        raw = "Bearer 中文密钥".encode()
        with _admin_client(monkeypatch) as c:
            r = c.get("/admin/api/config/static", headers=[(b"authorization", raw)])
            assert r.status_code == 401, f"非 ASCII 凭据须 401，实得 {r.status_code}"

    def test_admin_401_body_shape_stable(self, monkeypatch):
        """401 响应必须是 {"code":"auth"} 形状——前端拦截器只认这一种。"""
        with _admin_client(monkeypatch) as c:
            body = c.get("/admin/api/overview").json()
            assert body["detail"]["code"] == "auth"


# ===========================================================================
# 2. 数据面凭据隔离 —— 管理面 key 绝不等于数据面 key
# ===========================================================================


class TestDataPlaneIsolation:
    def test_admin_key_cannot_use_data_plane(self, monkeypatch):
        """红线：能改配置的 API_KEY 绝不能直接拿去推理（否则永远不能下发给客户端）。"""
        with _admin_client(monkeypatch) as c:
            assert c.get("/v1/models", headers={"Authorization": f"Bearer {ADMIN_KEY}"}).status_code == 401
            assert c.get("/v1/models", headers={"Authorization": f"Bearer {CLIENT_KEY}"}).status_code == 200

    def test_client_key_cannot_use_admin_plane(self, monkeypatch):
        """反向：数据面 key 也进不了管理面（两把 key 各管一段，不可互相提权）。"""
        with _admin_client(monkeypatch) as c:
            assert c.get("/admin/api/overview", headers={"Authorization": f"Bearer {CLIENT_KEY}"}).status_code == 401

    def test_gateway_client_key_unset_denies_all_v1(self, monkeypatch):
        """GATEWAY_CLIENT_API_KEY 未配 = /v1* 全 401（fail-closed，不因"没配"而放行）。"""
        monkeypatch.setenv("API_KEY", ADMIN_KEY)
        monkeypatch.delenv("GATEWAY_CLIENT_API_KEY", raising=False)
        with TestClient(create_app()) as c:
            assert c.get("/v1/models", headers={"Authorization": f"Bearer {ADMIN_KEY}"}).status_code == 401


class TestVerifyClientLabels:
    """verify_client 的四态标签直接写进审计；标签错=审计口径错=事后无法归因。"""

    class _Req:
        def __init__(self, headers):
            self.headers = headers

    def test_unconfigured_when_no_key(self, monkeypatch):
        monkeypatch.delenv("GATEWAY_CLIENT_API_KEY", raising=False)
        monkeypatch.setattr("modelctl.core.gateway._client_key_env_loaded", True)
        assert verify_client(self._Req({})) == AUTH_UNCONFIGURED

    def test_missing_when_no_credential(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", CLIENT_KEY)
        monkeypatch.setattr("modelctl.core.gateway._client_key_env_loaded", True)
        assert verify_client(self._Req({})) == AUTH_MISSING

    def test_ok_on_bearer_and_x_api_key(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", CLIENT_KEY)
        monkeypatch.setattr("modelctl.core.gateway._client_key_env_loaded", True)
        assert verify_client(self._Req({"authorization": f"Bearer {CLIENT_KEY}"})) == AUTH_OK
        # x-api-key 是 Anthropic SDK 唯一会带的头（双通道嗅探的存在理由）
        assert verify_client(self._Req({"x-api-key": CLIENT_KEY})) == AUTH_OK

    def test_invalid_on_wrong_key_never_500_on_non_ascii(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", CLIENT_KEY)
        monkeypatch.setattr("modelctl.core.gateway._client_key_env_loaded", True)
        assert verify_client(self._Req({"authorization": "Bearer 中文"})) == AUTH_INVALID
        assert verify_client(self._Req({"x-api-key": CLIENT_KEY + "tamper"})) == AUTH_INVALID


# ===========================================================================
# 3. JWT 账号面 —— 伪造 / 篡改 / 过期 / 算法降级一律拒绝
# ===========================================================================


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unsigned_token(payload: dict) -> str:
    """alg=none 伪造 token：历史上"接受 none 算法"是 JWT 最致命的绕过。"""
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    return f"{header}.{_b64url(payload)}."


class TestJWTSecurity:
    @pytest.fixture()
    def jwt_mod(self):
        pytest.importorskip("jwt")
        from modelctl.core.webui import account_auth

        return account_auth

    def test_missing_secret_fails_fast(self, jwt_mod, monkeypatch):
        """ACCOUNTS_JWT_SECRET 缺失必须抛错，绝不回退 API_KEY。

        红线：共用一把秘密会让"管理面密钥泄漏"直接升级成"任意用户身份伪造"。
        """
        monkeypatch.delenv("ACCOUNTS_JWT_SECRET", raising=False)
        with pytest.raises(RuntimeError):
            jwt_mod.issue_token(user_id=1, is_admin=False)

    def test_verify_rejects_alg_none(self, jwt_mod, monkeypatch):
        """alg=none 的自签 admin token 必须被拒（只认 HS256）。"""
        monkeypatch.setenv("ACCOUNTS_JWT_SECRET", JWT_SECRET)
        forged = _unsigned_token({"user_id": 1, "is_admin": True, "iat": 1, "exp": 2**31})
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            jwt_mod.verify_token(forged)

    def test_verify_rejects_wrong_secret(self, jwt_mod, monkeypatch):
        monkeypatch.setenv("ACCOUNTS_JWT_SECRET", JWT_SECRET)
        other = jwt_mod.issue_token(user_id=1, is_admin=True)
        # 换一个 secret 签的同 payload token 必须验签失败
        monkeypatch.setenv("ACCOUNTS_JWT_SECRET", "a-totally-different-secret-32bytes!")
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            jwt_mod.verify_token(other)

    def test_verify_rejects_expired(self, jwt_mod, monkeypatch):
        monkeypatch.setenv("ACCOUNTS_JWT_SECRET", JWT_SECRET)
        expired = jwt_mod.issue_token(user_id=1, is_admin=True, now=0)  # exp = JWT_TTL_S（远古）
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            jwt_mod.verify_token(expired)

    def test_verify_rejects_missing_user_id(self, jwt_mod, monkeypatch):
        monkeypatch.setenv("ACCOUNTS_JWT_SECRET", JWT_SECRET)
        import jwt as _jwt

        tok = _jwt.encode({"is_admin": True, "iat": 10, "exp": 2**31}, JWT_SECRET, algorithm="HS256")
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            jwt_mod.verify_token(tok)

    def test_tampered_payload_rejected(self, jwt_mod, monkeypatch):
        """改 payload 后不重签名 → 验签失败（防权限提升：is_admin False→True）。"""
        monkeypatch.setenv("ACCOUNTS_JWT_SECRET", JWT_SECRET)
        tok = jwt_mod.issue_token(user_id=5, is_admin=False)
        hdr, payload, sig = tok.split(".")
        data = json.loads(base64.urlsafe_b64decode(payload + "=="))
        data["is_admin"] = True
        forged_payload = _b64url(data)
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            jwt_mod.verify_token(f"{hdr}.{forged_payload}.{sig}")

    def test_tampered_signature_first_char_rejected(self, jwt_mod, monkeypatch):
        """翻转签名**首字符**（有效 bit）必被拒。

        known-pitfalls 记录的 flake：翻末字符有 1/16 概率因 base64url 末位填充而假绿，
        因此安全用例一律翻首字符。
        """
        monkeypatch.setenv("ACCOUNTS_JWT_SECRET", JWT_SECRET)
        tok = jwt_mod.issue_token(user_id=5, is_admin=True)
        hdr, payload, sig = tok.split(".")
        tampered = f"{hdr}.{payload}.{'A' if sig[0] != 'A' else 'B'}{sig[1:]}"
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            jwt_mod.verify_token(tampered)


# ===========================================================================
# 4. 凭据哈希与脱敏 —— 库泄漏不等于明文泄漏
# ===========================================================================


class TestCredentialHashing:
    def test_api_key_hash_is_deterministic_sha256(self):
        pytest.importorskip("bcrypt")
        from modelctl.core.accounts.hashing import hash_api_key

        key = "sk-mctl-abc123"
        assert hash_api_key(key) == hashlib.sha256(key.encode()).hexdigest()
        assert len(hash_api_key(key)) == 64

    def test_generated_keys_are_high_entropy_and_unique(self):
        pytest.importorskip("bcrypt")
        from modelctl.core.accounts.hashing import generate_api_key

        keys = {generate_api_key() for _ in range(50)}
        assert len(keys) == 50  # 无碰撞
        assert all(k.startswith("sk-mctl-") and len(k) >= len("sk-mctl-") + 32 for k in keys)

    def test_key_mask_never_leaks_full_key(self):
        pytest.importorskip("bcrypt")
        from modelctl.core.accounts.hashing import generate_api_key, key_prefix

        key = generate_api_key()
        masked = key_prefix(key)
        assert key not in masked
        assert masked.startswith("sk-mctl-***")
        assert masked[-4:] == key[-4:]

    def test_key_mask_short_key_fully_masked(self):
        """短 key（末段 ≤4）整段 ***，防"末 4 位 == 全长"泄漏。"""
        pytest.importorskip("bcrypt")
        from modelctl.core.accounts.hashing import key_prefix

        assert key_prefix("sk-mctl-ab") == "***"
        assert key_prefix("") == "***"
        assert key_prefix(None) == "***"

    def test_password_hash_roundtrip_and_negative(self):
        pytest.importorskip("bcrypt")
        from modelctl.core.accounts.hashing import hash_password, verify_password

        h = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", h) is True
        assert verify_password("wrong password", h) is False
        assert verify_password("", h) is False
        assert verify_password("x", "") is False


# ===========================================================================
# 5. 注入面 —— 目录穿越 / 路径参数
# ===========================================================================


class TestPathTraversalDefense:
    @pytest.mark.parametrize(
        "name",
        [
            "..%2F..%2Fetc%2Fpasswd",
            "../../etc/passwd",
            "..\\..\\windows\\win.ini",
            "....//....//etc/passwd",
            "/etc/passwd",
            "models/../../secret",
            ".",
            "..",
        ],
    )
    def test_startup_progress_rejects_traversal(self, monkeypatch, name):
        """GET /models/{name}/startup 的 name 参与文件路径拼接：穿越 payload 一律 404。

        白名单 `[A-Za-z0-9._-]+` + 拒绝 `..`；即便 URL 解码后含分隔符也进不了拼接。
        """
        with _admin_client(monkeypatch) as c:
            r = c.get(
                f"/admin/api/models/{name}/startup",
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            )
            # FastAPI 可能对非法路径直接 404；无论如何绝不能 200 且泄露文件内容
            assert r.status_code in (401, 404), f"穿越 payload {name!r} -> {r.status_code}"
            assert "root:" not in r.text and "[extensions]" not in r.text


# ===========================================================================
# 6. 敏感配置脱敏 —— /config/static 不回显密钥明文
# ===========================================================================


class TestSensitiveConfigMasking:
    @pytest.mark.parametrize(
        "key,is_sensitive",
        [
            ("API_KEY", True),
            ("GATEWAY_CLIENT_API_KEY", True),
            ("ACCOUNTS_JWT_SECRET", True),
            ("CLUSTER_NODE_TOKEN", True),
            ("ADMIN_PASSWORD", True),
            ("DB_PASSWD", True),
            ("NODE_ID", False),
            ("GATEWAY_PORT", False),
            ("GATEWAY_DEFAULT_MODEL", False),
        ],
    )
    def test_is_sensitive_key_pattern(self, key, is_sensitive):
        """显式名单 + 语义模式双保险：任何含 KEY/SECRET/TOKEN/PASSWORD/PASSWD 的键都敏感。

        红线：新增密钥类配置键时即便忘了加名单，模式兜底也不会让它明文回显。
        """
        from modelctl.core.webui.admin_config import _is_sensitive_key

        assert _is_sensitive_key(key) is is_sensitive, key

    def test_mask_value_keeps_only_tail(self):
        from modelctl.core.webui.admin_config import _mask_value

        assert _mask_value("abcdefghijkl") == "***ijkl"
        assert _mask_value("abcd") == "***"    # len<=4 全掩（末 4 位==全长）
        assert _mask_value("abcde") == "***bcde"  # len==5 保留末 4 位
        # 空值实际返回 "***"（docstring 写"原样返回"与实现不符，但脱敏更严，安全）
        assert _mask_value("") == "***"

    def test_all_mask_helpers_reject_short_key(self):
        """三处脱敏实现对短于保留位数的 key 必须整体掩掉，绝不出全文明。

        红线：`"abc"[-4:] == "abc"`（Python 负切片不越界），早期实现直接拼切片时
        短 key 被原样贴进可截屏的管理面响应；`admin_models._mask_key` 的旧写法
        `f"***{key}"` 更危险——星号打头看起来像已脱敏，实则全文明。
        三处口径：长 key → ***+末4位；短/空 → 全掩（admin_models 对 None/空保留
        None 语义，前端据此显示"未配置"）。
        """
        from modelctl.core.webui.admin_auth import mask_key
        from modelctl.core.webui.admin_config import _mask_value
        from modelctl.core.webui.admin_models import _mask_key

        secret = "abc"
        for got in (mask_key(secret), _mask_value(secret), _mask_key(secret)):
            assert secret not in (got or ""), got
        assert mask_key("abcdefghijkl") == _mask_value("abcdefghijkl") == "***ijkl"
        assert _mask_key("abcdefghijkl") == "***ijkl"
        assert mask_key("abcd") == _mask_value("abcd") == _mask_key("abcd") == "***"
        # admin_models 独有语义：未配置 = None，不得退化成 "***"（前端按 null 判未配置）
        assert _mask_key(None) is None and _mask_key("") is None

    def test_config_static_endpoint_masks_values(self, monkeypatch, tmp_path):
        """真起端点：写入含密钥的 .env，响应体里敏感值必须是脱敏态。

        PROJECT_ROOT 指向 tmp（endpoint 内 `from envfile import PROJECT_ROOT` 是延迟
        导入，取模块属性）——绝不写仓库真实 .env，否则测试中断会毁掉开发者本地配置。
        """
        tmp_path.joinpath(".env").write_text(
            "API_KEY=sk-super-secret-admin-value-9999\n"
            "GATEWAY_CLIENT_API_KEY=sk-super-secret-client-value-8888\n"
            "NODE_ID=210\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("modelctl.core.envfile.PROJECT_ROOT", tmp_path)
        # load_env 会把 .env 键经 setdefault 注入进程；本用例只验脱敏，注入无妨
        with _admin_client(monkeypatch) as c:
            body = c.get("/admin/api/config/static", headers={"Authorization": f"Bearer {ADMIN_KEY}"}).json()
        entries = {e["key"]: e for e in body["entries"]}
        assert entries["NODE_ID"]["value"] == "210" and entries["NODE_ID"]["sensitive"] is False
        for k in ("API_KEY", "GATEWAY_CLIENT_API_KEY"):
            assert entries[k]["sensitive"] is True
            assert "super-secret" not in entries[k]["value"]
            assert entries[k]["value"].startswith("***")


# ===========================================================================
# 7. 审计不泄漏密钥 —— 审计行只记结果标签，不记 key 值
# ===========================================================================


class TestAuditNoSecretLeak:
    def test_rejected_request_audit_has_no_key_material(self, monkeypatch, tmp_path):
        """被拒请求写审计：auth 字段是失败标签，绝不落 key 明文/片段。

        审计文件常对更大范围人员可读；把 key 写进审计等于开了第二条泄漏通道。
        """
        monkeypatch.setenv("AUDIT_DIR", str(tmp_path / "audit"))
        monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", CLIENT_KEY)
        with TestClient(create_app()) as c:
            c.get("/v1/models", headers={"Authorization": "Bearer wrong-key-leak-me"})
        files = list((tmp_path / "audit").glob("*.jsonl"))
        if not files:
            pytest.skip("当前网关配置未产生审计文件")
        content = "\n".join(f.read_text(encoding="utf-8") for f in files)
        assert "wrong-key-leak-me" not in content
