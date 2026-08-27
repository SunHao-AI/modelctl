# modelctl 引擎独立虚拟环境隔离设计

日期：2026-08-27
状态：已确认（用户评审通过）

## 1. 背景与目标

vllm 与 sglang 的核心依赖（torch / flashinfer 等）版本互斥，无法共存于同一个 Python 环境。当前项目仅靠文案提示用户手动隔离（`core/capabilities.py` 安装提示语），且存在两处隐式耦合：

1. sglang 启动用 `sys.executable -m sglang.launch_server`（`engines/sglang.py`），隐式要求 sglang 包装在 modelctl 主环境中，与 `check_requirements` 只探测 PATH 的逻辑不一致；
2. vllm 直接取 PATH 中的 `vllm` 命令，环境归属不受 modelctl 控制。

本设计将 Python 系引擎的运行时依赖与 modelctl 控制面彻底分离：**每个托管引擎一个独立虚拟环境**，modelctl 主环境退化为纯控制面（PyYAML / loguru / modelscope / gateway 等）。

### 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 适用范围 | 仅 **vllm、sglang**；ollama / llamacpp 为原生二进制不涉及；unsloth 依赖官方安装器自建运行时（"仅 pip install 不够"），保持外部安装 |
| 管理方式 | modelctl 全托管：新增 `modelctl env` 命令族负责创建 / 安装 / 卸载 |
| 环境缺失行为 | 启动直接报错并提示 setup 命令，不做静默回退 |
| 依赖定义 | 每引擎独立 uv 子项目 + `uv.lock` 入库（方案 B，保证 torch/cuda 组合可复现） |
| 主项目清理 | 移除 `pyproject.toml` 中 `vllm` extra |

## 2. 总体结构

```
envs/
├── vllm/
│   ├── pyproject.toml      # dependencies = ["vllm==0.27.*"]；[tool.uv] 声明 torch cu13 index
│   ├── .python-version     # 固定 Python 版本
│   └── uv.lock             # 锁定文件，提交入库
└── sglang/
    ├── pyproject.toml      # dependencies = ["sglang[all]==0.5.9"]
    ├── .python-version
    └── uv.lock
.venvs/
├── vllm/                   # 实际虚拟环境实体（.gitignore）
└── sglang/
```

要点：

- `envs/*` **不注册**为主项目 uv workspace 成员，彼此及与主项目完全独立；
- 各子项目自带 `[tool.uv]` index 配置（vllm 所需的 torch cu13 explicit index 从根 `uv.toml` 迁移过来），保证 `uv sync` 独立可复现；
- Python 版本基线取主项目当前要求（3.12）；若实施时发现与引擎包元数据冲突，以引擎约束为准在各自 `.python-version` 单独固定；
- 环境实体统一放在项目根 `.venvs/<engine>/`，通过 `UV_PROJECT_ENVIRONMENT` 环境变量重定向（uv 默认落在 `envs/<engine>/.venv`）；
- Windows（`Scripts\python.exe`）与 Linux（`bin/python`）差异集中在 `core/envs.py` 处理，调用方无感知。

## 3. 新模块 `core/envs.py`

| 接口 | 职责 |
|---|---|
| `ENVS_ROOT` / `VENV_ROOTS` | 仓库内 `envs/` 子项目根、`.venvs/<engine>/` 环境根（基于 `PROJECT_ROOT`） |
| `MANAGED_ENGINES = ("vllm", "sglang")` | 托管引擎清单（唯一事实来源，模块内定义） |
| `has_env(engine) -> bool` | 探测专用环境是否存在（venv 目录 + python 可执行文件） |
| `ensure_env(engine) -> Path` | 环境缺失时抛 `EngineEnvError`，文案含 `modelctl env setup <engine>`；存在则返回 venv 根 |
| `engine_python(engine) -> Path` | 专用 venv 的解释器绝对路径（跨平台） |
| `engine_bin(engine, name) -> Path` | 专用 venv 内可执行文件绝对路径（跨平台，含 `.exe` 后缀处理） |
| `setup(engine) -> int` | 执行 `uv sync --project envs/<engine>`（`UV_PROJECT_ENVIRONMENT` 重定向到 `.venvs/<engine>`），流式转发输出，返回退出码；uv 不可用时直接报错并给安装提示 |
| `remove(engine)` | 删除 `.venvs/<engine>/` |
| `status() -> dict` | 各托管引擎环境状态：存在性、Python 版本（读 venv 内 `pyvenv.cfg`）、已装引擎包版本（扫描 site-packages 下 dist-info，不依赖 venv 内 pip，uv 创建的 venv 默认无 pip） |

- 新异常 `EngineEnvError` 定义于本模块；
- 纯路径/状态逻辑不执行外部命令，`setup()` 内才调用 `subprocess`（遵守"模块导入期禁止执行外部命令"约定）。

## 4. CLI 命令族（`cli.py`）

```
modelctl env setup <engine>   # 创建并安装指定引擎环境（engine ∈ MANAGED_ENGINES）
modelctl env list             # 列出托管引擎环境状态；ollama/llamacpp/unsloth 标注"原生/外部安装，无需托管"
modelctl env remove <engine>
```

- 与现有子命令一致：`build_parser()` 注册、`main()` 分发到 `_cmd_env_*`；
- `env setup` 失败（uv 缺失 / 网络 / 依赖冲突）时输出退出码与日志尾部，提示重试，不静默吞错。

## 5. 启动链路改造

数据流不变：`build_command()` 仍返回 `(command, extra_env)`。仅改造三个适配器的可执行文件来源：

| 引擎 | 现状 | 改造后 |
|---|---|---|
| vllm | PATH 中 `vllm` | `<.venvs/vllm>/bin/vllm serve ...`（Windows: `Scripts\vllm.exe`） |
| sglang | `sys.executable -m sglang.launch_server` | `<.venvs/sglang>/bin/python -m sglang.launch_server` |
| unsloth | PATH 中 `unsloth`（官方安装器） | 不变 |
| ollama / llamacpp | 原生二进制 | 不变 |

- `vllm.py` / `sglang.py` 在 `build_command()` 内通过 `core.envs.engine_bin()/engine_python()` 解析路径；
- `extra_env` 注入 `VIRTUAL_ENV=<venv 根>` 与前置 `<venv>/bin`（Windows: `Scripts`）的 `PATH`；`HF_HOME` / `CUDA_VISIBLE_DEVICES` 等既有逻辑不变；
- `core/process.py` 的 env 合并（`{**os.environ, **extra_env}`）无需改动；
- `stop_patterns`（如 `"vllm serve"`、`"-m sglang.launch_server"`）基于命令行子串匹配，可执行文件换成 venv 绝对路径后依然命中，无需调整；
- `check_requirements()` 第一步改为 `ensure_env(engine)`（替代现有 PATH 探测），顺带修复 sglang"探测 PATH、启动用 sys.executable"的不一致；硬件 / 显存 / GPU 锁等后续校验不变。

## 6. 探测与兼容性检查改造

- `core/capabilities.py`：
  - `ENGINE_BINARIES` 中的 vllm / sglang 探测改为优先解析专用 venv 内的二进制（未建环境记为 missing）；
  - `ENGINE_INSTALL_HINTS` 的 vllm / sglang 提示语更新为 `modelctl env setup <engine>`；unsloth / ollama / llamacpp 提示语不变；
  - 探测逻辑（`probe()` / `which_binaries` / `binary_paths`）对托管引擎从 `core/envs` 取路径，非托管引擎维持 `shutil.which`。
- `core/compat.py`：`run_compat_checks()` 使用的 `EnvSpec.from_env(site_packages=...)` 改为扫描目标引擎专用 venv 的 site-packages（该函数已支持外部路径参数，改动小）；
- `core/profile.py`、`core/all_service.py`、gateway / stats / ui / nginx 等模块无改动（`all start` 复用 start 路径，自动获得环境预检）。

## 7. 错误处理

| 场景 | 行为 |
|---|---|
| 引擎环境未创建 | `start` / `all start` / `restart` 抛 `RequirementError`，提示 `modelctl env setup <engine>`，exit 2 |
| 环境存在但损坏（python 可执行文件缺失） | `has_env()` 判定不存在，走同一报错路径，建议 `env setup` 重建（setup 幂等，重跑即可修复） |
| `env setup` 失败 | 透传 uv 退出码与输出尾部，提示重试；不自动回退到主环境 |
| 依赖版本冲突 / 安装网络失败 | 属 uv sync 运行期错误，输出日志摘录，人工决策 |

明确不做：启动时自动创建环境（首次启动耗时不可控，且可能掩盖配置问题）。

## 8. 测试策略

- 单元测试（`tests/test_core_envs.py` + 各适配器测试更新）：
  - 路径解析（Linux / Windows 双平台形态）、`has_env/ensure_env` 分支、`ENGINE_ENV` 错误文案；
  - vllm / sglang `build_command()` 首元素指向 venv 路径、`extra_env` 注入 `VIRTUAL_ENV` 与 `PATH`；
  - `check_requirements` 在环境缺失时抛出并含 setup 提示；
  - 全部 mock `subprocess` 与路径解析，不在导入期执行外部命令，Windows 开发机可完整跑通。
- 集成验证（部署机 Linux + 真实 GPU，手动执行）：`env setup vllm && env setup sglang` → `start` → 健康检查 → `stop` → `probe`/`env list` 展示状态。

## 9. 兼容性与迁移说明

- **行为变化（预期内破坏性变更）**：升级后首次 `start` vllm/sglang 模型会因环境未建而报错，执行一次 `modelctl env setup vllm` / `modelctl env setup sglang` 即可；README 快速开始同步更新。
- `pyproject.toml` 移除 `vllm` extra（及对应注释）；已习惯 `uv sync --extra vllm` 的用户改用 `modelctl env setup vllm`。
- 主环境瘦身收益：`uv sync` 不再拉取 torch 全家桶，控制面安装更快、依赖更稳。
- `.gitignore` 新增 `.venvs/`。

## 10. 非目标（明确不做）

- 不封装 unsloth 官方安装器流程（保持 `curl ... | sh` 手动安装）；
- 不引入 conda 支持或跨机器环境分发（如导出/导入环境包）；
- 不做 `.env` 覆盖 venv 路径（环境位置固定于项目根 `.venvs/<engine>`，需要时可后续扩展）；
- 不改变 ollama / llamacpp 的二进制发现机制。
