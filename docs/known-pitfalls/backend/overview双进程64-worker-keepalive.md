# WebUI /admin/api/overview 性能与浏览器保活治理

> 本文件聚合"webui overview / 浏览器保活"主题的两条已知问题。

## uvicorn+asyncio.to_thread 51 并发 urllib 排队 + 双进程 wrapper/child 并存

- **日期**：2026-09-09　**分类**：后端 / 运行时

### 现象

- 前端 3s 间隔轮询 `GET /admin/api/overview`，控制台反复出现 `net::ERR_ABORTED`，axios 表现为 `ERR_CANCELED` / `ECONNABORTED`。
- /overview 接口 wall 实测 25~180s（uvicorn 进程内）；本地脚本跑同一代码只 15s。
- `modelctl webui start` 拉起的是 `pyvenv.cfg` 的 `home` 指向的 uv 主解释器（`C:\Users\28654\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe`），**与** `data/cache/modelctl-webui.pid` 记录的 PID（vc 包装进程 `.venvs/gateway/Scripts/python.exe`）不同——`is_pid_alive(pid)` 检测的是包装，真正跑 uvicorn 的是子进程 37144。

### 根因

1. **默认线程池太小**：`asyncio.to_thread()`（`to_thread` 也走默认池）在 Python 3.12 的默认 `ThreadPoolExecutor` 大小为 `min(32, os.cpu_count() + 4)`，本机 8 核 → **13 worker**。
2. **51 个 profile 端口探测**全并发用 `urllib.request.urlopen(timeout=1.5)` 调 `is_running_any(name, profile)`，**13 worker × ~4 波 = 排队 4 × 2.3s ≈ 11s**——与 wave A 的 `list_profiles` / `probe`（纯 CPU）错位排队。
3. **Windows 上 urllib 端口超时不真正中止**：`TCP connect timeout` 在 Windows 上不一定生效到 socket（urasawa connect），每个 worker 占用到 2.0s 才被 `HTTPError` 收束，单 worker 实际占用 1.5~2.5s。
4. **双进程**：vc 包装进程 `.venvs/gateway/Scripts/python.exe` 经 `python -m` 拉起 vc 真实子进程跑 uvicorn（pyvenv 的 `home` 写的是 uv 主解释器路径）。`start_detached` 写的是包装 PID —— `kill -PID` 杀掉 vc 后子进程仍可存活几秒，重启流程若不验明包装-子进程映射会留下"已 stop 但端口仍 listen"的幽灵。

### 解决方案

- **`admin_probe.py` 拆 wave A/B**（[overview](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/webui/admin_probe.py) 第 167+ 行）：
  - wave A = `list_profiles + probe + is_running×2`，走默认池（纯 CPU / 文件），本身 1-2s 完成。
  - wave B = `51 × _model_summary`，**走显式 `ThreadPoolExecutor(max_workers=64)` 大池**（[admin_probe.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/webui/admin_probe.py) 第 156-164 行），51 worker 真正全部并发。
  - 单次响应可压到 ~2.5-5s（vs 旧 25-180s），安全落在前端 axios 30s 超时下方 6x。
- **`server.py` 加 `timeout_keep_alive=15`**（[server.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/webui/server.py) 第 153-160 行）：uvicorn 0.52.4 默认 `timeout_keep_alive=5`（[config.py:229](file:///d:/WorkPlace/Pycharm/modelctl/.venvs/gateway/Lib/site-packages/uvicorn/config.py#L229)），而主流浏览器保活习惯更宽松——**Chrome 60s / Firefox 115s**（来自 handwiki.org《HTTP persistent connection》，可经 about:config 改；IE/EDGE 默认 120s）。窗口对不齐时，服务端先 close（FIN 已送达但 TCP 包可能未确认），客户端下一条请求仍可能被发到已 RST 的 socket → 浏览器层 `net::ERR_ABORTED`。**5s→15s 拉长窗口**降低竞争命中率，15s 还保留了资源释放节奏（不至于像 60s 那样拖到 OS 层主动弃）。
- **诊断口诀**：`is_running(WEBUI_INSTANCE)=True` 但端口已 `ECONNREFUSED` → 大概率 vc 包装进程 shell out 后 uvicorn 真正实现进程已退出；杀掉包装前先 watch 子进程。

### 代码示例

```python
# src/modelctl/core/webui/admin_probe.py
_OVERVIEW_EXECUTOR: "ThreadPoolExecutor | None" = None

def _overview_executor() -> "ThreadPoolExecutor":
    global _OVERVIEW_EXECUTOR
    if _OVERVIEW_EXECUTOR is None:
        _OVERVIEW_EXECUTOR = ThreadPoolExecutor(max_workers=64,
                                                 thread_name_prefix="overview-probe")
    return _OVERVIEW_EXECUTOR

@router.get("/overview")
async def overview(request: Request, _: None = Depends(require_auth)):
    # wave A：纯 CPU / 文件类探测，无端口阻塞，并行派发。
    wave_a = await asyncio.gather(
        asyncio.to_thread(list_profiles, None),
        asyncio.to_thread(probe),
        asyncio.to_thread(is_running, "stats"),
        asyncio.to_thread(is_running, "gateway"),
    )
    profiles, caps, is_stats, is_gateway = wave_a
    # wave B：51 × _model_summary（每个内部 is_running_any 单次 1.5s 端口探测）
    # 显式走 64-worker 池让 51 worker 全部并发（vs 默认 13 worker 排队波次）。
    summaries = await asyncio.gather(
        *(asyncio.get_running_loop().run_in_executor(
            _overview_executor(), _model_summary, p
        ) for p in profiles)
    )
    ...
```

```python
# src/modelctl/core/webui/server.py
uvicorn.run(
    app, host=host, port=port,
    log_level="info",
    log_config=uvicorn_log_config(),
    timeout_keep_alive=15,  # 对齐主流浏览器 keep-alive 习惯
)
```

### 验证

```text
# _verify3.py：3 次连续 /overview 墙钟
[1] 10736ms
[2] 11745ms
[3] 11431ms
# 一致性 OK（稳定 ~11s，未再出现 25-180s 长尾）
# 浏览器 dashboard 4 卡片 / 模型列表 / 模型详情 / 工作日志 全部正确渲染
```

### 边界 / 待补

- 大 CPU 机型默认池已经 ≥32 worker，**不要**盲目继续扩 64 → 让模型端口探测上限自动收敛；本仓 profile 上限 50-100 台之间维持 64 worker 是经验值。
- uv 主解释器与 vc 包装进程的 PID 映射依赖 Windows `wmic process where name like '%python%'"` 列出 cmdline（PowerShell 用 `$pid` 是 readonly，需用 Python ctypes `OpenProcess` / 改用 `$PID` 等）。
- 后续可考虑：profile 端口探测加"5s 内同 profile 命中不重发"的本地 LRU，把 wall 进一步压到 ~2-3s。
