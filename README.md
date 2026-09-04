# modelctl — 多模型部署启动器

在 CUDA/Ada GPU 上通过**引擎插件式架构**启动多种模型服务（llamacpp / ollama / vllm / sglang / unsloth），每模型一个 YAML profile，统一 CLI 管理启动、停止、重启、状态与用量统计。

## 特性

- **多引擎支持**：llamacpp（官方 llama.cpp + DSpark 投机解码）、ollama、vllm、sglang、unsloth（无头 API 服务，Unsloth 动态量化 GGUF）
- **YAML profile**：每模型一个 YAML（`models/<engine>/<name>.yaml`），配置模型路径、端口、引擎参数、用量单价
- **自动下载**：model 为空/不存在时从 ModelScope 自动下载，落地路径由 `MODEL_ROOT` + `modelscope_id` 确定性推导（不改写 YAML）
- **能力探测与自动降级**：启动前探测 GPU/CC/显存/引擎二进制，硬性不满足拒绝启动并说明原因，可降级项自动降级并告警
- **统一生命周期**：后台启动、PID 管理、健康检查、优雅停止
- **用量统计**：`/api/usage` 输出与 cc-switch 兼容，支持多模型按 `?model=` 路由
- **Web 管理控制台**：`modelctl webui` 单进程同时提供管理 API（`/admin/api/*`）与 Vue 3 控制台，模型启停、日志实时跟随、环境安装、体检、审计均可在浏览器完成
- **配置外置**：全局配置通过 `.env` 管理，模型级配置通过 profile YAML 管理
- **分布式集群管理面**：`modelctl cluster` 单中心模式——各节点一条 `cluster join` 注册到中心 webui，心跳/租约判活、令牌准入与吊销，跨 LAN 统一节点视图（推理流量不经过中心）

## 目录结构

```
modelctl/
├── README.md                       # 本文档（入口）
├── docs/
│   └── DeepSeek-V4-Flash后台启动指南.md   # 部署与运维详细指南
├── src/modelctl/
│   ├── cli.py                      # 统一 CLI 入口（start/stop/restart/status/list/probe/stats/gateway/webui）
│   ├── __main__.py                 # python -m modelctl 入口
│   ├── core/                       # 核心模块：envfile / profile / capabilities / process / stats / envs（引擎 venv 管理）/ webui（Web 管理面）
│   ├── engines/                    # 引擎适配器：base / llamacpp / ollama / vllm / sglang / unsloth
│   └── py.typed                    # PEP 561 类型标记
├── envs/                           # vllm / sglang 引擎子项目（uv 独立工作区）
├── .venvs/                         # 实际 venv 实体（state，gitignore）
├── script/
│   ├── modelctl.sh                 # bash 薄封装（调用已安装的 modelctl 命令）
│   └── modelctl-all.sh             # bash 薄封装（modelctl all 一键启停）
├── models/                         # 模型 profile（每模型一个 YAML，按引擎分目录）
│   ├── llamacpp/                   # llamacpp 引擎 profile 子目录
│   │   ├── deepseek-v4-flash.yaml  # DeepSeek-V4-Flash（llamacpp + DSpark）
│   │   ├── qwen3.8.yaml            # Qwen3.8-27B GGUF（llamacpp）
│   │   ├── qwen3.8-flash-next.yaml # Qwen3.8-Flash-Next 111GB GGUF（llamacpp，需 PR 分支）
│   │   ├── qwen3-coder.yaml        # Qwen3-Coder-480B MoE GGUF（llamacpp，8 卡全量）
│   │   └── kimi-k2.5.yaml          # Kimi-K2.5 120B dense GGUF（llamacpp）
│   ├── ollama/                     # ollama 引擎 profile 子目录
│   │   ├── deepseek-v4-flash.yaml.disabled  # DeepSeek-V4-Flash（ollama，已停用：无本地支持）
│   │   ├── qwen3.8.yaml            # Qwen3.8-27B（ollama）
│   │   ├── qwen3-coder.yaml        # Qwen3-Coder-480B（ollama）
│   │   └── kimi-k2.5.yaml          # Kimi-K2.5（ollama）
│   ├── vllm/                       # vllm 引擎 profile 子目录
│   │   ├── deepseek-v4-flash.yaml  # DeepSeek-V4-Flash（vllm）
│   │   ├── qwen3.8.yaml            # Qwen3.8-27B（vllm）
│   │   ├── qwen3.8-flash-next.yaml # Qwen3.8-Flash-Next 173GB FP8（vllm，多卡需≥4）
│   │   └── kimi-k2.5.yaml          # Kimi-K2.5（vllm；qwen3-coder 无此变体：HF 权重超总显存）
│   ├── sglang/                     # sglang 引擎 profile 子目录
│   │   ├── deepseek-v4-flash.yaml  # DeepSeek-V4-Flash（sglang）
│   │   ├── qwen3.8.yaml            # Qwen3.8-27B（sglang）
│   │   ├── qwen3.8-flash-next.yaml # Qwen3.8-Flash-Next 173GB FP8（sglang）
│   │   └── kimi-k2.5.yaml          # Kimi-K2.5（sglang；qwen3-coder 同上）
│   └── unsloth/                    # unsloth 引擎 profile 子目录
│       ├── deepseek-v4-flash.yaml  # DeepSeek-V4-Flash（unsloth）
│       ├── qwen3.8.yaml            # Qwen3.8-27B（unsloth）
│       ├── qwen3.8-flash-next.yaml # Qwen3.8-Flash-Next 111GB GGUF（unsloth，多卡或高内存）
│       ├── qwen3-coder.yaml        # Qwen3-Coder-480B（unsloth，多卡 GGUF 分片）
│       └── kimi-k2.5.yaml          # Kimi-K2.5（unsloth）
├── web/                            # Web 管理控制台前端源码（Vue 3 + Vite + TypeScript）
│   ├── src/                        # 前端源码（views / components / api / stores / router）
│   └── package.json                # 前端脚本：dev（vite 5173）/ build（产物输出 ../dist）
├── dist/                           # 前端构建产物（npm run build 生成，webui 挂载为 SPA）
├── data/                           # 运行时数据（gitignore；LOG_DIR/CACHE_DIR/USAGE_DATA_DIR/AUDIT_DIR 默认落点）
│   ├── logs/                       # modelctl.log + launch-<name>.log
│   ├── cache/                      # *.pid / *.gpu-lock / cluster-meta.db
│   ├── usage-data/                 # 用量累计 <name>.json（stats 与网关共用）
│   └── audit/                      # 审计 modelctl-YYYY-MM-DD.jsonl
├── .env.example                    # 全局配置模板（复制为 .env 后修改）
├── .env                            # 本地配置（含密钥，不入库）
├── .gitignore
└── pyproject.toml
```

## 安装

```bash
uv sync --extra dev
uv run modelctl list
```

> 引擎 venv（vllm / sglang）与网关 venv（gateway）均独立于主项目，由 `modelctl env setup <target>` 在首次启动前自动初始化（也可手动执行），无需 `uv sync --extra ...`。

## 快速开始

### 1. 安装依赖

- Python 3.12+、PyYAML
- `git`、`cmake`、CUDA 工具链、`nvidia-smi`（llamacpp 引擎编译用）
- 各引擎二进制：`ollama` / `unsloth` / `llamacpp`（按需安装）；`vllm` / `sglang` 通过 `modelctl env setup <engine>` 自动初始化（引擎 venv 在 `.venvs/<engine>/`，与主环境隔离）
- docker_image 型引擎（vllm Day-0 镜像 / tokenspeed / tensorrt_llm）：宿主机 docker + nvidia-container-toolkit，用 `modelctl env setup docker` 一键准备（见下）

#### 1.1 Docker 系统依赖（docker_image 型引擎）

```bash
# 仅诊断 + 打印可复制的安装脚本（不做任何变更，任何平台可跑）
modelctl env setup docker

# 部署机（Linux + root）实际执行安装：docker-ce + nvidia-container-toolkit + runtime 注册
modelctl env setup docker --run
```

Docker Hub 大镜像（如 `vllm/vllm-openai` 约 21.8GB）拉取加速由 `registry-mirrors` 提供，
写入 `/etc/docker/daemon.json`（与 nvidia runtime 配置合并写，互不覆盖）：

```bash
# 默认：写入内置多源（2026-09 实测可用，按序容灾），无需任何参数
modelctl env setup docker --run

# 显式指定：可重复传 --registry-mirror，完全覆盖内置默认（不追加）
modelctl env setup docker --run \
  --registry-mirror https://docker.1ms.run \
  --registry-mirror https://your.private.mirror
```

> 注意：清华 TUNA / 中科大 / 网易的 Docker Hub 加速均已停服，内置默认源已剔除；
> TUNA 仍保留的是 `docker-ce` **apt 仓库**镜像（安装 deb 包用），二者不要混淆。
> 换源后 `docker pull` 报 `manifest unknown`（Day-0 专用 tag 未同步）等排障细节见
> [docs/known-pitfalls/build/docker-install-mirror.md](docs/known-pitfalls/build/docker-install-mirror.md)。

### 2. 配置 .env 与 profile

```bash
cp .env.example .env
vi .env        # 修改 API 密钥、存储目录、日志目录等全局配置
```

模型级配置（模型路径、端口、并行度、量化等）在 `models/<engine>/<name>.yaml` 中修改。

配置优先级：**profile YAML > 环境变量 > .env 文件 > 代码默认值**。

#### Profile 字段自动推导

profile 按 `models/<engine>/<group>[-<variant>].yaml` 组织，多个头部字段**可省略**，由路径自动推导：

| 字段 | 缺省规则 | 示例 |
|---|---|---|
| `engine` | 从父目录名推导（如 `models/vllm/…` → `vllm`）；根目录文件需显式声明 | — |
| `group` | 从文件名推导（去掉 `-<variant>` 后缀） | `deepseek-v4-flash-high.yaml` → `deepseek-v4-flash` |
| `variant` | 空字符串（默认变体）；`light` / `high` / `pp` 等 | `high` |
| `name` | `{group}-{engine}[-{variant}]`（CLI 标识符） | `deepseek-v4-flash-vllm-high` |
| `alias` / `aliases` | 空列表（网关/nginx 短名路由） | — |
| `port` | **必填**，无法推导 | `8103` |

最小 profile 示例（`models/vllm/qwen3.8.yaml`）：

```yaml
group: qwen3.8
port: 8101
api_key: ${API_KEY}

vllm:
  model: Qwen/Qwen3.8-27B
  ...
```

带变体示例（`models/vllm/qwen3.8-light.yaml`）：

```yaml
group: qwen3.8
variant: light     # 显式声明后 name 自动推导为 qwen3.8-vllm-light
port: 8105

vllm:
  model: Qwen/Qwen3.8-27B
  ...
```

显式声明任意字段（`name` / `engine` / `group`）时优先于自动推导，兼容旧格式。

> 首次启动前，请务必在 `.env` 中设置模型存储目录，否则下载/缓存位置会回退到代码默认值（可能落在项目根目录或当前盘符）：
>
> | 引擎 | 控制下载/缓存位置的环境变量 |
> |---------|----------------------------|
> | llamacpp（全部 `-llamacpp` profile：deepseek-v4-flash / qwen3.8 / qwen3-coder / kimi-k2.5） | `MODEL_ROOT`（GGUF 保存父目录）、`MODELSCOPE_CACHE` |
> | ollama（全部 `-ollama` profile） | `OLLAMA_MODELS` |
> | vllm（全部 `-vllm` profile） | `MODEL_ROOT`（ModelScope 下载目录）、`HF_HOME`（vLLM 缓存） |
> | sglang（全部 `-sglang` profile） | `MODEL_ROOT`（ModelScope 下载目录）、`HF_HOME`（SGLang 缓存） |
> | unsloth（全部 `-unsloth` profile） | `UNSLOTH_API_KEY`（必填）、`HF_ENDPOINT`（HF 兜底镜像）、`MODEL_ROOT`（ModelScope 下载） |

### 2.5 模型自动下载

profile 的 `model` 字段为空或指向的文件/目录不存在时，若配置了 `download` 段，启动时自动从
ModelScope 下载模型：

- **llamacpp**：`download.modelscope_id`（GGUF 仓库）+ `download.quant`（量化名），只下载指定量化分片
- **vllm / sglang**：`download.modelscope_id`（HF 格式仓库），下载整个仓库目录
- **ollama**：无需 download 段，由 `ollama pull` 自动处理

下载目标目录为 `$MODEL_ROOT/<仓库名>`，完全由 `MODEL_ROOT` + `download.modelscope_id` 确定性推导：
目录已就位则直接复用不重复下载；**profile YAML 不会被改写**（保持 git 干净、多机可移植）。

环境变量 `MODEL_ROOT` 控制下载目录（默认：项目根目录上级的 `model-gguf/`（llamacpp / unsloth
的 GGUF）或 `model-hf/`（vllm / sglang / lmdeploy / aphrodite / tokenspeed 的 HF 权重）——
两者是**不同目录**，设置 `MODEL_ROOT` 则两类都改到该根目录下）。

### 2.6 查看可用模型目录

`modelctl list` 按模型家族（group）分组展示全部可用 profile，含引擎、变体、端口与运行状态；每个家族标题行标注**网关路由映射**（输入该家族名会路由到哪个实际模型）：

```bash
uv run modelctl list
```

输出示例（节选）：

```
deepseek-v4-flash（10 配置）｜输入 "deepseek-v4-flash" 当前无运行成员
引擎      变体   端口   状态    标识符
--------  -----  -----  ------  --------------------------------
vllm      -      8100   已停止  deepseek-v4-flash-vllm
vllm      high   8103   已停止  deepseek-v4-flash-vllm-high
vllm      light  8104   已停止  deepseek-v4-flash-vllm-light
vllm      pp     8106   已停止  deepseek-v4-flash-vllm-pp
sglang    -      8200   已停止  deepseek-v4-flash-sglang
...

qwen3.8（8 配置）｜输入 "qwen3.8" 路由至 qwen3.8-vllm（运行中）
引擎      变体   端口   状态    标识符
--------  -----  -----  ------  ----------------------
vllm      -      8101   运行中  qwen3.8-vllm
...

qwen3.8-flash-next（4 配置）｜输入 "qwen3.8-flash-next" 路由至该家族内运行中成员
引擎      变体   端口   状态    标识符
--------  -----  -----  ------  ---------------------------
vllm      -      8110   已停止  qwen3.8-flash-next-vllm
sglang    -      8210   已停止  qwen3.8-flash-next-sglang
unsloth   -      8010   已停止  qwen3.8-flash-next-unsloth
llamacpp  -      18909  已停止  qwen3.8-flash-next-llamacpp
```

> **qwen3.8-flash-next 模型说明**（2026-08 阿里 Qwen 开源，Qwen4 架构早期预览）：
> 125B 总参 + 51B N-gram Embedding，每 token 激活 6B；GDN + QSA 混合注意力，多模态
> （图+文）。FP8 权重 172.78 GiB（BF16 335.28 GiB），原生 262144 token 上下文。
> 四种部署路线对应此处 `qwen3.8-flash-next-{vllm|sglang|unsloth|llamacpp}`：
> - **FP8 路线（vllm/sglang）**：≥ 4 卡（H200 / 8×RTX 5880 等满足 CC 8.9 即可），
>   `tensor_parallel_size` 推荐 8；Flash-Next 需要 vLLM 0.28.0+ 的专用构建，官方以镜像
>   `vllm/vllm-openai:qwen38-flash-next` 提供 Day-0 支持（官方明确 PyPI 安装不支持本 recipe）
> - **GGUF 路线（unsloth/llamacpp）**：Unsloth 已发 GGUF 量化（UD-Q4_K_XL 约 111GB），
>   需 llama.cpp PR #27793（qwen4exp/qwen3.8-flash-next 分支）才识别该架构；
>   8×RTX 5880（48GB/卡，共 384GB）完全装得下，unsloth 2 卡以上 + tensor_parallel 即可

路由规则（与网关一致）：组内按引擎优先级（vllm 优先）取第一个**运行中**的成员；组内成员全部停止时请求失败。`name` / `alias` 输入则精确路由到对应 profile。若设置了 `GATEWAY_DEFAULT_MODEL`，未匹配任何家族/标识符的请求回退至该默认模型。

### 3. 启动服务

```bash
# 启动 DeepSeek-V4-Flash（llamacpp，首次运行会自动编译 llama.cpp 并下载模型）
# 模型会下载到 .env 中 MODEL_ROOT / MODELSCOPE_CACHE 指定的位置
bash script/modelctl.sh start deepseek-v4-flash-llamacpp

# 启动 Qwen3.8-27B（ollama）
# 模型会下载到 .env 中 OLLAMA_MODELS 指定的位置
bash script/modelctl.sh start qwen3.8-ollama

# 启动 Qwen3.8-27B（vllm）
# 模型会下载到 .env 中 HF_HOME 指定的位置
bash script/modelctl.sh start qwen3.8-vllm

# 启动 Qwen3.8-27B GGUF（llamacpp，首次运行自动编译 llama.cpp + 从 ModelScope 下载模型）
# 模型会下载到 .env 中 MODEL_ROOT 指定的位置（$MODEL_ROOT/<仓库名>，YAML 不会被改写）
bash script/modelctl.sh start qwen3.8-llamacpp

# 启动 DeepSeek-V4-Flash（unsloth 无头 API，Unsloth 动态量化 GGUF）
# 模型下载到 $MODEL_ROOT/<仓库名>；api_key 取 .env 中 UNSLOTH_API_KEY
bash script/modelctl.sh start deepseek-v4-flash-unsloth
```

每个引擎子目录均提供 **deepseek-v4-flash**、**qwen3.8**、**qwen3-coder**、**kimi-k2.5** 带注释的示例配置，便于学习各引擎参数（qwen3-coder 因 HF 权重超出本机总显存，无 vllm/sglang 变体）。按引擎启动示例：

```bash
# llamacpp：DeepSeek-V4-Flash（DSpark 投机解码）/ Qwen3.8-27B GGUF / Qwen3-Coder-480B / Kimi-K2.5
bash script/modelctl.sh start deepseek-v4-flash-llamacpp
bash script/modelctl.sh start qwen3.8-llamacpp
bash script/modelctl.sh start qwen3-coder-llamacpp
bash script/modelctl.sh start kimi-k2.5-llamacpp

# ollama：Qwen3.8-27B / Qwen3-Coder-480B / Kimi-K2.5（ollama pull 自动拉取）
# 注：DeepSeek-V4-Flash 无 ollama 本地支持（官方仅有 -cloud 云端标签；ollama 的 llama.cpp 不支持
#     DeepseekV4ForCausalLM 架构），对应 profile 已停用（models/ollama/deepseek-v4-flash.yaml.disabled），
#     本地推理请用 llamacpp / vllm / sglang / unsloth 引擎。
bash script/modelctl.sh start qwen3.8-ollama
bash script/modelctl.sh start qwen3-coder-ollama
bash script/modelctl.sh start kimi-k2.5-ollama

# vllm：DeepSeek-V4-Flash / Qwen3.8-27B / Kimi-K2.5（HF 原始权重）
bash script/modelctl.sh start deepseek-v4-flash-vllm
bash script/modelctl.sh start qwen3.8-vllm
bash script/modelctl.sh start kimi-k2.5-vllm

# sglang：DeepSeek-V4-Flash / Qwen3.8-27B / Kimi-K2.5（HF 原始权重）
bash script/modelctl.sh start deepseek-v4-flash-sglang
bash script/modelctl.sh start qwen3.8-sglang
bash script/modelctl.sh start kimi-k2.5-sglang

# unsloth：DeepSeek-V4-Flash / Qwen3.8-27B / Qwen3-Coder-480B / Kimi-K2.5（无头 API，动态量化 GGUF）
bash script/modelctl.sh start deepseek-v4-flash-unsloth
bash script/modelctl.sh start qwen3.8-unsloth
bash script/modelctl.sh start qwen3-coder-unsloth
bash script/modelctl.sh start kimi-k2.5-unsloth

# Qwen3.8-Flash-Next（2026-08 新架构 Qwen4 预览版，家族名 qwen3.8-flash-next）
# 注意：vllm/sglang 走 FP8（173GB）需 ≥ 4 卡；GGUF（unsloth/llamacpp）依赖 llama.cpp PR 分支
# vllm/sglang FP8（多卡高显存机器）
bash script/modelctl.sh start qwen3.8-flash-next-vllm
bash script/modelctl.sh start qwen3.8-flash-next-sglang
# unsloth/llamacpp GGUF（使用 UD-Q4_K_XL；llamacpp 需用 PR #27793 分支编译的 llama-server，见下）
bash script/modelctl.sh start qwen3.8-flash-next-unsloth
bash script/modelctl.sh start qwen3.8-flash-next-llamacpp
```

#### llamacpp 需用 PR #27793（qwen4exp 分支）编译的 llama-server

Qwen3.8-Flash-Next 的模型架构（`qwen4exp`）主线 llama.cpp 尚未识别，直接
`modelctl start qwen3.8-flash-next-llamacpp` 会报
`error loading model: unknown model architecture: 'qwen4exp'`。
需在**独立目录**构建一份 PR 分支副本（不影响主线 `/raid5/sh/code/llama.cpp` 的构建）：

```bash
git clone --depth 1 https://github.com/ggml-org/llama.cpp.git /raid5/sh/code/llama.cpp-qwen38-flash-next
cd /raid5/sh/code/llama.cpp-qwen38-flash-next
git fetch --depth 1 origin pull/27793/head:qwen4exp-flash-next
git checkout qwen4exp-flash-next
# 注意：必须指定 CUDA 算力（RTX 5880 为 Ada / sm_89），否则编译出的内核与设备不匹配，
# 启动模型时会报 "CUDA error: no kernel image is available for execution on the device"。
cmake -B build -DGGML_CUDA=ON -DGGML_AVX512=ON -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_ARCHITECTURES=89
cmake --build build --config Release -j 4
```

> 若此前已经用**未指定算力**的旧 `build` 目录编过并出现上述 CUDA PDL 报错，请先清空重编：
> `rm -rf build` 后再执行上面的 cmake 命令（`-DCMAKE_CUDA_ARCHITECTURES=89` 对应 8×RTX 5880 的 Ada 架构）。

再把副本路径写入 profile 的 `llamacpp.source_dir`（`models/llamacpp/qwen3.8-flash-next.yaml`）：

```yaml
llamacpp:
  source_dir: /raid5/sh/code/llama.cpp-qwen38-flash-next
```

`source_dir` 留空时依次回退：环境变量 `LLAMACPP_SOURCE_DIR` → 默认主线。`modelctl` 会自动命中
`<副本>/build/bin/llama-server`，跳过主线编译，直接用 PR 分支二进制启动。

也可直接调用已安装的 `modelctl` 命令：

```bash
uv run modelctl start deepseek-v4-flash-llamacpp
```

### 3.5 指定 GPU（可选）

默认使用全部可见 GPU；以下三种方式可指定使用的 GPU 子集，按优先级从高到低：

- **Profile 字段**：在对应引擎段加 `gpu_list`，逗号分隔的 GPU 索引：

  ```yaml
  llamacpp:
    model: /path/to/model.gguf
    gpu_list: "0,1,2,3"   # 仅使用 GPU 0~3
  ```

- **CLI 参数**：`modelctl start <name> --gpus 0,1,2,3`（`restart` 与 `all start` 同样支持）。
- **环境变量**：`MODELCTL_GPUS=4,5 modelctl start <name>`。

三者都未设置时默认使用全部可见 GPU（保持旧行为）。

各引擎说明：

- vllm/sglang：`tensor_parallel_size`/`tp` 缺省时自动取 `len(gpu_list)`；若显式配置则必须等于 `len(gpu_list)`，否则报错。
- llamacpp：`--tensor-split` 数量 = `len(gpu_list)`。
- unsloth：开启 `tensor_parallel` 且指定 `gpu_list` 时需至少 2 块 GPU。
- ollama：`CUDA_VISIBLE_DEVICES` 限制的是常驻 `ollama serve` 进程可见的全部 GPU（所有 ollama 模型共享，无法按单模型隔离）。

冲突检测：启动前对选中的 GPU 做文件锁占用检查（data/cache/\*.gpu-lock），两个模型抢占同一张卡会在启动前报 `[gpu_lock] ... 已被模型 X 占用`；停止时自动释放。该机制为 best-effort。

严格校验：GPU 索引越界或重复会直接报错并提示可用范围。

### 4. 验证

```bash
curl http://127.0.0.1:18888/health   # deepseek-v4-flash-llamacpp
curl http://127.0.0.1:18890/health   # qwen3-coder-llamacpp
curl http://127.0.0.1:18891/health   # kimi-k2.5-llamacpp
curl http://127.0.0.1:11434/         # ollama 常驻服务（qwen3.8 / qwen3-coder / kimi-k2.5）
curl http://127.0.0.1:8100/health    # deepseek-v4-flash-vllm
curl http://127.0.0.1:8101/health    # qwen3.8-vllm
curl http://127.0.0.1:8102/health    # kimi-k2.5-vllm
curl http://127.0.0.1:8202/health    # kimi-k2.5-sglang
curl http://127.0.0.1:8001/v1/models -H "Authorization: Bearer $UNSLOTH_API_KEY"   # unsloth 无头 API（deepseek-v4-flash / qwen3-coder / kimi-k2.5）
curl http://127.0.0.1:8110/health    # qwen3.8-flash-next-vllm
curl http://127.0.0.1:8210/health    # qwen3.8-flash-next-sglang
curl http://127.0.0.1:8010/v1/models -H "Authorization: Bearer $API_KEY"   # qwen3.8-flash-next-unsloth
curl http://127.0.0.1:18909/health   # qwen3.8-flash-next-llamacpp
```

### 5. 停止 / 重启 / 状态

```bash
# 停止
bash script/modelctl.sh stop deepseek-v4-flash-llamacpp

# 重启（先停后启）
bash script/modelctl.sh restart deepseek-v4-flash-llamacpp

# 查看所有模型状态（含健康检查）
bash script/modelctl.sh status

# 列出所有 profile
bash script/modelctl.sh list

# 探测硬件与引擎二进制可用性
bash script/modelctl.sh probe
```

### 6. 用量统计服务

```bash
# 启动用量统计服务（/api/usage，cc-switch 兼容）
bash script/modelctl.sh stats start

# 停止用量统计服务
bash script/modelctl.sh stats stop
```

**token 统计显示**（cc-switch 卡片，已精简；金额由 `used`/`unit` 字段渲染，`extra` 为附加信息）：

```
已使用：0.40 CNY  累计 648.5k toks（输入 499.3k/输出 149.2k）| 输入速率 2127.8 tok/s| 输出速率 373 tok/s
```

- token 数量按 k/m/g 单位换算（`648,532` → `648.5k`），减少显示长度
- 仅保留：金额、累计 token 总量、输入/输出 token 数、输入/输出速率
- 已移除运行时间等非必要信息

**样式化显示**（两种形态均需在 profile 配置预算后生效）：

在模型 YAML 的 `usage` 段设置预算上限（元，可选）：

```yaml
# models/vllm/qwen3.8.yaml
usage:
  price_in: 0.5
  price_out: 1.0
  budget: 50   # 预算上限（元）；不设置则只显示已用金额
```

1. **自定义模板（默认）**：卡片内联显示 `已用 X.XX | 剩余 Y.YY CNY [extra]`，剩余额度带颜色（<10% 预算橙色告警，充足绿色，失效红色）。
2. **Token Plan 模板（百分比徽章，与官方订阅同款样式）**：用量查询选择内置「Token Plan」模板，把请求 URL 改为 `{{baseUrl}}/api/usage?model=<模型名>&view=tier`（聚合多模型用 `?model=all&view=tier`）。服务把各模型的预算消耗折算为百分比（0–100），cc-switch 按使用率渲染彩色徽章（<70% 绿 / 70–89% 橙 / ≥90% 红），未配置预算或模型不可用时返回 503，卡片显示失败态并可重试。nginx 无需改动（查询参数随 `/api/usage` 路由透传）。

cc-switch 推荐 extractor 片段：

```js
// ① 自定义模板（金额 + 附加信息模式）
({
  request: { url: "{{baseUrl}}/api/usage", method: "GET" },
  extractor: function(response) {
    if (!response || response.error || response.isValid === false) {
      return { isValid: false, invalidMessage: (response && (response.invalidMessage || response.error)) || "接口调用失败" };
    }
    return {
      isValid: true, used: response.used, remaining: response.remaining, total: response.total,
      unit: response.unit || "CNY", planName: response.planName, extra: response.extra
    };
  }
})
```

```js
// ② Token Plan 模板（百分比徽章模式；查询方式选内置「Token Plan」后把 URL 改为 ?view=tier）
({
  request: { url: "{{baseUrl}}/api/usage?model=all&view=tier", method: "GET" },
  extractor: function(response) {
    if (!Array.isArray(response)) {
      return { isValid: false, invalidMessage: (response && response.error) || "响应格式异常" };
    }
    return response; // 数组 → cc-switch 逐条渲染为彩色徽章
  }
})
```

查看日志（`LOG_DIR` 默认 = 项目根的 `data/logs/`，可用 `LOG_DIR` 改到别处）：

```bash
tail -f data/logs/launch-deepseek-v4-flash-llamacpp.log   # 最近一次启动日志
tail -f data/logs/modelctl.log                            # modelctl 自身运行日志
```

### 请求级审计

需要**单次请求**的 token 数 / 性能指标（TTFT, tps, queue time）时，启用本功能：

1. 在 `.env` 配置 `AUDIT_DIR`（默认项目根的 `data/audit`）、`AUDIT_RETENTION_DAYS`（默认 30）、
   `AUDIT_MAX_SIZE_MB`（默认 512）。
2. 在目标 vLLM profile 的 `vllm:` 段加：
   ```yaml
   enable_per_request_metrics: true
   enable_force_include_usage: true   # 保证流式末块回 usage
   ```
3. 重启该模型 + 网关（`modelctl restart <name> && modelctl gateway restart`）。
4. 查询 / 统计 / 清理：
   ```bash
   modelctl audit                                # 最近 20 条（表格）
   modelctl audit --model qwen3.8-vllm --limit 50
   modelctl audit --json | jq 'select(.source=="vllm_native")'
   modelctl audit stats                          # 目录统计
   modelctl audit --cleanup --dry-run            # 预览清理
   modelctl audit --cleanup                      # 执行清理
   modelctl audit path                           # 打印 AUDIT_DIR
   ```

**与 stats 服务的分工**：
- `modelctl stats`：趋势 / 聚合（每秒速率、累计），适合大盘监控
- `modelctl audit`：单次请求明细（TTFT, tps, 队列耗时），适合 debug / 审计

**注意**：
- 直连引擎端口的流量（绕过网关）**不**产生审计记录
- `endpoint` 字段值包括 `chat/completions` / `completions` / `embeddings` / `messages`
- 非 vLLM 引擎（llamacpp/sglang/ollama/unsloth）：`source=gateway_estimate`，`native_metrics` 为 null
- 流式不开 `--enable-force-include-usage` 时：token 数走聚合 collector 差分（`tokens_source=collector-diff`）

### 7. 多模型路由与统一网关

B 机 nginx 通过 URL 路径把请求路由到不同模型；同时提供按 `model` 参数的统一网关。

**访问地址**

| 方式 | baseUrl / URL | 说明 |
|---|---|---|
| 路径式直连 | `https://xxx:5000/210/llm/deepseek-v4-flash/v1` | cc-switch 每模型一张卡片 |
| 路径式直连 | `https://xxx:5000/210/llm/qwen3.8/v1` | 同上 |
| 路径式直连 | `https://xxx:5000/210/llm/qwen3-coder/v1` | 同上 |
| 路径式直连 | `https://xxx:5000/210/llm/kimi-k2.5/v1` | 同上 |
| 统一网关 | `https://xxx:5000/210/llm/v1` | body 里 `model=模型名` 切换；缺省/未知回退默认模型 |
| 用量查询 | `https://xxx:5000/210/llm/<模型名>/v1/api/usage` | cc-switch 用量卡片 |

> 网关转发请求时会把 `model` 参数改写为后端实际注册的模型名：vLLM / SGLang 启动时显式传入 `--served-model-name <profile.name>`（与 `/v1/models` 返回的 id 一致），因此无论经网关还是直连后端端口，请求体 `model` 都使用 `modelctl list` 显示的标识符（如 `qwen3.8-vllm`）。

**生成 nginx 注册表**

```bash
modelctl nginx-snippet --node 210 --host 192.168.77.210
```

输出 `map $uri $llm_model_target` 片段（含统一网关入口 `/<node>/llm/v1` → `GATEWAY_PORT`，以及各模型 name/alias 直连规则），上传到 B 机 `/etc/nginx/conf.d/` 并 `nginx -t && systemctl reload nginx`（`conf.d/*.conf` 已被 nginx.conf 默认 include，无需改主配置；完整示例见 `docs/nginx/llm-routing.example.conf`）。新增模型只需新增一条 profile，重新生成即可。

**启动/停止网关**

```bash
bash script/modelctl.sh gateway start    # 或 modelctl gateway start
modelctl gateway status
modelctl gateway stop
```

网关依赖 `fastapi/uvicorn/httpx` 已迁出主项目 lockfile，独立在 `gateway/` 子项目；
首次 `modelctl gateway start` 时自动通过 `uv sync --project gateway` 落到 `.venvs/gateway`，
无需手动 `uv sync`。如需手动重建：

```bash
modelctl env setup gateway
```

`.env` 中新增 `NODE_ID`、`NODE_HOST`、`GATEWAY_HOST`、`GATEWAY_PORT`、`GATEWAY_DEFAULT_MODEL`、`GATEWAY_READ_TIMEOUT`（见 `.env.example`）。

### 8. 一键启停（modelctl all）

`modelctl all` 把**默认模型 + 统一网关（gateway）+ Web 管理控制台（webui）+ 用量统计（stats）**四件套作为一个整体管理：

```bash
# 启动：默认模型 → gateway → webui → stats
modelctl all start

# 停止：stats → webui → gateway → 全部运行中模型（含非默认）
modelctl all stop

# 重启：仅默认模型停后启，gateway / webui / stats 重启
modelctl all restart

# 状态汇总：四件套逐项 [ok]
modelctl all status
```

**四动作语义**

- **start / restart** 仅操作默认模型：默认模型取 `GATEWAY_DEFAULT_MODEL`（profile 的 name 或其 alias），未设置回退 `deepseek-v4-flash`，也可用 `--model <name>` 临时指定；`--timeout` 控制模型健康检查超时（默认 300s）
- **stop** 除 webui / gateway / stats 外，会停止**全部运行中**的模型（包括经 `modelctl start <name>` 启动的非默认模型），避免遗留进程
- **status** 汇总四件套状态，恒 exit 0

单组件同样支持四动作：

```bash
modelctl gateway start|stop|restart|status
modelctl webui start|stop|restart|status
modelctl stats start|stop|restart|status
```

bash 薄脚本（等价于 `uv run modelctl all <动作>`）：

```bash
bash script/modelctl-all.sh start
bash script/modelctl-all.sh status
```

**失败语义**：逐组件尝试并汇总（某组件失败仍继续后续组件），任一组件 `[error]` 使 start / restart 返回 exit 2、stop 返回 exit 1（status 恒 exit 0）；可再 `modelctl status` 细查模型状态（网关/统计用 `modelctl gateway status` / `modelctl stats status`）。

### 9. Web 管理控制台（modelctl webui）

`modelctl webui` 启动 **单进程 FastAPI** 管理面，在原有 `/v1/*`（OpenAI 兼容代理，nginx 依赖）之外挂上 `/admin/api/*`（管理 API）与前端 SPA（项目根 `dist/`，Vue 3 构建产物）。**与 `modelctl gateway start` 是同一份 FastAPI 代码**（`gateway.py::create_app(admin=True)`）两个端口：gateway 专职数据面（5003），webui 兼管理面（4173），互不影响（端口、PID 文件、实例名独立）。

#### 9.1 使用命令

```bash
# 启动 Web UI（默认读 .env 的 WEBUI_HOST/WEBUI_PORT，缺省 127.0.0.1:4173）
modelctl webui start

# 命令行覆盖端口 / 绑定地址（--port/--host 优先于 .env）
modelctl webui start --port 8080 --host 0.0.0.0

# 停止 / 重启 / 状态（复用 gateway venv，无需额外依赖）
modelctl webui stop
modelctl webui restart
modelctl webui status

# 等价的前台独立运行（不经 start_detached，日志直接打到当前终端，便于调试；
# 需当前解释器可用 fastapi/uvicorn，即 gateway venv 或已装依赖的 uv 环境，
# 端口/地址用环境变量 WEBUI_HOST / WEBUI_PORT 控制）
python -m modelctl.core.webui.server
```

> `action` 是必填位置参数，只接受 `start` / `stop` / `restart` / `status`；缺省会报 `the following arguments are required: action`。

**前端环境自动处理**：`start` / `restart` 前会检查项目根 `dist/`，缺失时**自动补齐整条前端链路**——缺 Node.js 就用系统包管理器装（apt/dnf/yum/zypper 走 NodeSource 拿 Node 22，pacman/apk 直接装；需 root 或 sudo），缺 `web/node_modules` 就 `npm install`（默认走 npmmirror 镜像，用户已自配 `.npmrc` 的 registry 时不覆盖），随后 `npm run build` 产出 `dist/`。取向与 gateway venv 的自动搭建一致：目标是一条 `modelctl webui start` 直接可用。

```bash
# 交互终端默认自动处理；以下两个开关用于覆盖默认判断
modelctl webui start --build      # 强制自动安装/构建（非交互场景，如 CI、脚本内）
modelctl webui start --no-build   # 完全跳过，产物缺失也只启 /admin/api（仅 API 模式）
```

自动安装会改动机器状态且首次耗时较长，因此**非交互终端**（`ssh host 'modelctl webui start'`、cron、CI）默认只检测不安装，回一条可复制的手动命令清单；需要它在脚本里也自动装就显式加 `--build`。前端不可用**不阻断启动**：`/admin/api` 与 `/v1` 照常可用，仅浏览器访问根路径 404。

**端口优先级**：命令行 `--port` > `.env` 的 `WEBUI_PORT` > 代码默认 `4173`。与 `GATEWAY_PORT`（默认 5003）不冲突，可同时运行。

**登录**：浏览器访问控制台后，登录页只需输入 `.env` 中的 `API_KEY`（作为 Bearer Token），复用 modelctl 现有鉴权；无独立密码体系。SSE 流式端点（任务进度 / 模型日志）因浏览器 `EventSource` 无法携带 header，改用 `?key=<API_KEY>` query 传递同一令牌，鉴权强度与 Bearer 一致。

**依赖**：webui 与 gateway 完全共享 gateway venv（`fastapi / uvicorn / httpx`），首次 `modelctl webui start` 自动初始化落到 `.venvs/gateway`，无需重复 `env setup`。

**启动日志**：webui 子进程的 stdout / stderr 由 `start_detached` 重定向到 `log_dir()/launch-modelctl-webui.log`（与 model profile 启动日志同目录——`LOG_DIR` 环境变量决定，缺省项目根的 `data/logs/`，每次 start 覆盖不追加）。`modelctl webui status` 不读该文件，只探 PID + `/admin/api/health` 端口。

#### 9.2 前端构建与产物路径

前端源码在 `web/`（Vue 3 + Vite + TypeScript + UnoCSS），构建产物由 `web/vite.config.ts` 的 `build.outDir: '../dist'` 输出到**项目根 `dist/`**（非 `web/dist`）。`server.py::dist_dir()` 读的正是项目根 `dist/`，两端一致。

手动构建（与自动处理等价的命令，供排障或单独产出前端时使用）：

```bash
cd web
npm install          # 首次安装依赖
npm run build        # vue-tsc 类型检查 + vite build，产物落到 ../dist/
```

`dist/index.html` 缺失且未触发自动构建（`--no-build` 或非交互终端）时，webui 仍暴露 `/admin/api` 与 `/v1`，仅浏览器直连域名根会拿到 404；启动日志会提示 `（仅 /admin/api 可用；……）` 并附下一步命令。

#### 9.3 本地开发（前端热更新联调）

开发期不构建 `dist/`，改用 vite dev server：前端 5173，`/admin/api` 代理到后端 `WEBUI_PORT`（读同一份 `.env`，单一真值来源）。

```bash
# 终端 1：起后端管理 API（二选一）
modelctl webui start --no-build                   # 后台守护形态（--no-build 免去构建 dist/）
uv run python -m modelctl.core.webui.server       # 前台形态，日志直接可见，便于调试

# 终端 2：起前端 dev server（vite 默认 5173，host 0.0.0.0）
cd web
npm run dev
```

开发期页面由 vite 提供，`dist/` 用不上，加 `--no-build` 避免白跑一次构建（前台形态不经过自动处理，无需该参数）。

浏览器访问 `http://127.0.0.1:5173`，用 `.env` 的 `API_KEY` 登录即可联调。

> **注意**：vite 若发现 5173 被占用会静默改用其它端口（如 5174），联调前用 `netstat -ano | findstr :5173` 确认端口未被历史进程占用。

#### 9.4 管理 API 能力一览（`/admin/api`）

| 分组 | 端点 | 说明 |
|---|---|---|
| 会话 | `POST /login` · `GET /health` | 校验 API_KEY；健康探针（免鉴权） |
| 概览/体检 | `GET /overview` · `GET /probe` | 仪表板聚合快照；硬件与引擎体检 |
| 模型 | `GET /models` · `GET /models/{name}` · `GET /models/{name}/yaml` | 列表 / 详情 / YAML 原文 |
| 模型操作 | `POST /models/{name}/start\|stop\|restart` · `POST /models/{name}/ui/start\|stop` | 生命周期（start/restart 走异步任务返回 202） |
| 模型日志 | `GET /models/{name}/log` · `GET /models/{name}/log/stream` | 日志尾随（tail）/ SSE 实时流 |
| 任务 | `GET /tasks` · `GET /tasks/{id}/stream` | 长任务列表 / SSE 进度流 |
| 一键启停 | `POST /all/start\|stop\|restart` · `GET /all/status` | 四件套整体控制 |
| 服务 | `GET /services` · `POST /services/{svc}/{action}` | gateway / stats 状态与操作 |
| 环境 | `GET /envs` · `POST /envs/{target}/setup\|remove` | 引擎 venv 安装 / 卸载（setup 走异步任务） |
| 审计 | `GET /audit` · `GET /audit/stats` · `GET /audit/path` · `POST /audit/cleanup` | 请求级审计查询 / 统计 / 清理 |
| 配置 | `GET /config/static` · `GET /nginx-snippet` | 静态配置回显 / nginx 路由片段生成 |
| TensorRT | `POST /trtllm/{name}/build` · `GET /trtllm/{name}/status` | 引擎编译（异步任务）/ 状态 |

**与 gateway 进程的差异**：

| 维度 | `modelctl gateway start` | `modelctl webui start` |
|---|---|---|
| 模块 | `modelctl.core.gateway` | `modelctl.core.webui.server` |
| 实例名 / PID 文件 | `llm-gateway` | `modelctl-webui` |
| 端口 | `GATEWAY_PORT`（默认 5003） | `WEBUI_PORT`（默认 4173） |
| 挂载路由 | 仅 `/v1/*` | `/v1/*` + `/admin/api/*` + 前端 SPA |
| admin 标志 | `create_app(admin=False)` | `create_app(admin=True)` |

### 10. 分布式集群管理面（modelctl cluster）

适用场景：N 台服务器分布在 M 个局域网（N ≥ M），用**一个中心 webui** 统一查看/管理所有节点。取向与 torchrun 类似——每台机器照常跑自己的 `modelctl webui`，额外用一条 `cluster join` 命令注册到中心；但**没有 rank/rendezvous 概念**，中心故障不影响任何节点的本地推理与本地 webui。

> **当前范围（M0）**：节点注册、心跳判活（lease 三态）、节点视图（Web UI「集群节点」页 + `modelctl cluster nodes`）、令牌准入与吊销。跨机模型下发（goal 声明式同步）、集群级起停属 M1+，见设计文档。
>
> **数据面零改动**：推理流量仍按 nginx `/{node-id}/llm/*` 规则直连各节点端口，**不经过中心**。

#### 10.1 角色（CLUSTER_ROLE）

| 角色 | 本地跑模型 | 挂集群 API/WS | 节点台账 | 典型机器 |
|---|---|---|---|---|
| `solo`（默认） | ✅ | ❌（集群端点一律 404） | — | 未入集群的单机部署，行为与旧版完全一致 |
| `both` | ✅ | ✅ | ✅ | 中心机自己也跑推理（推荐中心用此值） |
| `control-plane` | ✅ | ✅ | ✅ | 纯中心（不强调跑模型） |
| `worker` | ✅ | ❌（只出站连中心 WS） | ❌ | 各 LAN 的工作节点 |

`.env` 集群键位见 [.env.example](.env.example) 末尾「集群」注释块（`CLUSTER_ROLE` / `CLUSTER_CENTER_URL` / `CLUSTER_NODE_ID` / `CLUSTER_LAN` / `CLUSTER_HEARTBEAT_INTERVAL_S=10` / `CLUSTER_LEASE_S=90` 等）。

#### 10.2 中心部署（机器 A）

```bash
# 1. .env 设角色并放行监听（默认 127.0.0.1 只有本机能访问，集群模式必须显式放开）
#    CLUSTER_ROLE=both
#    WEBUI_HOST=0.0.0.0
#    （建议用防火墙把 WEBUI_PORT 限制到已知 LAN 网段）

# 2. 初始化台账 + 生成 join token（幂等，重复执行复用现有 token 并打印）
modelctl cluster init
#  → 台账落 data/cache/cluster-meta.db；打印 JT-xxxx（发给各 worker 用）

# 3. 照常启动 webui
modelctl webui start

# 4. 查看集群摘要 / 节点表
modelctl cluster status          # 角色/在线计数
modelctl cluster nodes           # node_id/LAN/状态/心跳/token 尾号
```

浏览器登录中心控制台后，侧边栏「集群节点」页实时展示各节点状态（online/stale/offline）、引擎版本、心跳与租约倒计时。

#### 10.3 worker 加入（每台一条命令）

```bash
# 在 worker 的 modelctl 目录执行（token 来自中心 `modelctl cluster init` 的输出）
modelctl cluster join --center http://192.168.77.210:4173 --token JT-xxxx --node-id w-210 --lan lan-2
#  → 预检通过后才写 .env：CLUSTER_ROLE=worker / CLUSTER_CENTER_URL / CLUSTER_NODE_ID / CLUSTER_LAN / CLUSTER_NODE_TOKEN
#  → token 写错/中心不可达时不落盘，直接报错退出（exit 2）

# 之后照常启动本机 webui——启动时自动拉起后台 Agent（出站 WS 注册 + 周期心跳），无需额外进程
modelctl webui start
```

断线自愈：Agent 按 1→2→…→30s 指数退避重连；中心重启后所有 worker 自动重新注册，节点用 node_token 免 join_token 重连，**无需人工干预**。中心按租约判活：`lease_expiry`（默认 90s）过期 → `stale`；`last_seen` 超 3×lease → `offline`；期间该节点推理不受影响。

#### 10.4 令牌管理（仅中心本机）

```bash
modelctl cluster join-token                    # 查看当前 join token（脱敏尾号）
modelctl cluster join-token --rotate           # 轮换 join token（旧的立即失效；已入集群节点不受影响）
modelctl cluster join-token --rotate-node w-210  # 单独吊销/重发某节点令牌（该节点下次重连需重新 join）
```

信任链：`join token`（一次性准入）→ 中心为节点签发专属 `node_token`（自动写回 worker 的 .env，可单独吊销）→ 后续重连只用 node_token。集群 REST（`/admin/api/cluster/*`）走 webui 同款 `API_KEY` Bearer 鉴权。

#### 10.5 冒烟示例（两台机验证全链路）

```bash
# 中心（A 机）：
CLUSTER_ROLE=both modelctl cluster init && modelctl webui start
modelctl cluster status                # 期望: 角色: both 中心: True 节点: 0 online / 0 total

# worker（B 机）：
modelctl cluster join --center http://<A>:4173 --token JT-xxxx --node-id w-b1 --lan lan-2
modelctl webui start

# 中心侧 ≤ 心跳间隔(10s) 内：
modelctl cluster nodes                 # w-b1 状态 online，租约倒计时滚动
# kill 掉 B 机 webui 后：约 lease(90s) 后转 stale、3×lease 后转 offline；
# 重启 B 机 webui（无需重新 join）→ 自动回到 online，node_token 不变
```

## 文档

部署前置条件、目录布局、日志/停止/重启、参数速查等详见 [docs/DeepSeek-V4-Flash后台启动指南.md](docs/DeepSeek-V4-Flash后台启动指南.md)。

多模型 nginx 路由的部署与测试步骤详见 [docs/nginx/测试指南.md](docs/nginx/测试指南.md)（nginx 参考配置见 [docs/nginx/llm-routing.example.conf](docs/nginx/llm-routing.example.conf)）。

Web 管理控制台的完整设计（信息架构、页面清单、API 契约、SSE 事件协议）详见 [docs/superpowers/specs/2026-09-02-webui-design.md](docs/superpowers/specs/2026-09-02-webui-design.md)。

多机分布式管理面（单中心 + worker 注册/心跳判活/令牌准入，含竞品校准与 M0-M2 分期）详见 [docs/superpowers/specs/2026-09-03-modelctl-cluster-design.md](docs/superpowers/specs/2026-09-03-modelctl-cluster-design.md)；日常用法见上文「10. 分布式集群管理面」。

## 时区

`.env` 的 `TZ`（默认 `Asia/Shanghai`，仅 Linux/macOS 生效）统一以下时间的显示：

- modelctl 自身的 loguru 日志、审计记录 `ts` 与按天切分的审计文件名
- 引擎子进程（venv 形态的 vllm / sglang / llamacpp / ollama 等）与用量统计服务
- docker 形态的 vllm / tokenspeed / tensorrt_llm：自动注入 `-e TZ=`，宿主机能定位 tz 文件时再挂 `/etc/localtime`

**部署机仍需把系统时区设为东八区**，否则 modelctl 管不到的部分仍是 UTC：

```bash
sudo timedatectl set-timezone Asia/Shanghai
```

nginx 的 access/error log、logrotate、`docker logs` 与 `journalctl` 的时间戳都取**宿主机系统时区**，`TZ` 环境变量对它们无效。

Windows 开发机上 `TZ` 会被忽略（无 `time.tzset`，且 UCRT 会把 IANA 名误解析成 +0100 而污染子进程），请直接设置系统时区。

## 说明

- 模型级配置（模型路径、端口、并行度、量化、用量单价）在 `models/*.yaml` 中管理，全局配置（API 密钥、存储目录、日志目录、统计服务）在 `.env` 中管理
- `.env` 含 API 密钥等敏感信息，已加入 `.gitignore`，请勿提交
- 详细注意事项（KV cache 量化、DSpark 参数、NCCL 优化等）见上方文档

> **迁移说明**：
> - vllm / sglang 引擎已从主项目 `uv sync --extra vllm` 迁出，改用独立引擎 venv（`.venvs/<engine>/`）。原本执行 `uv sync --extra vllm` 的用户，请改为 `modelctl env setup vllm`（sglang 同理 `modelctl env setup sglang`），首次启动前会自动完成初始化并复用 `.venvs/<engine>/`。
> - gateway 的 fastapi/uvicorn/httpx 已从主项目 `uv sync --extra gateway` 迁出，独立在 `gateway/` 子项目（`.venvs/gateway/`）。原本执行 `uv sync --extra gateway` 的用户，请改为 `modelctl env setup gateway`；`modelctl gateway start` 在首次执行时会自动初始化。
> - 运行时数据目录默认统一到项目根 `data/`（`logs` / `cache` / `usage-data` / `audit`，见 `src/modelctl/core/paths.py`）。**显式配置过** `LOG_DIR` / `CACHE_DIR` / `USAGE_DATA_DIR` / `AUDIT_DIR` 的机器不受影响；未配置且想保留历史数据的机器手工迁移：
>   ```bash
>   mkdir -p data/logs data/usage-data
>   mv ../logs/* data/logs/ 2>/dev/null                 # 历史运行/启动日志（旧默认在项目根上级）
>   mv data/cache/*.json data/usage-data/ 2>/dev/null   # 已积累的用量累计（旧默认与 cache 同目录）
>   ```
>   **切换前先停服务**：PID 文件写在旧 `CACHE_DIR`，直接改配置会让 `modelctl status` 误报已停止、`all stop` 停不掉、再 start 撞端口。旧目录有 PID 时先用 `$env:CACHE_DIR=<旧目录>`（Linux 用 `CACHE_DIR=<旧目录>`）逐个 `stats/gateway/webui stop` 再改。

> **部署前必做**：`pyproject.toml` 自 vllm extra 迁出后 `uv.lock` 重新解析过，而仓库内 `uv.lock` 与部署机实际解析（Linux + CUDA 13 平台差异）存在差异。**部署到 Linux CUDA 机器前，务必在目标机器上重新执行 `uv lock` + `uv sync`**，由目标平台完成最终解析，避免直接沿用开发机（Windows）生成的锁文件。
