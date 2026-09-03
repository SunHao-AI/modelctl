# 中文/CJK 终端对齐处理指南

## 问题背景

终端是**等宽字体**（monospace），每个 ASCII 字符占 1 列，每个 CJK（中日韩）字符占 **2 列**。

Python 的 `str` / f-string 格式化（`<N`、`>N`、`center`）按 `len()` 计，而 `len()` 返回的是 Python 字符数（一个 CJK 也是 1），导致基于 f-string 的对齐在中英混排时**视觉错位**。

### 典型错误示范

```python
def kv(key: str, value: str) -> None:
    print(f"  {key:<12}  {value}")

kv("GPU 数量", "8")
kv("GPU 型号", "NVIDIA RTX 5880 Ada Generation")
kv("单卡显存", "48.0 GB (49140 MB)")
kv("site-packages", "/usr/lib/python3.12/site-packages")
```

实际渲染（注意"GPU 型号"比"单卡显存"多了 2 个字符宽，但 `len()` 都是 8）：

```
  GPU 数量        8
  GPU 型号        NVIDIA RTX 5880 Ada Generation   ← 比下一缩进 1 列多
  单卡显存         48.0 GB (49140 MB)
  site-packages     /usr/lib/python3.12/site-packages
```

键值起点参差不齐，无法对齐。

## 解决方案

### 步骤 1：计算显示宽度 `display_width(text)`

按 Unicode codepoint 判断每个字符宽度，CJK/谚文/全角符号 → 2，其余 → 1。

```python
def _is_wide(cp: int) -> bool:
    """判断 Unicode codepoint 在等宽终端是否占 2 列。"""
    return (
        0x1100 <= cp <= 0x115F        # Hangul 兼容 Jamo
        or 0x2E80 <= cp <= 0x303E      # CJK 部首补充
        or 0x3041 <= cp <= 0x33FF      # 日韩文标点 + 假名
        or 0x3400 <= cp <= 0x4DBF      # CJK 扩展 A
        or 0x4E00 <= cp <= 0x9FFF      # CJK 统一表意文字（主要字段）
        or 0xA000 <= cp <= 0xA4CF      # 彝文
        or 0xAC00 <= cp <= 0xD7A3      # 谚文音节
        or 0xF900 <= cp <= 0xFAFF      # CJK 兼容表意文字
        or 0xFE30 <= cp <= 0xFE4F      # CJK 兼容形式
        or 0xFF00 <= cp <= 0xFF60      # 全角 ASCII
        or 0xFFE0 <= cp <= 0xFFE6      # 全角符号
        or 0x20000 <= cp <= 0x3FFFD    # CJK 扩展 B+
    )

def display_width(text: str) -> int:
    """返回文本在等宽终端的显示列数（CJK 双宽、ASCII 单宽）。"""
    return sum(2 if _is_wide(ord(ch)) else 1 for ch in text)
```

> **本仓库实现**：[src/modelctl/core/colors.py](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/colors.py) 中已提供 `display_width` 与 `pad_width`，直接导入使用即可。
>
> 第三方库备选：`wcwidth`（`pip install wcwidth`）提供更精确的 Unicode 宽度表（含变化区间、零宽字符、控制字符），但引入依赖。本项如果不需要处理 console/网页/手机 emoji 宽度渲染，自实现即可。

### 步骤 2：按显示宽度补齐 `pad_width(text, width, align)`

```python
def pad_width(text: str, width: int, *, align: str = "left") -> str:
    """按显示宽度填充（left/right/center）。文本超过宽原样返回，不截断。"""
    w = display_width(text)
    gap = max(0, width - w)
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap
```

要点：

- **不截断**：文本超过 `width` 时不切割（避免破坏路径、提示等长文本），只留 0 余量。
- **按显示宽度算填充**，不是按 `len()`。
- **统一 `width`**：同一区块的所有 `key` 都用同一个 `width`，否则每行右边界仍然错位。
- **`width` 取整块所有键 `display_width` 的最大值**（可以再起余量）。
  - 例如本次的 KV 键 `GPU 数量`(8)、`单卡显存`(8)、`CUDA 驱动`(8)、`site-packages`(13)、`MODELSCOPE_CACHE`(16) → `KEY_W = 16`。

### 步骤 3：组合输出

```python
from modelctl.core.colors import display_width, pad_width, _table_paint

KEY_W = 16  # max(display_width(k) for k in all_keys)

def kv(key: str, value: str) -> None:
    print(f"  {pad_width(key, KEY_W)}  {value}")

kv("GPU 数量", "8")
kv("GPU 型号", "NVIDIA RTX 5880 Ada Generation")
kv("单卡显存", "48.0 GB (49140 MB)")
kv("site-packages", "/usr/lib/python3.12/site-packages")
kv("MODELSCOPE_CACHE", "/models/modescope")
```

实际渲染（所有值起点在列 20）：

```
  GPU 数量          8
  GPU 型号          NVIDIA RTX 5880 Ada Generation
  单卡显存          48.0 GB (49140 MB)
  site-packages     /usr/lib/python3.12/site-packages
  MODELSCOPE_CACHE  /models/modescope
```

## 颜色 / ANSI 码注意事项

如果键/列内容里带 ANSI 转义码（如 `_table_paint("可用", "SUCCESS")`），`display_width` 仍要按**可见字符**计算，否则码不会作用于前置换行（绝大多数终端会正确处理码宽度为 0）。

- **简单做法**：先算纯文本宽度，再上色 → `pad_width(pure_text, w)` 后 `_table_paint(...)`。
- **本仓库 `_cmd_probe`** 的做法：先用 `pad_width("可用", 6)` 补齐再上色对颜色。因为 ANSI 码对终端列宽无贡献，结果同样正确；但若要条件性修改，仍应先算宽度再上色。

```python
# 正例
status = _table_paint("可用", "SUCCESS") + "   "
# "可用" 显示宽 4，补 2 空格 → 6 列；"   " 在 ANSI 外不会改变宽度

# 反例（若需要再补齐的话，应该先算宽度）
# 避免: pad_width(_table_paint(...), w)  —— 会把 ANSI 码当字符计
```

## 表格列宽度对齐（多列）

```python
def build_table(rows: list[tuple[str, str]], cols_w: list[int]) -> str:
    """cols_w 是每列指定的显示宽度。"""
    headers = "\t".join(pad_width(c, cols_w[i]) for i, c in enumerate(("引擎", "变体", "端口"))) ...
    lines = []
    for row in rows:
        lines.append("  ".join(pad_width(str(c), cols_w[i]) for i, c in enumerate(row)))
    return "\n".join(lines)
```

`cols_w` 各列宽度 = 该列所有单元格 `display_width` 的最大值（可以留着量）。

## 日志系统对齐（loguru 动态列）

针对 loguru 这种**多记录、字段分散在调用栈各处、列宽跨记录动态变化**的日志格式，单点的 `{x:<N}`/`pad_width` 调用不够，需要外加一层包装。

### 难点

日志列通常包含：

- **时间戳**：`{time}` / `{timestamp}` —— 全 ASCII，但 pad 后会影响显示
- **级别**：`{level}` —— 含 CJK 时（如 `PERF` 之外的级别）宽度可变
- **file/line**：`{file}` / `{line}` —— 文件名纯 ASCII，行号纯 ASCII
- **caller 自定义字段**：如 `task=xxx` 等，可能含 CJK（如上 L143 的"耗时分解"、L134 的"初始化…"）

### 方案：预处理 message

**核心思路**：在调用 `logger.bind(...)` / `logger.opt(lazy=...)` 之前，先把日志正文里的列定位助手 `info_col(...) / level_col(...) / name_col(...)` 都按 `display_width` 补齐，再做 `print`。

```python
# 一个独立的 fmt 模块（例如 core/logfmt.py）
from functools import partial
from modelctl.core.colors import display_width, pad_width

class LogCols:
    """日志列宽容器。按记录最大值动态维护，thread-safe（GIL 保护）。"""
    _time = 0
    _level = 4
    _name = 28
    _ts = 19  # "2026-08-28 14:52:53.994"

    @classmethod
    def sync(cls, time: str, level: str, name: str, ts: str = "") -> None:
        cls._time = max(cls._time, display_width(time))
        cls._level = max(cls._level, display_width(level))
        cls._name = max(cls._name, display_width(name))

    @classmethod
    def reset(cls) -> None:
        """跨记录重置；切换 task/root/pid 时调用。"""
        cls._time, cls._level, cls._name = 0, 4, 28


def align_row(cols: tuple[str], widths: tuple[int], sep: str = " | ") -> str:
    """把 cols 按 widths 左对齐 + 分隔符拼接。"""
    return sep.join(pad_width(c, w) for c, w in zip(cols, widths))


def log_row(time_: str, level: str, name: str, message: str, ts: str = "") -> str:
    """组装一条对齐日志行。"""
    LogCols.sync(time_ or "", level or "", name or "", ts)
    widths = (LogCols._time, LogCols._level, LogCols._name)
    return ts + " " + align_row((time_, level, name, message), widths)
```

调用侧：

```python
from modelctl.core.logfmt import LogCols, log_row, align_row
from loguru import logger

for ... in work_items:
    msg = "耗时分解: total=0.270s, queue=0.065s, ..."
    logger.add_sink(
        lambda line: logger.log("INFO", log_row("14:52:53", "INFO", "OnlineDetect", line["message"])),
        format="{message}",
    )
```

或者更省事：直接用 `logger.bind(name="OnlineDetect").info(...)` 的格式片段自己把它预处理成对齐后字符串。

### 与现有 loguru `format` 模板的兼容

**不要**直接把 `{level:<20}` 这种花括号模板塞给 loguru —— 它按 `len()` 而非 `display_width` 计算，结果错位。改法：

1. 所有"自定义对齐的彩条"（如 L135 `⏱ Performance|Total   18.796s | Status: Success`）**不要靠 loguru 模板**，而是调用侧用 `align_row` 把 `{record["message"]}` 拼好后，loguru 只负责时间/级别前缀。
2. 想让 `{level}` 等内置字段在 front 对齐，**自定义 sink** 而不是直接依赖 loguru 默认格式。

### 任务级别重置

跨 task / 跨进程 (如模型切换、worker 重启) 时 `LogCols` 会残留"宽度峰值"，**应在以下时机 reset**：

- task / root 切换（比较 `task_id` 哈希）
- PID 切换（容器 multi-process 模型）
- 进阶：把 LogCols 接到 `logging` 的 `RecordFactory`，在拿到 `task_id`/`root_id` 字段时主动 reset

## 现有仓库整改清单

按下面的原则把 modelctl 中所有"硬编码对齐"重写到 `display_width` + `pad_width`：

| 位置 | 现有实现 | 问题 | 整改 |
|---|---|---|---|
| [cli.py#L585-L671](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/cli.py#L585-L671) `_cmd_probe` | `pad_width(key, KEY_W)` 区块一/三/四 + `name_w = max(display_width(n) ...)` 区块二 | ✅ 已改 | 保持 |
| [cli.py#L179-L222](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/cli.py#L179-L222) `_ljust_width` / `_print_table` | `_display_width` + `str.ljust` 等价操作 | ✅ 已用 `display_width` 机制 | 保持 |
| [cli.py#L935-L965](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/cli.py#L935-L965) `_format_audit_table` | `str.ljust` + `len()`，当前列全 ASCII 标识符（ts/model/endpoint 等）暂未错位，但若未来加中文描述列则错位 | ⚠️ 风险 | 改用 `display_width` 计算列宽 + `pad_width` 补齐 |
| 日志系统（loguru） `level` / `message` 对齐 | loguru 模板 `{time} {level:<10} {message}` | ⚠️ 含 CJK 时分段错位 | 在调用侧用 `align_row` 预对齐 message，loguru 只做时间/级别前缀 + `reset` 时机 |
| 引擎提示（`ENGINE_INSTALL_HINTS`）多行 | 固定文本 | 无（已无列宽计算） | 保持 |

### 整改示例（`_format_audit_table`）

```python
def _format_audit_table(records: list[dict]) -> list[str]:
    if not records:
        return []
    headers = ["ts", "model", "endpoint", "stream", "src", "tokens (in/out)",
               "ttft_ms", "tps", "status"]
    # ... 同上构造 rows ...
    widths = [
        max(_display_width(h),
            max((_display_width(str(r[i])) for r in rows), default=0))
        for i, h in enumerate(headers)
    ]
    out: list[str] = [_ljust_width(h, widths[i]) for i, h in enumerate(headers)]
    for r in rows:
        out.append("  ".join(_ljust_width(str(c), widths[i]) for i, c in enumerate(r)))
    return out
```

> 注：上游 `records` 字段（model、endpoint 等）当前都是 ASCII 标识符，但 `timestamp` 等字段若未来引入本地时区差异（如 `+08:00` 的半角/全角 colon），仍可能错位；防患于未然是项目原则。

## 常见陷阱

1. **不要混用 `len()` 与 `display_width`**。同一个对齐规则下，要么都用 `display_width`，要么都不用。
2. **集中定义 `COL_W`/`KEY_W`**，且整块共用同一个值，不要每行自定义。
3. **路径、URL、安装提示等长文本不要截断**。超过就超长，让终端回行，否则破坏调试信息。
4. **混 ANSI 的文本**：先算宽度上色，再参与补齐（见上面"颜色注意事项"）。
5. **全角符号**：`（）：，、·`（U+FF08 等）也是 2 列，别以为是中文字符才双宽。
6. **`site-packages`（13 列） vs `MODELSCOPE_CACHE`（16 列）** 这种键名差异较大时，还是按最长键设 `KEY_W`，短的会被补齐（可读性 OK）。
7. **单元测试**：补 `display_width` / `pad_width` 单测验证，尤其是中英混排、空串、超长等边界。

## 本仓库入口

- 实现：[src/modelctl/core/colors.py#L89-L110](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/core/colors.py#L89-L110)
  - `display_width(text: str) -> int`
  - `pad_width(text: str, width: int, *, align: str = "left") -> str`
- 使用范例：[src/modelctl/cli.py#L585-L671](file:///d:/WorkPlace/Pycharm/modelctl/src/modelctl/cli.py#L585-L671) 的 `_cmd_probe`
  - `KEY_W = 16`（区块一/三/四共用）
  - `name_w = max(display_width(n) for n in ENGINE_BINARIES)`（区块二列宽自适应）

## 全量整改与规范（所有 CLI 命令强制）

> **核心原则**：本仓库任何 CLI 子命令、日志 sink、表格、KV 行的输出，只要字段潜在含 CJK，**必须**走 `display_width` + `pad_width` 这一套，禁止直接 `f"{x:<N}"` / `str.ljust` / `len()` 对齐。

### 实施细则

1. **新增代码**：
   - 任何"看起来像对齐"的代码，第一反应是 `from modelctl.core.colors import display_width, pad_width`。
   - Code review 时见到 `:<\d+` / `len(\w+)` 用于列宽计算 → 退回，要求换 `_display_width` / `display_width`。
   - 只有**纯 ASCII 行**（如异常栈、audit JSON）可以保留 `len()`。

2. **统一封装**（可选但推荐）：
   - 在 `core/formatter.py` 提供 `KV / Table / LogRow` 三个 helper，所有命令复用，避免各处独立推测宽度。
   - 例：
     ```python
     print(kv_row("GPU 数量", "8", key_w=16))
     print(table_row(row, widths, state_idx=3))
     ```

3. **测试守护**：
   - 每个使用 `display_width` 的命令补"渲染快照"测试：用一个 mock caps/records 跑命令，断言渲染出的.strip() 形态符合预期（不要断言 Unicode 原始字节）。
   - CI 里跑 `pytest tests/test_cli_*.py`，确保无回归。

4. **改动清单**：在每次提交前过一遍本文档"现有仓库整改清单"里的检查项目录。

### 典型反例（Code Review 红线）

```python
# ❌ 反例：len() + f-string 对齐
print(f"  {'GPU 数量 '} {caps.gpu_count}")
print(f"  {'单卡显存 '} {vm} MB")          # 列 11 起点，错位

# ✅ 正例：display_width + pad_width
from modelctl.core.colors import pad_width
kv_row = lambda k, v: print(f"  {pad_width(k, 16)}  {v}")
kv_row("GPU 数量", str(caps.gpu_count))
kv_row("单卡显存", f"{vm} MB")
```

```python
# ❌ 反例：日志级别按 len() 垫片
level_col = f"{level}{ ' ' * (8 - len(level)) }"   # 5 个英文 + 3 个空白 ≠ 5 个中文 + 1 个空白

# ✅ 正例：display_width
from modelctl.core.colors import pad_width
level_col = pad_width(level, 8)
```

## 快速检查清单（处理新对齐需求前先看）

- [ ] 是不是同一个区块内所有行的键都是相同的列宽？
- [ ] 是不是同一列的每一行 width 一致？
- [ ] 是不是按 `display_width` 算宽度，而不是 `len()`？
- [ ] 文本是否会被截断（不应该）？
- [ ] 颜色文本是不是"先算宽度再上色"？
- [ ] 最长键的 `display_width` 是不是已经写进 `COL_W` 常量？
- [ ] 日志 sink 是不是 loguru 模板之外的 `_align_row` 渲染？
- [ ] 跨记录/跨进程切换时是不是 reset 了宽度峰？
