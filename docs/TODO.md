# modelctl 待开发清单

> 本文档梳理当前项目**未完成/部分实现/待验证**的功能点，并按优先级给出后续开发指引。
> 基于代码静态分析生成（2026-09-01）。涉及行号会随代码演进变化，使用时以文件为准。
>
> **整体判断**：项目主链路（CLI、profile 编排、网关代理、用量统计、GPU 管理、模型下载、审计）**主体已打通且可用**，测试底座完整（35 个文件 / 557 个用例 / CI 含 ruff+mypy+pytest）。真正的待开发集中在「能力外延」：新引擎补全、非 vLLM 原生统计、自动编译、更通用的显存预估、跨平台托管环境、更细的引擎级测试。
> 项目里**几乎没有显式 `TODO`/`FIXME`**，所以以下条目都是「部分实现/降级/缺口」性质，而非未接线的空桩。

---

## 0. 引擎优先级分层（路线规划）

已有 9 个引擎全部注册并实现适配器（`src/modelctl/core/profile.py` 的 `KNOWN_ENGINES`、`src/modelctl/engines/__init__.py` 的 `_REGISTRY`、`src/modelctl/core/capabilities.py` 的 `ENGINE_BINARIES` 三者完全一致）。

| 优先级 | 定位 | 引擎 | 状态 |
|---|---|---|---|
| **已集成（路线 A / 高）** | 生产 GPU 高性能四件套 | aphrodite、lmdeploy、tensorrt_llm、tokenspeed | 适配器已落地（venv 为主，部分 docker），但补全度参差，见 §1/§3 |
| **已集成（早期）** | 通用/入门/生态 | vllm（最完整）、sglang、llamacpp、ollama、unsloth | 基本可用，vllm 满分链路，其余见 §2 |
| **中优先级：硬件/场景特化** | 针对特定硬件/场景的引擎 | ktransformers、MLX/mlx-lm、mistral.rs、DeepSpeed-MII/FastGen | 均未集成，见下表，按需立项 |
| **低优先级 / 观望** | 生态未稳/已归档/定位重叠 | TGI、MLC-LLM、ik_llama.cpp | 持续跟踪，不主动投入，见下表 |

> 说明：「中优先级/低优先级」两档来自早期引擎选型分层原表（成文于本文档）。下表所列引擎**当前都没有已注册的适配器**，属于「可选新增」而非「缺失修复」，落地时需走 §5 标准流程。

### 高优先级：补齐主流生产/量化短板（已全部集成，补全度见 §1/§2/§3）

| 引擎 | 定位 | 与现有线的关系 | 集成复杂度 | 建议优先级 |
|---|---|---|---|---|
| **TensorRT-LLM** | NVIDIA 极致性能，编译式推理 | 与 vLLM 同层，但走编译引擎路线 | 中-高（需处理 engine 编译缓存、28min 冷启动） | ★★★★★ |
| **LMDeploy** | InternLM 团队 TurboMind C++ 引擎 | 对标 vLLM/SGLang，INT4/单卡量化强 | 中 | ★★★★ |
| **Aphrodite Engine** | vLLM fork，量化格式最全 | 与 vLLM 命令行高度兼容 | 低-中 | ★★★ |
| **TokenSpeed** | 面向 Agentic 负载的新兴引擎，MIT 开源 | 2026 新引擎，Qwen3.5 上性能突出 | 中（文档/接口仍在演进） | ★★★ |

> 这 4 个均已落地适配器（`src/modelctl/engines/{tensorrt_llm,lmdeploy,aphrodite,tokenspeed}.py`），但补全度参差（缺下载/速率 gauge/docker 等），具体补全项见 §1、§2、§3。

### 中优先级：硬件/场景特化（未集成）

| 引擎 | 定位 | 适用场景 | 集成复杂度 |
|---|---|---|---|
| **ktransformers** | 消费级 GPU 跑超大 MoE | 单卡 24GB 跑 DeepSeep/Kimi/Qwen3-235B | 中（需处理 CPU/GPU 混合推理参数） |
| **MLX / mlx-lm** | Apple Silicon 原生 | M 系列 Mac 本地/边缘部署 | 低-中 |
| **mistral.rs** | Rust 多模型引擎 | 安全/水印/长上下文场景 | 中 |
| **DeepSpeed-MII / FastGen** | 微软分布式推理 | ZeRO 推理、长序列 | 中-高 |

> 这 4 个均未集成。若立项：MLX 锁 Apple Silicon 平台（`os.uname().machine == "arm64"` + macOS）、ktransformers/mistral.rs/FastGen 走 §5 插件式适配器标准流程；建议按「实际硬件/场景刚需」排序，不主动批量集齐。

### 低优先级 / 观望

| 引擎 | 原因 |
|---|---|
| **TGI** | 2026-03 已进入 maintenance/archived 模式，Hugging Face 官方推荐迁移到 vLLM/SGLang/llama.cpp/MLX |
| **MLC-LLM** | 更偏向移动端/Web 部署，与当前服务器端定位重合度低 |
| **ik_llama.cpp** | llama.cpp fork，差异点（MTP、MoE kernel）未来可能被 mainline 吸收 |

> 上述均**不主动投入**，仅持续跟踪生态变化（如 TGI 是否重启维护、ik_llama.cpp 是否并入上游）。

---

## 1. 网关缺口（代码层面，优先级最高的登记项）

### 1.1 [高] `ENGINE_PRIORITY` 未登记 4 个新引擎
- **位置**：`src/modelctl/core/gateway.py:50`
- **现状**：`ENGINE_PRIORITY = {"vllm":0, "sglang":1, "unsloth":2, "ollama":3, "llamacpp":4}`，缺 `aphrodite`/`lmdeploy`/`tensorrt_llm`/`tokenspeed`，家族路由时走兜底 `99`。
- **影响**：当一个 family 内同时存在多个引擎候选时，4 个新引擎几乎永远排在最后，影响自动路由可用性。
- **建议**：按引擎「成熟度 + 吞吐 + 混合注意力支持」补 4 个条目，保守排在 `unsloth(2)` 之后、`llamacpp(4)` 之前。例如：`aphrodite:5, tokenspeed:6, lmdeploy:7, tensorrt_llm:8`（数值可再议）。补测试：`tests/test_gateway.py` 增加 family 多引擎排序用例。

### 1.2 [中] thinking / reasoning_effort 策略硬编码
- **位置**：`src/modelctl/core/gateway.py:57-59`（`_THINKING_DISABLED_GROUPS`/`_THINKING_DISABLED_ENGINES`）与 `:65-73`（`_REASONING_EFFORT_MAP`）
- **现状**：`enable_thinking=false` 的 group 白名单写死 `{"qwen3.8"}`，注入白名单写死 `{"vllm","sglang","unsloth"}`；`reasoning_effort` 映射为静态 dict（只适配 vLLM 0.27 的 `xhigh/medium/low`）。
- **影响**：换模型族 / 换 vLLM 大版本时需改源码，无法配置驱动。
- **建议**：把这些策略抽到 profile 的可选字段（如 `gateway.thinking_disabled`、`gateway.engine_reasoning_map`），源码侧只留默认值；补配置化 + 默认回退的测试。

### 1.3 [中] 非 vLLM 引擎缺少原生 per-request 指标回传
- **位置**：`src/modelctl/engines/base.py:56-63`（`native_metrics_mapping` 默认 `None`），仅 `vllm.py:215` 实现。
- **影响**：`stats` 对非 vLLM 引擎只能退化到 totals/window diff，per-request 精确归因只对 vLLM 生效。
- **建议**：对 sglang/aphrodite/tokenspeed 逐个验证其 `/metrics` 是否暴露 `vllm:tokens_*` 等周期 counter；有则补 `metrics_mapping` 的 `prompt_total/predicted_total` + 速率 gauge，接入 stats collector；无则在接受降级的前提下文档化。

---

## 2. 引擎适配器补全

### 2.1 stats / metrics 完成度对照

| 引擎 | venv | docker | 下载 | metrics 总量 | 速率 gauge | native per-request | UI |
|---|---|---|---|---|---|---|---|
| vllm | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| sglang | ✅ | — | ✅ | ✅ | — | — | — |
| llamacpp | — | — | ✅(GGUF) | ✅ | ✅ | — | — |
| ollama | — | — | 部分 | ❌(None) | ❌ | — | — |
| unsloth | — | — | ✅ | ❌(None) | ❌ | ✅?(未验证) | ✅ |
| aphrodite | ✅ | — | ❌ | ✅ | ✅ | — | — |
| lmdeploy | ✅ | — | ❌ | ✅ | ✅ | — | — |
| tokenspeed | ✅ | ✅ | ✅ | ✅ | ❌ | — | — |
| tensorrt_llm | ✅ | ✅ | ❌ | ✅ | ❌ | — | — |

### 2.2 待办

- **[高] unsloth `/metrics` 端点未验证** — `src/modelctl/engines/unsloth.py:86-87,180-181`。`metrics_mapping()` 返回 `None`，注释明确「暂未验证 `/metrics` 端点，降级为不支持精确统计」。先实测端点是否存在；存在则补映射，不存在则固化降级说明。
- **[高] tensorrt_llm 首跑不自动编译** — `src/modelctl/engines/tensorrt_llm.py:74-83`。`engine_dir` 为空时只 warning，不自动 `trtllm-build`（设计有意避免 28min 阻塞 `start`）。建议提供独立 `modelctl trtllm build` 子命令 + 在 `probe` 中明确提示缺 engine_dir，让首次部署体验闭环。
- **[中] tokenspeed 缺速率 gauge** — `src/modelctl/engines/tokenspeed.py:136-140`。verify 其 Prometheus 是否暴露 `tokenspeed_tokens_*rate`/`gpu_utilization`，补 `prompt_rate/predicted_rate`。
- **[中] tensorrt_llm 缺速率 gauge** — 同上，`tensorrt_llm.py:142-146`。
- **[中] aphrodite/lmdeploy 缺下载与 VRAM 预估** — `aphrodite.py`/`lmdeploy.py` 的 `pre_start` 无下载分支，依赖本地已有模型；接入 `engines/_download.py` 与 `vram_estimator`。
- **[中] 仅 3 个引擎支持 docker 运行时（vllm/tokenspeed/tensorrt_llm）** — aphrodite/lmdeploy/sglang 仅 venv。是否需要 docker 取决于 CI/隔离需求，可选。
- **[低] gpu memory 去重**：`engine_config["gpu_memory_utilization"]` 在 `vllm.py:164` 与 `sglang.py:91,106` 重复，可提取为基类辅助。

---

## 3. 核心链路加固

| 优先级 | 位置 | 缺口 |
|---|---|---|
| **高** | `src/modelctl/engines/_download.py:36-46` | 下载模块测试仅 1 个；真实路径/失败回退/幂等/模型大小预检均无覆盖，影响多引擎冷启动 |
| **高** | `src/modelctl/core/envs.py:112-120` | 托管引擎 venv 仅 Linux（`_is_linux_managed` 抛 `EngineEnvError`）。Windows 目标明确则补齐；否则在 CLI/文档显式声明平台限制 |
| **中** | `src/modelctl/core/vram_estimator.py:106-139` | KV 预检仅覆盖 `llamacpp/vllm/sglang/ollama`，`unsloth` 等 `return None`；内置 `KNOWN_MODEL_ARCHS` 仅 `qwen3.8-27b`（`vram_estimator.py:48-50`）。补新引擎 + 本地 `config.json` 自动识别更多架构 |
| **中** | `src/modelctl/core/compat.py:234-255` | CUDA 库解析依赖 `ldconfig`，非 Linux/windows 下退化为「视为不满足」。补 nvidia-smi 直读 fallback |
| **中** | `src/modelctl/core/stats.py:268-289` | `build_tier_item` 强依赖 `usage.budget`，无 budget 直接 `raise ValueError`。若要「零配置看 tier」需补默认/降级策略 |
| **低** | `src/modelctl/core/ufw.py:20-28` | UFW 放行仅服务 `modelctl ui`（unsloth），未泛化到其他控制台/gateway 端口 |
| **低** | `src/modelctl/core/nginx_snippet.py` | nginx 片段只覆盖 map 生成，缺 proxy 头/upstream 高级策略 |
| **低** | `src/modelctl/core/all_service.py:51,284` | `DEFAULT_MODEL_ID="deepseek-v4-flash"` 硬编码；`start_all` 只起默认 profile 而 `stop_all` 停全部，start/stop 语义不对称。建议配置化 + 加 CLI `--all` 显式开关 |

---

## 4. 测试覆盖补强

现有底座完整（557 用例），缺口集中在**新引擎 + 下载 + 部分 core 工具**：

| 优先级 | 测试文件 / 模块 | 现状 | 建议 |
|---|---|---|---|
| 高 | `tests/test_engines_download.py` | 仅 1 用例 | 扩到覆盖 `ensure_modelscope`、路径回退、失败不阻断、大小预检、幂等复用 |
| 高 | `tests/test_engines_{tokenspeed,lmdeploy}.py` | 各 4 用例 | 补 pre_start、stop_patterns、GPU 越界/冲突、持久化、docker 边车 |
| 高 | `tests/test_engines_aphrodite.py` | 6 用例 | 补 GPU 冲突、pre-start、下载/预检路径 |
| 中 | `tests/test_core_capabilities.py` | ~20 用例 | 补 nvidia-smi 异常行、托管 venv 存在性组合、binary/executable 不一致 |
| 中 | `tests/test_engine_native_metrics.py` | 4 用例 | 补 native mapping 消费端（stats collector 注入路径）集成 |
| 中 | `tests/test_core_deps.py` | 8 mock 用例 | 加真实安装集成（隔离临时 venv） |
| 中 | `tests/test_compat_flow.py` | 5 用例 | 扩其他引擎的 compat 组合 |
| 低 | `tests/test_envfile.py` / `test_nginx_snippet.py` / `test_gateway_context_switch.py` | 各 4-9 用例 | 边界/非法格式/多 profile 重整 |

---

## 5. 新增引擎的标准流程（落地指引）

新增任意 §0 候选引擎时，遵循**插件式最小改动**约定（改 6 处，无需动 CLI/网关/统计核心）：

1. [src/modelctl/core/profile.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/profile.py) 的 `KNOWN_ENGINES` 加引擎名。
2. 新建 `src/modelctl/engines/<name>.py`，实现 `EngineAdapter` 子类的 `build_command()`；可选覆写 `check_requirements`/`metrics_mapping`/`validate_gpu_selection`/`stop_patterns`/`pre_start`。
3. [src/modelctl/engines/__init__.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/engines/__init__.py) 的 `_REGISTRY` 注册。
4. [src/modelctl/core/capabilities.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/capabilities.py) 的 `ENGINE_BINARIES` + `ENGINE_INSTALL_HINTS` 同步。
5. [src/modelctl/core/envs.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/envs.py) 的 `MANAGED_ENGINES`（若托管 venv）+ 新建 `envs/<name>/pyproject.toml`。
6. [src/modelctl/core/gateway.py:50](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/gateway.py#L50) 的 `ENGINE_PRIORITY` 排序（§1.1 一并补齐）。
7. 配套：`models/<name>/<model>.yaml` 示例 + `tests/test_engines_<name>.py`（先写失败测试 → 最小实现 → 全绿）。
8. 文档：更新本文件的引擎表与优先级层。

TDD 要求与既有引擎一致（参考 `tests/test_engines_vllm.py` 体量）。

---

## 6. 按优先级汇总（Top 12）

| # | 优先级 | 项 | 位置 |
|---|---|---|---|
| 1 | 高 | `ENGINE_PRIORITY` 补 4 新引擎 | `gateway.py:50` |
| 2 | 高 | unsloth `/metrics` 端点验证 | `engines/unsloth.py:180` |
| 3 | 高 | tensorrt_llm 自动编译子命令 | `engines/tensorrt_llm.py:74` |
| 4 | 高 | `engines/_download.py` 测试加固 | `tests/test_engines_download.py` |
| 5 | 高 | 托管 venv Windows 支持 / 平台声明 | `core/envs.py:112` |
| 6 | 中 | 非 vLLM 引擎原生 per-request 指标 | `engines/base.py:56` |
| 7 | 中 | tokenspeed/tensorrt_llm 速率 gauge | 各自 `metrics_mapping` |
| 8 | 中 | vram_estimator 扩大引擎覆盖 | `core/vram_estimator.py:106` |
| 9 | 中 | thinking/reasoning 策略配置化 | `gateway.py:57,65` |
| 10 | 中 | aphrodite/lmdeploy 下载+VRAM 接入 | 各自 `pre_start` |
| 11 | 中 | 新引擎（tokenspeed/lmdeploy/aphrodite）测试加固 | `tests/test_engines_*` |
| 12 | 低 | `compat.py` ldconfig 非 Linux fallback | `core/compat.py:234` |
