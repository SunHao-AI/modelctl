# 引擎端口仅回环绑定加固设计（堵 AI Agent 直连绕过网关）

- 日期：2026-09-08
- 状态：设计已确认（用户逐节评审通过，采用方案 A + 删直连走网关）
- 关联：`engines/vllm.py`（主改）、`engines/base.py`、`core/nginx_snippet.py`、`models/vllm/qwen3.8-flash-next.yaml`、`docs/nginx/llm-routing.example.conf`
- 上游参照：2026-09-07 网关鉴权 spec（三层防线）、2026-09-08 accounts spec（账号/多 Key 限额）

## 1. 背景与目标

### 问题

用户部署 `qwen3.8-flash-next-vllm` 后，被外部 **AI Agent 扫描**到项目，并**直连引擎端口 `8110`** 调用了模型，完全绕过网关 `5003` 的鉴权/账号/多 Key/token 限速/审计。

| 事实 | 位置 |
|---|---|
| vLLM venv 分支 `--host 0.0.0.0`，引擎在**所有网卡**上监听 | `engines/vllm.py` `build_command` |
| vLLM docker 分支 `-p {port}:8000`，宿主机 docker-proxy 监听全网卡 | `engines/vllm.py` `build_command` |
| 网关转发引擎默认走 `127.0.0.1`（loopback） | `core/gateway.py` `build_registry` / `build_groups` 默认 `host="127.0.0.1"`，`create_app` 调用不传 host |
| 网关自身 `GATEWAY_HOST` 默认 `0.0.0.0`，对外可达 | `core/gateway.py` 启动段 |
| nginx 模型直连 location 指向 `http://{host}:{p.port}`（内网 IP:8110） | `core/nginx_snippet.py` `build_llm_map` |

**根因**：引擎端口对网卡暴露且未受限，成为独立于网关外的"第二入口"。任何经网关的流量才受 `verify_client`/限额保护，直连引擎则上游认证形同虚设（见 `docs/known-pitfalls/backend/网关鉴权.md`「准入与上游认证分离」陷阱）。

### 目标

1. 让引擎端口**只在机器本地 loopback 可达**，外部（含内网其他主机、公网、AI Agent）物理上无法直连。
2. 不破坏网关经 loopback 转发引擎的现有链路。
3. nginx 层**移除模型直连 location**，所有 `/llm/*` 统一走网关（鉴权限速唯一入口）。
4. 引入 `bind_host` 配置项，默认 `127.0.0.1`，可按需回退（显式 `0.0.0.0` 场景）。

### 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 主方案 | **方案 A：引擎只绑定 loopback** |
| 网关与引擎拓扑 | **同一台机器**（网关走 loopback 转发引擎，方案 A 可行） |
| nginx 模型直连 location | **删除**，统一走网关（用户选定「删直连走网关」） |
| 引擎端口对外需求 | **无需外部直连**（用户已确认，成立零暴露）
| 执行方式 | 先写设计文档，确认后再动代码（用户选定） |
| `--host` 覆盖优先级 | `bind_host` **权威**，置于命令末尾（extra 之后），不被 `extra_args` 里的同名 `--host` 覆盖 |

### 非目标

- 不做多实例/多网的端口隔离（本机 loopback + 网关单点入口即可）。
- 本次修复以 vLLM（用户当前部署）为起点，随后按用户要求扩展到 aphrodite/sglang/tokenspeed/tensorrt_llm/lmdeploy/ollama/unsloth 全部引擎（见 §6），所有模型 YAML 统一默认 `bind_host: 127.0.0.1`。
- 不改网关鉴权/限额逻辑本身（已有能力，只是此前被直连绕过）。

## 2. 总体架构

```
AI Agent / 外部 ──▶ ✗ 直连引擎 :8110  （引擎仅绑 127.0.0.1，外部连接被拒）
客户端 ──▶ ① nginx（B 机）──▶ ② 网关 :5003（A 机）
              （删除模型直连 location）      │ verify_client() 账号/多Key/限额/审计
                                            └─loopback▶ ③ 引擎 :8110（仅 127.0.0.1）
```

- **引擎层**：只监听 `127.0.0.1`，机器上仅网关（同机）与本地进程可达。
- **网关层**：`GATEWAY_HOST` 保持对外可达，作为唯一对外入口。
- **nginx 层**：删除模型直连 location，所有 `/llm/*` 转发到网关 `5003`。

## 3. 引擎绑定改动（`engines/vllm.py`）

`build_command` 内读取 `bind_host`，安全默认 `127.0.0.1`：

```python
bind_host = str(cfg.get("bind_host", "127.0.0.1"))   # 安全默认：仅回环
```

### 3.1 venv 分支

`--host` 从 `model_args` 中移除，改为**追加到命令末尾（`extra` 之后）**，使 `bind_host` 权威、不被 `extra_args` 里的 `--host` 覆盖：

```python
cmd = [
    envs.engine_bin("vllm", "vllm"),
    "serve",
    str(cfg["model"]),
    *model_args,          # 移除其中的 "--host", "0.0.0.0"
    "--port",
    str(self.profile.port),
    *tail,                # api_key_args() + extra
    "--host",
    bind_host,            # 权威绑定：置于 extra 之后，避免被 extra_args 覆盖回 0.0.0.0
]
```

> 说明：`--port` 的"extra 可覆盖"语义**保持不变**（`--port` 仍置于 `tail` 之前）。仅 `--host` 提升为权威参数，这是安全修复的核心——安全值不应被临时参数静默回退。若确需对外暴露，改 `bind_host` 配置项而非塞 `--host`。

### 3.2 docker 分支

**关键**：宿主机 `-p` 绑定改为 `{bind_host}:{port}:8000`，让 docker-proxy 只监听 `127.0.0.1`。容器内 vLLM 的 `--host` **保持不变**（不动 `model_args`），避免 docker-proxy 转发目标地址为 `127.0.0.1` 时的边界问题：

```python
"-p",
f"{bind_host}:{self.profile.port}:8000",
```

> 效果：宿主机仅在 `127.0.0.1:{port}` 上转发到容器 `8000`，外部无法连接宿主机该端口；容器内 vLLM 仍绑 `0.0.0.0` 但被 docker-proxy 的监听地址隔离。

### 3.3 配置项接入

`bind_host` 为 `vllm` 段普通键，经 `profile.engine_config` 读取（`engines/base.py` 不感知，属引擎适配器私有配置），无需改 `profile.py`。

## 4. nginx 直连移除（`core/nginx_snippet.py`）

`build_llm_map` 删除对每个 profile 生成的模型直连条目（指向 `http://{host}:{p.port}` 的几行），保留统一网关入口：

```python
# 删除以下两个循环生成的条目：
#   ~^/{node_id}/llm/{p.name}/    http://{host}:{p.port};
#   ~^/{node_id}/llm/{alias}/     http://{host}:{p.port};
# 保留：
#   ~^/{node_id}/llm/v1/          http://{host}:{gateway_port};
```

同时更新 `docs/nginx/llm-routing.example.conf`：
- 移除「模型直连」location（原注释：本 location 不改写 Authorization，是 Ollama/TRT-LLM 的唯一准入闸门——引擎绑 loopback 后该闸门已无意义，统一走网关）。
- 保留网关 location（`$llm_reject`）与用量统计 location（`$llm_reject_all` 视需保留）。

> 注意：`$llm_reject_all`（含各 profile key）仅剩用量 location 使用；若用量 location 也一并收口，可同步精简。若某引擎确实需要直连（如 Ollama/TRT-LLM 引擎侧无鉴权），须依赖网关作为唯一入口——这正是本次加固的意图。

## 5. 模型配置（`models/vllm/qwen3.8-flash-next.yaml`）

在 `vllm:` 段显式声明 `bind_host: 127.0.0.1`（显式化便于回溯；缺省也由代码兜底 `127.0.0.1`）：

```yaml
vllm:
  ...
  port: 8110
  api_key: ${API_KEY}
  bind_host: 127.0.0.1   # 新增：仅回环绑定，禁止外部直连引擎端口
```

## 6. 同类引擎扩展（已落地，全部引擎统一默认）

按 §3.1 同款方案，`bind_host`（默认 `127.0.0.1`、置于命令末尾权威覆盖）已扩展到**全部引擎适配器**与**所有模型 YAML 配置**：

| 引擎 | 绑定参数 | 位置 |
|---|---|---|
| aphrodite | `--host` | `engines/aphrodite.py` |
| llama.cpp | `--host` | `engines/llamacpp.py` |
| sglang | `--host` | `engines/sglang.py` |
| tokenspeed | venv `--host` / docker `-p {bind_host}:{port}:8000` | `engines/tokenspeed.py` |
| tensorrt_llm | venv `--host` / docker `-p {bind_host}:{port}:8000` | `engines/tensorrt_llm.py` |
| lmdeploy | `--server-name` | `engines/lmdeploy.py` |
| ollama | `OLLAMA_HOST`（环境变量） | `engines/ollama.py` |
| unsloth | `-H` | `engines/unsloth.py` |
| vllm | `--host` | `engines/vllm.py` |

> 统一规则：`bind_host` 安全值置于命令/环境变量末尾（`extra_args` 之后），确保不被 `extra_args` 里的同名参数覆盖回 `0.0.0.0`；docker 运行时通过宿主机 `-p {bind_host}:{port}:8000` 隔离，容器内仍绑 `0.0.0.0`（保持容器内部语义）。
>
> 覆盖说明：模型 YAML 默认插入 `bind_host: 127.0.0.1`；确需对外暴露时显式改 `0.0.0.0`（配合防火墙/白名单，非默认，风险自担）。

## 7. 验证与测试

1. **单元**：各引擎 `build_command` 断言绑定参数为 `127.0.0.1` 且位于命令/环境末尾（`extra` 之后）；docker 分支断言宿主机 `-p 127.0.0.1:{port}:8000`。profile 未配 `bind_host` 时回退 `127.0.0.1`。
2. **覆盖测试**：`bind_host: 0.0.0.0` 或 `extra_args: '--host/-H 0.0.0.0'` 时，命令最终绑定参数仍为 `bind_host`（权威）。
3. **nginx 片段**：`build_llm_map` 不再生成 `/{node}/llm/{name}/` 直连条目，仅保留网关入口与原有网关/用量 location。
4. **集成（服务器）**：重启引擎（`bind_host=127.0.0.1`）后，
   - 本机 `curl http://127.0.0.1:{port}/v1/models` → 正常；
   - 外部/内网其他主机 `curl http://<内网IP>:{port}/v1/models` → **连接被拒**（原漏洞已封堵）；
   - 经网关 `curl http://<网关>:5003/v1/chat/completions` → 正常，鉴权/限额/审计生效。
5. **回归**：网关走 loopback 转发引擎链路不受影响；nginx 所有 `/llm/*` 请求经网关返回。

## 8. 风险与回退

| 风险 | 处置 |
|---|---|
| 存量 `extra_args` 含 `--host 0.0.0.0` 导致回退暴露 | `--host` 置于命令末尾权威覆盖，`bind_host` 默认安全 |
| 需要引擎对外暴露的个别场景（如独立测试机直连） | 显式配置 `bind_host: 0.0.0.0`，并配合防火墙/白名单；非默认 |
| nginx 删直连后旧直连客户端（`/llm/{name}/` 免网关调用方）全断 | 统一改走网关路径，由网关鉴权/限额接管；属预期收敛 |
| docker 容器内 `--host` 未改导致的边界问题 | 保持容器内不动，仅宿主机 `-p` 绑定 `127.0.0.1`（§3.2） |

回退方式：删除 yaml `bind_host` 或设为 `0.0.0.0` 即恢复旧行为（不破坏原有启动语义）。

## 9. 验收清单

- [x] `engines/vllm.py` venv 分支 `--host` 使用 `bind_host`（默认 `127.0.0.1`）且位于 `extra` 之后
- [x] `engines/vllm.py` docker 分支 `-p {bind_host}:{port}:8000`
- [x] `core/nginx_snippet.py` `build_llm_map` 移除模型直连条目
- [x] `docs/nginx/llm-routing.example.conf` 移除「模型直连」location
- [x] `models/vllm/qwen3.8-flash-next.yaml` 增 `bind_host: 127.0.0.1`
- [x] 单元测试覆盖默认回退 / `extra_args` 覆盖保护（含 venv 与 docker 分支）
- [ ] 服务器实证：外部直连 `:8110` 被拒，经网关正常
