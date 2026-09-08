#!/usr/bin/env python3
# ===============================================================================
# @File   : src/modelctl/core/accounts/store.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/8 16:25
# @Desc   : 账号体系 SQLite DAO（五表）
# ===============================================================================

"""core/accounts/store.py — 账号 / Key / 用量 / 会话 的单文件 SQLite 台账。

沿用 `core/cluster/store.py` 已验证的范式：stdlib sqlite3 + 单连接
（`check_same_thread=False`）+ `threading.Lock` 写串行化 + WAL + busy_timeout +
幂等建表/补列 + 读路径零 `SELECT *`。

设计要点（改动前必读）：

1. **时间戳统一 epoch float（REAL），且一律由调用方注入 `now`**。本模块不取系统时间，
   一是让限流/会话窗口的单测可以完全控制时钟而不必 sleep，二是与 cluster store 同口径
   （ISO 字符串在时区切换下的比较不可靠）。"YYYY-MM-DD HH:mm:ss" 只在输出层格式化。
2. **`0/None` 限额语义是"不限制"，但 store 不做这个判断**。列可空就存 NULL，
   "NULL 视为不限制"属于 `limits.py` 的策略解释——DAO 里塞业务判断会让两处口径打架。
3. **列清单是 schema 的单一真值源**（`_USER_COLUMNS` 等）。建表 DDL 由它拼装生成，
   `_ensure_columns` 也由它算差集补列，避免"改了 schema 忘改 SELECT 清单"静默丢字段。
   护栏：`tests/test_accounts_store.py::test_explicit_column_lists_cover_full_schema`。
4. **会话归属校验写在 SQL 里**（`WHERE id=? AND user_id=?`）。自助面板只能动自己的会话，
   归属判定下放 DAO 才不可能被上层忘记；跨用户撞同一 `session_key` 时另起会话而非复用。
5. 本库是**应用自管 SQLite**（非线上生产库），`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE
   ADD COLUMN` 属业务库自举，不需要用户手工执行 SQL；**只增不删**列，绝不改/删既有列。
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Any

#: 无 X-Session-Id 时的会话聚合窗口（秒）：同一 (user, key, model) 距上次活跃
#: ≤ 本值视为同一会话（spec §6.1）。
SESSION_IDLE_TTL_S = 1800

#: 会话标题取首条 user 消息的截断长度（面板列表一屏可读）
TITLE_MAX_LEN = 120

#: users / api_keys 状态白名单：防调用方 typo 污染状态机（同 cluster.NODE_STATUSES 口径）
USER_STATUSES = ("active", "disabled")
KEY_STATUSES = ("active", "disabled", "revoked")

#: `update_user_limits` 可改字段白名单。刻意排除 username / password_hash：
#: 改名与改密是独立语义（各有专用方法 + 独立审计意图），混进通用 PATCH 会让
#: "改限额"的接口顺带拥有凭据写权限。
_USER_MUTABLE = ("display_name", "is_admin", "status", "concurrency_limit", "rpm_limit",
                 "tpm_limit", "token_budget", "budget_period", "budget_reset_at",
                 "retention_days")

# ---- 列清单（(列名, SQL 类型) 顺序即建表列序，亦是读路径列清单）----

_USER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("username", "TEXT NOT NULL"),
    ("password_hash", "TEXT NOT NULL"),
    ("display_name", "TEXT"),
    ("is_admin", "INTEGER NOT NULL DEFAULT 0"),
    ("status", "TEXT NOT NULL DEFAULT 'active'"),
    ("concurrency_limit", "INTEGER"),
    ("rpm_limit", "INTEGER"),
    ("tpm_limit", "INTEGER"),
    ("token_budget", "INTEGER"),
    ("budget_consumed", "INTEGER NOT NULL DEFAULT 0"),
    ("budget_period", "TEXT NOT NULL DEFAULT 'day'"),
    ("budget_reset_at", "REAL"),
    ("retention_days", "INTEGER"),
    ("created_at", "REAL"),
    ("updated_at", "REAL"),
)

_KEY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("user_id", "INTEGER NOT NULL"),
    ("key_prefix", "TEXT NOT NULL"),
    ("key_hash", "TEXT NOT NULL"),
    ("name", "TEXT NOT NULL DEFAULT ''"),
    ("status", "TEXT NOT NULL DEFAULT 'active'"),
    ("expires_at", "REAL"),
    ("last_used_at", "REAL"),
    ("created_at", "REAL"),
)

_USAGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("user_id", "INTEGER NOT NULL"),
    ("key_id", "INTEGER"),
    ("request_id", "TEXT"),
    ("model", "TEXT"),
    ("prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("completion_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("total_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("client_ip", "TEXT"),
    ("ttft_ms", "INTEGER"),
    ("latency_ms", "INTEGER"),
    ("status", "TEXT"),
    ("created_at", "REAL"),
)

_SESSION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("user_id", "INTEGER NOT NULL"),
    ("key_id", "INTEGER"),
    ("model", "TEXT"),
    ("session_key", "TEXT"),
    ("title", "TEXT"),
    ("message_count", "INTEGER NOT NULL DEFAULT 0"),
    ("created_at", "REAL"),
    # 活跃时刻：空闲窗口聚合与列表排序都读它。刻意不叫 updated_at——本列语义是
    # "最后一次对话发生时间"（限流窗口输入），不是"这一行最后被改的时间"。
    ("last_active_at", "REAL"),
)

_MESSAGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("session_id", "INTEGER NOT NULL"),
    ("role", "TEXT NOT NULL"),
    ("content", "TEXT"),
    ("prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("completion_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("created_at", "REAL"),
)

_USER_COLS = tuple(name for name, _ in _USER_COLUMNS)
_KEY_COLS = tuple(name for name, _ in _KEY_COLUMNS)
_USAGE_COLS = tuple(name for name, _ in _USAGE_COLUMNS)
_SESSION_COLS = tuple(name for name, _ in _SESSION_COLUMNS)
_MESSAGE_COLS = tuple(name for name, _ in _MESSAGE_COLUMNS)


def _create_table(table: str, columns: tuple[tuple[str, str], ...],
                  constraints: tuple[str, ...] = ()) -> str:
    """由列清单拼装建表 DDL（列清单是单一真值源，见模块 docstring 要点 3）。"""
    body = ", ".join(f"{name} {sql_type}" for name, sql_type in columns)
    if constraints:
        body += ", " + ", ".join(constraints)
    return f"CREATE TABLE IF NOT EXISTS {table} ({body});"


_SCHEMA = "\n".join((
    _create_table("users", _USER_COLUMNS, ("UNIQUE (username)",)),
    _create_table("api_keys", _KEY_COLUMNS, ("UNIQUE (key_hash)",)),
    _create_table("usage_records", _USAGE_COLUMNS),
    _create_table("sessions", _SESSION_COLUMNS),
    _create_table("messages", _MESSAGE_COLUMNS),
    # Key 按 hash 查（数据面每请求一次）→ UNIQUE 索引已覆盖；这里补按账号列出的索引
    "CREATE INDEX IF NOT EXISTS idx_keys_user ON api_keys(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage_records(user_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user_active ON sessions(user_id, last_active_at);",
    "CREATE INDEX IF NOT EXISTS idx_sessions_key ON sessions(user_id, key_id, model, last_active_at);",
    "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);",
))

_USER_SELECT = "SELECT " + ", ".join(_USER_COLS) + " FROM users"
_KEY_SELECT = "SELECT " + ", ".join(_KEY_COLS) + " FROM api_keys"
_USAGE_SELECT = "SELECT " + ", ".join(_USAGE_COLS) + " FROM usage_records"
_SESSION_SELECT = "SELECT " + ", ".join(_SESSION_COLS) + " FROM sessions"

#: 补列输入：表 → 列清单。旧库缺哪列补哪列（只增不删）。
_TABLE_COLUMNS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("users", _USER_COLUMNS),
    ("api_keys", _KEY_COLUMNS),
    ("usage_records", _USAGE_COLUMNS),
    ("sessions", _SESSION_COLUMNS),
    ("messages", _MESSAGE_COLUMNS),
)


def _like_pattern(keyword: str) -> str:
    """关键词 → LIKE 模式，转义 `%`/`_`/`\\` 让用户输入的通配符按字面量匹配。"""
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class AccountsStore:
    """账号台账。进程内共享一个连接（check_same_thread=False），写经锁串行化。"""

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            from modelctl.core.paths import accounts_db_path

            db_path = accounts_db_path()
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    @property
    def db_path(self) -> Path:
        """库文件路径（只读；诊断展示用）。"""
        return self._db_path

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # autocommit：与 cluster store 同口径——避免 DML 在 auto-BEGIN 与显式 commit
            # 之间抛错时残留 in_transaction；原子性由 self._lock + 显式 BEGIN IMMEDIATE 保证。
            self._conn.isolation_level = None
            # 网关与 webui 是两个进程共用同一个库文件：WAL 让读不被写阻塞，
            # busy_timeout 让偶发写锁冲突排队而不是直接抛 database is locked。
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def init_db(self) -> None:
        with self._lock:
            self._db().executescript(_SCHEMA)
            self._ensure_columns()
            self._db().commit()

    def _ensure_columns(self) -> None:
        """幂等补列：**只增不删**，绝不改/删既有列（CLAUDE.md 迁移顺序约束）。

        全新库为空操作；升级前的旧库逐列 ALTER，新列走 schema 声明的默认值
        （NOT NULL DEFAULT 由 SQLite 回填既有行）。调用方已持锁，不再取锁。
        """
        for table, columns in _TABLE_COLUMNS:
            have = {r["name"] for r in self._db().execute(f"PRAGMA table_info({table})").fetchall()}
            for name, sql_type in columns:
                if name not in have:
                    self._db().execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    # ---- row → dict ----

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
        d: dict[str, Any] = {c: row[c] for c in _USER_COLS}
        d["is_admin"] = bool(row["is_admin"])
        return d

    @staticmethod
    def _row_to_key(row: sqlite3.Row) -> dict[str, Any]:
        return {c: row[c] for c in _KEY_COLS}

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> dict[str, Any]:
        return {c: row[c] for c in _SESSION_COLS}

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
        return {c: row[c] for c in _MESSAGE_COLS}

    # ---- users ----

    def create_user(self, *, username: str, password_hash: str, display_name: str = "",
                    is_admin: bool = False, concurrency_limit: int | None = None,
                    rpm_limit: int | None = None, tpm_limit: int | None = None,
                    token_budget: int | None = None, budget_period: str = "day",
                    retention_days: int | None = None, now: float) -> int:
        """建号并返回自增 id。username 重复由 UNIQUE 抛 IntegrityError（上层转 409）。"""
        with self._lock:
            cur = self._db().execute(
                """INSERT INTO users(username, password_hash, display_name, is_admin, status,
                                      concurrency_limit, rpm_limit, tpm_limit, token_budget,
                                      budget_consumed, budget_period, budget_reset_at,
                                      retention_days, created_at, updated_at)
                   VALUES(?,?,?,?, 'active', ?,?,?,?, 0, ?,NULL,?,?,?)""",
                (username, password_hash, display_name, 1 if is_admin else 0,
                 concurrency_limit, rpm_limit, tpm_limit, token_budget, budget_period,
                 retention_days, now, now),
            )
            self._db().commit()
        return int(cur.lastrowid)

    def get_user_by_id(self, user_id: int) -> dict | None:
        with self._lock:
            row = self._db().execute(_USER_SELECT + " WHERE id=?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_username(self, username: str) -> dict | None:
        with self._lock:
            row = self._db().execute(_USER_SELECT + " WHERE username=?", (username,)).fetchone()
        return self._row_to_user(row) if row else None

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self._db().execute(_USER_SELECT + " ORDER BY id").fetchall()
        return [self._row_to_user(r) for r in rows]

    def update_user_limits(self, user_id: int, *, now: float, **fields: Any) -> dict | None:
        """按白名单改账号（限额/展示名/管理员标记/状态）；未知字段静默忽略。

        返回更新后的整行，账号不存在返回 None。`is_admin` 传 bool 时转 INTEGER。
        """
        sets, params = [], []
        for key, value in fields.items():
            if key not in _USER_MUTABLE:
                continue
            sets.append(f"{key}=?")
            params.append(int(value) if key == "is_admin" and isinstance(value, bool) else value)
        if not sets:
            return self.get_user_by_id(user_id)
        sets.append("updated_at=?")
        params.extend([now, user_id])
        with self._lock:
            cur = self._db().execute(
                f"UPDATE users SET {', '.join(sets)} WHERE id=?", params)
            self._db().commit()
        return self.get_user_by_id(user_id) if cur.rowcount else None

    def set_user_status(self, user_id: int, status: str, *, now: float) -> bool:
        if status not in USER_STATUSES:
            raise ValueError(f"非法账号状态: {status!r}，允许值 {USER_STATUSES}")
        with self._lock:
            cur = self._db().execute("UPDATE users SET status=?, updated_at=? WHERE id=?",
                                     (status, now, user_id))
            self._db().commit()
        return cur.rowcount > 0

    def set_user_password_hash(self, user_id: int, password_hash: str, *, now: float) -> bool:
        with self._lock:
            cur = self._db().execute(
                "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                (password_hash, now, user_id))
            self._db().commit()
        return cur.rowcount > 0

    def increment_budget_consumed(self, user_id: int, tokens: int, *, now: float) -> None:
        """预算消费累加（异步记账路径调用；token 为本次请求 total_tokens）。"""
        with self._lock:
            self._db().execute(
                "UPDATE users SET budget_consumed = budget_consumed + ?, updated_at=? WHERE id=?",
                (max(0, int(tokens)), now, user_id))
            self._db().commit()

    def reset_budget(self, user_id: int, *, next_reset_at: float, now: float) -> None:
        """清零消费并写入下次重置时刻（惰性重置由 `limits` 判定后调用本方法持久化）。"""
        with self._lock:
            self._db().execute(
                "UPDATE users SET budget_consumed=0, budget_reset_at=?, updated_at=? WHERE id=?",
                (next_reset_at, now, user_id))
            self._db().commit()

    def delete_user(self, user_id: int) -> dict | None:
        """删号并级联清理 keys / usage / sessions / messages（单事务）。

        会话消息属隐私数据，账号删除即彻底清除（spec §6.3）；不做软删是因为
        "已删除账号的对话仍在库里"与 GDPR 式自助删除诉求直接冲突。
        """
        snapshot = self.get_user_by_id(user_id)
        if snapshot is None:
            return None
        with self._lock:
            conn = self._db()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """DELETE FROM messages WHERE session_id IN
                       (SELECT id FROM sessions WHERE user_id=?)""", (user_id,))
                conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM usage_records WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM api_keys WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM users WHERE id=?", (user_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.rollback()
                raise
        return snapshot

    # ---- api_keys ----

    def create_key(self, *, user_id: int, key_hash: str, key_prefix: str, name: str = "",
                   expires_at: float | None = None, now: float) -> int:
        """签发 Key（只存 sha256 摘要 + 脱敏前缀）；返回自增 id。

        明文 Key 只在生成的那一次由上层返回给用户，库里**永不落地**——所以本方法
        不接收明文，签名层面就不可能误存。
        """
        with self._lock:
            cur = self._db().execute(
                """INSERT INTO api_keys(user_id, key_prefix, key_hash, name, status,
                                        expires_at, last_used_at, created_at)
                   VALUES(?,?,?,?, 'active', ?,NULL,?)""",
                (user_id, key_prefix, key_hash, name, expires_at, now),
            )
            self._db().commit()
        return int(cur.lastrowid)

    def get_key_by_hash(self, key_hash: str) -> dict | None:
        with self._lock:
            row = self._db().execute(_KEY_SELECT + " WHERE key_hash=?", (key_hash,)).fetchone()
        return self._row_to_key(row) if row else None

    def get_key_by_id(self, key_id: int) -> dict | None:
        with self._lock:
            row = self._db().execute(_KEY_SELECT + " WHERE id=?", (key_id,)).fetchone()
        return self._row_to_key(row) if row else None

    def list_keys_for_user(self, user_id: int) -> list[dict]:
        with self._lock:
            rows = self._db().execute(
                _KEY_SELECT + " WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
        return [self._row_to_key(r) for r in rows]

    def set_key_status(self, key_id: int, status: str, *, now: float) -> bool:
        if status not in KEY_STATUSES:
            raise ValueError(f"非法 Key 状态: {status!r}，允许值 {KEY_STATUSES}")
        with self._lock:
            cur = self._db().execute("UPDATE api_keys SET status=? WHERE id=?", (status, key_id))
            self._db().commit()
        return cur.rowcount > 0

    def touch_key_last_used(self, key_id: int, *, now: float) -> None:
        with self._lock:
            self._db().execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now, key_id))
            self._db().commit()

    # ---- usage_records ----

    def insert_usage(self, *, user_id: int, key_id: int | None, request_id: str, model: str,
                     prompt_tokens: int, completion_tokens: int, client_ip: str,
                     ttft_ms: int | None, latency_ms: int | None, status: str,
                     now: float) -> int:
        p, c = max(0, int(prompt_tokens)), max(0, int(completion_tokens))
        with self._lock:
            cur = self._db().execute(
                """INSERT INTO usage_records(user_id, key_id, request_id, model, prompt_tokens,
                                             completion_tokens, total_tokens, client_ip, ttft_ms,
                                             latency_ms, status, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, key_id, request_id, model, p, c, p + c, client_ip, ttft_ms,
                 latency_ms, status, now),
            )
            self._db().commit()
        return int(cur.lastrowid)

    def sum_usage_for_user(self, user_id: int, since: float | None = None) -> dict[str, int]:
        """按账号累计用量（`since` 非空则只统计该时刻之后，用于"本预算周期"口径）。"""
        sql = ("SELECT COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
               "COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
               "COALESCE(SUM(total_tokens),0) AS total_tokens FROM usage_records WHERE user_id=?")
        params: list[Any] = [user_id]
        if since is not None:
            sql += " AND created_at>=?"
            params.append(since)
        with self._lock:
            row = self._db().execute(sql, params).fetchone()
        return {k: int(row[k]) for k in
                ("requests", "prompt_tokens", "completion_tokens", "total_tokens")}

    def list_usage_for_user(self, user_id: int, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db().execute(
                _USAGE_SELECT + " WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (user_id, limit)).fetchall()
        return [{c: r[c] for c in _USAGE_COLS} for r in rows]

    # ---- sessions / messages ----

    def get_or_create_session(self, *, user_id: int, key_id: int | None, model: str,
                              session_id: str | None = None, title: str = "",
                              now: float, idle_ttl_s: int = SESSION_IDLE_TTL_S) -> dict:
        """定位或新建会话，返回会话行。

        - 显式 `session_id`（客户端 `X-Session-Id`）优先：命中条件是
          `session_key=? AND user_id=?` —— **user_id 必须在 SQL 里**。只按 session_key
          查会让 A 传的 id 命中的会话被写进 B 的库（会话劫持 + 数据泄露）。
        - 无显式 id 时按 (user_id, key_id, model) 的空闲窗口聚合（spec §6.1）。
        """
        clip_title = (title or "")[:TITLE_MAX_LEN]
        with self._lock:
            conn = self._db()
            row = None
            if session_id:
                row = conn.execute(_SESSION_SELECT +
                                   " WHERE session_key=? AND user_id=?",
                                   (session_id, user_id)).fetchone()
            else:
                row = conn.execute(
                    _SESSION_SELECT +
                    " WHERE user_id=? AND key_id IS ? AND model=? AND last_active_at>=?" +
                    " ORDER BY last_active_at DESC, id DESC LIMIT 1",
                    (user_id, key_id, model, now - idle_ttl_s)).fetchone()
            if row is not None:
                conn.execute("UPDATE sessions SET last_active_at=? WHERE id=?",
                             (now, row["id"]))
                conn.commit()
                return self._row_to_session(conn.execute(
                    _SESSION_SELECT + " WHERE id=?", (row["id"],)).fetchone())
            session_key = session_id or f"s-{secrets.token_hex(8)}"
            cur = conn.execute(
                """INSERT INTO sessions(user_id, key_id, model, session_key, title,
                                        message_count, created_at, last_active_at)
                   VALUES(?,?,?,?,?, 0, ?, ?)""",
                (user_id, key_id, model, session_key, clip_title, now, now))
            conn.commit()
            return self._row_to_session(conn.execute(
                _SESSION_SELECT + " WHERE id=?", (int(cur.lastrowid),)).fetchone())

    def bump_session(self, session_id: int, *, now: float) -> None:
        """消息落库后递增计数并刷新活跃时刻（与 add_message 成对调用）。"""
        with self._lock:
            self._db().execute(
                "UPDATE sessions SET message_count = message_count + 1, last_active_at=? WHERE id=?",
                (now, session_id))
            self._db().commit()

    def add_message(self, *, session_id: int, role: str, content: str, now: float,
                    prompt_tokens: int = 0, completion_tokens: int = 0) -> int:
        with self._lock:
            cur = self._db().execute(
                """INSERT INTO messages(session_id, role, content, prompt_tokens,
                                        completion_tokens, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (session_id, role, content, max(0, int(prompt_tokens)),
                 max(0, int(completion_tokens)), now))
            self._db().commit()
        return int(cur.lastrowid)

    def get_session(self, session_id: int, *, user_id: int) -> dict | None:
        """单条会话；**归属不符即 None**（自助面板越权访问的兜底）。"""
        with self._lock:
            row = self._db().execute(_SESSION_SELECT + " WHERE id=? AND user_id=?",
                                     (session_id, user_id)).fetchone()
        return self._row_to_session(row) if row else None

    def list_sessions(self, user_id: int, q: str = "", limit: int = 200) -> list[dict]:
        """会话列表（活跃倒序）；`q` 命中标题**或消息内容**。"""
        sql, params = _SESSION_SELECT + " WHERE user_id=?", [user_id]
        if q:
            pattern = _like_pattern(q)
            sql += (""" AND (title LIKE ? ESCAPE '\\' OR EXISTS (
                          SELECT 1 FROM messages m
                          WHERE m.session_id = sessions.id AND m.content LIKE ? ESCAPE '\\'))""")
            params.extend([pattern, pattern])
        sql += " ORDER BY last_active_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._db().execute(sql, params).fetchall()
        return [self._row_to_session(r) for r in rows]

    def get_messages(self, session_id: int) -> list[dict]:
        with self._lock:
            rows = self._db().execute(
                "SELECT " + ", ".join(_MESSAGE_COLS) +
                " FROM messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        return [self._row_to_message(r) for r in rows]

    def export_session(self, session_id: int, *, user_id: int) -> dict | None:
        """导出会话：会话元信息 + 全部消息（按时间正序）。归属不符返回 None。"""
        session = self.get_session(session_id, user_id=user_id)
        if session is None:
            return None
        return {"session": session, "messages": self.get_messages(session_id)}

    def search_messages(self, user_id: int, q: str, limit: int = 100) -> list[dict]:
        """按消息内容搜索，命中项带会话上下文（标题/模型/会话 id）。"""
        if not q:
            return []
        with self._lock:
            rows = self._db().execute(
                """SELECT messages.id AS id, messages.session_id AS session_id,
                          messages.role AS role, messages.content AS content,
                          messages.created_at AS created_at, sessions.title AS title,
                          sessions.model AS model
                   FROM messages JOIN sessions ON sessions.id = messages.session_id
                   WHERE sessions.user_id=? AND messages.content LIKE ? ESCAPE '\\'
                   ORDER BY messages.id DESC LIMIT ?""",
                (user_id, _like_pattern(q), limit)).fetchall()
        return [{"id": r["id"], "session_id": r["session_id"], "role": r["role"],
                 "content": r["content"], "created_at": r["created_at"],
                 "title": r["title"], "model": r["model"]} for r in rows]

    def delete_session(self, session_id: int, *, user_id: int) -> bool:
        """删除会话及其消息（单事务）。归属不符或不存在返回 False（幂等）。"""
        with self._lock:
            conn = self._db()
            owned = conn.execute("SELECT id FROM sessions WHERE id=? AND user_id=?",
                                 (session_id, user_id)).fetchone()
            if owned is None:
                return False
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.rollback()
                raise
        return True
