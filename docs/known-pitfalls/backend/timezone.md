# 后端 / 运行时

> 原始单文件已并入本文件归档，保留溯源信息。

## 进程未设置时区，日志与审计时间比预期早 8 小时

**日期**：2026-09-03
**分类**：后端 / 运行时
**一句话描述**：全项目时间都是"隐式本地时间"，继承宿主 OS 时区；部署机是 UTC，日志/审计/容器内引擎日志就比上海时间早 8 小时。

### 症状

- `data/logs/modelctl.log` 的时间戳、`modelctl audit` 的 `ts` 列与本地墙钟差 8 小时。
- Web UI「审计日志」「仪表板 → 探测于」显示的时间跟服务器本地时间对不上。
- **docker 形态的 vllm/tokenspeed/trtllm 日志始终是 UTC**，即使宿主机已是东八区。
- 同一份审计 JSONL 在不同机器上查询，时间窗过滤结果不一致（跨机偏差 8h）。

### 根因

仓库内原本**没有任何一处设置时区**（搜 `TZ` / `Asia/Shanghai` / `tzdata` 零命中），所有时间都隐式取 OS 本地时区：

| 位置 | 写法 | 时区来源 |
|---|---|---|
| loguru 控制台/文件 | `format="{time:HH:mm:ss}"` | loguru 默认本地时间 |
| 审计 `ts` | `datetime.now().astimezone().isoformat()` | `astimezone()` 无参 = 系统本地 |
| 审计按天分文件 | `date.today()` | 本地日期，UTC 机按 UTC 切日 |
| 探测时间 | `time.strftime("%Y-%m-%dT%H:%M:%S")` | **naive，不带偏移** |

开发机（Windows）本身是 +08，本地看不出问题；部署到 Linux（默认 UTC）才暴露。

### 解决方案：`core/timezone.py` + 三条传播路径

`apply_timezone()` 在三个进程入口（`cli.main` / `webui.server.main` / `gateway.main`）钉死本地时区，loguru 的 `{time}`、`datetime.now()`、`date.today()` 全部随 C 库时区变化，**一处生效、全链路对齐**。

配置项用**标准 `TZ`**（不是自造 `MODELCTL_TZ`），三条路径各有坑：

| # | 目标 | 机制 | 坑 |
|---|---|---|---|
| 1 | 本进程 | `os.environ["TZ"]` + `time.tzset()` | Windows 无 `tzset`（见陷阱 1） |
| 2 | venv 子进程、stats 服务 | `start_detached` 的 `env = {**os.environ, **extra_env}` 继承 | 纯隐式，重构成"白名单 env"会**静默失效**，故 `subprocess_timezone()` 显式兜底 |
| 3 | docker 容器 | **只认 `-e`** | `start_detached` 注入的 env 只进 docker CLI 宿主进程，**进不了容器** |

第 3 条最隐蔽：项目里早就有注释记录了"env 进不了容器"这一事实（`vllm.py` 的 `docker_env` 处理），但没人把它应用到 `TZ` 上，于是三个 docker 适配器全部默认 UTC。

```python
# container_timezone_args()：镜像缺 tzdata 时 glibc 会静默忽略 -e TZ，仍 UTC。
# 因此能定位宿主机 tz 文件时额外挂载，不依赖镜像内 tzdata。
args = ["-e", f"TZ={tz}"]
tz_file = _zoneinfo_file(tz)          # 只从 zoneinfo.TZPATH 找，且校验 is_file()
if tz_file is not None:
    args += ["-v", f"{tz_file.as_posix()}:/etc/localtime:ro"]
```

挂载源只从 `zoneinfo.TZPATH` 取并校验 `is_file()`；找不到就只 `-e`（优雅降级，不阻断启动）。

### 陷阱 1：Windows 写 `TZ` 会把子进程时间污染成 +0100（**首次修复引入的回归**）

最初实现是"无条件 `os.environ["TZ"] = tz`，`tzset` 用 `hasattr` 保护"，并断言"Windows 无副作用，仅透传给子进程"。**这个断言是错的**，实测：

```
TZ 未设置        → 16:08:43 +0800   正确
TZ=Asia/Shanghai → 09:08:43 +0100   错，比 UTC 更难排查
TZ=UTC           → 08:08:43 +0000
```

根因：**UCRT 按 POSIX 语法解析 `TZ`**，`Asia/Shanghai` 被切成 STD 名 `Asia` + DST 名 `Shanghai`，DST 段无偏移量则按 +1h 处理，夏令时期间即 +0100。`America/New_York`、甚至 `Asia%Shanghai` 都是同样结果。

之所以第一版"实测无副作用"没发现：那次是在**同一进程内**改 `os.environ`，而 Windows 没有 `tzset`，CRT 只在**进程启动时**读一次 `TZ` —— 必须真的 `Popen` 出子进程才暴露。

> **教训**：验证"环境变量对子进程的影响"，绝不能只在父进程内改后打印；必须起子进程观察。

修复：无 `tzset` 的平台**完全不写 `os.environ["TZ"]`**，`apply_timezone()` 返回空串并 `logger.debug`（Windows 只作开发机，每次 CLI 调用都 warning 会变噪音）。

### 陷阱 2：Windows 的 `zoneinfo` 需要 `tzdata`

Windows 无 `/usr/share/zoneinfo`，`ZoneInfo("Asia/Shanghai")` 直接抛 `ZoneInfoNotFoundError`，导致合法名校验全判 False。且此时 `zoneinfo.TZPATH` 是**空元组** —— 所以容器那条 `-v /etc/localtime` 挂载在 Windows 宿主机上会自动降级为仅 `-e`。

```toml
"tzdata; sys_platform == 'win32'",   # Linux/macOS 自带，用 marker 避免冗余安装
```

### 陷阱 3：naive 时间戳被前端按浏览器时区二次解释

`time.strftime("%Y-%m-%dT%H:%M:%S")` 不带偏移，前端 `dayjs(v)` / `new Date(v)` 按**浏览器**时区解释 → 浏览器不在 +08 就显示偏差。后端一律输出带偏移 ISO：

```python
"probed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
```

同类隐患：`admin_audit._entry_time` 对**数值型**按 `tz=UTC`、对 **naive 字符串**按本地 `astimezone()` 解释。钉死 `TZ` 后新数据不再矛盾，但**历史审计文件**跨机查询仍可能偏差 8h。

### 陷阱 4：modelctl 管不到的部分仍需宿主机时区

nginx access/error log、logrotate、`docker logs`、`journalctl` 的时间戳都取**宿主机系统时区**，`TZ` 环境变量对它们无效。部署机必须 `timedatectl set-timezone Asia/Shanghai`（已写入 README「时区」节）。

### 为什么不选"全链路 UTC + 展示层转换"

改造面覆盖 4 个写入点 + 前端渲染 + 审计文件名切日规则，且历史数据格式断裂。本项目诉求本质是"展示对齐东八区"，`tzset()` 性价比远高于改数据模型。若将来需跨多时区部署且要求统一存储，再走 UTC 方案。
