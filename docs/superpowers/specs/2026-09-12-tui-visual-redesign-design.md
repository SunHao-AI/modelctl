# modelctl TUI 视觉重构设计（rich 原生组件 + llmfit 风格）

> 日期：2026-09-12 · 状态：已确认 · 前置 specs：`2026-09-07-modelctl-tui-design.md`
>
> 概览：在现有 5 视图 TUI 基础上，用 rich 原生组件（Table/Panel/Columns）全面替换手写 `pad_width` 拼行，统一整屏骨架（顶栏 + 硬件栏 + 上下文栏 + 主区 + 底栏），修复 `vram_gib` 恒 0 的数据层 Bug，补采 CTX/TP/QUANT 字段，新增搜索/过滤/排序/帮助键位接线。

---

## 1. 背景与问题诊断

### 1.1 当前痛点

| 症状 | 根因 |
|---|---|
| `highllamacpp`、`ostopped` 挤成一团 | `COL_NAME = 20`，真实 profile 名最长 31+ 字符，`pad_width` 超长不截断 → 吞掉后一列 |
| 状态列显示 `ostopped` | `○` 字形在部分终端回退成 `o`，且与 `stopped` 之间无间隔 |
| VRAM / RATE 全是 `-` | `ModelsSnapshot.fetch` 读 `profile.variants[0].vram_gib`，而真实 `Profile` 无 `variants` 字段 → 恒为 0 |
| 无表头、无选中底色、无视图栏/搜索行 | 当前是纯 `Text` 手拼行，未用列头/边框；`/ f s d PgUp` 在 `_dispatch_key` 里根本没接线（keybar 文案先行） |

### 1.2 关键实证

- rich `cell_len` 对 CJK/符号宽度计算准确（`自由显存`=8、`○stopped`=8、长名 31 字符），旧 spec 中"Rich Table 对 CJK 不算准确宽度"的结论已过时。
- 信息密度原料现成：`Profile.engine_config` 有 `max_model_len` / `tensor_parallel_size` / `quantization` / `kv_cache_dtype`；`Capabilities` 有逐卡 `vram_free_mb`；`vram_estimator` 和 `_check_weights_advisory` 已有 KV/权重估算。
- `docs/known-pitfalls/backend/tui-交互与渲染.md` 已记录"手写 pad_width 拼行是错位根因"。

### 1.3 设计范围

5 视图全量重做：Dashboard / Detail / Plan / Cluster / Monitor 的渲染组件全部换成 rich 原生，交互逻辑保持不变。

---

## 2. 全局骨架与主题系统

### 2.1 整屏骨架（5 视图共享）

```
行 1   顶栏      Panel 单行：modelctl TUI | 视图标签条（[1]DASH [2]DETAIL ...） | [dark]
行 2   硬件栏    Panel 单行：1x GTX 1660 Ti | 自由显存 4/6G | 模型 0/51 运行 | 集群: 0 节点
行 3   上下文栏  Panel 单行：搜索: xxx | 过滤: 全部 | 排序: name | 引擎: all | 模式: NORMAL
行 4~H-2 主区   各视图 render() 返回的 RenderableType（Table / Group / Panel）
行 H-1 分隔线    ── 细线（theme["dim"]）
行 H   底栏      keybar（视图特定）+ 模式指示（NORMAL / SEARCH / VISUAL）
```

**实现**：新增 `panels/chrome.py`，导出 `render_chrome(state, hw, models, cluster, view_renderable, width, height, theme) -> Group`，把 5 个视图统一包进这套骨架。各视图 `render()` 只负责主区内容，不再自己画顶栏/keybar。

### 2.2 主题扩展

在现有 `theme.py` 三套主题基础上，新增语义键：

| 新增键 | 用途 | dark 示例 |
|---|---|---|
| `accent` | 当前视图标签 / 选中 Tab 高亮 | `bold cyan` |
| `selected_bg` | 表格选中行反白 | `reverse` |
| `search_active` | 搜索模式顶栏高亮 | `bold yellow` |
| `bar_empty` | 显存占比条空块色 | `dim` |
| `bar_fill` | 显存占比条填充色 | `green` |
| `bar_warn` | 显存占比条 ≥60% 色 | `yellow` |
| `bar_over` | 显存占比条 ≥90% 色 | `red` |
| `fit_perfect` / `fit_good` / `fit_marginal` | 未来 fit 色块 | `green` / `yellow` / `red` |

light / high-contrast 同步补齐。

### 2.3 模式指示器

底部状态栏右侧实时显示当前模式：
- `NORMAL`：常规导航
- `SEARCH`：按 `/` 进入，顶栏搜索框高亮，字母键直接入框
- `HELP`：按 `h` 弹层，不改变底层视图

---

## 3. Dashboard 主表

### 3.1 列定义与响应式布局

用 `rich.table.Table` + `Column(width=N, overflow="ellipsis", no_wrap=True)`，列宽按 `display_width` 预设，总宽留 2 列余量兜底。

| 布局 | 触发 | 可见列 |
|---|---|---|
| `full` | width ≥ 120 | NAME / ENGINE / VARIANT / PORT / STATUS / RATE / CTX / TP / QUANT / VRAM |
| `medium` | 100 ≤ width < 120 | NAME / ENGINE / PORT / STATUS / RATE / CTX / TP / VRAM |
| `narrow` | 80 ≤ width < 100 | NAME / ENGINE / PORT / STATUS / VRAM |

### 3.2 选中行反白

Table 构建时设 `row_styles=[""] * page_size`，渲染前把 `state.active_index` 对应行改为 `theme["selected_bg"]`（即 `reverse`）+ `bold`。

### 3.3 搜索高亮

`state.search` 非空时，NAME 列匹配子串用 `theme["accent"]` 高亮（rich `Text.stylize`）。

### 3.4 空态与分页

- 过滤后为空：表格区显示 `(无匹配 profile)`，下方保留 keybar
- 分页：`page_size = max(5, height - 6)`（预留 chrome 行，极端高度下限 5），底部显示 `第 x/y 页`

---

## 4. 数据层修复与扩展

### 4.1 修复 `vram_gib` 恒 0 的 Bug

`ModelsSnapshot.fetch` 不再读 `variants`，改为调用 `vram_estimator.kv_estimate_for_profile(profile)` + 权重目录扫描，把结果缓存进 `ModelsSnapshot.profiles[i]["vram_gib"]`。

### 4.2 新增字段采集

在 `ModelsSnapshot.fetch` 循环里，从 `p.engine_config` 提取：

```python
profiles.append({
    "name": name,
    "engine": engine,
    "variant": variant,
    "port": port,
    "status": status,
    # ── 新增 ──
    "ctx":        _extract_ctx(p.engine_config),       # int | None
    "tp":         _extract_tp(p.engine_config),        # int
    "quant":      _extract_quant(p.engine_config),     # str | ""
    "vram_gib":   _estimate_vram_gib(p, caps),         # float | None（None=不可估算）
    "kv_mb":      _estimate_kv_mb(p),                  # float | None（来自 vram_estimator）
    # ── 保留 ──
    "rate_in": rate_in,
    "rate_out": rate_out,
})
```

### 4.3 估算函数封装

在 `data.py` 内部新增私有 helper（纯函数、不触 I/O 副作用）：

| 函数 | 逻辑 |
|---|---|
| `_extract_ctx(ec)` | `max_model_len` → `ctx_size` → `context_length`，int 转换失败回 None |
| `_extract_tp(ec)` | `tensor_parallel_size` → `tensor_parallel` → `gpu_list.split(',')`，缺省 1 |
| `_extract_quant(ec)` | `quantization` 非空取前 8 字符，否则取 `kv_cache_dtype` |
| `_estimate_vram_gib(p, caps)` | 优先 `kv_estimate_for_profile` 得 `per_card_mb × gpu_count`；其次扫 `MODEL_ROOT/<model>` 目录 `*.safetensors`/`*.bin` 累加；都失败回退 `gpu_memory_utilization × caps.vram_total_mb / 1024` |
| `_estimate_kv_mb(p)` | 直接调 `vram_estimator.kv_estimate_for_profile` 返回 `kv_total_mb` |

### 4.4 VRAM 显示策略

| 场景 | 显示 |
|---|---|
| 有 KV 估算 + 权重扫描 | `▓▓▓░░ 62%`（估算值 / 总显存） |
| 仅 `gpu_memory_utilization` 回退 | `~62%`（加 `~` 前缀表"粗略"） |
| 完全无法估算 | `-` |

### 4.5 权重目录扫描

`_estimate_vram_gib` 里的权重扫描逻辑：
- 路径：`Path(os.environ.get("MODEL_ROOT") or PROJECT_ROOT.parent / "model-hf") / <model 名>`
- 仅扫 `*.safetensors` / `*.bin`，`rglob` 累加 `st_size`
- 目录不存在或为空 → 跳过该路估算

### 4.6 向后兼容

现有测试 mock fixture 可能喂旧格式 dict（无 `ctx/tp/quant`）。`render_dashboard` 用 `profile.get("ctx")` 等安全读取，缺失回退 `-`。

---

## 5. 键盘交互与模式系统

### 5.1 模式状态机

在 `TUIState` 新增：

```python
mode: Literal["normal", "search", "help"] = "normal"
search_input: str = ""          # 搜索框实时文本（与 search 不同：search 是过滤条件）
help_scroll: int = 0            # 帮助弹层滚动位置
```

### 5.2 模式切换

| 当前模式 | 按键 | 目标模式 | 行为 |
|---|---|---|---|
| normal | `/` | search | `search_input = ""`，顶栏搜索框高亮 |
| search | 字母/数字/`-` | search | 追加到 `search_input`，实时过滤 |
| search | Backspace | search | 删最后一个字符 |
| search | Ctrl-U | search | 清空 `search_input` |
| search | Esc / Enter | normal | `search = search_input`，退出搜索模式 |
| normal | `h` / `?` | help | 弹层打开，底层视图冻结 |
| help | Esc / `h` / `?` / `q` | normal | 关闭弹层 |
| help | `j`/`k` / `↑`/`↓` | help | 滚动帮助内容 |

### 5.3 键位接线

**normal 模式（dashboard 视图）**：

| 键 | 动作 | 状态 |
|---|---|---|
| `j`/`↓` | `active_index += 1` | 已有 |
| `k`/`↑` | `active_index -= 1` | 已有 |
| `Enter` | 切 `detail` 视图 | 已有 |
| `f` | `filter_status` 循环：`all → running → stopped → all` | 新增 |
| `s` | `sort_key` 循环：`name → port → rate_out → vram → name` | 新增 |
| `d` | `filter_engine` 循环：`all → <已知引擎列表>` | 新增 |
| `g` / `G` | `active_index = 0` / `active_index = len-1` | 新增 |
| `PgUp`/`PgDn` | `page -= 1` / `page += 1`（越界自动回卷） | 新增 |
| `1`~`5` | 切视图 dashboard/detail/plan/cluster/monitor | 新增 |
| `t` | 切主题（循环） | 已有 |
| `q` | 退出（dashboard 上无确认，子视图 Esc 回 dashboard） | 已有 |

**normal 模式（非 dashboard 视图）**：

| 键 | 动作 |
|---|---|
| `1`~`5` | 直接切视图（任意视图生效） |
| `Esc` | 回 dashboard |
| 其他视图特定键 | 保持现有 detail/plan/cluster/monitor 键位不变 |

### 5.4 搜索实时过滤

`search_input` 每变一字符即触发 `filter_candidates` 重算当前页，`state.search` 在退出搜索模式时才正式写入。Esc 取消则恢复原过滤。

### 5.5 帮助弹层

新增 `panels/help.py`，渲染键位表（用 rich Table），覆盖 `render_chrome` 的主区位置，底层视图保留但不可交互。内容从 `_KEYMAP` 常量生成，避免硬编码字符串。

### 5.6 键位冲突检查

- `d` 仅在 dashboard 视图用作引擎过滤，plan 视图行为不变（Dry-run）
- `1`~`5` 在 plan 视图无冲突（plan 不用数字键）
- `g` 在 monitor 视图是 GPU 轮询开关 → 仅 dashboard 视图新增 `g`/`G`，monitor 保持原语义
- detail 视图 `Tab`/`Shift+Tab` 切子 Tab，与数字键无冲突

---

## 6. Detail / Plan / Cluster / Monitor 四视图改造

### 6.1 共通原则

1. **弃用 `pad_width` 拼行**：所有表格/列表一律 `rich.table.Table`，CJK 宽度由 rich `cell_len` 保证
2. **保留 `pad_width` 的场景**：仅用于顶栏单行 `Text` 的定宽补齐（chrome.py 内部），不再出现在数据表格
3. **Group 返回签名不变**：各视图 `render()` 仍返回 `Group`，只是内部组件全换 rich 原生
4. **`panels/base.py` 的 `keybar()` 骨架**：改为 `Text` + `pad_width` 单行，样式统一 `theme["keybar"]`

### 6.2 Detail 视图

| 位置 | 改为 |
|---|---|
| Tab 头 | rich `Table` 单行 Tab 条，active Tab 用 `theme["accent"]` 高亮，inactive 用 `dim` |
| YAML Tab | `Syntax` 组件保留，包 Panel，边框色随主题 |
| 智能体配置 Tab | rich `Table` 两列（KEY / VALUE），KEY 列 `width=20` |
| 日志 Tab | 直接 `Text` 逐行 append，Panel 自动处理 CJK 宽度 |
| 速率 Tab | 保留单行，加单位 `tok/s` |
| 健康预检 Tab | 保留三态色，Panel 边框色同步 |

**新增**：左侧 YAML / 右侧 Tab 内容的双栏 `rich.columns.Columns` 布局（width ≥ 140 时启用），narrow 时回退上下堆叠。

### 6.3 Plan 视图

| 位置 | 改为 |
|---|---|
| 字段表单 | rich `Table` 两列（字段 / 值），编辑光标行 `reverse` 反白 |
| KV 估算区 | `Panel` 标题"KV 显存估算"，内容 Table |
| 预检结果区 | `Panel` 标题"健康预检"，内容 Table，三态色 |

交互逻辑不变：`Tab`/`Shift+Tab` 切字段、`D` 触发 Dry-run、`Esc` 放弃。

### 6.4 Cluster 视图

| 位置 | 改为 |
|---|---|
| Tab 头 | rich `Table` Tab 条，同 Detail |
| 节点表 | rich `Table`，列宽 `display_width` 预设 |
| Goal stage 链 | 保留 `▓/░` 块字符，包 Panel |
| 事件流 | rich `Table`，级别列（info=青 / warn=黄 / error=红） |

### 6.5 Monitor 视图

| 位置 | 改为 |
|---|---|
| 速率表 | rich `Table`，RATE 列右对齐 |
| GPU 卡片 | `Panel` + `Table`，`util_pct` 列用 `▓/░` 条 |
| 双 Panel 横排 | `rich.columns.Columns` 原生双栏 |

---

## 7. 测试策略

| 测试文件 | 更新点 |
|---|---|
| `test_tui_panel_dashboard.py` | 断言改为 rich `Table` 输出的列名存在、选中行含 `reverse` ANSI |
| `test_tui_panel_detail.py` | 断言 Tab 条 active 高亮存在 |
| `test_tui_data_snapshot.py` | mock `Profile.engine_config` 补 `max_model_len`/`tensor_parallel_size`，断言 `ctx`/`tp`/`quant` 字段 |
| `test_tui_app.py` | 新增 `f`/`s`/`d`/`g`/`G`/`PgUp`/`PgDn`/`1`~`5` 键位断言 |
| 新增 `test_tui_chrome.py` | 断言 chrome 骨架 5 行结构 + 模式指示器 |

---

## 8. 里程碑

| 里程碑 | 产出 | 验收 |
|---|---|---|
| M1 数据层修复 | `data.py` 补采 `ctx/tp/quant/vram_gib/kv_mb`，修 `vram_gib` 恒 0 | `test_tui_data_snapshot.py` 断言新字段 |
| M2 chrome 骨架 | `panels/chrome.py` + 主题扩展 + `test_tui_chrome.py` | 5 行骨架渲染正确，模式指示器切换 |
| M3 Dashboard 重做 | `main_dashboard.py` 换 rich Table，新增列，选中反白，搜索高亮 | `test_tui_panel_dashboard.py` 全绿 |
| M4 键位接线 | `app.py` 接 `f/s/d/g/G/PgUp/PgDn/1-5`，`state.py` 加模式状态机 | `test_tui_app.py` 键位断言全过 |
| M5 Detail/Plan/Cluster/Monitor 改造 | 4 视图内部组件全换 rich 原生 | 各 panel 测试全绿 |
| M6 帮助弹层 | `panels/help.py` + `_KEYMAP` 常量 | 帮助内容覆盖全部键位 |
| M7 回归收尾 | 全量测试 + 80x24 窄屏验证 + README/CHANGELOG | 所有测试通过，无 traceback |

---

## 9. 不做的事

- 不改 Detail/Plan/Cluster/Monitor 的交互逻辑（只换渲染组件）
- 不引入 prompt_toolkit / textual / blessed / curtsies
- 不改 `core/colors.py` 的 `display_width`/`pad_width` 语义
- 不新增写操作（保持只读）
- 不做 GPU 矩阵健康自动验证
