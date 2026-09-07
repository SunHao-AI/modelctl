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
