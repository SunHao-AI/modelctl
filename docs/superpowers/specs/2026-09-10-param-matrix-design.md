# 参数级能力矩阵（环境 × 模型 × 引擎 → 参数可用性）设计

- 日期：2026-09-10
- 状态：设计已确认（用户逐节评审通过，采用方案 B + 规则可序列化 + 三层混合来源 + 本机优先预留集群）
- 关联：`core/compat.py`（不复用其执行链，仅复用 Spec 与工具）、`core/capabilities.py`、`core/vram_estimator.py`、`core/colors.py`、`cli.py`、`core/all_service.py`、`models/vllm/qwen3.8-flash-next.yaml`
- 上游参照：`specs/2026-08-19-compat-check-design.md`（引擎级能力检测，本文是其**参数级**延伸）、`docs/known-pitfalls/backend/vllm-kv-offload-connector-support.md`、`docs/known-pitfalls/backend/vllm-kv-cache-metrics.md`

## 1. 背景与目标

### 问题

`recipes.vllm.ai/Qwen/Qwen3.8-Flash-Next` 页面提供 `KV Offload → Mooncake (Distributed)` 选项，URL 形如 `?kv_offload=kv_store_distributed_mooncake`，看起来是"显存不够用内存补 KV"的现成方案。实际在本仓硬件（8×RTX 5880 Ada，CC 8.9，128GB 内存，PCIe 4.0 无 NVLink 无 RDMA）+ 本模型（36 层 GDN + 12 层 QSA 混合注意力）上完全不可用，且**报错点离真实原因很远**（connector 取首层 backend → 拿到 `GDNAttentionBackend` → 对 `FullAttentionSpec` 组调 `get_kv_cache_shape()` 崩）。

同类坑在本仓已反复付出代价，全部只能靠人工踩完再写注释：

| 坑 | 现有拦截能力 |
|---|---|
| `quantization: fp8` 在 A6000（CC 8.6）上不可用 | **有**（`compat_rules.fp8_quant_cc`，block） |
| plain TP8 与 FP8 128 宽量化块不兼容，必须 TEP8 | 无（只在 yaml 注释里） |
| `kv_cache_dtype: fp8` 被 QSA 硬拒 | 无（只在 yaml 注释里） |
| `kv_transfer_config` / Mooncake 在混合注意力上全线不可用 | 无（本次新踩） |
| `VLLM_PLE_CPU_OFFLOAD` 在 ≤80GB 卡上是必须项 | 无 |
| `max_model_len: 262144` 超出可承载 KV（差 ~1.0 GiB/卡） | 部分（`vram_estimator` 正向估算，仅 warning） |
| `enable_prefix_caching` 在混合模型走 align mode（experimental，有代价） | 无 |

**根因**：现有 `core/compat.py` 规则引擎粒度停在**"引擎能否启动该模型"**，9 条规则没有一条落在"某个 `engine_config` 参数是否被支持"的粒度上；且只有 block/degrade 两级，无法表达"有条件支持（附条件说明）"。

### 目标

1. 建立**参数级**判定内核：给定（环境事实, 模型事实, 引擎, 参数集），逐项输出四态结论 + 理由 + 条件 + 依据。
2. 四个消费端复用同一内核：写 yaml 前 CLI 查询、WebUI 矩阵视图、启动 preflight 告警/拦截、反向推荐参数。
3. 采集目前完全缺失的环境事实：**宿主 RAM、RDMA 网卡、GPU 互联拓扑**；扩充模型架构表使未下载模型也可判定。
4. 三层来源（存在性/条件性/容量）**置信度显式分离**，用户能分辨"事实"与"估算"。

### 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 内核形态 | **方案 B：新建平行内核 `core/param_matrix.py`**，`compat.py` 一行不动 |
| 规则载体 | **Python dataclass 声明**，但保持纯数据形状可 `asdict()` 序列化（沿用 2026-08-19 spec 的"非 yaml 声明式"决策） |
| 判定知识来源 | **三层混合**：存在性（引擎 `--help`）+ 条件性（人工规则）+ 容量（显存估算反解） |
| 判定粒度 | 四态：`supported` / `conditional` / `unsupported` / `unknown` |
| 环境范围 | **本机优先，但 Facts 做成可序列化结构**，集群透传留到 P4 不改内核 |
| 新增事实采集 | 宿主 RAM（必需）、RDMA 网卡、GPU 互联拓扑、内置模型架构表 |
| 消费端分期 | P0 内核+CLI → P1 WebUI → P2 存在性+preflight → P3 容量反解+推荐 → P4 集群 |
| preflight 阻塞条件 | **收紧到四条同时成立**（见 §6），容量层与条件层永不 block |

### 非目标

- 不做全量引擎参数目录。只覆盖本仓 profile 实际用到的参数字段。
- 不引入 `psutil`（RAM/RDMA/拓扑用平台原生手段）。
- 不做参数自动改写/自动修复。只判定、只建议，改 yaml 是人的事。
- 不实现 CLAUDE.md 提及但 `src/` 中不存在的 `align_row` / `LogCols`（见 §10）。

## 2. 架构与数据流

```
Facts（事实快照，全可序列化）
  ├─ GpuSpec       复用 compat.GpuSpec.from_caps(capabilities)
  ├─ HostSpec      ★新建 core/host_facts.py
  ├─ ModelFacts    复用 compat.ModelSpec + ★扩层类型
  └─ EngineFacts   ★新建（P2 才有 params_available；P0 仅 version）
                          │
                          ▼
        param_matrix.evaluate(engine, facts, requested) -> Matrix
                          │
        ┌─────────────┬──────────────┬───────────────┬──────────────┐
     CLI          WebUI          preflight        反向推荐
  modelctl     GET /admin/api   薄消费(P2)       (P3)
   compat         /compat
```

`requested` 即 yaml 的 engine 段 dict。同一内核三种用法：

- 给全量参数目录（`requested=None`）→ "这台机器能用什么"
- 给 `requested` → "我写的这些对不对"
- 给空 + 容量求解 → "推荐什么"（P3）

## 3. 核心数据结构

全部 `frozen dataclass`、无 IO、纯函数，`dataclasses.asdict()` 可直接 JSON 往返。

```python
State = Literal["supported", "conditional", "unsupported", "unknown"]
Layer = Literal["existence", "condition", "capacity"]
Confidence = Literal["fact", "inferred", "estimated"]


@dataclass(frozen=True)
class Verdict:
    state: State
    reason: str
    condition: str = ""        # state=conditional 时必填
    evidence: str = ""         # 上游 issue 号 / 官方文档 URL / 本仓实测记录路径
    suggestion: str = ""       # 可执行动作
    limit_mb: int | None = None  # capacity 层的量化结论


@dataclass(frozen=True)
class ParamRule:
    id: str                    # 唯一，如 "kv_connector_needs_pure_attention"
    param: str                 # "kv_cache_dtype"；extra_args 内参数用
                               # "extra_args:--kv-transfer-config"
    engines: tuple[str, ...]   # 空元组 = 全引擎
    layer: Layer
    confidence: Confidence
    check: Callable[["Facts"], Verdict | None]   # 返回 None = 本规则不适用


@dataclass(frozen=True)
class ParamFinding:
    param: str
    rule_id: str
    state: State
    layer: Layer
    confidence: Confidence
    reason: str
    condition: str
    evidence: str
    suggestion: str
    limit_mb: int | None = None
    requested_value: str = ""  # yaml 里显式写了该参数时回填，便于对照
    explicit: bool = False     # 是否由用户在 yaml 显式写出（preflight 阻塞前置条件）


@dataclass(frozen=True)
class Matrix:
    engine: str
    findings: tuple[ParamFinding, ...]   # 排序：unsupported → conditional → unknown → supported
    facts_digest: dict[str, str]         # 人类可读环境指纹，供 UI/日志复现结论
```

### 3.1 四态语义与 `unknown` 红线

沿用 `compat._cmp_versions` 的"无法解析视为匹配（不误报哲学）"，反向同样成立：

> **证据缺失一律 `unknown`，绝不报成 `unsupported`。**

这条是红线。若把"采集不到 RAM"报成"Mooncake 不支持"，工具就从"避免踩坑"退化成"制造误报"，比没有工具更糟——用户会开始忽略整个矩阵。四态判据：

| state | 成立条件 |
|---|---|
| `supported` | 所需事实**全部采集成功**且条件谓词全部为真 |
| `conditional` | 谓词含未定变量，但能给出人类可判的条件文本（如"需宿主空闲 ≥51GB"） |
| `unsupported` | 所需事实全部采集成功且条件谓词确定为假 |
| `unknown` | 任一所需事实缺失 / `check()` 抛异常 / 规则自判不适用 |

### 3.2 置信度必须对外可见

| layer | confidence | 含义 | CLI 呈现 |
|---|---|---|---|
| existence | fact | 引擎 `--help` 里有/没有这个 flag | 直接显示 |
| condition | inferred | 人工规则推理（CC、层类型、RAM） | 直接显示 |
| capacity | estimated | 显存公式估算 | `≈` 前缀 + `[estimated]` |

三层混显成同一视觉权重会诱导用户把估算当事实，故置信度是**对外字段**而非内部元数据。

## 4. Facts 采集

### 4.1 `core/host_facts.py`（★新建）

```python
@dataclass(frozen=True)
class HostSpec:
    ram_total_mb: int | None = None
    ram_available_mb: int | None = None
    rdma_devices: tuple[str, ...] | None = None   # None=未探测；()=确认无 RDMA
    nvlink: bool | None = None                    # GPU 互联是否含 NVLink
```

`None` 与空值严格区分：`rdma_devices=None` 是"没探"，`() ` 是"探了确认没有"。前者让规则走 `unknown`，后者可走 `unsupported`。

| 事实 | Windows | Linux | 失败行为 |
|---|---|---|---|
| RAM | `ctypes` `GlobalMemoryStatusEx` | 读 `/proc/meminfo` 的 `MemTotal`/`MemAvailable` | 字段置 `None` |
| RDMA | 列 `ibv_devices` 是否存在（通常无） | 列 `/sys/class/infiniband/*` 目录名 | 置 `None` |
| 拓扑 | `nvidia-smi topo -m` | 同 | 置 `None` |

- **`nvidia-smi` 复用 `capabilities._safe_smi()`**（吞 `OSError`/`SubprocessError` 归约空串），不新写一份。
- 全部探测**不引入 psutil**，与现有依赖面一致。
- `Capabilities` 增补对应字段并在 `probe()` 内填充，使集群心跳能顺带带上（P4 用）。

### 4.2 `ModelFacts` 扩层类型

在 `compat.ModelSpec` 之上补一个判定视图（不修改 ModelSpec 本身）：

```python
is_hybrid_attn: bool | None    # 混合注意力（GDN/QSA/Mamba）
n_layers / kv_heads / head_dim # 供容量层
```

`is_hybrid_attn` 判据（按可得性优先级，任一命中即定）：

1. 本地 `config.json` 的 `layer_types` 含 `linear_attention`，或 `architectures` 命中已知混合类；
2. 启动日志出现 `Mamba cache mode is set to` / `Hybrid KV cache manager`；
3. 内置架构表命中；
4. 以上皆无 → `None`（**不判为 False**）。

这个谓词单条就同时判掉三类参数，是本期性价比最高的一个事实。

### 4.3 `vram_estimator` 扩充

- `KNOWN_MODEL_ARCHS` 从 1 条扩到覆盖本仓 `models/vllm/*.yaml` 全部 10 个 profile 对应的模型（离线表：modelscope_id → 层数 / kv_heads / head_dim / 是否混合注意力），使**未下载模型**也能判定。
- 新增反解：给定权重占用、`gpu_memory_utilization`、`max_num_seqs`、dtype，求**最大可行 `max_model_len`**。沿用现有公式 `ctx × n_layers × kv_heads × head_dim × 2 × dtype_bytes`，改为在单卡上限内二分/闭式求 ctx 上界。

## 5. 首批规则清单

全部来自本仓**已付出代价的真实坑**，无一条虚构需求。

| rule_id | param | layer | 判定 |
|---|---|---|---|
| `fp8_quant_cc` | `quantization` | condition | `cc_at_least(cc, 8, 9)`；**判定口径引用 `compat_rules` 同一谓词函数**，但注册在 param_rules 的独立注册表（两个注册表互不相干，`register_rule()` 的重复 id 检查各自独立） |
| `fp4_quant_blackwell` | `quantization` | condition | CC major ∈ {10,12} |
| `qsa_kv_cache_dtype_fp8` | `kv_cache_dtype` | condition | `is_hybrid_attn` 且含 QSA → `unsupported`（QSA 硬校验需 BF16 主 KV） |
| `kv_connector_needs_pure_attention` | `kv_transfer_config`、`extra_args:--kv-transfer-config` | condition | `is_hybrid_attn=True` → `unsupported`，evidence 引 vllm#43765 / #41860 |
| `mooncake_segment_fits_ram` | `kv_transfer_config`(Mooncake) | condition | `global_segment_size × 卡数 ≤ ram_available_mb` → 否则 `unsupported`；RAM 缺失 → `unknown` |
| `mooncake_protocol_rdma` | 同上 | condition | `rdma_devices` 空 → `conditional`（"仅 tcp，收益可能为负"） |
| `ple_cpu_offload_recommended` | `docker_env.VLLM_PLE_CPU_OFFLOAD` | condition | 单卡显存 ≤ 80GB 且模型含 N-gram/PLE → 建议 `supported` 且提示"应开启" |
| `ple_offload_needs_host_ram` | 同上 | condition | `ram_available_mb ≥ 51GB + 余量`，否则 `conditional` |
| `fp8_tp_block_divisibility` | `tensor_parallel_size` | condition | FP8 `weight_block_size=[128,128]` 下 plain TP8 出 80 宽 → `conditional`（"需 `--enable-expert-parallel --moe-backend triton`"） |
| `prefix_caching_hybrid_align` | `extra_args:--enable-prefix-caching` | condition | `is_hybrid_attn` → `conditional`，说明 align mode experimental + 实测 70~73% 命中 + KV 池 -0.87% 代价 |
| `pp_unsupported_with_ple` | `extra_args:--pipeline-parallel-size` | condition | 模型含 N-gram Embedding → `unsupported`（PLE 不支持 PP） |
| `ngram_offload_gpu_only` | N-gram offload 类 | condition | 非 NVIDIA → `unsupported` |
| `gpu_mem_util_shared_caution` | `gpu_memory_utilization` | condition | > 0.95 → `conditional`（共享卡风险） |
| `spec_decode_mtp_memory` | `extra_args:--speculative-config` | capacity | MTP 头 + draft KV 额外占显存，与 `max_model_len` 争预算 → `conditional` |
| `max_model_len_capacity` | `max_model_len` | capacity | 反解上界；超出 → `conditional` + `limit_mb` + 建议值 |

规则模块 `core/param_rules.py`（与内核 `param_matrix.py` 分离，保持"内核=机制、规则=知识"边界，规则文件可独立增长而不碰内核）。

`register_rule()` / 重复 id 抛错 / 按引擎过滤 / 排序输出，**照 `compat.py` 的既有形状实现**，不发明新机制。

## 6. 消费端

### 6.1 P0 · CLI `modelctl compat`

模板抄 `cli._cmd_probe`（四区块 + `pad_width` 对齐），CJK 一律 `core/colors.py` 的 `display_width`/`pad_width`，**禁止** `f"{x:<N}"`。

```
参数支持矩阵 —— qwen3.8-flash-next / vllm
  (8×RTX 5880 Ada · CC 8.9 · RAM 空闲 78.3/128GB · RDMA 未探测 · 无 NVLink)

  kv_cache_dtype: fp8        ✗ 不支持    QSA 12 层硬校验需 BF16 主 KV          [inferred]
  kv_transfer_config         ✗ 不支持    混合注意力，connector 取错 backend     [inferred]  vllm#43765
  docker_env.PLE_CPU_OFFLOAD ✓ 支持      ≤80GB 卡上为必须项，建议开启           [inferred]
  --enable-prefix-caching    ⚠ 有条件    走 Mamba align（experimental），        [inferred]
                                         实测命中 70~73%，代价 KV 池 -0.87%
  max_model_len: 262144      ≈ 建议 131072  超出可承载约 1.0 GiB/卡            [estimated]
  quantization: fp8          ✓ 支持      CC 8.9 ≥ 8.9                          [fact]
  mooncake:global_segment    ? 未知      宿主 RDMA 信息未探测                   [unknown]
```

四色互异（绿/黄/红/灰），列宽取**整块所有键 `display_width` 最大值**统一补齐。

参数形式：`modelctl compat <profile>`（读 yaml 的 engine 段作 `requested`）；`modelctl compat --engine vllm --model <id>`（无 profile 时纯目录查询）。

### 6.2 P1 · WebUI

新增 `core/webui/admin_compat.py`，走 `admin_router._SUBROUTER_MODULES` 注册表挂 `_router()`。

- `GET /admin/api/compat?engine=&model=&profile=` → `Matrix` 的 JSON 形式。
- **绝不挂进 `/overview`**（3s 轮询）。矩阵必须是显式按需端点。
- 前端照 `EnvsView.vue` 的 `docker_bypass` 区块范式渲染四态徽标（现有唯一的能力矩阵 UI）。

### 6.3 P2 · preflight 薄消费

唯一有风险的消费端，收紧到四条同时成立才 block：

```
layer == "existence" && confidence == "fact"
  && state == "unsupported"
  && finding.explicit           # 用户在 yaml 里显式写了该参数
```

其余一律走 `logger.warning`（沿用 `all_service` 现有 `adapter.warnings` 循环）。**容量层与条件层永不 block。**

配套硬约束：**preflight 只读缓存，永不触发探测子进程**。preflight 已在五阶段机第一段，塞 `docker run --rm` 进去等于把启动时间挂在十几秒 IO 上。无缓存 → 存在性层整体 `unknown` → 不阻塞。

### 6.4 P2 · 存在性层

`docker run --rm <image> vllm serve --help` / venv 同理，正则提取 `--xxx` 全集。

- 缓存：`lru_cache` + 按（镜像 tag / venv 版本）落盘到 `CACHE_DIR`；命中才给 `fact`，否则 `unknown`。
- 只在**显式 CLI/UI 查询**时跑，永不进 preflight。
- 诚实定位：参数名写错时 vLLM 自己会报 `unrecognized arguments` 并 exit 2 —— 引擎已免费承担这层。这层的增量价值主要是"这个 flag 在你这个版本叫不叫这个名字"，故 P2 而非 P0。

### 6.5 P3 · 反向推荐

在 Facts + 规则之上加容量求解，输出"本机该模型该引擎的建议参数组合"。依赖 §4.3 的反解能力。不自动写 yaml。

### 6.6 P4 · 集群

`Matrix.facts_digest` 与 `Facts` 已可序列化 → worker 心跳附带能力指纹，中心下发前 gate 坏参数。**不改内核**，只搬运。

## 7. 错误处理

| 情况 | 行为 |
|---|---|
| `nvidia-smi` 失败 | `GpuSpec` 空 → 依赖它的规则全 `unknown` |
| RAM 采集失败（平台未覆盖） | RAM 类规则 `unknown`，**不是 `unsupported`** |
| 模型未下载且不在内置架构表 | `is_hybrid_attn=None` → connector 类规则 `unknown`（不拦） |
| `--help` 探测失败 / 无缓存 | 存在性层整体 `unknown` |
| 单条规则 `check()` 抛任意异常 | 该参数 `unknown` + `logger.warning(rule_id, exc)`，**绝不冒泡打断整张矩阵** |
| `Capabilities.probe()` 超时 | 沿用现有 30s 超时语义，Facts 相应字段置 `None` |

最后一条是对本仓已知"一坏俱坏"模式（cluster 候选读取裸异常）的正面规避：一条规则的异常不能毁掉整张矩阵。

`Capabilities.probe()` 每次调用都跑 `nvidia-smi`，无缓存。新模块在 WebUI 场景自行加 TTL 缓存，不改 `probe()` 的现有语义。

## 8. 测试策略

规则是 `Facts → Verdict` 纯函数，无 IO，表驱动。

### 8.1 每条规则三个用例（缺一不可）

1. 条件不满足且证据齐备 → `unsupported`
2. 条件满足 → `supported`
3. 证据缺失 → `unknown`

**第 1 与第 3 条不可合并**。一条恒返回 `unknown` 的退化规则能让"只测不支持"的用例集全绿；反之若把边界写反（缺失时报 `unsupported`），没有独立用例变异就不转红。这正是本仓 known-pitfalls 记录的"恒真断言 / 变异验证假阴性"一类。

### 8.2 不变量测试（比单条规则更有价值）

- `test_never_blocks_without_explicit_param`：yaml 未写该参数 → 无论规则判什么，preflight 不 block。
- `test_capacity_layer_never_blocks`：容量层任意结论 → 不进 block 列表。
- `test_rule_serializable`：每条规则 `asdict()` JSON 往返，保证 P4 下发不会突然爆炸。
- `test_duplicate_rule_id_rejected`：与 `compat.register_rule()` 同口径。
- `test_check_exception_yields_unknown`：让某规则 `check` 抛 `RuntimeError` → 该参数 `unknown`，矩阵其余项正常产出。
- `test_unknown_and_unsupported_are_distinct`：同一规则在"事实齐备不满足"与"事实缺失"两种输入下必须返回不同 state。

### 8.3 CLI 对齐测试

数据必须含双宽字符（中文参数说明），断言**各列起始位置一致**而非仅"包含某字符串"——沿用 `profile-config-drift.md` 里 CJK 对齐用例的既有做法。

## 9. 分期与改动面

| 期 | 交付 | 风险 |
|---|---|---|
| **P0** | `param_matrix.py` 内核 + `host_facts.py`(RAM/RDMA/拓扑) + `param_rules.py` 首批规则 + `ModelFacts` + 架构表扩充 + CLI `compat` | 纯函数 + 只读，零启动链风险 |
| **P1** | `admin_compat.py` + 前端矩阵视图 | 新端点，不碰现有链路 |
| **P2** | 存在性层（`--help` + 缓存）+ preflight 薄消费 | **唯一有启动风险**，靠 §6.3 四条收紧 + 只读缓存兜住 |
| **P3** | 容量反解 + 反向推荐 | 依赖 P0 Facts |
| **P4** | 集群透传（心跳带 Facts、中心 gate） | 不改内核 |

P0 只上条件层就已覆盖本仓全部已知坑——条件层才是价值主体，存在性与容量是补充。

**新建**：`core/param_matrix.py`（机制）、`core/param_rules.py`（知识）、`core/host_facts.py`、`tests/test_param_matrix.py`、`tests/test_host_facts.py`
**改动**：`core/capabilities.py`（+RAM/RDMA/拓扑字段）、`core/vram_estimator.py`（架构表 + 反解）、`core/all_service.py`（P2 preflight 消费）、`cli.py`（`compat` 子命令）、`core/webui/admin_compat.py` + 注册表、`web/src/views/CompatView.vue` + api/types
**不动**：`core/compat.py`、`core/compat_rules.py`、各 `engines/*.py` 的 `build_command`

## 10. 附带发现（不在本期范围，仅记录）

CLAUDE.md 的 CJK 规范提到 `align_row` / `LogCols`，但 `src/` 中**并不存在**这两个符号（全仓仅命中 `CLAUDE.md` 与 `docs/CJK-Terminal-Alignment.md`）；`core/logging.py` 仍是 `{level:<7}` 旧格式；`cli.py` 另有一份本地 `_display_width`/`_ljust_width` 重复实现（CJK 区间较窄）。本模块统一走 `core/colors.py` 的 `display_width`/`pad_width`，不在本次范围内补齐规范与实现的偏差。
