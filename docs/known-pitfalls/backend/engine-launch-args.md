# 引擎启动参数与健康检查

> 原始单文件已并入本文件归档，保留溯源信息。

## vLLM venv 分支漏传 --port，健康检查永远 Connection refused

**日期**：2026-09-03
**症状**：

```
modelctl start qwen3.8-vllm
17:46:09 | INFO    | 已启动 qwen3.8-vllm（PID 314722），等待健康检查（超时 600s）...
17:46:21 | WARNING | 引擎进程已提前退出，中止健康检查等待
17:46:21 | WARNING | 健康检查未通过（http://127.0.0.1:8101/health），最后错误：[Errno 111] Connection refused
...
OSError: [Errno 98] Address already in use
```

profile 配了 `port: 8101`，日志里却是端口冲突后秒退。

**根因**：`engines/vllm.py` 的 `build_command()` 中，venv 分支的 `model_args` 只带了
`--host 0.0.0.0`，**没有 `--port`**。于是：

1. `vllm serve` 回退默认端口 **8000**，与 profile 的 `8101` 完全脱节；
2. 8000 已被其他实例/容器占用 → `Address already in use`，进程秒退；
3. `wait_health()` 探测的是 `profile.port`（8101），必然 `Connection refused`。
   —— 即便端口不冲突，健康检查也永远打不到实际监听的 8000，只会白等满 600s 超时。

docker 分支有 `-p {port}:8000` + `--port 8000`，其余引擎（aphrodite / tokenspeed /
tensorrt_llm / lmdeploy / llamacpp）均显式传 `profile.port`，**唯独 vLLM venv 分支遗漏**。

**解决方案**：venv 分支显式追加 `--port`，且必须放在 `extra_args` **之前**——
argparse 对重复参数取最后一次出现，放末尾会让 `extra_args` 里的 `--port` 失效。

```python
# api_key / extra_args 恒定追加到末尾，保证 yaml 可覆盖内置默认
tail = self.api_key_args() + extra

if runtime == "venv":
    cmd = [
        str(envs.engine_bin("vllm", "vllm")), "serve", str(cfg["model"]),
        *model_args, "--port", str(self.profile.port), *tail,
    ]
```

**教训**：

- 端口在 `profile.py` 只做了「必填 + 1-65535」校验，全项目**没有任何主动的端口占用预检**
  （无 socket bind 探测）。端口是否真正生效，完全依赖各引擎 `build_command` 自觉传参，
  新增引擎分支时极易漏。
- 端口不匹配会以两种形态出现，都指向同一根因：占用者存在 → 秒退 + `Address already in use`；
  占用者不存在 → 进程活着但健康检查超时。**后者更隐蔽**，容易被误判为「模型加载慢」。
- 新增/修改引擎适配器时，用一条断言守住：`--port` 必须存在且等于 `profile.port`。

## `vllm --version` 探测 5s 超时，稳定误报「无法探测 vLLM 版本」

**日期**：2026-09-03
**症状**：

```
WARNING | 无法探测 vLLM 版本（将放行；若启动报错请人工确认 ≥ 0.13.0）
```

明明 `vllm serve` 随后能正常打印 `version 0.27.1`，探测却失败。

**根因**：`core/envs.py` 的 `vllm_version()` 用
`subprocess.run([vllm_bin, "--version"], timeout=5)` 探测。`vllm --version` 入口要
`import vllm` 并连带加载 torch / CUDA 扩展等重型依赖，冷启动（page cache 未命中）
轻松超过 5s → `TimeoutExpired` → 返回 `None` → 门控降级为「放行」。
即**版本门控实际长期失效**，且每次冷启动都告警。

**解决方案**：首选纯磁盘读 dist-info，彻底绕开 import 成本（`status()` 早已有此能力）：

```python
v = _parse_version(_read_installed_packages(VENV_ROOT / "vllm").get("vllm") or "")
if v is not None:
    return v
# 仅在元数据缺失/异常时退回 CLI，超时预算放宽到 60s
return _parse_version(_run_probe([str(engine_bin("vllm", "vllm")), "--version"], 60))
```

**教训 1 — 别用「执行 CLI」的方式问「装了什么版本」**：
Python 包的 `--version` 常常触发完整 import 链，耗时不可控。包版本是**安装元数据**，
直接读 `site-packages/<pkg>-<ver>.dist-info/METADATA` 即可，零子进程、零超时、毫秒级。
同理 `importlib.metadata.version()` 也优于跑 CLI（但它仍需起解释子进程，磁盘读更彻底）。

**教训 2 — 版本正则绝不能碰 stderr**：
中途试过 `version('vllm')` 走 `importlib.metadata`，未安装时抛 `PackageNotFoundError`，
解释器写到 **stderr** 的 traceback 自带 `Python 3.13.11`，正则 `(\d+)\.(\d+)\.(\d+)`
直接把**解释器版本当成 vLLM 版本**返回 `(3, 13, 11)`，比 `None` 危险得多——
它会让「版本过低」的门控**静默误放行**。因此 `_run_probe()` 必须
「只取 stdout 且要求 `returncode == 0`」，任何失败一律归约空串。

**教训 3 — 探测函数别在两个后端上重复付费**：
`lru_cache` 只缓存一次结果，但函数体内若无条件跑完「快路径 + 慢路径」，
慢路径的成本照样每次都付。快路径命中必须**立即 return 短路**。

## 端口占用无预检：引擎秒退后靠翻日志反推 EADDRINUSE

**日期**：2026-09-03
**症状**：`modelctl start` 后进程 12s 退出，日志里一句 `Address already in use`；
更隐蔽的形态是端口没冲突但配错，健康检查白等满 600s 超时。

**根因**：全项目启动前**没有任何端口可用性探测**（此前 `import socket` 零命中）。
占用只能事后从日志标记 `_EXCERPT_MARKERS` 里的 `"Address already in use"` 反推。

**解决方案**：`core/process.py` 新增 `port_in_use()` / `describe_port_listener()`，
`all_service.start_profile()` 在 `is_running_any` 之后、`check_requirements` 之前拦截：

```python
def port_in_use(port, host="127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0   # 0 ⇒ 已有监听者
```

抛 `RequirementError` 复用 cli 的 exit 2 语义，并尽力点名占用者
（POSIX `ss -ltnp` / Windows `netstat -ano`，拿不到就降级空串）。

**必须豁免 ollama 共享后端**：`models/ollama/*.yaml` 三个 profile 都配 `port: 11434`，
共享同一 serve 是**设计语义**（`stop_profile` 有同族特判：只 unload 模型不杀进程）。
第二个 ollama profile 启动时 `is_running_any` 探 `/health` 得 404 不会 skip，
靠「新 serve bind 失败但健康检查命中已有 serve」就绪 —— 端口被占是**正常状态**，
不排除会直接打断共享用法：

```python
if profile.engine != "ollama" and port_in_use(profile.port):
    raise RequirementError(...)
```

**教训**：加「启动前校验」类拦截时，先枚举项目里的**共享后端 / 复用端口**引擎
（ollama 这类 sidecar 常驻服务最容易踩），它们的「端口已被占用」不是错误而是前提。

## 缺 cmake 报错只说"请安装"，不给出可直接执行的安装命令

**日期**：2026-09-04
**症状**：`modelctl start qwen3.8-llamacpp` 在 clone 完 llama.cpp 源码后终止：

```
13:36:34 | ERROR   | 缺少 cmake。请安装后再运行脚本。
```

用户（尤其远程 GPU 机器）还得自己查发行版对应的安装命令，报错不闭环。

**根因**：`engines/llamacpp.py` 的 `require()` 只检测 `shutil.which(name)`，
错误消息是固定文案，未携带任何安装指引。

**解决方案**：新增 `install_hint(name)`，按系统实际存在的包管理器
（apt-get / dnf / yum / zypper / pacman / apk，探测顺序与
`core/webui/frontend.py::_package_manager` 一致）拼出安装命令，
非 root 且有 sudo 时自动补 `sudo` 前缀；`require()` 报错消息追加该命令：

```python
raise RequirementError(f"缺少 {name}。请安装后重试：{install_hint(name)}")
# Ubuntu root 下 → 缺少 cmake。请安装后重试：apt-get install -y cmake
```

**教训**：环境缺失类报错（`RequirementError`）应尽量**闭环**——直接给出当前机器
可复制执行的修复命令，而不是让用户带着错误信息去查文档；项目里已有同类先例
（`frontend.py::manual_hint`、`docker_setup`），新代码优先复用同一套探测思路。
