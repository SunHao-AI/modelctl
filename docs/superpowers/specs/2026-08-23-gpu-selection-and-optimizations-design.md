# GPU 指定与启动生命周期优化设计

- 日期：2026-08-23
- 状态：待评审
- 范围：modelctl 模型 profile 支持显式 GPU 选择，以及关联的显存预检、GPU 冲突检测、进程停止健壮性、健康检查退避、测试补齐

## 1. 背景与动机

当前 profile 只能配置 GPU 数量（如 `llamacpp.gpu_count`、`vllm.tensor_parallel_size`），无法指定具体使用哪几块 GPU。在以下场景不够用：

1. 单台服务器跑多个模型，需要把不同模型绑定到不同 GPU，避免显存争抢。
2. 某些卡被其他任务占用，只想让 modelctl 使用剩余的几张卡。
3. 调试或对比时，需要固定模型到某张卡。

同时梳理代码发现若干关联问题：

- 显存预检按全部 GPU 汇总计算，若实际只用部分 GPU，预检会失真。
- 多个模型未做 GPU 占用互斥，可能抢到同一张卡。
- ollama serve 是常驻进程，没有按模型隔离 GPU 的能力。
- 停止流程依赖 `fuser`/`pkill`/信号组，在 Windows 上不可用。
- 健康检查固定 2s 轮询，启动慢时不够高效。
- GPU / TP 校验逻辑在 llamacpp/vllm/sglang 中重复。
- 除 stats 外，各引擎缺少单元测试。

## 2. 决策记录

| 决策点 | 结论 |
|---|---|
| GPU 指定方式 | profile 字段 + CLI 参数 + 环境变量，优先级：profile > CLI > env |
| GPU 索引格式 | 逗号分隔整数字符串，例如 `"0,1,2,3"` |
| 校验策略 | 严格校验：重复、越界、与 tensor_parallel_size 不匹配均报错 |
| 作用范围 | 所有 CUDA 引擎（llamacpp / vllm / sglang / unsloth）；ollama 受限支持 |
| 越界处理 | `RequirementError`，提示可用索引范围 |
| 冲突检测 | 轻量文件锁：`data/cache/<name>.gpu-lock`，启动前检查、退出时清理 |
| 阶段划分 | 4 个独立阶段，每阶段可单独评审、测试、提交 |

## 3. 阶段划分

### 阶段 1：GPU 指定与校验统一

新增 `gpu_list` 配置，统一解析与校验逻辑，消除重复代码。

#### 3.1 配置格式与优先级

优先级从高到低：

1. profile 中引擎段的 `gpu_list`，例如 `llamacpp.gpu_list: "0,1,2,3"`。
2. CLI 参数 `--gpus 0,1,2,3`。
3. 环境变量 `MODELCTL_GPUS=0,1,2,3`（通过 `os.environ` 直接读取，不经过 `.env` 插值）。
4. 未指定：使用所有可见 GPU（保持现状）。

#### 3.2 新增工具函数

在 `core/profile.py` 或新建 `core/gpu_utils.py` 中实现：

```python
def parse_gpu_list(raw: str | list[int] | None) -> list[int] | None:
    """把字符串/数组解析为去重的 GPU 索引列表；非法输入抛 ValueError。"""

def validate_gpu_selection(gpus: list[int], available: list[int]) -> None:
    """严格校验：重复、越界均抛 RequirementError。"""

def resolve_gpu_list(
    profile_value: str | list[int] | None,
    cli_value: str | None,
    env_value: str | None,
) -> list[int] | None:
    """按优先级解析最终使用的 GPU 列表。"""
```

#### 3.3 `Capabilities` 增强

`core/capabilities.py`：

```python
@dataclass
class Capabilities:
    gpu_count: int = 0
    gpu_indices: list[int] = field(default_factory=list)  # 新增
    gpu_name: str = ""
    vram_total_mb: int = 0
    vram_free_mb: list[int] = field(default_factory=list)  # 已存在，按索引访问
    cuda_driver: str = ""
    compute_capability: str = ""
    binaries: dict[str, bool] = field(default_factory=dict)
    binary_paths: dict[str, str | None] = field(default_factory=dict)
```

`probe()` 中：`caps.gpu_indices = list(range(len(frees)))`，`gpu_count = len(frees)` 保留兼容。

#### 3.4 `EngineAdapter` 抽象层

`engines/base.py`：

```python
def selected_gpus(self) -> list[int] | None:
    """按优先级解析 profile / CLI / env 的 GPU 列表。"""

def validate_gpu_selection(self, gpus: list[int]) -> None:
    """调用 core.gpu_utils.validate_gpu_selection，越界/重复报错。"""

def cuda_visible_devices(self, gpus: list[int]) -> dict[str, str]:
    """返回 {'CUDA_VISIBLE_DEVICES': '0,1,2,3'}。"""
```

CLI 解析的 `--gpus` 通过 `EngineAdapter` 构造时传入：`adapter = get_adapter(profile.engine)(profile, caps, cli_gpus="0,1,2,3")`。`EngineAdapter.__init__` 签名扩展为可传入 `cli_gpus: str | None = None`。

#### 3.5 各引擎适配器变更

**llamacpp**

- `check_requirements`：读取 `cfg.get("gpu_list")`，调用 `validate_gpu_selection`。若指定了 `gpu_list`，忽略 `gpu_count`；否则仍用 `gpu_count` 兼容旧配置。
- `build_command`：
  - 注入 `CUDA_VISIBLE_DEVICES=...` 到 env。
  - `tensor-split` 数量 = `len(gpu_list)`，每个元素为 `"1"`。

**vllm**

- `check_requirements`：若指定 `gpu_list`：
  - `tensor_parallel_size` 未显式配置时，自动置为 `len(gpu_list)`。
  - `tensor_parallel_size` 显式配置时，必须等于 `len(gpu_list)`，否则报错。
- `build_command`：注入 `CUDA_VISIBLE_DEVICES=...`。

**sglang**

- 同 vllm：`tp` 未配置时自动推导，显式配置时必须等于 `len(gpu_list)`。

**unsloth**

- `check_requirements`：若 `tensor_parallel` 开启且指定 `gpu_list`，校验 `len(gpu_list) >= 2`。
- `build_command`：注入 `CUDA_VISIBLE_DEVICES=...`。

**ollama**

- `build_command`：若指定 `gpu_list`，注入 `CUDA_VISIBLE_DEVICES=...` 限制 ollama serve 可见 GPU。
- 文档注明：ollama serve 是常驻进程，所有 ollama 模型共享该限制。

#### 3.6 CLI 参数

`cli.py`：

- `modelctl start <name>` 增加 `--gpus GPUS`。
- `modelctl all start` 增加 `--gpus GPUS`，作为被启动模型的默认值（profile 仍可覆盖）。

### 阶段 2：按 GPU 的显存预检与冲突检测

#### 2.1 显存预检细化

`core/capabilities.py` 新增：

```python
def selected_vram_total_mb(caps: Capabilities, gpus: list[int]) -> int:
    """只汇总被选中的 GPU 总显存。"""

def selected_vram_free_mb(caps: Capabilities, gpus: list[int]) -> int:
    """只汇总被选中的 GPU 剩余显存。"""
```

各引擎 `check_requirements` 中，模型文件大小预检改为用 `selected_vram_free_mb`：

- llamacpp：`need_mb > selected_vram_free_mb(caps, gpus)` 时报错。
- unsloth：同上。
- vllm / sglang：粗略按 `selected_vram_total_mb * gpu_memory_utilization` 估算可用上限，需要时给出 warning（不做硬性 block，因为 HF 权重加载复杂）。

#### 2.2 GPU 冲突检测

新增 `core/gpu_lock.py`：

```python
LOCK_DIR = PROJECT_ROOT / "data" / "cache"

def acquire_gpu_lock(name: str, gpus: list[int]) -> None:
    """写入 data/cache/<name>.gpu-lock；若与现有锁冲突抛 RequirementError。"""

def release_gpu_lock(name: str) -> None:
    """删除对应锁文件。"""

def list_gpu_locks() -> dict[int, str]:
    """返回 {gpu_index: model_name}。"""
```

锁文件内容：JSON，例如 `{"gpus": [0,1,2,3], "pid": 12345, "updated_at": ...}`。

调用点：

- `check_requirements` 中校验 GPU 合法后，调用 `acquire_gpu_lock`。
- `stop_instance` 成功后调用 `release_gpu_lock`。
- 启动时若发现锁文件对应的 PID 已不存在，视为残留锁，自动清理后重试。

限制：

- ollama 模型由于共享 serve 进程，不写入 GPU 锁。
- 文件锁为尽力而为（best-effort），并发启动同一模型时仍可能产生竞态，但可拦截绝大多数日常冲突。

### 阶段 3：进程生命周期健壮性

#### 3.1 停止流程跨平台

`core/process.py` 中 `stop_instance`：

- 保留 PID 文件 + `os.killpg` 逻辑（POSIX）。
- Windows 下 `os.killpg` 不存在，改用 `os.kill(pid, signal.SIGTERM)`。
- `fuser`/`pkill` 调用前判断平台：非 POSIX 或命令不存在时跳过，不报错。
- 增加兜底：尝试 `taskkill /PID <pid> /T /F`（Windows）或 `kill -9 <pid>`（POSIX）。

#### 3.2 健康检查指数退避

`core/process.py` 中 `wait_health`：

```python
interval = 1.0
while time.time() < deadline:
    ...
    time.sleep(min(interval, remaining))
    interval = min(interval * 2, 5.0)
```

### 阶段 4：补齐测试

新增测试文件：

- `tests/test_gpu_utils.py`：`parse_gpu_list`、`validate_gpu_selection`、`resolve_gpu_list`。
- `tests/test_gpu_lock.py`：锁获取/释放/冲突/残留清理。
- `tests/test_engines_llamacpp.py`：`build_command` 与 `check_requirements`（mock Capabilities）。
- `tests/test_engines_vllm.py`：补充 GPU 指定与 TP 校验。
- `tests/test_engines_sglang.py`：同上。
- `tests/test_engines_unsloth.py`：GPU 指定与 tensor_parallel 校验。
- `tests/test_engines_ollama.py`：`CUDA_VISIBLE_DEVICES` 注入。
- `tests/test_process.py`：健康检查退避、停止流程跨平台分支。

## 4. Profile 示例

### llamacpp

```yaml
name: deepseek-v4-flash-llamacpp
engine: llamacpp
port: 18888

llamacpp:
  model: /path/to/model.gguf
  gpu_list: "0,1,2,3"  # 新增
  parallel: 4
  ctx_size: 32768
```

### vllm

```yaml
name: qwen3.8-vllm
engine: vllm
port: 8101

vllm:
  model: /path/to/hf
  tensor_parallel_size: 2
  gpu_list: "4,5"  # 新增，必须与 tensor_parallel_size 数量一致
```

### ollama

```yaml
name: qwen3.8-ollama
engine: ollama
port: 11434

ollama:
  model: qwen3.8:27b
  gpu_list: "6,7"  # 限制 ollama serve 可见 GPU
```

## 5. 错误消息

### GPU 越界

```
[gpu_list] 配置的 GPU 索引 [0, 1, 8] 超出可用范围。
当前可用 GPU 索引：0,1,2,3,4,5,6,7
```

### 重复索引

```
[gpu_list] 存在重复 GPU 索引：1
```

### TP 数量不匹配

```
[vllm] gpu_list 指定了 2 块 GPU (4,5)，但 tensor_parallel_size=4，二者必须一致。
```

### GPU 冲突

```
[gpu_lock] GPU 0,1 已被模型 kimi-k2.5-vllm 占用（PID 12345）。
请先停止占用模型，或更换 gpu_list。
```

## 6. 边界与不做什么

- 不做 NUMA / MIG 等高级 GPU 分组。
- 不做模型自动调度分配（用户显式指定，冲突时提示）。
- 不在 Windows 上实现完全等价 POSIX 的进程组信号，只保证基本停止可用。
- 不改动现有 `gpu_count` 字段语义，仅在新配置存在时优先使用 `gpu_list`。

## 7. 测试与验收

### 验证命令

```bash
uv run ruff check src tests
uv run mypy src
uv run pytest -q
```

### 验收标准

- `modelctl start deepseek-v4-flash-llamacpp --gpus 0,1,2,3` 启动后，进程环境变量 `CUDA_VISIBLE_DEVICES=0,1,2,3`。
- `gpu_list` 与 `tensor_parallel_size` 不匹配时启动前 exit 2。
- 两个模型指定相同 GPU 时，第二个启动前 exit 2 并提示冲突。
- 健康检查在慢启动场景下比固定 2s 轮询更快就绪。
- Windows 上 `modelctl stop` 不因为缺少 `fuser`/`pkill` 而抛异常。
- 新增测试覆盖所有 CUDA 引擎的 GPU 指定与停止/健康检查逻辑。

## 8. 里程碑

1. `core/gpu_utils.py` + `Capabilities.gpu_indices` + 适配器抽象层
2. 各引擎接入 `gpu_list` 与 `CUDA_VISIBLE_DEVICES`
3. CLI `--gpus` 参数与 `MODELCTL_GPUS` 环境变量
4. `core/gpu_lock.py` 冲突检测 + 按 GPU 显存预检
5. `core/process.py` 跨平台停止 + 指数退避健康检查
6. 补齐各引擎单元测试与集成验证
