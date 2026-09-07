# 环境页 Docker 旁路指引设计（方案 A：后端数据源扩展 + 按需诊断端点）

- 日期：2026-09-07
- 状态：待审阅
- 关联：`src/modelctl/core/envs.py`、`src/modelctl/core/docker_setup.py`、`src/modelctl/core/webui/admin_envs.py`、`web/src/views/EnvsView.vue`、`web/src/components/common/TaskButton.vue`

## 1. 背景与问题

Windows 上点击环境页托管引擎的 Setup 按钮，任务立即失败并提示：

> 引擎 vllm 的托管 venv 仅支持 Linux（CUDA 推理栈依赖 nvidia-pypi + cudatools）；当前平台 'win32' 不支持。请在部署机（Linux）上执行 `modelctl env setup vllm`，或走 docker 镜像绕过 venv。

文案末尾"或走 docker 镜像绕过 venv"（`envs.py::platform_limitation_message`）**只是一句话建议，用户在 WebUI 里没有任何可操作的落地路径**：

1. 项目实际已具备 docker 旁路能力——vllm / tokenspeed / tensorrt_llm 三个适配器支持 yaml `engine.docker_image` 非空即走 `docker run`（如 `vllm/vllm-openai` 镜像），`docker_setup.py` 有现成的 `diagnose()`（4 项诊断）与 `render_instructions()`（可复制安装脚本），但这些**只在 CLI 可达，WebUI 完全未暴露**（`core/webui` 全目录 grep "docker" 零命中）。
2. 环境页对平台不支持的引擎照常亮着 Setup 按钮，用户只能"点了才知道失败"；2026-09-02 webui 设计文档（L332）已规划"平台不支持 → disabled + tooltip"但未实现。
3. sglang / aphrodite / lmdeploy 适配器**没有** docker 分支（TODO.md 2.2 已记录），对其渲染 docker 指引会误导。

## 2. 目标与非目标

**目标**：

- 环境页新增常驻"Docker 旁路"区块：展示 Docker 环境就绪状态（PATH 级轻量探测）、逐引擎是否支持 docker 运行时、官方镜像名、可复制的 yaml 配置片段与三步操作指引。
- 完整诊断按需触发：新端点暴露 `docker_setup.diagnose()` 4 项检查 + 可复制安装脚本（只读，绝不执行安装）。
- 平台不支持的引擎行：Setup 按钮置灰 + tooltip 说明并锚点引导"见下方 Docker 旁路区块"。
- Setup 失败提示（notice）检测"docker 镜像绕过"关键字时追加引导句。

**非目标**：

- WebUI 在线修改 yaml（`docker_image` 仍靠用户手工编辑文件——全仓无任何 yaml 写端点，本次不新增写能力）。
- WebUI 执行 docker 安装/镜像拉取（遵循"默认只读给指引、显式 CLI `--run` 才执行"的既有范式；Windows 上也无法安装 Docker 本身）。
- 为 sglang / aphrodite / lmdeploy 补 docker 适配器分支（独立待办，见 TODO.md 2.2）。

## 3. 后端改动

### 3.1 事实来源：`envs.py` 新增 `DOCKER_CAPABLE_ENGINES`

与平台矩阵同区新增常量，作为"适配器已实现 docker_image 分支"的单一事实来源：

```python
# 已实现 docker_image 分支的引擎（engines/<name>.py 的 _resolve_runtime 认
# cfg.docker_image）；其余托管引擎仅 venv，UI 不得渲染 docker 指引。
DOCKER_CAPABLE_ENGINES = ("vllm", "tokenspeed", "tensorrt_llm")
```

测试锚定：`tests/test_core_envs.py` 断言三者适配器 `_resolve_runtime`（或等价逻辑）在 `docker_image` 非空时返回 `("docker", image)`，防止常量与实现漂移。

### 3.2 `GET /admin/api/envs` 响应扩展

`targets[]` 每项新增两个布尔字段；顶层新增两个对象。向后兼容（纯增字段）：

```jsonc
{
  "targets": [
    {"name": "vllm", "installed": false, "detail": "未安装",
     "platform_supported": false,        // envs.platform_supports(target)
     "docker_supported": true}           // target in DOCKER_CAPABLE_ENGINES
  ],
  "unmanaged": [ /* 不变 */ ],
  "docker_env": {                        // 轻量 PATH 探测，无子进程
    "ready": false,
    "missing": ["docker 命令不在 PATH", "nvidia-smi 不在 PATH / nvidia-container-toolkit 未就绪"],
    "guide": "执行 `modelctl env setup docker` 查看安装指引（部署机上加 `--run` 可自动安装）"  // docker_setup.MSG_GUIDE
  },
  "docker_bypass": [
    {"name": "vllm", "docker_supported": true,
     "image_example": "vllm/vllm-openai:<tag>",
     "yaml_field_path": "vllm.docker_image",
     "example_yaml": "models/vllm/qwen3.8-flash-next.yaml",
     "steps": ["编辑 models/vllm/<模型>.yaml，在 vllm: 块下加一行 docker_image: <官方镜像>",
               "部署机准备 Docker + NVIDIA Container Toolkit（见上方环境诊断，或 CLI：modelctl env setup docker --run）",
               "modelctl stop <模型> && modelctl start <模型>；docker 路径要求 model 为本地已有目录（HF id 需先跑一次触发下载）"]},
    {"name": "sglang", "docker_supported": false,
     "note": "modelctl 的 sglang 适配器暂不支持 docker 运行时，官方镜像 lmsysorg/sglang 无法经 modelctl 启动；建议改用已支持的引擎或 Linux 部署机"}
  ]
}
```

- 镜像事实：vllm → `vllm/vllm-openai:<tag>`（tag 随模型官方 Day-0 镜像而定）；tokenspeed → `lightseekorg/tokenspeed:latest`；tensorrt_llm → `nvcr.io/nvidia/tensorrt-llm:<tag>`。
- `docker_env` 用 `docker_setup.path_level_missing()`（纯 `shutil.which`，绝不落子进程，不拖慢页面加载）。
- `docker_bypass` 由常量表驱动（镜像名、示例 yaml 路径、三步文案），非支持引擎仅 `note`，不造假指引。

### 3.3 新端点 `GET /admin/api/envs/docker/diagnose`

按需完整诊断（点击才调用）：

```python
@router.get("/docker/diagnose")
async def docker_diagnose(_: None = Depends(require_auth)):
    checks = await asyncio.to_thread(docker_setup.diagnose())   # 含 docker info 子进程，内建 15s 超时
    return {
        "checks": [{"key": c.key, "label": c.label, "ok": c.ok, "detail": c.detail} for c in checks],
        "instructions": docker_setup.render_instructions(),      # 可复制 root 安装脚本
    }
```

- 异常统一 try/except → 500 `{"error": {"code": "internal", ...}}` + logger（与同文件 remove_env 惯例一致）。
- 路由顺序：注册于 `GET ""`（list_envs）之后、`POST /{target}/setup` 之前。`GET /docker/diagnose` 为两段静态路径，与 `GET ""` 及两个 `POST /{target}/...` 无遮蔽。鉴权 `require_auth`。

## 4. 前端改动

### 4.1 类型与 API（`web/src/api/types.ts` / `envs.ts`）

- `EnvTarget` 增 `platform_supported: boolean`、`docker_supported: boolean`。
- 新增 `DockerEnv { ready; missing: string[]; guide }`、`DockerBypassEntry { name; docker_supported; image_example?; yaml_field_path?; example_yaml?; steps?: string[]; note? }`、`DockerDiagnose { checks: Array<{key;label;ok;detail}>; instructions: string }`。
- `EnvTargetsResponse` 增 `docker_env`、`docker_bypass`。
- `envs.ts` 新增 `dockerDiagnose(): Promise<DockerDiagnose>`。

### 4.2 EnvsView：常驻"Docker 旁路"区块

置于"受管目标"表格与"非托管引擎"区块之间，样式仿"非托管引擎"区块（`section.card`）：

- **头部行**：标题"Docker 旁路" + 副标题"托管 venv 仅支持 Linux；以下引擎可改用官方 docker 镜像绕过 venv" + 右侧 Docker 环境徽标（`docker_env.ready` → 就绪/缺失，缺失时 el-tooltip 语义用 title 展示 `missing` 明细）+ "完整诊断"按钮。
- **诊断面板**（点"完整诊断"后展开，懒加载一次）：4 项检查逐行（名称 + ok 徽标 + detail），底部 `<pre>` 展示 `instructions` + "复制"按钮（复用 ConfigView 的复制模式）。
- **引擎卡片列表**（`v-for docker_bypass`）：每引擎一行——名称 + 支持/不支持徽标；支持：镜像名 `<code>` + 可复制 yaml 片段 `<code>`（`{yaml_field_path}: {image_example}`）+ 三步 `steps` 逐条列出（含示例 yaml 路径）；不支持：`note` 文案。

### 4.3 受管表格：平台不支持行禁用 Setup

- `platform_supported === false` 时：TaskButton 传 `disabled` + `disabled-reason="托管 venv 仅支持 Linux 部署机，可走下方 Docker 旁路"`。
- TaskButton 新增 props：`disabled?: boolean`、`disabledReason?: string`；`disabled || disabledReason` 时按钮 `:disabled` 且 `title` 显示原因（现有 `phase !== 'idle'` 逻辑保留，取或）。不新增任务逻辑，纯 UI 门禁。

### 4.4 失败提示引导

EnvsView 中 TaskButton 的 `@error="(msg) => (notice = `${t.name} setup 失败：${msg}`)"` 回调改为调用新增函数 `onSetupError(name, msg)`：若 `msg.includes('docker 镜像绕过')` 则在 notice 末尾追加"旁路步骤见下方「Docker 旁路」区块"。

## 5. 错误处理

- diagnose 端点子进程异常 / 超时：`diagnose()` 内部已捕获（返回该检查项 ok=false）；路由级再兜底 try/except → 500。
- `docker_env` 探测永不抛错（`shutil.which` 语义）。
- 前端诊断失败：区块内红字提示，不弹 toast（非阻断性操作）。

## 6. 测试

- **后端 pytest**（`tests/test_webui_admin_envs.py` 扩展或新建）：
  1. `GET /envs` 含 `platform_supported/docker_supported/docker_env/docker_bypass`，6 托管引擎 + gateway 字段齐全；monkeypatch `sys.platform` 验证 Windows/Linux 两态。
  2. `GET /envs/docker/diagnose`：monkeypatch `docker_setup.diagnose/render_instructions` 断言透传形状与鉴权 401。
  3. `DOCKER_CAPABLE_ENGINES` 一致性：对 vllm/tokenspeed/tensorrt_llm 构造 `docker_image` 非空 cfg，断言 `is_docker_runtime()` 为 True；对 sglang 断言常量不含且适配器无 docker 分支（回归锚点）。
- **前端**：`npm run build` 通过；浏览器 E2E——Windows 本机应见：Setup 置灰 + tooltip、Docker 环境"缺失"徽标、旁路区块三步指引、诊断面板可展开且安装脚本可复制、失败 notice 带引导句。
- 遵守仓库规范：UI 无真实 ID、时间格式 `YYYY-MM-DD HH:mm:ss`（本设计无新增时间字段）、无任何 DDL/写操作。

## 7. 决策记录

| 决策 | 选项 | 结论 |
|---|---|---|
| 交互深度 | 纯文案 / 指引+诊断 / 可执行旁路 | 指引区 + 环境诊断（用户裁决） |
| 覆盖范围 | 全引擎 / 仅已支持 / 顺带补 sglang | 仅 vllm/tokenspeed/tensorrt_llm 给完整指引，其余标注不支持（用户裁决） |
| UI 位置 | 常驻区块 / 行内展开 / 仅失败处 | 常驻区块 + 失败提示锚点引导（用户裁决） |
| 实现方案 | A 后端数据源+按需诊断 / B 纯前端静态 / C 聚合完整诊断 | 方案 A（用户裁决） |
| docker_image 在线编辑 | 支持 / 不支持 | 不支持，指引用户手工编辑 yaml（全仓无写端点，避免新增写面） |
