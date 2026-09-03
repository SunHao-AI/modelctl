#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/store.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : 集群中心 SQLite 台账（设计文档 §11.1）
# ===============================================================================

"""core/cluster/store.py — 中心单文件 SQLite 台账（stdlib sqlite3，无外部中间件）。

时间戳统一 epoch float（REAL）：ISO 字符串在时区切换下比较不可靠（计划全局约束，
对 spec §11.1 的有意偏离）。goals/model_states/metrics_rollups 等表 M1/M2 才写入，
一次建齐避免迁移。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_MASK_KEEP_TAIL = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (
  node_id TEXT PRIMARY KEY, node_token TEXT NOT NULL, lan_id TEXT, role TEXT NOT NULL DEFAULT 'worker',
  host_ip TEXT, hostname TEXT, engines TEXT, created_at REAL, last_seen REAL, lease_expiry REAL,
  status TEXT NOT NULL DEFAULT 'offline', disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS goals (
  goal_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, profile TEXT NOT NULL, engine TEXT NOT NULL,
  profile_yaml TEXT NOT NULL, profile_sha TEXT NOT NULL, profile_version TEXT,
  intent TEXT NOT NULL DEFAULT 'start', params TEXT, env_overlay TEXT, placement TEXT,
  runtime_ref TEXT, target_role TEXT, traffic_weight INTEGER DEFAULT 0,
  stage TEXT NOT NULL DEFAULT 'PENDING_PROFILE_SYNC', stage_reason TEXT, error_class TEXT,
  created_by TEXT, created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS model_states (
  node_id TEXT NOT NULL, profile TEXT NOT NULL, state TEXT NOT NULL, gpu TEXT, port INTEGER,
  pid INTEGER, reason TEXT, endpoint_url TEXT, endpoint_ready INTEGER, engine_version TEXT,
  gpu_util INTEGER, metrics_p50_ms INTEGER, last_probe_ms INTEGER, error_class TEXT, updated_at REAL,
  PRIMARY KEY (node_id, profile)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, node_id TEXT, goal_id TEXT,
  kind TEXT NOT NULL, payload TEXT
);
CREATE TABLE IF NOT EXISTS metrics_rollups (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, node_id TEXT NOT NULL, profile TEXT,
  window_s INTEGER NOT NULL, requests INTEGER DEFAULT 0, errors_4xx INTEGER DEFAULT 0,
  errors_5xx INTEGER DEFAULT 0, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
  latency_p50_ms INTEGER, latency_p95_ms INTEGER, tps REAL
);
CREATE TABLE IF NOT EXISTS token_ops (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, op TEXT, node_id TEXT, operator TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, operator TEXT, node_id TEXT, goal_id TEXT,
  action TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_node ON events(node_id, ts);
"""

_NODE_COLS = ("node_id", "node_token", "lan_id", "role", "host_ip", "hostname",
              "engines", "created_at", "last_seen", "lease_expiry", "status", "disabled")


def mask_tail(value: str) -> str:
    """密钥脱敏：*** + 末 4 位；空值/短值 → ***（与 admin_auth.mask_key 同口径）。"""
    if not value or len(value) <= _MASK_KEEP_TAIL:
        return "***"
    return "***" + value[-_MASK_KEEP_TAIL:]


class ClusterStore:
    """中心台账。进程内共享一个连接（check_same_thread=False），写经锁串行化。"""

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            from modelctl.core.process import cache_dir

            db_path = cache_dir() / "cluster-meta.db"
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    @property
    def db_path(self) -> Path:
        """台账文件路径（只读；CLI 展示用）。"""
        return self._db_path

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def init_db(self) -> None:
        with self._lock:
            self._db().executescript(_SCHEMA)
            self._db().commit()

    # ---- meta ----
    def get_meta(self, key: str) -> str:
        with self._lock:
            row = self._db().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db().execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._db().commit()

    # ---- nodes ----
    def _row_to_node(self, row: sqlite3.Row) -> dict[str, Any]:
        d: dict[str, Any] = {c: row[c] for c in _NODE_COLS}
        d["engines"] = json.loads(row["engines"]) if row["engines"] else None
        return d

    def upsert_node(self, *, node_id: str, node_token: str, lan_id: str, role: str,
                    host_ip: str, hostname: str, engines: dict | None, now: float) -> str:
        """注册/重注册节点；返回 joined（新）/rejoined（已存在）。engines=None 不覆盖既有值。"""
        with self._lock:
            existing = self._db().execute("SELECT engines FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            result = "rejoined" if existing else "joined"
            merged = engines
            if existing and existing["engines"] and engines is None:
                merged = json.loads(existing["engines"])
            self._db().execute(
                """INSERT INTO nodes(node_id,node_token,lan_id,role,host_ip,hostname,engines,created_at,
                                     last_seen,status)
                   VALUES(?,?,?,?,?,?,?,?,?,'online')
                   ON CONFLICT(node_id) DO UPDATE SET
                     node_token=excluded.node_token, lan_id=excluded.lan_id, role=excluded.role,
                     host_ip=excluded.host_ip, hostname=excluded.hostname, engines=excluded.engines,
                     last_seen=excluded.last_seen, status='online'""",
                (node_id, node_token, lan_id, role, host_ip, hostname,
                 json.dumps(merged) if merged is not None else None, now, now),
            )
            self._db().commit()
        return result

    def get_node(self, node_id: str) -> dict | None:
        with self._lock:
            row = self._db().execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        return self._row_to_node(row) if row else None

    def find_node_by_token(self, token: str) -> dict | None:
        if not token:
            return None
        with self._lock:
            row = self._db().execute("SELECT * FROM nodes WHERE node_token=?", (token,)).fetchone()
        return self._row_to_node(row) if row else None

    def list_nodes(self) -> list[dict]:
        with self._lock:
            rows = self._db().execute("SELECT * FROM nodes ORDER BY node_id").fetchall()
        return [self._row_to_node(r) for r in rows]

    def touch_heartbeat(self, node_id: str, now: float, lease_s: int) -> None:
        with self._lock:
            self._db().execute(
                "UPDATE nodes SET last_seen=?, lease_expiry=?, status='online' WHERE node_id=?",
                (now, now + lease_s, node_id),
            )
            self._db().commit()

    def set_node_status(self, node_id: str, status: str) -> None:
        with self._lock:
            self._db().execute("UPDATE nodes SET status=? WHERE node_id=?", (status, node_id))
            self._db().commit()

    def rotate_node_token(self, node_id: str) -> str | None:
        from modelctl.core.cluster.tokens import new_node_token

        token = new_node_token()
        with self._lock:
            cur = self._db().execute("UPDATE nodes SET node_token=? WHERE node_id=?", (token, node_id))
            self._db().commit()
        return token if cur.rowcount else None

    def sweep_expired(self, now: float, lease_s: int) -> list[tuple[str, str]]:
        """lease 过期→stale；last_seen 超 3×lease→offline。仅返回本次发生迁移的节点。"""
        transitions: list[tuple[str, str]] = []
        with self._lock:
            rows = self._db().execute(
                "SELECT node_id,status,last_seen,lease_expiry FROM nodes WHERE disabled=0"
            ).fetchall()
            for r in rows:
                cur_status = r["status"]
                if cur_status in ("offline", "disabled"):
                    continue
                new_status: str | None = None
                last_seen = r["last_seen"]
                if last_seen is not None and last_seen + 3 * lease_s < now:
                    new_status = "offline"
                elif r["lease_expiry"] is not None and r["lease_expiry"] < now:
                    new_status = "stale"
                if new_status and new_status != cur_status:
                    self._db().execute("UPDATE nodes SET status=? WHERE node_id=?",
                                       (new_status, r["node_id"]))
                    transitions.append((r["node_id"], new_status))
            self._db().commit()
        return transitions

    # ---- events ----
    def append_event(self, kind: str, *, node_id: str | None = None, goal_id: str | None = None,
                     payload: dict | None = None, now: float | None = None) -> None:
        with self._lock:
            self._db().execute(
                "INSERT INTO events(ts,node_id,goal_id,kind,payload) VALUES(?,?,?,?,?)",
                (now if now is not None else time.time(), node_id, goal_id, kind,
                 json.dumps(payload, ensure_ascii=False) if payload else None),
            )
            self._db().commit()

    def recent_events(self, limit: int = 100, node_id: str | None = None) -> list[dict]:
        sql = "SELECT ts,node_id,goal_id,kind,payload FROM events"
        params: list[Any] = []
        if node_id:
            sql += " WHERE node_id=?"
            params.append(node_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._db().execute(sql, params).fetchall()
        return [{"ts": r["ts"], "node_id": r["node_id"], "goal_id": r["goal_id"], "kind": r["kind"],
                 "payload": json.loads(r["payload"]) if r["payload"] else None} for r in rows]
