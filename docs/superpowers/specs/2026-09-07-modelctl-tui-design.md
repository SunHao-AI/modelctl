# modelctl TUI 设计（仿 llmfit 风格交互式终端）

> 日期：2026-09-07 · 状态：草案 → 待用户 review → writing-plans 起实现计划
>
> 概览：在 modelctl CLI 上新增 tui 子命令，启动 5 视图富文本 TUI（Dashboard / Detail / Plan / Cluster / Monitor），复用现有 core/ 全部查询与采集函数；首版只读，底层 rich 库，Windows + Linux 双端运行。

---

## 1. 背景与目标

### 1.1 背景

- 当前 modelctl 已是一个完整的 LLM 模型部署启动器：14+ 子命令 (start / stop / restart / status / list / probe 等)、9 引擎适配器、9 角色 (gateway/center/worker/cluster)、WebUI (Vue 3 + Element Plus 5)、集群管理面。
- 现有体验：命令行单行命令 + WebUI 看运行状态。运维体验双向割裂：单条启动要找参数翻 --help、运行状态要切到 WebUI 或 modelctl status 表格。
- llmfit（AlexsJones/llmfit）是 2026 年开源的"实体 GPU 智能 LLM 启动器"：单个交互终端窗口打开，模型列表按硬件 fit 评分排序，j/k 翻页 + Enter 详情 + d 下载 + p 规划 + / 搜索 + f/s 过滤排序，一个命令即可入手。该交互风格契合本项目需求。

### 1.2 目标

- 单命令入手：modelctl tui 即起 TUI，覆盖日常最高频的 5 类操作
- 只读优先：首版 5 视图全部只读，零副作用；写操作走已验证的既有 handler 放到第二版（缩小首版误操作的爆炸半径）
- 与 CLI 同源：TUI 调用与现有 handler 同一套函数（_cmd_lookup / _cmd_status / _cluster_aggregate / all_service / center_probe 等），保证 audit / pid 日志 / CJK 宽度 / 时间格式化 100% 一致
- 跨平台：Windows 10/11 (PowerShell ANSI 2.4+) + Linux 部署机；最小终端 80 x 24

### 1.3 非目标

- 不替代 WebUI（WebUI 保留作为浏览器侧富交互面板，TUI 是终端侧"快速巡检 + 操作"入口）
- 不重写现有逻辑：所有 probe / list / start / stop / cluster 逻辑都通过现有 core/ 模块函数复用，TUI 层只负责"读快照 + 渲染 + 派发用户事件"
- 不新增异步框架 / SSE / WebSocket：所有数据来自现有采集函数 + 定时轮询（Dashboard/Monitor 30s，日志 1s）

---

## 2. 顶层架构

### 2.1 库层与技术选型

| 维度 | 决策 | 理由 |
|---|---|---|
| 渲染库 | rich（纯 Python，BSD 许可） | Rich 提供 Table / Panel / Syntax / Layout / Console / Progress 全套，本需求（表格 + 高亮面板 + 主题 + 布局）全部覆盖；与现有 core/colors.py 的 ANSI 颜色规范可对齐（统一走 Style 描述符） |
| 输入处理 | stdlib select / msvcrt 自写 keyboard.py（约 200 行） | 不引入 prompt_toolkit / curtsies / blessed：保持依赖最小克制（rich 是唯一新增外部依赖），规避 prompt_toolkit 与 Rich 终端状态机冲突，避免 Windows 下 msvcrt 多键码顺序偶发漂移的复杂度 |
| 主题 | rich.theme.Theme 三套（dark / light / high-contrast） | 与 core/colors.py 颜色常量语义词汇一一对齐；主题 ID 字符串持久化到 data/cache/tui.theme，TUI 启动时读、退出时写 |
| GPU 图表 | 不引入 matplotlib，用 Rich Panel + Table 的纯色块（进度条） | 终端 chart 像素痛点重且依赖冗；Bar + Table 的进度块是 htop / bat / cc-switch 等 TUI 通用做法 |
| 日志实时化 | Path.read_lines 轮询（默认 1s，跟踪尾时 0.5s） | 不引入 SSE 客户端：launch-*.log 是纯文本流，末 20 行已足够；SSE 仅在第二版启动流式日志时复用 |

### 2.2 分层模型

cli.py 的 _cmd_tui handler：
1) load_env()  与现有 CLI 入口一致
2) all_service()  与 _cmd_status 入口一致
3) probe()  与 _cmd_status 入口一致
4) TuiApp(caps).run()  进入主循环

TuiApp 主类（core/tui/app.py）：
- state: TUIState  全局可变态
- console: rich.console.Console  渲染目标
- panels: dict[str, Callable]  视图工厂
- keyboard_loop()  键盘事件 → state
- render_once()  state → Rich Renderable
- schedule_refresh()  视图无关的 30s/1s 刷新任务

panels（core/tui/panels/*.py）：
- render(state, cap_width) -> Group  纯函数（无副作用）

data（core/tui/data.py，只读采集器）：
- HardwareSnapshot   = probe() 已有字段
- ModelsSnapshot     = list_profiles + _instance_state + _stats_token_rate
- LogsSnapshot       = Path.read_lines(launch_log(name), 20)
- ClusterSnapshot    = _cluster_aggregate() + events 列表
- MonitorSnapshot    = _stats_token_rate() + 可选 pynvml

core/（现有，未改动）：
- probe / all_service / center_probe / vram_estimator
- profile_config / stats / ui / envs / cluster / constants

### 2.3 主循环伪代码

class TuiApp:
    def run(self):
        self.loop_begin()
        while True:
            ev = self.read_key_block(timeout=2.0)   # 阻塞 2s 内，期间定时任务可触发
            if ev is EXIT:
                self.loop_end(); return
            self.dispatch(ev)                       # 改变 state
            self.schedule_refresh()                 # 按 view + key 决定是否需要重采集
            self.render_once()
        # 第二版：再开一个 Thread 做日志周期刷新，仅刷新 logs 区，不重绘整屏

- 键盘阻塞上限 2s：期间定时任务可触发（写入 state.pending_refresh），键盘事件插入时优先响应用户输入
- 重绘策略：3 种渲染模式：全重绘（视图切换 / Ctrl-R / 定时刷新）/ 日志区局部重绘（Detail-Log 子页 1s 轮询，仅该 Layout 子块）/ 帮助静音（Help 视图不自动刷新）。第二版决策时引入 Layout 组件可加区域细粒度补刷，首版不做

---

## 3. 目录与模块

### 3.1 文件树

src/modelctl/core/tui/
- __init__.py           # 导出 TuiApp, TuiState
- __main__.py           # python -m modelctl.core.tui 直接跑（便于无 CLI 实测）
- app.py                # TuiApp（主循环 + 主题 + 持久化 + 状态持久化）
- keyboard.py           # 键盘钩：select/msvcrt 跨平台封装 + 键事件 → 枚举
- theme.py              # RICH_THEME 字典 + 主题 ID 切换函数
- state.py              # TUIState、ProfileTree、过滤/排序/搜索三层（纯数据，不渲染）
- data.py               # 5 种 Snapshot 类：Hardware / Models / Logs / Cluster / Monitor
- panels/
  - __init__.py
  - base.py             # 公共 helper：section 标题条 / 状态徽 / 主题条 / keybar
  - main_dashboard.py   # 视图 1（默认）
  - detail.py           # 视图 2
  - plan.py             # 视图 3
  - cluster.py          # 视图 4
  - monitor.py          # 视图 5
  - help.py             # 键位说明（全局弹窗）

### 3.2 入口接入

- src/modelctl/cli.py 新增子命令：

  sub.add_parser("tui", help="启动富文本 TUI（仿 llmfit 风格）").set_defaults(handler=_cmd_tui)

- handler _cmd_tui(base)：
  - 与现有 handler 同入口流程：load_env() → all_service() → probe() → list_profiles(models_dir) → TuiApp(...).run()
  - 失败分支：probe() 抛 ProbingError 时用 Rich 红色 Panel 展示（不 crash）
  - 用 try/except KeyboardInterrupt 与 except ProbingError 两层保护，Ctrl-C 退出时无 traceback

### 3.3 依赖

- pyproject.toml 主项目 deps 增 rich>=13.0（现有 deps 已含 uv / fastapi / httpx，rich 是无传递依赖的纯渲染库）
- pynvml 不进主 deps：Monitor 视图直接 try: import pynvml 内部 option import；未装时用 nvidia-smi --query-gpu ... -l 1 子进程 4s 轮询 fallback（现有 _cmd_probe 已有 nvidia-smi 调用逻辑可复用）

### 3.4 测试

- 单元：tests/test_tui_*.py
  - test_tui_app.py：TuiApp 在 mock console 下按 1→2→1 视图切后 state 状态正确
  - test_tui_state.py：ProfileTree 三层过滤/排序/搜索组合断言
  - test_tui_panel_dashboard.py：Console(record=True) 捕获渲染文本，断言 "运行中" 含深绿码；CJK 对齐：含中文 state 跑后断言每行 display_width 相等
  - test_tui_data_snapshot.py：mock probe() / list_profiles() / _cluster_request 返回 fixture，断言 Snapshot 字段
  - test_tui_keyboard.py：mock stdin 喂入 j / k / Enter / q 字节，断言 state.frame 转移
- 集成（可选）：起本地临时 profiles 目录 + mock probe + uv run modelctl tui --smoke-test（隐藏 flag，自动 5 步 key 序列后退出，输出 ANSI 码到 stdout 供 CI 截图）
- 不写 snapshot 测试：避免 Rich 不同版本 ANSI 码漂移导致 CI 抖动；改断言"关键词 + 样式字符序列"

---

## 4. 5 视图详细设计

### 4.1 数据刷新策略

每个 Snapshot 带 TTL，过期才重采，未过期返回 cache（避免每次 key 触发全量 API）：

| Snapshot | TTL | 数据来源 |
|---|---|---|
| Hardware | 60 s | probe() 已有 core/constants._MEM_COLS 等正常字段 |
| Models | 8 s（Dashboard/Monitor）；Detail 子页直读 name.yaml 不 cache | list_profiles + _instance_state + _stats_token_rate |
| Logs | 1 s（仅 Detail 视图 - 日志子页） | data/logs/launch-name.log 末 20 行 |
| Cluster | 30 s（Cluster 视图） | _cluster_aggregate() + center_probe.get_json('cluster/events?limit=50') |
| Monitor | 5 s（Monitor 视图） | _stats_token_rate() + 可选 pynvml 4 s 轮询 |

用户按 Ctrl-R 时把对应 Snapshot 的 TTL 临时置 0 强制重采一次。

### 4.2 视图 1: Dashboard（默认视图）

语义：
- 顶部 1 行硬件概览条：GPU 4x RTX 5880 48GB | 自由显存 336/384G | 模型 12/35 运行 | 集群: 7 节点 OK 在线
- 主区：列表带状态徽、速率、当前选层大纲上下文；filter / sort / search 叠加作用
- 底部 1 行操作栏：键说明 + 当前上下文提示

Layout 骨架（以 120 x 40 为例，简化显示）：

  modelctl TUI - Dashboard | Detail | Plan | Cluster | Monitor | Help - [dark]
  GPU 4x RTX 5880 48GB   自由显存 336/384G   模型 12/35 运行   集群: 7 节点 OK
  搜索: (none)   过滤: 全部   排序: name   引擎: -   选中: deepseek-v4-flash (10 配置)
  vllm 配置
  标识符                     引擎    变体  端口   状态       速率(入/出)  显存占比
  deepseek-v4-flash          vllm    -     8100 运行中     3.2k/210       36.8/48.0
  deepseek-v4-flash-vllm-high vllm   high  8103 已停止     -              -
  minimax-m3-7               vllm    -     8300 已停止     -              -
  deepseek-v4-flash-sglang   sglang  -     8200 运行中     1.8k/195       38/48
  q 退   / 搜   f 状态   s 排序   d 切引擎   g/G 顶/底   PgUp/PgDn 翻页   h 帮助   t 主题

渲染契约：
- 全部列名/单元格禁止直接使用 f"{x:<N}" 或 str.ljust，必须 pad_width(x, width)（Rich Table 的 justify 参数对 CJK 不算准确宽度）
- 状态徽：实心圆点 + 前景色（运行中）/ 空心圆 + 灰色（已停止）/ 警告 + 黄色（PID 残留）/ 叉 + 红色（failed）
- 速率列：input/s 与 predicted/s 单值超 6 位千分位分隔，空状态用 -
- 超长 name：在终端最小 80 列不丢；优先 … 截尾，120 列全露；超出仍超则用 Rich 自带 overflow="ellipsis"

### 4.3 视图 2: Detail

语义：3 区布局
- 左 1/3：YAML 原文（rich.Syntax，yaml 高亮）
- 中 1/3：参数字段聚合（Vram 估算、智能体参、health 预检结果、agent_config 尺寸/采样/版本字段表）
- 右 2/3：tab 切换（YAML / 智能体配置参考 / 日志 / 速率 / 健康预检）

字段来源：
- YAML：<models_dir>/…/<name>.yaml 直接 read_text，失败显示"未找到 profile"红 Panel
- 智能体配置参考：现有 _agent_config_info
- 速率：_stats_token_rate（每 2s 重新拉，仅当该 tab active）
- 健康预检：engine_config (check_vla_model, writer_class, user_class, …) → [] 或异常 str 列表
- 日志：data/logs/launch-<name>.log 末 20 行，1 s 轮询 + 局部重绘；无文件显示 (no log)

键盘：
- Tab/Shift-Tab：右侧 Tab 互切；Esc：回 Dashboard
- 日志子页内：up/down 跳 20 行；Ctrl-F/Ctrl-B 页翻；r 切换跟踪尾（默认 on）

### 4.4 视图 3: Plan mode

语义：参照 llmfit 的 Plan mode——选 profile 后当前值 与 用户拟值 二栏对照，下方"硬件资源 + VRAM 估算 + 健康预检"3 区，给运维一个"换 ctx_size / gpu_list 能不能跑"的可视化执行器。

输入字段（可编辑集合，与现有 models/<engine>/*.yaml 的 launch_args 共用键名）：

| 字段 | 类型 | 默认值取值 |
|---|---|---|
| ctx_size | int | 现有 ctx_size（或 engine_config 默认） |
| gpu_list | list[int] | 现有 gpu_list（缺省取全部） |
| tensor_parallel_size 或 engine-specific 的 vllm tensor_parall... / ollama n_gpu_layers / trtllm gpus_per_worker | int/str | engine_config.get(...) |
| max_model_len | int | 引擎特异性 |
| quant | str (q4_k_m/q8_0/k_raw etc.) | - |
| batch_size | int | - |

字段集合按 engine 动态拉：engine_config.get_editable_fields() 返回 [key, type, default, required, hint]，Plan 视图只做"表单渲染 + 用户输入 + 校验"，不直接写 YAML——dry-run 只输出：
- 硬件资源预览（GPU 间容量、client 总预算等）
- vram_estimate(profile, caps) 估算值
- engine_config.check_requirement() 报告列表（"OK: CUDA13 + 48G memory; FAIL: 缺失 apt::xxx"）
- 首版显式拒绝任何写操作——右下角按钮区：[Save Y]（第二版） / [Dry-run D] / [Esc 放弃]

键盘：
- Tab/Shift-Tab 或 up/down：当前字段切换；键入文本 / left/right 改字段
- Ctrl-U 清空；? 字段帮助
- D Dry-run：重出 <field>_arg + 当前 profile 重算 vram_estimator + 调 engine_config.check_requirement precheck 列表，结果区实时露
- Esc 放弃（无二次确认）

### 4.5 视图 4: Cluster

语义：3 个子 Tab + 顶部摘要条
- 摘要条：角色（solo/center/worker）、节点合计、在线/离线计数、center_url（截断后 display_width 限 12）
- Tab 1 节点表：modelctl cluster nodes 等价列 + 状态徽（在线 / 离线 / 半衰）
- Tab 2 goal 表：modelctl cluster goals 等价 + stage chain（validate ok → env ok → model fetch → …），用进度块 表示 stage 链
- Tab 3 事件流：列最近 50 条 cluster event（时间/级别/型号/nodes/path/star），级别用颜色（info=青 / warn=黄 / error=红）

数据源（全只读）：
- cluster_aggregate_data()（现有，solo 角色下返回 placeholder）
- center_probe.get_json('/cluster/events', params={'limit':50}) 只读
- 失败 / worker 未接入 center：Stub → 顶部摘要 "中心未接入（solo role）" + 引导命令 modelctl cluster init --center-url ...

### 4.6 视图 5: Monitor

语义：2 区并排（>= 120 列）或上下（< 120 列）布局
- 速率表：所有运行中 profile 的 input/s / predicted/s / mean_ttft（复用 _stats_token_rate），加最近 5 s 滚动条进度块（基于速率压缩至 8 块，纯 Rich 字符串）
- GPU 卡片：装了 pynvml 用 nvmlDeviceUtilization* 4 s 轮询；否则 fallback nvidia-smi --query-gpu=... -l 1 4 s 轮询；没 GPU 时整面板显示 (无 GPU) 并显示机型

行为约束：
- pynvml 在 Windows/Linux 用 import pynvml（同包名），不在主 deps 里声明，只在用户主动按 g 启轮询时第一次 import（避免 30 ms import 拖 Monitor 启动）
- 默认 5 s 刷新率表；r 键强制刷新速率表；g 切换 "GPU 卡片开/关" 轮询（避免无 GPU 环境长 CPU 打满）

### 4.7 公共 Part（所有视图共享）

- keybar（底部）：每视图自定文案，长度 <= width - 4；用 pad_width 对齐（CJK 安全）
- top 条：modelctl TUI - 视图标签条 - 主题 [dark] [q 退] [h 帮助]
- error flash：任何 Snapshot 重采抛异常时不 crash，在 top 条下插一行红色 "失败：xxxxxx（Ctrl-R 重试）"，下轮成功后自动清除
- 退出确认：按 q 弹 "确认退出？y/n"（按 n 返回），1 s 内连续按 2 次 q 即退（避免误中）

---

## 5. 键盘操作总表

### 5.1 全局键（所有视图生效）

| 键 | 动作 |
|---|---|
| 1/2/3/4/5 | 切视图（Dashboard / Detail / Plan / Cluster / Monitor） |
| h / ? | 帮助面板（叠加弹层，不切换视图） |
| t | 主题循环 dark → light → high-contrast → dark |
| Ctrl-R | 当前视图 Snapshot 强制重采一次 |
| q / Esc | 退 TUI（顶层 q 含确认，带二次按压；子页 Esc 见 5.2） |

### 5.2 视图键

Dashboard（视图 1）
- j / down 与 k / up：上下移光棒
- /：Enter 搜索 mode（顶部搜框点亮，Esc 退）
- f：状态过滤循环：全部 / 运行中 / 已停止 / PID 残留 / failed
- s：排序循环：name / engine / speed(出) / 状态
- d：引擎过滤循环：all / vllm / llamacpp / sglang / ollama / unsloth / trtllm / tensor_aphrodite / lmdeploy / tensorspeed
- Enter：进入 Detail 视图（View 2）
- PgUp/PgDn：上下页（page 大小 = terminal height - 4 行）
- g / G：顶 / 底

Detail（视图 2）
- Tab / Shift-Tab：切右侧 5 个子 Tab（YAML / 智能体 / 日志 / 速率 / 健康预检）
- left / right 或 n / p：同名 profile 下上一组 / 下一组
- Esc：回 Dashboard
- 日志子页内：up/down 20 行；Ctrl-F/Ctrl-B 页翻；r 切换跟踪尾（默认 on）

Plan（视图 3）
- Tab / up / down：字段切换
- 键入文本 / left / right：修字段
- Ctrl-U：清空字段
- ?：字段帮助
- D：Dry-run
- Esc：放弃

Cluster（视图 4）
- Tab / up / down：切 section（节点 / goal / 事件流 / 摘要）
- j / k：行内
- Enter：展开行 detail
- Esc：回 Dashboard

Monitor（视图 5）
- r：强刷速率表
- g：启/停 GPU 卡 4 s 轮询
- Esc：回 Dashboard

### 5.3 跨平台键处理

- Windows：msvcrt.getwch() 单字符 + msvcrt.kbhit() 轮询；Enter 是 \r，Esc 直接；Ctrl-R 等组合键走 msvcrt.getch() ASCII 0x12 直接匹配
- Linux / macOS：tty.setraw(stdin) + select.select([stdin], [], [], timeout) + os.read(stdin, 32)，扫描 ANSI escape seq
- 统一：所有键事件归一化为 Key 枚举（Up / Down / Left / Right / Enter / Esc / CtrlR / CtrlU / CtrlF / CtrlB / Digit1..5 / T / H / Q / D 等），输入层只抛 enum
- 按下 q 退出时先 termios 恢复 raw mode，避免 exit 后输入仍卡

### 5.4 最低终端要求

- 最小 80 x 24：80 列时主 layout 自动压到 2 列；24 行时 keybar 信息条压一行 + 1 主题条 + 内容
- 低于 80 x 24：TUI 起前检查，打印 Rich 红面板 "Terminal too small (80x24 required)" 并返回 0

---

## 6. 数据流与只读契约

### 6.1 读路径

TUIState 包含：
- caches（5 类 Snapshot 缓存 + TTL）
- active_index（profile 在 profiles 列表内原始 index）
- active_detail_subtab
- plan_edit（dict[field → value_str]）
- filter / sort / search
- errors（list[str] 最近 flash 错误）

### 6.2 写路径（首版禁止）

- 任何 panel.render 不得调用 subprocess / open('w') / os.kill / engine_config.launch
- 任何 key handler 不得调 all_service.start_profile / stop_profile / restart_profile / cluster_request_* 写操作
- 测试护栏：tests/test_tui_no_side_effects.py 在测试中 mock sys.modules['subprocess'] 监控调用 + 检查 open 的 mode 参数，任何写操作直接 AssertionError
- 第二版 modelctl tui-write 子命令独立入口（不在本 spec 范围内）

### 6.3 权限与角色要求

- 不需要任何 cluster role：TUI 任意模式下可启；worker 模式下 Cluster 视图显示 Stub
- 不需要任何特殊网络：probe() 会跑一次（即使单机 standalone 模式无 GPU 也跑内存/字段采集）
- worker & center 网络中断：Cluster 视图 Stub，所有其它视图不受影响

---

## 7. 错误处理与边界

| 场景 | 行为 |
|---|---|
| 终端过小 | 起前检查 cols < 80 或 rows < 24 → 红 Panel 提示并 return 0 |
| 终端非 TTY（CI 重定向） | Rich 自动 no- color；起后打印提示 "TUI requires a TTY; use --smoke-test for CI" + 立即返回非 0 |
| 集群不可达 / solo | Cluster 视图显示 Stub 红 Panel；其它视图正常 |
| 主题持久化文件损坏 | try/except → 默认 dark |
| profile 不存在 | Detail 视图显示红 panel "未找到 xxx" + 自动 fallback 默认列表 |
| 日志文件被锁定/写入中 | Path.read_lines(errors='ignore') + per-line 截断 200 字 |
| GPU 无 nvidia-smi 与 pynvml 都没有 | Monitor GPU 卡片纯 Panel "无 GPU 可用设备" |
| 用户输入过快（连续 10 次 j/k） | 事件队列 FIFO，每次 render 消费最多 1 事件（cursor 切换不 glitch） |

---

## 8. 测试策略

### 8.1 单元（按文件）

| 文件 | 关键断言 |
|---|---|
| test_tui_state.py | ProfileTree.filter/sort/search 5 组；PlanEdit 字段 set 边界 |
| test_tui_keyboard.py | mock byte stream 喂 stdin，断言 Key 枚举序列 |
| test_tui_app.py | 1→3→1 切视图后 state 视图正确；Ctrl-R 后 snapshot.ttl=0 一次 |
| test_tui_panel_dashboard.py | mock snap.caps + profiles 2 项 + 单元 1 运行中 → 渲染文本含 "运行中" 与深绿 ANSI 码；CJK 对齐：含中文 state 跑后断言每行 display_width 相等 |
| test_tui_panel_detail.py | 5 子 Tab 切换后 content 不同 |
| test_tui_panel_plan.py | 改 5 字段后 Dry-run 调 mock vram_estimate，断言输出含估算值 |
| test_tui_panel_cluster.py | solo 角色下 Stub；worker + mock center 数据下 3 section |
| test_tui_panel_monitor.py | 无 GPU 时纯 Panel 占位；有 mock token rate 时表行 |
| test_tui_no_side_effects.py | 遍历 5 panel，断言 subprocess.Popen.call count=0，open mode "w" count=0 |

### 8.2 手动验收

- Windows PowerShell 5.x / 10 / 11：uv run modelctl tui → 按 5 视图键 → q → 排除 traceback
- WSL 2 + Ubuntu 22.04：同上
- 终端 80x24 缩屏运行无字段缺失

### 8.3 回归

- 既有 CLI 命令全量不受影响（handler 不动，仅 sub.add_parser("tui", ...) 多一条新映射，--help 输出文档化更新）
- 既有 WebUI / cluster 视图 / 日志 / 启动行协议不变

---

## 9. 实现里程碑（供 writing-plans 拆条）

| 里程碑 | 产出 | 验收 |
|---|---|---|
| M0 脚手架 | core/tui 目录 + app / state / keyboard / theme + modelctl tui 起空 TUI 沙箱 | modelctl tui 起后 Ctrl-C 无 traceback |
| M1 Dashboard | data.py Models + Hardware Snapshot + main_dashboard.py + 5 key handler 在 dashboard | j/k/f/s/d/Enter/PgUp/g/G 全通，test_tui_panel_dashboard 全绿 |
| M2 Detail | 5 子 Tab + Logs read_lines 引入 1s 轮询 + Rich Syntax yaml 高亮 | YAML / agent_cfg / log → Tab 切 → Esc → 回 dashboard |
| M3 Plan | PlanEdit + vram_estimator 复用 + engine_config.check_requirement 复用 | 改 5 字段 + D 出 dry-run 三区块 |
| M4 Cluster | Cluster Snapshot + Stub 路径 + worker Probe + 3 tabs | 无 center + mock center 两态 |
| M5 Monitor | 速率表 + pynvml option import + nvidia-smi fallback | r/g 两键通；产双 GPU mock 渲染 |
| M6 收尾 | 主题持久化 + 错误 flash + 最低端检查 + 全量回归 + README 段 + CHANGELOG | uv run modelctl tui 在 80x24 与 200x60 都通行 |

---

## 10. 不做的事（剪除 / 非目标）

- 不启动 ssh / 远程节点
- 不直接写 agent_config（保留 webui 独占 config 通道）
- 不新写 SSE 客户端（除非第二版启动流式日志需要；首版纯 read_lines）
- 不引入 prompt_toolkit / textual / blessed / curtsies（库决策见 2.1）
- 不新写 color / display_width 工具（复用 core/colors.py）
- 不引入 GPU 矩阵健康自动验证（首版不做 num_gpu scale 矩阵）
- 不落库任何配置改动（首版纯只读）

---

## 11. 风险与开放点（留给实现期决策）

| 风险 | 触发条件 | 可选项 / 决策建议 |
|---|---|---|
| Windows PowerShell 5.x ANSI 显示不完整 | 老系统 | 不强制要求 ANSI code；rich 在 PS 5.x 走 ConPTY 自身 stdout sink，多数系统可用，开发机 Win 10 已验证 |
| 主题持久化与 data/cache/ 目录不存在 | 老版本 data 升级 | 写前 mkdir(parents=True, exist_ok=True) |
| pynvml import 慢（30 ms） 拖慢 Monitor 启动 | 用户第一次进 Monitor | 仅在用户按 g 时 first-import 推迟一次；启动 process 不碰 pynvml |
| 1s 日志 poll 在 200 条大文件上 seek 偏慢 | 文件持续增长 > 100 KB/s | 改为 mtime 变化判定 + 仅 seek-to-end 20 行（头尾判） |
| 5 视图在 80 列下视觉拥挤 | 终端小屏 | 80 列检测：Cluster/Monitor 视图加"窄屏模式"——单 section 滚动 + section 切 Tab |
| 用户按 D 后想撤销已确认的拟值 | design gap | 二版 tui-write 引入 staging token，首版纯 discard |

### 与现有代码的对接点（仅列举）

- core/constants 中 profile args 的 num 字段不改写；TUI 只读
- core/ui.py 里所有 _ helper 已有 ANSI 拼接的依赖字符串与 display_width；TUI 不直接 import ui.py，而是程序调 console 吃 _dict_print 返回 list 字符串；如需重写则另起 helper
- `core/colors.py` 的 ANSI 颜色前缀（如 `RED / GREEN / YELLOW / CYAN / BLUE`）和 Rich `Style` 描述符不冲突——TUI 内部统一用 Rich `Style`（`bold / rgb(102,153,0) / reverse`），CLI 侧维持现有彩色前缀变量

---

## 12. 一句话周报

> modelctl tui 即将成为 llmfit 风格的 5 视图富文本工作台：复用现有 core/ 全量查询与采集函数（零新采集逻辑），用 rich 画 UI、自写约 200 行 keyboard 钩、5 视图键位 vim-like，把日常"模型巡检 / 跑参 / 看集群 / 实时率表" 从 2-3 次 CLI 命令凝成一屏可视。首版纯只读 + 只集中语义，写操作放第二版 tui-write，缩小首版误操作的爆炸半径。