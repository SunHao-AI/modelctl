# TUI 交互与渲染（Rich 终端）

> 原始单文件已并入本文件归档，保留溯源信息。

## 浏览界面（detail / plan 预检）在渲染路径清容器、抢 GPU 锁

**日期**：2026-09-11
**症状**：TUI 里只是用方向键浏览某个 profile 的"预检"子页，就可能把同名 docker 容器
`docker rm -f` 掉，或在 GPU 上落一把 `.gpu-lock`——**只看不启动却改掉了运行时状态**。

**根因**：`EngineAdapter.check_requirements()` 一个方法同时承担两种语义：CLI `start`
前的真预检（允许清理残留、需要占卡）与 TUI 渲染时的"能不能跑"展示。适配器在方法内直接调
`clear_stale_docker_container()` 与 `acquire_gpu_lock()`，渲染路径一调用就触发写副作用。

**解决方案**：抽象签名扩为 `check_requirements(self, *, readonly: bool = False)`，
9 个引擎逐个加形参并用 `readonly` 门控副作用：

```python
if not readonly:
    from modelctl.core.process import clear_stale_docker_container
    clear_stale_docker_container(...)
gpus = self.selected_gpus()
if gpus is not None and not readonly:
    acquire_gpu_lock(...)
```

TUI 侧（`panels/detail.py`、`panels/plan.py`）一律 `adapter.check_requirements(readonly=True)`。

**教训**：

- **渲染路径必须只读**。凡可能被 UI/预检调用的函数，写副作用要用显式 `readonly=True`
  门控，而不是靠调用方"记得别调"。新增引擎适配器时容易漏，所以形参要写进抽象基类签名。
- 门控逻辑的门禁测试必须带**对照组**（`readonly=False` 断言副作用确实发生），否则桩打不中
  时只读用例会假绿——详见 `test-isolation.md` 的 monkeypatch 模块绑定条目。
- 顺带修正的相邻缺陷：`selected_gpus()` 原先未走 profile 优先级，改为
  **profile > override > env** 三级，否则 `readonly` 门控的分支在测试里根本不执行。

## `TUIApp.run()` 只渲染一帧就返回，主循环与按键派发根本不存在

**日期**：2026-09-11
**症状**：TUI 启动后画面静止，方向键/Tab/Enter 全无反应，只有 `q` 能退出（靠启动前的一次
性判定）；快照数据也永不刷新。

**根因**：`run()` 的真实分支里只调了一次 `render_once()` 便落到 `loop_end()`。各面板的
按键语义只写在 keybar 文案里（`j/k 翻页`、`Tab/Shift-Tab`、`Enter 详情`），实现侧没有
对应的读取-派发循环，属于"文案先行、实现缺位"。

**解决方案**：补真主循环 + 集中派发：

```python
while True:
    self.render_once()
    key = self.keyboard.read_key_block(FRAME_TIMEOUT_S)   # 超时即下一帧 → 快照自动刷新
    if key is None:
        continue
    if not self._dispatch_key(key):
        break
```

`_dispatch_key()` 按当前视图分支派发，**逐条对齐 keybar 文案**：Q 退出；Esc 在 dashboard
退出、否则回 dashboard；dashboard 的 J/K/↑/↓ 移动选中（clamp 0）、Enter 进 detail、Tab 进
plan；detail 的 Tab/Shift-Tab 沿 `_DETAIL_TABS` 回绕；plan 的 Tab/Shift-Tab 走
`cycle_plan_cursor(±1, 字段数)`、D 触发 dry-run。

**教训**：

- keybar 是**对外契约**：写文案时同步在 `_dispatch_key` 钉分支并加用例，别让"看着能按"
  的界面骗过验收。
- 测试主循环时把渲染与键盘都打桩，`read_key_block` 耗尽后**兜底返回退出键**防死循环；
  再用 `next(it, None) is None` 断言按键被**恰好吃满**，证明派发次数与预期一致。
- 帧超时常量（本次 0.5s）决定无输入时的刷新节奏，别顺手设成阻塞等待——那等于取消自动刷新。
- 测试别真写用户 cache：用 `theme_file=tmp_path / "tui.theme"` 隔离主题落盘。

## Windows `msvcrt.getwch()` 的方向键返回两字节，第二字节不消费会吞键并让按键全变 Unknown

**日期**：2026-09-11
**症状**：Windows 原生终端里 TUI 按方向键/PgUp/PgDn 无反应，且**下一次按键会莫名丢失**
（表现为要按两下才有响应）。

**根因**：Windows 扩展键 `getwch()` 先返回 `0x00` 或 `0xE0` 前缀，**再返回扫描码**。原
`_read_windows` 把第一个字节当普通键直接 `return`，扫描码滞留输入缓冲区，被下一轮当成
未知键读走——既丢了本次方向键，又吞了下一次输入。

**解决方案**：识别扩展前缀并**立刻消费第二字节**，拼成与 POSIX 侧同构的两字节序列交给统一
解码器：

```python
_WIN_EXTENDED_CODES = frozenset({0x00, 0xE0})
_WIN_SCANCE_KEYS = {0x48: Key.Up, 0x50: Key.Down, 0x4B: Key.Left, 0x4D: Key.Right,
                    0x49: Key.PgUp, 0x51: Key.PgDn}

if code in _WIN_EXTENDED_CODES:
    code2 = msvcrt.getch()
    return bytes([0xE0, code2 & 0xFF])
```

`_decode_key` 尾部按 `b"\xe0<扫描码>"` 查表还原 `Key`。

**教训**：

- 扫描码表**必须以实现的映射常量为准**，不能凭记忆写。本次计划文档里的示例扫描码
  （`S/T/I/G` 对应 Left/Right/PgDn/PgUp）就是错的，实现是 `K/M/I/Q`；写用例前先读
  `_WIN_SCANCE_KEYS`。
- 前缀有两个值（`0x00` 与 `0xE0`，取决于终端与键盘状态），只判 `0xE0` 会在部分环境漏网。
- 测试用假 msvcrt（依次吐预置字符）覆盖端到端解码，同时**分别覆盖 `0xE0` 与 `0x00` 前缀**，
  并断言"缓冲区未被多留字节"（一次输入只产出一个 Key）。
