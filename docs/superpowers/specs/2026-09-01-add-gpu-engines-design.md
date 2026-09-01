# modelctl 新增生产 GPU 推理引擎设计

日期：2026-09-01
状态：待评审

## 1. 背景与目标

当前 modelctl 已支持 `llamacpp`、`ollama`、`vllm`、`sglang`、`unsloth` 五种引擎，覆盖 GGUF 本地量化、Ollama 常驻服务、HF 格式高吞吐服务以及 Unsloth 动态量化等场景。为了进一步把 modelctl 打造成通用的一键 LLM 部署工具，本次补充四个生产级 GPU 推理引擎：

- **TensorRT-LLM**：NVIDIA 极致性能，适合固定模型长期高负载生产服务。
- **LMDeploy**：InternLM 团队 TurboMind C++ 引擎，INT4/单卡量化能力强，国内生态活跃。
- **Aphrodite Engine**：vLLM fork，支持最丰富的量化格式（GGUF/GPTQ/AWQ/EXL2/FP8 等）。
- **TokenSpeed**：面向 Agentic 负载的新兴引擎，Qwen 系列上性能突出，MIT 许可。

## 2. 关键决策

| 决策点 | 结论 |
|---|---|
| 集成形态 | 每个引擎新增独立 `EngineAdapter` 子类，保持现有插件式约定 |
| 部署形态 | 优先命令行/venv 方式；TensorRT-LLM 和 TokenSpeed 增加 docker 模式兜底 |
| 模型来源 | HF / ModelScope 本地目录；TensorRT-LLM 额外需要编译产物缓存目录 |
| 进程模型 | 一 profile 一进程，与 vLLM/SGLang 保持一致 |

## 3. 技术可行性分析

**结论：可行**，四个引擎均提供 OpenAI 兼容 HTTP API 或可通过轻量包装提供，且均可在 NVIDIA GPU 上独立运行。

### 3.1 有利条件

1. **插件架构成熟**：新增引擎只需"1 个适配器文件 + 1 条注册 + 1 个 KNOWN_ENGINES 条目"。vLLM 适配器已验证 docker/venv 双模式、模型下载、端口映射、Prometheus 指标映射完整链路，可直接借鉴。
2. **命令行接口稳定**：LMDeploy/Aphrodite 提供类似 `vllm serve` 的子命令；TensorRT-LLM 可通过 `tritonserver` 或 `tensorrt_llm.serve` 启动；TokenSpeed 提供 docker 镜像和 Python 包。
3. **统一健康检查**：均支持 `/health` 或 `/v1/models` 作为就绪探测端点。
4. **指标可映射**：vLLM 派生引擎（Aphrodite）指标与 vLLM 同源；LMDeploy/TensorRT-LLM/TokenSpeed 均有 Prometheus 指标输出，名称待实现阶段确认。

### 3.2 需验证的不确定点

| 不确定点 | 影响 | 降级路径 |
|---|---|---|
| TensorRT-LLM 编译耗时与缓存策略 | 首次启动可能 28min+ | 设计独立的 `engine_dir` 缓存目录，编译前检查存在性 |
| TokenSpeed 安装方式与接口演进 | 命令行参数可能变化 | 优先 docker 模式；本地模式 flag 常量集中管理 |
| LMDeploy 具体 Prometheus 指标名 | 用量统计 | 若探测不到则标记"不支持精确统计" |
| Aphrodite 对非 llama 架构模型的支持 | 模型兼容性 | 通过 `core/compat` 规则拦截已知不兼容模型 |

## 4. 环境配置要求

### 4.1 新增环境变量（`.env.example` 补充）

| 变量 | 用途 |
|---|---|
| `TENSORRT_LLM_ENGINE_ROOT` | TensorRT-LLM 编译产物默认根目录 |
| `TOKEN_SPEED_IMAGE` | TokenSpeed docker 镜像，可覆盖 profile 中的 `docker_image` |

### 4.2 新增/复用环境变量

| 变量 | 用途 |
|---|---|
| `MODEL_ROOT` / `MODELSCOPE_CACHE` | 模型下载目录与缓存 |
| `HF_HOME` | HF 缓存目录 |
| `CUDA_VISIBLE_DEVICES` | GPU 隔离（复用现有机制） |

### 4.3 新增 venv（可选）

| 引擎 | 是否新建 venv | 说明 |
|---|---|---|
| `lmdeploy` | 建议 | 与 vLLM 依赖冲突风险低，但独立环境更稳定 |
| `aphrodite` | 建议 | vLLM fork，可基于 vllm venv 扩展或独立 |
| `tokenspeed` | 建议 | 新兴项目，依赖变化快，独立环境安全 |
| `tensorrt_llm` | 可选 | 优先 docker 模式；本地安装复杂 |

## 5. 代码适配要点

### 5.1 新增/修改文件清单

| 文件 | 动作 |
|---|---|
| `src/modelctl/engines/tensorrt_llm.py` | 新增 `TensorRtLlmAdapter` |
| `src/modelctl/engines/lmdeploy.py` | 新增 `LmdeployAdapter` |
| `src/modelctl/engines/aphrodite.py` | 新增 `AphroditeAdapter` |
| `src/modelctl/engines/tokenspeed.py` | 新增 `TokenSpeedAdapter` |
| `src/modelctl/engines/__init__.py` | 注册 4 个新引擎 |
| `src/modelctl/core/profile.py` | `KNOWN_ENGINES` 增加 4 个名称 |
| `src/modelctl/core/capabilities.py` | 探测新引擎二进制/版本（可选） |
| `envs/{lmdeploy,aphrodite,tokenspeed}/pyproject.toml` | 新增独立 venv |
| `models/{tensorrt_llm,lmdeploy,aphrodite,tokenspeed}/*.yaml` | 新增示例配置 |
| `tests/test_engines_*.py` | 新增各引擎单元测试 |

### 5.2 各引擎启动命令草案

#### TensorRT-LLM（venv 模式）

```bash
python -m tensorrt_llm.serve \
  --model /models/Qwen3.8-27B \
  --engine_dir /engines/qwen3.8-tp4-fp8 \
  --host 0.0.0.0 \
  --port 8120 \
  --tp 4 \
  --max_input_len 32768 \
  --max_output_len 8192 \
  --max_batch_size 64
```

docker 模式：挂载模型目录与 engine 缓存目录，透传 GPU。

#### LMDeploy

```bash
lmdeploy serve api_server \
  /models/Qwen3.8-27B \
  --server-name 0.0.0.0 \
  --server-port 8130 \
  --tp 1 \
  --session-len 32768 \
  --cache-max-entry-count 0.8 \
  --quant-policy 4
```

#### Aphrodite

```bash
aphrodite run \
  /models/Qwen3.8-27B-Q4_K_M.gguf \
  --host 0.0.0.0 \
  --port 8140 \
  --tensor-parallel-size 1 \
  --quantization gguf \
  --max-model-len 32768
```

#### TokenSpeed（docker 模式）

```bash
docker run --gpus all -p 8150:8000 \
  -v /models:/models:ro \
  lightseekorg/tokenspeed:latest \
  serve /models/Qwen3.5-397B-A17B --tp 8
```

## 6. 配置示例

### 6.1 TensorRT-LLM

```yaml
group: qwen3.8
port: 8120
api_key: ${API_KEY}

tensorrt_llm:
  model: /raid5/sh/model-hf/Qwen/Qwen3.8-27B
  engine_dir: /raid5/sh/trt_engines/qwen3.8-tp4-fp8
  tensor_parallel_size: 4
  quantization: fp8
  max_input_len: 32768
  max_output_len: 8192
  max_batch_size: 64
  extra_args: "--use_fused_mlp --enable_chunked_context"

usage:
  price_in: 0.5
  price_out: 1.0
```

### 6.2 LMDeploy

```yaml
group: qwen3.8
port: 8130
api_key: ${API_KEY}

lmdeploy:
  model: /raid5/sh/model-hf/Qwen/Qwen3.8-27B
  tensor_parallel_size: 1
  cache_max_entry_count: 0.8
  quant_policy: 4
  session_len: 32768
  extra_args: "--enable-prefix-caching"

usage:
  price_in: 0.5
  price_out: 1.0
```

### 6.3 Aphrodite

```yaml
group: qwen3.8
port: 8140
api_key: ${API_KEY}

aphrodite:
  model: /raid5/sh/model-gguf/Qwen3.8-27B-Q4_K_M.gguf
  tensor_parallel_size: 1
  quantization: gguf
  max_model_len: 32768
  extra_args: "--disable-log-requests"

usage:
  price_in: 0.5
  price_out: 1.0
```

### 6.4 TokenSpeed

```yaml
group: qwen3.5-397b
port: 8150
api_key: ${API_KEY}

tokenspeed:
  model: Qwen/Qwen3.5-397B-A17B
  tensor_parallel_size: 8
  max_model_len: 131072
  docker_image: lightseekorg/tokenspeed:latest
  extra_args: "--enable-prefix-caching"

usage:
  price_in: 1.0
  price_out: 3.0
```

## 7. 测试计划

| 测试项 | 方法 |
|---|---|
| 配置文件加载 | `tests/test_cli_env.py` / `test_profile.py` 扩展，确认新引擎 profile 能被识别 |
| 命令构建 | 每个引擎新增 `test_engines_<engine>.py`，mock 环境探测，断言 `build_command()` 输出 |
| 健康检查 | mock `/health` 或 `/v1/models` 响应 |
| GPU 选择 | 复用 `test_engines_base_gpu.py` 模式，验证 `gpu_list` / `tensor_parallel_size` 一致性 |
| 模型下载/编译缓存 | 集成测试：小模型或空目录验证 `pre_start` 行为 |
| 兼容性规则 | 在 `compat_rules.py` 补充不支持的模型/量化组合 |

## 8. 风险与缓解

| 风险 | 缓解措施 |
|---|---|
| TensorRT-LLM 编译产物占用大量磁盘 | 提供 `engine_dir` 配置并建议统一缓存根目录 |
| 新引擎依赖与 vLLM 冲突 | 每个新引擎独立 venv |
| TokenSpeed 接口不稳定 | 优先 docker 模式，本地模式 flag 集中常量管理 |
| 指标映射缺失导致用量统计不准 | 实现阶段优先确认 `/metrics` 端点，缺失时降级为差分统计 |
| 测试环境缺少 NVIDIA GPU | CI 中仅跑命令构建与配置校验；集成测试本地运行 |

## 9. 实施顺序建议

1. **Aphrodite**：命令行与 vLLM 最接近，集成最快，可复用大量 vLLM 适配器逻辑。
2. **LMDeploy**：命令行稳定，国产生态，适合第二步验证 C++ 引擎接入模式。
3. **TensorRT-LLM**：编译缓存逻辑需要专门设计，放在第三位。
4. **TokenSpeed**：接口最新，docker 模式先做，本地模式后续跟进。
