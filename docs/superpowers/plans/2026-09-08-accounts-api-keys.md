# 多账号 / API Key 体系 实施计划

- 日期：2026-09-08
- 状态：计划已定稿（待执行）
- 关联 spec：`docs/superpowers/specs/2026-09-08-accounts-api-keys-design.md`（已确认并提交，commit `36f4284`）
- 执行方式：Subagent-Driven（推荐）或 Inline，待用户选择

---

## Goal

把当前仅单把共享 key 的网关升级为**账号 + 多 API Key**体系：账号下可签发多个 Key，Key 继承账号级**并发 / RPM / TPM / token 预算**限额；保留每个账号的**对话信息**（会话分组/搜索/查看/导出/删除）；新增 **webui 管理员面板**与**账号自助面板**。默认 `ACCOUNTS_ENABLED=false`，行为与现状完全一致（零回归）。

## Architecture

```
客户端 ──Authorization: Bearer sk-mctl-...──▶ ① nginx ──▶ ② 网关(数据面)
                                          x-api-key         │ verify_client()→accounts.resolve_key()
                                                            │ limits.check(预算/并发/RPM/TPM)→401/429
                                                            │ 代理转发(流式) → finally 并发槽释放
                                                            │ 结束后 account.settle()→ 异步队列
                                                            ▼
管理面 ──API_KEY / JWT──▶ webui ──▶ /admin/api/*(账号管理) + /api/account/*(自助)
                                                │
                                                └────▶ ③ SQLite(modelctl_accounts.db)
```

- 数据面：`core/gateway.py` 串接鉴权解析 + 四道限流 + 会话/用量截获。
- 管理/API：`core/webui/` 新增两套路由（`admin_accounts` + `account_self`）与 JWT 认证。
- 存储：新增 `core/accounts/store.py`（stdlib sqlite3，单连接 + 写锁 + WAL + 幂等加列），五表。

## Tech Stack

- 后端：Python 3.12 + FastAPI（已存在于 gateway 子项目）+ stdlib `sqlite3` + `bcrypt` + `PyJWT`。
- 前端：Vue 3 + Element Plus + `<script setup>` + vue-tsc（沿现有 `web/` 工程约定）。
- 存储：本地 SQLite 单文件 `data/modelctl_accounts.db`。
- 测试：pytest（现有 `tests/`）；网关/账号测试在 gateway venv 中运行。

## Global Constraints（来自 CLAUDE.md / spec）

1. **DDL 禁止自动执行**：`modelctl_accounts.db` 是应用自管 SQLite（非线上生产库），属业务库 DDL，允许用 `CREATE TABLE IF NOT EXISTS` + `_ensure_columns` 幂等建表。**不得**对任何线上 MySQL 执行 DDL。
2. **UI 不显示真实 ID**：Key 只展示 `key_prefix`（`sk-mctl-***x4`）；时间统一 `YYYY-MM-DD HH:mm:ss`；cluster `node_id` 除外（既有裁决，与本次无关）。
3. **CJK 对齐**：CLI/日志/表格可能含中文的字段用 `display_width` + `pad_width`，禁止 `len()`/`f"{x:<N}"`。
4. **接口全方法同路由组**：GET 列表 / GET 单条 / POST / PUT / DELETE 在同一路由组定义，避免 405/404。
5. **错误 OpenAI 兼容信封**：`{"error": {"message", "type"}}`，限流带 `Retry-After`。
6. **查询显式列名**：零 `SELECT *`；含中文的 SQL 顶部 `SET NAMES utf8mb4`（本库无中文 SQL，纯参数绑定）。
7. **SSE 不能用 BaseHTTPMiddleware**：四道限流在处理器首行显式调用，流式释放挂在流完成回调。
8. **依赖落点**：`bcrypt` + `PyJWT` 加入 `gateway/pyproject.toml`（accounts 包只在 gateway/webui 进程使用，二者均在 gateway venv 运行）。

---

## Task 1 — 依赖与配置底座

**Files**
- 新增/修改：`gateway/pyproject.toml`（依赖）、`src/modelctl/core/paths.py`（`accounts_db_path()`）、`.env.example`（配置项）
- 测试：`tests/test_accounts_paths.py`（新增）

**Interfaces**
- `paths.accounts_db_path() -> Path`：优先 `ACCOUNTS_DB_PATH`（相对按 `PROJECT_ROOT` 解析），默认 `DATA_ROOT / "modelctl_accounts.db"`。
- 环境变量：`ACCOUNTS_ENABLED`（bool，默认 `false`）、`ACCOUNTS_DB_PATH`。

**Steps（TDD：写失败测试 → 确认失败 → 实现 → 通过 → 提交）**

1. 写 `test_accounts_paths.py`：设置 `ACCOUNTS_DB_PATH` 相对/绝对/缺省三种情况断言 `accounts_db_path()` 返回正确路径；未设 `ACCOUNTS_ENABLED` 时 `accounts_enabled()` 返回 False。
2. 运行测试确认失败（函数不存在）。
3. 在 `paths.py` 增加 `accounts_db_path()`；新增 `core/accounts/__init__.py` 空包 + `accounts_enabled()`（读 env 转 bool，`_parse_env_bool` 复用 `core/stats` 语义或独立实现）。
4. 运行测试通过。
5. 更新 `gateway/pyproject.toml` 加 `bcrypt>=4.0`、`PyJWT>=2.8`；`.env.example` 追加两个配置键。
6. 提交（消息：`feat(accounts): 依赖与配置底座`）。

---

## Task 2 — accounts/store.py（SQLite DAO）

**Files**
- 新增：`src/modelctl/core/accounts/store.py`、`tests/test_accounts_store.py`
- 参考范式：`core/cluster/store.py`（单连接 + 写锁 + WAL + `_ensure_columns` + 显式列清单）

**Interfaces（`AccountsStore`）**
- `__init__(db_path: Path | None)` / `init_db()` / `close()`
- 建表：`users` / `api_keys` / `usage_records` / `sessions` / `messages`（`CREATE TABLE IF NOT EXISTS` + `_ensure_columns` 幂等加列；`AUTO_INCREMENT` 列显式 `PRIMARY KEY`）
- users：`create_user(username, password_hash, display_name, is_admin, concurrency, rpm, tpm, token_budget, budget_period, retention_days) -> id`、`get_user_by_username`、`get_user_by_id`、`list_users`、`update_user_limits`、`set_user_status`、`set_user_password_hash`、`increment_budget_consumed`、`reset_budget`
- api_keys：`create_key(user_id, key_hash, key_prefix, name, expires_at) -> id`、`get_key_by_hash`、`get_key_by_id`、`list_keys_for_user`、`set_key_status`、`touch_key_last_used`
- usage_records：`insert_usage(...)`、`sum_usage_for_user(user_id, since)`、`list_usage_for_user`
- sessions/messages：`get_or_create_session(user_id, key_id, model, session_id, title)`、`bump_session`、`add_message`、`list_sessions(user_id, q)`、`get_session`、`export_session`、`delete_session`、`search_messages`

**Steps（TDD）**

1. 写 `test_accounts_store.py`：schema 建库、唯一约束（username unique / key_hash unique）、五表 CRUD 往返、幂等加列（重复 `init_db` 不报错）、零 `SELECT *`（显式列清单护栏）。用 `tmp_path` 建临时库。
2. 运行确认失败。
3. 实现 `store.py`（`row_factory=sqlite3.Row`、`isolation_level=None`、WAL、`busy_timeout`、`threading.Lock` 写串行化，方法内部 `with self._lock`）。
4. 运行通过。
5. 提交（消息：`feat(accounts): accounts store SQLite DAO`）。

---

## Task 3 — accounts/hashing.py（bcrypt + Key 生成/脱敏）

**Files**
- 新增：`src/modelctl/core/accounts/hashing.py`、`tests/test_accounts_hashing.py`

**Interfaces**
- `hash_password(plain: str) -> str`（bcrypt）
- `verify_password(plain: str, hashed: str) -> bool`
- `generate_api_key() -> str`：`sk-mctl-` 前缀 + 高熵随机段
- `hash_api_key(key: str) -> str`：sha256 hex
- `key_prefix(key: str) -> str`：`sk-mctl-***x4`（复用 cluster/store `mask_tail` 语义）

**Steps（TDD）**

1. 写测试：bcrypt 往返、verify 错密 False、`hash_api_key` 确定性、`key_prefix` 脱敏（短 key 一律 `***`）、`generate_api_key` 满足前缀/长度/唯一性。
2. 运行确认失败。
3. 实现（`bcrypt` 延迟导入，避免主包无此依赖时 import 崩溃；`hashlib.sha256` 用于 Key）。
4. 运行通过。
5. 提交（消息：`feat(accounts): bcrypt 密码哈希与 API Key 生成`）。

---

## Task 4 — accounts/limits.py（LimitGuard）

**Files**
- 新增：`src/modelctl/core/accounts/limits.py`、`tests/test_accounts_limits.py`

**Interfaces**
- `class UsageLimitError(Exception)`：携带 `status`（429）、`type`（`budget_exceeded` / `concurrency_exceeded` / `rate_limit_exceeded`）、`message`、`retry_after`
- `@dataclass UserPolicy`：`concurrency_limit, rpm_limit, tpm_limit, token_budget, budget_period, budget_reset_at, budget_consumed, retention_days`（`0/None` 视为不限制）
- `class LimitGuard`（进程内单例，纯内存计数器）：
  - `reset_budget_if_needed(user, store) -> None`（`now >= budget_reset_at` 则清零 + 推进 period 并持久化；惰性，无定时器）
  - `acquire(user_id, limit)` / `release(user_id)`
  - `check_rpm(user_id, limit)`（固定 60s 窗口 `{user_id:(window,count)}`，进窗重置，成功 +1）
  - `check_tpm_reserve(user_id, limit, est_total)`（预估 pre-占，超限拒）
  - `add_tpm_actual(user_id, actual)`（请求结束把实际 total 加回窗口）
  - `check_budget(policy)`（`budget_consumed < token_budget`）

**Steps（TDD）**

1. 写测试：acquire→release 计数与并发超限码；RPM 进窗重置 + 满额拒；TPM 预估 + 实际回补；预算 `budget_reset_at` 到期惰性重置（注入 `monotonic/now` 可控）；`0` 视为不限制；错误 `type` 穷举（budget/concurrency/rate_limit）。
2. 运行确认失败。
3. 实现 `LimitGuard`（进程内存态；预算重置/消费持久化经 `store`，先只做内存判定，持久化交给 Task 5 accountant + Task 2 store）。
4. 运行通过。
5. 提交（消息：`feat(accounts): 并发/RPM/TPM/预算 LimitGuard`）。

---

## Task 5 — accounts/auth.py（resolve_key）+ accountant.py（异步结算）

**Files**
- 新增：`src/modelctl/core/accounts/auth.py`、`src/modelctl/core/accounts/accountant.py`、`tests/test_accounts_auth.py`、`tests/test_accounts_accountant.py`

**Interfaces**
- `auth.extract_credential(request) -> str`（Bearer / `x-api-key` 双通道，与 gateway `verify_client` 一致）
- `@dataclass AccountIdentity`：`user_id, key_id, key_prefix, username, is_admin, policy`
- `auth.resolve_account(store, credential) -> AccountIdentity | None`（按 `hash_api_key(credential)` 查 `api_keys`，join 出 `users` 策略；无效/禁用/过期返回 None）
- `accountant.Accountant(store)`：
  - `settle(*, user_id, key_id, model, prompt_tokens, completion_tokens, status, client_ip, ttft_ms, latency_ms, session_id, title, user_msg, assistant_msg) -> None`（异步入队，非阻塞）
  - `start() / stop()`（后台线程 drain 队列；写 `usage_records` + 累加 `budget_consumed` + upsert `sessions`/`messages`；异常仅日志）

**Steps（TDD）**

1. 写测试：
   - auth：`extract_credential` 双通道；`resolve_account` 有效 Key 带出 policy、无效/禁用/过期 Key 返回 None。
   - accountant：`settle` 后 `store` 里出现 usage 记录、budget_consumed 累加、session 首建 + message_count 递增；两次 settle 同 session_id 归并。
2. 运行确认失败。
3. 实现 `auth.py`、`accountant.py`。
4. 运行通过。
5. 提交（消息：`feat(accounts): 账号 Key 解析与异步记账`）。

---

## Task 6 — 网关数据面接入（gateway.py）

**Files**
- 修改：`src/modelctl/core/gateway.py`
- 测试：`tests/test_accounts_gateway.py`（新增，走 `create_app(transport=MockTransport, ...)` + `ASGITransport`）

**Interfaces / 集成点**
- `create_app` 新增参数 `accounts_enabled: bool | None = None`（缺省读 env）、`accounts_store: AccountsStore | None`（测试注入）。
- 当启用时：构建单例 `LimitGuard` / `Accountant`，赋 `app.state.accounts`、`app.state.limit_guard`、`app.state.accountant`，并 `init_db()`。
- 三个处理器（`list_models` / `anthropic_proxy` / `proxy`）首行显式调用一个新增模块级函数 `accounts_gate(request, app)`：
  - 未启用 → 沿用现有 `verify_client`（零回归）。
  - 启用 → `extract_credential` → `resolve_account`（None → 401 `invalid_api_key`）→ `LimitGuard` 四道检查（预算→并发 `acquire`→RPM→TPM 预估；任一失败 → 429 信封 + `Retry-After`）。
- 代理转发（流式/非流式）结束后：
  - `finally`：`limit_guard.release(user_id)`（SSE 在流生成器 finally 释放，保证流结束才释放）。
  - 结束：`accountant.settle(...)` 入队；`limit_guard.add_tpm_actual`；`audit_log.record(...)` 新增 `key_id/user_id` 字段（扩展 `_build_audit_entry` 签名，带默认值以免破坏既有调用/既有审计文件解析）。
- 上游 key / 验完即丢 / 失败请求落审计逻辑保持不变。

**Steps（TDD）**

1. 先写**回归护栏**测试：`ACCOUNTS_ENABLED` 未设时，现有用例（`test_gateway.py` 存活）零回归；/v1* 无 key 401、带 key 透传。
2. 写启用分支用例：无效 Key→401 `invalid_api_key`；预算耗尽→429 `budget_exceeded`；并发超限→429；RPM/TPM 超限→429；流式结束并发槽释放；会话按 `X-Session-Id` 归属；`settle` 落库。
3. 运行确认失败。
4. 实现接入逻辑。
5. 运行通过。
6. 提交（消息：`feat(gateway): 账号 Key 数据面接入（限额/会话/结算）`）。

---

## Task 7 — WebUI 账号认证（JWT）+ 管理路由 + 自助路由

**Files**
- 新增：`src/modelctl/core/webui/account_auth.py`、`src/modelctl/core/webui/admin_accounts.py`、`src/modelctl/core/webui/account_self.py`
- 修改：`src/modelctl/core/webui/admin_router.py`（`_SUBROUTER_MODULES` 追加 `("modelctl.core.webui.admin_accounts", "")`）
- 测试：`tests/test_webui_admin_accounts.py`

**Interfaces**
- `account_auth`：
  - `login(username, password) -> JWT`（bcrypt 校验，payload 含 `user_id/is_admin`，`iat/exp` 用秒）
  - `verify_token(token) -> dict`（JWT HS256，SECRET 取 `ACCOUNTS_JWT_SECRET` 或退 `API_KEY`）
  - `require_account` / `require_admin`（Depends：校验 JWT + 归属/权限）
- `admin_accounts.py`（`/admin/api/accounts*`，`Depends(require_auth)` 管理面单钥）：
  - GET `""` / POST `""` / GET `/{id}` / PUT `/{id}` / DELETE `/{id}`（同一路由组，避免 405/404）
  - Key：GET `/{id}/keys` / POST `/{id}/keys`（一次性返回明文 key）/ PUT `/keys/{kid}`（禁用/吊销）/ DELETE `/keys/{kid}`
  - 用量：GET `/usage`（按账号累计 / 预算消耗）。
- `account_self.py`（`/api/account/*`，`Depends(require_account)`，只能动自己的资源）：
  - POST `/login`（无鉴权；成功签发 JWT）
  - GET `/keys` / POST `/keys` / PUT `/keys/{id}` / DELETE `/keys/{id}`
  - GET `/usage`
  - GET `/sessions` / GET `/sessions/{id}` / GET `/sessions/{id}/export` / DELETE `/sessions/{id}` / GET `/sessions/search`

**Steps（TDD）**

1. 写测试：登录 200/401；无 token 访问 `/api/account/*` 401；JWT 过期/篡改 401；管理员面板 CRUD 账号；账号只能访问自己的 Key/会话；导出内容正确；路由组五方法齐全。
2. 运行确认失败。
3. 实现 `account_auth.py` + `admin_accounts.py` + `account_self.py`，并在 `admin_router.py` 注册 `admin_accounts`；`gateway.py::create_app(admin=True)` 已挂 `/admin/api`，另需在 `create_app` 挂 `/api/account/*`（或并入同一 admin app，注意不冲突 `/v1` 前缀）。
4. 运行通过。
5. 提交（消息：`feat(webui): 账号 JWT 认证 + 管理员面板 + 账号自助 API`）。

---

## Task 8 — 前端（Vue3）管理/自助面板

**Files**
- 新增：`web/src/api/accounts.ts`、`web/src/api/accountSelf.ts`
- 新增视图：`web/src/views/accounts/AccountsView.vue`（管理员面板）、`web/src/views/accounts/AccountSelfView.vue`（自助面板，含 Key 申请/用量/会话检索导出）
- 修改：`web/src/router/index.ts`（注册 `/accounts`、`/account/self`、`/account/sessions` 等；路由名与组件 `name` 一致以配合 `keep-alive`）、`web/src/components/layout/Sidebar.vue`（菜单项）
- 测试：`web/src/api/accounts.spec.ts` 或沿用现有测试方式（如有）；至少保证 `vue-tsc`/`build` 通过（`verify` 里跑 `npm run build`）。

**Interfaces / 约定**
- 复用 `api/client.ts` axios 实例（Bearer/401 跳登录）；账户自助若走 JWT，需在请求拦截器区分 token 来源（管理面 `API_KEY` vs 账号 JWT），可用双实例或拦截器按路径区分。
- Key 只显示 `key_prefix` 脱敏；时间 `YYYY-MM-DD HH:mm:ss`；长文本 `el-tooltip`；列宽 `min-width`；BEM 类名。
- 路由名与组件 `name` 一致（`<script setup name="Xxx">`）。

**Steps（TDD）**

1. 写前端 api 模块与路由/视图骨架（空态 + 列表 + 表单）。
2. 构建验证失败（类型/未注册路由），迭代补齐。
3. 实现视图（账号 CRUD、Key 签发展示、用量图表/表格、会话检索/查看/导出/删除）。
4. `npm run build`（含 vue-tsc）通过。
5. 提交（消息：`feat(webui): 账号管理/自助前端面板`）。

---

## Task 9 — 回归、文档与 pits 沉淀

**Files**
- 修改：`gps.yaml`/README 相关（如有）、`.env.example`（已含）、`docs/known-pitfalls/README.md`（索引条目）
- 测试：全量 `pytest tests/` + `cd gateway && uv sync --project gateway && pytest tests/test_accounts*.py tests/test_gateway.py tests/test_webui_admin_accounts.py`

**Steps**

1. 跑全量回归：确认 `ACCOUNTS_ENABLED=false` 默认零回归；旧 `test_gateway.py`/`test_audit.py`/`test_webui_smoke.py` 全绿。
2. 手工冒烟：webui 管理员建号 → 账号登录 → 申请 Key → `curl` 打 `/v1/chat/completions` → 核对限额/会话/预算（`ACCOUNTS_ENABLED=true`）。
3. 按 CLAUDE.md 渐进式披露沉淀到 `docs/known-pitfalls/`（如：bcrypt 延迟导入避免主包 import 崩溃；`sqlite` 多线程写锁；SSE 下并发槽释放时机；JWT `exp/iat` 秒制；前端双 token 拦截器区分）。
4. 提交（消息：`docs(accounts): 多账号体系回归与 known-pitfalls 沉淀`）。

---

## 自审清单（spec 覆盖）

- [x] 账号→多 Key（1:N，Key 带用途名）：Task 2/5/7
- [x] 管理员开号 + 账号登录 + JWT：Task 7
- [x] token 配额预算 + 惰性重置 + 持久化：Task 4/5
- [x] 并发 + RPM + TPM 全启用：Task 4/6
- [x] 完整会话管理（分组/搜索/导出/删除）：Task 5/7/8
- [x] 本地 SQLite（五表）：Task 2
- [x] 管理员面板 + 账号自助面板：Task 7/8
- [x] 网关集成四道限流 + 异步结算 + SSE + 审计补 key_id/user_id：Task 5/6
- [x] `ACCOUNTS_ENABLED=false` 零回归：Task 1/6/9
- [x] 错误信封 / `Retry-After` / fail-closed：Task 4/6
- [x] 配置 `.env.example` 两项：Task 1

## 占位符扫描

计划内无 `TODO/FIXME/xxx/占位`；所有接口签名与既有代码（`verify_client`、`estimate_prompt_tokens`、`_build_audit_entry`、`cluster.store` 范式、`webui.admin_auth`、`admin_router._SUBROUTER_MODULES`、`frontend` 工程）对齐。

## 类型一致性

- 时间统一 `YYYY-MM-DD HH:mm:ss`（输出层）；DB 内时间戳采用与 `cluster.store` 一致的 epoch float（`REAL`）或 ISO 字符串，读写两侧统一（Task 2 定稿）。
- `0/None` 限额语义为"不限制"；错误 `type` 枚举固定。
- 前端只显示 `key_prefix` 与脱敏值，不出现真实 key/真实 user id。

---

## 执行选项（待用户确认）

- **Subagent-Driven（推荐）**：按以上 Task 逐个派发 subagent 执行，每 Task 独立 TDD + 独立提交，状态最清晰。
- **Inline Execution**：在单会话内顺序执行全部 Task，连贯但上下文占用大。
