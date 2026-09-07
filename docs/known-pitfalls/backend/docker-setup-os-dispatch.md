# Docker 安装 OS dispatcher：4 交叉矩阵 + SSE SoT + Check 字段锁 + 路由顺序 + platform monkeypatch

> 本文件是 `modelctl env setup docker --os=<linux|windows>` + WebUI 一键安装链路的后端沉淀；原始单文件已并入本文件归档（本主题为首次沉淀，无历史归档）。

## OS dispatcher 4 种 `--os` × host × 动作交叉矩阵

- **日期**：2026-09-07　**分类**：后端 / 环境管理

### 根因

跨平台 `--run` 在历史上没有"显式判定边界"：`docker_setup.run_install` 旧版只看 `sys.platform`，若未来加 `--os` 开关需要**两层判定**叠加——外层（`cli._cmd_env_setup_docker` / dispatcher 自身）做"target_os vs 本机一致性"、内层（`run_install`）做"最终动作仅本机可执行"。两层职责悬殊：前者管"能不能进入执行分支 + 精确文案"，后者管"最终误入时的兜底 error"。

### 4 种交叉矩阵（硬性行为契约）

| host | `--os` | 无 `--run` | 加 `--run` |
|---|---|---|---|
| win32 | linux | exit 0：打印跨平台预览 + apt 脚本 + "在目标 linux 主机上执行" | **exit 2** + stderr `Linux 安装路径仅 Linux 主机可 --run，当前平台 'win32'` |
| win32 | windows | 本平台 diagnose + 4 项 OK 或指引 + "自动执行：… --os=windows --run" | 执行 windows_setup，**绝不**在此处做 shell |
| linux | linux | 本平台 diagnose + 4 项 OK 或指引 | 执行 apt 流水线（要求 root，否则 exit 2） |
| linux | windows | 跨平台预览：Windows 指引 | **exit 2** + stderr `Windows 安装路径仅 Windows 主机可 --run，当前平台 'linux'` |

**dispatch 顺序（cli._cmd_env_setup_docker 严格次序，缺一会有隐性行为漂移）：**
1. `target_os != host_os and not args.run` → 只走 `render_instructions` 预览（**不**跑 diagnose，因为本机无法可靠跨平台探测 wsl / winget / cgroup）
2. `target_os != host_os and args.run` → **stderr + return 2**（不能走 run_install 让上层代收，文案会落到 loguru 混进日志混淆 CLI 输出）
3. `target_os == host_os and not args.run` → diagnose + (全 OK ? 提示就绪 : 打印指引)
4. `target_os == host_os and args.run` → windows 直接 `windows_setup.run_install(...)`；linux 走 `docker_setup.run_install(..., os_hint=target_os)` 多一层 hint 复核

### 解决方案

- **预览模式严禁落 diagnose**：跨平台宿主（Linux 主机 + `--os=windows`）如果跑 `windows_setup.diagnose()` 会真发 `wsl --version` 子进程，在配置无障碍的 Linux 上要么 5s 超时要么静默 false，把"可部署的目标"风格地打成"不可部署"；预览只 `render_instructions` 就完。
- **跨平台 `--run` 的 refusal 写在 CLI 层而非核心层**：核心层 `run_install` 的 `logger.error` 会被 loguru 打进日志文件，把人工 piped 到 stdout 的"应当向 stderr 展示的报错"混进日志流；CLI 层 `sys.stderr.write` 明确走 `ToStdErr`，避免退出码 2 配套的 stderr 管路混乱。
- **`--os` 未给 = host_os** 这一默认在 CLI 层实现（`getattr(args, "os", None) or host_os`），`run_install` 仍支持 `os_hint=None` 走同一套 fallback，两层默认值同源避免"CLI 与 WebUI 不同源"。
- 判定 `sys.platform` 用 `==`/`startswith` 精确表达式（不模糊判 `in`），任何增加分支（如 macOS / msys2）走"未识别 platform"路径 return 2 而非默认到 linux。

### 代码示例

```python
# src/modelctl/cli.py:1060 _cmd_env_setup_docker（必须保持顺序）
host_os = "windows" if sys.platform == "win32" else "linux"
target_os = getattr(args, "os", None) or host_os
cross_platform = target_os != host_os

if cross_platform and not args.run:          # 1) 预览：只 render_instructions
    print(_table_paint(f"跨平台预览（本机={host_os}，目标={target_os}；仅打印安装指引，不执行探测）：", "WARNING"))
    print(render(mirrors, limit))
    print(_table_paint(f"在目标 {target_os} 主机上执行：modelctl env setup docker --os={target_os} --run", "DIM"))
    return 0

if cross_platform and args.run:              # 2) 跨平台执行：硬拒
    msg = ("Windows 安装路径仅 Windows 主机可 --run，当前平台 " if target_os == "windows"
           else "Linux 安装路径仅 Linux 主机可 --run，当前平台 ") + repr(sys.platform)
    sys.stderr.write(msg + "\n")
    return 2

# 3) 同平台非执行 / 4) 同平台执行 依次走 diagnose / run_install
```

```python
# src/modelctl/core/docker_setup.py:431 run_install（内层复核）
hint = os_hint or ("windows" if sys.platform == "win32" else "linux")
if hint == "windows":
    if sys.platform != "win32":
        logger.error(f"Windows 安装路径仅 Windows 主机可 --run，当前平台 {sys.platform!r}")
        return 2
    from modelctl.core import windows_setup
    return windows_setup.run_install(registry_mirrors, max_downloads, on_stage)
if hint == "linux":
    if not sys.platform.startswith("linux"):
        logger.error(f"Linux 安装路径仅 Linux 主机可 --run，当前平台 {sys.platform!r}")
        return 2
    return _install_linux(registry_mirrors, max_downloads, on_stage)
logger.error(f"未知 os_hint: {hint!r}")
return 2
```

---

## SSE `StageEvent` SoT：`core/sse_stage_event.py` 是单一事实来源

- **日期**：2026-09-07　**分类**：后端 / SSE

### 根因

`windows_setup.run_install` 内部通过 `on_stage(stage, ...)` 回调 emit `12 值 stage 枚举`；`admin_envs.py` 的 SSE 端点再把 emit 帧序列化成 HTTP body；前端 TS `DockerSSEStage` 是**同一枚举**的第三端消费方。若三端各自硬编码字符串字面量，任一字符修改（`need_UAC` → `need_uac`）都会隐式分级失配：emit 端字面量改了、消费端没跟 → 前端收不到 `need_UAC` 帧，UAC 弹窗不出现，用户卡在"winget 好像在等"的假象，且**没有任何日志报错**（EventSource 回调静默无命中是合法行为）。

### 解决方案

- **单一事实来源格式**：`StageEvent` dataclass 放在 `core/sse_stage_event.py`（frozen=True 防中途篡改、`to_sse_dict()` 剥 None），`windows_setup.STAGES: frozenset[str]` 与前端 `PostInstallStep.action` 枚举（`"restart" | "open_desktop" | "verify" | null`）三端必须同形同步。
- **StageEvent 字段契约**（`frozen` 保证不可变）：
  - `type: "stage" | "log" | "error" | "complete"`（4 值，**不是** 12 值；12 值是 `stage` 字段的枚举）
  - `stage: <12 值 STAGES 之一>`
  - `message: str`（type=log 时是原始 stdout 一行）
  - `ts: "YYYY-MM-DD HH:mm:ss"`（项目统一格式）
  - `code: int | None`（仅 error / complete 时给）
  - `payload: dict | None`（`post_install_plan` 给 `{"steps": [...]}`；error 给 `{"tail": [...]}`）
- **`None` 字段剥除**（`to_sse_dict` 行为）：`code=None` / `payload=None` 不出现在 SSE body，避免前端 `catch (e) if (e.code)` 之类的 JS null 判断分支无谓执行。
- **三端漂移防线**：单元测试 `test_core_windows_setup.py` 里 stage 名集合必须与 `STAGES` 硬编码比对；前端 TS `DockerSSEStage` 变更时触发 `npm run build` 类型检查失败（vue-tsc 严格）拦截。
- **`heartbeat` 是**"SSE 层"保活帧**，不是** `StageEvent`**；EventSource 必须 `addEventListener('heartbeat', () => {})` 注册空监听（不注册虽不报错但排查时无法区分"没来"和"没接"，见 [sse-named-events.md](sse-named-events.md) 已知陷阱）。

### 代码示例

```python
# src/modelctl/core/sse_stage_event.py（frozen 防篡改）
@dataclass(frozen=True)
class StageEvent:
    type: str                    # "stage" | "log" | "error" | "complete"
    stage: str                   # 12 值，见 windows_setup.STAGES
    message: str
    ts: str                      # YYYY-MM-DD HH:mm:ss
    code: int | None = None
    payload: dict | None = None

    def to_sse_dict(self) -> dict:
        out = {"type": self.type, "stage": self.stage, "message": self.message, "ts": self.ts}
        if self.code is not None: out["code"] = self.code
        if self.payload is not None: out["payload"] = self.payload
        return out
```

```python
# src/modelctl/core/windows_setup.py（STAGES 12 值 SoT）
STAGES: frozenset[str] = frozenset({
    "detect_winget", "detect_wsl2", "winget_running", "need_UAC",
    "winget_done", "write_daemon_json", "test_docker_version",
    "test_gpus", "post_install_plan", "already_installed",
    "done", "error",
})
```

---

## `windows_setup.Check` 字段名锁：必须真子集于 `docker_setup.Check`

- **日期**：2026-09-07　**分类**：后端 / 环境管理

### 根因

前端"完整诊断"卡片按字段名渲染表格（`c.ok` / `c.label` / `c.detail` 三点硬编码），`windows_setup.diagnose()` 返回 5 项 `Check`，比 `docker_setup.Check` 多一个 `hint`（Windows 专用提示，如 "non-GPU host, skipped"）。字段集合必须是 **`docker_setup.Check.字段 ⊆ windows_setup.Check.字段`**（真子集），否则前端模板按缺失列渲染/超列渲染 → 表头与数据脱节。

### 解决方案

- **字段名集合锁定**：单元测试 `tests/test_core_windows_setup.py::test_check_dataclass_fields_match_docker_setup` 用 `dataclasses.fields()` 比对两侧字段名集合，断言 `ds_fields ⊆ ws_fields`；未来若有人在 `windows_setup.Check` 改名（`hint` → `tip`）会让测试红一次拦下。
- **梯队化命名约定**：`docker_setup.Check` 是"最小通用集"（`{key, label, ok, detail}`），平台专属提示靠"加字段"而非"换字段"扩展；禁止在 `windows_setup.Check` 里删字段（就算 Windows 端暂不需要，也要与 Linux 同形保持模板镜像一致）。
- **前端消费点**：`web/src/views/EnvsView.vue` 的 `dockerDiagnose` 展开区块只按通用集渲染 `label/ok/detail`，`hint` 由客户端可选消费（后端已格式化，前端不二次位置校验）；两端的字段语义必须同源，注释备注见 `windows_setup.py:108` 的 docstring。

### 代码示例

```python
# ✅ tests/test_core_windows_setup.py
def test_check_dataclass_fields_match_docker_setup():
    """windows_setup.Check 字段必须真子集于 docker_setup.Check，防止模板漂移。"""
    ds_fields = {f.name for f in dataclasses.fields(ds.Check)}
    ws_fields = {f.name for f in dataclasses.fields(ws.Check)}
    assert ds_fields <= ws_fields, (ds_fields, ws_fields)
```

```python
# ✅ src/modelctl/core/windows_setup.py
@dataclass(frozen=True)
class Check:
    """单项诊断结果（5 字段，比 docker_setup.Check 多 hint 作 Windows 额外提示）。"""
    key: str
    label: str
    ok: bool
    detail: str
    hint: str = ""   # Windows 扩展；保持默认空而非 None，前端判 `if (c.hint)` 稳定
```

```python
# ❌ 反例：删字段 / 改字段名都会让前端模板渲染缺列
@dataclass(frozen=True)
class Check:
    key: str
    label: str
    ok: bool       # 缺 detail，cli 打印行 "无响应 （xxx）" 为空行
```

---

## 精确路径路由注册顺序：`/docker/install*` 必须在 `/{target}/setup` 之前

- **日期**：2026-09-07　**分类**：后端 / WebUI

### 根因

FastAPI 路由按 `include_router` 注册的**顺序** first-match 命中。`admin_envs.py` 里既有 `POST /{target}/setup`（负责托管 venv 的 setup 异步任务）+ 新增 5 条 docker 精确路由。docker 的 5 条路由**路径首段是字面量 "docker"**，而 `/{target}/setup` 首段是**路径参数 `{target}`** —— 若 5 条精确路由**注册晚于** `/{target}/setup`，`POST /docker/install` 的 `docker` 会被 `{target}` **吞掉**（`target="docker"` + `path="install"`），然后因为 `{target}` 在 `envs.known_targets()` 里找不到 `docker` 而是返回 404 或落入"未知 target"分支，根本走不到 docker 安装端点。

### 解决方案

- **文件顶部单例块就是顺序声明**：`admin_envs.py` 在 `router = APIRouter()` 之后、其它 `@router.get/list_envs` 之前声明 `docker_install_task_manager = TaskManager()` 与相关单例变量（文件行 40-47 的注释块 "这 5 个精确路由必须在下方 `POST /{target}/setup` 之前注册"），**让 docker 5 条路由的 `@router.*` 装饰器紧邻单例块**，物理位置前于 `/{target}` 通配路由。
- **新增精确路由时**：若后续再加 `POST /docker/xxx` 精确路径，必须**同样紧跟单例块**（不要插到任何通配路由之后）；若未来出现"非 docker 的精确路径 + 通配并存"同情况，务必通配者放最后（FastAPI 无 `@scope` 优先级语法）。
- **测试锚点**：`tests/test_webui_admin_envs.py` 必须有 `POST /docker/install` 与 `POST /{target}/setup` 的用例各显式指定 task_id / target，各自断言命中不同 handler（返回 shape 不同：install 返 202 + task_id，setup 返 202 + task_id + 不同的 task 类型），防止未来路由顺序回归时静默吞掉 docker。
- **禁止反向**：千万不要"先把通配放前面以统一代码结构"再"用路径中特殊字符区分 docker"——FastAPI 的 first-match 不看 type 注解，只看路径段数与字符结构，`"{target}"` 与 `"docker"` 同宽，靠路径段数无法区分，只能靠注册顺序。

### 代码示例

```python
# ✅ admin_envs.py 现有顺序（单例 + docker 5 条精确路由 在 /{target}/setup 之前）
router = APIRouter()
# —— Docker 一键安装（Windows-only）单例
# 端点注册顺序：这 5 个精确路由必须在此后、`POST /{target}/setup` 之前注册
docker_install_task_manager = TaskManager()
_user_active_installs: dict[str, dict] = {}

@router.post("/docker/install")         # 1) 精确首段 "docker" —— 必须早于 /{target}/setup
@router.get("/docker/install/{task_id}/events")   # 2)
@router.get("/docker/install/{task_id}")          # 3)
@router.post("/docker/system-action")             # 4)
@router.get("/docker/diagnose")                   # 5)

@router.get("")                            # 既有 list_envs（保留 disjoint 首段）
@router.post("/{target}/setup")            # 通配：必须**最后**注册
@router.delete("/{target}")
```

```python
# ❌ 反例：通配在前，docker/install 被 {target} 吞
@router.post("/{target}/setup")           # 变宽 first-match
@router.post("/docker/install")           # 永远命不中
```

---

## `import sys` vs `from sys import platform`：monkeypatch 兼容性的决定性差异

- **日期**：2026-09-07　**分类**：后端 / 单元测试

### 根因

测试中使用 `monkeypatch.setattr(sys, "platform", "win32")` 切换平台是正确的（改 `sys` 模块对象的 `platform` 属性），但它只影响**通过 `sys.platform` 属性访问**的读取点。若模块顶层写了 `from sys import platform` 做**名字绑定**，该模块局部的 `platform` 变成一个**独立字符串常量**，后续 `sys.platform` 改动**不会**反映到 `from-sys-import` 模块内部的 `platform` 变量上 → 单测假绿（在真实 win32 主机上跑了 win32 分支，根本没走到 linux 分支）。

### 解决方案

- **本仓统一 `import sys`**：`windows_setup.py` / `docker_setup.py` / `cli.py` 都是 `import sys` + `sys.platform == "win32"` 的读取方式（`.py` 内 grep 结果显示**无一处** `from sys import platform` 顶层导入），monkeypatch 改 `sys.platform` 就能同步反映到所有读取点（Python module 是单例对象，属性 read 是动态查找）。
- **测试写法**：
  - `monkeypatch.setattr(sys, "platform", "win32")` —— 直接改"模块对象的属性"（最快）
  - `monkeypatch.setattr(ws.sys, "platform", "win32")` —— 等价（`ws.sys` 是同一 module 对象），但显式绑定到被测模块；写法更保守（WSL 场景有时 `sys` 来自别的 import，写全路径不会错）
- **反例**：`from sys import platform` 顶层 + `monkeypatch.setattr(sys, "platform", "win32")` → `ws.platform` 仍为宿主机实际值，单测假绿（本仓 zero hit，历史落缓但未来引入 `from sys import platform` 时必须连读点一起改造为 `checker = sys.platform.startswith` 闭包，或把 `from` 改成 `import`）
- **同型判定**：`sys.platform.startswith("win")` vs `== "win32"` —— `win` 前缀同时命中 `win32` / `win64`（Python 3 永远 `win32` 无视 64 位；`msys2` / `cygwin` 虽含 `win` 子串但 `sys.platform` 值是 `"msys"`，`startswith("win")` 也不命中）；用 `==`/`startswith` 精确表达式，任何未来新增分支（如 macOS 桌面）走"未识别"分支 return 2 而不是默认到 linux。

### 代码示例

```python
# ✅ windows_setup.py / cli.py / docker_setup.py 层面：import sys（模块对象保持单一）
import sys
host_os = "windows" if sys.platform == "win32" else "linux"

# ✅ tests/test_core_windows_setup.py（两种 monkeypatch 写法都 OK）
def _win(monkeypatch):
    monkeypatch.setattr(ws.sys, "platform", "win32")   # ws.sys 是 sys 模块本身
# 或更简洁：
# monkeypatch.setattr(sys, "platform", "win32")        # sys 是全局 sys

# ❌ 反例：`from sys import platform` 顶层绑定剥离了模块属性引用
# from sys import platform
# host_os = "windows" if platform == "win32" else "linux"
# → 此处 platform 已是"字符串值快照"，monkeypatch.setattr(sys, "platform", "win32") 改不动它
```
