# 网关与 nginx 数据面客户端鉴权设计

- 日期：2026-09-07
- 状态：已确认（用户评审通过，含 §9 三项裁决）
- 关联：`core/gateway.py`（`/v1*` 数据面）、`core/nginx_snippet.py`、`docs/nginx/llm-routing.example.conf`、`engines/{sglang,tokenspeed}.py`

## 1. 背景与目标

### 问题

统一网关的 `/v1*` 全部端点**对客户端凭据零校验**，且经 B 机 nginx 以 `http://<公网IP>:5000/<node>/llm/v1/...` 暴露：

| 事实 | 位置 |
|---|---|
| 网关三条 `/v1` 路由无任何鉴权依赖，无 401/403 分支 | `core/gateway.py` `list_models` / `anthropic_proxy` / `proxy` |
| 客户端传了 key 也**被读取后直接丢弃覆盖**，等价于不校验 | `core/gateway.py` L818-828、L615-619 |
| nginx 数据面 location 无 `auth_basic` / `allow`/`deny` / `limit_req` | `docs/nginx/llm-routing.example.conf` L39-63 |
| 网关监听 `0.0.0.0`，`GATEWAY_HOST=0.0.0.0` | `.env` L87、`core/gateway.py` L1076 |
| SGLang / TensorRT-LLM / Ollama / TokenSpeed(docker) 引擎侧未下发 key，直连端口亦全裸 | `engines/sglang.py` L93-120、`tensorrt_llm.py` L155-203、`ollama.py` L26-37、`tokenspeed.py` L99-122 |

净结果：任何知道 URL 的人可无凭据白嫖 GPU，且传输为明文 http。

### 目标

1. 网关 `/v1*` 全部端点强制校验客户端凭据，**fail-closed**（服务端未配 key → 全 401）。
2. 客户端凭据与管理面 `API_KEY` **域隔离**，新增独立 `GATEWAY_CLIENT_API_KEY`。
3. nginx 层增加统一凭据校验，**同时覆盖网关路径与模型直连路径**，兜住无鉴权能力的引擎。
4. nginx 限流抗 key 爆破。
5. 传输层升级 https（自建 CA）。
6. 补齐引擎侧 key 下发缺口。

### 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 凭据来源 | 新增独立 `GATEWAY_CLIENT_API_KEY`，不复用管理面 `API_KEY` |
| 未配置时行为 | **fail-closed**：`/v1*` 全部 401，与 `admin_auth.require_auth` 口径一致 |
| 鉴权范围 | 全部 `/v1*`：`chat/completions`、`completions`、`embeddings`、`messages`、`GET /v1/models`、裸 `POST /v1` |
| 凭据通道 | `Authorization: Bearer <key>` **或** `x-api-key: <key>` 任一命中即通过 |
| 校验实现位置 | 各路由处理器**首行显式调用**校验函数，不用 ASGI/BaseHTTP 中间件 |
| 上游 key 逻辑 | **不改**——客户端 key 仅用于准入，验完即丢，上游仍用 profile key |
| nginx 校验 | `map` 常量比对 + `if (...) return 401`；分两组白名单——网关 location 只认 `GATEWAY_CLIENT_API_KEY`，直连/用量 location 额外认各 profile `api_key` |
| 限流阈值 | `limit_req 30r/m burst=60`；`limit_conn 16`（按单 IP） |
| HTTPS | 自建 CA 签发服务端证书，客户端信任该 CA |
| 引擎侧 key | SGLang / TokenSpeed-docker 补齐下发；TRT-LLM / Ollama 官方无能力，仅靠 nginx 兜底 |

### 非目标

- 不做多 key 签发体系（无 user/app/tenant 实体，无签发接口、无过期、无配额、无按调用方计量）。当前单把共享 key 满足"挡掉未授权访问"这一诉求，YAGNI。
- 不实现 CLAUDE.md L49 所述 HMAC 客户端签名（全库无对应实现，属待落地前瞻约定）。本设计的"嗅探 `Authorization` 或 `x-api-key`，不强要求 Bearer"是该规范的首次落地形态。

## 2. 总体架构

三层防线，自外向内：

```
公网 http(s)://<B>:5000/208/llm/...
        │
   ① nginx  ── TLS 终止（自建 CA）
        │    ── limit_req 30r/m burst=60 / limit_conn 16
        │    ── map 凭据常量比对 → 401（网关 + 直连两路径统一）
        │    ── 补 X-Real-IP / X-Forwarded-For
        ▼
   ② 网关 :5003 ── verify_client() → 401（OpenAI 错误信封）
        │          ── 审计记 auth=ok|missing|invalid|unconfigured + client_ip
        │          ── 覆盖客户端 Authorization 为 profile key（不变）
        ▼
   ③ 引擎端口 ── 原生 --api-key（vLLM/llamacpp/Aphrodite/LMDeploy/
                  TokenSpeed/SGLang(本次补齐)）
                  TRT-LLM / Ollama 无能力 → 依赖 ①② 兜底
```

②③ 都在，是为纵深防御：nginx 挡掉绝大多数爆破与扫描流量，不让它打到 Python 进程；网关保证即使 nginx 配置漂移、或有人直连 `:5003` 也进不来。

## 3. 网关侧设计

### 3.1 校验函数

新增于 `core/gateway.py`（不放 `admin_auth.py`——该模块属管理面域且 `API_KEY_ENV` 写死单值；数据面不应反向依赖管理面）：

```python
GATEWAY_CLIENT_KEY_ENV = "GATEWAY_CLIENT_API_KEY"

def client_api_key() -> str:
    """读取网关客户端 key；进程内首次调用触发 load_env（与 admin_auth._ensure_env_loaded 同范式）。"""

def verify_client(request: Request) -> str:
    """网关数据面准入校验。

    通过 → 返回审计标签 "ok"；失败 → 抛 GatewayAuthError(status=401, message)。
    fail-closed：GATEWAY_CLIENT_API_KEY 未配置/为空时一律拒绝，标签 "unconfigured"。
    凭据比较用 hmac.compare_digest（bytes 比较，非 ASCII 输入干净拒绝，
    与 cluster.tokens.token_matches 同范式）。
    """
```

通道嗅探顺序：`Authorization: Bearer <key>` → `x-api-key: <key>` → 拒绝。

`x-api-key` 是刚需而非冗余：Anthropic 协议客户端（Trae CN 内置 Claude SDK）只带 `x-api-key`，`gateway.py` L616 的白名单透传已证明这点。只认 Bearer 会打挂 `/v1/messages`。

### 3.2 挂载方式：处理器首行调用

在三个处理器的**函数体第一行**调用，失败即返回 401 `JSONResponse`：

| 处理器 | 路由 |
|---|---|
| `list_models` | `GET /v1/models` |
| `anthropic_proxy` | `POST /v1/messages` |
| `proxy` | `POST /v1` + `POST /v1/{path:path}` |

不用 `app.middleware("http")`（BaseHTTPMiddleware）：本项目对 SSE 生命周期做了手工管理（`gateway.py` L830-832 明确注释不能用 `async with` 包裹，否则流式连接被提前切断），引入中间件层属于给 SSE 链路增加未验证的缓冲风险。只有 3 个处理器，显式调用更简单、覆盖可枚举、且天然不会误伤 `/admin/api/*` 与 SPA。

裸 `POST /v1` 连通性探测同样要求凭据——它虽只返回 `{"status":"ok"}`，但保留即等于给扫描器一个"该路径存活"的探针。

### 3.3 失败响应

OpenAI 错误信封，保证 OpenAI / Anthropic SDK 能正常解析并抛客户端异常，而不是解析层报错：

```json
{"error": {"message": "invalid API key", "type": "authentication_error"}}
```

message 分三档，便于排障但不泄露期望值：

| 场景 | message |
|---|---|
| 服务端未配 key | `gateway client API key not configured` |
| 未提供任何凭据头 | `missing API key: send 'Authorization: Bearer <key>' or 'x-api-key: <key>'` |
| 凭据不匹配 | `invalid API key` |

HTTP 状态码统一 **401**（不用 403；与 `admin_auth` 及 OpenAI 惯例一致）。

### 3.4 审计增强

`_build_audit_entry`（`gateway.py` L213-265）新增两字段：

- `auth`: `"ok" | "missing" | "invalid" | "unconfigured"`——**只记结果标签，绝不记 key 值或片段**
- `client_ip`: 取 `X-Forwarded-For` 首段，回退 `X-Real-IP`，再回退 `request.client.host`

被拒请求（401）**同样落审计**，否则爆破行为不可见。`core/webui/admin_audit.py` 的过滤链（L142-188）同步支持按 `auth` / `client_ip` 过滤。

### 3.5 上游 key 逻辑保持不变

`gateway.py` L818-828 的"用 profile key 覆盖客户端 Authorization"**不改**，语义澄清为：客户端 key 用于**准入**，验完即丢；上游认证永远用 profile key。

因此 `tests/test_gateway.py` L222-239「客户端传 `x-api-key: root123456`、上游收到 `fly@@see`」的既有契约仍然成立——只需给所有用例补合法客户端 key。

## 4. nginx 层设计

### 4.1 凭据校验 map

`modelctl nginx-snippet` 新增 `--client-key <key>`（未传时读 `GATEWAY_CLIENT_API_KEY`），在现有 `map $uri $llm_model_target` 之外追加输出**两组**白名单，与两条路径各自的下游校验能力精确对齐：

```nginx
# 组 1：网关 location 专用（只含 GATEWAY_CLIENT_API_KEY，与网关 verify_client 同口径）
map $http_authorization $llm_bearer_gw { default 0; "Bearer <CLIENT_KEY>" 1; }
map $http_x_api_key     $llm_xkey_gw   { default 0; "<CLIENT_KEY>" 1; }
map "$llm_bearer_gw$llm_xkey_gw" $llm_reject { "11" 0; "10" 0; "01" 0; default 1; }

# 组 2：模型直连 / 用量 location 专用（额外含各 profile api_key）
map $http_authorization $llm_bearer_all { default 0; "Bearer <CLIENT_KEY>" 1; "Bearer <PROFILE_KEY>" 1; }
map $http_x_api_key     $llm_xkey_all   { default 0; "<CLIENT_KEY>" 1; "<PROFILE_KEY>" 1; }
map "$llm_bearer_all$llm_xkey_all" $llm_reject_all { "11" 0; "10" 0; "01" 0; default 1; }
```

location 内统一 `if ($llm_reject*) { default_type application/json; return 401 '{"error":{"message":"invalid API key","type":"authentication_error"}}'; }`。

**为何必须分两组**：模型直连 location 不改写 `Authorization`，vLLM/SGLang 等引擎只认 profile `api_key`——若直连 location 也只放行 client key，全部既有直连客户端会断；反之若网关 location 也放行 profile key，profile key 能过 nginx 却被网关 401，表现为无法解释的配置矛盾。分两组后两条路径的白名单与其唯一校验方完全一致。

`if` + `return` 是 nginx 官方明确的安全用法（"if is evil" 针对的是 location 内的非 `return` 指令继承问题）。

设计要点：
- **兜住无鉴权能力的引擎**：Ollama / TRT-LLM 引擎侧不校验，但流量必须先过 nginx 这道闸。
- 生成的文件**含明文密钥**：文档必须写明上传 B 机后 `chmod 600 /etc/nginx/conf.d/llm-routes-<node>.conf`，且该文件不得入库。

### 4.2 限流

`http` 块（由 example conf 提供，zone 全局唯一，多 server 复用）：

```nginx
limit_req_zone  $binary_remote_addr zone=llm_req:10m  rate=30r/m;
limit_conn_zone $binary_remote_addr zone=llm_conn:10m;
limit_req_status  429;
limit_conn_status 429;
```

四个 location 内：

```nginx
limit_req  zone=llm_req burst=60 nodelay;
limit_conn llm_conn 16;
```

`limit_req` 只管新建请求速率，SSE 长会话不占该速率但占并发，故两者必须同时配。429 而非 503，便于客户端区分"限流退避"与"服务故障"。阈值取宽松档，避免误伤 Trae CN 这类高频编码客户端。

### 4.3 真实客户端 IP

当前数据面 location 缺 `X-Real-IP` / `X-Forwarded-For`（对比 `webui.example.conf` 是有的），网关无法记真实来源 IP。四个 location 补：

```nginx
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
```

### 4.4 HTTPS（自建 CA）

裸 IP `36.156.121.146` 无法签 Let's Encrypt（LE 只签域名），故自建 CA。

一次性在服务端生成（写入 `docs/nginx/` 操作文档，脚本不落仓库）：

```bash
# 1) 根 CA（10 年，私钥仅留服务端，离线保管）
openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
  -keyout ca.key -out ca.crt -subj "/CN=modelctl-internal-CA"

# 2) 服务端证书：SAN 同时含 IP 与域名，1 年
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr \
  -subj "/CN=llm.modelctl.internal"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 365 -sha256 -extfile <(printf "subjectAltName=IP:36.156.121.146,DNS:llm.modelctl.internal") -out server.crt
```

`server` 块改造：

```nginx
listen 5000 ssl;
ssl_certificate     /etc/nginx/tls/server.crt;
ssl_certificate_key /etc/nginx/tls/server.key;   # chmod 600
ssl_protocols       TLSv1.2 TLSv1.3;
ssl_ciphers         HIGH:!aNULL:!MD5;
```

客户端信任 `ca.crt` 后，`base_url` 从 `http://` 改 `https://`。不便安装 CA 的第三方工具，退化为 `curl -k` / SDK 关闭校验，文档需明示这是接受降级。

**过渡策略**：5000 直接切 https（不做 http/https 双监听）。理由是明文继续暴露等于本次白做；切换瞬间存量 http 客户端会失败，属预期，需在发布说明列出 `base_url` 改造点。

## 5. 引擎侧 key 下发补齐

按官方能力分两类，**已核实**：

| 引擎 | 官方支持 | 本次动作 |
|---|---|---|
| SGLang | **是**，`--api-key`（[官方 Server Arguments](https://docs.sglang.io/advanced_features/server_arguments.html) API related 段） | `sglang.py` `build_command` 追加 `self.api_key_args()` |
| TokenSpeed docker | 是（venv 分支 L134 已下发） | `tokenspeed.py` docker 分支 L111-115 补齐 |
| TensorRT-LLM | 文档无 `--api-key`（[trtllm-serve](https://nvidia.github.io/TensorRT-LLM/latest/commands/trtllm-serve/index.html)） | **不改代码**，依赖 nginx + 网关兜底 |
| Ollama | **官方明确无鉴权**——"No authentication is required when accessing Ollama's API locally"（[docs.ollama.com/api/authentication](https://docs.ollama.com/api/authentication)） | **不改代码**，依赖 nginx + 网关兜底 |

SGLang 用 `--api-key`（连字符），LMDeploy 用 `--api-keys`，llamacpp/vLLM/Aphrodite 用 `--api-key`——基类 `api_key_args()` 返回 `["--api-key", ...]`，SGLang 可直接复用，无需覆盖。

### 必须验证的回归点（实现时首要做）

`--api-key` 是否连带门控**非 OpenAI 端点**（SGLang 官方仅说"It is also used in the OpenAI API compatible server"，措辞有歧义）。若门控，以下探测会失败：

| 探测 | 位置 | 是否带 key | 影响 |
|---|---|---|---|
| `wait_ready` → `/health` | `engines/base.py` L182 | **带**（`upstream_api_key()`） | 预计无影响，需实测确认 |
| `is_model_available` | `gateway.py` L392-398 → `process.is_running_any` L124-133 | **带**（已显式从 `profile.api_key` / `engine_config.api_key` 取值注入 `/health` 探测头） | 预计无影响 |
| `status_model` → `wait_health("http://127.0.0.1:<port>", 3.0)` | `all_service.py` L460 | **不带**，且请求根路径 `/` | **唯一真实风险点**：若 `--api-key` 门控根路径，则 `modelctl status` 误报未运行 |

前两处已带 key，符合预期。第三处是唯一需要修的点：`status_model` 改为 `wait_health(..., api_key=<profile 有效 key>)`，并确认根路径 `/` 在 SGLang 启用 `--api-key` 后的响应语义（`/health` 与 `/` 可能不同，需在真机确认，不能假设）。

验证方法（实现首步，在 208 节点上）：给某个 SGLang profile 加 `--api-key` 启动，依次执行 `modelctl status <name>`、`modelctl list`、经网关发一次 `/v1/chat/completions`，三者全部正常方可继续。任一回退则该引擎的 key 下发单独回滚，不影响第 1-3 步的网关/nginx 加固。

## 6. 配置与运维

### `.env` / `.env.example`

```ini
# 网关数据面客户端密钥：外部调用方访问 /v1* 与 nginx 直连路径的唯一凭据。
# 与管理面 API_KEY 严格隔离——API_KEY 可改配置/启停模型，绝不可下发给外部客户端。
# 留空 ⇒ fail-closed，网关 /v1* 全部返回 401（预期行为，非故障）。
GATEWAY_CLIENT_API_KEY=
```

### `core/all_service.py`

| 函数 | 改动 |
|---|---|
| `start_gateway` (L247-251) | 启动时若 `GATEWAY_CLIENT_API_KEY` 为空 → `logger.warning` 明示"网关将以 fail-closed 运行，全部 /v1 请求返回 401" |
| `status_gateway` (L350-355) | `wait_health(..., api_key=<GATEWAY_CLIENT_API_KEY>)`，否则 `/v1/models` 返回 401 会让 status 误报"无响应" |

### 密钥轮换（一次性人工操作）

`.env` 现有 `API_KEY=fly@@see` 已在 PowerShell 报错日志中明文出现。本次设计的新 key 必须**另生成一把**，不得复用该值；同时建议一并轮换 `API_KEY`（它同时是各引擎 `api_key: ${API_KEY}` 的插值源，改动后需重启全部模型进程 + 重新登录 webui）。此操作由用户手动完成，Agent 只生成建议命令。

### 文档同步

| 文件 | 改动 |
|---|---|
| `docs/nginx/测试指南.md` | 鉴权说明段改写（网关路径由"无需 key"改为"必须带 `GATEWAY_CLIENT_API_KEY`"）；8 条 curl 校验矩阵全部补 `-H "Authorization: Bearer $KEY"`；`http://` → `https://`；排错表新增 `401 → key 缺失/不匹配/服务端未配`、`429 → 命中 limit_req/limit_conn` |
| `docs/nginx/llm-routing.example.conf` | TLS + 限流 zone + 凭据 map + IP 头，完整可抄 |
| `docs/known-pitfalls/backend/` | 按 CLAUDE.md 渐进式披露规范沉淀条目（摘要层 `README.md` 索引 + 详情文件） |

## 7. 测试

`tests/test_gateway.py` 全部 `/v1` 用例受 fail-closed 影响，统一处理：

- 在测试辅助层（现有 `_post(app, path, json=...)`）默认注入合法 `Authorization` 头，避免逐用例改动；客户端 key 经 fixture 设入环境变量后再构建 app。
- 现有「上游收到 profile key」断言（L222-239）保持不变，仅入参补合法 key。

新增用例：

| 用例 | 期望 |
|---|---|
| 缺全部凭据头 → `POST /v1/chat/completions` | 401，`error.type == "authentication_error"` |
| 错误 key | 401 |
| 仅 `x-api-key` → `/v1/messages` | 200，且上游收到 profile key |
| 服务端未配 `GATEWAY_CLIENT_API_KEY` | 401，message 为 `unconfigured` 档 |
| `GET /v1/models` 无 key | 401 |
| 裸 `POST /v1` 无 key | 401（原返回 200，行为变更点） |
| 401 请求落审计 | `auth` 标签正确，且不含 key 片段 |

nginx 侧无法单测，验证依赖 `nginx -t` + `docs/nginx/测试指南.md` 的 curl 矩阵人工回归（补 401 / 429 两条）。

引擎侧新增断言：`sglang.build_command()` 输出含 `--api-key`；`tokenspeed` docker 分支输出含 `--api-key`（若现有测试有命令快照，需同步更新）。

## 8. 实施顺序

1. 网关 `verify_client` + 三处理器接入 + 审计字段 + `.env.example` + `all_service` 两处 —— **可独立交付并单测**
2. `test_gateway.py` 适配 + 新增用例
3. `nginx_snippet.py` 输出凭据 map（含 `--client-key`）+ example conf 全量更新
4. SGLang / TokenSpeed-docker 补 `--api-key`，**并验证第 5 节回归点**
5. 服务端 TLS 部署 + `base_url` 迁移 + 客户端 CA 分发
6. `docs/nginx/测试指南.md` 重写 + `docs/known-pitfalls/` 沉淀

第 1、2 步是安全收益主体，可先行合入；第 5 步涉及停机切换，需单独安排窗口。

## 9. 评审裁决记录（2026-09-07）

以下四项按推荐方案定案：

| # | 事项 | 裁决 |
|---|---|---|
| 1 | 鉴权实现方式 | **采用处理器首行显式调用**，不引入 `BaseHTTPMiddleware`。理由：SSE 已手工管理生命周期（`gateway.py` L830-832），中间件层属未验证风险 |
| 2 | 裸 `POST /v1` 由 200 → 401 | **接受该破坏性变更**。发布说明必须列出：依赖 baseUrl 连通性探测的客户端（hertz 等）需改为带 key，或改探 `GET /v1/models` |
| 3 | TLS 部署 | 自建 CA 的 `openssl` 命令写入 `docs/nginx/` 操作文档；**CA/证书生成与分发为一次性人工操作**，由用户在维护窗口执行，Agent 只产出文档与配置，不代跑 |
| 4 | 密钥取值 | 新 `GATEWAY_CLIENT_API_KEY` **必须另生成**，不得复用已在 PowerShell 报错日志中明文泄露的 `fly@@see`；生成命令写入文档，实际轮换由用户手动执行 |
