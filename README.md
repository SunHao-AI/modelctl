# modelctl — 多模型部署启动器

在 CUDA/Ada GPU 上通过**引擎插件式架构**启动多种模型服务（llamacpp / ollama / vllm / sglang / unsloth），每模型一个 YAML profile，统一 CLI 管理启动、停止、重启、状态与用量统计。

## 特性

- **多引擎支持**：llamacpp（官方 llama.cpp + DSpark 投机解码）、ollama、vllm、sglang、unsloth（无头 API 服务，Unsloth 动态量化 GGUF）
- **YAML profile**：每模型一个 YAML（`models/<engine>/<name>.yaml`），配置模型路径、端口、引擎参数、用量单价
- **自动下载**：model 为空/不存在时从 ModelScope 自动下载，并把本地路径持久化写回 YAML（备份 .yaml.bak）
- **能力探测与自动降级**：启动前探测 GPU/CC/显存/引擎二进制，硬性不满足拒绝启动并说明原因，可降级项自动降级并告警
- **统一生命周期**：后台启动、PID 管理、健康检查、优雅停止
- **用量统计**：`/api/usage` 输出与 cc-switch 兼容，支持多模型按 `?model=` 路由
- **配置外置**：全局配置通过 `.env` 管理，模型级配置通过 profile YAML 管理

## 目录结构

```
modelctl/
├── README.md                       # 本文档（入口）
├── docs/
│   └── DeepSeek-V4-Flash后台启动指南.md   # 部署与运维详细指南
├── src/modelctl/
│   ├── cli.py                      # 统一 CLI 入口（start/stop/restart/status/list/probe/stats）
│   ├── __main__.py                 # python -m modelctl 入口
│   ├── core/                       # 核心模块：envfile / profile / capabilities / process / stats
│   ├── engines/                    # 引擎适配器：base / llamacpp / ollama / vllm / sglang / unsloth
│   └── py.typed                    # PEP 561 类型标记
├── script/
│   ├── modelctl.sh                 # bash 薄封装（调用已安装的 modelctl 命令）
│   └── modelctl-all.sh             # bash 薄封装（modelctl all 一键启停）
├── models/                         # 模型 profile（每模型一个 YAML，按引擎分目录）
│   ├── llamacpp/                   # llamacpp 引擎 profile 子目录
│   │   ├── deepseek-v4-flash.yaml  # DeepSeek-V4-Flash（llamacpp + DSpark）
│   │   ├── qwen3.8.yaml            # Qwen3.8-27B GGUF（llamacpp）
│   │   ├── qwen3-coder.yaml        # Qwen3-Coder-480B MoE GGUF（llamacpp，8 卡全量）
│   │   └── kimi-k2.5.yaml          # Kimi-K2.5 120B dense GGUF（llamacpp）
│   ├── ollama/                     # ollama 引擎 profile 子目录
│   │   ├── deepseek-v4-flash.yaml  # DeepSeek-V4-Flash（ollama）
│   │   ├── qwen3.8.yaml            # Qwen3.8-27B（ollama）
│   │   ├── qwen3-coder.yaml        # Qwen3-Coder-480B（ollama）
│   │   └── kimi-k2.5.yaml          # Kimi-K2.5（ollama）
│   ├── vllm/                       # vllm 引擎 profile 子目录
│   │   ├── deepseek-v4-flash.yaml  # DeepSeek-V4-Flash（vllm）
│   │   ├── qwen3.8.yaml            # Qwen3.8-27B（vllm）
│   │   └── kimi-k2.5.yaml          # Kimi-K2.5（vllm；qwen3-coder 无此变体：HF 权重超总显存）
│   ├── sglang/                     # sglang 引擎 profile 子目录
│   │   ├── deepseek-v4-flash.yaml  # DeepSeek-V4-Flash（sglang）
│   │   ├── qwen3.8.yaml            # Qwen3.8-27B（sglang）
│   │   └── kimi-k2.5.yaml          # Kimi-K2.5（sglang；qwen3-coder 同上）
│   └── unsloth/                    # unsloth 引擎 profile 子目录
│       ├── deepseek-v4-flash.yaml  # DeepSeek-V4-Flash（unsloth）
│       ├── qwen3.8.yaml            # Qwen3.8-27B（unsloth）
│       ├── qwen3-coder.yaml        # Qwen3-Coder-480B（unsloth，多卡 GGUF 分片）
│       └── kimi-k2.5.yaml          # Kimi-K2.5（unsloth）
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

## 快速开始

### 1. 安装依赖

- Python 3.12+、PyYAML
- `git`、`cmake`、CUDA 工具链、`nvidia-smi`（llamacpp 引擎编译用）
- 各引擎二进制：`ollama` / `vllm` / `sglang`（按需安装）

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

下载成功后，本地路径会**持久化写回** profile YAML 的 `model` 字段（原文件备份为 `.yaml.bak`），
下次启动直接复用本地模型，无需重复下载。

环境变量 `MODEL_ROOT` 控制下载目录（默认：项目根目录上级的 `model-gguf/` 或 `model-hf/`）。

### 2.6 查看可用模型目录

`modelctl list` 按模型家族（group）分组展示全部可用 profile，含引擎、变体、端口与运行状态：

```bash
uv run modelctl list
```

输出示例（节选）：

```
deepseek-v4-flash（10 配置）
引擎      变体   端口   状态    标识符
--------  -----  -----  ------  --------------------------------
vllm      -      8100   已停止  deepseek-v4-flash-vllm
vllm      high   8103   已停止  deepseek-v4-flash-vllm-high
vllm      light  8104   已停止  deepseek-v4-flash-vllm-light
vllm      pp     8106   已停止  deepseek-v4-flash-vllm-pp
sglang    -      8200   已停止  deepseek-v4-flash-sglang
...
```

组内按引擎优先级排序（vllm 优先，与网关家族路由一致），默认变体（`-`）在前。

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
# 模型会下载到 .env 中 MODEL_ROOT 指定的位置，下载后路径自动写回 profile YAML
bash script/modelctl.sh start qwen3.8-llamacpp

# 启动 DeepSeek-V4-Flash（unsloth 无头 API，Unsloth 动态量化 GGUF）
# 模型从 ModelScope 下载并写回 profile；api_key 取 .env 中 UNSLOTH_API_KEY
bash script/modelctl.sh start deepseek-v4-flash-unsloth
```

每个引擎子目录均提供 **deepseek-v4-flash**、**qwen3.8**、**qwen3-coder**、**kimi-k2.5** 带注释的示例配置，便于学习各引擎参数（qwen3-coder 因 HF 权重超出本机总显存，无 vllm/sglang 变体）。按引擎启动示例：

```bash
# llamacpp：DeepSeek-V4-Flash（DSpark 投机解码）/ Qwen3.8-27B GGUF / Qwen3-Coder-480B / Kimi-K2.5
bash script/modelctl.sh start deepseek-v4-flash-llamacpp
bash script/modelctl.sh start qwen3.8-llamacpp
bash script/modelctl.sh start qwen3-coder-llamacpp
bash script/modelctl.sh start kimi-k2.5-llamacpp

# ollama：DeepSeek-V4-Flash / Qwen3.8-27B / Qwen3-Coder-480B / Kimi-K2.5（ollama pull 自动拉取）
bash script/modelctl.sh start deepseek-v4-flash-ollama
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
```

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

查看日志（LOG_DIR 默认 = 项目根目录上级的 `../logs/`）：

```bash
tail -f ../logs/launch-deepseek-v4-flash-llamacpp-*.log   # 最近一次启动日志
```

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

**生成 nginx 注册表**

```bash
modelctl nginx-snippet --node 210 --host 192.168.77.210
```

输出 `map $uri $llm_model_target` 片段，上传到 B 机 `/etc/nginx/conf.d/` 并 `nginx -t && systemctl reload nginx`（`conf.d/*.conf` 已被 nginx.conf 默认 include，无需改主配置；完整示例见 `docs/nginx/llm-routing.example.conf`）。新增模型只需新增一条 profile，重新生成即可。

**启动/停止网关**

```bash
bash script/modelctl.sh gateway start    # 或 modelctl gateway start
modelctl gateway status
modelctl gateway stop
```

网关依赖 `fastapi/uvicorn/httpx`（optional extra）：

```bash
uv sync --extra dev --extra gateway
```

`.env` 中新增 `NODE_ID`、`NODE_HOST`、`GATEWAY_HOST`、`GATEWAY_PORT`、`GATEWAY_DEFAULT_MODEL`、`GATEWAY_READ_TIMEOUT`（见 `.env.example`）。

### 8. 一键启停（modelctl all）

`modelctl all` 把**默认模型 + 统一网关（gateway）+ 用量统计（stats）**三件套作为一个整体管理：

```bash
# 启动：默认模型 → gateway → stats
modelctl all start

# 停止：stats → gateway → 全部运行中模型（含非默认）
modelctl all stop

# 重启：仅默认模型停后启，gateway / stats 重启
modelctl all restart

# 状态汇总：三件套逐项 [ok]
modelctl all status
```

**四动作语义**

- **start / restart** 仅操作默认模型：默认模型取 `GATEWAY_DEFAULT_MODEL`（profile 的 name 或其 alias），未设置回退 `deepseek-v4-flash`，也可用 `--model <name>` 临时指定；`--timeout` 控制模型健康检查超时（默认 300s）
- **stop** 除 gateway / stats 外，会停止**全部运行中**的模型（包括经 `modelctl start <name>` 启动的非默认模型），避免遗留进程
- **status** 汇总三件套状态，恒 exit 0

单组件同样支持四动作：

```bash
modelctl gateway start|stop|restart|status
modelctl stats start|stop|restart|status
```

bash 薄脚本（等价于 `uv run modelctl all <动作>`）：

```bash
bash script/modelctl-all.sh start
bash script/modelctl-all.sh status
```

**失败语义**：逐组件尝试并汇总（某组件失败仍继续后续组件），任一组件 `[error]` 使 start / restart 返回 exit 2、stop 返回 exit 1（status 恒 exit 0）；可再 `modelctl status` 细查模型状态（网关/统计用 `modelctl gateway status` / `modelctl stats status`）。

## 文档

部署前置条件、目录布局、日志/停止/重启、参数速查等详见 [docs/DeepSeek-V4-Flash后台启动指南.md](docs/DeepSeek-V4-Flash后台启动指南.md)。

多模型 nginx 路由的部署与测试步骤详见 [docs/nginx/测试指南.md](docs/nginx/测试指南.md)（nginx 参考配置见 [docs/nginx/llm-routing.example.conf](docs/nginx/llm-routing.example.conf)）。

## 说明

- 模型级配置（模型路径、端口、并行度、量化、用量单价）在 `models/*.yaml` 中管理，全局配置（API 密钥、存储目录、日志目录、统计服务）在 `.env` 中管理
- `.env` 含 API 密钥等敏感信息，已加入 `.gitignore`，请勿提交
- 详细注意事项（KV cache 量化、DSpark 参数、NCCL 优化等）见上方文档
