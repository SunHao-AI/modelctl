# RTX 5880 Ada 8 卡服务器模型配置优化指南

本指南针对 8×NVIDIA RTX 5880 Ada Generation（48GB 显存/卡，共 384GB，PCIe 4.0 x16，无 NVLink，128GB 系统内存）服务器，说明 modelctl 项目优化后的配置选择、上下文变体使用方式及注意事项。

## 一、硬件规格与约束

| 项目 | 规格 |
|------|------|
| GPU | 8 × NVIDIA RTX 5880 Ada Generation |
| 单卡显存 | 48 GB GDDR6（ECC） |
| 总显存 | 384 GB |
| 显存带宽 | 960 GB/s × 8 |
| 互联 | PCIe 4.0 x16，无 NVLink |
| 系统内存 | 128 GB |
| 计算能力 | 8.9（支持 FP8） |
| 单卡功耗 | 285 W |

### 关键约束

1. **无 NVLink**：张量并行（TP）跨卡通信走 PCIe，带宽远低于 NVLink。大模型 TP8 会成为吞吐瓶颈，但大模型权重大于单卡显存，必须多卡分片。
2. **系统内存 128GB**：不足以 offload 大模型权重，必须完全装载到 GPU 显存。
3. **FP8 支持**：RTX 5880 Ada 计算能力 8.9，原生支持 FP8 权重和 FP8 KV cache，可在精度损失 <1% 的前提下节省约 50% KV 显存。

## 二、优化策略概览

本次优化围绕以下目标：

1. **消除 OOM 风险**：修复原配置中上下文长度过大、并发度过高、单卡无法装载等高危配置。
2. **启用 FP8 KV**：为 vLLM 和 SGLang 后端显式启用 FP8 KV cache，降低显存占用。
3. **多上下文变体**：为同一模型创建 high / balanced / light 三种上下文配置，适应不同 agent 场景。
4. **合理利用多卡**：通过 `gpu_count` / `tensor_parallel_size` / `gpu_list` 控制 GPU 分配，支持单节点多模型隔离。

## 三、上下文变体说明

三种变体分别对应不同的使用场景：

| 变体 | 上下文长度 | 适用场景 | 显存压力 | 推荐模型 |
|------|-----------|---------|---------|---------|
| **high** | 65K | 长文档分析、RAG、多轮长对话 | 高 | deepseek-v4-flash-vllm-high |
| **balanced** | 原配置（DeepSeek 1M/槽、Qwen3.8 262K/槽） | 通用 agent、代码生成、日常推理 | 中 | deepseek-v4-flash-llamacpp / qwen3.8-llamacpp |
| **light** | 16K | 低延迟 API、轻量 agent、高并发短输入 | 低 | deepseek-v4-flash-vllm-light |

### 选择建议

- **agent 需要处理长文档或大量历史上下文**：选择 `high` 变体。
- **通用对话、代码补全、工具调用**：选择 `balanced` 变体。
- **API 服务要求高吞吐、低延迟、输入较短**：选择 `light` 变体。

## 四、各模型启动命令

### DeepSeek-V4-Flash

```bash
# 高性能上下文（65K，8 卡 TP）
modelctl start deepseek-v4-flash-vllm-high      # 端口 8103

# 平衡上下文（32K/131K，8 卡 TP，推荐默认）
modelctl start deepseek-v4-flash-vllm             # 端口 8100

# 轻量上下文（16K，4 卡 TP，可与其他模型共享节点）
modelctl start deepseek-v4-flash-vllm-light      # 端口 8104

# 管线并行实验（TP4 + PP2，无 NVLink 下与 TP8 对比吞吐，附录 B.2）
modelctl start deepseek-v4-flash-vllm-pp         # 端口 8106

# llama.cpp 版本（GGUF，带 DSpark 投机解码）
modelctl start deepseek-v4-flash-llamacpp        # 默认上下文（1M/槽 × 2 并发，8 卡），端口 18888
modelctl start deepseek-v4-flash-llamacpp-high  # 65K 高上下文，端口 18892
modelctl start deepseek-v4-flash-llamacpp-light  # 16K 轻量，端口 18893
```

### Qwen3.8-27B

```bash
# vLLM 版本（FP8 KV，支持视觉）
modelctl start qwen3.8-vllm             # 默认上下文（262K），端口 8101
modelctl start qwen3.8-vllm-light       # 16K 轻量，gpu 4-7，端口 8105

# llama.cpp 版本（Q4_K_M GGUF，支持视觉）
modelctl start qwen3.8-llamacpp        # 默认上下文（262K/槽 × 4 并发，8 卡），端口 18889
modelctl start qwen3.8-llamacpp-high   # 65K 高上下文，端口 18894
modelctl start qwen3.8-llamacpp-light  # 16K 轻量，端口 18895
```

### Kimi-K2.5 / Qwen3-Coder

原配置保持可用，vLLM/SGLang 版本已启用 FP8 KV cache（SGLang 另已显式启用 FP8 权重量化，见附录 A.2）：

```bash
modelctl start kimi-k2.5-vllm
modelctl start qwen3.8-vllm
```

## 五、关键配置参数说明

### 输入/输出上下文长度

`modelctl status <name>` 现在显示三个字段：

```text
智能体配置参考：
  上下文长度：32768
  输入上下文长度：28672
  输出上下文长度：4096
  ...
```

- **上下文长度**：模型支持的最大总上下文（输入 + 输出）。
- **输入上下文长度**：建议的最大输入 token 数（总上下文 - 输出上下文）。
- **输出上下文长度**：建议的最大输出 token 数。

约束：`输入上下文长度 + 输出上下文长度 <= 上下文长度`。

输出上下文长度未在 YAML 中配置时，系统会自动推荐：约为总上下文的 1/8，下限 1024，上限 8192，并保证输入至少预留 1024 tokens。

### KV Cache 量化

| 引擎 | 推荐 KV 量化 | 精度损失 | 说明 |
|------|-------------|---------|------|
| **llama.cpp** | q8_0（默认）/ q5_0（light） | <1% / 1-2% | GGUF 生态最稳定 |
| **vLLM** | fp8 | <1% | RTX Ada 原生支持 |
| **SGLang** | fp8（通过 extra_args） | <1% | 需显式传入 `--kv-cache-dtype fp8` |
| **Ollama** | 不可配置 | 依赖模型 | 由 ollama 内部决定 |
| **Unsloth** | 不可配置 | 依赖模型 | 由 unsloth 内部决定 |

### 多卡并行

| 引擎 | 参数 | 说明 |
|------|------|------|
| **llama.cpp** | `gpu_count` | 层分片到 N 张卡 |
| **vLLM** | `tensor_parallel_size` + `gpu_list` | TP 并行，可指定 GPU 子集 |
| **SGLang** | `tensor_parallel_size` + `gpu_list` | TP 并行 |
| **Unsloth** | `tensor_parallel: true/false` | GGUF tensor-parallel |
| **Ollama** | `gpu_list`（CUDA_VISIBLE_DEVICES） | 适配器支持按 profile 独立端口（OLLAMA_HOST）起 serve；现有配置共用 11434（附录 B.5） |

## 六、多模型 GPU 隔离示例

8 张卡可以划分为两个 4 卡域，同时服务两个模型。light 变体已在 profile 内硬编码 `gpu_list`，直接启动即可，无需再叠加 CUDA_VISIBLE_DEVICES：

```bash
# 终端 1：DeepSeek-V4-Flash-light 占 0-3 号卡（profile 内 gpu_list: "0,1,2,3"）
modelctl start deepseek-v4-flash-vllm-light

# 终端 2：Qwen3.8-light 占 4-7 号卡（profile 内 gpu_list: "4,5,6,7"）
modelctl start qwen3.8-vllm-light
```

或对未配置 gpu_list 的 profile 使用环境变量（vLLM/SGLang 已支持 `gpu_list`）：

```yaml
vllm:
  tensor_parallel_size: 4
  gpu_list: "0,1,2,3"
```

> Ollama：适配器按 profile 的 port 设置 `OLLAMA_HOST`，理论上可独立 serve；但现有
> ollama/*.yaml 均使用 11434（共享 serve 语义），stop 只卸载模型不杀进程。如需
> per-profile 隔离，请给不同 ollama profile 配置不同端口并加 `gpu_list`。

> 注意：`gpu_list` 必须与 `tensor_parallel_size` 一致（vLLM/SGLang 启动前会校验）。
> 管线并行（PP）组合不设 gpu_list，改用 `MODELCTL_GPUS` / `--gpus` 控制（见 deepseek-v4-flash-vllm-pp 配置注释）。

## 七、显存占用估算

### DeepSeek-V4-Flash（FP8 权重 + FP8 KV）

| 变体 | 上下文 | TP 卡数 | 单卡权重 | 单卡 KV 约 | 单卡总占用约 |
|------|--------|---------|---------|-----------|-------------|
| high | 65K | 8 | ~15 GB | ~25 GB | ~45 GB |
| balanced | 32K | 8 | ~15 GB | ~12 GB | ~32 GB |
| light | 16K | 4 | ~30 GB | ~12 GB | ~45 GB |

### Qwen3.8-27B（Q4_K_M GGUF + q8_0/q5_0 KV）

| 变体 | 上下文 | GPU 数 | 单卡权重 | 单卡 KV 约 | 单卡总占用约 |
|------|--------|--------|---------|-----------|-------------|
| high | 65K | 4 | ~4.5 GB | ~20 GB | ~28 GB |
| balanced | 32K | 2 | ~9 GB | ~20 GB | ~32 GB |
| light | 16K | 2 | ~9 GB | ~5 GB | ~17 GB |

> 以上为理论估算。实际占用还受 CUDA context、cuBLAS workspace、NCCL buffer 等固定开销（每卡约 1-3 GB）影响。
>
> 注：llamacpp 的 KV cache 显存 = `parallel × ctx_size × per_token_kv_bytes`，不是 `(parallel × ctx_size)²`。
> 以 Qwen3.8-27B 为例（64 层、4 KV heads、256 head_dim、q8_0 KV），原配置 `262144 × 4 = 1,048,576` 总 token 的 KV 约为 128 GB，均分到 8 卡后每卡约 16 GB，加上权重和 CUDA 开销后远低于 48 GB，因此可稳定运行。

## 八、精度损失说明

| 量化组合 | 精度损失（相对 BF16） | 是否符合 ≤2% 要求 |
|----------|---------------------|------------------|
| FP8 权重 + FP8 KV | <1% | 是 |
| Q8_0 权重 + Q8_0 KV | <1% | 是 |
| Q4_K_M 权重 + Q8_0 KV | 1-2% | 是 |
| Q5_0 KV | 1-2% | 是 |
| Q4_0 KV | 2-4% | 否（本次未使用） |

## 九、常见问题

### Q1: 启动后仍然 OOM？

可能原因：
- 其他进程占用显存。使用 `nvidia-smi` 检查。
- 上下文长度对于当前 batch 仍然过大。尝试更小的 `ctx_size` / `max_model_len` 或 `parallel`。
- CUDA context 和通信 buffer 占用超出预期。尝试降低 `gpu_memory_utilization`（vLLM/SGLang）。

### Q2: 为什么 light 变体只使用 4 张卡？

light 变体目标是在保证低延迟的同时释放 GPU 资源，让同一节点可以运行其他模型。如需单模型最大吞吐，可使用 balanced/high 变体。

### Q3: 如何同时启动多个模型？

为每个模型指定互不重叠的 `gpu_list`（或 `MODELCTL_GPUS`）。Ollama 现有配置共用 11434 端口的共享 serve，如需隔离请给不同 ollama profile 配不同端口 + `gpu_list`（附录 B.5）。

### Q4: 为什么 SGLang 的 FP8 KV 要放在 extra_args？

当前 modelctl 的 SGLang 适配器未单独封装 `kv_cache_dtype` 字段，因此通过 `extra_args` 透传 `--kv-cache-dtype fp8`。附录 A.2 起同时透传 `--quantization fp8` 启用权重 FP8；若引擎不支持权重 FP8，回退为仅保留 `--kv-cache-dtype fp8`。

### Q5: `modelctl status <name>` 里的 Token 速率是什么？

两行消息：**Token 计费** 读取 profile 的 `usage.price_in/price_out`（元/千token）；**Token 速率** 查询用量统计服务 `/api/usage` 的实时输入/输出 tok/s（需先 `modelctl stats start`；服务未运行或引擎不支持精确统计时显示 `-`）。

### Q6: 启动前会有显存预检提示吗？

会（附录 B.4）。`modelctl start` 时若模型架构可解析（内置表或本地 `config.json`），会按引擎上下文/并发/KV 量化类型估算 KV cache 显存；估算值超过单卡 48GB 上限时打印 OOM 警告，接近上限（>90%）时打印提示。仅告警、不拦截启动。

## 十、引擎最佳配置建议（官方 recipe 参考）

本节基于 vLLM 官方 recipe（[recipes.vllm.ai](https://recipes.vllm.ai/Qwen/Qwen3.8-27B?hardware=rtx_5090_2x&kv_offload=lmcache&variant=fp8&features=tool_calling%2Creasoning%2Cspec_decoding)，Qwen3.8-27B / 2×RTX 5090 / TP2 / FP8）与 SGLang cookbook（[docs.sglang.io](https://docs.sglang.io/cookbook/autoregressive/Qwen/Qwen3.8#hw=gb300&variant=default&quant=fp8&strategy=balanced&nodes=multi-4)，Qwen3.8-2.4T MoE / 4×GB300）的已验证参数，结合本机 8×RTX 5880（48GB/卡、128GB 系统内存、无 NVLink）给出各模型引擎配置建议。

### 10.1 Qwen3.8-27B —— 首选 vLLM（FP8 检查点 + MTP 投机）

模型特性（官方 recipe）：
- **混合注意力**：64 层中仅 16 层全注意力（`full_attention_interval: 4`），其余 48 层为线性注意力（恒定循环状态）；并内置视觉 tower 与 MTP 草稿头。
- 262K 原生上下文，可经 `--hf-overrides` 扩展至 1M。
- 官方 FP8 检查点 `Qwen/Qwen3.8-27B-FP8`（block-scaled，约 28.7 GiB，**单卡可装**）。

推荐配置要点：

| 项 | 建议 | 说明 |
|----|------|------|
| 权重精度 | FP8 检查点 + `quantization: fp8` | 28.7 GiB 权重，TP4 下每卡约 7 GB，KV 余量大 |
| KV cache | `kv_cache_dtype: fp8` | 已启用 |
| 推理解析 | `--reasoning-parser qwen3` | **非可选**：chat 模板以 `<think>` 开头，缺省会导致整段推理进入 `content` |
| 工具调用 | `--enable-auto-tool-choice --tool-call-parser qwen3_coder` | agent / 工具调用场景必备 |
| 投机解码 | `--speculative-config '{"method":"mtp","num_speculative_tokens":3}'` | 使用检查点内置 MTP 头，2×5090 实测 acceptance ≈0.77 |
| batch 预算 | `--max-num-batched-tokens 8192 --max-num-seqs 256` | recipe 标注的常用甜点值 |
| 显存利用率 | `gpu_memory_utilization: 0.95` | recipe 建议（共享卡请谨慎） |
| 依赖 | `transformers>=5.8.0` | `config.json` 由 transformers 5.8.0 写入，需匹配 |

注意事项：
- **LMCache KV offload 不推荐**：recipe 的 2×RTX 5090 方案依赖 LMCache 512GB CPU-DRAM 池；
  本机系统内存仅 128GB 且为 PCIe 4.0，收益有限、复杂度高。
- **1M 上下文**：`--max-model-len 1010000 --hf-overrides '{"text_config": {"max_position_embeddings": 1010000}}'`；
  262K 已覆盖绝大多数场景，默认不开启。
- **llama.cpp 优先级低**：混合注意力（linear attention）在 llama.cpp 支持有限，且无 MTP 投机（`dspark: off`），Qwen3.8 建议以 vLLM 为主引擎。

### 10.2 DeepSeek-V4-Flash —— 保持 FP8 KV + DSpark

DeepSeek-V4 权重自带 fp8 量化（`deepseek_v4_fp8` / `fp8_ds_mla` 布局），vLLM 强制 FP8 KV；llama.cpp 侧使用 DSpark 投机解码。与官方"FP8 权重 + FP8 KV"方向一致，维持现状即可，无需额外参数。

### 10.3 Kimi-K2.5 120B —— TP8 BF16（FP8 检查点未确认）

BF16 权重约 240GB，必须 TP8 才能装载；FP8 检查点存在性未确认（见附录 A.1）。确认后按注释切换，可降至 TP4（约 30GB/卡）并释放 4 张卡。

### 10.4 Qwen3.8-2.4T MoE —— 本机无法部署

SGLang cookbook 的 Qwen3.8 旗舰为 **2.4T 参数（95B active）**：BF16≈4.8TB、FP8≈2.4TB，远超本机 384GB 显存，需 4 节点 16×GB300 才能服务。其已验证参数中可借鉴到本机小模型的思路：NEXTN/MTP 投机解码、`--kv-cache-dtype fp8_e4m3`、`--reasoning-parser qwen3 --tool-call-parser qwen3_coder`（已部分应用到本机 Qwen3.8 配置）。

### 10.5 落地状态

- `models/vllm/qwen3.8.yaml` 已补充 `--reasoning-parser qwen3` / `--tool-call-parser qwen3_coder` 参数与 FP8 检查点切换注释（见下）。
- MTP 投机、batch 调优等参数保持注释说明，待实测验证后启用。

## 十一、附录 A/B 优化落地

以下内容来自 `docs/superpowers/plans/2026-08-24-rtx5880-optimization.md` 附录 A/B，本次已实施：

### A.1 Kimi-K2.5 vLLM FP8 权重量化（说明文档化）

`moonshotai/Kimi-K2.5-Instruct-FP8` 检查点存在性未确认，保持 BF16 主配置不变；切换步骤与回退方式已写入 `models/vllm/kimi-k2.5.yaml` 头部注释。

### A.2 SGLang 显式 FP8 权重量化（已启用）

`sglang/deepseek-v4-flash.yaml`、`sglang/kimi-k2.5.yaml`、`sglang/qwen3.8.yaml` 的 `extra_args` 已由 `--kv-cache-dtype fp8` 升级为 `--quantization fp8 --kv-cache-dtype fp8`；若引擎不支持权重 FP8 请回退。

### A.3 vLLM 显存利用率上调（已启用）

`models/vllm/*.yaml`（含 high/light/pp 变体）的 `gpu_memory_utilization` 由 0.9 上调至 0.92，释放更多 KV cache 容量。若长请求 OOM，可回调至 0.9。

### B.1 多模型 GPU 隔离（已启用）

新增 `models/vllm/qwen3.8-light.yaml`（16K，TP4，`gpu_list: "4,5,6,7"`，端口 8105），与 `deepseek-v4-flash-vllm-light`（0-3 号卡）构成双模型共存的 4+4 卡域方案。

### B.2 管线并行实验（新增实验 profile）

新增 `models/vllm/deepseek-v4-flash-pp.yaml`（TP4 + PP2，端口 8106），主配置保持不变。实测吞吐高于 TP8 时再考虑推广。

### B.3 网关上下文切换（已实现）

`modelctl.core.gateway` 支持按估算输入 token 数路由到 high/balanced/light 变体。配置方式：

```bash
# 启动网关前设置
export GATEWAY_CONTEXT_SWITCH='{"deepseek-v4-flash": [{"min_prompt_tokens": 32768, "target": "deepseek-v4-flash-vllm-high"}, {"min_prompt_tokens": 8192, "target": "deepseek-v4-flash-vllm"}, {"min_prompt_tokens": 0, "target": "deepseek-v4-flash-vllm-light"}]}'
modelctl gateway start
```

输入 token 数为 `messages` 字符数 / 4 的启发式估算；目标变体未注册（未启动）时回退原模型。相关单测见 `tests/test_gateway_context_switch.py`。

### B.4 显存预检（已实现）

新增 `src/modelctl/core/vram_estimator.py`：按 `ctx × n_layers × kv_heads × head_dim × 2 × bytes` 估算 KV 显存，内置 Qwen3.8-27B 架构并支持读取本地 `config.json`。`modelctl start` 时自动告警（见 Q6）。单测见 `tests/test_vram_estimator.py`。

### B.5 Ollama 多模型 GPU 隔离（适配器能力确认 + 注释修正）

ollama 适配器按 profile 的 `port` 设置 `OLLAMA_HOST`，**支持**独立 serve + `gpu_list` 隔离；但现有 `ollama/*.yaml` 均使用 11434（共享 serve），stop 时仅卸载模型。已修正 `engines/ollama.py` 注释，说明隔离的正确配置方式（不同端口 + gpu_list）。

## 十二、后续调优方向

1. **实测 batch size 上限**：使用 `script/benchmark_latency.py` 在不同 batch 下测试吞吐。
2. **pipeline-parallel 实测**：对比 `deepseek-v4-flash-vllm-pp` 与 `deepseek-v4-flash-vllm`（TP8）的吞吐，确认无 NVLink 下是否提升。
3. **动态调度**：结合 `modelctl all` 和网关上下文切换（B.3），根据负载自动切换模型。
4. **量化权重**：Kimi-K2.5 若官方提供 FP8 检查点，按 A.1 注释切换，可进一步降低 vLLM/SGLang 权重显存占用。
5. **Qwen3.8 MTP 投机**：按第十节参数实测 MTP acceptance 后决定是否启用 `--speculative-config`。
