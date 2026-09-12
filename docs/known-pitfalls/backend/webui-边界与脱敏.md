# WebUI 边界与脱敏

> 2026-09-11 测试覆盖专项在 `core/webui` 管理面发现并修复的三个边界缺陷。共性根因：**"看起来安全"的实现缺少最坏输入的守卫**——短 key、float、GBK 文件这三类边界都不在开发机默认路径上。

## 负切片脱敏对短 key 泄漏全文明（BUG-PRB-02）

**日期**：2026-09-12 · **级别**：P3（理论泄漏，实际部署 key 为 44 位）

`/admin/api/probe` 的 API_KEY 回显原实现：

```python
api_key_masked = ("****" + api_key[-4:]) if api_key else ""
```

Python 负切片**不抛越界**：`"abc"[-4:] == "abc"`。key 短于 4 位时输出 `****abc` = 完整密钥原样贴进可截屏的体检页。更隐蔽的是 `admin_auth.mask_key` 的 docstring 白纸黑字写着"短于4位时仅 ***"，实现却没有长度守卫——**契约与实现分裂**，调用方按 docstring 信任它。

同族三个实现的对照（修复前）：

| 实现 | 修复前短值行为 | 判定 |
|---|---|---|
| `admin_probe` 内联 `"****"+k[-4:]` | 全文明 | ✅ 已修（复用 `mask_key`） |
| `admin_auth.mask_key`（docstring 承诺全掩） | 全文明 | ✅ 已修（补 `len(key) <= 4 → "***"`） |
| `admin_config._mask_value` | `"***"` | ✓ 口径基准 |
| `admin_models._mask_key` | `f"***{key}"` 全文明（星号打头反而更像已脱敏，比直出明文更具迷惑性） | ✅ 已修（≤4 → `"***"`，保留 None/空 → None 的"未配置"语义） |

**修法**：单点复用带守卫的 `mask_key`，禁止各端点内联拼切片；`admin_models._mask_key` 因 None/空须返回 `None`（前端按 null 显示"未配置"）而保留独立实现，仅修短 key 分支。

**测试原则**：脱敏函数必须钉**短于保留位数**的用例（如 `"abc"`）；只测 44 位真实长度的用例对这类缺陷零判别力。断言写 `assert secret not in masked` 优于只比对掩码字符串。四处实现由 `test_security_authz.py::test_all_mask_helpers_reject_short_key` 同一用例集体钉住（长 key 口径一致 + 短 key 全掩 + None 语义不回归）。

## 全捕获兜底把类型漂移伪装成合法的 0（BUG-PRB-01）

**日期**：2026-09-12 · **级别**：P3

```python
def _vram_gb(mb) -> float:
    try:
        return round(int(str(mb).strip()) / 1024, 1)   # 24576.7 → int("24576.7") 抛 ValueError
    except Exception:
        return 0.0                                      # "不可解析"与"真的 0 显存"混成同一返回值
```

两个叠加错误：

1. `int(str(x))` 连**原生 float** 都拒（不止浮点字符串）——上游经 JSON 反序列化后 `24576.7` 是常态，UI 显存静默显示 0.0 GB；
2. 兜底 `return 0.0` 使类型漂移**无任何可观测信号**（不报错、不告警），要等人截图"为什么 8 卡显示 0 显存"才能发现。

**修法**：`float(str(mb).strip())` 兼容 int / 整数字符串 / 浮点字符串三形态；兜底保留但语义收窄为"确属不可解析"。

**测试原则**：数值兜底函数的参数表必须含**小数**（`24576.7`、`"8192.0"`）——纯整数夹具对 `int()` vs `float()` 变异零判别力（与"取整类用例数据必须带小数"同一条教训）。

## 读用户文件严格 UTF-8：UnicodeDecodeError 不是 OSError（BUG-FE-01 复发位）

**日期**：2026-09-12 · **级别**：P2（崩溃路径）

```python
try:
    if scope.is_file() and "registry" in scope.read_text(encoding="utf-8"):
        return []
except OSError:          # UnicodeDecodeError 是 ValueError 子类——捕不住
    continue
```

中文 Windows 上用户级 `~/.npmrc` 常以 GBK 存储，`modelctl webui start` 走到 npm 源探测直接抛栈。这与 2026-09-04 已记录的「非 UTF-8 profile 破功'绝不抛异常'」（[profile-config-drift.md](profile-config-drift.md)）**同根因复发**——凡 `read_text(encoding="utf-8")` 读用户可控文件，二选一：

- `except (OSError, UnicodeDecodeError)`；或
- `errors="ignore"`：ASCII 键名（`registry=`）在 ignore 下完好保留，语义不破坏（本次采用）。

**教训**：修一类异常漏点必须 `grep` 全仓同类裸 `read_text(encoding="utf-8")` 一次泛兜，只修被点名那处 = 其它位继续炸（同"gate 三处裸 int()"教训）。测试钉法：`write_bytes(text.encode("gbk"))` 造非 UTF-8 文件，断言**不抛且语义正确**（配了 registry → 尊重；没配 → 回落镜像）。
