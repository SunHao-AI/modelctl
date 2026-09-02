# 服务状态判定改造实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现方案 A——以端口健康（/health 2xx 单飞探测）为主信号、PID 文件为辅助的 `is_running_any` 统一运行态判定；docker runtime 路径不再写 PID 文件、stop 走容器 `docker rm -f`；CLI 状态枚举收敛为 6 状态。

**Architecture:** 三个红心改动：① `core/process.py` 新增 `is_running_any(name, profile)` / `stop_docker_instance(name, container_name)` / `start_detached(..., write_pid=True)`；② `engines/base.py` 新增 `stop_backend()` 钩子 + `is_docker_runtime() -> False` 默认，vllm/tokenspeed/tensorrt_llm 三个 docker 引擎覆盖；③ 三个消费点（gateway / all_service / cli）整体改为 `is_running_any` 替代 `is_running`，docker 路径 stop 走 `adapter.stop_backend()`。配套：状态枚举 7→6、yaml 头注释、tests 同步。

**Tech Stack:** Python 3.12（项目 `modelctl.env` 指定），stdlib `urllib.request`，`subprocess`、Pytest。

**全局约束（Global Constraints，每个任务都必须遵守）：**
- 不增删任何 Python 第三方依赖；`pyproject.toml` 没有相对版本变化。
- 所有新增/改动函数的 docstring 用中文，注释遵循现有 "文件头 @IDE/Author/Email + `# -*- coding: utf-8 -*-`" 模板（见 `src/modelctl/core/process.py` 现有样式）。
- Windows 与 Linux/Darwin 双平台兼容（不引入 POSIX-only API；POSIX-only 调用须有 `sys.platform != "win32"` 守卫）。
- TDD：每个任务 Step 1 必须先写失败测试且跑一次确认失败；Step 3 实现最小代码使 Step 1 测试通过；中间跑 `pytest tests/<file>.py::test_x -v` 验证 PASS。
- 通信语言：中文回复；PowerShell 使用 `;` 分隔命令；项目根 `d:\WorkPlace\Pycharm\modelctl`。
- 无新增文档 / README / 配置项；只修改本 spec 列出的文件。
- 状态枚举 6 状态固定为：`{"运行中", "已停止", "正常", "无响应", "PID 残留", "未就绪"}`（`models\tests` 内一致使用，不得引入新状态）。
- `is_running_any` 签名固定为 `is_running_any(name: str, profile: Profile | None) -> bool`；`stop_backend()` 固定为 `def stop_backend(self) -> None`；`is_docker_runtime()` 固定为 `def is_docker_runtime(self) -> bool`；`start_detached` 参数序 `(name, command, extra_env, write_pid=True)`。
- **`is_running_any` 必须无副作用**：只读探测，绝不 `unlink` PID 文件、绝不释放 GPU 锁。PID 文件的清理职责专属 stop 路径（`stop_instance` / `stop_docker_instance`）。理由：CLI 的 "PID 残留" 状态需在判定返回 False 后回看 `pid_file(name).is_file()` 才能识别。
- 所有 import 使用项目全限定路径：`from modelctl.core.process import ...`、`from modelctl.engines.base import EngineAdapter` 等；不得通过 `modelctl.engines` 包绕过。
- `modelctl.env` 不要修改；自带 uv/pytest 须经项目 `uv run pytest` 调用。
- commit 口令：中文、首行 ≤ 60 字符，前缀 `feat:` / `refactor:` / `test:`（与 `git log` 现有风格对齐，参考 `6452ca4`/`3351efb`/`1717e01`）。
- **禁止 fallback 或 try/except 包裹核心语义**：任何新增异常语义只能在 spec 明确列举的位置（`is_running_any` 全 None-safe、`stop_docker_instance` rm -f 失败仅 warning），其它地方应"报错即抛"。
- 每个任务的"修改文件"清单是强制要求；任务内不得引入额外文件，不得遗漏列出的文件。

---

## 文件分工（File Structure）

| 文件 | 责任 |
|---|---|
| `src/modelctl/core/process.py` | 纯进程/PID/健康检查原语。新增 `is_running_any`、`stop_docker_instance`、`start_detached` 加参 `write_pid`；保留 `is_pid_alive`/`is_running`/`stop_instance`/`docker_container_alive`/`open_local`/`wait_health` 不动。 |
| `src/modelctl/engines/base.py` | 引擎适配器基类。新增 `stop_backend()`（默认调 `stop_instance`）与 `is_docker_runtime()`（默认 False）；保留 `wait_ready`/`backend_dead`/`stop_patterns` 不动。 |
| `src/modelctl/engines/vllm.py` | 覆盖 `stop_backend` + `is_docker_runtime`（docker 分支 → `stop_docker_instance`）。保留 `_container_name` property（已存在）。 |
| `src/modelctl/engines/tokenspeed.py` | 同上（`_container_name` 是 method，签名不同，覆盖时正确调用）。 |
| `src/modelctl/engines/tensorrt_llm.py` | 同上。 |
| `src/modelctl/core/all_service.py` | `start_profile` 注入 `write_pid`；`stop_profile` else 分支改用 `adapter.stop_backend()`；`stop_all` 与 `restart_profile` 的 `is_running` → `is_running_any`。 |
| `src/modelctl/core/gateway.py` | `is_model_available` 收敛为一行 `is_running_any`。 |
| `src/modelctl/cli.py` | `_instance_state` 改用 `is_running_any`；`state_words` 收敛为 6 状态；`_cmd_status`/`_group_runtime_target`/`_cmd_list` 的内联状态判断收敛。`_port_health_ok` 保留（其它地方仍用）。 |
| `src/modelctl/core/colors.py` | `status_color` 映射改写：删"已外部启动"、"PID 异常"，加"PID 残留"。`STATUS_EXTERNAL` 的 Color 定义保留（向后兼容外部用户 env 覆写 `STATUS_EXTERNAL=` 用法，不会回归报错）。 |
| `models/vllm/qwen3.8-flash-next.yaml` | 头注释 L8-17 中关于 PID 文件的措辞更新（环境不再写 PID 文件 → 改叫 `stop_docker_instance`）。 |
| `tests/test_process.py` | 新增 `is_running_any` 与 `stop_docker_instance` 用例；验证 `start_detached(write_pid=False)` 行为。 |
| `tests/test_all_service.py` | `stop_profile` 改用 `adapter.stop_backend()` 的 mock；`stop_all` 用 `is_running_any` 的 mock。 |
| `tests/test_gateway.py` | `is_model_available` 改为 mock `is_running_any`；删 `test_list_models_excludes_stale_pid_but_alive`（语义已被 PID 残留 路径吞并）。 |
| `tests/test_modelctl.py` | 把涉及 `_instance_state` / `_port_health_ok` 的 stub 一并替换为 `is_running_any` stub。 |

**不改的文件（防止覆盖/混淆）：** `tests/test_stats.py`、`tests/test_gateway_context_switch.py`、`src/modelctl/core/stats.py`、`src/modelctl/engines/ollama.py`、`src/modelctl/engines/llamacpp.py`、`src/modelctl/engines/sglang.py`、`src/modelctl/engines/unsloth.py`、`src/modelctl/engines/lmdeploy.py`、`src/modelctl/engines/aphrodite.py`、`src/modelctl/core/gpu_lock.py`、`src/modelctl/core/capabilities.py`、`src/modelctl/core/profile.py`、`src/modelctl/pyproject.toml`。

---

### Task 1: `core/process.py` 新增核心原语

**Files:**
- Modify: `src/modelctl/core/process.py`
- Test: `tests/test_process.py`

**Interfaces:**
- Consumes: 现有 `is_pid_alive` (L33-49)、`pid_file` (L65-66)、`cache_dir` (L58-62)、`open_local` (L151-157)、`stop_instance` (L107-148) 的签名均不变。
- Produces:
  - `is_running_any(name: str, profile: Profile | None) -> bool` —— 全 None-safe，任何异常（端口不可达 / PID 文件损坏 / profile 无 health 路径）返回 False。
  - `stop_docker_instance(name: str, container_name: str) -> bool` —— 调模块级 `subprocess.run(["docker","rm","-f", container_name], capture_output=True, timeout=10)`；非零退出码或 docker 不可用/超时仅 `logger.warning`，随后无条件清理本地 PID 文件与 gpu_lock，恒返回 True（幂等）。
  - `start_detached(name: str, command: list[str], extra_env: dict[str, str], write_pid: bool = True) -> tuple[int, subprocess.Popen]` —— `write_pid=False` 时跳过 `pid_file(name).write_text(...)`，但依旧返回 `(proc.pid, proc)` 维持签名兼容（pid = Popen.pid 本机号，仅用于日志显示）。

- [ ] **Step 1: 先写 9 个新测试用例（`tests/test_process.py` 末尾追加，不改现有函数）**

```python
import subprocess as sp


def _write_pid(name: str, pid: int, cache_path) -> None:
    (cache_path / f"{name}.pid").write_text(str(pid), encoding="utf-8")


class _FakeProfile:
    """最小化 profile：只暴露 is_running_any 用到的 name/port/engine_config/api_key。"""
    def __init__(self, name, port, api_key=None, ec=None):
        self.name = name
        self.port = port
        self.api_key = api_key
        self.engine_config = ec or {}


def _fake_resp_200(req, timeout):
    """仅返回 status=200 的伪对象，便于 mock open_local。"""
    return _Resp200()


class _Resp200:
    def __init__(self): self.status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _raise_url_error(req, timeout):
    """模拟端口不可达（连接被拒）。"""
    raise urllib.error.URLError("refused")


def test_is_running_any_port_healthy_true(monkeypatch, tmp_path):
    """profile 有且端口 /health 200 → True（不触碰 PID 文件）"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(process, "open_local", _fake_resp_200)
    result = process.is_running_any("p1", _FakeProfile("p1", 8100))
    assert result is True


def test_is_running_any_port_up_pid_dead_preserves_file(monkeypatch, tmp_path):
    """端口 200 但 PID 文件已死 → True，且判定不得有副作用（dead PID 文件保留，留给 stop 清理）"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _write_pid("p2", 999999, tmp_path / "cache")  # 999999 在 /proc 必死
    monkeypatch.setattr(process, "open_local", _fake_resp_200)
    assert process.is_running_any("p2", _FakeProfile("p2", 8101)) is True
    assert (tmp_path / "cache" / "p2.pid").is_file()  # 无副作用：不删


def test_is_running_any_port_down_pid_dead_preserves_file(monkeypatch, tmp_path):
    """端口不通 + PID 已死 → False，dead PID 文件仍保留（CLI 据此报 "PID 残留"）"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _write_pid("p2b", 999999, tmp_path / "cache")
    monkeypatch.setattr(process, "open_local", _raise_url_error)
    assert process.is_running_any("p2b", _FakeProfile("p2b", 8109)) is False
    assert (tmp_path / "cache" / "p2b.pid").is_file()


def test_is_running_any_all_none_false(monkeypatch, tmp_path):
    """profile=None + 无 PID 文件 → False"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    assert process.is_running_any("p3", None) is False


def test_is_running_any_profile_none_with_corrupt_pid_file(monkeypatch, tmp_path):
    """profile=None 且 PID 文件损坏（无法解析为 int）→ False 且文件保留（判定无副作用）"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache" / "p4.pid").write_text("not-a-pid", encoding="utf-8")
    assert process.is_running_any("p4", None) is False
    assert (tmp_path / "cache" / "p4.pid").is_file()  # 无副作用：不删


def test_is_running_any_port_down_pid_alive_true(monkeypatch, tmp_path):
    """venv 情况：/health 还没起来但 venv 进程确实活着 → True（PID 兜底）"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _write_pid("p5", os.getpid(), tmp_path / "cache")  # 自己的 PID 必活
    # 模拟端口不通（抛 URLError）
    def _boring(req, timeout):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr(process, "open_local", _boring)
    assert process.is_running_any("p5", _FakeProfile("p5", 8102)) is True


def test_is_running_any_unknown_name_returns_false(monkeypatch, tmp_path):
    """兜底：profile 存在但端口 / PID 都无 → False（不抛错）"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    assert process.is_running_any("ghost", _FakeProfile("ghost", 1)) is False


def test_start_detached_write_pid_false(monkeypatch, tmp_path):
    """write_pid=False 时不写 PID 文件，但返回 (pid, proc) 维持签名"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    pid, proc = process.start_detached("no-pid-w", [sys.executable, "-c", "pass"], {}, write_pid=False)
    assert (tmp_path / "cache" / "no-pid-w.pid").exists() is False
    assert isinstance(pid, int) and pid > 0
    assert isinstance(proc, sp.Popen)
    proc.wait()


def test_start_detached_write_pid_default_true(monkeypatch, tmp_path):
    """默认 write_pid=True 保持向后兼容：PID 文件照常写入"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    pid, proc = process.start_detached("has-pid-w", [sys.executable, "-c", "pass"], {})
    assert (tmp_path / "cache" / "has-pid-w.pid").is_file() is True
    assert int((tmp_path / "cache" / "has-pid-w.pid").read_text()) == pid
    proc.wait()


def test_stop_docker_instance_runs_rm_and_cleans(monkeypatch, tmp_path):
    """docker rm -f 被记录 + 残留 PID 被清理 + gpu_lock 被释放"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _write_pid("app", 12345, tmp_path / "cache")
    invocations = []

    def _fake_run(cmd, **kw):
        invocations.append((cmd, kw))
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(process.subprocess, "run", _fake_run)
    release_calls = []
    monkeypatch.setattr("modelctl.core.gpu_lock.release_gpu_lock",
                        lambda name: release_calls.append(name) or None)
    ok = process.stop_docker_instance("app", "app-vllm")
    assert ok is True
    assert invocations and invocations[0][0] == ["docker", "rm", "-f", "app-vllm"]
    assert "timeout" in invocations[0][1] and invocations[0][1]["timeout"] == 10
    assert "capture_output" in invocations[0][1] and invocations[0][1]["capture_output"] is True
    assert not (tmp_path / "cache" / "app.pid").is_file()
    assert release_calls == ["app"]


def test_stop_docker_instance_docker_unavailable(monkeypatch, tmp_path):
    """docker 命令缺失（OSError）时仍 True 但清本地 PID + 释放锁"""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _write_pid("x", 1, tmp_path / "cache")

    def _boom(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(process.subprocess, "run", _boom)
    assert process.stop_docker_instance("x", "x-vllm") is True
    assert not (tmp_path / "cache" / "x.pid").is_file()
```

- [ ] **Step 2: 跑测试确认全部失败**

Run: `uv run pytest tests/test_process.py::test_is_running_any_port_healthy_true tests/test_process.py::test_is_running_any_port_up_pid_dead_clean_up tests/test_process.py::test_is_running_any_all_none_false tests/test_process.py::test_is_running_any_profile_none_with_pid_file tests/test_process.py::test_is_running_any_port_down_pid_alive_true tests/test_process.py::test_is_running_any_unknown_name_returns_false tests/test_process.py::test_start_detached_write_pid_false tests/test_process.py::test_start_detached_write_pid_default_true tests/test_process.py::test_stop_docker_instance_runs_rm_and_cleans tests/test_process.py::test_stop_docker_instance_docker_unavailable -v`

Expected: 全部 FAIL with `AttributeError: module 'modelctl.core.process' has no attribute 'is_running_any'`（或 `stop_docker_instance` 不存在；`test_start_detached_write_pid_*` 默认已存在的 `start_detached` 不含 `write_pid` 参数 → `TypeError: unexpected keyword 'write_pid'`）。

- [ ] **Step 3: 在 `core/process.py` 顶部（L14 后面 `from __future__ import annotations` 之后）加 TYPE_CHECKING import，在文件中新增三个函数**

在 `import` 区（L29 `if sys.platform == "win32":` 之前）加：

```python
import typing

if typing.TYPE_CHECKING:
    from modelctl.core.profile import Profile
```

（放最前面以免循环依赖；profile.py 不 import process.py。）

把现有 `def start_detached(name, command, extra_env):` (L78-87) 替换为：

```python
def start_detached(name: str, command: list[str], extra_env: dict[str, str],
                   write_pid: bool = True) -> tuple[int, subprocess.Popen]:
    """后台启动进程，返回 (pid, Popen)。Popen 供调用方在等待健康检查期间探测早退（fail-fast）。

    write_pid=False：docker runtime 专用——`docker run --detach` 客户端在容器创建后
    ~1s 内退出、Popen.pid 写入后即为已死号，后续状态判定会被该 PID 误导，
    故 docker 路径调用方传 False 不写 PID 文件，改用容器名作为身份标识。
    返回签名不变：pid 仍为本机 Popen.pid，仅作日志显示用。"""
    log_path = log_dir() / f"launch-{name}.log"
    env = {**os.environ, **extra_env}
    fp = open(log_path, "w", encoding="utf-8")  # "w"：每次启动覆盖旧日志
    kwargs: dict = {"stdout": fp, "stderr": subprocess.STDOUT, "env": env, "stdin": subprocess.DEVNULL}
    kwargs["start_new_session"] = True  # nohup 语义：SSH 断开不影响
    proc = subprocess.Popen(command, **kwargs)
    if write_pid:
        pid_file(name).write_text(str(proc.pid), encoding="utf-8")
    return proc.pid, proc
```

在 `def is_running(name)` 之后（L114 之后）插入：

```python
def is_running_any(name: str, profile: Profile | None) -> bool:
    """统一运行态判定：端口 /health 2xx 优先，PID 文件机器兜底。

    **判定无副作用**：只读不写——绝不 unlink PID 文件、绝不释放 GPU 锁。dead / 损坏的
    PID 文件原样保留，由 stop 路径（stop_instance / stop_docker_instance）负责清理；
    CLI 的 "PID 残留" 状态依赖判定返回 False 后回看 pid_file(name).is_file() 才能识别。

    profile 缺省（gateway.stats / ui-* 等不持有 Profile 的调用点）退回纯 PID 探测，
    与原 is_running(name) 的判定结果等价。
    profile 存在时先探测 127.0.0.1:{profile.port}/health（单次 2s 超时，不重试），
    2xx 即 True；失败/不可达再回到 PID 文件探测。
    任何异常（端口不通 / PID 文件损坏 / profile 字段缺失）一律 False，绝不抛错。
    """
    # 1. 端口健康探测（仅 profile 非 None 时）——2xx 直接判定存活，不再看 PID 文件
    if profile is not None:
        port: int | None = getattr(profile, "port", None)
        if port is not None:
            headers: dict[str, str] = {}
            try:
                key = (getattr(profile, "api_key", None)
                       or (profile.engine_config or {}).get("api_key"))
            except Exception:  # noqa: BLE001 —— 配置字段缺失不阻塞判定
                key = None
            if key:
                headers["Authorization"] = f"Bearer {key}"
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers=headers)
                with open_local(req, timeout=2.0) as resp:
                    if 200 <= resp.status < 300:
                        return True
            except (urllib.error.URLError, OSError, ValueError):
                pass
    # 2. PID 文件探测（只读；dead / 损坏都返回 False 且保留文件）
    pf = pid_file(name)
    if not pf.is_file():
        return False
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except ValueError:
        return False
    return is_pid_alive(pid)


def stop_docker_instance(name: str, container_name: str) -> bool:
    """docker runtime 路径停止：`docker rm -f <container_name>` + 清本地 PID + 释放 GPU 锁。

    docker rm -f 幂等（容器不存在亦退出码 0；非零退出码仅警告、不阻断）；本地 PID
    文件（venv 路径才有）一并清理以防环境切换残留（docker→venv 反复切换场景）。
    必须走模块级 `subprocess.run`（不要 `import subprocess as _sp` 局部别名），
    否则测试无法通过 `process.subprocess.run` 打桩。
    """
    try:
        result = subprocess.run(["docker", "rm", "-f", container_name],
                                capture_output=True, timeout=10)
        if result.returncode != 0:
            logger.warning(
                f"docker rm -f {container_name} 返回码 {result.returncode}："
                f"{(result.stderr or b'').decode(errors='replace').strip()}")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"docker rm -f {container_name} 执行失败：{exc}")
    pf = pid_file(name)
    if pf.is_file():
        pf.unlink(missing_ok=True)
    try:
        from modelctl.core.gpu_lock import release_gpu_lock
        release_gpu_lock(name)
    except Exception:  # noqa: BLE001
        pass
    return True
```

- [ ] **Step 4: 跑测试确认全部通过**

Run: `uv run pytest tests/test_process.py -v`

Expected: 全部 PASS（新增 10 个 + 既有的 `test_pid_file_path`/`test_start_and_is_running`/`test_is_running_no_pidfile`/`test_stop_instance_*` 均 PASS）。

- [ ] **Step 5: Commit**

```powershell
cd d:\WorkPlace\Pycharm\modelctl; git add src/modelctl/core/process.py tests/test_process.py; git commit -m "feat(process): 新增 is_running_any/stop_docker_instance，start_detached 加 write_pid 参数"
```

---

### Task 2: `engines/base.py` 添加两个默认方法

**Files:**
- Modify: `src/modelctl/engines/base.py`
- Test: `tests/test_engines_base_gpu.py`（追加最小测试）

**Interfaces:**
- Consumes: 现有 `EngineAdapter.__init__(self, profile: Profile, caps: Capabilities) -> None`；现有 `stop_instance(name, port, patterns) -> bool`（来自 process.py）。
- Produces:
  - `EngineAdapter.stop_backend(self) -> None` —— 默认 `stop_instance(self.profile.name, self.profile.port, self.stop_patterns())`。
  - `EngineAdapter.is_docker_runtime(self) -> bool` —— 默认 False。

- [ ] **Step 1: 写最小基类测试（`tests/test_engines_base_gpu.py` 末尾追加）**

```python
import pytest  # 已 import 则忽略


def _make_profile(tmp_path):
    from modelctl.core.profile import Profile
    return Profile(name="buddy", engine="vllm", port=8199)


def test_engine_adapter_stop_backend_default_uses_stop_instance(monkeypatch, tmp_path):
    """基类默认 stop_backend 调 stop_instance(profile.name, profile.port, stop_patterns())"""
    from modelctl.core.capabilities import Capabilities
    from modelctl.engines import get_adapter
    profile = _make_profile(tmp_path)
    adapter = get_adapter("vllm")(profile, Capabilities())
    captured = {}

    def _fake_stop(name, port, patterns):
        captured.update(name=name, port=port, patterns=patterns)
        return True
    monkeypatch.setattr("modelctl.core.process.stop_instance", _fake_stop)
    adapter.stop_backend()
    assert captured["name"] == "buddy"
    assert captured["port"] == 8199
    assert captured["patterns"] == ["vllm serve"]


def test_engine_adapter_is_docker_runtime_default_false(tmp_path):
    """基类默认 is_docker_runtime() 返回 False（venv / 无 docker 概念路径）"""
    from modelctl.core.capabilities import Capabilities
    from modelctl.engines import get_adapter
    profile = _make_profile(tmp_path)
    adapter = get_adapter("llamacpp")(profile, Capabilities())
    assert adapter.is_docker_runtime() is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_engines_base_gpu.py::test_engine_adapter_stop_backend_default_uses_stop_instance tests/test_engines_base_gpu.py::test_engine_adapter_is_docker_runtime_default_false -v`

Expected: 全部 FAIL with `AttributeError: 'VllmAdapter' object has no attribute 'stop_backend'` / `'LlamacppAdapter' object has no attribute 'is_docker_runtime'`。

- [ ] **Step 3: 在 `EngineAdapter` 类内（`stop_patterns` 方法之后、`upstream_model_name` 之前，共 ~L98 处）插入两个方法**

```python
    def stop_backend(self) -> None:
        """停止本 profile 后端（默认 = venv / 通用进程路径）。

        默认实现（涵盖 vllm-venv / llamacpp / ollama-serve / unsloth / sglang / aphrodite /
        lmdeploy / tokenspeed-venv / trtllm-venv）调 stop_instance(name, port, stop_patterns())。
        子类覆盖点：VllmAdapter / TokenSpeedAdapter / TensorRtLlmAdapter 的 docker runtime
        覆盖为 stop_docker_instance(name, container_name)——容器路径下 PID 文件本不写
        （write_pid=False），且 fuser/pkill 对 docker 容器客户端失效，必须 docker rm -f。
        OllamaAdapter 不覆盖（共享 serve 特判在 all_service.stop_profile 内做）。
        """
        from modelctl.core.process import stop_instance
        stop_instance(self.profile.name, self.profile.port, self.stop_patterns())

    def is_docker_runtime(self) -> bool:
        """本 profile 是否走 docker runtime（docker run --detach）。

        默认 False。VllmAdapter / TokenSpeedAdapter / TensorRtLlmAdapter 在
        `cfg.docker_image` 非空时覆盖为 True；all_service.start_profile 据此
        决定 start_detached 的 write_pid 参数（docker 路径不写 PID 文件）。
        """
        return False
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_engines_base_gpu.py -v`

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```powershell
cd d:\WorkPlace\Pycharm\modelctl; git add src/modelctl/engines/base.py tests/test_engines_base_gpu.py; git commit -m "feat(engines): 基类新增 stop_backend 钩子与 is_docker_runtime 标识"
```

---

### Task 3: 三个 docker 引擎覆盖 `stop_backend` + `is_docker_runtime`

**Files:**
- Modify: `src/modelctl/engines/vllm.py`
- Modify: `src/modelctl/engines/tokenspeed.py`
- Modify: `src/modelctl/engines/tensorrt_llm.py`
- Test: `tests/test_engines_vllm.py` / `tests/test_engines_tokenspeed.py` / `tests/test_engines_tensorrt_llm.py`

**Interfaces:**
- Consumes: Task 1 的 `stop_docker_instance(name, container_name) -> bool`；各适配器已有的 `_container_name`（vllm 是 property，tokenspeed/trtllm 是 method）；各适配器已有的 `_resolve_runtime() -> (str, str | None)`。
- Produces: 三套 `stop_backend` + `is_docker_runtime` 覆盖（docker 分流 → 容器路径；venv 分流 → 基类行为）。

- [ ] **Step 1: 三个引擎各追加 2 个测试（docker + venv 分流）**

`tests/test_engines_vllm.py` 追加：

```python
def _fake_docker_runtime_profile():
    from modelctl.core.profile import Profile
    return Profile(name="q", engine="vllm", port=8110,
                   engine_config={"docker_image": "vllm/vllm-openai:test",
                                  "model": "/x"})


def test_vllm_stop_backend_docker_uses_docker_rm(monkeypatch):
    from modelctl.core.capabilities import Capabilities
    from modelctl.engines import get_adapter
    adapter = get_adapter("vllm")(_fake_docker_runtime_profile(), Capabilities())
    captured = {}
    def _fake_rm(name, container_name):
        captured.update(name=name, container_name=container_name)
        return True
    monkeypatch.setattr("modelctl.core.process.stop_docker_instance", _fake_rm)
    captured_pid = []
    monkeypatch.setattr("modelctl.core.process.stop_instance",
                        lambda *a, **k: captured_pid.append(a) or False)
    adapter.stop_backend()
    assert captured == {"name": "q", "container_name": "q-vllm"}
    assert captured_pid == []  # 不走 venv 路径


def test_vllm_stop_backend_venv_uses_stop_instance(monkeypatch):
    from modelctl.core.capabilities import Capabilities
    from modelctl.engines import get_adapter
    from modelctl.core.profile import Profile
    profile = Profile(name="q2", engine="vllm", port=8111, engine_config={"model": "/m"})
    adapter = get_adapter("vllm")(profile, Capabilities())
    captured = {}
    monkeypatch.setattr("modelctl.core.process.stop_instance",
                        lambda name, port, patterns: captured.update(name=name, port=port, patterns=patterns) or True)
    adapter.stop_backend()
    assert captured["name"] == "q2"
    assert captured["port"] == 8111
    assert captured["patterns"] == ["vllm serve"]


def test_vllm_is_docker_runtime_flag():
    from modelctl.core.capabilities import Capabilities
    from modelctl.engines import get_adapter
    from modelctl.core.profile import Profile
    docker_p = get_adapter("vllm")(
        Profile(name="q", engine="vllm", port=8110,
                engine_config={"docker_image": "x", "model": "/m"}), Capabilities())
    venv_p = get_adapter("vllm")(
        Profile(name="q2", engine="vllm", port=8111, engine_config={"model": "/m"}), Capabilities())
    assert docker_p.is_docker_runtime() is True
    assert venv_p.is_docker_runtime() is False
```

`tests/test_engines_tokenspeed.py` 追加对称三个测试（容器名 `x-tokenspeed`，runtime 翻转逻辑相同）；`tests/test_engines_tensorrt_llm.py` 追加对称三个测试（容器名 `x-trtllm`）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_engines_vllm.py -k "stop_backend or is_docker_runtime" -v; uv run pytest tests/test_engines_tokenspeed.py -k "stop_backend or is_docker_runtime" -v; uv run pytest tests/test_engines_tensorrt_llm.py -k "stop_backend or is_docker_runtime" -v`

Expected: 全部 FAIL with `AttributeError: 'VllmAdapter' object has no attribute 'is_docker_runtime'` 等。

- [ ] **Step 3: 在三个文件的 `class ...Adapter` 内各加两个方法（在 `stop_patterns` 之后）**

`src/modelctl/engines/vllm.py`（方法名前无装饰器，保持 `def`）：

```python
    def is_docker_runtime(self) -> bool:
        """vllm 路径判定：docker_image 字段非空时走 docker runtime。"""
        return self._resolve_runtime()[0] == "docker"

    def stop_backend(self) -> None:
        """docker 分支：docker rm -f <container>（清 PID 防御 venv/docker 环境切换残留）；
        venv 分支：基类 stop_instance（pkill 兜底）。"""
        if self._resolve_runtime()[0] == "docker":
            from modelctl.core.process import stop_docker_instance
            stop_docker_instance(self.profile.name, self._container_name)
        else:
            super().stop_backend()
```

`src/modelctl/engines/tokenspeed.py` 对称（`self._container_name` 是 method，签名相同、调用一致）：

```python
    def is_docker_runtime(self) -> bool:
        """tokenspeed 路径判定：docker_image 字段非空时走 docker runtime。"""
        return self._resolve_runtime()[0] == "docker"

    def stop_backend(self) -> None:
        """docker 分支：docker rm -f <container>；venv 分支：基类 stop_instance。"""
        if self._resolve_runtime()[0] == "docker":
            from modelctl.core.process import stop_docker_instance
            stop_docker_instance(self.profile.name, self._container_name)
        else:
            super().stop_backend()
```

`src/modelctl/engines/tensorrt_llm.py` 对称：

```python
    def is_docker_runtime(self) -> bool:
        """trtllm 路径判定：docker_image 字段非空时走 docker runtime。"""
        return self._resolve_runtime()[0] == "docker"

    def stop_backend(self) -> None:
        """docker 分支：docker rm -f <container>；venv 分支：基类 stop_instance。"""
        if self._resolve_runtime()[0] == "docker":
            from modelctl.core.process import stop_docker_instance
            stop_docker_instance(self.profile.name, self._container_name)
        else:
            super().stop_backend()
```

- [ ] **Step 4: 跑测试确认全部通过**

Run: `uv run pytest tests/test_engines_vllm.py tests/test_engines_tokenspeed.py tests/test_engines_tensorrt_llm.py -v`

Expected: 既有测试全部 PASS + 新增 3 套 × 3 = 9 个用例 PASS。

- [ ] **Step 5: Commit**

```powershell
cd d:\WorkPlace\Pycharm\modelctl; git add src/modelctl/engines/vllm.py src/modelctl/engines/tokenspeed.py src/modelctl/engines/tensorrt_llm.py tests/test_engines_vllm.py tests/test_engines_tokenspeed.py tests/test_engines_tensorrt_llm.py; git commit -m "feat(engines): vllm/tokenspeed/trtllm 覆盖 stop_backend 与 is_docker_runtime"
```

---

### Task 4: `all_service.py` 三处消费点接入

**Files:**
- Modify: `src/modelctl/core/all_service.py`
- Test: `tests/test_all_service.py`

**Interfaces:**
- Consumes: Task 1 的 `stop_docker_instance`；Task 2-3 的三个引擎 `stop_backend` / `is_docker_runtime`；既有 `is_running_any`。
- Produces:
  - `start_profile` 调用 `start_detached(profile.name, cmd, env, write_pid=not adapter.is_docker_runtime())`。
  - `stop_profile` 非 ollama 分支改 `adapter.stop_backend()`。
  - `stop_all` 内 `if is_running(profile.name):` → `if is_running_any(profile.name, profile):`。
  - `restart_profile` 开头的 `if is_running(profile.name):` → `if is_running_any(profile.name, profile):`。

- [ ] **Step 1: 改测试（`tests/test_all_service.py`）追加 3 个回归**

> 注：以下 3 个测试函数须 **整体复制** 到 `tests/test_all_service.py` 末尾（如需 mock 亦在函数内 inline `import`，避免顶部 import 污染）。

```python
from unittest.mock import MagicMock, patch  # 顶部 `tests/test_all_service.py` 如缺则补这一行


def test_start_profile_docker_write_pid_false(monkeypatch, tmp_path):
    """docker runtime（is_docker_runtime True）时 start_detached 必须收到 write_pid=False"""
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import Profile
    from modelctl.core.all_service import start_profile

    profile = Profile(name="qd", engine="vllm", port=8100,
                      engine_config={"docker_image": "vllm/vllm-openai:test", "model": "/m"})
    fake_adapter = MagicMock()
    fake_adapter.profile = profile
    fake_adapter.is_docker_runtime.return_value = True
    fake_adapter.build_command.return_value = (["docker", "run", "x"], {})
    fake_adapter.selected_gpus.return_value = None
    fake_adapter.wait_ready.return_value = True
    fake_adapter.upstream_api_key.return_value = None
    fake_adapter.metrics_mapping.return_value = None

    fake_proc = MagicMock(); fake_proc.pid = 123
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    with (
        patch("modelctl.engines.get_adapter", return_value=lambda p, c: fake_adapter),
        patch("modelctl.core.all_service.is_running_any", return_value=False),
        patch("modelctl.core.all_service.start_detached",
              return_value=(123, fake_proc)) as fake_start,
        patch("modelctl.core.all_service.wait_health", return_value=True),
    ):
        r = start_profile(profile, Capabilities(), 1.0)
    assert r.status == "skipped" or r.status == "ok"  # is_running_any False → 不 skipped，run 进去
    assert fake_start.call_args.kwargs["write_pid"] is False


def test_start_profile_venv_write_pid_default_true(monkeypatch, tmp_path):
    """venv runtime（is_docker_runtime False）时 start_detached 维持默认 write_pid=True（向后兼容）"""
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import Profile
    from modelctl.core.all_service import start_profile

    profile = Profile(name="qv", engine="vllm", port=8101, engine_config={"model": "/m"})
    fake_adapter = MagicMock()
    fake_adapter.profile = profile
    fake_adapter.is_docker_runtime.return_value = False
    fake_adapter.build_command.return_value = (["vllm", "serve", "/m"], {})
    fake_adapter.selected_gpus.return_value = None
    fake_adapter.wait_ready.return_value = True
    fake_adapter.upstream_api_key.return_value = None
    fake_adapter.metrics_mapping.return_value = None
    fake_proc = MagicMock(); fake_proc.pid = 124
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    with (
        patch("modelctl.engines.get_adapter", return_value=lambda p, c: fake_adapter),
        patch("modelctl.core.all_service.is_running_any", return_value=False),
        patch("modelctl.core.all_service.start_detached",
              return_value=(124, fake_proc)) as fake_start,
    ):
        start_profile(profile, Capabilities(), 1.0)
    # venv 路径要么不传 write_pid（默认 True），要么显式传 True
    kw = fake_start.call_args.kwargs
    assert kw.get("write_pid", True) is True


def test_stop_profile_calls_adapter_stop_backend(monkeypatch, tmp_path):
    """stop_profile 非 ollama 分支改走 adapter.stop_backend()，不再直接 stop_instance"""
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import Profile
    from modelctl.core.all_service import stop_profile

    fake_adapter = MagicMock()
    fake_adapter.profile = Profile(name="n1", engine="vllm", port=8112,
                                   engine_config={"docker_image": "x", "model": "/m"})
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr("modelctl.engines.get_adapter", lambda engine: lambda p, c: fake_adapter)
    r = stop_profile(fake_adapter.profile, Capabilities(), tmp_path)
    assert r.status == "ok"
    fake_adapter.stop_backend.assert_called_once_with()
    # stop_instance 旧路径不应被直接调用（stop_backend 内部才调）


def test_stop_all_uses_is_running_any(monkeypatch, tmp_path):
    """一键关闭按 is_running_any(name, profile) 判是否 stop（docker 容器在跑也要 stop）"""
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import Profile
    from modelctl.core.all_service import stop_all

    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    profiles = [Profile(name="n1", engine="ollama", port=11434,
                        engine_config={"model": "/m"}),
                Profile(name="n2", engine="vllm", port=8113,
                        engine_config={"docker_image": "x", "model": "/m"})]
    monkeypatch.setattr("modelctl.core.all_service.list_profiles", lambda d: profiles)
    seen = []
    monkeypatch.setattr("modelctl.core.all_service.is_running_any",
                        lambda n, p: seen.append(n) or (n == "n2"))
    monkeypatch.setattr("modelctl.core.all_service.stop_stats", lambda: stop_all.__globals__["ComponentResult"]("s", "ok"))
    monkeypatch.setattr("modelctl.core.all_service.stop_gateway", lambda: stop_all.__globals__["ComponentResult"]("g", "ok"))
    monkeypatch.setattr("modelctl.core.all_service.stop_profile",
                        lambda p, c, d: stop_all.__globals__["ComponentResult"](f"m:{p.name}", "ok"))
    r = stop_all(tmp_path)
    assert "n2" in seen and "n1" in seen  # 两个 profile 都问过了 is_running_any
    assert len([x for x in r if x.component.startswith("m:")]) == 1  # 仅 n2（is_running_any True）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_all_service.py -k "write_pid or stop_backend or is_running_any" -v`

Expected: 三个新增测试 FAIL（start_profile 没传 write_pid、stop_profile 走 stop_instance、stop_all 用 is_running）。

- [ ] **Step 3: 改 `all_service.py`**

`start_profile` 顶部 `if is_running(profile.name):` → `if is_running_any(profile.name, profile):`，并把 `pid, proc = start_detached(profile.name, cmd, env)` 改为：

```python
    pid, proc = start_detached(profile.name, cmd, env,
                               write_pid=not adapter.is_docker_runtime())
```

`stop_profile` 内 `else: stop_instance(profile.name, profile.port, adapter.stop_patterns())` 改为：

```python
    else:
        adapter.stop_backend()
```

（保持不变：ollama 特殊分支、`logger.info`）

`stop_all`：`if is_running(profile.name):` → `if is_running_any(profile.name, profile):`。

`restart_profile`：`if is_running(profile.name):` → `if is_running_any(profile.name, profile):`。

`stop_all` 与 `restart_profile` 顶部的 from-import 已存在；确认 `is_running_any` 在 `from modelctl.core.process import` 中添加（同文件的 `from modelctl.core.process import ...` 行加 `is_running_any,`）。注：`stop_profile` 的非 ollama 分支改 `adapter.stop_backend()` 后，函数体顶层已不再调 `stop_instance`（stop_instance 由 `adapter.stop_backend()` 内部触发），`is_running` / `stop_instance` 两个 from-import 是否需要保留请由读者自行裁剪；本步骤**仅保证** `is_running_any` 被 import + `stop_profile` 调 `adapter.stop_backend()`，不要求清理旧 import（陈旧 import 无 runtime 影响）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_all_service.py -v`

Expected: 全部 PASS（新增 3 个 + 既有用例）。

- [ ] **Step 5: Commit**

```powershell
cd d:\WorkPlace\Pycharm\modelctl; git add src/modelctl/core/all_service.py tests/test_all_service.py; git commit -m "refactor(all_service): start/stop 接入 is_docker_runtime/stop_backend/is_running_any"
```

---

### Task 5: `gateway.py::is_model_available` 收敛 + 状态映射

**Files:**
- Modify: `src/modelctl/core/gateway.py`
- Modify: `src/modelctl/core/colors.py`
- Test: `tests/test_gateway.py`

**Interfaces:**
- Consumes: Task 1 的 `is_running_any`。
- Produces:
  - `is_model_available(model: GatewayModel) -> bool` 收敛为 `return is_running_any(model.name, model.adapter.profile if model.adapter else None)`。
  - `colors.status_color(state)` 映射收敛为 6 状态。

- [ ] **Step 1: 改网关测试（`tests/test_gateway.py`）**

先 `Read` 全部既有 `test_list_models_*` 测试函数（在 L400-470 区间），逐个改造：

`test_list_models_includes_external_started`（L421-438）：去掉 `patch("modelctl.core.gateway.is_model_healthy", return_value=True)` 与 `is_running`；改为 `patch("modelctl.core.gateway.is_running_any", return_value=True)`，构造 GatewayModel 时**带 adapter + profile**（使 `is_running_any(name, profile)` 正确访问）：

```python
def test_list_models_includes_external_started(tmp_path, monkeypatch):
    """Running 端口健康（无受管 PID，docker/supervisor 拉起）的模型须出现在 /v1/models"""
    from modelctl.core.profile import Profile
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    reg = {
        "qwen3.8-flash-next-vllm": GatewayModel(
            name="qwen3.8-flash-next-vllm",
            engine="vllm",
            backend_url="http://upstream",
            upstream_model="x",
            api_key=None,
            health_url="http://upstream/",
            adapter=MagicMock(profile=Profile(
                name="qwen3.8-flash-next-vllm", engine="vllm", port=8110)),
        )
    }
    app = create_app(reg, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with patch("modelctl.core.gateway.is_running_any", return_value=True):
        resp = _run(_get(app, "/v1/models"))
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["data"]] == ["qwen3.8-flash-next-vllm"]
```

`test_list_models_excludes_stale_pid_but_alive`（L441-461）**删除整个函数**（语义已被 `is_running_any` 的 dead-PID 清理路径吞并——新方案下只要 /health 2xx 就算运行中，不再有"端口占用但 PID 死 → 不可用"分支）。

新增两个白盒用例：

```python
def test_is_model_available_with_profile_uses_profile():
    """is_model_available 持有 adapter.profile 时以 profile 接入 is_running_any"""
    from modelctl.core.profile import Profile
    profile = Profile(name="ap", engine="vllm", port=8111)
    m = GatewayModel(
        name="ap", engine="vllm", backend_url="http://x", upstream_model="x",
        api_key=None, health_url="http://x/",
        adapter=MagicMock(profile=profile))
    with patch("modelctl.core.gateway.is_running_any", return_value=False) as spy:
        assert is_model_available(m) is False
    spy.assert_called_once_with("ap", profile)


def test_is_model_available_no_adapter_fallback_to_none():
    """旧 GatewayModel（adapter=None）仍能判定，profile 缺省 → 纯 PID 探测"""
    m = GatewayModel(
        name="np", engine="vllm", backend_url="http://x", upstream_model="x",
        api_key=None, health_url="http://x/")
    with patch("modelctl.core.gateway.is_running_any", return_value=True) as spy:
        assert is_model_available(m) is True
    spy.assert_called_once_with("np", None)
```

`test_list_models_includes_external_started` 既有的 `from modelctl.core.gateway import ...` import 应已有，若 test 顶部缺 `MagicMock` / `GatewayModel` / `is_model_available` import 则补：

```python
from unittest.mock import MagicMock, patch
from modelctl.core.gateway import GatewayModel, create_app, is_model_available  # 依实际 import 补
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_gateway.py -k "is_model_available or external_started or stale_pid" -v`

Expected: 3 新增/改造测试 FAIL（旧实现走 `is_running`/`is_model_healthy`，新签名 `is_running_any` 没接入）。

- [ ] **Step 3: 改 `gateway.py` + `colors.py`**

`src/modelctl/core/gateway.py` L390-401 的 `is_model_available` 函数体替换为：

```python
def is_model_available(model: GatewayModel) -> bool:
    """模型是否可路由：端口 /health 2xx 优先，PID 文件机器兜底（与原 is_running 退化一致）。

    adapter.profile 缺省（旧 GatewayModel / 未注入 adapter 时）退回纯 PID 探测——
    等效 venv-only 路径下"PID 文件可读 + 进程 alive"语义，无回归。
    """
    return is_running_any(model.name, model.adapter.profile if model.adapter else None)
```

文件顶部确认有 `from modelctl.core.process import is_running_any`（如无则加）。

`src/modelctl/core/colors.py` L415-437 的 `status_color` 映射重写：

```python
def status_color(state: str) -> str:
    """实例状态 → ANSI 色码。未知状态回退 DIM。

    映射（6 状态，PID 残留 = venv 孤儿 / docker 切换残留；未知 → DIM）：
    - 运行中 → STATUS_RUNNING（绿色加粗）
    - 已停止 → STATUS_STOPPED（灰色）
    - 未就绪 → STATUS_NA（灰色）
    - 正常   → STATUS_HEALTHY（绿色加粗）
    - 无响应 → STATUS_UNHEALTHY（红色）
    - PID 残留 → STATUS_ERROR（红色加粗）
    - 未知   → DIM
    """
    mapping = {
        "运行中": "STATUS_RUNNING",
        "已停止": "STATUS_STOPPED",
        "未就绪": "STATUS_NA",
        "正常": "STATUS_HEALTHY",
        "无响应": "STATUS_UNHEALTHY",
        "PID 残留": "STATUS_ERROR",
        "unknown": "STATUS_NA",
    }
    return style_of(mapping.get(state, "DIM"))
```

注：`STATUS_EXTERNAL` 颜色定义保留（L238 的 `Color(...)`），避免外部用户 env 覆写 `STATUS_EXTERNAL=...` 仍然可用；只是状态映射表不再引用它。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_gateway.py -v`

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```powershell
cd d:\WorkPlace\Pycharm\modelctl; git add src/modelctl/core/gateway.py src/modelctl/core/colors.py tests/test_gateway.py; git commit -m "refactor(gateway): is_model_available 收敛为 is_running_any；colors 状态映射收敛为 6 状态"
```

---

### Task 6: `cli.py` 状态枚举 7→6 + 标题注释

**Files:**
- Modify: `src/modelctl/cli.py`
- Modify: `models/vllm/qwen3.8-flash-next.yaml`
- Test: `tests/test_modelctl.py`

**Interfaces:**
- Consumes: Task 1 的 `is_running_any`。
- Produces:
  - `state_words = {"运行中", "已停止", "正常", "无响应", "PID 残留", "未就绪"}` (L208)。
  - `_instance_state(profile, name)` 改判 `is_running_any`，下列两个内联 `("运行中", "已外部启动")` 收敛为 `("运行中",)`。
  - `models/vllm/qwen3.8-flash-next.yaml` 头注释 L8-17：docker 生命周期第 5 条改为"modelctl stop 走 `docker rm -f <container>`，并通过 `stop_docker_instance` 清理本工具 PID 文件（容器路径本身不写 PID，PID 仅 venv 路径写）"。

- [ ] **Step 1: 改 CLI 测试（`tests/test_modelctl.py`）**

`test_list_group_route_mapping` (L52-74)：把 `monkeypatch.setattr("modelctl.cli.is_running", lambda name: name == "qwen3.8-vllm")` 改为：

```python
    monkeypatch.setattr("modelctl.cli.is_running_any",
                        lambda name, profile: name == "qwen3.8-vllm")
```

（PID 文件创建行保留——现在 `is_running_any` 对 `qwen3.8-vllm` 走的是 stub，PID 文件状态不影响断言。）

`test_list_group_route_mapping_counts_external_started` (L77-95) 整个测试**删除**——新方案下"外部启动"并入"运行中"，不再有 `已外部启动` 字面输出；用下面回归替：

```python
def test_list_external_started_shown_as_running(monkeypatch, tmp_path):
    """外部启起的模型（is_running_any True）在 list 中标记为"运行中"（端口 2xx 不区分来源）"""
    (tmp_path / "flash-next.yaml").write_text(
        "group: qwen3.8-flash-next\nengine: vllm\nport: 8110\nvllm:\n  model: q\n", encoding="utf-8")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr("modelctl.cli.is_running_any",
                        lambda name, profile: name == "qwen3.8-flash-next-vllm")
    monkeypatch.setattr("modelctl.cli._stats_token_rate", lambda p: None)
    out = io.StringIO(); _reset_log()  # 实际测试可用 capsys/stub capsys
    rc = cli.main(["list", "--models-dir", str(tmp_path)])
    assert rc == 0
    # 路由提示括号内取状态列原值
    out = capsys.readouterr().out
    assert "运行中" in out
    assert '输入 "qwen3.8-flash-next" 路由至 qwen3.8-flash-next-vllm（运行中）' in out
```

`test_status_running_minimal` 与 `test_status_output` 无需改（既有用例未硬断言"已外部启动"/"PID 异常"字面）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_modelctl.py -k "group_route or external" -v`

Expected: `test_list_group_route_mapping` FAIL（旧 `_instance_state` 不调 `is_running_any`）；`test_list_external_started_shown_as_running` 现有测试名不存在 → 新增测试用 capsys 时若 `list` 输出文案仍含"已外部启动" → FAIL。

- [ ] **Step 3: 改 `cli.py`**

`_cmd_list` / `_cmd_status` / `_group_runtime_target` 顶部 from-import 加 `is_running_any`（已 import `is_running`，同行追加 `is_running_any`）。

L208 的 `state_words = {...}` 替换为：

```python
    state_words = {"运行中", "已停止", "正常", "无响应", "PID 残留", "未就绪"}
```

L259-277 的 `_instance_state` 整体替换为：

```python
def _instance_state(profile: Profile | None = None, name: str | None = None) -> str:
    """判断实例状态（is_running_any 统一口径：/health 2xx 优先，PID 文件兜底）。

    状态取值：
    - 运行中     /health 2xx 或 venv PID 文件存活（含外部 docker/supervisor 拉起的同端口服务）
    - PID 残留   PID 文件存在但进程不存活且端口不响应该 profile（venv 孤儿 + docker 残留）
    - 已停止     无 PID 文件且端口无响应
    """
    if name is None and profile is not None:
        name = profile.name
    assert name is not None
    if is_running_any(name, profile):
        return "运行中"
    # "PID 残留"识别：PID 文件仍在但判定为 False（is_running_any 无副作用，不删文件；
    # 清理由 stop_instance / stop_docker_instance 负责），此处提示用户先 stop 再 start
    if pid_file(name).is_file():
        logger.warning(f"{name}：疑似残留 PID 文件，建议执行 `modelctl stop {name}` 清理后再次 start")
        return "PID 残留"
    return "已停止"
```

L470：`if state in ("运行中", "已外部启动"):` → `if state == "运行中":`。

L491：`if _instance_state(profile=profiles[0]) in ("运行中", "已外部启动"):` → `if _instance_state(profile=profiles[0]) == "运行中":`。

L509 函数 docstring 末尾改为"`'运行中'`"；L516 `if state in ("运行中", "已外部启动"):` → `if state == "运行中":`。

L553：`if state in ("运行中", "已外部启动"):` → `if state == "运行中":`。

L559 括号内注释改为 "（运行中）"（运行时状态已收敛为单值）。

确保 `logger` 已 import（L42 已存在）；`_port_health_ok` 保留不动（status 后还有 L474 处 `health = "正常" if ok else "无响应"` 判定流量，`_instance_state` 不再使用后 `_port_health_ok` 仍被 `_cmd_status` 入口的 `state in ("运行中",)` 门控——实际 `_cmd_status` 在 L469-474 仍用 `adapter.wait_ready(3.0)`，`_port_health_ok` 仅被 `_instance_state` 旧版本使用，改后即成"孤儿函数"，**保留**它不删（避免周期内其他 test / plugin 引用——若用户后续无引用可自行清，本任务不扩范围）。

`models/vllm/qwen3.8-flash-next.yaml` L17 行替换为：

```
#   5. modelctl stop 走 stop_docker_instance——docker rm -f + 清本地 PID（容器路径不写
#      PID 文件，仅 venv 路径写；环境切换残留由同一把防御性清理兜住）
```

更细致，把 L11 行 `# 生命周期：` 之后的 5 条子项整体重排（第 5 条就是此次重点）：

```
# 生命周期：
#   1. 首次启动前 docker 自动 pull 该镜像（约 21.8GB）
#   2. 模型按 download 段从 ModelScope 拉到 $MODEL_ROOT（默认 /raid5/sh/model-hf），
#      modelctl 写回 yaml `model:` 字段为本地绝对路径（之后的启动不再重复下载）
#   3. 容器使用 nvidia-container-toolkit + --gpus JSON 透传 yaml gpu_list 指定的同卡
#   4. 容器内 vLLM 绑定 8000，宿主端口 = yaml 顶层 `port`；docker 路径不写 PID 文件
#   5. modelctl stop 走 stop_docker_instance——docker rm -f + 清本地 PID（防御 venv→docker
#      环境切换残留），释放 GPU 锁
```

- [ ] **Step 4: 跑测试确认全部通过**

Run: `uv run pytest tests/test_modelctl.py -v`

Expected: 全部 PASS（含新增 `test_list_external_started_shown_as_running`；`test_status_output` 等既有用例不受影响）。

- [ ] **Step 5: Commit**

```powershell
cd d:\WorkPlace\Pycharm\modelctl; git add src/modelctl/cli.py models/vllm/qwen3.8-flash-next.yaml tests/test_modelctl.py; git commit -m "refactor(cli): 状态枚举 7→6，_instance_state 改用 is_running_any；yaml docker 生命周期注释更新"
```

---

### Task 7: 全量回归 + 验收报告（无新增文件，无新增 commit）

**Files:** 无（验证 + 报告）

**Interfaces:** 无

- [ ] **Step 1: 全量测试**

Run: `uv run pytest -v`（项目根 `d:\WorkPlace\Pycharm\modelctl`）

Expected: 全部 PASS。重点关注：
- `tests/test_process.py` 新增 9 个 + 既有用例
- `tests/test_engines_vllm.py` / `tokenspeed` / `tensorrt_llm` 各 3 个新用例
- `tests/test_all_service.py` 新增 3 个 + 既有用例
- `tests/test_gateway.py` 删除 1 个 + 新增 2 个 + 改造 1 个
- `tests/test_modelctl.py` 删除 1 个 + 新增 1 个 + 改造 1 个 + 既有用例
- 全套跑下来不得有 `AttributeError` / `ImportError` / `Failed: ...`（若显现本地 `timer` / 麦克风等系统级失败与本改无关可忽略，须在报告中记录）

- [ ] **Step 2: 文字级验收（对应 spec §5 七条）**

逐条在报告中写明：

1. ✅ `modelctl status <docker-profile>` 显示"运行中"——由 `test_list_group_route_mapping`（stub `is_running_any` 返回 True）+ `test_status_output` 既有用例覆盖；docker 路径 PID 文件不写的回归已由 `test_start_profile_docker-write_pid_false` 保证。
2. ✅ `modelctl stop <docker-profile>` 后容器无残留、PID 清掉——由 `test_vllm_stop_backend_docker_uses_docker_rm`（mock 断言 docker rm -f + 不串 venv 路径）+ `test_stop_docker_instance_runs_rm_and_cleans`（PID + gpu_lock 都清）覆盖。
3. ✅ 手动 docker run（不经 modelctl）也能被 status 识别——由 `test_list_external_started_shown_as_running` 覆盖（`is_running_any` 接入 port 健康 → 运行中）。
4. ✅ `modelctl list` 所有 docker / venv / 外部 docker 拉起的 profile 统一"运行中"——由 `test_list_group_route_mapping` + `test_list_external_started_shown_as_running` 覆盖。
5. ✅ `modelctl gateway status` 命中"网关已运行"（现有行为不变，回归通过）——由 `tests/test_all_service.py` 既有 `test_status_gateway_*` 既有用例 + 本次 `tests/test_gateway.py` 三个 `is_model_available` 用例联合覆盖；gateway 主路径回归靠全量 `pytest` PASS 验证。
6. ✅ 现有 tests 在无适配 PR 之前通过——本计划的 Task 7 Step 1 全量 `uv run pytest -v` 必须 0 FAIL（含 before-change snapshot）；任何 FAIL 必须显式修到 PASS 再 commit，不得留 skip 或 skip 理由写 "夹带"。
7. ✅ `modelctl start <venv-profile>` 仍写 PID 文件，stop 后清理——由 `test_start_profile_venv-default-write_pid` （Task 1 `test_start_detached_write_pid_default_true` + Task 4 start_profile 注入 `write_pid=not adapter.is_docker_runtime()`，venv 引擎 `is_docker_runtime()` False → `write_pid=True`）联合覆盖；stop 路径 `test_stop_instance_*` 既有 + `test_stop_profile_calls_adapter_stop_backend` 联合回归。

- [ ] **Step 3: 状态枚举终验（grep 残留）**

Run:

```powershell
cd d:\WorkPlace\Pycharm\modelctl; (git grep -n "已外部启动\|PID 异常\|STATUS_EXTERNAL") 2>&1
```

预期命中行（除本报告与本 spec 文件外，业务代码应当为 0）：

| 文件 | 预期 | 说明 |
|---|---|---|
| `docs/superpowers/specs/2026-09-02-*.md` | ✔ | 历史 spec，专述旧→新对照，**保留** |
| `docs/superpowers/plans/2026-09-02-*.md` | ✔ | 本计划，专述旧→新对照，**保留** |
| `src/modelctl/cli.py` | ✘ | 不应再出现 |
| `src/modelctl/core/colors.py` 状态映射表 | ✘ | `STATUS_EXTERNAL` 颜色定义保留（L238 Color 定义）即可，docstring 提到 `STATUS_EXTERNAL` 仅删 |
| `src/modelctl/core/gateway.py` docstring | ✘ | 措辞旧"已外部启动"应改为 `运行中` 或删除 |
| `tests/test_*.py` | ✘ | 全部旧断言应已被本计划替换 |

若命中的行不属于上面"保留"两列，必须**修复**后再交付；放进报告后 commit（单独 `refactor(cleanup): grep 残留字面清理` commit 或在原 commit amend 内带上）。

---

## Self-Review（本计划作者自检，不重复占位）

执行本计划前先快速自检三项：

1. **Spec 覆盖**：spec §4 的 8 项实施任务与下方 Task 对应：
   - §4.1 `core/process.py` 新增 → Task 1
   - §4.2 `engines/base.py` 新增 → Task 2
   - §4.3 三个 docker 引擎 override → Task 3
   - §4.4 `all_service.py` 注入 → Task 4
   - §4.5 `gateway.py::is_model_available` 收敛 → Task 5
   - §4.6 `cli.py` 状态枚举 → Task 6
   - §4.7 `qwen3.8-flash-next.yaml` 头注释 → Task 6（与 cli 同步 commit）
   - §4.8 tests 同步 → 每个 Task Step 1 已带 TDD
2. **占位扫描**：每次检查任一 Step 不能有余 "TBD"、"参考 Task N"、"实现细节略"——均已展开；唯二例外说明：(a) `tests/test_engines_tokenspeed.py` / `tensorrt_llm.py` 的"对称三个测试"在 Plan 内未逐行展开，**执行时按 vllm 测试的容器名/引擎名简单 `replace`**（vllm → tokenspeed / trtllm，端口 8110 → 8111/8112，`docker_image: vllm/vllm-openai:test` → 对应）。 (b) Task 7 Step 2 第 5 条验收仅在"覆盖范围"粒度展开，逐行回归交给全量 pytest 跑通的事后报告。
3. **类型 / 签名一致性**：cross-task 签名核对：
   - `is_running_any(name, profile)`：Task 1 def → Task 4/5/6 调用点均为 `(name, profile)` 二元，`profile: Profile | None`
   - `stop_backend()` / `is_docker_runtime()`：Task 2 定义默认 → Task 3 覆盖，`all_service` 调用 `adapter.stop_backend()` / `adapter.is_docker_runtime()`，零参
   - `_container_name`：vllm 是 property（`self._container_name` 无 `()`），tokenspeed/trtllm 是 method（同样不吃参数、同样调用一致）——Task 3 Step 3 体已按此统一写
   - `Profile.port`、`Profile.api_key`、`Profile.engine_config`：与 `Profile` dataclass 字段对齐（见 `src/modelctl/core/profile.py` L37-58）
   - `GatewayModel.adapter` 可空：`gateway.is_model_available` 内用 `model.adapter.profile if model.adapter else None` 防 AttributeError

---

## Execution Handoff

按 writing-plans 规范，本计划保存后由执行方选择：

**"Plan complete and saved to `docs/superpowers/plans/2026-09-02-service-state-via-health.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - 每个 Task 派一个新 subagent、between-task 两阶 review、快迭代
**2. Inline Execution** - 当前 session 用 executing-plans 顺序跑 7 个 Task、按 Task 末 checkpoint review

**Which approach?"**

若 Subagent-Driven：**REQUIRED SUB-SKILL: superpowers:subagent-driven-development**（fresh subagent per task + two-stage review）
若 Inline Execution：**REQUIRED SUB-SKILL: superpowers:executing-batch**（Execute tasks in this session using executing-plans, batch execution with checkpoints）

> 注：Task 1 是其余 6 个 Task 的 hard dependency（提供 `is_running_any` / `stop_docker_instance` / `write_pid` 参数）；Task 2 是 Task 4 的 hard dependency（提供 `stop_backend` / `is_docker_runtime`）；Task 3 是 Task 4 的 soft dependency（start_profile 仍可用，但 docker 路径需要 override 才能写 write_pid=False，不要求 Task 3 先于 Task 4）。 建议执行顺序：1 → 2 → 3 → 4 → 5 → 6 → 7（串行）。