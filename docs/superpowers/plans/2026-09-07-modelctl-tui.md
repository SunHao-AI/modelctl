# modelctl TUI 实施计划

> 日期：2026-09-07 · Spec 日期：2026-09-07
>
> 目标：在 modelctl CLI 上新增 `tui` 子命令，实现 5 视图富文本 TUI（仿 llmfit），底层 rich 库，首版只读。
>
> 分支：feature/tui（从 main 分出）

---

## 全局约束（所有 Task 必须遵守）

- 所有列/单元格禁止 `f"{x:<N}"` 或 `str.ljust`，必须用 `pad_width`（`core/colors.py` 提供）
- 首版 TUI 任何 panel 不得调 `subprocess` / `open('w')` / `os.kill` / 写操作
- 模块结构 `src/modelctl/core/tui/`，入口 `modelctl tui` 子命令
- 跨平台：Windows (msvcrt) + Linux/macOS (tty + select + os.read)
- 时间格式：`YYYY-MM-DD HH:mm:ss`
- CJK 对齐：`display_width` + `pad_width`，全角符号也是 2 列
- 最小终端：80 x 24
- 依赖：`rich>=13.0` 加入 pyproject.toml；pynvml 不在主 deps，Monitor 视图 option import
- 文件头遵循仓库标准（`@File`/`@IDE`/`@Author`/`@Email`/`@Date`/`@Desc`）

---

## Task 0: 脚手架 — core/tui 目录 + 空 TUI 启动器

**Files:**
- `src/modelctl/core/tui/__init__.py` — 导出 TuiApp, TuiState
- `src/modelctl/core/tui/__main__.py` — `python -m modelctl.core.tui` 入口
- `src/modelctl/core/tui/app.py` — TuiApp 空壳骨架（run/loop_begin/loop_end/render_once，Ctrl-C 无 traceback）
- `src/modelctl/core/tui/state.py` — TUIState dataclass（active_view/active_index/plan_edit/filter/sort/search/errors/caches 字段先定义）
- `src/modelctl/core/tui/keyboard.py` — 键盘空壳（read_key_block 方法 + Key 枚举占位，具体实现在 Task 1）
- `src/modelctl/core/tui/theme.py` — RICH_THEME 三套主题 + 切换函数
- `src/modelctl/core/tui/panels/__init__.py` — 空
- `src/modelctl/core/tui/panels/base.py` — 公共 helper（section_title/error_flash/keybar 骨架）
- `src/modelctl/cli.py` — 新增 `sub.add_parser("tui", ...)` + `_cmd_tui` handler
- `pyproject.toml` — 增加 `rich>=13.0` 依赖

**Commits:**
1. `feat(tui): scaffold core/tui module with empty TuiApp skeleton`
2. `feat(tui): add modelctl tui subcommand and rich dependency`

**TDD 步骤:**

1. **Write failing test** `tests/test_tui_app.py`:
   - 测试 TuiApp 可用 mock console 构造
   - 测试 `run(smoke=True)` 执行 3 步 key 序列后干净退出
   - 测试 TUIState 各字段初始值正确
2. **Run fails** — `uv run pytest tests/test_tui_app.py -x -q` 预期 ImportError
3. **实现** `__init__.py` / `__main__.py` / `state.py` / `app.py` / `keyboard.py` / `theme.py` / `panels/__init__.py` / `panels/base.py`
4. **修改** `pyproject.toml` 加 `rich>=13.0`
5. **修改** `cli.py` 加 `list.add_parser("tui", ...)` + `_cmd_tui` handler（调 load_env → probe → TuiApp.run）
6. **Run passes** — 全部测试绿
7. **手动验证** — `uv run modelctl tui --smoke-test` 起 TUI 后按 q 退出，无 traceback
8. **Commit**

---

## Task 1: 跨平台 keyboard.py + Key 枚举

**Files:**
- `src/modelctl/core/tui/keyboard.py` — 完整实现
- `tests/test_tui_keyboard.py` — 键盘事件字节流 → Key 枚举映射测试

**Commits:**
1. `feat(tui): implement cross-platform keyboard handler with Key enum`

**TDD 步骤:**

1. **Write failing test** `tests/test_tui_keyboard.py`:
   - Windows 路径：mock `msvcrt.getwch()` / `msvcrt.getch()` 返回单字符，断言 Key 枚举
   - Linux 路径：mock `os.read(stdin, 32)` 返回上/下/Tab/Ctrl-R/ESC 字节序列，断言 Key 枚举
   - ANSI escape 序列：`\x1b[A` → Up / `\x1b[B` → Down / `\x1b[1;5D` → Shift-Tab 等
   - `read_key_block(timeout)` 超时返回 None
2. **Run fails** — `uv run pytest tests/test_tui_keyboard.py -x -q`
3. **实现** `keyboard.py`：
   - `Key` Enum：Up/Down/Left/Right/Enter/Esc/CtrlR/CtrlU/CtrlF/CtrlB/Digit1..5/T/H/Q/D/F/S/J/K/G/H_He/Colon/QuestionMark 等
   - `KeyboardInput` 类：`__init__(console)`，`read_key_block(timeout: float) -> Key | None`，退出时 `restore_term()`
   - Windows：`msvcrt.kbhit()` 轮询 + `getwch()`/`getch()`，组合键 via ASCII 代码（0x12=Ctrl-R 等）
   - Linux/macOS：`tty.setraw(stdin)` + `select.select` + `os.read`，扫描 ANSI 序列
   - 平台自动检测：`sys.platform == "win32"` 分支
4. **Run passes**
5. **Commit**

---

## Task 2: Snapshot 数据层 + Dashboard 面板

**Files:**
- `src/modelctl/core/tui/data.py` — 5 种 Snapshot 类（Hardware/Models/Logs/Cluster/Monitor），TTL 过期重采
- `src/modelctl/core/tui/panels/main_dashboard.py` — Dashboard 视图渲染
- `src/modelctl/core/tui/state.py` — 追加 filter/sort/search/page 方法
- `tests/test_tui_data_snapshot.py`
- `tests/test_tui_panel_dashboard.py`

**Commits:**
1. `feat(tui): implement 5 solver snapshot classes with TTL caching`
2. `feat(tui): implement Dashboard panel with CJK alignment and key handlers`

**TDD 步骤:**

1. **Write failing test** `tests/test_tui_data_snapshot.py`:
   - mock `probe()` / `list_profiles()` / `_stats_token_rate()` 返回 fixture
   - 断言 HardwareSnapshot / ModelsSnapshot 字段正确
   - TTL 测试：fresh cache 返回同对象，过期后重采
2. **Write failing test** `tests/test_tui_panel_dashboard.py`:
   - mock Caps + profiles 2 项（1 运行中 1 已停止），Console(record=True) 捕获渲染
   - 断言 "运行中" 文本出现 + CJK 对齐：每行 `display_width` 相等
   - 过滤/排序/搜索：按 engine "vllm" 过滤后只留 1 项
3. **Run fails**
4. **实现** `data.py`：
   - `HardwareSnapshot`：调 `capabilities.probe()`，TTL=60s
   - `ModelsSnapshot`：调 `list_profiles()` + 逐项 `_instance_state` + `_stats_token_rate`，TTL=8s
   - `LogsSnapshot`：`Path.read_lines(launch_log(name), -20)`，TTL=1s
   - `ClusterSnapshot`：调 `_cluster_aggregate()` + `center_probe.get_json('/cluster/events', limit=50)`，TTL=30s
   - `MonitorSnapshot`：调 `_stats_token_rate()` 全 profile 遍历，TTL=5s；可选 pynvml option import
   - 每类带 `_fresh` / `_fetched_at` 字段 + `is_expired() -> bool`
5. **实现** `panels/main_dashboard.py`：
   - `render(state: TUIState, cap_width: int) -> rich.renderable.Group`
   - 顶部硬件条：`GPU {N}x {name} | 自由显存 {free}/{total}G | 模型 {run}/{total} 运行 | 集群: {n} 节点 OK`
   - 中部 profile 表格（pad_width 对齐）：标识符/引擎/变体/端口/状态（带徽）/速率/显存占比
   - 底部 keybar：`q 退 | / 搜 | f 状态 | s 排序 | d 引擎 | j/k 翻页 | Enter 详情 | PgUp/PgDn 翻页`
6. **追加** `state.py` 方法：`filter_by_engine/` `filter_by_status/` `sort_by_key/` `search/` `apply_page`
7. **Run passes**
8. **Commit**

---

## Task 3: Detail 视图 5 子 Tab

**Files:**
- `src/modelctl/core/tui/panels/detail.py` — Detail 视图（YAML/agent config/log tail/rate/precheck 5 子 Tab）
- `src/modelctl/core/tui/state.py` — 追加 `active_detail_subtab` 字段 + `switch_detail_tab` 方法
- `tests/test_tui_panel_detail.py`

**Commits:**
1. `feat(tui): implement Detail view with 5 sub-tabs (YAML/agent/log/rate/precheck)`

**TDD 步骤:**

1. **Write failing test** `tests/test_tui_panel_detail.py`:
   - 临时 profile 目录 + name.yaml，mock 日志文件，断言 5 子 Tab 切换后 content 不同
   - YAML tab：断言文件内容 substring 出现
   - log tab：断言 "no log" 或最后 20 行
   - rate tab：mock `_stats_token_rate` 返回数值，断言速率数字出现
2. **Run fails**
3. **实现** `panels/detail.py`：
   - 左栏：`rich.Syntax(yaml_text, "yaml")`（文件缺失显示红 Panel "未找到 profile"）
   - 中栏：agent_config 字段表（writer_class/user_class/model_path 等，pad_width 对齐）
   - 右栏 Tab 切换：
     - Tab 0 "YAML"
     - Tab 1 "智能体配置"
     - Tab 2 "日志"（1s 轮询读 launch_log 末 20 行，局部重绘）
     - Tab 3 "速率"（_stats_token_rate，每 2s 刷）
     - Tab 4 "健康预检"（调 adapter.check_requirements，捕获 RequirementError → 显示异常消息列表）
   - 键盘：Tab/Shift-Tab 切 Tab；Esc 回 Dashboard；日志内 up/down 20 行、Ctrl-F/Ctrl-B 翻、r 切换跟踪
4. **Run passes**
5. **Commit**

---

## Task 4: Plan Mode（dry-run 可视化）

**Files:**
- `src/modelctl/core/tui/panels/plan.py` — Plan 视图（字段表单 + dry-run 3 块输出）
- `src/modelctl/core/tui/state.py` — 追加 `PlanEdit` dataclass + `plan_edit` 字段
- `tests/test_tui_panel_plan.py`

**Commits:**
1. `feat(tui): implement Plan mode with dry-run visualization`

**TDD 步骤:**

1. **Write failing test** `tests/test_tui_panel_plan.py`:
   - 构造 fake Profile（engine=vllm, engine_config 含 ctx_size/gpu_list）
   - 修改 2 字段后 Dry-run，断言 vram_estimator 返回值出现在输出
   - 断言 check_requirements 异常被捕获并显示
2. **Run fails**
3. **实现** `panels/plan.py`：
   - 从 `profile.engine_config` 动态拉字段集合（按 engine 类型不同字段：vllm 有 ctx_size/gpu_list/tensor_parallel_size；llamacpp 有 ctx_size/n_gpu_layers；等）
   - `PlanEdit`：`dataclass` 含 `fields: dict[str, str]`，`set_field(name, val)` / `get_all()`
   - 渲染：上半区字段表单（当前值 + 可编辑光标），下半区 3 块：
     - 硬件资源预览（GPU 间容量 / client 总预算）
     - vram_estimate 估算值（调 vram_estimator.kv_estimate_for_profile）
     - engine check_requirements 预检（调 get_adapter(profile).check_requirements()，捕获异常）
   - 键盘：Tab/up/down 切字段；键入文本修改；Ctrl-U 清空；D Dry-run；Esc 放弃
   - 首版 `[Save Y]` 按钮显示但 disabled（"写操作需第二版"）
4. **Run passes**
5. **Commit**

---

## Task 5: Cluster 3 Section + Monitor 速率表 + GPU 卡片

**Files:**
- `src/modelctl/core/tui/panels/cluster.py` — Cluster 视图（节点表/goal 表/事件流 3 Section + 顶部摘要条）
- `src/modelctl/core/tui/panels/monitor.py` — Monitor 视图（速率表 + GPU 卡片）
- `tests/test_tui_panel_cluster.py`
- `tests/test_tui_panel_monitor.py`

**Commits:**
1. `feat(tui): implement Cluster view with 3 sections and gap fallback`
2. `feat(tui): implement Monitor view with rate table and GPU cards`

**TDD 步骤:**

1. **Write failing test** `tests/test_tui_panel_cluster.py`:
   - solo role：ClusterSnapshot 显示 Stub "中心未接入（solo role）"
   - worker + mock center 数据：断言 3 section 渲染 + 节点数正确
2. **Write failing test** `tests/test_tui_panel_monitor.py`:
   - 无 GPU：Monitor GPU 卡片纯 Panel "无 GPU 可用设备"
   - mock token rate 数据：断言速率表行内容
3. **Run fails**
4. **实现** `panels/cluster.py`：
   - 顶部摘要条：`角色: {role} | 节点: {total} ({online} 在线/{offline} 离线) | center: {url[:12]}…`
   - Tab 0 "节点"：节点表（node_id/ip/状态徽/托管 profile 数），pad_width 对齐
   - Tab 1 "goal"：goal 表（profile/stage chain 进度块/节点数），stage 链用 `[>]validate ✓ →[>]env ✓ →[>]model…` 色块
   - Tab 2 "事件"：最近 50 条（时间/级别色/model/nodes/path），info=青/warn=黄/error=红
   - SoloStub：顶部红 Panel "中心未接入（solo role）" + 引导命令模型提示
5. **实现** `panels/monitor.py`：
   - 左半（或上半 < 120 列）速率表：每 profile 一行（name/input/s/predicted/s/ttft/进度块），pad_width 对齐
   - 右半（或下半）GPU 卡片：
     - 有 pynvml：`nvmlDeviceUtilization*` 4s 轮询，每卡显示 (GPU N: util%, mem_used/mem_total)
     - 无 pynvml：复用 `_safe_smi()` 4s 轮询 output 解析
     - 无 GPU：纯 Panel "无 GPU 可用设备"
   - 键盘：r 强刷速率表；g 启/停 GPU 轮询；Esc 回 Dashboard
6. **Run passes**
7. **Commit**

---

## Task 6: 主题持久化 + 窄屏适配 + no_side_effects 护栏 + 文档

**Files:**
- `src/modelctl/core/tui/app.py` — 追加主题持久化（read/write `data/cache/tui.theme`）
- `src/modelctl/core/tui/theme.py` — 3 主题完整 patch
- `src/modelctl/core/tui/panels/main_dashboard.py` — 80 列窄屏适配（主表格压缩到 2 列）
- `tests/test_tui_no_side_effects.py` — 遍历 5 panel，mock subprocess.Popen + open mode "w"，断言 count=0
- `tests/test_tui_app.py` — 追加 theme persist test
- `docs/known-pitfalls/README.md` — 加入 TUI 已知坑记录
- `src/modelctl/pyproject.toml` — 确保 rich 在 kwargs 不会触发错误（依赖 + monkeypatch 检查）

**Commits:**
1. `feat(tui): implement theme persistence and 80-column narrow-screen adaptation`
2. `test(tui): add no_side_effects guardrail test for all 5 panels`
3. `docs(tui): add known-pitfalls entry for display_width CJK alignment`

**TDD 步骤:**

1. **Write failing test** `tests/test_tui_no_side_effects.py`:
   - 遍历 5 panel（dashboard/detail/plan/cluster/monitor），mock `subprocess.Popen` + `open` mode "w"
   - 断言 5 panel render 过程中 Popen.call count=0，open mode "w" count=0
   - 断言 TuiApp.run 主循环不触发任何 subprocess call
2. **Write failing test** theme 持久化：
   - 设 theme = "light"，退出，重进，断言 theme == "light"
   - 损坏文件：assert fallback to "dark"
3. **Run fails**
4. **实现** 主题持久化：
   - `data/cache/tui.theme`（mkdir parents=True exist_ok=True）
   - 读：`json.loads` 失败 → 默认 "dark"
   - 写：TUI 退出时写
5. **实现** 窄屏适配（main_dashboard）：
   - `cap_width < 100`：隐藏变体列 + 速率列压缩为 `3.2k/↓210` 单列
   - `cap_width < 80`：主表格压到 2 列（标识符 + 状态），其余 key 键帮键栏压行
6. **写** `docs/known-pitfalls/README.md` 加入 TUI CJK 对齐已知坑
7. **Run passes**
8. **Commit**

---

## Final Review（全分支）

所有 Task 完成后 dispatch 宽 scope code reviewer（最强模型），review 整个 `feature/tui` 分支 vs main。

---

## 手动回归清单

- [ ] `uv run modelctl tui` → 按 1-5 切视图 → 所有视图无 traceback
- [ ] `uv run modelctl tui --smoke-test` → 自动 5 步退出，无 traceback
- [ ] 80x24 终端窗口：无字段缺失
- [ ] 终端 200x60 窗口：所有列完整
- [ ] `uv run modelctl --help`：tui 子命令出现
- [ ] `uv run modelctl status`（原有命令）：行为不变
- [ ] Windows PowerShell 5.1 + Linux WSL 2 双端测试通过
