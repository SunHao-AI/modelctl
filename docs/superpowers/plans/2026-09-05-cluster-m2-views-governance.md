# Cluster M2：视图 · 治理 · 备份（中心+前端闭环）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 modelctl 集群管理面的 M2 里程碑：节点治理（disable/enable/rotate-token/kick/retire）、事件流展示面（EVENT_KINDS 词表守卫 + `GET /cluster/events` 定版 + CLI `cluster events`）、中心 SQLite 热备/校验/恢复（`core/cluster/backup.py` + `cluster backup/restore` + dashboard 下载）、profile 目录与集群设置只读端点、`status/list --cluster` 中心聚合，以及 M1 留 M2 小项波（A-3/A-1/T1-④/T2-②/T7-③/T8）与前端全可操作闭环（目标矩阵 + 节点详情 + Settings 集群块）。

**Architecture:** 沿用 M0/M1 的"worker 主动出站 WS + 心跳 ack 捎带"骨架，**不新增任何反向通道**。治理动作全部落在中心：状态位经 SQLite（disabled）、连接切断经 `ConnectionRegistry.revoke` 世代表摘除（WS 循环下一帧被动感知，时延 ≤ 心跳周期）、台账清理经 store 复合删除。备份用 sqlite3 backup API 热备（不阻写），restore 只允许 CLI 且在中心停机时执行（在线替换自身运行库无可靠语义）。前端三视图全部消费既有 + 新增 REST，**轮询而非推送**（10s 节拍，与心跳同频）。

**Tech Stack:** Python 3.12（stdlib `sqlite3`/`hashlib`/`tempfile`/`pathlib`/`urllib`）、loguru；FastAPI 仅在 `core/webui/admin_cluster.py` 层导入；前端 Vue 3 `<script setup>` + TypeScript + UnoCSS + 原生 table + 自研按钮类（`btn-ghost/btn-danger/btn-primary`）——**仓库无 Element Plus，禁止新增任何前端依赖**（brainstorm 阶段设计文案中"Element Plus"表述系误述，以本计划为准）。

**Spec:** `docs/superpowers/specs/2026-09-05-cluster-m2-views-governance-design.md`（M2 定版 spec；§0.3 两处对总 spec 的有据偏离、§5 留 M2 波、§7 计划切分均已并入本计划）
**前置:** M1 已入库（HEAD=517b15f；13/13 评审闭环；回归基线见下）

## Global Constraints

- **硬约束（brainstorm 定版）**：worker 面零 diff、WS 协议不升版。精确边界：
  - `src/modelctl/core/cluster/agent.py`、`reconcile.py` **零 diff**；
  - `wsproto.py` 仅允许 **T2-②** 一处消费侧加固（`_opt_str_list` 加单条 512 截断，帧形状零变更，不算升版）；
  - `sync.py` 本地写盘属 M1 已开放的消费侧实现细节，仅 **T8**（换 engine 孤儿旧 YAML 清理）允许动；
  - 验收时对 `wsproto.py` 做 `git diff --stat` 核验。
- **不加表、不加列、零迁移**：nodes 表 `disabled` 列 M0 已建，M2 首次接通写路径。**不对任何库执行 DDL**（CLAUDE.md 铁律）。
- Python >= 3.12；ruff line-length 120、`select = ["E","F","I","B","UP"]`；`uv run mypy src/modelctl` 零错误。
- **主包新代码只允许 stdlib + PyYAML + loguru**；`fastapi` 只在 `core/webui/admin_cluster.py` 导入。M2 **不新增第三方依赖**（前后端皆然）。
- 新 `.py` 文件带仓库标准文件头（`@File/@IDE/@Author : SunHao/@Email : 2865467769@qq.com/@Date/@Desc`）。
- 时间戳一律 **epoch float（REAL）**；`YYYY-MM-DD HH:mm:ss` 只在展示层（`_fmt_ts`）出现，且**后端单端格式化**，前端/CLI 零二次加工。
- **敏感字段纪律**：token 明文永不入 events payload / 日志 / GET 响应；rotate 例外——**响应一次性返回新 token**（同 join-check 先例）。
- **solo/worker 角色零影响**：全部新 REST 端点走 `_disabled()` 404 闸门；`status/list --cluster` 在中心不可达时报错退 2，**绝不静默回退本机视图**（假报数据源比报错更糟）。
- **测试命令（overlay 铁律）**：主 venv 刻意不含 fastapi/httpx，凡测试文件触达 REST/WS 一律
  `uv run --with fastapi --with httpx pytest <显式文件清单> -q`（PowerShell 用 `;`，禁 `&&`；**pytest 不吃 PowerShell 通配符展开**，必须逐文件列出，禁 `test_cluster_*.py`，禁 `pytest -k`）。
  - **回归基线定版（计划撰写时控制器实测）**：集群 **21 文件全量 = 437 passed**（M1 账本"401"为 15 文件口径、"M0 81"为其补集——M0+M1+终审波后并集实测 437；两口径自洽）。**每个 Task 的全量回归 = 这 21 文件 437 不回退 + 本 Task 新文件全绿**。
  - 纯 core/CLI 测试（不 import fastapi）可直接 `uv run pytest <files> -q`。
- **前端基线**：`web/node_modules` 缺失，前端 Task 首步必须 `cd web; npm install`；验收线 = `npm run build`（内含 `vue-tsc --noEmit` 0 错）。前端范式样板 = `ClusterNodesView.vue`（原生 table + `STATUS_STYLE` Record + `setInterval` 轮询 + 404→disabled 提示）。
- 提交纪律：**必须** `git add <显式文件>` + `git commit --only <同样文件>`（禁 `-A`/`.`）。提交信息：`feat(cluster): ...` / `test(cluster): ...` / `fix(cluster): ...` / `docs(cluster): ...`（中文）。
- CLI 含中文表格一律复用 `cli._print_table`（内部 `_display_width`/`_ljust_width`），禁 `f"{x:<N}"` 裸对齐。
- 同文件多点编辑**串行**执行（M1 三次并行丢写教训）；brief 与实现冲突立即停手上报，禁私改测试。
- 评审/裁决**先落盘** `.superpowers/sdd/2026-09-05-cluster-m2-views-governance/task-N-review.md` 再派 fix；派发词内联裁决全文。

## 计划撰写时的事实核查裁决（实现前必读）

1. **EVENT_KINDS 守卫只告警不抛**：WS event 帧的 kind 是 worker 自由字符串
   （`admin_cluster.py` L476 `str(data.get("kind",""))` 直接入库，M0 契约"未知消息一律不回显不拒绝"）。
   守卫若在 `append_event` 硬抛，worker 一条未知 kind 事件就把 WS 循环打成 500——违反"回流炸不掉"铁律。
   定版：`append_event` 对词表外 kind 打 `logger.warning`（kind 经 `[:64]` 消毒）后**照常入库**；
   fail-fast 断言只用于**导入期自检**（中心自有埋点全部在词表内）与测试钉，不用于对端输入路径。
   词表定版 **19 种 = 12 既有 + 7 新增**：既有 12 含 `node.heartbeat`（`test_cluster_store.py` L100 在写的
   M0 历史 kind，spec 抄漏第 12 种）；`event_text` 兜底分支保证任何词表外 kind 也有合法展示。
2. **T2-② 是单条长度截断**：`_opt_str_list` 的**条数** limit 已存在（`[:limit]`），要加的是每条
   `str(v)[:512]`。`drift`（goal_id 串）与 `local_profiles`（profile 名）两处消费共用，白名单正则保证
   512 上限下无合法值被误伤。
3. **`conns.ConnectionRegistry` 无 revoke**：kick = 新增 `revoke(node_id)` 摘除当前 epoch →
   WS 循环下一次 `receive_text` 返回后 `is_current` 失败 → error 帧 + close(4409)。**被动感知**，
   时延 ≤ 对端下一次发帧（正常 ≤ 心跳 10s）；worker 指数退避重连（最坏 30s 回来，届时未禁用即恢复）。
   docstring L436-438 的"M2 与 audit.query 一并做"预告由本计划兑现（audit.query 本身仍属 M3）。
4. **store 无 `set_node_disabled`**、**center_probe 无二进制下载**、**SettingsView 无任何集群内容**、
   **`recent_events` 无 kind 过滤**、**`GoalService` 无 disabled 过滤**（`_candidates` 只在 all_nodes
   分支过滤）——全部由本计划补齐。
5. **restore 判"中心未运行"**：`core.process.is_running(WEBUI_INSTANCE)`（先例 `all_service.py` L284）。
6. **治理端点面刻意不含 GET**：POST 动作型 + DELETE 幂等预检（`get_node` 存在性前置），
   M1 终审已裁决豁免 CLAUDE.md 五方法约定（spec §0.3-2）。

## 文件结构（本计划创建/修改）

```
创建
  src/modelctl/core/cluster/events.py       EVENT_KINDS 词表 + event_text 单端拼装
  src/modelctl/core/cluster/backup.py       热备 / 校验 / 恢复（sqlite backup API）
  tests/test_cluster_governance.py          治理 core（store/conns/nodes/cli 直调面）
  tests/test_cluster_governance_http.py     治理 5 端点 + events/profiles/settings 端点（overlay）
  tests/test_cluster_events_http.py         events 端点定版（overlay）
  tests/test_cluster_backup.py              backup core + CLI（备份/校验/恢复往返）
  tests/test_cluster_events_cli.py          cluster events 子命令
  tests/test_cluster_gov_cli.py             node disable/enable/rotate-token/kick/retire 子命令
  tests/test_cluster_agg_cli.py             status/list --cluster 聚合
  web/src/api/cluster.ts 的 M2 扩展在既有文件上扩（见 Task 10）
  web/src/views/ClusterGoalsView.vue        目标矩阵 + 批量下发两段式抽屉
  web/src/views/ClusterNodeDetailView.vue   节点详情 + 治理按钮组
修改
  src/modelctl/core/cluster/store.py        set_node_disabled / delete_node_cascade /
                                            retire_node / list_profiles_in_use / recent_events(kind=) /
                                            append_event kind 告警 / SELECT * → 显式列×6
  src/modelctl/core/cluster/conns.py        revoke(node_id) -> int | None
  src/modelctl/core/cluster/nodes.py        并发契约 docstring + 新增 disable/enable/kick 复合动作 +
                                            handle_hello disabled 拒（token 校验后、upsert 前）
  src/modelctl/core/cluster/goals.py        snapshot 尺寸封顶 + goal.create 拒 disabled
  src/modelctl/core/cluster/config.py       max_snapshot_bytes()
  src/modelctl/core/cluster/wsproto.py      _opt_str_list 单条 [:512]（仅 T2-②）
  src/modelctl/core/cluster/sync.py         T8 换 engine 删旧 path
  src/modelctl/core/cluster/profiles.py     list_profile_catalog（GET /cluster/profiles 数据源）
  src/modelctl/core/cluster/center_probe.py download_file（备份下载）
  src/modelctl/core/webui/admin_cluster.py  治理×5 + events 定版 + profiles/settings 端点 + backup 下载
  src/modelctl/cli.py                       cluster node 子组、cluster events、cluster backup/restore、
                                            --cluster 聚合 flag
  web/src/api/client.ts                     downloadBlob（备份下载带 Bearer）
  web/src/router/index.ts                   新路由 cluster-goals / cluster-nodes/:id
  web/src/components/layout/Sidebar.vue     菜单"集群目标"
  web/src/views/SettingsView.vue            集群块（只读 + rotate + 备份下载）
  .env.example                              CLUSTER_MAX_SNAPSHOT_BYTES
  README.md                                 10.7 治理/备份/聚合 flag
  docs/known-pitfalls/                      按 CLAUDE.md 渐进式披露沉淀
```

**M2 范围外（勿做，spec §0.2）**：metrics_summary 帧 + `metrics_rollups` 读写 + `cluster stats`、
`audit.query` 反向帧、远程日志、dashboard 推送通道、`_drift_seen` 落库、Settings 远程写 cluster 配置、
周期自动备份、`probe --cluster`、RBAC/mTLS——属 M3/P2。

---

### Task 1: 治理 core——`store.set_node_disabled` / `delete_node_cascade` / `retire_node` + `conns.revoke` + `NodeRegistry` disable/enable/kick + hello 拒 disabled

治理的复合写全部落 core，REST（Task 3）只做参数消毒。disabled 在 `handle_hello` 的检查置于
**token 校验之后、upsert 之前**（先认身份再谈解禁，防未授权探测禁用态）。

**Files:**
- Modify: `src/modelctl/core/cluster/store.py`（nodes 分节内追加）
- Modify: `src/modelctl/core/cluster/conns.py`（尾部追加 revoke）
- Modify: `src/modelctl/core/cluster/nodes.py`（docstring + 三个方法 + handle_hello 一处插入）
- Test: `tests/test_cluster_governance.py`

**Interfaces:**
- Consumes: `ClusterStore.get_node/upsert_node/set_node_status`、`NodeRegistry.store`、`ConnectionRegistry.current_epoch/release`、`AuthError`
- Produces:
  - `ClusterStore.set_node_disabled(node_id: str, disabled: bool, *, status: str = "") -> bool`——置/清 disabled；`status` 非空时同语句连带改写 status（禁用传 `"disabled"`，404 语义由 rowcount 表达）；返回是否命中行
  - `ClusterStore.delete_node_cascade(node_id: str) -> int`——**单事务**删 nodes 行 + 该节点全部 goals + 该节点全部 model_states（**不删** events，审计留痕）；返回连带删除的 goals 数；**原子性是本方法契约**——中途失败必须整体回滚，否则会留下"节点没了但快照仍指向它"的毒台账（心跳风暴空转、gate 幽灵候选、前端 404 死链）
  - `ClusterStore.retire_node(node_id: str, *, append_event_fn=None) -> dict | None`——先 `delete_node_cascade`，再（可选回调）记 `node.retire` 事件；节点不存在返回 None。**`append_event_fn` 形参是 restore 特判的接口缝**：正常路径传 `store.append_event`，restore 场景由上层决定事件落旧库还是新库；node 不存在时不得记事件
  - `ConnectionRegistry.revoke(node_id: str) -> int | None`——摘除该节点当前登记 epoch 并返回它（无连接返回 None）。摘除后旧连接下一帧 `is_current` 必失败——kick 世代表机制的**唯一入口**，测试据此断言"连接已被取代"路径可达
  - `NodeRegistry.disable_node(node_id: str) -> dict | None`——`set_node_disabled(id, True, status="disabled")` + 记 `node.disable` 事件 + 若在线返回 `{"kicked": <revoke 结果>}`；节点不存在 None
  - `NodeRegistry.enable_node(node_id: str) -> dict | None`——清 disabled；status 回落由下次 hello/心跳决定；记 `node.enable`
  - `NodeRegistry.kick_node(node_id: str) -> dict | None`——`conns.revoke` + 记 `node.kick` 事件；节点不存在 None
  - `handle_hello` 在 token 匹配成功后、`upsert_node` 前：`known.disabled` → `raise AuthError("节点已禁用")`（join token 首次准入同样先查 `get_node` 的 disabled）

- [ ] **Step 1: 写失败测试** — 创建 `tests/test_cluster_governance.py`（纯 core，无 fastapi）

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_governance.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : M2 治理 core：disabled 写路径 / revoke / 级联退役 / hello 拒禁用
# ===============================================================================
"""治理动作的正确性只盯台账与世表实态，不信任方法返回值自述。"""
from __future__ import annotations

import pytest

from modelctl.core.cluster import conns as conns_mod
from modelctl.core.cluster.goals import GoalService
from modelctl.core.cluster.nodes import AuthError, NodeRegistry
from modelctl.core.cluster.store import ClusterStore
from modelctl.core.cluster.wsproto import make_hello


@pytest.fixture()
def store(tmp_path):
    s = ClusterStore(tmp_path / "cluster-meta.db")
    s.init_db()
    return s


@pytest.fixture()
def reg(store):
    return NodeRegistry(store, goals=GoalService(store))


def _node(store, node_id="w-1", now=100.0):
    store.upsert_node(node_id=node_id, node_token=f"NT-{node_id}", lan_id="lan-1",
                      role="worker", host_ip="", hostname="", engines=None, now=now)


def _goal(store, goal_id, node_id, profile):
    store.upsert_goal(goal_id=goal_id, node_id=node_id, profile=profile, engine="vllm",
                      profile_yaml="port: 8101\n", profile_sha="sha-a", profile_version=None,
                      intent="start", params=None, env_overlay=None, placement=None,
                      runtime_ref=None, target_role="primary",
                      stage="READY", created_by="op", now=100.0)


# ---------------- store：disabled / 级联 ----------------
def test_set_node_disabled_sets_status_and_flag(store):
    _node(store)
    assert store.set_node_disabled("w-1", True, status="disabled") is True
    row = store.get_node("w-1")
    assert row["disabled"] == 1 and row["status"] == "disabled"
    assert store.set_node_disabled("w-1", False) is True
    row = store.get_node("w-1")
    assert row["disabled"] == 0 and row["status"] == "disabled"  # 不带 status 则不动 status


def test_set_node_disabled_missing_node_returns_false(store):
    assert store.set_node_disabled("ghost", True, status="disabled") is False


def test_delete_node_cascade_removes_goals_and_states_keeps_events(store):
    _node(store)
    _goal(store, "qwen@@w-1", "w-1", "qwen")
    _goal(store, "llm@@w-1", "w-1", "llm")
    _goal(store, "qwen@@w-2", "w-2", "qwen")
    store.upsert_model_state(node_id="w-1", profile="qwen", state="running",
                             gpu=[0], port=8101, pid=7, now=100.0)
    store.append_event("node.join", node_id="w-1", now=100.0)
    assert store.delete_node_cascade("w-1") == 2
    assert store.get_node("w-1") is None
    assert [g["goal_id"] for g in store.list_goals()] == ["qwen@@w-2"]  # 他节点不动
    assert store.list_model_states(node_id="w-1") == []
    assert [e["kind"] for e in store.recent_events()] == ["node.join"]  # 审计留痕


def test_retire_node_returns_count_and_records_event(store):
    _node(store)
    _goal(store, "qwen@@w-1", "w-1", "qwen")
    out = store.retire_node("w-1", append_event_fn=store.append_event)
    assert out == {"removed_goals": 1}
    kinds = [e["kind"] for e in store.recent_events()]
    assert "node.retire" in kinds


def test_retire_node_missing_returns_none_without_event(store):
    assert store.retire_node("ghost", append_event_fn=store.append_event) is None
    assert store.recent_events() == []


# ---------------- conns.revoke ----------------
def test_revoke_invalidates_current_epoch_and_returns_it():
    c = conns_mod.ConnectionRegistry()
    epoch = c.join("w-1")
    assert c.revoke("w-1") == epoch
    assert c.is_current("w-1", epoch) is False
    assert c.current_epoch("w-1") is None


def test_revoke_without_connection_returns_none():
    assert conns_mod.ConnectionRegistry().revoke("w-1") is None


def test_release_after_revoke_does_not_evict_new_join():
    # revoke 后旧连接 finally 调 release(旧 epoch)：不得误杀 revoke 后重连的新 epoch
    c = conns_mod.ConnectionRegistry()
    old = c.join("w-1")
    c.revoke("w-1")
    new = c.join("w-1")
    c.release("w-1", old)
    assert c.is_current("w-1", new) is True


# ---------------- NodeRegistry 复合动作（签名与实现逐字对齐）----------------
def test_disable_online_node_kicks_connection(store):
    _node(store)
    c = conns_mod.ConnectionRegistry()
    epoch = c.join("w-1")
    out = reg.disable_node("w-1", conns_registry=c)
    assert out == {"kicked": True} and c.is_current("w-1", epoch) is False
    assert store.get_node("w-1")["status"] == "disabled"
    assert "node.disable" in [e["kind"] for e in store.recent_events()]


def test_disable_without_registry_keeps_connection(store):
    # CLI 直调面（无进程内连接面）：不传 registry 也能禁用，kicked 恒 False
    _node(store)
    assert reg.disable_node("w-1") == {"kicked": False}


def test_disable_missing_node_returns_none(reg):
    assert reg.disable_node("ghost") is None
    assert reg.enable_node("ghost") is None
    assert reg.kick_node("ghost", conns_mod.ConnectionRegistry()) is None


def test_enable_clears_disabled_flag(store):
    _node(store)
    reg.disable_node("w-1")
    assert reg.enable_node("w-1") is not None
    assert store.get_node("w-1")["disabled"] == 0


def test_kick_records_event_and_revokes(store):
    _node(store)
    c = conns_mod.ConnectionRegistry()
    epoch = c.join("w-1")
    assert reg.kick_node("w-1", c) == {"kicked": True}
    assert c.is_current("w-1", epoch) is False
    assert "node.kick" in [e["kind"] for e in store.recent_events()]


def test_kick_offline_node_reports_not_kicked(reg):
    _node(reg.store)
    assert reg.kick_node("w-1", conns_mod.ConnectionRegistry()) == {"kicked": False}


# ---------------- hello 拒 disabled ----------------
def _hello(node_id="w-1", key="NT-w-1"):
    from modelctl.core.cluster.wsproto import parse_hello
    return parse_hello(make_hello(node_id, "lan-1", key, {}))


def test_hello_rejects_disabled_node_with_node_token(store, reg):
    _node(store)
    store.set_node_disabled("w-1", True, status="disabled")
    with pytest.raises(AuthError):
        reg.handle_hello(_hello())


def test_hello_rejects_disabled_node_with_join_token(store, reg):
    _node(store)
    store.set_node_disabled("w-1", True, status="disabled")
    join = reg.ensure_join_token()
    with pytest.raises(AuthError):
        reg.handle_hello(_hello(key=join))  # join token 合法但节点禁用——身份对也拒


def test_hello_rejects_disabled_before_upsert(store, reg):
    # 禁用节点的 last_seen 不得因被拒的 hello 而刷新（防"禁而不死"的假在线心跳）
    _node(store, now=100.0)
    store.set_node_disabled("w-1", True, status="disabled")
    with pytest.raises(AuthError):
        reg.handle_hello(_hello())
    assert store.get_node("w-1")["last_seen"] == 100.0
```

- [ ] **Step 2: 运行确认失败**

`uv run pytest tests/test_cluster_governance.py -q` → `AttributeError: 'ClusterStore' object has no attribute 'set_node_disabled'` 等。

- [ ] **Step 3: store.py 实现**（nodes 分节 `set_node_status` 之后追加）

```python
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
```

- [ ] **Step 4: conns.py 追加 revoke**

```python
    def revoke(self, node_id: str) -> int | None:
        """主动摘除该节点当前登记的 epoch 并返回它；无连接返回 None。

        kick 的世代表入口：摘除后旧连接下一帧 `is_current` 必失败，WS 循环自行
        发 error 帧 + close(4409)。被动感知是刻意取舍（与"旧连接后来者胜"同构）：
        中心不持有连接对象，也就无需跨任务 send——代价是断连时延 ≤ 对端下一次
        发帧（正常 ≤ 心跳周期）。
        """
        with self._mu:
            return self._current.pop(node_id, None)
```

- [ ] **Step 5: nodes.py 实现**

(a) 类 docstring 增补并发契约（终审 A-1）：

```python
class NodeRegistry:
    """中心侧节点编排。

    并发契约（终审 A-1 显式化）：`_actions/_seq_base/_force_sync/_drift_seen` 四个
    进程内状态**只在单事件循环内**被安全访问——REST handler 与 WS 循环都跑在同一
    uvicorn event loop 的协程里，方法体内无 await 即无切换点，天然互斥。违例场景：
    把 `drain_actions`/`push_action` 挪到线程池（`run_in_executor`）或多 worker
    部署（uvicorn --workers>1）都会破坏该前提——届时须加锁或外置状态。
    """
```

(b) `handle_hello` 在 `node_token` 确定后（两条分支汇合处）、`upsert_node` **前**插入：

```python
        # 禁用闸门（M2）：置于 token 校验之后、upsert 之前——先认身份再谈解禁，
        # 未授权请求得到与"节点不存在"完全相同的文案，探测不出禁用态。join token
        # 首次准入同样受闸：禁用是节点级意志，换准入令牌绕开不成。
        existing = self.store.get_node(hello.node_id)
        if existing is not None and existing.get("disabled"):
            raise AuthError("节点已禁用")
```

(c) 尾部追加分节：

```python
    # ---- 治理复合动作（M2：REST 只做参数消毒，复合写在此）----
    def disable_node(self, node_id: str, *, conns_registry=None) -> dict | None:
        """禁用：置位 + 事件；`conns_registry` 非空且该节点在线时顺带 revoke（等同一次 kick）。

        goal 台账**不动**：禁用 ≠ 撤销声明，重新启用后 reconciler 按既有 revision 收敛。
        """
        if not self.store.set_node_disabled(node_id, True, status="disabled"):
            return None
        kicked = False
        if conns_registry is not None:
            kicked = conns_registry.revoke(node_id) is not None
        self.store.append_event("node.disable", node_id=node_id,
                                payload={"kicked": kicked, "operator": "api"})
        return {"kicked": kicked}

    def enable_node(self, node_id: str) -> dict | None:
        if not self.store.set_node_disabled(node_id, False):
            return None
        self.store.append_event("node.enable", node_id=node_id, payload={"operator": "api"})
        return {"ok": True}

    def kick_node(self, node_id: str, conns_registry) -> dict | None:
        if self.store.get_node(node_id) is None:
            return None
        kicked = conns_registry.revoke(node_id) is not None
        self.store.append_event("node.kick", node_id=node_id,
                                payload={"kicked": kicked, "operator": "api"})
        return {"kicked": kicked}
```

> **签名裁决**：`disable_node` 的 revoke 走可选形参 `conns_registry`（admin_cluster 传 `_CONNS`），
> **不把 ConnectionRegistry 塞进 `__init__`**——NodeRegistry 被 `cluster init` 等 CLI 直建
> （`_cmd_cluster_init` L1416 `NodeRegistry(store)`），注入连接面会逼构造点伪造依赖；
> `kick_node` 则必传（kick 的语义就是断连，离线 kick 也必须显式传 registry 拿 `kicked=False`）。
> 测试 `reg_disable` 辅助函数按此签名传参；`test_disable_online_node_kicks_connection` 与
> `test_kick_*` 的调用形式据此对齐（评审时核对测试与实现签名一致）。

- [ ] **Step 6: 运行本文件测试**

`uv run pytest tests/test_cluster_governance.py -q` → 全绿（17 条左右）。

- [ ] **Step 7: 全量回归（21 文件 overlay，禁通配符）**

```
uv run --with fastapi --with httpx pytest tests/test_cluster_store.py tests/test_cluster_store_goals.py tests/test_cluster_tokens.py tests/test_cluster_config.py tests/test_cluster_envwrite.py tests/test_cluster_wsproto.py tests/test_cluster_wsproto_v2.py tests/test_cluster_profiles.py tests/test_cluster_gate.py tests/test_cluster_goals.py tests/test_cluster_conns.py tests/test_cluster_nodes.py tests/test_cluster_ingest.py tests/test_cluster_sync_writer.py tests/test_cluster_reconcile.py tests/test_cluster_agent.py tests/test_cluster_agent_v2.py tests/test_cluster_cli.py tests/test_cluster_http.py tests/test_cluster_goals_http.py tests/test_cluster_goal_cli.py tests/test_cluster_governance.py -q
```
Expected: **437 + 本 Task 新增全绿，0 failed**（hello 拒 disabled 不破坏既有 `test_cluster_nodes.py`/`test_cluster_http.py`——它们不建 disabled 节点）。

- [ ] **Step 8: 提交**

```
git add src/modelctl/core/cluster/store.py src/modelctl/core/cluster/conns.py src/modelctl/core/cluster/nodes.py tests/test_cluster_governance.py
git commit --only src/modelctl/core/cluster/store.py src/modelctl/core/cluster/conns.py src/modelctl/core/cluster/nodes.py tests/test_cluster_governance.py -m "feat(cluster): M2 治理 core——disabled 写路径/级联退役/revoke/hello 闸门"
```

---

### Task 2: 治理 REST 端点（disable/enable/rotate-token/kick/DELETE）+ join-check 拒 disabled

薄层原则：消毒入参→调 core→消毒出参。rotate-token 一次性返回新 token 明文（同 join-check 先例）+ 连带 kick；DELETE 带 `removed_goals` 计数。

**Files:**
- Modify: `src/modelctl/core/webui/admin_cluster.py`
- Test: `tests/test_cluster_governance_http.py`（overlay）

**Interfaces:**
- Consumes: Task 1 的 `reg.disable_node(node_id, conns_registry=_CONNS)` / `reg.enable_node` / `reg.kick_node(node_id, _CONNS)`、既有 `store.rotate_node_token`、`store.retire_node(append_event_fn=store.append_event)`
- Produces（全部 `require_auth` + `_disabled()` 闸门 + 节点不存在 404）:
  - `POST /cluster/nodes/{node_id}/disable` → `{"kicked": bool}`
  - `POST /cluster/nodes/{node_id}/enable` → `{"ok": true}`
  - `POST /cluster/nodes/{node_id}/rotate-token` → `{"node_token": "<新明文>", "kicked": bool, "hint": "请在该节点 .env 更新 CLUSTER_NODE_TOKEN 后重启"}`
  - `POST /cluster/nodes/{node_id}/kick` → `{"kicked": bool}`
  - `DELETE /cluster/nodes/{node_id}` → `{"removed": true, "removed_goals": n}`
  - `join_check`：已存在节点 `disabled` → 401 `节点已禁用`（置于 token 匹配后；join token 新节点同样先查 `get_node`）

- [ ] **Step 1: 写失败测试** — `tests/test_cluster_governance_http.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_governance_http.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : M2 治理 5 端点 + join-check 拒禁用（REST 面实态断言）
# ===============================================================================
from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from modelctl.core.cluster import conns  # noqa: E402
from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_12345"
BASE = "/admin/api/cluster/nodes"


def _h():
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture()
def center(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CLUSTER_ROLE", "both")
    monkeypatch.setattr("modelctl.core.cluster.goals.MODELS_DIR", tmp_path / "models")
    import modelctl.core.webui.admin_cluster as ac

    ac._REGISTRY = None
    ac._CONNS = conns.ConnectionRegistry()
    app = create_app(admin=True)
    with TestClient(app) as c:
        reg = ac.get_registry()
        reg.store.upsert_node(node_id="w-1", node_token="NT-1", lan_id="lan-1", role="worker",
                              host_ip="", hostname="", engines=None, now=time.time())
        yield c
    ac._REGISTRY = None
    ac._CONNS = conns.ConnectionRegistry()


def _store():
    import modelctl.core.webui.admin_cluster as ac
    return ac.get_registry().store


def test_disable_sets_disabled_status_and_event(center):
    r = center.post(f"{BASE}/w-1/disable", headers=_h())
    assert r.status_code == 200 and r.json()["kicked"] is False
    assert _store().get_node("w-1")["status"] == "disabled"
    kinds = [e["kind"] for e in _store().recent_events()]
    assert "node.disable" in kinds


def test_enable_round_trip(center):
    center.post(f"{BASE}/w-1/disable", headers=_h())
    r = center.post(f"{BASE}/w-1/enable", headers=_h())
    assert r.status_code == 200
    assert _store().get_node("w-1")["disabled"] == 0


def test_disable_missing_node_404(center):
    assert center.post(f"{BASE}/ghost/disable", headers=_h()).status_code == 404
    assert center.post(f"{BASE}/ghost/enable", headers=_h()).status_code == 404
    assert center.post(f"{BASE}/ghost/kick", headers=_h()).status_code == 404
    assert center.post(f"{BASE}/ghost/rotate-token", headers=_h()).status_code == 404
    assert center.delete(f"{BASE}/ghost", headers=_h()).status_code == 404


def test_rotate_token_returns_new_plaintext_once(center):
    before = _store().get_node("w-1")["node_token"]
    r = center.post(f"{BASE}/w-1/rotate-token", headers=_h())
    body = r.json()
    assert r.status_code == 200 and body["node_token"].startswith("NT-")
    assert body["node_token"] != before
    assert _store().get_node("w-1")["node_token"] == body["node_token"]
    assert "hint" in body
    # GET 列表/详情仍只出 mask，明文不外泄
    assert center.get("/admin/api/cluster/nodes", headers=_h()).json()["nodes"][0]["token_mask"].startswith("***")


def test_rotate_token_kicks_connection(center):
    import modelctl.core.webui.admin_cluster as ac
    epoch = ac._CONNS.join("w-1")
    center.post(f"{BASE}/w-1/rotate-token", headers=_h())
    assert ac._CONNS.is_current("w-1", epoch) is False


def test_kick_returns_kicked_flag(center):
    import modelctl.core.webui.admin_cluster as ac
    ac._CONNS.join("w-1")
    assert center.post(f"{BASE}/w-1/kick", headers=_h()).json()["kicked"] is True
    assert center.post(f"{BASE}/w-1/kick", headers=_h()).json()["kicked"] is False


def test_delete_retires_and_reports_removed_goals(center):
    _store().upsert_goal(goal_id="qwen@@w-1", node_id="w-1", profile="qwen", engine="vllm",
                         profile_yaml="port: 8101\n", profile_sha="sha-a", profile_version=None,
                         intent="start", params=None, env_overlay=None, placement=None,
                         runtime_ref=None, target_role="primary", stage="READY",
                         created_by="op", now=time.time())
    r = center.delete(f"{BASE}/w-1", headers=_h())
    assert r.status_code == 200
    assert r.json() == {"removed": True, "removed_goals": 1}
    assert _store().get_node("w-1") is None
    assert "node.retire" in [e["kind"] for e in _store().recent_events()]


def test_join_check_rejects_disabled(center):
    import modelctl.core.webui.admin_cluster as ac
    join = ac.get_registry().ensure_join_token()
    center.post(f"{BASE}/w-1/disable", headers=_h())
    r = center.post("/admin/api/cluster/join-check",
                    json={"node_id": "w-1", "key": join})
    assert r.status_code == 401 and "禁用" in r.json()["detail"]
```

- [ ] **Step 2: 确认失败**（404 全红）→ **Step 3: 实现**（`admin_cluster.py`，插在 `cluster_node_detail` 之后、`force_node_sync` 前的"节点治理（M2）"分节）

```python
# ================================ 节点治理（M2，spec §2.1）================================
@router.post("/cluster/nodes/{node_id}/disable")
async def disable_node(node_id: str, _base: None = Depends(require_auth)):
    """禁用节点：hello/join-check 此后拒绝；goal 台账不动（禁用≠撤销声明）。"""
    if (off := _disabled()) is not None:
        return off
    out = get_registry().disable_node(node_id, conns_registry=_CONNS)
    if out is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    return out


@router.post("/cluster/nodes/{node_id}/enable")
async def enable_node(node_id: str, _base: None = Depends(require_auth)):
    if (off := _disabled()) is not None:
        return off
    out = get_registry().enable_node(node_id)
    if out is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    return out


@router.post("/cluster/nodes/{node_id}/rotate-token")
async def rotate_node_token(node_id: str, _base: None = Depends(require_auth)):
    """轮换节点 token：**响应一次性返回明文**（同 join-check 先例），连带 kick。

    旧 token 即刻失效（rotate 后 find_node_by_token 不再命中旧值）+ revoke 断连，
    worker 用 .env 里的旧 token 重连会被拒——必须人工把新 token 写进该节点 .env。
    """
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    if reg.store.get_node(node_id) is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    fresh = reg.store.rotate_node_token(node_id)
    if fresh is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    kicked = _CONNS.revoke(node_id) is not None
    reg.store.append_event("token.rotate", node_id=node_id,
                           payload={"scope": "node", "kicked": kicked, "operator": "api"},
                           now=time.time())
    return {"node_token": fresh, "kicked": kicked,
            "hint": "请在该节点 .env 更新 CLUSTER_NODE_TOKEN 后重启 webui"}


@router.post("/cluster/nodes/{node_id}/kick")
async def kick_node(node_id: str, _base: None = Depends(require_auth)):
    """主动断连（世代表摘除 → 下一帧自退）。一次性动作：worker 退避重连后即恢复。"""
    if (off := _disabled()) is not None:
        return off
    out = get_registry().kick_node(node_id, _CONNS)
    if out is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    return out


@router.delete("/cluster/nodes/{node_id}")
async def retire_node(node_id: str, _base: None = Depends(require_auth)):
    """节点退役：先 kick，再级联删 goals/model_states（连带撤销声明，防幽灵 goal）。

    有 goals 被连带删除时响应带 removed_goals 计数（CLI 二次确认文案消费）；
    events 不删——审计留痕。
    """
    if (off := _disabled()) is not None:
        return off
    reg = get_registry()
    if reg.store.get_node(node_id) is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    _CONNS.revoke(node_id)
    out = reg.store.retire_node(node_id, append_event_fn=reg.store.append_event)
    if out is None:
        return JSONResponse(status_code=404, content={"detail": f"节点 {node_id} 不存在"})
    return {"removed": True, **out}
```

`join_check` 在 `tokens.token_matches(...)` 分支内、`upsert_node` 前，及 node_token 回退分支查到 `known` 后，各加一处（同文案，勿泄露差异）：

```python
    existing = reg.store.get_node(body.node_id)
    if existing is not None and existing.get("disabled"):
        return JSONResponse(status_code=401, content={"detail": "节点已禁用"})
```
（第一处放 join 匹配成功体内；第二处并入既有 `known` 校验链——`known.get("disabled")` 为真同样 401 `节点已禁用`。实现者注意：两处判据都取**被 join 的 node_id** 的行，不是 token 归属行。）

- [ ] **Step 4**: `uv run --with fastapi --with httpx pytest tests/test_cluster_governance_http.py -q` 全绿。
- [ ] **Step 5**: 21 文件全量回归（上条命令 + 本文件）→ 437+ 不回退。
- [ ] **Step 6**: 提交（显式文件 `git add` + `--only`，信息 `feat(cluster): M2 治理端点——disable/enable/rotate/kick/retire + join-check 闸门`）。

---

### Task 3: 事件流定版——`events.py` 词表守卫 + `recent_events(kind=)` + `GET /cluster/events` 契约 + `goal.create` 拒 disabled

events 表 M1 已在写入，缺展示面。本 Task 定版响应形状 `{ts, node_id, goal_id, kind, text}`（后端单端拼装）+ EVENT_KINDS 守卫（**告警语义**，见全局裁决 1）。

**Files:**
- Create: `src/modelctl/core/cluster/events.py`
- Modify: `src/modelctl/core/cluster/store.py`（`append_event` 告警 + `recent_events` kind 过滤）
- Modify: `src/modelctl/core/webui/admin_cluster.py`（events 端点改造）
- Modify: `src/modelctl/core/cluster/goals.py`（`set_goals` 逐候选拒 disabled——补 `_candidates` 显式点名 `--node` 路径的 disabled 过滤空洞，verdict="节点已禁用，重新启用后才能下发"）
- Test: `tests/test_cluster_events_http.py`、`tests/test_cluster_store.py`（追加 2 条）

**Interfaces:**
- Consumes: `store.recent_events`、既有 `set_goals` verdict 形状
- Produces:
  - `events.EVENT_KINDS: frozenset[str]`——19 种定版：`node.join, node.join_check, node.sync, node.model_action, goal.create, goal.update, goal.delete, goal.retry, goal.drift, action.result, token.rotate, node.heartbeat`（既有 12）+ `node.disable, node.enable, node.kick, node.retire, db.backup, db.restore, goal.sync_overflow`（新增 7）
  - `events.is_known_kind(kind: str) -> bool`
  - `events.event_text(row: dict) -> str`——`{kind, payload}` 拼一句展示文本（详见实现）；未知 kind 兜底 `f"{kind}"` + payload 摘要，**永不抛**
  - `ClusterStore.recent_events(limit=100, node_id=None, *, kind=None) -> list[dict]`
  - `GET /cluster/events?node_id=&kind=&limit=` → `{"events": [{ts, node_id, goal_id, kind, text}]}`；`kind` 非法（∉ EVENT_KINDS）→ 400
  - `set_goals` 对 disabled 候选出 `error` verdict（不静默 skip——下发到禁用节点是意图错误，不是容量跳过）

- [ ] **Step 1: 写失败测试** — `tests/test_cluster_events_http.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_events_http.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : M2 events 端点定版：形状/过滤/词表守卫/单端拼装
# ===============================================================================
from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from modelctl.core.cluster import conns, events  # noqa: E402
from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_12345"
EV = "/admin/api/cluster/events"
GOALS = "/admin/api/cluster/goals"


def _h():
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture()
def center(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CLUSTER_ROLE", "both")
    models = tmp_path / "models"
    (models / "vllm").mkdir(parents=True)
    (models / "vllm" / "qwen.yaml").write_text("port: 8101\n", encoding="utf-8")
    monkeypatch.setattr("modelctl.core.cluster.goals.MODELS_DIR", models)
    import modelctl.core.webui.admin_cluster as ac

    ac._REGISTRY = None
    ac._CONNS = conns.ConnectionRegistry()
    app = create_app(admin=True)
    with TestClient(app) as c:
        reg = ac.get_registry()
        reg.store.upsert_node(node_id="w-1", node_token="NT-1", lan_id="lan-1", role="worker",
                              host_ip="", hostname="", engines=None, now=time.time())
        yield c
    ac._REGISTRY = None
    ac._CONNS = conns.ConnectionRegistry()


def test_event_kinds_vocabulary_pinned():
    # 19 = 12 既有 + 7 新增；增删 kind 必须同时改词表（守卫的另一半在 append_event 告警）
    assert len(events.EVENT_KINDS) == 19
    for k in ("node.disable", "node.enable", "node.kick", "node.retire",
              "db.backup", "db.restore", "goal.sync_overflow", "node.heartbeat"):
        assert k in events.EVENT_KINDS


def test_events_response_shape(center):
    store = center  # 造事件走一次真实写路径：disable 即产 node.disable
    store.post("/admin/api/cluster/nodes/w-1/disable", headers=_h())
    body = store.get(EV, headers=_h()).json()["events"]
    row = [e for e in body if e["kind"] == "node.disable"][0]
    assert set(row) == {"ts", "node_id", "goal_id", "kind", "text"}
    assert row["node_id"] == "w-1"
    import re
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row["ts"])
    assert "禁用" in row["text"]          # 后端拼装中文展示，前端零加工


def test_events_kind_filter(center):
    store = center
    store.post("/admin/api/cluster/nodes/w-1/disable", headers=_h())
    store.post("/admin/api/cluster/nodes/w-1/enable", headers=_h())
    kinds = [e["kind"] for e in store.get(EV + "?kind=node.enable", headers=_h()).json()["events"]]
    assert kinds and set(kinds) == {"node.enable"}


def test_events_unknown_kind_400(center):
    assert center.get(EV + "?kind=not.a.kind", headers=_h()).status_code == 400


def test_events_node_filter_and_limit(center):
    import modelctl.core.webui.admin_cluster as ac
    st = ac.get_registry().store
    for i in range(5):
        st.append_event("node.heartbeat", node_id="w-x", now=100.0 + i)
    out = center.get(EV + "?node_id=w-x&limit=3", headers=_h()).json()["events"]
    assert len(out) == 3 and all(e["node_id"] == "w-x" for e in out)


def test_unknown_worker_kind_still_stored_and_renderable(center):
    # worker event 帧 kind 是自由串（M0 契约）：词表外 kind 必须照常入库、照常展示
    import modelctl.core.webui.admin_cluster as ac
    ac.get_registry().store.append_event("vendor.custom", node_id="w-1", payload={"a": 1})
    rows = center.get(EV, headers=_h()).json()["events"]
    row = [e for e in rows if e["kind"] == "vendor.custom"][0]
    assert "vendor.custom" in row["text"]


def test_event_text_never_raises_on_garbage():
    for row in ({}, {"kind": None}, {"kind": "goal.update", "payload": "不是dict"},
                {"kind": "node.join", "payload": {}}):
        assert isinstance(events.event_text(row), str)


def test_create_goal_rejects_disabled_node(center):
    import modelctl.core.webui.admin_cluster as ac
    ac.get_registry().store.set_node_disabled("w-1", True, status="disabled")
    r = center.post(GOALS, json={"profile": "qwen", "node_ids": ["w-1"], "create": True},
                    headers=_h())
    assert r.status_code == 200
    rep = r.json()["report"]
    assert "禁用" in rep and r.json()["created"] == 0
```

在 `tests/test_cluster_store.py` 尾部追加：

```python
def test_recent_events_kind_filter(store: ClusterStore) -> None:
    store.append_event("node.join", node_id="w-1", now=1.0)
    store.append_event("node.heartbeat", node_id="w-1", now=2.0)
    out = store.recent_events(kind="node.join")
    assert [e["kind"] for e in out] == ["node.join"]


def test_append_event_unknown_kind_still_stored(store: ClusterStore, caplog) -> None:
    # 告警语义（全局裁决 1）：未知 kind 入库 + warning，绝不抛——worker 上报炸不得
    store.append_event("vendor.custom", node_id="w-1", now=1.0)
    assert [e["kind"] for e in store.recent_events()] == ["vendor.custom"]
```

- [ ] **Step 2: 确认失败** → **Step 3: 实现 `events.py`**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/events.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : 事件 kind 词表守卫 + 展示文本单端拼装（M2 spec §1/§2.2）
# ===============================================================================

"""core/cluster/events.py — EVENT_KINDS 守卫与 event_text 拼装。

守卫语义（计划裁决 1，勿改成 fail-fast）：WS event 帧的 kind 是 worker 自由字符串，
对端输入路径只告警不抛；词表的约束力在"中心自有埋点不得野 kind"（测试钉 + 告警
可见性）与"GET /cluster/events 的 kind 过滤白名单"两处兑现。
"""

from __future__ import annotations

from typing import Any

EVENT_KINDS: frozenset[str] = frozenset({
    # 既有（M0/M1）
    "node.join", "node.join_check", "node.sync", "node.model_action",
    "goal.create", "goal.update", "goal.delete", "goal.retry", "goal.drift",
    "action.result", "token.rotate", "node.heartbeat",
    # M2 新增
    "node.disable", "node.enable", "node.kick", "node.retire",
    "db.backup", "db.restore", "goal.sync_overflow",
})


def is_known_kind(kind: str) -> bool:
    return kind in EVENT_KINDS


def _p(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else {}


def event_text(row: dict[str, Any]) -> str:
    """kind+payload → 一句中文展示文本（spec §2.2：中心单端拼装，前端/CLI 零加工）。

    兜底分支必须覆盖"payload 非 dict / kind 词表外"两类对端输入，且永不抛。
    """
    kind = str(row.get("kind") or "")
    p = _p(row)
    if kind == "goal.create":
        return f"创建目标（{p.get('profile', '?')} intent→{p.get('intent', '?')}；操作者 {p.get('operator', '-')}）"
    if kind == "goal.update":
        fields = ",".join(p.get("fields") or [])
        return f"更新目标（{p.get('profile', '?')} 字段 {fields or '-'}；操作者 {p.get('operator', '-')}）"
    if kind == "goal.delete":
        return f"撤销目标（{p.get('profile', '?')}；操作者 {p.get('operator', '-')}）"
    if kind == "goal.retry":
        return f"人工重试（排队={'是' if p.get('queued') else '否'}；操作者 {p.get('operator', '-')}）"
    if kind == "goal.drift":
        return "漂移：worker 本地 profile 与中心声明不一致"
    if kind == "action.result":
        ok = p.get("ok")
        verdict = "成功" if ok is True else ("失败" if ok is False else "未知")
        return f"指令回执（seq={p.get('seq', 0)} {verdict}）{str(p.get('detail', ''))[:120]}"
    if kind == "node.join":
        return "节点接入（WS hello）"
    if kind == "node.join_check":
        return f"join 预检（{p.get('result', '-')}）"
    if kind == "node.sync":
        return f"强制全量同步（操作者 {p.get('operator', '-')}）"
    if kind == "node.model_action":
        return f"远程指令 {p.get('verb', '-')}（排队={'是' if p.get('queued') else '否'}）"
    if kind == "token.rotate":
        scope = "join token" if p.get("scope") == "join" else "节点 token"
        return f"轮换 {scope}"
    if kind == "node.heartbeat":
        return "心跳"
    if kind == "node.disable":
        return "禁用节点" + ("（顺带断连）" if p.get("kicked") else "")
    if kind == "node.enable":
        return "解除禁用"
    if kind == "node.kick":
        return "主动踢除连接" + ("" if p.get("kicked") else "（当时无连接）")
    if kind == "node.retire":
        return f"节点退役（连带撤销 {p.get('removed_goals', 0)} 个目标）"
    if kind == "db.backup":
        return f"台账备份（{p.get('bytes', '-')} 字节）"
    if kind == "db.restore":
        return f"台账自备份恢复而来（源 {p.get('source', '-')}）"
    if kind == "goal.sync_overflow":
        return f"快照超限未下发（{p.get('bytes', '?')} > 上限 {p.get('limit', '?')}）"
    digest = " ".join(f"{k}={v}" for k, v in list(p.items())[:4])
    return f"{kind} {digest}".strip()
```

- [ ] **Step 4: store.py 两处改造**

```python
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
        ...  # 原 INSERT 不动，kind 换 safe_kind
```
（store.py 需新增 `from loguru import logger` 导入。）

```python
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
        ...  # 其余不变
```

- [ ] **Step 5: admin_cluster events 端点改造**（替换 L157-163 现函数；import events 模块）

```python
@router.get("/cluster/events")
async def cluster_events(node_id: str = Query(""), kind: str = Query(""),
                         limit: int = Query(100, ge=1, le=1000),
                         _base: None = Depends(require_auth)):
    """事件流定版（spec §2.2）：{ts,node_id,goal_id,kind,text}，ts/text 后端单端格式化。"""
    if (off := _disabled()) is not None:
        return off
    if kind and not events_mod.is_known_kind(kind):
        return _bad_request(f"未知事件类型 {kind!r}")
    rows = get_registry().store.recent_events(limit=limit, node_id=node_id or None,
                                              kind=kind or None)
    return {"events": [{"ts": _fmt_ts(r["ts"]), "node_id": r["node_id"],
                        "goal_id": r["goal_id"], "kind": r["kind"],
                        "text": events_mod.event_text(r)} for r in rows]}
```
（`from modelctl.core.cluster import events as events_mod`，避让局部变量名 `events`。）

- [ ] **Step 6: goals.py 拒 disabled**——`set_goals` 中 `candidates` 解析后、进 gate 前过滤改写：候选行的 disabled 检查**逐候选**产出 error verdict。实现要点：不新增 gate 函数，在 `set_goals` 组装 `candidates` 后把 disabled 候选从 `candidates` 移出、为其构造 `{"node_id": n, "ok": False, "reason": "节点已禁用，重新启用后才能下发"}` verdict 并入 `verdicts`/`errors` 计数（与 `evaluate_gate` 返回形状逐键一致，report 渲染零改动）。gate 本体零 diff（容量/卡位职责不掺治理）。

> 若构造 verdict 形状与 `gate.evaluate_gate` 实际返回键不完全一致，**以 `gate.py` 实际形状为准对齐**，禁私改 gate 测试。

- [ ] **Step 7: 本文件 + store 测试全绿**

`uv run --with fastapi --with httpx pytest tests/test_cluster_events_http.py tests/test_cluster_store.py -q`

- [ ] **Step 8: 21 文件全量回归**（同 Task 1 Step 7 命令 + 两个新文件）→ 437+ 不回退。

- [ ] **Step 9: 提交**（`feat(cluster): M2 事件流定版——EVENT_KINDS 守卫/单端拼装/kind 过滤/禁用节点拒下发`）。

---

### Task 4: 治理 CLI（`cluster node …` 五子命令）+ `cluster events`

CLI 是 REST 薄壳：只断言"发了什么请求/退出码/输出了什么"，不复测 core。`retire` 有二次确认（连带撤销语义不可逆）。

**Files:**
- Modify: `src/modelctl/cli.py`（`build_parser` cluster 段 + `_cmd_cluster` 分发 + 两个新 `_cmd_cluster_*`）
- Test: `tests/test_cluster_gov_cli.py`、`tests/test_cluster_events_cli.py`

**Interfaces:**
- Consumes: `_cluster_request`（四显式分支）、`_center_detail`、`_print_table`
- Produces:
  - `modelctl cluster node disable|enable|rotate-token|kick|retire --node <ID> [--yes]`（rotate 成功打印新 token + 更新指引；retire 先 `GET /cluster/goals?node_id=` 数 goals → 确认文案含 `removed_goals` 数 → `DELETE`；非交互/拒绝 → 退 2 不发 DELETE）
  - `modelctl cluster events [--node ID] [--kind K] [--limit N]` → `_print_table(["时间","节点","类型","描述"], …)`（`ts/text` 中心已格式化，零二次加工；query 值 `quote(v, safe='')`）
  - 退出码沿用 M1：HTTP≠200 → 2；成功 0。

- [ ] **Step 1: 写失败测试** — `tests/test_cluster_gov_cli.py`（复用 `test_cluster_goal_cli.py` 的 `_env`/`Recorder`/`probe` 夹具形状，整段照抄后按需增 `download_file` 打桩——Task 6 才用到，本 Task 不引）

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_gov_cli.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : cluster node 治理子命令（请求形状/退出码/确认闸门）
# ===============================================================================
from __future__ import annotations

import pytest

BASE = "http://center:4173"
NODES = "/admin/api/cluster/nodes"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CLUSTER_ROLE", "CLUSTER_CENTER_URL", "CLUSTER_NODE_ID", "CLUSTER_LAN",
              "CLUSTER_JOIN_TOKEN", "CLUSTER_NODE_TOKEN", "API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLUSTER_CENTER_URL", BASE)
    monkeypatch.setenv("API_KEY", "sk-cli")
    import modelctl.core.envfile as ef

    monkeypatch.setattr(ef, "PROJECT_ROOT", tmp_path)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._replies: dict[tuple[str, str], tuple[int, dict]] = {}

    def reply(self, method: str, path: str, status: int, body: dict) -> None:
        self._replies[(method, path)] = (status, body)

    def call(self, method, url, payload, api_key=""):
        self.calls.append((method, url, payload, api_key))
        for (m, path), out in self._replies.items():
            if m == method and url.endswith(path):
                return out
        return 200, {}

    def one(self, method: str):
        return [c for c in self.calls if c[0] == method]


@pytest.fixture()
def probe(monkeypatch):
    import modelctl.core.cluster.center_probe as cp

    rec = Recorder()
    monkeypatch.setattr(cp, "get_json",
                        lambda url, api_key="", timeout=5.0: rec.call("GET", url, None, api_key))
    monkeypatch.setattr(cp, "post_json",
                        lambda url, payload, api_key="", timeout=5.0: rec.call("POST", url, payload, api_key))
    monkeypatch.setattr(cp, "put_json",
                        lambda url, payload, api_key="", timeout=5.0: rec.call("PUT", url, payload, api_key))
    monkeypatch.setattr(cp, "delete_json",
                        lambda url, api_key="", timeout=5.0: rec.call("DELETE", url, None, api_key))
    return rec


def _main(argv):
    from modelctl import cli

    return cli.main(argv)


@pytest.mark.parametrize("act,path", [
    ("disable", "/disable"), ("enable", "/enable"),
    ("rotate-token", "/rotate-token"), ("kick", "/kick"),
])
def test_node_actions_post_expected_path(probe, act, path) -> None:
    assert _main(["cluster", "node", act, "--node", "w-1"]) == 0
    method, url, _body, key = probe.calls[0]
    assert (method, url) == ("POST", BASE + NODES + "/w-1" + path)
    assert key == "sk-cli"


def test_rotate_token_prints_new_token_and_hint(probe, capsys) -> None:
    probe.reply("POST", "/cluster/nodes/w-1/rotate-token", 200,
                {"node_token": "NT-new", "kicked": True, "hint": "更新 .env 后重启"})
    assert _main(["cluster", "node", "rotate-token", "--node", "w-1"]) == 0
    out = capsys.readouterr().out
    assert "NT-new" in out and "更新 .env 后重启" in out


def test_center_error_maps_exit_2(probe) -> None:
    probe.reply("POST", "/cluster/nodes/ghost/disable", 404, {"detail": "节点不存在"})
    assert _main(["cluster", "node", "disable", "--node", "ghost"]) == 2


def test_retire_requires_node_or_yes_first_counts_goals(probe, monkeypatch) -> None:
    probe.reply("GET", "/cluster/goals?node_id=w-1", 200,
                {"goals": [{"node_id": "w-1"}, {"node_id": "w-1"}]})
    # --yes 跳过交互：先数 goals 再 DELETE，DELETE 响应回带 removed_goals
    probe.reply("DELETE", "/cluster/nodes/w-1", 200, {"removed": True, "removed_goals": 2})
    assert _main(["cluster", "node", "retire", "--node", "w-1", "--yes"]) == 0
    assert probe.one("DELETE")
    methods = [c[0] for c in probe.calls]
    assert methods == ["GET", "DELETE"]        # 确认前只读，DELETE 最后发


def test_retire_declined_makes_no_delete(probe, monkeypatch) -> None:
    probe.reply("GET", "/cluster/goals?node_id=w-1", 200, {"goals": [{"node_id": "w-1"}]})
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert _main(["cluster", "node", "retire", "--node", "w-1"]) == 2
    assert probe.one("DELETE") == []


def test_node_requires_node_arg(probe) -> None:
    with pytest.raises(SystemExit):            # argparse required=True
        _main(["cluster", "node", "disable"])
    assert probe.calls == []
```

`tests/test_cluster_events_cli.py`（同夹具，断言不同）：

```python
def test_events_default_request(probe) -> None:
    probe.reply("GET", "/cluster/events?limit=50", 200,
                {"events": [{"ts": "2026-09-06 10:00:00", "node_id": "w-1",
                             "goal_id": None, "kind": "node.disable",
                             "text": "禁用节点"}]})
    assert _main(["cluster", "events"]) == 0
    method, url, _, _ = probe.calls[0]
    assert method == "GET" and "/cluster/events?" in url and "limit=50" in url


def test_events_filters_are_percent_encoded(probe) -> None:
    _main(["cluster", "events", "--node", "w 1", "--kind", "goal.update", "--limit", "5"])
    url = probe.calls[0][1]
    assert "node_id=w%201" in url and "kind=goal.update" in url and "limit=5" in url


def test_events_renders_center_formatted_fields(probe, capsys) -> None:
    probe.reply("GET", "/cluster/events", 200,
                {"events": [{"ts": "2026-09-06 10:00:00", "node_id": "w-1",
                             "goal_id": None, "kind": "node.retire",
                             "text": "节点退役（连带撤销 2 个目标）"}]})
    assert _main(["cluster", "events"]) == 0
    out = capsys.readouterr().out
    assert "2026-09-06 10:00:00" in out and "节点退役（连带撤销 2 个目标）" in out


def test_events_center_down_exit_2(probe) -> None:
    probe.reply("GET", "/cluster/events", -1, {"error": "connection refused"})
    assert _main(["cluster", "events"]) == 2
```

- [ ] **Step 2: 确认失败**（argparse 报 invalid choice）→ **Step 3: build_parser 增段**（cluster 段 `cy = csub.add_parser("sync", …)` 之后）

```python
    cn = csub.add_parser("node", help="节点治理：禁用/启用/轮换 token/踢除/退役（spec §2.1）")
    nsub = cn.add_subparsers(dest="node_action", required=True)
    for act, hlp in (("disable", "禁用：hello/join 拒；goal 台账不动"),
                     ("enable", "解除禁用：状态由下次 hello/心跳自然决定"),
                     ("rotate-token", "轮换节点 token：响应一次性给新值，须人工写该节点 .env 后重启"),
                     ("kick", "主动断连：worker 指数退避后自动重连"),
                     ("retire", "退役：级联撤销该节点全部 goal（二次确认）")):
        np = nsub.add_parser(act, help=hlp)
        np.add_argument("--node", required=True, metavar="NODE_ID")
        if act == "retire":
            np.add_argument("--yes", action="store_true", help="跳过二次确认（脚本/cron 用）")
    ce = csub.add_parser("events", help="集群事件流（读中心 events 台账，中心已格式化）")
    ce.add_argument("--node", default="", metavar="NODE_ID", help="按节点过滤")
    ce.add_argument("--kind", default="", metavar="KIND", help="事件类型过滤（非法值中心 400）")
    ce.add_argument("--limit", type=int, default=50, help="条数上限 1..1000（默认 50）")
```

- [ ] **Step 4: 实现两个 handler + 分发**（`_cmd_cluster` 的 if 链补 `if args.action == "node": return _cmd_cluster_node(args)` / `if args.action == "events": return _cmd_cluster_events(args)`；handler 置于 `_cmd_cluster_sync` 之后）

```python
def _confirm_or_yes(args, question: str) -> bool:
    """破坏性操作闸门：--yes 直通；交互 y/Y 确认；EOF（非交互终端）拒绝。"""
    if getattr(args, "yes", False):
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _cmd_cluster_node(args) -> int:
    from urllib.parse import quote

    node = quote(args.node, safe="")
    act = args.node_action
    if act == "retire":
        # 确认文案必须带真实连带数：先查该节点 goals（查不到按 0 并报提示，不阻断退役）
        gs, gb = _cluster_request("GET", f"/cluster/goals?node_id={quote(args.node, safe='')}")
        n_goals = len(gb.get("goals", [])) if gs == 200 else 0
        if not _confirm_or_yes(args, f"退役节点 {args.node}：将连带撤销 {n_goals} 个托管目标"
                                     f"（不可恢复，events 审计保留），确认？"):
            print("已取消")
            return 2
        status, body = _cluster_request("DELETE", f"/cluster/nodes/{node}")
        if status != 200:
            logger.error(f"退役失败 {args.node}: {_center_detail(status, body)}")
            return 2
        print(f"节点 {args.node} 已退役（连带撤销 {body.get('removed_goals', 0)} 个目标）")
        return 0
    status, body = _cluster_request("POST", f"/cluster/nodes/{node}/{act}")
    if status != 200:
        logger.error(f"操作失败 {act} {args.node}: {_center_detail(status, body)}")
        return 2
    if act == "rotate-token":
        print(f"新 node_token: {body.get('node_token', '')}")
        print(f"注意: {body.get('hint', '')}")
        print("旧 token 立即失效" + ("（已顺带断开当前连接）" if body.get("kicked") else ""))
    elif act in ("disable", "kick"):
        verb = "禁用" if act == "disable" else "踢除"
        print(f"已{verb} {args.node}" + ("（顺带断开当前连接）" if body.get("kicked") else "（当时无连接）"))
    else:
        print(f"已解除禁用 {args.node}（状态将由其下次 hello/心跳决定）")
    return 0


def _cmd_cluster_events(args) -> int:
    from urllib.parse import quote

    pairs = (("node_id", args.node), ("kind", args.kind), ("limit", str(args.limit)))
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in pairs if v)
    status, body = _cluster_request("GET", "/cluster/events" + (f"?{query}" if query else ""))
    if status != 200:
        logger.error(f"事件查询失败: {_center_detail(status, body)}")
        return 2
    rows = [[e.get("ts", ""), e.get("node_id") or "-", e.get("kind", ""), e.get("text", "")]
            for e in body.get("events", [])]
    _print_table(["时间", "节点", "类型", "描述"], rows, dim_indices=(2,))
    return 0
```

- [ ] **Step 5**: 两新文件全绿 → Step 6: 21 文件全量回归不回退 → Step 7: 提交（`feat(cluster): M2 治理 CLI——cluster node 五子命令 + cluster events`）。

---

### Task 5: `core/cluster/backup.py`——热备 / 校验 / 恢复（restore 判"中心未运行"）

备份用 `sqlite3` backup API（在线热备不阻写）；restore 只在中心停机时执行且先自动就近备份，事件写"恢复后的新库"（spec §2.5 裁决）。

**Files:**
- Create: `src/modelctl/core/cluster/backup.py`
- Test: `tests/test_cluster_backup.py`（纯 core，无 fastapi）

**Interfaces:**
- Consumes: `ClusterStore(db_path).db_path`、`core.process.is_running`、`core.webui.server.WEBUI_INSTANCE`、`events.EVENT_KINDS` 的 `db.backup/db.restore`
- Produces:
  - `class BackupError(Exception)`——CLI/REST 统一转退出码 2 / 400
  - `create_backup(dest: Path, *, force: bool = False) -> dict{"sha256": str, "bytes": int}`——源 = `ClusterStore().db_path`；dest 父目录不存在 → BackupError；同名存在且非 force → BackupError；先写 `dest.tmp` 再 `replace`（不留半成品）
  - `verify_backup(src: Path) -> tuple[bool, str]`——可打开 + `PRAGMA integrity_check` 为 ok + 必备表 `{nodes, goals, model_states, events, meta}` 齐备；`meta.join_token` 缺失**仅告警不硬失败**（老备份可能无）；返回 (ok, reason)
  - `restore_backup(src: Path, *, assume_stopped: bool = False) -> Path`——verify 不过 → BackupError；中心运行中 → BackupError（`is_running(WEBUI_INSTANCE)`，`assume_stopped=True` 仅供测试注入）；通过 → 当前库就近复制 `<db>.pre-restore.<UTC ts>.bak` → src 覆盖当前库（含清 `-wal/-shm`）→ **对新库** append `db.restore` 事件（payload `{"source": str(src)}`）→ 返回 .bak 路径

- [ ] **Step 1: 写失败测试** — `tests/test_cluster_backup.py`

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_backup.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : 备份三函数：热备对账 / 校验拒坏档 / 恢复往返 + 事件落新库
# ===============================================================================
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from modelctl.core.cluster.backup import BackupError, create_backup, restore_backup, verify_backup
from modelctl.core.cluster.store import ClusterStore


@pytest.fixture()
def live(tmp_path, monkeypatch):
    db = tmp_path / "cluster-meta.db"
    monkeypatch.setattr("modelctl.core.cluster.store.cache_dir", lambda: tmp_path, raising=False)
    s = ClusterStore(db)
    s.init_db()
    s.upsert_node(node_id="w-1", node_token="NT-1", lan_id="lan-1", role="worker",
                  host_ip="", hostname="", engines=None, now=100.0)
    s.set_meta("join_token", "JT-x")
    return s, db


def test_create_backup_roundtrip_sha(live, tmp_path):
    _, db = live
    dest = tmp_path / "bk" / "m.db"
    dest.parent.mkdir()
    out = create_backup(dest)
    assert out["bytes"] == dest.stat().st_size > 0
    assert out["sha256"] == hashlib.sha256(dest.read_bytes()).hexdigest()


def test_create_backup_rejects_existing_without_force(live, tmp_path):
    _, db = live
    dest = tmp_path / "m.db"
    create_backup(dest)
    with pytest.raises(BackupError):
        create_backup(dest)
    create_backup(dest, force=True)          # force 覆盖成功


def test_create_backup_missing_parent_dir_errors(live, tmp_path):
    _, db = live
    with pytest.raises(BackupError):
        create_backup(tmp_path / "nope" / "m.db")


def test_verify_accepts_good_backup(live, tmp_path):
    _, db = live
    dest = tmp_path / "m.db"
    create_backup(dest)
    ok, reason = verify_backup(dest)
    assert ok, reason


def test_verify_rejects_non_db_and_missing_tables(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite file at all")
    ok, reason = verify_backup(bad)
    assert not ok and reason

    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).execute("CREATE TABLE x (a int)").execute("COMMIT")
    ok, reason = verify_backup(empty)
    assert not ok and "nodes" in reason


def test_verify_warns_but_passes_without_join_token(live, tmp_path):
    s, db = live
    s.set_meta("join_token", "")
    dest = tmp_path / "m.db"
    create_backup(dest)
    conn = sqlite3.connect(str(dest))
    conn.execute("DELETE FROM meta WHERE key='join_token'")
    conn.commit()
    conn.close()
    ok, _ = verify_backup(dest)
    assert ok                                  # 仅告警不硬失败


def test_restore_refuses_when_center_running(live, tmp_path, monkeypatch):
    s, db = live
    dest = tmp_path / "m.db"
    create_backup(dest)
    monkeypatch.setattr("modelctl.core.cluster.backup._center_running", lambda: True)
    with pytest.raises(BackupError):
        restore_backup(dest)


def test_restore_replaces_and_writes_event_into_new_db(live, tmp_path, monkeypatch):
    s, db = live
    dest = tmp_path / "m.db"
    create_backup(dest)
    # 备份之后再写一条 goal——恢复后它必须"消失"（对拍语义）
    s.upsert_goal(goal_id="q@@w-1", node_id="w-1", profile="q", engine="vllm",
                  profile_yaml="port: 1\n", profile_sha="s", profile_version=None,
                  intent="start", params=None, env_overlay=None, placement=None,
                  runtime_ref=None, target_role="primary", stage="READY",
                  created_by="op", now=200.0)
    monkeypatch.setattr("modelctl.core.cluster.backup._center_running", lambda: False)
    bak = restore_backup(dest)
    assert bak.exists() and ".pre-restore." in bak.name

    after = ClusterStore(db)
    after.init_db()
    assert after.list_goals() == []            # 恢复回备份时刻
    assert after.get_node("w-1") is not None   # 备份时刻已有的数据在
    kinds = [e["kind"] for e in after.recent_events()]
    assert "db.restore" in kinds               # 事件写恢复后的新库（spec §1 裁决）


def test_restore_refuses_corrupt_source(live, tmp_path, monkeypatch):
    s, db = live
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"garbage")
    monkeypatch.setattr("modelctl.core.cluster.backup._center_running", lambda: False)
    with pytest.raises(BackupError):
        restore_backup(bad)
    assert ClusterStore(db).get_node("w-1") is not None   # 现库未被碰
```

- [ ] **Step 2: 确认失败** → **Step 3: 实现 `backup.py`**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/backup.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : 中心台账热备/校验/恢复（M2 spec §2.5，零 DDL）
# ===============================================================================

"""core/cluster/backup.py — sqlite backup API 热备 + 校验 + 停机恢复。

restore 前置"中心未运行"是本模块的安全边界：运行中的 webui 进程持着 SQLite 连接，
在线替换其底层文件 = 让进程对着被抽换的 inode 继续写 WAL，损坏是必然不是偶然。
因此 restore 仅 CLI 可达（无 REST 端点）；_center_running 单独成函数供测试注入。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from loguru import logger

_REQUIRED_TABLES = frozenset({"nodes", "goals", "model_states", "events", "meta"})


class BackupError(Exception):
    """备份面业务失败：CLI 转退出码 2，REST 转 400。"""


def _live_db_path() -> Path:
    from modelctl.core.cluster.store import ClusterStore

    return ClusterStore().db_path


def _center_running() -> bool:
    from modelctl.core.process import is_running
    from modelctl.core.webui.server import WEBUI_INSTANCE

    return is_running(WEBUI_INSTANCE)


def create_backup(dest: Path, *, force: bool = False) -> dict[str, Any]:
    """当前台账 → dest 的一致性热备（在线可执行，不阻写）。返回 {sha256, bytes}。"""
    dest = Path(dest)
    if not dest.parent.is_dir():
        raise BackupError(f"目标目录不存在: {dest.parent}")
    if dest.exists() and not force:
        raise BackupError(f"目标已存在（覆盖需 --force）: {dest}")
    src = _live_db_path()
    if not src.is_file():
        raise BackupError(f"台账不存在，请先 cluster init: {src}")
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with sqlite3.connect(str(src)) as src_conn, sqlite3.connect(str(tmp)) as dst_conn:
            src_conn.backup(dst_conn)          # online backup API：读锁不阻写者
        data = tmp.read_bytes()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.replace(dest)
    except sqlite3.Error as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"备份失败: {exc}") from exc
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def verify_backup(src: Path) -> tuple[bool, str]:
    """可打开 + integrity_check ok + 必备表齐备。join_token 缺失仅告警（老备份可能无）。"""
    src = Path(src)
    if not src.is_file():
        return False, f"文件不存在: {src}"
    try:
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return False, f"无法打开: {exc}"
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or str(row[0]).lower() != "ok":
            return False, f"integrity_check 失败: {row[0] if row else '无结果'}"
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = _REQUIRED_TABLES - have
        if missing:
            return False, f"缺必备表: {sorted(missing)}"
        jt = conn.execute("SELECT value FROM meta WHERE key='join_token'").fetchone()
        if jt is None:
            logger.warning("备份无 join_token（老备份可接受；下次备份后消失）")
        return True, ""
    except sqlite3.Error as exc:
        return False, f"校验失败: {exc}"
    finally:
        conn.close()


def restore_backup(src: Path, *, assume_stopped: bool = False) -> Path:
    """校验通过 → 现库就近 .bak → 文件替换（清 -wal/-shm）→ 事件写新库。返回 .bak 路径。"""
    src = Path(src)
    ok, reason = verify_backup(src)
    if not ok:
        raise BackupError(f"备份校验失败，现库未做任何改动: {reason}")
    if not assume_stopped and _center_running():
        raise BackupError("中心 webui 运行中：请先 modelctl webui stop 再恢复")
    db = _live_db_path()
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = db.with_name(db.name + f".pre-restore.{ts}.bak")
    if db.is_file():
        shutil.copyfile(db, bak)
    for suffix in ("-wal", "-shm"):           # 陈旧 WAL/SHM 配新主文件 = 日志回放进错库
        side = db.with_name(db.name + suffix)
        side.unlink(missing_ok=True)
    shutil.copyfile(src, db)
    from modelctl.core.cluster.store import ClusterStore

    store = ClusterStore(db)
    store.init_db()                            # 老备份缺 M1 列时经幂等补列（只增不删）
    store.append_event("db.restore", payload={"source": str(src)})
    return bak
```

- [ ] **Step 4**: `uv run pytest tests/test_cluster_backup.py -q` 全绿。
- [ ] **Step 5**: 21 文件全量回归 + 本文件不回退。
- [ ] **Step 6**: 提交（`feat(cluster): M2 备份 core——热备/校验/停机恢复（事件落新库）`）。

---

### Task 6: 备份全链接线——`GET /cluster/backup` 下载端点 + `center_probe.download_file` + `cluster backup/restore` CLI

REST 只做"调 backup.create_backup → FileResponse + X-Backup-Sha256 + db.backup 事件"；CLI `backup` 走下载 + 本地 sha 复核对账，`restore` **不走 REST** 直调 core。

**Files:**
- Modify: `src/modelctl/core/webui/admin_cluster.py`、`src/modelctl/core/cluster/center_probe.py`、`src/modelctl/cli.py`
- Test: `tests/test_cluster_backup.py`（追加 CLI 段）、`tests/test_cluster_governance_http.py`（追加下载端点段）

**Interfaces:**
- Consumes: Task 5 三函数、`_cluster_request` 旁路（下载需原始字节，不能走 JSON 折叠）
- Produces:
  - `GET /cluster/backup` → `FileResponse`（附件名 `modelctl-cluster-<YYYYMMDD-HHMMSS>.db`）+ 响应头 `X-Backup-Sha256`；写 `db.backup` 事件（只记 bytes，不记内容）；临时文件经 `BackgroundTask` 删除
  - `center_probe.download_file(url, dest, api_key="", timeout=60.0) -> tuple[int, dict]`——成功 `(200, {"sha256", "header_sha256", "bytes"})`；HTTP 错误 `(code, {"detail"...})`；网络折叠 `(-1, {"error"})`
  - `cluster backup --to <path> [--force]` → 下载 + `本地 sha == X-Backup-Sha256` 对账，不符退 2 并删落盘文件
  - `cluster restore --from <path> [--yes]` → 交互确认后直调 `restore_backup`；打印 .bak 路径

- [ ] **Step 1: 写失败测试**（追加到两个既有测试文件尾部，勿新建）

`tests/test_cluster_governance_http.py` 追加：

```python
# ---------------- GET /cluster/backup（Task 6）----------------
def test_backup_download_streams_and_records_event(center):
    r = center.get("/admin/api/cluster/backup", headers=_h())
    assert r.status_code == 200
    sha_header = r.headers.get("x-backup-sha256", "")
    import hashlib
    assert sha_header == hashlib.sha256(r.content).hexdigest()
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "modelctl-cluster-" in r.headers.get("content-disposition", "")
    kinds = [e["kind"] for e in _store().recent_events()]
    assert "db.backup" in kinds


def test_backup_download_requires_auth(center):
    assert center.get("/admin/api/cluster/backup").status_code == 401
```

`tests/test_cluster_backup.py` 追加 CLI 段（Recorder 打桩沿用 goal_cli 形状，此处需 patch `download_file`）：

```python
# ---------------- CLI 接线（probe 打桩）----------------
BASE = "http://center:4173"


@pytest.fixture()
def cli_env(monkeypatch, tmp_path):
    for k in ("CLUSTER_ROLE", "CLUSTER_CENTER_URL", "CLUSTER_NODE_ID", "CLUSTER_LAN",
              "CLUSTER_JOIN_TOKEN", "CLUSTER_NODE_TOKEN", "API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLUSTER_CENTER_URL", BASE)
    monkeypatch.setenv("API_KEY", "sk-cli")
    import modelctl.core.envfile as ef

    monkeypatch.setattr(ef, "PROJECT_ROOT", tmp_path)


def _main(argv):
    from modelctl import cli

    return cli.main(argv)


def test_cli_backup_downloads_and_verifies_sha(monkeypatch, tmp_path, capsys):
    from modelctl.core.cluster import center_probe

    payload = b"fake-sqlite-bytes"
    import hashlib
    sha = hashlib.sha256(payload).hexdigest()

    def fake_download(url, dest, api_key="", timeout=60.0):
        assert url.startswith(BASE + "/admin/api/cluster/backup")
        dest.write_bytes(payload)
        return 200, {"sha256": sha, "header_sha256": sha, "bytes": len(payload)}

    monkeypatch.setattr(center_probe, "download_file", fake_download)
    dest = tmp_path / "bk.db"
    assert _main(["cluster", "backup", "--to", str(dest)]) == 0
    assert dest.read_bytes() == payload
    assert sha[:12] in capsys.readouterr().out


def test_cli_backup_sha_mismatch_exit2_and_removes_file(monkeypatch, tmp_path):
    from modelctl.core.cluster import center_probe

    def fake_download(url, dest, api_key="", timeout=60.0):
        dest.write_bytes(b"x")
        return 200, {"sha256": "0" * 64, "header_sha256": "f" * 64, "bytes": 1}

    monkeypatch.setattr(center_probe, "download_file", fake_download)
    dest = tmp_path / "bk.db"
    assert _main(["cluster", "backup", "--to", str(dest)]) == 2
    assert not dest.exists()                   # 对账失败的文件不留


def test_cli_restore_yes_calls_core_restore(monkeypatch, tmp_path):
    calls = {}

    def fake_restore(src, *, assume_stopped=False):
        calls["src"] = src
        from pathlib import Path

        return Path(str(src) + ".bak")

    monkeypatch.setattr("modelctl.core.cluster.backup.restore_backup", fake_restore)
    src = tmp_path / "bk.db"
    src.write_bytes(b"x")
    assert _main(["cluster", "restore", "--from", str(src), "--yes"]) == 0
    assert calls["src"] == src


def test_cli_restore_declined_no_call(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr("modelctl.core.cluster.backup.restore_backup",
                        lambda *a, **k: called.append(1))
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    src = tmp_path / "bk.db"
    src.write_bytes(b"x")
    assert _main(["cluster", "restore", "--from", str(src)]) == 2
    assert called == []
```

- [ ] **Step 2: 确认失败** → **Step 3: admin_cluster 端点**

```python
@router.get("/cluster/backup")
async def cluster_backup_download(_base: None = Depends(require_auth),
                                  background: BackgroundTasks = None):  # 见下注
    """台账热备下载（spec §2.1）：FileResponse 附件 + X-Backup-Sha256 + db.backup 事件。

    备份含 node_token/join_token 明文——require_auth 管理员域（总 spec §11 信任模型），
    事件只记动作与字节数，永不记内容。
    """
    if (off := _disabled()) is not None:
        return off
    import tempfile

    from fastapi.responses import FileResponse

    tmp_dir = Path(tempfile.mkdtemp(prefix="modelctl-backup-"))
    fname = f"modelctl-cluster-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    dest = tmp_dir / fname
    try:
        out = backup.create_backup(dest)
    except backup.BackupError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return _bad_request(str(exc))
    get_registry().store.append_event("db.backup",
                                      payload={"bytes": out["bytes"], "operator": "api"},
                                      now=time.time())
    background.add_task(shutil.rmtree, tmp_dir, True)
    return FileResponse(path=str(dest), filename=fname,
                        headers={"X-Backup-Sha256": out["sha256"]})
```
> FastAPI 注入写法以仓库 FastAPI 版本为准：`background: BackgroundTasks`（默认参数行删掉，直接形参 `background: BackgroundTasks`）。新增导入：`import shutil`、`from pathlib import Path`、`from fastapi import BackgroundTasks`、`from modelctl.core.cluster import backup`。

**Step 4: center_probe.download_file**（文件头已声明"stdlib urllib"，保持一致）：

```python
def download_file(url: str, dest, api_key: str = "", timeout: float = 60.0) -> tuple[int, dict]:
    """GET 二进制落盘 + sha 对账素材：(status, {"sha256","header_sha256","bytes"})。

    专给 `cluster backup`：JSON 折叠的 _request 会毁掉 SQLite 字节，下载必须独立通道。
    写盘失败（权限/磁盘满）折叠为 (-1, {"error"})——与网络故障同一处置面。
    """
    import hashlib
    import pathlib

    try:
        req = urllib.request.Request(url, method="GET")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            data = resp.read()
            header_sha = str(resp.headers.get("X-Backup-Sha256", ""))
        pathlib.Path(dest).write_bytes(data)
        return resp.status, {"sha256": hashlib.sha256(data).hexdigest(),
                             "header_sha256": header_sha, "bytes": len(data)}
    except urllib.error.HTTPError as exc:
        return exc.code, _safe_json(exc.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return -1, {"error": str(exc)}
```

**Step 5: CLI**——parser（cluster 段）+ 分发 + handler：

```python
    cb = csub.add_parser("backup", help="下载中心台账热备份（含 sha 对账）")
    cb.add_argument("--to", required=True, metavar="PATH", help="落盘路径")
    cb.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    cr = csub.add_parser("restore", help="从备份恢复中心台账（须先停中心；不走 REST）")
    cr.add_argument("--from", dest="src", required=True, metavar="PATH")
    cr.add_argument("--yes", action="store_true", help="跳过二次确认")
```

```python
def _cmd_cluster_backup(args) -> int:
    from pathlib import Path as _P

    from modelctl.core.cluster import center_probe

    dest = _P(args.to)
    if dest.exists() and not args.force:
        logger.error(f"目标已存在（覆盖加 --force）: {dest}")
        return 2
    url = f"{_cluster_center_base()}/admin/api/cluster/backup"
    status, info = center_probe.download_file(url, dest, api_key=_cluster_api_key())
    if status != 200:
        logger.error(f"备份下载失败: {_center_detail(status, info)}")
        return 2
    if not info.get("header_sha256") or info["sha256"] != info["header_sha256"]:
        dest.unlink(missing_ok=True)
        logger.error(f"sha 对账不符（本地 {info['sha256'][:12]}… != 中心 "
                     f"{str(info.get('header_sha256'))[:12]}…），已删除落盘文件")
        return 2
    print(f"备份就绪: {dest}（{info['bytes']} 字节, sha256 {info['sha256'][:12]}…）")
    return 0


def _cmd_cluster_restore(args) -> int:
    from pathlib import Path as _P

    from modelctl.core.cluster import backup

    src = _P(args.src)
    if not src.is_file():
        logger.error(f"备份文件不存在: {src}")
        return 2
    if not _confirm_or_yes(args, f"将用 {src} 覆盖中心台账（现库自动留 .pre-restore.bak），确认？"):
        print("已取消")
        return 2
    try:
        bak = backup.restore_backup(src)
    except backup.BackupError as exc:
        logger.error(f"恢复失败: {exc}")
        return 2
    print(f"恢复完成（恢复前状态: {bak}）")
    print("提醒: 中心已停机的话现在 modelctl webui start 即加载新库")
    return 0
```
（`_cmd_cluster` if 链补两分支。`--from` 用 `dest="src"`。）

- [ ] **Step 6**: `uv run --with fastapi --with httpx pytest tests/test_cluster_backup.py tests/test_cluster_governance_http.py -q` 全绿 → 全量回归 → 提交（`feat(cluster): M2 备份链路——下载端点+download_file+cluster backup/restore`）。

---

### Task 7: `GET /cluster/profiles` + `GET /cluster/settings` 只读端点（前端弹窗与设置块数据源）

profiles 是 goal 新建弹窗数据源（同名多引擎逐条列出，选边交给 POST gate，前端不重复实现）；settings 定版新端点承载角色/中心地址/interval/lease 现值（不污染 `/cluster/status`，零写端点）。

**Files:**
- Modify: `src/modelctl/core/cluster/profiles.py`（新公共函数）、`src/modelctl/core/webui/admin_cluster.py`
- Test: `tests/test_cluster_governance_http.py`（追加两段）

**Interfaces:**
- Consumes: `profiles` 模块既有私有助手（`_load_candidate`/`display_name_of`）、`config.*`、`store.get_meta("join_token")`、`store.mask_tail`
- Produces:
  - `profiles.list_profile_catalog(models_dir: Path | None = None) -> list[dict]`——`[{name, engine, display_name, version}]`，同名多引擎逐条；非法名/超限/读败文件**跳过**（目录扫描面绝不抛）
  - `GET /cluster/profiles` → `{"profiles": [...]}`
  - `GET /cluster/settings` → `{"role", "center_url", "heartbeat_interval_s", "lease_s", "reconcile_interval_s", "max_snapshot_bytes", "join_token_mask"}`

> `config.max_snapshot_bytes()` 在**本 Task** 落地（settings 响应键依赖），Task 8 只做 `snapshot_for` 侧的消费封顶。

- [ ] **Step 1: 写失败测试** — `tests/test_cluster_governance_http.py` 追加两段 + `tests/test_cluster_config.py` 追加一条

`tests/test_cluster_governance_http.py` 追加（`center`/`_h()` 夹具沿用本文件 Task 2 已建版本）：

```python
# ---------------- GET /cluster/profiles（Task 7）----------------
def _models_with_dup_engine(tmp_path):
    """vllm/sglang 同名 qwen.yaml（逐条列出的判据）+ 一份坏文件（跳过判据）。"""
    root = tmp_path / "models"
    (root / "vllm").mkdir(parents=True, exist_ok=True)
    (root / "sglang").mkdir(parents=True, exist_ok=True)
    (root / "vllm" / "qwen.yaml").write_text("port: 8001\n", encoding="utf-8")
    (root / "sglang" / "qwen.yaml").write_text("port: 8002\n", encoding="utf-8")
    (root / "vllm" / "broken.yaml").write_text("port: not-a-port\n", encoding="utf-8")
    return root


def test_profiles_catalog_lists_same_name_per_engine(center, monkeypatch, tmp_path):
    import modelctl.core.cluster.goals as goals_mod

    monkeypatch.setattr(goals_mod, "MODELS_DIR", _models_with_dup_engine(tmp_path))
    r = center.get("/admin/api/cluster/profiles", headers=_h())
    assert r.status_code == 200
    rows = r.json()["profiles"]
    assert ("qwen", "vllm") in [(p["name"], p["engine"]) for p in rows]
    assert ("qwen", "sglang") in [(p["name"], p["engine"]) for p in rows]
    assert all(p["name"] != "broken" for p in rows)      # 坏文件静默跳过，绝不 500
    for p in rows:
        assert p["display_name"] and p["version"][:10].count("-") == 2   # 版本号=日期前缀


def test_profiles_requires_auth(center):
    assert center.get("/admin/api/cluster/profiles").status_code == 401


# ---------------- GET /cluster/settings（Task 7）----------------
def test_settings_readonly_shape(center, monkeypatch):
    from modelctl.core.cluster import config

    import modelctl.core.webui.admin_cluster as ac

    monkeypatch.setenv("CLUSTER_CENTER_URL", "http://10.0.0.9:4173")
    ac.get_registry().ensure_join_token()
    body = center.get("/admin/api/cluster/settings", headers=_h()).json()
    assert body["role"] == "both"
    assert body["center_url"] == "http://10.0.0.9:4173"
    # 数值键与 config 现值逐一相等（同一 env 源；不锁死默认值，避免 env 残留假红）
    assert body["heartbeat_interval_s"] == config.heartbeat_interval_s()
    assert body["lease_s"] == config.lease_s()
    assert body["max_snapshot_bytes"] == config.max_snapshot_bytes()
    assert body["join_token_mask"].startswith("***")      # 明文永不外泄


def test_settings_requires_auth(center):
    assert center.get("/admin/api/cluster/settings").status_code == 401
```

`tests/test_cluster_config.py` 追加（并把 `"CLUSTER_MAX_SNAPSHOT_BYTES"` 加进文件顶部 `CLUSTER_KEYS`，防残留 env 串测）：

```python
def test_max_snapshot_bytes_default_override_floor(monkeypatch) -> None:
    assert config.max_snapshot_bytes() == 8 * 1024 * 1024          # 默认 8 MiB
    monkeypatch.setenv("CLUSTER_MAX_SNAPSHOT_BYTES", "1048576")
    assert config.max_snapshot_bytes() == 1048576
    monkeypatch.setenv("CLUSTER_MAX_SNAPSHOT_BYTES", "1024")       # < floor 64 KiB → 回默认
    assert config.max_snapshot_bytes() == 8 * 1024 * 1024
```

- [ ] **Step 2: 确认失败**（profiles/settings 404、`config` 无属性）→ **Step 3: 实现 `profiles.list_profile_catalog`**（文件尾追加）

```python
def list_profile_catalog(models_dir: Path | None = None) -> list[dict[str, Any]]:
    """goal 新建弹窗的 profile 目录：同名多引擎**逐条列出**（选边是 POST gate 的事）。

    纯展示面：校验失败的文件**静默跳过**（与 `_resolve` 展示名回退同口径）——目录里
    一份坏 YAML 不该让 GET 500；"能不能下发"由 POST /goals 的 gate 逐项裁决，
    本函数绝不重复实现选边/歧义逻辑（单一裁决点）。
    """
    root = models_dir or PROJECT_ROOT / "models"
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in _root_first(_scan(root)[0], root):
        got = _load_candidate(path)
        if isinstance(got, str):
            continue
        engine, raw, text = got
        sha = profile_sha(text)
        out.append({"name": path.stem, "engine": engine,
                    "display_name": display_name_of(raw, path, engine),
                    "version": default_profile_version(sha)})
    return out
```

**Step 4: 实现 `config.max_snapshot_bytes`**（文件尾追加，沿用 `_int_env` 模式）

```python
_DEFAULT_MAX_SNAPSHOT_B = 8 * 1024 * 1024


def max_snapshot_bytes() -> int:
    """单节点 goal 快照总量封顶（spec §2.4）；floor 64 KiB 防误配把 sync 整体禁掉。"""
    return _int_env("CLUSTER_MAX_SNAPSHOT_BYTES", _DEFAULT_MAX_SNAPSHOT_B, floor=64 * 1024)
```

**Step 5: 实现两端点**（`admin_cluster.py`，插在 `cluster_export` 之前；导入行把 `from modelctl.core.cluster.store import ClusterStore` 扩为 `..., mask_tail`）

```python
# ================================ 只读目录/设置（M2，spec §2.3/§4.3）================================
@router.get("/cluster/profiles")
async def cluster_profiles(_base: None = Depends(require_auth)):
    """profile 目录（goal 弹窗数据源）：只呈现不裁决，选边留在 POST gate。"""
    if (off := _disabled()) is not None:
        return off
    from modelctl.core.cluster import goals as goals_module
    from modelctl.core.cluster import profiles

    return {"profiles": profiles.list_profile_catalog(goals_module.MODELS_DIR)}


@router.get("/cluster/settings")
async def cluster_settings(_base: None = Depends(require_auth)):
    """集群配置只读展示（spec §4.3 定版新端点）：零写端点；join token 仅脱敏出参。"""
    if (off := _disabled()) is not None:
        return off
    join = get_registry().store.get_meta("join_token")
    return {"role": config.cluster_role(), "center_url": config.center_url(),
            "heartbeat_interval_s": config.heartbeat_interval_s(), "lease_s": config.lease_s(),
            "reconcile_interval_s": config.reconcile_interval_s(),
            "max_snapshot_bytes": config.max_snapshot_bytes(),
            "join_token_mask": mask_tail(join)}
```

> settings 刻意**不回带** `join_token` 明文与 `CLUSTER_NODE_TOKEN`；`center_url` 非敏感（worker 侧本就配着中心地址）。

- [ ] **Step 6**: 单文件验证 `uv run --with fastapi --with httpx pytest tests/test_cluster_governance_http.py -q` 全绿；`uv run pytest tests/test_cluster_config.py -q` 全绿。
- [ ] **Step 7**: 全量回归（既有 21 文件 + M2 新测试文件显式清单，overlay，禁通配符）——437 + M2 新增全绿不回退。
- [ ] **Step 8**: 提交（`feat(cluster): M2 profiles 目录/settings 只读端点 + max_snapshot_bytes 配置`）。

---

### Task 8: `status/list --cluster` 中心聚合 + snapshot 尺寸封顶（spec §2.4/§3、终审 A-4/T2-①）

两件事互不依赖但都是"中心面收口"：CLI 聚合走中心 REST（中心不可达**退 2 绝不回退本机视图**——静默回退=假报数据源）；`snapshot_for` 总量超 `max_snapshot_bytes` → **不下发** + `goal.sync_overflow` 事件 + logger.error（不静默截断，半套快照比不下发更危险）。

**Files:**
- Modify: `src/modelctl/cli.py`（`--cluster` flag + main 分发短路 + 两个聚合 handler）、`src/modelctl/core/cluster/goals.py`（snapshot_for 封顶）、`src/modelctl/core/cluster/nodes.py`（ack 捎带尊重 overflow）、`.env.example`
- Test: `tests/test_cluster_agg_cli.py`（新）、`tests/test_cluster_goals.py`（追加 3 条）、`tests/test_cluster_ingest.py`（追加 1 条）

**Interfaces:**
- Consumes: Task 7 的 `config.max_snapshot_bytes()`；`events.EVENT_KINDS` 含 `goal.sync_overflow`（Task 3 已入词表）
- Produces:
  - `snapshot_for(node_id) -> dict`——**仅快照存在时**的超形状新增 `"sync_overflow": bool`；空快照分支返回形状**零变更**（既有 `== {"revision": "", "goals": []}` 严格断言不破）；revision/goals 仍回带（观测面），消费方由 `sync_overflow` 决定是否下发
  - `handle_heartbeat` 的 ack：`sync_overflow` 为真 → **不带 `sync` 段**、不写 `last_goal_sync_sha`
  - `status --cluster` / `list --cluster`（子命令各自 flag）——数据源 `GET /cluster/nodes` + `GET /cluster/goals`；HTTP≠200 → 2
  - 分发**短路于 `caps = probe()` 之前**：纯中心机器可能没有本地引擎环境，聚合视图不该被硬件探测拦住

- [ ] **Step 1: 写失败测试**

`tests/test_cluster_goals.py` 追加（沿用本文件既有 `store`/`svc` fixtures 与 `_online` 助手）：

```python
# ---------------- snapshot 尺寸封顶（M2 Task 8，spec §2.4）----------------
def test_snapshot_overflow_refuses_delivery_and_events(store, svc, monkeypatch):
    """超限判据基于 canonical JSON 字节数：夹具 YAML 只有几十字节，必须人为灌大
    profile_yaml 才能越限（update_goal 允许改 profile_yaml，_GOAL_MUTABLE 在列）。"""
    monkeypatch.setenv("CLUSTER_MAX_SNAPSHOT_BYTES", "65536")   # floor 64 KiB
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    store.update_goal("qwen@@w-1", now=2.0, profile_yaml="x" * 70000)
    kinds_before = [e["kind"] for e in store.recent_events(node_id="w-1")]
    snap = svc.snapshot_for("w-1")
    assert snap["sync_overflow"] is True
    assert snap["revision"]                                      # 诊断面保留（观测不丢）
    new_kinds = [e["kind"] for e in store.recent_events(node_id="w-1")][len(kinds_before):]
    assert new_kinds == ["goal.sync_overflow"]                   # 恰好补记一条


def test_snapshot_within_limit_no_overflow_flag(store, svc):
    _online(store, "w-1")
    svc.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    snap = svc.snapshot_for("w-1")
    assert snap.get("sync_overflow") is False                     # 常态形状仅多一个 False 键
    assert "goal.sync_overflow" not in [e["kind"] for e in store.recent_events()]


def test_snapshot_empty_node_shape_unchanged_under_tiny_cap(store, monkeypatch):
    """空快照永不判超限：返回形状必须与 M1 逐字节一致（既有 == 断言是回归锚）。"""
    from modelctl.core.cluster.goals import GoalService

    monkeypatch.setenv("CLUSTER_MAX_SNAPSHOT_BYTES", "65536")
    assert GoalService(store).snapshot_for("nobody") == {"revision": "", "goals": []}
```

`tests/test_cluster_ingest.py` 追加（沿用 `env`/`_hb` 夹具）：

```python
def test_ack_omits_sync_on_snapshot_overflow(env, monkeypatch):
    """封顶的执行点在 ack：快照超限 → 不带 sync 段，worker 保持上一份完整快照。"""
    store, goals, reg = env
    monkeypatch.setenv("CLUSTER_MAX_SNAPSHOT_BYTES", "65536")
    goals.set_goals(profile="qwen", node_ids=["w-1"], create=True)
    store.update_goal("qwen@@w-1", now=2.0, profile_yaml="x" * 70000)   # 灌大越限
    assert goals.snapshot_for("w-1")["sync_overflow"] is True
    ack = reg.handle_heartbeat("w-1", _hb(goal_sync={"revision": ""}), now=100.0)
    assert "sync" not in ack
```

`tests/test_cluster_agg_cli.py`（新文件，Recorder/probe 夹具逐字沿用 `test_cluster_gov_cli.py` 形状）：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_agg_cli.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 14:00
# @Desc   : status/list --cluster 中心聚合（数据源切换/失败不回退）
# ===============================================================================
from __future__ import annotations

import pytest

BASE = "http://center:4173"

NODES_BODY = {"nodes": [{"node_id": "w-1", "status": "online", "lan_id": "lan-1",
                         "capacity_text": "4 卡 / 154 GiB"},
                        {"node_id": "w-2", "status": "offline", "lan_id": "",
                         "capacity_text": "-"}]}
GOALS_BODY = {"goals": [{"node_id": "w-1", "profile": "qwen", "intent": "start",
                         "stage": "READY", "state": "running", "port": 8001},
                        {"node_id": "w-1", "profile": "ds", "intent": "stop",
                         "stage": "STOPPED", "state": "", "port": None}]}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CLUSTER_ROLE", "CLUSTER_CENTER_URL", "CLUSTER_NODE_ID", "CLUSTER_LAN",
              "CLUSTER_JOIN_TOKEN", "CLUSTER_NODE_TOKEN", "API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLUSTER_CENTER_URL", BASE)
    monkeypatch.setenv("API_KEY", "sk-cli")
    # 聚合视图零 models/ 依赖：根指向空目录，误走本机路径立刻能被形状断言抓住
    import modelctl.core.envfile as ef

    ef.PROJECT_ROOT = tmp_path
    monkeypatch.setattr("modelctl.core.profile.PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.chdir(tmp_path)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._replies: dict[tuple[str, str], tuple[int, dict]] = {}

    def reply(self, method: str, path: str, status: int, body: dict) -> None:
        self._replies[(method, path)] = (status, body)

    def call(self, method, url, payload, api_key=""):
        self.calls.append((method, url, payload, api_key))
        for (m, path), out in self._replies.items():
            if m == method and url.endswith(path):
                return out
        return 200, {}


@pytest.fixture()
def probe(monkeypatch):
    import modelctl.core.cluster.center_probe as cp

    rec = Recorder()
    monkeypatch.setattr(cp, "get_json",
                        lambda url, api_key="", timeout=5.0: rec.call("GET", url, None, api_key))
    monkeypatch.setattr(cp, "post_json",
                        lambda url, payload, api_key="", timeout=5.0: rec.call("POST", url, payload, api_key))
    monkeypatch.setattr(cp, "put_json",
                        lambda url, payload, api_key="", timeout=5.0: rec.call("PUT", url, payload, api_key))
    monkeypatch.setattr(cp, "delete_json",
                        lambda url, api_key="", timeout=5.0: rec.call("DELETE", url, None, api_key))
    return rec


def _main(argv):
    from modelctl import cli

    return cli.main(argv)


def test_status_cluster_aggregates_nodes_and_goals(probe, capsys) -> None:
    probe.reply("GET", "/cluster/nodes", 200, NODES_BODY)
    probe.reply("GET", "/cluster/goals", 200, GOALS_BODY)
    assert _main(["status", "--cluster"]) == 0
    assert [c[1] for c in probe.calls] == [BASE + "/admin/api/cluster/nodes",
                                           BASE + "/admin/api/cluster/goals"]
    out = capsys.readouterr().out
    # w-1: start×1 且 READY×1 → "1/1"，stop×1 → "1"；w-2 无 goal → "0/0"/"0"
    assert "w-1" in out and "w-2" in out and "1/1" in out and "0/0" in out


def test_list_cluster_groups_by_profile(probe, capsys) -> None:
    probe.reply("GET", "/cluster/nodes", 200, NODES_BODY)
    probe.reply("GET", "/cluster/goals", 200, GOALS_BODY)
    assert _main(["list", "--cluster"]) == 0
    out = capsys.readouterr().out
    assert "qwen" in out and "ds" in out and "READY" in out


def test_center_down_exit2_no_local_fallback(probe, capsys) -> None:
    """中心不可达必须报错退 2：静默回退本机视图 = 假报数据源（spec §3）。"""
    probe.reply("GET", "/cluster/nodes", -1, {"error": "connection refused"})
    assert _main(["status", "--cluster"]) == 2
    assert len(probe.calls) == 1               # 失败即停，不发第二个请求


def test_center_404_exit2(probe) -> None:
    probe.reply("GET", "/cluster/nodes", 404, {"detail": "未启用"})
    assert _main(["list", "--cluster"]) == 2
```

- [ ] **Step 2: 确认失败**（`--cluster` invalid choice；`sync_overflow` KeyError）

- [ ] **Step 3: 实现 `snapshot_for` 封顶**（goals.py——**模块顶部已 import json**，勿再局部 import；logger 已有）

整函数替换：

```python
    def snapshot_for(self, node_id: str) -> dict[str, Any]:
        """该节点的全量期望状态。revision 是内容哈希：同一 goal 集在中心重启后同值，
        故 worker 端"要不要重写盘"的判据在两侧都稳定。空节点用空串（不是哈希）。

        尺寸封顶（终审 A-4，spec §2.4）：总量超 `max_snapshot_bytes` → sync_overflow
        置真，消费方（handle_heartbeat 的 ack 组装）**不下发**。刻意不静默截断——
        半套声明会让 worker 剪掉仍服役的模型文件，比一份不发更危险；也不抛异常——
        心跳 ack 组装路径"回流炸不掉"是铁律。revision 照算照回：观测面（列表/CLI
        显示期望代际）不因此失明。
        """
        rows = self.store.list_goals(node_id=node_id)
        goals = [{"goal_id": g["goal_id"], "profile": g["profile"], "engine": g["engine"],
                  "yaml": g["profile_yaml"], "sha": g["profile_sha"],
                  "version": g.get("profile_version") or "", "intent": g["intent"],
                  "params": g.get("params"), "env_overlay": g.get("env_overlay")} for g in rows]
        if not goals:
            return {"revision": "", "goals": []}
        canon = json.dumps(goals, sort_keys=True, ensure_ascii=False)
        overflow = len(canon.encode("utf-8")) > config.max_snapshot_bytes()
        if overflow:
            logger.error(f"节点 {node_id} 的 goal 快照 {len(canon.encode('utf-8'))} B 超过上限 "
                         f"{config.max_snapshot_bytes()} B：本轮不下发 sync（goal 数 {len(goals)}，"
                         f"检查是否误下发超大 profile YAML）")
            self.store.append_event("goal.sync_overflow", node_id=node_id,
                                    payload={"bytes": len(canon.encode("utf-8")),
                                             "goal_count": len(goals)})
        return {"revision": hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16],
                "goals": goals, "sync_overflow": overflow}
```

> goals.py 的导入区新增 `from modelctl.core.cluster import config`（并入既有 `from modelctl.core.cluster import gate, profiles` 行）。空快照分支**先返回**，`sync_overflow` 键不出现——既有 `test_snapshot_empty_for_unknown_node` 的严格 `==` 断言是不变量。

**Step 4: `nodes.handle_heartbeat` 消费 overflow**（nodes.py 顶部无 logger，需新增 `from loguru import logger`；L126 处修改）：

```python
            # overflow 闸门（M2 Task 8，spec §2.4）：中心自己算出的快照超限就不下发，
            # worker 保持上一份**完整**快照继续收敛；不写水位，下拍继续尝试（运维
            # 撤掉毒 goal 后立即恢复）。acked 条件在既有 revision 判断上叠加。
            if snapshot.get("sync_overflow"):
                logger.warning(f"节点 {node_id} 快照超限，本轮 ack 不带 sync 段")
            elif snapshot["revision"] != reported or forced:
                ack["sync"] = dict(snapshot, force=True) if forced else snapshot
                self.store.set_node_last_goal_sync_sha(node_id, snapshot["revision"])
```

> 注意 `ack["sync"]` 现在会多带一个 `sync_overflow: False` 键（常态快照）。worker 侧 `wsproto.parse_ack` 对 sync 段只取 `revision/goals`（其余键忽略），帧形状向后兼容——**这正是消费侧宽容解析的红利**，`agent.py` 零 diff 不破。`make_sync`（WS 独立 sync 帧路径）不经此处，不受影响。

**Step 5: CLI `--cluster` flag**（build_parser，L112-119 循环内）：

```python
    for cmd in ("start", "stop", "restart", "status"):
        p = sub.add_parser(cmd)
        p.add_argument("name", nargs="?" if cmd == "status" else None)
        if cmd == "status":
            p.add_argument("--cluster", action="store_true",
                           help="中心聚合视图：走中心 REST 按节点展示 goal 声明/实际（中心不可达退 2，不回退本机）")
        if cmd in ("start", "restart"):
            # 默认 600s：vLLM 首次冷启动（torch.compile + warmup + CUDA graph 捕获）实测约 6 分钟
            p.add_argument("--timeout", type=float, default=600, help="健康检查超时秒数（默认 600）")
            p.add_argument("--gpus", default=None, help="逗号分隔的 GPU 索引，如 0,1,2（覆盖环境变量 MODELCTL_GPUS）")
    sub.add_parser("list", help="列出所有 profile").add_argument(
        "--cluster", action="store_true", help="中心聚合视图：按 profile 分组展示各节点托管状态")
```

**Step 6: 两个聚合 handler**（cli.py，放 `_cmd_status` 之前）：

```python
def _cluster_aggregate() -> tuple[list[dict], list[dict], str]:
    """--cluster 数据源：中心 nodes + goals。失败给错误文案（调用方退 2）。

    不回退本机视图是定版裁决：数据源静默切换比报错恶劣——用户会拿本机数字
    当集群全貌做运维决策。probe --cluster 不提供（中心无法反连 worker，spec §0.3）。
    """
    status_n, nodes = _cluster_request("GET", "/cluster/nodes")
    if status_n != 200:
        return [], [], f"中心聚合不可用: {_center_detail(status_n, nodes)}"
    status_g, goals = _cluster_request("GET", "/cluster/goals")
    if status_g != 200:
        return [], [], f"中心 goal 台账不可用: {_center_detail(status_g, goals)}"
    return nodes.get("nodes", []), goals.get("goals", []), ""


def _cmd_status_cluster() -> int:
    nodes, goals, err = _cluster_aggregate()
    if err:
        logger.error(err)
        return 2
    per: dict[str, dict[str, int]] = {}
    for g in goals:
        cell = per.setdefault(str(g.get("node_id", "")), {"start": 0, "stop": 0, "ready": 0})
        intent = str(g.get("intent", "start"))
        cell[intent] = cell.get(intent, 0) + 1
        if g.get("stage") == "READY":
            cell["ready"] += 1
    rows = [[n.get("node_id", ""), n.get("status", ""), n.get("lan_id") or "-",
             n.get("capacity_text") or "-",
             f"{c['ready']}/{c['start']}" if c else "0/0",
             str(c["stop"]) if c else "0"]
            for n in nodes for c in [per.get(str(n.get("node_id", "")), {})]]
    _print_table(["节点", "状态", "LAN", "容量", "goal(start 收敛/声明)", "goal(stop)"],
                 rows, dim_indices=(2, 3, 5))
    return 0


def _cmd_list_cluster() -> int:
    nodes, goals, err = _cluster_aggregate()
    if err:
        logger.error(err)
        return 2
    grouped: dict[str, list[dict]] = {}
    for g in goals:
        grouped.setdefault(str(g.get("profile", "")), []).append(g)
    for idx, profile in enumerate(sorted(grouped)):
        if idx > 0:
            print()
        rows = [[g.get("node_id", ""), g.get("intent", ""), g.get("stage", ""),
                 g.get("state") or "-", "-" if g.get("port") is None else g["port"]]
                for g in grouped[profile]]
        print(_table_paint(f"{profile}（{len(rows)} 节点托管）", "SECTION"))
        _print_table(["节点", "intent", "stage", "实际状态", "端口"], rows, dim_indices=(1, 3))
    if not grouped:
        print("集群暂无 goal（中心台账为空）")
    return 0
```

**Step 7: main() 分发短路**（`args = parser.parse_args(rest)` 之后、`caps = probe()` **之前**）：

```python
    # --cluster 聚合走纯中心数据源，短路于 probe() 之前：纯中心机器可能没有本地
    # 引擎环境（无 venv/无 GPU），聚合视图不该被硬件探测拦住或拖慢。
    if getattr(args, "cluster", False):
        if args.command == "status":
            return _cmd_status_cluster()
        if args.command == "list":
            return _cmd_list_cluster()
```

**Step 8: `.env.example`** 在 `# CLUSTER_START_TIMEOUT_S=300` 之后追加：

```
# 单节点 goal 快照总量上限（字节，默认 8388608=8 MiB，下限 65536）；超限不下发并记 goal.sync_overflow 事件
# CLUSTER_MAX_SNAPSHOT_BYTES=8388608
```

- [ ] **Step 9**: `uv run pytest tests/test_cluster_agg_cli.py tests/test_cluster_goals.py tests/test_cluster_ingest.py tests/test_cluster_config.py -q` 全绿 → 全量回归不回退 → 提交（`feat(cluster): M2 --cluster 中心聚合 + goal 快照尺寸封顶（sync_overflow）`）。

---

### Task 9: M1 留 M2 清偿波——A-3 展示名归一 / T1-④ 显式列 / T2-② 单条 512 / T8 换 engine 删旧

四条互相独立的小债，一次清偿（spec §5）。A-1（并发契约 docstring）已在 Task 1 落地；T7-③（孤儿 model_states）已由 Task 1/2 的 retire 闭环。

**Files:**
- Modify: `src/modelctl/core/cluster/goals.py`（公开 `find_goal_by_name`）、`src/modelctl/core/webui/admin_cluster.py`（model_verb 归一）、`src/modelctl/core/cluster/store.py`（显式列 ×3 组）、`src/modelctl/core/cluster/wsproto.py`（**仅 T2-②**）、`src/modelctl/core/cluster/sync.py`（**仅 T8**）
- Test: `tests/test_cluster_goals_http.py`（追加 2 条）、`tests/test_cluster_store.py`（追加 1 条）、`tests/test_cluster_wsproto_v2.py`（追加 1 条）、`tests/test_cluster_sync_writer.py`（追加 1 条）

**Interfaces:**
- Consumes: 既有 `GoalService._resolve_names`（stem 优先 + 原词兜底，remove 侧同款）
- Produces:
  - `GoalService.find_goal_by_name(profile: str, node_id: str) -> dict | None`——展示名/文件名都能命中 goal；命中行的 `profile` 字段（恒 stem）是 worker 侧寻址唯一真值
  - `model_verb` 端点：按归一后的 goal 行下发动作（action 帧的 `profile` 用 **stem**，不再是调用方原词）
  - store 的 nodes/goals/model_states 三类读路径**零 `SELECT *`**（行为零变更：列清单与 `_NODE_COLS`/schema 逐列一致）
  - `_opt_str_list`：每条 `[:512]` 截断（条数 limit 语义不变）
  - `apply_snapshot`：同 goal_id 换 engine 时删除旧引擎目录下的孤儿 YAML

- [ ] **Step 1: 写失败测试**

`tests/test_cluster_goals_http.py` 追加（沿用本文件 `center` 夹具与 `_add_display_profile` 式建文件手法——该助手在 core 测试文件里，此处直接内联建文件）：

```python
# ---------------- A-3：model_verb 展示名归一（M2 Task 9）----------------
def _make_display_profile(center_fixture_models, stem="qwen-fast", display="qwen-display"):
    (center_fixture_models / "vllm").mkdir(parents=True, exist_ok=True)
    (center_fixture_models / "vllm" / f"{stem}.yaml").write_text(
        f"port: 8001\nname: {display}\n", encoding="utf-8")


def test_model_verb_accepts_display_name(center, monkeypatch, tmp_path):
    import modelctl.core.cluster.goals as goals_mod

    _make_display_profile(tmp_path / "models")
    monkeypatch.setattr(goals_mod, "MODELS_DIR", tmp_path / "models")
    import modelctl.core.webui.admin_cluster as ac
    reg = ac.get_registry()
    reg.store.upsert_goal(goal_id="qwen-fast@@w-1", node_id="w-1", profile="qwen-fast",
                          engine="vllm", profile_yaml="port: 8001\nname: qwen-display\n",
                          profile_sha="sha-a", profile_version=None, intent="start",
                          params=None, env_overlay=None, placement=None, runtime_ref=None,
                          target_role="primary", stage="READY", created_by="op", now=time.time())
    r = center.post("/admin/api/cluster/nodes/w-1/model/qwen-display/stop", headers=_h())
    assert r.status_code == 200
    # action 帧给 worker 的 profile 必须是 stem（worker 用它对本地文件/进程名寻址）
    queued = reg._actions.get("w-1", [])
    assert queued and queued[-1]["profile"] == "qwen-fast"


def test_model_verb_unknown_name_404(center):
    r = center.post("/admin/api/cluster/nodes/w-1/model/nope/stop", headers=_h())
    assert r.status_code == 404
```

`tests/test_cluster_store.py` 追加（T1-④ 防"显式列漏列"回归）：

```python
def test_explicit_columns_cover_full_row(store: ClusterStore) -> None:
    """显式列改造的护栏：三类读路径返回的键集合必须与 schema 全列一致。"""
    store.upsert_node(node_id="w-1", node_token="t", lan_id="l", role="worker",
                      host_ip="", hostname="", engines=None, now=1.0)
    store.upsert_goal(goal_id="g@@w-1", node_id="w-1", profile="g", engine="vllm",
                      profile_yaml="port: 1\n", profile_sha="s", profile_version=None,
                      intent="start", params=None, env_overlay=None, placement=None,
                      runtime_ref=None, target_role="primary", stage="READY",
                      created_by="op", now=1.0)
    store.upsert_model_state(node_id="w-1", profile="g", state="running",
                             gpu=[0], port=8001, pid=1, now=1.0)
    node = store.get_node("w-1")
    goal = store.get_goal("g@@w-1")
    ms = store.list_model_states(node_id="w-1")[0]
    assert set(node) == {
        "node_id", "node_token", "lan_id", "role", "host_ip", "hostname", "engines",
        "created_at", "last_seen", "lease_expiry", "status", "disabled", "capacity_json",
        "runtime_json", "gateway_url", "last_goal_sync_sha", "local_profiles_json"}
    assert set(goal) == {
        "goal_id", "node_id", "profile", "engine", "profile_yaml", "profile_sha",
        "profile_version", "intent", "params", "env_overlay", "placement", "runtime_ref",
        "target_role", "traffic_weight", "stage", "stage_reason", "error_class",
        "created_by", "created_at", "updated_at"}
    assert set(ms) == {
        "node_id", "profile", "state", "gpu", "port", "pid", "reason", "endpoint_url",
        "endpoint_ready", "engine_version", "gpu_util", "metrics_p50_ms", "last_probe_ms",
        "error_class", "updated_at"}
```

`tests/test_cluster_wsproto_v2.py` 追加（T2-②）：

```python
def test_opt_str_list_caps_each_entry_length():
    """条数封顶之外，单条也须截断（drift/local_profiles 元素是 worker 自由串）。"""
    long_item = "x" * 2000
    out = wsproto._opt_str_list([long_item, "ok"], limit=10)
    assert out == ["x" * 512, "ok"]
```

`tests/test_cluster_sync_writer.py` 追加（T8）：

```python
def test_engine_change_prunes_old_engine_file(dirs):
    """同 goal_id 换 engine：新目录写盘 + 旧引擎目录的孤儿 YAML 必须删除。

    旧文件不删 = worker 上残留一份"中心不知道"的 YAML；后续有人手工
    load_profile 会启动中心从未声明过的服务（幽灵服务，终审 T8）。
    """
    models, cache = dirs
    _apply(dirs, [_goal(engine="vllm")])
    assert (models / "vllm" / "qwen.yaml").is_file()
    out = _apply(dirs, [_goal(engine="sglang")], revision="r2")
    assert (models / "sglang" / "qwen.yaml").is_file()
    assert not (models / "vllm" / "qwen.yaml").exists()
    assert out.pruned == []                     # 不是 goal 撤销，不进 pruned 账
    state = read_state(cache)
    assert state["goals"][0]["path"].endswith("sglang/qwen.yaml").replace("\\", "/") or \
        state["goals"][0]["path"].endswith("sglang\\qwen.yaml")
```

- [ ] **Step 2: 确认失败**（display 404 / `_opt_str_list` 不截断 / 旧文件仍在 / 键集断言此刻也过——它是护栏防后续漏列）

- [ ] **Step 3: A-3 实现**（goals.py 增公开方法；admin_cluster 的 `model_verb` 换查找）

```python
    def find_goal_by_name(self, profile: str, node_id: str) -> dict | None:
        """按寻址名（文件名或展示名）定位 (profile,node) 的 goal 行（终审 A-3）。

        与 remove/stop 共用 `_resolve_names` 的归一口径：set 侧把展示名归一成 stem
        落库，读侧不归一就会出现"展示名下发的 goal，展示名却操作不了"的分裂。
        候选顺序 = stem 优先，原词兜底（goal 源文件被删后仍能按落库名操作）。
        """
        for name in self._resolve_names(profile):
            goal = self.store.get_goal(goal_id_of(name, node_id))
            if goal is not None:
                return goal
        return None
```

`model_verb` 中把

```python
    goal = reg.store.get_goal(goal_id_of(profile, node_id))
```

替换为

```python
    goal = _goals().find_goal_by_name(profile, node_id)
```

并把 `push_action(..., profile=profile)` 改为 `profile=str(goal["profile"])`（action 帧的 profile 是 worker 侧文件/进程寻址成分，**必须是 stem**；`append_event` 的 `goal_id` 同样取 `str(goal["goal_id"])`，现写法已如此）。

- [ ] **Step 4: T1-④ 实现**（store.py）——模块级新增两组列常量，六处 `SELECT *` 换显式列：

```python
_GOAL_COLS = ("goal_id", "node_id", "profile", "engine", "profile_yaml", "profile_sha",
              "profile_version", "intent", "params", "env_overlay", "placement",
              "runtime_ref", "target_role", "traffic_weight", "stage", "stage_reason",
              "error_class", "created_by", "created_at", "updated_at")

_MODEL_STATE_COLS = ("node_id", "profile", "state", "gpu", "port", "pid", "reason",
                     "endpoint_url", "endpoint_ready", "engine_version", "gpu_util",
                     "metrics_p50_ms", "last_probe_ms", "error_class", "updated_at")
```

- `get_node`/`find_node_by_token`/`list_nodes`：`SELECT * FROM nodes` → `SELECT {", ".join(_NODE_COLS)} FROM nodes`（模块内用一个预拼串常量 `_NODE_SELECT = "SELECT " + ", ".join(_NODE_COLS) + " FROM nodes"` 更省重复）；
- `get_goal`/`list_goals`：`SELECT * FROM goals` → 显式 `_GOAL_COLS`；
- `list_model_states`：`SELECT * FROM model_states` → 显式 `_MODEL_STATE_COLS`，行转 dict 改 `{c: r[c] for c in _MODEL_STATE_COLS}`（原 `r.keys()` 写法随 SELECT 列收紧，一并改掉避免两处口径）。

> 行为零变更前提：列清单与 `_SCHEMA` 逐列核对（Step 1 的护栏测试就是核对此项）。`_row_to_goal` 的 `row.keys()` 遍历 JSON 字段逻辑不动（显式列含全部 JSON 字段）。

**Step 5: T2-② 实现**（wsproto.py `_opt_str_list` 单行改动，**这是 wsproto 全里程碑唯一允许 diff**）：

```python
    return [str(v)[:512] for v in value if isinstance(v, str)][:limit]
```

**Step 6: T8 实现**（sync.py，`entries[goal_id] = {...}` 赋值之后、`result.written.append` 之前）：

```python
        # T8（终审）：同 goal_id 换 engine（如 vllm→sglang 的同名 profile）时，旧引擎
        # 目录那份 YAML 已成孤儿——中心快照里再也不会出现该路径，prune 分支永远摸不到
        # 它。不删 = worker 上潜伏一份"中心不知道"的 YAML，被手工 load 后就是一台
        # 中心从未声明的幽灵服务。goal 未撤销故不进 pruned 账（撤销与换目录语义不同）。
        old = previous.get(goal_id)
        if old is not None:
            old_path = Path(str(old.get("path", "")))
            if old_path.name and old_path != path and old_path.is_file():
                _prune(old_path)
                logger.info(f"集群 sync：goal {goal_id} 换引擎，已清理旧文件 {old_path}")
```

- [ ] **Step 7**: `uv run --with fastapi --with httpx pytest tests/test_cluster_goals_http.py -q`；`uv run pytest tests/test_cluster_store.py tests/test_cluster_wsproto_v2.py tests/test_cluster_sync_writer.py -q` 全绿 → 全量回归不回退 → **硬约束自查**：`git diff --stat src/modelctl/core/cluster/wsproto.py` 应只有 1 行变更、`git diff src/modelctl/core/cluster/agent.py src/modelctl/core/cluster/reconcile.py` 应为空 → 提交（`fix(cluster): M2 清偿波——model_verb 展示名归一/显式列清单/单条512截断/换引擎孤儿清理`）。

---

### Task 10: 前端 API 层——`cluster.ts` M2 全量契约 + `client.ts` blob 下载

后端契约在 Task 1-9 已全部定版，本 Task 把 REST 面逐键翻译成 TS 类型 + 请求函数，供 Task 11-13 三个视图消费。**router/Sidebar 改动刻意不在本 Task**：路由懒加载 import 尚不存在的 `.vue` 会让 `vue-tsc` 报错，路由与菜单随各自视图在 Task 11/12 一起落地，保证每个 Task 收尾 `npm run build` 都是绿的。

**Files:**
- Modify: `web/src/api/client.ts`（新增 `downloadBlob`）
- Modify: `web/src/api/cluster.ts`（全量替换为下方内容）

**Interfaces（与后端逐键对齐，实现时以 Task 1-9 落盘代码为准）:**
- `GoalView` ← `_goal_view`（无 `profile_yaml`）；`EventRow` ← Task 3 events 定版 `{ts,node_id,goal_id,kind,text}`；`ProfileCatalogEntry` ← Task 7；`ClusterSettings` ← Task 7 七键；`NodeDetail` ← 既有 `cluster_node_detail`；`GoalCreateResult` ← `_GoalCreateBody` 响应（**注意 `created/skipped/errors` 是计数 int**）；`DeleteGoalResult.removed/missing` 是 **`string[]`**（`remove_goals` 返回 goal_id 列表，不是计数）；治理四端点响应 ← Task 2；`downloadBlob` 走同一 axios 实例（Bearer 拦截器复用，blob 响应 401 拦截照常生效）。

- [ ] **Step 1: `npm install`**（`web/node_modules` 缺失，前端 Task 铁律前置）

```
cd web; npm install; cd ..
```

- [ ] **Step 2: `client.ts` 追加 `downloadBlob`**（放在 `dataOf` 之前；`export async function`）

```ts
/**
 * Blob 下载（备份导出等二进制端点）：复用同一 axios 实例 ⇒ Bearer 注入与
 * 401 跳登录零重复。文件名优先取 Content-Disposition（后端已带时间戳），
 * 落盘动作走临时 <a download>，随后立即回收 ObjectURL。
 */
export async function downloadBlob(path: string, fallbackFilename: string): Promise<string> {
  const res = await client.get(path, { responseType: 'blob', timeout: 120_000 });
  const disp = String(res.headers['content-disposition'] || '');
  const match = /filename="?([^";]+)"?/i.exec(disp);
  const filename = match?.[1] || fallbackFilename;
  const url = URL.createObjectURL(res.data as Blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  return filename;
}
```

- [ ] **Step 3: `cluster.ts` 全量替换**（既有 `NodeView`/`ClusterStatus`/两个函数保留原文，其余为 M2 新增）

```ts
import client, { dataOf, downloadBlob } from './client';

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
  capacity: Record<string, number> | null;
  capacity_text: string;
}

export interface ClusterStatus {
  role: string;
  is_center: boolean;
  nodes_total: number;
  nodes_online: number;
}

/** goal 视图行：与 admin_cluster._goal_view 逐键对齐（刻意不含 profile_yaml） */
export interface GoalView {
  goal_id: string;
  node_id: string;
  profile: string;
  engine: string;
  intent: string;
  stage: string;
  reason: string;
  error_class: string;
  target_role: string;
  profile_version: string;
  state: string;
  gpu: number[] | null;
  port: number | null;
  pid: number | null;
  created_at: string;
  updated_at: string;
  age_s: number | null;
}

/** model_states 原样行（worker 回流直存）：updated_at 是 epoch 秒且后端未格式化，前端是展示端 */
export interface ModelState {
  node_id: string;
  profile: string;
  state: string;
  gpu: number[] | null;
  port: number | null;
  pid: number | null;
  reason: string | null;
  endpoint_url: string | null;
  endpoint_ready: boolean | null;
  engine_version: string | null;
  gpu_util: number | null;
  metrics_p50_ms: number | null;
  last_probe_ms: number | null;
  error_class: string | null;
  updated_at: number | null;
}

/** GET /cluster/events 定版行：ts/text 均由后端单端拼装，前端零加工 */
export interface EventRow {
  ts: string;
  node_id: string;
  goal_id: string;
  kind: string;
  text: string;
}

/** GET /cluster/profiles 目录行：同名多引擎逐条（选边在 POST gate，前端只联动） */
export interface ProfileCatalogEntry {
  name: string;
  engine: string;
  display_name: string;
  version: string;
}

/** GET /cluster/settings 只读现值（零写端点；join token 仅脱敏） */
export interface ClusterSettings {
  role: string;
  center_url: string;
  heartbeat_interval_s: number;
  lease_s: number;
  reconcile_interval_s: number;
  max_snapshot_bytes: number;
  join_token_mask: string;
}

/** GET /cluster/nodes/{id}：台账视图 + goals + model_states 三段 */
export interface NodeDetail {
  node: NodeView;
  goals: GoalView[];
  model_states: ModelState[];
}

/** POST /cluster/goals 载荷（与 _GoalCreateBody 对齐；engine 空串=不选边） */
export interface GoalCreatePayload {
  profile: string;
  node_ids?: string[] | null;
  all_nodes?: boolean;
  intent?: string;
  create?: boolean;
  params?: Record<string, unknown> | null;
  env_overlay?: Record<string, string> | null;
  gpus?: string;
  engine?: string;
  lan_allow?: string[] | null;
  runtime_ref?: string | null;
  target_role?: string;
  dry_run?: boolean;
}

/** 创建响应：gate 的 skip 是 200+report 不是 HTTP 错误；report 是逐节点定版文本 */
export interface GoalCreateResult {
  created: number;
  skipped: number;
  errors: number;
  report: string;
  reason: string;
  goals: GoalView[];
}

/** PUT 载荷（与 _GoalUpdateBody 对齐：无 profile/node_ids——换目标=create+remove 两步） */
export interface GoalUpdatePayload {
  intent?: string;
  params?: Record<string, unknown>;
  env_overlay?: Record<string, string>;
  placement?: Record<string, unknown>;
  runtime_ref?: string;
  target_role?: string;
  profile_version?: string;
}

/** DELETE goal：removed/missing 是 goal_id 字符串列表（remove_goals 的聚合口径） */
export interface DeleteGoalResult {
  removed: string[];
  missing: string[];
}

export interface QueuedResult {
  queued: boolean;
}
export interface KickedResult {
  kicked: boolean;
}
/** rotate-token 一次性返回新明文（同 join-check 先例），界面只展示、不落任何存储 */
export interface RotateTokenResult {
  node_token: string;
  kicked: boolean;
  hint: string;
}
export interface RetireResult {
  removed: boolean;
  removed_goals: number;
}

export function getClusterStatus() {
  return dataOf(client.get<ClusterStatus>('/cluster/status'));
}

export function getClusterNodes() {
  return dataOf(client.get<{ nodes: NodeView[] }>('/cluster/nodes'));
}

export function getClusterNodeDetail(nodeId: string) {
  return dataOf(client.get<NodeDetail>(`/cluster/nodes/${encodeURIComponent(nodeId)}`));
}

export function listClusterGoals(params: { node_id?: string; profile?: string } = {}) {
  return dataOf(client.get<{ goals: GoalView[] }>('/cluster/goals', { params }));
}

export function createClusterGoal(payload: GoalCreatePayload) {
  return dataOf(client.post<GoalCreateResult>('/cluster/goals', payload));
}

export function updateClusterGoal(goalId: string, payload: GoalUpdatePayload) {
  return dataOf(client.put<{ goal: GoalView }>(`/cluster/goals/${encodeURIComponent(goalId)}`, payload));
}

export function deleteClusterGoal(goalId: string) {
  return dataOf(client.delete<DeleteGoalResult>(`/cluster/goals/${encodeURIComponent(goalId)}`));
}

export function retryGoal(goalId: string) {
  return dataOf(client.post<QueuedResult>(`/cluster/goals/${encodeURIComponent(goalId)}/retry`));
}

export function forceNodeSync(nodeId: string) {
  return dataOf(client.post<QueuedResult>(`/cluster/nodes/${encodeURIComponent(nodeId)}/sync`));
}

export function nodeModelVerb(nodeId: string, profile: string, verb: 'start' | 'stop' | 'restart') {
  return dataOf(
    client.post<QueuedResult>(
      `/cluster/nodes/${encodeURIComponent(nodeId)}/model/${encodeURIComponent(profile)}/${verb}`,
    ),
  );
}

export function disableNode(nodeId: string) {
  return dataOf(client.post<KickedResult>(`/cluster/nodes/${encodeURIComponent(nodeId)}/disable`));
}

export function enableNode(nodeId: string) {
  return dataOf(client.post<{ ok: boolean }>(`/cluster/nodes/${encodeURIComponent(nodeId)}/enable`));
}

export function rotateNodeToken(nodeId: string) {
  return dataOf(client.post<RotateTokenResult>(`/cluster/nodes/${encodeURIComponent(nodeId)}/rotate-token`));
}

export function kickNode(nodeId: string) {
  return dataOf(client.post<KickedResult>(`/cluster/nodes/${encodeURIComponent(nodeId)}/kick`));
}

export function retireNode(nodeId: string) {
  return dataOf(client.delete<RetireResult>(`/cluster/nodes/${encodeURIComponent(nodeId)}`));
}

export function listClusterEvents(params: { node_id?: string; kind?: string; limit?: number } = {}) {
  return dataOf(client.get<{ events: EventRow[] }>('/cluster/events', { params }));
}

export function listClusterProfiles() {
  return dataOf(client.get<{ profiles: ProfileCatalogEntry[] }>('/cluster/profiles'));
}

export function getClusterSettings() {
  return dataOf(client.get<ClusterSettings>('/cluster/settings'));
}

export function rotateJoinToken() {
  return dataOf(client.post<{ join_token: string }>('/cluster/join-tokens/rotate'));
}

/** 备份下载：后端附件名已带时间戳，这里只兜底；返回实际落盘文件名 */
export function downloadClusterBackup() {
  return downloadBlob('/cluster/backup', 'modelctl-cluster.db');
}
```

- [ ] **Step 4: 构建验收** — `cd web; npm run build`（内含 `vue-tsc --noEmit`）0 错、vite build 绿。

- [ ] **Step 5: 后端回归零触碰确认 + 提交**（本 Task 纯前端，后端测试不需重跑，但 `git status` 必须只见两个前端文件）

```
git add web/src/api/client.ts web/src/api/cluster.ts
git commit --only web/src/api/client.ts web/src/api/cluster.ts -m "feat(cluster): M2 前端 API 层——cluster.ts 治理/事件/目录全量契约 + client.ts blob 下载"
```

---

### Task 11: `ClusterGoalsView.vue`——目标矩阵 + 两段式下发抽屉 + 行内动作

spec §4.1 全量落地。三条实现裁决（撰写时定版，实现者不再自决）：

1. **矩阵列集合取自 goals 数据**（catalog 里有但从无 goal 的 profile 不占列——矩阵是"托管总览"不是目录复读机）；列数 > 8 时**强制降级**列表形态（`effectiveMode` computed，手动切换按钮同时隐藏，防运维误点回矩阵进横向滚动地狱）。
2. **筛选器不提供 drift 项**：`goal.drift` 是 worker 心跳增量事件（`_record_drift` 只在集合新增时记，漂移消失无终止事件），goal 行不携带"当前漂移"位——按历史事件过滤会把早已收敛的 goal 永远标成漂移，宁缺毋假。筛选项 = 全部/收敛/收敛中/失败。
3. **矩阵格只呈现，动作按钮在列表形态行内**（stop/remove/retry + 节点级 sync）；矩阵的定位是一眼看出"哪台哪列颜色不对"，逐格塞按钮会把矩阵炸成按钮墙。
4. 收敛判据：`intent==='start' ? stage==='READY' : stage==='STOPPED'`；失败 = `stage==='FAILED'` 或 `error_class` 非空；其余 = 收敛中。
5. 路由 `path: 'cluster/goals'`、`name: 'cluster-goals'`（spec 的 `cluster-goals` 指 name；path 沿用既有 `cluster/nodes` 风格）。组件不加 `<script setup name>`——现仓库所有视图均无此惯例（grep 零命中），M2 不孤立引入。

**Files:**
- Create: `web/src/views/ClusterGoalsView.vue`
- Modify: `web/src/router/index.ts`（`cluster/nodes` 路由**之前**插入 goals 路由）
- Modify: `web/src/components/layout/Sidebar.vue`（menus 加一项 + goals 图标分支）

- [ ] **Step 1: 新建 `ClusterGoalsView.vue`**（全量文件；错误展示走内联 banner——仓库无 Element Plus，不引入 `ElMessage`）

```vue
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { AxiosError } from 'axios';
import ConfirmDialog from '@/components/common/ConfirmDialog.vue';
import {
  createClusterGoal,
  deleteClusterGoal,
  forceNodeSync,
  getClusterNodes,
  listClusterGoals,
  listClusterProfiles,
  retryGoal,
  updateClusterGoal,
  type GoalCreateResult,
  type GoalView,
  type NodeView,
  type ProfileCatalogEntry,
} from '@/api/cluster';

/**
 * 集群目标（M2，spec §4.1）：节点×profile 矩阵 + 两段式下发抽屉 + 行内动作。
 * 矩阵是总览（格=收敛色），动作在列表形态；profile 列 >8 强制降级列表。
 */
const router = useRouter();

const nodes = ref<NodeView[]>([]);
const goals = ref<GoalView[]>([]);
const catalog = ref<ProfileCatalogEntry[]>([]);
const disabled = ref(false);
const error = ref('');
const actionError = ref('');
let timer: number | undefined;

type Mode = 'matrix' | 'list';
const mode = ref<Mode>('matrix');
const filterState = ref<'all' | 'converged' | 'converging' | 'failed'>('all');
const filterNode = ref('');
const filterProfile = ref('');

const STAGE_STYLE: Record<string, string> = {
  converged: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  converging: 'bg-blue-500/15 text-blue-400 border-blue-500/30',
  failed: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
};
const NODE_STATUS_STYLE: Record<string, string> = {
  online: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  stale: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  offline: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
  disabled: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
};

/** Axios 错误 → 后端 detail 原文（展示纪律：失败给人看的永远是后端原话） */
function errText(e: unknown): string {
  const ax = e as AxiosError<{ detail?: string }>;
  return ax.response?.data?.detail || ax.message || String(e);
}

async function refresh() {
  try {
    const [n, g] = await Promise.all([getClusterNodes(), listClusterGoals()]);
    nodes.value = n.nodes;
    goals.value = g.goals;
    disabled.value = false;
    error.value = '';
  } catch (e) {
    if ((e as AxiosError).response?.status === 404) {
      disabled.value = true;
      return;
    }
    error.value = errText(e);
  }
}

async function loadCatalog() {
  try {
    catalog.value = (await listClusterProfiles()).profiles;
  } catch {
    catalog.value = []; // 目录失败不拦主视图：抽屉打开时会重试
  }
}

function classify(g: GoalView): 'converged' | 'converging' | 'failed' {
  if (g.stage === 'FAILED' || g.error_class) return 'failed';
  const settled = g.intent === 'start' ? 'READY' : 'STOPPED';
  return g.stage === settled ? 'converged' : 'converging';
}

const matrixColumns = computed(() => {
  const names = [...new Set(goals.value.map((g) => g.profile))].sort();
  return filterProfile.value ? names.filter((p) => p === filterProfile.value) : names;
});
/** 列数失控对策（裁决 1）：>8 列强制列表形态，不给手动切回矩阵的入口 */
const effectiveMode = computed<Mode>(() => (matrixColumns.value.length > 8 ? 'list' : mode.value));

const filteredGoals = computed(() =>
  goals.value.filter((g) => {
    if (filterNode.value && g.node_id !== filterNode.value) return false;
    if (filterProfile.value && g.profile !== filterProfile.value) return false;
    if (filterState.value !== 'all' && classify(g) !== filterState.value) return false;
    return true;
  }),
);

function goalAt(nodeId: string, profile: string): GoalView | undefined {
  return goals.value.find((g) => g.node_id === nodeId && g.profile === profile);
}

// ---------------- 行内动作 ----------------
const removeTarget = ref<GoalView | null>(null);
const busy = ref(false);

async function rowStop(g: GoalView) {
  try {
    actionError.value = '';
    await updateClusterGoal(g.goal_id, { intent: 'stop' });
    await refresh();
  } catch (e) {
    actionError.value = errText(e);
  }
}

async function rowRemoveConfirm() {
  if (!removeTarget.value) return;
  busy.value = true;
  try {
    actionError.value = '';
    const out = await deleteClusterGoal(removeTarget.value.goal_id);
    if (out.missing.length) actionError.value = `撤销返回 missing：${out.missing.join(', ')}`;
    removeTarget.value = null;
    await refresh();
  } catch (e) {
    actionError.value = errText(e);
  } finally {
    busy.value = false;
  }
}

async function rowRetry(g: GoalView) {
  try {
    actionError.value = '';
    await retryGoal(g.goal_id);
    await refresh();
  } catch (e) {
    actionError.value = errText(e);
  }
}

async function rowSync(nodeId: string) {
  try {
    actionError.value = '';
    await forceNodeSync(nodeId);
  } catch (e) {
    actionError.value = errText(e);
  }
}

// ---------------- 两段式下发抽屉 ----------------
const drawer = ref(false);
const formName = ref('');
const formEngine = ref('');
const allNodes = ref(true);
const pickedNodes = ref<string[]>([]);
const intent = ref<'start' | 'stop'>('start');
const create = ref(true);
const envText = ref('');
const preview = ref<GoalCreateResult | null>(null);
/** 预览成功才点亮提交；表单任何改动经 @change/@input 调 resetPreview 复位（永不盲发） */
const previewOk = ref(false);
const drawerError = ref('');

const catalogNames = computed(() => [...new Set(catalog.value.map((p) => p.name))].sort());
const engineOptions = computed(() =>
  catalog.value.filter((p) => p.name === formName.value).map((p) => p.engine),
);
const needsEngine = computed(() => engineOptions.value.length > 1);
/** 同名多引擎才强制选边；单引擎留空串=不选边（选边歧义裁决留在 POST gate） */
const enginePayload = computed(() => (needsEngine.value ? formEngine.value : ''));

const envOverlayError = computed(() => {
  const t = envText.value.trim();
  if (!t) return '';
  try {
    const v: unknown = JSON.parse(t);
    if (typeof v !== 'object' || v === null || Array.isArray(v)) return 'env_overlay 必须是 JSON 对象';
    for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
      if (typeof val !== 'string') return `env_overlay.${k} 必须是字符串值`;
    }
    return '';
  } catch {
    return 'JSON 解析失败';
  }
});

const formValid = computed(
  () => !!formName.value && !envOverlayError.value &&
    (!needsEngine.value || !!formEngine.value) &&
    (allNodes.value || pickedNodes.value.length > 0),
);

function resetPreview() {
  preview.value = null;
  previewOk.value = false;
}

async function openDrawer() {
  drawerError.value = '';
  drawer.value = true;
  if (!catalog.value.length) await loadCatalog();
}

function closeDrawer() {
  drawer.value = false;
  resetPreview();
}

function payload(dryRun: boolean) {
  return {
    profile: formName.value,
    engine: enginePayload.value,
    all_nodes: allNodes.value,
    node_ids: allNodes.value ? undefined : pickedNodes.value,
    intent: intent.value,
    create: create.value,
    env_overlay: envText.value.trim() ? (JSON.parse(envText.value) as Record<string, string>) : undefined,
    dry_run: dryRun,
  };
}

async function doPreview() {
  if (!formValid.value) return;
  drawerError.value = '';
  resetPreview();
  try {
    preview.value = await createClusterGoal(payload(true));
    previewOk.value = true;
  } catch (e) {
    drawerError.value = errText(e);
  }
}

async function doSubmit() {
  if (!previewOk.value) return;
  busy.value = true;
  drawerError.value = '';
  try {
    const out = await createClusterGoal(payload(false));
    closeDrawer();
    actionError.value = out.reason || '';
    await refresh();
  } catch (e) {
    drawerError.value = errText(e);
  } finally {
    busy.value = false;
  }
}

onMounted(() => {
  refresh();
  loadCatalog();
  timer = window.setInterval(refresh, 5000);
});
onBeforeUnmount(() => window.clearInterval(timer));
</script>

<template>
  <div class="p-6">
    <div class="mb-4 flex items-center justify-between">
      <h1 class="text-lg font-semibold text-slate-100">集群目标</h1>
      <div class="flex items-center gap-3">
        <span class="text-sm text-slate-400">{{ goals.length }} 个 goal · {{ nodes.length }} 节点</span>
        <button class="btn-primary" @click="openDrawer">新建下发</button>
      </div>
    </div>

    <div v-if="disabled" class="rounded-lg border border-slate-700 bg-slate-800/50 p-6 text-sm text-slate-400">
      当前节点未启用集群角色。中心机请在 .env 设置 CLUSTER_ROLE=both 后重启 webui。
    </div>

    <template v-else>
      <div v-if="error" class="mb-4 rounded-lg border border-rose-800 bg-rose-900/30 p-4 text-sm text-rose-300">
        {{ error }}
      </div>
      <div v-if="actionError" class="mb-4 rounded-lg border border-amber-800 bg-amber-900/30 p-4 text-sm text-amber-300">
        {{ actionError }}
      </div>

      <!-- 筛选器 -->
      <div class="mb-4 flex flex-wrap items-center gap-3 text-sm">
        <select v-model="filterState" class="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200">
          <option value="all">全部状态</option>
          <option value="converged">已收敛</option>
          <option value="converging">收敛中</option>
          <option value="failed">失败</option>
        </select>
        <select v-model="filterNode" class="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200">
          <option value="">全部节点</option>
          <option v-for="n in nodes" :key="n.node_id" :value="n.node_id">{{ n.node_id }}</option>
        </select>
        <select v-model="filterProfile" class="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200">
          <option value="">全部 profile</option>
          <option v-for="p in [...new Set(goals.map((g) => g.profile))].sort()" :key="p" :value="p">{{ p }}</option>
        </select>
        <button
          v-if="matrixColumns.length <= 8"
          class="btn-ghost"
          @click="mode = mode === 'matrix' ? 'list' : 'matrix'"
        >
          {{ mode === 'matrix' ? '切换列表' : '切换矩阵' }}
        </button>
        <span v-else class="text-xs text-slate-500">profile 列超过 8 个，已降级为列表形态</span>
      </div>

      <!-- 矩阵形态 -->
      <table v-if="effectiveMode === 'matrix' && filteredGoals.length" class="w-full text-left text-sm">
        <thead class="text-slate-400">
          <tr class="border-b border-slate-700">
            <th class="py-2 pr-4">节点</th>
            <th v-for="p in matrixColumns" :key="p" class="py-2 pr-4">{{ p }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="n in nodes" :key="n.node_id" class="border-b border-slate-800">
            <td class="py-2 pr-4">
              <router-link
                :to="`/cluster/nodes/${n.node_id}`"
                class="font-mono text-blue-400 hover:underline"
              >{{ n.node_id }}</router-link>
              <span class="ml-2 rounded border px-1.5 py-0.5 text-xs" :class="NODE_STATUS_STYLE[n.status] || NODE_STATUS_STYLE.offline">
                {{ n.status }}
              </span>
            </td>
            <td v-for="p in matrixColumns" :key="p" class="py-2 pr-4 align-top">
              <div
                v-if="goalAt(n.node_id, p)"
                class="cursor-pointer rounded border px-2 py-1 text-xs"
                :class="STAGE_STYLE[classify(goalAt(n.node_id, p)!)]"
                :title="`${goalAt(n.node_id, p)!.stage}${goalAt(n.node_id, p)!.reason ? ' · ' + goalAt(n.node_id, p)!.reason : ''}`"
                @click="router.push(`/cluster/nodes/${n.node_id}`)"
              >
                {{ goalAt(n.node_id, p)!.intent }} · {{ goalAt(n.node_id, p)!.stage }}
                <div v-if="goalAt(n.node_id, p)!.gpu?.length" class="text-slate-400">
                  gpu {{ goalAt(n.node_id, p)!.gpu!.join(',') }}<span v-if="goalAt(n.node_id, p)!.port"> :{{ goalAt(n.node_id, p)!.port }}</span>
                </div>
              </div>
              <span v-else class="text-xs text-slate-600">-</span>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- 列表形态（含 >8 列降级） -->
      <table v-else-if="filteredGoals.length" class="w-full text-left text-sm">
        <thead class="text-slate-400">
          <tr class="border-b border-slate-700">
            <th class="py-2 pr-4">节点</th>
            <th class="py-2 pr-4">profile</th>
            <th class="py-2 pr-4">engine</th>
            <th class="py-2 pr-4">intent</th>
            <th class="py-2 pr-4">stage</th>
            <th class="py-2 pr-4">实际</th>
            <th class="py-2 pr-4">gpu/端口</th>
            <th class="py-2 pr-4">更新时间</th>
            <th class="py-2">动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="g in filteredGoals" :key="g.goal_id" class="border-b border-slate-800 text-slate-200">
            <td class="py-2 pr-4">
              <router-link :to="`/cluster/nodes/${g.node_id}`" class="font-mono text-blue-400 hover:underline">{{ g.node_id }}</router-link>
            </td>
            <td class="py-2 pr-4 font-mono">{{ g.profile }}</td>
            <td class="py-2 pr-4 text-slate-400">{{ g.engine }}</td>
            <td class="py-2 pr-4">{{ g.intent }}</td>
            <td class="py-2 pr-4">
              <span class="rounded border px-2 py-0.5 text-xs" :class="STAGE_STYLE[classify(g)]">{{ g.stage }}</span>
              <div v-if="g.reason" class="max-w-64 truncate text-xs text-slate-500" :title="g.reason">{{ g.reason }}</div>
            </td>
            <td class="py-2 pr-4 text-slate-400">{{ g.state || '-' }}</td>
            <td class="py-2 pr-4 text-slate-400">{{ g.gpu?.join(',') || '-' }}<span v-if="g.port"> :{{ g.port }}</span></td>
            <td class="py-2 pr-4 text-slate-400">{{ g.updated_at }}</td>
            <td class="py-2 whitespace-nowrap">
              <button v-if="g.intent === 'start'" class="btn-ghost mr-1" @click="rowStop(g)">stop</button>
              <button v-if="g.stage === 'FAILED'" class="btn-ghost mr-1" @click="rowRetry(g)">retry</button>
              <button class="btn-ghost mr-1" @click="rowSync(g.node_id)">sync</button>
              <button class="btn-danger" @click="removeTarget = g">remove</button>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-else class="rounded-lg border border-slate-700 bg-slate-800/50 p-6 text-center text-sm text-slate-500">
        暂无匹配的 goal
      </div>
    </template>

    <!-- 下发抽屉（两段式：预览成功才点亮提交，永不盲发） -->
    <div v-if="drawer" class="fixed inset-0 z-40 flex justify-end bg-black/60" @click.self="closeDrawer">
      <div class="h-full w-full max-w-lg overflow-y-auto border-l border-slate-700 bg-slate-900 p-5">
        <div class="mb-4 flex items-center justify-between">
          <h2 class="text-base font-semibold text-slate-100">批量下发 goal</h2>
          <button class="text-xl text-slate-400 hover:text-slate-200" @click="closeDrawer">×</button>
        </div>

        <div class="space-y-3 text-sm">
          <label class="block">
            <span class="text-slate-400">profile</span>
            <select v-model="formName" class="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-slate-200" @change="formEngine = ''; resetPreview()">
              <option value="">请选择…</option>
              <option v-for="n in catalogNames" :key="n" :value="n">{{ n }}</option>
            </select>
          </label>

          <label v-if="needsEngine" class="block">
            <span class="text-amber-400">engine（同名 YAML 多引擎，必须选边）</span>
            <select v-model="formEngine" class="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-slate-200" @change="resetPreview">
              <option value="">请选择…</option>
              <option v-for="e in engineOptions" :key="e" :value="e">{{ e }}</option>
            </select>
          </label>

          <div>
            <label class="inline-flex items-center gap-2 text-slate-300">
              <input v-model="allNodes" type="checkbox" class="size-4 accent-blue-500" @change="resetPreview" />
              全部节点（--all）
            </label>
            <div v-if="!allNodes" class="mt-2 max-h-40 overflow-y-auto rounded border border-slate-700 bg-slate-950 p-2">
              <label v-for="n in nodes" :key="n.node_id" class="flex items-center gap-2 py-0.5 text-slate-300">
                <input v-model="pickedNodes" type="checkbox" :value="n.node_id" class="size-4 accent-blue-500" @change="resetPreview" />
                <span class="font-mono">{{ n.node_id }}</span>
                <span class="text-xs text-slate-500">{{ n.status }} · {{ n.capacity_text }}</span>
              </label>
            </div>
          </div>

          <div class="flex gap-4">
            <label class="flex-1">
              <span class="text-slate-400">intent</span>
              <select v-model="intent" class="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-slate-200" @change="resetPreview">
                <option value="start">start</option>
                <option value="stop">stop</option>
              </select>
            </label>
            <label class="mt-5 inline-flex items-center gap-2 text-slate-300">
              <input v-model="create" type="checkbox" class="size-4 accent-blue-500" @change="resetPreview" />
              允许新建（--create）
            </label>
          </div>

          <label class="block">
            <span class="text-slate-400">env_overlay（JSON 对象，可空）</span>
            <textarea
              v-model="envText"
              rows="3"
              class="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-xs text-slate-200"
              placeholder='{"MODEL_ROOT": "/mnt/nas"}'
              @input="resetPreview"
            />
            <span v-if="envOverlayError" class="text-xs text-rose-400">{{ envOverlayError }}</span>
          </label>

          <div v-if="drawerError" class="rounded border border-rose-800 bg-rose-900/30 p-3 text-xs text-rose-300">{{ drawerError }}</div>

          <!-- 预览区：gate 逐项 verdict 报告原文 -->
          <div v-if="preview" class="rounded border border-slate-700 bg-slate-950 p-3">
            <div class="mb-1 text-xs text-slate-400">
              预览（dry-run）：预计 created {{ preview.created }} · skipped {{ preview.skipped }} · errors {{ preview.errors }}
            </div>
            <pre class="max-h-56 overflow-auto whitespace-pre-wrap text-xs text-slate-300">{{ preview.report }}</pre>
          </div>

          <div class="flex justify-end gap-3 pt-2">
            <button class="btn-ghost" :disabled="!formValid || busy" @click="doPreview">预览（dry-run）</button>
            <button class="btn-primary" :disabled="!previewOk || busy" @click="doSubmit">确认提交</button>
          </div>
        </div>
      </div>
    </div>

    <ConfirmDialog
      :open="!!removeTarget"
      title="撤销 goal"
      :message="removeTarget ? `撤销 ${removeTarget.node_id} 上的 ${removeTarget.profile}？会撤 worker 本地 YAML、停模型并删台账。` : ''"
      danger
      confirm-text="撤销"
      :loading="busy"
      @confirm="rowRemoveConfirm"
      @cancel="removeTarget = null"
    />
  </div>
</template>
```

- [ ] **Step 2: 路由**（`router/index.ts`，插在 `cluster/nodes` 条目**之前**；兜底 `/:pathMatch(.*)*` 不受影响）

```ts
      {
        path: 'cluster/goals',
        name: 'cluster-goals',
        component: () => import('@/views/ClusterGoalsView.vue'),
        meta: { title: '集群目标' },
      },
```

- [ ] **Step 3: Sidebar**（`menus` 里 `{ to: '/cluster/nodes', … }` **之前**插入菜单项；图标分支在 cluster 分支后加 `goals`）

```ts
  { to: '/cluster/goals', label: '集群目标', icon: 'goals' },
```

```html
<!-- goals -->
<template v-else-if="m.icon === 'goals'"><svg class="size-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 6h11M9 12h11M9 18h11" /><circle cx="4.5" cy="6" r="1.5" /><circle cx="4.5" cy="12" r="1.5" /><circle cx="4.5" cy="18" r="1.5" /></svg></template>
```

- [ ] **Step 4: 构建验收** — `cd web; npm run build` 0 错绿。

- [ ] **Step 5: 提交**

```
git add web/src/views/ClusterGoalsView.vue web/src/router/index.ts web/src/components/layout/Sidebar.vue
git commit --only web/src/views/ClusterGoalsView.vue web/src/router/index.ts web/src/components/layout/Sidebar.vue -m "feat(cluster): M2 集群目标视图——节点×profile 矩阵 + 两段式下发抽屉 + 行内动作"
```

---

### Task 12: `ClusterNodeDetailView.vue`——详情 + 治理按钮组 + 事件流轮询

spec §4.2 全量落地。裁决：

1. **事件流数据源是 REST 轮询（10s）而非 SSE**（spec 定版"视觉复用 SseLogViewer 但数据源不同"——独立组件实现，不 import SseLogViewer，它是 EventSource 语义的壳）。
2. **rotate-token 成功弹一次性面板**：token 只显示在当前组件内存 ref 里，关闭即丢，绝不写 localStorage/sessionStorage（敏感字段纪律在前端的延伸）；复制走 `navigator.clipboard`，失败回退"请手选复制"文案。
3. **退役确认 = 自绘模态 + 输入 node_id 完全匹配才点亮红按钮**，确认文案动态带出"将连带删除 N 个 goal"（N 取当前详情数据的 goals.length）。用自绘模态而非 ConfirmDialog：后者无输入框插槽，为塞输入去改公共组件会波及既有调用方；样式对齐 ConfirmDialog（`bg-black/60` 遮罩 + `max-w-md` 卡片）。本视图因此**不 import ConfirmDialog**。
4. **model_states.updated_at 是 epoch 秒**（后端未格式化——它不是 `_goal_view` 体系成员），前端用仓库既有 `dayjs` 直接 `.format('YYYY-MM-DD HH:mm:ss')`（AuditLogView 同款先例），这是"前端零二次加工"纪律的既有例外口径：**后端已格式化的字段不动，未格式化的时间戳前端负责转规范格式**。
5. 路由 `path: 'cluster/nodes/:id'`、`name: 'cluster-node-detail'`；Sidebar 无需改动（`isActive` 前缀匹配已让详情高亮"集群"项）。

**Files:**
- Create: `web/src/views/ClusterNodeDetailView.vue`
- Modify: `web/src/router/index.ts`（`cluster/nodes` 列表路由**之前**插入详情路由——vue-router 静态段优先，实际两条不冲突，顺序仅取可读性；放列表后即可）

- [ ] **Step 1: 新建 `ClusterNodeDetailView.vue`**（全量文件）

```vue
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { AxiosError } from 'axios';
import dayjs from 'dayjs';
import ConfirmDialog from '@/components/common/ConfirmDialog.vue';
import {
  disableNode,
  enableNode,
  forceNodeSync,
  getClusterNodeDetail,
  kickNode,
  listClusterEvents,
  retireNode,
  rotateNodeToken,
  type EventRow,
  type NodeDetail,
} from '@/api/cluster';

/**
 * 节点详情（M2，spec §4.2）：基础信息 + goals/model_states + 事件流（10s 轮询）
 * + 治理按钮组（sync / disable|enable / rotate-token / kick / retire）。
 */
const route = useRoute();
const router = useRouter();
const nodeId = computed(() => String(route.params.id || ''));

const detail = ref<NodeDetail | null>(null);
const events = ref<EventRow[]>([]);
const notFound = ref('');
const error = ref('');
const actionError = ref('');
const busy = ref(false);
let timer: number | undefined;

const STATUS_STYLE: Record<string, string> = {
  online: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  stale: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  offline: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
  disabled: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
};

function errText(e: unknown): string {
  const ax = e as AxiosError<{ detail?: string }>;
  return ax.response?.data?.detail || ax.message || String(e);
}

function fmtEpoch(v: number | null): string {
  return v === null ? '-' : dayjs.unix(v).format('YYYY-MM-DD HH:mm:ss');
}

async function refresh() {
  try {
    detail.value = await getClusterNodeDetail(nodeId.value);
    events.value = (await listClusterEvents({ node_id: nodeId.value, limit: 100 })).events;
    notFound.value = '';
    error.value = '';
  } catch (e) {
    const st = (e as AxiosError).response?.status;
    if (st === 404) notFound.value = `节点 ${nodeId.value} 不存在（或已退役）`;
    else error.value = errText(e);
  }
}

async function runAction(fn: () => Promise<unknown>) {
  if (busy.value) return;
  busy.value = true;
  actionError.value = '';
  try {
    await fn();
    await refresh();
  } catch (e) {
    actionError.value = errText(e);
  } finally {
    busy.value = false;
  }
}

// ---------------- 治理动作 ----------------
const rotateResult = ref<{ token: string; hint: string } | null>(null);
const copyTip = ref('');

function onRotate() {
  void runAction(async () => {
    const out = await rotateNodeToken(nodeId.value);
    rotateResult.value = { token: out.node_token, hint: out.hint };
  });
}

async function copyToken() {
  if (!rotateResult.value) return;
  try {
    await navigator.clipboard.writeText(rotateResult.value.token);
    copyTip.value = '已复制到剪贴板';
  } catch {
    copyTip.value = '剪贴板不可用，请手选复制';
  }
}

const retireOpen = ref(false);
const retireInput = ref('');
const retireOk = computed(() => retireInput.value === nodeId.value && !!nodeId.value);

function onRetireConfirm() {
  if (!retireOk.value) return;
  void runAction(async () => {
    await retireNode(nodeId.value);
    retireOpen.value = false;
    await router.push('/cluster/nodes');
  });
}

onMounted(() => {
  refresh();
  timer = window.setInterval(refresh, 10_000);
});
onBeforeUnmount(() => window.clearInterval(timer));
</script>

<template>
  <div class="p-6">
    <div class="mb-4 flex items-center justify-between">
      <div class="flex items-center gap-3">
        <button class="btn-ghost" @click="router.push('/cluster/nodes')">← 节点列表</button>
        <h1 class="font-mono text-lg font-semibold text-slate-100">{{ nodeId }}</h1>
        <span
          v-if="detail"
          class="rounded border px-2 py-0.5 text-xs"
          :class="STATUS_STYLE[detail.node.status] || STATUS_STYLE.offline"
        >{{ detail.node.status }}</span>
      </div>
      <div v-if="detail" class="flex flex-wrap items-center justify-end gap-2">
        <button class="btn-ghost" :disabled="busy" @click="runAction(() => forceNodeSync(nodeId))">重新 sync</button>
        <button
          v-if="detail.node.status !== 'disabled'"
          class="btn-ghost"
          :disabled="busy"
          @click="runAction(() => disableNode(nodeId))"
        >禁用</button>
        <button v-else class="btn-ghost" :disabled="busy" @click="runAction(() => enableNode(nodeId))">启用</button>
        <button class="btn-ghost" :disabled="busy" @click="onRotate">轮换 token</button>
        <button class="btn-ghost" :disabled="busy" @click="runAction(() => kickNode(nodeId))">踢除</button>
        <button class="btn-danger" :disabled="busy" @click="retireInput = ''; retireOpen = true">退役</button>
      </div>
    </div>

    <div v-if="notFound" class="rounded-lg border border-slate-700 bg-slate-800/50 p-6 text-sm text-slate-400">
      {{ notFound }}
    </div>

    <template v-else>
      <div v-if="error" class="mb-4 rounded-lg border border-rose-800 bg-rose-900/30 p-4 text-sm text-rose-300">{{ error }}</div>
      <div v-if="actionError" class="mb-4 rounded-lg border border-amber-800 bg-amber-900/30 p-4 text-sm text-amber-300">{{ actionError }}</div>

      <!-- 一次性 token 面板：只活在组件内存里，关闭即丢，绝不落任何存储 -->
      <div v-if="rotateResult" class="mb-4 rounded-lg border border-amber-700 bg-amber-950/40 p-4 text-sm">
        <div class="mb-2 font-semibold text-amber-300">新节点令牌（仅此一次显示，关闭后无法找回）</div>
        <div class="flex items-center gap-3">
          <code class="flex-1 select-all break-all rounded bg-slate-950 p-2 font-mono text-xs text-amber-200">{{ rotateResult.token }}</code>
          <button class="btn-ghost shrink-0" @click="copyToken">复制</button>
          <button class="btn-ghost shrink-0" @click="rotateResult = null; copyTip = ''">关闭</button>
        </div>
        <div class="mt-2 text-xs text-slate-400">{{ rotateResult.hint }}</div>
        <div v-if="copyTip" class="mt-1 text-xs text-slate-500">{{ copyTip }}</div>
      </div>

      <!-- 基础信息卡 -->
      <section v-if="detail" class="card mb-4">
        <h3 class="mb-3 text-sm font-semibold text-slate-100">基础信息</h3>
        <dl class="grid grid-cols-2 gap-x-8 gap-y-2 text-sm md:grid-cols-3">
          <div><dt class="text-slate-500">LAN</dt><dd class="text-slate-200">{{ detail.node.lan_id || '-' }}</dd></div>
          <div><dt class="text-slate-500">角色</dt><dd class="text-slate-200">{{ detail.node.role }}</dd></div>
          <div><dt class="text-slate-500">主机</dt><dd class="text-slate-200">{{ detail.node.hostname || '-' }}（{{ detail.node.host_ip || '-' }}）</dd></div>
          <div><dt class="text-slate-500">容量</dt><dd class="text-slate-200">{{ detail.node.capacity_text || '-' }}</dd></div>
          <div><dt class="text-slate-500">令牌</dt><dd class="font-mono text-slate-400">{{ detail.node.token_mask }}</dd></div>
          <div><dt class="text-slate-500">最后心跳</dt><dd class="text-slate-200">{{ detail.node.since_seen_s === null ? '-' : detail.node.since_seen_s.toFixed(0) + 's 前' }}</dd></div>
          <div><dt class="text-slate-500">租约剩余</dt><dd class="text-slate-200">{{ detail.node.lease_left_s === null ? '-' : Math.max(detail.node.lease_left_s, 0).toFixed(0) + 's' }}</dd></div>
          <div class="col-span-2">
            <dt class="text-slate-500">引擎</dt>
            <dd class="text-slate-200">
              <span v-if="!detail.node.engines">-</span>
              <span v-for="(ver, eng) in detail.node.engines || {}" :key="eng" class="mr-3">{{ eng }}: {{ ver || '未知' }}</span>
            </dd>
          </div>
        </dl>
      </section>

      <!-- goals 表 -->
      <section v-if="detail" class="card mb-4">
        <h3 class="mb-3 text-sm font-semibold text-slate-100">托管目标（{{ detail.goals.length }}）</h3>
        <table class="w-full text-left text-sm">
          <thead class="text-slate-400">
            <tr class="border-b border-slate-700">
              <th class="py-2 pr-4">profile</th><th class="py-2 pr-4">engine</th>
              <th class="py-2 pr-4">intent</th><th class="py-2 pr-4">stage</th>
              <th class="py-2 pr-4">实际</th><th class="py-2">gpu/端口</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="g in detail.goals" :key="g.goal_id" class="border-b border-slate-800 text-slate-200">
              <td class="py-2 pr-4 font-mono">{{ g.profile }}</td>
              <td class="py-2 pr-4 text-slate-400">{{ g.engine }}</td>
              <td class="py-2 pr-4">{{ g.intent }}</td>
              <td class="py-2 pr-4">
                {{ g.stage }}
                <div v-if="g.reason" class="max-w-72 truncate text-xs text-slate-500" :title="g.reason">{{ g.reason }}</div>
              </td>
              <td class="py-2 pr-4 text-slate-400">{{ g.state || '-' }}</td>
              <td class="py-2">{{ g.gpu?.join(',') || '-' }}<span v-if="g.port"> :{{ g.port }}</span></td>
            </tr>
            <tr v-if="!detail.goals.length"><td colspan="6" class="py-4 text-center text-slate-500">该节点暂无托管目标</td></tr>
          </tbody>
        </table>
      </section>

      <!-- model_states 表 -->
      <section v-if="detail" class="card mb-4">
        <h3 class="mb-3 text-sm font-semibold text-slate-100">运行事实（worker 回流）</h3>
        <table class="w-full text-left text-sm">
          <thead class="text-slate-400">
            <tr class="border-b border-slate-700">
              <th class="py-2 pr-4">profile</th><th class="py-2 pr-4">state</th>
              <th class="py-2 pr-4">gpu</th><th class="py-2 pr-4">端口</th>
              <th class="py-2 pr-4">endpoint</th><th class="py-2">更新于</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in detail.model_states" :key="m.profile" class="border-b border-slate-800 text-slate-200">
              <td class="py-2 pr-4 font-mono">{{ m.profile }}</td>
              <td class="py-2 pr-4">{{ m.state }}</td>
              <td class="py-2 pr-4 text-slate-400">{{ m.gpu?.join(',') || '-' }}</td>
              <td class="py-2 pr-4 text-slate-400">{{ m.port ?? '-' }}</td>
              <td class="py-2 pr-4 font-mono text-xs text-slate-400">{{ m.endpoint_url || '-' }}</td>
              <td class="py-2 text-slate-400">{{ fmtEpoch(m.updated_at) }}</td>
            </tr>
            <tr v-if="!detail.model_states.length"><td colspan="6" class="py-4 text-center text-slate-500">该节点当前无在跑模型上报</td></tr>
          </tbody>
        </table>
      </section>

      <!-- 事件流（REST 10s 轮询；后端已拼好 text，前端零加工） -->
      <section class="card">
        <h3 class="mb-3 text-sm font-semibold text-slate-100">事件流（最近 100 条 · 10s 轮询）</h3>
        <div class="max-h-96 overflow-y-auto rounded bg-slate-950 p-3 font-mono text-xs">
          <div v-for="(e, i) in events" :key="i" class="flex gap-3 border-b border-slate-900 py-1">
            <span class="shrink-0 text-slate-500">{{ e.ts }}</span>
            <span class="shrink-0 text-blue-400">{{ e.kind }}</span>
            <span class="break-all text-slate-300">{{ e.text }}</span>
          </div>
          <div v-if="!events.length" class="py-4 text-center text-slate-500">暂无事件</div>
        </div>
      </section>
    </template>

    <!-- 退役确认：自绘模态（ConfirmDialog 无输入插槽），输入 node_id 完全匹配才点亮红按钮 -->
    <div v-if="retireOpen" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm" @click.self="retireOpen = false">
      <div class="w-full max-w-md rounded-lg border border-slate-700 bg-slate-900 shadow-xl">
        <div class="border-b border-slate-800 px-5 py-3">
          <h3 class="text-base font-semibold text-red-300">退役节点 {{ nodeId }}</h3>
        </div>
        <div class="space-y-3 px-5 py-4 text-sm text-slate-300">
          <p>将从台账删除该节点及其全部运行事实，并连带撤销 <b class="text-red-300">{{ detail?.goals.length ?? 0 }}</b> 个托管 goal（worker 下拍剪文件、停模型）。事件历史保留。</p>
          <label class="block">
            <span class="text-xs text-slate-500">输入节点 ID <code class="font-mono text-slate-300">{{ nodeId }}</code> 以确认</span>
            <input
              v-model="retireInput"
              class="mt-1 w-full rounded border border-slate-600 bg-slate-950 px-3 py-1.5 font-mono text-sm text-slate-100"
            />
          </label>
        </div>
        <div class="flex justify-end gap-3 border-t border-slate-800 px-5 py-3">
          <button class="btn-ghost" :disabled="busy" @click="retireOpen = false">取消</button>
          <button class="btn-danger" :disabled="!retireOk || busy" @click="onRetireConfirm">确认退役</button>
        </div>
      </div>
    </div>
  </div>
</template>
```

- [ ] **Step 2: 路由**（`router/index.ts`，放在 `cluster/nodes` 列表条目**之后**）

```ts
      {
        path: 'cluster/nodes/:id',
        name: 'cluster-node-detail',
        component: () => import('@/views/ClusterNodeDetailView.vue'),
        meta: { title: '节点详情' },
      },
```

- [ ] **Step 3: 构建验收** — `cd web; npm run build` 0 错绿。

- [ ] **Step 4: 提交**

```
git add web/src/views/ClusterNodeDetailView.vue web/src/router/index.ts
git commit --only web/src/views/ClusterNodeDetailView.vue web/src/router/index.ts -m "feat(cluster): M2 节点详情视图——治理按钮组 + 一次性令牌面板 + 事件流轮询"
```

---

### Task 13: `SettingsView.vue` 集群块——只读配置 + join token 轮换 + 备份下载

spec §4.3 的只读版：`GET /cluster/settings` 七键展示（零写端点——远程改 `CLUSTER_ROLE`/`CLUSTER_CENTER_URL` 可自断控制面，写入仍走 .env + 重启）+ join token 脱敏与一次性轮换面板（复用既有 rotate 端点，Task 10 已封装）+ 备份下载按钮（`downloadBlob`）。非中心角色 settings 返回 404 → 整块隐藏只留一行提示（与 ClusterNodesView 的 404 处置同口径）。

**Files:**
- Modify: `web/src/views/SettingsView.vue`

- [ ] **Step 1: `<script setup>` 增量**（三处，串行编辑）

导入区追加（`import { health } ...` 之后）：

```ts
import { AxiosError } from 'axios';
import {
  downloadClusterBackup,
  getClusterSettings,
  rotateJoinToken,
  type ClusterSettings,
} from '@/api/cluster';
```

状态与函数（`onMounted(fetchVersion)` **之前**插入；并把该行改为 `onMounted(() => { fetchVersion(); fetchSettings(); })`）：

```ts
// ---------------- 集群块（M2，只读 + 轮换 + 备份） ----------------
const settings = ref<ClusterSettings | null>(null);
const clusterOff = ref(false); // 404 = 非中心角色，整块降级为一行提示
const joinTokenOnce = ref(''); // 一次性明文：只存内存，离开页面即丢
const clusterBusy = ref(false);
const backupTip = ref('');
const clusterError = ref('');

async function fetchSettings() {
  try {
    settings.value = await getClusterSettings();
    clusterOff.value = false;
  } catch (e) {
    if ((e as AxiosError).response?.status === 404) clusterOff.value = true;
    else clusterError.value = (e as Error).message;
  }
}

async function onRotateJoin() {
  if (clusterBusy.value) return;
  clusterBusy.value = true;
  clusterError.value = '';
  try {
    joinTokenOnce.value = (await rotateJoinToken()).join_token;
  } catch (e) {
    clusterError.value = (e as AxiosError<{ detail?: string }>).response?.data?.detail || (e as Error).message;
  } finally {
    clusterBusy.value = false;
  }
}

async function onBackup() {
  if (clusterBusy.value) return;
  clusterBusy.value = true;
  clusterError.value = '';
  backupTip.value = '';
  try {
    backupTip.value = `已下载 ${await downloadClusterBackup()}（sha256 见响应头 X-Backup-Sha256，可用 cluster backup --to 的本地复核口径对账）`;
  } catch (e) {
    clusterError.value = (e as AxiosError<{ detail?: string }>).response?.data?.detail || (e as Error).message;
  } finally {
    clusterBusy.value = false;
  }
}
```

- [ ] **Step 2: `<template>` 集群块**（插在"后端端点" section 与"清除 token" section 之间；`max_snapshot_bytes` 直接显示字节数——它没有"后端已格式化"版本，前端造 KB/MB 单位即违反单端格式化纪律，宁显原始数）

```html
    <!-- 集群（M2：只读 + join token 轮换 + 备份下载） -->
    <section class="card">
      <h3 class="mb-3 text-sm font-semibold text-slate-100">集群</h3>
      <p v-if="clusterOff" class="text-xs text-slate-500">
        当前节点未启用集群角色（solo/worker），无集群配置。
      </p>
      <template v-else>
        <dl v-if="settings" class="grid grid-cols-2 gap-x-8 gap-y-2 text-sm md:grid-cols-3">
          <div><dt class="text-slate-500">角色</dt><dd class="text-slate-200">{{ settings.role }}</dd></div>
          <div><dt class="text-slate-500">中心地址</dt><dd class="font-mono text-slate-200">{{ settings.center_url || '-' }}</dd></div>
          <div><dt class="text-slate-500">心跳间隔</dt><dd class="text-slate-200">{{ settings.heartbeat_interval_s }}s</dd></div>
          <div><dt class="text-slate-500">租约时长</dt><dd class="text-slate-200">{{ settings.lease_s }}s</dd></div>
          <div><dt class="text-slate-500">reconcile 周期</dt><dd class="text-slate-200">{{ settings.reconcile_interval_s }}s</dd></div>
          <div><dt class="text-slate-500">快照上限</dt><dd class="text-slate-200">{{ settings.max_snapshot_bytes }} 字节</dd></div>
          <div><dt class="text-slate-500">join token</dt><dd class="font-mono text-slate-400">{{ settings.join_token_mask }}</dd></div>
        </dl>
        <span v-else class="text-xs text-slate-500">配置加载中…</span>
        <p class="mt-2 text-xs text-slate-500">
          以上为进程启动时读取的 .env 现值，改配置请在中心机改 <span class="font-mono">.env</span> 后重启 webui（远程写可自断控制面，刻意不提供）。
        </p>
        <div class="mt-3 flex items-center gap-3">
          <button class="btn-danger" :disabled="clusterBusy" @click="onRotateJoin">轮换 join token</button>
          <button class="btn-ghost" :disabled="clusterBusy" @click="onBackup">下载数据库备份</button>
        </div>
        <div v-if="joinTokenOnce" class="mt-3 rounded border border-amber-700 bg-amber-950/40 p-3 text-sm">
          <div class="mb-1 text-xs font-semibold text-amber-300">新 join token（仅此一次显示）——旧 token 立即失效，所有未加入节点须改用新 token</div>
          <code class="select-all break-all font-mono text-xs text-amber-200">{{ joinTokenOnce }}</code>
        </div>
        <div v-if="backupTip" class="mt-2 text-xs text-emerald-400">{{ backupTip }}</div>
        <div v-if="clusterError" class="mt-2 text-xs text-rose-400">{{ clusterError }}</div>
      </template>
    </section>
```

- [ ] **Step 3: 构建验收** — `cd web; npm run build` 0 错绿。

- [ ] **Step 4: 提交**

```
git add web/src/views/SettingsView.vue
git commit --only web/src/views/SettingsView.vue -m "feat(cluster): M2 设置页集群块——只读配置 + join token 轮换 + 备份下载"
```

---

### Task 14: 文档 + 终审波（README 10.7 / known-pitfalls / 硬约束核验 / 端到端冒烟）

M2 关闭判据 = spec §6 五条全过。本 Task 是收口：文档两处、硬约束 git 核验、全量回归总表、人工冒烟清单交回用户。

**Files:**
- Modify: `README.md`（10.6 之后新增 10.7）
- Modify: `docs/known-pitfalls/README.md`（索引 3 行）
- Create: `docs/known-pitfalls/backend/cluster-governance-backup.md`（M2 主题聚合详情文件）

- [ ] **Step 1: README 10.7**（插在 10.6 末段"CLI 一律走中心 REST…"之后、`## 文档` 之前）

```markdown
#### 10.7 治理、备份与中心聚合（M2）

| 命令 | 效果 |
|---|---|
| `modelctl cluster events [--node w-210] [--kind goal.update] [--limit 200]` | 中心事件流（与 dashboard 事件流同一响应，后端单端拼装文本） |
| `modelctl cluster node disable --node w-210` | 禁用：在线则即刻断连，hello/join 被拒；**goal 台账不动**，重新启用后按既有 revision 收敛 |
| `modelctl cluster node enable --node w-210` | 解除禁用（状态由下一次 hello/心跳自然决定） |
| `modelctl cluster node rotate-token --node w-210` | 轮换节点令牌（**新令牌仅本次打印**）+ 连带断连；在该节点 `.env` 更新 `CLUSTER_NODE_TOKEN` 后重启 |
| `modelctl cluster node kick --node w-210` | 一次性断连（worker 指数退避重连，最坏 30s 回来） |
| `modelctl cluster node retire --node w-210` | 退役：删节点 + 运行事实 + **连带撤销全部 goal**；事件历史保留 |
| `modelctl cluster backup --to D:\bak\cluster.db [--force]` | 经中心 REST 热备下载，落盘后本地 sha256 与响应头对账 |
| `modelctl cluster restore --from D:\bak\cluster.db [--yes]` | 恢复（**不走 REST**）：要求中心 webui 已停机；校验通过后先把当前库备份为 `<db>.pre-restore.<时间戳>.bak` 再替换 |
| `modelctl status --cluster` / `modelctl list --cluster` | 中心聚合视图（按节点/按 profile 分组）；中心不可达**报错退 2，绝不回退本机视图** |

备份文件含 join/node token **明文**——它等价于中心 `.env` 的敏感度，按同一等级保管。dashboard 的「设置 → 集群 → 下载数据库备份」是同一端点。

restore 的完整性校验 = 可打开 + `PRAGMA integrity_check` + 五张必备表齐备；老备份缺 `join_token` 只告警不拦截。`probe --cluster` 不提供：worker 只出站、中心不反连，聚合无数据源。
```

- [ ] **Step 2: known-pitfalls 沉淀**（CLAUDE.md 渐进式披露：索引行 + 主题聚合文件）

`docs/known-pitfalls/README.md` 表尾追加 3 行：

```markdown
| 2026-09-06 | 后端 / 集群下发 | goal 快照超尺寸时"截断下发"会让 worker 剪掉服役文件 | 半套快照比不下发更危险（prune 语义会把缺失当撤销）；定版：超限整段不下发 + `goal.sync_overflow` 事件 + logger.error，worker 保持旧快照继续服役。 | [backend/cluster-governance-backup.md](backend/cluster-governance-backup.md) |
| 2026-09-06 | 后端 / 集群治理 | 事件 kind 词表守卫若 fail-fast，worker 一条未知 kind 就把 WS 循环打成 500 | 对端可控输入只能告警入库，fail-fast 只用于导入期自检与测试钉；`--cluster` 聚合中心不可达时报错退 2 而非静默回退本机视图（假报数据源比报错恶劣）。 | [backend/cluster-governance-backup.md](backend/cluster-governance-backup.md) |
| 2026-09-06 | 后端 / 集群备份 | 在线替换自身运行库：restore 若走 REST 等于让进程抽掉自己的地基 | restore 只留 CLI 且前置 `is_running(WEBUI_INSTANCE)` 判停；备份用 sqlite3 backup API 热备不阻写，落盘后必须 sha256 对账（传输损坏在恢复时才炸就晚了）。 | [backend/cluster-governance-backup.md](backend/cluster-governance-backup.md) |
```

新建 `docs/known-pitfalls/backend/cluster-governance-backup.md`：

```markdown
# 集群治理与备份（主题聚合）

> 本文件为 M2 期间沉淀问题的聚合入口；原始单文件已并入本文件归档以保留溯源信息。

## goal 快照超尺寸时"截断下发"会让 worker 剪掉服役文件

- **日期**：2026-09-06　**分类**：后端 / 集群下发
- **根因**：sync 快照是"全量声明"语义——worker 的 prune 分支把"快照里没有"当作"中心已撤销"，会删本地 YAML、停对应模型。若因体积上限对快照做截断下发，被截掉的 goal 在 worker 视角等价于被撤销，服役中的模型会被静默剪掉。
- **解决方案**：超限**整段不下发**（ack 不带 sync 段、不写 `last_goal_sync_sha`），写 `goal.sync_overflow` 事件 + `logger.error`；worker 保持旧快照继续服役，运维在 dashboard/事件流看到溢出后拆分或清理 goal。上限 `CLUSTER_MAX_SNAPSHOT_BYTES`（默认 8 MiB，floor 64 KiB 防误配把 sync 整体禁掉）。
- **测试钉**：`tests/test_cluster_goals.py::test_snapshot_overflow_*`（灌大 `profile_yaml` 越限，断言 ack 无 sync 段 + 事件计数切片只含 `goal.sync_overflow`）。

## 事件 kind 词表守卫若 fail-fast，worker 一条未知 kind 就把 WS 循环打成 500

- **日期**：2026-09-06　**分类**：后端 / 集群治理
- **根因**：WS event 帧的 kind 是 worker 自由字符串（M0 契约"未知消息一律不回显不拒绝"）。若在 `append_event` 对词表外 kind 硬抛，一条恶意/旧版 worker 的未知 kind 就能掀掉整条长连接。
- **解决方案**：守卫降级为 `logger.warning`（kind 经 `[:64]` 消毒）后照常入库；fail-fast 断言只用于导入期自检（中心自有埋点必须全在 `EVENT_KINDS` 内）与测试钉。`event_text` 兜底分支保证词表外 kind 也有合法展示、永不抛。
- **同族裁决**：`status/list --cluster` 中心不可达时报错退 2，**绝不静默回退本机视图**——数据源静默切换会让用户拿本机数字当集群全貌做运维决策。`--cluster` 分发短路于 `caps = probe()` 之前（纯中心机可能无本地引擎环境）。

## 在线替换自身运行库：restore 若走 REST 等于让进程抽掉自己的地基

- **日期**：2026-09-06　**分类**：后端 / 集群备份
- **根因**：中心 webui 运行中直接 `os.replace` 自己的 SQLite 文件，已打开的连接与 WAL 状态未定义；"恢复成功"的响应甚至可能由即将被替换的旧库处理链路发出。
- **解决方案**：restore 不提供 REST，仅 CLI 直调 `backup.restore_backup`；前置 `process.is_running(WEBUI_INSTANCE)` 检测，webui 在跑即拒执。替换前先自动就近备份 `<db>.pre-restore.<ts>.bak`。备份侧用 sqlite3 backup API（热备不阻写），`GET /cluster/backup` 经 `BackgroundTask` 清理临时文件；CLI 下载后本地 sha256 与 `X-Backup-Sha256` 对账，不符退 2 并删除坏文件。
```

- [ ] **Step 3: 硬约束核验**（逐条执行并贴出结果；基线 = M1 收官 commit `517b15f`）

```
git diff --stat 517b15f -- src/modelctl/core/cluster/wsproto.py        # 预期 1 file changed, 1 insertion(+), 1 deletion(-)
git diff 517b15f -- src/modelctl/core/cluster/agent.py src/modelctl/core/cluster/reconcile.py   # 必须空输出
```

- [ ] **Step 4: 全量回归总表**（M2 最终口径 = 既有 21 + 新增 7 = 28 文件）

```
uv run --with fastapi --with httpx pytest tests/test_cluster_store.py tests/test_cluster_store_goals.py tests/test_cluster_tokens.py tests/test_cluster_config.py tests/test_cluster_envwrite.py tests/test_cluster_wsproto.py tests/test_cluster_wsproto_v2.py tests/test_cluster_profiles.py tests/test_cluster_gate.py tests/test_cluster_goals.py tests/test_cluster_conns.py tests/test_cluster_nodes.py tests/test_cluster_ingest.py tests/test_cluster_sync_writer.py tests/test_cluster_reconcile.py tests/test_cluster_agent.py tests/test_cluster_agent_v2.py tests/test_cluster_cli.py tests/test_cluster_http.py tests/test_cluster_goals_http.py tests/test_cluster_goal_cli.py tests/test_cluster_governance.py tests/test_cluster_governance_http.py tests/test_cluster_events_http.py tests/test_cluster_backup.py tests/test_cluster_events_cli.py tests/test_cluster_gov_cli.py tests/test_cluster_agg_cli.py -q
```
Expected: **437 + M2 全部新增，0 failed**。前端 `cd web; npm run build` 绿。

- [ ] **Step 5: 端到端冒烟清单（人工，交回用户执行——spec §6.3"全程不开终端"判据）**

单机中心 + ≥1 worker，dashboard 依次走查：
1. 集群目标 → 新建下发：选 profile（同名多引擎出现 engine 选边下拉）→ 选节点 → 预览（gate 报告逐行）→ 提交；
2. 矩阵格 5s 轮询内从蓝（收敛中）转绿（READY）；节点详情事件流可见 `goal.create → node.model_action → action.result` 全链；
3. 详情页轮换 token → 一次性面板出明文 → 旧连接立断（状态 stale→offline）→ worker 改 `.env` 重启后回 online；
4. 禁用 → worker 重连被拒（详情显示 disabled）→ 启用 → 回归 online；
5. 退役确认需输入 node_id，连带撤销数与矩阵格数一致；
6. 设置 → 集群：七键只读展示、join token 轮换出新明文、备份下载落盘且文件名带时间戳。

- [ ] **Step 6: 提交**

```
git add README.md docs/known-pitfalls/README.md docs/known-pitfalls/backend/cluster-governance-backup.md
git commit --only README.md docs/known-pitfalls/README.md docs/known-pitfalls/backend/cluster-governance-backup.md -m "docs(cluster): M2 收官——README 10.7 治理/备份/聚合命令表 + known-pitfalls 沉淀三条"
```

---

## Self-Review 记录（计划撰写完成后）

- **spec 覆盖**：§2.1 治理五端点=T1/T2；§2.2 events 定版=T3+T4(CLI)；§2.3 profiles=T7；§2.4 封顶=T7(config)/T8(消费)；§2.5 backup=T5/T6；§3 CLI 全表=T4/T6/T8；§4.1=T10/T11；§4.2=T12；§4.3=T13；§5 波=T1(A-1)/T9(A-3、T1-④、T2-②、T8)/T2(T7-③)；§6.4 文档=T14。无遗漏项；§0.2 范围外清单未被越界实现。
- **占位符扫描**：全文无 TODO/待定/略；唯一显式授权对齐点=Task 3 Step 6（verdict 形状以 gate.py 实际为准）。
- **跨 Task 签名一致性**：`conns_registry` 形参（T1 裁决=T2 端点调用）、`sync_overflow` 键（T7 config→T8 snapshot/ack/测试）、`removed/missing: string[]`（后端 remove_goals=T10 TS 类型=T11 `out.missing.length`）、`recent_events(kind=)`（T3=T12 前端不传 kind 只用 node_id/limit）、settings 七键（T7 后端=T10 TS=T13 展示）已逐一核对。
- **围栏完整性**：全文 216 个 ``` 围栏行两两配对（脚本校验 balanced，无嵌套粘连），3918 行。

## Execution Handoff

计划定版。**两种执行方式（二选一）**：

1. **Subagent-Driven（推荐，M1 全 13 Task 已验证该模式）**——每 Task 派发全新子代理：控制器先发 brief（Task 全文 + 上游落盘接口）→ 子代理实现并按 Step 勾选自验 → 控制器对 diff 做规格/质量评审 → 问题先落盘 `.superpowers/sdd/2026-09-05-cluster-m2-views-governance/task-N-review.md` 再派 fix → re-review 通过才进下一 Task。账本 `progress.md` 逐 Task 记录 HEAD/评审结论/回归数。
2. **Inline 执行**——当前会话顺序执行，省派发开销，但长里程碑下上下文压力全部压在单会话，M1 曾因此在中段压缩丢失细节。

无论哪种：**Task 1 开工前**控制器先建账本目录并跑一次 21 文件基线复测（437 passed 复核），此后每个 Task 的全量回归都必须引用该基线数字；Task 间不打断评审闭环（"先落盘再派 fix"是铁律）。