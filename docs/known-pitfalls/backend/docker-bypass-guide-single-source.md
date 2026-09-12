# Docker 旁路指引：能力矩阵必须有单一事实来源

> 原始单文件已并入本文件归档（本主题为首次沉淀，无历史归档）。

## 平台限制文案承诺了 UI 无法兑现的旁路路径

**现象**：Windows 环境页点 Setup，任务失败提示"…或走 docker 镜像绕过 venv"，但 UI 上没有任何对应操作入口，用户无处可去。

**根因**：`envs.platform_limitation_message()` 的文案是手写字符串，与实际能力（仅 vllm/tokenspeed/tensorrt_llm 适配器实现 `_resolve_runtime` 的 docker_image 分支）没有代码级关联；`docker_setup.diagnose()/render_instructions()` 只在 CLI 可达，webui 层 grep "docker" 零命中。

**解决**：
1. 能力矩阵落到常量，并由测试锚定实现，避免文案与实现漂移：

```python
# envs.py
DOCKER_CAPABLE_ENGINES = ("vllm", "tokenspeed", "tensorrt_llm")

# tests/test_core_envs.py —— 常量成员必须 _resolve_runtime() == ('docker', image)，
# 未列入的引擎适配器类不得定义 _resolve_runtime
```

2. UI 指引由常量表驱动，未支持引擎只给 `note` 不给 `steps`（严禁对没有 docker 分支的引擎渲染镜像指引）：

```python
guide = DOCKER_BYPASS_GUIDES.get(name)
if guide: ...          # 镜像事实 + 三步
else: {"docker_supported": False, "note": DOCKER_UNSUPPORTED_NOTE}
```

## 只读探测与子进程诊断必须分层

**现象/风险**：`docker_setup.diagnose()` 内含 `docker info`（15s 超时）；若并入 `GET /envs` 列表端点，Docker 缺失或 daemon 卡顿时环境页加载会阻塞数秒。

**解决**：列表端点只用 `path_level_missing()`（纯 `shutil.which`，零子进程）做徽标；完整 4 项诊断放独立端点 `GET /envs/docker/diagnose`，由前端"完整诊断"按钮首次展开时懒加载一次，并用 `asyncio.to_thread` 包裹避免阻塞事件循环：

```python
missing = docker_setup.path_level_missing()          # 列表内联，永不抛错
checks = await asyncio.to_thread(docker_setup.diagnose)  # 按需端点
```

## 平台不支持的操作应在入口禁用而非仅事后报错

**现象**：环境页对 Windows 不可能成功的 Setup 按钮照常亮着，用户必须点一次、等任务失败才知道不行。

**解决**：响应带 `platform_supported`，前端把门禁前移到按钮，并让失败文案兜底二次引导（`disabled` + `disabled-reason` 走 TaskButton 新 props；`@error` 回调检测固定关键字追加引导语）：

```vue
<TaskButton :disabled="!t.platform_supported" :disabled-reason="'托管 venv 仅支持 Linux 部署机，可走下方「Docker 旁路」区块'" />
```

```ts
if (msg.includes('docker 镜像绕过')) notice.value += '——旁路步骤见下方「Docker 旁路」区块';
```

注意：TaskButton 的 `disabled` 是**外部**门禁，与既有内部 `phase !== 'idle'` 取或，不可替换掉原有判断。

## docker ∨ venv 的聚合可达性必须在 UI 标注来源

**现象**：仪表板「引擎二进制」里 vllm / tokenspeed / tensorrt_llm 显示绿色 ✓，环境页同一批引擎却全是「未安装」——用户认定其中一处是误报。实际两处都「对」，只是口径不同：仪表板走 `reachable = venv_ok or (name in DOCKER_CAPABLE_ENGINES and docker_ready())`，环境页 `installed` 是纯 `has_env()`。

**根因**：把「能不能跑起来」与「venv 装没装」两个语义压进同一个 ✓/✗ 布尔。OR 逻辑本身是正确的（与 `_resolve_runtime` 的 `docker_image > venv` 分流对齐），错在**丢失来源维度**——聚合值无法自证是本地装好还是靠容器旁路。

**解决**：后端回**结构化来源**而不是聚合布尔，前端按来源分档渲染：

```python
# admin_probe.py —— /overview 与 /probe 共用同一形态，别再各写一套判定
"reachable": reachable,
"runtime": "docker" if (reachable and not venv_ok) else ("venv" if venv_ok else None),
"docker_capable": docker_capable,
"docker_ready": bool(dready),
```

```vue
<!-- ✓ = venv 已装；docker 徽标 = venv 未装但有旁路；✗ = 不可用 -->
<span v-if="engineRuntime(b) === 'venv'" class="text-emerald-300">✓</span>
<span v-else-if="engineRuntime(b) === 'docker'">docker</span>
<span v-else class="text-red-300">✗</span>
```

三点约束：
1. 来源命名（`venv` / `docker`）必须与启动路径词表一致（`StartupSnapshot.runtime` 同词），不要另造 `available`/`bypass`。
2. 并行端点里 `docker_ready()` 由调用方算一次经 `dready=` 传入，勿在 per-engine 循环里重复 `shutil.which`（overview 是 3s 轮询）。
3. 卡片必须带图例说明「docker 徽标 ≠ 已安装」，否则与 EnvironmentView 的「未安装」并排看仍会被读成矛盾。

## 托管引擎 venv 判定：只查 python 存在就拼可执行路径

**现象**：`modelctl probe` / 体检页展示 `.venvs/<engine>/bin/<engine>` 这类路径，但该文件根本不存在（env setup 中途失败、或该引擎本就不提供同名 console script）。

**根因**：`capabilities._managed_binary_path()` 旧实现只判 `has_env(engine)`（= `.venvs/<engine>/bin/python` 在位）就直接 `return engine_bin(engine, name)`——路径是**拼出来的**，从未 `is_file()`。而 `sglang` / `tensorrt_llm` 的 `build_command` 走 `engine_python(...) -m <module>`，venv/bin 下**本来就没有**同名可执行文件，按裸名 `is_file()` 又会把装好的 venv 误判成缺失。

**解决**：按启动方式分两类校验，并把「模块型」引擎显式登记：

```python
# capabilities.py
ENGINE_MODULES = {"sglang": "sglang", "tensorrt_llm": "tensorrt_llm"}

top_pkg = ENGINE_MODULES.get(engine)
if top_pkg is not None:
    sp = engine_site_packages(engine)                 # python -m 型：查顶层包目录
    if sp is None or not (sp / top_pkg).is_dir():
        return None
    return engine_python(engine)                      # 路径=解释器本身才有意义
path = engine_bin(engine, name)
return path if path.is_file() else None               # console script 型：必须真在
```

**教训**：`has_env()` 回答的是「venv 建好了吗」，不是「引擎能用吗」——环境页用它判「已安装」是对的，探测层复用它当「可执行」就是错的。凡从模板拼出的路径要当事实对外展示，必须落一次 `is_file()`；而判据本身还得跟适配器 `build_command` 的实际调用方式（可执行文件 vs `python -m`）逐个对齐，不能统一按裸名找。
