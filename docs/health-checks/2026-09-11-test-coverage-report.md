# modelctl 全面测试覆盖报告

- 日期：2026-09-11
- 范围：后端（CLI / TUI / WebUI 后端 / gateway / cluster / engines）+ 前端（Vue 3 SPA）+ CI 流水线
- 层级：单元 / 集成 / 安全 / 性能基准 / E2E / 兼容性
- 环境：Windows 11 + Python 3.12（uv 主 venv，`--extra dev --extra test`）+ Node 24.14；CI 口径为 ubuntu + windows 双 OS matrix
- 口径：本轮以**补测 + 基建 + 门禁**为主，生产代码仅修复 1 个测试暴露的缺陷（BUG-FE-02，见 §5）

> 严重级沿用健康检查约定：P0=可利用/必崩 · P1=高概率故障/鉴权 · P2=边界与一致性 · P3=风格与可维护性

---

## 0. 执行摘要

| 维度 | 命令/方式 | 结果 |
|---|---|---|
| 后端全量 | `uv run --extra dev --extra test pytest tests/ -m "not perf"` | ✅ **2116 passed / 2 skipped**（39 min） |
| 后端增量 | 本轮新增 5 个测试文件 | **+248 用例**（1887 → 2135 collected） |
| 覆盖率（py 口径） | `--cov=modelctl`（branch=true） | ✅ **80.9% 综合 / 行 83.1% / 分支 74.3%**，≥ 门槛 80% |
| 覆盖率门禁 | `pyproject [tool.coverage.report] fail_under=80` | ✅ 已落地（"Total coverage of 80.0% reached"） |
| 安全测试 | `pytest -m security`（新建套件） | ✅ 50/50（三密钥隔离 / fail-closed / JWT 伪造 / 脱敏） |
| 性能基准 | `pytest -m perf`（pytest-benchmark） | ✅ 17/17，热点基线已采集（§6） |
| 前端单测 | `npm run test`（vitest + jsdom，新搭） | ✅ **50/50**（router/stores/api/utils） |
| 前端 E2E | `npx playwright test`（生产拓扑起服） | ✅ chromium **10 passed / 2 skip**（移动断言按视口 skip） |
| 依赖漏洞 | pip-audit（CI security job 常态门禁） | 上轮 2026-09-10 扫描无已知漏洞，本轮入 CI |
| 缺陷 | 本轮发现 4 个，**全部已修复**（09-11 修复 FE-02；09-12 修复 FE-01 / PRB-01 / PRB-02） | P2×2 · P3×2（§5） |

**总体判断**：全量测试从"能跑"升级为"**分层可筛选、覆盖率有门禁、安全/性能/前端/E2E 各有专属通道、CI 五 job 自动化**"的体系。后端覆盖率稳定压在 80% 线上；前端从**零测试**到 50 单测 + 10 E2E。发现 4 个真实生产缺陷，截至 2026-09-12 全部修复并沉淀 known-pitfalls。

---

## 1. 测试基建（自动化框架）

### 1.1 依赖与运行口径

- `pyproject.toml`：`dev` extra 增补 `pytest-cov` / `pytest-benchmark` / `pip-audit`；新增 **`test` extra**（fastapi/uvicorn/httpx/bcrypt/PyJWT）——数据面依赖刻意不落主包（生产由 `gateway/` 子项目独立持有），本地/CI 用 `uv run --extra dev --extra test pytest` 一次装齐，根治"gateway venv 过期副本 ImportError"与"主 venv 缺 httpx 收集失败"两类环境事故。
- `web/package.json`：新增 `vitest` / `@vitest/coverage-v8` / `jsdom` / `@vue/test-utils` / `@playwright/test`；脚本 `test` / `test:coverage` / `test:e2e` / `test:e2e:install`。

### 1.2 分层 marker（自动注入，110+ 文件零手工改造）

`tests/conftest.py` 的 `pytest_collection_modifyitems` 按**文件名词缀**自动打 marker（pyproject 注册 6 个：`unit`/`integration`/`e2e`/`security`/`perf`/`slow`）：

| 词缀/特征 | 层 | 例 |
|---|---|---|
| `test_security_*` | security | test_security_authz.py |
| `test_perf_*` | perf | test_perf_hotpaths.py |
| `test_e2e_*` | e2e | （后端侧预留，浏览器侧走 Playwright） |
| `test_webui_*` / `test_cli_*` / `*_http` / `*_cli` 等 | integration | test_webui_admin_probe.py |
| 其余 | unit（默认） | test_core_observability_units.py |

筛选示例：开发回路 `-m "unit and not slow"`；安全门禁 `-m security`；CI 常规 `-m "not perf"`。

### 1.3 覆盖率配置

```toml
[tool.coverage.run]   source=["modelctl"], branch=true, omit=[两个 __main__.py]
[tool.coverage.report] fail_under=80, show_missing=true
[tool.coverage.html]  directory="build/coverage-html"
```

`exclude_also` 排除 `__main__` 守护 / `NotImplementedError` / `TYPE_CHECKING` / `@overload` 等运行时不可达行，避免分母掺水。

---

## 2. 用例矩阵（本轮实测计数）

| 层 | 通道 | 用例数 | 结果 |
|---|---|---:|---|
| unit | pytest | 1385 | ✅ 全绿 |
| integration | pytest | 683 | ✅ 全绿 |
| security | pytest | 50 | ✅ 全绿（本轮新建） |
| perf | pytest-benchmark | 17 | ✅ 全绿（本轮新建，常规跑 deselected） |
| **后端合计** | | **2135**（collect）= 2116 passed + 2 skipped + 17 perf | |
| 前端单测 | vitest + jsdom | 50 | ✅ 全绿（本轮新建） |
| 前端 E2E | Playwright（生产拓扑） | 10 + 2 视口 skip | ✅ 全绿（本轮新建） |

2 个后端 skip 为存量条件跳过（需真实 docker/GPU 环境），非本轮引入。

## 3. 本轮新增测试资产

### 3.1 后端（+248 用例，全部对应此前零覆盖/薄覆盖模块）

| 文件 | 用例 | 覆盖对象（此前状态 → 现在） |
|---|---:|---|
| `tests/test_webui_admin_probe.py` | 53 | `webui/admin_probe.py`（0 → **100%**）：_vram_gb / GPU 锁序列化 / 引擎二进制探测 + /health /login /probe /overview 端点 |
| `tests/test_webui_frontend.py` | 91 | `webui/frontend.py`（0 → **94.4%**）+ `webui/server.py`（→77.8%）：node/npm 探测、install 决策矩阵、SPA 挂载、webui_port/host |
| `tests/test_core_observability_units.py` | 37 | `core/logging.py`（0 → **96%**）、`sse_stage_event`（0 → **100%**）、`core/ufw.py`（0 → **100%**）、`tui/panels/{base,common}`（0 → **100%**） |
| `tests/test_security_authz.py` | 50 | 安全红线（§4 详述） |
| `tests/test_perf_hotpaths.py` | 17 | 热点基准（§6） |

### 3.2 前端（从零到 6 文件 50 用例 + E2E）

| 文件 | 用例 | 钉住的不变式 |
|---|---:|---|
| `src/router/router.test.ts` | 9 | 守卫三层顺序：public 放行 → accountAuth 跳账号登录 → isLoggedIn 跳管理登录；兜底 404 路由 fail-closed 回 /login |
| `src/stores/auth.test.ts` | 11 | 管理面 token 与账号面 JWT **持久化 key 不重叠、clear 互不影响**（双信任域） |
| `src/api/client.test.ts` | 6 | 请求拦截器 Bearer 注入；**401 是唯一**清 token 跳登录的状态码（403/500 保留登录态） |
| `src/api/sse.test.ts` | 12 | EventSource ?key= 鉴权与编码；log 帧解析失败→onError；close 后 error 抑制（BUG-FE-02 回归钉） |
| `src/utils/time.test.ts` / `toast.test.ts` | 12 | 时间格式 `YYYY-MM-DD HH:mm:ss`（CLAUDE.md 约定）；toast 自动消失/常驻/幂等 dismiss |
| `e2e/smoke.spec.ts` | 8 | 生产拓扑全链路：登录成败、深链守卫带回跳、SPA fallback vs API 404 JSON、fail-closed 401 形状 |
| `e2e/responsive.spec.ts` | 3 | 响应式断点（侧栏 md 折叠）+ 无横向溢出（三渲染内核排版差异哨兵） |

前端被测核心模块行覆盖率：router **100%** · stores/auth **95.2%** · api/sse **98.2%** · utils **100%**（全项目口径 28.3%，views 组件层由 E2E 承担）。

---

## 4. 安全测试（50 用例，`pytest -m security`）

威胁模型 = 本项目三把独立密钥（API_KEY 管理面 / GATEWAY_CLIENT_API_KEY 数据面 / ACCOUNTS_JWT_SECRET 账号面），全部为"改坏即漏洞"的红线不变式：

| 组 | 关键用例 |
|---|---|
| 管理面 fail-closed | API_KEY 未配置全端点 401；空/畸形 Bearer、非 Bearer scheme、近似 key 全拒；非 ASCII 头 401 不 500；401 形状恒 `{"code":"auth"}` |
| 数据面隔离 | 管理面 key 打数据面 401、数据面 key 打管理面 401（两域绝不互换） |
| JWT | secret 缺失 fail-fast（不回退 API_KEY）；**alg=none 拒绝**；错 secret / 过期 / 缺 user_id / payload 篡改 / 签名首字符翻转全拒（翻转首字符而非末字符 = known-pitfalls 的 1/16 假绿教训） |
| 路径穿越 | 端点 payload 注入 `../` 一律 404/400，不泄漏文件系统 |
| 脱敏 | key/config 值掩码末 4 位；审计与探针响应体不含明文密钥（以 sha256 摘要比对证明泄漏） |

## 5. 缺陷清单（本轮发现）

| 编号 | 级别 | 位置 | 描述 | 状态 |
|---|---|---|---|---|
| **BUG-FE-02** | P2 | `web/src/api/sse.ts` onLog | `hooks.onLine?.(JSON.parse(raw))`：可选调用在回调未注册时**跳过参数求值**，JSON.parse 不执行 → 畸形 SSE 帧静默吞掉、onError 永不触发 | ✅ **已修复**（解析/分发拆两步）+ 回归用例 + 沉淀 [frontend/sse-optional-call-skip-args.md](../known-pitfalls/frontend/sse-optional-call-skip-args.md) |
| BUG-FE-01 | P2 | `core/webui/frontend.py` _registry_args | 读非 UTF-8 编码的用户级 `.npmrc` 抛 UnicodeDecodeError（`except OSError` 捕不住，ValueError 子类）→ 中文 Windows 上 `webui start` 崩 | ✅ **已修复**（`errors="ignore"` 保留 ASCII 键名语义）+ 回归用例 ×2 |
| BUG-PRB-01 | P3 | `core/webui/admin_probe.py` _vram_gb | `int(str(mb))` 连**原生 float** 也拒（不止浮点字符串）→ 兜底 0.0，显存静默归零且与"真的 0"无区分 | ✅ **已修复**（改 `float()` 兼容三形态）+ 带小数用例 |
| BUG-PRB-02 | P3 | 同上 probe 短 key | 内联 `"****"+api_key[-4:]` 对短 key **输出全文明**（`"abc"[-4:]==全串`）；且 `admin_auth.mask_key` docstring 承诺"短于4位仅 ***"而实现无守卫 | ✅ **已修复**（`mask_key` 补长度守卫，probe 单点复用）+ 短 key 用例 |

> 定位 BUG-FE-02 的插桩法（try 尾部三路日志区分"未执行/未抛错"）已记入 known-pitfalls。
>
> **2026-09-12 修复轮补记**：BUG-PRB-02 修复过程中发现 `admin_auth.mask_key` 的 docstring 与实现分裂（声称全掩、实为负切片直出），已一并补守卫；三个缺陷的共性根因（缺最坏输入守卫）与测试原则沉淀至 [backend/webui-边界与脱敏.md](../known-pitfalls/backend/webui-边界与脱敏.md)。
>
> **同类存量一并修复（09-12）**：`admin_models._mask_key` 对 ≤4 位 key 原返回 `f"***{key}"`（星号打头反而更像已脱敏的全文明，属 2026-09-10 扫描记录的 WEB-P3），已改为 ≤4 位 → `"***"`，并保留 None/空 → None 的"未配置"语义；四处掩码实现由 `test_security_authz.py::test_all_mask_helpers_reject_short_key` 集体钉住口径。
>
> **09-12 修复后全量回归**：2118 passed / 2 skipped（+2 为 `_registry_args` 非 UTF-8 拆分出的新用例），综合覆盖率 **80.38%**，仍 ≥ 80 门禁；`_mask_key` 修复后安全 + probe + smoke + classify 相关套件复跑全绿。

## 6. 性能基准（pytest-benchmark，`-m perf` 显式跑）

策略：**不做绝对值断言**（runner CPU 波动 ±30% 必造 flakes），用 `--benchmark-compare/--benchmark-autosave` 做相对历史比较，本表即初始基线。

| 热点（每请求/每帧必经） | mean | 备注 |
|---|---:|---|
| `hash_api_key`（sha256） | 1.66 µs | 数据面每请求 |
| `generate_api_key` | 1.02 µs | secrets CSPRNG |
| `_consteq` 44B 恒定时间比较 | 1.41 µs | 管理面每请求 |
| JWT `issue_token` / `verify_token` | 44.8 / 72.6 µs | WebUI 每请求 |
| `display_width` CJK 1k / ASCII 1k | 415 / 497 µs | CLI/日志/表格全链路 |
| `parse_env_file` 500 行 | 457 µs | 进程启动 |
| `_serialize_gpu_locks` 16 卡 | 2.68 µs | /probe 每刷新 |
| `bcrypt hash/verify`（cost=12） | 273 / 227 ms | 登录路径（pedantic 3 轮） |

## 7. E2E 与兼容性

- **拓扑口径**：Playwright `webServer` 以 `python -m modelctl.core.webui.server`（= `modelctl webui` 生产入口）起服，同源 API + SPA——E2E 验证的是生产挂载契约（SPA 深链回 index.html、API 前缀 404 JSON、401 统一形状），不是 vite dev server。
- **矩阵**：projects = chromium / firefox / webkit（三大渲染内核）+ Pixel 7 / iPhone 14（移动视口 + 触摸）；OS 维度由 CI matrix（ubuntu + windows 双 runner）承担。
- **本地实跑**：chromium 10 passed / 2 skipped（移动视口断言按 viewportSize 在桌面项目 skip、移动项目执行）。firefox/webkit 二进制下载在本机沙箱受限，CI `playwright install --with-deps` 装齐后三内核全量跑（§8）。
- 本地经验：沙箱环境把 `PLAYWRIGHT_BROWSERS_PATH` 指到仓库内 `.playwright-browsers/`（已 gitignore）+ npmmirror 镜像可通。

## 8. CI 流水线（`.github/workflows/ci.yml` 重写，5 job）

| job | 内容 | 门禁语义 |
|---|---|---|
| `test` (matrix: ubuntu/windows) | ruff → mypy → `pytest -m "not perf" --cov` | 兼容性 OS 维度；lint/类型/功能 |
| `coverage-gate` | 解析 coverage.xml，`line-rate < 0.80` 即红 | 覆盖率独立红叉（与用例失败区分）；pyproject `fail_under=80` 双保险 |
| `security` | pip-audit（uv export 锁定清单）+ gitleaks 密钥扫描 | 三方 CVE + 凭据入库 |
| `web-unit` | vue-tsc + vitest coverage | 前端类型 + 单测 |
| `e2e` | npm build → playwright install → 三内核+移动视口 | 生产拓扑端到端 |

## 9. 剩余缺口与建议（按优先级）

1. **`webui/admin_models.py` 行覆盖 35.7%**（197/552；1167 行最大管理面文件）——模型启停/日志流/启动进度端点的分支矩阵大，建议按"端点 × 引擎状态"两维补到 ≥70%，是下一轮最大单点收益。
2. `all_service.py` 65.5%、`cli.py` 77.2%（miss 332）、`process.py` 79.8%：存量厚文件，随功能迭代顺带补。
3. `core/colors.py` 40.1%：display_width/pad_width 已钉，其余 ANSI 上色函数属存量缺口。
4. firefox/webkit 本地实跑（沙箱限制，CI 已覆盖）；建议开发机执行一次 `npm run test:e2e:install`。
5. 性能基准历史化：在 CI 引入 `--benchmark-autosave --benchmark-compare` 存档，变慢 >20% 时人工裁决。
6. 移动端**真机**云测（当前为视口+触摸模拟）与浏览器**真版本**矩阵（当前为内核最新稳定版）。

---

## 附录 A：复现命令速查

```bash
# 后端全量（CI 口径，含覆盖率门禁）
uv sync --extra dev --extra test
uv run pytest tests/ -q -m "not perf" --cov=modelctl --cov-report=term-missing

# 分层筛选
uv run pytest tests/ -m security        # 安全红线（~30s）
uv run pytest tests/ -m "not perf"      # 常规门禁
uv run pytest tests/ -m perf --benchmark-autosave   # 性能基线

# 前端
cd web && npm ci
npm run test:coverage                   # vitest
npm run build                           # E2E 前置（生产拓扑）
npm run test:e2e                        # playwright（首次先 test:e2e:install）
```

## 附录 B：本轮改动文件清单

| 文件 | 动作 |
|---|---|
| `pyproject.toml` | test extra、markers、coverage 配置（fail_under=80） |
| `tests/conftest.py` | 自动分层钩子 `_layer_of` + `pytest_collection_modifyitems` |
| `tests/test_webui_admin_probe.py` 等 5 个 | 新增（后端 +248） |
| `web/package.json` / `vite.config.ts` / `playwright.config.ts` | 前端测试栈 |
| `web/src/**/*.test.ts` × 6、`web/e2e/*.spec.ts` × 2 | 新增（前端 +60） |
| `web/src/api/sse.ts` | **BUG-FE-02 修复**（09-11 轮生产代码改动） |
| `core/webui/frontend.py` / `admin_probe.py` / `admin_auth.py` / `admin_models.py` | **BUG-FE-01 / PRB-01 / PRB-02 + `_mask_key` 存量修复**（09-12 轮，短 key 全掩守卫 ×2） |
| `.github/workflows/ci.yml` | 重写为 5 job |
| `.gitignore` | coverage.xml、.playwright-browsers/、test-results/ 等 |
| `docs/known-pitfalls/frontend/sse-optional-call-skip-args.md` + README 索引 | BUG-FE-02 沉淀 |
