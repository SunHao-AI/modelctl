# AI 对话（模型调试台）设计

日期：2026-09-12
状态：已与用户逐节确认（布局 A / 链路 A + 走网关开关 / 通用传图 + 软徽章）

## 1. 目标与非目标

**目标**：在侧边导航新增「AI 对话」，让运维在管理面板里直接选中**已启动且健康**的模型发对话请求，用来回答两件事——这个模型现在能不能正常对话，以及它答得怎么样。

由此派生的必需要素：流式输出、思考(reasoning)与正文分区、token/耗时统计、上游请求与响应原文、可在页面调的推理参数。

**非目标**：

- 不做多会话服务端持久化、标题自动生成、会话搜索（那是聊天产品，不是调试台）。
- 不做 prompt 模板库、导出、分享、评分。
- 不做多模态能力的硬门禁（见 §6）。
- 不新增数据库表，不碰 accounts 的 `sessions/messages`。

## 2. 关键背景（决定方案的事实现状）

- webui 进程与网关是**同一份 FastAPI 的两个端口**：`create_app(admin=True)` 同时挂 `/v1/*`、`/admin/api/*`、`/api/account/*` 与 SPA（`src/modelctl/core/webui/server.py:143`、`src/modelctl/core/gateway.py:2000-2022`）。
- 管理面鉴权 `require_auth`（Bearer `API_KEY`）与数据面 `GATEWAY_CLIENT_API_KEY` 是**严格隔离的两个信任域**，且数据面 key 默认为空即 fail-closed（`.env.example:118`、`gateway.py:74-110`）。前端只持有管理面 token。
- `/v1/chat/completions` 的代理逻辑（`gateway.py:1482-1998`）在转发前会做：家族路由解析、上下文长度切换、`reasoning_effort` 归一、thinking 注入、上游 key 覆盖。这套判定**是网关行为的一部分**，不是噪音——但它会让"页面选 A、实际打 B"。
- 模型可选性口径：`_model_summary` 给出 `state == "running"` / `health == "healthy"`（`admin_models.py:78-130`）。
- 多模态：**没有统一标识**。只有 llamacpp 在 `engine_config.vision` 里声明（`engines/llamacpp.py:268`）。
- 前端现状：无 markdown / 高亮 / XSS 消毒依赖；无 `src/utils/request.js`（实际是 `api/client.ts` 的 axios 实例）；样式是 UnoCSS（presetWind3），非 Tailwind CLI。

## 3. 整体架构

```
浏览器 ChatView
  │  Bearer 管理面 API_KEY（复用 client.ts 拦截器已有的 token）
  ▼
POST /admin/api/chat/completions          ← 新增 admin_chat.py
  │
  ├─ route_mode = "direct"   （默认）
  │     profile 名 → GatewayModel → backend_url + upstream_api_key
  │     → POST {backend_url}/v1/chat/completions      【选谁打谁】
  │
  └─ route_mode = "gateway"
        复用网关的路由判定（家族/上下文/effort/thinking）得到最终 target
        → POST {target.backend_url}/v1/chat/completions 【复现网关落点】
  │
  ▼  SSE 原样透传（不解析、不重组上游字节流）
浏览器逐块解析 delta.content / delta.reasoning / usage
```

两种模式**都不走 HTTP 自调用**，而是复用同一份路由判定函数（§5）。这样"走网关"能复现网关的路由决策，却不占用两条连接、不需要数据面 key、也不重复计入网关并发槽。代价是 `gateway` 模式不复现网关的限流与记账——这是刻意取舍，调试台不该消耗生产配额；需要验证限流时用真实客户端打 `/v1`。

## 4. 后端

### 4.1 新增 `src/modelctl/core/webui/admin_chat.py`

按 `admin_router._include_subrouter` 约定提供 `def _router() -> APIRouter`，前缀 `/chat`，在 `create_admin_router()` 里注册。

**`POST /admin/api/chat/completions`**

- 鉴权：`Depends(require_auth)`。
- 请求体：
  ```json
  {
    "model": "qwen3.8-35b",
    "messages": [{"role": "user", "content": [{"type":"text","text":"…"},
                                             {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,…"}}]}],
    "route_mode": "direct",
    "temperature": 0.7, "top_p": 0.95, "max_tokens": 2048,
    "system": "可选系统提示词",
    "include_usage": true
  }
  ```
  白名单外字段一律丢弃（不整体转发调用方 JSON）。`temperature` / `top_p` / `max_tokens` 原样透传；`system` 由服务端转成 `messages` 首条 `role=system`（前端消息数组里不存 system，避免与历史混淆）；`include_usage` 控制是否注入 `stream_options`；`stream` 服务端强制为 true，前端不需要传。
- 校验：`model` 必须在注册表内且 `is_model_available()` 为真，否则 `409 {"error":{"code":"model_not_running", …}}`；请求体上限 24 MB，超出 `413`（图片是 base64，必须有界）。
- 响应：上游 2xx → `StreamingResponse`，`media_type` 与上游一致，附 `Cache-Control: no-cache`、`X-Accel-Buffering: no`（与 `admin_models.py:716` 日志流同套路）。落点信息走响应头 `X-Chat-Routed-To` / `X-Chat-Route-Reason`（前端从 `response.headers` 读，用于右栏"实际落到"）；**上游 4xx/5xx 的透传响应同样带这两个头**——"打到谁却报 400"正是最常见的调试场景。
- 上游 4xx/5xx → **原样透传**状态码与响应体（这是调试台最重要的输出）。
- `stream_options.include_usage` 默认注入（统计依赖它），但由请求字段 `include_usage` 控制、缺省 true：个别引擎的 OpenAI 兼容层不认该字段，返回 400 时前端关掉它即可重发（见 §8）。
- 客户端断开 → 生成器 `finally` 里关闭 httpx client 与上游响应（照 `gateway.py:1625-1628` 的注释教训：不能用 `async with` 包住再返回 StreamingResponse）。
- 请求体上限 24 MB（读原始 body 后判长度，超出 413）；base64 图片必须有界。
- 不做：记账、限流、写审计 JSONL（网关侧链路已有；direct 模式刻意绕开，避免调试流量污染生产统计）。

**模型选择器不新增端点**：直接复用 `GET /admin/api/models`（前端 `listModels()`），只给 `_model_summary` 增补一个 `vision` 字段——`engine_config.vision == "on"` → `true`，引擎不认该配置 → `null`（前端只是不显示徽章，绝不禁用图片按钮）。理由：`/models` 已经把 `state/health/engine/group` 都算好了，再加一个 `/chat/selection` 等于把 `is_running_any` 探测做第二遍、把可选性口径分叉成两处；`vision` 对模型列表页本身也有用。前端类型 `ModelInfo`（`api/types.ts:28-53`）同步加 `vision: boolean | null`。

### 4.2 抽出共享的路由准备函数（唯一的既有代码改动）

`gateway.py` 里 `proxy()` 第 1567-1624 行做的是"body → 最终 target + 改写后的 body + 上游 headers + url"。新端点需要同一份判定，若复制一份，网关每改一次路由规则这里就静默漂移。故抽出：

```python
@dataclass
class PreparedUpstream:
    target: GatewayModel
    body: dict          # 已改写：upstream_model / reasoning_effort / chat_template_kwargs
    headers: dict       # 上游认证（永不用调用方的 key）
    url: str
    route_reason: str | None   # None | "group_route" | "context_switch"

def prepare_openai_upstream(registry, groups, group_cache, default_model,
                            context_rules, body: dict) -> PreparedUpstream | PreparedError
```

`proxy()` 改为调用它（行为零变化，仅把 `resolve_model` → `apply_context_switch` → `body["model"]=` → `_normalize_reasoning_effort` → thinking 注入 → headers/url 拼装这段搬进来）；`admin_chat` 用 `request.app.state` 上已有的 `registry/groups/group_route_cache/default_model`（需把这几样挂到 `app.state`，目前只存在于闭包里——`gateway.py:957-966` 已挂了 audit/accounts，一并补上）。

## 5. 前端

新增文件（遵循现有约定：`<script setup name="Xxx">` 与 `route.name` 一致以配 keep-alive；文件 kebab-case；UnoCSS 类名 + `styles/global.css` 里的暗色变量）：

| 文件 | 职责 |
| --- | --- |
| `views/chat/index.vue` | 三栏骨架（约定俗成的模块入口）；只做布局与组合，不含业务逻辑 |
| `components/chat/ChatMessage.vue` | 单条气泡：markdown 正文 + 可折叠 reasoning + 统计行 + 错误原文 |
| `components/chat/ChatComposer.vue` | 输入框、粘贴/选择图片、图片缩略图删除、参数 chips、发送/停止 |
| `components/chat/ChatParamsPanel.vue` | 右栏上：temperature / top_p / max_tokens / system prompt |
| `components/chat/ChatRawPanel.vue` | 右栏中：本次实际发出的 JSON + 上游原文（可复制） |
| `components/chat/ChatStatsPanel.vue` | 右栏下：TTFT / 总耗时 / tok/s / prompt·completion / finish_reason / 实际落点 |
| `components/chat/ChatHistoryList.vue` | 左栏本机历史（含"存储降级"提示） |
| `stores/chat.ts` | Pinia：消息流、SSE 解析与取消、统计计算、localStorage 持久化 |
| `api/chat.ts` | `fetch` 流式（axios 不适合流）+ 手动注入 Bearer；模型列表复用 `api/models.ts` 的 `listModels()` |
| `utils/markdown.ts` | markdown-it + DOMPurify 消毒 + 高亮封装（唯一使用点，便于以后替换） |

（对话子组件放 `src/components/chat/`，与既有 `components/common`、`components/docker`、`components/startup` 的分类方式一致；`views/` 下只放路由入口。）

改动：`Sidebar.vue` 的 `menus` 加 `{ to: '/chat', label: 'AI 对话', icon: 'chat' }` + 一段内联 SVG；`router/index.ts` 在 Layout `children` 下加 `/chat`（`meta: { requiresAuth: true }`，与兄弟路由一致）。

**依赖新增**（`web/package.json`）：`markdown-it`、`dompurify`、`highlight.js`。理由：模型输出是 markdown，必须渲染才能判断格式对不对；上游返回是不可信内容，`v-html` 前必须消毒。

**为什么必须 `fetch` 而不是 axios**：需要读 `ReadableStream` 边到边渲染，并读响应头拿 `X-Chat-Routed-To`。代价是绕过了 `client.ts` 的 401 拦截器，故 `api/chat.ts` 里要自己处理 401（复用 `stores/auth.clear()` + 跳 `/login`，与 `client.ts:23-39` 同语义）。

### 5.1 流式解析与统计

- 按 `\n\n` 切 SSE 帧，取 `data:` 行 `JSON.parse`；`[DONE]` 结束。解析失败的帧**不中断**，累积到"原文"面板并计入一个 warning——上游引擎的流格式差异正是调试台要看见的东西。
- `delta.reasoning`（vLLM/SGLang 思考型）与 `delta.reasoning_content` 都认，进 reasoning 分区；正文进 `content`。
- 统计：发送时刻 `t0`；首个含 content 或 reasoning 的块 → TTFT；`usage` 取最后一个带 usage 的块（`include_usage` 的块 `choices` 为空，**不能**当正文处理）；`tok/s = completion_tokens / (t_last_token - t_first_token)`。

### 5.2 停止与错误

- 停止：`AbortController.abort()`；后端生成器随连接关闭在 `finally` 关上游。已到达的部分文本保留并标「已中断」。
- 错误分层展示：`409 model_not_running`（模型被停）、`413`（图太大）、`401`（登录态失效 → 跳登录）、上游 4xx/5xx（红块 + 原文 + 复制按钮）、网络中断（保留已收内容）。
- 时间显示用 `utils/time.ts` 既有格式化，保持 `YYYY-MM-DD HH:mm:ss`。

### 5.3 历史与容量

- 结构：`{ id, title(首条用户消息前 24 字), model, createdAt(YYYY-MM-DD HH:mm:ss), messages[] }`。
- key `modelctl.chat.v1`，上限 4 MB；保留最近 50 个会话。
- 超限降级：先从最旧会话剔除图片 data URL（保留"曾有 N 张图"占位文本），仍超则整会话丢弃最旧；触发时在左栏底部出现一行提示，可点「清理」。
- 图片：canvas 压到最长边 1280、JPEG q0.82，单条 ≤4 张。

## 6. 多模态

所有模型都允许传图，`_model_summary` 新增的 `vision` 只用于下拉与气泡上的软徽章，**不参与禁用**。理由：profile 层没有权威的多模态字段，任何硬门禁都会误杀（例如 vLLM 起了视觉模型但 profile 未声明）；而对调试台来说，"不支持的模型收到图 → 上游 400 原文"本身就是有价值的输出。不为它新增 profile 字段。

## 7. 测试

测试沿用现有扁平布局（`tests/test_gateway.py`、`tests/test_webui_admin_accounts.py` 的命名与 fixture 风格）与 `httpx.MockTransport` 注入方式。

- `tests/test_webui_admin_chat.py`：
  direct 命中选定 profile；gateway 模式命中家族/上下文切换后的 target 且响应头带落点；模型未运行 409；上游 400 原样透传状态码与 body；流式逐块透传 + 客户端断开后上游被 `aclose`（用 transport 计数断言）；未知采样字段被丢弃；超 24 MB → 413。
- `tests/test_gateway.py` 内新增等价性回归：抽出函数后同一 body 经 `proxy()` 与经 `prepare_openai_upstream()` 得到相同 url / headers / 改写后 body。
- 前端 `stores/chat.test.ts`（vitest + jsdom，已有同目录测试范式）：SSE 分帧与畸形帧容错、usage 不当正文、TTFT/统计计算、localStorage 超限降级顺序、abort 保留部分文本。
- 前端 `utils/markdown.test.ts`：`<script>` / `onerror` 被消毒；代码块输出 `hljs` 类。

## 8. 已知风险

- `prepare_openai_upstream` 抽取触碰的是 500 行的 `proxy()`，属最高风险改动。缓解：只搬"路由+改写"，审计与流式分支一律不动；先加 §7 的等价性回归测试再动手。
- 大模型长回复会把整段 markdown 反复重渲染。缓解：渲染结果按消息 id 缓存，只对最后一条流式消息做增量拼接后再渲染。
- localStorage 装不下多图会话是确定的，已用 §5.3 的降级顺序兜住，不做服务端存储。
- `stream_options` 并非所有引擎的 OpenAI 兼容层都支持，若某引擎因此报 400，前端把请求里的 `include_usage` 关掉重发即可（代价是该次没有 token 统计，气泡上显示"无 usage"）。
