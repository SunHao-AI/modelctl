# 设计：以端口健康为主的状态判定 + PID 文件降为辅助

- 日期：2026-09-02
- 状态：已与用户确认设计（方案 A）
- 影响范围：`core/process.py`、`core/gateway.py`、`core/all_service.py`、`cli.py`、`engines/{base,vllm,tokenspeed,tensorrt_llm}.py`、相关测试

## 1. 背景与问题

`modelctl` 在 docker runtime 下启动引擎时无有效 PID 文件可用——`docker run --detach` 客户端在容器创建后 ~1s 内退出，`Popen.pid` 写入 pid 文件的号已死。当前实现把"PID 文件存在性"作为运行态判定的主信号，导致：

1. `modelctl status` / `list` 显示 docker 实例为 "PID 异常"，而实际容器在跑——状态列失真。
2. `modelctl stop` 会 kill 已死的客户端 PID（无效），仅靠 `fuser` / `pkill -f` 兜底；`pf.unlink` 后容器仍跑、下次 status 才算"已外部启动"。
3. `gateway.is_model_available` 分支 2（PID 文件存在 + dead → False）会让 docker 实例在前 1s 内不被网关路由。
4. 任何外部 docker/supervisor 拉起的实例（用户手动 `docker run`）只能靠 pid 文件不存在才落到"已外部启动"分支，一旦有残留 PID 文件就误判。
5. venv 路径与 docker 路径使用同一个"PID 文件存在性"判定抽象，但语义不兼容（venv：PID 文件 = 真实进程；docker：PID 文件 = 已死客户端）。

需求边界（已与用户确认）：
- 支持 4 类部署模式：`modelctl start <docker>`、外部 docker/supervisor 拉起、`modelctl start <venv>`、共享进程（多 profile 共用一个后端，含 ollama）。
- 同 [引擎, 端口] 组合在单台机器上唯一（enforce）。
- 状态判定"以端口健康为准"。
- PID 文件保留为辅助（venv 路径仍写，docker 路径不写）。

## 2. 设计方案：方案 A

### 2.1 核心抽象

**单一事实来源**：新增 `core/process.py::is_running_any(name: str, profile: Profile | None) -> bool`。

判定链（全部 None-safe，任一步异常 → False）：

```
1. 若 profile 不为 None：探测 profile.port /health (open_local, timeout=2s)
   - 2xx → True
   - 失败/异常 → fall through 到 2
2. PID 文件存在 (cache_dir()/{name}.pid)
   - 存在 + is_pid_alive(pid)  → True
   - 存在 + not is_pid_alive   → False（dead PID 文件保留，不在此清理）
   - 文件损坏（无法解析为 int） / 缺失 / 其他 → False（损坏文件同样保留）
3. False
```

**判定必须无副作用**：`is_running_any` 只读不写——不得 `unlink` PID 文件、不得释放 GPU 锁。理由：CLI 的 "PID 残留" 状态需要在判定返回 False 之后回看 `pid_file(name).is_file()` 才能识别；若判定内部就地删除文件，该状态永不可达，且用户会丢失"有孤儿实例需要 stop"的唯一线索。清理职责归 **stop 路径**：`stop_instance`（已有 `pf.unlink`）与 `stop_docker_instance`（防御 venv↔docker 切换残留）。

签名约定：`profile` 缺失的位置（如 `gateway.restart` 等不持有 Profile 的调用点、`stats_restart`、`ui-*` 实例）退回 PID-only 探测，与原 `is_running(name)` 判定结果等价（`is_running` 自身在 dead/损坏分支会顺手 unlink——保留其原行为不动，仅新的统一入口 `is_running_any` 不做副作用）。

### 2.2 接口层改动（`core/process.py`）

```python
def is_running_any(name: str, profile: Profile | None) -> bool:
    """统一运行态判定：端口 /health 优先，PID 文件机器兜底。

    profile 缺省时（gateway / stats / 等不持有 Profile 的场景）只做 PID 探测，
    与原 is_running(name) 行为一致；任何异常（端口不可达 / PID 损坏）返回 False。
    """


def stop_docker_instance(name: str, container_name: str) -> bool:
    """docker runtime 路径停止：

    1. subprocess.run(["docker", "rm", "-f", container_name])
    2. 删除 PID 文件（best-effort，docker runtime 当前不写，兼容历史残留）
    3. 释放 gpu_lock
    返回是否执行了 rm -f（容器无所谓 — docker rm -f 幂等）。
    """


def start_detached(name: str, command: list[str], extra_env: dict[str, str],
                    write_pid: bool = True) -> tuple[int, subprocess.Popen]:
    """增加 write_pid 参数（默认 True 保持向后兼容）。
    docker runtime 调用方传 False，让后续状态判定不被已死客户端 PID 误导。
    """
```

保留不动：`is_pid_alive`、`is_running`、`stop_instance`、`docker_container_alive`、`wait_health`、`open_local`。`is_running` 仍保留**纯 PID 探测**语义（venue runtime 路径与 CLI `gateway` / `stats` / `ui-*` 实例 stop 的健壮语义）。

### 2.3 引擎适配器接口（`engines/base.py`）

新增 engine 级停止钩子（默认 = 现有 stop_instance）：

```python
class EngineAdapter:
    def stop_backend(self) -> None:
        """停止本 profile 后端。

        默认实现（涵盖 vllm-venv / llamacpp / ollama-serve / unsloth / sglang /
        aphrodite / lmdeploy / tokenspeed-venv / trtllm-venv）调用
        stop_instance(profile.name, profile.port, stop_patterns())。

        子类覆盖点：
        - VllmAdapter / TokenSpeedAdapter / TensorRtLlmAdapter 的 docker runtime
          覆盖为 stop_docker_instance(profile.name, container_name)（先 rm -f
          容器，再清理 PID 文件——venv 与 docker 共用 PID 文件位置，防环境切换残留）。
        - OllamaAdapter 不覆盖（共享 serve 特判在 all_service 内做）。
        """
        from modelctl.core.process import stop_instance
        stop_instance(self.profile.name, self.profile.port, self.stop_patterns())
```

`VllmAdapter` 新增（基类已有 `is_running_any`，docker 路径 stop 钩子覆盖）：

```python
def stop_backend(self) -> None:
    runtime, _ = self._resolve_runtime()
    if runtime == "docker":
        from modelctl.core.process import stop_docker_instance
        stop_docker_instance(self.profile.name, self._container_name)
    else:
        super().stop_backend()
```

`TokenSpeedAdapter`、`TensorRtLlmAdapter` 同模式（已有 `self._container_name`、`self._resolve_runtime()`）。

### 2.4 启动路径微调

`VllmAdapter.build_command` 不变；`all_service.start_profile` 不直接调 `start_detached`，由 adapter 提供"这是 docker runtime"信号：

```python
# all_service.start_profile
adapter = get_adapter(profile.engine)(profile, caps)
...
cmd, env = adapter.build_command()
write_pid = not adapter.is_docker_runtime()  # 新增方法（基类默认 False）
pid, proc = start_detached(profile.name, cmd, env, write_pid=write_pid)
adapter.spawned_proc = proc
```

`EngineAdapter.is_docker_runtime()` 默认 False；vllm/tokenspeed/trtllm 的运行时为 docker 时覆盖为 True。`write_pid=False` 时不写 PID 文件，仍返回 `(pid, proc)` 维持签名兼容（pid 是 Popen.pid 的本机号，仅用于日志显示）。

`VllmAdapter.wait_ready` 与 `backend_dead` 不变（已用 `docker_container_alive`）。

### 2.5 消费点改造

| 文件 | 现状 | 改造 |
|---|---|---|
| `cli.py::_instance_state` | 三段判定（PID 存在+alive → "运行中"，PID 存在+dead → "PID 异常"，PID 不存在+port 健康 → "已外部启动"，否则 "已停止"） | 简化为 `if is_running_any(name, profile) → "运行中"` + 特殊状态 `pid_file 存在+is_running_any False` → "PID 残留"（提示用户 `modelctl stop <name>`） |
| `cli.py::state_words` | `{"运行中", "已外部启动", "已停止", "正常", "无响应", "PID 异常", "未就绪"}` | `{"运行中", "已停止", "正常", "无响应", "PID 残留", "未就绪"}` |
| `cli.py::_cmd_ui_start/stop` | `is_running(instance)` | 保持（ui-* 实例是纯 venv，PID 语义已正确） |
| `gateway.py::is_model_available` | 4 行 if/else（PID 存在性 + 端口兜底） | `return is_running_any(model.name, model.adapter.profile)`（一行） |
| `all_service.py::stop_profile` | ollama 特判 + stop_instance | ollama 特判保留；`else` 改为 `adapter.stop_backend()` |
| `all_service.py::start_guide/status_gateway/stats` 内 `is_running` | Venv 子进程 Python，PID 文件正常写 | 保留（venv 路径语义一致） |
| `all_service.py::stop_all` 中 `if is_running(profile.name):` | 同上 | **改造**：`is_running_any(profile.name, profile)`（with profile 拿得到 port，docker runtime 也能正确 skip） |
| `stats.py` 中使用 PID 的位置 | 仅 ollama / llamacpp / 走 gateway 模块的引用 | 无改动 |

### 2.6 状态枚举收敛

旧 7 状态 → 新 6 状态：

| 旧 | 新 | 语义变化 |
|---|---|---|
| 运行中 | 运行中 | `is_running_any` 命中（端口 200 或 venv PID 活） |
| 已外部启动 | ~~移除~~ | 合并进"运行中"（端口 200 不区分来源） |
| PID 异常 | PID 残留 | 仅当 PID 文件存在 + PID 死 + 端口无响应（venv 路径的孤儿实例 + docker 路径的历史残留） |
| 已停止 | 已停止 | `is_running_any` False 且无 PID 文件 |
| 正常 / 无响应 / 未就绪 | 不变 | 健康检查诊断性状态 |

提示文案：检测到"PID 残留"时 `logger.warning(f"{name} 疑似残留 PID，建议执行 `modelctl stop {name}` 清理后再次 start")`，纯信息性，不阻断。

### 2.7 错误处理与边界场景

| 场景 | 行为 |
|---|---|
| 端口被外部进程占用（绕过 modelctl 的工具，如手工 docker run） | `is_running_any` → True，状态"运行中"。stop 走 adapter.stop_backend：docker runtime → `docker rm -f <container>`（命中已名同容器），venv runtime → stop_instance + pkill 兜底。 |
| docker 容器 OOM 被 daemon 杀掉 | 容器不存在 → `is_running_any` False（端口 + PID 双探失败）→ 状态"已停止"。下次 start 仍由 `check_requirements` 的 `docker rm -f` 清残留容器。 |
| venv 进程崩溃 | `wait_ready` 的 `alive_check` 立刻命中 → `backend_dead` True → 日志摘录（已有逻辑）。`is_running_any` → False。 |
| 用户 kill -9 venv 进程 | 状态"PID 残留"，提示 stop 后再 start。`start_profile` 看到 `is_running_any` False → 走启动流程，PID 文件被 `start_detached` 覆盖写入新 PID；若旧 GPU 锁残留，`acquire_gpu_lock` 会按 name 覆盖（见 `core/gpu_lock.py`，锁文件以 name 为 key 而非 PID）。 |
| dock 环境切换：同一个 profile 从 docker 改 venv 运行 | 启动 docker 时不写 PID，启动 venv 时写——切换方向（docker→venv）意味着 docker 不写、venv 写，PID 文件从无到有；反向（venv→docker）venv 一次性写 PID，docker 路径不覆盖但 `stop_backend` 的 `stop_docker_instance` 也会清理（防御残留）。 |
| 共享进程（ollama 多 profile 共用 serve） | `is_running_any` 按 `profile.port`（11434）探测，A 启了 serve 之后 B 启动返回 True——"运行中"。`stop_profile(ollama)` 保留 owns-else 跳过 + `unload_model` 语义（已有逻辑）。 |
| `gateway` 多次 `/v1/models` 探测开销 | 每次都对未运行 profile 做 open_local（2s 超时）——与现 `is_model_healthy` 行为一致，无新增开销。 |
| venv 路径下的 PID 文件 vs docker 路径下的污染 | 两种清理机制：docker 路径不写 PID 文件（`write_pid=False`）+ stop 路径主动清理（`stop_docker_instance` 无条件 `pf.unlink`，`stop_instance` 已有同款）。判定路径 `is_running_any` 不做清理（见 §2.1 无副作用约束），以保住 CLI "PID 残留" 状态的可观测性。 |

## 3. 不做的事（YAGNI）

- 不引入服务发现 / 注册中心。端口探测 + PID 兜底在本工具单进程模型下足够。
- 不把 `is_running_any` 做成可插拔的 backend 抽象。
- 不在 docker runtime 写 "container_id.pid" 文件（同端口单实例约束使其钝化；且会让 PID 抽象与真实 PID 混淆，违背原 `is_pid_alive` 承诺）。
- 不改 `gpu_lock` 的 key 设计（仍以 profile.name 为主键）。
- 不更新 tests / 不写 conftest 调整（实现阶段再做）。

## 4. 实施任务清单（供 writing-plans 展开）

1. `core/process.py`：新增 `is_running_any`、`stop_docker_instance`，扩展 `start_detached` 参数（`write_pid: bool = True`）。
2. `engines/base.py`：新增 `stop_backend`、`is_docker_runtime`（默认 False）。
3. `engines/{vllm,tokenspeed,tensorrt_llm}.py`：override `stop_backend` 与 `is_docker_runtime`。
4. `core/all_service.py`：`start_profile` 注入 `write_pid=not adapter.is_docker_runtime()`；`stop_profile` 改用 `adapter.stop_backend()`；`stop_all` / `restart_profile` 的 `is_running` 调用改为 `is_running_any(name, profile)`。
5. `core/gateway.py`：`is_model_available` 收敛为 `is_running_any(model.name, model.adapter.profile)`。
6. `cli.py`：`_instance_state` 与 `state_words` 收敛为 6 状态（"运行中 / 已停止 / 正常 / 无响应 / PID 残留 / 未就绪"）。
7. `models/vllm/qwen3.8-flash-next.yaml` 头注释：更新 L8-17 中关于 PID 文件的描述措辞为"端口健康 + PID 辅助（docker 路径不写 PID 文件）"。
8. tests：`test_process.py` 新增 `is_running_any` 与 `stop_docker_instance` 用例；`test_all_service.py`、`test_gateway.py`、`test_cli_*.py` 中涉及 `is_model_available` / `_instance_state` / 状态枚举的断言更新。

## 5. 验收标准

- `modelctl status <docker-profile>` 在 docker 容器运行时显示"运行中"（不是 "PID 异常"）。
- `modelctl stop <docker-profile>` 后 `docker ps | grep <container>` 无残留；`data/cache/<name>.pid` 不存在。
- 手动 `docker run` 起的容器（不经 modelctl）也能被 `modelctl status` 识别为"运行中"（端口探测命中）。
- `modelctl list` 中所有 docker / venv / 外部 docker 拉起的 profile 状态"运行中"统一。
- `modelctl gateway status` 命中"网关已运行"（现有行为不变，回归通过）。
- 现有 tests 在无适配 PR 之前通过。
- `modelctl start <venv-profile>` 仍写 PID 文件，stop 后清理（与现状一致）。
