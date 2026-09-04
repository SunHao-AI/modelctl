# 运行时数据目录（LOG_DIR / CACHE_DIR / USAGE_DATA_DIR / AUDIT_DIR）

> 本文件聚合三个同源问题（默认值没有单一真值来源）：默认值分散、模块级常量绕过 env、
> 相对路径按 CWD 解析。首次沉淀于 2026-09-04 目录统一改造。

## 运行时数据目录默认值散在各调用点，长成三套口径

**日期**：2026-09-04 · **分类**：后端 / 配置管理

### 症状

- 在 A 目录执行 `modelctl audit path` 返回 `D:\some\cwd\data\audit`，在 B 目录执行返回另一个路径；
  审计文件按启动 cwd 散落各处。
- 换机器部署必须逐项手填 `LOG_DIR` / `CACHE_DIR` / `USAGE_DATA_DIR` / `AUDIT_DIR` 绝对路径，
  漏一项就把日志写到仓库外。
- 用量统计面板累计为 0：`modelctl stats` 读 `data/cache`，网关其实写到了另一处。

### 根因

项目无集中 Settings 类，默认值散在各调用点的 `os.environ.get(...) or <default>`，
同类的四个目录各写各的，最终成三套口径：

| 变量 | 改造前默认值 | 口径 |
|---|---|---|
| `LOG_DIR` | `PROJECT_ROOT.parent / "logs"` | 项目根**上级**（写到仓库外） |
| `CACHE_DIR` | `PROJECT_ROOT / "data" / "cache"` | `data/` ✅ |
| `USAGE_DATA_DIR` | `PROJECT_ROOT / "data" / "cache"` | 与 `CACHE_DIR` **撞同一目录** |
| `AUDIT_DIR` | `"data/audit"`（字符串） | **相对 CWD**，三处重复定义且仅 webui 那处修过 |

### 解决方案

新增 `src/modelctl/core/paths.py` 作为**唯一**真值来源：

```python
DATA_ROOT = PROJECT_ROOT / "data"

def resolve_data_dir(env_value: str | None, subdir: str) -> Path:
    raw = (env_value or "").strip()
    if not raw:
        return DATA_ROOT / subdir
    p = Path(raw).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p   # 相对值按项目根，绝不按 CWD
```

`log_dir()` / `cache_dir()` / `usage_data_dir()` / `audit_dir()` 四个函数各自
`resolve_data_dir(os.environ.get(KEY), "<子目录>")`。消费点（`logging.py`、`process.py`、
`gpu_lock.py`、`stats.py`、`gateway.py`、`all_service.py`、`cli.py`、`webui/admin_audit.py`、
`webui/admin_probe.py`）一律改调这四种函数，删除本地默认值。

### 注意

- `process.py` 保留 `log_dir()` / `cache_dir()` 同名薄转发，避免改动扩散到十余处无关调用点。
- `audit_dir()` **故意不 mkdir**：写入方 `RequestAuditLog._today_path` 落盘前幂等建目录，
  而只读的 `modelctl audit stats` 在目录不存在时应返回空结果，不该有建目录副作用。
- stats 与网关共用 `USAGE_DATA_DIR`：`all_service.py` 必须**总是透传解析后的绝对路径**，
  只透传"env 是否设置"会让子进程按自身 `PROJECT_ROOT` 重算相对值，token 累计直接分家。
- 默认值变更属行为变更：历史日志需手工 `mv ../logs/* data/logs/`；旧配置里已显式写了
  绝对路径的机器不受影响。
- **切 `CACHE_DIR` 前必须先停服务**（实测踩过）：PID 文件按旧目录写，改完默认值后
  `modelctl status` 一律报"已停止"、`all stop` 停不掉、再 `start` 直接撞 5002/5003/4173 端口。
  正确顺序：`$env:CACHE_DIR="<旧目录>"; modelctl stats stop; modelctl gateway stop; modelctl webui stop`
  （显式环境变量优先于 `.env`，能定位旧 PID），确认端口释放后再改配置启动。

## 模块级常量目录绕过环境变量，GPU 互斥静默失效

**日期**：2026-09-04 · **分类**：后端 / 配置管理

### 症状

设置 `CACHE_DIR=/data/nvme/cache` 后，`modelctl status` 能读到 PID 文件，但两个模型抢占同一张
GPU 却**不报** `[gpu_lock] ... 已被模型 X 占用`，直接把引擎拉起来撞显存。

### 根因

`gpu_lock.py` 用模块级常量定义锁目录：

```python
LOCK_DIR = PROJECT_ROOT / "data" / "cache"   # 导入时求值，之后与 CACHE_DIR 无关
```

PID 文件走 `cache_dir()`（读 env），锁文件走常量（不读 env）→ 设了 `CACHE_DIR` 两者分家，
`list_gpu_locks()` 在新目录里 glob 不到任何 `.gpu-lock`，冲突检测恒为"无冲突"。
这类失败**无任何报错**，只在显存 OOM 时才暴露。

### 解决方案

删除常量，目录一律由函数即时解析：

```python
def _lock_path(name: str) -> Path:
    return cache_dir() / f"{name}{LOCK_SUFFIX}"
```

测试侧把 `monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp)`（共 14 处）
换成 `monkeypatch.setenv("CACHE_DIR", str(tmp))` —— 这也说明：**测试被迫 patch 模块常量，
本身就是"该值不该是常量"的信号**。

## 相对路径默认值按 CWD 解析，CLI 换个目录就写错位置

**日期**：2026-09-04 · **分类**：后端 / 配置管理

### 症状

`AUDIT_DIR` 默认写成 `Path("data/audit")`，从 systemd / cron / 别的目录调用 `modelctl audit query`
时读到的是 `<启动目录>/data/audit`，而不是项目里的审计目录；webui 看得到审计文件、CLI 说目录为空。

### 根因

`Path` 相对路径由**进程 CWD** 决定，而 `.env` 的值是纯字符串直接进 `os.environ`
（项目自实现解析，无 pydantic-settings 的绝对化能力），用户完全可以写相对值。
三处消费点里只有 `webui/admin_audit.py` 打了 `is_absolute()` 补丁 —— 补丁散落即缺陷。

### 解决方案

在 `resolve_data_dir()` 单点做绝对化：相对值 `PROJECT_ROOT / p`。
约定：**项目内所有数据/配置路径的相对值一律按 `PROJECT_ROOT` 解析，绝不按 CWD**；
新增同类路径不得再写裸 `Path("data/...")`。
