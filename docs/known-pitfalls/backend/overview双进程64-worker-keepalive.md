# WebUI /admin/api/overview 性能与浏览器保活治理

> 本文件聚合"同步 /health 探测拖慢 asyncio 服务 / 浏览器保活"主题的已知问题。
> 原始单文件已并入本文件归档。

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
  - wave B = `51 × _model_summary`，**走显式 `ThreadPoolExecutor(max_workers=64)` 大池**，51 worker 真正全部并发。
  - 单次响应可压到 ~2.5-5s（vs 旧 25-180s），安全落在前端 axios 30s 超时下方 6x。
  - **2026-09-10 修正**：大池由 `admin_probe` 独有收敛为 [admin_models.probe_executor()](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/webui/admin_models.py#L280-L288) **单例**（`thread_name_prefix="webui-probe"`），`/admin/api/overview` 与 `/admin/api/models` 共用。此前只有 overview 显式传池，`list_models` 走 `asyncio.to_thread` 默认池（本机 cpu=8 → `min(32, 12)=12` worker）→ 冷路径被排成 4 波 **10~18s**；改后 models 冷路径降至 ~5.7-7.5s。
- **`server.py` 加 `timeout_keep_alive=15`**（[server.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/webui/server.py) 第 153-160 行）：uvicorn 0.52.4 默认 `timeout_keep_alive=5`（[config.py:229](file:///d:/WorkPlace/Pycharm/modelctl/.venvs/gateway/Lib/site-packages/uvicorn/config.py#L229)），而主流浏览器保活习惯更宽松——**Chrome 60s / Firefox 115s**（来自 handwiki.org《HTTP persistent connection》，可经 about:config 改；IE/EDGE 默认 120s）。窗口对不齐时，服务端先 close（FIN 已送达但 TCP 包可能未确认），客户端下一条请求仍可能被发到已 RST 的 socket → 浏览器层 `net::ERR_ABORTED`。**5s→15s 拉长窗口**降低竞争命中率，15s 还保留了资源释放节奏（不至于像 60s 那样拖到 OS 层主动弃）。
- **诊断口诀**：`is_running(WEBUI_INSTANCE)=True` 但端口已 `ECONNREFUSED` → 大概率 vc 包装进程 shell out 后 uvicorn 真正实现进程已退出；杀掉包装前先 watch 子进程。

### 代码示例

```python
# src/modelctl/core/webui/admin_models.py —— 全 webui 进程共享的探测大池（单例）
_PROBE_EXECUTOR: "ThreadPoolExecutor | None" = None

def probe_executor() -> "ThreadPoolExecutor":
    global _PROBE_EXECUTOR
    if _PROBE_EXECUTOR is None:
        _PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=64,
                                             thread_name_prefix="webui-probe")
    return _PROBE_EXECUTOR
```

```python
# src/modelctl/core/webui/admin_probe.py
from modelctl.core.webui.admin_models import build_summaries, probe_executor

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
    # wave B：运行态判定走共享 TTL 缓存；未命中项显式走 64-worker 大池，
    # 让 51 个端口探测真正全部并发（vs 默认 12 worker 排队波次）。
    summaries = await build_summaries(request, profiles, executor=probe_executor())
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
- **任何列表端点都必须显式传 `probe_executor()`**：`build_summaries(..., executor=None)` 会退回 `asyncio.to_thread` 默认池，在没有报错的情况下把冷路径从 ~6s 悄悄拉回 10~18s。判断方法：`getattr(build_summaries.__doc__)` 或直接搜调用点是否带 `executor=`。
- uv 主解释器与 vc 包装进程的 PID 映射依赖 Windows `wmic process where name like '%python%'"` 列出 cmdline（PowerShell 用 `$pid` 是 readonly，需用 Python ctypes `OpenProcess` / 改用 `$PID` 等）。
- 后续可考虑：profile 端口探测加"5s 内同 profile 命中不重发"的本地 LRU，把 wall 进一步压到 ~2-3s。

## 网关家族路由每请求同步探测 N 个成员 /health，阻塞事件循环

- **日期**：2026-09-10　**分类**：后端 / 运行时

### 现象

- 数据面 `/v1/chat/completions` 并发上去后，整体延迟随并发**非线性恶化**；nginx `limit_conn`、vLLM `--max-num-seqs`、账号 `concurrency_limit` 全都没触顶，吞吐却上不去。
- 高负载时不仅新请求慢，**连不需要路由的请求也一起慢**——这是"进程级停摆"而非"配额耗尽"的特征指纹。

### 根因

1. **`_resolve_group` 是同步 urllib，却跑在 async handler 里**：`resolve_model` → `_resolve_group` → `is_model_available` → `process.is_running_any` → `urllib.request.urlopen(timeout=1.5)`。执行期间 uvicorn **单事件循环**上所有请求停摆。
2. **代价按家族成员数线性放大**：`qwen3.8` 组有 **11 个成员**（vllm×2 / sglang / unsloth / ollama / llamacpp×3 / aphrodite / lmdeploy / tensorrt_llm），按 `ENGINE_PRIORITY` 顺序探测直到命中第一个可用成员。
3. **`.env` 的 `GATEWAY_DEFAULT_MODEL=qwen3.8` 正是家族名** ⇒ 不传 model 的请求也走这条路径；显式传 `model=qwen3.8` 同样命中。
4. **正反馈雪崩**：引擎高负载 → 自身 `/health` 变慢 → 网关事件循环被拖住 → 全部请求排队 → 引擎更忙。
5. 精确名命中 `registry`（`body_model in registry`）**不受影响**——这解释了"只有部分用户卡"的迷惑现象。

> 纠正一个常见误判：Linux 上端口**未监听**时是 connect refused（< 20ms，见 `process.py` 注释），不吃 1.5s。只有"端口 listen 但 `/health` 不响应"（引擎满负载 decode）才吃满超时——而那恰是高并发常态。
>
> **Windows 开发机例外（2026-09-10 实测）**：本机连未监听的 `127.0.0.1:<port>` 需 **~2046ms** 才回 `WinError 10061`（raw socket / urllib 均如此），比 `is_running_any` 的 `timeout=1.5` 还长 ⇒ **每个停用模型必然耗满整个超时**，这是本机冷路径地板价（~51 × 1.5s / 并发度）的真正来源，与线程池容量无关。生产是 Linux，不适用此例外。

### 解决方案

**不要**改成 `asyncio.to_thread`（只是把停摆换成线程池争抢，且默认池仅 `min(32, cpu+4)`），也不要盲目加 worker 数（`LimitGuard` 的并发/RPM/TPM 计数器是**进程内存**，多 worker 会把 `concurrency_limit` 放大 N 倍，见 `accounts/limits.py` 顶部口径）。正确解法是**短 TTL 缓存把每请求 N 次探测摊薄成每 TTL 秒 N 次**：

- `GroupRouteCache`（[gateway.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/gateway.py#L626-L693)）：**两层**缓存，键空间互不相通（route 用 group 名、avail 用模型 name）——
  - `route`：家族解析结果，数据面每请求走；命中即零 HTTP，且保留"命中第一个可用成员就短路"的语义。
  - `avail`：单成员可用性判定（`cached` / `record` / `invalidate_model`），`/v1/models` 走这层。
  - 单事件循环内使用，**不加锁**。
- **负缓存同 TTL**：全家族宕机时探测最贵（11 × 1.5s），必须一起缓存。`stop` 后运维必然看到失败并重试，1~2s 陈旧窗口可接受。
- **502 主动失效**：`httpx.HTTPError` 分支**必须成对调用** `invalidate(group)` + `invalidate_model(name)`。只清路由、留着 `available=True`，`/v1/models` 会在 TTL 内继续把死端口列为可用模型。这是"敢缓存负结果"的前提。
- TTL 可配：`GATEWAY_GROUP_ROUTE_TTL`（默认 2.0，`0` 关闭仅供排障），非法值告警回退默认，不在启动期崩。
- `resolve_model` 的 `group_cache` 参数**默认 None = 每次实探**，既有调用方与单测行为零变化。
- **`/v1/models` 顺带修掉一个结构性浪费**：旧实现对同一批模型探测**两轮**（`_available` 遍历 registry + `_group_healthy` 再遍历 groups）。改为"探测集 = registry 去重 ∪ 家族成员"一次并集探测，家族名展示直接复用同一份判定；只派发 TTL 未命中项。

### 代码示例

```python
class GroupRouteCache:
    def __init__(self, ttl: float = GROUP_ROUTE_CACHE_TTL_S, *, clock=time.monotonic) -> None:
        self.ttl, self._clock = ttl, clock
        self._entries: dict[str, tuple[GatewayModel | None, float]] = {}  # route: group 名
        self._avail: dict[str, tuple[bool, float]] = {}                   # avail: 模型 name

    def resolve(self, groups, name):
        if self.ttl <= 0:
            return _resolve_group(groups, name)      # 显式关闭
        now = self._clock()
        hit = self._entries.get(name)
        if hit is not None and hit[1] > now:
            return hit[0]                             # 命中：零 HTTP
        target = _resolve_group(groups, name)
        self._entries[name] = (target, now + self.ttl)  # None 也写缓存
        return target

    def cached(self, name) -> bool | None:            # None = 未命中/过期
        if self.avail_ttl <= 0:
            return None
        hit = self._avail.get(name)
        return None if hit is None or hit[1] <= self._clock() else hit[0]

    def record(self, name, ok: bool) -> None:
        if self.avail_ttl > 0:
            self._avail[name] = (ok, self._clock() + self.avail_ttl)

    def invalidate(self, name: str) -> None:          # 502：两个必须成对调
        self._entries.pop(name, None)

    def invalidate_model(self, name: str) -> None:
        self._avail.pop(name, None)
```

```python
# 共享 helper：TTL 内复用，只并发派发未命中项；fetch / executor 由调用方注入
async def probe_availability(cache, names, fetch, *, executor=None) -> dict[str, bool]:
    snapshot = {n: cache.cached(n) for n in names}     # 一次快照读全，避免探测期跨 TTL
    stale = [n for n in names if snapshot[n] is None]
    # executor 非空（overview 的 64-worker 池）走 run_in_executor，否则 to_thread
    ...
    available = {n: bool(v) for n, v in snapshot.items() if v is not None}
    for name, ok in results:
        cache.record(name, ok)
        available[name] = ok
    return available

# 网关 /v1/models：探测集 = registry 去重 ∪ 家族成员，家族名展示复用同一份 available
available = await probe_availability(
    group_cache, list(candidates), lambda name: is_model_available(candidates[name])
)

# webui /admin/api/models 与 /admin/api/overview 共用 build_summaries → 同一份缓存
async def build_summaries(request, profiles, executor=None):
    from modelctl.core.process import is_running_any   # 函数内导入：保 patch 生效
    cache = request.app.state.group_route_cache
    by_name = {p.name: p for p in profiles}
    available = await probe_availability(
        cache, list(by_name), lambda n: is_running_any(n, by_name[n]), executor=executor
    )
    return [_model_summary(p, available[p.name]) for p in profiles]  # 传入 running 不再自探
```

### 三个必须踩住的坑

**1. avail 层 TTL 必须 > 前端轮询间隔，否则命中率恒 0**

`DashboardView.vue` 是 `setInterval(refresh, 3000)`。如果 avail TTL 沿用 route 层的 2.0s，那么每次 3s 轮询都落在缓存过期**之后**——接了缓存但一次都没命中，还白搭一层复杂度。故两层 TTL **必须解耦**：

| 层 | env | 默认 | 约束来源 |
|---|---|---|---|
| route | `GATEWAY_GROUP_ROUTE_TTL` | 2.0s | 影响**路由正确性**（选错成员 = 请求打错端口），必须小 |
| avail | `GATEWAY_AVAIL_CACHE_TTL` | 5.0s | 只是状态展示，但必须 > 3s 轮询间隔 |

**2026-09-10 补正（比"3s 轮询间隔"更硬的约束）**：TTL 还必须 **> 一次冷探测本身的耗时**。前端 `setInterval` 是"请求没回不发下一次"，所以真实节奏 = `3s 等待 + 上一条请求耗时`，而不是固定 3s。冷探测本机 7~13s，若 TTL 只有 5s，缓存**在冷探测返回的瞬间就已过期**（写入时 `now+5`，而下一次请求最早也在写完之后 4~13s）→ 每次都冷启、缓存永不命中。本机 `.env` 因此取 `GATEWAY_AVAIL_CACHE_TTL=12`（> 冷路径下限），实测 models 稳态 ~300ms、overview 稳态 ≈ 探测地板价。

> **稳态下仍会周期性 cold，属正常**：TTL 从"冷探测返回那一刻"起算（`record` 在 fetch 之后）。热请求不回写缓存，过期时间固定不动；而每个热周期 = `3s + ~1.2s`，于是大约每 3 个周期就会跨过一次 TTL 边界 → 10 次轮询出现 3~4 次 cold 是预期的，**不是缓存没共享、也不是 uvicorn 多 worker**（`server.py` 走 `uvicorn.run(app, ...)` 单进程单事件循环）。

**2. `is_running_any` 的 patch 目标各处不同，helper 必须接受回调**

- gateway：模块级 `from ... import is_running_any` → 测试 patch `modelctl.core.gateway.is_running_any`。
- admin_models：函数体内 `from modelctl.core.process import is_running_any` → 测试 patch `modelctl.core.process.is_running_any`（模块属性）。

共享 helper 若在此写死任一个绑定，另一处的测试桩会**静默失效**：桩没被调到、真探测跑起来 → 端口不通 → 恒 False。表现为"测试莫名说模型已停止"，且不会报错。故 `probe_availability(cache, names, fetch, *, executor=None)` 把 `fetch` 交给调用方。

**3. 启停端点必须主动失效（否则是产品级回归）**

`stop_model` 是**同步**的，状态立即翻转。缓存里的 `True` 会让界面在用户点了"停止"之后仍显示 running 达 5s。`start` / `restart` 不需要——它们是 202 异步任务，引擎起来本身远超 TTL。

### 测试隔离（新增配置键的必做项）

`GATEWAY_GROUP_ROUTE_TTL` / `GATEWAY_AVAIL_CACHE_TTL` 决定探测结果是否跨请求缓存 = **改变控制流**，两个都必须进 `conftest.py` 的 delenv 清单。开发者本地 `.env` 里为排障设成 `0` 后，所有"第二次请求应命中缓存"的断言会**全量红、单跑绿**——与本项目已记录的 `GATEWAY_DEFAULT_MODEL` 泄漏完全同一口径（见 [test-isolation.md](test-isolation.md)）。

同理，测试要关/开缓存时，**必须 setenv 正确的那一层**：`/v1/models` 走 avail 层，把 route 层设成 0 不会让它多探一次（第一版测试就踩了这个，报 `assert 1 == 2`）。

### 验证

用**注入 clock / 长 TTL + 断言探测次数**钉语义，绝不用 `sleep` 碰时钟：

- `tests/test_gateway.py` 路由层：命中不探测 / 过期重探 / 负结果缓存 / invalidate 强制重探 / `ttl=0` 关闭 / 未注入缓存无回归 / 502 失效 / 两层 env 覆盖与非法回退。
- `tests/test_gateway.py` 列表层：TTL 内二次请求零探测 / `avail_ttl=0` 回到每请求实探 / 部分命中只派发未命中项 / **502 清可用性后死端口不再列出**。
- `tests/test_admin_models_classify.py`：两次 `build_summaries` 只探一轮（overview 与列表合流）/ 注入的 64-worker 池确实被用上（记录线程名，防退回默认池的 11s 排队回归）/ `avail_ttl=0` 每调实探。

### 边界 / 待补

- `clock=time.monotonic`：NTP 回拨不得影响过期判定（与 `accounts/limits.py` 的 RPM/TPM 同口径）。
- **两层缓存键空间不相通**是刻意取舍：打通成"`resolve` 内部走 `avail`"确实能让路由层在 TTL 内不再遍历成员，但要在热路径上维护 `any()` 等价逻辑，收益边际、风险更大。当前形态下数据面走 route 层、列表走 avail 层，各自命中即零 HTTP。
- **webui 与 gateway 是两个进程**（`modelctl webui` 4173 / `modelctl gateway start` 5003，同一份 `create_app` 代码、不同端口与 PID），**缓存不跨进程共享**。本节的"合流"指 webui 进程内 `/admin/api/overview` 与 `/admin/api/models` 共用一份；两个进程对同一批 profile 仍各探一轮。要再收敛只能上共享存储（Redis / 文件），成本明显高于收益。
- `/admin/api/models/{name}` 详情端点**刻意不接缓存**：详情页要看最新态，且它是单条查询不是遍历，没有放大效应。
