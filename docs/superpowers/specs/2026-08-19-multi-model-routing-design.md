# nginx 多模型路由与统一网关设计

- 日期：2026-08-19
- 状态：已确认（用户逐节评审通过）

## 1. 背景与目标

当前架构：C 机（192.168.77.210）部署 modelctl，运行 DeepSeek-V4-Flash（llama.cpp，端口 18888）；B 机 nginx 监听 :5000，将 `/<node>/llm/` 硬编码转发到对应节点；A 设备 cc-switch 以 `https://xxx:5000/210/llm/v1` 为 baseUrl。

现状问题：nginx 只写死了一条 `^/210/llm/(.*)$ → 192.168.77.210:18888`，**仅支持 deepseek-v4-flash 单一模型**。需要扩展为多模型支持：不同模型通过 URL 路径或 model 参数被正确路由和访问。

### 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 部署拓扑 | **通用方案**：路由规则同时兼容单机多模型与多机多模型 |
| 路由方式 | **两者都要**：路径式直连为主入口 + 网关支持 model 参数 |
| 整体方案 | 方案 C 混合架构（路径式直连 + 轻量网关 + 旧地址兼容） |
| 网关技术 | FastAPI + uvicorn + httpx 轻量自研（可选 extra 依赖） |
| 注册表来源 | 单一来源 = `models/*.yaml`，nginx map 由脚本生成 |
| 网关部署 | 与 modelctl 同节点（每跑模型的节点带一个网关），nginx 规则对所有节点统一 |

## 2. 总体架构

```
A 设备（cc-switch / 自定义客户端）
        │
        ▼
B 机 nginx :5000
        │
        ├── /<node>/llm/<model>/v1/...           路径式直连（主入口，cc-switch 每模型一张卡）
        │        └─> 192.168.77.<node>:<model-port>/v1/...
        │
        ├── /<node>/llm/<model>/v1/api/usage     用量统计（精确路由优先于直连）
        │        └─> 192.168.77.<node>:5002/api/usage?model=<model>
        │
        └── /<node>/llm/v1/...                   网关（model 参数场景 + 旧地址兼容）
                 └─> 192.168.77.<node>:5003/v1/...（网关按 body.model 分发到本节点模型）
```

访问示例（当前两模型 + 多节点通用）：

| URL | 转发目标 |
|---|---|
| `https://xxx:5000/210/llm/deepseek-v4-flash/v1/chat/completions` | `192.168.77.210:18888` |
| `https://xxx:5000/210/llm/qwen3.8/v1/chat/completions` | `192.168.77.210:8000`（或 ollama 11434） |
| `https://xxx:5000/209/llm/deepseek-v4-flash/v1` | `192.168.77.209:8001` |
| `https://xxx:5000/210/llm/v1/chat/completions`（旧卡片，body 带 model） | 网关 → 按 model 分发，缺省 deepseek-v4-flash |

## 3. nginx 改动（B 机）

沿用现有 `map $uri` 风格，将硬编码单规则升级为 **节点+模型 → 后端** 注册表：

```nginx
map $uri $llm_model_target {
    default "";
    ~^/210/llm/deepseek-v4-flash/   http://192.168.77.210:18888;
    ~^/210/llm/qwen3.8/             http://192.168.77.210:8000;
    ~^/209/llm/deepseek-v4-flash/   http://192.168.77.209:8001;
    # 新增模型只需追加一行（由 modelctl nginx-snippet 自动生成）
}
```

### location 优先级（正则 location 按声明顺序匹配）

**顺序即优先级，`v1` 必须优先于模型名匹配，否则 `/llm/v1/...` 会被误判为模型名 `v1`：**

1. **旧用量统计**（兼容旧卡片）：`~ ^/(\d+)/llm/v1/api/usage(.*)$`
   → `proxy_pass http://192.168.77.$1:5002/api/usage$2`（保持现状）
2. **按模型用量统计**：`~ ^/(?<node_id>\d+)/llm/(?<model_name>[^/]+)/v1/api/usage$`
   → `proxy_pass http://192.168.77.$node_id:5002/api/usage?model=$model_name`
3. **网关**：`~ ^/(?<node_id>\d+)/llm/v1/(?<rest>.*)$`
   → `proxy_pass http://192.168.77.$node_id:5003/$rest`（统一 5003，可加 `proxy_buffering off` 支持流式）
4. **模型直连**：`~ ^/(?<node_id>\d+)/llm/(?<model_name>[^/]+)/(?<llm_rest>.*)$`
   - `if ($llm_model_target = "") { return 404; }`
   - `rewrite ^/\d+/llm/[^/]+/(.*)$ /$1 break;` 剥离前缀，保留 `/v1/...`
   - `proxy_pass $llm_model_target;`（与现有 data-receiver 模式一致）

校验矩阵：

| 请求路径 | 命中 |
|---|---|
| `/210/llm/v1/api/usage` | 规则 1（旧用量） |
| `/210/llm/deepseek-v4-flash/v1/api/usage` | 规则 2 |
| `/210/llm/v1/chat/completions` | 规则 3（网关） |
| `/210/llm/deepseek-v4-flash/v1/chat/completions` | 规则 4（直连） |
| `/210/llm/unknown-model/v1/...` | 规则 4，map 为空 → 404 |

### 注册表生成（`modelctl nginx-snippet`）

新增子命令，扫描本节点 `models/*.yaml`，结合节点信息输出该节点的 map 片段：

```bash
modelctl nginx-snippet --node 210 --host 192.168.77.210
```

输出 `llm-routes-210.conf`（map 片段），部署时同步到 B 机 `/etc/nginx/llm-routes/`，nginx 配置中 `include /etc/nginx/llm-routes/*.conf;`。**加模型 = 加一条 profile，nginx 段自动同步**，杜绝手写漂移。

## 4. 网关组件（modelctl gateway，FastAPI 自研）

### 部署形态

网关与 modelctl 同节点部署，读本节点 `models/*.yaml` 作为注册表；nginx 以统一规则 `/<node>/llm/v1/` → 本节点网关（5003）。多节点 = 每个模型节点各起一个网关，互不依赖。

### 端点与行为

| 端点 | 行为 |
|---|---|
| `POST /v1/chat/completions` | 读 body.model → 查注册表 → **改写为后端期望的模型名**后转发 → 流式 SSE 原样透传 |
| `POST /v1/completions` / `POST /v1/embeddings` | 同上，按 model 路由透传 |
| `GET /v1/models` | 返回本节点可用模型列表（OpenAI 格式）；不可达模型剔除并记日志 |

### 注册表构建

复用 `core/profile.py`，加载本节点全部 profile：

```python
{profile.name: {
    "backend_url": f"http://127.0.0.1:{profile.port}",
    "upstream_model": <引擎段取模型名>,   # ollama.model / vllm.model / llamacpp 任意
    "api_key": profile.api_key,            # 可能为 None
    "engine": profile.engine,
}}
```

`upstream_model` 是**必须的**：ollama 严格校验 body.model（客户端发 `model=qwen3.8` 需改写为 `qwen3.8:27b`，否则 404 "model not found"）；llamacpp 忽略 model 名，透传原名即可；vllm/sglang 取 serve 的模型名。

### 请求处理流程

1. 校验 Content-Type，读取完整 body（含 `stream: true` 的请求，请求体本身非流式）
2. 提取 `model`；未知/缺省 → 回退 `GATEWAY_DEFAULT_MODEL`（默认 deepseek-v4-flash，保持旧卡片行为）
3. 改写 `body.model = upstream_model`
4. 透传 `Authorization` 头，用 httpx 转发到 backend_url
5. `stream: true` → httpx 流式读，`StreamingResponse` 逐块转发 SSE；否则整体透传
6. 后端非 2xx → 状态码与 body 原样透传；连接失败 → 502 + JSON 错误；读超时 `GATEWAY_READ_TIMEOUT`（默认 600s，对齐 nginx）

### 健康检查

网关启动时（及定时）探测本节点各 profile 端口 `/health`（llamacpp/vllm 有，ollama 用根路径）；不可达模型从 `/v1/models` 剔除。

## 5. modelctl 集成

- 新增子命令：
  - `modelctl gateway start|stop|status`：管理网关进程（复用 `core/process.py`，日志进 LOG_DIR）
  - `modelctl nginx-snippet --node <id> --host <ip>`：输出本节点 nginx map 片段
- `pyproject.toml` 新增可选依赖组：`gateway = ["fastapi", "uvicorn", "httpx"]`（`uv sync --extra gateway` 安装）

## 6. 配置项（.env 新增）

```bash
NODE_ID=210                    # 本节点编号（nginx 路径前缀）
NODE_HOST=192.168.77.210       # 本节点 IP（nginx-snippet 用）
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=5003
GATEWAY_DEFAULT_MODEL=deepseek-v4-flash
GATEWAY_READ_TIMEOUT=600
```

## 7. 兼容性与错误处理

### 向后兼容清单

| 现有用法 | 改后行为 |
|---|---|
| `.../210/llm/v1/...`（旧 cc-switch 卡片） | → 网关，缺省模型 deepseek-v4-flash，行为与现在一致 |
| `.../210/llm/v1/api/usage`（旧用量查询） | 规则 1 保留 → `:5002` |
| `.../210/llm/<model>/v1/...` | 新增直连路径 |
| `/smoke_detect`、`/anylabeling`、各节点 `detect/api`、RabbitMQ、Jupyter 等 | 全部不动 |

### 错误处理

- nginx：未知模型/节点 → 404；后端不可达 → 502
- 网关：未知 model → 回退默认模型；后端 4xx/5xx 透传；连接失败 502；读超时 600s
- 不可达模型从 `/v1/models` 剔除，不阻断其他模型

## 8. 测试

- **tests/test_gateway.py**（pytest，httpx ASGI/transport mock）：
  - 注册表构建（fixture profile → backend_url / upstream_model）
  - model 改写（ollama 场景：`qwen3.8` → `qwen3.8:27b`）
  - 流式 SSE 透传（mock 后端分块响应 → 逐块输出）
  - 未知 model 回退默认模型
  - `/v1/models` 格式与健康剔除
  - 502 / 后端状态透传
- **tests/test_nginx_snippet.py**：给定 profiles + node/host → 断言生成的 map 片段
- **集成验证**：部署后 curl 各路径，断言转发目标与 404/502（覆盖第 3 节校验矩阵）

## 9. 实施顺序

1. `modelctl nginx-snippet` 子命令 + 测试
2. 网关模块（`src/modelctl/gateway.py`）+ `modelctl gateway` 子命令 + 测试
3. pyproject 可选依赖组 `gateway`
4. B 机 nginx 配置改造（map + 4 条 location + include 目录）
5. 端到端验证（cc-switch 新卡片 + 旧卡片 + 用量查询）
