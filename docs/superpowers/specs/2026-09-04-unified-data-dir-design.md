# modelctl 运行时数据目录统一设计

日期：2026-09-04
状态：已确认（用户评审通过）

## 1. 背景与目标

### 1.1 现状

`.env.example` 把九个项目录路径全部硬编码成某台部署机的绝对路径（`/raid5/sh/...`），
其中四项运行时数据目录（`LOG_DIR` / `CACHE_DIR` / `USAGE_DATA_DIR` / `AUDIT_DIR`）
本应默认落在项目根的 `data/` 下，却要求用户逐台机器手填绝对值。

更严重的是**代码默认值有三套口径并存**（默认值分散在各调用点的 `os.environ.get(...) or <default>`，
无集中 Settings 类）：

| 环境变量 | 现有代码默认值 | 定义位置 | 口径 |
|---|---|---|---|
| `LOG_DIR` | `PROJECT_ROOT.parent / "logs"` | `core/logging.py:44`、`core/process.py:60` | 项目根**上级** |
| `CACHE_DIR` | `PROJECT_ROOT / "data" / "cache"` | `core/process.py:67` | `data/` ✅ |
| `USAGE_DATA_DIR` | `PROJECT_ROOT / "data" / "cache"` | `core/stats.py:942` | `data/` 但与 CACHE_DIR **撞目录** |
| `AUDIT_DIR` | `"data/audit"`（**相对 CWD**） | `cli.py:974`、`core/gateway.py:490` | 相对 CWD ❌ |
| `AUDIT_DIR` | 同上但按 `PROJECT_ROOT` 修正 | `core/webui/admin_audit.py:53-60` | 三处重复、行为不一致 |

由同一根因引出的缺陷：

1. `core/gpu_lock.py:31` 的 `LOCK_DIR = PROJECT_ROOT / "data" / "cache"` 是**模块级常量**，
   完全不读 `CACHE_DIR`。设置 `CACHE_DIR` 后 PID 文件与 `.gpu-lock` 分家，
   `list_gpu_locks()` 读不到锁 → GPU 占用互斥失效。
2. `USAGE_DATA_DIR` 是 stats 服务与网关**共用**的累计口径，但 `core/gateway.py:1078` 未设置时传
   `None` → 落到 `cache_dir()`；`all_service.py:244` 又仅在**已设置**时才透传给子进程。
   一旦只改 stats 的默认值，两侧写入不同目录，token 累计直接分家。
3. `core/webui/admin_probe.py:269` 把 `LOCK_DIR` 当 `paths.cache_dir` 上报前端，展示值不可信。
4. `.env.example` 的 `UNSLOTH_STUDIO_URL` 全仓库**零读取点**（仅模板与历史文档出现），是死配置。
5. `HF_ENDPOINT` 只出现在 `engines/unsloth.py:134` 的**报错文案字符串**里，从不注入任何子进程或
   下载链路 → 用户按 README 配置后完全不生效。

### 1.2 目标

1. 四项运行时数据目录的默认值统一到 `<项目根>/data/<子目录>`，且**只有一处真值来源**。
2. 相对路径一律按 `PROJECT_ROOT` 解析，彻底消除"从其它目录执行 CLI 就写错位置"的缺陷。
3. `CACHE_DIR` 成为 GPU 锁与集群元数据的真实上游，`USAGE_DATA_DIR` 在 stats 与网关之间口径一致。
4. 清理 `.env.example` 中的死配置与"配了不生效"的配置。
5. 文档默认值描述与代码一致；旧位置历史数据提供**手工**迁移命令，不做隐式搬迁。

### 1.3 关键决策（已与用户确认）

| 决策点 | 结论 |
|---|---|
| 统一范围 | **仅运行时数据四项**。`MODEL_ROOT` / `MODELSCOPE_CACHE` / `OLLAMA_MODELS` / `HF_HOME` 存几十 GB 权重、`LLAMACPP_SOURCE_DIR` 是外部 git 仓库，均**不纳入**（语义不是数据） |
| 模板写法 | 四项在 `.env.example` 中改为**注释态 + 默认值说明**（`.env` 值是纯字符串，无法表达"项目根"这种动态路径）；原 `/raid5` 绝对值保留在注释里作自定义示例 |
| usage 目录 | 与 `CACHE_DIR` **分开**为 `data/usage-data`，统计 JSON 不与 PID/锁文件混放 |
| 实现方式 | **方案 1：新增 `core/paths.py` 统一解析**。方案 2（各点原地改默认值）会让 `AUDIT_DIR` 改三遍且相对路径坑仍在；方案 3（引入 pydantic-settings）与手写的 `os.environ.setdefault` + profile `${VAR}` 插值机制冲突，属无关大重构 |
| 旧数据迁移 | **只改默认值 + 文档给手工 `mv` 命令**。代码不做隐式搬迁，行为可预测 |
| 死配置 | 删除 `UNSLOTH_STUDIO_URL`；让 `HF_ENDPOINT` 真正生效；澄清 `MODEL_ROOT` 双默认语义 |

## 2. 目录布局与默认值

| 环境变量 | 新默认值 | 存放内容 |
|---|---|---|
| `LOG_DIR` | `<项目根>/data/logs` | loguru `modelctl.log`、启动日志 `launch-<name>.log` |
| `CACHE_DIR` | `<项目根>/data/cache` | `*.pid`、`*.gpu-lock`、`cluster-meta.db` |
| `USAGE_DATA_DIR` | `<项目根>/data/usage-data` | 用量累计 `<name>.json`（stats 与网关**共用同一目录**） |
| `AUDIT_DIR` | `<项目根>/data/audit` | 审计 `modelctl-YYYY-MM-DD.jsonl` |

解析规则（四项完全一致）：

1. 环境变量值非空 → 使用它；若是**相对路径则按 `PROJECT_ROOT` 解析**（不按 CWD）。
2. 环境变量缺失或空串 → `<项目根>/data/<固定子目录名>`。
3. 取用时幂等 `mkdir(parents=True, exist_ok=True)`，与各调用点现有行为一致。

不改动项：`MODEL_ROOT`（含 `model-gguf` / `model-hf` 双默认）、`MODELSCOPE_CACHE`、
`OLLAMA_MODELS`、`HF_HOME`、`LLAMACPP_SOURCE_DIR` 的默认值与语义全部保持原样。

## 3. 单一真值来源：`src/modelctl/core/paths.py`

新增模块，只做路径解析，不含业务逻辑：

```python
DATA_ROOT = PROJECT_ROOT / "data"

def resolve_data_dir(env_value: str | None, subdir: str) -> Path:
    """env 值非空 → 用之（相对路径按 PROJECT_ROOT 解析）；否则 DATA_ROOT / subdir。"""

def log_dir() -> Path: ...         # resolve + mkdir
def cache_dir() -> Path: ...       # resolve + mkdir
def usage_data_dir() -> Path: ...  # resolve + mkdir
def audit_dir() -> Path: ...       # 只 resolve，不 mkdir（理由见下）
```

设计约束：

- 只依赖 `core/envfile.PROJECT_ROOT`，不 import `process` / `gpu_lock` / `stats`，
  因此不会与现有模块构成循环导入（`gpu_lock` 已因循环导入问题在注释里明确过约束）。
- 四个函数每次调用都重读 `os.environ`，**不做模块级缓存**——测试靠 `monkeypatch.setenv` 隔离，
  现有 `LOCK_DIR` 常量正是因模块级缓存导致 env 覆盖无效才被淘汰。
- `audit_dir()` 不建目录：写入方 `RequestAuditLog._today_path`（`core/audit.py:112-115`）已在每次
  落盘前幂等 `mkdir`，读取方（CLI `audit query/stats/cleanup`、webui 审计接口）本就允许目录不存在
  并返回空结果。此处集中 mkdir 会让只读的 `modelctl audit stats` 产生建目录副作用。

改造点清单（全部改为调用 `paths.*`，删除各自本地实现）：

| 文件 | 现状 | 改为 |
|---|---|---|
| `core/logging.py:44` | `Path(os.environ.get("LOG_DIR") or PROJECT_ROOT.parent / "logs")` | `paths.log_dir()` |
| `core/process.py:59-62` | 本地 `log_dir()` | 转发 `paths.log_dir()`（保留同名导出，调用方不动） |
| `core/process.py:65-69` | 本地 `cache_dir()` | 转发 `paths.cache_dir()` |
| `core/gpu_lock.py:31` | `LOCK_DIR` 常量 | 各函数内 `cache_dir()`（删除常量） |
| `core/stats.py:938-942` | `... or PROJECT_ROOT / "data" / "cache"` | `paths.usage_data_dir()` |
| `core/gateway.py:490` | `Path(os.environ.get("AUDIT_DIR", "data/audit"))` | `paths.audit_dir()` |
| `core/gateway.py:1078` | `Path(os.environ.get("USAGE_DATA_DIR", "")) or None` | `paths.usage_data_dir()` |
| `core/gateway.py:280` | `data_dir or cache_dir()` | `data_dir or paths.usage_data_dir()` |
| `core/all_service.py:244-246` | 仅 env 已设置才透传 | 总是透传 `str(paths.usage_data_dir())` |
| `cli.py:972-974` | `_audit_dir_from_env()` | 转发 `paths.audit_dir()` |
| `core/webui/admin_audit.py:53-60` | 本地 `is_absolute()` 修正 | 转发 `paths.audit_dir()` |
| `core/webui/admin_probe.py:269` | `str(LOCK_DIR)` | `str(paths.cache_dir())` |

`process.py` 保留 `log_dir()` / `cache_dir()` 两个同名薄转发（不重命名调用方），
避免本次改动扩散到 `cluster/store.py`、`gateway.py` 等十余处无关调用点。

## 4. B 组缺陷修复

### 4.1 GPU 锁跟随 CACHE_DIR

删除 `gpu_lock.py` 的 `LOCK_DIR` 常量，`_lock_path` / `list_gpu_locks` / `acquire_gpu_lock` /
`release_gpu_lock` / `update_gpu_lock_owner` 内改用 `cache_dir()`。
`admin_probe.py` 同步删除 `LOCK_DIR` 导入。

测试侧：`monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp)` 全部替换为
`monkeypatch.setenv("CACHE_DIR", str(tmp))`（涉及 `test_gpu_lock.py`、`test_process.py`、
`test_engines_vllm.py`、`test_engines_unsloth.py`、`test_engines_llamacpp.py` 共 14 处）。
`tests/conftest.py` 的 autouse fixture 已隔离 `CACHE_DIR`，改后隔离更干净。

### 4.2 usage 累计口径统一

stats 服务与网关都从 `paths.usage_data_dir()` 取目录，`all_service.py` **总是**把解析后的绝对路径
透传给子进程（不再依赖 env 是否设置），保证 `modelctl all start` 下两侧写入同一目录。

### 4.3 死配置与无效配置

- **删除 `UNSLOTH_STUDIO_URL`**：`.env.example` 移除该行。零读取点，远程 unsloth 部署可用 profile 的
  `base_url`，无功能损失。
- **`HF_ENDPOINT` 真正生效**：`engines/unsloth.py` 的 `build_command` 在返回的子进程 env 中注入
  `HF_ENDPOINT`（非空才注入，与原有 `HF_HOME` 同一处理方式），使报错文案中"配置 HF_ENDPOINT
  走 HF 镜像"的建议对 unsloth studio 运行期真实可用，而不是只躺在提示字符串里。
- **`MODEL_ROOT` 语义澄清**：`.env.example` 注释写明它同时服务两类权重目录——GGUF 引擎
  默认落 `../model-gguf`，HF 类引擎（vllm/sglang/lmdeploy/aphrodite/tokenspeed）默认落
  `../model-hf`，未设置时按引擎类型分目录，并非单一目录。

### 4.4 杂项

- `.gitignore`：删除第 52 行 `/data/audit/`（第 41 行 `data/` 已覆盖任意层级 `data`）。
- `data/testlogs/`：代码零引用的本地调试遗留目录，删除。

## 5. 文档与迁移

- `README.md`：`LOG_DIR` 默认值描述（第 477 行"项目根上级的 `../logs/`"及其下方 `tail -f ../logs/...`
  示例）、`AUDIT_DIR` 默认值（第 487 行）改为 `data/` 口径；`HF_ENDPOINT` 条目补"注入 unsloth 下载链路"。
- `docs/DeepSeek-V4-Flash后台启动指南.md`：环境变量表格中 `LOG_DIR` 等默认值同步（该文档现值
  `/raid5/sh/logs` 与 `.env.example` 本就不一致，统一为默认值说明）。
- `.env.example`：四项改注释态；`MODEL_ROOT` 注释澄清；删 `UNSLOTH_STUDIO_URL`。
- **手工迁移**（仅未显式配置过这些变量的机器需要，文档中给出）：

  ```bash
  mkdir -p data/logs data/usage-data
  mv ../logs/* data/logs/ 2>/dev/null            # 历史运行/启动日志
  mv data/cache/*.json data/usage-data/ 2>/dev/null  # 已积累的用量累计
  ```

  显式配过 `LOG_DIR` / `USAGE_DATA_DIR` 的机器（如现有 `/raid5` 部署）默认值变更不产生任何影响。
- 按 `CLAUDE.md` 规范，把"默认值三套口径 / 相对 CWD / 模块级常量绕过 env"沉淀到
  `docs/known-pitfalls/backend/`（摘要层 `README.md` + 详情主题文件）。

## 6. 测试

新增 `tests/test_paths.py`：

1. `resolve_data_dir(None, "logs")` == `DATA_ROOT / "logs"`（用 `monkeypatch.setattr` 改写
   `paths.PROJECT_ROOT`/`DATA_ROOT` 到 `tmp_path`，参照 `test_core_envs.py:49-50` 的既有范式）。
2. env 空串 == 未设置，均回退默认。
3. 相对 env 值（`"my/logs"`）按 `PROJECT_ROOT` 解析，**与当前 CWD 无关**。
4. 绝对 env 值原样采用。
5. 四个 `*_dir()` 的默认路径分别等于 `data/{logs,cache,usage-data,audit}`。

回归重点：

- `test_gpu_lock.py` 全量（`LOCK_DIR` → `CACHE_DIR` 改造后仍须全绿）。
- `test_stats.py` / `test_audit.py` / `test_process.py`（口径变更后无路径漂移）。
- 现有 `tests/conftest.py` 的 autouse fixture 已隔离 `CACHE_DIR` / `LOG_DIR` / `AUDIT_DIR`
  （`conftest.py:41-43`），但**缺 `USAGE_DATA_DIR`**：本次补 `setenv("USAGE_DATA_DIR", tmp/usage-data)`，
  并 `delenv("HF_ENDPOINT")`（§4.3 后 unsloth 会真实读取它，防开发者 `.env` 泄漏改变下载行为）。
  注：`MODELSCOPE_CACHE` 已由 `test_engines_*.py` 在用例内 `delenv` 隔离，不重复处理。

验证命令：`uv run pytest -q`、`uv run ruff check .`、`uv run mypy src`。
