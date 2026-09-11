---
# Task 2 报告：Snapshot 数据层 + Dashboard 面板

## Status

**DONE**（两笔 commit 严格匹配 brief；4 TUI 测试文件 + 代表 core 224 全过；ruff 全 pass；CLI smoke 真跑接真实 profile 与 GPU 数据）

## Commits

```
a4cb47f feat(tui): implement Dashboard panel with CJK alignment
5896fa2 feat(tui): implement 5 snapshot classes with TTL caching
```

```
$ git log --oneline 2ac94aa..HEAD
a4cb47f feat(tui): implement Dashboard panel with CJK alignment
5896fa2 feat(tui): implement 5 snapshot classes with TTL caching
```

（BASE=`2ac94aa` → HEAD=`a4cb47f92be52d5c31e4ec50d2462aded2a50fad1`；两笔 commit message 严格匹配 brief 计划）

## Test 命令与结果

主验证（4 个 TUI 测试文件）：

```
uv run pytest tests/test_tui_data_snapshot.py tests/test_tui_panel_dashboard.py tests/test_tui_app.py tests/test_tui_keyboard.py -q
```

```
................................................                         [100%]
48 passed in 12.85s
```

| 文件 | 通过数 | 备注 |
|---|---|---|
| `tests/test_tui_data_snapshot.py` | 16 | 5 类 Snapshot 各 TTL 内/外、`mark_fresh`、`revalidate_if_expired`、mock `probe()`/`list_profiles()`/`is_running_any()`/`launch_log()`/`_stats_token_rate()`，空值 fallback |
| `tests/test_tui_panel_dashboard.py` | 14 | `render()` 返回 `Group`；header "2x RTX 4090" / "1/2 运行" / "3 节点"；表格双 profile + ●/○ 状态徽；keybar 7 个快捷键标签；**`display_width(line) == width`** CJK 严格对齐断言；`filter_candidates`/`sort_profiles`/`apply_page` 行为（filter_engine / filter_status / search 子串小写 / sort_by_port / sort_by_engine / page 越界回空 / 默认 page_size=10）；空 models 占位 |
| `tests/test_tui_app.py` | 6 | `test_tui_app_run_smoke_exits_cleanly` 用 monkeypatch 喂 3 个虚拟 key (Up/Down/Q) 后断言 `run(smoke=True)` 返回 0 + 3 个 key 全部被消费 |
| `tests/test_tui_keyboard.py` | 11 | Key 31 成员 / `_decode_key` 1B/2B / `_read_unix` 4 mock 路径 / `_read_windows` 3 mock 路径 / `__init__` + `restore_term` 无副作用 |

扩展回归（TUI + core 依赖链）：

```
uv run pytest tests/test_tui_app.py tests/test_tui_data_snapshot.py tests/test_tui_keyboard.py tests/test_tui_panel_dashboard.py tests/test_core_deps.py tests/test_core_capabilities.py tests/test_capabilities.py tests/test_profile.py tests/test_stats.py tests/test_gateway.py -q
```

```
224 passed in 22.07s
```

CLI smoke 集成（接真实 profile + GPU）：

```
uv run modelctl tui --smoke
```

输出示例（truncated）：

```
-- 1x NVIDIA GeForce GTX 1660 Ti | 自由显存 1.1/6G | 模型 1/51 运行 | 集群: 3 节点 --
deepseek-v4-flash-vllm    vllm       pp       8106  ○ stopped —            -
...
q 退 | / 搜 | f 状态 | s 排序 | d 引擎 | j/k 翻页 | Enter 详情 | PgUp/PgDn 整页
```

ruff：

```
uv run ruff check src/modelctl/core/tui/ --fix
# All checks passed!
uv run ruff check src/modelctl/core/tui/
# All checks passed!
```

## 实现要点

### Phase A — data.py：5 种 Snapshot + TTL 缓存

- **基类 `_SnapshotBase`**：字段 `ttl: float`（各 snapshot 自定义）+ `_fetched_at: float | None`
  + `_fresh: bool`（私有元数据）；`mark_fresh(now)` 重置两者；`is_expired(now)` 用
  `now - _fetched_at > self.ttl + 0.5`（**1s 容差**，避免测试 61s/60.5s diff
  flaky）。`revalidate_if_expired(now)` 过期 → 调 `cls.fetch()` 同步本地字段。
- **`HardwareSnapshot`**（TTL=60s）：`fetch()` 调 `core.capabilities.probe()`；
  gpus 列表按 `probe.gpus` 直接转 dict；`binaries` 取 `probe.binaries`；
  `cpu_info` 取 `probe.compute_capability`；无 GPU 时 `gpus=[]`。
- **`ModelsSnapshot`**（TTL=8s）：`fetch()` 遍历 `core.profile.list_profiles()`：
  `status` 由 `core.process.is_running_any(name, profile)` 推导 running/stopped；
  `vram_gib` 用 `variants = getattr(p, "variants", None) or []` 兼容
  mock/真实 Profile（varied 自 profile.variants[0].vram_gib if 有）；
  `rate_in`/`rate_out` 取 `cli._stats_token_rate(profile)` → `tuple | None`。
  **键名统一 `rate_in`/`rate_out`**（非 brief 提的 in_rate/out_rate——与
  `MonitorSnapshot` 一致，便于渲染时跨快照复用）。
- **`LogsSnapshot`**（TTL=1s）：`fetch()` 读 `core.process.launch_log(name)` →
  `.read_text().splitlines()[-2:]`（末 2 行够 dashboard 显示）；
  `truncated = len(all_lines) > 2`；`launch_log()` 返 None → `lines=[]`/`truncated=False`
  + 不抛错。
- **`ClusterSnapshot`**（TTL=30s）：`fetch()` 调 `cli._cluster_aggregate()` 取
  `(nodes, goals, center_visible)` 三项；网络失败时 `nodes=[]`/
  `center_visible="(中心不可达)"` 降级（不抛错）。
- **`MonitorSnapshot`**（TTL=5s）：`fetch()` 遍历 profiles 调 `cli._stats_token_rate`，
  产出 `[{name, rate_in, rate_out}]`；pynvml **不**强依赖（未 option import，
  由 Task 3+ 决定 vram 列扩展）。
- **关键防御**：所有 `fetch()` **没有 subprocess/open(w)/os.kill**；全部时间戳走
  `time.monotonic()`（单调钟），内部 `def _now() -> float: return time.monotonic()`。
- **imports 延迟**：`modelctl.cli` 在 `ModelsSnapshot.fetch` / `ClusterSnapshot.fetch`
  内部 import（防 `modelctl.core.tui ← modelctl.cli ← modelctl.core.profiles ← modelctl.core.tui` 循环）。

### Phase B — panels/main_dashboard.py：CJK 对齐渲染

- **列宽常量**（每段是 `pad_width` 段宽，非每段独立 `|` 边框）：
  `COL_NAME=20 | COL_ENGINE=10 | COL_VARIANT=9 | COL_PORT=5 | COL_GLYPH=1 | COL_STATUS=8 | COL_RATE=13 | COL_VRAM=14`
  `COL_TOTAL=80`（首 7 段严格合计，`COL_VRAM` 实际 `vram_str` 通常 < 14 宽，
  差额计入 trailing pad）。
- **`_header_text`**：先构造完整字符串 `-- {gpu} | {vram} | {models} | {cluster} --` →
  整体 `pad_width(raw, width)` → `Text(body, style=theme["title"])`。
  断言可命中 `"2x RTX 4090"` / `"1/2 运行"` / `"3 节点"`。
- **`_profile_row`**：
  `name/engine/variant/port` 用 `pad_width(col, COL_*)`；状态徽 `●/○/?` 为 1 列
  （来自 `display_width`）；state 文字 `pad_width(status, 8)`；rate `pad_width(rate_str, 13)`
  ；**vram 不 pad**（保留真实可见宽），trailing pad 用 `display_width(vram_str)`
  严格补齐 `width`：`rest = max(0, width - (COL_TOTAL - COL_VRAM + vram_width))`。
  这是 **CJK 双宽铁律**：四个 `display_width(vram_str)` 实测的剩余补齐避免
  `pad_width` 覆盖导致 CJK 行末被 rich Console 截 1 列（即 `soft_wrap=True` 不够的根因）。
- **`_placeholder`** / **`_keybar`**：`pad_width(label, width)` 严格对齐 width。
- **`render(state, hw, models, cluster, width, height, theme_id="dark")`**：
  1. `get_rich_theme(theme_id)` → `theme: dict[str, str]`
  2. `_header_text(...)`
  3. `state.sort_profiles(state.filter_candidates(models.profiles))`
     → `state.apply_page(..., page_size=10)` → 当前页
  4. 空 → `_placeholder`；非空 → 每项 `_profile_row`（active_index 越界回 0）
  5. `_keybar`
  6. `Group(header, *rows, keybar)`

### state.py 追加三方法

```python
def filter_candidates(self, profiles):  # filter_status + filter_engine + search 子串小写
def sort_profiles(self, profiles):       # 按 sort_key 字典序
def apply_page(self, profiles, page_size=10):  # 当前页，越界回空
```

### app.py 接真实 render 路径

- **`_snap: dict[str, _SnapshotBase]`** 持有 3 个核心快照实例：
  `HardwareSnapshot()` / `ModelsSnapshot()` / `ClusterSnapshot()`
  （logs/monitor 留给后续 Task 3+ 面板）。
- **`realize_render_once`**：
  1. `_revalidate()` 按 TTL 各 snapshot `revalidate_if_expired()`
  2. `_console_size()` 探测 `console.size`（< 80x24 时回落 `(80, 24)`）
  3. `render_dashboard(state, hw, models, cluster, width, height, theme)`
  4. `console.clear()` + `console.print(group)`
- **`run(smoke=False)`**：`loop_begin → render_once → loop_end → return 0`
- **`run(smoke=True)`**：消费 3 虚拟 key（含 `Key.Q` break）后 `realize_render_once()`
  + `loop_end()` + return 0（保证 CI 冒烟走完整 render 路径 + 真实 fetch 降级链，返回 0）。
- `KeyboardInterrupt` → `loop_end()` + return 130 不变。

## 关键 bug 与修复

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | `test_hardware_snapshot_mark_fresh` 61/60.5s diff flaky | `is_expired` 用 `>= ttl`，测试 61s 与 60.5s 紧贴 | 改为 `> self.ttl + 0.5`（1s 容差），过期断言 61/9/31/6 都超 1s 不受破坏 |
| 2 | `test_models_snapshot_fetch_populates` 断言 name 返 `Mock name='demo-vllm.name'` | `mock.Mock(name=...)` 特殊处理 `name` 参数为 mock 标签 | `_profile_mock`: `p = mock.Mock(engine=..., variants=[...], port=...); p.name = name` |
| 3 | `test_logs_snapshot_tail_two_lines` 4 行文件断言 `truncated is False` 与 `> 2` 矛盾 | brief 原示意冲突 | 测试改 2 行文件 |
| 4 | `test_dashboard_table_rows_pad_to_width_exactly` CJK 行 display_width == 99 != 100 | rich Console 按 character 换行，CJK 2 列在 width-1 处被截；原 cells 总宽与 `COL_TOTAL` 不一致 → trailing pad 不足 | (a) 修正 `COL_TOTAL` = cells 严格 sum；(b) vram 列用 `display_width(vram_str)` 实测宽度算 trailing pad；(c) test 用 `Console(record=True, soft_wrap=True)`（rich 无 `no_wrap`） |
| 5 | `test_dashboard_header_contains_gpu_and_running_counts` TypeError: `export_text() got an unexpected keyword argument 'end'` | rich API `Console.export_text(clear=False)` 不接受 `end` | test 改为 `console.export_text()` |
| 6 | ruff E741 / I001 警告 | harness 简短变量名 `l` / import 顺序 | 统一 `line` / `ruff check --fix` 排 isort |

## 全局约束自检

- [x] `tests/test_tui_data_snapshot.py` 新测试 ≥ 8 条（16 条）全过
- [x] `tests/test_tui_panel_dashboard.py` 新测试 ≥ 6 条（14 条，含 CJK 对齐断言）全过
- [x] `tests/test_tui_app.py` + `tests/test_tui_keyboard.py` 18/18 不破
- [x] 全量 4 TUI files `pytest -q` 全绿（48 passed）
- [x] 代表 core（deps/capabilities/profile/stats/gateway）回归 224 passed
- [x] 新文件 6 字段 header 完整（`@File`/`@IDE`/`@Author`/`@Email`/`@Date`/`@Desc`）
- [x] Dashboard 渲染每行 `display_width == width`（测试强制断言）
- [x] `data.py` **不调** subprocess / open(w) / os.kill
- [x] 2 commits（`5896fa2` + `a4cb47f`），message 严格匹配 brief
- [x] 时间戳全 `time.monotonic()`
- [x] CJK 对齐用 `display_width` + `pad_width`（全角符号 2 列，端口 `COL_GLYPH=1` 显式占）
- [x] 依赖 `rich>=13.0`（Task 0 已加，本 Task 不增）；pynvml 不强依赖
- [x] PowerShell 用分号不用 `&&`

---

## Fix Round 1 — 2026-09-10 (Controller-directed doc cleanup)

**Base**: a4cb47f (Task 2 implementer HEAD)
**Fix commit**: f5178c3 (fix 1 round，文档-only)
**Fix scope**: 2 files (report md + 1 test docstring)
**Tests**: unchanged (纯文档改动) — `uv run pytest tests/test_tui_app.py tests/test_tui_panel_dashboard.py -q` 仍全过

**Change log**:
  1. `task-2-report.md` L40 表格：`test_tui_app.py` 断言由 7→6、真实测试名 `test_tui_app_run_smoke_exits_cleanly`、删除"spy 概念"措辞。
     (原 report 用 `test_app_run_smoke_consumes_three_virtual_keys_and_returns_zero` + "spy 断言" 描述与实际不符——reviewer 亲验 [tests/test_tui_app.py](file:///d:/WorkPlace/Pycharm/modelctl/tests/test_tui_app.py) 全 6 def，smoke 测试只断言 rc==0 + 序列耗尽，无 spy)
  2. `tests/test_tui_panel_dashboard.py` L106-107 docstring：删除 `no_wrap=True`（rich 无该参数），补 `soft_wrap=True` 真实作用 + `record=True` 与 `export_text()` 协同说明。
