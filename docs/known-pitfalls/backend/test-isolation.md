# 测试隔离与 pytest 收集陷阱

> 原始单文件已并入本文件归档，保留溯源信息。

## 生产代码 `load_env()` 把本地 .env 泄漏进测试进程，用例结论随开发者机器漂移

**日期**：2026-09-03
**症状**：`test_gateway.py::test_anthropic_messages_404_unknown_model` 全量跑必挂、
单跑必过（`assert 200 == 404`）—— 典型的顺序依赖污染。

**根因**：`core/envfile.py` 的 `load_env()` 用 `os.environ.setdefault(...)` 注入 `.env`，
而 `cli.py` 入口、`gateway.main()`、以及 `webui/admin_*.py` 的**十几个端点**都会调它
（`admin_auth._ensure_env_loaded()` 在首次认证时懒加载）。测试一旦触发这些路径：

```python
# admin_auth._ensure_env_loaded() → load_env() → os.environ.setdefault(...)
# 开发者 .env 里有 GATEWAY_DEFAULT_MODEL=qwen3.8
```

注入的是**真实进程环境**，`monkeypatch` 管不到它（monkeypatch 只回滚它自己 set 的键），
于是永久存活到 session 结束。后续用例调 `create_app(default_model=None)`，
而 `create_app` 内部 `default_model = default_model or os.environ.get("GATEWAY_DEFAULT_MODEL")`
被 env 兜底 → 未知 model 不再 404 而是落到默认模型 → 200。

**解决方案**：在 `tests/conftest.py` 的 autouse fixture 里**每个用例前强制清除**这些键，
让测试只认用例自己显式设置的值：

```python
monkeypatch.delenv("GATEWAY_DEFAULT_MODEL", raising=False)
monkeypatch.delenv("GATEWAY_CONTEXT_SWITCH", raising=False)
```

**教训**：

- **「读 env 的生产代码」与「测试隔离」是一对天然冲突**：只要生产路径里存在
  `os.environ.setdefault`（而非显式传参），测试环境就会被开发者本地 `.env` 污染，
  表现为「CI 绿、本地红」或「全量红、单跑绿」。conftest 里对**所有会被 .env 影响且
  改变控制流的键**做 `delenv` 白名单，是成本最低的止血。
- 定位这类污染的通法：**全量跑失败、单跑通过 ⇒ 一定有人改了全局态**。先按嫌疑文件
  `pytest A.py B.py::test` 两两组合复现，再 grep 生产代码里所有 `os.environ.setdefault`
  / `load_env()` 调用点。
- 别只盯着 `CACHE_DIR`/`LOG_DIR`。本轮之前就隔离了目录类变量，但**改变业务分支的
  env（默认模型、开关）同样要隔离**，且更致命。

## `tests/` 目录里的调试脚本：模块级 `sys.exit()` 掀翻整个 pytest session

**日期**：2026-09-03
**症状**：`pytest` 全量跑输出 `INTERNALERROR> SystemExit: 1`，一个用例都没跑完。

**根因**：`test_webui_smoke.py` / `test_route_debug.py` 是**冒烟脚本**冒充测试文件
（`test_*.py` 命名），模块顶层直接执行副作用：

```python
os.environ.setdefault("API_KEY", "test_key_12345")   # 收集阶段就污染全局
app = create_app(admin=True)                          # 收集阶段就构建应用
...
sys.exit(rc)                                          # 收集阶段直接掀桌
```

pytest **import 测试文件即执行模块体**，`sys.exit` 抛的 `SystemExit` 被 pytest 当作
内部错误，整个 session 终止。`os.environ.setdefault` 同样是上一节的污染源。

**解决方案**：改写为标准用例——`API_KEY` 用 `monkeypatch.setenv`（测试后自动回滚），
建 app 放进 fixture，打印改断言，`sys.exit(rc)` 删除：

```python
@pytest.fixture()
def admin_client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", KEY)
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c
```

**教训**：

- `test_*.py` 的**模块顶层是禁止执行区**：任何 I/O、建连接、起服务、`sys.exit`、
  `os.environ` 写入都会在 collection 阶段发生，且不受 fixture 生命周期约束。
- 真需要保留一次性冒烟脚本，命名避开 `test_` 前缀（如 `scripts/smoke_admin.py`），
  或加 `if __name__ == "__main__":` 守护。
- 顺带发现：**该 FastAPI 版本 `app.routes` 平铺不到 `include_router` 的子路由**
  （懒展开的 `_IncludedRouter` 内部结构），遍历会得到空集合而误判「路由没挂」。
  枚举路由改用稳定公开口径 `app.openapi()["paths"]`。

## conftest 的 env 清理必须按前缀扫描，白名单枚举必然漏新增键

**日期**：2026-09-05
**症状**：`tests/conftest.py` 的 autouse fixture 用 9 行
`monkeypatch.delenv("CLUSTER_ROLE", ...)` 逐键枚举清理 CLUSTER_* 泄漏。M1 新增
`CLUSTER_START_TIMEOUT_S`、`CLUSTER_RECONCILE_INTERVAL_S` 等配置键后，枚举清单没有
同步——开发者 .env 里只要有这些键，就会经 `load_env()` 泄漏进测试进程，静默改变
reconciler/agent 的节拍与超时行为，而 conftest 看起来"已经清理过 CLUSTER_*"，
排查时最先被排除。

**根因**：白名单枚举是**与生产配置键集合赛跑**的防御——每新增一个可被 .env 注入的
键就多一处必漏点，且泄漏是"测试变慢/变快"级别的软污染，不像 404 变 200 那样立刻
炸红，往往要等跨机器结论漂移才被发现。枚举清单的"看起来完整"是最危险的假象。

**解决方案**：按**前缀**扫描删除，让"新增键"落入清理范围成为默认行为：

```python
for key in list(os.environ):
    if key.startswith("CLUSTER_"):
        monkeypatch.delenv(key, raising=False)
```

（`list(os.environ)` 先取快照，避免迭代中改字典；`GATEWAY_*` 仅 2 键且语义独立，
保留显式枚举反而更点名——前缀式适用于"同族键会持续新增"的场景。）

**教训**：

- env 隔离清单的粒度应与**配置的演化粒度**一致：一族会持续新增键的配置（CLUSTER_*）
  用前缀；少量彼此独立的开关（GATEWAY_*）可枚举，但要在注释里写明"本族不再扩键"
  的前提，前提失效即改前缀式。
- 判据："这个前缀下未来会不会加新键？"会 ⇒ 枚举必然漏 ⇒ 用前缀扫描。漏一个键的
  代价不是失败而是**软污染**（结论随机器/时序漂移），比硬失败更难归因。
