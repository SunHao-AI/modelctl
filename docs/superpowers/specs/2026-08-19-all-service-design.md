# 一键启停（modelctl all）设计

- 日期：2026-08-19
- 状态：待评审
- 范围：modelctl 一键启动/停止/重启/查看默认模型 + 网关 + 用量统计三件套

## 1. 背景与动机

日常部署需要依次执行多条命令：`modelctl start <默认模型>`、`modelctl gateway start`、`modelctl stats start`，且停止时需反序逐条操作。目标：一条命令完成三件套的启停/重启/状态查看，并给出逐组件结果汇总。

## 2. 决策记录

| 决策点 | 结论 |
|---|---|
| 落地形式 | `modelctl all start\|stop\|restart\|status` 子命令 + `script/modelctl-all.sh` 薄脚本包装 |
| 组件范围 | 默认模型 + gateway + stats 三件套 |
| 默认模型 | `GATEWAY_DEFAULT_MODEL`（.env 可覆盖），未设置回退 `deepseek-v4-flash`，经 profile 的 alias/name 解析；`--model` 可覆盖 |
| 失败处理 | 逐组件尝试 + 汇总：单组件失败继续后续，任一步 error → 整体 exit 非 0 |
| 命令集 | start / stop / restart / status 四动作全部支持 |
| 代码组织 | 新增 `core/all_service.py` 编排模块（可单测），cli.py 注册子命令，不内联编排逻辑 |

## 3. CLI 接口

三件套各组件与 `all` 均支持 start / stop / restart / status 四动作：

```
modelctl start|stop|restart|status <name>        # 模型（既有，保留不变）
modelctl gateway start|stop|restart|status       # 网关（补齐 restart；status 既有）
modelctl stats  start|stop|restart|status        # 统计（补齐 restart、status）

modelctl all start    [--model NAME] [--timeout N]   # 一键启动：默认模型 → gateway → stats
modelctl all stop                                     # 一键关闭：stats → gateway → 全部运行中模型（依赖方先停）
modelctl all restart  [--model NAME] [--timeout N]   # 仅默认模型：已运行→停后启；未运行→直接启
modelctl all status                                   # 汇总三件套（默认模型 + 网关 + 统计）状态
```

- `--model`：覆盖默认模型 profile（缺省解析 `GATEWAY_DEFAULT_MODEL`）
- `--timeout`：模型健康检查超时（默认 300，与 `start` 一致）
- 薄脚本 `script/modelctl-all.sh`（与 `script/modelctl.sh` 同模式：优先原生 uv，回退 uv.exe）：`uv run modelctl all "$@"`

### 3.1 单组件四动作语义（gateway / stats 补齐部分）

- **gateway restart**：运行中 → stop → start；未运行 → 直接 start（exit 2 语义同 start）。**gateway status** 已有（运行中 + /v1/models 健康探测）。
- **stats restart**：同上（stop → start）。**stats status**：新增——`is_running("usage-stats")` + 端口 `/api/usage` 健康探测，输出 `运行中/已停止/无响应`。

## 4. 编排模块 `core/all_service.py`

```python
@dataclass
class ComponentResult:
    component: str            # "model:<name>" / "gateway" / "stats"
    status: Literal["ok", "skipped", "error"]
    detail: str = ""          # 错误原因 / 提示

def start_all(models_dir, model_name=None, timeout=300) -> list[ComponentResult]
def stop_all(models_dir) -> list[ComponentResult]
def restart_all(models_dir, model_name=None, timeout=300) -> list[ComponentResult]
def status_all(models_dir) -> list[ComponentResult]
```

### 4.1 启动/停止顺序与语义

- **start**：默认模型 → gateway → stats。每步独立 try：成功 `ok`；已运行 `skipped`；异常 `error`（记录原因），**继续后续组件**。默认模型解析失败（profile 不存在）记 error，不阻断网关/统计。
- **stop**：stats → gateway → **所有运行中的模型**（遍历全部 profiles，逐个 `is_running` 检查，运行中的才 stop；未运行跳过 `skipped`）。覆盖用户经 `modelctl start|restart <name>` 启动的所有模型，不限于默认模型。每步尽力，汇总。
- **restart**：仅默认模型——逐组件执行"运行中则 stop → start"；未运行直接 start。汇总（语义同 start：只重启默认模型，不影响其他运行中模型）。
- **status**：默认模型 + gateway + stats 三组件复用 `is_running` + `wait_ready`，输出 `运行中/已停止/无响应`。

### 4.2 汇总输出

每行 `[ok/skipped/error] 组件：详情`；任一步 error → start/restart 整体 exit 2、stop exit 1，并提示"后续可执行 `modelctl status` 细查"。

### 4.3 复用与抽取

- 模型启停：复用现有 `_cmd_start`/`_cmd_stop`/`_cmd_restart` 的核心逻辑，抽为公共函数（如 `start_profile(profile, timeout)` / `stop_profile(profile)` / `restart_profile(profile, timeout)`），cli 既有命令与 all_service 共用，避免双份逻辑。
- gateway/stats：抽取 `start_gateway()` / `stop_gateway()` / `restart_gateway()` / `status_gateway()` 与 `start_stats()` / `stop_stats()` / `restart_stats()` / `status_stats()` 公共函数，单组件四动作命令与 `all` 编排共用。

## 5. 默认模型解析

`resolve_default_profile(models_dir, model_id) -> Profile | None`：

- `model_id` = `GATEWAY_DEFAULT_MODEL`（未设置回退 `deepseek-v4-flash`，与既有文档/nginx 示例默认一致）
- 匹配 `profile.name == model_id` 或 `model_id in profile.aliases`（与 gateway `build_registry` 的 name+aliases 语义一致）
- 解析不到 → 该组件记 error（提示配置 `GATEWAY_DEFAULT_MODEL` 或 `--model`），继续其余组件

## 6. 边界与不做

- **幂等**：start 时已运行 → `skipped`；stop 时未运行 → `skipped`。
- **stop 覆盖全部模型**：stop_all 遍历全部 profiles 停止所有运行中的模型（含非默认），start/restart 仅作用于默认模型。
- **不处理**：并发调用互斥、组件间依赖（三组件相互独立）、模型自动发现（start 仅默认模型一个模型，stop 需遍历全部）。

## 7. 测试与文档

- **测试**：`tests/test_all_service.py`（默认模型解析、启动/停止顺序、失败汇总继续、幂等 skipped、restart 的停后启、**stop_all 遍历全部运行中模型（含非默认）**，mock 启动原语）+ `tests/test_modelctl.py` 补 `all start|stop|restart|status` 与 `gateway restart|status`、`stats restart|status` 命令分发用例。
- **文档**：README 增补"一键启停"一节（各组件与 all 的四命令、组件、默认模型、失败语义）；`.env.example` 补 `GATEWAY_DEFAULT_MODEL` 注释。

## 8. 里程碑

1. `core/all_service.py`：ComponentResult + resolve_default_profile + 四个编排函数（依赖 cli 抽取的公共原语）
2. cli.py：抽取公共原语（模型 start/stop/restart + gateway/stats 各 start/stop/restart/status），`all` 子命令注册，gateway/stats 补齐 restart/status
3. `script/modelctl-all.sh` 薄脚本
4. 测试 + README/.env.example 文档更新
