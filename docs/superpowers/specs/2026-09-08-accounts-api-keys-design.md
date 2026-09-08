# 多账号 / API Key 体系设计

- 日期：2026-09-08
- 状态：设计已确认（用户逐节评审通过，采用方案 A）
- 关联：`core/gateway.py`（`/v1*` 数据面）、`core/webui/`（管理面 + SPA）、`core/audit.py`、`core/stats.py`、`data/modelctl_accounts.db`（新增）
- 上游参照：LiteLLM Virtual Keys（虚拟 Key 继承 owner 限额/预算）、One API / New API（用户-令牌-渠道配额与倍率）

## 1. 背景与目标

### 问题

当前网关只有**单把共享 key**：

| 事实 | 位置 |
|---|---|
| 数据面仅一把 `GATEWAY_CLIENT_API_KEY`，fail-closed（未配则 `/v1*` 全 401） | `core/gateway.py` `verify_client` |
| 管理面仅一把 `API_KEY`（Bearer） | `core/webui/admin_auth.py` |
| 无 user/app/tenant 实体、无多 Key 签发、无配额、无按调用方计量 | 2026-09-07 网关鉴权 spec §1 非目标 |
| 审计仅有 `auth` 标签 + `client_ip`，无调用方归属 | `core/gateway.py` `_build_audit_entry` |
| 无会话存储，只有请求级审计 | `core/audit.py` |

结果：无法按人/按应用隔离凭据、无法限流与计费、无法追溯与保留对话。

> **说明**：2026-09-07 网关鉴权 spec §1 的非目标"不做多 key 签发体系（无 user/app/tenant 实体…）YAGNI"已被本次需求**取代**——本轮引入账号/多 Key 实体，属对该声明的升级覆盖。

### 目标

1. 引入**账号（user）**实体，每个账号可申请/被分配**多个 API Key**（1:N）。
2. 按**账号级**限制并发 / RPM / TPM / 预算（token 配额），Key 继承账号策略。
3. 保留该账号的**对话信息**（会话分组/搜索/导出/删除）。
4. 提供 webui **管理员面板**与**账号自助面板**。
5. 与现有单钥模式**兼容**（开关可回退），不破坏既有行为。

### 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 账号与 Key 关系 | 账号 → 多 Key（1:N，Key 带用途名称） |
| 账号开通与登录 | 管理员开号 + 账号用户密码登录 webui（账号可自助申请/管理自己的 Key） |
| 金额度量 | **token 配额**（不引入价格表/真实货币）；预算单位为 token 总量 |
| 限流维度 | 并发数 + RPM + TPM **全启用**；账号级限制，Key 继承 |
| "保留对话信息"程度 | **完整会话管理**（分组、关键词搜索、查看、导出、删除） |
| 存储后端 | 本地 **SQLite**（复用集群 `store.py` 单连接 + 写锁范式） |
| 管理/自服务 | 管理员面板 + 账号自助面板（webui） |

### 非目标

- 不做真实货币计费/价格表、充值、退款。
- 不做多实例分布式限流计数（网关为单进程，进程内计数器即可；多实例需外置计数，属后续迭代项）。
- 不做 HMAC 客户端签名（CLAUDE.md L49 前瞻约定，本轮沿用 Bearer / `x-api-key` 双通道即可）。
- 不做 CLI 账号子命令（本轮仅 webui + 管理 API；可后置）。

## 2. 总体架构

```
客户端 ──Authorization: Bearer sk-mctl-...──▶ ① nginx ──▶ ② 网关(数据面)
                                          x-api-key         │ verify_client()
                                                            │ accounts.resolve_key()   ← 查 Key + 账号策略
                                                            │ limits.check(预算/并发/RPM/TPM) → 401/429
                                                            │ 代理转发(流式) → finally 并发槽释放
                                                            │ 结束后 account.settle()→ 异步队列
                                                            ▼
管理面 ──API_KEY / JWT──▶ webui ──▶ /admin/api/*(账号管理) + /api/account/*(自助)
                                                │
                                                └────────▶ ③ SQLite(modelctl_accounts.db)
```

三层职责：
- **数据面**（`core/gateway.py`）：鉴权解析 + 四道限流 + 会话/用量截获。
- **管理面/API**（`core/webui/`）：账号、Key、用量、会话的管理与自助。
- **存储**（新增 `core/accounts/store.py`）：账号、Key、用量、会话/消息单库持久化。

## 3. 数据模型（本地 SQLite）

新库 `data/modelctl_accounts.db`，stdlib `sqlite3`，单连接 + 写锁，`CREATE TABLE IF NOT EXISTS` + 幂等加列（`_ensure_columns`），查询显式列名（零 `SELECT *`）。

| 表 | 关键字段 | 说明 |
|---|---|---|
| `users` | `id` PK, `username` UNIQUE, `password_hash`(bcrypt), `display_name`, `is_admin`, `status`(active/disabled), `concurrency_limit`, `rpm_limit`, `tpm_limit`, `token_budget`, `budget_consumed`, `budget_period`(day/month), `budget_reset_at`, `retention_days`, `created_at`, `updated_at` | 账号及账号级限额与预算消费 |
| `api_keys` | `id` PK, `user_id` FK, `key_prefix`(展示用), `key_hash`(sha256 UNIQUE), `name`(用途), `status`(active/disabled/revoked), `expires_at`(nullable), `last_used_at`, `created_at` | 账号下多个 Key；限额继承账号 |
| `usage_records` | `id` PK, `user_id`, `key_id`, `request_id`, `model`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `client_ip`, `ttft_ms`, `latency_ms`, `status`, `created_at` | 请求级用量明细 |
| `sessions` | `id` PK, `user_id`, `key_id`, `model`, `title`, `message_count`, `created_at`, `updated_at` | 会话聚合 |
| `messages` | `id` PK, `session_id` FK, `role`(user/assistant), `content`, `prompt_tokens`, `completion_tokens`, `created_at` | 会话内消息 |

## 4. 模块划分与网关集成

### 4.1 新增包 `src/modelctl/core/accounts/`

| 模块 | 职责 | 依赖 |
|---|---|---|
| `store.py` | SQLite DAO（schema + 五表 CRUD）；单连接 + 写锁，幂等加列 | 仅 stdlib |
| `hashing.py` | bcrypt 密码哈希；Key 生成（`sk-mctl-...`）与 sha256 查找/唯一；`key_prefix` 脱敏 | passlib/bcrypt |
| `auth.py` | `resolve_key(request) -> KeyRecord`：解析凭据、查 Key、带出账号策略 | store |
| `limits.py` | `LimitGuard`：并发槽 + RPM/TPM 固定窗口计数器 + 预算检查 | store |
| `accountant.py` | 异步记账队列（后台线程）：写 usage/预算累加/会话消息 | store |

配置项（`.env` → `.env.example`）：`ACCOUNTS_DB_PATH`（默认 `data/modelctl_accounts.db`）、`ACCOUNTS_ENABLED`（bool）。

### 4.2 网关接入（`core/gateway.py`）

在现有 `verify_client` 之后串接（沿用"处理器首行显式调用"模式，不用 BaseHTTPMiddleware）：

```
verify_client() 存在性校验
 → accounts.resolve_key()  查 Key + 账号策略（ACCOUNTS_ENABLED=false 则回退旧单钥逻辑）
 → limits.acquire(并发)     超并发 429
 → limits.check_rate(rpm/tpm)  超限 429
 → limits.check_budget()    预算耗尽 429
 → 代理转发(流式)
 → finally limits.release(并发)
 → 结束 account.settle(usage, session)  异步入队
```

- 并发：请求开始 `acquire`，`try/finally` / 流完成回调 `release`（保证 SSE 结束才释放）。
- 结算：`settle()` 将实际 token/耗时推入 `accountant` 队列，非阻塞；SSE 在流结束时一次性结算。
- Key 验完即丢、上游 key 逻辑不变；被拒请求仍落审计，审计新增 `key_id/user_id`。

### 4.3 兼容策略

`ACCOUNTS_ENABLED=false`（默认）→ 行为完全等同现状（`GATEWAY_CLIENT_API_KEY` 单钥 fail-closed）；`true` → 走账号解析。管理面单钥仍作为根管理员，账号面板另走 JWT。

## 5. 限流与预算执行细节

四道检查于 `limits.LimitGuard` 内，进程内单例，按固定窗口/量级计数器实现。

| # | 检查 | 实现 | 超限返回 |
|---|---|---|---|
| 1 | 预算(token 配额) | `users.budget_consumed < token_budget`，不足则拒 | `429 budget_exceeded` |
| 2 | 并发 | per-user 活跃计数 `acquire`（< limit 才放行） | `429 concurrency_exceeded` |
| 3 | RPM | 固定 60s 窗口 `{user_id:(window,count)}`，进窗重置，count ≥ rpm_limit 拒 | `429 rate_limit_exceeded` |
| 4 | TPM | 固定 60s 窗口，按预估 prompt token（字符宽估算）预占，`window_tokens+预估 > tpm_limit` 拒 | `429 rate_limit_exceeded` |

- 错误均 OpenAI 兼容信封（`error.type` + `message`），带 `Retry-After`。
- 记账与窗口更新（请求结束）：并发 `release`；RPM 已在开头 +1；TPM 结束时把实际 total_tokens 加回窗口；预算结束时 `budget_consumed += 实际 total_tokens`（异步落账）。
- 异步任务失败不影响已返回响应，仅记日志，下次从 DB 权威值重算。

### 重置与持久化

- 预算周期：默认 `day`，检查时若 `now >= budget_reset_at` 则清零并推进（惰性重置，无需定时器）。
- 限流计数器：纯内存，重启即清零（瞬时保护语义）；**预算消费持久化到 DB**，保证重启不丢账。
- 限额来源：账号行读取（账号级），`0/None` 视为不限制；管理员建号时设合理默认（如并发 10 / RPM 120 / TPM 100k / 预算 100 万 token/天），按账号可调。

### 边界说明

- Key 无效/禁用/过期 → `401 invalid_api_key`（与现有 fail-closed 语义一致）。
- 流式：四道检查在开流前完成；预算/并发释放挂在流完成回调。

## 6. 会话存储

### 6.1 归属与识别

- 优先：客户端 `X-Session-Id`（或可选 `session_id` 字段）作稳定标识。
- 回退：按 `(user_id, key_id, model)` 空闲窗口聚合，距上次活跃 ≤ `SESSION_IDLE_TTL`（默认 30 分钟）视为同一会话。

### 6.2 消息写入（异步，经 accountant 队列）

| 消息 | 触发 | 内容来源 |
|---|---|---|
| `user` | 请求开始 | 请求体 `messages` 最后一条 user 文本 |
| `assistant` | 请求结束 | 普通：响应全文；流式：SSE 分片拼接 |

- 会话首次出现即建 `sessions`（`title` 取首条 user 截断）；`message_count`/`updated_at` 随消息递增。
- 写入走后台队列，不阻塞请求路径，失败仅日志。

### 6.3 保留与隐私

- 默认不自动删除；管理员可设账号级 `retention_days`（超过 N 天批量清理）；账号可删除自己的会话；支持导出 JSON/Markdown。
- 会话/消息仅存 SQLite（不写审计文件，避免内容泄露到纯文本 JSONL）；审计仍保留请求级 `key_id/user_id` 关联。

## 7. 管理与认证

### 7.1 认证体系（两套并存）

| 面 | 凭据 | 机制 |
|---|---|---|
| 管理面 `/admin/api/*` | 现有单钥 `API_KEY` | 保留，作为根管理员 |
| 账号登录 `/api/account/login` | 用户名 + 密码 | bcrypt → 签发 JWT（含 `user_id/is_admin`） |
| 数据面 `/v1*` | `sk-mctl-...` Key | 双通道 Bearer / `x-api-key` |
| 账号自助 `/api/account/*` | JWT | 校验 JWT + 归属校验（只能动自己的资源） |

兼容：JWT 与现有管理面 Bearer 互不冲突；接口需同路由组定义全组方法（避免 405/404）。

### 7.2 管理员面板（webui）

- 账号管理：创建/禁用/重置密码；设 `concurrency/rpm/tpm/token_budget/budget_period/retention_days`。
- Key 管理：签发（一次性展示）、吊销、禁用、设过期；按账号查看。
- 用量总览：全部账号 token 累计、预算消耗、活跃度。

### 7.3 账号自助面板（webui）

- 我的 Key：自助申请（填用途名称）、查看（前缀脱敏）、禁用/删除。
- 我的用量：按日/按模型查看 token、请求数、预算剩余。
- 我的会话：会话列表、关键词搜索、查看内容、导出、删除。

### 7.4 界面与路由约定

- Vue3 + Element Plus；路由名与组件 `name` 一致（`keep-alive`）；新页面放 `views/accounts/`。
- 前端不显示真实 ID（遵循 CLAUDE.md 裁决）：Key 只显示 `key_prefix` 脱敏；时间用 `YYYY-MM-DD HH:mm:ss`。
- 长文本 el-tooltip 展示完整；列宽用 `min-width`。

## 8. 错误处理 / 边界

| 场景 | HTTP | `error.type` |
|---|---|---|
| Key 无效/不存在/禁用/过期 | 401 | `invalid_api_key` |
| 预算耗尽 | 429 | `budget_exceeded` |
| 并发超限 | 429 | `concurrency_exceeded` |
| RPM/TPM 超限 | 429 | `rate_limit_exceeded` |
| 账号系统启用但 DB 不可用 | 503 | `service_unavailable`（fail-closed） |
| 会话/记账异步失败 | — | 仅日志，不影响响应 |

其余边界同 §5.1 与 §6.3；`ACCOUNTS_ENABLED=false` 时零回归。

## 9. 测试

- 单元：`limits.py`（窗口重置/acquire-release/预算惰性重置/超限码）、`hashing.py`（bcrypt 往返/前缀/唯一）、`store.py`（CRUD/幂等加列/唯一约束）。
- 集成（`test_client` 打网关）：无效 Key → 401；预算/并发/RPM/TPM 超限 → 429 各分支；流式结束后并发槽释放、budget 正确累加；会话归属（`X-Session-Id` / 空闲窗口）；`ACCOUNTS_ENABLED=false` 与旧版一致。
- 手工：webui 建号 → 申请 Key → `curl` 打 `/v1/chat/completions` → 核对用量/会话/限额。

## 10. 配置

`.env` / `.env.example` 追加：

```ini
# 账号/多 Key 体系开关与库路径
ACCOUNTS_ENABLED=false        # 默认关闭，行为等同现有单钥 fail-closed
ACCOUNTS_DB_PATH=data/modelctl_accounts.db
```

## 11. 涉及文件（实现预览）

- 新增：`src/modelctl/core/accounts/{store,hashing,auth,limits,accountant}.py`
- 修改：`core/gateway.py`（接入）、`core/webui/`（admin + account 路由与依赖）、`core/audit.py`（`key_id/user_id`），前端 `views/accounts/` 组件
- 文档：`.env.example`；完成后按 CLAUDE.md 沉淀到 `docs/known-pitfalls/`

## 12. 实施顺序（供 writing-plans）

1. `accounts` 包（store/hashing/auth/limits/accountant）+ 网关接入；
2. WebUI 管理员面板 + 账号登录 + 账号自助面板（前后端）；
3. 测试加固、文档、`known-pitfalls` 沉淀。

## 13. 设计评审记录（2026-09-08）

按推荐方案定案：

| # | 事项 | 裁决 |
|---|---|---|
| 1 | 实现方式 | **采用方案 A**（网关内同步准入 + 请求结束异步落账），贴近 LiteLLM，非阻塞、SSE 兼容 |
| 2 | 限额粒度 | **账号级限额，Key 继承**；每 Key 独立额卡暂缓（YAGNI） |
| 3 | 预算单位 | **token 配额**，`budget_period` 默认 `day`，惰性重置 |
| 4 | 会话归属 | `X-Session-Id` 优先 + 空闲窗口（30 分钟）聚合回退 |
| 5 | 兼容 | `ACCOUNTS_ENABLED=false` 默认，行为等同现状；旧 spec 非目标被本次升级覆盖 |
