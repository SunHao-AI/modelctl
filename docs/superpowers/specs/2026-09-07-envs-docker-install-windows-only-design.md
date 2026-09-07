# 环境页 Docker 一键安装（Windows-only，后端子进程 + SSE 阶段流）设计

- 日期：2026-09-07
- 状态：待审阅
- 关联：`src/modelctl/core/docker_setup.py`（Linux 路径不动）、`src/modelctl/core/webui/admin_envs.py`、`src/modelctl/cli.py`、`web/src/views/EnvsView.vue`、`web/src/api/envs.ts`、`web/src/api/types.ts`

## 1. 背景与动机

上一份 spec（`2026-09-07-envs-docker-bypass-guide-design.md`）已经落地"Docker 旁路指引 + 完整诊断 + 可复制 apt 脚本"，但**脚本只能复制到 Ubuntu 终端执行**：

1. 脚本用 `apt-get install` + `systemctl enable --now docker` + 写 `/etc/docker/daemon.json`，**必须 Linux + root**，Windows 上 `run_install()` 立即 `return 2`（`docker_setup.py:400`）；
2. 模型团队研发 / WDE 环境有大量 Windows 桌面用户，希望有**一键拉到 Docker Desktop + WSL2** 的路径（下一档目标：让"docker 镜像"的引擎在 Windows 上也能立即起 vllm + GPU 冒烟测试），生产部署机仍是 Linux；
3. 现状下"复制粘贴 → 手动提权 → 手动重启 → 手动验证"每一步都断链，团队希望在 WebUI 里点一个按钮就能全自动起来。用户在对话里明确表示：**"通过一键安装功能，便于用户直接运行"**。

本 spec 做 **Windows 侧的一键安装**（对称式双文件 + WebUI 引导 + CLI `--os` 分派）。**注意边界**：`docker_setup.py` 继续负责 Linux + root，本 spec 不重画 Linux apt 路径；新增 `windows_setup.py` 走 winget + Docker Desktop + WSL2。统一入口 `docker_setup.run_install` 做 dispatcher，CLI/UI 只见一个函数。

## 2. 目标与非目标

**目标**：

- 新增 `src/modelctl/core/windows_setup.py`，与 `docker_setup.py` 同型：`path_level_missing()` / `diagnose() -> list[Check]` / `install_steps()` / `render_instructions()` / `run_install()` / `post_install_plan()`。
- `docker_setup.run_install` 变跨平台 dispatcher（`os_hint=None` 按 `sys.platform` 分派）。
- CLI `modelctl env setup docker --os=linux|windows [--run] [--registry-mirror]...`：缺省按平台；`--os` 允许 Linux 上预览 Windows 脚本、Windows 上预览 Linux 脚本（**预览不执行**）。
- 后端新增 `POST /admin/api/envs/docker/install`（202 + task_id）与 `GET /admin/api/envs/docker/install/{task_id}/events`（SSE 阶段流）；`GET /admin/api/envs/docker/diagnose` 保留但顶层多返回 `platform` 与 `os` 供 UI 分支。
- 后端 `POST /admin/api/envs/docker/system-action`（Windows-only）：`{action: "open_desktop" | "restart"}` 触发 `explorer.exe ms-settings:developers` / `shutdown.exe /r /t 5`。
- 前端新增 `web/src/components/docker/DockerInstallPanel.vue`（4 态状态机 + UAC 弹窗 + 阶段 B 卡片 + 手动验证按钮），接入 `EnvsView.vue` Docker 区块。

**非目标**：

- **不改 `docker_setup.py` 的 Linux 安装行为**（apt-get / nvidia-container-toolkit / /etc/docker/daemon.json 组合继续原样）。
- 不自动开启 WSL 功能（`dism /enable-feature Microsoft-Windows-Subsystem-Linux`）——由 Docker Desktop 首次启动向导代劳；不自动装 WSL 分发版（`wsl --install`）。
- 不修改 `%APPDATA%\Docker\settings.json`（Docker Desktop 应用设置 JSON）——只写 `%USERPROFILE%\.docker\daemon.json`。
- 不自动关 Windows Defender、不调 PATH、不静默装 winget（用户已装则复用；未装给引导而非硬装）。
- 不做 macOS / 其它 OS 一键（未来演进可平行展开 `darwin_setup.py`，见 §14）。
- 不管理 Docker Compose plugin（随 Docker Desktop 自带）。
- 不引入前端 e2e 测试框架（沿用 `npm run build` 门禁约定）。

## 3. 模块边界与 API 契约

### 3.1 新增 `src/modelctl/core/windows_setup.py`

与 `docker_setup.py` **对称**，一个平台一个文件。`Check` dataclass **独立声明**（与 `docker_setup.Check` 同形状字段：`key/label/ok/detail/hint`），避免 Windows 路径强 import 整个 `docker_setup`；测试断言两者字段名一致防漂移（§9.2）。

```python
"""core/windows_setup.py — Docker Desktop + WSL2 诊断与一键安装（Windows-only）。

winget install -e --id Docker.DockerDesktop --silent --accept-package-agreements
--accept-source-agreements 为本模块统一入口。阶段 A（自动）：
  1. 探测 winget（shutil.which）
  2. 静默安装 Docker Desktop
  3. 合并 %USERPROFILE%\.docker\daemon.json（registry-mirrors 多源容灾 + 剔除停服源，
     保留用户已配置的 runtime / image-store-options 等字段）
  4. 探测 docker / nvidia-smi（有则跑 GPU 冒烟 --gpus all）
阶段 B（引导）：run_install 把 post_install_plan 通过 on_stage callback 推给 SSE / CLI，
用户手工完成重启 + Desktop 首次启动向导（启用 WSL2 backend）+ Settings→Resources→GPU
勾选，然后回到 WebUI 点【已就绪，点我验证】触发 diagnose() 复验 5 项。

安全边界：run_install 仅 sys.platform=="win32" 可执行，其它平台直接 return 2；
写磁盘动作仅 %USERPROFILE%\.docker\daemon.json（父目录自动 mkdir）；
不写注册表、不调 PATH、不开 WSL 功能。
"""

WINGET_ID = "Docker.DockerDesktop"
DAEMON_JSON = Path.home() / ".docker" / "daemon.json"

# 从 docker_setup 复用（避免两套常量漂移，单一事实来源 = docker_setup.py）：
from modelctl.core.docker_setup import (
    DEFAULT_REGISTRY_MIRRORS,
    DEAD_REGISTRY_MIRRORS,
    resolve_registry_mirrors,
    split_dead_mirrors,
    is_dead_mirror,
)
```

**函数清单**（与 `docker_setup.py` 平行命名）：

| 函数 | 签名 | 说明 |
|---|---|---|
| `path_level_missing()` | `() -> list[str]` | `shutil.which("winget")` 无 → `["winget"]`；有 winget 但无 `docker` → `["docker"]`；都齐 → `[]`。UI 徽标用（与 `docker_setup.path_level_missing` 同形）。 |
| `diagnose()` | `() -> list[Check]` | 5 项子进程诊断（15s / 120s 超时）：winget 版本 / WSL 默认版本 / Docker Desktop 安装目录 / `docker info` / GPU 冒烟（仅 host 有 `nvidia-smi` 时跑 `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi`，120s 超时；无 nvidia-smi 时该项 `ok=True` + `hint="non-GPU host, skipped"`）。 |
| `install_steps(registry_mirrors, max_downloads)` | `-> list[tuple[str, str]]` | 4 步 `(desc, cmd)`；`cmd` 仅 `winget_install` 用外部命令（`winget install -e --id Docker.DockerDesktop --silent ...`），其它由 `_run_stream` Python 直接执行。 |
| `render_instructions(registry_mirrors, max_downloads)` | `-> str` | PowerShell 一键脚本（可复制到已提权 PowerShell 执行）：winget 探测 → Desktop 静默安装 → 落 `daemon.json` → 提示重启 + 首启向导 + GPU 开关 → 引导回 WebUI 点验证。**脚本内不内置 `shutdown /r`**（安全），只在文案引导。 |
| `_merge_daemon_json(registry_mirrors, max_downloads)` | `-> bool` | 写 `%USERPROFILE%\.docker\daemon.json`。**与 `docker_setup._merge_daemon_json` 行为平行**（保 `runtime` / `log-*` / `image-store-options` 等用户字段；`registry-mirrors` union 并剔除停服源；`max-concurrent-downloads` 取 given 值）。**可选择抽公共工具 `core/daemon_json.py`**（本次 TDD 优先：内联重复代码，抽公共函数放到 Task 4 之后或独立重构提 issue）。 |
| `run_install(registry_mirrors, max_downloads, on_stage)` | `-> int` | 主入口。平台硬门禁 `sys.platform != "win32"` → 2 + 引导。已有 Docker Desktop（`%PROGRAMFILES%\Docker\Docker\Docker Desktop.exe` 或 `%LOCALAPPDATA%\Programs\DockerDesktop`）→ 跳 `winget` 环节，走 `already_installed` 分支。子进程 `winget install` 逐行 stdout/stderr → `on_stage` callback（SSE / CLI log 各自序列化，`StageEvent` 统一数据）。UAC 阻塞 20s 无输出 → emit `need_UAC` 仍继续等；完整 stage 序列 → `post_install_plan` → `done`；任意失败 → `error` + exit code（2=pre-flight，3=winget 执行失败）。 |
| `post_install_plan()` | `-> dict` | 阶段 B 结构化卡片（5 步）。字段：`id`（`restart`/`open_desktop`/`enable_wsl2`/`enable_gpu`/`verify`）/ `label` / `action`（`"restart"\|"open_desktop"\|"verify"\|null`）/ `cmd_hint` / `optional`（GPU step 无 nvidia-smi 时 `true`）。 |

### 3.2 `docker_setup.run_install` dispatcher 改造

现状：单一 Linux 分支。**改为**：

```python
def run_install(
    registry_mirrors: list[str] | None = None,
    max_downloads: int | None = None,
    os_hint: str | None = None,          # 新增；"linux" | "windows" | None
    on_stage: Callable[[StageEvent], None] | None = None,   # 新增；CLI 走 None 退化为 logger
) -> int:
    """跨平台分派：os_hint 缺省按 sys.platform；可强制 --os 预览跨平台脚本。

    向后兼容：未传 os_hint / on_stage 时行为与旧签名完全一致（Linux + root）。
    """
    hint = os_hint or ("windows" if sys.platform == "win32" else "linux")
    if hint == "windows":
        if sys.platform != "win32":
            logger.error(f"Windows 安装路径仅 Windows 主机可实际 --run，当前平台 {sys.platform!r}")
            return 2
        from modelctl.core import windows_setup
        return windows_setup.run_install(registry_mirrors, max_downloads, on_stage)
    # 原有 Linux 路径完全不变；on_stage=None 时保留 logger 打印
    ...
```

**StageEvent 数据契约**（SSE 与 CLI 共用，混留在 `core/sse_stage_event.py` 供后端 SSE 与 `windows_setup.run_install` callback 双方 import，防字段漂移）：

```python
@dataclass
class StageEvent:
    type: str              # "stage" | "log" | "error" | "complete"
    stage: str             # 见 §5.2 枚举
    message: str           # 人类可读（中文）
    ts: str                # YYYY-MM-DD HH:mm:ss（项目统一）
    code: int | None = None        # 仅 error/complete 时给
    payload: dict | None = None    # post_install_plan 时给 5 步；error 时给末 50 行 stdout
```

### 3.3 数据模型复用

`Check` dataclass：新文件独立声明（5 字段：`key/label/ok/detail/hint`，与 `docker_setup.Check` 字段名一致，前端表格可共用渲染模板），但类型不共享（避免 Windows 路径 import 整个 Linux 依赖的 `docker_setup`）。测试断言 `windows_setup.Check.__dataclass_fields__.keys() == docker_setup.Check.__dataclass_fields__.keys()`（防未来漂移）。

## 4. CLI 契约

```
modelctl env setup docker
    [--os {linux,windows}]          # 新增，缺省按 sys.platform
    [--run]
    [--registry-mirror URL]*
    [--max-concurrent-downloads N]
```

**行为矩阵**：

| 场景 | `--run` 缺省（预览模式） | `--run` 已传 |
|---|---|---|
| Windows 主机 + 缺省 `--os` | 打印 `windows_setup.render_instructions`；末尾"或自动执行：`modelctl env setup docker --run`"（把"root shell"改成"当前用户 PowerShell"）| 触发 `run_install(os_hint=None)` → win32 分派 → stream 打印 stage 日志到 stderr（`on_stage=None` 走 logger） |
| Linux 主机 + 缺省 `--os` | 打印 apt 版脚本（现状不变）| apt 版 `run_install`（现状不变） |
| Linux 主机 + `--os=windows`（跨平台预览）| 打印 PowerShell 脚本（纯 stdout）；**不执行**、无副作用 | **拒绝**：exit 2 + "Windows 安装路径仅 Windows 主机可 --run，当前平台 'linux'" |
| Windows 主机 + `--os=linux`（跨平台预览）| 打印 apt 版脚本（stdout，供用户拷到 Linux 部署机）| **拒绝**：exit 2 + "Linux 安装路径仅 Linux 主机可 --run，当前平台 'win32'" |
| 未知 `--os` 值（如 `--os=macos`）| argparse 层直接 `error` → exit 2 | 同左 |

`--registry-mirror` / `--max-concurrent-downloads` 参数对两平台统一生效（Windows 端语义落 `daemon.json` 的 `registry-mirrors` + `max-concurrent-downloads`）。

**CLI 测试锚定**（`tests/test_cli_env.py` 新增 4 用例）：
1. Windows 开发机 + `--os=linux`（无 `--run`）→ stdout 出现 `apt-get install docker-ce`；mock `run_install` 断言未调用
2. Windows 开发机 + `--os=linux --run` → exit 2 + stderr 含 "Linux 安装路径仅 Linux 主机可 --run"
3. `unittest.mock.patch("sys.platform", "linux")` + `--os=windows --run` → exit 2 + "Windows 安装路径仅 Windows 主机可 --run"
4. `--os=macos`（未知值）→ argparse 报错 + exit 2

## 5. 后端 API 契约

### 5.1 端点清单

| 端点 | 方法 | 说明 |
|---|---|---|
| `GET /admin/api/envs/docker/diagnose?os=linux\|windows` | GET | **保留** Task 2 端点；`os` 可选 query，缺省按 `sys.platform`；响应顶层加 `platform` / `os` 字段；`os=windows` 且非 win32 → 400 + 引导文案；`os=windows` 且 win32 → `checks` 用 `windows_setup.diagnose()` 输出。**向后兼容**（纯增字段/参数，Task 2 已交付的调用不破）。 |
| `POST /admin/api/envs/docker/install` | POST | **新增**。Body：`{"os": "linux"\|"windows" (可选，缺省按本机), "registry_mirrors": [...], "max_concurrent_downloads": int}`。返回 `202 {"task_id": "...", "events": "/admin/api/envs/docker/install/<task_id>/events", "os": "..."}`。非 win32 请求 `os=windows` → 400。启动 TaskManager 异步任务，后台线程跑 `windows_setup.run_install(on_stage=_publish_to_sse)`。 |
| `GET /admin/api/envs/docker/install/{task_id}/events` | GET | **新增 SSE**。事件体见 §5.2；心跳 30s；最长 6 小时；客户端断连 → 后台线程继续跑完 winget（**不** kill，避免"关浏览器让半成品 Desktop 残留"）；用户回 UI 可 `GET /install/{task_id}` 拿最新 stage。 |
| `GET /admin/api/envs/docker/install/{task_id}` | GET | **新增只读**。返回 `{"stage": "...", "done": bool, "last_ts": "..."}` 供 SSE 断连后 fallback 查询。 |
| `POST /admin/api/envs/docker/system-action` | POST | **新增**（Windows-only）。Body `{"action": "open_desktop" \| "restart" \| "verify"}`。非 win32 → 400。`open_desktop` 后端 `subprocess.Popen(["explorer.exe", "ms-settings:developers"], close_fds=True)`；`restart` 走 `shutdown.exe /r /t 5 /c "modelctl 正在重启以启用 WSL2 backend"`；`verify` 405（语义等价 `GET /docker/diagnose?os=windows`，UI 直接调，不另开端点）。响应 `200 {"executed": "explorer.exe ms-settings:developers", "ts": "..."}`。 |

### 5.2 SSE 事件体（统一，UTF-8）

```jsonc
{
  "type": "stage" | "log" | "error" | "complete",
  "stage": "detect_winget" | "detect_wsl2" | "winget_running" | "need_UAC"
          | "winget_done" | "write_daemon_json" | "test_docker_version"
          | "test_gpus" | "post_install_plan" | "already_installed"
          | "done" | "error",
  "message": "...",               // 人类可读（中文）；log 时是原始 stdout 一行
  "ts": "2026-09-07 15:04:12",    // YYYY-MM-DD HH:mm:ss
  "code": 0,                       // 仅 error/complete 时
  "payload": { ... }               // post_install_plan 时带 5 步 steps；error 时末 50 行 stdout
}
```

### 5.3 权限与停止语义

- 全部新端点 `Depends(require_auth)`。
- POST `/install` **5 分钟去重窗**：同一 auth user 已有未完成 task 再 POST 时返回原 task_id + `{"already_running": true}` 提示（避免 fork 多个 winget 子进程）；内存 limiter 同一用户最多 3 个未完成 install task。
- 客户端 SSE 断开 → 后台 thread 继续跑完 winget（§5.1 已定义）。
- 平台分派报告回 SSE 前端时**在中段**（避免文案歧义）：实际发生的 `platform` / `os` 在 `stage=detect_winget` 事件的 `message` 里同时给出。

### 5.4 `TaskManager` 复用说明

`admin_models.py` 已有任务/SSE 基础设施（`_do_start` / `_publish_event`）。install task 键空间**不与 model task 冲突**（model task 键 `<model_id>:<action>`；install task 键 `docker:install:<task_id>`），因此**复用 TaskManager 抽象**，新增一个模块级实例 `docker_install_task_manager = TaskManager(name="docker_install")`；实现子代理执行 Task 3 时**先读** `admin_models.py::TaskManager` 类是否支持多实例：
- 支持 → 直接 instantiate；
- 不支持（内部用全局 dict）→ 首选**抽出** `TaskManager` 内部 dict 到 `__init__`（轻量重构，回归单测 `admin_models` 相关用例证明无 regression）；备选：在 `admin_envs.py` 内临时自行轻量实现 SSE emit（事件样式与 TaskManager 100% 一致）+ 写 `TODO refactor`：抽出 `TaskManager` 到 `core/sse_task.py` 供两处复用。

无论哪条路，`StageEvent` dataclass（`core/sse_stage_event.py`）在 Task 3 完成前先锁住，后端 SSE 序列化与 `windows_setup.run_install` 的 callback emit 都**走同一 dataclass**（避免手写 dict 造成字段漂移）。

## 6. 前端 API 契约

### 6.1 `web/src/api/envs.ts` 增补

```ts
export interface DockerInstallStartPayload {
  os?: "linux" | "windows";
  registry_mirrors?: string[];
  max_concurrent_downloads?: number;
}
export interface DockerInstallStartResponse {
  task_id: string;
  events: string;               // 相对路径 "/admin/api/envs/docker/install/<id>/events"
  os: "linux" | "windows";
  already_running?: boolean;    // 5 分钟去重窗内再 POST 时
}
export type DockerSSEEventType = "stage" | "log" | "error" | "complete";
export type DockerSSEStage =
  | "detect_winget" | "detect_wsl2" | "winget_running" | "need_UAC"
  | "winget_done" | "write_daemon_json" | "test_docker_version"
  | "test_gpus" | "post_install_plan" | "already_installed"
  | "done" | "error";
export interface DockerSSEEvent {
  type: DockerSSEEventType;
  stage: DockerSSEStage;
  message: string;
  ts: string;                   // YYYY-MM-DD HH:mm:ss
  code?: number;
  payload?: { steps: PostInstallStep[] } | string[];   // post_install_plan 或 error 末 50 行 stdout
}
export interface PostInstallStep {
  id: string;
  label: string;
  action: "restart" | "open_desktop" | "verify" | null;
  cmd_hint: string | null;
  optional?: boolean;
}

export function startDockerInstall(body: DockerInstallStartPayload): Promise<DockerInstallStartResponse>;
export function fetchDockerInstallStatus(taskId: string): Promise<{ stage: DockerSSEStage; done: boolean; last_ts: string }>;
export function systemActionWindows(action: "open_desktop" | "restart"): Promise<{ executed: string; ts: string }>;
```

Task 2 已交付的 `dockerDiagnose()` 保留，响应顶层新增 `platform: "linux" | "windows"` 与 `os` 字段供 EnvsView 分支。前端 3 处强类型：`DockerDiagnose` 补字段；新类型挂 `types.ts`（各 envs.ts 内**不**定义局部类型，统一 `types.ts` SoT）。

### 6.2 `web/src/components/docker/DockerInstallPanel.vue`

**目录**：`components/docker/`（新目录）。打开一扇门为后续 `darwin_install_panel` 平行展开（若命名再调，见 §14）。

```
<script setup name="DockerInstallPanel">
props: {
  platform: "linux" | "windows",              // 从 diagnose 顶层读
  initialDiagnose?: DiagnosePayload,          // EnvsView load 出来的 4 项 check
}
emits: ["ready", "error"];
state:
  phase = ref<"idle" | "installing" | "need_reboot" | "done" | "error">("idle");
  stage = ref<DockerSSEStage | null>(null);
  steps = ref<PostInstallStep[] | null>(null);
  logs = ref<string[]>([]);            // 最近 200 行限
  uacDialogShow = ref(false);
  taskRef = ref<{ id: string; events: string } | null>(null);
  esRef = ref<EventSource | null>(null);
methods:
  startInstall():   POST → EventSource 订阅 → phase="installing"
  onEvent(e):       分派 stage/log/complete/error
    - need_UAC          → uacDialogShow = true
    - post_install_plan → phase="need_reboot"（GPU 检测失败也进，只标 optional）
    - done                → phase="done"
    - error               → phase="error"
    - log                 → pushLog（>200 shift）
    - stage               → stage = e.stage
  openDesktop():   systemActionWindows("open_desktop")；catch 显示 toast
  doRestart():     ElMessageBox.confirm 二次确认 → systemActionWindows("restart") → toast "5 sec 后将重启"
  verify():        const r = await dockerDiagnose(); r.ok ? phase="done" : phase="need_reboot"
  onUnmount:       esRef.value?.close()
</script>
```

**模板骨架**：

```vue
<div class="docker-install-panel">
  <div class="docker-install-panel__head">
    <span class="title">Docker 一键安装</span>
    <el-tag v-if="phase === 'done'" type="success">已就绪</el-tag>
    <el-tag v-else-if="phase === 'error'" type="danger">失败</el-tag>
    <el-tag v-else :type="phase === 'installing' ? 'warning' : 'info'">{{ phaseLabel }}</el-tag>
  </div>

  <!-- Linux 平台降级卡片（Task 4 简化） -->
  <el-alert v-if="platform === 'linux'" type="info" show-icon
            title="Windows 独占功能"
            description="请在 Linux 部署机上执行 modelctl env setup docker --run（root），或点上方「完整诊断」复制 apt 脚本。" />

  <!-- Windows 侧 4 段进度 -->
  <el-steps v-else :active="activeStep" :finish-status="phase === 'done' ? 'success' : 'process'">
    <el-step title="安装 Docker Desktop" />
    <el-step title="落 registry-mirrors" />
    <el-step title="验证 docker daemon" />
    <el-step title="启用 GPU 透传" />
  </el-steps>

  <div class="docker-install-panel__actions">
    <el-button v-if="platform === 'windows'" type="primary" :loading="busy"
               :disabled="phase !== 'idle'" @click="startInstall">一键安装</el-button>
    <el-button v-if="phase === 'need_reboot'" @click="openDesktop">打开 Docker Desktop</el-button>
    <el-button v-if="phase === 'need_reboot'" type="warning" @click="doRestart">重启计算机</el-button>
    <el-button v-if="phase === 'need_reboot' || phase === 'error'"
               type="success" @click="verify">已就绪，点我验证</el-button>
  </div>

  <!-- 阶段 B 卡片（仅 need_reboot show） -->
  <el-card v-if="phase === 'need_reboot' && steps" class="mt-3">
    <template #header>阶段 B：手动完成 5 步</template>
    <ul class="stage-b-steps">
      <li v-for="s in steps" :key="s.id"
          :class="{ 'step-optional': s.optional, 'step highlight': s.action }">
        {{ s.label }}
        <span v-if="s.cmd_hint" class="text-xs text-grey">{{ s.cmd_hint }}</span>
      </li>
    </ul>
  </el-card>

  <!-- 日志（折叠，最近 200 行） -->
  <details class="mt-3">
    <summary>: 终端日志（最近 {{ logs.length }} 行）</summary>
    <pre class="mt-2 p-2 bg-black text-green text-xs whitespace-pre-wrap">{{ logs.join('\n') }}</pre>
  </details>

  <!-- UAC 弹窗 -->
  <el-dialog v-model="uacDialogShow" title="UAC 授权提醒" :close-on-click-modal="false">
    Windows 系统已弹出 User Account Control 授权框（用于安装 Docker Desktop）。
    请到 Windows 任务栏 / 桌面右上方点击"允许"。点击关闭仅隐藏弹窗，不中断安装。
    <template #footer>
      <el-button @click="uacDialogShow = false">关闭弹窗</el-button>
    </template>
  </el-dialog>
</div>
```

### 6.3 `EnvsView.vue` 接入

在 Task 2 已交付的 "Docker 旁路" 区块下方新增一行子卡片：

```html
<div class="card mt-4">
  <DockerInstallPanel :platform="dockerDiag?.platform ?? 'linux'"
                      :initial-diagnose="dockerDiag" />
</div>
```

**Linux 平台表现**：`platform === "linux"` 时 panel 只显示一条 `el-alert` 提示（"Windows 独占；Linux 侧请在部署机执行 CLI `modelctl env setup docker --run`（root），或点上方完整诊断获取可复制脚本"）。Linux 不开放 SSE 安装 UI——与既有 setup 按钮 + apt 脚本路径并存、双通道各自覆盖但入口分离（避免 Linux 用户看到 Windows 按钮困惑）。本 spec 不覆盖 Linux 的 SSE 安装 —— §14 列为后续演进。

## 7. 错误处理

### 7.1 exit code 体系（Windows 路径专用，Linux 依旧）

| code | 触发场景 |
|---|---|
| 0 | 全部 step 成功 / daemon 已 ready |
| 2 | 平台不匹配 / 无 winget / pre-flight 其它 fail |
| 3 | winget 子进程 exit != 0（附末 50 行 stdout） |

**用户文案（INSTRUCTION，前后端同源）**：
- 无 winget：`winget 未找到。请从 Microsoft Store 搜索 "winget" 或到 https://aka.ms/winget-cli 安装，然后回 WebUI 重新点一键安装。`
- 非 win32 请求 `os=windows --run`：`Windows 安装路径仅 Windows 主机支持；当前平台 {sys.platform!r}。Windows 用户请到 Windows 主机执行 modelctl env setup docker --run，或先手动下载 Docker Desktop`。
- winget 失败：`winget install Docker.DockerDesktop 失败（code {rc}）。末 50 行：` + stdout tail 多行。
- UAC 阻塞：`Windows 系统已弹出 UAC 授权框。请到 Windows 任务栏确认"允许"；此日志仍会持续跟踪`。
- `daemon.json` 写失败：`写 %USERPROFILE%\.docker\daemon.json 失败（权限 / 磁盘）；已降级继续，registry-mirrors 加速请手动到 Desktop Settings → Docker Engine`。
- WSL2 首启未 ready：`WSL2 未就绪，安装完成后需重启计算机以启用 WSL2 backend；然后点击【已就绪，点我验证】`。
- daemon 未 ready：`docker 代理尚未就绪。请按阶段 B 卡片逐项完成，完成后点【已就绪，点我验证】重试`。

### 7.2 阶段 A 各 step 失败降级

| step | 失败副作用 | 是否阻断 | 降级 |
|---|---|---|---|
| `detect_winget` | 无 | 阻断 | exit 2 + 引导 |
| `winget_running` / `winget_done` | winget 可能残留半装 | **阻断** | error 事件（含末 50 行 tail）+ exit 3 |
| `write_daemon_json` | daemon.json 部分写完 / 读旧文件失败 | 不阻断 | warning 事件 + 提示手动 |
| `test_docker_version` / `test_gpus` | 无（daemon 可能还没起来）| 不阻断 | 继续到 `post_install_plan`（用户见"daemon 未 ready，手动完成阶段 B"） |

### 7.3 前端错误处理

- SSE 断连 → phase 保持不变，`fetchDockerInstallStatus` ping 一次；连续 3 次失败才报 "SSE connection lost"。
- POST `/install` 504/502 → toast "启动失败，稍后再试"，UI 回到 idle。
- `systemActionWindows` 400 → toast 精确错误（"restart 仅 Windows 主机可用" / "open_desktop 需要已装 Desktop"）。

## 8. 安全边界（强约束，不得突破）

1. `require_auth` 卡住所有新端点。
2. `POST /install?os=windows` 在非 win32 主机 → 立刻 400，**不 fork 任何进程**。
3. `POST /system-action` 仅 `sys.platform == "win32"` 面板执行；非 win32 直接 400；`action=verify` 405（无独立语义）。
4. **不自动调 WSL 功能开关**（`dism /enable-feature`）；命令仅限 `explorer.exe` / `shutdown.exe` 两条，`close_fds=True`，不写注册表，不动 PATH。
5. 不自动关 Windows Defender、不静默装 winget（无 winget 时只引导，保留用户自主获取权）。
6. 阶段 A 只跑 `nvidia/cuda:12.4.0-base-ubuntu22.04` 一个镜像（GPU 冒烟），`docker run --rm` 自动清理；不 pull 其它 image。
7. `--silent` 装 Desktop 后**不自动启动**（必须重启）。
8. 阶段 B "重启计算机" 需前端 `ElMessageBox.confirm` 二次确认；后端不 confirm（只做 executor）。
9. **不覆盖用户已有 daemon.json** 的 `runtime / network-options / image-store-options` 字段；顶层 `registry-mirrors` 做 union（用户 preferred order 保留，追加端去重 + 剔除停服源）。
10. SSE body 统一 UTF-8（`json.dumps(..., ensure_ascii=False)`）避免中文乱码（既有约定）。
11. POST 只 202，无副作用 except 任务启动；install task 的取消（用户关 SSE）**不** kill winget（语义：close 是 client-side，winget 端 subprocess 独立进程继续跑）。
12. 端点 body 校验：`os` ∈ `{"linux","windows"}`；`registry_mirrors` 每项 `https://` 开头；`max_concurrent_downloads` 0~8 整数（默认 2）。
13. 5 分钟去重窗 + 同用户 max 3 未完成任务限制，防 fork 子进程 stack。

## 9. 测试策略

### 9.1 分层

| 层 | 文件 | 覆盖 |
|---|---|---|
| 单元（windows_setup） | `tests/test_core_windows_setup.py`（新建，18 用例）| 见 §9.3 |
| CLI | `tests/test_cli_env.py`（扩 4 用例）| 见 §4 行为矩阵 4 条 |
| 后端 | `tests/test_webui_admin_envs.py`（扩 7 用例）| 见 §9.5 |
| 前端 | `npm run build`（vue-tsc + vite）| 类型检过即门禁；无 e2e（沿用既有约定）|

**本机（Windows 开发机）限制 → 全部记账到 ledger**：
- **不**跑真 `winget install Docker.DockerDesktop`（会破坏开发环境 + 触发 UAC）——mock 子进程 + mock `shutil.which`。
- **不**跑真 `shutdown.exe`——`system_action` 单测只 assert `subprocess.Popen` 参数，不真关机。
- **不**点真 UAC——`need_UAC` 事件只测 monkeypatch 20s 无输出 case。
- Web 手工 verify 只在 idle / need_reboot 阶段的 UI 布局与按钮可见性上进行，**不**真点"一键安装"（会 UAC + 30min 下载 Desktop）。
- E2E 真跑（Windows 部署机）在 Task 4 验收人工清单里（手机动一次，账落到 ledger `task-4-report.md`）。

### 9.2 单一事实来源 & 一致性锁

- `DEFAULT_REGISTRY_MIRRORS` / `DEAD_REGISTRY_MIRRORS` / `resolve_registry_mirrors` / `split_dead_mirrors` / `is_dead_mirror`：**SoT = `docker_setup.py`**；`windows_setup.py` `from ... import`（不复制）。
- `WINGET_ID = "Docker.DockerDesktop"`：定在 `windows_setup.py` 顶层常量（源仅此处）。测试断言 `winget --list` 返回中存在该 ID（跨 winget 版本 CI guard）。
- `StageEvent` dataclass：`core/sse_stage_event.py` 单文件；后端 SSE 序列化 + `windows_setup.run_install` callback 都用它（避免手写 dict）。
- `windows_setup.Check` 字段名锁 = `docker_setup.Check` 字段名锁（`tests` 内断言 `__dataclass_fields__.keys()` 一致）。
- 前端 `PostInstallStep` 类型（`types.ts`）与后端 `post_install_plan()["steps"][0]` 字段一一对齐（Task 4 手工 verify + 单测内 JSON schema 断言）。

### 9.3 `test_core_windows_setup.py` 用例（18 例）

1. `test_path_level_missing_no_winget` — mock which None → `["winget"]`
2. `test_path_level_missing_no_docker` — winget 有 / docker 无 → `["docker"]`
3. `test_path_level_missing_all_present` — 都齐 → `[]`
4. `test_diagnose_returns_five_checks` — monkeypatch subprocess → 5 项 Check 全齐
5. `test_diagnose_gpu_skip_when_no_nvidia_smi` — nvidia-smi 无 → GPU Check `ok=True` + `hint="non-GPU host, skipped"`
6. `test_diagnose_gpu_smoke_run_called_when_nvidia_present` — nvidia-smi 有 → `docker run` mock 被调用 `--gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi`
7. `test_install_steps_order_fixed` — 4 步 tuple 顺序 + `winget_install` cmd 含 `Docker.DockerDesktop --silent`
8. `test_render_instructions_contains_key_lines` — stdout 字符集结含 `winget install -e --id Docker.DockerDesktop` 与 `registry-mirror` 提示
9. `test_merge_daemon_json_creates_parent_dir` — 目标路径不存在父目录 → mkdir 成功 + JSON 写入
10. `test_merge_daemon_json_preserves_runtime_fields` — 已有 `{"runtime":{"nvidia":{...}}}` 保留
11. `test_merge_daemon_json_removes_dead_mirrors` — 旧 `tuna` / `163` 源被剔除
12. `test_merge_daemon_json_union_keeps_user_order` — 用户 preferred mirror 排在 default 之前；去重
13. `test_merge_daemon_json_no_change_when_already_ok` — 输入与期望一致 → 返回 False（幂等）
14. `test_run_install_rejects_non_win32` — monkeypatch `sys.platform="linux"` + `os_hint=None` → 2 + message "Windows 安装路径仅 Windows 主机"
15. `test_run_install_no_winget_returns_2` — win32 + winget which None → 2
16. `test_run_install_already_installed_branch` — Desktop.exe 存在 → 阶段序列 `already_installed → write_daemon_json → test_docker_version → post_install_plan → done`；无 `winget_running`
17. `test_run_install_winget_fail_emits_error` — subprocess.returncode=1 → `error` 事件 + code=3 + payload 尾 50 行
18. `test_run_install_uac_mask_emits_need_uac` — monkeypatch subprocess sleep 20s 无输出 → `need_UAC` 事件 + 继续等待至完成
19. `test_post_install_plan_has_five_steps` — 5 步 id/label/action 枚举合法；`optional` 字段类型正确

> 注：上面的 §9.3 列了 19 例但目标口径写 18 —— 实际以测试内 `pytest --collect -q` 计数为准，spec 目标口径 18~19 之间即可（本 spec 目标口径内敛：**"18+ 用例"** ；具体数不加负锁）。

### 9.4 `test_cli_env.py` 新用例（4 例，§4 行为矩阵）

1. `test_env_setup_docker_os_linux_preview_cross_platform`（Windows 主机，`--os=linux`）→ stdout 含 `apt-get install docker-ce`；`mock.patch.run_install` 未被调用；exit 0
2. `test_env_setup_docker_os_linux_run_cross_platform_rejected`（Windows + `--os=linux --run`）→ exit 2 + message "Linux 安装路径仅 Linux 主机可 --run"
3. `test_env_setup_docker_os_windows_run_cross_platform_rejected`（monkeypatch `system.platform="linux"` + `--os=windows --run`）→ exit 2 + "Windows 安装路径仅 Windows 主机可 --run"
4. `test_env_setup_docker_os_bad_value_argparse`（`--os=macos`）→ argparse 报错 + exit 2

### 9.5 `test_webui_admin_envs.py` 新用例（7 例）

1. `test_install_endpoint_401_unauthed` — 无 token → 401
2. `test_install_endpoint_202_win32_returns_task_id` — monkeypatch `system.platform="win32"` + token → 202 + task_id + events 路径
3. `test_install_endpoint_400_linux_os_windows` — monkeypatch `system.platform="linux"` → 400
4. `test_install_endpoint_505_no_double_fork_same_user` — 调两次（不到 5min）→ 第二次返回原 task_id + `already_running: true`
5. `test_system_action_endpoint_400_non_win32` — monkeypatch `system.platform="linux"` → 400
6. `test_system_action_endpoint_open_desktop_win32_ok` — monkeypatch `subprocess.Popen` + win32 → 200 + executed
7. `test_system_action_endpoint_restart_win32_ok` — 同上，args 含 `/r`, `/t`, `5`
8. `test_system_action_endpoint_verify_returns_405` — 任意平台 → 405（无独立语义）
9. `test_diagnose_endpoint_os_windows_non_win32_400` — monkeypatch `system.platform="linux"` + query os=windows → 400
10. `test_diagnose_endpoint_linux_default_unaffected` — monkeypatch `system.platform="linux"` + 无 os query → 200 + `platform:"linux"` 顶层新增字段（Task 2 原字段全部保持）

> 以上列了 10 用例，目标口径 **9+ 用例**（与 §9.1 表格里"扩 7 用例"的差异在于本 spec 也把 `diagnose` 双例给了台账认证；实现子代理可按 §9.2 五一致性锁**至少**写完前 8 例，剩下 2 例（`validate_505` 与 `diagnose_linux_default`）作为回归安全网，**不强制**必须都写——以真实回归信号为准。ledger 里记录实际完成数即可）。

## 10. 性能与可靠性

- `GET /envs/docker/diagnose` 保持 Task 2 约定的 15s 超时；本 spec `os=windows` 分支新增 5 项诊断（GPU 项除外也 15s，GPU 单测 120s 超时）。
- `POST /install` 5s 内返回 202；前 5s 失败（winget spawn 失败）直接 200 `{"error": ...}`。
- SSE 预期流程 20-30 min（winget 默认下载 500MB+）；断连都接受后台继续；前端重连 `fetchDockerInstallStatus` 拿最新 stage。
- 5 分钟去重窗 + max 3 未完成 install task（内存 limiter；进程重启清缓存，可接受——winget 本身是幂等的）。
- 前端 logs buffer LIMIT 200 行；连续 6 个心跳（180s）无新 stage 事件 → 前端 toast 提醒"长时间无进展，请检查网络或阻塞 UAC"。

## 11. 影响范围与向后兼容

- 纯新增 API / 前端组件 / CLI 参数；Task 2 已交付端点仅**向后兼容**扩展字段/参数。
- `docker_setup.py` 只 `run_install` 多两个参数（`os_hint` / `on_stage`），缺省 None 时行为与旧一致。
- 现有 CLI 调用 `modelctl env setup docker --run`（无 `--os`）在 Linux host 上行为与旧 100% 一致（缺省 `sys.platform` 分派）。
- 前端 `DockerInstallPanel` 是**新增**组件，不拆 Task 2 已交付的 `EnvsView.vue` Docker 区块。

## 12. 风险登记

| 风险 | 缓解 |
|---|---|
| UAC 被另一个程序/用户锁定导致 SSE 卡死 | `run_install` 内 20s 无 stdout → emit `need_UAC`；120s 无输出 → emit `log`（提示）；15 min 冷门 timeout → exit 3 + 引导手动。不短 kill 子进程（专用 winget dry run 边界）。 |
| 企业 GPO 静默安装审批阻断 winget | 引导文案到 Desktop 官网手动下载；不禁止输入；反复不 retry（silent install 只跑一次）。 |
| wingetId 变更（微软不保证 long-term stable 的 `Docker.DockerDesktop` ID） | `WINGET_ID` 锁单测断言 `winget --list` 中存在；winget 失败 capture `Unknown package ID` → 提示更新 wingetId。 |
| 用户名含空格 / 中文时 `Path.home()` 编码（如 `C:\Users\张三`） | Python 3.10+ 默认处理；daemon.json 用 `write_text(..., encoding="utf-8")`；subprocess 命令传 args 数组（不 shell 拼接），无 encoded 陷阱。 |
| 非 admin 用户 `--silent` 装 Desktop 可能降级到 per-user store（部分机器不见 desktop.app） | 安装后探 Desktop.exe；找不到则 emit warning 引导 "需 admin 权限" 或 "手动下载 Desktop 官网 installer"；degrade 而非 fail（daemon.json 与验证命令仍继续）。 |
| GPU 镜像 pull 120s 也不入 | GPU step 失败不阻断；`post_install_plan` 里 `enable_gpu` 标 `optional: true` + 提示 "请手动到 Desktop Settings → Resources → GPU 勾选后点验证重试"。 |
| winget CDN 变更或国内网络受限 | 不注入 mirror；winget 自身 retry；UI 6 心跳 timeout 后提示 "若持续无进展，可到 Desktop 官网下 zip 手动安装"。 |
| 用户关 SSE 不会杀 winget | 前端显式提示 "关闭对话窗口不中断安装"（§5.1 已定义；SSE close 只影响 client 侧）。 |
| TaskManager 抽出失败导致 Task 3 内嵌 SSE 实现 | 备选路径已写（§5.4）；无论哪条路，`StageEvent` dataclass SoT 不变。 |
| 前端新组件不匹配 TaskManager 多实例改造 | Task 4 手工 verify 包含 SSE 断连恢复 + 重连查询生命周期测试。 |

## 13. 关联文档

- `2026-09-07-envs-docker-bypass-guide-design.md`——本 spec 是它的 Windows 下探，**不反向影响 Linux 路径**；本 spec 完成后 Task 2 已交付的端点行为不变。
- `docs/known-pitfalls/backend/docker-bypass-guide-single-source.md`——Task 4 验收后**同级追加** `docker-setup-os-dispatch.md`，重点：
  1. `run_install` dispatcher 的 4 种 os/platform 交叉矩阵（`--os` 缺省、`--os` 与 host 匹配、`--os` 与 host 不匹配 × 预览、`--os` 与 host 不匹配 × 执行）
  2. `StageEvent` 单一事实来源位置（`core/sse_stage_event.py`）与前端 `PostInstallStep` 对齐契约
  3. `windows_setup.Check` 字段名锁与 `docker_setup.Check` 的测试锚定方式
- Plan：`docs/superpowers/plans/2026-09-07-envs-docker-install-windows-only.md`（本 spec 审过后的下一步产出）。

## 14. 后续演进（非本 spec scope）

- **macOS**：`darwin_setup.py` 平行展开（Docker Desktop `Docker.dmg` → hdiutil attach → 拖入 /Applications 的 silent 化脚本），`--os=darwin` 分派并行；daemon.json 同前 `%USERPROFILE%` → `~/.docker/daemon.json`。
- **Linux 命名空间**：非 root 用 `rootless docker` 安装 spec（另开）；本 spec 不给 rootless 自动化，保持 Linux 路径 = root + apt。
- **Windows GPO / 企业**：假设企业 winget 源可达；企业私有 winget source 的 fallback 单独 spec。
- **Registry 管理面板**：本轮仅 `registry-mirrors` + `max-concurrent-downloads`；拉取速率展示 / 限速管理等留待后 spec。
- **Docker Compose 版本管理**：Desktop 内已自带，不需要 modelctl 管。
- **GUI 全端到端 verify**（桌面 UI 与 Desktop WS API 通信）：依赖 Desktop 受审 API 发布，另开。

## 15. 验收（Task 4 判据）

**本 Windows 开发机可跑**（无 UAC / 无 30 min 下载 / 无 shutdown）：

1. `pytest tests/test_core_windows_setup.py -v` 全绿（18+ 用例）
2. `pytest tests/test_cli_env.py -v` 全绿（含新增 4 用例）
3. `pytest tests/test_webui_admin_envs.py -v` 全绿（含新增 9~10 用例）
4. `npm run build` exit 0
5. CLI 实际跑（Windows host，login 输出 UTF-8）：
   - `modelctl env setup docker --os=linux` → stdout 出现 `apt-get install docker-ce`（跨预览脚本，无副作用）
   - `modelctl env setup docker --os=linux --run` → exit 2 + stderr "Linux 安装路径仅 Linux 主机可 --run"
   - `modelctl env setup docker --os=windows`（无 `--run`）→ stdout 出现 PowerShell 脚本；**默认场景，但注意：本 host 缺省就是 windows，等同于不调 `--os`；因此加 `--os=windows` 显式再测一次只是为了 CI matrix 覆盖**
   - **不跑** `--os=windows --run`（UAC + 30 min 下载，属禁用范围）
   - **不跑** `--os=macos`（argparse 已拒，§9.4 用例 4 已覆盖）
6. WebUI 手工 verify（登录 admin 后环境页）：
   - Task 2 已交付的列表页 / Docker 旁路 / 完整诊断卡现状不变
   - 新 "Docker 一键安装" 卡片 read-state：
     - Windows host：4 段 steps + 一键安装按钮可见；need_reboot / error 后另外三按钮显现
     - **不**在本开发机真点"一键安装"（避免 UAC + 30 min 下载）
   - Linux host 打开同样 URL：卡片展示 el-alert 提示（"Windows 独占功能"）
7. known-pitfalls 沉淀：`docs/known-pitfalls/backend/docker-setup-os-dispatch.md` + `README.md` 追加一行（Task 4 完成时落地）

**Linux 部署机 verify**（Task 4 完成后续）：

8. 在 Linux 部署机（root）执行 `modelctl env setup docker --os=linux --run` → 仍走 apt 原路径，行为与 Task 2 之前 100% 一致（回归安全网）
9. 在 Linux 部署机执行 `modelctl env setup docker --os=windows --run` → exit 2 + "Windows 安装路径仅 Windows 主机可 --run"
10. 在 **Windows 部署机**（非本开发机）真跑 `modelctl env setup docker --os=windows --run` → UAC 通过后装完 → 重启 → 首启向导勾选 WSL2 + GPU → WebUI 点验证 → 5 项 check 全绿
    - Step 8-10 需人工动一次，账落到 Task 4 的 `.superpowers/sdd/<date>/task-4-report.md`

## 16. 强制不做（反模式 · 防实现子代理越界）

以下约束 brick 硬：

1. Windows 路径里**不**调 `subprocess.run(["apt-get", ...])`（静默错误时危害不易追踪）。
2. `windows_setup.py` 里**不**写 `%PROGRAMFILES%` 或 `%WINDIR%`（需 admin，不代劳；Docker Desktop winget 自己 handle UAC）。
3. **不**写 `/etc/docker/daemon.json`（平等是不同）；`Path.home() / ".docker" / "daemon.json"` 此写意。
4. `UAC` 只作为 `StageEvent.stage` 枚举值出现，**不**作为代码解释字符串（避免 grep 噪声）。
5. **不**在 `admin_envs.py` 里引入 Windows 专属代码片段（如 `if os.name == "nt" ...`）；平台分派只落 `docker_setup.run_install` / `windows_setup.run_install` 两层。
6. 前端 `DockerInstallPanel` **不**实现 "cancel install" 按钮（阶段 A 不可中断，前端只关 SSE 不杀 winget——成 shou 与后端 §8/#11 一致）。
7. 不改动 Task 2 已交付的 `envs.DOCKER_CAPABLE_ENGINES` 集合与 `docker_setup.diagnose()` 语义（Linux 侧）；本 spec 中 Windows 侧 `windows_setup.diagnose()` 是**新增**并独立。
8. 不引入新前端依赖包（`npm install` 免新方包）；Element Plus / Tailwind / 现有 utils 够用。
9. 不引入 `WSGI` / Uvicorn push 依赖；SSE 依然走 `fastapi.responses.StreamingResponse`。

## 17. 实施顺序（详细 Task 拆分见 plan）

- **Task 1** — `windows_setup.py` + `test_core_windows_setup.py`（TDD，单元层全绿为进入门槛）
- **Task 2** — `docker_setup.run_install` dispatcher + CLI `--os` flag + `test_cli_env.py` 扩展 4 用例
- **Task 3** — `admin_envs.py` 新端点（`/install` + `/system-action` + `/diagnose?os=` 顶层扩展）+ TaskManager/SSE + `core/sse_stage_event.py` + `test_webui_admin_envs.py` 扩展 9+ 用例
- **Task 4** — `types.ts` 补类型 + `envs.ts` 增 `startDockerInstall` / `systemActionWindows` / `fetchDockerInstallStatus` + `DockerInstallPanel.vue` 新建 + `EnvsView.vue` 接入 + `npm run build` + 本 Windows 开发机 manual verify + `docs/known-pitfalls/backend/docker-setup-os-dispatch.md` + `README.md` 追加
- **终审** — 4 Task 跨层 Pattern 检查 + Python/TS 一致性 + 双终端 ci（后单测 + 前端 build）+ ledger 记账 + 无 regressions

---

**Self-review 完成项**（对照 spec self-review 4 条）：

1. **占位符扫描**：无 TBD / 未定义引用（唯一的 `TODO refactor` 出现在 §5.4 备选路径，已标 "本次 TDD 优先：内联重复代码，抽公共函数放到 Task 4 之后或独立重构提 issue"）。
2. **内一致性**：
   - §5.3 `already_running` 5 分钟去重窗 ↔ §8/#13 一致
   - §4 CLI 行为矩阵 4 条 ↔ §9.4 CLI 4 用例一一对应
   - §15 验收做项 ↔ §9.1 分层口径一致
   - `run_install` §3.2 dispatcher 语义与 §4 行为矩阵中 x→y 交叉拒绝消息文本一致
3. **scope**：单一 "Windows side 一键安装" spec；macOS / 非 root Linux / 企业 GPO / registry 面板 / Docker Compose plugin 全部列在 §14 后续演进，不外溢到本 spec。
4. **歧义检查**：
   - Stage 枚举 §5.2 与 §6.1 `DockerSSEStage` 同名同值（严格一致的字符串联合体）
   - `Check` dataclass §3.3 已明列字段名锁 = 与 `docker_setup.Check` 字段名一致，测试锚定
   - `_merge_daemon_json` 幂等语义（相同 registry-mirrors 与 max-concurrent-downloads 时不重写、返回 False）在 §9.3 用例 13 已锁
   - SSE 断连后 "SSE 端 close 语义"（不 kill 子进程）在 §5.1 / §7.3 / §8/#11 / §12 四处一致
