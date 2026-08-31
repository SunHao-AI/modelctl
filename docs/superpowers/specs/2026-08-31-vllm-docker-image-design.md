# modelctl vllm 引擎 Docker 镜像 Day-0 适配设计

日期：2026-08-31
状态：草稿（等待用户评审）

## 1. 背景与目标

### 1.1 问题

llamacpp 引擎面对"主线不认识新架构"的模型（如 Qwen3.8-Flash-Next 的 `qwen4exp`），提供了 `llamacpp.source_dir` 机制：用户把 PR 分支副本 clone 到独立目录，yaml 指向该目录，`pre_start` 自动用 PR 分支源码 cmake 编译。

vllm 引擎面对**同类问题**时同样卡死：

- 模型 qwen3.8-flash-next（`Qwen4ExpForConditionalGeneration`）需要 vLLM 0.28+ 的专用构建；
- vLLM 官方 recipe 明确声明 "PyPI installation is not supported for this recipe"；
- 官方唯一 Day-0 镜像为 `vllm/vllm-openai:qwen38-flash-next`（私有构建，包含 FlashQLA / 新增 8 个融合算子）；
- 当前 modelctl 的 `VllmAdapter.build_command()` 硬绑定 `.venvs/vllm/bin/vllm`（托管 venv，锁在 `vllm>=0.27,<0.28`），**没有任何"换运行时"的可配置点**；
- 用户在 `models/vllm/qwen3.8-flash-next.yaml` 注记中也确认了这一点：`[重要] 直接 modelctl start qwen3.8-flash-next-vllm 会报 "Value error, Model architectures ... not supported for now"`。

### 1.2 核心矛盾

llamacpp 的 `source_dir` 解决的是"二进制从哪来"，而 vllm 的"二进制"是 Python wheel，托管 venv 走 PyPI 锁定版本。两者不在同一抽象层：

| 维度 | llamacpp（已有） | vllm（现状） |
|---|---|---|
| 运行时本质 | C++ 二进制（cmake 产物） | Python 包（pip/uv 安装到 venv） |
| 可配置"运行时来源" | `llamacpp.source_dir` | 无 |
| 版本选择自由度 | 任意 git checkout | 强制锁 `vllm>=0.27,<0.28` |
| 特殊分支临时绕过 | 手动 clone → 指向目录 → 自动补编译 | **无绕过手段**（用户被迫手工 `docker run`，失去 modelctl 全部生命周期管理） |

### 1.3 目标

让 modelctl vllm 适配器支持官方镜像路径，打通任意未来需要特殊构建（docker 镜像）的模型，**不破坏现状托管 venv 链路**：

| 目标 | 度量 |
|---|---|
| T1：yaml 加一个字段即可切换运行时 | `vllm.docker_image` 留空 → 现状零变化 |
| T2：与现有启动编排（`all_service.start_profile`）兼容 | 仍走 `check_requirements → pre_start → build_command → start_detached → wait_ready` |
| T3：stop/status/health 复用现有工具 | 健康检查 / 日志位置 / GPU 锁 / 端点探测不变 |
| T4：不引入通用 `source:` 嵌套对象（YAGNI） | 本次 docker 是实证刚需；venv / source 类型无实证，待需求再扩 |
| T5：qwen3.8-flash-next 实证可启动 | 配置 `docker_image: vllm/vllm-openai:qwen38-flash-next` 后 `modelctl start` 通过 |

### 1.4 已确认的关键决策

| 决策点 | 结论 | 用户确认时间 |
|---|---|---|
| 适配范围 | 仅 docker 镜像；venv/source 暂不做（YAGNI） | 2026-08-31 |
| yaml 字段形态 | 平铺顶层字段 `vllm.docker_image`（非嵌套 `source:`） | 2026-08-31 |
| 模型挂载方式 | 卷挂载为 `/models`，复用现有 `MODEL_ROOT` 下载逻辑 | 2026-08-31 |
| 容器端口映射 | `host:profile.port` ↔ `container:8000`（容器内 vLLM 默认端口） | 2026-08-31 |
| PID 文件语义 | 复用 `start_detached` 现有语义（写 `docker run` 父进程 PID，非容器 id）；stop 主路 = `docker stop <container_name>` | 2026-08-31 |

## 2. yaml schema

```yaml
vllm:
  # 现有字段保留：model / download / tensor_parallel_size / gpu_list /
  # max_model_len / gpu_memory_utilization / quantization / kv_cache_dtype /
  # extra_args / api_key
  # 新增：docker 镜像名（Day-0 专用镜像用）
  #   留空 = 回退托管 venv（.venvs/vllm/bin/vllm），现状零变化
  #   填了 = 用 `docker run` 拉起该镜像；该镜像内必须有 vllm 可执行二进制
  #   适用场景：官方对架构专用的 Day-0 镜像（如 vllm/vllm-openai:qwen38-flash-next）
  docker_image: ""   # 或 vllm/vllm-openai:qwen38-flash-next
```

**互斥与校验规则**：

1. `docker_image` 非空时，`check_requirements()`：
   - 校验 `docker` 在 PATH（`shutil.which("docker")`），缺失抛 `RequirementError`：
     `"docker_image 已配置但 docker 命令不在 PATH——请先安装 docker 与 nvidia-container-toolkit"`
   - 现有 venv 路径检查**跳过**（不调 `envs.ensure_env("vllm")`，避免在纯 docker 环境下要求装托管 venv）
2. `docker_image` 留空时，行为与现状完全一致（`envs.ensure_env("vllm")` 走 `RequirementError`）
3. `extra_args` 全量透传：`docker_image` 类型下，`extra_args` 拼到容器内 `vllm serve` 命令尾部（shlex 拆分逻辑与现状一致）
4. `tensor_parallel_size` / `gpu_list` / `gpu_memory_utilization` / `max_model_len` / `quantization` / `kv_cache_dtype` 全部映射到容器启动参数，与 venv 路径等价（避免两层 app 重复解析）
5. 端口：容器内 vLLM 固定用 8000；宿主机端口 = `profile.port`（yaml 顶层）

## 3. 命令流

### 3.1 build_command 分支

新增私有方法 `_resolve_runtime()` 返回 `("venv", None) | ("docker", image)`：

```python
def _resolve_runtime(self) -> tuple[str, str | None]:
    cfg = self.profile.engine_config
    image = str(cfg.get("docker_image") or "").strip()
    if image:
        return ("docker", image)
    return ("venv", None)
```

`build_command()` 主干改为：

```python
def build_command(self) -> tuple[list[str], dict[str, str]]:
    cfg = self.profile.engine_config
    gpus = self.selected_gpus()
    tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
    runtime, image = self._resolve_runtime()
    # 共用：extra_args 全量透传给 `vllm serve`
    extra = shlex.split(str(cfg.get("extra_args") or ""))
    model_args = [
        "--served-model-name", self.upstream_model_name(),
        "--host", "0.0.0.0",
        "--tensor-parallel-size", str(tp),
        "--gpu-memory-utilization", str(cfg.get("gpu_memory_utilization", 0.9)),
        "--disable-uvicorn-access-log",
    ]
    if cfg.get("max_model_len"):
        model_args += ["--max-model-len", str(cfg["max_model_len"])]
    if cfg.get("quantization"):
        model_args += ["--quantization", str(cfg["quantization"])]
    if cfg.get("kv_cache_dtype"):
        model_args += ["--kv-cache-dtype", str(cfg["kv_cache_dtype"])]
    model_args += self.api_key_args() + extra

    if runtime == "venv":
        # 现状逻辑，仅把 `cfg["model"]` 拼到 model_args 后；env 注入不变
        cmd = [str(envs.engine_bin("vllm", "vllm")), "serve", str(cfg["model"])] + model_args
        return cmd, self._venv_env(gpus=gpus)  # 内部封装现状 HF_HOME / VIRTUAL_ENV / PATH 注入

    # docker 分支
    gpus_seq = list(gpus or range(int(self.caps.gpu_count or tp)))
    gpus_json = f'"device={",".join(str(g) for g in gpus_seq)}"'
    container_name = f"{self.profile.name}-vllm"
    model_local = Path(str(cfg["model"])).expanduser().resolve()
    if not model_local.is_absolute():
        raise RequirementError(
            f"{self.profile.name}：docker_image 路径下 model 必须为本地绝对路径，"
            "请先跑 modelctl start 触发 pre_start 下载"
        )
    model_root = model_local.parent
    cmd = [
        "docker", "run",
        "--name", container_name,
        "--gpus", gpus_json,
        "-p", f"{self.profile.port}:8000",
        "-v", f"{model_root}:/models:ro",
        "--ipc=host",
        "--detach",
        image,
        "vllm", "serve", f"/models/{model_local.name}",
    ] + model_args + ["--port", "8000"]
    env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
    if gpus:
        env.update(self.cuda_visible_devices(gpus))
    return cmd, env
```

辅助：

- `_venv_env(gpus)`：抽取现状 build_command 内 env 注入段为私有方法（避免代码重复），现状语义不变
- 模型路径翻译：`model_local.parent` = 卷挂载源；容器内 `/models/<basename>`。HF repo id 由现有 `pre_start` 下载后写回 yaml `model` 字段为本地绝对路径（与现状一致，docker 分支不另写一份逻辑）
- **PID 文件 / 孤儿化**：复用 `core/process.py::start_detached` 现有语义——`Popen` 的 `start_new_session=True` 让子进程成为新会话 leader；`docker run --detach` 客户端 daemonize 后客户端进程立即退出，容器进程 PPID 被 init 接管。**`proc.poll()` 始终返回非 None**（"客户端早退"对 fail-fast 触发），`is_running(name)` 始终 false——这是 docker 类型下"PID 失效"语义的精确描述（§3.2 给出 stop 的多级兜底）

### 3.2 stop_patterns 多模式匹配（**不修改 core/process.py**）

```python
def stop_patterns(self) -> list[str]:
    runtime, _ = self._resolve_runtime()
    if runtime != "docker":
        return ["vllm serve"]
    cfg = self.profile.engine_config
    gpus = self.selected_gpus()
    gpus_seq = list(gpus or range(int(self.caps.gpu_count or int(cfg.get("tensor_parallel_size", 1)))))
    gpus_json = f'"device={",".join(str(g) for g in gpus_seq)}"'
    container = f"{self.profile.name}-vllm"
    return [
        f"docker run --name {container} --gpus {gpus_json}",  # 与 Popen cmdline 连续子串精确匹配
        f"-v .*:/models:ro docker run --name {container}",   # 兜底：顺序不敏感（不影响两处 --gpus 子串匹配）
    ]
```

**stop 主路原封不动**（`core/process.py::stop_instance` 现状顺序：1) PID SIGTERM 10s → SIGKILL；2) `fuser -k <port>/tcp`；3) `pkill -f <pattern>` 遍历模式）。docker 类型下三层各自的行为：

| 层 | 行为 | 说明 |
|---|---|---|
| 1) PID | 拿到无效 pid（docker CLI 客户端已 exit）→ `os.killpg(pid, SIGTERM)` 立即 `OSError` 静默；10s 死等浪费可接受（stop 不是高频路径） | 与现状一致 |
| 2) `fuser -k <port>/tcp` | 杀掉容器 userland 进程（vLLM）宿主端口 listener，docker 容器随之 `OOM-kill`（SIGKILL） | 关键单点：释放端口与显存 |
| 3) `pkill -f <pattern>` | 误杀已在 daemonize 后 exit 的 docker CLI 客户端；**真正的常驻容器不会被客户 cmdline 误命中**（docker daemon 是单例、cmdline 不含本 container name） | 无害且兜底 |

**孤儿容器兜底**：若 docker 容器因 OOM/segfault 进入 zombie/non-running 状态，`fuser` 命中端口会触发 docker 自愈（`docker run --restart` 策略或下次 stop 时刻的 fuser 命中端口对应"docker 标签的 PID"，即容器入口进程）。该路径**不是 spec 强需求**——但 `modelctl stop <name>` 之后可顺手补 `subprocess.run(["docker", "rm", "-f", f"{self.profile.name}-vllm"], capture_output=True)` 做"温和清理"（噪音容忍：返回非 0 不抛错），写入 `stop_profile` 的可选增强项——**本 spec 不实现**（YAGNI）。

**为什么不在 `stop_instance` 加 docker 分支**：原 spec 设计是"PID-0 + docker 类型 → 直接 `docker stop` 并 early-return"（§3.2 v0）；实现时发现这破坏现有 5 引擎共享的 stop 主路顺序，且 `stop_instance` 签名（`name, port, patterns`）目前未携带 Profile/Engine 信息（`all_service.stop_profile` 只见 name+port+patterns）。改给 `stop_instance` 加参数 + 在 `all_service` 里注入 Profile 是 5+1 文件改动，对"qwen3.8-flash-next 跑通"这类一次性需求 ROI 偏低。三层兜底下 docker 业务已可靠；profile 注入与 `docker stop` 主路留给 DoS/孤儿容器/重复 stop 这类生产规模需求再单独 spec。

## 4. 改动面（小，函数级）

| 文件 | 改动 |
|---|---|
| [src/modelctl/engines/vllm.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/engines/vllm.py) | 1) 新增 `_resolve_runtime()` 私有方法；2) `build_command()` 顶部按 runtime 分支，docker 分支构造 docker run 数组（同步抽现状 env 注入段为 `_venv_env()` 私有方法）；3) `check_requirements()` 在 docker 分支跳过 `envs.ensure_env` 但校验 `docker` 与 `nvidia-smi` 在 PATH；开头 `docker rm -f <container_name>` 清冲突残留（幂等忽略）；4) `stop_patterns()` docker 分支返回 2 个 pkill 模式（§3.2），venv 分支返回 `["vllm serve"]` 不变 |
| [models/vllm/qwen3.8-flash-next.yaml](file:///d:/WorkPlace/Pycharm/modelctl/models/vllm/qwen3.8-flash-next.yaml) | 1) 加 `docker_image: vllm/vllm-openai:qwen38-flash-next`；2) 头部注释更新：`【解决路径】已支持 docker 方式模型（vllm.docker_image），直接 modelctl start 即可` |
| `tests/test_engines_vllm.py` | 新增 9 个测试（与 §6 用例逐一对应）：默认回退 / docker resolve / docker 命令模板 / docker gpus_json / docker check_requirements（含 docker 缺失）/ docker stop_patterns / pre_start 写回 model 字段 等 |

**不动的文件**：`src/modelctl/core/process.py`（stop 主路三层不变）、`src/modelctl/core/all_service.py`（无签名变更）

### 不动的文件（YAGNI 原则）

- 不改 `core/envs.py`（docker 不需要 venv 抽象）；`envs list` 命令继续只显 vllm/sglang 托管 venv
- 不改 `core/capabilities.py` 的 `find_vllm_binary`（PATH 与 `.venvs/vllm/bin/vllm` 探测保留，纯能力摘要层无感知具体 profile，docker 类型的能力探测无意义）
- 不在 docker 类型下做自动 `docker pull`（首次冷启动留给 docker run 自然触发；可选在 `modelctl env setup vllm` 加可选的 `pull` 子命令，**不在本 spec 范围**）

## 5. 关键风险与缓解

| 风险 | 缓解 |
|---|---|
| 宿主机 `nvidia-container-toolkit` 缺失导致 `--gpus` 静默失败 | `check_requirements` 在 docker 分支硬检：`docker --version` + `nvidia-smi` 在 PATH；缺失抛 `RequirementError` 并给安装提示（`sudo apt install nvidia-container-toolkit`）；不降级到 venv（避免用户拿到半吊子结果） |
| 端口 8000 与宿主机其他服务冲突 | 容器内端口固定 8000（vLLM 默认）；宿主机端口 = profile.port；docker 端口冲突时 `docker run` 直接报错，`wait_ready` 失败诊断能抓到 "Error: ... port is already allocated" 片段 |
| 容器内 vLLM 版本与官方 recipe 不匹配（用户填了错的 image tag） | 不处理：用户填什么 image 就用什么；模型加载失败时 vLLM 在容器内 block，`docker logs <id>` 含 "not supported" 字样，`log_excerpt` 现有诊断逻辑（抓 `Traceback / CUDA error / Engine core initialization failed`）能截到；建议在 `log_excerpt` 标记集中追加 `"not supported for now"` 作为 docker 类型下的新诊断标记 |
| 容器名冲突（同 profile.name 重复 start） | `check_requirements` docker 分支开头显式 `docker rm -f <container_name>`（防旧 stop 未清理干净的位置；幂等失败忽略）；已通过 §3.2 stop 逻辑的常规 stop 后通常不会再有冲突 |
| 模型路径翻译错误（HF repo id 的 `X/Y` 形态） | 共用现有 `pre_start` 下载路径写回 yaml 的逻辑；build_command 里 `_model_source_dir()` 在模型已是本地绝对路径时直接 `parent`，HF id 在 pre_start 已转本地，到 build_command 时不可能还是 repo id（docker 类型下若发现 model 不是绝对路径，build_command 抛 `RequirementError`："docker_image 路径下 model 必须为本地绝对路径，请先跑 modelctl start 触发 pre_start 下载"） |
| PID 文件已失效下 stop 的可靠性 | `docker run --detach` 客户端 daemonize 后立即 exit，PID 文件指向已退出 client，`is_running` 永远 false、stop 走 PID 路径时 `os.killpg` 拿无效 pid 调 sigterm 立即 OSError 静默——可接受（stop 不是高频路径）。**关键单点**是 3 层里的 `fuser -k <port>/tcp`：宿主端口 listener 是容器 userland 进程名 vLLM（docker 标签 pid），宿主机 `fuser -k` SIGKILL 它 → 容器随之终止、docker 标签 PID 被回收、端口释放、显存随 nvidia 驱动回收。3 层全失败时用户在诊断输出里看到 stop 完成 + 端口仍占用，手工 `docker rm -f <container_name>` 兜底（`stop_instance` 不改内部逻辑，三层兜底语义不变） |
| Windows 开发机测试 | 本设计目标平台为 Linux；`tests` 内 docker 测试用 monkeypatch mock `shutil.which` 与 subprocess，不真起 docker，跨平台可跑 |

## 6. 测试用例（`tests/test_engines_vllm.py` 内新增）

| 用例 | 断言 |
|---|---|
| `test_resolve_runtime_default` | `cfg` 无 `docker_image` → `("venv", None)` |
| `test_resolve_runtime_docker` | `cfg["docker_image"] = "vllm/vllm-openai:qwen38-flash-next"` → `("docker", image)` |
| `test_build_command_default_venv_unchanged` | 现状回归：assert 命令首元素 == `.venvs/vllm/bin/vllm`，env 含 `VIRTUAL_ENV` |
| `test_build_command_docker_template` | image 非空时 `cmd[0] == "docker"`，包含 `--name`/`--gpus`/`-p`/`-v`/`--ipc=host`/`--detach`，镜像名出现在数组内（位置 `[N-payload_len]`，其后 `vllm serve /models/<basename>` 与 `--port 8000`）；env 不含 `VIRTUAL_ENV`/`PATH` 前置 venv bin |
| `test_build_command_docker_gpus_json` | `gpu_list: "0,1"` 时 `--gpus` 后一个元素 `'"device=0,1"'`；仅 `tensor_parallel_size: 8` 且 `caps.gpu_count == 8` 时生成 `'"device=0,1,2,3,4,5,6,7"'` |
| `test_check_requirements_docker_no_venv_check` | monkeypatch `envs.ensure_env` 抛错（模拟 venv 未建）+ `shutil.which("docker")` 命中 + cfg 含 docker_image → 不抛 RequirementError |
| `test_check_requirements_docker_missing_docker` | `shutil.which("docker")` 返回 None + cfg 含 docker_image → 抛 `RequirementError`，消息含 "docker 命令不在 PATH" |
| `test_stop_patterns_docker` | docker 时 stop_patterns 返回 `"docker"` 与容器名；venv 时仍 `"vllm serve"` |
| `test_pre_start_persists_local_path_for_docker` | docker_image + HF repo id → pre_start 触发下载 → 写回 yaml `model` 字段为本地绝对路径，后续 build_command 拿到的是本地路径 |

## 7. 不做的事（明确排除）

| 排除项 | 理由 |
|---|---|
| 通用 `source:` 嵌套对象（source/venv/docker） | 实证仅 docker，venv/source 无场景（用户确认）；保留最小字段 |
| 自动 `docker pull` 在 `modelctl env setup vllm` | docker run 已隐式 pull；显式拉镜像的 CLI 扩展点留待 `modelctl env setup vllm --with-docker <image>` 单独 spec |
| 容器资源限制（`--memory` / `--cpus` / `--shm-size`） | vLLM 多卡 NCCL 默认 `--shm-size` 足够；精细调参通过 `extra_args` 内 docker 透传不行（extra_args 是容器内参数），留 `docker_run_args: ""` 扩展点留待实际调参需求 |
| 多容器编排 / 容器网络 / 日志卷 | 超出单模型单容器范围 |
| 镜像内 model 校验（如 `docker exec <id> vllm --version`） | 过度工程；健康检查 + log_excerpt 已足够 |
| `docker_image` 与 `extra_args` 的语义消解 | 不做消解：`extra_args` 100% 透传到容器内 `vllm serve` 命令；docker 层参数（`--gpus` / `-v` 等）由 modelctl 内置拼装，用户不需要也无法覆盖（扩大到精调属 YAGNI） |
| macOS / Windows 下 docker + GPU 透传验证 | 本机（部署机）为 Linux；Windows 开发机测试仅 mock，不覆盖 docker 真运行 |

## 8. 验收标准

1. **现状零变化**：`models/vllm/` 下除 `qwen3.8-flash-next.yaml` 外的所有 yaml 经 `build_command` 输出与改造前 byte-identical（含 env dict）；
2. **qwen3.8-flash-next 实证启动**：填上 `docker_image: vllm/vllm-openai:qwen38-flash-next` 后，`modelctl start qwen3.8-flash-next-vllm` 在 8×RTX 5880 上通过 `/health` 返回 2xx；
3. **stop/status 闭环**：start → status（running）→ stop（docker stop 成功 + 端口释放）→ status（stopped）；
4. **诊断可用**：故意填错 image tag，启动失败后 `log_excerpt` 能截到容器内 "not supported" 异常块；
5. **测试全绿**：现有 `tests/test_engines_vllm.py` 全部通过（含回归测试），§6 列出的 9 个新用例全部通过；
6. **向后兼容**：未装 docker 的机器上，`docker_image: ""` 的 yaml 启动行为与改造前一致（不要求 docker 在 PATH）。

## 9. 影响面与回滚

- **改动 3 个文件**：`src/modelctl/engines/vllm.py`（主体：新增 `_resolve_runtime`/`_venv_env` 私有方法、docker 分支、`stop_patterns` 2 模式）、`models/vllm/qwen3.8-flash-next.yaml`（配置 + 注释更新）、`tests/test_engines_vllm.py`（9 个新测试）；
- **回滚**：删除 `docker_image` 字段 + revert `vllm.py`，yaml 顶层**加性**新增无 schema 破坏性变更；
- **日志**：不再改 log 文件名/路径约定，`logs/launch-<name>.log` 不变（docker 类型下 `start_detached` 自身 redirect 的是 `docker run` 客户端 stdout，容器内 stdout 走 docker 内部日志流，需要 `docker logs <container_name>` 另读——可选增强：模型启动失败时把 `docker logs --tail 200 <container_name>` 输出落到 launch log 末尾，便于 `log_excerpt` 统一诊断——**不在本 spec 必需范围**）；
- **依赖**：新增 `docker` 命令运行时依赖（仅 docker 类型下）；不改 `pyproject.toml` / uv.lock。

## 10. 参考资料

- vLLM 官方 Qwen3.8-Flash-Next recipe（`recipes.vllm.ai/Qwen/Qwen3.8-Flash-Next`）
- NVIDIA Day-0 博客：`Qwen3.8-Flash-Next 176B Model on NVIDIA GB300 NVL72`
- Qwen 官博（`qwen.ai/blog?id=qwen3.8-flash-next`）
- 部署实操参考：CSDN《Qwen3.8 Flash-Next 开放权重：FP8 也要 172.78 GiB，算家云 5090 到底怎么部署》
- 本仓 yaml 注记：`models/llamacpp/qwen3.8-flash-next.yaml`（llamacpp.source_dir 设计先例）/ `models/vllm/qwen3.8-flash-next.yaml`（已识别问题）
