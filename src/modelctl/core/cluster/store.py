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

from loguru import logger

_MASK_KEEP_TAIL = 4

#: set_node_status 允许的状态白名单，防止调用方 typo 污染台账状态机
NODE_STATUSES = ("online", "stale", "offline", "disabled")

# events 台账保留策略（纯 DML 裁剪，不改 schema）：中心节点事件只进不出会让
# 单文件 SQLite 无界膨胀 + 备份变慢。保留 30 天；每 500 次 append 顺带清一次
# （摊销成本，避免每次写都扫表）。
EVENTS_RETENTION_DAYS = 30
EVENTS_TRIM_EVERY = 500

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
  node_id TEXT NOT NULL, profile TEXT NOT NULL, engine TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL, gpu TEXT, port INTEGER,
  pid INTEGER, reason TEXT, endpoint_url TEXT, endpoint_ready INTEGER, engine_version TEXT,
  gpu_util INTEGER, metrics_p50_ms INTEGER, last_probe_ms INTEGER, error_class TEXT, updated_at REAL,
  PRIMARY KEY (node_id, profile, engine)
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

GOAL_JSON_FIELDS: tuple[str, ...] = ("params", "env_overlay", "placement")

#: M1 新增列（改进 B）；只增不删，旧库经 _ensure_columns 幂等补齐
_NODE_M1_COLUMNS: tuple[tuple[str, str], ...] = (
    ("capacity_json", "TEXT"),
    ("runtime_json", "TEXT"),
    ("gateway_url", "TEXT"),
    ("last_goal_sync_sha", "TEXT"),
    ("local_profiles_json", "TEXT"),
)

_NODE_COLS = ("node_id", "node_token", "lan_id", "role", "host_ip", "hostname",
              "engines", "created_at", "last_seen", "lease_expiry", "status", "disabled",
              "capacity_json", "runtime_json", "gateway_url", "last_goal_sync_sha",
              "local_profiles_json")

# T1-④（终审）：三类读路径零 SELECT *——列清单与 _SCHEMA 逐列一致（护栏：
# tests/test_cluster_store.py::test_explicit_columns_cover_full_row）。加列必须同步
# 这里的常量，漏列会静默丢字段（视图/CLI 显示空值而非报错）。
_GOAL_COLS = ("goal_id", "node_id", "profile", "engine", "profile_yaml", "profile_sha",
              "profile_version", "intent", "params", "env_overlay", "placement",
              "runtime_ref", "target_role", "traffic_weight", "stage", "stage_reason",
              "error_class", "created_by", "created_at", "updated_at")

_MODEL_STATE_COLS = ("node_id", "profile", "engine", "state", "gpu", "port", "pid", "reason",
                     "endpoint_url", "endpoint_ready", "engine_version", "gpu_util",
                     "metrics_p50_ms", "last_probe_ms", "error_class", "updated_at")

_NODE_SELECT = "SELECT " + ", ".join(_NODE_COLS) + " FROM nodes"
_GOAL_SELECT = "SELECT " + ", ".join(_GOAL_COLS) + " FROM goals"
_MODEL_STATE_SELECT = "SELECT " + ", ".join(_MODEL_STATE_COLS) + " FROM model_states"


def mask_tail(value: str) -> str:
    """密钥脱敏：*** + 末 4 位；空值或长度 ≤4 的短值一律返回 ***。"""
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
        # append_event 计数（每 EVENTS_TRIM_EVERY 次触发一次 events 保留裁剪）
        self._append_count = 0

    @property
    def db_path(self) -> Path:
        """台账文件路径（只读；CLI 展示用）。"""
        return self._db_path

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # autocommit：禁用 LEGACY 隐式 BEGIN，避免 DML 在 auto-BEGIN 与 commit 之间
            # 抛错（如跨进程 database is locked）残留 in_transaction，导致 sweep_expired
            # 的显式 BEGIN IMMEDIATE 抛 "cannot start a transaction within a transaction"。
            # 各写方法的 commit() 在无活动事务时是 no-op；原子性由 self._lock +
            # sweep_expired 的显式 BEGIN IMMEDIATE/COMMIT/ROLLBACK 保证。
            self._conn.isolation_level = None
            # CLI 与常驻 center 进程可能共库：WAL 提升并发读写，busy_timeout 缓解锁冲突
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
        return self._conn

    def init_db(self) -> None:
        with self._lock:
            self._db().executescript(_SCHEMA)
            self._ensure_columns()
            self._db().commit()

    def _ensure_columns(self) -> None:
        """幂等补列（M1 起 nodes 需要；M2 起 model_states.engine 需要）。

        **只增不删**，绝不改/删既有列。全新库为空操作；旧库（M0 早期版本）逐列 ALTER。
        调用方已持锁，不再取锁。

        engine 列加入后，`model_states` 的联合主键从 `(node_id, profile)` 变为
        `(node_id, profile, engine)`——旧库需 DROP PRIMARY KEY 重建 PK（详见
        `_migrate_model_states_pk`，幂等）。同 stem 多引擎共存（vllm + aphrodite
        同指 qwen2.5-1.5b）时，旧 PK 会让两行互相覆盖，goal 视图端口/显存漂移
        （known-pitfalls backend/stem-ledger-collision.md）。
        """
        have = {r["name"] for r in self._db().execute("PRAGMA table_info(nodes)").fetchall()}
        for name, sql_type in _NODE_M1_COLUMNS:
            if name not in have:
                self._db().execute(f"ALTER TABLE nodes ADD COLUMN {name} {sql_type}")
        self._ensure_model_state_engine()
        self._migrate_model_states_pk()

    def _ensure_model_state_engine(self) -> None:
        """model_states 补 engine 列（幂等，旧库 M1 之前没有该列）。"""
        have = {r["name"] for r in self._db().execute("PRAGMA table_info(model_states)").fetchall()}
        if "engine" not in have:
            self._db().execute("ALTER TABLE model_states ADD COLUMN engine TEXT NOT NULL DEFAULT ''")

    def _migrate_model_states_pk(self) -> None:
        """model_states 主键 `(node_id, profile)` → `(node_id, profile, engine)`。

        SQLite 的 ALTER TABLE 不支持 DROP/改 PK；标准做法是"建临时副本 → 拷数据
        （去重同 stem 多行）→ 删旧表 → 重命名"。幂等：已经含 engine PK 的库直接返回。
        """
        cols = {r["pk"]: r for r in self._db().execute("PRAGMA table_info(model_states)").fetchall()}
        pk_cols = tuple(cols[i]["name"] for i in range(1, len(cols) + 1) if i in cols and cols[i]["pk"])
        if pk_cols == ("node_id", "profile", "engine"):
            return
        self._db().execute("PRAGMA foreign_keys=OFF;")
        try:
            self._db().execute(
                """CREATE TABLE model_states_new (
                     node_id TEXT NOT NULL, profile TEXT NOT NULL, engine TEXT NOT NULL DEFAULT '',
                     state TEXT NOT NULL, gpu TEXT, port INTEGER,
                     pid INTEGER, reason TEXT, endpoint_url TEXT, endpoint_ready INTEGER,
                     engine_version TEXT, gpu_util INTEGER, metrics_p50_ms INTEGER,
                     last_probe_ms INTEGER, error_class TEXT, updated_at REAL,
                     PRIMARY KEY (node_id, profile, engine)
                   )""")
            # 同 stem 多行（旧 PK 下 engine 全为 ''）只保留最新 updated_at 一行
            self._db().execute(
                """INSERT INTO model_states_new(node_id, profile, engine, state, gpu, port,
                                                pid, reason, endpoint_url, endpoint_ready,
                                                engine_version, gpu_util, metrics_p50_ms,
                                                last_probe_ms, error_class, updated_at)
                   SELECT node_id, profile, COALESCE(engine, ''), state, gpu, port,
                          pid, reason, endpoint_url, endpoint_ready,
                          engine_version, gpu_util, metrics_p50_ms,
                          last_probe_ms, error_class, updated_at
                   FROM model_states
                   WHERE (node_id, profile, updated_at) IN (
                       SELECT node_id, profile, MAX(updated_at) FROM model_states GROUP BY node_id, profile
                   )""")
            # 补回同 (node_id, profile) 但不同 engine 的行（理论上旧库不存在，防御性）
            self._db().execute(
                """INSERT OR IGNORE INTO model_states_new
                   (node_id, profile, engine, state, gpu, port, pid, reason,
                    endpoint_url, endpoint_ready, engine_version, gpu_util,
                    metrics_p50_ms, last_probe_ms, error_class, updated_at)
                   SELECT node_id, profile, COALESCE(engine, ''), state, gpu, port,
                          pid, reason, endpoint_url, endpoint_ready,
                          engine_version, gpu_util, metrics_p50_ms,
                          last_probe_ms, error_class, updated_at FROM model_states""")
            self._db().execute("DROP TABLE model_states")
            self._db().execute("ALTER TABLE model_states_new RENAME TO model_states")
        finally:
            self._db().execute("PRAGMA foreign_keys=ON;")

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
        d["capacity"] = json.loads(row["capacity_json"]) if row["capacity_json"] else None
        d["runtimes"] = json.loads(row["runtime_json"]) if row["runtime_json"] else None
        d["local_profiles"] = (json.loads(row["local_profiles_json"])
                               if row["local_profiles_json"] else [])
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
                     last_seen=excluded.last_seen, status='online', lease_expiry=NULL""",
                (node_id, node_token, lan_id, role, host_ip, hostname,
                 json.dumps(merged, ensure_ascii=False) if merged is not None else None, now, now),
            )
            self._db().commit()
        return result

    def get_node(self, node_id: str) -> dict | None:
        with self._lock:
            row = self._db().execute(_NODE_SELECT + " WHERE node_id=?", (node_id,)).fetchone()
        return self._row_to_node(row) if row else None

    def find_node_by_token(self, token: str) -> dict | None:
        if not token:
            return None
        with self._lock:
            row = self._db().execute(_NODE_SELECT + " WHERE node_token=?", (token,)).fetchone()
        return self._row_to_node(row) if row else None

    def list_nodes(self) -> list[dict]:
        with self._lock:
            rows = self._db().execute(_NODE_SELECT + " ORDER BY node_id").fetchall()
        return [self._row_to_node(r) for r in rows]

    def touch_heartbeat(self, node_id: str, now: float, lease_s: int) -> None:
        with self._lock:
            self._db().execute(
                "UPDATE nodes SET last_seen=?, lease_expiry=?, status='online' WHERE node_id=?",
                (now, now + lease_s, node_id),
            )
            self._db().commit()

    def set_node_status(self, node_id: str, status: str) -> None:
        if status not in NODE_STATUSES:
            raise ValueError(f"非法节点状态: {status!r}，允许值 {NODE_STATUSES}")
        with self._lock:
            self._db().execute("UPDATE nodes SET status=? WHERE node_id=?", (status, node_id))
            self._db().commit()

    def set_node_disabled(self, node_id: str, disabled: bool, *, status: str = "") -> bool:
        """置/清 disabled；status 非空时同语句连带改写（禁用传 "disabled"）。

        两条 UPDATE 分支而非一条拼串：status 可选出现在 f-string 里会把"传了非法
        status"这类调用方错误伪装成 SQL 注入面，显式分支让 SQL 文本恒封闭。
        """
        if status and status not in NODE_STATUSES:
            raise ValueError(f"非法节点状态: {status!r}，允许值 {NODE_STATUSES}")
        with self._lock:
            if status:
                cur = self._db().execute(
                    "UPDATE nodes SET disabled=?, status=? WHERE node_id=?",
                    (1 if disabled else 0, status, node_id))
            else:
                cur = self._db().execute("UPDATE nodes SET disabled=? WHERE node_id=?",
                                         (1 if disabled else 0, node_id))
            self._db().commit()
        return cur.rowcount > 0

    def delete_node_cascade(self, node_id: str) -> int:
        """退役级联：nodes + goals + model_states 三表**单事务**删除，返回连带 goals 数。

        events 刻意不删（审计留痕）。原子性是本方法契约——"节点删了但它的 goal 还
        在快照里"是毒台账：心跳风暴、gate 幽灵候选、前端死链三连。
        """
        with self._lock:
            conn = self._db()
            conn.execute("BEGIN IMMEDIATE")
            try:
                n = conn.execute("SELECT COUNT(*) AS c FROM goals WHERE node_id=?",
                                 (node_id,)).fetchone()["c"]
                cur = conn.execute("DELETE FROM nodes WHERE node_id=?", (node_id,))
                conn.execute("DELETE FROM goals WHERE node_id=?", (node_id,))
                conn.execute("DELETE FROM model_states WHERE node_id=?", (node_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.rollback()
                raise
        return n if cur.rowcount else 0

    def retire_node(self, node_id: str, *, append_event_fn=None) -> dict | None:
        """退役节点：级联删除 + `node.retire` 事件；节点不存在返回 None 且不记事件。

        事件经 `append_event_fn` 回调注入（而非内部硬调 self.append_event）：
        restore 场景需要决定事件落"恢复后的新库"，接口缝在此留好（Task 5 消费）。
        存在性必须在级联删除**前**判定——delete_node_cascade 对不存在节点同样返回 0，
        删后再查无法区分"退役了个空节点"与"节点本就不存在"。
        """
        if self.get_node(node_id) is None:
            return None
        removed = self.delete_node_cascade(node_id)
        if append_event_fn is not None:
            append_event_fn("node.retire", node_id=node_id,
                            payload={"removed_goals": removed, "operator": "api"})
        return {"removed_goals": removed}

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
            conn = self._db()
            # BEGIN IMMEDIATE：读全表 + 逐条改写必须是原子单元，且避免跨进程写锁冲突
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
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
                        conn.execute("UPDATE nodes SET status=? WHERE node_id=?",
                                     (new_status, r["node_id"]))
                        transitions.append((r["node_id"], new_status))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return transitions

    # ---- nodes 容量/运行时/本机 profile 清单（改进 B；None 不覆盖既有值，与 engines 同语义）----
    # 无 now 形参：本方法不写任何时间列（capacity_updated_at 属 DDL 演进，M1 未加列），
    # 心跳时间归 touch_heartbeat 单责——保留死参会诱导调用方以为它影响时间语义。
    def update_node_capacity(self, node_id: str, *, capacity: dict | None,
                             runtimes: dict | None,
                             local_profiles: list[str] | None = None) -> None:
        with self._lock:
            row = self._db().execute(
                "SELECT capacity_json, runtime_json, local_profiles_json FROM nodes WHERE node_id=?",
                (node_id,)).fetchone()
            if row is None:
                return
            merged_cap = capacity if capacity is not None else (
                json.loads(row["capacity_json"]) if row["capacity_json"] else None)
            merged_rt = runtimes if runtimes is not None else (
                json.loads(row["runtime_json"]) if row["runtime_json"] else None)
            # local_profiles 用 None 判定而非真值：worker 本机清空 profile 时是 []，
            # 必须能覆盖旧值（否则 gate 会一直以为 profile 还在）
            merged_lp = local_profiles if local_profiles is not None else (
                json.loads(row["local_profiles_json"]) if row["local_profiles_json"] else [])
            self._db().execute(
                "UPDATE nodes SET capacity_json=?, runtime_json=?, local_profiles_json=? WHERE node_id=?",
                (json.dumps(merged_cap, ensure_ascii=False) if merged_cap is not None else None,
                 json.dumps(merged_rt, ensure_ascii=False) if merged_rt is not None else None,
                 json.dumps(merged_lp, ensure_ascii=False),
                 node_id))
            self._db().commit()

    def set_node_last_goal_sync_sha(self, node_id: str, sha: str) -> None:
        with self._lock:
            self._db().execute("UPDATE nodes SET last_goal_sync_sha=? WHERE node_id=?", (sha, node_id))
            self._db().commit()

    # ---- goals（source of truth；created_* 只在首次插入生效）----
    def _row_to_goal(self, row: sqlite3.Row) -> dict[str, Any]:
        d = {k: row[k] for k in row.keys()}
        for f in GOAL_JSON_FIELDS:
            d[f] = json.loads(row[f]) if row[f] else None
        return d

    def upsert_goal(self, *, goal_id: str, node_id: str, profile: str, engine: str,
                    profile_yaml: str, profile_sha: str, profile_version: str | None,
                    intent: str, params: dict | None, env_overlay: dict | None,
                    placement: dict | None, runtime_ref: str | None, target_role: str,
                    stage: str, created_by: str, now: float) -> None:
        def j(v: dict | None) -> str | None:
            return json.dumps(v, ensure_ascii=False) if v is not None else None

        with self._lock:
            self._db().execute(
                """INSERT INTO goals(goal_id,node_id,profile,engine,profile_yaml,profile_sha,profile_version,
                                      intent,params,env_overlay,placement,runtime_ref,target_role,traffic_weight,
                                      stage,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(goal_id) DO UPDATE SET
                     node_id=excluded.node_id, profile=excluded.profile, engine=excluded.engine,
                     profile_yaml=excluded.profile_yaml, profile_sha=excluded.profile_sha,
                     profile_version=excluded.profile_version, intent=excluded.intent,
                     params=excluded.params, env_overlay=excluded.env_overlay,
                     placement=excluded.placement, runtime_ref=excluded.runtime_ref,
                     target_role=excluded.target_role, stage='PENDING_PROFILE_SYNC',
                     stage_reason=NULL, error_class=NULL, updated_at=excluded.updated_at""",
                (goal_id, node_id, profile, engine, profile_yaml, profile_sha, profile_version,
                 intent, j(params), j(env_overlay), j(placement), runtime_ref, target_role, 0,
                 stage, created_by, now, now))
            self._db().commit()

    def get_goal(self, goal_id: str) -> dict | None:
        with self._lock:
            row = self._db().execute(_GOAL_SELECT + " WHERE goal_id=?", (goal_id,)).fetchone()
        return self._row_to_goal(row) if row else None

    def list_goals(self, *, node_id: str = "", profile: str = "") -> list[dict]:
        sql, params, conds = _GOAL_SELECT, [], []
        if node_id:
            conds.append("node_id=?")
            params.append(node_id)
        if profile:
            conds.append("profile=?")
            params.append(profile)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY node_id, profile"
        with self._lock:
            return [self._row_to_goal(r) for r in self._db().execute(sql, params).fetchall()]

    # 必须与 GoalService._UPDATABLE 声明的可变字段保持一致（fix round 1 Major-1）：
    # 缺 runtime_ref/target_role 会让 PUT 走 service 校验、记 goal.update 事件却
    # 在 update 这里被 continue 丢弃——200 + 审计假报，台账纹丝不动。
    _GOAL_MUTABLE = ("intent", "stage", "stage_reason", "error_class", "params",
                     "env_overlay", "placement", "profile_yaml", "profile_sha",
                     "profile_version", "runtime_ref", "target_role")

    def update_goal(self, goal_id: str, *, now: float, **fields: Any) -> dict | None:
        sets, params = [], []
        for k, v in fields.items():
            if k not in self._GOAL_MUTABLE:
                continue
            sets.append(f"{k}=?")
            params.append(json.dumps(v, ensure_ascii=False)
                          if k in GOAL_JSON_FIELDS and v is not None else v)
        if not sets:
            return self.get_goal(goal_id)
        sets.append("updated_at=?")
        params.extend([now, goal_id])
        with self._lock:
            cur = self._db().execute(f"UPDATE goals SET {', '.join(sets)} WHERE goal_id=?", params)
            self._db().commit()
            if not cur.rowcount:
                return None
        return self.get_goal(goal_id)

    def delete_goal(self, goal_id: str) -> dict | None:
        snapshot = self.get_goal(goal_id)
        if snapshot is None:
            return None
        with self._lock:
            self._db().execute("DELETE FROM goals WHERE goal_id=?", (goal_id,))
            self._db().commit()
        return snapshot

    # ---- model_states（心跳全量覆盖式写入）----
    def upsert_model_state(self, *, node_id: str, profile: str, state: str,
                           gpu: list[int] | None, port: int | None, pid: int | None,
                           reason: str = "", error_class: str = "", now: float,
                           engine: str = "") -> None:
        with self._lock:
            self._db().execute(
                """INSERT INTO model_states(node_id,profile,engine,state,gpu,port,pid,reason,error_class,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(node_id,profile,engine) DO UPDATE SET
                     state=excluded.state, gpu=excluded.gpu, port=excluded.port, pid=excluded.pid,
                     reason=excluded.reason, error_class=excluded.error_class,
                     updated_at=excluded.updated_at""",
                (node_id, profile, engine or "", state,
                 json.dumps(gpu) if gpu else None, port, pid, reason, error_class, now))
            self._db().commit()

    def list_model_states(self, *, node_id: str = "", profile: str = "",
                          engine: str = "") -> list[dict]:
        sql, params = _MODEL_STATE_SELECT, []
        where: list[str] = []
        if node_id:
            where.append("node_id=?")
            params.append(node_id)
        if profile:
            where.append("profile=?")
            params.append(profile)
        if engine:
            where.append("engine=?")
            params.append(engine)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY node_id, profile, engine"
        with self._lock:
            rows = self._db().execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = {c: r[c] for c in _MODEL_STATE_COLS}
            d["gpu"] = json.loads(r["gpu"]) if r["gpu"] else None
            out.append(d)
        return out

    def delete_model_state(self, node_id: str, profile: str, engine: str = "") -> None:
        """删除该节点的 model_state 行。engine 传空时按 (node_id, profile) 全删
        （兼容旧调用方；stem 下所有 engine 清空），传引擎名时只删指定 engine 一行。
        """
        with self._lock:
            if engine:
                self._db().execute(
                    "DELETE FROM model_states WHERE node_id=? AND profile=? AND engine=?",
                    (node_id, profile, engine))
            else:
                self._db().execute(
                    "DELETE FROM model_states WHERE node_id=? AND profile=?",
                    (node_id, profile))
            self._db().commit()

    # ---- events ----
    def append_event(self, kind: str, *, node_id: str | None = None, goal_id: str | None = None,
                     payload: dict | None = None, now: float | None = None) -> None:
        safe_kind = str(kind)[:64]
        try:
            from modelctl.core.cluster.events import is_known_kind
            if not is_known_kind(safe_kind):
                logger.warning(f"事件 kind 不在 EVENT_KINDS 词表: {safe_kind!r}（照常入库；"
                               "worker 上报面 kind 为自由串，见计划裁决 1）")
        except Exception:  # noqa: BLE001 — 守卫自身绝不阻断写入
            pass
        with self._lock:
            self._db().execute(
                "INSERT INTO events(ts,node_id,goal_id,kind,payload) VALUES(?,?,?,?,?)",
                (now if now is not None else time.time(), node_id, goal_id, safe_kind,
                 json.dumps(payload, ensure_ascii=False) if payload else None),
            )
            self._db().commit()
            # 周期性保留裁剪（同一锁内完成，不新开锁；不可调 self.trim_events——
            # threading.Lock 非重入会自死锁）
            self._append_count += 1
            if self._append_count % EVENTS_TRIM_EVERY == 0:
                self._db().execute(
                    "DELETE FROM events WHERE ts < ?",
                    (time.time() - EVENTS_RETENTION_DAYS * 86400,),
                )
                self._db().commit()

    def trim_events(self, now: "float | None" = None) -> int:
        """删除超过保留窗口的事件，返回删除条数。幂等 DML，不动表结构。"""
        cutoff = (now if now is not None else time.time()) - EVENTS_RETENTION_DAYS * 86400
        with self._lock:
            cur = self._db().execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._db().commit()
            return cur.rowcount

    def recent_events(self, limit: int = 100, node_id: str | None = None, *,
                      kind: str | None = None) -> list[dict]:
        sql = "SELECT ts,node_id,goal_id,kind,payload FROM events"
        params: list[Any] = []
        conds = []
        if node_id:
            conds.append("node_id=?")
            params.append(node_id)
        if kind:
            conds.append("kind=?")
            params.append(kind)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._db().execute(sql, params).fetchall()
        return [{"ts": r["ts"], "node_id": r["node_id"], "goal_id": r["goal_id"], "kind": r["kind"],
                 "payload": json.loads(r["payload"]) if r["payload"] else None} for r in rows]
