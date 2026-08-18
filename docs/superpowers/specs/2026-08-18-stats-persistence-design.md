# stats.py 用量统计改造设计

## 背景

当前 `src/modelctl/core/stats.py` 实现的用量统计服务存在三个问题：

1. **数据不持久化**：累计 token 数据只保存在内存中，统计服务进程停止后全部丢失，模型重启后无法延续历史用量。
2. **token/s 非实时**：当引擎没有原生速率 gauge 时，当前回退逻辑用“总 token / 总时间”计算，不能反映当前吞吐。
3. **cc-switch 字段有限**：`/api/usage` 仅返回累计费用与文字 `extra`，缺少结构化实时速率字段，也未提供多模型聚合视图。

## 目标

1. 实现累计 token 的持久化存储，确保统计服务重启后历史数据不丢失。
2. 重构 token/s 计算逻辑，优先使用引擎原生 gauge，缺失时用滑动窗口计算瞬时生成速率。
3. 增强 `/api/usage` 响应字段，新增结构化实时速率，并支持 `?model=all` 多模型聚合。
4. 保证改造不破坏现有接口、稳定性与性能。

## 设计

### 1. 持久化存储

#### 1.1 存储位置

- 默认目录：`${PROJECT_ROOT}/data/cache/`
- 覆盖方式：`.env` 中新增 `USAGE_DATA_DIR` 环境变量
- 每个模型一个 JSON 文件：`data/cache/<model-name>.json`
- 文件名直接复用 `profile.name`，包含 `-` 等字符也无妨

#### 1.2 文件格式

```json
{
  "prompt_total": 123456,
  "predicted_total": 789012,
  "updated_at": 1692345678.123
}
```

- 仅保存跨进程需要延续的累计值，不保存瞬时速率或历史采样点
- `updated_at` 为最后一次成功写回的时间戳（`time.time()`，秒级浮点）

#### 1.3 读写策略

- `UsageCollector` 初始化时调用 `_load_persisted()` 读取文件；文件不存在时累计基线均为 0
- 每次成功轮询后：
  1. 用引擎返回的累计值与内存基线比较，取较大者作为新的累计值
  2. 若累计值有变化，调用 `_persist()` 原子写回 JSON
- 原子写回：先写入 `*.json.tmp`，再用 `os.replace()` 替换，避免 corrupt
- 写文件失败不抛异常，仅记录到 snapshot 的 `error` 字段，不影响轮询继续

#### 1.4 与现有行为的兼容性

- 新增持久化逻辑对现有 `/api/usage` 字段完全透明
- 不引入第三方依赖，保持 `stats.py` 纯标准库

### 2. 实时 token/s 计算

#### 2.1 优先使用引擎原生 gauge

- 如果 `parse_metrics()` 返回的 `prompt_rate` / `predicted_rate` 大于 0，直接使用
- 该逻辑与现有代码一致，无需改动

#### 2.2 滑动窗口回退计算

当引擎未暴露速率 gauge 时，在 `UsageCollector` 内部维护最近 10 次采样的环形缓冲区：

```python
self._rate_window: list[tuple[float, float, float]] = []
# 元素为 (timestamp, prompt_total, predicted_total)
```

每次 `_poll_once()` 成功后：

1. 追加新采样 `(now, prompt_total, predicted_total)`
2. 保留最近 10 条，移除最旧
3. 若窗口大小 >= 2 且时间差 > 0：
   - `predicted_rate = (latest.predicted - oldest.predicted) / (latest.time - oldest.time)`
   - `prompt_rate` 同理
4. 若窗口不足 2 条或时间差为 0，速率保持 0

#### 2.3 展示

- 在 `/api/usage` 响应中新增 `prompt_rate` / `predicted_rate` 字段
- `extra` 字符串继续保留原有文字描述，不删除

### 3. cc-switch 整合

#### 3.1 字段增强

单个模型响应示例：

```json
{
  "isValid": true,
  "used": 12.34,
  "unit": "CNY",
  "planName": "DeepSeek-V4-Flash 本地部署",
  "extra": "累计 1,000,000 tokens ... | 生成速率 55.0 tok/s | 运行 1h23m",
  "total": 100,
  "remaining": 87.66,
  "model": "deepseek-v4-flash-llamacpp",
  "prompt_rate": 100.5,
  "predicted_rate": 55.0
}
```

#### 3.2 多模型聚合

新增 `/api/usage?model=all`：

- 遍历全部 `targets`
- 忽略 `mapping is None` 的引擎（如 ollama、unsloth 暂不支持精确统计）
- 聚合规则：
  - `isValid`：所有目标 `isValid` 的 `and` 结果
  - `used`：各目标费用之和
  - `total` / `remaining`：仅当所有目标均配置 budget 时求和；任一目标无 budget 则均为 `None`
  - `planName`：固定为 `"modelctl 聚合用量"`
  - `extra`：拼接各模型名称、累计 token 与速率
  - `model`：固定为 `"all"`
  - `prompt_rate` / `predicted_rate`：各目标速率之和
  - 任一目标报错时，`isValid=false`，`invalidMessage` 包含具体错误

#### 3.3 向后兼容

- `?model=<name>` 行为不变，仅多两个字段
- 不带 `?model` 时仍返回第一个 target

## 接口变更

| 接口 | 变更 |
|---|---|
| `UsageCollector.__init__` | 新增 `data_dir: Path` 参数；初始化加载持久化文件 |
| `UsageCollector._poll_once` | 轮询成功后更新窗口、持久化累计值 |
| `UsageCollector.snapshot` | 返回新增 `prompt_rate`、`predicted_rate` |
| `build_usage_payload` | 新增 `prompt_rate`、`predicted_rate` 入参与响应字段 |
| `UsageHandler._resolve_payload` | 新增 `model=all` 分支 |
| `_targets_from_profiles` | 新增 `data_dir` 透传 |
| `run_server` | 新增 `data_dir` 构造与传入 |

## 新增环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `USAGE_DATA_DIR` | `${PROJECT_ROOT}/data/cache` | 用量累计持久化目录 |

## 测试策略

1. **持久化**：构造临时目录，验证重启后累计值从文件恢复、增量正确累加、文件损坏时可安全回退。
2. **实时速率**：模拟 vllm 等无 gauge 场景，验证滑动窗口计算正确、窗口满后旧点被剔除。
3. **聚合**：构造多个 target，验证 `?model=all` 各字段聚合规则。
4. **兼容性**：验证原有测试（`test_parse_metrics_llamacpp`、`test_build_payload_with_budget` 等）继续通过。

## 风险与注意事项

1. JSON 文件写回频率与轮询间隔一致，I/O 开销可控；`on-demand` 模式下每次请求都会写文件，若 cc-switch 轮询过频可能增加 I/O，可接受。
2. 聚合视图中若包含大量模型，`extra` 字符串会较长，但仍在合理范围。
3. 持久化数据仅累计总量，不保存时间序列；如需历史趋势分析需后续扩展。

## 依赖

- 仅使用标准库 `json`、`pathlib`、`threading`、`os`，无新增第三方依赖。
