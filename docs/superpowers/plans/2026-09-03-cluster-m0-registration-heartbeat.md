# Cluster M0：节点注册与心跳 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 modelctl 集群管理面的 M0 里程碑：`CLUSTER_ROLE` 角色体系、`cluster init/join/nodes/status/join-token` CLI、SQLite 中心台账（nodes/events）、WebSocket 注册与心跳协议（lease 判活）、中心 REST 查询端点、worker 常驻 Agent、最小"集群节点"Web 视图。

**Architecture:** 单中心（机器 A）+ N 个 worker 复用现有 webui 进程。worker 内 `WorkerAgent` 线程经 WebSocket 主动连中心（`/admin/api/ws/cluster`），`hello`（join_token/node_token 鉴权）→ `welcome`（签发节点专属 token）→ 周期 `heartbeat`（续租）。中心状态落单文件 SQLite（`data/cache/cluster-meta.db`），stale/offline 用 lease 到期判定（K3s/Nomad 借鉴）。`CLUSTER_ROLE=solo`（默认）时全部集群路由 404、无 Agent，现有行为零变化。

**Tech Stack:** Python 3.12（stdlib `sqlite3`/`urllib`/`threading`）、FastAPI WebSocket 路由（gateway venv 内）、`websockets`（新增依赖：uvicorn WS 实现 + worker 客户端）、Vue 3 `<script setup>` + axios（现有 web/ 工程）。

**Spec:** `docs/superpowers/specs/2026-09-03-modelctl-cluster-design.md`（§4 角色、§5 协议、§10 鉴权、§11 schema、§12 M0 范围）

## Global Constraints

- Python >= 3.12；ruff line-length 120、`select = ["E","F","I","B","UP"]`；`uv run mypy src/modelctl` 必须零错误（CI 门禁）。
- **主包 `src/modelctl` 新代码只允许 stdlib + PyYAML + loguru**（fastapi 仅在 `core/webui/admin_cluster.py` 与 `core/gateway.py` 层导入，与现有 webui 模块一致）。
- 新增运行时依赖只有 `websockets`（加 `gateway/pyproject.toml` deps 供生产；加主 `pyproject.toml` dev extra 供测试/mypy）。**不加进主包 `dependencies`**。
- `CLUSTER_ROLE=solo`（或未设置）时：无 WS 路由数据、无 Agent 线程、`/admin/api/cluster/*` 全部 404、现有测试全绿——零行为变化。
- 中心 DB 路径 = `modelctl.core.process.cache_dir() / "cluster-meta.db"`（自动被 conftest autouse fixture 的 `CACHE_DIR` 隔离；`data/` 已在 .gitignore:39）。
- lease 时间戳用 **epoch float（REAL）**存储与比较（非 ISO 字符串——字符串比较在时区切换下不可靠；这是对 spec §11.1 ISO 字段的一处有意偏离）；展示层再格式化。
- 所有新 `.py` 文件带仓库标准文件头（`@File/@IDE/@Author : SunHao/@Email : 2865467769@qq.com/@Date/@Desc`，参照 `src/modelctl/core/envfile.py:1-10`）。
- 测试命令 `uv run pytest tests/<file> -q`；全量门禁 `uv run ruff check src tests; uv run mypy src/modelctl; uv run pytest tests/ -q`（PowerShell 用 `;` 分隔）。
- 测试中凡 `import fastapi`/`websockets` 的新文件一律顶部 `pytest.importorskip(...)`（主 lockfile 不含 fastapi/websockets；CI 干净 `uv sync --extra dev` 下须可跳过而非 collection error）。
- 提交信息风格：`feat(cluster): ...` / `test(cluster): ...` / `build(cluster): ...`（中文，参照 git log）。

## 文件结构（本计划创建/修改）

```
创建
  src/modelctl/core/cluster/__init__.py      包说明（无逻辑）
  src/modelctl/core/cluster/config.py        CLUSTER_* 配置读取（纯 os.environ，仿 envfile 惯例）
  src/modelctl/core/cluster/tokens.py        join_token / node_token 生成与轮换
  src/modelctl/core/cluster/store.py         SQLite 中心台账（全量 schema，M0 只用 nodes/events/meta）
  src/modelctl/core/cluster/wsproto.py       WS 消息协议（dataclass + parse/make，纯函数）
  src/modelctl/core/cluster/nodes.py         中心侧业务：hello 鉴权/注册、heartbeat、lease 扫描、视图
  src/modelctl/core/cluster/center_probe.py  urllib 探活中心（CLI join 用，stdlib）
  src/modelctl/core/cluster/agent.py         WorkerAgent 线程（websockets sync 客户端 + 退避重连）
  src/modelctl/core/webui/admin_cluster.py   中心路由：GET /cluster/*、POST /cluster/hello-check、WS /ws/cluster
  tests/test_cluster_config.py               config 单测
  tests/test_cluster_tokens.py               tokens 单测
  tests/test_cluster_store.py                store 单测
  tests/test_cluster_wsproto.py              协议单测
  tests/test_cluster_nodes.py                中心逻辑单测（含 lease 三态迁移）
  tests/test_cluster_http.py                 admin_cluster REST + WS 端点测试（TestClient）
  tests/test_cluster_agent.py                Agent 真连测试（websockets.sync.server 假中心）
  tests/test_cluster_cli.py                  CLI init/join/nodes/status/join-token 测试
  web/src/api/cluster.ts                     前端 API 模块 + 类型
  web/src/views/ClusterNodesView.vue         集群节点视图（中心角色才有数据；solo 显示未启用）
修改
  src/modelctl/core/envfile.py               + set_env_values()（.env 定点写回，仓库首个写函数）
  src/modelctl/cli.py                        cluster 子命令解析 + main() 分发 + 5 个 handler
  src/modelctl/core/webui/admin_router.py    _SUBROUTER_MODULES += admin_cluster
  src/modelctl/core/webui/server.py          main() 里按角色启动 WorkerAgent
  pyproject.toml                             dev extra += websockets
  gateway/pyproject.toml                     deps += websockets
  .env.example                               CLUSTER_* 注释块
  web/src/router/index.ts                    + /cluster/nodes 路由
  web/src/components/layout/Sidebar.vue      + 菜单项（solo/未启用时隐藏）
```

**M0 范围外（勿做）**：goals 表读写、profile 同步、reconciler、`goal set`、placement gate、metrics_rollups 写入、前端 goals 视图——全部属 M1/M2。store.py 会按 spec §11.1 建全量表（避免 M1 迁移），但 M0 只有 nodes/events/meta 有读写方。

---

### Task 1: `envfile.set_env_values` — .env 定点写回

仓库现有 `envfile.py` 只读（parse_env_file/load_env，`load_env` 用 `setdefault`）。`cluster join` 需把 `CLUSTER_*` 写回 `.env`：已存在的 key 行原地替换，其余行（注释/顺序）保留，缺失的 key 追加到文件尾部。

**Files:**
- Modify: `src/modelctl/core/envfile.py`（文件尾部追加）
- Test: `tests/test_cluster_envwrite.py`

**Interfaces:**
- Produces: `set_env_values(values: dict[str, str], env_path: Path | None = None) -> Path`（返回写入路径；文件不存在时创建；值不加引号）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_cluster_envwrite.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_envwrite.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : envfile.set_env_values .env 定点写回测试
# ===============================================================================
"""set_env_values：已存在 key 原地替换、注释与顺序保留、新 key 追加。"""
from __future__ import annotations

from pathlib import Path

from modelctl.core.envfile import parse_env_file, set_env_values


def test_replace_existing_key_preserves_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# 头注释\nAPI_KEY=abc\n# 中间注释\nWEBUI_PORT=4173\n", encoding="utf-8")
    set_env_values({"API_KEY": "xyz"}, env_path=env)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# 头注释"          # 注释保留
    assert lines[1] == "API_KEY=xyz"       # 原地替换
    assert lines[2] == "# 中间注释"
    assert lines[3] == "WEBUI_PORT=4173"   # 其余行不动


def test_append_new_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    set_env_values({"B": "2", "C": "3"}, env_path=env)
    assert parse_env_file(env) == {"A": "1", "B": "2", "C": "3"}


def test_create_missing_file(tmp_path: Path) -> None:
    env = tmp_path / "sub" / ".env"
    env.parent.mkdir()
    set_env_values({"K": "v"}, env_path=env)
    assert parse_env_file(env) == {"K": "v"}


def test_commented_key_untouched_and_appended(tmp_path: Path) -> None:
    """被注释的旧值（#KEY=old）不算存在：保留注释行，另起一行新值。"""
    env = tmp_path / ".env"
    env.write_text("#CLUSTER_ROLE=solo\n", encoding="utf-8")
    set_env_values({"CLUSTER_ROLE": "worker"}, env_path=env)
    assert parse_env_file(env) == {"CLUSTER_ROLE": "worker"}
    assert "#CLUSTER_ROLE=solo" in env.read_text(encoding="utf-8")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_cluster_envwrite.py -q`
Expected: `ImportError: cannot import name 'set_env_values'`

- [ ] **Step 3: 实现**

在 `src/modelctl/core/envfile.py` 末尾追加：

```python
def set_env_values(values: dict[str, str], env_path: Path | None = None) -> Path:
    """把 values 定点写回 .env：已存在的 key 行原地替换，其余行保留，缺失 key 追加。

    被注释掉的行（#KEY=…）不视为已存在——保留注释、另追加新行。文件不存在时创建。
    值按原样写入（不添加引号）；写后不影响当前进程 os.environ（load_env 的
    setdefault 语义决定下次进程才生效，调用方需同步 setenv 时自行处理）。
    """
    path = env_path or PROJECT_ROOT / ".env"
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(values)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    if remaining:
        if lines and lines[-1] != "":
            lines.append("")
        for key, value in remaining.items():
            lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_cluster_envwrite.py tests/test_envfile.py -q`
Expected: 全 PASS（新 4 条 + 原 envfile 测试不回归）

- [ ] **Step 5: 门禁 + 提交**

```powershell
uv run ruff check src/modelctl/core/envfile.py tests/test_cluster_envwrite.py ; uv run mypy src/modelctl/core/envfile.py
git add src/modelctl/core/envfile.py tests/test_cluster_envwrite.py ; git commit -m "feat(envfile): 新增 set_env_values 定点写回 .env（cluster join 前置能力）"
```

---

### Task 2: `cluster/config.py` — CLUSTER_* 配置读取

**Files:**
- Create: `src/modelctl/core/cluster/__init__.py`、`src/modelctl/core/cluster/config.py`
- Modify: `.env.example`（追加注释块）
- Test: `tests/test_cluster_config.py`

**Interfaces:**
- Produces（`modelctl.core.cluster.config`，全部纯函数读 `os.environ`）:
  - `cluster_role() -> str`（非法值回退 `"solo"`；合法集 `VALID_ROLES = ("solo", "worker", "control-plane", "both")`）
  - `is_center() -> bool`（role ∈ {control-plane, both}）；`is_worker() -> bool`（role ∈ {worker, both}）
  - `center_url() -> str`、`node_id() -> str`、`lan_id() -> str`、`join_token() -> str`、`node_token() -> str`（默认 `""`）
  - `heartbeat_interval_s() -> int`（`CLUSTER_HEARTBEAT_INTERVAL_S`，默认 10，最小 1）
  - `lease_s() -> int`（`CLUSTER_LEASE_S`，默认 90，最小 5）
  - `ws_insecure() -> bool`（`CLUSTER_WS_INSECURE == "1"`）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_cluster_config.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_config.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : cluster.config 集群配置读取测试
# ===============================================================================
"""CLUSTER_* 配置读取：默认值、角色集合、非法值回退。"""
from __future__ import annotations

import pytest

from modelctl.core.cluster import config

CLUSTER_KEYS = [
    "CLUSTER_ROLE", "CLUSTER_CENTER_URL", "CLUSTER_NODE_ID", "CLUSTER_LAN",
    "CLUSTER_JOIN_TOKEN", "CLUSTER_NODE_TOKEN", "CLUSTER_HEARTBEAT_INTERVAL_S",
    "CLUSTER_LEASE_S", "CLUSTER_WS_INSECURE",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in CLUSTER_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_default_role_is_solo() -> None:
    assert config.cluster_role() == "solo"
    assert not config.is_center() and not config.is_worker()


def test_invalid_role_falls_back_to_solo(monkeypatch) -> None:
    monkeypatch.setenv("CLUSTER_ROLE", "BOSS")
    assert config.cluster_role() == "solo"


def test_both_is_center_and_worker(monkeypatch) -> None:
    monkeypatch.setenv("CLUSTER_ROLE", "BOTH")  # 大小写不敏感
    assert config.is_center() and config.is_worker()


def test_worker_only_is_not_center(monkeypatch) -> None:
    monkeypatch.setenv("CLUSTER_ROLE", "worker")
    assert config.is_worker() and not config.is_center()


def test_interval_and_lease_defaults_and_floor(monkeypatch) -> None:
    assert config.heartbeat_interval_s() == 10
    assert config.lease_s() == 90
    monkeypatch.setenv("CLUSTER_HEARTBEAT_INTERVAL_S", "0")   # 低于下限 → 回退
    monkeypatch.setenv("CLUSTER_LEASE_S", "abc")              # 非法 → 回退
    assert config.heartbeat_interval_s() == 10
    assert config.lease_s() == 90


def test_ws_insecure_flag(monkeypatch) -> None:
    assert not config.ws_insecure()
    monkeypatch.setenv("CLUSTER_WS_INSECURE", "1")
    assert config.ws_insecure()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_cluster_config.py -q`
Expected: `ModuleNotFoundError: No module named 'modelctl.core.cluster'`

- [ ] **Step 3: 实现**

`src/modelctl/core/cluster/__init__.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/__init__.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : 分布式集群管理面（设计文档 docs/superpowers/specs/2026-09-03-modelctl-cluster-design.md）
# ===============================================================================

"""core/cluster — 单中心分布式管理面：配置、SQLite 台账、WS 协议、worker Agent。"""
```

`src/modelctl/core/cluster/config.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/config.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : CLUSTER_* 集群配置读取
# ===============================================================================

"""core/cluster/config.py — 集群配置项读取（口径与全库一致：os.environ 就地读取，无集中 settings）。"""

from __future__ import annotations

import os

VALID_ROLES: tuple[str, ...] = ("solo", "worker", "control-plane", "both")

_DEFAULT_INTERVAL_S = 10
_DEFAULT_LEASE_S = 90


def cluster_role() -> str:
    """CLUSTER_ROLE；非法/未设回退 solo（现有部署零影响的关键闸门）。"""
    role = os.environ.get("CLUSTER_ROLE", "solo").strip().lower()
    return role if role in VALID_ROLES else "solo"


def is_center() -> bool:
    return cluster_role() in ("control-plane", "both")


def is_worker() -> bool:
    return cluster_role() in ("worker", "both")


def center_url() -> str:
    return os.environ.get("CLUSTER_CENTER_URL", "").strip()


def node_id() -> str:
    return os.environ.get("CLUSTER_NODE_ID", "").strip()


def lan_id() -> str:
    return os.environ.get("CLUSTER_LAN", "").strip()


def join_token() -> str:
    return os.environ.get("CLUSTER_JOIN_TOKEN", "").strip()


def node_token() -> str:
    return os.environ.get("CLUSTER_NODE_TOKEN", "").strip()


def _int_env(key: str, default: int, floor: int) -> int:
    raw = os.environ.get(key, "")
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= floor else default


def heartbeat_interval_s() -> int:
    return _int_env("CLUSTER_HEARTBEAT_INTERVAL_S", _DEFAULT_INTERVAL_S, floor=1)


def lease_s() -> int:
    return _int_env("CLUSTER_LEASE_S", _DEFAULT_LEASE_S, floor=5)


def ws_insecure() -> bool:
    return os.environ.get("CLUSTER_WS_INSECURE", "").strip() == "1"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_cluster_config.py -q`
Expected: 7 PASS

- [ ] **Step 5: .env.example 追加注释块**

在 `.env.example` 文件尾部追加：

```bash

# ── 集群（分布式管理面，设计文档 docs/superpowers/specs/2026-09-03-modelctl-cluster-design.md）──
# 角色：solo(默认,单机) | worker(仅注册中心) | control-plane(仅中心) | both(中心且自跑模型)
# CLUSTER_ROLE=solo
# 中心地址（worker join 后写入；scheme 用 http，WS 自动推导 ws）
# CLUSTER_CENTER_URL=http://192.168.77.210:4173
# 本节点在集群内的唯一标识（modelctl cluster join 写入）
# CLUSTER_NODE_ID=w-210
# 所属局域网标签（仅展示/分组用）
# CLUSTER_LAN=lan-2
# 心跳间隔秒 / 租约秒（中心按 lease 过期判 stale，last_seen 超 3×lease 判 offline）
# CLUSTER_HEARTBEAT_INTERVAL_S=10
# CLUSTER_LEASE_S=90
# 裸 ws:// 传输（仅内网调试用；默认 ws(s) 跟随 center_url scheme）
# CLUSTER_WS_INSECURE=0
# 集群准入令牌（中心 modelctl cluster init 生成，cluster join 时消费）
# CLUSTER_JOIN_TOKEN=
# 节点专属令牌（join 成功后由中心签发并自动写回本文件；重连用它，可单独吊销）
# CLUSTER_NODE_TOKEN=
```

- [ ] **Step 6: 门禁 + 提交**

```powershell
uv run ruff check src/modelctl/core/cluster tests/test_cluster_config.py ; uv run mypy src/modelctl
git add src/modelctl/core/cluster .env.example tests/test_cluster_config.py ; git commit -m "feat(cluster): cluster/config CLUSTER_* 角色与参数读取（solo 默认零影响）"
```

---

### Task 3: `cluster/store.py` — SQLite 中心台账

按 spec §11.1 建全量 schema（goals/model_states/metrics_rollups 等 M1/M2 才写入，但一次建齐避免迁移）。**时间戳用 epoch float**（见全局约束）。

**Files:**
- Create: `src/modelctl/core/cluster/store.py`
- Test: `tests/test_cluster_store.py`

**Interfaces:**
- Consumes: `modelctl.core.process.cache_dir()`（`CACHE_DIR` env，测试被 conftest 隔离）
- Produces（`modelctl.core.cluster.store`）:
  - `class ClusterStore(db_path: Path | None = None)`（默认 `cache_dir() / "cluster-meta.db"`）
  - `init_db() -> None`；`get_meta(key: str) -> str`；`set_meta(key: str, value: str) -> None`
  - `upsert_node(*, node_id: str, node_token: str, lan_id: str, role: str, host_ip: str, hostname: str, engines: dict | None, now: float) -> str`（返回 `"joined"` 新注册 / `"rejoined"` 已存在更新）
  - `get_node(node_id: str) -> dict | None`；`find_node_by_token(token: str) -> dict | None`；`list_nodes() -> list[dict]`
  - `touch_heartbeat(node_id: str, now: float, lease_s: int) -> None`（last_seen + lease_expiry + status=online）
  - `set_node_status(node_id: str, status: str) -> None`；`rotate_node_token(node_id: str) -> str | None`
  - `sweep_expired(now: float, lease_s: int) -> list[tuple[str, str]]`（`[(node_id, new_status)]` 仅返回发生迁移的；stale→offline 规则见实现）
  - `append_event(kind: str, *, node_id: str | None = None, goal_id: str | None = None, payload: dict | None = None, now: float | None = None) -> None`
  - `recent_events(limit: int = 100, node_id: str | None = None) -> list[dict]`
  - `ClusterStore.db_path: Path`（公开只读属性；CLI init 打印台账路径用，勿用私有名访问）
  - `mask_tail(value: str) -> str`（模块级函数；`"***"` + 末 4 位，空值 → `"***"`）
  - 节点 dict 键固定：`node_id, node_token, lan_id, role, host_ip, hostname, engines(dict|None), created_at(float), last_seen(float|None), lease_expiry(float|None), status(str), disabled(int 0/1)`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_cluster_store.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_store.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : cluster.store SQLite 中心台账测试
# ===============================================================================
"""ClusterStore：建表、meta、节点 upsert、心跳/lease 三态迁移、事件。"""
from __future__ import annotations

from pathlib import Path

import pytest

from modelctl.core.cluster.store import ClusterStore, mask_tail


@pytest.fixture()
def store(tmp_path: Path) -> ClusterStore:
    s = ClusterStore(db_path=tmp_path / "cluster-meta.db")
    s.init_db()
    return s


def test_meta_roundtrip(store: ClusterStore) -> None:
    assert store.get_meta("join_token") == ""
    store.set_meta("join_token", "JT-1")
    assert store.get_meta("join_token") == "JT-1"


def test_upsert_node_join_then_rejoin(store: ClusterStore) -> None:
    assert store.upsert_node(node_id="w-210", node_token="t1", lan_id="lan-2",
                             role="worker", host_ip="10.0.0.5", hostname="w210",
                             engines={"vllm": "0.9.1"}, now=1000.0) == "joined"
    assert store.upsert_node(node_id="w-210", node_token="t1", lan_id="lan-2",
                             role="worker", host_ip="10.0.0.6", hostname="w210",
                             engines=None, now=1001.0) == "rejoined"
    node = store.get_node("w-210")
    assert node is not None and node["host_ip"] == "10.0.0.6"
    assert node["engines"] == {"vllm": "0.9.1"}  # None 不覆盖既有 engines


def test_find_by_token(store: ClusterStore) -> None:
    store.upsert_node(node_id="w-1", node_token="secret", lan_id="", role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    assert store.find_node_by_token("secret")["node_id"] == "w-1"
    assert store.find_node_by_token("nope") is None


def test_lease_three_states(store: ClusterStore) -> None:
    store.upsert_node(node_id="w-1", node_token="t", lan_id="", role="worker",
                      host_ip="", hostname="", engines=None, now=0.0)
    store.touch_heartbeat("w-1", now=0.0, lease_s=90)
    assert store.get_node("w-1")["status"] == "online"
    # lease 过期但未过 3×lease → stale
    assert ("w-1", "stale") in store.sweep_expired(now=95.0, lease_s=90)
    assert store.get_node("w-1")["status"] == "stale"
    # 已是 stale 再扫不重复报告；last_seen 过 3×lease → offline
    transitions = store.sweep_expired(now=95.0, lease_s=90)
    assert ("w-1", "stale") not in transitions
    assert ("w-1", "offline") in store.sweep_expired(now=300.0, lease_s=90)
    assert store.get_node("w-1")["status"] == "offline"


def test_rotate_node_token(store: ClusterStore) -> None:
    store.upsert_node(node_id="w-1", node_token="old", lan_id="", role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    new = store.rotate_node_token("w-1")
    assert new and new != "old"
    assert store.get_node("w-1")["node_token"] == new
    assert store.rotate_node_token("ghost") is None


def test_events_ordering_and_filter(store: ClusterStore) -> None:
    store.append_event("node.join", node_id="w-1", now=1.0)
    store.append_event("node.heartbeat", node_id="w-2", now=2.0)
    assert [e["kind"] for e in store.recent_events()] == ["node.heartbeat", "node.join"]
    assert [e["kind"] for e in store.recent_events(node_id="w-1")] == ["node.join"]


def test_mask_tail() -> None:
    assert mask_tail("") == "***"
    assert mask_tail("abcdef") == "***cdef"
    assert mask_tail("ab") == "***"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_cluster_store.py -q`
Expected: `ModuleNotFoundError: No module named 'modelctl.core.cluster.store'`

- [ ] **Step 3: 实现**

创建 `src/modelctl/core/cluster/store.py`（全量 schema 一次建齐；M0 只有 nodes/events/meta 有读写方）：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_cluster_store.py -q`
Expected: 8 PASS

- [ ] **Step 5: 门禁 + 提交**

```powershell
uv run ruff check src/modelctl/core/cluster/store.py tests/test_cluster_store.py ; uv run mypy src/modelctl
git add src/modelctl/core/cluster/store.py tests/test_cluster_store.py ; git commit -m "feat(cluster): cluster/store SQLite 中心台账（全量 schema + lease 三态）"
```

---

### Task 4: `cluster/tokens.py` — 令牌生成

**Files:**
- Create: `src/modelctl/core/cluster/tokens.py`
- Test: `tests/test_cluster_tokens.py`

**Interfaces:**
- Produces（`modelctl.core.cluster.tokens`）:
  - `new_join_token() -> str`（前缀 `JT-` + `secrets.token_urlsafe(24)`）
  - `new_node_token() -> str`（前缀 `NT-` + `secrets.token_urlsafe(24)`）
  - `token_matches(candidate: str, expected: str) -> bool`（`hmac.compare_digest`；任一为空 → False）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_cluster_tokens.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_tokens.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : cluster.tokens 令牌生成与恒定时间比较测试
# ===============================================================================
from __future__ import annotations

from modelctl.core.cluster.tokens import new_join_token, new_node_token, token_matches


def test_prefixes_and_uniqueness() -> None:
    a, b = new_join_token(), new_join_token()
    assert a.startswith("JT-") and b.startswith("JT-") and a != b
    assert new_node_token().startswith("NT-")


def test_token_matches_fail_closed() -> None:
    assert token_matches("abc", "abc")
    assert not token_matches("abc", "abd")
    assert not token_matches("", "")          # 空值 fail-closed
    assert not token_matches("abc", "")
    assert not token_matches("", "abc")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_cluster_tokens.py -q`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: 实现**

创建 `src/modelctl/core/cluster/tokens.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/tokens.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : 集群准入令牌与节点令牌生成
# ===============================================================================

"""core/cluster/tokens.py — join_token（一次性准入）/ node_token（节点长期身份）。

设计文档 §10.2。比较一律经 hmac.compare_digest 恒定时间，空值 fail-closed。
"""

from __future__ import annotations

import hmac
import secrets

_ENTROPY_BYTES = 24


def new_join_token() -> str:
    return "JT-" + secrets.token_urlsafe(_ENTROPY_BYTES)


def new_node_token() -> str:
    return "NT-" + secrets.token_urlsafe(_ENTROPY_BYTES)


def token_matches(candidate: str, expected: str) -> bool:
    """恒定时间比较；任一为空返回 False（fail-closed）。"""
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate, expected)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_cluster_tokens.py -q`
Expected: 2 PASS

- [ ] **Step 5: 门禁 + 提交**

```powershell
uv run ruff check src/modelctl/core/cluster/tokens.py tests/test_cluster_tokens.py ; uv run mypy src/modelctl
git add src/modelctl/core/cluster/tokens.py tests/test_cluster_tokens.py ; git commit -m "feat(cluster): cluster/tokens 准入与节点令牌（恒定时间比较）"
```

---

### Task 5: `cluster/wsproto.py` — WS 消息协议（纯函数）

**Files:**
- Create: `src/modelctl/core/cluster/wsproto.py`
- Test: `tests/test_cluster_wsproto.py`

**Interfaces:**
- Produces（`modelctl.core.cluster.wsproto`）:
  - `PROTO_VERSION = 1`
  - `make_hello(node_id, lan, key, meta) -> dict`；`make_welcome(node_token, interval_s, lease_s) -> dict`；`make_heartbeat(payload) -> dict`；`make_event(kind, payload) -> dict`；`make_error(message) -> dict`
  - `parse_type(raw: str) -> str`（非法 JSON / 非 dict / 缺 `t` → `""`）
  - `parse_hello(data) -> HelloMsg`（dataclass：`node_id/lan/key/meta`，缺省空值）
  - `parse_heartbeat(data) -> dict`（返回 `data["payload"]`，缺省 `{}`）
  - `dumps(msg) -> str`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_cluster_wsproto.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_wsproto.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : cluster.wsproto 消息编解码测试
# ===============================================================================
from __future__ import annotations

from modelctl.core.cluster import wsproto


def test_hello_roundtrip() -> None:
    msg = wsproto.make_hello("w-210", "lan-2", "NT-x", {"host_ip": "10.0.0.5"})
    assert msg["t"] == "hello" and msg["v"] == wsproto.PROTO_VERSION
    assert wsproto.parse_type(wsproto.dumps(msg)) == "hello"
    h = wsproto.parse_hello(msg)
    assert h.node_id == "w-210" and h.key == "NT-x" and h.meta["host_ip"] == "10.0.0.5"


def test_welcome_and_heartbeat() -> None:
    w = wsproto.make_welcome("NT-new", 10, 90)
    assert w["t"] == "welcome" and w["node_token"] == "NT-new" and w["lease_s"] == 90
    hb = wsproto.make_heartbeat({"profiles": {}, "gpu": {}})
    assert hb["t"] == "heartbeat"
    assert wsproto.parse_heartbeat(hb) == {"profiles": {}, "gpu": {}}


def test_parse_type_invalid() -> None:
    assert wsproto.parse_type("not-json") == ""
    assert wsproto.parse_type("[1,2]") == ""
    assert wsproto.parse_type('{"no_type":1}') == ""


def test_event_and_error() -> None:
    assert wsproto.make_event("model.up", {"profile": "q"})["kind"] == "model.up"
    assert wsproto.make_error("bad token")["t"] == "error"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_cluster_wsproto.py -q`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: 实现**

创建 `src/modelctl/core/cluster/wsproto.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/wsproto.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : 集群 WebSocket 消息协议（JSON 编解码，无网络依赖）
# ===============================================================================

"""core/cluster/wsproto.py — 一行一条 JSON 的 WS 消息编解码（设计文档 §5）。

M0 只用 hello/welcome/heartbeat/event/error；goal.sync/status.query 等 M1+ 再加。
零第三方依赖，可脱离 WS 单测。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTO_VERSION = 1


@dataclass
class HelloMsg:
    node_id: str = ""
    lan: str = ""
    key: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def make_hello(node_id: str, lan: str, key: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {"t": "hello", "v": PROTO_VERSION, "node_id": node_id, "lan": lan, "key": key, "meta": meta}


def make_welcome(node_token: str, interval_s: int, lease_s: int) -> dict[str, Any]:
    return {"t": "welcome", "node_token": node_token, "interval_s": interval_s, "lease_s": lease_s}


def make_heartbeat(payload: dict[str, Any]) -> dict[str, Any]:
    return {"t": "heartbeat", "payload": payload}


def make_event(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"t": "event", "kind": kind, "payload": payload}


def make_error(message: str) -> dict[str, Any]:
    return {"t": "error", "message": message}


def dumps(msg: dict[str, Any]) -> str:
    return json.dumps(msg, ensure_ascii=False)


def parse_type(raw: str) -> str:
    """解析消息类型；非法 JSON / 非 dict / 缺 t 一律返回空串（调用侧回 error 帧）。"""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    return str(data.get("t", "")) if isinstance(data, dict) else ""


def parse_hello(data: dict[str, Any]) -> HelloMsg:
    meta = data.get("meta")
    return HelloMsg(
        node_id=str(data.get("node_id", "")),
        lan=str(data.get("lan", "")),
        key=str(data.get("key", "")),
        meta=meta if isinstance(meta, dict) else {},
    )


def parse_heartbeat(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else {}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_cluster_wsproto.py -q`
Expected: 4 PASS

- [ ] **Step 5: 门禁 + 提交**

```powershell
uv run ruff check src/modelctl/core/cluster/wsproto.py tests/test_cluster_wsproto.py ; uv run mypy src/modelctl
git add src/modelctl/core/cluster/wsproto.py tests/test_cluster_wsproto.py ; git commit -m "feat(cluster): cluster/wsproto WS 消息编解码纯函数"
```

---

### Task 6: `cluster/nodes.py` — 中心侧 NodeRegistry

**Files:**
- Create: `src/modelctl/core/cluster/nodes.py`
- Test: `tests/test_cluster_nodes.py`

**Interfaces:**
- Consumes: `ClusterStore`（Task 3）、`tokens`（Task 4）、`wsproto`（Task 5）、`config`（Task 2）
- Produces（`modelctl.core.cluster.nodes`）:
  - `class AuthError(Exception)`
  - `class NodeRegistry(store: ClusterStore)`
  - `ensure_join_token() -> str`（meta 无则生成入库，有则原样返回）
  - `handle_hello(hello: HelloMsg) -> tuple[dict, str]` → `(welcome, node_id)`；`key` == join_token → 首次 join 签发新 node_token；否则按 node_token 查（命中 → 沿用其 token rejoin；未命中 → `AuthError`）；均 upsert + 记 `node.join` 事件
  - `handle_heartbeat(node_id: str, payload: dict, now: float) -> None`（M0 仅 touch_heartbeat）
  - `sweep(now: float) -> list[tuple[str, str]]`
  - `node_view(node: dict, now: float) -> dict`（去 `node_token`，加 `token_mask` / `since_seen_s` / `lease_left_s`）
  - `list_node_views(now: float) -> list[dict]`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_cluster_nodes.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_nodes.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : cluster.nodes 注册/心跳/视图编排测试
# ===============================================================================
from __future__ import annotations

from pathlib import Path

import pytest

from modelctl.core.cluster.nodes import AuthError, NodeRegistry
from modelctl.core.cluster.store import ClusterStore
from modelctl.core.cluster.wsproto import HelloMsg


@pytest.fixture()
def reg(tmp_path: Path, monkeypatch) -> NodeRegistry:
    monkeypatch.setenv("CLUSTER_LEASE_S", "90")
    s = ClusterStore(db_path=tmp_path / "m.db")
    s.init_db()
    return NodeRegistry(s)


def test_ensure_join_token_stable(reg: NodeRegistry) -> None:
    t = reg.ensure_join_token()
    assert t.startswith("JT-")
    assert reg.ensure_join_token() == t


def test_first_join_issues_node_token(reg: NodeRegistry) -> None:
    jt = reg.ensure_join_token()
    welcome, node_id = reg.handle_hello(HelloMsg(node_id="w-1", lan="lan-2", key=jt,
                                                 meta={"host_ip": "10.0.0.5"}))
    assert node_id == "w-1"
    assert welcome["t"] == "welcome" and welcome["node_token"].startswith("NT-")


def test_rejoin_reuses_node_token(reg: NodeRegistry) -> None:
    jt = reg.ensure_join_token()
    nt = reg.handle_hello(HelloMsg(node_id="w-1", lan="", key=jt, meta={}))[0]["node_token"]
    welcome2, _ = reg.handle_hello(HelloMsg(node_id="w-1", lan="", key=nt, meta={}))
    assert welcome2["node_token"] == nt


def test_bad_token_rejected(reg: NodeRegistry) -> None:
    reg.ensure_join_token()
    with pytest.raises(AuthError):
        reg.handle_hello(HelloMsg(node_id="w-x", lan="", key="bogus", meta={}))


def test_heartbeat_then_sweep(reg: NodeRegistry) -> None:
    jt = reg.ensure_join_token()
    reg.handle_hello(HelloMsg(node_id="w-1", lan="", key=jt, meta={}))
    reg.handle_heartbeat("w-1", {"profiles": {}}, now=0.0)
    assert reg.store.get_node("w-1")["status"] == "online"
    assert ("w-1", "stale") in reg.sweep(now=95.0)


def test_node_view_masks_token(reg: NodeRegistry) -> None:
    jt = reg.ensure_join_token()
    nt = reg.handle_hello(HelloMsg(node_id="w-1", lan="lan-9", key=jt,
                                   meta={"hostname": "w1"}))[0]["node_token"]
    reg.handle_heartbeat("w-1", {}, now=100.0)
    view = reg.list_node_views(now=105.0)[0]
    assert "node_token" not in view
    assert view["token_mask"] == "***" + nt[-4:]
    assert view["since_seen_s"] == pytest.approx(5.0)
    assert view["lease_left_s"] == pytest.approx(85.0)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_cluster_nodes.py -q`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: 实现**

创建 `src/modelctl/core/cluster/nodes.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/nodes.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : 中心侧节点注册/心跳/lease 扫描/脱敏视图编排
# ===============================================================================

"""core/cluster/nodes.py — 中心 NodeRegistry（设计文档 §5、§10.2）。

纯逻辑可单测；admin_cluster 仅做 HTTP/WS 薄封装。
"""

from __future__ import annotations

import time
from typing import Any

from modelctl.core.cluster import config, tokens
from modelctl.core.cluster.store import ClusterStore, mask_tail
from modelctl.core.cluster.wsproto import HelloMsg, make_welcome

_JOIN_TOKEN_META_KEY = "join_token"


class AuthError(Exception):
    """hello 鉴权失败：key 既不是 join_token 也不匹配任何 node_token。"""


class NodeRegistry:
    def __init__(self, store: ClusterStore) -> None:
        self.store = store

    def ensure_join_token(self) -> str:
        existing = self.store.get_meta(_JOIN_TOKEN_META_KEY)
        if existing:
            return existing
        fresh = tokens.new_join_token()
        self.store.set_meta(_JOIN_TOKEN_META_KEY, fresh)
        return fresh

    def handle_hello(self, hello: HelloMsg) -> tuple[dict[str, Any], str]:
        if not hello.node_id:
            raise AuthError("hello 缺少 node_id")
        join_token = self.ensure_join_token()
        if tokens.token_matches(hello.key, join_token):
            node_token = tokens.new_node_token()  # 首次 join：签发节点专属 token
        else:
            known = self.store.find_node_by_token(hello.key)
            if known is None:
                raise AuthError("无效的 join/node token")
            node_token = str(known["node_token"])  # 重连：沿用既有 token
        engines = hello.meta.get("engines")
        self.store.upsert_node(
            node_id=hello.node_id, node_token=node_token, lan_id=hello.lan,
            role="worker", host_ip=str(hello.meta.get("host_ip", "")),
            hostname=str(hello.meta.get("hostname", "")),
            engines=engines if isinstance(engines, dict) else None,
            now=time.time(),
        )
        self.store.append_event("node.join", node_id=hello.node_id)
        welcome = make_welcome(node_token, config.heartbeat_interval_s(), config.lease_s())
        return welcome, hello.node_id

    def handle_heartbeat(self, node_id: str, payload: dict[str, Any], now: float) -> None:
        self.store.touch_heartbeat(node_id, now=now, lease_s=config.lease_s())

    def sweep(self, now: float) -> list[tuple[str, str]]:
        return self.store.sweep_expired(now=now, lease_s=config.lease_s())

    def node_view(self, node: dict[str, Any], now: float) -> dict[str, Any]:
        view = {k: v for k, v in node.items() if k != "node_token"}
        view["token_mask"] = mask_tail(str(node.get("node_token", "")))
        last_seen = node.get("last_seen")
        lease_expiry = node.get("lease_expiry")
        view["since_seen_s"] = round(now - last_seen, 1) if last_seen is not None else None
        view["lease_left_s"] = round(lease_expiry - now, 1) if lease_expiry is not None else None
        return view

    def list_node_views(self, now: float) -> list[dict[str, Any]]:
        return [self.node_view(n, now) for n in self.store.list_nodes()]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_cluster_nodes.py -q`
Expected: 6 PASS

- [ ] **Step 5: 门禁 + 提交**

```powershell
uv run ruff check src/modelctl/core/cluster/nodes.py tests/test_cluster_nodes.py ; uv run mypy src/modelctl
git add src/modelctl/core/cluster/nodes.py tests/test_cluster_nodes.py ; git commit -m "feat(cluster): cluster/nodes 中心注册与 lease 编排"
```

---

### Task 7: `admin_cluster.py` — REST + WS 路由（依赖 websockets）

**Files:**
- Modify: `pyproject.toml`（dev extra += `websockets>=12`）
- Modify: `gateway/pyproject.toml`（deps += `websockets>=12`）
- Create: `src/modelctl/core/webui/admin_cluster.py`
- Modify: `src/modelctl/core/webui/admin_router.py:40-47`（`_SUBROUTER_MODULES` 追加）
- Modify: `tests/test_webui_smoke.py`（CHECKS 增 1 条 + solo 404 断言）
- Test: `tests/test_cluster_http.py`

**Interfaces:**
- Consumes: `NodeRegistry`/`AuthError`（Task 6）、`config.is_center`（Task 2）、`wsproto`（Task 5）、`tokens`（Task 4）、`admin_auth.require_auth`（现有）
- Produces（`modelctl.core.webui.admin_cluster`）:
  - `get_registry() -> NodeRegistry`（进程内单例：懒建 ClusterStore + init_db；REST/WS/server 共享）
  - `_REGISTRY: NodeRegistry | None`（模块级单例变量；测试通过置 None 重置）
  - `_router() -> APIRouter`
  - 端点（非中心角色统一 `404 {"detail":"cluster disabled"}`；REST 全部 `Depends(require_auth)`）：
    - `GET  /cluster/status` → `{role, is_center, nodes_total, nodes_online}`
    - `GET  /cluster/nodes` → `{"nodes": [node_view…]}`
    - `GET  /cluster/events?node_id=&limit=` → `{"events": […]}`
    - `POST /cluster/join-tokens/rotate` → `{"join_token": "JT-…"}`
    - `WS   /ws/cluster`（hello/welcome/heartbeat/event 循环，见实现）
  - 模块内路径写完整（`/cluster/...`、`/ws/cluster`），`_SUBROUTER_MODULES` 挂 **空前缀**。

- [ ] **Step 1: 加依赖（先装再测）**

`pyproject.toml` dev extra（:18-23）加一行 `"websockets>=12",`；`gateway/pyproject.toml` deps（:9-14）`"httpx>=0.27",` 后加一行 `"websockets>=12",`。

```powershell
uv lock ; uv sync --extra dev ; uv sync --project gateway
```

Expected: 两处 venv 均可 `import websockets`（`.venvs/gateway` 由第二条保证）。

- [ ] **Step 2: 写失败测试**

创建 `tests/test_cluster_http.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_http.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : admin_cluster REST/WS 端点测试
# ===============================================================================
"""admin_cluster：中心角色端点行为、鉴权、solo 404、WS hello/heartbeat 全流程。"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("websockets")
from fastapi.testclient import TestClient  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_12345"


def _h():
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture()
def center_client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CLUSTER_ROLE", "both")
    import modelctl.core.webui.admin_cluster as ac

    ac._REGISTRY = None  # 重置单例（CACHE_DIR 已由 conftest 隔离到 tmp_path）
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c
    ac._REGISTRY = None


def test_status_reports_center(center_client) -> None:
    r = center_client.get("/admin/api/cluster/status", headers=_h())
    assert r.status_code == 200 and r.json()["is_center"] is True


def test_nodes_empty(center_client) -> None:
    assert center_client.get("/admin/api/cluster/nodes", headers=_h()).json()["nodes"] == []


def test_events_empty(center_client) -> None:
    assert center_client.get("/admin/api/cluster/events", headers=_h()).json()["events"] == []


def test_join_token_rotate(center_client) -> None:
    r = center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h())
    assert r.status_code == 200 and r.json()["join_token"].startswith("JT-")


def test_cluster_requires_auth(center_client) -> None:
    assert center_client.get("/admin/api/cluster/nodes").status_code == 401


def test_solo_role_returns_404(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CLUSTER_ROLE", "solo")
    import modelctl.core.webui.admin_cluster as ac

    ac._REGISTRY = None
    app = create_app(admin=True)
    with TestClient(app) as c:
        assert c.get("/admin/api/cluster/nodes", headers=_h()).status_code == 404
    ac._REGISTRY = None


def test_ws_hello_heartbeat_registers_node(center_client) -> None:
    jt = center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h()).json()["join_token"]
    with center_client.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 1, "node_id": "w-9", "lan": "lan-1", "key": jt, "meta": {}})
        welcome = ws.receive_json()
        assert welcome["t"] == "welcome" and welcome["node_token"].startswith("NT-")
        ws.send_json({"t": "heartbeat", "payload": {"profiles": {}}})
        assert ws.receive_json()["t"] == "ack"
    nodes = center_client.get("/admin/api/cluster/nodes", headers=_h()).json()["nodes"]
    assert any(n["node_id"] == "w-9" and n["status"] == "online" for n in nodes)


def test_ws_bad_token_closes(center_client) -> None:
    from starlette.websockets import WebSocketDisconnect

    center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h())
    with center_client.websocket_connect("/admin/api/ws/cluster") as ws:
        ws.send_json({"t": "hello", "v": 1, "node_id": "w-x", "lan": "", "key": "bogus", "meta": {}})
        msg = ws.receive_json()
        assert msg["t"] == "error"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
```

修改 `tests/test_webui_smoke.py`：CHECKS 列表（:36-47）`("/admin/api/config/static", {}, 200),` 之前插入一行（默认 solo → 404）：

```python
    ("/admin/api/cluster/status", {}, 404),
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/test_cluster_http.py -q`
Expected: `ModuleNotFoundError: No module named 'modelctl.core.webui.admin_cluster'`

- [ ] **Step 4: 实现 admin_cluster.py**

创建 `src/modelctl/core/webui/admin_cluster.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_cluster.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : 集群控制面 REST + WebSocket 端点（仅中心角色启用）
# ===============================================================================

"""core/webui/admin_cluster.py — /admin/api/cluster/* 与 /admin/api/ws/cluster。

非中心角色（solo/worker）全部端点 404；REST 过 require_auth（operator）；WS 在
hello 帧内用 join_token/node_token 鉴权（worker 不持有 API_KEY）。NodeRegistry
进程内单例，REST/WS 共享同一 SQLite 台账。设计文档 §5、§6.5、§10。
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from loguru import logger

from modelctl.core.cluster import config, tokens, wsproto
from modelctl.core.cluster.nodes import AuthError, NodeRegistry
from modelctl.core.cluster.store import ClusterStore
from modelctl.core.webui.admin_auth import require_auth

router = APIRouter()

_REGISTRY: NodeRegistry | None = None

_SWEEP_INTERVAL_S = 10.0
_last_sweep = 0.0


def get_registry() -> NodeRegistry:
    """NodeRegistry 进程内单例（懒建库）。测试经 admin_cluster._REGISTRY=None 重置。"""
    global _REGISTRY
    if _REGISTRY is None:
        store = ClusterStore()
        store.init_db()
        _REGISTRY = NodeRegistry(store)
    return _REGISTRY


def _router() -> APIRouter:
    return router


def _disabled() -> JSONResponse | None:
    return None if config.is_center() else JSONResponse(status_code=404, content={"detail": "cluster disabled"})


def _sweep_if_due() -> None:
    """惰性 lease 扫描：任一 REST/WS 事件顺带触发，≥10s 才真正扫一次（免后台线程）。"""
    global _last_sweep
    now = time.time()
    if now - _last_sweep < _SWEEP_INTERVAL_S:
        return
    _last_sweep = now
    for node_id, new_status in get_registry().sweep(now=now):
        logger.info(f"节点 {node_id} 状态迁移 → {new_status}")


@router.get("/cluster/status")
async def cluster_status(_base: None = Depends(require_auth)):
    if (off := _disabled()) is not None:
        return off
    _sweep_if_due()
    reg = get_registry()
    nodes = reg.store.list_nodes()
    return {"role": config.cluster_role(), "is_center": config.is_center(),
            "nodes_total": len(nodes), "nodes_online": sum(1 for n in nodes if n["status"] == "online")}


@router.get("/cluster/nodes")
async def cluster_nodes(_base: None = Depends(require_auth)):
    if (off := _disabled()) is not None:
        return off
    _sweep_if_due()
    return {"nodes": get_registry().list_node_views(now=time.time())}


@router.get("/cluster/events")
async def cluster_events(node_id: str = Query(""), limit: int = Query(100, ge=1, le=1000),
                         _base: None = Depends(require_auth)):
    if (off := _disabled()) is not None:
        return off
    events = get_registry().store.recent_events(limit=limit, node_id=node_id or None)
    return {"events": events}


@router.post("/cluster/join-tokens/rotate")
async def rotate_join_token(_base: None = Depends(require_auth)):
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    fresh = tokens.new_join_token()
    reg.store.set_meta("join_token", fresh)
    reg.store.append_event("token.rotate", payload={"scope": "join"})
    return {"join_token": fresh}


@router.websocket("/ws/cluster")
async def ws_cluster(ws: WebSocket):
    """worker 通道：hello（token 鉴权）→ welcome → heartbeat/event 循环。"""
    if not config.is_center():
        await ws.close(code=4404)
        return
    await ws.accept()
    reg = get_registry()
    node_id = ""
    try:
        hello_raw = await ws.receive_text()
        if wsproto.parse_type(hello_raw) != "hello":
            await ws.send_text(wsproto.dumps(wsproto.make_error("首帧须为 hello")))
            await ws.close(code=4400)
            return
        welcome, node_id = reg.handle_hello(wsproto.parse_hello(json.loads(hello_raw)))
        await ws.send_text(wsproto.dumps(welcome))
        while True:
            raw = await ws.receive_text()
            mtype = wsproto.parse_type(raw)
            data: dict[str, Any] = json.loads(raw)
            if mtype == "heartbeat":
                reg.handle_heartbeat(node_id, wsproto.parse_heartbeat(data), now=time.time())
                _sweep_if_due()
                await ws.send_text(wsproto.dumps({"t": "ack"}))
            elif mtype == "event":
                payload = data.get("payload")
                reg.store.append_event(str(data.get("kind", "")), node_id=node_id,
                                       payload=payload if isinstance(payload, dict) else None)
                await ws.send_text(wsproto.dumps({"t": "ack"}))
            else:
                await ws.send_text(wsproto.dumps(wsproto.make_error(f"未知消息类型: {mtype}")))
    except WebSocketDisconnect:
        return
    except AuthError:
        await ws.send_text(wsproto.dumps(wsproto.make_error("鉴权失败")))
        await ws.close(code=4401)
        return
```

- [ ] **Step 5: 注册子路由**

`src/modelctl/core/webui/admin_router.py` `_SUBROUTER_MODULES`（:40-47）元组末尾追加：

```python
    ("modelctl.core.webui.admin_cluster", ""),
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_cluster_http.py tests/test_webui_smoke.py -q`
Expected: 全 PASS（smoke 的路由枚举 `app.openapi()["paths"]` 自然包含新路径；solo 下 cluster 端点 404 符合 CHECKS）

- [ ] **Step 7: 门禁 + 提交**

```powershell
uv run ruff check src/modelctl tests/test_cluster_http.py ; uv run mypy src/modelctl
git add pyproject.toml uv.lock gateway/pyproject.toml gateway/uv.lock src/modelctl/core/webui/admin_cluster.py src/modelctl/core/webui/admin_router.py tests/test_cluster_http.py tests/test_webui_smoke.py ; git commit -m "feat(cluster): admin_cluster REST/WS 端点与 websockets 依赖"
```

---

### Task 8: `cluster/agent.py` — WorkerAgent 线程 + 接入 webui server

**Files:**
- Create: `src/modelctl/core/cluster/agent.py`
- Modify: `src/modelctl/core/webui/server.py`（`main()` 在 `create_app` 后、`uvicorn.run` 前接入）
- Test: `tests/test_cluster_agent.py`

**Interfaces:**
- Consumes: `wsproto`、`config.*`、`envfile.set_env_values` / `envfile.PROJECT_ROOT`、`capabilities.probe`
- Produces（`modelctl.core.cluster.agent`）:
  - `ws_url(center_url: str, insecure: bool) -> str`（`http→ws`/`https→wss`；insecure 强制 `ws://`；拼 `/admin/api/ws/cluster`；对尾斜杠容错）
  - `collect_heartbeat() -> dict`（M0：`{"profiles": {}, "gpu": {"count","vram_total_mb_per_gpu"}, "host": {}}`；探测异常返回空壳不抛）
  - `class WorkerAgent(stop_event: threading.Event)`：`run()`（退避 1→2→…→30s 重连；`stop_event` 置位即退）
  - `start_agent_in_background() -> threading.Thread | None`（仅 `is_worker()` 且配齐 center_url+node_id 才起 daemon 线程）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_cluster_agent.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_agent.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : cluster.agent 真连假中心测试
# ===============================================================================
"""WorkerAgent：URL 推导、心跳形状、hello→welcome→heartbeat 全链路（假中心）。"""
from __future__ import annotations

import json
import threading

import pytest

pytest.importorskip("websockets")
from websockets.sync.server import serve  # noqa: E402

from modelctl.core.cluster import agent, wsproto  # noqa: E402


def test_ws_url_derivation() -> None:
    assert agent.ws_url("http://a:4173", insecure=False) == "ws://a:4173/admin/api/ws/cluster"
    assert agent.ws_url("https://a:4173", insecure=False) == "wss://a:4173/admin/api/ws/cluster"
    assert agent.ws_url("https://a:4173", insecure=True) == "ws://a:4173/admin/api/ws/cluster"
    assert agent.ws_url("http://a:4173/", insecure=False) == "ws://a:4173/admin/api/ws/cluster"


def test_collect_heartbeat_shape() -> None:
    hb = agent.collect_heartbeat()
    assert "profiles" in hb and "gpu" in hb and "host" in hb


def test_agent_hello_then_heartbeat(tmp_path, monkeypatch) -> None:
    seen: dict[str, str] = {}
    hello_event = threading.Event()
    hb_event = threading.Event()

    def handler(conn):
        hello = wsproto.parse_hello(json.loads(conn.recv()))
        seen["node"] = hello.node_id
        seen["key"] = hello.key
        hello_event.set()
        conn.send(wsproto.dumps(wsproto.make_welcome("NT-signed", 1, 5)))
        msg = json.loads(conn.recv())
        if msg.get("t") == "heartbeat":
            hb_event.set()
        conn.send(wsproto.dumps({"t": "ack"}))

    with serve(handler, "127.0.0.1", 0) as srv:
        port = srv.socket.getsockname()[1]
        monkeypatch.setenv("CLUSTER_CENTER_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("CLUSTER_NODE_ID", "w-test")
        monkeypatch.setenv("CLUSTER_JOIN_TOKEN", "JT-bootstrap")
        monkeypatch.setenv("CLUSTER_NODE_TOKEN", "")
        monkeypatch.setenv("CLUSTER_LAN", "lan-x")
        # 指向临时 .env，杜绝测试写仓库根 .env
        import modelctl.core.cluster.agent as ag

        monkeypatch.setattr(ag, "ENV_PATH", tmp_path / ".env")
        stop = threading.Event()
        t = threading.Thread(target=agent.WorkerAgent(stop_event=stop).run, daemon=True)
        t.start()
        assert hello_event.wait(5), "假中心未收到 hello"
        assert hb_event.wait(5), "假中心未收到 heartbeat"
        assert seen["node"] == "w-test" and seen["key"] == "JT-bootstrap"
        stop.set()
        t.join(timeout=5)
        # welcome 带回的 node_token 应已写回目标 .env
        assert "CLUSTER_NODE_TOKEN=NT-signed" in (tmp_path / ".env").read_text(encoding="utf-8")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_cluster_agent.py -q`
Expected: `ModuleNotFoundError: No module named 'modelctl.core.cluster.agent'`

- [ ] **Step 3: 实现 agent.py**

创建 `src/modelctl/core/cluster/agent.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/agent.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : worker 侧常驻 Agent：主动连中心、心跳上报、node_token 落盘、退避重连
# ===============================================================================

"""core/cluster/agent.py — WorkerAgent（设计文档 §5；reconciler 的 M0 前奏）。

阻塞式 websockets.sync.client 跑在 daemon 线程；M0 心跳仅 GPU 概要（模型级留 M1）。
welcome 带回的 node_token 写回 .env（set_env_values）并驻内存，下次重连免用一次性
join_token。ENV_PATH 模块级变量便于测试重定向。
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Any

from loguru import logger

from modelctl.core.cluster import config, wsproto
from modelctl.core.envfile import PROJECT_ROOT, set_env_values

ENV_PATH = PROJECT_ROOT / ".env"

_BACKOFF_MAX_S = 30


def ws_url(center_url: str, insecure: bool) -> str:
    """中心 http(s) URL → WS URL；insecure=True 强制 ws://（仅内网调试）。"""
    base = center_url.rstrip("/")
    if insecure:
        for prefix in ("https://", "http://"):
            if base.startswith(prefix):
                base = "ws://" + base[len(prefix):]
                break
        else:
            base = "ws://" + base
    elif base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    else:
        base = "ws://" + base
    return base + "/admin/api/ws/cluster"


def collect_heartbeat() -> dict[str, Any]:
    """M0 心跳 payload：GPU 概要 + 空 profiles/主机段（模型级采集属 M1 reconciler）。"""
    gpu: dict[str, Any] = {}
    try:
        from modelctl.core.capabilities import probe

        caps = probe()
        gpu = {"count": caps.gpu_count, "vram_total_mb_per_gpu": caps.vram_total_mb_per_gpu}
    except Exception as exc:  # noqa: BLE001 — 探测失败不阻断心跳
        logger.debug(f"心跳 GPU 探测失败（忽略）: {exc}")
    return {"profiles": {}, "gpu": gpu, "host": {}}


class WorkerAgent:
    """worker→中心长连接维护者。异常一律吞掉进退避重连，绝不让线程带崩宿主 webui。"""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event
        self._node_token = config.node_token()

    def _current_key(self) -> str:
        return self._node_token or config.join_token()

    def _persist_node_token(self, token: str) -> None:
        self._node_token = token
        try:
            set_env_values({"CLUSTER_NODE_TOKEN": token}, env_path=ENV_PATH)
        except OSError as exc:  # noqa: BLE001 — 落盘失败不影响本连接
            logger.warning(f"node_token 写回 .env 失败（内存态仍生效）: {exc}")

    def _connect_and_serve(self) -> None:
        from websockets.sync.client import connect

        url = ws_url(config.center_url(), config.ws_insecure())
        with connect(url, open_timeout=10) as ws:
            ws.send(wsproto.dumps(wsproto.make_hello(
                config.node_id(), config.lan_id(), self._current_key(),
                {"hostname": socket.gethostname()})))
            welcome = json.loads(ws.recv())
            if welcome.get("t") != "welcome":
                raise ConnectionError(f"中心拒绝注册: {welcome}")
            issued = str(welcome.get("node_token", ""))
            if issued and issued != self._node_token:
                self._persist_node_token(issued)
            interval = float(welcome.get("interval_s", config.heartbeat_interval_s()))
            while not self._stop.is_set():
                if self._stop.wait(interval):
                    break
                ws.send(wsproto.dumps(wsproto.make_heartbeat(collect_heartbeat())))
                ws.recv()  # 等 ack，保持请求-应答有序

    def run(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            if not (config.center_url() and config.node_id()):
                logger.debug("缺 CLUSTER_CENTER_URL/CLUSTER_NODE_ID，Agent 暂不连接")
                if self._stop.wait(backoff):
                    break
                continue
            try:
                self._connect_and_serve()
                backoff = 1  # 正常断开（如中心重启）后从 1s 重新起步
            except Exception as exc:  # noqa: BLE001 — 连接级异常统一退避重连
                logger.warning(f"集群中心连接中断，{backoff}s 后重连: {exc}")
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, _BACKOFF_MAX_S)


def start_agent_in_background() -> threading.Thread | None:
    """webui server 启动钩子：worker/both 角色且配置齐备时起 daemon 线程，否则 None。"""
    if not (config.is_worker() and config.center_url() and config.node_id()):
        return None
    stop = threading.Event()
    thread = threading.Thread(target=WorkerAgent(stop_event=stop).run,
                              name="modelctl-worker-agent", daemon=True)
    thread.start()
    return thread
```

- [ ] **Step 4: 接入 server.main()**

`src/modelctl/core/webui/server.py` 的 `main()`（:92-111）中，`create_app(admin=True)` 之后、`uvicorn.run(...)` 之前插入：

```python
    from modelctl.core.cluster import config as cluster_config

    if cluster_config.is_worker():
        from modelctl.core.cluster import agent as cluster_agent

        cluster_agent.start_agent_in_background()
```

（solo/纯中心：`is_worker()` False，一行 if 即过，零副作用。）

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_cluster_agent.py -q`
Expected: 3 PASS

- [ ] **Step 6: 门禁 + 提交**

```powershell
uv run ruff check src/modelctl tests/test_cluster_agent.py ; uv run mypy src/modelctl
git add src/modelctl/core/cluster/agent.py src/modelctl/core/webui/server.py tests/test_cluster_agent.py ; git commit -m "feat(cluster): agent WorkerAgent 与 webui 启动接入"
```

---

### Task 9: `center_probe.py` + `POST /cluster/join-check` + CLI `cluster` 子命令

CLI join 需在写 `.env` **之前**校验 token 并同步拿 node_token（否则 token 写错后 Agent 永远连不上）。新增轻量 REST `POST /cluster/join-check`（凭据=请求体内 join token，与 WS hello 同一校验路径，参照现有 `/login` 的先例不走 Bearer）；CLI 经 stdlib `urllib` 调中心（中心 webui 必须在跑）。

**Files:**
- Create: `src/modelctl/core/cluster/center_probe.py`
- Modify: `src/modelctl/core/webui/admin_cluster.py`（Task 7 产物，追加端点）
- Modify: `src/modelctl/cli.py`（`build_parser()` :178-181 前插子命令组；`main()` :1226 后插分发；文件尾部加 handler）
- Test: `tests/test_cluster_cli.py`；`tests/test_cluster_http.py`（追加 join-check 2 条）

**Interfaces:**
- Consumes: `NodeRegistry`、`tokens`、`ClusterStore`、`envfile.set_env_values`、`cli._print_table`、`config`
- Produces:
  - `center_probe.post_json(url: str, payload: dict, api_key: str = "", timeout: float = 5.0) -> tuple[int, dict]`（stdlib urllib；网络/解析异常 → `(-1, {"error": "..."})`）
  - `center_probe.get_json(url: str, api_key: str = "", timeout: float = 5.0) -> tuple[int, dict]`（同上）
  - `center_probe.check_join(center_url: str, token: str, node_id: str, lan: str = "") -> tuple[bool, str, str]` → `(ok, node_token, message)`（POST `{center_url}/admin/api/cluster/join-check`）
  - `admin_cluster` 新端点 `POST /cluster/join-check`（body `{node_id, key, lan, host_ip, hostname}`；成功 `{"ok": true, "node_token": ...}`，失败 401）
  - CLI：`modelctl cluster init` / `join --center --token --node-id [--lan] [--role worker|both]` / `nodes` / `status` / `join-token [--rotate] [--rotate-node <id>]`

- [ ] **Step 1: 写失败测试（join-check 端点）**

`tests/test_cluster_http.py` 末尾追加：

```python
def test_join_check_valid_token_registers_offline_node(center_client) -> None:
    jt = center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h()).json()["join_token"]
    r = center_client.post("/admin/api/cluster/join-check",
                           json={"node_id": "w-c", "key": jt, "lan": "lan-7"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["node_token"].startswith("NT-")
    nodes = center_client.get("/admin/api/cluster/nodes", headers=_h()).json()["nodes"]
    target = [n for n in nodes if n["node_id"] == "w-c"][0]
    assert target["status"] == "offline"  # 预注册未连接：offline，WS hello 后转 online


def test_join_check_bad_token_401(center_client) -> None:
    center_client.post("/admin/api/cluster/join-tokens/rotate", headers=_h())
    r = center_client.post("/admin/api/cluster/join-check",
                           json={"node_id": "w-x", "key": "bogus", "lan": ""})
    assert r.status_code == 401
```

Run: `uv run pytest tests/test_cluster_http.py -q -k join_check`
Expected: FAIL（404/405，端点不存在）

- [ ] **Step 2: 实现 join-check 端点**

`admin_cluster.py`：顶部加 `from pydantic import BaseModel`，`rotate_join_token` 之后追加：

```python
class _JoinCheckBody(BaseModel):
    node_id: str
    key: str
    lan: str = ""
    host_ip: str = ""
    hostname: str = ""


@router.post("/cluster/join-check")
async def join_check(body: _JoinCheckBody):
    """CLI join 预检：凭据=请求体 join token（同 WS hello 校验路径，参照 /login 先例）。

    成功即预注册节点（status=offline，WS hello 后转 online）并同步返回 node_token，
    CLI 直接落 .env，Agent 首连即用节点身份。
    """
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    if tokens.token_matches(body.key, reg.ensure_join_token()):
        node_token = tokens.new_node_token()
        result = reg.store.upsert_node(node_id=body.node_id, node_token=node_token, lan_id=body.lan,
                                       role="worker", host_ip=body.host_ip, hostname=body.hostname,
                                       engines=None, now=time.time())
        if result == "joined":
            reg.store.set_node_status(body.node_id, "offline")  # 预注册：等待首次 WS hello
        reg.store.append_event("node.join_check", node_id=body.node_id, payload={"result": result})
        return {"ok": True, "node_token": node_token}
    known = reg.store.find_node_by_token(body.key)
    if known is None:
        return JSONResponse(status_code=401, content={"detail": "无效的 join/node token"})
    return {"ok": True, "node_token": str(known["node_token"])}
```

Run: `uv run pytest tests/test_cluster_http.py -q`
Expected: 全 PASS（含原 8 条 + 新 2 条）

- [ ] **Step 3: 写失败测试（CLI）**

创建 `tests/test_cluster_cli.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_cli.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : modelctl cluster CLI 子命令测试（无 HTTP，probe 打桩）
# ===============================================================================
"""cluster init/join/nodes/join-token：exit code、.env 写回、probe 调用参数。"""
from __future__ import annotations

import pytest

from modelctl.core.cluster.store import ClusterStore

CLUSTER_KEYS = ["CLUSTER_ROLE", "CLUSTER_CENTER_URL", "CLUSTER_NODE_ID", "CLUSTER_LAN",
                "CLUSTER_JOIN_TOKEN", "CLUSTER_NODE_TOKEN", "API_KEY"]


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for k in CLUSTER_KEYS:
        monkeypatch.delenv(k, raising=False)
    # .env 读写整体重定向到 tmp（load_env/set_env_values 缺省路径均随之改变）
    import modelctl.core.envfile as ef

    monkeypatch.setattr(ef, "PROJECT_ROOT", tmp_path)


def _main(argv):
    from modelctl import cli

    return cli.main(argv)


def test_init_on_center_creates_join_token(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLUSTER_ROLE", "both")
    assert _main(["cluster", "init"]) == 0
    store = ClusterStore()  # CACHE_DIR 已由 conftest 隔离
    assert store.get_meta("join_token").startswith("JT-")


def test_init_refuses_solo() -> None:
    assert _main(["cluster", "init"]) == 2  # 默认 solo：拒绝并提示先设 CLUSTER_ROLE


def test_join_writes_env_on_success(monkeypatch, tmp_path) -> None:
    import modelctl.core.cluster.center_probe as cp

    monkeypatch.setattr(cp, "check_join", lambda *a, **k: (True, "NT-signed", ""))
    assert _main(["cluster", "join", "--center", "http://c:4173", "--token", "JT-1",
                  "--node-id", "w-1", "--lan", "lan-2"]) == 0
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "CLUSTER_CENTER_URL=http://c:4173" in env_text
    assert "CLUSTER_NODE_ID=w-1" in env_text
    assert "CLUSTER_LAN=lan-2" in env_text
    assert "CLUSTER_ROLE=worker" in env_text
    assert "CLUSTER_NODE_TOKEN=NT-signed" in env_text


def test_join_fails_without_writing(monkeypatch, tmp_path) -> None:
    import modelctl.core.cluster.center_probe as cp

    monkeypatch.setattr(cp, "check_join", lambda *a, **k: (False, "", "token 无效"))
    assert _main(["cluster", "join", "--center", "http://c:4173", "--token", "bad",
                  "--node-id", "w-1"]) == 2
    assert not (tmp_path / ".env").exists()


def test_join_token_rotate_on_center(monkeypatch) -> None:
    monkeypatch.setenv("CLUSTER_ROLE", "both")
    assert _main(["cluster", "init"]) == 0
    store = ClusterStore()
    old = store.get_meta("join_token")
    assert _main(["cluster", "join-token", "--rotate"]) == 0
    assert store.get_meta("join_token") != old


def test_nodes_requires_center_url(monkeypatch) -> None:
    monkeypatch.setenv("CLUSTER_ROLE", "both")
    monkeypatch.delenv("CLUSTER_CENTER_URL", raising=False)
    assert _main(["cluster", "nodes"]) == 2
```

Run: `uv run pytest tests/test_cluster_cli.py -q`
Expected: `ModuleNotFoundError`（center_probe 不存在）或 argparse 报错 exit 2 不符

- [ ] **Step 4: 实现 center_probe.py**

创建 `src/modelctl/core/cluster/center_probe.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/center_probe.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : CLI 侧中心 HTTP 探活/join 预检（stdlib urllib，无 httpx 依赖）
# ===============================================================================

"""core/cluster/center_probe.py — CLI 到中心 REST 的最小 HTTP 客户端。

主包不依赖 httpx（gateway 子项目专属），此处 stdlib urllib 够用：短超时 + JSON 解析，
网络异常一律折叠为 (-1, {"error": ...})，由调用方决定用户提示。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def _request(method: str, url: str, payload: dict | None, api_key: str, timeout: float) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — 内网 http，scheme 由调用方保证
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, _safe_json(body)
    except urllib.error.HTTPError as exc:
        return exc.code, _safe_json(exc.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return -1, {"error": str(exc)}


def _safe_json(body: str) -> dict:
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {"data": parsed}
    except ValueError:
        return {"raw": body[:200]}


def get_json(url: str, api_key: str = "", timeout: float = 5.0) -> tuple[int, dict]:
    return _request("GET", url, None, api_key, timeout)


def post_json(url: str, payload: dict, api_key: str = "", timeout: float = 5.0) -> tuple[int, dict]:
    return _request("POST", url, payload, api_key, timeout)


def check_join(center_url: str, token: str, node_id: str, lan: str = "") -> tuple[bool, str, str]:
    """join 预检：(ok, node_token, message)。center_url 末尾斜杠容错。"""
    base = center_url.rstrip("/")
    status, body = post_json(f"{base}/admin/api/cluster/join-check",
                             {"node_id": node_id, "key": token, "lan": lan})
    if status == 200 and body.get("ok"):
        return True, str(body.get("node_token", "")), ""
    if status == -1:
        return False, "", f"中心不可达: {body.get('error', '未知错误')}"
    detail = body.get("detail")
    message = detail if isinstance(detail, str) else f"HTTP {status}"
    return False, "", message
```

- [ ] **Step 5: 实现 CLI 子命令与分发**

`src/modelctl/cli.py` 三处修改：

**(a) `build_parser()`**：`tp = sub.add_parser("trtllm", ...)` 之前插入：

```python
    # ── 集群管理面（设计文档 §4.2 M0 子集：init/join/nodes/status/join-token）──
    cp = sub.add_parser("cluster", help="分布式集群管理面（单中心 + worker 注册）")
    csub = cp.add_subparsers(dest="action", required=True)
    csub.add_parser("init", help="中心初始化：建 SQLite 台账 + 生成/复用 join token")
    cj = csub.add_parser("join", help="worker 加入集群：预检 token 并写 .env")
    cj.add_argument("--center", required=True, help="中心地址，如 http://192.168.77.210:4173")
    cj.add_argument("--token", required=True, help="中心 cluster init 打印的 join token")
    cj.add_argument("--node-id", required=True, help="本节点集群内唯一 ID（如 w-210）")
    cj.add_argument("--lan", default="", help="所属局域网标签（展示/分组用）")
    cj.add_argument("--role", choices=["worker", "both"], default="worker",
                    help="本节点角色（中心机填 both）")
    csub.add_parser("nodes", help="列出集群节点（读中心 REST，需 CLUSTER_CENTER_URL/API_KEY）")
    csub.add_parser("status", help="集群摘要（角色/节点计数）")
    ct = csub.add_parser("join-token", help="查看/轮换 join token（仅中心本机，直读台账）")
    ct.add_argument("--rotate", action="store_true", help="生成新 join token（旧的立即失效）")
    ct.add_argument("--rotate-node", default=None, metavar="NODE_ID", help="轮换指定节点的 node_token")
```

**(b) `main()` 分发**：`if args.command == "trtllm":` 分支之后追加：

```python
        if args.command == "cluster":
            return _cmd_cluster(args)
```

**(c) handler 区**（`_cmd_trtllm_status` 定义之后、`main()` 之前）追加：

```python
def _cmd_cluster(args) -> int:
    if args.action == "init":
        return _cmd_cluster_init()
    if args.action == "join":
        return _cmd_cluster_join(args)
    if args.action == "nodes":
        return _cmd_cluster_nodes()
    if args.action == "status":
        return _cmd_cluster_status()
    if args.action == "join-token":
        return _cmd_cluster_join_token(args)
    return 2


def _cluster_center_base() -> str:
    from modelctl.core.cluster import config as cluster_config
    from modelctl.core.webui.server import webui_port

    return cluster_config.center_url() or f"http://127.0.0.1:{webui_port()}"


def _cmd_cluster_init() -> int:
    from modelctl.core.cluster import config as cluster_config
    from modelctl.core.cluster.nodes import NodeRegistry
    from modelctl.core.cluster.store import ClusterStore

    if not cluster_config.is_center():
        logger.error("当前 CLUSTER_ROLE=solo/worker：请先在 .env 设 CLUSTER_ROLE=both（或 control-plane）再 init")
        return 2
    store = ClusterStore()
    store.init_db()
    token = NodeRegistry(store).ensure_join_token()
    logger.info(f"集群台账就绪: {store.db_path}")
    print(f"join token: {token}")
    print("worker 侧执行: modelctl cluster join --center http://<本机IP>:<WEBUI_PORT> --token <上面的token> --node-id <id>")
    print("提醒: 跨机接入需 WEBUI_HOST=0.0.0.0 并在防火墙放行 webui 端口")
    return 0


def _cmd_cluster_join(args) -> int:
    from modelctl.core.cluster import center_probe
    from modelctl.core.envfile import set_env_values

    ok, node_token, message = center_probe.check_join(args.center, args.token, args.node_id, args.lan)
    if not ok:
        logger.error(f"join 失败: {message}")
        return 2
    values = {"CLUSTER_ROLE": args.role, "CLUSTER_CENTER_URL": args.center.rstrip("/"),
              "CLUSTER_NODE_ID": args.node_id, "CLUSTER_LAN": args.lan,
              "CLUSTER_NODE_TOKEN": node_token}
    set_env_values(values)
    for key, value in values.items():
        os.environ[key] = value  # 当前进程立即可用（load_env 是 setdefault 语义）
    logger.info(f"已加入集群: node_id={args.node_id}（node_token 已写回 .env）")
    print("下一步: modelctl webui restart 使角色生效，Agent 将随 webui 自动注册")
    return 0


def _cmd_cluster_nodes() -> int:
    import os as _os

    from modelctl.core.cluster import center_probe

    base = _cluster_center_base()
    status, body = center_probe.get_json(f"{base}/admin/api/cluster/nodes",
                                         api_key=_os.environ.get("API_KEY", ""))
    if status == 404:
        logger.error("中心未启用集群角色（solo/worker）或路径不存在")
        return 2
    if status != 200:
        logger.error(f"中心返回异常: HTTP {status} {body}")
        return 2
    nodes = body.get("nodes", [])
    _print_table(["节点", "LAN", "角色", "状态", "最后心跳(s)", "租约剩余(s)", "主机"],
                 [[n.get("node_id", ""), n.get("lan_id") or "-", n.get("role", ""), n.get("status", ""),
                   "-" if n.get("since_seen_s") is None else f"{n['since_seen_s']:.0f}",
                   "-" if n.get("lease_left_s") is None else f"{n['lease_left_s']:.0f}",
                   n.get("hostname") or n.get("host_ip") or "-"] for n in nodes],
                 dim_indices=(1, 6))
    return 0


def _cmd_cluster_status() -> int:
    import os as _os

    from modelctl.core.cluster import center_probe

    base = _cluster_center_base()
    status, body = center_probe.get_json(f"{base}/admin/api/cluster/status",
                                         api_key=_os.environ.get("API_KEY", ""))
    if status != 200:
        logger.error(f"中心返回异常: HTTP {status} {body}")
        return 2
    print(f"角色: {body.get('role')}  中心: {body.get('is_center')}  "
          f"节点: {body.get('nodes_online')} online / {body.get('nodes_total')} total")
    return 0


def _cmd_cluster_join_token(args) -> int:
    from modelctl.core.cluster import config as cluster_config
    from modelctl.core.cluster.nodes import NodeRegistry
    from modelctl.core.cluster.store import ClusterStore

    if not cluster_config.is_center():
        logger.error("join-token 仅中心本机可用（CLUSTER_ROLE 需 control-plane/both）")
        return 2
    store = ClusterStore()
    store.init_db()
    if args.rotate_node:
        new_token = store.rotate_node_token(args.rotate_node)
        if new_token is None:
            logger.error(f"节点不存在: {args.rotate_node}")
            return 2
        store.append_event("token.rotate", node_id=args.rotate_node, payload={"scope": "node"})
        print(f"节点 {args.rotate_node} 新 node_token: {new_token}")
        print("注意: 该 worker 下次重连将被拒，需带此 token 更新 .env 或用 join token 重新加入")
        return 0
    if args.rotate:
        fresh = NodeRegistry(store).store  # noqa: F841 — 保持与 init 同一 ensure 路径
        from modelctl.core.cluster import tokens as cluster_tokens

        new_join = cluster_tokens.new_join_token()
        store.set_meta("join_token", new_join)
        store.append_event("token.rotate", payload={"scope": "join"})
        print(f"新 join token: {new_join}")
        print("旧 token 立即失效；已 join 的节点不受影响")
        return 0
    token = NodeRegistry(store).ensure_join_token()
    print(f"join token: {token}")
    return 0
```

> **实现时必须**：`(c)` 段 `--rotate` 分支里的 `fresh = NodeRegistry(store).store  # noqa` 一行是无用占位，**直接删掉**，只保留其下 `new_join = cluster_tokens.new_join_token()` 三行。

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_cluster_cli.py tests/test_cluster_http.py -q`
Expected: CLI 6 条 + HTTP 10 条全 PASS

Run（防回归）: `uv run pytest tests/test_modelctl.py tests/test_cli_env.py -q`
Expected: PASS（`cluster` 子命令不影响现有解析）

- [ ] **Step 7: 门禁 + 提交**

```powershell
uv run ruff check src/modelctl tests/test_cluster_cli.py ; uv run mypy src/modelctl
git add src/modelctl/core/cluster/center_probe.py src/modelctl/core/webui/admin_cluster.py src/modelctl/cli.py tests/test_cluster_cli.py tests/test_cluster_http.py ; git commit -m "feat(cluster): join-check 预检端点与 cluster CLI 子命令"
```

---

### Task 10: 前端"集群节点"视图（api 模块 + 路由 + 菜单）

**Files:**
- Create: `web/src/api/cluster.ts`、`web/src/views/ClusterNodesView.vue`
- Modify: `web/src/router/index.ts`（children 增一条）、`web/src/components/layout/Sidebar.vue`（menus 增一项 + 图标分支）

**Interfaces:**
- Consumes: 现有 `api/client.ts`（axios Bearer 注入）、`GET /admin/api/cluster/status|nodes` 响应（Task 7）
- Produces: 路由 `/cluster/nodes`（name `cluster-nodes`）；`api/cluster.ts` 导出 `getClusterStatus()/getClusterNodes()` 与 `NodeView/ClusterStatus` 类型

- [ ] **Step 1: 实现 api 模块**

创建 `web/src/api/cluster.ts`：

```ts
import client, { dataOf } from './client';

/** 与后端 admin_cluster.node_view 对齐（node_token 永不下发，仅 mask） */
export interface NodeView {
  node_id: string;
  lan_id: string | null;
  role: string;
  host_ip: string | null;
  hostname: string | null;
  engines: Record<string, string | null> | null;
  status: 'online' | 'stale' | 'offline' | 'disabled';
  token_mask: string;
  since_seen_s: number | null;
  lease_left_s: number | null;
}

export interface ClusterStatus {
  role: string;
  is_center: boolean;
  nodes_total: number;
  nodes_online: number;
}

export function getClusterStatus() {
  return dataOf(client.get<ClusterStatus>('/cluster/status'));
}

export function getClusterNodes() {
  return dataOf(client.get<{ nodes: NodeView[] }>('/cluster/nodes'));
}
```

- [ ] **Step 2: 实现视图**

创建 `web/src/views/ClusterNodesView.vue`（solo/404 显示"未启用"提示而非报错；5s 轮询，样式对齐现有视图的 slate 暗色系）：

```vue
<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { getClusterNodes, getClusterStatus, type NodeView, type ClusterStatus } from '@/api/cluster';
import { AxiosError } from 'axios';

const status = ref<ClusterStatus | null>(null);
const nodes = ref<NodeView[]>([]);
const disabled = ref(false); // 404 = 未启用集群角色（solo/worker）
const error = ref('');
let timer: number | undefined;

const STATUS_STYLE: Record<string, string> = {
  online: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  stale: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  offline: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
  disabled: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
};

async function refresh() {
  try {
    status.value = await getClusterStatus();
    nodes.value = (await getClusterNodes()).nodes;
    disabled.value = false;
    error.value = '';
  } catch (e) {
    if ((e as AxiosError).response?.status === 404) {
      disabled.value = true;
      return;
    }
    error.value = (e as Error).message;
  }
}

function fmtAge(s: number | null): string {
  return s === null ? '-' : s < 60 ? `${s.toFixed(0)}s` : `${(s / 60).toFixed(1)}m`;
}

onMounted(() => {
  refresh();
  timer = window.setInterval(refresh, 5000);
});
onBeforeUnmount(() => window.clearInterval(timer));
</script>

<template>
  <div class="p-6">
    <div class="mb-4 flex items-center justify-between">
      <h1 class="text-lg font-semibold text-slate-100">集群节点</h1>
      <div v-if="status" class="text-sm text-slate-400">
        角色 {{ status.role }} · {{ status.nodes_online }}/{{ status.nodes_total }} online
      </div>
    </div>

    <div v-if="disabled" class="rounded-lg border border-slate-700 bg-slate-800/50 p-6 text-sm text-slate-400">
      当前节点未启用集群角色。中心机请在 .env 设置 CLUSTER_ROLE=both 后重启 webui，
      并执行 <code class="text-blue-400">modelctl cluster init</code>。
    </div>

    <div v-else-if="error" class="rounded-lg border border-rose-800 bg-rose-900/30 p-4 text-sm text-rose-300">
      {{ error }}
    </div>

    <table v-else class="w-full text-left text-sm">
      <thead class="text-slate-400">
        <tr class="border-b border-slate-700">
          <th class="py-2 pr-4">节点</th>
          <th class="py-2 pr-4">LAN</th>
          <th class="py-2 pr-4">角色</th>
          <th class="py-2 pr-4">状态</th>
          <th class="py-2 pr-4">最后心跳</th>
          <th class="py-2 pr-4">租约剩余</th>
          <th class="py-2">主机</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="n in nodes" :key="n.node_id" class="border-b border-slate-800 text-slate-200">
          <td class="py-2 pr-4 font-mono">{{ n.node_id }}</td>
          <td class="py-2 pr-4 text-slate-400">{{ n.lan_id || '-' }}</td>
          <td class="py-2 pr-4">{{ n.role }}</td>
          <td class="py-2 pr-4">
            <span class="rounded border px-2 py-0.5 text-xs" :class="STATUS_STYLE[n.status] || STATUS_STYLE.offline">
              {{ n.status }}
            </span>
          </td>
          <td class="py-2 pr-4">{{ fmtAge(n.since_seen_s) }}</td>
          <td class="py-2 pr-4">{{ n.lease_left_s === null ? '-' : fmtAge(Math.max(n.lease_left_s, 0)) }}</td>
          <td class="py-2 text-slate-400">{{ n.hostname || n.host_ip || '-' }}</td>
        </tr>
        <tr v-if="!nodes.length">
          <td colspan="7" class="py-6 text-center text-slate-500">暂无节点，等待 worker join…</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
```

- [ ] **Step 3: 注册路由与菜单**

`web/src/router/index.ts` children 数组（`settings` 项之后）追加：

```ts
      {
        path: 'cluster/nodes',
        name: 'cluster-nodes',
        component: () => import('@/views/ClusterNodesView.vue'),
        meta: { title: '集群节点' },
      },
```

`web/src/components/layout/Sidebar.vue`：`menus` 数组（:14-23）`audit` 项之后追加：

```ts
  { to: '/cluster/nodes', label: '集群', icon: 'cluster' },
```

同文件图标分支处（`<template v-if="m.icon === ...">` 序列）追加一个分支：

```html
          <svg v-else-if="m.icon === 'cluster'" class="size-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="8.5" y="14" width="7" height="7" rx="1" /><path d="M6.5 10v1.5h11V10M12 11.5V14" /></svg>
```

（若该文件图标结构是 `v-if/v-else-if` 链且末项无 `v-else`，把新分支并入链尾；实现时以现有代码结构为准，保持每项 icon 都有分支。）

- [ ] **Step 4: 前端构建验证**

```powershell
cd web ; if (!(Test-Path node_modules)) { npm install } ; npm run build
```

Expected: `vue-tsc --noEmit` 零错误，`vite build` 产出 `../dist`（仓库根 `dist/`）。

- [ ] **Step 5: 后端防回归 + 提交**

```powershell
uv run pytest tests/test_webui_smoke.py -q
git add web/src/api/cluster.ts web/src/views/ClusterNodesView.vue web/src/router/index.ts web/src/components/layout/Sidebar.vue ; git commit -m "feat(cluster): 前端集群节点视图与路由菜单"
```

---

### Task 11: 全量门禁 + 双进程端到端验收

**Files:**
- 无新增；发现缺陷就地修复并归到对应模块提交。

**Interfaces:**
- Consumes: Task 1-10 全部产物
- Produces: 一条 M0 验收记录（写在最终汇报里，不落新文档）

- [ ] **Step 1: 全量静态门禁与测试**

```powershell
uv run ruff check src tests ; uv run mypy src/modelctl ; uv run pytest tests/ -q
```

Expected: 三项全绿；`tests/test_cluster_*.py` 共 7 个文件约 35 条用例全部 PASS（或按 CI 环境 `pytest.importorskip` 合理 skip）。

- [ ] **Step 2: 双进程端到端冒烟（本机模拟中心+worker）**

中心（仓库根，角色 both）：

```powershell
# .env: CLUSTER_ROLE=both, WEBUI_HOST=127.0.0.1, API_KEY 已配置
modelctl cluster init          # 记下打印的 join token
modelctl webui restart
modelctl cluster status        # 期望: 角色 both 中心 True 节点 0 online / 0 total
```

worker（另开一个 modelctl 副本目录或第二台机；模拟可同机不同 CACHE_DIR——但 `.env` 写回是仓库根，故推荐**第二目录 clone** 或第二台机）：

```powershell
modelctl cluster join --center http://<中心IP>:4173 --token <JT-xxx> --node-id w-local --lan lan-1
modelctl webui restart         # join 已写 CLUSTER_ROLE=worker；Agent 随 webui 起
```

中心侧验收断言：

```powershell
modelctl cluster nodes         # w-local 出现且 status=online
modelctl cluster events        # 若已实现 CLI events 子命令则跳过；用浏览器开 /cluster/nodes 看同一行
```

- [ ] **Step 3: 断连恢复冒烟**

```powershell
modelctl webui stop            # worker 侧停 → 中心 90s 后该节点应转 stale（CLUSTER_LEASE_S 默认）
```

（可临时 `CLUSTER_LEASE_S=5` 加速验证。）worker 侧 `modelctl webui start` 后 **无需再 join**：用 .env 里的 node_token 自动重连，中心侧回到 online。`modelctl cluster nodes` 复核。

- [ ] **Step 4: 最终提交（若冒烟有修复）**

```powershell
git status --short ; # 有改动则: git add <相关文件> ; git commit -m "fix(cluster): M0 端到端冒烟修复"
```

---

## 自查（Self-Review，写计划时完成）

**1. Spec 覆盖对照（M0 范围）**

| spec 条目 | 落点 |
|---|---|
| §4.1 角色矩阵（solo/worker/control-plane/both） | Task 2（config）+ Task 7（_disabled 404）+ Task 8（agent 钩子） |
| §4.3 join 写 .env 三行 + node_token | Task 1 + Task 9（join-check 同步签发） |
| §5.1-5.2 hello/welcome/heartbeat/event/error | Task 5 + Task 7（WS 循环）+ Task 8（Agent 侧） |
| §5.3 凭据链（join token 一次性 → node_token） | Task 4 + Task 6（handle_hello）+ Task 9（join-check 同路径） |
| §5.4 指数退避 1→30s / 同 id 单连接 | Task 8（退避）；同 id 踢旧连接 **见下方偏差 ①** |
| §5.4 lease 判 stale/offline（改进 G） | Task 3（sweep_expired）+ Task 7（_sweep_if_due 惰性扫描） |
| §10.2 token 存明文 + hmac 比较 + mask | Task 4 + Task 6（node_view 脱敏）+ Task 7（REST 只回 mask） |
| §10.3 WS 传输（默认跟随 scheme / INSECURE） | Task 8（ws_url） |
| §11.1 schema 全量表 + epoch 时间戳 | Task 3（含有意偏离说明） |
| §11.4 与单机状态隔离 | Task 3（cluster-meta.db 独立于 *.pid/*.lock） |
| §9.3 集群节点视图（M0 最小版） | Task 10 |
| §12 M0 验收 | Task 11 |

**已知偏差（有意为之，勿当 bug 修）**：
- ① spec §5.4"同 node_id 单连接踢旧"：M0 **不做**（实现需连接登记表+跨协程关闭；TestClient 难覆盖）。重复 WS 的写路径是幂等 upsert，仅 `node.join` 事件重复记录，可接受；M1 加 `ConnectionManager` 时一并落地。
- ② spec §4.2 CLI 全集（goal/sync/launch/backup…）：属 M1/M2，见 spec §12。
- ③ `cluster events` 作为 CLI 子命令：M0 只开 REST（`GET /cluster/events`），Task 11 Step 2 相应跳过。
- ④ lease 时间戳 epoch float 非 ISO（全局约束已声明理由）。

**2. 占位符扫描**：Task 9(c) `--rotate` 分支与 Task 7 早期草稿各有一处**已显式标注"实现时必须删除/替换"**的反模式占位（分别见各自步骤末尾注意块）——实现者照做即可，无其他 TBD/TODO。

**3. 类型一致性**：`NodeRegistry`/`AuthError`/`HelloMsg`/`ClusterStore` 方法签名在 Task 3/5/6/7/8/9 的 Produces 与各测试代码间已互相核对（`handle_hello -> tuple[dict, str]`、`upsert_node(..., now) -> str`、`welcome["node_token"]`、`node_view` 键名 `since_seen_s/lease_left_s/token_mask` 与 `web/src/api/cluster.ts` 的 `NodeView` 一致）。

---

## 执行交接

Plan complete and saved to `docs/superpowers/plans/2026-09-03-cluster-m0-registration-heartbeat.md`. 两种执行方式：

1. **Subagent-Driven（推荐）** — 每任务派新 subagent 实现 + 任务间评审（superpowers:subagent-driven-development）
2. **Inline Execution** — 本会话内按 executing-plans 批量执行、检查点评审

M1（goal+同步+reconciler+placement gate）计划待 M0 验收通过后另行编写。