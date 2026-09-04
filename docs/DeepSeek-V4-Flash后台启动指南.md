# DeepSeek-V4-Flash 部署与运维指南

在远程服务器（8× RTX 5880 Ada）上部署 DeepSeek-V4-Flash-0731（官方 llama.cpp + DSpark）的完整说明。工程化改造后，统一使用 `modelctl` CLI 管理生命周期，配置分层为：

- **全局配置**：项目根 `.env`（API 密钥、模型存储目录、日志目录、llama.cpp 源码目录、用量统计服务）
- **模型级配置**：`models/<engine>/<name>.yaml`（模型路径、端口、并行度、量化、DSpark 参数、下载配置、用量单价）

配置优先级：**profile YAML > 环境变量 > .env 文件 > 代码默认值**。

## 前置条件

- Python 3.12+，已安装项目依赖：`uv sync --extra dev`
- `git`、`cmake`、CUDA 工具链、`nvidia-smi`
- 官方 llama.cpp 源码目录（用于编译 `llama-server`），默认由 `.env` 的 `LLAMACPP_SOURCE_DIR` 指定
- 如尚未下载模型，首次启动时会根据 `models/llamacpp/deepseek-v4-flash.yaml` 的 `download` 段自动从 ModelScope 拉取

模型文件预期布局：

```
${MODEL_ROOT}/DeepSeek-V4-Flash-0731-GGUF/
├── UD-Q8_K_XL/
│   └── DeepSeek-V4-Flash-0731-UD-Q8_K_XL-00001-of-00005.gguf  (+00002~00005)
└── dspark-DeepSeek-V4-Flash-0731-Q8_0.gguf
```

> 默认使用 **UD-Q8_K_XL 无损量化**（162GB，与官方权重 bit-identical）。若需近无损的 Q4（155GB），修改 `models/llamacpp/deepseek-v4-flash.yaml` 中的 `model` 路径为 `UD-Q4_K_XL/` 对应分片即可。

## 配置管理

### 1. 复制并编辑 `.env`

```bash
cp .env.example .env
vi .env
```

与 DeepSeek-V4-Flash 启动直接相关的全局变量：

| 变量 | 默认值示例 | 说明 |
| --- | --- | --- |
| `API_KEY` | `root123456` | API 密钥（供 profile 的 `${API_KEY}` 插值；留空则不校验） |
| `MODEL_ROOT` | `<项目根上级>/model-gguf` | GGUF 模型根目录（llamacpp / unsloth 下载段保存父目录；HF 类引擎默认为同级的 `model-hf/`） |
| `MODELSCOPE_CACHE` | `~/.cache/modelscope` | ModelScope 下载缓存目录（仅透传给子进程） |
| `LLAMACPP_SOURCE_DIR` | `<项目根上级>/llama.cpp` | llama.cpp 源码目录（编译用） |
| `LOG_DIR` | `<项目根>/data/logs` | 启动日志与服务运行日志目录 |
| `USAGE_HOST` | `0.0.0.0` | 用量统计服务监听地址 |
| `USAGE_PORT` | `5002` | 用量统计服务监听端口 |

> 运行时数据目录（`LOG_DIR` / `CACHE_DIR` / `USAGE_DATA_DIR` / `AUDIT_DIR`）默认全部落在
> **项目根 `data/`** 下，无需配置；相对值按项目根解析（不按当前工作目录）。统一解析规则见
> `src/modelctl/core/paths.py`。示例中的 `/raid5/...` 只是把目录挪到大容量盘的写法。

### 1.5 models 目录布局

profile 统一按引擎分目录存放。每个引擎子目录均提供 **deepseek-v4-flash / qwen3.8 / qwen3-coder / kimi-k2.5** 带注释的示例配置（qwen3-coder 因 HF 权重超出本机总显存，无 vllm/sglang 变体），便于学习各引擎参数：

```
models/
├── llamacpp/                   # llamacpp 引擎 profile 子目录
│   ├── deepseek-v4-flash.yaml  # DeepSeek-V4-Flash（llamacpp + DSpark）
│   ├── qwen3.8.yaml            # Qwen3.8-27B GGUF（llamacpp）
│   ├── qwen3-coder.yaml        # Qwen3-Coder-480B MoE GGUF（llamacpp，8 卡全量）
│   └── kimi-k2.5.yaml          # Kimi-K2.5 120B dense GGUF（llamacpp）
├── ollama/                     # ollama 引擎 profile 子目录
│   │   ├── deepseek-v4-flash.yaml.disabled  # DeepSeek-V4-Flash（ollama，已停用：无本地支持）
│   ├── qwen3.8.yaml            # Qwen3.8-27B（ollama）
│   ├── qwen3-coder.yaml        # Qwen3-Coder-480B（ollama）
│   └── kimi-k2.5.yaml          # Kimi-K2.5（ollama）
├── vllm/                       # vllm 引擎 profile 子目录
│   ├── deepseek-v4-flash.yaml  # DeepSeek-V4-Flash（vllm）
│   ├── qwen3.8.yaml            # Qwen3.8-27B（vllm）
│   └── kimi-k2.5.yaml          # Kimi-K2.5（vllm；qwen3-coder 无此变体：HF 权重超总显存）
├── sglang/                     # sglang 引擎 profile 子目录
│   ├── deepseek-v4-flash.yaml  # DeepSeek-V4-Flash（sglang）
│   ├── qwen3.8.yaml            # Qwen3.8-27B（sglang）
│   └── kimi-k2.5.yaml          # Kimi-K2.5（sglang；qwen3-coder 同上）
└── unsloth/                    # unsloth 引擎 profile 子目录
    ├── deepseek-v4-flash.yaml  # DeepSeek-V4-Flash（unsloth）
    ├── qwen3.8.yaml            # Qwen3.8-27B（unsloth）
    ├── qwen3-coder.yaml        # Qwen3-Coder-480B（unsloth，多卡 GGUF 分片）
    └── kimi-k2.5.yaml          # Kimi-K2.5（unsloth）
```

各引擎 profile 的 `name` 全局唯一，自动推导为 `<group>-<engine>[-<variant>]`（如 `deepseek-v4-flash-llamacpp`）。文件本身位于 `models/<engine>/` 下，因此 `engine`（从父目录推导）与 `name`（从 group+engine 推导）均可省略。

### 2. 按需修改 `models/llamacpp/deepseek-v4-flash.yaml`

```yaml
port: 18888
api_key: ${API_KEY}

llamacpp:
  model: /raid5/sh/model/model-gguf/DeepSeek-V4-Flash-0731-GGUF/UD-Q8_K_XL/DeepSeek-V4-Flash-0731-UD-Q8_K_XL-00001-of-00005.gguf
  draft: ""                # 留空自动发现 dspark*.gguf
  parallel: 2
  ctx_size: ""             # 留空 = 每槽 1M（单槽 = 每并发请求可用上下文）
  reasoning: on
  reasoning_format: deepseek
  dspark: on
  spec_type: draft-dspark
  spec_draft_n_max: 3
  n_gpu_layers_draft: 999
  cache_type_k: q8_0
  cache_type_v: q8_0
  gpu_count: 8
  fit: off
  download:
    modelscope_id: unsloth/DeepSeek-V4-Flash-0731-GGUF
    quant: UD-Q8_K_XL

usage:
  price_in: 1.0
  price_out: 2.0
```

> **model 字段可留空自动下载**：若 `model` 为空或指向的 GGUF 不存在，且配置了 `download` 段，
> 首次启动会自动从 ModelScope 下载指定量化分片到 `$MODEL_ROOT/<仓库名>`。落地路径由
> `MODEL_ROOT` + `modelscope_id` 确定性推导，**YAML 不会被改写**（保持 git 干净）。
> 下次启动本地分片已就位则直接复用，不再触发下载。
> 下载目录由 `.env` 的 `MODEL_ROOT` 控制。

## 启动 / 停止 / 重启 / 状态

启动前请确认 `.env` 中的 `MODEL_ROOT`、`MODELSCOPE_CACHE`、`LLAMACPP_SOURCE_DIR`、`LOG_DIR` 已按实际环境设置。

```bash
# 启动（首次会自动编译 llama.cpp 并下载模型到 MODEL_ROOT / MODELSCOPE_CACHE 指定位置）
bash script/modelctl.sh start deepseek-v4-flash-llamacpp

# 停止
bash script/modelctl.sh stop deepseek-v4-flash-llamacpp

# 重启
bash script/modelctl.sh restart deepseek-v4-flash-llamacpp

# 查看状态
bash script/modelctl.sh status

# 列出所有 profile
bash script/modelctl.sh list

# 探测硬件与引擎二进制可用性
bash script/modelctl.sh probe
```

也可直接调用已安装的 `modelctl` 命令：

```bash
uv run modelctl start deepseek-v4-flash-llamacpp
```

## 验证服务

模型加载需要 1-2 分钟，之后健康检查：

```bash
curl http://127.0.0.1:18888/health
```

预期返回 `{"status":"ok"}`。再做一次推理测试（注意带 API key 头）：

```bash
curl http://127.0.0.1:18888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer root123456" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"你好，用一句话自我介绍"}],"max_tokens":100}'
```

## 用量统计服务

```bash
# 启动用量统计服务（/api/usage，cc-switch 兼容）
bash script/modelctl.sh stats start

# 停止用量统计服务
bash script/modelctl.sh stats stop
```

## 查看日志

日志目录由 `.env` 的 `LOG_DIR` 决定。

```bash
# 启动过程日志
tail -f ${LOG_DIR}/launch-deepseek-v4-flash-llamacpp-*.log

# 服务运行日志（llama-server 输出）
tail -f ${LOG_DIR}/llama-server-18888-*.log
```

## 重启 / 换量化

1. 停止服务：`bash script/modelctl.sh stop deepseek-v4-flash-llamacpp`
2. 修改 `models/llamacpp/deepseek-v4-flash.yaml` 中的 `model` 路径（例如换 `UD-Q4_K_XL/`）
3. 重新启动：`bash script/modelctl.sh start deepseek-v4-flash-llamacpp`

> 若首次启动时 `model` 留空、由 `download` 段自动下载，再次换量化时只需修改 `download.quant`
> 并重新启动（`model` 不会被写回，无需清理）。

如需调整 `extra_args` 等额外参数，目前 llamacpp 引擎尚未支持该字段，请直接修改 `build_command()` 输出或提交 issue。

## 参数速查（models/llamacpp/deepseek-v4-flash.yaml）

| YAML 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `model` | `UD-Q8_K_XL/...-00001-of-00005.gguf` | GGUF 模型第一个分片；留空 + `download` 段时自动下载到 `$MODEL_ROOT/<仓库名>`（YAML 不被改写） |
| `draft` | 空（自动发现） | DSpark 草稿路径 |
| `port` | `18888` | 服务端口 |
| `ctx_size` | 空（自动） | **单槽上下文**（每个并发请求完整可用的上下文）。留空自动计算每槽 `1048576`（1M）；启动时总量自动取 `ctx_size × parallel`（llama-server 的 `--ctx-size` 为槽位共享总量） |
| `parallel` | `2` | 并发序列数 |
| `gpu_count` | `8` | GPU 数量 |
| `dspark` | `on` | DSpark 投机解码开关 |
| `reasoning` | `on` | 思考模式 |
| `cache_type_k` / `cache_type_v` | `q8_0` | KV cache 量化 |
| `download.modelscope_id` | `unsloth/DeepSeek-V4-Flash-0731-GGUF` | ModelScope 模型 ID |
| `download.quant` | `UD-Q8_K_XL` | 下载的量化版本 |

## 已知注意事项

1. **`--spec-draft-n-max` 会被钳制到 5**（checkpoint 的 `dspark_block_size`），默认 3 是实测最优
2. **不要传 `--spec-draft-device`**：草稿模型借用主模型的 embedding/输出头，必须跨同一批 GPU
3. **`--no-mmap` 已从默认命令移除**：fork 时代遗留参数，官方版 mmap 加载更快
4. **OpenSSL 未找到**：HTTPS 禁用，本机 HTTP 使用无影响

## 可选优化

- **安装 NCCL**：多卡 layer split 跨卡通信依赖 NCCL，当前编译警告 `NCCL not found`。安装后需重新编译 llama.cpp 才能生效：
  ```bash
  apt-get install -y libnccl-dev
  # 重新配置 + 编译
  cmake -S ${LLAMACPP_SOURCE_DIR} -B ${LLAMACPP_SOURCE_DIR}/build \
    -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_ARCHITECTURES=89
  cmake --build ${LLAMACPP_SOURCE_DIR}/build --config Release -j
  ```
- **换 Q4 近无损量化**：如果显存吃紧或想加快加载，把 `models/llamacpp/deepseek-v4-flash.yaml` 的 `model` 改为 `UD-Q4_K_XL/` 路径，分片约小 7GB

## Unsloth 引擎（实验性）

基于 Unsloth 无头 API 服务（`unsloth studio --api-only`）部署 Unsloth 动态量化 GGUF 模型。

### 前置条件

- 在目标服务器安装 Unsloth：`curl -fsSL https://unsloth.ai/install.sh | sh`（或独立 venv 安装，避免重依赖污染项目环境）
- `.env` 配置 `UNSLOTH_API_KEY`（必填，健康检查依赖）、可选 `HF_ENDPOINT`（HF 兜底镜像）、复用 `MODEL_ROOT`/`MODELSCOPE_CACHE`
- 启动前用 `unsloth --help` 核实无头服务 flag（`--api-only`、`--model`、`-p` 等），与本工具内置常量不一致时需调整 `engines/unsloth.py`

### 使用

```bash
bash script/modelctl.sh start deepseek-v4-flash-unsloth   # 首次自动从 ModelScope 下载到 $MODEL_ROOT
curl http://127.0.0.1:8001/v1/models -H "Authorization: Bearer $UNSLOTH_API_KEY"
bash script/modelctl.sh status
```

### 已知限制

- 用量统计暂不支持精确统计（`/metrics` 端点未验证，`modelctl stats` 对该模型返回"不支持精确统计"）
- 健康检查使用 `/v1/models`（需认证），非 `/health`

## 采样参数与重复输出排查

llamacpp 部署的 DeepSeek-V4 在推理/工具调用场景可能偶发**重复输出**（如 `<｜DSML｜tool_calls` 反复生成、整段文本循环复制）。主要原因：llama.cpp 默认 `repeat-penalty=1.0`（无重复惩罚），且采样参数未显式配置。本工具已在 `models/llamacpp/deepseek-v4-flash.yaml` 提供采样配置：

```yaml
llamacpp:
  repeat_penalty: 1.1      # 重复惩罚（1.0=关闭；1.05~1.15 可有效中断循环）
  repeat_last_n: 256       # 对最后 N 个 token 施加重复惩罚
  temperature: 0.6         # 采样温度（0=贪心，易重复；建议 0.6~0.8）
  top_p: 0.95              # 核采样阈值
  top_k: 40                # 候选 token 数
  # stops: ['<｜DSML｜tool_calls']   # 可选：额外停止序列（高级调优）
```

- 这些字段**均为可选**，缺省时适配器不传参，保持 llama.cpp 默认行为（向后兼容）
- `temperature` 传 `0` 是合法值（贪心模式），会被正确传递
- `stops` 为字符串列表，透传为 llama-server 的 `--stops` 参数；**谨慎使用**——若把 `<｜DSML｜tool_calls` 设为停止符，模型将无法完整输出工具调用，仅在确认截断不影响功能时使用
- 调参建议：先只加 `repeat_penalty: 1.1` + `repeat_last_n: 256` 验证效果；仍循环再降 `temperature`；仍有问题再考虑 `top_p`/`top_k`
- 其他 llama.cpp profile（如 `qwen3.8-llamacpp`）同样支持这些字段

### 其他引擎的采样参数位置

| 引擎 | 服务端采样参数 | 说明 |
|---|---|---|
| llamacpp | profile `llamacpp:` 段（本工具透传） | 见上文 |
| unsloth | Unsloth 对 GGUF 自动调参（temp/top-k 自动推理） | 无头 CLI 采样 flag 未验证，不额外透传；可按需通过 API 请求体覆盖 |
| ollama | Modelfile / API 请求级 | `ollama serve` 默认已有 `repeat_penalty 1.1 / top_k 40 / top_p 0.9` 合理默认，一般无需处理 |
| vllm / sglang | API 请求级（OpenAI 兼容 `temperature`/`repetition_penalty` 等） | serve 启动不支持服务端采样默认；由调用方在请求体传入 |
