# 能力检测框架（硬件 + 软件）设计

- 日期：2026-08-19
- 状态：待评审
- 范围：modelctl 各引擎启动前的能力检测（硬件兼容性 + 软件/环境/依赖）

## 1. 背景与动机

`modelctl start deepseek-v4-flash-vllm` 在 8×RTX 5880 Ada（CC 8.9）服务器上踩了一连串问题：

1. `libcudart.so.13` 找不到 —— LD_LIBRARY_PATH 只指向 CUDA 12.8，未指向 CUDA 13 运行库
2. `torch_list_size` undefined symbol —— vllm 0.27.1 硬性要求 `torch==2.13.0`，环境里却是 2.9.1（ABI 不匹配）
3. `No module named 'xgrammar.openai_tool_call_schema'` —— xgrammar 版本过旧
4. `libcudnn.so.9` 等库缺失 —— nvidia 包"空壳"（wheel 元数据存在但 .so 文件未落盘）
5. `Unsupported architecture` —— DeepSeek-V4 的 mHC 层依赖 DeepGEMM `tf32_hc_prenorm_gemm` 内核，官方仅提供 SM90（Hopper）/SM100（Blackwell DC）实现，Ada（sm_89）不支持

其中 1-4 是软件/环境问题，5 是硬件架构不兼容。这些都在引擎 import / 启动阶段才暴露，故障时间长达数分钟（健康检查超时 300s），且报错晦涩。

目标：在**启动前**通过静态检测（硬件快照 + 软件元数据/文件检查）提前拦截不兼容组合，给出清晰原因与修复建议。

## 2. 决策记录

| 决策点 | 结论 |
|---|---|
| 规则载体 | 内置规则库（代码维护，可单测），非 yaml 声明式 |
| 检测时机 | 两段式：`check_requirements` 按 id 特征预检 + `pre_start` 下载完成后读 config.json 精检 |
| 覆盖范围（第一阶段） | 框架 + 现有规则重构（vllm/sglang 的 DeepSeek-V4 与 FP8、软件依赖规则）；llamacpp/unsloth/ollama 接入软件环境规则 |
| 行为分级 | 两级：block（RequirementError，exit 2）/ degrade（warning 继续） |
| 软件检测深度 | 静态元数据 + 文件检查（不导入引擎、不做子进程冒烟） |

## 3. 架构与模块

新增 `src/modelctl/core/compat.py`，六个核心类型，全部无副作用、可单测：

### 3.1 GpuSpec（硬件能力快照）

| 字段 | 说明 |
|---|---|
| `cc` | compute capability 字符串，如 "8.9" |
| `cc_major` | CC 主版本 int（"8.9"→8）；无法解析为 None |
| `arch_family` | 架构家族：8=Ampere/Ada、9=Hopper、10=Blackwell、12=Blackwell-Consumer、unknown |
| `gpu_count` / `gpu_name` / `vram_total_mb` / `vram_free_mb` | 直接取自 Capabilities |

工厂：`GpuSpec.from_caps(Capabilities)`

### 3.2 EnvSpec（软件/环境快照）

| 字段 | 说明 |
|---|---|
| `packages` | `importlib.metadata` 已装包版本 dict |
| `wheel_requires` | 指定 wheel（如 vllm）METADATA 的 `Requires-Dist` 映射 |
| `nvidia_so` | venv 下 nvidia 包目录中实际存在的 .so 文件集合 |
| `cuda_libs_resolvable` | `ldconfig -p` + LD_LIBRARY_PATH 可解析的关键库集合；探测失败置空集并标 `resolvable="unknown"` |
| `env_vars` | 关键环境变量（HF_HOME / MODEL_ROOT / MODELSCOPE_CACHE / LD_LIBRARY_PATH） |
| `disk_free_mb` | 剩余磁盘空间 |

工厂：`EnvSpec.from_env(site_packages: Path | None = None)` —— 注入路径参数便于测试（tmp_path 伪造虚拟 site-packages）。

### 3.3 ModelSpec（模型特征）

| 字段 | 说明 |
|---|---|
| `source` | `"local"` / `"id"` |
| `architectures` / `model_type` | 来自本地 config.json |
| `quantization` | 从 quantization_config 提取（fp8 / fp4 / awq / gptq …）或 yaml 显式 |
| `engine` | 目标引擎 |
| `name_hint` | 模型 id / 文件名，供 id 特征兜底 |

工厂：`ModelSpec.from_local(dir)` / `ModelSpec.from_id(model_id, download_id)`。

### 3.4 CompatIssue / CompatRule / run_compat

```python
@dataclass(frozen=True)
class CompatIssue:
    level: Literal["block", "degrade"]
    rule_id: str
    reason: str            # 完整中文原因，含修复建议

@dataclass(frozen=True)
class CompatRule:
    id: str
    engines: tuple[str, ...]
    check: Callable[[GpuSpec, EnvSpec, ModelSpec | None], CompatIssue | None]
    # 命中返回 issue；不适用/通过返回 None

def run_compat(engine, gpu, env, model) -> list[CompatIssue]: ...
    # 按 engine 过滤规则 → 逐条执行 → 收集 issue，block 在前
```

配套 helper：

```python
def apply_compat(adapter, issues) -> None:
    # block：拼接全部 reason → 抛 RequirementError
    # degrade：逐条写入 adapter.warnings
```

## 4. 内置规则清单（第一阶段）

### 4.1 硬件规则（ModelSpec × GpuSpec）

| rule_id | 引擎 | 命中条件 | 不满足时 | 级别 |
|---|---|---|---|---|
| `deepseek_v4_mhc` | vllm, sglang | 模型为 DeepSeek-V4（arch/model_type/id 特征） | CC 主版本 ∉ {9, 10} | block（提示 llamacpp GGUF 替代） |
| `fp8_quant_cc` | vllm, sglang | 量化含 fp8（yaml 显式或 config.json） | CC < 8.9 | block |
| `fp4_quant_blackwell`（预留） | vllm | 量化含 fp4 | CC 主版本 ∉ {10, 12} | block |

### 4.2 软件规则（EnvSpec）

| rule_id | 引擎 | 检查内容 | 级别 |
|---|---|---|---|
| `vllm_torch_abi` | vllm | vllm wheel `Requires-Dist: torch==x` vs 已装 torch | block（提示安装命令） |
| `nvidia_pkg_complete` | vllm, sglang | nvidia 各包 wheel 元数据声明的 .so vs 磁盘实际文件（拦截空壳包） | block |
| `cuda_lib_resolvable` | vllm, sglang | 按驱动 CUDA 版本推断关键库（libcudart/libcudnn/libnccl…），检查可解析性 | block |
| `engine_dep_missing` | vllm（追认：第一阶段仅 vllm，sglang 依赖体系不同留待后续） | vllm METADATA 关键硬依赖（如 xgrammar）版本匹配 | block |
| `env_var_missing` | 全部 | HF_HOME / MODEL_ROOT 等缺失 | degrade |

## 5. 两段式调用流程

```
modelctl start <name>
 └─ _cmd_start
     ├─ ① 预检：check_requirements()
     │     ├─ EnvSpec.from_env()                    # 软件快照，每次 start 探测一次
     │     ├─ ModelSpec.from_id(...)                # id 特征
     │     ├─ run_compat(engine, gpu, env, model) → issues
     │     └─ apply_compat()：block → 抛 / degrade → warnings
     ├─ ② pre_start()：下载 / 编译 / pull
     │     └─ 模型文件就位后 → 精检
     │           ├─ ModelSpec.from_local(模型目录)  # config.json，判定更精确
     │           ├─ run_compat(...)（复用同一 EnvSpec）
     │           └─ apply_compat()
     └─ build_command() → start_detached
```

关键决策：

- 精检时机 = `pre_start` 末尾（模型就位后、`build_command` 前），不改 cli.py 流程骨架
- 规则幂等：精检重跑全部规则；模型规则在 `source=local` 下判定更准
- EnvSpec 单次进程内缓存（同一 CLI 调用只探测一次）
- 数据源缺失容错：字段探测失败置"未知"，相关规则返回不适用，**不误报**
- 执行顺序：软件规则先于模型规则；block 与 degrade 完整收集后统一处理

## 6. 错误处理与行为分级

| 级别 | 语义 | 处置 |
|---|---|---|
| block | 确定无法运行/必然崩溃 | 拼接全部 reason → `RequirementError` → exit 2（复用现有机制） |
| degrade | 仅影响性能/功能子集 | 写入 `adapter.warnings` 打印 |

block 消息格式（逐行列出，规则内嵌修复建议）：

```
当前服务器不支持 vllm 引擎部署 deepseek-v4-flash-vllm 模型：
  [deepseek_v4_mhc] DeepSeek-V4 的 mHC 层依赖 DeepGEMM hyperconnection 内核，官方仅支持
     Hopper/Blackwell DC（CC 9.0/10.0），当前 GPU 为 NVIDIA RTX 5880 Ada（CC 8.9）。
     可改用 llamacpp 引擎运行 GGUF 版本。
  [vllm_torch_abi] vllm 0.27.1 要求 torch==2.13.0，当前已装 2.9.1。
     建议执行：uv pip install "torch==2.13.0" "torchvision==0.28.0" "torchaudio==2.11.0"
```

数据容错原则：**宁可漏检（等启动时报错），不因数据不全而误拦。**

### 与现有检查的关系

- 并入框架：vllm 现有 DeepSeek-V4/FP8 检查、sglang 待补同规则 → 迁移为规则（删除原代码，避免逻辑漂移）
- 保持原位：GPU 数 vs TP、显存预检、DSpark 显存降级、mmproj 缺失降级（资源配置/功能降级，不并入）
- `modelctl probe` 增强：输出 GpuSpec + EnvSpec 摘要

### 边界与不做什么

- 不做引擎导入冒烟（已定：静态检查）
- 不做模型自动降档（只报告，处置交给用户）
- 不做启动后日志扫描式自检

## 7. 测试策略

### 单元测试（`tests/test_compat.py`）

- Spec 工厂：CC 解析/arch_family 推导、`EnvSpec.from_env` 用 tmp_path 伪造虚拟 site-packages（含 METADATA 与假 .so）、`ModelSpec` config.json 存在/缺失/坏 JSON、quantization 提取
- 每条规则直测：命中/未命中/数据缺失三种路径
- 框架：`run_compat` 引擎过滤与聚合排序；`apply_compat` block 抛/拼接、degrade 写 warnings

### 迁移回归（`tests/test_engines_vllm.py`）

现有 4 个 DeepSeek-V4 测试原样保留，规则迁移后行为必须不变；新增 FP8 从 config.json 识别量化用例。

### 集成（`tests/test_compat_flow.py`）

- 两段式：`check_requirements`（id 命中 block）→ `pre_start` 后 `from_local` 精检再次命中
- 软件规则在 `check_requirements` 阶段即生效（不依赖模型下载）

### 验证命令

```
uv run ruff check src tests
uv run mypy src
uv run pytest -q
```

### 验收标准

- `modelctl start deepseek-v4-flash-vllm`（Ada 服务器）→ 启动前输出多行 block 错误 + exit 2，不等 300s 超时
- 软件不兼容（torch 版本错配）→ 同样启动前拦截
- 现有测试全绿 + 新增约 20-25 条

## 8. 里程碑

1. `core/compat.py`：Spec 类型 + 注册表 + run_compat/apply_compat
2. 内置规则：3 硬件（含 fp4 预留）+ 5 软件
3. vllm/sglang 适配器接入（预检 + 精检），迁移原 DeepSeek-V4/FP8 检查
4. llamacpp/unsloth/ollama 接入软件环境规则
5. `modelctl probe` 增强 + 全量测试
