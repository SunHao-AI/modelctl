# modelctl 请求级性能指标 + 全引擎审计日志设计

日期：2026-08-31
状态：已确认（用户评审通过）

## 1. 背景与目标

### 1.1 现状

- vLLM 支持 `--enable-per-request-metrics` 参数：启用后每个 OpenAI 兼容响应会额外携带 `metrics` 对象（`time_to_first_token_ms` / `generation_time_ms` / `queue_time_ms` / `mean_itl_ms` / `tokens_per_second`），流式场景出现在**最后一个 SSE chunk**。
- 当前项目 `modelctl` 的聚合统计（`core/stats.py` `UsageCollector`：`/metrics` 轮询 + 滑窗速率）与网关 token 累计（`core/gateway.py` 旁路解析 `usage` 调 `collector.record_tokens`）都工作在**聚合口径**上，区分不出单次请求。
- [build_command](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/engines/vllm.py#L99-L134) 未附加 `--enable-per-request-metrics`，引擎侧原生数据未被消费。

### 1.2 目标

1. vLLM 启动参数支持 `--enable-per-request-metrics`（yaml 显式开关，默认关闭，向后兼容）。
2. 通过统一**网关**对所有引擎的请求级 token 数 + 性能指标做**请求级审计**（JSONL 文件）。
3. 提供 `modelctl audit` CLI 子命令查询 / 统计 / 手动清理审计日志。
4. vLLM 开启该参数后，审计记录的 token/metrics **优先采用 vLLM 原生字段**，本项目聚合层面（collector 差分）作为兜底。
5. 审计日志带**定时清理**（按保留天数 + 总大小上限），避免无限增长。

### 1.3 关键决策（已与用户确认）

| 决策点 | 结论 |
|---|---|
| 集成目的 | 单请求性能指标 + 请求级 token 审计**两者都要** |
| 审计范围 | 全引擎（vllm / sglang / llamacpp / ollama / unsloth）统一写入 |
| 记录位置 | **网关**作为单一接缝点，所有 `/v1/{chat/completions,completions,embeddings,messages}` 都经过它；直连引擎端口的流量**不**产生审计记录（文档中写明） |
| 输出形式 | JSONL 文件（按天分片） + `modelctl audit` CLI，**不**加对外 API |
| vLLM 参数控制 | yaml `vllm:` 段新增 `enable_per_request_metrics: true/false`，默认 false；不高于最低版本启动时报错 |
| 流式 usage 配合 | 同段新增 `enable_force_include_usage: true/false`，默认 false；与前者同步开启才保证流式末块有 `usage` |
| 数据源优先级 | 见 §2（性能指标 source 二值 + token 来源两值各自独立；token 三级回退规则见 §2 表格） |
| 清理策略 | 保留天数 + 总大小上限，任一达到即触发；绝不清当天文件 |
| 与现有 stats 关系 | **共存，不替换**：stats 看趋势/聚合，audit 看单次请求 |

## 2. 数据源优先级（§1 决策落地）

### 2.1 字段语义
- `source`：**性能指标来源**（二值：`vllm_native` / `gateway_estimate`；预留 `stats_polling` 不用）
- `tokens_source`：**token 数来源**（两值：`response-usage` / `collector-diff`），与 `source` 独立
- "三层回退"仅指 **token 取值**的 fallback 顺序；性能指标只按 `source` 二分

### 2.2 取值规则

每条审计记录写入时按以下规则取值，满足即用、不满足降级：

| 条件 | token 来源 | 性能指标来源 | source 标记 |
|---|---|---|---|
| 响应含原生 `metrics` 对象（vLLM 开启参数） | 响应 `usage` | 原生 `metrics` | `vllm_native` |
| 无原生 `metrics`，但 `usage` 可用 | 响应 `usage` | 网关自测 TTFT/tps | `gateway_estimate` |
| `usage` 也不可用（流式未回 usage / SSE 被丢弃） | **collector 差分**（网关聚合累计兜底） | 网关自测 TTFT/tps | `gateway_estimate`（附 `tokens_source: "collector-diff"`） |

规则解释：
- `usage` 是 vLLM 引擎按请求计费打出的**精确值**，与"vLLM 原生能力"同源；token 始终取 `usage`。
- "本项目实现兜底"专指**由网关对 collector 做前后差分**这一条路径（即 `tokens_source: "collector-diff"`）。
- `gateway_metrics`（TTFT/tps）无论 `source` 如何，只要网关能测就填写，便于**对比** vLLM 原生 metrics 与网关自测值、回归测试。
- `stats_polling` 作为预留值（直连引擎端口、网关感知不到的流量），本次实现**不使用**，仅约定枚举值保留以便未来扩展。

## 3. vLLM 引擎侧

### 3.1 YAML 新字段

```yaml
vllm:
  model: Qwen/Qwen3.8-27B
  # ... 现有字段 ...
  enable_per_request_metrics: true    # 新增，默认 false
  enable_force_include_usage: true    # 新增，默认 false
```

- `enable_per_request_metrics: true` → `build_command` 在命令尾部追加 `--enable-per-request-metrics`
- `enable_force_include_usage: true` → `build_command` 追加 `--enable-force-include-usage`（保证流式末块回 `usage`；不开则流式场景审计走 collector-diff 兜底）
- **未配置时 `build_command` 输出与改造前逐字节一致**（向后兼容的关键守门）
- 这两个字段**仅 vLLM 段识别**：其他引擎的 yaml 写了不报 warning（避免误伤）

### 3.2 版本探测与 fail-fast

在 `VllmAdapter.check_requirements` 的 `envs.ensure_env("vllm")` 之后新增：

```python
if cfg.get("enable_per_request_metrics") or cfg.get("enable_force_include_usage"):
    v = envs.vllm_version()    # subprocess.run([bin, "--version"]) 解析 stdout 首 token
    min_v = (0, 13, 0)         # 实施时实测/核对真实最低版本后固化（联网资料见 §8 参考资料）
    if v and v < min_v:
        raise RequirementError(
            f"enable_per_request_metrics 需 vLLM ≥ {min_v}，当前 {v}；"
            "可升级（uv sync --project envs/vllm --upgrade vllm）或在 yaml 中关闭该项"
        )
    elif v is None:
        logger.warning("无法探测 vLLM 版本（将放行；若启动报错请人工确认版本）")
```

- 探测方式：`subprocess.run([vllm_bin, "--version"], capture_output=True, timeout=5)`；**不** `import vllm`（避免 venv 切换/加载成本）
- 进程内缓存探测结果（一次解析），同进程 start/status 复用
- 探测失败**不** fail-fast（CI/降级场景友好），仅 warning

### 3.3 与统计服务共存（**不替换**）

```
现有聚合 stats（保留不改）                新增请求级审计
─────────────────────────              ─────────────────────────
stats.py: UsageCollector               core/audit.py: RequestAuditLog
  ↑                                          ↑
gateway.record_tokens         [同一次响      gateway（同一响应同一解析动作）
  不变                          应同一解析]    但同时写 audit
（stats 服务 5002/api/usage）                       （JSONL + modelctl audit CLI）
```

- 一次响应 → **两次副作用**：`collector.record_tokens(...)`（现有，不动）+ `audit_log.record(entry)`（新增）
- 两者**独立失败隔离**：审计 I/O 故障不影响 stats；反之亦然
- vLLM 开启参数后 `/metrics` 端点的 `vllm:prompt_tokens_total` 不变，stats 服务照常
- 文档表述（面向用户）：**stats 服务看趋势/聚合，audit 看单次请求**

## 4. JSONL Schema 与写入

### 4.1 文件布局

```
<_AUDIT_DIR>/modelctl-YYYY-MM-DD.jsonl        # 按天分片，默认 AUDIT_DIR=data/audit
<_AUDIT_DIR>/modelctl-deleting-<ts>.jsonl     # 清理暂存（rename 后删除）
```

- `AUDIT_DIR` 来自 `.env`，默认 `data/audit`；与 `USAGE_DATA_DIR` 解耦（不污染 stats 持久化目录）
- 天分片按**网关本地时钟**；跨天时刻不强制重命名（由下一条记录触发文件名切换）
- `os.open(O_APPEND)` 原子追加，不引入多进程锁（同一网关进程内用 `threading.Lock` 串行化）
- 写入失败（磁盘满/权限）**静默吞掉并 `logger.warning`**，绝不阻塞/中断请求转发

### 4.2 单条记录 Schema

```json
{
  "ts": "2026-08-31T10:23:11.123+08:00",
  "model": "qwen3.8-vllm",
  "engine": "vllm",
  "path": "chat/completions",
  "stream": true,
  "source": "vllm_native",
  "tokens_source": "response-usage",
  "prompt_tokens": 42,
  "completion_tokens": 128,
  "total_tokens": 170,
  "input_char_len": 512,
  "native_metrics": {
    "time_to_first_token_ms": 85.2,
    "generation_time_ms": 1240.5,
    "queue_time_ms": 12.3,
    "mean_itl_ms": 9.1,
    "tokens_per_second": 103.2
  },
  "gateway_metrics": {
    "ttft_ms": 92.4,
    "generation_time_ms": 1260.8,
    "tokens_per_second": 100.7
  },
  "status_code": 200,
  "error": null,
  "finish_reason": "stop"
}
```

- `source` 枚举：`vllm_native` / `gateway_estimate`（`stats_polling` 预留不用）
- `tokens_source` 枚举：`response-usage` / `collector-diff`
- `native_metrics`：原样透传 vLLM 返回的 `metrics` 对象，**不裁剪字段**（vLLM 升级新字段自动可见）
- `gateway_metrics.ttft_ms`：流式场景 = 从请求发出到首块 `data:` 字节到达的耗时；非流式为 null
- `gateway_metrics.tokens_per_second`：`completion_tokens / generation_time_s`（网关侧自测）
- 非 vLLM 引擎（llamacpp/sglang/ollama/unsloth）：`native_metrics` 为 null，`source` 恒为 `gateway_estimate`
- 敏感数据**不记录**：`messages` / `content` / API key
- `input_char_len`：请求 body 序列化后字符数（粗略索引长上下文）
- 每行一个 JSON 对象（`ensure_ascii=False`，UTF-8）

### 4.3 网关三条链路的精确改点

均位于 [core/gateway.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/gateway.py)：

**A. OpenAI 非流式**（[L687-712](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/gateway.py#L687-L712)）
- 现有 `json.loads(upstream.content)` 处**同时提取 `usage` 和 `metrics`**（目前只用 usage，metrics 被丢弃）
- 计时：`t0 = 发出请求前 time.monotonic()`；`t1 = 读完全响应`
- `gateway_metrics`：`ttft_ms=null`，`tokens_per_second = completion/(t1-t0)`（非流式无 TTFT）
- `source` 判定：响应有 `metrics` → `vllm_native`，否则 `gateway_estimate`
- `_record_usage(`collector.difference delta 复用现有逻辑）+ 新增 `audit_log.record(entry)`

**B. OpenAI 流式**（[L641-686 `_sse_stream`](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/gateway.py#L641-L686)）
- 每个 `data.payload.json.loads` 处扩展捕获：
  - `data.metrics` → `seen_metrics`（vLLM 约定末块才有，实现上不预设，哪个 chunk 带就取哪个，便于兼容未来实现）
  - `data.usage` → `seen_usage`（现有 `_record_usage` 逻辑保留不动）
  - 首块 TTFT：第一个 `yield chunk` 前记 `t_first`，`TTFT_ms = (t_first - t_start) * 1000`
- `finally` 块（现有 `logger.info("OpenAI 流式响应摘要...")` 处）在 `client.aclose()` **之前**写入 audit
- 关键不变式：**审计 I/O 失败不能打断 SSE**——`record` 内部已 try/except 静默

**C. Anthropic 流式 / 非流式**（[L500-546](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/gateway.py#L500-L546)）
- 响应 JSON 格式：`usage` 在根级（非流式），流式在 `message_delta` 事件里
- **只记 token**（`usage` 可用则取，否则 collector-diff），**不**解析性能字段（Anthropic 格式无 per-request metrics）
- `source` 恒为 `gateway_estimate`，`native_metrics` 为 null
- 流式 `message_delta` 捕获：行前缀不变，新增 `elif data.get("type") == "message_delta": seen_usage = data.get("usage")`

### 4.4 `core/audit.py` 模块边界

- 新建 `core/audit.py`，导出 `RequestAuditLog`
- **不依赖** `stats.py` / `gateway.py` 任何对象；只依赖标准库 + `envfile.load_env`
- 公开的 API：
  - `RequestAuditLog(data_dir: Path, retention_days: int, max_size_mb: int, cleanup_interval_s: float)`
  - `record(entry: dict) -> bool`（`entry` 为 §4.2 的 JSONL 单条记录 dict；写入成功返回 True，失败 False 仅 warning）
  - `ensure_cleanup_thread() -> None`（幂等启动 daemon 线程）
  - `collect_dead_files() -> list[Path]`（纯函数，不删除；给 CLI `--cleanup --dry-run` 用）
  - `destroy() -> None`（`stop_cleanup_thread`）
- **明确区分**：
  - `source` 是**记录字段**（三值：`vllm_native` / `gateway_estimate` / 预留 `stats_polling`）
  - `tokens_source` 是**独立字段**（两值：`response-usage` / `collector-diff`），与 `source` 无派生关系
  - 不要将 `source` 误读为"数据来源"统称，它只表达"性能指标来源"
- `GatewayModel` 在 `create_app` 里挂 `audit_log` 属性（**多个 GatewayModel 共享同一实例**）
- 测试注入：`create_app(..., audit_log=NoopAuditLog())` 允许单测关闭写盘；不注入时默认 `RequestAuditLog(Path(AUDIT_DIR or "data/audit"))`

### 4.5 流式写入顺序时序

```
t0          发出请求（t_start = time.monotonic()）
...
t_first     首块 data 字节到 → gateway_metrics.ttft_ms = (t_first - t_start) * 1000
...
t_last      末块到（含 metrics + usage）
            → 现有 collector.record_tokens(...) 不动
            → 新增 audit_log.record(builder.build(...))   ← 在 aclose() 之前
            → source = "vllm_native" (若 metrics 不空)
aclose()    SSE 连接释放
```

## 5. 定时清理

### 5.1 新增配置（[.env.example](file:///d:/WorkPlace/Pycharm/modelctl/.env.example)）

```bash
# ---------- 请求级审计日志 ----------
AUDIT_DIR=/raid5/sh/code/modelctl/data/audit
AUDIT_RETENTION_DAYS=30
AUDIT_MAX_SIZE_MB=512
AUDIT_CLEANUP_INTERVAL=86400
```

由 `core/envfile.py` 的 `load_env()` 统一加载（现有机制，无需改）。

### 5.2 清理机制

`RequestAuditLog` 内部：

- 后台 worker（daemon），`ensure_cleanup_thread` 幂等启动
- 循环：`sleep(AUDIT_CLEANUP_INTERVAL)` → 一次 `cleanup_once()`
- `cleanup_once()`：
  1. 收集审计目录内所有 `modelctl-*.jsonl` 与 `modelctl-deleting-*.jsonl`
  2. 按两条规则分别计算应删列表（时间 / 大小），取并集
  3. 删除前先 rename 到 `.audit-deleting-<ts>`（避免写到一半的文件被 `modelctl audit` CLI 读取）
  4. `os.unlink`，失败 `logger.warning` 后继续
- **两条规则（任一达到即触发）**：
  - **按时间**：文件名日期解析失败或 `日期 < 今天 - RETENTION_DAYS` 的文件删除
  - **按总大小**：`sum(stat.st_size for x in 文件列表) > MAX_SIZE_MB * 1024**2` 时，**从最旧删到低于上限**
- **铁律：绝不清当天文件**（至少保一天数据，避免 in-flight 请求过多时"数据全被清掉"）
- `RETENTION_DAYS=0` 时仅大小上限生效；`MAX_SIZE_MB=0` 时仅时间上限生效；都为 0 完全不清理

### 5.3 手动清理（CLI 不依赖网关进程）

`modelctl audit --cleanup` 与 `--cleanup --dry-run` 走**同一套 `collect_dead_files` 逻辑**（独立函数，不读取网关单例），接受当前 `AUDIT_*` 环境变量：

- 默认显示：`Deleted 3 files, freed 142.7 MB`
- `--dry-run`：`Would delete 3 files (142.7 MB): modelctl-2026-07-31.jsonl, ...`

## 6. CLI 命令

[cli.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/cli.py) `argparse` 子命令组新增 `audit`，与 `stats` / `gateway` / `all` 同级：

```
modelctl audit [--model NAME] [--endpoints EP] [--since T]
               [--limit N] [--json]                     # 查最近 N 条
modelctl audit path                                      # 打印 AUDIT_DIR 绝对路径
modelctl audit stats                                     # 目录大小/文件数/按天字节分布
modelctl audit --cleanup [--dry-run]                     # 手动清理
```

参数说明：
- `--model`：按 `model` 字段过滤（精确匹配）
- `--endpoints`：逗号分隔端点列表（chat/completions,completions,embeddings,messages）
- `--since`：`1h` / `24h` / `7d` / ISO 时间戳
- `--limit`：条数上限，默认 20
- `--json`：原样 JSONL 输出（便于 jq / logstash / 可视化管道）
- 表格列（非 `--json` 模式）：`ts  model  endpoint  stream  src  tok_in/tok_out  ttft_ms  tps  status`

数据源：直接读 `AUDIT_DIR`，**不**依赖网关进程活着（CLI 与网关解耦）。

## 7. 配置文件改动

[.env.example](file:///d:/WorkPlace/Pycharm/modelctl/.env.example) 新增"请求级审计日志"段（§5.1），风格对齐现有 `USAGE_*` 变量组：

```bash
# ---------- 请求级审计日志 ----------
# 审计 JSONL 落盘目录（<DIR>/modelctl-YYYY-MM-DD.jsonl）
AUDIT_DIR=/raid5/sh/code/modelctl/data/audit
# 保留天数（文件名日期 < 今天-该值 的文件删除）；0 = 不按时间清理
AUDIT_RETENTION_DAYS=30
# 总大小上限（MB），超出从最旧删除；0 = 不按大小清理
AUDIT_MAX_SIZE_MB=512
# 定时清理检查间隔（秒），默认 1 天
AUDIT_CLEANUP_INTERVAL=86400
```

### 7.1 现网 profile 迁移

- 现有 8 个 vLLM profile **默认不加**两个新字段（行为与改造前完全一致）
- 仅 [models/vllm/qwen3.8.yaml](file:///d:/WorkPlace/Pycharm/modelctl/models/vllm/qwen3.8.yaml) 加：
  ```yaml
  enable_per_request_metrics: true
  enable_force_include_usage: true
  ```
  作为**示范模板**；其他 profile 按需启用
- [data/audit](file:///d:/WorkPlace/Pycharm/modelctl/data) 首次启动 `mkdir -p` 创建；如 `.gitignore` 已存在则追加 `/data/audit/`，**若已存在该条目则跳过**

### 7.2 README 增补

[README.md](file:///d:/WorkPlace/Pycharm/modelctl/README.md) "用量统计" 段落末尾增补"请求级审计"小节：
- 快速上手：配置 `AUDIT_*` → 在 vllm yaml 开启两个参数 → 重启 → `modelctl audit`
- 边界说明：直连引擎端口（绕过网关）的流量不产生审计记录
- 与 stats 服务的分工：stats 看趋势/聚合；audit 看单次请求
- `--cleanup` 用法

## 8. 测试与验收

### 8.1 测试策略

新增 `tests/test_audit.py`（或并入现有 `tests/test_gateway.py`，视项目组织风格）；**不**跑真实 vLLM 进程（用 `httpx.MockTransport` / monkeypatch 注入版本探测）。

#### 8.1.1 审计记录层

| 用例 | 断言 |
|---|---|
| `test_record_native_source` | 构造含 `metrics` 的响应 → `source=vllm_native`，`native_metrics` 字段原样、不被裁剪 |
| `test_record_usage_source` | `usage` 在、无 `metrics` → `source=gateway_estimate` |
| `test_record_fallback_source` | 无 `usage` → 走 collector-diff，`tokens_source=collector-diff` |
| `test_day_rollover` | fake `datetime.now` 跨天 → 文件名切换，旧文件保留 |
| `test_cleanup_by_retention` | 写入 4 个文件（今天 / 1 / 2 / 3 天前），RETENTION=2 → 保留今天+1+2 天前，删 3 天前 |
| `test_cleanup_by_size` | 3 个文件总 600MB，MAX=500 → 从最旧删；保当天至少 1 文件 |
| `test_cleanup_never_deletes_today` | 当日文件，MAX 多小都不动 |
| `test_record_failure_isolated` | mock `write` 抛 `OSError` → 不冒泡，`logger.warning` 发生 |
| `test_collect_dead_files_is_pure` | 同输入两次调用返回相同列表（不删除、不改状态） |

#### 8.1.2 网关集成（复用现有 MockTransport）

| 用例 | 断言 |
|---|---|
| `test_openai_non_stream_emits_audit` | 响应含 `metrics` → audit 写 1 条，`source=vllm_native` |
| `test_openai_stream_emits_audit_with_metrics_from_last_chunk` | SSE 末块带 `metrics` + `usage` → audit 写 1 条 `vllm_native`，`gateway_metrics.ttft_ms` 非 null |
| `test_openai_stream_fallback_without_usage` | SSE 不返 usage（未开 `--enable-force-include-usage`）→ 走 collector-diff，status 200 不受影响 |
| `test_anthropic_non_stream_usage_capture` | 根级 `usage` → audit 写 1 条 `source=gateway_estimate`，`native_metrics=null` |
| `test_anthropic_stream_message_delta_capture` | 流式 `message_delta` 事件取 usage |
| `test_audit_failure_does_not_break_proxy` | `audit_log.record` 抛异常 → 响应仍 200，stats 不受影响 |

#### 8.1.3 vLLM 命令拼装

| 用例 | 断言 |
|---|---|
| `test_build_command_with_per_request_metrics_flag` | yaml 两字段同时 true → cmd 含 `--enable-per-request-metrics` 与 `--enable-force-include-usage` |
| `test_build_command_default_unchanged` | yaml 未配置 → cmd 与改造前**逐字节一致**（关键守门） |
| `test_build_command_only_force_include_usage` | 只开 `enable_force_include_usage=true` → cmd 仅含 `--enable-force-include-usage` |
| `test_requirement_version_guard` | mock venv 版本 0.12.0 < 阈值 → `RequirementError` |
| `test_requirement_version_missing_warns_not_raises` | 版本探测失败 → 放行 + warning（monkeypatch `subprocess.run` 抛异常） |
| `test_requirement_not_flagged_no_check` | 两字段均 false → 不跑版本探测（加速 check） |

### 8.2 现有测试不回退

- `tests/test_engines_vllm.py`：`test_build_command_default_unchanged` 守门
- `tests/test_gateway.py`：现有 MockTransport 用例全量保留
- `tests/test_stats.py`：不动

### 8.3 验收标准（DoD）

1. 全新 vLLM profile 未配两个新字段 → 启动、stats、网关行为与改造前一致（`test_build_command_default_unchanged` 守门）
2. 配置开启 + vLLM 版本 ≥ 阈值 → 启动成功；配置开启 + 版本 < 阈值 → fail-fast
3. 一次请求产生**一条** JSONL 记录，字段齐全（`ts`/`model`/`engine`/`source`/`tokens_source`/`gateway_metrics` 至少；`native_metrics` 按规则可空）
4. 流式未开 `--enable-force-include-usage` 时记录仍可生成（collector-diff），请求不报 500
5. `modelctl audit stats` / `--cleanup --dry-run` 在网关未运行时也能工作
6. 磁盘满场景（mock `write` 异常）：网关转发不受影响，stats 不受影响
7. 所有现有测试保持通过（PR 级门禁）
8. audit 目录大小与文件数在 30 天后按 `AUDIT_RETENTION_DAYS` 收敛

### 8.4 边界与已知限制

- **仅网关抓取**：直连引擎端口的流量不产生审计记录（客户端自行消费 vLLM 返回的 `metrics` 字段）
- **`n > 1`（多次采样）请求**：vLLM 原生 metrics 计时字段返回 null（vLLM 官方说明）；token 计入 `usage` 仍准确。审计记录此场景下 `native_metrics` 内的 TTFT 可能 null，不影响 token 统计
- **多模态输入**（图像）：不计入 `input_char_len`（仅计 text 字符数）
- **`AUDIT_RETENTION_DAYS=0` 且 `MAX_SIZE_MB=0`**：永不清理（用户需自管）
- **跨机审计**：`AUDIT_DIR` 共享挂载时，清理逻辑**不**跨进程互斥（依赖单网关部署；若多网关共享目录需自行约定）

## 9. 实施顺序（建议，供 writing-plans 参考）

1. **`core/audit.py` + `tests/test_audit.py`**：独立模块 + 记录/清理/切日测试（无外部依赖，先落地）
2. **`core/envfile.py` 加载 `AUDIT_*`**（如已有 `load_env` 通配机制则免改）
3. **`core/gateway.py` 三处链路**接 `audit_log`（A 非流式、B 流式、C Anthropic 流/非流）+ MockTransport 集成测试
4. **`engines/vllm.py`**：`build_command` 追加参数 + `check_requirements` 版本探测 + `envs.vllm_version()` 缓存 + 测试
5. **`cli.py` `audit` 子命令** + 表格 / `--json` / `--cleanup` / `path` / `stats`
6. **`.env.example` + `models/vllm/qwen3.8.yaml`（示范）+ README 增补 + `.gitignore`**

## 10. 参考资料

- vLLM per-request metrics 设计讨论：[vllm-project/vllm#40076](https://github.com/vllm-project/vllm/issues/40076)（含字段定义、flag 语义、流式末块约定）
- llama-swap 集成 vLLM metrics 实际实现（社区范例）：[mostlygeek/llama-swap#906](https://github.com/mostlygeek/llama-swap/issues/906)（佐证 `--enable-per-request-metrics` + `--enable-force-include-usage` 组合用法）
- DeepSeek 分享讨论（用户最初引用）：`https://chat.deepseek.com/share/kum48og8cjbjppvmmj`
- vLLM 官方 metrics 端点文档（现有 stats 依赖，不重复）：`https://docs.vllm.ai/en/latest/design/metrics/`
