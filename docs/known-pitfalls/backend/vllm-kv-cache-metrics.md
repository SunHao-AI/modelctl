# vLLM KV cache metrics 与混合注意力行为

> 2026-09-04：沉淀 qwen3.8-vllm（Qwen3.8-27B-FP8，TP8，FP8 KV）一次"GPU KV cache usage 长期 0~1.2%"的排查与 `--enable-prefix-caching` 单变量实验验证结论。

## `GPU KV cache usage` 长期 0~1.2% 是正常，不是故障

**日期**：2026-09-04
**症状**：`modelctl start qwen3.8-vllm` 后，APIServer metrics 行长期打印 `GPU KV cache usage: 0.0% ~ 1.2%`，直觉上误以为池子"几乎空着"是配置错误或泄漏。

**根因**：`GPU KV cache usage` 的**分母**是按 `max_model_len × 池容量并发上限` 满刻度预留，不是按实际请求上下文。

以 qwen3.8（vLLM 0.27.1 + TP8 + FP8 权重 + FP8 KV）为例：

- 启动日志关键三行：
  - `GPU KV cache size: 4,684,667 tokens`
  - `Maximum concurrency for 262,144 tokens per request: 17.87x`
  - `attention block size = 800 tokens`（非默认 16/32，被引擎主动抬高，见下条）
- 池容量 4.68M token ÷ 单条上下文上限 262k = **17.87**——即"满刻度"能同时塞 17.87 条 262k 长序列。
- `--max-num-seqs 8` 是调度上限，实际永远不会到 17.87 条。
- 换算：usage 天花板 = `max_num_seqs ÷ 17.87`：
  - `max-num-seqs=8` → 天花板 **44.8%**
  - `max-num-seqs=16` → 天花板 **89.5%**
- 按**实际观察**的 agent 单请求 ~56k 上下文进一步收敛：天花板 `8×56k/4.68M ≈ 9.6%`，所以长期 0~1.2% 完全合理，且 `Waiting: 0` 无抢占、MTP accept 长度 3.5~4，运行健康。

**换算口径**：`1% usage = 池容量 tokens ÷ 100`（上面例子里就是 4,685 tokens），可据此把 metrics 行百分比直接换算为驻留 token 数。

**不要为此调 `gpu_memory_utilization`**：
vLLM ≥0.21 默认启用 CUDA graph memory profiling，0.95 实际等效旧版 0.9417，日志会提示"加到 0.9583 可回补"——那只补回 CUDA graph 扣掉的 0.0083 等效值（≈1% 池容量），在池子 90% 闲置时加上去纯属浪费。真正瓶颈在负载形态（每轮只发一条 56k 上下文），不在池子大小。

**教训**：

- 看 `GPU KV cache usage` 前，先确认三个数：**`GPU KV cache size`**、**`Maximum concurrency`**、**`--max-num-seqs`**，否则没有讨论"高/低"的基础。
- metrics 行的百分比和实际"有多少 KV 显存被占"没关系，它衡量的是"满载序列数÷满刻度序列数"。
- 想为"长上下文突发"留余量的是 `max_model_len` + `gpu_memory_utilization`，想为"平均并发吞吐"调 `--max-num-seqs`，**没有第三个旋钮**该动。
- 完整诊断见 [qwen3.8.yaml](file:///d:/WorkPlace/Pycharm/modelctl/models/vllm/qwen3.8.yaml) 头部【KV cache 容量口径】注释块。

## 混合注意力模型：attention block size 被引擎抬到 800 token

**日期**：2026-09-04
**症状**：Qwen3.8 是**混合注意力**架构（64 层 = 16 层全注意力 + 48 层线性注意力 GDN），启动日志出现：

```
Setting attention block size to 800 tokens to ensure that attention page size is >= mamba page size.
Padding mamba page size by 0.25% to ensure that mamba page size and attention page size are exactly equal.
```

**根因**：vLLM 对混合架构统一做 **HybridPage**——每个 KV block 同时容纳

- 全注意力层的 KV（K/V × kv_heads × head_dim）
- 线性注意力层（GDN/Mamba）的**循环状态快照**

两层页面必须严格等长才能做 `paged` 统一调度；当 mamba page 尺寸 > attention page 尺寸时，引擎会**抬高 attention block size** 来对齐（qwen3.8 上即 16/32 → 800，再把 mamba page padding 0.25% 使两者严格相等）。

**直观后果**：

- `GPU KV cache size` 的 token 数被按 800 的 block 折算，**看起来"变少"**——但 4.68M 其实已经扣掉了 mamba 状态页的份额（1 页真 KV + 1 页等额 GDN 状态）。
- `--enable-prefix-caching` 打开后，**缓存命中粒度 = 800 token**，前缀必须连续相同满一个 block 才计一次命中。这是"命中率爬升需要几个请求的积累"的根本原因。
- `Add N padding layers, may waste at most X% KV cache memory` 是**上界警告**：混合注意力 64 层 ÷ 8 = 8 个 hybrid 单元，`Add 3 padding layers` 意味着上界 6.25%；实测 qwen3.8 上 KV 池只掉了 **0.87%**（4,684,667 → 4,643,693）。

**教训**：

- 看"KV 池 token 数"之前，先确认架构是否混合注意力。纯注意力模型上 4,684,667 和混合架构的 4,643,693 完全不是一个口径（后者每 token 里还含 GDN 状态）。
- 800 token block 是**引擎**选的，不是 yaml 能配的；想改 block size 只能等 vLLM 版本升级或换架构。
- 完整对照见 [qwen3.8.yaml](file:///d:/WorkPlace/Pycharm/modelctl/models/vllm/qwen3.8.yaml) 的注释块。

## vLLM 0.27.1 对混合注意力架构**静默**把 `enable_prefix_caching` 默认值改成 False

**日期**：2026-09-04
**症状**：`extra_args` 完全没传 prefix caching 相关参数，但 `Prefix cache hit rate` 恒 0.0%，agent 逐轮重发递增上下文的负载下 prompt throughput 高达 5,096 tokens/s（每轮都在全量 prefill 相同前缀）。排查时发现：

- 启动日志 api_utils.py 打印的 `non-default args` 里**没有** `enable_prefix_caching` 键（我们只传了 18 个参数，该键不在其中）；
- 但 EngineCore config dump 里 `enable_prefix_caching=False` 明确出现。

**根因**：**vLLM 在 config 后处理阶段**检测到混合注意力架构（`Qwen3_5ForConditionalGeneration`），把 `enable_prefix_caching` 的运行时默认值从 `True` 改成了 `False`，且**不产生任何 warning、不打印到 non-default args**。这是 0.27.1 的静默默认值改写。

**解决方案**：显式在 `extra_args` 追加 `--enable-prefix-caching`（适配器的 `extra_args` 经 `shlex.split` 后**恒定追加到命令末尾**，会覆盖引擎默认）。

**验证**：`enable_prefix_caching=True` 出现在 non-default args 中，EngineCore config dump 里同步为 `True`。

**教训**：

- "non-default args 里没这个键"≠"用的是默认值"。默认值可能被 config 后处理改写；**唯一可靠判据是 EngineCore config dump**。
- 反向教训：改参数后，**同时** grep 两处确认——`non-default args`（证明 CLI 参数吃进去）和 `config dump`（证明引擎最终生效值）。
- 若 vLLM 版本之间的静默默认值发生漂移，`Prefix cache hit rate` 会**再次**长期 0.0%；此时先 grep config dump，再决定要不要显式加回 `--enable-prefix-caching`。

## Mamba/GDN cache mode 自动切到 `align`（experimental）的代价与收益

**日期**：2026-09-04
**症状**（打开 `--enable-prefix-caching` 后）：启动日志出现：

```
Mamba cache mode is set to 'align' for Qwen3_5ForConditionalGeneration by default when prefix caching is enabled
Warning: Prefix caching in Mamba cache 'align' mode is currently enabled. Its support for Mamba layers is experimental.
```

且实际指标：

- KV 池 4,684,667 → **4,643,693** tokens（-0.87%）
- `GPU KV cache usage` 峰值 1.2% → **4.2%**（多请求时 GDN 状态页按块驻留，上限变低）
- 但 `Prefix cache hit rate` 0.0% → **12% → 27% → 42% → 58% → 70~73%** 单调爬升
- `prompt throughput` 峰值 5,096 → **163~1,733** tokens/s

**根因**：混合同 block 里 GDN 循环状态是**有状态**的（不是每 token 独立 KV），默认 cache mode 不做状态快照，前缀缓存只能命中全注意力层（16/64）；`--enable-prefix-caching` 打开后，引擎自动切到 `align` mode——按 800-token 块对齐做**状态快照**，让 GDN 状态也进入缓存。官方标注 experimental 是名不副实的**当前唯一通路**，不开 `align` 基本等于没开。

**收益**（qwen3.8 实测）：

- 命中率单调上升，agent 逐轮重发递增上下文的负载下，后续请求主要 prefill 未命中增量。
- prompt throughput 结构性下降（不是变差，是"省事了"）：5,096 → 163~1,733 tokens/s，TTFT 相应改善。
- MTP 接受率 **没有下降**（实验前后同秒数的 `Per-position acceptance rate` 一致）。

**代价**（qwen3.8 实测，均属可接受）：

- KV 池 **-0.87%**（不是 `Add 3 padding layers` 警告到的 6.25% 上界，那个是"上界"）。
- `usage` 峰值 **1.2% → 4.2%**：多请求并发时 GDN 状态页按块驻留，KPI 上限变低；但在池子 90% 闲置下无实际影响。
- GC 行为：首次命中 GDN 块可能要带状态重算；多轮 agent 会话下命中率仍持续上升，总体收益远大于此开销。
- 启动时间多了 Mamba 'align' 模式的 4 个 Triton JIT 编译（precopy_mamba_align_fused_kernel / postprocess_mamba_fused_kernel 等），一次性 ~5 分钟。

**回退条件**：若 `Prefix cache hit rate` 长期 **< 10%**，说明 vLLM 升级后 Mamba 对齐路径变了或有回归，从 `extra_args` 删掉 ` --enable-prefix-caching` 并 `stop + start` 即可；关掉不会反噬，收益消失而已。

**教训**：

- 混合注意力模型上"前缀缓存命中"的语义 ≠ 纯注意力模型——命中意味着**全 64 层**（含 48 层 GDN）的 KV+状态快照都能复用，因此 qwen3.8 上 70~73% 命中率的收益远大于纯注意力模型同样命中率下的收益。
- 看到 `Warning: ... experimental ...` **不必回退**，先验证命中率再决策；vLLM 0.27.1 + Qwen3.5/Qwen3_5 家族上此路径稳定。
- 想主动关闭 align 需要 `--mamba-cache-mode=...`（当前版本下），但这等于放弃 prefix caching 绝大部分收益，几乎不存在"值得关"的场景。
