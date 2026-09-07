# WebUI 启动失败提供环境创建入口 —— 设计

- 日期：2026-09-07
- 状态：待审阅
- 相关提交：无（新增特性）
- 相关设计：`2026-09-07-envs-docker-bypass-guide-design.md`（环境页 Docker 旁路指引，已实现）

## 1. 问题

点击模型详情页"启动"后，若该引擎的托管 venv 不存在，用户在 toast 里看到：

```
qwen2.5-1.5b-vllm start 失败：vllm 的专用环境未创建，请先执行：modelctl env setup vllm
```

文案要求用户去执行一条 **CLI 命令**，但 WebUI 早已具备同等的图形化能力——`/envs` 环境页每个引擎都有 Setup 按钮（`POST /admin/api/envs/{target}/setup`）。用户不知道它存在，只能离开 WebUI 开终端。失败提示把用户指向了错误的一端。

错误的产生链（保持不变，仅作定位参考）：

- `engines/vllm.py:55` → `envs.ensure_env("vllm")`
- `core/envs.py:122` 抛 `EngineEnvError("<target> 的专用环境未创建，请先执行：modelctl env setup <target>")`
- `core/all_service.py` 的 `start_profile` 或 `_do_start` 的异常分支收敛为任务失败
- `stores/tasks.ts:143` `toast.error(...)` 呈现

## 2. 目标与非目标

### 目标

1. 因"环境缺失"或"Docker 未就绪"导致的启动失败，在任务抽屉的失败卡片上提供**可点击的修复入口**，而不是只给一句 CLI 文案。
2. Linux 部署机上入口直接触发该引擎的 `env_setup` 任务（复用既有端点，不新增）。
3. 非 Linux（Windows 开发机）上托管 venv 客观不可创建，入口改为跳转环境页并定位到 Docker 旁路指引。
4. 错误分类由后端给出**结构化字段**，前端不解析错误文案。

### 非目标（明确排除）

- 不自动重试启动：环境创建成功后由用户回模型详情页手动点"启动"（失败归因互不污染）。
- 不改 `envs.setup` 的签名、不加 uv 输出行回调：环境任务维持现有"状态级 step 事件"，实时安装日志留作后续独立需求。
- 不自动为 profile 写入 `docker_image`、不自动拉镜像（属 Docker 旁路自动化，另一个议题）。
- 不改 toast 的形态（toast 生命周期短，不放交互按钮；入口一律在任务抽屉）。
- 不动鉴权、不新增 HTTP 端点。

## 3. 方案

在既有任务总线上补一个**错误分类语义**：后端分类 → 任务对象携带 `code`/`engine` → SSE `done` 事件与 `to_dict()` 透传 → 前端按 `code` 渲染修复动作。

被否决的替代方案：

- 前端正则从 detail 里抓 `modelctl env setup (\w+)`：零后端改动，但把 UI 契约建立在可变的错误文案上，改一个标点按钮即失效。
- 后端分类 + 前端解析混用：责任边界模糊，两处都要维护文案知识。

## 4. 详细设计

### 4.1 后端：共享分类函数

新增于 `src/modelctl/core/cluster/reconcile.py`。既有 `_ERROR_RULES`（L128-149）已含 `("专用环境未创建", "venv_missing")` 等关键字规则，本函数**以该表为唯一关键字源**，仅按码过滤，避免两处规则各自漂移：

```python
#: 启动失败中"WebUI 可提供修复入口"的错误码（顺序即优先级，取自 _ERROR_RULES）
_ACTIONABLE_CODES = ("docker_missing", "venv_missing")


def classify_start_failure(detail: str, engine: str) -> tuple[str | None, str | None]:
    """启动失败详情 → (错误码, 相关引擎)；无法归类时 (None, None)。"""
```

实现即：遍历 `_ERROR_RULES`，命中的首个规则若其 `code` 属于 `_ACTIONABLE_CODES`，返回 `(code, engine)`；命中不可操作码或未命中一律 `(None, None)`。

需向 `_ERROR_RULES` 补一条新规则（集群侧 `classify_error` 同样受益）：

| 错误码 | 触发关键字 | 语义 |
|---|---|---|
| `docker_missing` | `Docker 环境未就绪` | profile 配了 `docker_image` 但 docker CLI/daemon 不可用（`engines/vllm.py:44` 的 `RequirementError`） |

> **顺序陷阱（务必遵守）**：`Docker 环境未就绪` 同时包含既有关键字 `环境未就绪`（映射 `venv_missing`）。新规则**必须插在 `("环境未就绪", "venv_missing")` 之前**，否则会被 venv_missing 抢先命中，Windows 用户将被引导去创建一个在 Windows 上根本不可用的托管 venv。`_ERROR_RULES` 的表头注释已声明"先具体后笼统"，此条是该原则的又一实例，插入时在注释中点名。

`engine` 一律由调用方从 `profile.engine` 直传，**不从 detail 猜测**。既有 `classify_error`（L152，注意实名无下划线前缀）保持"兜底 `runtime_capability`"语义不变，仅新增 docker 规则影响其归类。

### 4.2 后端：Task 携带错误码

`src/modelctl/core/webui/admin_tasks.py`：

- `Task` 增加字段 `code: str | None = None`、`engine: str | None = None`。
- `error()` 签名改为 `error(self, exit_code: int = 1, message: str = "", *, code: str | None = None, engine: str | None = None)`；赋值后随 `done` 事件广播。
- `done` 事件 payload 与 `to_dict()` **仅在非 None 时**包含这两个键——旧前端与既有测试的断言形状不变。

`src/modelctl/core/webui/admin_models.py`：

- `_do_start` 的 `result.status == "error"` 分支与 `except Exception` 分支都调用 `classify_start_failure(detail, profile.engine)`，命中才把 `code`/`engine` 传给 `task.error(...)`。
- `_do_restart`（同款启动路径）同样处理。
- 其余任务类型（service/all/env_setup/trtllm_build）本期不接入分类，行为完全不变。

### 4.3 前端：类型与 store

`web/src/api/types.ts`：`TaskInfo` 与 `done` 事件类型加可选 `code?: string`、`engine?: string`。

`web/src/stores/tasks.ts`：`TaskRecord` 加 `code`/`engine` 两个可选字段；SSE `onDone` 与降级轮询 `getTask` 两条路径都写入（后者已从 `to_dict()` 带出字段，天然可用）。`finalize` 签名相应扩展，保持"幂等终态"逻辑不变。

### 4.4 前端：失败卡片的修复动作

`web/src/components/layout/TaskDrawer.vue` 的失败任务卡片，当 `task.code` 存在时渲染动作区：

- `code === "venv_missing"`：查 `GET /envs` 结果中该 `engine` 的 `platform_supported`
  - `true` → **[创建环境]** 按钮：调既有 `envSetup(engine)`（`web/src/api/envs.ts`），新任务照常进抽屉跟踪；卡片副文案「环境创建成功后，回到模型详情页点击启动」。
  - `false` → **[去环境页]** 按钮：`router.push("/envs?focus=" + engine)`。
- `code === "docker_missing"` → 仅 **[去环境页]**：`router.push("/envs?focus=" + engine)`。
- `/envs` 结果未加载或查不到该 engine：降级为 **[去环境页]**（保守可用，不猜平台）。

`envSetup` 提交返回 409（同 target 任务冲突）时沿用 `TaskButton` 既有处理口径：toast 提示已有任务在跑。

`web/src/views/EnvsView.vue` 新增 query 处理：挂载后读 `route.query.focus`，命中则 `scrollIntoView` 到该引擎卡片；若该引擎 `!platform_supported`，额外高亮 Docker 旁路区块（一次性视觉强调，用后清 query 以免刷新重复滚动）。

`web/src/api/envs.ts`：确认/补充 `listEnvs()` 复用（既有 `GET /envs` 已在用），不新增 API。

### 4.5 交互流（Linux 正常路径）

1. 详情页点「启动」→ 任务 `model_start` 失败，`done` 带 `code=venv_missing`、`engine=vllm`
2. toast 照旧弹文案；抽屉里该失败卡片显示 **[创建环境]**
3. 点按钮 → `env_setup` 任务（28min+）进抽屉，状态实时推进
4. 该任务成功 → 用户回详情页点「启动」→ 成功

Windows 路径第 3 步替换为：点 **[去环境页]** → `/envs?focus=vllm` → 看到 Setup 按钮禁用原因与 Docker 旁路指引。

## 5. 错误处理与边界

- **分类是关键字匹配**，引擎文案变更会让按钮消失。失效模式为退化回今天的纯文案提示，无功能回归；`classify_start_failure` 的关键字集中一处，改文案时单点跟进。
- **未命中分类绝不渲染按钮**（`(None, None)`），避免在端口冲突、OOM 等无关失败上挂"创建环境"这种误导动作。
- 环境创建任务自身失败：卡片维持现状（既有重试按钮），本期不加二次引导。
- 页面刷新后：任务经 `bootstrap` 恢复时走 `getTask`，`code`/`engine` 字段随 `to_dict()` 带回，按钮不丢。
- `/envs` 请求失败（如 401）：按"查不到"降级为跳转按钮，不阻塞。
- 并发：同一引擎重复点「创建环境」由后端 target 互斥（409）挡住，前端只做提示。

## 6. 测试策略

后端（pytest，TDD）：

1. `classify_start_failure` 纯函数（`tests/test_admin_tasks.py` 或新文件）：EngineEnvError 文案 → `venv_missing`；`Docker 环境未就绪` 文案 → `docker_missing`（优先于 venv，锁死顺序陷阱）；端口冲突/OOM/空 detail → `(None, None)`。
2. `tests/test_cluster_reconcile.py`：`classify_error("docker_image 已配置但 Docker 环境未就绪…")` → `docker_missing`，且既有用例（`test_classify_error_rules_in_priority_order` 参数表）全数不回归。
3. 端点级（`tests/test_admin_tasks.py` 风格）：注入 venv 缺失失败的 `start` → `GET /tasks/{id}` 的 JSON 含 `code`/`engine`；注入正常失败 → 两键缺席（向后兼容断言）。

前端：仓库无 vitest 基建（`web/package.json` 无 test 脚本），验证为 `npm run build`（`vue-tsc --noEmit` 严格类型）+ 下列手工冒烟：

- Linux：造一个无 venv 的 profile → 启动失败 → 抽屉出现 [创建环境] → 点击后抽屉出现 env_setup 任务
- Windows：同操作 → 出现 [去环境页] → 跳转后定位到 vllm 卡片且 Docker 旁路区块高亮
- 无关失败（占用端口 6006 造冲突）→ 失败卡片**无**任何修复按钮
- 刷新页面 → 失败任务与按钮仍在

## 7. 影响文件清单

| 文件 | 改动 |
|---|---|
| `src/modelctl/core/cluster/reconcile.py` | 新增 `classify_start_failure`；`_ERROR_RULES`/`_classify_error` 复用之；补 `docker_missing` |
| `src/modelctl/core/webui/admin_tasks.py` | `Task.code`/`engine` 字段；`error()` 扩展；条件进 payload/to_dict |
| `src/modelctl/core/webui/admin_models.py` | `_do_start`/`_do_restart` 接分类并透传 |
| `web/src/api/types.ts` | `TaskInfo`/done 事件加可选 `code`/`engine` |
| `web/src/stores/tasks.ts` | `TaskRecord` 字段 + 两路 finalize 透传 |
| `web/src/components/layout/TaskDrawer.vue` | 失败卡片动作区（创建环境 / 去环境页） |
| `web/src/views/EnvsView.vue` | `?focus=` 定位与旁路区块高亮 |
| `tests/test_admin_tasks.py`（实名，既有任务端点测试文件） | 分类纯函数 + 端点级 code 断言 |
| `tests/test_cluster_reconcile.py` | `classify_error` 新增 docker 规则的优先级用例 |

## 8. 后续（不在本期）

- `envs.setup` 增加行回调，环境安装任务的 uv 输出实时流入抽屉 log 事件。
- 环境任务成功后在原失败卡片点亮「重新启动」（跨任务链状态）。
- Windows Docker Desktop 未启动的场景化引导（现仅跳转环境页），依赖未实现的 `2026-09-07-envs-docker-install-windows-only-design.md`。
- 前端引入 vitest，为 store/组件补自动化覆盖。
