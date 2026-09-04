# Cluster M1：声明式目标 + placement gate + reconciler 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 modelctl 集群管理面的 M1 里程碑：`goals` 表读写 + `cluster goal set/list/remove`、`cluster launch/stop/sync`、中心侧 placement gate（含 `--dry-run`）、profile 声明式下发（原子写 + 漂移检测 + 剪枝）、worker 端 intent reconciler（8 态 stage 状态机）、远程启停、worker 状态经心跳回流中心。

**Architecture:** 沿用 M0 的"worker 主动连中心的单条 WS"，中心**不反向建连**、不引入消息总线：期望状态（该节点全部 goal 的快照 + revision）**随心跳 ack 下发**，worker 落盘 `data/cache/cluster-goals.json` 与 `models/<engine>/<name>.yaml`，本地 reconciler 线程按周期把实际状态逼向 intent（起停原语完全复用 `core/all_service.py`，集群层只决定"去不去调"），再把 `profiles/stage/drift` 随下一次心跳回流。中心因此**天然自愈**：只要 `revision` 不一致就在 ack 里带全量 goal 集，无需额外投递通道。goal 是中心 SQLite 里唯一 source of truth，worker 的 `models/` 是同步产物。

**Tech Stack:** Python 3.12（stdlib `sqlite3`/`threading`/`pathlib`/`hashlib`/`urllib`）、PyYAML、loguru；FastAPI 仅在 `core/webui/admin_cluster.py` 层导入；测试 `pytest`（`TestClient` + `websockets.sync.server` 假中心，均 `importorskip`）。

**Spec:** `docs/superpowers/specs/2026-09-03-modelctl-cluster-design.md`（§6 声明式目标与同步、§6.6 gate、§7 reconciler 与 8 态状态机、§8 故障矩阵、§10 鉴权、§11 schema、§12 M1 范围）
**前置:** M0 已入库（`core/cluster/{config,tokens,store,wsproto,nodes,center_probe,agent}.py`、`core/webui/admin_cluster.py`、`cli.py` cluster 子命令、`web/src/views/ClusterNodesView.vue`）

## Global Constraints

- Python >= 3.12；ruff line-length 120、`select = ["E","F","I","B","UP"]`；`uv run mypy src/modelctl` 零错误（CI 门禁）。
- **主包新代码只允许 stdlib + PyYAML + loguru**；`fastapi` 只在 `core/webui/admin_cluster.py` 导入。M1 **不新增第三方依赖**。
- 新 `.py` 文件带仓库标准文件头（`@File/@IDE/@Author : SunHao/@Email : 2865467769@qq.com/@Date/@Desc`，参照 `src/modelctl/core/envfile.py:1-10`）。
- 时间戳一律 **epoch float（REAL）**（延续 M0 对 spec §11.1 的有意偏离）；展示层再格式化。
- **solo/worker 角色零影响**：未设或 `solo` 时 goals 端点全 404、无 reconciler 线程、`modelctl cluster goal ...` 报错退出 2；现有测试必须全绿。
- **控制面绝不越界**：worker 只写 `models/<engine>/<name>.yaml`、`data/cache/cluster-goals.json`、`data/cache/cluster-sync-marker.json`；起停只经 `all_service.start_profile/stop_profile`，不自行 spawn、不改 `.env`（node_token 写回除外，M0 已有）。
- **密钥不出中心**：下发 profile YAML **原文**（`${API_KEY}` 由 worker 本地解析）；`env_overlay` 只允许白名单键，密钥类键一律 400。
- **不可信输入边界**（中心→worker 是"远端可写 worker 文件"的通道，按外部输入对待）：`profile`/`engine`/`goal_id` 走白名单正则 + `KNOWN_ENGINES`；单份 YAML ≤ 256 KiB；单节点 goal ≤ 512 份；写盘前 `yaml.safe_load` 必须通过且含 `port`。
- **表结构演进只增列，永不删/改列**：`nodes` 新增 4 列经 `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` 幂等补齐。这是应用自带 SQLite 文件（`data/cache/cluster-meta.db`，已 gitignore）的模式演进，与 M0 的 `CREATE TABLE IF NOT EXISTS` 同性质；**不对任何生产库执行 DDL**。
- 测试 `uv run pytest tests/<file> -q`；全量门禁 `uv run ruff check src tests; uv run mypy src/modelctl; uv run pytest tests/ -q`（PowerShell 用 `;`，不支持 `&&`）。
- 新测试凡 `import fastapi`/`websockets` 一律顶部 `pytest.importorskip(...)`。
- CLI 含中文表格一律复用 `cli._print_table`（内部 `display_width`/`_ljust_width`），禁止 `f"{x:<N}"` 裸对齐（CLAUDE.md CJK 规范）。
- **M1 前端仅做最小增量**：`ClusterNodesView.vue` 表格补 `capacity`（卡数/显存）一列（后端已格式化，前端不二次加工）；goals 矩阵视图与节点详情页属 M2。
- 提交信息：`feat(cluster): ...` / `test(cluster): ...` / `fix(cluster): ...` / `docs(cluster): ...`（中文）。

## 对 spec 的四处有意偏离（实现前必读）

1. **下发通道**：spec §5.2 把 `goal.sync` 描述为中心主动 push。实现改为**心跳 ack 捎带全量 goal 集**（`revision` 不一致才带），不引入 per-connection 发送队列/跨任务 send。语义等价（幂等全量快照 + 自愈），延迟上界 = 心跳间隔（默认 10s），冷启动本身分钟级，不影响体验；REST 响应显式返回 `"delivery": "next-heartbeat"` 以免误导。
2. **`--create` 语义**：spec §6.1/§6.6 的"自动下载"落地为——`--create` 只决定是否**允许在缺少该 profile 文件的节点上创建 YAML**（不带则 skip 并提示）；模型权重下载无需新机制：worker 调 `all_service.start_profile` 时引擎适配器（`engines/_download.py` + `adapter.pre_start`）本就走 ModelScope 下载，失败即 `error_class=model_download_failed`。
3. **错误一律不自动重试**（spec §7.2 立场）：任何失败 → `stage=FAILED` + `reason` + `error_class`，reconciler 停止调度该 goal，直到中心 `retry`（新 WS action）或 goal 变更。**不设 retryable 集合、不设重试次数**（比 spec 附带说明的"model_download_failed 可 dry-retry"更严格、更可预测）。error_class 新增 `port_conflict`（spec 清单未含，但 `all_service.start_profile` 端口预检必然产生，归入 `runtime_capability` 会误导排障）。
4. **placement gate 的 GPU 冲突为粗筛**：中心只有心跳上报的 GPU 事实，卡位裁决真正在 worker 侧 `gpu_lock`。gate 只做：profile 声明的 `gpu_list` 与在用 GPU 求交、"节点 GPU 已全被占用"、容量/显存估算。精确冲突由 worker 侧 `gpu_lock` 兜底并上报 `error_class=gpu_lock`。

## 文件结构（本计划创建/修改）

```
创建
  src/modelctl/core/cluster/profiles.py    中心侧 profile 源读取（原文 + sha256 + 安全名）
  src/modelctl/core/cluster/gate.py        placement gate 纯函数（容量/运行时/冲突/LAN/显存）
  src/modelctl/core/cluster/goals.py       GoalService（CRUD + 期望状态快照 + revision + env 白名单）
  src/modelctl/core/cluster/conns.py       连接世代表（同 node_id 后来者胜 + 令牌轮换即时失效）
  src/modelctl/core/cluster/sync.py        worker 侧写盘（原子写 + managed-by 头 + .master + 漂移 + 剪枝）
  src/modelctl/core/cluster/reconcile.py   worker 侧 reconciler（8 态 stage + recipe + 错误分类 + 心跳快照）
  tests/test_cluster_store_goals.py  tests/test_cluster_wsproto_v2.py  tests/test_cluster_profiles.py
  tests/test_cluster_gate.py         tests/test_cluster_goals.py      tests/test_cluster_conns.py
  tests/test_cluster_ingest.py       tests/test_cluster_sync_writer.py tests/test_cluster_reconcile.py
  tests/test_cluster_agent_v2.py     tests/test_cluster_goals_http.py  tests/test_cluster_goal_cli.py
修改
  src/modelctl/core/cluster/store.py           _ensure_columns + update_node_capacity + goals/model_states CRUD
  src/modelctl/core/cluster/wsproto.py         make_sync/make_action/make_result/parse_action/parse_ack/parse_heartbeat_v2
  src/modelctl/core/cluster/nodes.py           handle_heartbeat v2（回流落库 + ack 组装）+ push_action/drain_actions
  src/modelctl/core/cluster/agent.py           ack 交给 reconciler；心跳合并 reconciler 快照；start_reconciler_in_background
  src/modelctl/core/cluster/center_probe.py    补 delete_json（goal remove 用）
  src/modelctl/core/webui/admin_cluster.py     goals CRUD / retry / node sync / model start|stop|restart / export + WS 世代与令牌复核
  src/modelctl/core/webui/server.py            worker 角色额外启动 reconciler 线程
  src/modelctl/cli.py                          cluster goal set|list|remove、launch、stop、sync、goals
  .env.example                                 CLUSTER_RECONCILE_INTERVAL_S / CLUSTER_START_TIMEOUT_S
  README.md                                    第 10 节补 "10.6 声明式下发模型（M1）"
```

**M1 范围外（勿做）**：前端 goals 视图与节点详情页、`--cluster` 聚合 flag、`metrics_rollups` 写入、`cluster backup/restore`、`audit.query` 远程拉取、gateway cluster-aware 路由、RBAC/mTLS——属 M2/P2（spec §12、§1.8）。`metrics_rollups` 表 M0 已建，M1 保持无读写方。

---

### Task 1: `store.py` 扩展——nodes 增列迁移 + goals / model_states CRUD

M0 已按 spec §11.1 建齐全部表，但 `nodes` 缺 M1 需要的 4 列（改进 B），且 goals/model_states 无读写方。本任务补齐：幂等增列（只增不删）+ goal/model_state 全套 CRUD。

**Files:**
- Modify: `src/modelctl/core/cluster/store.py`（`init_db` 内插入 `_ensure_columns`；尾部追加分节）
- Test: `tests/test_cluster_store_goals.py`

**Interfaces:**
- Consumes: `ClusterStore._db()`（autocommit + WAL + busy_timeout）、`init_db()`
- Produces:
  - `ClusterStore.update_node_capacity(node_id: str, *, capacity: dict | None, runtimes: dict | None, local_profiles: list[str] | None = None, now: float) -> None`
  - `ClusterStore.set_node_last_goal_sync_sha(node_id: str, sha: str) -> None`
  - `ClusterStore.upsert_goal(*, goal_id: str, node_id: str, profile: str, engine: str, profile_yaml: str, profile_sha: str, profile_version: str | None, intent: str, params: dict | None, env_overlay: dict | None, placement: dict | None, runtime_ref: str | None, target_role: str, stage: str, created_by: str, now: float) -> None`
  - `ClusterStore.get_goal(goal_id: str) -> dict | None`
  - `ClusterStore.list_goals(*, node_id: str = "", profile: str = "") -> list[dict]`
  - `ClusterStore.update_goal(goal_id: str, *, now: float, **fields: Any) -> dict | None`（白名单：`intent/stage/stage_reason/error_class/params/env_overlay/placement/profile_yaml/profile_sha/profile_version`）
  - `ClusterStore.delete_goal(goal_id: str) -> dict | None`
  - `ClusterStore.upsert_model_state(*, node_id: str, profile: str, state: str, gpu: list[int] | None, port: int | None, pid: int | None, reason: str = "", error_class: str = "", now: float) -> None`
  - `ClusterStore.list_model_states(*, node_id: str = "") -> list[dict]`
  - `ClusterStore.delete_model_state(node_id: str, profile: str) -> None`
  - 常量 `GOAL_JSON_FIELDS: tuple[str, ...] = ("params", "env_overlay", "placement")`
  - `get_node()` 返回体新增 `capacity` / `runtimes` 解码键

- [ ] **Step 1: 写失败测试** — 创建 `tests/test_cluster_store_goals.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_store_goals.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : ClusterStore M1 扩展测试（nodes 增列迁移 + goals/model_states CRUD）
# ===============================================================================

import sqlite3

import pytest

from modelctl.core.cluster.store import ClusterStore


@pytest.fixture()
def store(tmp_path):
    s = ClusterStore(tmp_path / "cluster-meta.db")
    s.init_db()
    return s


def _mk_goal(store, goal_id="qwen-vllm@@w-1", **over):
    kw = dict(node_id="w-1", profile="qwen-vllm", engine="vllm", profile_yaml="port: 8101\n",
              profile_sha="sha-a", profile_version="2026-09-04-aaaaaa", intent="start",
              params=None, env_overlay=None, placement=None, runtime_ref=None,
              target_role="primary", stage="PENDING_PROFILE_SYNC", created_by="op", now=100.0)
    kw.update(over)
    store.upsert_goal(goal_id=goal_id, **kw)
    return goal_id


def test_init_db_adds_m1_columns_idempotently(tmp_path):
    """旧库（无 M1 列）再 init 必须补齐列，且重复 init 不报错。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE nodes (node_id TEXT PRIMARY KEY, node_token TEXT NOT NULL)")
    conn.commit()
    conn.close()
    s = ClusterStore(db)
    s.init_db()
    s.init_db()  # 第二次：列已存在，不得抛 duplicate column
    cols = {r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(nodes)")}
    assert {"capacity_json", "runtime_json", "gateway_url", "last_goal_sync_sha"} <= cols


def test_update_node_capacity_none_keeps_existing(store):
    store.upsert_node(node_id="w-1", node_token="NT-1", lan_id="lan-1", role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    store.update_node_capacity("w-1", capacity={"gpu_count": 4, "vram_total_mb": 157280},
                               runtimes={"vllm": {"ok": True, "version": "0.9.1"}},
                               local_profiles=["qwen-vllm"], now=2.0)
    node = store.get_node("w-1")
    assert node["capacity"]["gpu_count"] == 4
    assert node["runtimes"]["vllm"]["ok"] is True
    assert node["local_profiles"] == ["qwen-vllm"]
    store.update_node_capacity("w-1", capacity=None, runtimes=None, now=3.0)
    assert store.get_node("w-1")["capacity"]["gpu_count"] == 4  # None 不覆盖（同 engines 语义）
    assert store.get_node("w-1")["local_profiles"] == ["qwen-vllm"]


def test_set_node_last_goal_sync_sha(store):
    store.upsert_node(node_id="w-1", node_token="NT-1", lan_id="", role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    store.set_node_last_goal_sync_sha("w-1", "rev-7")
    assert store.get_node("w-1")["last_goal_sync_sha"] == "rev-7"


def test_goal_upsert_roundtrip_decodes_json(store):
    g = store.get_goal(_mk_goal(store, params={"gpu_list": [0, 1]},
                                env_overlay={"MODEL_ROOT": "/m"},
                                placement={"gpu_count": 2, "min_vram_mb": 4096}))
    assert g["params"] == {"gpu_list": [0, 1]}
    assert g["env_overlay"] == {"MODEL_ROOT": "/m"}
    assert g["placement"]["gpu_count"] == 2
    assert g["stage"] == "PENDING_PROFILE_SYNC"


def test_goal_json_none_stays_none(store):
    g = store.get_goal(_mk_goal(store))
    assert g["params"] is None and g["env_overlay"] is None and g["placement"] is None


def test_goal_upsert_same_id_updates_but_keeps_created_at(store):
    gid = _mk_goal(store)
    created = store.get_goal(gid)["created_at"]
    store.upsert_goal(goal_id=gid, node_id="w-1", profile="qwen-vllm", engine="vllm",
                      profile_yaml="port: 8102\n", profile_sha="sha-b", profile_version="v2",
                      intent="stop", params=None, env_overlay=None, placement=None,
                      runtime_ref=None, target_role="replica", stage="READY",
                      created_by="op2", now=200.0)
    g = store.get_goal(gid)
    assert g["profile_sha"] == "sha-b" and g["intent"] == "stop" and g["stage"] == "READY"
    assert g["created_at"] == created and g["updated_at"] == 200.0


def test_list_goals_filters(store):
    _mk_goal(store, "a@@w-1", node_id="w-1", profile="a")
    _mk_goal(store, "b@@w-1", node_id="w-1", profile="b")
    _mk_goal(store, "a@@w-2", node_id="w-2", profile="a")
    assert {g["goal_id"] for g in store.list_goals(node_id="w-1")} == {"a@@w-1", "b@@w-1"}
    assert {g["goal_id"] for g in store.list_goals(profile="a")} == {"a@@w-1", "a@@w-2"}
    assert len(store.list_goals()) == 3


def test_update_goal_whitelist_and_absent(store):
    gid = _mk_goal(store)
    out = store.update_goal(gid, now=300.0, stage="FAILED", stage_reason="venv 缺失",
                            error_class="venv_missing", bogus_field="x")
    assert out["stage"] == "FAILED" and out["error_class"] == "venv_missing"
    assert "bogus_field" not in out
    assert store.update_goal("nope@@w-1", now=1.0, stage="READY") is None


def test_update_goal_json_field_roundtrip(store):
    gid = _mk_goal(store)
    out = store.update_goal(gid, now=1.0, params={"gpu_list": [3]})
    assert out["params"] == {"gpu_list": [3]}


def test_delete_goal_returns_snapshot_or_none(store):
    gid = _mk_goal(store)
    assert store.delete_goal(gid)["profile"] == "qwen-vllm"
    assert store.get_goal(gid) is None
    assert store.delete_goal(gid) is None


def test_model_state_upsert_and_delete(store):
    store.upsert_model_state(node_id="w-1", profile="qwen-vllm", state="READY",
                             gpu=[0, 1], port=8101, pid=4321, now=10.0)
    store.upsert_model_state(node_id="w-1", profile="qwen-vllm", state="FAILED",
                             gpu=None, port=None, pid=None, reason="gpu 冲突",
                             error_class="gpu_lock", now=20.0)
    rows = store.list_model_states(node_id="w-1")
    assert len(rows) == 1 and rows[0]["state"] == "FAILED"
    assert rows[0]["gpu"] is None and rows[0]["error_class"] == "gpu_lock"
    store.delete_model_state("w-1", "qwen-vllm")
    assert store.list_model_states(node_id="w-1") == []
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cluster_store_goals.py -q`
Expected: FAIL — `AttributeError: 'ClusterStore' object has no attribute 'upsert_goal'`

- [ ] **Step 3: 实现——常量与增列迁移**

在 `mask_tail` 之前加：

```python
GOAL_JSON_FIELDS: tuple[str, ...] = ("params", "env_overlay", "placement")

#: M1 新增列（改进 B）；只增不删，旧库经 _ensure_columns 幂等补齐
_NODE_M1_COLUMNS: tuple[tuple[str, str], ...] = (
    ("capacity_json", "TEXT"),
    ("runtime_json", "TEXT"),
    ("gateway_url", "TEXT"),
    ("last_goal_sync_sha", "TEXT"),
    ("local_profiles_json", "TEXT"),
)
```

`_NODE_COLS` 追加 4 列名，`_row_to_node` 追加解码，`init_db` 调迁移：

```python
    def init_db(self) -> None:
        with self._lock:
            self._db().executescript(_SCHEMA)
            self._ensure_columns()
            self._db().commit()

    def _ensure_columns(self) -> None:
        """幂等补列（M1 起 nodes 需要）。**只增不删**，绝不改/删既有列。

        全新库为空操作；旧库（M0 早期版本）逐列 ALTER。调用方已持锁，不再取锁。
        """
        have = {r["name"] for r in self._db().execute("PRAGMA table_info(nodes)").fetchall()}
        for name, sql_type in _NODE_M1_COLUMNS:
            if name not in have:
                self._db().execute(f"ALTER TABLE nodes ADD COLUMN {name} {sql_type}")
```

```python
    def _row_to_node(self, row: sqlite3.Row) -> dict[str, Any]:
        d: dict[str, Any] = {c: row[c] for c in _NODE_COLS}
        d["engines"] = json.loads(row["engines"]) if row["engines"] else None
        d["capacity"] = json.loads(row["capacity_json"]) if row["capacity_json"] else None
        d["runtimes"] = json.loads(row["runtime_json"]) if row["runtime_json"] else None
        d["local_profiles"] = (json.loads(row["local_profiles_json"])
                               if row["local_profiles_json"] else [])
        return d
```

- [ ] **Step 4: 实现——capacity / goals / model_states CRUD**

在 `# ---- events ----` 之前插入：

```python
    # ---- nodes 容量/运行时/本机 profile 清单（改进 B；None 不覆盖既有值，与 engines 同语义）----
    def update_node_capacity(self, node_id: str, *, capacity: dict | None,
                             runtimes: dict | None, local_profiles: list[str] | None = None,
                             now: float) -> None:
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
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?,?)
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
            row = self._db().execute("SELECT * FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
        return self._row_to_goal(row) if row else None

    def list_goals(self, *, node_id: str = "", profile: str = "") -> list[dict]:
        sql, params, conds = "SELECT * FROM goals", [], []
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

    _GOAL_MUTABLE = ("intent", "stage", "stage_reason", "error_class", "params",
                     "env_overlay", "placement", "profile_yaml", "profile_sha", "profile_version")

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
                           reason: str = "", error_class: str = "", now: float) -> None:
        with self._lock:
            self._db().execute(
                """INSERT INTO model_states(node_id,profile,state,gpu,port,pid,reason,error_class,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(node_id,profile) DO UPDATE SET
                     state=excluded.state, gpu=excluded.gpu, port=excluded.port, pid=excluded.pid,
                     reason=excluded.reason, error_class=excluded.error_class,
                     updated_at=excluded.updated_at""",
                (node_id, profile, state, json.dumps(gpu) if gpu else None, port, pid,
                 reason, error_class, now))
            self._db().commit()

    def list_model_states(self, *, node_id: str = "") -> list[dict]:
        sql, params = "SELECT * FROM model_states", []
        if node_id:
            sql += " WHERE node_id=?"
            params.append(node_id)
        sql += " ORDER BY node_id, profile"
        with self._lock:
            rows = self._db().execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = {k: r[k] for k in r.keys()}
            d["gpu"] = json.loads(r["gpu"]) if r["gpu"] else None
            out.append(d)
        return out

    def delete_model_state(self, node_id: str, profile: str) -> None:
        with self._lock:
            self._db().execute("DELETE FROM model_states WHERE node_id=? AND profile=?",
                               (node_id, profile))
            self._db().commit()
```

> `upsert_goal` 的 VALUES 占位符必须正好 18 个 `?`（18 列）；上面示例中 `?, ?,?` 间的空格是排版遗留，实现时写连续，并由 Step 5 的插入测试兜住。
> `ON CONFLICT` 里把 `stage` 强制回 `PENDING_PROFILE_SYNC` 并清 `stage_reason/error_class`：goal 内容变更即视为重新收敛（spec §7.2 "用户手动 retry → RESET 回 PENDING_PROFILE_SYNC" 的同源语义）。

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/test_cluster_store_goals.py tests/test_cluster_store.py -q`
Expected: PASS（新 11 条 + M0 store 测试全绿，证明迁移未破坏 M0）

- [ ] **Step 6: 提交**

```bash
git add src/modelctl/core/cluster/store.py tests/test_cluster_store_goals.py
git commit -m "feat(cluster): store 增 M1 列幂等迁移与 goals/model_states CRUD"
```

---

### Task 2: `wsproto.py` v2——sync / action / result / 心跳扩展

M0 协议只有 hello/welcome/heartbeat/event/error/ack。M1 新增：中心→worker 的 `sync`（goal 全量快照）与 `action`（start/stop/restart/retry）；worker→中心的 `result`（action 回执）。心跳 payload 扩展 `profiles` / `goal_sync` / `drift` 三段。

**Files:**
- Modify: `src/modelctl/core/cluster/wsproto.py`（`PROTO_VERSION` 改 2；尾部追加）
- Test: `tests/test_cluster_wsproto_v2.py`

**Interfaces:**
- Consumes: 既有 `dumps` / `parse_type` / `parse_heartbeat`（保留不删）
- Produces:
  - `PROTO_VERSION = 2`
  - `VALID_ACTIONS: tuple[str, ...] = ("start", "stop", "restart", "retry")`
  - `make_sync(revision: str, goals: list[dict]) -> dict`（goal 快照即全量集；"剪枝"由 worker 侧以本地 managed 清单对照快照自行推导，无需中心列举 pruned）
  - `make_action(seq: int, action: str, goal_id: str = "", profile: str = "") -> dict`
  - `make_result(seq: int, ok: bool, detail: str = "") -> dict`
  - `parse_action(data: Any) -> dict` → `{"seq": int, "action": str, "goal_id": str, "profile": str}`
  - `parse_ack(data: Any) -> dict` → `{"seq": int, "sync": dict|None, "actions": list[dict]}`
  - `parse_heartbeat_v2(data: Any) -> dict` → `{"profiles": dict, "goal_sync": dict|None, "drift": list[str]}`

- [ ] **Step 1: 写失败测试** — 创建 `tests/test_cluster_wsproto_v2.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_wsproto_v2.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : wsproto v2（sync/action/result/心跳扩展）编解码与输入消毒测试
# ===============================================================================

from modelctl.core.cluster import wsproto


def test_proto_version_bumped():
    assert wsproto.PROTO_VERSION == 2
    assert wsproto.make_hello("n", "l", "k", {})["v"] == 2


def test_make_sync_shape():
    assert wsproto.make_sync("rev-1", [{"goal_id": "a@@w-1"}]) == {
        "t": "sync", "revision": "rev-1", "goals": [{"goal_id": "a@@w-1"}]}


def test_make_action_and_result_shapes():
    assert wsproto.make_action(3, "start", goal_id="a@@w-1") == {
        "t": "action", "seq": 3, "action": "start", "goal_id": "a@@w-1", "profile": ""}
    assert wsproto.make_result(3, True, "ok") == {"t": "result", "seq": 3, "ok": True, "detail": "ok"}


def test_make_result_truncates_detail():
    assert len(wsproto.make_result(1, False, "x" * 900)["detail"]) == 500


def test_parse_action_sanitizes_types():
    assert wsproto.parse_action({"t": "action", "seq": "x", "action": 1, "goal_id": None}) == {
        "seq": 0, "action": "", "goal_id": "", "profile": ""}
    assert wsproto.parse_action("nope") == {"seq": 0, "action": "", "goal_id": "", "profile": ""}
    # bool 不是合法 seq（True == 1 会造成静默 seq 冲突）
    assert wsproto.parse_action({"seq": True})["seq"] == 0


def test_parse_ack_none_when_no_control_fields():
    """M0 中心的 ack 无控制字段：缺字段是常态，须回落默认而非抛错（滚动升级）。"""
    assert wsproto.parse_ack({"t": "ack"}) == {"seq": 0, "sync": None, "actions": []}
    assert wsproto.parse_ack("nope") == {"seq": 0, "sync": None, "actions": []}


def test_parse_ack_extracts_sync_and_actions():
    got = wsproto.parse_ack({"t": "ack", "seq": 5,
                             "sync": {"revision": "r", "goals": [], "pruned": ["x"]},
                             "actions": [{"t": "action", "seq": 9, "action": "stop", "goal_id": "a@@w"}]})
    assert got["seq"] == 5
    assert got["sync"]["revision"] == "r"
    assert got["actions"] == [{"seq": 9, "action": "stop", "goal_id": "a@@w", "profile": ""}]


def test_parse_ack_drops_malformed_entries():
    got = wsproto.parse_ack({"sync": ["not", "a", "dict"], "actions": ["junk", {"seq": 1}]})
    assert got["sync"] is None
    assert got["actions"] == [{"seq": 1, "action": "", "goal_id": "", "profile": ""}]


def test_parse_heartbeat_v2_defaults():
    """缺字段 → None（= 未知），**不得**回落到 {}/[]。空值是"worker 明确说没有"，
    与"旧版 worker 没上报该字段"必须可区分，否则中心会拿空值覆盖既有事实
    （profiles=None 而非 {}：否则旧版 worker 会把台账里在跑的模型全抹掉）。"""
    assert wsproto.parse_heartbeat_v2({}) == {
        "profiles": None, "goal_sync": None, "drift": None, "local_profiles": None,
        "capacity": None, "runtimes": None}
    assert wsproto.parse_heartbeat_v2(None) == {
        "profiles": None, "goal_sync": None, "drift": None, "local_profiles": None,
        "capacity": None, "runtimes": None}


def test_parse_heartbeat_v2_empty_list_means_explicit_empty():
    got = wsproto.parse_heartbeat_v2({"payload": {"local_profiles": [], "drift": []}})
    assert got["local_profiles"] == [] and got["drift"] == []


def test_parse_heartbeat_v2_keeps_whitelisted_shapes_only():
    got = wsproto.parse_heartbeat_v2({"t": "heartbeat", "payload": {
        "profiles": {"a": {"state": "READY", "port": 8101}, "b": "junk"},
        "goal_sync": {"revision": "r1", "drift": True},
        "drift": ["a@@w-1", 5],
        "local_profiles": ["qwen-vllm", 7],
        "capacity": {"gpu_count": 4}, "runtimes": ["junk"],
        "injected": "ignored"}})
    assert got["profiles"] == {"a": {"state": "READY", "port": 8101}}
    assert got["goal_sync"] == {"revision": "r1", "drift": True}
    assert got["drift"] == ["a@@w-1"]
    assert got["local_profiles"] == ["qwen-vllm"]   # gate 的 --create 判定依赖它
    assert got["capacity"] == {"gpu_count": 4}      # 改进 B：容量随心跳上报
    assert got["runtimes"] is None                  # 非 dict 消毒为 None（未知≠没有）


def test_parse_heartbeat_v2_accepts_top_level_for_rolling_upgrade():
    """M0 心跳把字段放 payload；对混版本对端，顶层位置也须能解。"""
    got = wsproto.parse_heartbeat_v2({"profiles": {"a": {"state": "READY"}}})
    assert got["profiles"] == {"a": {"state": "READY"}}
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cluster_wsproto_v2.py -q`
Expected: FAIL —`AttributeError: module 'modelctl.core.cluster.wsproto' has no attribute 'make_sync'`

- [ ] **Step 3: 实现**

`PROTO_VERSION = 1` → `2`；文件尾部追加：

```python
VALID_ACTIONS: tuple[str, ...] = ("start", "stop", "restart", "retry")


def make_sync(revision: str, goals: list[dict[str, Any]]) -> dict[str, Any]:
    """goal 全量快照下发。幂等语义：worker 以 revision 判重、同 sha 跳过写盘。

    不设 pruned 字段：goal 从快照消失即删除语义，worker 用本地 managed 清单对照
    快照自行推导（见 Task 8），中心无需保留"已删除"墓碑。
    """
    return {"t": "sync", "revision": revision, "goals": goals}


def make_action(seq: int, action: str, goal_id: str = "", profile: str = "") -> dict[str, Any]:
    return {"t": "action", "seq": int(seq), "action": action, "goal_id": goal_id, "profile": profile}


def make_result(seq: int, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"t": "result", "seq": int(seq), "ok": bool(ok), "detail": str(detail)[:500]}


def _safe_seq(data: dict[str, Any]) -> int:
    """seq 只接受非 bool 的 int（bool 是 int 子类，会静默变成 0/1 造成回执错配）。"""
    seq = data.get("seq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else 0


def parse_action(data: Any) -> dict[str, Any]:
    """action 帧消毒：文本字段非 str → 空串（对端可控输入不得污染下游类型）。"""
    if not isinstance(data, dict):
        return {"seq": 0, "action": "", "goal_id": "", "profile": ""}
    out: dict[str, Any] = {"seq": _safe_seq(data)}
    for k in ("action", "goal_id", "profile"):
        v = data.get(k)
        out[k] = str(v)[:200] if isinstance(v, str) else ""
    return out


def parse_ack(data: Any) -> dict[str, Any]:
    """ack 消毒：只认 dict 型 sync 与 list 型 actions，其余回落安全默认。

    ack 由中心写、worker 读；旧版中心的 ack 不含控制字段，缺字段是常态而非错误，
    因此一律回落而非抛错（否则 worker 在中心升级前会整条链路失败）。
    """
    out: dict[str, Any] = {"seq": 0, "sync": None, "actions": []}
    if not isinstance(data, dict):
        return out
    out["seq"] = _safe_seq(data)
    sync = data.get("sync")
    if isinstance(sync, dict):
        out["sync"] = sync
    actions = data.get("actions")
    if isinstance(actions, list):
        out["actions"] = [parse_action(a) for a in actions]
    return out


def _opt_dict(value: Any) -> dict | None:
    return value if isinstance(value, dict) else None


def _opt_str_list(value: Any, *, limit: int) -> list[str] | None:
    """字段缺失 → None（未知）；显式空列表 → []（worker 明确说"没有"）。

    中心据此决定是否覆盖台账：None 保留旧值，[] 清空。二者混淆会让旧版 worker
    （不上报该字段）把新版写入的事实抹掉。列表内非 str 条目丢弃，长度封顶防放大。
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    return [str(v) for v in value if isinstance(v, str)][:limit]


def parse_heartbeat_v2(data: Any) -> dict[str, Any]:
    """心跳扩展段消毒（v2 新增段全部按"缺失=None=未知"处理）。

    profiles 同样遵循"缺失即 None"：中心拿它全量覆盖 model_states，若把"旧版
    worker 没上报"当成"上报了空集"，会把该节点所有在跑模型记录抹掉（dashboard
    集体假 down）。中心侧对 profiles 取 .items() 前须先判 None。
    `local_profiles` 是 worker 本机 models/ 下的 profile 名清单，中心用它决定
    gate 的 --create 判定（该节点是否已有同名 profile 文件）。
    """
    if not isinstance(data, dict):
        return {"profiles": None, "goal_sync": None, "drift": None,
                "local_profiles": None, "capacity": None, "runtimes": None}
    src = data.get("payload") if isinstance(data.get("payload"), dict) else data
    profiles_raw = src.get("profiles")
    profiles = ({str(k): v for k, v in profiles_raw.items() if isinstance(v, dict)}
                if isinstance(profiles_raw, dict) else None)
    return {"profiles": profiles,
            "goal_sync": _opt_dict(src.get("goal_sync")),
            "drift": _opt_str_list(src.get("drift"), limit=512),
            "local_profiles": _opt_str_list(src.get("local_profiles"), limit=1024),
            "capacity": _opt_dict(src.get("capacity")),
            "runtimes": _opt_dict(src.get("runtimes"))}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_cluster_wsproto_v2.py tests/test_cluster_wsproto.py -q`
Expected: PASS（新 11 条 + M0 协议测试全绿）

- [ ] **Step 5: 提交**

```bash
git add src/modelctl/core/cluster/wsproto.py tests/test_cluster_wsproto_v2.py
git commit -m "feat(cluster): wsproto v2 增加 sync/action/result 与心跳扩展消毒"
```

---

### Task 3: `cluster/profiles.py`——中心侧 profile 源读取与 sha

中心把本机 `models/<engine>/<name>.yaml` 的**原文**作为下发内容。两条硬约束决定本模块形态：① `${VAR}` 必须由 worker 本地解析（否则密钥随 sync 落进中心 DB 与网络帧，违反 §10.5）；② `profile_sha` 取**原始文件文本**而非"解析后再 dump"，避免读→dump→写往返差异被漂移检测误判。同时 name/engine 是将来 worker 侧的写盘路径成分，必须白名单校验。

**Files:**
- Create: `src/modelctl/core/cluster/profiles.py`
- Test: `tests/test_cluster_profiles.py`

**Interfaces:**
- Consumes: `modelctl.core.profile.KNOWN_ENGINES`、`modelctl.core.envfile.PROJECT_ROOT`
- Produces:
  - `SAFE_NAME_RE`、`is_safe_name(value: str) -> bool`
  - `MAX_YAML_BYTES: int = 256 * 1024`
  - `profile_sha(text: str) -> str`（`"sha256:" + hexdigest`，总长 71）
  - `default_profile_version(sha: str) -> str`（`YYYY-MM-DD-<sha[7:13]>`）
  - `find_profile_path(name: str, models_dir: Path | None = None) -> tuple[Path, str] | None`
  - `read_profile_source(name: str, models_dir: Path | None = None) -> dict`
    - 成功：`{"name","engine","path","yaml","sha","version","raw","ok": True}`
    - 失败：`{"name","ok": False,"reason": str}`（**绝不抛异常**，gate 要聚合 reason）

- [ ] **Step 1: 写失败测试** — 创建 `tests/test_cluster_profiles.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_profiles.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : cluster/profiles.py 中心侧 profile 源读取（原文/sha/路径安全）
# ===============================================================================

import pytest

from modelctl.core.cluster import profiles as P


@pytest.fixture()
def models(tmp_path):
    (tmp_path / "vllm").mkdir(parents=True)
    (tmp_path / "vllm" / "qwen.yaml").write_text(
        "port: 8101\napi_key: ${API_KEY}\nengine_config:\n  model: Qwen/Qwen3-8B\n", encoding="utf-8")
    (tmp_path / "vllm" / "noport.yaml").write_text("engine: vllm\n", encoding="utf-8")
    (tmp_path / "vllm" / "badyaml.yaml").write_text("a: [1,\n", encoding="utf-8")
    (tmp_path / "evil").mkdir()
    (tmp_path / "evil" / "x.yaml").write_text("port: 1\n", encoding="utf-8")
    return tmp_path


def test_is_safe_name_rejects_traversal_and_cjk():
    assert P.is_safe_name("qwen3.8-vllm") and P.is_safe_name("a_b-1.2")
    for bad in ("../etc/passwd", "a/b", "a\\b", "", "-lead", "x" * 65, "中文", "a b", ".hidden"):
        assert not P.is_safe_name(bad), bad


def test_sha_prefixed_stable_and_distinguishing():
    a = P.profile_sha("port: 1\n")
    assert a.startswith("sha256:") and len(a) == 71
    assert a == P.profile_sha("port: 1\n") and a != P.profile_sha("port: 2\n")


def test_default_version_is_date_plus_sha_prefix():
    sha = P.profile_sha("x")
    ver = P.default_profile_version(sha)
    assert len(ver.split("-")[0]) == 4 and ver.endswith(sha[7:13])


def test_read_keeps_raw_text_and_never_interpolates(models):
    got = P.read_profile_source("qwen", models)
    assert got["ok"] is True and got["engine"] == "vllm"
    assert "${API_KEY}" in got["yaml"]          # 原文下发，中心绝不插值
    assert got["raw"]["engine_config"]["model"] == "Qwen/Qwen3-8B"  # 未插值的解析结果
    assert got["sha"] == P.profile_sha(got["yaml"])


def test_engine_from_explicit_yaml_field_wins(models):
    (models / "vllm" / "expl.yaml").write_text("port: 1\nengine: sglang\n", encoding="utf-8")
    assert P.read_profile_source("expl", models)["engine"] == "sglang"


def test_missing_profile_reports_hint(models):
    got = P.read_profile_source("nope", models)
    assert got["ok"] is False and "不存在" in got["reason"]


def test_unsafe_name_never_touches_fs(models):
    got = P.read_profile_source("../escape", models)
    assert got["ok"] is False and "非法" in got["reason"]


def test_requires_port_mapping_and_valid_yaml(models):
    assert P.read_profile_source("noport", models)["ok"] is False
    assert "YAML" in P.read_profile_source("badyaml", models)["reason"]


def test_engine_must_be_known(tmp_path):
    (tmp_path / "evil").mkdir()
    (tmp_path / "evil" / "qwen.yaml").write_text("port: 1\n", encoding="utf-8")
    got = P.read_profile_source("qwen", tmp_path)
    assert got["ok"] is False and "engine" in got["reason"]


def test_size_cap_rejects_huge_file(tmp_path):
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "big.yaml").write_text(
        "port: 1\npad: " + "x" * (P.MAX_YAML_BYTES + 10), encoding="utf-8")
    assert P.read_profile_source("big", tmp_path)["ok"] is False


def test_missing_models_dir_is_not_crash(tmp_path):
    assert P.read_profile_source("qwen", tmp_path / "absent")["ok"] is False


def test_find_profile_path_returns_none_for_root_file_without_engine(tmp_path):
    """根目录 YAML 且无显式 engine：engine 决定 worker 写盘子目录，宁可拒发也不猜。"""
    (tmp_path / "loose.yaml").write_text("port: 1\n", encoding="utf-8")
    assert P.find_profile_path("loose", tmp_path) is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cluster_profiles.py -q`
Expected: FAIL —`ModuleNotFoundError: No module named 'modelctl.core.cluster.profiles'`

- [ ] **Step 3: 实现** — 创建 `src/modelctl/core/cluster/profiles.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/profiles.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : 中心侧 profile 源读取（原文 + sha256 + 路径安全，供 goal 下发）
# ===============================================================================

"""core/cluster/profiles.py — goal 下发内容的来源读取（设计文档 §6.1/§6.3）。

中心把本机 models/<engine>/<name>.yaml 的**原始文本**作为下发内容，三个理由：
1) `${VAR}` 占位符必须由 worker 本地 envfile 解析——中心若插值，API_KEY 明文会
   随 sync 进入中心 SQLite 与网络帧，违反 §10.5"密钥不出中心"；
2) `profile_sha` 取原始文本而非"safe_load 后再 dump"，避免键序/引号风格等往返
   差异被 worker 侧漂移检测误判成本地篡改；
3) 下发的 name/engine 就是 worker 侧写盘路径成分，故白名单正则 + KNOWN_ENGINES
   双闸，杜绝 `../` 穿越与任意目录写。

本模块**不 import fastapi、不碰网络**，纯文件系统读取，可脱离中心单测。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.profile import KNOWN_ENGINES

#: 与 core.profile 的命名现实一致：ASCII 字母数字开头，允许 . _ -，总长 ≤ 64
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: 单份 profile YAML 上限；worker 写盘前再校验一次（双侧纵深防御）
MAX_YAML_BYTES = 256 * 1024


def is_safe_name(value: str) -> bool:
    """路径安全名（同时用于 goal_id 组成与 worker 侧写盘文件名）。"""
    return bool(value) and SAFE_NAME_RE.fullmatch(value) is not None


def profile_sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_profile_version(sha: str) -> str:
    """可读且可追溯的版本占位（Triton version policy 借鉴）：日期 + sha 前缀。"""
    return f"{_dt.date.today().isoformat()}-{sha[len('sha256:'):13]}"


def find_profile_path(name: str, models_dir: Path | None = None) -> tuple[Path, str] | None:
    """按 name 定位 (path, engine)。查找顺序与 core.profile.load_profile 一致。

    engine 判定：YAML 显式 `engine:` 优先，否则取父目录名；两者都必须落在
    KNOWN_ENGINES 内——engine 决定 worker 侧写入哪个引擎子目录，猜错会让模型
    加载到错误引擎，宁可拒发。
    """
    root = models_dir or PROJECT_ROOT / "models"
    if not root.is_dir():
        return None
    for path in [root / f"{name}.yaml", *sorted(root.rglob(f"{name}.yaml"))]:
        if not path.is_file():
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue  # 交由 read_profile_source 给出具体 reason
        engine = raw.get("engine") if isinstance(raw, dict) else None
        engine = engine.strip().lower() if isinstance(engine, str) and engine.strip() else path.parent.name.lower()
        if engine in KNOWN_ENGINES:
            return path, engine
    return None


def read_profile_source(name: str, models_dir: Path | None = None) -> dict[str, Any]:
    """读取下发源；一切失败返回 {"ok": False, "reason": ...}，**绝不抛异常**。

    返回的 `yaml` 是待下发原文（含未插值占位符），`raw` 是同一文本的解析结果
    （未插值），供 gate 做 GPU 数/显存估算——刻意不走 load_profile 的插值路径，
    因为中心 .env 未必定义 worker 侧的变量（缺变量时 load_profile 会抛错）。
    """
    if not is_safe_name(name):
        return {"name": name, "ok": False, "reason": f"非法 profile 名: {name!r}（仅允许字母数字与 . _ -）"}
    root = models_dir or PROJECT_ROOT / "models"
    if not root.is_dir():
        return {"name": name, "ok": False, "reason": f"profile {name!r} 不存在（{root} 目录缺失）"}

    # 先按文件定位，再判定 engine，以便对"engine 未知"给出精确 reason（而非笼统"不存在"）
    candidates = [p for p in [root / f"{name}.yaml", *sorted(root.rglob(f"{name}.yaml"))] if p.is_file()]
    if not candidates:
        return {"name": name, "ok": False,
                "reason": f"profile {name!r} 不存在（models/<engine>/{name}.yaml 未找到）"}

    last_reason = ""
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            last_reason = f"读取失败: {exc}"
            continue
        if len(text.encode("utf-8")) > MAX_YAML_BYTES:
            last_reason = f"profile 过大（>{MAX_YAML_BYTES} B），拒绝下发"
            continue
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            last_reason = f"YAML 语法错误: {exc}"
            continue
        if not isinstance(raw, dict):
            last_reason = "profile 顶层必须是映射"
            continue
        if raw.get("port") in (None, ""):
            last_reason = "profile 缺 port（worker 侧写盘前置校验项）"
            continue
        explicit = raw.get("engine")
        engine = (explicit.strip().lower() if isinstance(explicit, str) and explicit.strip()
                  else path.parent.name.lower())
        if engine not in KNOWN_ENGINES:
            last_reason = (f"engine {engine!r} 不在 KNOWN_ENGINES（{sorted(KNOWN_ENGINES)}）内，"
                           "请显式设置 engine: 或放入已知引擎子目录")
            continue
        sha = profile_sha(text)
        return {"name": name, "engine": engine, "path": str(path), "yaml": text,
                "sha": sha, "version": default_profile_version(sha), "raw": raw, "ok": True}
    return {"name": name, "ok": False, "reason": last_reason or f"profile {name!r} 不可用"}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_cluster_profiles.py -q`
Expected: PASS（13 条）

- [ ] **Step 5: 提交**

```bash
git add src/modelctl/core/cluster/profiles.py tests/test_cluster_profiles.py
git commit -m "feat(cluster): profile 源读取（原文下发 + sha256 + 路径安全双闸）"
```

---

### Task 4: `cluster/gate.py`——placement gate 纯函数 + dry-run 报告

goal 下发前的中心侧校验门禁（spec §6.6，Ray Placement Group / autoscaler 借鉴）。纯函数、零 I/O、零 DB：输入候选节点 + profile 事实 + 在用 GPU，输出逐节点 `ok/skip/error` 与 reason。`--dry-run` 只跑它。

**Files:**
- Create: `src/modelctl/core/cluster/gate.py`
- Test: `tests/test_cluster_gate.py`

**Interfaces:**
- Consumes: `cluster.profiles`（`read_profile_source` 的 `raw`）、`modelctl.core.profile.Profile`（dataclass 直接构造，不走插值）、`modelctl.core.vram_estimator.kv_estimate_for_profile`、`modelctl.core.colors.pad_width`
- Produces:
  - `RESULT_OK / RESULT_SKIP / RESULT_ERROR = "ok" / "skip" / "error"`
  - `@dataclass NodeVerdict: node_id: str; result: str; reason: str = ""`
  - `declared_gpu_count(raw: dict, engine: str) -> int`（vllm/sglang 读 `tensor_parallel_size`，llamacpp 读 `gpu_count`，其余 1；异常一律 1）
  - `estimate_vram_mb(raw: dict, engine: str, name: str) -> int | None`（构造未插值 `Profile` → `kv_estimate_for_profile`，任何异常/None → None）
  - `evaluate_gate(*, candidates, source, in_use, existing_goal_ids, lan_allow, create, profile_exists) -> list[NodeVerdict]`
  - `format_gate_report(verdicts: list[NodeVerdict], *, created: int, dry_run: bool) -> str`

**`source` 约定**（Task 5 负责产出）：`{"name","ok",...}`；`ok=True` 时含 `engine/raw/yaml/sha/version`，且 Task 5 会附加 gate 需要的 `gpu_count: int`、`min_vram_mb: int`、`requested_gpus: list[int]`（来自 CLI/REST 参数与 profile 事实的合并结果）。

- [ ] **Step 1: 写失败测试** — 创建 `tests/test_cluster_gate.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_gate.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : placement gate 全分支测试（运行时/容量/冲突/LAN/幂等/创建/源失败/报告）
# ===============================================================================

from modelctl.core.cluster import gate as G

SRC = {"name": "qwen", "ok": True, "engine": "vllm", "yaml": "port: 8101\n",
       "sha": "sha256:" + "a" * 64, "version": "2026-09-04-aaaaaa",
       "raw": {"port": 8101, "engine_config": {"tensor_parallel_size": 2}},
       "gpu_count": 2, "min_vram_mb": 0, "requested_gpus": []}


def _node(nid, *, status="online", gpu_count=4, vram=40960, runtimes=None, lan=""):
    return {"node_id": nid, "status": status, "lan_id": lan,
            "capacity": {"gpu_count": gpu_count, "vram_total_mb": vram},
            "runtimes": {"vllm": {"ok": True}} if runtimes is None else runtimes}


def _ev(candidates=None, **over):
    kw = dict(candidates=[_node("w-1")], source=SRC, in_use={}, existing_goal_ids=set(),
              lan_allow=[], create=True, profile_exists={"w-1": True})
    kw.update(over)
    if candidates is not None:
        kw["candidates"] = candidates
    return G.evaluate_gate(**kw)


def _one(**over):
    return _ev(**over)[0]


def test_runtime_probe_failure_skips_with_setup_hint():
    v = _one(candidates=[_node("w-1", runtimes={"vllm": {"ok": False}})])
    assert v.result == "skip" and "env setup vllm" in v.reason


def test_runtime_unknown_is_treated_as_unavailable():
    """runtimes 为 None（节点从未上报）→ 保守 skip，避免盲发导致 worker 侧报错。"""
    assert _one(candidates=[_node("w-1", runtimes=None)]).result == "skip"


def test_gpu_capacity_shortage():
    v = _one(candidates=[_node("w-1", gpu_count=1)])
    assert v.result == "skip" and "gpu_count" in v.reason


def test_vram_capacity_shortage():
    v = _one(candidates=[_node("w-1", vram=1024)], source={**SRC, "min_vram_mb": 40960})
    assert v.result == "skip" and "vram" in v.reason


def test_gpu_conflict_with_in_use_sets():
    v = _one(source={**SRC, "requested_gpus": [1, 2]}, in_use={"w-1": [2, 3]})
    assert v.result == "skip" and "2" in v.reason and "GPU" in v.reason


def test_all_gpus_busy_blocks_even_without_request():
    v = _one(candidates=[_node("w-1", gpu_count=2)], in_use={"w-1": [0, 1]})
    assert v.result == "skip" and "已占满" in v.reason


def test_lan_allow_filter():
    v = _one(candidates=[_node("w-1", lan="lan-9")], lan_allow=["lan-1", "lan-2"])
    assert v.result == "skip" and "lan" in v.reason.lower()


def test_existing_goal_skipped_for_idempotency():
    assert _one(existing_goal_ids={"qwen@@w-1"}).reason.count("已存在") == 1


def test_missing_profile_without_create_is_skip():
    v = _one(create=False, profile_exists={"w-1": False})
    assert v.result == "skip" and "--create" in v.reason


def test_missing_profile_with_create_is_ok_and_flagged():
    v = _one(create=True, profile_exists={"w-1": False})
    assert v.result == "ok" and "待同步" in v.reason


def test_offline_candidate_skipped():
    assert "offline" in _one(candidates=[_node("w-1", status="offline")]).reason


def test_source_failure_is_error_and_short_circuits():
    got = _ev(candidates=[_node("w-1"), _node("w-2")], source={"name": "qwen", "ok": False,
                                                               "reason": "YAML 语法错误"})
    assert [v.result for v in got] == ["error", "error"]
    assert got[0].reason == "YAML 语法错误"


def test_candidate_order_and_reasons_preserved():
    got = _ev(candidates=[_node("w-1", vram=1024), _node("w-2", gpu_count=1), _node("w-3")],
              source={**SRC, "min_vram_mb": 40960})
    assert [v.node_id for v in got] == ["w-1", "w-2", "w-3"]
    assert [v.result for v in got] == ["skip", "skip", "ok"]


def test_no_capacity_reported_skips_vram_and_gpu_checks():
    """老 worker 未上报 capacity（None）：容量维度不可判，交由 worker 侧兜底 → ok。"""
    n = {"node_id": "w-old", "status": "online", "lan_id": "", "capacity": None,
         "runtimes": {"vllm": {"ok": True}}}
    assert _one(candidates=[n], source={**SRC, "min_vram_mb": 999999}).result == "ok"


def test_declared_gpu_count_per_engine():
    assert G.declared_gpu_count({"engine_config": {"tensor_parallel_size": 4}}, "vllm") == 4
    assert G.declared_gpu_count({"engine_config": {"gpu_count": 2}}, "llamacpp") == 2
    # unsloth 的字段名是 tensor_parallel（与 vram_estimator._ctx_tokens_and_gpus 同源，非 _size）
    assert G.declared_gpu_count({"engine_config": {"tensor_parallel": 3}}, "unsloth") == 3
    assert G.declared_gpu_count({}, "vllm") == 1
    assert G.declared_gpu_count({"engine_config": {"tensor_parallel_size": "bad"}}, "vllm") == 1
    assert G.declared_gpu_count(None, "vllm") == 1
    assert G.declared_gpu_count({"engine_config": {"tensor_parallel_size": 0}}, "vllm") == 1


def test_estimate_vram_returns_none_on_unparseable_or_missing_model():
    assert G.estimate_vram_mb({"port": 1}, "vllm", "x") is None
    assert G.estimate_vram_mb({"port": 1, "engine_config": {"tensor_parallel_size": 1}},
                              "totally-unknown-engine", "x") is None


def test_estimate_vram_returns_int_for_known_model():
    got = G.estimate_vram_mb(
        {"port": 8101, "engine_config": {"model": "Qwen/Qwen3-8B", "max_model_len": 32768,
                                         "tensor_parallel_size": 2}}, "vllm", "qwen")
    assert got is None or isinstance(got, int)   # 架构表命中则给 MB 数，否则 None（不抛）


def test_report_lists_every_node_with_result_and_counts():
    got = _ev(candidates=[_node("w-1", vram=1024), _node("w-2")], source={**SRC, "min_vram_mb": 40960})
    text = G.format_gate_report(got, created=1, dry_run=True)
    assert "w-1" in text and "w-2" in text
    assert text.startswith("[dry-run]") and "created=1" in text
    assert len(text.strip().split("\n")) == 3     # 两节点行 + 一行 summary


def test_report_dry_run_prefix_marker_for_cli():
    """CLI 靠 `[dry-run]` 前缀区分演练与真实下发（供测试断言）。"""
    text = G.format_gate_report([G.NodeVerdict("w-1", "ok")], created=0, dry_run=True)
    assert text.startswith("[dry-run]")
    assert not G.format_gate_report([G.NodeVerdict("w-1", "ok")], created=1, dry_run=False).startswith("[dry-run]")
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cluster_gate.py -q`
Expected: FAIL —`ModuleNotFoundError: No module named 'modelctl.core.cluster.gate'`

- [ ] **Step 3: 实现** — 创建 `src/modelctl/core/cluster/gate.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/gate.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : placement gate：goal 下发前的中心侧容量/运行时/冲突校验（§6.6）
# ===============================================================================

"""core/cluster/gate.py — 下发校验门禁（纯函数；Ray Placement Group 借鉴）。

只做**粗筛**：中心掌握的节点事实全部来自心跳（capacity/runtimes/在用 GPU），
卡位分配的真正裁决者是 worker 侧 gpu_lock。中心拦掉"明显放不下"的候选，边界
情形由 worker 侧兜底并上报 error_class=gpu_lock——与 §6.6 目标一致，但把精确
性责任放在信息完整的一侧。

显存估算刻意**不做 ${VAR} 插值**：直接以未插值的 raw dict 构造 Profile，因为中心
.env 未必定义 worker 侧变量（缺变量时 core.profile.load_profile 会抛 ProfileError）。
估算失败一律返回 None → 跳过该维度校验，绝不因估算不可得而阻断下发。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from modelctl.core.colors import pad_width

RESULT_OK, RESULT_SKIP, RESULT_ERROR = "ok", "skip", "error"

#: 可参与下发的节点状态（offline/disabled 不发；stale 允许但提示）
_GATEABLE = ("online", "stale")

#: 各引擎"用几张卡"的字段名（与 core.vram_estimator._ctx_tokens_and_gpus 同源）
_GPU_COUNT_KEYS = {
    "vllm": "tensor_parallel_size",
    "sglang": "tensor_parallel_size",
    "aphrodite": "tensor_parallel_size",
    "tensorrt_llm": "tensor_parallel_size",
    "llamacpp": "gpu_count",
    "unsloth": "tensor_parallel",
}

_MARKS = {"ok": "OK  ", "skip": "SKIP", "error": "ERR "}


@dataclass
class NodeVerdict:
    node_id: str
    result: str
    reason: str = ""


def declared_gpu_count(raw: Any, engine: str) -> int:
    """从 profile 原文推断所需 GPU 数；任何异常/缺字段一律保守取 1。"""
    if not isinstance(raw, dict):
        return 1
    ec = raw.get("engine_config")
    if not isinstance(ec, dict):
        return 1
    key = _GPU_COUNT_KEYS.get(engine, "gpu_count")
    try:
        return max(1, int(ec.get(key, 1) or 1))
    except (TypeError, ValueError):
        return 1


def _safe_port(value: Any) -> int:
    """port 容错解析（profile 原文里可能是 str/int/None；估算路径不参与实际启动）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def estimate_vram_mb(raw: Any, engine: str, name: str) -> int | None:
    """KV 显存估算（MB）；不可估算一律 None（gate 跳过该维度，不阻断下发）。

    复用仓库既有 vram_estimator，不重复实现模型架构表。`engine` 不在
    vram_estimator 支持范围时它内部返回 None，故这里无需引擎白名单。
    """
    if not isinstance(raw, dict):
        return None
    try:
        from modelctl.core.profile import Profile
        from modelctl.core.vram_estimator import kv_estimate_for_profile

        ec = raw.get("engine_config")
        profile = Profile(name=name, engine=engine, port=_safe_port(raw.get("port")),
                          engine_config=ec if isinstance(ec, dict) else {})
        est = kv_estimate_for_profile(profile)
    except Exception:  # noqa: BLE001 — 估算在任何异常下都不得影响下发决策
        return None
    if not isinstance(est, dict) or est.get("kv_total_mb") is None:
        return None
    return int(est["kv_total_mb"])


def evaluate_gate(
    *,
    candidates: list[dict[str, Any]],
    source: dict[str, Any],
    in_use: dict[str, list[int]],
    existing_goal_ids: set[str],
    lan_allow: list[str],
    create: bool,
    profile_exists: dict[str, bool],
) -> list[NodeVerdict]:
    """逐候选判定（保序）。`source.ok=False` 时全部 error 并短路——源本身坏了，
    逐节点判定无意义且会产出误导性 reason。

    参数（由 Task 5 的 GoalService 组装）：
      in_use          node_id → 该节点已被占用的 GPU 序号列表
      existing_goal_ids 已存在的 goal_id 集合（`<profile>@@<node_id>`），用于幂等
      lan_allow       LAN 白名单（空 = 不限）
      profile_exists  node_id → 该节点本地是否已有同名 profile 文件（心跳上报）
    """
    if not source.get("ok"):
        reason = str(source.get("reason", "profile 源不可用"))
        return [NodeVerdict(str(c.get("node_id", "")), RESULT_ERROR, reason) for c in candidates]

    name = str(source.get("name", ""))
    engine = str(source.get("engine", ""))
    need_gpus = int(source.get("gpu_count") or declared_gpu_count(source.get("raw"), engine))
    min_vram = int(source.get("min_vram_mb") or 0)
    requested = [g for g in (source.get("requested_gpus") or []) if isinstance(g, int)]

    verdicts: list[NodeVerdict] = []
    for node in candidates:
        nid = str(node.get("node_id", ""))
        verdicts.append(_verdict_one(
            node_id=nid, node=node, name=name, engine=engine, need_gpus=need_gpus,
            min_vram=min_vram, requested=requested, in_use=in_use.get(nid) or [],
            exists=f"{name}@@{nid}" in existing_goal_ids, lan_allow=lan_allow,
            create=create, has_profile=bool(profile_exists.get(nid, False))))
    return verdicts


def _verdict_one(*, node_id: str, node: dict[str, Any], name: str, engine: str, need_gpus: int,
                 min_vram: int, requested: list[int], in_use: list[int], exists: bool,
                 lan_allow: list[str], create: bool, has_profile: bool) -> NodeVerdict:
    status = str(node.get("status", ""))
    if status not in _GATEABLE:
        return NodeVerdict(node_id, RESULT_SKIP, f"节点状态 {status or '未知'} 不可下发（需 online/stale）")
    if exists:
        return NodeVerdict(node_id, RESULT_SKIP, "goal 已存在（幂等跳过；改参请走 PUT 或先 remove）")
    if lan_allow and str(node.get("lan_id") or "") not in lan_allow:
        return NodeVerdict(node_id, RESULT_SKIP,
                           f"LAN {node.get('lan_id') or '未标注'} 不在 --lan-allow {lan_allow} 内")
    runtimes = node.get("runtimes")
    ok_runtime = bool(isinstance(runtimes, dict) and isinstance(runtimes.get(engine), dict)
                      and runtimes[engine].get("ok"))
    if not ok_runtime:
        detail = "（节点未上报运行时信息）" if not runtimes else ""
        return NodeVerdict(node_id, RESULT_SKIP,
                           f"engine {engine} 在 {node_id} 上不可用{detail}，需先 modelctl env setup {engine}")

    capacity = node.get("capacity")
    if isinstance(capacity, dict):
        have_gpus = capacity.get("gpu_count")
        if isinstance(have_gpus, int) and have_gpus < need_gpus:
            return NodeVerdict(node_id, RESULT_SKIP,
                               f"gpu_count 不足：需 {need_gpus} 卡，节点仅 {have_gpus} 卡")
        total_vram = capacity.get("vram_total_mb")
        if min_vram and isinstance(total_vram, int) and total_vram < min_vram:
            return NodeVerdict(node_id, RESULT_SKIP,
                               f"vram 不足：估算需 ≥{min_vram}MB，节点共 {total_vram}MB")

    busy = {g for g in in_use if isinstance(g, int)}
    clash = sorted(set(requested) & busy)
    if clash:
        return NodeVerdict(node_id, RESULT_SKIP,
                           f"GPU {clash} 已被在用模型占用（gpu_list 冲突，请改用空闲卡位）")
    if isinstance(capacity, dict) and isinstance(capacity.get("gpu_count"), int):
        if capacity["gpu_count"] and len(busy) >= capacity["gpu_count"]:
            return NodeVerdict(node_id, RESULT_SKIP,
                               f"节点 GPU 已占满（{sorted(busy)}），无空卡可分配")

    note = "" if has_profile else "（profile 文件待同步）"
    if not has_profile and not create:
        return NodeVerdict(node_id, RESULT_SKIP,
                           f"{node_id} 上无 profile {name}，未加 --create 故跳过")
    return NodeVerdict(node_id, RESULT_OK, f"engine={engine} gpu_count={need_gpus}{note}")


def format_gate_report(verdicts: list[NodeVerdict], *, created: int, dry_run: bool) -> str:
    """逐节点一行的报告（CJK 安全对齐；CLI 直接 print，不含 ANSI）。

    created 是"将创建/已创建"的 goal 数：dry-run 下为预计数，实跑下为实际数，
    文案前缀 [dry-run] 让调用方与测试都能区分演练与真实下发。
    """
    width = max((len(v.result) for v in verdicts), default=4)
    nid_width = max((len(v.node_id) for v in verdicts), default=5)
    head = "[dry-run] " if dry_run else ""
    lines = []
    for v in verdicts:
        mark = head + _MARKS.get(v.result, v.result)
        lines.append(f"{pad_width(mark, len(head) + width)}  {pad_width(v.node_id, nid_width)}  {v.reason}")
    tail = f"（dry-run：预计 created={created}）" if dry_run else f"（created: {created}）"
    lines.append(f"{head}summary: {tail}")
    return "\n".join(lines)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_cluster_gate.py -q`
Expected: PASS（20 条）

- [ ] **Step 5: 提交**

```bash
git add src/modelctl/core/cluster/gate.py tests/test_cluster_gate.py
git commit -m "feat(cluster): placement gate 纯函数（运行时/容量/冲突/LAN/幂等 + dry-run 报告）"
```

---

### Task 5: `cluster/goals.py`——GoalService（goal CRUD + 期望快照 + revision + env 白名单）

goal 的唯一写入口。三类职责：① `set_goals` 组装 gate 输入、跑门禁、只把 `ok` 的候选落库；② `snapshot_for(node_id)` 产出随心跳 ack 下发的全量快照 + `revision`（内容哈希，中心重启/重连后依然稳定）；③ `env_overlay` 白名单（密钥类键直接拒绝，落实 §10.5"密钥不出中心"）。

**Files:**
- Create: `src/modelctl/core/cluster/goals.py`
- Test: `tests/test_cluster_goals.py`

**Interfaces:**
- Consumes: `ClusterStore`（T1 的 goals CRUD）、`cluster.profiles.read_profile_source`、`cluster.gate.evaluate_gate`、`core.gpu_utils.parse_gpu_list`
- Produces:
  - `ENV_OVERLAY_ALLOWLIST: frozenset[str]`
  - `SECRET_KEY_HINTS: tuple[str, ...]`（`("API_KEY","TOKEN","SECRET","PASSWORD","KEY")`）
  - `validate_env_overlay(raw: dict | None) -> tuple[dict | None, str]`（返回清洗后的 dict 或错误文案）
  - `goal_id_of(profile: str, node_id: str) -> str`（`f"{profile}@@{node_id}"`）
  - `class GoalService(store: ClusterStore)`：
    - `set_goals(*, profile: str, node_ids: list[str] | None, all_nodes: bool = False, intent: str = "start", create: bool = False, params: dict | None = None, env_overlay: dict | None = None, gpu_list: list[int] | None = None, lan_allow: list[str] | None = None, runtime_ref: str | None = None, target_role: str = "primary", created_by: str = "", dry_run: bool = False, now: float | None = None) -> dict`
      → `{"verdicts": list[NodeVerdict], "report": str, "created": int, "skipped": int, "errors": int, "reason": str}`（`reason` 仅在源不可用/参数非法时非空）
    - `remove_goals(*, profile: str, node_ids: list[str] | None, all_nodes: bool = False, created_by: str = "", now: float | None = None) -> dict` → `{"removed": list[str], "missing": list[str], "report": str}`
    - `snapshot_for(node_id: str) -> dict` → `{"revision": str, "goals": [{"goal_id","profile","engine","yaml","sha","version","intent","params","env_overlay"}]}`
    - `mark_stage(goal_id: str, stage: str, *, reason: str = "", error_class: str = "", now: float | None = None) -> None`
    - `record_model_states(node_id: str, profiles: dict, now: float) -> None`（心跳回流落库，见 Task 7）

- [ ] **Step 1: 写失败测试** — 创建 `tests/test_cluster_goals.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_goals.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : GoalService（env 白名单 / gate 串联 / 快照 revision / stage 回写）
# ===============================================================================

import pytest

from modelctl.core.cluster.goals import (
    ENV_OVERLAY_ALLOWLIST, GoalService, goal_id_of, validate_env_overlay,
)
from modelctl.core.cluster.store import ClusterStore

YAML = "port: 8101\napi_key: ${API_KEY}\nengine_config:\n  tensor_parallel_size: 2\n"


@pytest.fixture()
def store(tmp_path):
    s = ClusterStore(tmp_path / "m.db")
    s.init_db()
    return s


@pytest.fixture()
def models(tmp_path):
    d = tmp_path / "models"
    (d / "vllm").mkdir(parents=True)
    (d / "vllm" / "qwen.yaml").write_text(YAML, encoding="utf-8")
    return d


@pytest.fixture()
def svc(store, models, monkeypatch):
    # gate 读中心 models/，测试把读取根目录指向 tmp（不改生产默认值）
    monkeypatch.setattr("modelctl.core.cluster.goals.MODELS_DIR", models)
    return GoalService(store)


def _online(store, nid, *, lan="", with_runtime=True):
    store.upsert_node(node_id=nid, node_token=f"NT-{nid}", lan_id=lan, role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    store.update_node_capacity(nid, capacity={"gpu_count": 4, "vram_total_mb": 157280},
                               runtimes=({"vllm": {"ok": True}} if with_runtime else {}),
                               local_profiles=["qwen"], now=1.0)


# ---------------- env_overlay 白名单 ----------------
def test_env_overlay_allowlist_covers_paths_only():
    assert {"MODEL_ROOT", "MODELSCOPE_CACHE", "HF_HOME", "OLLAMA_MODELS",
            "LOG_DIR", "AUDIT_DIR", "MODELCTL_GPUS"} <= ENV_OVERLAY_ALLOWLIST


def test_env_overlay_rejects_secret_keys_even_inside_allowlist_shape():
    got, err = validate_env_overlay({"MODEL_ROOT": "/m", "MODELSCOPE_API_KEY": "sk-1"})
    assert got is None and "API_KEY" in err
    got, err = validate_env_overlay({"MY_TOKEN": "x"})
    assert got is None and "TOKEN" in err


def test_env_overlay_rejects_unknown_key():
    got, err = validate_env_overlay({"NOPPE": "1"})
    assert got is None and "NOPPE" in err


def test_env_overlay_values_must_be_str():
    got, err = validate_env_overlay({"MODEL_ROOT": 1})
    assert got is None and "字符串" in err


def test_env_overlay_none_and_empty_are_ok():
    assert validate_env_overlay(None) == (None, "")
    assert validate_env_overlay({}) == (None, "")


def test_goal_id_of_uses_double_at():
    assert goal_id_of("qwen", "w-1") == "qwen@@w-1"


# ---------------- set_goals ----------------
def test_set_goals_creates_only_for_ok_candidates(store, svc):
    _online(store, "w-1")
    _online(store, "w-2", with_runtime=False)      # gate 应 skip
    out = svc.set_goals(profile="qwen", node_ids=["w-1", "w-2"], create=True, created_by="op")
    assert out["created"] == 1 and out["skipped"] == 1
    assert [g["node_id"] for g in store.list_goals()] == ["w-1"]
    assert store.get_goal("qwen@@w-1")["stage"] == "PENDING_PROFILE_SYNC"


def test_set_goals_all_nodes_targets_every_gateable_node(store, svc):
    _online(store, "w-1")
    _online(store, "w-2")
    store.upsert_node(node_id="w-9", node_token="NT-9", lan_id="", role="worker", host_ip="",
                      hostname="", engines=None, now=1.0)
    store.set_node_status("w-9", "offline")
    out = svc.set_goals(profile="qwen", node_ids=None, all_nodes=True, create=True)
    assert out["created"] == 2
    assert {g["node_id"] for g in store.list_goals()} == {"w-1", "w-2"}


def test_set_goals_is_idempotent_second_call_creates_zero(store, svc):
    _online(store, "w-1")
    assert svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)["created"] == 1
    out = svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    assert out["created"] == 0 and out["skipped"] == 1
    assert len(store.list_goals()) == 1


def test_set_goals_dry_run_writes_nothing(store, svc):
    _online(store, "w-1")
    out = svc.set_goals(profile="qwen", node_ids=["w-1"], create=True, dry_run=True)
    assert out["created"] == 0 and out["report"].startswith("[dry-run]")
    assert store.list_goals() == []
    assert store.get_goal("qwen@@w-1") is None


def test_set_goals_missing_profile_source_is_error(store, svc):
    _online(store, "w-1")
    out = svc.set_goals(profile="ghost", node_ids=["w-1"], create=True)
    assert out["created"] == 0 and out["errors"] == 1
    assert "不存在" in out["reason"]
    assert store.list_goals() == []


def test_set_goals_bad_env_overlay_is_rejected_before_write(store, svc):
    _online(store, "w-1")
    out = svc.set_goals(profile="qwen", node_ids=["w-1"], create=True,
                        env_overlay={"API_KEY": "sk"})
    assert out["created"] == 0 and "API_KEY" in out["reason"]
    assert store.list_goals() == []


def test_set_goals_writes_gpu_list_into_params_and_placement(store, svc):
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True, gpu_list=[0, 1])
    g = store.get_goal("qwen@@w-1")
    assert g["params"]["gpu_list"] == [0, 1]
    assert g["placement"]["gpu_count"] == 2


def test_set_goals_records_event(store, svc):
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True, created_by="op")
    kinds = [e["kind"] for e in store.recent_events(limit=20, node_id="w-1")]
    assert "goal.create" in kinds


def test_set_goals_rejects_unknown_intent(store, svc):
    _online(store, "w-1")
    assert "intent" in svc.set_goals(profile="qwen", node_ids=["w-1"], intent="destroy")["reason"]


# ---------------- snapshot ----------------
def test_snapshot_shape_and_raw_passthrough(store, svc):
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True, gpu_list=[0])
    snap = svc.snapshot_for("w-1")
    assert len(snap["goals"]) == 1
    g = snap["goals"][0]
    assert g["goal_id"] == "qwen@@w-1" and g["engine"] == "vllm"
    assert "${API_KEY}" in g["yaml"]                    # 原文下发，未插值
    assert g["sha"] in YAML and g["params"]["gpu_list"] == [0]


def test_snapshot_revision_is_stable_and_content_sensitive(store, svc):
    _online(store, "w-1")
    _online(store, "w-2")
    svc.set_goals(profile="qwen", node_ids=["w-1", "w-2"], create=True)
    a = svc.snapshot_for("w-1")["revision"]
    assert svc.snapshot_for("w-1")["revision"] == a          # 稳定（中心重启后同值）
    svc.set_goals(profile="qwen", node_ids=["w-2"], create=True, intent="stop")
    assert svc.snapshot_for("w-1")["revision"] == a          # 他节点变更不影响本节点
    svc.store.update_goal("qwen@@w-1", now=9.0, intent="stop")
    assert svc.snapshot_for("w-1")["revision"] != a          # 本节点变更 → revision 变


def test_snapshot_empty_for_unknown_node(store, svc):
    assert svc.snapshot_for("nobody") == {"revision": "", "goals": []}


# ---------------- remove / stage / model_states ----------------
def test_remove_goals_snapshots_yaml_for_worker_prune(store, svc):
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    out = svc.remove_goals(profile="qwen", node_ids=["w-1"], created_by="op")
    assert out["removed"] == ["qwen@@w-1"] and store.list_goals() == []
    assert out["report"] and "qwen" in out["report"]


def test_remove_goals_reports_missing(store, svc):
    assert svc.remove_goals(profile="qwen", node_ids=["w-1"])["missing"] == ["qwen@@w-1"]


def test_remove_all_nodes(store, svc):
    _online(store, "w-1")
    _online(store, "w-2")
    svc.set_goals(profile="qwen", node_ids=None, all_nodes=True, create=True)
    assert len(svc.remove_goals(profile="qwen", node_ids=None, all_nodes=True)["removed"]) == 2


def test_mark_stage_writes_reason_and_error_class(store, svc):
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    svc.mark_stage("qwen@@w-1", "FAILED", reason="venv 缺失", error_class="venv_missing", now=5.0)
    g = store.get_goal("qwen@@w-1")
    assert g["stage"] == "FAILED" and g["error_class"] == "venv_missing"
    assert g["stage_reason"] == "venv 缺失"


def test_mark_stage_on_absent_goal_is_silent(store, svc):
    svc.mark_stage("nope@@w-1", "READY")     # 不抛：心跳里可能出现已删 goal 的残留状态


def test_record_model_states_overwrites_and_prunes(store, svc):
    _online(store, "w-1")
    svc.record_model_states("w-1", {"qwen": {"state": "READY", "port": 8101,
                                             "gpu": [0, 1], "pid": 7}}, now=1.0)
    assert store.list_model_states(node_id="w-1")[0]["state"] == "READY"
    svc.record_model_states("w-1", {}, now=2.0)          # 空集 = 该节点当前无在跑模型
    assert store.list_model_states(node_id="w-1") == []


def test_record_model_states_ignores_non_dict_entries(store, svc):
    _online(store, "w-1")
    svc.record_model_states("w-1", {"qwen": "junk", "ok": {"state": "UP"}}, now=1.0)
    assert [r["profile"] for r in store.list_model_states(node_id="w-1")] == ["ok"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cluster_goals.py -q`
Expected: FAIL —`ModuleNotFoundError: No module named 'modelctl.core.cluster.goals'`

- [ ] **Step 3: 实现** — 创建 `src/modelctl/core/cluster/goals.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/goals.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : GoalService：声明式目标的唯一写入口（§6.1/§6.6/§10.5）
# ===============================================================================

"""core/cluster/goals.py — goal 的创建/删除/期望快照（source of truth 写入侧）。

设计要点：
1) **只有 gate 判定 ok 的候选才落库**——中心从不"先写 goal 再等 worker 报错"，
   避免把明显放不下的目标推到 worker 上产生 OOM 循环（§6.6 的动机）。
2) **快照 = 全量 + 内容哈希 revision**：中心不记投递水位、不做重试。worker 上报
   自己的 revision，中心只在两者不同时带回全量快照，于是"重连/中心重启/丢帧"
   三种情况共用一条自愈路径。
3) **env_overlay 白名单**：只允许路径/卡位类键，任何名字含 API_KEY/TOKEN/... 的
   键直接拒绝。profile YAML 走原文下发（占位符不插值），密钥因此永不出中心。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from modelctl.core.cluster import gate, profiles
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.cluster.store import ClusterStore

#: 测试/多实例可整体替换的 profile 读取根（默认为仓库 models/）
MODELS_DIR = PROJECT_ROOT / "models"

#: 允许中心下发的 env 覆盖键：全部是路径或卡位，无一是凭据（§6.4）
ENV_OVERLAY_ALLOWLIST: frozenset[str] = frozenset({
    "MODEL_ROOT", "MODELSCOPE_CACHE", "HF_HOME", "OLLAMA_MODELS",
    "LOG_DIR", "AUDIT_DIR", "MODELCTL_GPUS",
})

#: 键名命中任一子串即拒绝（纵深防御：防止有人把密钥塞进白名单形状的键里）
SECRET_KEY_HINTS: tuple[str, ...] = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "KEY")

VALID_INTENTS: tuple[str, ...] = ("start", "stop")
VALID_TARGET_ROLES: tuple[str, ...] = ("primary", "replica", "benchmark")

#: 单节点 goal 上限（防误操作 --all 打爆 worker 磁盘与心跳体积）
MAX_GOALS_PER_NODE = 512


def goal_id_of(profile: str, node_id: str) -> str:
    return f"{profile}@@{node_id}"


def validate_env_overlay(raw: dict | None) -> tuple[dict | None, str]:
    """清洗 env 覆盖项。返回 (清洗后的 dict 或 None, 错误文案)。

    None/{} 视为"无覆盖"→ (None, "")；任何越界一律整份拒绝而非静默丢键，
    否则调用方会以为"部分生效"而误判结果。
    """
    if raw is None:
        return None, ""
    if not isinstance(raw, dict):
        return None, "env_overlay 必须是映射"
    if not raw:
        return None, ""
    clean: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            return None, f"env_overlay 键必须是字符串：{key!r}"
        upper = key.strip().upper()
        if any(h in upper for h in SECRET_KEY_HINTS):
            return None, f"env_overlay 禁止下发凭据类键 {key!r}（worker 本地 .env 自行配置）"
        if upper not in ENV_OVERLAY_ALLOWLIST:
            return None, (f"env_overlay 键 {key!r} 不在白名单 "
                          f"{sorted(ENV_OVERLAY_ALLOWLIST)} 内")
        if not isinstance(value, str):
            return None, f"env_overlay.{key} 的值必须是字符串，收到 {type(value).__name__}"
        clean[upper] = value
    return clean, ""


class GoalService:
    """goal 读写编排。所有写操作都记 events（§10.7 审计）。"""

    def __init__(self, store: ClusterStore) -> None:
        self.store = store

    # ---------------- 创建 / 删除 ----------------
    def set_goals(
        self, *, profile: str, node_ids: list[str] | None, all_nodes: bool = False,
        intent: str = "start", create: bool = False, params: dict | None = None,
        env_overlay: dict | None = None, gpu_list: list[int] | None = None,
        lan_allow: list[str] | None = None, runtime_ref: str | None = None,
        target_role: str = "primary", created_by: str = "", dry_run: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """按 gate 结论批量建 goal。永不抛业务异常：一切失败以 verdicts/reason 呈现。"""
        now = time.time() if now is None else now
        if intent not in VALID_INTENTS:
            return self._abort(f"非法 intent {intent!r}（仅 {VALID_INTENTS}）")
        if target_role not in VALID_TARGET_ROLES:
            return self._abort(f"非法 target_role {target_role!r}（仅 {VALID_TARGET_ROLES}）")
        overlay, err = validate_env_overlay(env_overlay)
        if err:
            return self._abort(err)

        source = profiles.read_profile_source(profile, MODELS_DIR)
        candidates = self._candidates(node_ids=node_ids, all_nodes=all_nodes)
        if not source.get("ok"):
            verdicts = gate.evaluate_gate(candidates=candidates or [{"node_id": n} for n in (node_ids or [])],
                                          source=source, in_use={}, existing_goal_ids=set(),
                                          lan_allow=lan_allow or [], create=create, profile_exists={})
            report = gate.format_gate_report(verdicts, created=0, dry_run=dry_run)
            return {"verdicts": verdicts, "report": report, "created": 0, "skipped": 0,
                    "errors": len(verdicts), "reason": str(source.get("reason", ""))}
        if not candidates:
            return self._abort("无可下发节点（--node 指定的节点不存在或已 offline）")

        # gpu_list 同时进 params（worker 写盘时并入 YAML）与 placement（gate 冲突判定用）
        merged_params = dict(params or {})
        if gpu_list:
            merged_params["gpu_list"] = list(gpu_list)
        need_gpus = len(gpu_list) if gpu_list else gate.declared_gpu_count(source.get("raw"), source["engine"])
        est = gate.estimate_vram_mb(source.get("raw"), source["engine"], profile) or 0
        enriched = {**source, "gpu_count": need_gpus, "min_vram_mb": est,
                    "requested_gpus": list(gpu_list or [])}

        in_use = self._in_use_gpus([str(c["node_id"]) for c in candidates])
        existing = {g["goal_id"] for g in self.store.list_goals(profile=profile)}
        has_profile = {str(c["node_id"]): profile in (c.get("local_profiles") or []) for c in candidates}
        verdicts = gate.evaluate_gate(candidates=candidates, source=enriched, in_use=in_use,
                                      existing_goal_ids=existing, lan_allow=lan_allow or [],
                                      create=create, profile_exists=has_profile)

        created = 0
        for v in verdicts:
            if v.result != gate.RESULT_OK:
                continue
            if dry_run:
                created += 1
                continue
            if self._count_for_node(v.node_id) >= MAX_GOALS_PER_NODE:
                v.result, v.reason = gate.RESULT_SKIP, f"节点 goal 数已达上限 {MAX_GOALS_PER_NODE}"
                continue
            self._write_goal(profile=profile, source=source, node_id=v.node_id, intent=intent,
                             params=merged_params, env_overlay=overlay, gpu_count=need_gpus,
                             runtime_ref=runtime_ref, target_role=target_role,
                             created_by=created_by, now=now)
            created += 1
        report = gate.format_gate_report(verdicts, created=created, dry_run=dry_run)
        return {"verdicts": verdicts, "report": report, "created": created,
                "skipped": sum(1 for v in verdicts if v.result == gate.RESULT_SKIP),
                "errors": sum(1 for v in verdicts if v.result == gate.RESULT_ERROR), "reason": ""}

    def remove_goals(self, *, profile: str, node_ids: list[str] | None,
                     all_nodes: bool = False, created_by: str = "",
                     now: float | None = None) -> dict[str, Any]:
        """删 goal。worker 侧的 YAML 剪枝无需中心额外传话：goal 消失后下一次快照
        就不含它，worker 的 managed 清单对照快照即知要删（Task 8 的 prune）。
        """
        now = time.time() if now is None else now
        targets = self._targets_for_removal(profile=profile, node_ids=node_ids, all_nodes=all_nodes)
        removed: list[str] = []
        for goal_id in targets:
            gone = self.store.delete_goal(goal_id)
            if gone is None:
                continue
            removed.append(goal_id)
            self.store.delete_model_state(str(gone["node_id"]), profile)
            self.store.append_event("goal.delete", node_id=str(gone["node_id"]), goal_id=goal_id,
                                    payload={"profile": profile, "operator": created_by}, now=now)
        missing = [g for g in targets if g not in removed]
        report = (f"已删除 {len(removed)} 个 goal" + (f"；不存在 {len(missing)} 个" if missing else "")
                  if targets else f"profile {profile} 无任何 goal")
        return {"removed": removed, "missing": missing, "report": report}

    # ---------------- 下发快照（心跳 ack 捎带）----------------
    def snapshot_for(self, node_id: str) -> dict[str, Any]:
        """该节点的全量期望状态。revision 是内容哈希：同一 goal 集在中心重启后同值，
        故 worker 端"要不要重写盘"的判据在两侧都稳定。空节点用空串（不是哈希）。
        """
        rows = self.store.list_goals(node_id=node_id)
        goals = [{"goal_id": g["goal_id"], "profile": g["profile"], "engine": g["engine"],
                  "yaml": g["profile_yaml"], "sha": g["profile_sha"],
                  "version": g.get("profile_version") or "", "intent": g["intent"],
                  "params": g.get("params"), "env_overlay": g.get("env_overlay")} for g in rows]
        if not goals:
            return {"revision": "", "goals": []}
        canon = json.dumps(goals, sort_keys=True, ensure_ascii=False)
        return {"revision": hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16], "goals": goals}

    # ---------------- worker 回流（Task 7 调用）----------------
    def mark_stage(self, goal_id: str, stage: str, *, reason: str = "",
                   error_class: str = "", now: float | None = None) -> None:
        """回写 goal 生命周期阶段。goal 可能已被删（worker 状态滞后）→ 静默忽略。"""
        self.store.update_goal(goal_id, now=time.time() if now is None else now,
                               stage=stage, stage_reason=reason[:500], error_class=error_class)

    def record_model_states(self, node_id: str, profiles_reported: dict, now: float) -> None:
        """心跳回流：全量覆盖该节点的 model_states（空 dict 表示"当前无在跑模型"）。"""
        seen: set[str] = set()
        for name, info in profiles_reported.items():
            if not isinstance(info, dict) or not profiles.is_safe_name(str(name)):
                continue
            seen.add(str(name))
            self.store.upsert_model_state(node_id=node_id, profile=str(name),
                                          state=str(info.get("state", "unknown"))[:32],
                                          gpu=_int_list(info.get("gpu")),
                                          port=_safe_int(info.get("port")),
                                          pid=_safe_int(info.get("pid")),
                                          reason=str(info.get("reason", ""))[:500],
                                          error_class=str(info.get("error_class", ""))[:64],
                                          now=now)
        for row in self.store.list_model_states(node_id=node_id):
            if row["profile"] not in seen:
                self.store.delete_model_state(node_id, row["profile"])

    # ---------------- 内部 ----------------
    def _abort(self, reason: str) -> dict[str, Any]:
        return {"verdicts": [], "report": "", "created": 0, "skipped": 0, "errors": 0, "reason": reason}

    def _candidates(self, *, node_ids: list[str] | None, all_nodes: bool) -> list[dict[str, Any]]:
        """gate 候选 = 台账行（含 capacity/runtimes/local_profiles）；指定 --node 时保序。"""
        rows = {n["node_id"]: n for n in self.store.list_nodes()}
        if all_nodes:
            return [n for n in self.store.list_nodes() if not n.get("disabled")]
        return [rows[n] for n in (node_ids or []) if n in rows]

    def _in_use_gpus(self, node_ids: list[str]) -> dict[str, list[int]]:
        """在用 GPU：优先 model_states（更精确），回退 worker 侧 gpu_lock 上报。"""
        out: dict[str, list[int]] = {}
        for row in self.store.list_model_states():
            if row["node_id"] not in node_ids or row["state"] not in ("READY", "STARTING", "UP"):
                continue
            for g in (row.get("gpu") or []):
                out.setdefault(row["node_id"], []).append(int(g))
        return out

    def _count_for_node(self, node_id: str) -> int:
        return len(self.store.list_goals(node_id=node_id))

    def _write_goal(self, *, profile: str, source: dict, node_id: str, intent: str,
                    params: dict, env_overlay: dict | None, gpu_count: int,
                    runtime_ref: str | None, target_role: str, created_by: str, now: float) -> None:
        goal_id = goal_id_of(profile, node_id)
        existed = self.store.get_goal(goal_id) is not None
        self.store.upsert_goal(
            goal_id=goal_id, node_id=node_id, profile=profile, engine=str(source["engine"]),
            profile_yaml=str(source["yaml"]), profile_sha=str(source["sha"]),
            profile_version=str(source.get("version") or ""), intent=intent,
            params=params or None, env_overlay=env_overlay,
            placement={"gpu_count": gpu_count, "min_vram_mb": int(source.get("min_vram_mb") or 0)},
            runtime_ref=runtime_ref, target_role=target_role,
            stage="PENDING_PROFILE_SYNC", created_by=created_by, now=now)
        self.store.append_event("goal.update" if existed else "goal.create", node_id=node_id,
                                goal_id=goal_id,
                                payload={"profile": profile, "intent": intent,
                                         "engine": source["engine"], "operator": created_by}, now=now)

    def _targets_for_removal(self, *, profile: str, node_ids: list[str] | None,
                             all_nodes: bool) -> list[str]:
        if all_nodes:
            return [g["goal_id"] for g in self.store.list_goals(profile=profile)]
        return [goal_id_of(profile, n) for n in (node_ids or [])]


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _int_list(value: Any) -> list[int] | None:
    if not isinstance(value, list):
        return None
    return [v for v in value if isinstance(v, int) and not isinstance(v, bool)][:64]
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_cluster_goals.py -q`
Expected: PASS（23 条）

- [ ] **Step 5: 提交**

```bash
git add src/modelctl/core/cluster/goals.py tests/test_cluster_goals.py
git commit -m "feat(cluster): GoalService（gate 串联 + 内容哈希快照 + env 白名单 + 审计事件）"
```

---

### Task 6: `cluster/conns.py`——连接世代表（同 node_id 后来者胜）

M0 未处理"同一 node_id 两条 WS 并存"（旧连接尚未感知断开）。M1 起中心会随心跳 ack 投递 action/goal 快照：若旧连接也活着，指令会投给僵尸连接、或被双份执行。本任务给出最小机制——**世代表**：新连接登记一个递增 epoch，旧连接每轮心跳发现 epoch 不匹配即自行退出。

**Files:**
- Create: `src/modelctl/core/cluster/conns.py`
- Test: `tests/test_cluster_conns.py`

**Interfaces:**
- Consumes: 无（纯 stdlib + threading.Lock）
- Produces:
  - `class ConnectionRegistry`：
    - `join(node_id: str) -> int`（登记新连接，返回其 epoch；同 node_id 的旧 epoch 随即失效）
    - `is_current(node_id: str, epoch: int) -> bool`
    - `current_epoch(node_id: str) -> int | None`
    - `release(node_id: str, epoch: int) -> None`（仅当 epoch 是自己时才摘除）
    - `count() -> int`

- [ ] **Step 1: 写失败测试** — 创建 `tests/test_cluster_conns.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_conns.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : 连接世代表（后来者胜 / release 幂等 / 跨节点互不影响）
# ===============================================================================

from modelctl.core.cluster.conns import ConnectionRegistry


def test_join_returns_monotonic_epochs():
    reg = ConnectionRegistry()
    first = reg.join("w-1")
    second = reg.join("w-1")
    assert second > first


def test_new_join_supersedes_old_epoch():
    """worker 重启后旧进程的连接必须被判非当前，从而停止消费 ack（避免双份执行）。"""
    reg = ConnectionRegistry()
    old = reg.join("w-1")
    new = reg.join("w-1")
    assert reg.is_current("w-1", new) and not reg.is_current("w-1", old)


def test_nodes_are_isolated():
    reg = ConnectionRegistry()
    a = reg.join("w-1")
    b = reg.join("w-2")
    assert reg.is_current("w-1", a) and reg.is_current("w-2", b)
    assert not reg.is_current("w-1", b)


def test_unknown_node_or_epoch_is_never_current():
    reg = ConnectionRegistry()
    assert not reg.is_current("ghost", 1)
    assert reg.current_epoch("ghost") is None


def test_release_only_clears_own_epoch():
    """旧连接的 finally 绝不能把新连接踢下线（否则会误杀活着的 worker）。"""
    reg = ConnectionRegistry()
    old = reg.join("w-1")
    new = reg.join("w-1")
    reg.release("w-1", old)
    assert reg.is_current("w-1", new) and reg.count() == 1
    reg.release("w-1", new)
    assert reg.current_epoch("w-1") is None and reg.count() == 0


def test_release_is_idempotent_and_tolerates_unknown():
    reg = ConnectionRegistry()
    e = reg.join("w-1")
    reg.release("w-1", e)
    reg.release("w-1", e)          # 二次释放不抛
    reg.release("ghost", 99)       # 未知节点不抛
    assert reg.count() == 0


def test_rejoin_after_release_advances_epoch():
    reg = ConnectionRegistry()
    e1 = reg.join("w-1")
    reg.release("w-1", e1)
    e2 = reg.join("w-1")
    assert e2 > e1 and reg.is_current("w-1", e2)


def test_concurrent_joins_keep_exactly_one_current():
    """并发 join 不得出现两条同时 current（否则指令会被双投）。"""
    import threading

    reg = ConnectionRegistry()
    epochs: list[int] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        epochs.append(reg.join("w-1"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for e in epochs if reg.is_current("w-1", e)) == 1
    assert reg.count() == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cluster_conns.py -q`
Expected: FAIL —`ModuleNotFoundError: No module named 'modelctl.core.cluster.conns'`

- [ ] **Step 3: 实现** — 创建 `src/modelctl/core/cluster/conns.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/conns.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : 节点连接世代表（同 node_id 后来者胜；M1 action/sync 投递的正确性前提）
# ===============================================================================

"""core/cluster/conns.py — WS 连接世代表（设计文档 §5.4"同 node_id 只允许一条连接"）。

M0 只有一条被动心跳通道，两条并存连接不会造成错误行为；M1 起中心随心跳 ack
投递 goal 快照与 start/stop 指令，僵尸连接会吞掉或重复消费这些指令。世代表
用"每连接一个递增 epoch + 心跳自证"解决：旧连接在下一轮心跳发现 epoch 已非
自己，主动结束循环——中心不需要持有连接对象，也就无需跨任务 send。
"""

from __future__ import annotations

import itertools
import threading


class ConnectionRegistry:
    """进程内连接世代登记。线程安全（WS 处理循环与 REST 线程都会访问）。"""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._mu = threading.Lock()
        self._current: dict[str, int] = {}

    def join(self, node_id: str) -> int:
        """登记一条新连接并返回其 epoch；同 node_id 的旧 epoch 即刻失效。"""
        epoch = next(self._counter)
        with self._mu:
            self._current[node_id] = epoch
        return epoch

    def is_current(self, node_id: str, epoch: int) -> bool:
        with self._mu:
            return self._current.get(node_id) == epoch

    def current_epoch(self, node_id: str) -> int | None:
        with self._mu:
            return self._current.get(node_id)

    def release(self, node_id: str, epoch: int) -> None:
        """连接结束时摘除。**仅当 epoch 属于自己**才删：旧连接的 finally 晚于
        新连接的 join 是常见时序，无条件删会把刚上线的 worker 误判为离线。
        """
        with self._mu:
            if self._current.get(node_id) == epoch:
                self._current.pop(node_id, None)

    def count(self) -> int:
        with self._mu:
            return len(self._current)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_cluster_conns.py -q`
Expected: PASS（8 条）

- [ ] **Step 5: 提交**

```bash
git add src/modelctl/core/cluster/conns.py tests/test_cluster_conns.py
git commit -m "feat(cluster): 连接世代表（同 node_id 后来者胜，为 ack 投递保驾）"
```

### Task 7: `nodes.py` v2——心跳回流落库 + ack 组装（sync 捎带 / action 投递）

中心侧把"worker 上报的事实"写进台账，并在同一枚 ack 里回带期望快照与待执行指令。这是本里程碑的中枢：**投递不需要新通道**——worker 只在心跳里报告自己的 revision，中心发现不一致就带回全量快照，于是丢帧/重连/中心重启共用一条自愈路径。

**Files:**
- Modify: `src/modelctl/core/cluster/nodes.py`
- Test: `tests/test_cluster_ingest.py`

**Interfaces:**
- Consumes: `ClusterStore`（T1）、`GoalService`（T5：`snapshot_for` / `record_model_states` / `mark_stage`）、`wsproto.parse_heartbeat_v2` 的输出形状（T2）
- Produces（`NodeRegistry` 扩展）：
  - `__init__(self, store: ClusterStore, goals: GoalService | None = None) -> None`
  - `handle_heartbeat(self, node_id: str, hb: dict[str, Any], now: float) -> dict[str, Any]`（**签名变更**：第三参由 payload dict 改为 `parse_heartbeat_v2` 的结果；返回值由 `None` 变为 ack dict）
  - `push_action(self, node_id: str, action: str, *, goal_id: str = "", profile: str = "") -> bool`（入队；离线节点保留队列等其上线）
  - `drain_actions(self, node_id: str) -> list[dict]`（取空队列，返回 wsproto action 帧）
  - 常量 `MAX_QUEUED_ACTIONS = 16`

- [ ] **Step 1: 写失败测试** — 创建 `tests/test_cluster_ingest.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_ingest.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : 中心心跳回流（容量/模型状态/stage/漂移）与 ack 组装（sync/action）
# ===============================================================================

import pytest

from modelctl.core.cluster import wsproto
from modelctl.core.cluster.goals import GoalService
from modelctl.core.cluster.nodes import NodeRegistry
from modelctl.core.cluster.store import ClusterStore

YAML = "port: 8101\nengine_config:\n  tensor_parallel_size: 2\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    store = ClusterStore(tmp_path / "m.db")
    store.init_db()
    models = tmp_path / "models"
    (models / "vllm").mkdir(parents=True)
    (models / "vllm" / "qwen.yaml").write_text(YAML, encoding="utf-8")
    monkeypatch.setattr("modelctl.core.cluster.goals.MODELS_DIR", models)
    goals = GoalService(store)
    reg = NodeRegistry(store, goals=goals)
    store.upsert_node(node_id="w-1", node_token="NT-1", lan_id="lan-1", role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    return store, goals, reg


def _hb(**over):
    """构造 parse_heartbeat_v2 之后的形状（中心只见消毒过的形状）。"""
    raw = {"payload": {"profiles": {}, "local_profiles": ["qwen"],
                       "capacity": {"gpu_count": 4, "vram_total_mb": 157280},
                       "runtimes": {"vllm": {"ok": True}}, "goal_sync": {"revision": ""}}}
    raw["payload"].update(over)
    return wsproto.parse_heartbeat_v2(raw)


def test_heartbeat_returns_ack_dict_and_touches_lease(env):
    store, _goals, reg = env
    ack = reg.handle_heartbeat("w-1", _hb(), now=100.0)
    assert ack["t"] == "ack"
    assert store.get_node("w-1")["status"] == "online"


def test_heartbeat_persists_capacity_and_runtimes(env):
    store, _goals, reg = env
    reg.handle_heartbeat("w-1", _hb(), now=100.0)
    node = store.get_node("w-1")
    assert node["capacity"]["gpu_count"] == 4
    assert node["runtimes"]["vllm"]["ok"] is True
    assert node["local_profiles"] == ["qwen"]


def test_missing_sections_do_not_wipe_known_facts(env):
    """旧版 worker 不上报 capacity：中心必须保留既有值，而非写空。"""
    store, _goals, reg = env
    reg.handle_heartbeat("w-1", _hb(), now=100.0)
    reg.handle_heartbeat("w-1", wsproto.parse_heartbeat_v2({}), now=110.0)
    assert store.get_node("w-1")["capacity"]["gpu_count"] == 4


def test_ack_carries_sync_when_revision_differs(env):
    store, goals, reg = env
    reg.handle_heartbeat("w-1", _hb(), now=100.0)
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    ack = reg.handle_heartbeat("w-1", _hb(), now=110.0)
    assert ack["sync"]["goals"][0]["goal_id"] == "qwen@@w-1"
    assert store.get_node("w-1")["last_goal_sync_sha"] == ack["sync"]["revision"]


def test_ack_omits_sync_when_revision_matches(env):
    """revision 一致就不带快照：否则每 10s 白传一份 YAML 全集。"""
    store, goals, reg = env
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    reg.handle_heartbeat("w-1", _hb(), now=100.0)
    rev = goals.snapshot_for("w-1")["revision"]
    ack = reg.handle_heartbeat("w-1", _hb(goal_sync={"revision": rev}), now=110.0)
    assert "sync" not in ack


def test_ack_records_worker_stage_back_to_goal(env):
    store, goals, reg = env
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    reg.handle_heartbeat("w-1", _hb(profiles={"qwen": {"stage": "READY", "state": "READY",
                                                       "port": 8101, "gpu": [0, 1]}}), now=120.0)
    g = store.get_goal("qwen@@w-1")
    assert g["stage"] == "READY"
    assert store.list_model_states(node_id="w-1")[0]["port"] == 8101


def test_ack_records_failed_stage_with_error_class(env):
    store, goals, reg = env
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    reg.handle_heartbeat("w-1", _hb(profiles={"qwen": {
        "stage": "FAILED", "state": "down", "reason": "venv 缺失", "error_class": "venv_missing"}}),
        now=120.0)
    g = store.get_goal("qwen@@w-1")
    assert g["stage"] == "FAILED" and g["error_class"] == "venv_missing"


def test_stage_of_unmanaged_profile_is_ignored(env):
    """worker 本机自跑的模型（中心无 goal）只进 model_states，不得污染任何 goal 阶段。"""
    store, goals, reg = env
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    reg.handle_heartbeat("w-1", _hb(profiles={"local-only": {"stage": "READY"}}), now=120.0)
    assert store.get_goal("qwen@@w-1")["stage"] == "PENDING_PROFILE_SYNC"
    assert [r["profile"] for r in store.list_model_states(node_id="w-1")] == ["local-only"]


def test_drift_reported_becomes_event(env):
    store, goals, reg = env
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    reg.handle_heartbeat("w-1", _hb(drift=["qwen@@w-1"]), now=130.0)
    kinds = [e["kind"] for e in store.recent_events(limit=20, node_id="w-1")]
    assert "goal.drift" in kinds


def test_drift_event_is_not_duplicated_every_beat(env):
    """漂移是持续状态而非事件：只在"无→有"时记一次，否则 events 表每 10s 涨一条。"""
    store, goals, reg = env
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    reg.handle_heartbeat("w-1", _hb(drift=["qwen@@w-1"]), now=130.0)
    reg.handle_heartbeat("w-1", _hb(drift=["qwen@@w-1"]), now=140.0)
    kinds = [e["kind"] for e in store.recent_events(limit=50, node_id="w-1")]
    assert kinds.count("goal.drift") == 1


def test_push_action_is_delivered_once(env):
    _store, _goals, reg = env
    assert reg.push_action("w-1", "start", goal_id="qwen@@w-1") is True
    frames = reg.drain_actions("w-1")
    assert frames[0]["action"] == "start" and frames[0]["goal_id"] == "qwen@@w-1"
    assert reg.drain_actions("w-1") == []


def test_queued_action_reaches_ack(env):
    _store, _goals, reg = env
    reg.push_action("w-1", "stop", goal_id="qwen@@w-1")
    ack = reg.handle_heartbeat("w-1", _hb(), now=100.0)
    assert ack["actions"][0]["action"] == "stop"


def test_action_queue_is_capped(env):
    """离线节点长时间不收指令时队列必须有上限，否则内存被单个节点撑爆。"""
    _store, _goals, reg = env
    for i in range(40):
        reg.push_action("w-1", "retry", goal_id=f"g{i}")
    assert len(reg.drain_actions("w-1")) <= 16


def test_push_action_unknown_node_still_queued(env):
    """节点暂离线也要入队：中心重启后 worker 回连时才拿得到 pending retry。"""
    _store, _goals, reg = env
    assert reg.push_action("ghost", "start", goal_id="x@@ghost") is True


def test_heartbeat_for_unknown_node_returns_ack_without_crash(env):
    reg = env[2]
    ack = reg.handle_heartbeat("ghost", _hb(), now=1.0)
    assert ack["t"] == "ack"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cluster_ingest.py -q`
Expected: FAIL —`TypeError: NodeRegistry.__init__() got an unexpected keyword argument 'goals'`

- [ ] **Step 3: 实现** — 改写 `src/modelctl/core/cluster/nodes.py`

顶部 import 增加 `wsproto` 与 `GoalService`（**用 TYPE_CHECKING 避免运行时环依赖**：goals 不 import nodes，故直接 import 亦可；此处按直接 import 写）：

```python
from modelctl.core.cluster import config, tokens, wsproto
from modelctl.core.cluster.goals import GoalService
from modelctl.core.cluster.store import ClusterStore, mask_tail

MAX_QUEUED_ACTIONS = 16
```

`NodeRegistry` 改造（`handle_hello`/`sweep`/`node_view`/`list_node_views`/`ensure_join_token` 全部保持 M0 原样不动）：

```python
class NodeRegistry:
    def __init__(self, store: ClusterStore, goals: GoalService | None = None) -> None:
        self.store = store
        self.goals = goals
        # node_id → 待下发 action 帧队列（心跳 ack 取空）。进程内即可：中心重启后
        # 队列清空是可接受的——worker 侧 reconciler 会自行把实际状态逼向本地 goal。
        self._actions: dict[str, list[dict[str, Any]]] = {}
        # node_id → 上次已上报的漂移集合，用于"只在变化时记事件"
        self._drift_seen: dict[str, set[str]] = {}

    # ---- 心跳回流 + ack 组装（M1）----
    def handle_heartbeat(self, node_id: str, hb: dict[str, Any], now: float) -> dict[str, Any]:
        """落库 worker 事实，并组装 ack（sync 捎带 + action 投递）。

        `hb` 必须是 wsproto.parse_heartbeat_v2 的输出。三段落库遵循同一原则：
        **None 表示 worker 未上报（保留既有事实），[]/{} 表示明确为空（照实覆盖）**
        ——混用会让旧版 worker 每 10s 把新版写入的容量/模型状态抹成空。
        """
        self.store.touch_heartbeat(node_id, now=now, lease_s=config.lease_s())
        self.store.update_node_capacity(
            node_id, capacity=hb.get("capacity"), runtimes=hb.get("runtimes"),
            local_profiles=hb.get("local_profiles"), now=now)

        profiles = hb.get("profiles")
        if self.goals is not None and isinstance(profiles, dict):
            self.goals.record_model_states(node_id, profiles, now)
            self._sync_stages(node_id, profiles, now)

        ack: dict[str, Any] = {"t": "ack"}
        if self.goals is not None:
            snapshot = self.goals.snapshot_for(node_id)
            reported = ""
            goal_sync = hb.get("goal_sync")
            if isinstance(goal_sync, dict):
                reported = str(goal_sync.get("revision", ""))
            if snapshot["revision"] != reported:
                ack["sync"] = snapshot
                self.store.set_node_last_goal_sync_sha(node_id, snapshot["revision"])
        self._record_drift(node_id, hb.get("drift"), now=now)
        actions = self.drain_actions(node_id)
        if actions:
            ack["actions"] = actions
        return ack

    def _sync_stages(self, node_id: str, profiles: dict[str, Any], now: float) -> None:
        """把 worker 上报的 goal 阶段回写 goals 表（只认本节点声明过的 profile）。"""
        for goal in self.store.list_goals(node_id=node_id):
            info = profiles.get(str(goal["profile"]))
            if not isinstance(info, dict):
                continue
            stage = str(info.get("stage", ""))
            if not stage:
                continue
            self.goals.mark_stage(str(goal["goal_id"]), stage,
                                  reason=str(info.get("reason", "")),
                                  error_class=str(info.get("error_class", "")), now=now)

    def _record_drift(self, node_id: str, drift: Any, *, now: float) -> None:
        """漂移是持续状态：只在集合发生新增时记事件，避免每心跳刷一条。"""
        if not isinstance(drift, list):
            return
        current = {str(d) for d in drift}
        previous = self._drift_seen.get(node_id, set())
        for goal_id in sorted(current - previous):
            self.store.append_event("goal.drift", node_id=node_id, goal_id=goal_id,
                                    payload={"message": "worker 本地 profile 与中心声明不一致"},
                                    now=now)
        if current:
            self._drift_seen[node_id] = current
        else:
            self._drift_seen.pop(node_id, None)

    # ---- 指令队列（REST/WS 写入，心跳 ack 取走）----
    def push_action(self, node_id: str, action: str, *, goal_id: str = "",
                    profile: str = "") -> bool:
        """排队一条指令。节点离线也保留（等其回连）；超出上限拒绝而非静默丢弃。"""
        queue = self._actions.setdefault(node_id, [])
        if len(queue) >= MAX_QUEUED_ACTIONS:
            return False
        queue.append(wsproto.make_action(len(queue) + 1, action,
                                         goal_id=goal_id, profile=profile))
        return True

    def drain_actions(self, node_id: str) -> list[dict[str, Any]]:
        """取空队列并统一编号（seq 用全局递增的进程内计数，保证同连接内不重复）。

        入队时的 seq 只用于人读；真正给 worker 的 seq 在此重排，避免"队列被取空
        后再次入队"产生与历史 seq 撞号，导致 worker 把新指令当旧回执。
        """
        queue = self._actions.pop(node_id, [])
        base = int(self._seq_base.get(node_id, 0))
        self._seq_base[node_id] = base + len(queue)
        return [dict(f, seq=base + i + 1) for i, f in enumerate(queue)]
```

`__init__` 里补 `self._seq_base: dict[str, int] = {}`（放在 `self._actions` 之后）。

- [ ] **Step 4: 修 M0 调用点**

`src/modelctl/core/webui/admin_cluster.py` 的 WS 循环里，M0 代码是：

```python
            if mtype == "heartbeat":
                reg.handle_heartbeat(node_id, wsproto.parse_heartbeat(data), now=time.time())
                _sweep_if_due()
                await ws.send_text(wsproto.dumps({"t": "ack"}))
```

改为（本任务只改这一处，REST 端点留待 Task 11）：

```python
            if mtype == "heartbeat":
                ack = reg.handle_heartbeat(node_id, wsproto.parse_heartbeat_v2(data), now=time.time())
                _sweep_if_due()
                await ws.send_text(wsproto.dumps(ack))
```

同时 `get_registry()` 改为注入 GoalService（REST 与 WS 共用同一实例，队列才一致）：

```python
def get_registry() -> NodeRegistry:
    """NodeRegistry 进程内单例（懒建库）。测试经 admin_cluster._REGISTRY=None 重置。"""
    global _REGISTRY
    if _REGISTRY is None:
        store = ClusterStore()
        store.init_db()
        _REGISTRY = NodeRegistry(store, goals=GoalService(store))
    return _REGISTRY
```

（import 处加 `from modelctl.core.cluster.goals import GoalService`。）

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/test_cluster_ingest.py tests/test_cluster_nodes.py tests/test_cluster_agent.py -q`
Expected: PASS（新 15 条 + M0 相关测试；若 M0 测试直接调过 `handle_heartbeat(node, payload, now)` 并断言返回 None，按新签名改为传 `parse_heartbeat_v2({...})` 并断言 `ack["t"] == "ack"`——这是**有意的签名变更**，不要为兼容旧测试保留双签名）

- [ ] **Step 6: 提交**

```bash
git add src/modelctl/core/cluster/nodes.py src/modelctl/core/webui/admin_cluster.py tests/test_cluster_ingest.py
git commit -m "feat(cluster): 心跳回流落库与 ack 组装（sync 捎带 + action 队列）"
```

---

### Task 8: `cluster/sync.py`——worker 侧写盘（原子 / 漂移 / 剪枝）

把中心快照落到 `models/<engine>/<name>.yaml` + `data/cache/cluster-goals.json`。两个反直觉但关键的决定：

1. **不注入 `managed-by` 头注释**。头会改变文件字节，使"文件 sha == 中心 sha"这一漂移判据失效（要判就得剥头，多一处易错）。管理信息全部记在 `cluster-goals.json` 的 goal→{path, sha} 映射里，文件保持与中心逐字节相同。
2. **`.master` 备份**沿用仓库既有约定：覆盖一个内容不同的既有文件前先存 `<name>.yaml.master`，worker 本地误改仍有回退。

**Files:**
- Create: `src/modelctl/core/cluster/sync.py`
- Test: `tests/test_cluster_sync_writer.py`

**Interfaces:**
- Consumes: `cluster.profiles.is_safe_name` / `profile_sha`、`KNOWN_ENGINES`
- Produces:
  - `GOALS_FILE = "cluster-goals.json"`、`MARKER_FILE = "cluster-sync-marker.json"`
  - `@dataclass SyncResult: written: list[str]; skipped: list[str]; pruned: list[str]; rejected: list[str]; revision: str; drift: list[str]`
  - `read_state(cache_dir: Path) -> dict` → `{"revision": str, "goals": list[dict]}`（文件缺失/损坏 → `{"revision": "", "goals": []}`）
  - `apply_snapshot(snapshot: dict, *, models_dir: Path, cache_dir: Path, now: float) -> SyncResult`
  - `scan_drift(cache_dir: Path, models_dir: Path) -> list[str]`（返回漂移的 goal_id 列表）
  - `managed_paths(cache_dir: Path) -> list[Path]`

- [ ] **Step 1: 写失败测试** — 创建 `tests/test_cluster_sync_writer.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_sync_writer.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : worker 侧快照落盘（原子写/幂等/漂移/剪枝/不可信输入拒绝）
# ===============================================================================

from pathlib import Path

import pytest
import yaml

from modelctl.core.cluster import profiles as P
from modelctl.core.cluster.sync import (
    GOALS_FILE, MARKER_FILE, apply_snapshot, managed_paths, read_state, scan_drift,
)

YAML_A = "port: 8101\napi_key: ${API_KEY}\n"


def _goal(profile="qwen", engine="vllm", *, text=YAML_A, intent="start", goal_id=None):
    sha = P.profile_sha(text)
    return {"goal_id": goal_id or f"{profile}@@w-1", "profile": profile, "engine": engine,
            "yaml": text, "sha": sha, "version": "v1", "intent": intent,
            "params": None, "env_overlay": None}


@pytest.fixture()
def dirs(tmp_path):
    models = tmp_path / "models"
    cache = tmp_path / "cache"
    cache.mkdir()
    return models, cache


def _apply(dirs, goals, revision="r1"):
    models, cache = dirs
    return apply_snapshot({"revision": revision, "goals": goals},
                          models_dir=models, cache_dir=cache, now=100.0)


def test_writes_verbatim_text_and_state_file(dirs):
    models, cache = dirs
    g = _goal()
    out = _apply(dirs, [g])
    path = models / "vllm" / "qwen.yaml"
    assert path.read_text(encoding="utf-8") == YAML_A        # 逐字节相同，无注入头
    assert out.written == ["qwen@@w-1"] and out.revision == "r1"
    state = read_state(cache)
    assert state["revision"] == "r1" and state["goals"][0]["sha"] == g["sha"]
    assert (cache / MARKER_FILE).is_file()


def test_second_apply_with_same_revision_is_noop(dirs):
    _apply(dirs, [_goal()])
    out = _apply(dirs, [_goal()], revision="r1")
    assert out.written == [] and out.skipped == []           # 同 revision 直接短路


def test_same_revision_short_circuit_even_if_snapshot_changed(dirs):
    """revision 是唯一的"要不要动手"判据：中心不会在 revision 不变时改内容。"""
    _apply(dirs, [_goal()])
    out = _apply(dirs, [_goal(text="port: 9999\n")], revision="r1")
    assert (dirs[0] / "vllm" / "qwen.yaml").read_text(encoding="utf-8") == YAML_A
    assert out.written == []


def test_changed_content_writes_and_keeps_master_backup(dirs):
    models, _cache = dirs
    _apply(dirs, [_goal()])
    out = _apply(dirs, [_goal(text="port: 8102\n")], revision="r2")
    assert out.written == ["qwen@@w-1"]
    assert (models / "vllm" / "qwen.yaml").read_text(encoding="utf-8") == "port: 8102\n"
    assert (models / "vllm" / "qwen.yaml.master").read_text(encoding="utf-8") == YAML_A


def test_identical_content_does_not_touch_master(dirs):
    """同内容重写（如中心改了别的字段导致 revision 变）不得刷备份。"""
    models, _ = dirs
    _apply(dirs, [_goal()])
    (models / "vllm" / "qwen.yaml").write_text("本地改过\n", encoding="utf-8")
    out = _apply(dirs, [_goal()], revision="r2")
    assert out.written == ["qwen@@w-1"]
    assert (models / "vllm" / "qwen.yaml").read_text(encoding="utf-8") == YAML_A


def test_absent_goal_gets_pruned(dirs):
    models, _ = dirs
    _apply(dirs, [_goal("a"), _goal("b")], revision="r1")
    out = _apply(dirs, [_goal("a")], revision="r2")
    assert out.pruned == ["b@@w-1"]
    assert not (models / "vllm" / "b.yaml").exists()
    assert (models / "vllm" / "a.yaml").is_file()


def test_pruned_goal_leaves_no_state_entry(dirs):
    _apply(dirs, [_goal("a"), _goal("b")], revision="r1")
    _apply(dirs, [_goal("a")], revision="r2")
    assert [g["goal_id"] for g in read_state(dirs[1])["goals"]] == ["a@@w-1"]


def test_drift_detected_when_local_file_modified(dirs):
    models, cache = dirs
    _apply(dirs, [_goal()])
    (models / "vllm" / "qwen.yaml").write_text("port: 1\n# 我手改了\n", encoding="utf-8")
    assert scan_drift(cache, models) == ["qwen@@w-1"]


def test_drift_detected_when_file_deleted(dirs):
    models, cache = dirs
    _apply(dirs, [_goal()])
    (models / "vllm" / "qwen.yaml").unlink()
    assert scan_drift(cache, models) == ["qwen@@w-1"]


def test_drift_reported_inside_apply(dirs):
    models, _ = dirs
    _apply(dirs, [_goal()], revision="r1")
    (models / "vllm" / "qwen.yaml").write_text("port: 1\n", encoding="utf-8")
    out = _apply(dirs, [_goal()], revision="r2")
    assert out.drift == ["qwen@@w-1"] and out.written == ["qwen@@w-1"]  # 声明式：改回中心版本


def test_read_state_tolerates_missing_and_corrupt(dirs):
    _models, cache = dirs
    assert read_state(cache) == {"revision": "", "goals": []}
    (cache / GOALS_FILE).write_text("{坏 json", encoding="utf-8")
    assert read_state(cache) == {"revision": "", "goals": []}


# ---------------- 不可信输入拒绝（中心→worker 是可写文件的通道）----------------
def test_rejects_unsafe_profile_name(dirs):
    out = _apply(dirs, [_goal(profile="../escape")])
    assert out.rejected == ["../escape@@w-1"]
    assert not list((dirs[0]).rglob("escape.yaml"))


def test_rejects_unknown_engine(dirs):
    out = _apply(dirs, [_goal(engine="rm -rf")])
    assert out.rejected and not (dirs[0] / "rm -rf").exists()


def test_rejects_oversize_yaml(dirs):
    out = _apply(dirs, [_goal(text="port: 1\npad: " + "x" * (P.MAX_YAML_BYTES + 1))])
    assert out.rejected == ["qwen@@w-1"]
    assert not (dirs[0] / "vllm" / "qwen.yaml").exists()


def test_rejects_unparseable_yaml(dirs):
    out = _apply(dirs, [_goal(text="a: [1,\n")])
    assert out.rejected == ["qwen@@w-1"]


def test_rejects_non_mapping_yaml(dirs):
    assert _apply(dirs, [_goal(text="- 1\n- 2\n")]).rejected == ["qwen@@w-1"]


def test_rejects_yaml_without_port(dirs):
    assert _apply(dirs, [_goal(text="engine: vllm\n")]).rejected == ["qwen@@w-1"]


def test_rejected_goal_excluded_from_state(dirs):
    out = _apply(dirs, [_goal("good"), _goal("bad", engine="evil")], revision="r9")
    ids = [g["goal_id"] for g in read_state(dirs[1])["goals"]]
    assert ids == ["good@@w-1"] and out.written == ["good@@w-1"]


def test_revision_change_writes_atomically_without_tmp_leftover(dirs, monkeypatch):
    models, _ = dirs
    boom = RuntimeError("disk full")

    real_replace = Path.replace

    def fake_replace(self, target):
        if str(self).endswith("qwen.yaml.tmp") and target.name == "qwen.yaml":
            raise boom
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fake_replace)
    with pytest.raises(RuntimeError):
        _apply(dirs, [_goal()])
    assert not (models / "vllm" / "qwen.yaml").exists()
    assert not (models / "vllm" / "qwen.yaml.tmp").exists()      # 失败不得留临时文件
    assert read_state(dirs[1])["goals"] == []                     # 失败不落 state（下轮重试）


def test_managed_paths_lists_written_files(dirs):
    _apply(dirs, [_goal()])
    assert managed_paths(dirs[1]) == [dirs[0] / "vllm" / "qwen.yaml"]


def test_written_file_is_loadable_by_repo_profile_loader(dirs, monkeypatch, tmp_path):
    """落盘产物必须能被既有 loader 读取（占位符由 worker 本地 .env 解析）。"""
    from modelctl.core.profile import load_profile

    _apply(dirs, [_goal()])
    monkeypatch.setenv("API_KEY", "sk-local")
    prof = load_profile("qwen", dirs[0])
    assert prof.port == 8101 and prof.api_key == "sk-local"
    assert yaml.safe_load((dirs[0] / "vllm" / "qwen.yaml").read_text(encoding="utf-8"))["port"] == 8101
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cluster_sync_writer.py -q`
Expected: FAIL —`ModuleNotFoundError: No module named 'modelctl.core.cluster.sync'`

- [ ] **Step 3: 实现** — 创建 `src/modelctl/core/cluster/sync.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/sync.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : worker 侧 goal 快照落盘（原子写 + 漂移检测 + 剪枝；§6.3）
# ===============================================================================

"""core/cluster/sync.py — worker 端"中心声明 → 本地文件"的执行层。

两条与直觉相反但必要的设计：

1) **不注入 managed-by 头注释**。头会改变文件字节，使"文件 sha == 中心 sha"这一
   漂移判据失效（要判漂移就得先剥头，多一处可错的地方）。管理信息全部记在
   data/cache/cluster-goals.json 的 goal→{path, sha} 映射里，模型文件与中心
   逐字节相同，`modelctl list` / Web 控制台看到的与中心下发的完全一致。
2) **中心送来的内容一律按外部输入校验**（这是一条"远端能往本机 models/ 写文件"
   的通道）：profile/engine 走白名单名 + KNOWN_ENGINES，尺寸封顶，必须能被
   safe_load 且含 port。校验失败只拒该条（rejected），不中断整份快照。

原子写 = 同目录 .tmp + os.replace；失败必须清掉 .tmp 且不写 state，否则下一轮
revision 短路会以为"已同步"，把半途状态永久固化。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from modelctl.core.cluster.profiles import is_safe_name, profile_sha
from modelctl.core.profile import KNOWN_ENGINES

GOALS_FILE = "cluster-goals.json"
MARKER_FILE = "cluster-sync-marker.json"

#: 单条 goal 落盘前的尺寸/结构校验上限（与中心 profiles.MAX_YAML_BYTES 同值）
MAX_YAML_BYTES = 256 * 1024


@dataclass
class SyncResult:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    revision: str = ""
    drift: list[str] = field(default_factory=list)


def _goals_path(cache_dir: Path) -> Path:
    return cache_dir / GOALS_FILE


def read_state(cache_dir: Path) -> dict[str, Any]:
    """读回本地上一次应用的快照。文件缺失/损坏一律视作"从未同步过"。

    损坏时不清空文件：read_state 是只读路径（心跳也调它），把"读不懂"当成
    "写权限"会让一次偶发截断丢掉全部管理关系，进而让剪枝逻辑失去保护对象。
    """
    path = _goals_path(cache_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"revision": "", "goals": []}
    if not isinstance(data, dict) or not isinstance(data.get("goals"), list):
        return {"revision": "", "goals": []}
    goals = [g for g in data["goals"] if isinstance(g, dict) and g.get("goal_id")]
    return {"revision": str(data.get("revision", "")), "goals": goals}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def managed_paths(cache_dir: Path) -> list[Path]:
    """本地上一次落盘的文件路径（供状态采集与卸载清理）。"""
    out = []
    for g in read_state(cache_dir)["goals"]:
        p = g.get("path")
        if isinstance(p, str) and p:
            out.append(Path(p))
    return out


def _validate(goal: dict[str, Any]) -> str:
    """返回错误文案（空串表示通过）。措辞直接进 events/dashboard，须可行动。"""
    profile, engine = str(goal.get("profile", "")), str(goal.get("engine", ""))
    if not is_safe_name(profile):
        return f"非法 profile 名 {profile!r}"
    if engine not in KNOWN_ENGINES:
        return f"未知 engine {engine!r}"
    text = goal.get("yaml")
    if not isinstance(text, str) or not text.strip():
        return "yaml 为空"
    if len(text.encode("utf-8")) > MAX_YAML_BYTES:
        return f"yaml 过大（>{MAX_YAML_BYTES} B）"
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return f"yaml 语法错误: {exc}"
    if not isinstance(raw, dict):
        return "yaml 顶层必须是映射"
    if raw.get("port") in (None, ""):
        return "yaml 缺 port"
    want = goal.get("sha")
    if isinstance(want, str) and want and profile_sha(text) != want:
        return "sha 与快照不符（传输被截断或中心数据损坏）"
    return ""


def apply_snapshot(snapshot: dict[str, Any], *, models_dir: Path, cache_dir: Path,
                   now: float) -> SyncResult:
    """把中心快照落盘。同 revision 直接短路（幂等 + 省 IO）。

    剪枝以"快照全集"为准：上一次落过、这次不在快照里的 goal，其 YAML 被删除。
    中心从不送"已删除列表"——goal 从快照消失即是删除语义（§6.5）。
    """
    revision = str(snapshot.get("revision", "")) if isinstance(snapshot, dict) else ""
    raw_goals = snapshot.get("goals") if isinstance(snapshot, dict) else None
    goals = [g for g in raw_goals if isinstance(g, dict)] if isinstance(raw_goals, list) else []
    state = read_state(cache_dir)
    result = SyncResult(revision=revision)

    if revision and state["revision"] == revision:
        result.skipped = [str(g["goal_id"]) for g in goals]
        result.drift = scan_drift(cache_dir, models_dir)
        return result

    previous = {str(g["goal_id"]): g for g in state["goals"]}
    entries: dict[str, dict[str, Any]] = {}
    for goal in goals:
        goal_id = str(goal.get("goal_id", ""))
        if not goal_id:
            continue
        problem = _validate(goal)
        if problem:
            result.rejected.append(goal_id)
            logger.warning(f"集群 sync 拒绝落盘 {goal_id}: {problem}")
            continue
        path = models_dir / str(goal["engine"]) / f"{goal['profile']}.yaml"
        text = str(goal["yaml"])
        before = path.read_text(encoding="utf-8") if path.is_file() else None
        if before != text:
            _atomic_write(path, text, backup=before)
        entries[goal_id] = {"goal_id": goal_id, "profile": str(goal["profile"]),
                            "engine": str(goal["engine"]), "path": str(path),
                            "sha": profile_sha(text), "intent": str(goal.get("intent", "start")),
                            "params": goal.get("params"), "env_overlay": goal.get("env_overlay")}
        result.written.append(goal_id)

    for goal_id, old in previous.items():
        if goal_id in entries:
            continue
        _prune(Path(str(old.get("path", ""))))
        result.pruned.append(goal_id)

    _write_json(_goals_path(cache_dir), {"revision": revision, "applied_at": now,
                                         "goals": list(entries.values())})
    _write_json(cache_dir / MARKER_FILE, {"revision": revision, "applied_at": now,
                                          "goal_count": len(entries)})
    result.drift = scan_drift(cache_dir, models_dir)
    return result


def _atomic_write(path: Path, text: str, *, backup: str | None) -> None:
    """同目录临时文件 + os.replace。任一步失败必须清掉 .tmp（不留半成品）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        if backup is not None and backup != text:      # 内容真的变了才留 .master 回退
            _write_text_quiet(path.with_name(path.name + ".master"), backup)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _write_text_quiet(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 — 备份失败不值得让整个 sync 回滚
        logger.warning(f"集群 sync 备份 {path.name} 失败（忽略）: {exc}")


def _prune(path: Path) -> None:
    """删除已撤销 goal 的 YAML。**只删登记过的路径**，绝不按目录扫描乱删。"""
    if not str(path):
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001 — 删不掉就报 drift，不阻断其他 goal
        logger.warning(f"集群 sync 剪枝失败 {path}: {exc}")


def scan_drift(cache_dir: Path, models_dir: Path) -> list[str]:
    """返回"中心声明过但本地文件已改/已删"的 goal_id（保序）。

    `models_dir` 参数保留给未来多根目录场景；当前以登记的绝对路径为准，这样
    worker 把 models 目录整体搬走后仍能准确报"文件不见了"而不是误判为未托管。
    """
    del models_dir  # 见 docstring：判定完全依据登记的绝对路径
    out: list[str] = []
    for g in read_state(cache_dir)["goals"]:
        path = Path(str(g.get("path", "")))
        want = str(g.get("sha", ""))
        try:
            got = profile_sha(path.read_text(encoding="utf-8"))
        except OSError:
            out.append(str(g.get("goal_id", "")))
            continue
        if got != want:
            out.append(str(g.get("goal_id", "")))
    return out
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_cluster_sync_writer.py -q`
Expected: PASS（22 条）

- [ ] **Step 5: 提交**

```bash
git add src/modelctl/core/cluster/sync.py tests/test_cluster_sync_writer.py
git commit -m "feat(cluster): worker 侧快照落盘（原子写 + 漂移/剪枝 + 不可信输入拒绝）"
```

---

### Task 9: `cluster/reconcile.py`——worker 侧状态机（8 态 stage + 错误分类 + 心跳快照）

本模块是 M1 的执行核心，也是四条形态规则的落地点，实现前务必逐条对齐：

1. **只调度，不执行**。起停原语完全复用 `all_service.start_profile/stop_profile`（含引擎适配器、模型下载、GPU 锁、健康检查），reconcile 只决定"**这一轮要不要调、朝哪个方向调**"。集群层绝不自行 `Popen`、绝不自己等端口。
2. **失败即终态**。任何失败 → `FAILED` + `reason` + `error_class`，此后 `_step` 对该 goal **直接早退**，直到中心 `retry` 或 goal 内容/intent 变化。没有重试计数器、没有退避队列——一个下载失败的 70B 模型不该让 worker 每 5s 重跑一次 40GB 网络请求。
3. **手动指令压过声明式 intent**。`stop` 是**黏性**的：停止成功后**不清** `manual` 位，否则下一轮又按 `intent=start` 把模型拉起来（用户点"停止"看到它自己复活，是最恶劣的 UX）。`restart` 是**一次性**的：本轮只停，清位后下一轮由 intent 拉回。`retry` 只重置状态、不改方向。
4. **profile 标识一律用中心侧 stem**。中心 `goal.profile` 是文件 stem，而 PID 文件、GPU 锁持有者、`is_running_any` 探测用的是 `Profile.name`（YAML 里可显式覆盖，缺省才等于 stem）。二者错配会让"集群起的模型"在本地台账里查无此人。唯一可靠解：**`profile.load_profile_at(path)` 从下发文件本身解析 Profile**，本模块不再自己拼 name。

**单写者模型（本任务最重要的约束）**：所有副作用（写盘、起进程、停进程、改 `_recs`）**只发生在 reconcile 循环线程**。Agent(WS) 线程只能做两类事：

- **投递**：`offer_snapshot`（快照）、`handle_actions`（指令，同时把 result 帧投进 `_results`）——**必须无锁**。`_step` 里的 start 最长阻塞 `start_timeout_s()`（默认 300s），若 Agent 线程为此等 `_lock`，心跳就会停摆超过 lease（90s），中心会把一台正在拉 70B 模型的健康节点判成 stale 并标离线。线程安全靠 CPython 的原子操作达成：投递用 `list.extend/append` 与整体赋值，取空用 `a, self._x = self._x, a`。
- **读缓存**：`snapshot()` / `flush_results()`。`_lock` 只用于互斥"循环线程 vs 外部直调 `apply_snapshot`/`reconcile_once`"（测试与 CLI 路径）。

**快照每拍重建，不缓存上一轮结果**：若 `_collect` 复用上一轮的 stage，那 300s 冷启动期间中心看到的 stage 恒为 `SYNCED`，dashboard 上"点了启动、五分钟没反应"和"根本没收到指令"完全无法区分。因此 `_collect()` 每次读 `sync.read_state` + 实时 `_recs` 组装，`STARTING` 才可见（配套：起进程前先落 `STARTING` 并 `_save()`）。

**Files:**
- Create: `src/modelctl/core/cluster/reconcile.py`
- Modify: `src/modelctl/core/cluster/config.py`（`reconcile_interval_s()` / `start_timeout_s()`）
- Modify: `src/modelctl/core/profile.py`（`load_profile_at()`，纯增量，无行为变更）
- Test: `tests/test_cluster_reconcile.py`

**Interfaces:**
- Consumes: `cluster.sync.read_state/apply_snapshot/scan_drift`（Task 8）、`cluster.profiles.profile_sha`（Task 3）、`cluster.wsproto.VALID_ACTIONS/make_result`（Task 2）、`cluster.goals.ENV_OVERLAY_ALLOWLIST/SECRET_KEY_HINTS`（Task 5，**函数内 import**：goals 会连带 import `store`，worker 不需要中心的 sqlite）、`core.profile.KNOWN_ENGINES/load_profile`、`core.envs.MANAGED_ENGINES/has_env`、`core.capabilities.probe/all_vram_total_mb/free_vram_total_mb`、`core.process.cache_dir/pid_file`、`core.gpu_lock.list_gpu_locks`、`core.envfile.PROJECT_ROOT`
- Produces:
  - 常量：`STATE_FILE = "cluster-reconcile.json"`、`DEGRADE_LIMIT = 3`
  - 8 态：`PENDING_PROFILE_SYNC / PROFILE_SYNCED / RUNTIME_OK / STARTING / READY / DEGRADED / FAILED / STOPPED` + `VALID_STAGES`
  - `ERROR_CLASSES`（9 项）、`classify_error(detail) -> str`（有序子串规则表，兜底 `runtime_capability`）
  - `runtime_readiness(engine, raw, caps) -> tuple[bool, str, str]`（(ok, reason, error_class)）
  - `local_profile_paths(models_dir) -> dict[str, Path]`（stem → 路径；忽略 `.master`/`.tmp`/隐藏）
  - `@dataclass Outcome: status: str; detail: str = ""`
  - `profile_sha_safe(path) -> str`、`_raw_of(text) -> dict`
  - `class Reconciler`：`offer_snapshot / handle_actions / flush_results / apply_snapshot / reconcile_once / snapshot / heartbeat_payload / run`
  - 单例：`current()`、`start_reconciler_in_background()`
- 心跳扩展段形状（Task 2 `parse_heartbeat_v2` 的六个键，Task 4 gate 与 Task 7 `_sync_stages` 都依赖它）：
  `profiles` 按 **profile 名** 建键 → `{goal_id, stage, state, reason, error_class, managed, port, pid, gpu, at}`；`goal_sync = {"revision": str}`；`drift = [goal_id]`；`local_profiles = [stem]`；`capacity = {"gpu_count", "vram_total_mb", "vram_free_mb"}`；`runtimes = {engine: {"ok": bool}}`

- [ ] **Step 1: 写失败测试** — 创建 `tests/test_cluster_reconcile.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_reconcile.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 11:00
# @Desc   : worker 侧 reconciler（8 态 stage / 错误分类 / 手动优先 / 心跳快照）
# ===============================================================================

import os

import pytest

from modelctl.core.capabilities import Capabilities
from modelctl.core.cluster import config, reconcile
from modelctl.core.cluster import profiles as P
from modelctl.core.cluster import sync
from modelctl.core.cluster.reconcile import (
    DEGRADED, FAILED, PENDING_PROFILE_SYNC, PROFILE_SYNCED, READY, STARTING, STOPPED,
    Outcome, Reconciler, classify_error, local_profile_paths, profile_sha_safe,
    runtime_readiness,
)

YAML_A = "port: 8101\napi_key: ${API_KEY}\n"
YAML_B = "port: 8102\napi_key: ${API_KEY}\n"

_CAPS = Capabilities(gpu_count=4, gpu_indices=[0, 1, 2, 3],
                     vram_total_mb_per_gpu=[40960] * 4)


@pytest.fixture(autouse=True)
def _assume_venvs_ready(monkeypatch):
    """绝大多数用例只关心状态机，不该被"本机没建 vllm venv / 缺 API_KEY"绊住。

    `has_env` 必须 `setattr` 到 **reconcile 命名空间**：reconcile 在模块顶层
    `from core.envs import has_env` 已绑定函数对象，改 envs 里的同名属性无效。
    `API_KEY` 是必须的：`load_profile_at` 走 `_interpolate`，profile YAML 里的
    `${API_KEY}` 缺变量会抛 ProfileError，让所有状态机用例统一挂在"解析失败"上。
    """
    monkeypatch.setattr(reconcile, "has_env", lambda target: True)
    monkeypatch.setenv("API_KEY", "sk-test-local")


# --------------------------------------------------------------------------- 纯函数

@pytest.mark.parametrize("detail,expected", [
    ("[gpu_lock] GPU 0 已被 deepseek 占用", "gpu_lock"),
    ("gpu_list 指定的卡位不存在", "profile_invalid"),          # 早于 gpu_lock 规则
    ("端口 8101 已被占用（nginx:80）", "port_conflict"),
    ("vllm 专用环境未创建，执行 modelctl env setup vllm", "venv_missing"),
    ("引擎 ollama 的二进制在 PATH 中找不到", "venv_missing"),
    ("llamacpp 未安装", "venv_missing"),
    ("显存不足：需要 80GiB，实际 40GiB", "oom"),
    ("CUDA out of memory", "oom"),
    ("CUDA error: device-side assert", "oom"),
    ("模型下载失败（网络不可达）", "model_download_failed"),
    ("模型下载超时", "model_download_failed"),
    ("profile 必填字段 model 缺失", "profile_invalid"),
    ("port 必须是整数", "profile_invalid"),
    ("max_model_len 超过实际显存", "runtime_capability"),
    ("该量化格式不支持当前 GPU 计算能力", "runtime_capability"),
])
def test_classify_error_rules_in_priority_order(detail, expected):
    assert classify_error(detail) == expected


def test_classify_error_falls_back_to_runtime_capability():
    assert classify_error("") == "runtime_capability"
    assert classify_error("完全没见过的报错") == "runtime_capability"


def test_runtime_readiness_managed_engine_with_venv():
    ok, reason, cls = runtime_readiness("vllm", {}, _CAPS)
    assert ok is True and reason == "" and cls == ""


def test_runtime_readiness_docker_image_skips_venv(monkeypatch):
    """docker runtime 不需要 venv：免检路径必须真实可达，否则是死代码。"""
    monkeypatch.setattr(reconcile, "has_env", lambda target: False)
    ok, _reason, _cls = runtime_readiness("vllm", {"docker_image": "vllm/vllm:latest"}, _CAPS)
    assert ok is True


def test_runtime_readiness_managed_engine_without_venv(monkeypatch):
    monkeypatch.setattr(reconcile, "has_env", lambda target: False)
    ok, reason, cls = runtime_readiness("vllm", {}, _CAPS)
    assert ok is False and cls == "venv_missing" and "env setup" in reason


def test_runtime_readiness_unmanaged_engine_uses_binary_probe():
    caps = Capabilities(binaries={"ollama": False})
    ok, _reason, cls = runtime_readiness("ollama", {}, caps)
    assert ok is False and cls == "venv_missing"
    assert runtime_readiness("ollama", {}, Capabilities(binaries={"ollama": True}))[0] is True


def test_local_profile_paths_skips_sidecars(tmp_path):
    (tmp_path / "vllm").mkdir()
    (tmp_path / "ollama").mkdir()
    (tmp_path / "vllm" / "qwen.yaml").write_text(YAML_A, encoding="utf-8")
    (tmp_path / "vllm" / "qwen.yaml.master").write_text(YAML_B, encoding="utf-8")
    (tmp_path / "vllm" / ".qwen.yaml.tmp").write_text(YAML_B, encoding="utf-8")
    (tmp_path / "ollama" / "deepseek.yaml").write_text(YAML_B, encoding="utf-8")
    got = local_profile_paths(tmp_path)
    assert sorted(got) == ["deepseek", "qwen"]
    assert got["qwen"] == tmp_path / "vllm" / "qwen.yaml"


def test_local_profile_paths_stem_collision_is_deterministic(tmp_path):
    for engine in ("ollama", "vllm"):
        (tmp_path / engine).mkdir()
        (tmp_path / engine / "dup.yaml").write_text(YAML_A, encoding="utf-8")
    assert local_profile_paths(tmp_path)["dup"] == tmp_path / "ollama" / "dup.yaml"


def test_profile_sha_safe_missing_file_returns_empty(tmp_path):
    assert profile_sha_safe(tmp_path / "nope.yaml") == ""
    p = tmp_path / "x.yaml"
    p.write_text(YAML_A, encoding="utf-8")
    assert profile_sha_safe(p) == P.profile_sha(YAML_A)


# --------------------------------------------------------------------------- 替身与夹具

class Recorder:
    """起停替身：共享一份运行态，让 start/stop 真的翻转 up/alive。"""

    def __init__(self) -> None:
        self.up: dict[str, bool] = {}
        self.dead: set[str] = set()
        self.starts: list[str] = []
        self.stops: list[str] = []
        self.start_detail = ""
        self.stop_detail = ""

    def start(self, prof, caps, timeout):
        self.starts.append(prof.name)
        if self.start_detail:
            return Outcome("error", self.start_detail)
        self.dead.discard(prof.name)
        self.up[prof.name] = True
        return Outcome("ok", f"http://127.0.0.1:{prof.port}")

    def stop(self, prof, caps, models_dir):
        self.stops.append(prof.name)
        if self.stop_detail:
            return Outcome("error", self.stop_detail)
        self.up[prof.name] = False
        self.dead.add(prof.name)       # 真实 stop 会带走进程：alive 必须同步翻转
        return Outcome("ok", "已停止")

    def probe(self, prof):
        alive = prof.name not in self.dead
        return {"up": bool(self.up.get(prof.name)), "alive": alive, "port": prof.port,
                "pid": 4242 if alive else None, "gpus": [0] if self.up.get(prof.name) else [],
                "name": prof.name}


def _goal(profile="qwen", engine="vllm", *, text=YAML_A, intent="start", goal_id=None):
    return {"goal_id": goal_id or f"{profile}@@w-1", "profile": profile, "engine": engine,
            "yaml": text, "sha": P.profile_sha(text), "version": "v1", "intent": intent,
            "params": None, "env_overlay": None}


def _snap(*goals, revision="rev-1"):
    return {"revision": revision, "goals": list(goals)}


@pytest.fixture()
def dirs(tmp_path):
    models, cache = tmp_path / "models", tmp_path / "cache"
    cache.mkdir()
    return models, cache


def _rt(dirs, rec=None):
    models, cache = dirs
    rec = rec or Recorder()
    rt = Reconciler(models_dir=models, cache_dir=cache, starter=rec.start,
                    stopper=rec.stop, prober=rec.probe, caps=_CAPS)
    return rt, rec


# --------------------------------------------------------------------------- 状态机配方

def test_fresh_goal_goes_start_to_ready(dirs):
    rt, rec = _rt(dirs)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    assert rec.starts == ["qwen"]
    got = rt.snapshot()["profiles"]["qwen"]
    assert got["stage"] == READY and got["state"] == "running" and got["managed"] is True


def test_snapshot_missing_file_waits_for_sync(dirs):
    rt, rec = _rt(dirs)
    rt.reconcile_once(now=1.0)
    assert rec.starts == []


def test_start_failure_is_terminal_and_classified(dirs):
    rt, rec = _rt(dirs)
    rec.start_detail = "端口 8101 已被占用（nginx:80）"
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    first = rt.snapshot()["profiles"]["qwen"]
    assert first["stage"] == FAILED and first["error_class"] == "port_conflict"
    rt.reconcile_once(now=3.0)
    assert rec.starts == ["qwen"]                       # 不自动重试
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == FAILED


def test_retry_action_resets_failed_goal(dirs):
    rt, rec = _rt(dirs)
    rec.start_detail = "显存不足：需要 80GiB"
    g = _goal()
    rt.apply_snapshot(_snap(g), now=1.0)
    rt.reconcile_once(now=2.0)
    assert rt.snapshot()["profiles"]["qwen"]["error_class"] == "oom"
    rec.start_detail = ""
    rt.handle_actions([{"seq": 7, "action": "retry", "goal_id": g["goal_id"],
                        "profile": "qwen"}], now=3.0)
    out = list(rt.flush_results())
    assert out[0]["ok"] is True and out[0]["seq"] == 7
    rt.reconcile_once(now=4.0)
    assert rec.starts == ["qwen", "qwen"]
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == READY


def test_intent_stop_stops_and_stays_stopped(dirs):
    rt, rec = _rt(dirs)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    rt.apply_snapshot(_snap(_goal(intent="stop")), now=3.0)
    rt.reconcile_once(now=4.0)
    rt.reconcile_once(now=5.0)
    assert rec.stops == ["qwen"]
    assert rec.starts == ["qwen"]                       # 没有被 intent 再拉起来
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == STOPPED


def test_manual_stop_survives_intent_start(dirs):
    """手动 stop 压过声明式 intent=start，且位不清（清了就复活）。"""
    rt, rec = _rt(dirs)
    g = _goal()
    rt.apply_snapshot(_snap(g), now=1.0)
    rt.reconcile_once(now=2.0)
    rt.handle_actions([{"seq": 1, "action": "stop", "goal_id": g["goal_id"],
                        "profile": "qwen"}], now=3.0)
    rt.reconcile_once(now=4.0)
    assert rec.stops == ["qwen"]
    rt.reconcile_once(now=5.0)
    rt.reconcile_once(now=6.0)
    assert rec.starts == ["qwen"]                       # 关键：手动位仍在，未被重新拉起
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == STOPPED


def test_manual_stop_is_cleared_by_goal_change(dirs):
    rt, rec = _rt(dirs)
    g = _goal()
    rt.apply_snapshot(_snap(g), now=1.0)
    rt.reconcile_once(now=2.0)
    rt.handle_actions([{"seq": 1, "action": "stop", "goal_id": g["goal_id"],
                        "profile": "qwen"}], now=3.0)
    rt.reconcile_once(now=4.0)
    # 中心改了 YAML（sha 变）→ 视为"中心重新表达了一次意图"，手动位失效
    rt.apply_snapshot(_snap(_goal(text=YAML_B)), now=5.0)
    rt.reconcile_once(now=6.0)
    assert rec.starts == ["qwen", "qwen"]
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == READY


def test_restart_action_stops_once_and_intent_pulls_back(dirs):
    rt, rec = _rt(dirs)
    g = _goal()
    rt.apply_snapshot(_snap(g), now=1.0)
    rt.reconcile_once(now=2.0)
    rt.handle_actions([{"seq": 1, "action": "restart", "goal_id": g["goal_id"],
                        "profile": "qwen"}], now=3.0)
    rt.reconcile_once(now=4.0)
    assert rec.stops == ["qwen"]
    # restart 本轮只负责停：manual 已清，但"拉起"发生在下一拍（一拍只做一件事）
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == STOPPED
    rt.reconcile_once(now=5.0)
    assert rec.starts == ["qwen", "qwen"]
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == READY


def test_unknown_action_or_goal_rejected_in_result(dirs):
    rt, rec = _rt(dirs)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.handle_actions([
        {"seq": 1, "action": "destroy", "goal_id": "qwen@@w-1", "profile": "qwen"},
        {"seq": 2, "action": "stop", "goal_id": "ghost@@w-1", "profile": "ghost"},
    ], now=2.0)
    out = list(rt.flush_results())
    assert [r["ok"] for r in out] == [False, False]
    assert list(rt.flush_results()) == []               # 已取走，不重复回传
    rt.reconcile_once(now=3.0)
    assert rec.stops == []                              # 被拒的指令绝不进执行队列


def test_health_lost_with_dead_process_fails_immediately(dirs):
    rt, rec = _rt(dirs)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    rec.dead.add("qwen")          # 进程不存在：健康检查必然同时失败
    rec.up["qwen"] = False
    rt.reconcile_once(now=3.0)
    got = rt.snapshot()["profiles"]["qwen"]
    assert got["stage"] == FAILED and got["error_class"] == "health_lost"
    assert rec.starts == ["qwen"]                          # 不自动重拉


def test_health_lost_with_live_process_degrades_then_fails(dirs):
    rt, rec = _rt(dirs)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    rec.up["qwen"] = False                              # 进程还在，只是 /health 不通
    rt.reconcile_once(now=3.0)
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == DEGRADED
    rt.reconcile_once(now=4.0)
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == DEGRADED
    rt.reconcile_once(now=5.0)                          # 第 DEGRADE_LIMIT 次 → 终态
    got = rt.snapshot()["profiles"]["qwen"]
    assert got["stage"] == FAILED and got["error_class"] == "health_lost"


def test_missing_venv_never_calls_starter(dirs, monkeypatch):
    monkeypatch.setattr(reconcile, "has_env", lambda target: False)
    rt, rec = _rt(dirs)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    assert rec.starts == []
    got = rt.snapshot()["profiles"]["qwen"]
    assert got["stage"] == FAILED and got["error_class"] == "venv_missing"


def test_state_survives_process_restart(dirs):
    """手动位/终态跨进程存活：新实例首拍绝不能复活已停/已失败的 goal。"""
    rec = Recorder()
    rt, _ = _rt(dirs, rec)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    assert (dirs[1] / reconcile.STATE_FILE).is_file()
    rt2, _ = _rt(dirs, rec)        # 共享运行态：模型仍在服务
    rt2.reconcile_once(now=3.0)    # 未显式 _load：循环自己会从状态文件恢复
    assert rt2._recs["qwen@@w-1"]["stage"] == READY
    assert rec.starts == ["qwen"]  # 已 READY 的不重复起


def test_rejected_snapshot_goal_never_reaches_ready(dirs):
    rt, rec = _rt(dirs)
    bad = _goal(profile="../evil")
    rt.apply_snapshot(_snap(_goal(), bad), now=1.0)
    rt.reconcile_once(now=2.0)
    assert rec.starts == ["qwen"]
    assert "../evil" not in rt.snapshot()["profiles"]


# --------------------------------------------------------------------------- 心跳快照

def test_heartbeat_payload_shape(dirs):
    rt, _rec = _rt(dirs)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    rt._local.setdefault("solo-local", {"goal_id": "", "stage": READY, "state": "running",
                                        "reason": "", "error_class": "", "managed": False,
                                        "port": 9999, "pid": 1, "gpu": [3], "at": 2.0})
    got = rt.heartbeat_payload()
    assert sorted(got) == ["capacity", "drift", "goal_sync", "local_profiles", "profiles",
                           "runtimes"]
    assert got["goal_sync"] == {"revision": "rev-1"}
    assert got["profiles"]["qwen"]["stage"] == READY
    assert got["profiles"]["solo-local"]["managed"] is False
    assert "qwen" in got["local_profiles"]
    assert set(got["runtimes"]) == {"llamacpp", "ollama", "vllm", "sglang", "unsloth",
                                    "aphrodite", "lmdeploy", "tensorrt_llm", "tokenspeed"}
    assert got["capacity"]["gpu_count"] == 4


def test_goal_without_rec_derives_stage_from_disk(dirs):
    """goal 已落盘但本进程还没跑过（worker 刚重启）→ stage 由磁盘 sha 推导，
    且绝不能在这一步顺手把进程起起来（要等 reconcile 循环正式拍）。"""
    models, cache = dirs
    g = _goal()
    sync.apply_snapshot(_snap(g), models_dir=models, cache_dir=cache, now=1.0)
    rt, rec = _rt(dirs)
    snap = rt.snapshot()
    assert snap["revision"] == "rev-1"
    assert snap["profiles"]["qwen"]["stage"] == PROFILE_SYNCED
    assert snap["profiles"]["qwen"]["state"] == "synced"
    assert rec.starts == []
    (models / "vllm" / "qwen.yaml").write_text("port: 7777\n", encoding="utf-8")
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == PENDING_PROFILE_SYNC


def test_drift_reported_once_repaired_by_resync(dirs):
    rt, _rec = _rt(dirs)
    models, cache = dirs
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    (models / "vllm" / "qwen.yaml").write_text("port: 9999\n", encoding="utf-8")
    assert rt.snapshot()["drift"] == ["qwen@@w-1"]
    rt.apply_snapshot(_snap(_goal()), now=2.0)          # 同 revision 会被短路
    assert rt.snapshot()["drift"] == ["qwen@@w-1"]
    rt.apply_snapshot({"revision": "rev-2", "goals": [_goal()], "force": True}, now=3.0)
    assert rt.snapshot()["drift"] == []


# --------------------------------------------------------------------------- env_overlay

def test_env_overlay_applied_and_restored_exactly(dirs, monkeypatch):
    seen: dict[str, str | None] = {}
    rt, rec = _rt(dirs)
    g = _goal(env_overlay={"MODEL_ROOT": "/mnt/nas/models"})

    def spy_start(prof, caps, timeout):
        seen["MODEL_ROOT"] = os.environ.get("MODEL_ROOT")
        return rec.start(prof, caps, timeout)

    rt._starter = spy_start
    monkeypatch.setenv("MODEL_ROOT", "/original")
    rt.apply_snapshot(_snap(g), now=1.0)
    rt.reconcile_once(now=2.0)
    assert seen["MODEL_ROOT"] == "/mnt/nas/models"
    assert os.environ["MODEL_ROOT"] == "/original"      # 精确复原，不污染全局


def test_secret_keys_in_overlay_never_reach_environ(dirs, monkeypatch):
    """白名单外一律丢弃：整份 overlay 作废（宁可少给，不可多给）。"""
    seen: dict[str, str | None] = {}
    rt, rec = _rt(dirs)
    g = _goal(env_overlay={"MODEL_ROOT": "/m", "DEEPSEEK_API_KEY": "sk-leak"})

    def spy_start(prof, caps, timeout):
        seen["MODEL_ROOT"] = os.environ.get("MODEL_ROOT")
        seen["DEEPSEEK_API_KEY"] = os.environ.get("DEEPSEEK_API_KEY")
        return rec.start(prof, caps, timeout)

    rt._starter = spy_start
    monkeypatch.delenv("MODEL_ROOT", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    rt.apply_snapshot(_snap(g), now=1.0)
    rt.reconcile_once(now=2.0)
    assert seen == {"MODEL_ROOT": None, "DEEPSEEK_API_KEY": None}


# --------------------------------------------------------------------------- 投递路径与配置

def test_delivery_paths_do_not_take_the_lock(dirs):
    """中心宕机不起、心跳不停摆：起进程最长 300s 期间 Agent 线程投递 ack 必须返回。

    若 offer/handle 路径与 `_lock` 绑定，`t.join` 会一直等到 release.set()，本用例
    的第一个 `join(2)` 超时即失败——这正是"节点正在拉 70B 模型却被中心标 stale"的根因。
    """
    import threading

    rt, rec = _rt(dirs)
    started, release = threading.Event(), threading.Event()

    def slow_start(prof, caps, timeout):
        started.set()
        release.wait(5)
        return Outcome("ok", "done")

    rt._starter = slow_start
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    t = threading.Thread(target=rt.reconcile_once, kwargs={"now": 2.0})
    t.start()
    assert started.wait(5)
    rt.offer_snapshot(_snap(_goal(), revision="rev-2"))
    rt.handle_actions([{"seq": 1, "action": "retry", "goal_id": "qwen@@w-1",
                        "profile": "qwen"}], now=2.5)
    frames = list(rt.flush_results())
    assert frames and frames[0]["seq"] == 1                  # 投递真的被受理（非空判据）
    t.join(2)
    assert t.is_alive()                                      # 主线程从未被 reconcile 阻塞
    release.set()
    t.join(5)
    assert not t.is_alive()
    rt._drain_snapshot(now=3.0)                              # 新快照排在阻塞结束之后应用
    assert rt.snapshot()["revision"] == "rev-2"
    assert rec.starts == ["qwen"]                            # retry 未提前打断本轮


def test_reconcile_config_floors(monkeypatch):
    monkeypatch.delenv("CLUSTER_RECONCILE_INTERVAL_S", raising=False)
    monkeypatch.delenv("CLUSTER_START_TIMEOUT_S", raising=False)
    assert config.reconcile_interval_s() == 5
    assert config.start_timeout_s() == 300
    monkeypatch.setenv("CLUSTER_RECONCILE_INTERVAL_S", "0")
    monkeypatch.setenv("CLUSTER_START_TIMEOUT_S", "1")
    assert config.reconcile_interval_s() == 5            # 低于 floor 回退默认
    assert config.start_timeout_s() == 300


def test_local_only_profiles_are_reported_unmanaged(dirs):
    """本地手起的模型（无 goal）也必须进心跳，否则中心会以为节点空转。"""
    models, cache = dirs
    (models / "ollama").mkdir(parents=True)
    (models / "ollama" / "local.yaml").write_text(YAML_B, encoding="utf-8")
    rt, rec = _rt(dirs)
    rt._prober = lambda prof: {"up": True, "alive": True, "port": prof.port, "pid": 7,
                               "gpus": [], "name": prof.name}
    rt.reconcile_once(now=1.0)
    got = rt.snapshot()["profiles"]["local"]
    assert got["managed"] is False and got["stage"] == READY
    assert rec.starts == []                              # 未托管的绝不代客起停
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cluster_reconcile.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'modelctl.core.cluster.reconcile'`

- [ ] **Step 3: 实现——配置项与 `load_profile_at`**

在 `src/modelctl/core/cluster/config.py` 末尾追加（沿用既有 `_int_env`，勿改现有函数）：

```python
_DEFAULT_RECONCILE_S = 5
_DEFAULT_START_TIMEOUT_S = 300


def reconcile_interval_s() -> int:
    """worker 本地逼近期望状态的周期。刻意独立于心跳周期（见 reconcile 模块头）。"""
    return _int_env("CLUSTER_RECONCILE_INTERVAL_S", _DEFAULT_RECONCILE_S, floor=1)


def start_timeout_s() -> int:
    """单次启动等待引擎就绪的上限；与 all_service.start_all(timeout=300) 同默认。"""
    return _int_env("CLUSTER_START_TIMEOUT_S", _DEFAULT_START_TIMEOUT_S, floor=5)
```

在 `src/modelctl/core/profile.py` 的 `load_profile` 之后追加（复用既有 `_load_profile_from_path`，无新解析逻辑）：

```python
def load_profile_at(path: Path) -> Profile:
    """按**绝对路径**加载 profile —— 集群下发文件的唯一正确入口。

    `load_profile(name)` 靠目录扫描按 name 匹配，而中心只认文件 stem；YAML 里
    显式写了 `name:` 时两者会分叉（PID/GPU 锁用 name，中心用 stem）。直接按路径
    加载再把 `Profile.name` 交给下游，可让"集群下发的文件"与"本地台账记录"
    天然是同一条记录。

    `_load_profile_from_path` 内部会做 `${VAR}` 插值，worker 本地 .env 缺变量时抛
    `ProfileError`——调用方必须捕获（reconcile 把它折叠成一次 FAILED 而非中断整轮）。
    """
    return _load_profile_from_path(Path(path))
```

- [ ] **Step 4: 实现——`src/modelctl/core/cluster/reconcile.py`**

先写模块头与纯函数（状态机不依赖任何可变全局，便于单测）：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/reconcile.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 11:00
# @Desc   : worker 侧 reconciler（把实际状态逼向中心期望状态）
# ===============================================================================

"""core/cluster/reconcile.py — worker 侧声明式状态机。

四条形态规则：只调度不执行 / 失败即终态 / 手动压过 intent / profile 标识用中心侧 stem。

线程模型（单写者）：**所有副作用只在 reconcile 循环线程**。WS(Agent) 线程只能
`offer_snapshot` / `offer_actions` / `handle_actions`（投递）与 `snapshot` /
`flush_results`（读缓存）。投递路径**绝不可加锁**：`_step` 的 start 最长阻塞
`config.start_timeout_s()`（默认 300s），Agent 线程若等 `_lock` 就会让心跳停摆
超过 lease(90s)，中心据此把健康节点标成 stale。线程安全靠 CPython 原子操作：
`list.append` 投递、`a, self._x = self._x, a` 取空。`_lock` 只互斥"循环线程 vs
外部直调 apply_snapshot/reconcile_once"（测试与 CLI 路径）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml
from loguru import logger

from modelctl.core.capabilities import (
    Capabilities, all_vram_total_mb, free_vram_total_mb, probe,
)
from modelctl.core.cluster import config, sync, wsproto
from modelctl.core.cluster.profiles import profile_sha
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.envs import MANAGED_ENGINES, has_env
from modelctl.core.gpu_lock import list_gpu_locks
from modelctl.core.profile import KNOWN_ENGINES, load_profile_at

STATE_FILE = "cluster-reconcile.json"

#: 连续 N 拍"进程还在但健康检查不通"才降级为 FAILED；单拍抖动（引擎 GC、
#: 权重热加载）不该惊动中心。进程消失（alive=False）不适用此计数，直接终态。
DEGRADE_LIMIT = 3

#: 能力探测（nvidia-smi / which）开销大且在起进程期间会被反复调用，缓存 30s
_CAPS_TTL_S = 30.0

# --- 8 态 stage（spec §7.2）--------------------------------------------------
PENDING_PROFILE_SYNC = "PENDING_PROFILE_SYNC"   # 中心已声明，本地还没有这份文件
PROFILE_SYNCED = "PROFILE_SYNCED"               # 文件已落盘且 sha 相符
RUNTIME_OK = "RUNTIME_OK"                       # 运行时具备启动条件
STARTING = "STARTING"                           # 正在起（含下载权重/健康检查等待）
READY = "READY"
DEGRADED = "DEGRADED"                           # 进程在但健康检查连续失败
FAILED = "FAILED"                               # 终态：需 retry 或 goal 变更
STOPPED = "STOPPED"

VALID_STAGES: tuple[str, ...] = (
    PENDING_PROFILE_SYNC, PROFILE_SYNCED, RUNTIME_OK, STARTING,
    READY, DEGRADED, FAILED, STOPPED,
)

#: stage → 中心 `model_states.state`（与单机 CLI 的八态对齐，dashboard 一套渲染）
_STATE_OF_STAGE: dict[str, str] = {
    PENDING_PROFILE_SYNC: "pending", PROFILE_SYNCED: "synced", RUNTIME_OK: "ready_to_start",
    STARTING: "starting", READY: "running", DEGRADED: "degraded",
    FAILED: "failed", STOPPED: "stopped",
}

ERROR_CLASSES: tuple[str, ...] = (
    "venv_missing", "gpu_lock", "oom", "startup_timeout", "model_download_failed",
    "runtime_capability", "profile_invalid", "port_conflict", "health_lost",
)

#: 有序子串规则表：先具体后笼统。`[gpu_lock]` 带方括号前缀（gpu_lock.py 的报错格式），
#: 必须早于 `gpu_list`（profile 字段名），否则"卡位被占"会被误判成"profile 写错"。
_ERROR_RULES: tuple[tuple[str, str], ...] = (
    ("[gpu_lock]", "gpu_lock"),
    ("gpu_list", "profile_invalid"),
    ("端口", "port_conflict"),
    ("专用环境未创建", "venv_missing"),
    ("PATH 中找不到", "venv_missing"),
    ("未安装", "venv_missing"),
    ("环境未就绪", "venv_missing"),
    ("显存不足", "oom"),
    ("out of memory", "oom"),
    ("CUDA error", "oom"),
    ("下载失败", "model_download_failed"),
    ("下载超时", "model_download_failed"),
    ("必填", "profile_invalid"),
    ("必须是", "profile_invalid"),
    ("超过实际", "runtime_capability"),
    ("不支持", "runtime_capability"),
    ("需 vLLM", "runtime_capability"),
    ("需 SGLang", "runtime_capability"),
    ("健康检查超时", "startup_timeout"),
    ("进程提前退出", "startup_timeout"),
)


def classify_error(detail: str) -> str:
    """把引擎/编排的中文报错映射到 error_class（中心据此分类展示/告警）。

    兜底 `runtime_capability` 而非 `unknown`：运维看到"环境能力不足"会去查
    驱动/显存/引擎版本，这恰是未分类报错最常见的真实原因；`unknown` 什么也不提示。
    """
    text = detail or ""
    for needle, cls in _ERROR_RULES:
        if needle in text:
            return cls
    return "runtime_capability"


def _raw_of(text: str) -> dict[str, Any]:
    """profile YAML 原文 → 顶层映射（只为读 docker_image 一类免检字段）。

    解析失败返回 {}：`sync._validate` 已在写盘前拒过语法错误，这里宽容是为了
    "本地被人改坏的文件"不该让 reconciler 抛异常中断整轮调度。
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return raw if isinstance(raw, dict) else {}


def runtime_readiness(engine: str, raw: dict[str, Any],
                      caps: Capabilities) -> tuple[bool, str, str]:
    """运行时是否具备启动该引擎的条件 → (ok, reason, error_class)。

    托管引擎看 venv，但 YAML 显式声明 `docker_image` 时用容器 runtime，venv
    不参与（这条路径必须可达，否则是死代码）；非托管引擎看 `probe()` 的 PATH 结果。
    """
    if engine in MANAGED_ENGINES:
        if str(raw.get("docker_image", "")).strip():
            return True, "", ""
        if has_env(engine):
            return True, "", ""
        return False, f"{engine} 专用环境未创建，执行：modelctl env setup {engine}", "venv_missing"
    if caps.binaries.get(engine):
        return True, "", ""
    hint = "确认二进制已安装并在 PATH 中"
    return False, f"引擎 {engine} 的二进制在 PATH 中找不到，{hint}", "venv_missing"


def profile_sha_safe(path: Path) -> str:
    """读文件算 sha；任何 IO 失败 → ""（调用方按"不匹配"处理，不抛异常）。"""
    try:
        return profile_sha(path.read_text(encoding="utf-8"))
    except OSError:
        return ""


def local_profile_paths(models_dir: Path) -> dict[str, Path]:
    """models/ 下所有 profile 文件的 stem → 绝对路径。

    跳过三类"看起来像 YAML 但不是 profile"的文件：`.master` 备份（sync 覆盖前
    留的）、`.tmp`（原子写中间态）、隐藏文件。stem 冲突时按引擎名**字典序取第一**
    并告警——真实仓库里同 stem 跨引擎属病态配置，但确定性优先于"随机覆盖"，
    否则同一台机器两次心跳会给出不同结论。
    """
    out: dict[str, Path] = {}
    root = Path(models_dir)
    if not root.is_dir():
        return out
    for engine_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for f in sorted(engine_dir.glob("*.yaml")):
            if f.name.startswith(".") or f.name.endswith((".master", ".tmp")):
                continue
            stem = f.stem
            if stem in out:
                logger.warning(f"models/ 下 stem 冲突：{stem} 取 {out[stem]}，忽略 {f}")
                continue
            out[stem] = f
    return out


@dataclass
class Outcome:
    """起停替身的统一返回（形状与 all_service.ComponentResult 一致）。"""

    status: str
    detail: str = ""


def _default_cache_dir() -> Path:
    from modelctl.core.process import cache_dir

    return cache_dir()


def _default_starter(profile: Any, caps: Capabilities, timeout: float) -> Outcome:
    """生产起进程入口：委派 all_service（延迟 import，避免拖慢 worker 启动）。"""
    from modelctl.core.all_service import start_profile

    try:
        got = start_profile(profile, caps, timeout)
    except Exception as exc:  # RequirementError / 适配器内部异常 / 下载异常
        return Outcome("error", str(exc))
    return Outcome(got.status, got.detail)


def _default_stopper(profile: Any, caps: Capabilities, models_dir: Path | None) -> Outcome:
    from modelctl.core.all_service import stop_profile

    try:
        got = stop_profile(profile, caps, models_dir)
    except Exception as exc:
        return Outcome("error", str(exc))
    return Outcome(got.status, got.detail)


def _default_prober(profile: Any) -> dict[str, Any]:
    """观测单个 profile → {up, alive, port, pid, gpus, name}。

    `up` = 健康检查通（引擎真的能服务）；`alive` = PID 文件里的进程还活着。
    两者的差集是 DEGRADED 与 FAILED 的分水岭，不可合并成一个布尔。
    """
    from modelctl.core.process import is_pid_alive, is_running_any, pid_file

    up = is_running_any(profile.name, profile)
    pid = None
    path = pid_file(profile.name)
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = None
    alive = is_pid_alive(pid) if pid else False
    gpus = [idx for idx, owner in list_gpu_locks().items() if owner == profile.name]
    return {"up": bool(up), "alive": bool(alive), "port": profile.port,
            "pid": pid, "gpus": gpus, "name": profile.name}


def _usable_overlay(raw: Any) -> dict[str, str]:
    """中心下发的 env_overlay → 可注入进程环境的白名单子集。

    **整份作废**而不是逐键过滤：一份含 `*_API_KEY` 的 overlay 说明调用方误解了
    env_overlay 的用途（密钥必须留在 worker 本地 .env），逐键过滤会让运维以为
    "密钥已生效"却在下发链路里明文传输——这是需要被立刻发现并纠正的用法错误。
    """
    from modelctl.core.cluster.goals import ENV_OVERLAY_ALLOWLIST, SECRET_KEY_HINTS

    if not isinstance(raw, dict) or not raw:
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        k = str(key)
        if any(hint in k.upper() for hint in SECRET_KEY_HINTS):
            logger.warning(f"env_overlay 含疑似密钥键 {k}，整份 overlay 已丢弃")
            return {}
        if k not in ENV_OVERLAY_ALLOWLIST:
            logger.warning(f"env_overlay 含非白名单键 {k}，整份 overlay 已丢弃")
            return {}
        out[k] = str(value)
    return out


class _OverlayScope:
    """在 with 块内注入 overlay，退出时**逐键精确复原**（含"原本不存在 → 删除"）。

    不能整体备份/还原 os.environ 快照：起进程期间其他线程（webui 请求处理）也在
    读写环境，全量还原会吞掉它们的变更。
    """

    def __init__(self, overlay: dict[str, str]) -> None:
        self._overlay = overlay
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> "_OverlayScope":
        for key, value in self._overlay.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *exc: Any) -> None:
        for key, before in self._saved.items():
            if before is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = before
```

再写 `Reconciler` 类（接在 `_OverlayScope` 之后）：

```python
class Reconciler:
    """worker 侧单写者状态机。

    `starter/stopper/prober/caps` 全部可注入：起停真进程与 nvidia-smi 探测在 CI 与
    单测里不可用，而"该不该调"的判断逻辑（本任务的全部价值）必须能被完整测到。
    """

    def __init__(self, models_dir: Path | None = None, cache_dir: Path | None = None, *,
                 starter: Callable[..., Outcome] | None = None,
                 stopper: Callable[..., Outcome] | None = None,
                 prober: Callable[[Any], dict[str, Any]] | None = None,
                 caps: Capabilities | None = None) -> None:
        self._models = Path(models_dir) if models_dir else PROJECT_ROOT / "models"
        self._cache = Path(cache_dir) if cache_dir else _default_cache_dir()
        self._starter = starter or _default_starter
        self._stopper = stopper or _default_stopper
        self._prober = prober or _default_prober
        self._caps = caps
        self._lock = threading.RLock()
        # goal_id → {sha, intent, stage, reason, error_class, manual, degrade, path,
        #            engine, profile, port, pid, gpu, at}
        self._recs: dict[str, dict[str, Any]] = {}
        self._loaded = False
        # 投递槽：_incoming 只留最新一份快照（旧的已被更新覆盖，执行它没有意义）
        self._incoming: list[dict[str, Any]] = []
        self._pending: list[dict[str, Any]] = []
        self._results: list[dict[str, Any]] = []
        self._caps_cache: tuple[float, Capabilities] | None = None
        #: models/ 里"没有对应 goal"的本地 profile 观测结果（本地手起的模型也要上报）
        self._local: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 状态持久化

    def _state_path(self) -> Path:
        return self._cache / STATE_FILE

    def _load(self) -> None:
        """恢复上一进程的手动位与终态。

        不持久化的话，worker 每次重启都会把"运维手动停掉的模型"重新拉起、把
        "已判定失败的 goal"再撞一次——重启进程不是清除人工决策的理由。
        """
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        recs = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(recs, dict):
            return
        for goal_id, rec in recs.items():
            if isinstance(rec, dict) and rec.get("stage") in VALID_STAGES:
                self._recs[str(goal_id)] = rec

    def _save(self) -> None:
        """状态文件是"尽力而为"的加速件，写失败只告警：心跳回流才是权威上报。"""
        path = self._state_path()
        tmp = path.with_name(path.name + ".tmp")
        payload = {"updated_at": time.time(), "profiles": self._recs}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning(f"集群 reconciler 状态落盘失败（不影响运行）: {exc}")

    # ------------------------------------------------------------------ 投递（无锁）

    def offer_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Agent 线程投递中心快照。**不得加锁**（见模块头线程模型）。"""
        if isinstance(snapshot, dict):
            self._incoming = [snapshot]

    def handle_actions(self, actions: list[dict[str, Any]], *, now: float | None = None) -> None:
        """校验并受理手动指令，result 帧进 `_results` **唯一出口**（Agent 侧统一
        `flush_results()` 取走回传；本方法不返回帧，避免同一 ack 被发送两次）。

        "受理"≠"已执行"：detail 明说"下一轮生效"，避免 dashboard/CLI 把 ≤5s 的
        异步生效窗口谎报成瞬时完成。真正的状态变化由下一拍 `_step` 产生并随心跳回流。
        未知 action / 本机没有该 goal → ok=False 且**不入队**（否则中心先收到
        "已拒绝"，worker 却又在下一轮把它当有效指令处理）。
        """
        self._load()
        now = time.time() if now is None else now
        out: list[dict[str, Any]] = []
        goals = {str(g.get("goal_id", "")): g for g in sync.read_state(self._cache)["goals"]}
        for act in actions:
            action = str(act.get("action", ""))
            goal_id = str(act.get("goal_id", ""))
            seq = act.get("seq", 0)
            if action not in wsproto.VALID_ACTIONS:
                out.append(wsproto.make_result(seq, False, f"不支持的指令 {action!r}"))
                continue
            goal = goals.get(goal_id)
            if goal is None:
                out.append(wsproto.make_result(seq, False, f"本机没有目标 {goal_id}"))
                continue
            self._pending.append({"seq": seq, "action": action, "goal_id": goal_id,
                                  "ok_seen": now})
            name = str(goal.get("profile", goal_id))
            out.append(wsproto.make_result(seq, True, f"{name} 已受理，下一轮生效"))
        if out:
            self._results.extend(out)      # 无锁：list.extend 是原子的

    def flush_results(self) -> list[dict[str, Any]]:
        """取走并清空待回传的 result 帧（原子换表，Agent 线程可安全调用）。"""
        out, self._results = self._results, []
        return out

    # ------------------------------------------------------------------ 快照应用

    def apply_snapshot(self, snapshot: dict[str, Any], *, now: float | None = None) -> sync.SyncResult:
        """落盘中心快照并让后续拍次看到新 goal（外部直调路径，带锁）。"""
        now = time.time() if now is None else now
        with self._lock:
            return self._apply_locked(snapshot, now)

    def _apply_locked(self, snapshot: dict[str, Any], now: float) -> sync.SyncResult:
        self._load()
        out = sync.apply_snapshot(snapshot, models_dir=self._models,
                                  cache_dir=self._cache,
                                  force=bool(snapshot.get("force")), now=now)
        state = sync.read_state(self._cache)
        present = {str(g["goal_id"]): g for g in state["goals"]}
        for goal_id in list(self._recs):
            if goal_id not in present:
                del self._recs[goal_id]          # goal 已从快照消失 = 记录一并作废
        for goal_id, goal in present.items():
            self._ensure_rec(goal_id, goal, now)
        self._save()
        return out

    def _drain_snapshot(self, *, now: float) -> None:
        """循环线程消费投递槽（原子取空后应用，与外部直调互斥）。"""
        pending, self._incoming = self._incoming, []
        for snapshot in pending:
            with self._lock:
                self._apply_locked(snapshot, now)

    def _ensure_rec(self, goal_id: str, goal: dict[str, Any], now: float) -> None:
        """goal 内容或 intent 变化 → 整条重置（含清手动位）。

        重置是"中心重新表达了一次意图"的信号：运维改了 YAML 里的端口或把 intent
        从 stop 改成 start，都意味着此前的人工干预/失败判定不再适用。
        """
        sha, intent = str(goal.get("sha", "")), str(goal.get("intent", "start"))
        rec = self._recs.get(goal_id)
        if rec is not None and rec.get("sha") == sha and rec.get("intent") == intent:
            return
        self._recs[goal_id] = {"sha": sha, "intent": intent, "stage": PENDING_PROFILE_SYNC,
                               "reason": "", "error_class": "", "manual": "",
                               "degrade": 0, "engine": str(goal.get("engine", "")),
                               "profile": str(goal.get("profile", "")),
                               "path": str(goal.get("path", "")),
                               "env_overlay": goal.get("env_overlay"),
                               "port": None, "pid": None, "gpu": [], "at": now}

    def _apply_manual(self, actions: list[dict[str, Any]], *, now: float) -> None:
        for act in actions:
            goal_id = str(act.get("goal_id", ""))
            rec = self._recs.get(goal_id)
            if rec is None:
                continue
            action = str(act.get("action", ""))
            if action == "retry":
                rec.update({"stage": PENDING_PROFILE_SYNC, "reason": "", "error_class": "",
                            "manual": "", "degrade": 0, "at": now})
            elif action in ("stop", "restart", "start"):
                rec["manual"] = action
                rec["at"] = now

    # ------------------------------------------------------------------ 执行

    def _fail(self, rec: dict[str, Any], detail: str, now: float) -> None:
        rec.update({"stage": FAILED, "reason": detail, "error_class": classify_error(detail),
                    "degrade": 0, "at": now})

    def _managed_path(self, rec: dict[str, Any]) -> Path | None:
        """goal → 本地 YAML 路径。

        优先用 sync 落盘时登记的**绝对路径**（那才是真写出来的位置），回退按
        engine/profile 重算——覆盖"状态文件被删但 YAML 还在"的半损坏场景。
        """
        path = str(rec.get("path", ""))
        if path and Path(path).is_file():
            return Path(path)
        engine, profile = str(rec.get("engine", "")), str(rec.get("profile", ""))
        if not engine or not profile:
            return None
        guess = self._models / engine / f"{profile}.yaml"
        return guess if guess.is_file() else None

    def _observe(self, rec: dict[str, Any]) -> dict[str, Any]:
        """按**下发文件本身**解析 Profile 再观测（stem↔name 一致性的唯一保证）。"""
        path = self._managed_path(rec)
        if path is None:
            return {}
        try:
            prof = load_profile_at(path)
        except Exception as exc:  # ProfileError / yaml 错误 / 字段非法
            return {"up": False, "alive": False, "port": None, "pid": None, "gpus": [],
                    "name": str(rec.get("profile", "")), "profile_error": str(exc)}
        got = dict(self._prober(prof))
        got["profile"] = prof
        return got

    def _step(self, goal_id: str, goal: dict[str, Any], rec: dict[str, Any],
              caps: Capabilities, now: float) -> None:
        """单个 goal 的一拍推进。顺序敏感，改动前先读注释。"""
        # 失败即终态：早退，绝不重复撞同一个错误（本任务规则 2）
        if rec["stage"] == FAILED:
            return
        path = self._managed_path(rec)
        if path is None:
            rec.update({"stage": PENDING_PROFILE_SYNC,
                        "reason": "等待中心下发 profile 文件", "at": now})
            return
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            rec.update({"stage": PENDING_PROFILE_SYNC,
                        "reason": "profile 文件不可读，等待重新下发", "at": now})
            return
        want = str(rec.get("sha", ""))
        got = profile_sha(text)
        if want and got != want:
            rec.update({"stage": PENDING_PROFILE_SYNC,
                        "reason": "本地 profile 文件与中心声明不一致（漂移），等待重新下发",
                        "at": now})
            return

        # **必须在写 PROFILE_SYNCED 之前**取上一拍：否则"上一拍是否 READY"永远为假，
        # 健康降级判据彻底失效（每个 READY 的模型都会在第一拍被判成 SYNCED→重新起）。
        prev = str(rec.get("stage", ""))
        rec["path"], rec["engine"] = str(path), str(goal.get("engine", rec.get("engine", "")))
        rec["profile"] = str(goal.get("profile", rec.get("profile", "")))
        if prev not in (STARTING, READY, DEGRADED):
            rec["stage"] = PROFILE_SYNCED

        manual = str(rec.get("manual", ""))
        intent = manual or str(goal.get("intent", "start"))

        # ---- 方向为停：先停，本轮到此为止（restart 是"停完下轮由 intent 拉起"）
        if intent in ("stop", "restart"):
            obs = self._observe(rec)
            if obs.get("up") or obs.get("alive"):
                prof = obs.get("profile")
                if prof is None:
                    self._fail(rec, "profile 解析失败，无法定位进程", now)
                    return
                with _OverlayScope(_usable_overlay(goal.get("env_overlay"))):
                    out = self._stopper(prof, caps, self._models)
                if out.status == "error":
                    self._fail(rec, out.detail or "停止失败", now)
                    return
                rec.update({"stage": STOPPED, "reason": "", "error_class": "",
                            "degrade": 0, "pid": None, "gpu": [], "at": now})
            else:
                rec.update({"stage": STOPPED, "reason": "", "error_class": "",
                            "degrade": 0, "at": now})
            # 手动 stop **不清位**：清了下轮按 intent=start 复活（规则 3）
            if intent == "restart":
                rec["manual"] = ""
            return

        # ---- 方向为起：已在服务 → READY
        obs = self._observe(rec)
        if obs.get("up"):
            rec.update({"stage": READY, "reason": "", "error_class": "", "degrade": 0,
                        "port": obs.get("port"), "pid": obs.get("pid"),
                        "gpu": list(obs.get("gpus") or []), "at": now})
            return

        # ---- 上一拍在服务、这一拍不通：进程没了直接终态，进程还在才计数降级
        if prev in (READY, DEGRADED):
            if not obs.get("alive"):
                self._fail(rec, "进程已退出，健康检查不再可达（health_lost）", now)
                rec["error_class"] = "health_lost"
                return
            degrade = int(rec.get("degrade", 0)) + 1
            if degrade >= DEGRADE_LIMIT:
                self._fail(rec, f"连续 {degrade} 次健康检查失败，进程仍在但不可服务", now)
                rec["error_class"] = "health_lost"
                return
            rec.update({"stage": DEGRADED, "degrade": degrade,
                        "reason": "健康检查连续失败（进程仍在）", "at": now})
            return

        # ---- 运行时不具备条件：不撞进程，直接终态并说明怎么修
        ok, reason, cls = runtime_readiness(str(rec.get("engine", "")), _raw_of(text), caps)
        if not ok:
            rec.update({"stage": FAILED, "reason": reason, "error_class": cls,
                        "degrade": 0, "at": now})
            return
        rec["stage"] = RUNTIME_OK

        prof = obs.get("profile")
        if prof is None:
            try:
                prof = load_profile_at(path)
            except Exception as exc:
                self._fail(rec, f"profile 解析失败：{exc}", now)
                return

        # **先落 STARTING 再阻塞起进程**：否则 300s 冷启动期间中心只能看到
        # PROFILE_SYNCED，"指令没到"与"正在启动"在 dashboard 上无法区分。
        rec.update({"stage": STARTING, "reason": "", "error_class": "", "at": now})
        self._save()
        with _OverlayScope(_usable_overlay(goal.get("env_overlay"))):
            out = self._starter(prof, caps, float(config.start_timeout_s()))
        if out.status == "error":
            self._fail(rec, out.detail or "启动失败", now)
            return

        after = self._observe(rec)
        if after.get("up"):
            rec.update({"stage": READY, "port": after.get("port"), "pid": after.get("pid"),
                        "gpu": list(after.get("gpus") or []), "reason": "",
                        "error_class": "", "degrade": 0, "at": now})
        else:
            # skipped（已在运行）或起完还没过健康检查：保持 STARTING，下一拍再判
            rec.update({"port": after.get("port"), "pid": after.get("pid"), "at": now})

    def _observe_unmanaged(self, now: float) -> None:
        """本地存在但**没有 goal** 的 profile：只观测、只上报，绝不代客起停。

        这些是运维在 worker 本机手起的模型。集群接管它们需要 M2 的"收养"流程；
        M1 若擅自 stop，等于中心悄悄杀掉了不属于它管理的进程。
        """
        managed = {str(p) for p in sync.managed_paths(self._cache)}
        seen: set[str] = set()
        for stem, path in local_profile_paths(self._models).items():
            if str(path) in managed:
                continue
            seen.add(stem)
            try:
                prof = load_profile_at(path)
            except Exception:
                continue
            got = self._prober(prof)
            stage = READY if got.get("up") else (STOPPED if not got.get("alive") else DEGRADED)
            self._local.setdefault(stem, {"goal_id": "", "stage": stage,
                                          "state": _STATE_OF_STAGE[stage], "reason": "",
                                          "error_class": "", "managed": False,
                                          "port": got.get("port"), "pid": got.get("pid"),
                                          "gpu": list(got.get("gpus") or []), "at": now})
            self._local[stem]["stage"] = stage
            self._local[stem]["state"] = _STATE_OF_STAGE[stage]
            self._local[stem]["at"] = now
        for stem in set(self._local) - seen:
            del self._local[stem]

    def reconcile_once(self, *, now: float | None = None) -> None:
        """一拍：应用投递 → 处理指令 → 逐个推进 → 观测本地 → 落盘。"""
        now = time.time() if now is None else now
        self._drain_snapshot(now=now)
        with self._lock:
            self._load()
            pending, self._pending = self._pending, []
            self._apply_manual(pending, now=now)
            caps = self._capabilities()
            state = sync.read_state(self._cache)
            goals = {str(g["goal_id"]): g for g in state["goals"]}
            for goal_id, goal in goals.items():
                rec = self._recs.get(goal_id)
                if rec is None:
                    self._ensure_rec(goal_id, goal, now)
                    rec = self._recs[goal_id]
                try:
                    self._step(goal_id, goal, rec, caps, now)
                except Exception as exc:  # 单个 goal 的异常不得中断整轮调度
                    logger.exception(f"reconcile {goal_id} 异常")
                    self._fail(rec, f"内部异常：{exc}", now)
            self._observe_unmanaged(now)
            self._save()

    # ------------------------------------------------------------------ 上报

    def _capabilities(self) -> Capabilities:
        if self._caps is not None:
            return self._caps
        now = time.time()
        hit = self._caps_cache
        if hit and now - hit[0] < _CAPS_TTL_S:
            return hit[1]
        got = probe()
        self._caps_cache = (now, got)
        return got

    def _capacity(self, caps: Capabilities) -> dict[str, Any]:
        return {"gpu_count": caps.gpu_count, "vram_total_mb": all_vram_total_mb(caps),
                "vram_free_mb": free_vram_total_mb(caps)}

    def _runtimes(self, caps: Capabilities) -> dict[str, dict[str, Any]]:
        """遍历**全部** KNOWN_ENGINES：只报"装过的"会让中心无法区分"没装"与"没探测"，
        于是 gate 会把不可用的引擎放行到这台节点上。"""
        out: dict[str, dict[str, Any]] = {}
        for engine in sorted(KNOWN_ENGINES):
            ok = has_env(engine) if engine in MANAGED_ENGINES else bool(caps.binaries.get(engine))
            out[engine] = {"ok": bool(ok)}
        return out

    def _collect(self) -> dict[str, Any]:
        """每拍重建快照（不缓存上一轮），保证 STARTING/DEGRADED 实时可见。"""
        self._load()
        state = sync.read_state(self._cache)
        caps = self._capabilities()
        drift = sync.scan_drift(self._cache, self._models)
        profiles: dict[str, dict[str, Any]] = {}
        stems: list[str] = []
        for stem in local_profile_paths(self._models):
            stems.append(stem)
        for goal in state["goals"]:
            goal_id = str(goal["goal_id"])
            name = str(goal.get("profile", ""))
            rec = self._recs.get(goal_id)
            if rec is None:
                # 本进程尚未跑过这一条（worker 刚重启）：用磁盘 sha 推导最小可用 stage
                path = Path(str(goal.get("path", "")))
                synced = bool(goal.get("sha")) and profile_sha_safe(path) == str(goal["sha"])
                stage = PROFILE_SYNCED if synced else PENDING_PROFILE_SYNC
                port, pid, gpu = None, None, []
            else:
                stage = str(rec.get("stage", PENDING_PROFILE_SYNC))
                port, pid, gpu = rec.get("port"), rec.get("pid"), list(rec.get("gpu") or [])
            entry = {"goal_id": goal_id, "stage": stage,
                     "state": _STATE_OF_STAGE.get(stage, "pending"),
                     "reason": str((rec or {}).get("reason", "")),
                     "error_class": str((rec or {}).get("error_class", "")),
                     "managed": True, "port": port, "pid": pid, "gpu": gpu,
                     "at": (rec or {}).get("at")}
            # setdefault：有 goal 的条目**不被本地同名观测覆盖**（goal 侧信息更全）
            profiles.setdefault(name, entry)
        for name, entry in self._local.items():
            profiles.setdefault(name, dict(entry))
        return {"revision": state["revision"], "profiles": profiles, "drift": drift,
                "local_profiles": sorted(set(stems)), "capacity": self._capacity(caps),
                "runtimes": self._runtimes(caps)}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._collect()

    def heartbeat_payload(self) -> dict[str, Any]:
        """心跳扩展段（Task 2 的六个键）。缺失段由 Agent 侧决定"整段省略"。"""
        got = self.snapshot()
        return {"profiles": got["profiles"], "goal_sync": {"revision": got["revision"]},
                "drift": got["drift"], "local_profiles": got["local_profiles"],
                "capacity": got["capacity"], "runtimes": got["runtimes"]}

    # ------------------------------------------------------------------ 循环

    def run(self, stop_event: threading.Event) -> None:
        """后台循环。reconcile 周期与心跳周期**刻意不同步**：本地收敛不该等心跳，
        心跳也不该被本地收敛拖慢（两条时间线各自独立自愈）。"""
        interval = config.reconcile_interval_s()
        logger.info(f"集群 reconciler 启动（周期 {interval}s，模型目录 {self._models}）")
        while not stop_event.is_set():
            try:
                self.reconcile_once()
            except Exception:
                logger.exception("集群 reconciler 单轮异常（已忽略，继续下一轮）")
            stop_event.wait(interval)
        logger.info("集群 reconciler 已停止")


# --------------------------------------------------------------------------- 进程内单例

_CURRENT: Reconciler | None = None
_CURRENT_LOCK = threading.Lock()
_STOP: threading.Event | None = None


def current() -> Reconciler | None:
    return _CURRENT


def start_reconciler_in_background() -> Reconciler | None:
    """按角色闸门启动。

    **刻意不看 `center_url`**：中心宕机或未配置时，worker 仍必须按**最后已知的
    goal** 维持推理服务（spec §8.1：中心故障不得影响已下发的推理）。若在此处
    要求 center_url，一次中心重启就会让全部 worker 忘记该跑什么模型。
    """
    global _CURRENT, _STOP
    with _CURRENT_LOCK:
        if _CURRENT is not None:
            return _CURRENT
        if not config.is_worker():
            return None
        rt = Reconciler()
        stop = threading.Event()
        thread = threading.Thread(target=rt.run, args=(stop,),
                                  name="cluster-reconcile", daemon=True)
        _CURRENT, _STOP = rt, stop
        thread.start()
        return rt


def _stop_event() -> threading.Event | None:
    """测试/优雅退出用。"""
    return _STOP
```

- [ ] **Step 4b: 修正 Task 8 的 `apply_snapshot` 以支持 `force`**

Task 11 的"中心强制重下发"需要打破同 revision 短路，参数在此一次性加好（默认 `False`，
Task 8 既有 22 条测试不受影响）。把 `sync.apply_snapshot` 签名与短路分支改为：

```python
def apply_snapshot(snapshot: dict[str, Any], *, models_dir: Path, cache_dir: Path,
                   now: float, force: bool = False) -> SyncResult:
    """把中心快照落盘。同 revision 直接短路（幂等 + 省 IO）；`force=True` 打破短路。

    `force` 供中心 `POST /nodes/{id}/sync` 使用：本地文件被人为改坏时，revision
    并未变化，若不强制就会**永远修不好**（漂移被短路跳过，只上报不修复）。
    """
    ...
    if revision and not force and state["revision"] == revision:
        result.skipped = [str(g["goal_id"]) for g in goals]
        result.drift = scan_drift(cache_dir, models_dir)
        return result
```

并在 `tests/test_cluster_sync_writer.py` 追加 2 条：

```python
def test_force_breaks_revision_short_circuit(dirs):
    models, cache = dirs
    g = _goal()
    apply_snapshot({"revision": "r1", "goals": [g]}, models_dir=models, cache_dir=cache, now=1.0)
    (models / "vllm" / "qwen.yaml").write_text("port: 9999\n", encoding="utf-8")
    same = apply_snapshot({"revision": "r1", "goals": [g]}, models_dir=models, cache_dir=cache, now=2.0)
    assert same.written == [] and same.drift == [g["goal_id"]]      # 默认：只报不修
    forced = apply_snapshot({"revision": "r1", "goals": [g]}, models_dir=models,
                            cache_dir=cache, now=3.0, force=True)
    assert forced.written == [g["goal_id"]] and forced.drift == []  # 强制：重写并修好


def test_reconcile_forwards_force_flag_from_snapshot(dirs):
    """reconcile 侧只透传：快照里带 force 就强制（避免两处各写一套判断）。"""
```

（第二条已在 Step 1 的 `test_drift_reported_once_repaired_by_resync` 覆盖，此处仅登记意图。）

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/test_cluster_reconcile.py -q`
Expected: PASS（31 条）

Run: `uv run pytest tests/ -q -k "cluster"`
Expected: PASS（Task 1–9 全部 cluster 用例；`runtimes` 键变更不影响 Task 4/7 已有断言，若失败先检查 `_runtimes` 是否遍历了全量 `KNOWN_ENGINES`）

Run: `uv run ruff check src/modelctl/core/cluster/reconcile.py src/modelctl/core/cluster/config.py src/modelctl/core/profile.py tests/test_cluster_reconcile.py; uv run mypy src/modelctl/core/cluster/reconcile.py`
Expected: 全部通过（reconcile 里 `Any` 密集，`_observe` 返回 dict 时混入 `profile` 对象——保持 `dict[str, Any]` 注解，勿收窄）

- [ ] **Step 6: 提交**

```bash
git add src/modelctl/core/cluster/reconcile.py src/modelctl/core/cluster/config.py src/modelctl/core/profile.py src/modelctl/core/cluster/sync.py tests/test_cluster_reconcile.py tests/test_cluster_sync_writer.py
git commit -m "feat(cluster): worker 侧 reconciler（8 态 stage + 错误分类 + 心跳快照）"
```

<!-- MORE9 -->

---

### Task 10: Agent 接线 reconciler（ack 投递 + 心跳扩展段 + result 回传）

Task 9 造出了状态机，本任务把它**接到那条已经存在的 WS 上**——不改连接模型、不加第二条通道。

**硬约束：Agent 线程绝不在 `send`/`recv` 之间做任何重活。** 一次心跳周期内 Agent 只允许：
读缓存（`heartbeat_payload`）、发一帧、收一帧、将 ack 内容投递给 reconciler、把 result 帧发回去。
所有写盘/起停进程都在 reconcile 循环线程。这条约束是 M0"心跳从不断流"性质的延续，也是
Task 9 里"投递路径无锁"的另一半。

**心跳周期（`welcome.interval_s`，默认 10s）与 reconcile 周期（5s）刻意不同步**：本地收敛不该
等心跳，心跳也不该被本地收敛拖慢。代价是"中心改 goal → worker 开始动作"的延迟上界为
`心跳 + reconcile ≈ 15s`，相对分钟级冷启动可忽略；换来的是两条时间线各自独立自愈
（spec §8.1：任一侧卡顿都不能让另一侧失去推进能力）。

**Files:**
- Modify: `src/modelctl/core/cluster/agent.py`（`collect_heartbeat`/`deliver_ack`/`WorkerAgent.__init__`/循环）
- Modify: `src/modelctl/core/webui/server.py`（抽出 `start_cluster_background()`，先 reconciler 后 Agent）
- Test: `tests/test_cluster_agent_v2.py`

**Interfaces:**
- Consumes: `reconcile.current()/start_reconciler_in_background()`（Task 9）、`wsproto.parse_ack/make_heartbeat/dumps`（Task 2）
- Produces:
  - `collect_heartbeat(rt=None) -> dict`：基础段 `{profiles, gpu:{count,vram_total_mb_per_gpu}, host}` 与 M0 **逐键一致**；`rt` 非空则合并 `rt.heartbeat_payload()` 的六个扩展键
  - `deliver_ack(ack, rt) -> list[dict]`：解析 ack → 投递 snapshot/actions → 返回 `rt.flush_results()`；`rt is None` → `[]`
  - `WorkerAgent(stop_event, reconciler=None)`：`reconciler=None` 时每拍取 `reconcile.current()`
  - `server.start_cluster_background() -> bool`：角色闸门 + 异常兜底，返回是否启动了后台线程

- [ ] **Step 1: 写失败测试** — 创建 `tests/test_cluster_agent_v2.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_agent_v2.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 12:00
# @Desc   : Agent 与 reconciler 的接线（ack 投递 / 心跳扩展段 / result 回传 / 启动闸门）
# ===============================================================================
import contextlib
import itertools
import json
import threading

import pytest

pytest.importorskip("websockets")
from websockets.sync.server import serve  # noqa: E402

from modelctl.core.cluster import agent, reconcile, wsproto  # noqa: E402


class Recorder:
    """reconciler 替身：记录被投递的内容，返回固定扩展段。"""

    def __init__(self):
        self.snapshots: list[dict] = []
        self.actions: list[dict] = []
        self.payload = {"profiles": {"qwen": {"stage": "READY"}},
                        "goal_sync": {"revision": "rev-9"}, "drift": [],
                        "local_profiles": ["qwen"], "capacity": {"gpu_count": 2},
                        "runtimes": {"vllm": {"ok": True}}}

    def offer_snapshot(self, snapshot):
        self.snapshots.append(snapshot)

    def handle_actions(self, actions, *, now=None):
        self.actions.extend(actions)

    def flush_results(self):
        return []

    def heartbeat_payload(self):
        return dict(self.payload)


class Boom(Recorder):
    def heartbeat_payload(self):
        raise RuntimeError("探测炸了")


def test_collect_heartbeat_keeps_m0_shape_without_reconciler():
    """rt=None 必须与 M0 逐键一致：中心升级与 worker 升级不同步是常态。"""
    hb = agent.collect_heartbeat()
    assert sorted(hb) == ["gpu", "host", "profiles"]


def test_collect_heartbeat_merges_reconciler_segments():
    hb = agent.collect_heartbeat(Recorder())
    assert hb["goal_sync"] == {"revision": "rev-9"}
    assert hb["local_profiles"] == ["qwen"]
    assert hb["runtimes"] == {"vllm": {"ok": True}}
    assert hb["capacity"] == {"gpu_count": 2}
    assert hb["drift"] == []
    # 基础段仍在（中心 M0 分支还要读 gpu/host）
    assert "gpu" in hb and "host" in hb


def test_collect_heartbeat_drops_profiles_when_reconciler_broken():
    """reconciler 抛错时**整段省略**，尤其不能回 `profiles: {}`。

    空映射在中心侧是"worker 明确说本机没有模型"，会被当作事实覆盖台账——
    一次偶发探测异常就抹掉全部在跑模型，比不上报严重得多。省略 = 未知 = 保留旧值。
    """
    hb = agent.collect_heartbeat(Boom())
    assert "profiles" not in hb and "goal_sync" not in hb and "drift" not in hb
    assert "gpu" in hb and "host" in hb


def test_deliver_ack_noop_without_reconciler():
    """reconciler 尚未创建（启动空窗）时 ack 照常丢弃，不得抛错。"""
    ack = {"t": "ack", "seq": 1, "sync": {"revision": "rev-1", "goals": [{"goal_id": "a"}]}}
    assert agent.deliver_ack(ack, None) == []


def test_deliver_ack_offers_snapshot_and_actions():
    rt = Recorder()
    ack = {"t": "ack", "seq": 3,
           "sync": {"revision": "rev-1", "goals": [{"goal_id": "qwen@@w-1"}]},
           "actions": [{"seq": 9, "action": "stop", "goal_id": "qwen@@w-1", "profile": "qwen"}]}
    assert agent.deliver_ack(ack, rt) == []
    assert rt.snapshots == [{"revision": "rev-1", "goals": [{"goal_id": "qwen@@w-1"}]}]
    assert rt.actions == [{"seq": 9, "action": "stop", "goal_id": "qwen@@w-1",
                           "profile": "qwen"}]


def test_deliver_ack_returns_reconciler_results():
    class WithResult(Recorder):
        def flush_results(self):
            return [{"t": "result", "seq": 9, "ok": True, "detail": "已受理"}]

    out = agent.deliver_ack({"t": "ack"}, WithResult())
    assert out == [{"t": "result", "seq": 9, "ok": True, "detail": "已受理"}]


def test_deliver_ack_tolerates_m0_center_ack():
    """M0 中心的 ack 只有 `{"t":"ack"}`：不得抛错、不得投递任何东西。"""
    rt = Recorder()
    assert agent.deliver_ack({"t": "ack"}, rt) == []
    assert rt.snapshots == [] and rt.actions == []


# --------------------------------------------------------------------- 端到端（假中心）

@contextlib.contextmanager
def _fake_center(handler, monkeypatch, tmp_path, rt):
    """假中心 + Agent 线程。

    必须用 contextmanager 把断言包在 `with serve(...)` **内部**：若在 `with` 体内
    `return`，上下文立即退出、服务器随即关闭，测试拿到的端口已无人监听——表现为
    "Agent 一直在退避重连、断言超时"，且极易被误读成 Agent 逻辑有 bug。
    假中心沿用 M0 范式：`serve(...)` 只 bind/listen，须自起 `serve_forever` 线程。
    """
    from modelctl.core.cluster import agent as ag

    with serve(handler, "127.0.0.1", 0) as srv:
        port = srv.socket.getsockname()[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        monkeypatch.setenv("CLUSTER_CENTER_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("CLUSTER_NODE_ID", "w-v2")
        monkeypatch.setenv("CLUSTER_JOIN_TOKEN", "JT")
        monkeypatch.setenv("CLUSTER_NODE_TOKEN", "")
        monkeypatch.setattr(ag, "ENV_PATH", tmp_path / ".env")
        stop = threading.Event()
        t = threading.Thread(target=agent.WorkerAgent(stop_event=stop, reconciler=rt).run,
                             daemon=True)
        t.start()
        try:
            yield srv
        finally:
            stop.set()
            t.join(timeout=5)


def test_agent_delivers_ack_to_reconciler(tmp_path, monkeypatch):
    got = threading.Event()
    seen: dict[str, object] = {}

    def handler(conn):
        conn.recv()
        conn.send(wsproto.dumps(wsproto.make_welcome("NT", 1, 1)))
        conn.recv()                                        # heartbeat
        conn.send(wsproto.dumps({"t": "ack", "seq": 1,
                                 "sync": {"revision": "rev-7", "goals": []}}))

    rt = Recorder()
    original = rt.offer_snapshot

    def spy(snapshot):
        original(snapshot)
        seen["snapshot"] = snapshot
        got.set()

    rt.offer_snapshot = spy
    with _fake_center(handler, monkeypatch, tmp_path, rt):
        assert got.wait(5), "ack 里的 sync 未投递给 reconciler"
        assert seen["snapshot"]["revision"] == "rev-7"


def test_agent_sends_result_frame_after_ack(tmp_path, monkeypatch):
    """受理回执必须在**下一次 send** 就搭出去（不等下一次心跳周期）。"""
    arrived = threading.Event()
    frames: list[dict] = []

    def handler(conn):
        conn.recv()
        conn.send(wsproto.dumps(wsproto.make_welcome("NT", 1, 1)))
        conn.recv()                                        # heartbeat
        conn.send(wsproto.dumps({"t": "ack", "seq": 1,
                                 "actions": [{"seq": 4, "action": "stop",
                                              "goal_id": "qwen@@w-1", "profile": "qwen"}]}))
        reply = json.loads(conn.recv())
        frames.append(reply)
        if reply.get("t") == "result":
            arrived.set()

    class LateResult(Recorder):
        def flush_results(self):
            return [wsproto.make_result(4, True, "qwen 已受理，下一轮生效")]

    with _fake_center(handler, monkeypatch, tmp_path, LateResult()):
        assert arrived.wait(5), f"中心未收到 result 帧：{frames}"
        assert frames[0]["seq"] == 4 and frames[0]["ok"] is True


def test_agent_reports_reconciler_revision_to_center(tmp_path, monkeypatch):
    """心跳里的 goal_sync.revision 是中心判断"要不要捎带 sync"的唯一依据。"""
    seen: dict[str, object] = {}
    got = threading.Event()

    def handler(conn):
        conn.recv()
        conn.send(wsproto.dumps(wsproto.make_welcome("NT", 1, 1)))
        msg = json.loads(conn.recv())
        seen["hb"] = msg
        got.set()
        conn.send(wsproto.dumps({"t": "ack"}))

    with _fake_center(handler, monkeypatch, tmp_path, Recorder()):
        assert got.wait(5)
        assert seen["hb"]["payload"]["goal_sync"] == {"revision": "rev-9"}


def test_agent_survives_reconciler_broken(tmp_path, monkeypatch):
    """reconciler 取数炸了也必须继续心跳：断流会被中心按 lease 判离线。"""
    first, second = threading.Event(), threading.Event()
    n = itertools.count()

    def handler(conn):
        conn.recv()
        conn.send(wsproto.dumps(wsproto.make_welcome("NT", 1, 1)))
        for _ in range(2):
            msg = json.loads(conn.recv())
            if msg.get("payload", {}).get("goal_sync"):
                first.set()
            else:
                second.set()
            conn.send(wsproto.dumps({"t": "ack"}))

    class Flaky(Recorder):
        def heartbeat_payload(self):
            if next(n) == 0:
                return dict(self.payload)
            raise RuntimeError("探测炸了")

    with _fake_center(handler, monkeypatch, tmp_path, Flaky()):
        assert first.wait(5), "首拍未上报扩展段"
        assert second.wait(5), "reconciler 抛错后 Agent 停止了心跳"


# --------------------------------------------------------------------- 启动闸门

class Noop:
    """闸门测试用替身：`run` 立即返回，绝不触碰真实 models/ 目录。"""

    def __init__(self, *a, **kw):
        pass

    def run(self, stop_event):
        return None


@pytest.mark.parametrize(("role", "started"), [("solo", False), ("control-plane", False),
                                               ("worker", True), ("both", True)])
def test_background_start_gate_follows_role(monkeypatch, role, started):
    from modelctl.core.webui import server

    monkeypatch.setenv("CLUSTER_ROLE", role)
    monkeypatch.setattr(reconcile, "_CURRENT", None, raising=False)
    monkeypatch.setattr(reconcile, "_STOP", None, raising=False)
    monkeypatch.setattr(reconcile, "Reconciler", Noop)
    assert server.start_cluster_background() is started
    assert (reconcile.current() is not None) is started


def test_background_start_is_idempotent(monkeypatch):
    """重复调用不得起第二个 reconciler（两个循环会互相抢同一批 goal）。"""
    from modelctl.core.webui import server

    monkeypatch.setenv("CLUSTER_ROLE", "worker")
    monkeypatch.setattr(reconcile, "_CURRENT", None, raising=False)
    monkeypatch.setattr(reconcile, "_STOP", None, raising=False)
    monkeypatch.setattr(reconcile, "Reconciler", Noop)
    assert server.start_cluster_background() is True
    first = reconcile.current()
    assert server.start_cluster_background() is True
    assert reconcile.current() is first                  # 第二次未新建实例
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cluster_agent_v2.py -q`
Expected: FAIL —— `AttributeError: module 'modelctl.core.cluster.agent' has no attribute 'deliver_ack'`（`collect_heartbeat` 前两条会先通过，它们是 M0 既有形状）

- [ ] **Step 3: 实现——`agent.py`**

`collect_heartbeat` 改为（保留 M0 的 docstring 意图，新增 `rt` 参数）：

```python
def collect_heartbeat(rt: Any = None) -> dict[str, Any]:
    """心跳 payload：M0 基础段 + （可选）reconciler 扩展段。

    `rt` 为 None（solo 误启、reconciler 尚未创建）时返回与 M0 **逐键一致**的形状。
    `rt` 存在但取数抛错时**整段省略扩展段并删掉 profiles 键**：profiles={} 在中心侧
    读作"本机确实没有模型"，会覆盖台账；省略才读作"未知"，中心据此保留上一次的事实。
    """
    gpu: dict[str, Any] = {}
    try:
        from modelctl.core.capabilities import probe

        caps = probe()
        gpu = {"count": caps.gpu_count, "vram_total_mb_per_gpu": caps.vram_total_mb_per_gpu}
    except Exception as exc:  # noqa: BLE001 — 探测失败不阻断心跳
        logger.debug(f"心跳 GPU 探测失败（忽略）: {exc}")
    payload: dict[str, Any] = {"profiles": {}, "gpu": gpu, "host": {}}
    if rt is None:
        return payload
    try:
        payload.update(rt.heartbeat_payload())
    except Exception as exc:  # noqa: BLE001 — 扩展段缺失只是本轮少报，不影响注册/租约
        logger.debug(f"心跳扩展段采集失败（本轮省略）: {exc}")
        payload.pop("profiles", None)
    return payload


def deliver_ack(ack: Any, rt: Any) -> list[dict[str, Any]]:
    """把中心 ack 的内容投递给 reconciler，返回需要立刻回传的 result 帧。

    只做投递与读缓存（Task 9 的单写者约定）：这里任何"顺手做点事"都会把
    300s 量级的起停阻塞引进心跳线程，进而让中心把健康节点判成 stale。
    """
    if rt is None:
        return []
    parsed = wsproto.parse_ack(ack)
    if parsed["sync"] is not None:
        rt.offer_snapshot(parsed["sync"])
    if parsed["actions"]:
        rt.handle_actions(parsed["actions"])
    return list(rt.flush_results())
```

`WorkerAgent` 的 `__init__` 与循环：

```python
    def __init__(self, stop_event: threading.Event, reconciler: Any = None) -> None:
        self._stop = stop_event
        self._reconciler = reconciler
        self._node_token = config.node_token()

    def _reconciler_now(self) -> Any:
        """每拍取一次 reconciler。

        注入优先（测试）；否则读全局单例——webui 里 reconciler 与 Agent 的启动顺序
        不构成正确性前提：单例尚未就绪时本轮按"无扩展段"上报，下一拍自动接上。
        """
        return self._reconciler if self._reconciler is not None else reconcile.current()
```

循环体最后三行改为：

```python
            while not self._stop.is_set():
                if self._stop.wait(interval):
                    break
                rt = self._reconciler_now()
                ws.send(wsproto.dumps(wsproto.make_heartbeat(collect_heartbeat(rt))))
                ack = json.loads(ws.recv())          # 等 ack，保持请求-应答有序
                for frame in deliver_ack(ack, rt):   # 受理回执随同一次往返送出
                    ws.send(wsproto.dumps(frame))
```

模块头 import 增加 `reconcile`：`from modelctl.core.cluster import config, reconcile, wsproto`。

- [ ] **Step 4: 实现——`server.py` 抽出可测闸门**

把 `main()` 里那段 try/except 整体提为模块级函数（`main()` 改调它），并把启动顺序定为
**先 reconciler 后 Agent**：reconciler 是"有状态可独立运行"的一方（中心宕机也要按最后
已知 goal 维持推理），Agent 是"尽力上报"的一方；顺序反了会出现"Agent 已连上中心但本机
还没有 reconciler 实例"的空窗——那一拍心跳会缺 `goal_sync`，中心于是白搭一次全量 sync。

```python
def start_cluster_background() -> bool:
    """按角色启动集群后台线程（worker/both：reconciler + 中心 Agent）。

    任何导入/启动异常都不得阻断 webui，故整块 try/except 兜底；solo 与纯中心角色
    在一行 `if` 处即返回 False，零副作用。返回是否启动了后台线程（便于测试与自检）。
    """
    try:
        from modelctl.core.cluster import config as cluster_config

        if not cluster_config.is_worker():
            return False
        from modelctl.core.cluster import reconcile as cluster_reconcile

        cluster_reconcile.start_reconciler_in_background()
        from modelctl.core.cluster import agent as cluster_agent

        cluster_agent.start_agent_in_background()
        return True
    except Exception as exc:  # noqa: BLE001 — 集群能力缺失/异常只降级，不影响管理面
        from loguru import logger

        logger.warning(f"cluster 后台线程启动失败（Web UI 照常运行）: {exc}")
        return False
```

`main()` 内原位置替换为：

```python
    start_cluster_background()
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/test_cluster_agent_v2.py tests/test_cluster_agent.py -q`
Expected: PASS（新增 16 条用例：纯函数 7 + 假中心端到端 4 + 闸门参数化 4 + 幂等 1；M0 既有 3 条必须仍绿——`test_collect_heartbeat_shape` 与端到端用例是本任务的向后兼容回归网）

Run: `uv run pytest tests/ -q -k "cluster"`
Expected: PASS（Task 1–10）

- [ ] **Step 6: 提交**

```bash
git add src/modelctl/core/cluster/agent.py src/modelctl/core/webui/server.py tests/test_cluster_agent_v2.py
git commit -m "feat(cluster): Agent 接线 reconciler（ack 投递 + 心跳扩展段 + result 回传）"
```

### Task 11: `admin_cluster.py` v2——goal REST + 强制同步 + WS 世代表/result 落账

把前面 10 个任务的能力暴露成控制面：goal CRUD（过 gate）、失败重试、强制全量 sync、远程启停、全量导出；同时把 WS 循环补完——接入 Task 6 的世代表（同 node_id 后来者胜）并落账 worker 回传的 `result` 帧。

三条贯穿本任务的原则：

1. **端点是薄层，判定全在 core**。REST 只做「消毒入参 → 调 `GoalService`/`NodeRegistry` → 消毒出参」。gate、revision、白名单、状态机全在 core 层，CLI（Task 12）走同一批端点，两套入口不可能出现行为分叉。
2. **投递交给心跳，端点绝不直连 worker**。POST 类端点只写台账/入队/打标记，返回「已受理」。任何"返回时已生效"的暗示都是谎报——worker 最长 `CLUSTER_HEARTBEAT_INTERVAL_S` 后才动作。retry 尤其如此：中心**不**乐观改写 stage，否则节点离线时 dashboard 会显示一个假的 `PENDING_PROFILE_SYNC`。
3. **不新增反向通道**。spec §6.5 里的 `GET /nodes/{id}/log` 与 `GET /cluster/audit` 需要中心主动拉 worker，与本里程碑「worker 只出站」的既定立场冲突（§8.3、计划头部偏离说明），故本任务不做，留给 M2 的 `audit.query` 反向请求帧。

**Files:**
- Modify: `src/modelctl/core/webui/admin_cluster.py`（新增 8 个端点 + `_fmt_ts`/`_goal_views`/`_goals` 辅助 + WS 循环世代表与 `result` 分支）
- Modify: `src/modelctl/core/cluster/nodes.py`（`mark_force_sync` / `_consume_force_sync` + sync 捎带条件）
- Modify: `src/modelctl/core/cluster/wsproto.py`（`parse_result`）
- Modify: `tests/test_cluster_wsproto_v2.py`（追加 3 条 `parse_result` 用例）
- Test: `tests/test_cluster_goals_http.py`

**Interfaces:**
- Consumes: `NodeRegistry`（T7：`handle_heartbeat` 返回 ack dict、`push_action`、`store`）、`GoalService`（T5：`set_goals`/`remove_goals`/`snapshot_for`）、`gate.NodeVerdict`（T4）、`conns.ConnectionRegistry`（T6）、`wsproto.parse_heartbeat_v2` / `make_error` / `dumps`（T2）
- Produces（REST，全部 `/admin/api` 前缀、过 `require_auth`、非中心角色 404）：
  - `GET  /cluster/goals?node_id=&profile=` → `{"goals": [GoalView]}`
  - `POST /cluster/goals`（`_GoalCreateBody`）→ `{"created","skipped","errors","report","reason","goals"}`；仅 `reason` 非空（入参非法/profile 源不可用）→ 400
  - `PUT  /cluster/goals/{goal_id}`（`_GoalUpdateBody`，`exclude_unset` 语义）→ `{"goal": GoalView}`
  - `DELETE /cluster/goals/{goal_id}` → `{"removed": [...], "missing": [...]}`
  - `POST /cluster/goals/{goal_id}/retry` → `{"queued": bool}`
  - `POST /cluster/nodes/{node_id}/sync` → `{"queued": true}`（下一枚 ack 无条件带 `sync.force=true`）
  - `POST /cluster/nodes/{node_id}/model/{profile}/{verb}`，`verb ∈ start|stop|restart` → `{"queued": bool}`
  - `GET  /cluster/nodes/{node_id}` → `{"node", "goals", "model_states"}`
  - `GET  /cluster/export` → `{"exported_at", "role", "nodes", "goals", "model_states"}`
- Produces（`NodeRegistry` 扩展）：`mark_force_sync(node_id) -> None`、私有 `_consume_force_sync(node_id) -> bool`、`__init__` 增 `self._force_sync: set[str]`
- Produces（wsproto）：`parse_result(data) -> {"seq": int, "ok": bool, "detail": str}`
- Produces（模块级）：`admin_cluster._CONNS: ConnectionRegistry`（测试直接换实例复位）

`GoalView`（spec §7.5 的 goal 视图表，REST 是唯一投影点，CLI/dashboard 共用）：
`{"goal_id","node_id","profile","engine","intent","stage","reason","error_class","target_role","profile_version","state","gpu","port","age_s","created_at","updated_at"}`
——`created_at/updated_at` 在此格式化为 `YYYY-MM-DD HH:mm:ss`（项目规范），`age_s` 保留数值给排序，**不含 `profile_yaml`**（列表载荷不背整份 YAML，导出才给）。

- [ ] **Step 1: 写失败测试** — 创建 `tests/test_cluster_goals_http.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_goals_http.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : goal REST（创建/更新/删除/retry/强制同步/远程启停/导出）+ WS 世代表与 result 落账
# ===============================================================================
"""admin_cluster v2：goal 控制面端点与 WS 世代仲裁。

端点断言只盯两件事：① 台账/快照的真实变化（不信任响应体自述）；
② ack 里真正出现了应有的 sync/action（递交确实发生）。
"""
from __future__ import annotations

import re
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("websockets")
from fastapi.testclient import TestClient  # noqa: E402

from modelctl.core.cluster import conns  # noqa: E402
from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_12345"
YAML = "port: 8101\napi_key: ${API_KEY}\nengine_config:\n  tensor_parallel_size: 2\n"
GOALS = "/admin/api/cluster/goals"


def _h():
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture()
def center(monkeypatch, tmp_path):
    """中心角色 + 在线节点 w-1（4 卡 / vllm 可用 / 已有 qwen profile 文件）。"""
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CLUSTER_ROLE", "both")
    models = tmp_path / "models"
    (models / "vllm").mkdir(parents=True)
    (models / "vllm" / "qwen.yaml").write_text(YAML, encoding="utf-8")
    monkeypatch.setattr("modelctl.core.cluster.goals.MODELS_DIR", models)

    import modelctl.core.webui.admin_cluster as ac

    ac._REGISTRY = None
    ac._CONNS = conns.ConnectionRegistry()
    app = create_app(admin=True)
    with TestClient(app) as c:
        reg = ac.get_registry()
        reg.store.upsert_node(node_id="w-1", node_token="NT-1", lan_id="lan-1", role="worker",
                              host_ip="", hostname="", engines=None, now=time.time())
        reg.store.upsert_node(node_id="w-2", node_token="NT-2", lan_id="lan-2", role="worker",
                              host_ip="", hostname="", engines=None, now=time.time())
        reg.store.update_node_capacity(
            "w-1", capacity={"gpu_count": 4, "vram_total_mb": 157280},
            runtimes={"vllm": {"ok": True}}, local_profiles=["qwen"], now=time.time())
        reg.store.update_node_capacity(
            "w-2", capacity={"gpu_count": 4, "vram_total_mb": 157280},
            runtimes={"vllm": {"ok": True}}, local_profiles=[], now=time.time())
        yield c
    ac._REGISTRY = None
    ac._CONNS = conns.ConnectionRegistry()


def _create(client, **over):
    body = {"profile": "qwen", "node_ids": ["w-1"], "create": True}
    body.update(over)
    return client.post(GOALS, json=body, headers=_h())


def _heartbeat(ws, **payload):
    body = {"profiles": {}}
    body.update(payload)      # 不用 dict({"profiles": {}}, **payload)：键冲突时行为不明确
    ws.send_json({"t": "heartbeat", "payload": body})
    return ws.receive_json()


# ---------------- 列表 / 创建 ----------------
def test_goals_list_empty(center) -> None:
    assert center.get(GOALS, headers=_h()).json()["goals"] == []


def test_create_goal_reports_gate_and_writes_ledger(center) -> None:
    r = _create(center)
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 1 and body["reason"] == ""
    assert "w-1" in body["report"]
    assert [g["goal_id"] for g in center.get(GOALS, headers=_h()).json()["goals"]] == ["qwen@@w-1"]


def test_create_goal_missing_profile_422(center) -> None:
    assert center.post(GOALS, json={"node_ids": ["w-1"]}, headers=_h()).status_code == 422


def test_create_goal_unknown_node_400(center) -> None:
    r = _create(center, node_ids=["ghost"])
    assert r.status_code == 400 and "节点" in r.json()["detail"]


def test_create_goal_bad_intent_400(center) -> None:
    assert _create(center, intent="reboot").status_code == 400


def test_create_goal_duplicate_gpus_400(center) -> None:
    """重复卡位是典型误操作：`0,0` 会被 gate 当成 1 卡需求，静默接受比报错更危险。"""
    assert _create(center, gpus="0,0").status_code == 400


def test_create_goal_gpus_land_in_params_and_placement(center) -> None:
    import modelctl.core.webui.admin_cluster as ac

    _create(center, gpus="2,3")
    goal = ac.get_registry().store.get_goal("qwen@@w-1")
    assert goal["params"]["gpu_list"] == [2, 3] and goal["placement"]["gpu_count"] == 2


def test_create_goal_bad_target_role_400(center) -> None:
    assert _create(center, target_role="standby").status_code == 400


def test_create_goal_secret_overlay_400(center) -> None:
    r = _create(center, env_overlay={"API_KEY": "sk-leak"})
    assert r.status_code == 400 and "凭据" in r.json()["detail"]


def test_create_goal_dry_run_writes_nothing(center) -> None:
    body = _create(center, dry_run=True).json()
    assert body["created"] == 1 and "[dry-run]" in body["report"]
    assert center.get(GOALS, headers=_h()).json()["goals"] == []


def test_create_goal_all_nodes_creates_every_candidate(center) -> None:
    """create=True 时 w-2 本机没有 qwen 文件也算 ok（快照下发后由 worker 写盘）。"""
    body = _create(center, node_ids=None, all_nodes=True, create=True).json()
    assert body["created"] == 2
    assert len(center.get(GOALS, headers=_h()).json()["goals"]) == 2


def test_create_goal_all_nodes_without_create_skips_missing_profile(center) -> None:
    """未加 create：w-1 已有文件 → ok；w-2 无文件 → 逐节点 skip（不是整体失败）。"""
    body = _create(center, node_ids=None, all_nodes=True, create=False).json()
    assert body["created"] == 1 and body["skipped"] == 1
    assert [g["node_id"] for g in center.get(GOALS, headers=_h()).json()["goals"]] == ["w-1"]


def test_create_goal_offline_node_is_skipped_not_created(center) -> None:
    import modelctl.core.webui.admin_cluster as ac

    ac.get_registry().store.set_node_status("w-1", "offline")
    body = _create(center).json()
    assert body["created"] == 0 and body["skipped"] == 1


def test_list_goals_filters_by_node_and_profile(center) -> None:
    _create(center, node_ids=None, all_nodes=True)
    assert len(center.get(f"{GOALS}?node_id=w-1", headers=_h()).json()["goals"]) == 1
    assert center.get(f"{GOALS}?profile=nope", headers=_h()).json()["goals"] == []


# ---------------- 视图形状 ----------------
def test_goal_view_exposes_stage_and_runtime_facts(center) -> None:
    _create(center)
    jt = _jt(center)
    with center.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 2, "node_id": "w-1", "lan": "lan-1",
                      "key": jt, "meta": {}})
        assert ws.receive_json()["t"] == "welcome"
        _heartbeat(ws, profiles={"qwen": {"stage": "READY", "state": "READY",
                                          "port": 8101, "gpu": [0, 1], "pid": 4321}})
    # WS 关闭后再查台账：回流已落库，视图不依赖长连接存活
    goal = center.get(GOALS, headers=_h()).json()["goals"][0]
    assert goal["stage"] == "READY" and goal["port"] == 8101 and goal["gpu"] == [0, 1]
    assert "profile_yaml" not in goal


def test_goal_view_times_are_project_format(center) -> None:
    _create(center)
    goal = center.get(GOALS, headers=_h()).json()["goals"][0]
    pattern = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
    assert re.match(pattern, goal["created_at"]) and re.match(pattern, goal["updated_at"])
    assert goal["age_s"] >= 0


# ---------------- 更新 ----------------
def test_update_goal_params_bumps_snapshot_revision(center) -> None:
    import modelctl.core.webui.admin_cluster as ac

    _create(center)
    before = ac.get_registry().goals.snapshot_for("w-1")["revision"]
    r = center.put(f"{GOALS}/qwen@@w-1", json={"params": {"gpu_list": [2, 3]}}, headers=_h())
    assert r.status_code == 200 and r.json()["goal"]["goal_id"] == "qwen@@w-1"
    assert ac.get_registry().goals.snapshot_for("w-1")["revision"] != before


def test_update_goal_intent_persists(center) -> None:
    _create(center)
    center.put(f"{GOALS}/qwen@@w-1", json={"intent": "stop"}, headers=_h())
    assert center.get(GOALS, headers=_h()).json()["goals"][0]["intent"] == "stop"


def test_update_goal_empty_body_400(center) -> None:
    _create(center)
    assert center.put(f"{GOALS}/qwen@@w-1", json={}, headers=_h()).status_code == 400


def test_update_goal_bad_env_overlay_400(center) -> None:
    _create(center)
    r = center.put(f"{GOALS}/qwen@@w-1", json={"env_overlay": {"NOPPE": "1"}}, headers=_h())
    assert r.status_code == 400


def test_update_goal_missing_404(center) -> None:
    r = center.put(f"{GOALS}/ghost@@w-1", json={"intent": "stop"}, headers=_h())
    assert r.status_code == 404


# ---------------- 删除 ----------------
def test_delete_goal_disappears_from_snapshot(center) -> None:
    import modelctl.core.webui.admin_cluster as ac

    _create(center)
    r = center.delete(f"{GOALS}/qwen@@w-1", headers=_h())
    assert r.status_code == 200 and r.json()["removed"] == ["qwen@@w-1"]
    assert ac.get_registry().goals.snapshot_for("w-1")["goals"] == []


def test_delete_goal_missing_404(center) -> None:
    assert center.delete(f"{GOALS}/qwen@@ghost", headers=_h()).status_code == 404


# ---------------- retry / 强制同步 / 远程启停 ----------------
def test_retry_goal_queues_action_for_next_ack(center) -> None:
    _create(center)
    rev = center_revision(center, "w-1")      # 先取 revision：WS 上下文内不再发 HTTP 请求
    r = center.post(f"{GOALS}/qwen@@w-1/retry", headers=_h())
    assert r.status_code == 200 and r.json()["queued"] is True
    jt = _jt(center)
    with center.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 2, "node_id": "w-1", "lan": "", "key": jt,
                      "meta": {}})
        ws.receive_json()
        ack = _heartbeat(ws, goal_sync={"revision": rev})
    assert ack["actions"][0]["action"] == "retry"
    assert ack["actions"][0]["goal_id"] == "qwen@@w-1"


def test_retry_goal_keeps_reported_stage_untouched(center) -> None:
    """中心绝不乐观改写 stage：节点离线时假 PENDING 比真 FAILED 更有害。"""
    import modelctl.core.webui.admin_cluster as ac

    _create(center)
    ac.get_registry().goals.mark_stage("qwen@@w-1", "FAILED", error_class="oom", now=time.time())
    center.post(f"{GOALS}/qwen@@w-1/retry", headers=_h())
    assert ac.get_registry().store.get_goal("qwen@@w-1")["stage"] == "FAILED"


def test_retry_unknown_goal_404(center) -> None:
    assert center.post(f"{GOALS}/ghost@@w-1/retry", headers=_h()).status_code == 404


def test_force_sync_makes_ack_carry_snapshot(center) -> None:
    _create(center)
    rev = center_revision(center, "w-1")
    jt = _jt(center)
    assert center.post("/admin/api/cluster/nodes/w-1/sync", headers=_h()).status_code == 200
    with center.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 2, "node_id": "w-1", "lan": "", "key": jt,
                      "meta": {}})
        ws.receive_json()
        ack = _heartbeat(ws, goal_sync={"revision": rev})   # revision 相同也必须带
    assert ack["sync"]["force"] is True and ack["sync"]["revision"] == rev


def test_force_sync_is_one_shot(center) -> None:
    """一次性：第二拍 revision 已一致就恢复不带快照，否则每 10s 白传全量 YAML。"""
    _create(center)
    jt = _jt(center)
    center.post("/admin/api/cluster/nodes/w-1/sync", headers=_h())
    with center.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 2, "node_id": "w-1", "lan": "", "key": jt,
                      "meta": {}})
        ws.receive_json()
        rev = _heartbeat(ws, goal_sync={"revision": ""})["sync"]["revision"]
        assert "sync" not in _heartbeat(ws, goal_sync={"revision": rev})


def test_force_sync_unknown_node_404(center) -> None:
    r = center.post("/admin/api/cluster/nodes/ghost/sync", headers=_h())
    assert r.status_code == 404


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_model_verb_queues_action(center, verb) -> None:
    _create(center)
    r = center.post("/admin/api/cluster/nodes/w-1/model/qwen/stop", headers=_h())
    assert r.status_code == 200 and r.json()["queued"] is True


def test_model_verb_unknown_verb_400(center) -> None:
    _create(center)
    r = center.post("/admin/api/cluster/nodes/w-1/model/qwen/purge", headers=_h())
    assert r.status_code == 400


def test_model_verb_requires_existing_goal_404(center) -> None:
    """未托管的 profile 不能远程操作：worker 侧 handle_actions 同样会拒绝。"""
    r = center.post("/admin/api/cluster/nodes/w-1/model/ghost/stop", headers=_h())
    assert r.status_code == 404


# ---------------- 单节点详情 / 导出 / 闸门 ----------------
def test_node_detail_groups_goals_and_states(center) -> None:
    _create(center)
    r = center.get("/admin/api/cluster/nodes/w-1", headers=_h())
    body = r.json()
    assert body["node"]["node_id"] == "w-1"
    assert [g["goal_id"] for g in body["goals"]] == ["qwen@@w-1"]


def test_node_detail_missing_404(center) -> None:
    assert center.get("/admin/api/cluster/nodes/ghost", headers=_h()).status_code == 404


def test_export_covers_nodes_goals_states_without_tokens(center) -> None:
    _create(center)
    body = center.get("/admin/api/cluster/export", headers=_h()).json()
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", body["exported_at"])
    assert len(body["nodes"]) == 2 and len(body["goals"]) == 1
    assert body["goals"][0]["profile_yaml"]            # 导出是 restore 素材，必须含原文
    assert all("node_token" not in n for n in body["nodes"])


def test_solo_role_returns_404_on_goal_endpoints(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CLUSTER_ROLE", "solo")
    import modelctl.core.webui.admin_cluster as ac

    ac._REGISTRY = None
    app = create_app(admin=True)
    with TestClient(app) as c:
        assert c.get(GOALS, headers=_h()).status_code == 404
        assert c.post(GOALS, json={"profile": "qwen", "node_ids": ["w-1"]}, headers=_h()).status_code == 404
        assert c.post("/admin/api/cluster/nodes/w-1/sync", headers=_h()).status_code == 404
    ac._REGISTRY = None


# ---------------- WS：result 落账 + 世代表 ----------------
def test_ws_result_frame_recorded_as_event(center) -> None:
    import modelctl.core.webui.admin_cluster as ac

    jt = _jt(center)
    with center.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 2, "node_id": "w-1", "lan": "", "key": jt,
                      "meta": {}})
        ws.receive_json()
        ws.send_json({"t": "result", "seq": 3, "ok": False, "detail": "本机没有目标 x@@w-1"})
        assert ws.receive_json()["t"] == "ack"
    events = ac.get_registry().store.recent_events(limit=10, node_id="w-1")
    result = [e for e in events if e["kind"] == "action.result"][0]
    assert result["payload"] == {"seq": 3, "ok": False, "detail": "本机没有目标 x@@w-1"}


def test_second_connection_for_same_node_supersedes_first(center) -> None:
    """同 node_id 后来者胜：旧连接下一次发送即被踢，避免 action 双投/投给僵尸。"""
    jt = _jt(center)

    def _hello(ws) -> None:
        ws.send_json({"t": "hello", "v": 2, "node_id": "w-1", "lan": "", "key": jt, "meta": {}})
        assert ws.receive_json()["t"] == "welcome"

    from starlette.websockets import WebSocketDisconnect

    with center.websocket_connect("/admin/api/ws/cluster") as old:
        _hello(old)
        with center.websocket_connect("/admin/api/ws/cluster") as new:
            _hello(new)
            old.send_json({"t": "heartbeat", "payload": {"profiles": {}}})
            assert old.receive_json()["t"] == "error"
            with pytest.raises(WebSocketDisconnect):
                old.receive_json()
            # 新连接不受旧连接退场影响（release 只摘自己的 epoch）
            assert _heartbeat(new, profiles={})["t"] == "ack"


def _jt(client) -> str:
    return client.post("/admin/api/cluster/join-tokens/rotate", headers=_h()).json()["join_token"]


def center_revision(client, node_id: str) -> str:
    import modelctl.core.webui.admin_cluster as ac

    return ac.get_registry().goals.snapshot_for(node_id)["revision"]
```

- [ ] **Step 2: 追加 wsproto 消毒用例** — 在 `tests/test_cluster_wsproto_v2.py` 末尾追加

```python
def test_parse_result_sanitizes_shape():
    assert wsproto.parse_result({"t": "result", "seq": 4, "ok": True, "detail": "已受理"}) == {
        "seq": 4, "ok": True, "detail": "已受理"}


def test_parse_result_ok_missing_is_unknown_not_false():
    """缺 ok 字段 → None（未知），不得折叠成 False：台账假报"指令失败"会误导排障。"""
    assert wsproto.parse_result({"t": "result", "seq": 1})["ok"] is None
    assert wsproto.parse_result({"t": "result", "seq": 1, "ok": "yes"})["ok"] is None


def test_parse_result_rejects_non_dict_and_bool_seq():
    assert wsproto.parse_result(None) == {"seq": 0, "ok": None, "detail": ""}
    assert wsproto.parse_result({"seq": True, "ok": True, "detail": 5}) == {
        "seq": 0, "ok": True, "detail": ""}
```

- [ ] **Step 2b: 运行确认失败**

Run: `uv run pytest tests/test_cluster_goals_http.py tests/test_cluster_wsproto_v2.py -q`
Expected: FAIL —— 两处必须同时出现：`ImportError: cannot import name 'conns'`（测试夹具引用 Task 6 模块，若 Task 6 已落地则改为 `AttributeError: module 'modelctl.core.webui.admin_cluster' has no attribute '_CONNS'`）与 `AttributeError: module 'modelctl.core.cluster.wsproto' has no attribute 'parse_result'`。REST 用例此刻应全部报 404/405（路由尚未注册），而非 500——若看到 500，先排查夹具本身（`MODELS_DIR` patch 或 `ac._REGISTRY` 未复位）。

- [ ] **Step 3: 实现——协议消毒 + 中心强制同步标记**

### 3a. `src/modelctl/core/cluster/wsproto.py`（接在 `parse_ack` 之后追加）

```python
def parse_result(data: Any) -> dict[str, Any]:
    """result 帧消毒（中心侧使用；`ok` 三态：True/False/None=未知）。

    `ok` 与 seq/action 不同：**不能**把"字段缺失/类型不对"折叠成 False。中心拿它
    落 `action.result` 事件供排障，假报一次"指令失败"足以让运维对着一台好机器
    查半天；None 才诚实地表达"worker 回了但没告诉我们结果"。
    """
    if not isinstance(data, dict):
        return {"seq": 0, "ok": None, "detail": ""}
    ok = data.get("ok")
    detail = data.get("detail")
    return {"seq": _safe_seq(data), "ok": ok if isinstance(ok, bool) else None,
            "detail": str(detail)[:500] if isinstance(detail, str) else ""}
```

### 3b. `src/modelctl/core/cluster/nodes.py`

`__init__` 内在 `self._seq_base` 之后追加一行（连同注释）：

```python
        # 待处理的"强制全量 sync"标记（REST 打标 → 下一枚 ack 消费掉）
        self._force_sync: set[str] = set()
```

`handle_heartbeat` 里 sync 捎带段改为（**唯一改动是 `forced` 这一路**，其余保持 Task 7）：

```python
            forced = self._consume_force_sync(node_id)
            if snapshot["revision"] != reported or forced:
                ack["sync"] = dict(snapshot, force=True) if forced else snapshot
                self.store.set_node_last_goal_sync_sha(node_id, snapshot["revision"])
```

（注意：`forced` 为真时即便 revision 与 worker 上报值一致也必须带快照——这正是"强制"的全部含义：worker 本地文件被改坏而 revision 未变，短路会让漂移永不自愈。）

文件尾部（`list_node_views` 之后）追加两个方法：

```python
    # ---- 强制全量 sync（REST 打标，心跳消费）----
    def mark_force_sync(self, node_id: str) -> None:
        """打标"下次心跳无条件带全量快照"。set 而非 dict[bool]：幂等天然成立。

        刻意不落 SQLite：强制 sync 是"此刻这次运维动作"，中心重启即作废是可接受的
        ——重启后 worker 重连时 revision 若已一致，说明确实没有内容要补发。
        节点若一直离线，标记会留存到它下次上线，属期望行为（那正是最想修盘的时刻）。
        """
        self._force_sync.add(node_id)

    def _consume_force_sync(self, node_id: str) -> bool:
        """取走并清除标记：**一次性**。否则该节点每 10s 收一份全量 YAML 直到永远。"""
        if node_id not in self._force_sync:
            return False
        self._force_sync.discard(node_id)
        return True
```

### 3c. `src/modelctl/core/cluster/goals.py`（`remove_goals` 之后追加）

REST 的 PUT 必须经 GoalService 而不是直穿 `store.update_goal`：`env_overlay` 白名单与 `intent`
枚举是 core 的不变式，放进端点就等于"绕开 CLI 就能下发密钥"。

```python
    _UPDATABLE: tuple[str, ...] = ("intent", "params", "env_overlay", "placement",
                                   "runtime_ref", "target_role", "profile_version")

    def update_goal(self, goal_id: str, *, fields: dict[str, Any],
                    created_by: str = "", now: float | None = None) -> tuple[dict | None, str]:
        """按需更新单个 goal 的可变字段。返回 (最新 goal 行或 None, 错误文案)。

        `fields` 由调用方以 `model_dump(exclude_unset=True)` 产出——**未提供的键不
        动**（Pydantic 默认值不是用户意图，混进来会把未填字段刷成默认值）。空 fields
        视为调用方错误：没有任何字段要改却刷新 updated_at，会让"最后修改时间"说谎。
        目标不存在 → (None, "")，由调用方转 404。

        校验与 `set_goals` 同源（intent/target_role 枚举、env_overlay 白名单）：PUT 是
        已存在 goal 的唯一改参通道，此处放松等于"下发时拦住、改参时放行"。
        """
        now = time.time() if now is None else now
        goal = self.store.get_goal(goal_id)
        if goal is None:
            return None, ""
        clean = {k: v for k, v in fields.items() if k in self._UPDATABLE}
        if not clean:
            return None, f"没有可更新字段（可更新：{list(self._UPDATABLE)}）"
        if "intent" in clean:
            if clean["intent"] not in VALID_INTENTS:
                return None, f"非法 intent {clean['intent']!r}（仅 {VALID_INTENTS}）"
            clean["intent"] = str(clean["intent"])
        if "target_role" in clean and clean["target_role"] not in VALID_TARGET_ROLES:
            return None, f"非法 target_role {clean['target_role']!r}（仅 {VALID_TARGET_ROLES}）"
        if "env_overlay" in clean:
            overlay, err = validate_env_overlay(clean["env_overlay"])
            if err:
                return None, err
            clean["env_overlay"] = overlay          # None = 明确清空覆盖项
        for key in ("params", "placement"):
            if key in clean and not isinstance(clean[key], dict):
                return None, f"{key} 必须是映射"
        if "profile_version" in fields:
            clean["profile_version"] = str(fields["profile_version"])
        updated = self.store.update_goal(goal_id, now=now, **clean)
        if updated is None:
            return None, ""
        self.store.append_event("goal.update", node_id=str(goal["node_id"]), goal_id=goal_id,
                                payload={"profile": str(goal["profile"]),
                                         "fields": sorted(clean), "operator": created_by}, now=now)
        return updated, ""
```

- [ ] **Step 4: 实现——`src/modelctl/core/webui/admin_cluster.py` 薄层**

顶部 import 段改为（新增 `datetime`、`conns`、`goals` 相关与 `pad` 无关）：

```python
import datetime as _dt
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field

from modelctl.core.cluster import config, conns, tokens, wsproto
from modelctl.core.cluster.goals import GoalService, goal_id_of
from modelctl.core.cluster.nodes import AuthError, NodeRegistry
from modelctl.core.cluster.store import ClusterStore
from modelctl.core.gpu_utils import GPUValidationError, parse_gpu_list
from modelctl.core.webui.admin_auth import require_auth

router = APIRouter()

_REGISTRY: NodeRegistry | None = None
#: WS 世代表（同 node_id 后来者胜）。测试直接赋新实例复位，不经 get_registry()。
_CONNS = conns.ConnectionRegistry()

_SWEEP_INTERVAL_S = 10.0
_last_sweep = 0.0

#: 远程启停允许的动词；`retry` 只在 goal 端点暴露（语义是"重置失败状态"，不是起停）
_MODEL_VERBS: frozenset[str] = frozenset({"start", "stop", "restart"})
```

`get_registry()` 按 Task 7 Step 4 已改为注入 `GoalService`，本任务再补一个便捷属性（保持单例语义）：

```python
def get_registry() -> NodeRegistry:
    """NodeRegistry 进程内单例（懒建库）。测试经 admin_cluster._REGISTRY=None 重置。"""
    global _REGISTRY
    if _REGISTRY is None:
        store = ClusterStore()
        store.init_db()
        _REGISTRY = NodeRegistry(store, goals=GoalService(store))
    return _REGISTRY
```

在 `_sweep_if_due()` 之后插入投影/消毒辅助：

```python
def _goals() -> GoalService:
    """goal 唯一写入口（与 WS 共用同一 NodeRegistry.store，台账才一致）。"""
    return get_registry().goals


def _fmt_ts(value: Any) -> str:
    """epoch float → 项目规范时间串；缺值/非法返回空串（不显示 None）。"""
    if not isinstance(value, (int, float)):
        return ""
    try:
        return _dt.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return ""


def _node_id_of_goal(goal_id: str) -> str:
    """goal_id = `<profile>@@<node_id>`：node_id 取末段（profile 理论上可含 @@）。"""
    return goal_id.rsplit("@@", 1)[-1] if "@@" in goal_id else ""


def _goal_view(goal: dict[str, Any], state: dict[str, Any] | None, *,
               with_yaml: bool = False, now: float | None = None) -> dict[str, Any]:
    """goal 视图（spec §7.5 表格行）：声明侧字段 + model_states 的运行事实。

    连接键是 `(node_id, profile)`：worker 上报的是 profile 名（与它磁盘上的文件
    名同源），goal_id 对 worker 是透明串，两侧只在中心台账里join。
    刻意不含 profile_yaml——列表载荷不背整份 YAML，导出走 `with_yaml=True`。
    """
    now = time.time() if now is None else now
    created = goal.get("created_at")
    updated = goal.get("updated_at")
    view: dict[str, Any] = {
        "goal_id": goal["goal_id"], "node_id": goal["node_id"], "profile": goal["profile"],
        "engine": goal["engine"], "intent": goal["intent"], "stage": goal["stage"],
        "reason": goal.get("stage_reason") or "", "error_class": goal.get("error_class") or "",
        "target_role": goal.get("target_role") or "", "profile_version": goal.get("profile_version") or "",
        "state": (state or {}).get("state") or "", "gpu": (state or {}).get("gpu"),
        "port": (state or {}).get("port"), "pid": (state or {}).get("pid"),
        "created_at": _fmt_ts(created), "updated_at": _fmt_ts(updated),
        "age_s": round(now - created, 1) if isinstance(created, (int, float)) else None,
    }
    if with_yaml:
        view["profile_yaml"] = goal.get("profile_yaml") or ""
    return view


def _goal_views(rows: list[dict[str, Any]], *, now: float) -> list[dict[str, Any]]:
    states = {(s["node_id"], s["profile"]): s for s in get_registry().store.list_model_states()}
    return [_goal_view(g, states.get((g["node_id"], g["profile"])), now=now) for g in rows]
```

> `_goal_view` 里 `pid` 供 CLI/后续 dashboard 排障用；spec §7.5 表格里没有该列，属允许的额外字段。

### 4a. goal 端点（追加在 `cluster_events` 之后、`rotate_join_token` 之前）

```python
# ================================ 目标状态（M1，spec §6.5）================================
class _GoalCreateBody(BaseModel):
    profile: str = Field(min_length=1, max_length=64)
    node_ids: list[str] | None = None
    all_nodes: bool = False
    intent: str = "start"
    create: bool = False
    params: dict | None = None
    env_overlay: dict | None = None
    gpus: str = ""
    lan_allow: list[str] | None = None
    runtime_ref: str | None = None
    target_role: str = "primary"
    dry_run: bool = False


class _GoalUpdateBody(BaseModel):
    """PUT 载荷：全部可选 + `exclude_unset`，未提供的字段一律不动。

    刻意**不提供** `profile` / `node_ids`：改目标节点等价于换 goal_id，必须
    "先 create 新 goal 再 remove 旧 goal"，否则历史 stage/事件与 model_states
    会与新节点的运行事实串台。
    """

    intent: str | None = None
    params: dict | None = None
    env_overlay: dict | None = None
    placement: dict | None = None
    runtime_ref: str | None = None
    target_role: str | None = None
    profile_version: str | None = Field(default=None, max_length=64)


def _bad_request(reason: str) -> JSONResponse:
    """core 层业务失败统一 400（与既有端点的 401/404 一样只回固定短文案）。"""
    return JSONResponse(status_code=400, content={"detail": reason})


@router.get("/cluster/goals")
async def list_goals(node_id: str = Query("", max_length=64),
                     profile: str = Query("", max_length=64),
                     _base: None = Depends(require_auth)):
    if (off := _disabled()) is not None:
        return off
    _sweep_if_due()
    rows = get_registry().store.list_goals(node_id=node_id, profile=profile)
    return {"goals": _goal_views(rows, now=time.time())}


@router.post("/cluster/goals")
async def create_goals(body: _GoalCreateBody, _base: None = Depends(require_auth)):
    """批量下发 goal（过 placement gate）。gate 的 skip 是 200 + report，不是 HTTP 错误。

    只有"参数本身非法/无任何候选节点"才 400：逐节点容量不足是运维要逐行读的
    **结果**，不是请求失败——把 20 个节点里 3 个装不下变成 400，CLI 就拿不到报告了。
    """
    if (off := _disabled()) is not None:
        return off
    try:
        gpu_list = parse_gpu_list(body.gpus or "")   # 非法/重复即抛 GPUValidationError
    except GPUValidationError as exc:
        return _bad_request(f"非法 gpus {body.gpus!r}：{exc}")
    result = _goals().set_goals(profile=body.profile, node_ids=body.node_ids,
                                all_nodes=body.all_nodes, intent=body.intent,
                                create=body.create, params=body.params,
                                env_overlay=body.env_overlay, gpu_list=gpu_list,
                                lan_allow=body.lan_allow, runtime_ref=body.runtime_ref,
                                target_role=body.target_role, created_by="api",
                                dry_run=body.dry_run, now=time.time())
    if result["reason"]:
        return _bad_request(result["reason"])
    now = time.time()
    rows = [] if body.dry_run else get_registry().store.list_goals(profile=body.profile)
    return {"created": result["created"], "skipped": result["skipped"],
            "errors": result["errors"], "report": result["report"],
            "goals": _goal_views(rows, now=now)}


@router.put("/cluster/goals/{goal_id}")
async def update_goal(goal_id: str, body: _GoalUpdateBody, _base: None = Depends(require_auth)):
    """更新 goal 的声明式字段；worker 侧由 revision 变化自动重新写盘并重置状态机。"""
    if (off := _disabled()) is not None:
        return off
    goal, reason = _goals().update_goal(goal_id, fields=body.model_dump(exclude_unset=True),
                                        created_by="api", now=time.time())
    if reason:
        return _bad_request(reason)
    if goal is None:
        return JSONResponse(status_code=404, content={"detail": f"目标 {goal_id} 不存在"})
    return {"goal": _goal_view(goal, None, now=time.time())}


@router.delete("/cluster/goals/{goal_id}")
async def delete_goal(goal_id: str, _base: None = Depends(require_auth)):
    """删除 goal。worker 侧删除无需额外指令：goal 从下一份快照消失即剪枝语义（§6.4）。"""
    if (off := _disabled()) is not None:
        return off
    if get_registry().store.get_goal(goal_id) is None:
        return JSONResponse(status_code=404, content={"detail": f"目标 {goal_id} 不存在"})
    out = _goals().remove_goals(profile=goal_id.rsplit("@@", 1)[0],
                                node_ids=[_node_id_of_goal(goal_id)], created_by="api")
    return {"removed": out["removed"], "missing": out["missing"]}


@router.post("/cluster/goals/{goal_id}/retry")
async def retry_goal(goal_id: str, _base: None = Depends(require_auth)):
    """人工重试失败的 goal（spec §8.1：失败是终态，只有 retry 或 goal 变更能走出）。

    只做两件事：入队 retry 指令 + 记事件。**刻意不改 stage**——节点可能已离线，
    中心乐观写成 PENDING_PROFILE_SYNC 会让 dashboard 在故障机器上显示假的重启中。
    真正的状态回落由 worker 下一拍 reconcile 上报。
    """
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    goal = reg.store.get_goal(goal_id)
    if goal is None:
        return JSONResponse(status_code=404, content={"detail": f"目标 {goal_id} 不存在"})
    queued = reg.push_action(str(goal["node_id"]), "retry", goal_id=goal_id)
    reg.store.append_event("goal.retry", node_id=str(goal["node_id"]), goal_id=goal_id,
                           payload={"queued": queued, "operator": "api"}, now=time.time())
    return {"queued": queued}
```

### 4b. 节点/模型/导出端点（追加在 4a 之后）

```python
@router.get("/cluster/nodes/{node_id}")
async def cluster_node_detail(node_id: str, _base: None = Depends(require_auth)):
    """单节点详情：台账视图 + 该节点 goals + model_states（dashboard 详情页数据源）。"""
    if (off := _disabled()) is not None:
        return off
    _sweep_if_due()
    reg = get_registry()
    node = reg.store.get_node(node_id)
    if node is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    now = time.time()
    return {"node": reg.node_view(node, now),
            "goals": _goal_views(reg.store.list_goals(node_id=node_id), now=now),
            "model_states": reg.store.list_model_states(node_id=node_id)}


@router.post("/cluster/nodes/{node_id}/sync")
async def force_node_sync(node_id: str, _base: None = Depends(require_auth)):
    """强制全量 sync：下一枚 ack 无条件带 `sync.force=true`，worker 跳过 revision 短路重写盘。

    离线节点也接受（标记留存到其下次上线）——本地文件被改坏时，人最想立刻修好的
    正是还没连回来的那台。一次性标记见 `NodeRegistry._consume_force_sync`。
    """
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    if reg.store.get_node(node_id) is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    reg.mark_force_sync(node_id)
    reg.store.append_event("node.sync", node_id=node_id, payload={"operator": "api"},
                           now=time.time())
    return {"queued": True}


@router.post("/cluster/nodes/{node_id}/model/{profile}/{verb}")
async def model_verb(node_id: str, profile: str, verb: str,
                     _base: None = Depends(require_auth)):
    """远程启停单个模型。**必须先有 goal**。

    未托管的 profile 一律 404：中心一旦能对 worker 本机自跑的模型下指令，声明式
    边界就破了（运维会看到"我没下发过的模型被中心停了"）。worker 侧
    `Reconciler.handle_actions` 用同一立场兜底（goal 不在本地清单 → ok=False）。
    """
    if (off := _disabled()) is not None:
        return off
    if verb not in _MODEL_VERBS:
        return _bad_request(f"不支持的操作 {verb!r}（仅 {sorted(_MODEL_VERBS)}）")
    reg = get_registry()
    goal = reg.store.get_goal(goal_id_of(profile, node_id))
    if goal is None:
        return JSONResponse(status_code=404,
                            content={"detail": f"节点 {node_id} 上没有 profile {profile} 的托管目标"})
    queued = reg.push_action(node_id, verb, goal_id=str(goal["goal_id"]), profile=profile)
    reg.store.append_event("node.model_action", node_id=node_id, goal_id=str(goal["goal_id"]),
                           payload={"verb": verb, "queued": queued, "operator": "api"},
                           now=time.time())
    return {"queued": queued}


@router.get("/cluster/export")
async def cluster_export(_base: None = Depends(require_auth)):
    """全量 goal + 节点状态导出（备份/迁移素材；`cluster backup` 属 M2，此处只给数据）。"""
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    now = time.time()
    return {"exported_at": _fmt_ts(now), "role": config.cluster_role(),
            "nodes": [reg.node_view(n, now) for n in reg.store.list_nodes()],
            "goals": [_goal_view(g, None, with_yaml=True, now=now)
                      for g in reg.store.list_goals()],
            "model_states": reg.store.list_model_states()}
```

> 路由顺序无冲突：`GET /cluster/nodes`（M0 既有）与 `GET /cluster/nodes/{node_id}` 是两个不同 path，FastAPI 按精确段匹配；`/cluster/nodes/{id}/sync`、`/cluster/nodes/{id}/model/{profile}/{verb}` 段数不同，不会互相吞。

### 4c. WS 循环改造（整段替换 `ws_cluster`）

```python
@router.websocket("/ws/cluster")
async def ws_cluster(ws: WebSocket):
    """worker 通道：hello（token 鉴权）→ welcome → heartbeat/event/result 循环。

    身份绑定：node_id 只存本连接的局部变量，handle_hello 已强制 NT↔node_id 一致，
    故后续帧只能落到已鉴权的那个节点，无法伪造他人身份。
    对端可控输入（raw 帧、mtype）一律不回显原文：错误帧只用固定文案。

    世代表：hello 成功后向 _CONNS 登记 epoch，此后每帧先验 epoch。同 node_id 的旧连接
    （进程重启后遗留的半开连接）若继续活着，中心的 action 会被投给僵尸或被双份执行，
    故后来者胜、旧连接下一帧即退场。旧连接被动感知（不主动踢）是接受的取舍：它下一次
    发送才被踢，而它不发时中心只会用新连接投递，不影响正确性；主动 kick 需给
    ConnectionRegistry 加反向通知，M2 与 `audit.query` 一并做。
    """
    if not config.is_center():
        await ws.close(code=4404)
        return
    await ws.accept()
    reg = get_registry()
    node_id = ""
    epoch = 0
    try:
        hello_raw = await ws.receive_text()
        # parse_type 仅在"可解析且为 dict"时返回非空，故其返回 hello 时下面 loads 必成功
        if wsproto.parse_type(hello_raw) != "hello":
            await ws.send_text(wsproto.dumps(wsproto.make_error("首帧须为 hello")))
            await ws.close(code=4400)
            return
        welcome, node_id = reg.handle_hello(wsproto.parse_hello(json.loads(hello_raw)))
        epoch = _CONNS.join(node_id)
        await ws.send_text(wsproto.dumps(welcome))
        while True:
            raw = await ws.receive_text()
            if not _CONNS.is_current(node_id, epoch):
                await ws.send_text(wsproto.dumps(wsproto.make_error("连接已被同节点新连接取代")))
                await ws.close(code=4409)
                return
            mtype = wsproto.parse_type(raw)
            try:
                data: dict[str, Any] = json.loads(raw)
            except (ValueError, TypeError, RecursionError):
                # RecursionError：深嵌套 JSON 击穿递归上限，与非法 JSON 同等处置
                await ws.send_text(wsproto.dumps(wsproto.make_error("消息解析失败")))
                continue
            if mtype == "heartbeat":
                ack = reg.handle_heartbeat(node_id, wsproto.parse_heartbeat_v2(data), now=time.time())
                _sweep_if_due()
                await ws.send_text(wsproto.dumps(ack))
            elif mtype == "event":
                payload = data.get("payload")
                reg.store.append_event(str(data.get("kind", "")), node_id=node_id,
                                       payload=payload if isinstance(payload, dict) else None)
                await ws.send_text(wsproto.dumps({"t": "ack"}))
            elif mtype == "result":
                # 指令回执只落账不裁决：ok=False 时改不改状态由 worker 的 reconcile 决定，
                # 中心重复动作会与"失败即终态 + 人工 retry"的立场冲突。
                res = wsproto.parse_result(data)
                reg.store.append_event("action.result", node_id=node_id,
                                       payload={"seq": res["seq"], "ok": res["ok"],
                                                "detail": res["detail"]}, now=time.time())
                await ws.send_text(wsproto.dumps({"t": "ack"}))
            else:
                await ws.send_text(wsproto.dumps(wsproto.make_error("未知消息类型")))
    except WebSocketDisconnect:
        return
    except AuthError:
        await ws.send_text(wsproto.dumps(wsproto.make_error("鉴权失败")))
        await ws.close(code=4401)
        return
    finally:
        if node_id:
            _CONNS.release(node_id, epoch)   # 只摘自己的 epoch，绝不误杀新连接
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/test_cluster_goals_http.py tests/test_cluster_wsproto_v2.py -q`
Expected: PASS（`test_cluster_goals_http.py` 38 条定义 / 40 个用例——`test_model_verb_queues_action` 参数化 3 次；`test_cluster_wsproto_v2.py` 新增 3 条）

Run: `uv run pytest tests/ -q -k "cluster"`
Expected: PASS（Task 1–11）。M0 的 `tests/test_cluster_http.py` 必须**一条都不改**仍然全绿——它是本任务的向后兼容回归网（`test_ws_hello_heartbeat_registers_node` 断言 `receive_json()["t"] == "ack"`，新 ack dict 仍满足）。若某条 M0 用例失败，说明薄层改动了既有语义，按新语义修**实现**而不是修测试。

- [ ] **Step 6: 提交**

```bash
git add src/modelctl/core/webui/admin_cluster.py src/modelctl/core/cluster/nodes.py src/modelctl/core/cluster/goals.py src/modelctl/core/cluster/wsproto.py tests/test_cluster_goals_http.py tests/test_cluster_wsproto_v2.py
git commit -m "feat(cluster): goal 控制面 REST + 强制 sync + WS 世代表与指令回执落账"
```

---

<!-- MORE11 -->




