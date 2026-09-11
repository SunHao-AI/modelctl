# stem 台账键碰撞（同 stem 多引擎互相遮挡）

> **日期**：2026-09-10
> **涉及模块**：`core/cluster/{store,goals,reconcile}.py`、`core/webui/admin_cluster.py`
> **同源现象**：goal 视图端口漂移、UI stop 后容器未死
> **溯因文档**：`docs/cluster-ui-test/README.md`「已知缺陷」节（已在该节标 FIXED）

## 一句话概述

`_identity_of()`（规则 4）在解析边界把 **profile.name 归一为中心 stem**，应让
`qwen2.5-1.5b-vllm` 与 `qwen2.5-1.5b-aphrodite` 两个 profile 在 PID 文件、GPU 锁、
**心跳 `profiles` 顶层键**、**center `model_states` 台账**里都出现名为
`qwen2.5-1.5b` 的一对互斥键——同 stem 多引擎共存时这条"归一"硬伤被放大。

## 现象

**界面侧（缺陷 1）**：向 `w-mock-01` 下发 `qwen2.5-1.5b (engine=vllm, port=8107)`，
UI goal 行「gpu/端口」显示 `8141`（aphrodite 端口），nginx 代理仍按 goal 声明的
8107 转发——`_goal_view` 把 `model_states` join 的端口当作 goal 实际值。

**进程侧（缺陷 2）**：对缺陷 1 同源的 vllm goal 点 stop：intent=stop 下发、worker
回执 `ok`、中心 stage 变 `STOPPED`，但 `docker ps` 显示 `qwen2.5-1.5b-vllm` 仍在
127.0.0.1:8107 跑着、还在占 GPU。

## 根因链条（三节点）


### 节点 1：center `model_states` 台账把同 stem 多引擎互相覆盖

旧 schema：`PRIMARY KEY (node_id, profile)`——仅 stem，无 engine 维度。
worker `/heartbeat` 的 `profiles` 是"按 stem 合并"的 dict（`goals` 侧
`profiles.setdefault(name, entry)` + `self._local` 侧按 stem 落盘），center
`record_model_states` 按"全量覆盖"剪枝下拉，每次心跳都把 vllm 行与 aphrodite 行
**串行改写成同一行**，时序里后写者胜出。

UI goal 视图（[`_goal_views`](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/webui/admin_cluster.py)）
从 `model_states` 按 `(node_id, profile)` 精配——两 goal 都命中那行**只有一个
aphrodite 端口/状态**的条目。

### 节点 2：worker stop 分支的 `up/alive` 是被遮蔽的假信号

worker 停止判定（[`Reconciler._step`](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/cluster/reconcile.py)）
原本：

```python
obs = self._observe(rec)
if obs.get("up") or obs.get("alive"): ...  # 走 stopper
```

`up` 走 [`is_running_any`](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/process.py) =
**端口 health**。`_identity_of` 归一后,aphrodite goal 的 `profile.name = "qwen2.5-1.5b"`、
`profile.port = 8141` → prober 打 `127.0.0.1:8141/health`——**永远空**（aphrodite
还没起,它的容器根本不存在;本 goal 的目标是 vllm 的 8107)。`alive` 走 PID 文件,
且 vllm 容器启动过程**不写 PID 文件**（docker 走 docker run --detach，不
spawn 本地子进程）→ `alive=False`。`up/alive` 双双 False → **stopper 永远不被调用**。

落 STOPPED 的原因:
`rec.update({"stage": STOPPED, ...})` 没有走 stopper 也照样执行——只是把本地
记录改成 STOPPED,容器脱离了任何管控。

### 节点 3：两者耦合

节点 2 写出的 **"假 STOPPED"** 被节点 1 上行（`record_model_states` 覆写）,
center 端 goal 行 `state=stopped`、UI 显示"已停止",但 nginx 直连 goal 声明口的
8107 仍然是活的 vllm——**三处字段互相撒谎**。

## 修复（三处配套,缺一不可）

### 修复 A：`model_states` PK 扩 engine 维度（节点 1）

`core/cluster/store.py`:

```sql
CREATE TABLE IF NOT EXISTS model_states (
  node_id TEXT NOT NULL, profile TEXT NOT NULL, engine TEXT NOT NULL DEFAULT '',
  ...
  PRIMARY KEY (node_id, profile, engine)
);
```

旧库 `ALTER TABLE` 不支持改 PK——走 "建 `model_states_new` 副本 → 去重插入（同
stem 多行只保留最新 `updated_at`）→ DROP 旧表 → RENAME" 标准路径,`_migrate_model_states_pk`
幂等（启动时先 `PRAGMA table_info` 比对 PK 列元组,已是三键不迁移）。

`upsert_model_state(node_id, profile, engine="", ...)` 新签名,
`ON CONFLICT(node_id, profile, engine)`;`list_model_states(node_id, profile, engine)`
扩过滤条件;`delete_model_state(node_id, profile, engine="")`——engine 空串=按
`(node, profile)` 全删,兼容旧调用方（含 CLI 路径 `_delete_goals` 按 stem 全清）。

### 修复 B：worker `_collect` 上报 engine（节点 1 的数据供给侧）

`Reconciler._collect` 的 goals 循环把 goal 侧 engine 写进心跳 entry：

```python
goal_engine = str((rec or {}).get("engine") or goal.get("engine", "") or "")
entry = {"goal_id": goal_id, "stage": stage, ..., "engine": goal_engine, ...}
```

`_observe_unmanaged` 的 `self._local[stem].setdefault` 同键塞 `engine` 字段
（来自 `load_profile_at` 解析,同源同 stem 本地也想跑 vllm + aphrodite 时不撞行）。

center `record_model_states` 从 entry 读 `engine`（默认 `""` 兼容旧 worker）以
`(stem, engine)` 精配 upsert + 按 `(profile, engine)` 元组剪枝。

### 修复 C：`Reconciler` 构造新增 `docker_alive` 回调,`_step` stop 分支兜底（节点 2）

```python
def __init__(..., docker_alive: Callable[[Any], bool] | None = None):
    self._docker_alive = docker_alive or _default_docker_alive
    ...

def _default_docker_alive(profile: Any) -> bool:
    """保守语义:docker 不可用/解析失败一律 True(宁可多走 stopper 空扑,
    不漏杀活容器);非 docker 引擎 False(该 goal 不适用本兜底)。"""
    from modelctl.core.process import docker_container_alive
    from modelctl.engines import get_adapter
    ...
    if not adapter.is_docker_runtime(): return False
    return docker_container_alive(adapter._container_name)
```

`_step` 的 stop 分支：

```python
prof = obs.get("profile")
docker_fallback = prof is not None and self._docker_alive(prof)
if obs.get("up") or obs.get("alive") or docker_fallback:
    out = self._stopper(prof, caps, self._models)
    ...
```

单测注入 `Reconciler(docker_alive=lambda prof: True)` 即可,不依赖 docker daemon。

### 修复 D：`_goal_views` 改用三键精配 + 空串回退（节点 1 的展示侧）

```python
states: dict[tuple[str, str, str], dict] = {}
for s in store.list_model_states():
    states[(s["node_id"], s["profile"], s.get("engine", "") or "")] = s
for g in rows:
    engine = str(g.get("engine", "") or "")
    st = states.get((g["node_id"], g["profile"], engine))
    if st is None and engine:
        st = states.get((g["node_id"], g["profile"], ""))   # 防御性回退
    out.append(_goal_view(g, st, now=now))
```

engine 缺省回退空串：防止"新库 + 旧 worker"过渡期 worker 还没上报 engine 时
goal 视图整列变 `None`。

## 契约 / 数据兼容

- **worker 上报**：`engine` 字段新增，center 默认空串不当 error（缺维度降级为
  空串键，兼容旧 worker）；旧 worker 继续上报时按 `(profile, "")` 存在，新
  rec 上报 `(profile, engine)` 时 `ON CONFLICT(node_id, profile, engine)` 各落
  各键，**过渡期不会互相覆盖**。
- **SQLite 迁移**：`init_db` 启动时先 `_ensure_columns` 调
  `_ensure_model_state_engine`（ADD COLUMN）+ `_migrate_model_states_pk`
  （建临时副本去重插入→DROP 旧表→RENAME），单进程单实例（Design 裁决 1）
  保证不会被其它写者介入。
- **测试**：`test_cluster_store.py::test_explicit_columns_cover_full_row` 全列
  清单加 `"engine"`；`test_cluster_goals.py::test_remove_goals_prunes_model_state`
  因 `delete_model_state(node, profile, engine="")` 空串全删而行为不变。

## 验证口径

```powershell
# 1) 新旧库幂等迁移（单进程,旧库 + 新代码一起跑):
python -m modelctl start --role control-plane --center-port 4173; # 看 db 日志在启动路径跑 _migrate_model_states_pk
# 2) pytest 5 簇全 PASS：
python -m pytest tests/test_cluster_store.py tests/test_cluster_reconcile.py `
    tests/test_cluster_goals.py tests/test_cluster_governance.py tests/test_cluster_http.py -v
# 3) docker ps 复核：UI stop 后容器应为 Exited / 已消失(不再出现假 STOPPED)
```

与 `polling-overlap-err-aborted.md` 是两簇独立问题——前者 server state 撒谎、
后者 client 网络日志。本次修评里顺手一起带上是因为同一会话里两簇都遇到、都在
cluster 页面——分开文档方便后续按问题不是按时间检索。
