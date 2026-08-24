# RTX 5880 推理优化执行计划

## Goal

在 8×RTX 5880 服务器上，为 `modelctl` 支持的多个推理引擎（llamacpp、ollama、vllm、sglang）建立一套可稳定运行、显存占用可控、延迟可测的模型配置与验证方案。通过修复 OOM 配置、启用 FP8 KV Cache、创建上下文变体、补充文档与 benchmark 脚本，最终形成可交付的 RTX 5880 优化配置包。

## Architecture

本计划涉及的代码与配置文件分布在以下目录：

- `models/`：各引擎的 YAML 模型配置（llamacpp、ollama、vllm、sglang）。
- `docs/`：面向用户的优化指南与性能报告。
- `scripts/`：性能测试与对比脚本。
- `src/modelctl/`：引擎适配器与核心逻辑（本次不改动引擎适配器核心逻辑）。
- `tests/`：现有测试用例（需保持通过）。

优化工作按引擎分组推进：

| 引擎 | 当前问题 | 主要优化方向 |
|------|----------|--------------|
| llamacpp | 部分配置 OOM | 调整上下文长度与量化参数 |
| ollama | 部分配置 OOM | 调整上下文长度与量化参数 |
| vllm | 显存占用高 | 启用 FP8 KV Cache、创建上下文变体 |
| sglang | 显存占用高 | 启用 FP8 KV Cache、创建上下文变体 |

## Tech Stack

- Python 3.10+
- `modelctl` 自定义 CLI 与引擎适配框架
- vLLM 0.6.x / SGLang 0.4.x（支持 `kv_cache_dtype=fp8`）
- llama.cpp（支持 Flash Attention、KV 量化）
- Ollama（支持上下文长度参数、量化模型）
- pytest（现有测试）

## Global Constraints

本计划中所有任务必须遵守以下约束：

1. **精度损失不超过 2%**：任何量化、KV Cache 压缩或上下文裁剪改动，在下游 benchmark 或 perplexity 测试中相对原配置的精度损失不得超过 2%。
2. **单卡显存不超过 48GB**：所有最终配置在 8×RTX 5880（单卡 48GB）上运行时，单卡峰值显存占用不得超过 48GB。
3. **多卡并行必须显式可用**：所有 vLLM/SGLang 配置若使用多卡并行，必须显式声明 `tensor_parallel_size`，并确保 `modelctl start` 能正确拉起多卡服务。
4. **不改动引擎适配器核心逻辑**：仅在 YAML 配置、文档、脚本层面做修改，不改动 `src/modelctl/engines/*.py` 的核心适配逻辑。
5. **保持现有测试通过**：任何改动不得破坏 `pytest` 现有测试用例。

---

## Task 1：修复 OOM 配置

**目标**：修复当前在 RTX 5880 上运行时触发 OOM 的模型配置。

**说明**：
- `llamacpp/deepseek-v4-flash.yaml` 和 `llamacpp/qwen3.8.yaml` 经过实际验证可稳定运行，**保留原配置不变**。
- 仅修复 `unsloth/deepseek-v4-flash.yaml` 和 `ollama/qwen3.8.yaml`。

**Files：**
- Modify: `models/unsloth/deepseek-v4-flash.yaml`
- Modify: `models/ollama/qwen3.8.yaml`

**Interfaces：**
- `modelctl start unsloth/deepseek-v4-flash`
- `modelctl start ollama/qwen3.8`
- `nvidia-smi` 监控显存

**Step：**
- [ ] 1.1 读取 `models/unsloth/deepseek-v4-flash.yaml`，确认 OOM 原因（上下文过长、量化等级不足等）。
- [ ] 1.2 调整 `unsloth/deepseek-v4-flash.yaml` 的上下文长度或量化参数，使其在 RTX 5880 单卡/多卡下稳定运行。
- [ ] 1.3 读取 `models/ollama/qwen3.8.yaml`，确认 OOM 原因。
- [ ] 1.4 调整 `ollama/qwen3.8.yaml` 的上下文长度、量化参数或 `num_gpu`/`num_thread`，使其稳定运行。
- [ ] 1.5 分别启动两个 profile，运行 `nvidia-smi` 验证单卡峰值显存 ≤ 48GB。
- [ ] 1.6 运行现有测试，确保无回归。

---

## Task 2：为 vLLM / SGLang 启用 FP8 KV Cache

**目标**：在支持的 vLLM / SGLang 配置中启用 FP8 KV Cache，降低显存占用。

**Files：**
- Modify: `models/vllm/deepseek-v4-flash.yaml`
- Modify: `models/vllm/kimi-k2.5.yaml`
- Modify: `models/vllm/qwen3.8.yaml`
- Modify: `models/sglang/deepseek-v4-flash.yaml`
- Modify: `models/sglang/kimi-k2.5.yaml`
- Modify: `models/sglang/qwen3.8.yaml`

**Interfaces：**
- `modelctl start vllm/<profile>`
- `modelctl start sglang/<profile>`
- 引擎启动日志 / 错误输出

**Step：**
- [ ] 2.1 确认当前 vLLM、SGLang 安装版本支持 `kv_cache_dtype=fp8`。
- [ ] 2.2 为 vLLM 配置统一添加 `kv_cache_dtype: fp8`。
- [ ] 2.3 为 SGLang 配置在 `extra_args` 中显式传入 `--kv-cache-dtype fp8`。
- [ ] 2.4 对每个引擎各选一个 profile 启动，观察启动日志无 FP8 相关报错。
- [ ] 2.5 使用 `nvidia-smi` 对比启用前后单卡显存占用，确认有下降。
- [ ] 2.6 运行基础推理请求，验证输出无异常。
- [ ] 2.7 运行现有测试，确保无回归。

---

## Task 3：创建 high / balanced / light 上下文变体

**目标**：为 vLLM / SGLang 的主要模型创建 9 个新 profile，覆盖高上下文、均衡、轻量三种场景。

**说明**：
- 变体维度：`high`（长上下文）、`balanced`（默认上下文）、`light`（短上下文、低显存）。
- 涉及模型：deepseek-v4-flash、kimi-k2.5、qwen3.8。
- 引擎：vLLM、SGLang（llamacpp/ollama/unsloth 不在本次创建变体范围内）。

**Files：**
- Create: `models/vllm/deepseek-v4-flash-high.yaml`
- Create: `models/vllm/deepseek-v4-flash-light.yaml`
- Create: `models/vllm/kimi-k2.5-high.yaml`
- Create: `models/vllm/kimi-k2.5-light.yaml`
- Create: `models/vllm/qwen3.8-high.yaml`
- Create: `models/vllm/qwen3.8-light.yaml`
- Create: `models/sglang/deepseek-v4-flash-high.yaml`
- Create: `models/sglang/deepseek-v4-flash-light.yaml`
- Create: `models/sglang/kimi-k2.5-high.yaml`
- Create: `models/sglang/kimi-k2.5-light.yaml`
- Create: `models/sglang/qwen3.8-high.yaml`
- Create: `models/sglang/qwen3.8-light.yaml`

**Interfaces：**
- `modelctl start vllm/<model>-<variant>`
- `modelctl start sglang/<model>-<variant>`

**Step：**
- [ ] 3.1 定义三类变体的上下文长度与显存预算：
  - `high`：max_model_len ≥ 65536
  - `balanced`：max_model_len 保持原配置
  - `light`：max_model_len ≤ 8192
- [ ] 3.2 为 vLLM 的 3 个模型各创建 high / light 变体（balanced 使用现有原配置）。
- [ ] 3.3 为 SGLang 的 3 个模型各创建 high / light 变体（balanced 使用现有原配置）。
- [ ] 3.4 所有新 profile 继承对应引擎的 FP8 KV Cache 配置。
- [ ] 3.5 对每个新 profile 执行 `modelctl start` 并验证启动成功。
- [ ] 3.6 使用 `nvidia-smi` 验证 high / balanced / light 三类变体单卡显存均 ≤ 48GB。
- [ ] 3.7 运行现有测试，确保无回归。

---

## Task 4：编写配置说明文档 `docs/rtx5880-optimization-guide.md`

**目标**：面向用户编写 RTX 5880 优化配置的使用说明。

**Files：**
- Create: `docs/rtx5880-optimization-guide.md`

**Interfaces：**
- Markdown 文档阅读

**Step：**
- [ ] 4.1 文档开头说明适用范围：8×RTX 5880、modelctl、支持引擎。
- [ ] 4.2 列出所有优化后的 profile 及其适用场景（high / balanced / light）。
- [ ] 4.3 说明 FP8 KV Cache 的启用方式与兼容性要求。
- [ ] 4.4 说明 OOM 修复项（unsloth/ollama）及建议的启动命令。
- [ ] 4.5 给出显存占用参考表（单卡峰值、上下文长度、引擎）。
- [ ] 4.6 给出常见问题排查（FP8 不支持、OOM、多卡启动失败）。
- [ ] 4.7 文档完成后由至少一人审阅。

---

## Task 5：创建性能测试脚本 `scripts/benchmark_latency.py` 与对比报告 `docs/rtx5880-performance-report.md`

**目标**：提供可复用的延迟/吞吐 benchmark 脚本，并生成 RTX 5880 性能对比报告。

**Files：**
- Create: `scripts/benchmark_latency.py`
- Create: `docs/rtx5880-performance-report.md`

**Interfaces：**
- `python scripts/benchmark_latency.py --profile <profile> --prompt-len <n> --max-tokens <n> --iterations <n>`
- 输出 CSV / JSON 结果

**Step：**
- [ ] 5.1 设计 benchmark 脚本参数：
  - `--profile`：要测试的 modelctl profile
  - `--prompt-len`：输入 prompt token 长度
  - `--max-tokens`：生成最大 token 数
  - `--iterations`：重复次数
  - `--output`：结果文件路径
- [ ] 5.2 脚本实现功能：
  - 自动启动指定 profile
  - 构造指定长度的 prompt
  - 测量首 token 延迟（TTFT）、每 token 延迟、总耗时、峰值显存
  - 关闭服务并保存结果
- [ ] 5.3 在至少 3 个代表性 profile 上运行脚本，收集数据。
- [ ] 5.4 编写 `docs/rtx5880-performance-report.md`，包含：
  - 测试环境（GPU、驱动、CUDA、框架版本）
  - 测试方法说明
  - 各 profile 在不同 prompt 长度下的延迟/吞吐对比表
  - 显存占用对比
  - 结论与推荐配置
- [ ] 5.5 确保脚本不暴露任何敏感信息（API key、路径等）。
- [ ] 5.6 运行现有测试，确保无回归。

---

## Task 6：最终验证与报告

**目标**：整合 Task 1-5 的所有改动，执行最终验证并输出交付报告。

**Files：**
- Review: `models/**/*.yaml`
- Review: `docs/rtx5880-optimization-guide.md`
- Review: `docs/rtx5880-performance-report.md`
- Review: `scripts/benchmark_latency.py`

**Interfaces：**
- `pytest`
- `modelctl start <profile>` 全量启动检查

**Step：**
- [ ] 6.1 汇总所有新增/修改文件清单。
- [ ] 6.2 对每个新增或修改过的 profile 执行启动验证，确认无启动失败。
- [ ] 6.3 对 high / balanced / light 变体抽样执行 benchmark，确认性能符合预期。
- [ ] 6.4 全量运行 `pytest`，确认所有现有测试通过。
- [ ] 6.5 检查所有 YAML 配置均满足 Global Constraints（精度、显存、多卡、核心逻辑不改动）。
- [ ] 6.6 编写最终交付报告，包含改动摘要、验证结果、已知限制与后续建议。
- [ ] 6.7 提交 PR 或合并到主分支（按仓库流程）。

---

## 附录 A：中优先级优化项（后续执行）

以下优化项在 8×RTX 5880 服务器上有明确收益，但因依赖实测反馈或外部模型仓库状态，作为后续迭代任务保留。

### A.1 vLLM Kimi-K2.5 尝试 FP8 权重量化

**Files:**
- Modify: `models/vllm/kimi-k2.5.yaml`

**内容:**

当前 `quantization` 留空，使用 BF16 权重（约 240GB）。若 moonshotai/Kimi-K2.5-Instruct 官方或社区提供 FP8 检查点，可改为：

```yaml
vllm:
  model: moonshotai/Kimi-K2.5-Instruct-FP8
  quantization: fp8
  kv_cache_dtype: fp8
```

**验证:**
- 启动后 `nvidia-smi` 显示单卡权重占用从 ~30GB 降至 ~15GB。
- 精度损失 <1%（可通过 perplexity 或下游 benchmark 验证）。

### A.2 SGLang 显式传入量化参数

**Files:**
- Modify: `models/sglang/deepseek-v4-flash.yaml`
- Modify: `models/sglang/kimi-k2.5.yaml`
- Modify: `models/sglang/qwen3.8.yaml`

**内容:**

当前仅通过 `extra_args` 传入 `--kv-cache-dtype fp8`。若 SGLang 版本支持权重 FP8，可追加：

```yaml
sglang:
  extra_args: "--quantization fp8 --kv-cache-dtype fp8"
```

**验证:**
- `modelctl start` 正常启动。
- `/metrics` 中内存相关指标显示 KV 占用下降。

### A.3 调整 vLLM `gpu_memory_utilization`

**Files:**
- Modify: `models/vllm/*.yaml`

**内容:**

在验证稳定后，可将 `gpu_memory_utilization` 从 0.9 提升到 0.92-0.95，释放更多 KV cache：

```yaml
vllm:
  gpu_memory_utilization: 0.95
```

**风险:**
- 过高可能导致长请求 OOM。
- 需配合实测逐步上调。

---

## 附录 B：架构层优化（后续执行）

以下优化涉及框架级改动或更复杂的部署策略，作为独立任务后续推进。

### B.1 多模型 GPU 隔离示例化

**Files:**
- Create: `models/vllm/qwen3.8-light.yaml`（如需完整 light 变体）
- Modify: `docs/rtx5880-optimization-guide.md`

**内容:**

为 vLLM/SGLang 的 light 变体显式配置 `gpu_list`，实现 8 卡上运行两个模型：

```yaml
# models/vllm/deepseek-v4-flash-light.yaml 已存在，可补充 qwen3.8-light
name: qwen3.8-vllm-light
vllm:
  model: Qwen/Qwen3.8-27B
  tensor_parallel_size: 4
  gpu_list: "4,5,6,7"
  max_model_len: 16384
  kv_cache_dtype: fp8
```

**验证:**
- 同时启动 `deepseek-v4-flash-vllm-light`（gpu 0-3）和 `qwen3.8-vllm-light`（gpu 4-7），`nvidia-smi` 显示各用 4 卡。

### B.2 Pipeline Parallel 探索

**Files:**
- Modify: `models/vllm/*.yaml`

**内容:**

无 NVLink 时，vLLM 可组合 TP 与 PP 降低 all-reduce 通信量。例如 DeepSeek-V4-Flash：

```yaml
vllm:
  tensor_parallel_size: 4
  # vLLM 通过 extra_args 传入 pipeline-parallel-size
  extra_args: "--pipeline-parallel-size 2"
```

**验证:**
- 启动命令中同时出现 `--tensor-parallel-size 4 --pipeline-parallel-size 2`。
- 实测吞吐与纯 TP8 对比，确认无 NVLink 下是否有提升。

### B.3 动态上下文切换

**Files:**
- Modify: `src/modelctl/core/gateway.py`
- Create: `tests/test_gateway_context_switch.py`

**内容:**

在网关层根据请求 `max_tokens` 或上下文长度，自动路由到 high/balanced/light 变体：

```python
# 伪代码
if prompt_tokens > 32768:
    target = "deepseek-v4-flash-vllm-high"
elif prompt_tokens > 8192:
    target = "deepseek-v4-flash-vllm"
else:
    target = "deepseek-v4-flash-vllm-light"
```

**验证:**
- 网关测试覆盖不同 prompt 长度路由到正确后端。

### B.4 显存预检增强

**Files:**
- Modify: `src/modelctl/core/capabilities.py` 或新增 `src/modelctl/core/vram_estimator.py`
- Create: `tests/test_vram_estimator.py`

**内容:**

基于模型参数、上下文长度、并发数、KV 量化类型估算 KV cache 显存，并在启动前给出警告：

```python
# 伪代码
def estimate_kv_vram(ctx_size, parallel, n_layers, kv_heads, head_dim, dtype_bytes):
    return ctx_size * parallel * n_layers * kv_heads * head_dim * 2 * dtype_bytes
```

**验证:**
- 对 Qwen3.8-27B 原配置估算接近 128GB。
- 对不合理的配置在 `start` 前打印警告。

### B.5 Ollama 多模型 GPU 隔离

**Files:**
- Modify: `src/modelctl/engines/ollama.py`

**内容:**

当前 Ollama serve 是全局进程，多个 ollama profile 会共享同一 serve。可改进为按 profile 启动独立 serve 实例，绑定不同端口和 GPU：

```python
# 为每个 ollama profile 使用独立端口和 CUDA_VISIBLE_DEVICES
```

**风险:**
- 改动较大，可能影响现有 Ollama 用户。
- 需充分测试后再合并。

---

*附录 A/B 为后续迭代任务，不在本次计划主任务范围内，但已明确文件路径和验证方式。*
