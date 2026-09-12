# modelctl 全覆盖复核报告（2026-09-12）

> 范围与方法见 `docs/superpowers/specs/2026-09-12-full-coverage-review-design.md`；执行台账见
> `.superpowers/sdd/2026-09-12-full-coverage-review/progress.md`；上轮基线 = `2026-09-11-test-coverage-report.md`。
> **本报告所有数字均为本轮实跑**，凡引用上轮处均显式标注"09-11"。

---

## 0. 执行摘要

| 维度 | 命令 | 本轮结果 |
|---|---|---|
| 依赖同步 | `uv sync --extra dev --extra test` | OK（须 `UV_CACHE_DIR` 指仓库内，沙箱禁写默认 `E:\cache\uv`） |
| Lint | `uv run ruff check src tests` | **272** errors（0 panic；123 auto-fixable） |
| 类型 | `uv run mypy src` | **112** errors / 30 files（checked 95） |
| 全量测试（阶段 1 基线） | `uv run pytest tests/ -m "not perf" --cov=modelctl --cov-branch` | 1 failed / 2141 passed / 2 skipped / 17 deselected；覆盖率 **81.14%** |
| **全量门禁复跑（收尾）** | `uv run pytest -q --cov=src/modelctl`（含 perf） | **0 failed / 2192 passed / 2 skipped**（1123.82s）；覆盖率 **81.61%** |
| 安全 + 性能 | `uv run pytest -m "security or perf"` | 68 passed / 51.67s |
| 前端类型 | `npm run typecheck` | **exit 0**（阶段 1 曾为 exit 2 → 已修，见 P1-2） |
| 前端单测 | `npm test`（vitest） | **79 passed / 13 files**（09-11 = 50） |
| 前端构建 | `npm run build` | **exit 0** |
| E2E | `npx playwright test`（chromium + mobile-chrome） | 20 passed / 2 skipped；firefox/webkit 沙箱内不可安装 → 不可验证 |
| 依赖漏洞 | `pip-audit`（根环境）/ `npm audit` | 根环境 0 已知漏洞；npm **6** 漏洞全部 dev-only |
| 真引擎冒烟 | llama.cpp b10809 CPU + Qwen2.5-0.5B-Q4_K_M | **S1-S7/S9 全 PASS**（S8 由 S7 与单测覆盖）；发现并修复 **2 个 P1** |

**总体判断**：主链路（profile 编排、网关代理、审计、用量、GPU 互斥、WebUI 边界）在真引擎 + 真模型下确实可用；本轮 4 个 P1 **全部当场修复并四步闭环**，收尾门禁较阶段 1 **零回退**（+51 用例、覆盖率 +0.47pt）。真正的系统性风险仍集中在 **webui 管理面测试最薄 + 类型最脏**（10 个低于门禁的模块里占 7 个）。

**缺陷计数**：P0 **0** · P1 **4**（全修）· P2 **10**（记录）· P3 **11**（其中 P3-10 / P3-11 已顺手修 `add2913`）。

---

## 1. 工具链结果

### 1.1 后端

- **ruff 272 errors**，分布：E501 103 / I001 58 / F401 35 / E402 18 / B008 16 / B904 11 / F541 8 / E741 6 / F841 6 / E702 3 / F821 1 / B011 1 / B905 1 / B007 1。收尾复跑仍为 272 → **本轮改动未新增 lint**。
  - 唯一 F821（`tests/test_stats_native.py:20`）是**字符串注解引用**，运行时不求值 → P3 误报性质，非缺陷。
- **mypy 112 errors / 30 files**（阶段 1 = 113/31，收尾少 1 条，因 P1-4 修复未引入新类型问题）。错误头部：`webui/admin_models.py` 20、`webui/admin_services.py` 12、`core/tui/keyboard.py` 8、`cli.py` 8、`accounts/store.py` 7。
- **口径纪律**（本轮踩过的坑，已写入 `raw/coverage-hotspots.md` 头部警告）：本轮启用 `--cov-branch`，`combined%` 恒低于 `line%`。与 09-11 比较**必须 line% 对 line%**，否则会得出虚假的"覆盖率回退"结论。

### 1.2 前端

- 阶段 1：`vue-tsc` exit 2，2 个错误全在 `src/api/client.test.ts`（TS2322 L21 / TS2345 L31，axios `Adapter` 类型）→ `npm run build` 必然失败 = **P1-2，已修**（改用 `InternalAxiosRequestConfig`）。收尾 `typecheck` / `build` 双 exit 0。
- **vitest 79 passed**（09-11 = 50 → 阶段 1 = 68 → 收尾 79，+11 来自 T1 组件补测）。
- **npm audit 6 漏洞**：2 critical（`@vitest/coverage-v8`、`vitest` 直接 dev 依赖）、1 high（vite 传递）、3 moderate。**全部 dev-only，不进生产产物** → P2 记录。
- **Playwright**：完整矩阵 5 projects / 55 tests → 10 passed / **45 failed，全部是 firefox/webkit `Executable doesn't exist`**（沙箱禁写 `%LOCALAPPDATA%\ms-playwright`）。把浏览器装进仓库（`PLAYWRIGHT_BROWSERS_PATH`）后 chromium + mobile-chrome = **20 passed / 2 skipped**（与 09-11 同 2 条 skip）。→ 非产品缺陷，属**环境不可验证项**。

### 1.3 依赖安全

- 根环境 `pip-audit`：**No known vulnerabilities found**（需同时重定向 `--cache-dir` 与 `LOCALAPPDATA`）。
- `gateway/` 子环境：**未跑成**（`uv export` 出 `gw-req.txt`，但 `pip-audit -r` 在 PyPI 查询上挂死 >10min 被停）。**记为 NOT RUN，不得当作"已确认干净"**。
- `envs/*` 引擎 venv 属运行时构建，本轮明确排除在范围外。
- 仓库无 `requirements.txt`，只有 pyproject（根 + `gateway/` + 6× `envs/`）。

---

## 2. 覆盖率热区

总水位（阶段 1，`--cov-branch`）：**combined 81.14% · line 83.30%（12219/14669）· branch 74.65%（3631/4864）**；收尾复跑 combined **81.61%**（TOTAL 14692 stmt / 2384 miss / 4874 branch / 684 partial）。门禁 80% **达标**。

完整表见 `raw/coverage-hotspots.md`；要点：

| combined% | 缺失行 | DIFF | 模块 |
|---|---|---|---|
| 32.4 | 120 | | `core/colors.py` |
| **33.8** | **355** | DIFF | `webui/admin_models.py` ← 全库缺失行数第一 + mypy 第一 |
| 35.8 | 76 | DIFF | `webui/admin_config.py` |
| 45.2 | 81 | DIFF | `webui/admin_services.py` |
| 51.2 | 32 | DIFF | `webui/admin_router.py` |
| 63.8 | 116 | | `core/all_service.py` |
| 66.8 | 43 | DIFF | `core/tui/app.py` |
| 76.2 | **332** | | `cli.py`（绝对缺口第二） |

- **信号**：低于门禁的 10 个 diff 命中模块里 **7 个是 webui 管理面**，与 mypy 错误分布重合 → 同一批文件"类型最脏 + 测试最薄 + 本轮改动最多"。
- **同口径对照 09-11 无回退**：`colors` 40.1→40.6 / `all_service` 65.5→68.0 / `cli` 77.2→77.2 / `process` 79.8→80.7 / `admin_models` 35.7→36.6。
- 前端语句覆盖 **30.84%**（09-11 = 28.3%）。核心逻辑层已钉（`api/sse.ts` 98.18 / `stores/auth.ts` 95.23 / `stores/chat.ts` 92.74），**视图层 13 个视图 + 4 个 chat 组件仍为 0%**，其中 chat 4 件已由 T1 补测脱离 0（见 §3 T1）。

---

## 3. 缺陷清单

### 3.0 风险条目 T1–T8 结论（每条三选一：确认缺陷 / 无问题 / 待验证）

| # | 条目 | 处置 | 结论 |
|---|---|---|---|
| T1 | chat 前端组件零测试（4 组件 + 1 视图实测 0%） | `ba4201b` 补 `ChatHistoryList` 5 + `ChatStatsPanel` 6；审查另 3 件 | **确认缺陷（P2）已补测**；另 3 件仅审查 → P3-6 |
| T2 | chat SSE 代理跨层契约 4 条分支无钉 | `3e91e5a` 补 4 条契约钉（route_mode / 非对象体 / PreparedError / 流关闭传播） | **无问题**（契约与前端一致，前端在 `res.ok` 前先读 meta） |
| T3 | `admin_models` 最低覆盖 + mypy 第一 | `19ba4d5` 补 20 条生命周期/yaml_override 钉 | **无产品缺陷**（P1 候选降级）；测试缺口本身记 P2-1 |
| T3b | `admin_config` 35.8 / `admin_services` 45.2 / `admin_router` 51.2 | 并入 T3 分桶，仅对 diff 命中分支补测 | **待验证 → 记 P2-1** |
| T4 | `ENGINE_PRIORITY` 完整性 | `525aef8` 补 `KNOWN_ENGINES ⊆ ENGINE_PRIORITY` 钉 | **无问题**，TODO §1.1 为**文档滞后**（P3-2） |
| T5 | TUI 重构类型契约（mypy 21 条） | 只读审查：readonly 门 9/9 handler、主循环存在、Windows 扩展键已处理 | **无问题**（类型声明缺口，非运行故障）→ 降级 **P2-4** |
| T6 | 存量薄弱 5 模块 | 同口径 line% 核对：40.6/68.0/77.2/80.7/36.6 | **无问题（无回退）**，纯存量缺口 → P2-9 |
| T7 | 14 个引擎适配器一致性 | 读码 + 覆盖率对照 | **确认缺陷（P2）**：`tensorrt_llm` 两分支缺 `api_key_args()`（P2-3）；P3-3/P3-4 另记 |
| T8 | cluster 收敛状态机（FAILED 终态 + GPU 锁） | 读码 + 回归钉仍在；GPU 锁早退失败 → 即 **P1-1** | **确认缺陷（P1）已修** `bab8dcb`；reconcile 本体**无问题**（P2-6） |
| 冒烟新增 | Windows llamacpp / 组路由审计 | S1 与 S6 实测 | **确认缺陷（P1）已修** `9071b50` / `53c38e9` |

### 3.1 缺陷明细

级别定义：P0 可利用/必崩 · P1 高概率故障/重大信息暴露/鉴权 · P2 边界与一致性 · P3 风格与可维护性。
"已修复"要求四步闭环：**红灯用例 → 修生产码 → 全绿+子域回归 → `git stash` 回滚验红灯**，四步齐备才标注。

### P0 — 无

### P1-1 `start_profile` 启动失败不归还 GPU 锁 · **已修复** `bab8dcb`

- **位置**：`core/all_service.py`（`start_profile`）× `core/gpu_lock.py` × 9× `engines/*.py:check_requirements`
- **根因**：`acquire_gpu_lock` 在 `check_requirements` 末步落锁，owner = **常驻 worker pid**；此后 `pre_start` 失败 / `build_command` 失败 / 健康检查失败且后端确死，**三类出口都不归还**。`_read_lock` 只在 `is_pid_alive` 为假时清理，而 owner 恒为常驻进程（docker 路径按设计不改绑容器）→ **锁既不释放也不会 stale**，卡位对所有其它 profile 永久不可用。与 `stop_instance` 无条件释放（`process.py:419-424`）及 README:377 承诺相反。
- **复现**：`uv run pytest tests/test_all_service.py -k gpu_lock`（stash 掉修复 → 恰 3 条"应归还"用例红、2 条"应保留"用例绿）。
- **修复**：`_release_gpu_lock_if_idle()` 在三出口归还；**反向不变式**同样入钉——健康检查超时但进程仍活时**绝不归还**（否则两张模型撞同一张卡，即本项目最怕的"互斥静默失效"）。5 条回归钉；副作用同时闭合 reconcile 泄漏路径 B/C。

### P1-2 `npm run build` 因测试文件类型错而必然失败 · **已修复** `7ec7554`

- **位置**：`web/src/api/client.test.ts` L21 / L31
- **根因**：把 axios 拦截器参数标成 `Adapter` 派生类型，与 `InternalAxiosRequestConfig` 不兼容 → `vue-tsc` exit 2 → 构建产物无法产出（发布门口径）。
- **复现**：回滚该文件后 `npm run typecheck` → TS2322 / TS2345。
- **修复**：改用 `InternalAxiosRequestConfig`；`typecheck` exit 0、`client.test.ts` 6/6 绿、`build` exit 0。

### P1-3 llamacpp 找不到 Windows 预编译包的 `llama-server.exe` · **已修复** `9071b50`

- **位置**：`engines/llamacpp.py`（`find_server` / `pre_start`）
- **根因**：`find_server` 只找**无扩展名** `llama-server`，且 `pre_start` 的编译判据硬编码 `build/bin` 路径；官方 Windows Release 包是 **source 根目录的 `llama-server.exe`** → 判为"产物不存在" → 落编译分支 → 报 **`RequirementError：缺少 cmake`**（误导：产物早就在）。`pre_start` 注释写的是"产物已就绪则不校验 cmake"，与实现不符。**Windows 上从源码编译 llama.cpp 实际不可行，prebuilt 是唯一路径** → 等价于 Windows 引擎不可用。
- **复现**：`.tmp/llama-tmp`（Release 包扁平解压）作 `LLAMACPP_SOURCE_DIR` 跑 `modelctl start llamacpp/qwen2.5-0.5b`；或 `uv run pytest tests/test_engines_llamacpp.py -k "windows_prebuilt or platform_exe_name"`。
- **修复**：`_server_names()` 按 `os.name` 返回候选名；`find_server` 搜 `build/bin` → source 根；`pre_start` 判据复用 `find_server`（**判定与定位同一函数**）。2 条红灯钉 + stash 验红。
- **冒烟反证**：修复前 `start` 直接失败；修复后 S1 健康检查 200。**S1 通过本身即该修复的端到端验证。**

### P1-4 `create_app` 注入漏 groups 成员 → 家族路由成功请求整条不落审计 · **已修复** `53c38e9`

- **位置**：`core/gateway.py` `create_app` 注入段（原 1058）× `resolve_model` group_route
- **根因**：生产入口 `main()` / `webui` 不传 registry，走自动构建；`build_registry()` 与 `build_groups()` 为**同一 profile 各 new 一份 `GatewayModel`**，而注入循环只遍历 `registry.values()` 挂 `audit_log`/`collector`。家族路由命中的是 **groups 里的实例** → `target.audit_log is None` → 成功路径 `if self_audit_log is not None` 直接跳过。
- **症状不对称（这是它难被发现的原因）**：401/429 短路用**闭包** `audit_log` → 有审计；200 用 **`target.audit_log`** → 无审计。日志侧完全正常（`上游路由 ... （group_route）` + 响应摘要都有），只有审计行数对不上。冒烟实测 `data/audit/*.jsonl` **仅 2 行且全是 401**，3 条 200 静默丢失。
- **附带泄漏**：同一循环的 `collector` 注入同样只覆盖 registry → 家族路由流量不走网关侧 token 累计（本轮 llamacpp 走引擎 `/metrics` 才被掩盖，**vLLM 这类 metrics 恒 0 的引擎会直接漏计用量**）。
- **复现**：`uv run pytest tests/test_audit.py -k group_route_success_writes_audit`（patch 两个构建器返回**不同实例**复现生产；stash 修复 → 恰 1 条红"网关未产生审计文件"）。
- **修复**：注入集合取 `registry.values() + groups 全成员`，按 `id()` 去重。子域回归 **151 passed**（audit + gateway + accounts_gateway）。
- **真实链路复验**：重启网关后打 `model="qwen2.5-0.5b"`（组名）→ HTTP 200，审计新增 1 行且字段完整（`model=qwen2.5-0.5b-llamacpp / engine=llamacpp / status_code=200 / tokens 32+16 / auth=ok / finish_reason=length`）。

### P2（记录，不当场修）

| # | 条目 | 位置 / 证据 |
|---|---|---|
| P2-1 | 管理面测试薄 + 类型脏同源：`admin_models` 33.8%(缺 355)/mypy 20、`admin_services` 45.2%/12、`admin_config` 35.8%、`admin_router` 51.2% | 本轮已补 `admin_models` 生命周期 20 钉 + chat 代理 4 钉，其余按分支推进 |
| P2-2 | **anthropic SSE 上游 >=400 早退分支不写审计**（OpenAI 路径写）→ 协议间审计不对称 | `core/gateway.py:1303-1313` |
| P2-3 | `tensorrt_llm` 两个启动分支仍缺 `api_key_args()`（CLU-P2-4 至今未修） | `engines/tensorrt_llm.py` |
| P2-4 | TUI `_SnapshotBase` 家族 mypy 6 条 = 协议缺成员声明（`app.py:78` `dict[str,_SnapshotBase]` + 18 处 `type: ignore`）；运行路径 66.8-91.3% 已覆盖，无崩溃证据 → 由 P1 **降 P2** | `core/tui/app.py`、`data.py` |
| P2-5 | npm 6 漏洞全 dev-only（2 critical 在 vitest 系） | `web/package.json` |
| P2-6 | `cluster/reconcile.py` 81.1%（缺 104 行）由 P1 降 P2：无失败用例、FAILED 终态回归钉仍在 | `cluster/reconcile.py` |
| P2-7 | `engines/tokenspeed.py` 68.2%、`tensorrt_llm.py` 78.9% 低于门禁（新适配器） | 同覆盖率表 |
| P2-8 | `tests/test_envfile.py` 断言 `PROJECT_ROOT.name == "modelctl"` → 只有目录恰好叫 `modelctl` 才过（阶段 1 唯一 FAILED 即此）| **已修** `fdc0c18`：改断结构不变量（`pyproject.toml` + `models/` + `src/modelctl/`） |
| P2-9 | 存量薄弱 `colors.py` 32.4%(120)、`cli.py` 76.2%(332)、`all_service.py` 63.8%(116)、`process.py` 77.9%(67) —— 同口径**无回退**，属已登记缺口（详见 §6） | TODO §3/§4 |
| P2-10 | Playwright firefox/webkit 矩阵在本机不可验证 | 沙箱禁写 `%LOCALAPPDATA%` |

### P3（记录）

1. `/api/usage` 的 `isValid` 语义：targets 来自 `list_profiles()` **不筛运行状态**，任一未运行 profile `ConnectionRefused` 即置 `isValid:false`，而聚合数值本身正确（实测 llamacpp 累计 195 toks 正确）。建议 `isValid` 只反映"至少一个 target 可取"或按 target 分列。
2. `TODO.md §1.1` "ENGINE_PRIORITY 缺 4 引擎" **已过时**（现覆盖全 9 引擎）→ 已补完整性钉 `525aef8`；`TODO §4` "download 仅 1 用例" 亦不实（实测 12 用例 / 95.5%）。
3. `llamacpp` 硬编码 `--n-gpu-layers 999` 与 `--flash-attn on` 无说明性注释。
4. `sglang` metrics 的 2 个速率 gauge 恒为空列表（TODO §2.1 已登记）。
5. TUI readonly 门只在 vLLM 路径被直接测到（9/9 handler 有门，但覆盖面窄）。
6. `ChatParamsPanel` 空值不做 clamp；`views/chat/index.vue` 刷新无 `catch` 依赖全局 toast；`ChatRawPanel` 复制用可选链。
7. `F821` 字符串注解误报（`tests/test_stats_native.py:20`）。
8. `test_stats_native.py` 等 3 个文件 `E402`（模块级导入位置）属既有风格。
9. 本地 3.13 与 CI 3.12 的 ruff/mypy 必然有差异（口径以 CI 为准，见 known-pitfalls/build）。
10. **`web/tsconfig.json` 缺 `noEmit: true`**：脚本层靠 `vue-tsc --noEmit` 传参兜住，但任何一次**裸跑 `vue-tsc`**（手工排查、外部工具调用）都会把 73 个编译产物 `web/src/**/*.js` 直接吐进源码树，污染 `git status`。本轮收尾时发现并已 `git clean -fd web/src` 清理（逐条核对每个 `.js` 都有对应 `.ts`/`.vue` 源、孤儿数 0）。**已修 `add2913`**：加 `compilerOptions.noEmit: true`，并实测裸跑 `npx vue-tsc`（不带 `--noEmit`）exit 0 且工作区零新增产物。
11. **审计跳过无痕迹**（P1-4 的可观测性补丁）：**已修 `add2913`**——两条代理通道在 `target.audit_log is None` 时各加一条 `logger.warning("审计跳过 target=...")`，把"数据缺失但日志正常"变成可 grep 的信号。

---

## 4. 可优化项

**架构**

1. **注入面收敛**（P1-4 的根因层级）：`build_registry` / `build_groups` 各 new 实例是"同名对象多实例"的源头。建议二者共用一个 `GatewayModel` 工厂缓存（按 profile 名 memo），使 registry 与 groups 指向同一实例——比"在每个注入点记住两个集合"更可靠。收益：彻底消除注入遗漏类缺陷；代价：需复核家族路由是否依赖实例隔离（现无）。
2. **策略配置化**（TODO §1.2 仍成立）：thinking / reasoning_effort 白名单仍在源码。
3. **托管 venv 平台限制显式化**（TODO §3 高）：`_is_linux_managed` 抛错前，CLI/文档应先声明 Windows 不支持托管环境（本轮 llamacpp 走 vendor 解释器绕开，属个案）。

**一致性**

4. **审计写入的"可达集合"约束**（P1-4 通用法则）：所有 setter 注入的依赖，注入循环的遍历集合必须等于运行时全部可达对象集合；建议加一条对账断言"成功请求数 == 审计行数"进集成冒烟（本轮靠人肉数行发现）。
5. **anthropic / OpenAI 双协议审计口径对齐**（P2-2）。
6. **`isValid` 语义收敛**（P3-1）。

**可观测性**

7. **引擎定位类失败应打印搜索过的候选路径**：P1-3 的"缺 cmake"误导，本质是失败原因与搜索过程不可见。建议 `find_server` 未命中时在错误信息里列出全部候选。
8. **审计链路加"跳过原因"降级日志**：`audit_log is None` 时静默跳过应至少 DEBUG 一条，否则数据缺失无迹可循。

**测试基建**

9. **管理面按端点分桶补测**（P2-1）：`admin_models` 已起头（20 钉），`admin_config` / `admin_services` / `admin_router` 沿用同一夹具。
10. **沙箱/CI 重定向清单固化**：`UV_CACHE_DIR`、`TMP/TEMP`、`NPM_CONFIG_CACHE`、`PLAYWRIGHT_BROWSERS_PATH`、`MODELSCOPE_CACHE` + `MODELSCOPE_HOME` + `HF_HOME` 五组（本轮全部实跑有效），建议收进 `docs` 一节或 `conftest` 说明，避免每次重探。

---

## 5. 冒烟记录（真引擎 · 真模型）

环境：llama.cpp **b10809 CPU 预编译包**（18,407,457 B）+ **Qwen2.5-0.5B-Instruct-Q4_K_M**（397,807,936 B，ModelScope）；引擎 18910 / 网关 5003 / stats 5002 / webui 14173。原始日志 `raw/smoke-*.log`。

| # | 断言 | 实测 | 结论 |
|---|---|---|---|
| S1 | `modelctl start` 拉起真引擎且 `/health` 200 | llama-server PID 7140 监听 18910，health 200 | **PASS**（且**只有** P1-3 修复后才可能通过） |
| S2 | 直连引擎鉴权边界 + 真实生成 | 错 key 401 / 对 key 200，返回 "Hello! How can I assist you today" | **PASS** |
| S3 | 网关流式 SSE 完整 | 多行 `data:` chunk + 收尾 `data: [DONE]` | **PASS** |
| S4 | 两个信任域不可互换 | 管理面 `API_KEY` 打 `/v1` → 401；垃圾 key → 401 | **PASS** |
| S5 | 用量链路 | `data/usage-data/qwen2.5-0.5b-llamacpp.json` = prompt 39 / predicted 156（引擎 `/metrics` 轮询累计）；`/api/usage?model=all` 聚合数值正确但 `isValid:false` | **PASS**（附 P3-1） |
| S6 | 审计不含明文 key | `data/audit/*.jsonl` grep `smoke-*` / `sk-*` = **0 命中**，401 条目仅 `"auth":"invalid"` | **PASS**；修复后另加"成功请求数 == 审计行数"对账 |
| S7 | WebUI 拓扑 | SPA 深链 `/models/xxx` → **200**；`/admin/api/nope` → **404 JSON**；无 token 打 `/admin/api/overview` → **401** | **PASS** |
| S8 | 管理面 chat SSE（可选） | 未单独 curl；由 S7 + `tests/test_webui_admin_chat.py` 4 条契约钉覆盖 | 由单测覆盖 |
| S9 | 停服清理干净 | 全部服务停止；`15003/18910/5002/14173/5003` 监听数 = **0**；`llama-server` 进程数 = **0**；无 `.pid` 残留；无 GPU 锁 | **PASS** |
| S10 | 冒烟临时件清理 | `.env` 已删；`data/` 属 gitignore；`git status` 无泄漏 | **PASS** |

**沙箱约束（可复现性关键）**：禁写 `E:\cache\uv`、`%LOCALAPPDATA%`、`C:\Users\28654\.modelscope`、仓库外任意路径。ModelScope SDK 的配置目录由 **`MODELSCOPE_HOME`**（`modelscope_hub/constants.py:380`）控制，只设 `MODELSCOPE_CACHE` **无效** —— 必须 `MODELSCOPE_CACHE` + `MODELSCOPE_HOME` + `HF_HOME` 三者一并指进仓库。
**PS5 编码陷阱**：`Out-File -Encoding utf8` 会写 **BOM**，污染 `.env` 首行键名导致 `${API_KEY}` 插值失败；必须 `[IO.File]::WriteAllText`。curl 内联 JSON 引号会被剥 → body 落文件 + `--data-binary "@file"`。

---

## 6. TODO.md 存量缺口定性（只评估影响面，不实现）

| 登记项 | 本轮证据 | 定性 |
|---|---|---|
| §1.1 ENGINE_PRIORITY 缺 4 引擎 | 已覆盖全 9 引擎；补 `KNOWN_ENGINES ⊆ ENGINE_PRIORITY` 钉（`525aef8`） | **文档滞后**（已钉，防未来漏登记） |
| §2.1 llamacpp 速率 gauge / metrics 总量 | S5 实测 prompt/predicted 正确累计 | **行为与登记一致**，无新影响 |
| §2.1 unsloth `/metrics` 未验证 | 本轮无 unsloth 运行条件（需 `UNSLOTH_API_KEY`，profile 加载即被跳过） | **待验证**（移交：需真 key） |
| §2.2 tensorrt_llm 首跑不自动编译 | 读码确认仅 warning 不 `trtllm-build`，与设计一致 | **缺口**（另见 P2-3 是其派生缺陷） |
| §3 `_download.py` 仅 1 用例 | 实测 **12 用例 / 95.5%** | **文档滞后** |
| §3 托管 venv 仅 Linux | llamacpp 走 vendor 解释器可跑（S1），未触碰托管 venv 路径 | **缺口不变**；建议先做"平台声明显式化"（§4 第 3 条） |
| §4 新引擎测试加固（tokenspeed/lmdeploy/aphrodite） | tokenspeed 68.2% / tensorrt_llm 78.9% 仍低于线 | **缺口不变**（P2-7） |
| §4 `test_compat_flow` / `test_core_deps` 偏薄 | 无失败用例 | **缺口不变** |

---

## 7. 剩余缺口与建议（按优先级）

1. **管理面测试继续分桶推进**（P2-1）——本轮最强信号，`admin_config` / `admin_services` / `admin_router` 三个文件同时是类型最脏 + 覆盖最低。
2. **anthropic SSE 4xx 审计对齐**（P2-2）——审计是合规资产，协议间不一致迟早成为对账黑洞。
3. **registry/groups 单实例化**（§4 第 1 条）——从根上消灭 P1-4 这一类缺陷。
4. **`gateway/` 子环境 pip-audit 未跑成**——需网络稳定窗口或镜像源，**不得记为"已确认干净"**。
5. **firefox/webkit E2E**——换可写 `%LOCALAPPDATA%` 的机器或设全局 `PLAYWRIGHT_BROWSERS_PATH` 后补跑。
6. **unsloth `/metrics`、tensorrt_llm 首跑编译**——需真 key / 真 GPU 的移交验证项。
7. **剩余 P3 清单**（§3 P3 1-9）随迭代顺带处理，其中 **`isValid` 语义**性价比最高但**需产品决策**：它有真实消费方（`cli.py:597` / `:643` 用 `isValid` 决定是否显示速率），把"任一 target 不可用即 false"改成"至少一个可用即 true"是**行为变更**，不宜由测试轮顺手改。

---

## 附录 A 复现命令速查

```powershell
# 公共前置（沙箱必设，否则默认缓存目录被拦）
$env:UV_CACHE_DIR = "d:\Workplace\modelctl-1\.uv-cache"
$env:TMP = $env:TEMP = "d:\Workplace\modelctl-1\.tmp"
$env:NPM_CONFIG_CACHE = "d:\Workplace\modelctl-1\.npm-cache"
$env:PLAYWRIGHT_BROWSERS_PATH = "d:\Workplace\modelctl-1\.tmp\pw-browsers"

# 后端门禁
uv run ruff check src tests          # 272
uv run mypy src                      # 112 errors / 30 files
uv run pytest -q --cov=src/modelctl  # 2192 passed / 2 skipped / 81.61%

# 前端门禁（--prefix web；脚本名是 test 而非 test:unit）
npm --prefix web run typecheck       # 0
npm --prefix web run build           # 0
npm --prefix web test                # 79 passed / 13 files

# 四个 P1 的定点回归
uv run pytest tests/test_all_service.py -k gpu_lock
uv run pytest tests/test_engines_llamacpp.py -k "windows_prebuilt or platform_exe_name"
uv run pytest tests/test_audit.py -k group_route_success_writes_audit
cd web; npx vue-tsc --noEmit

# 真引擎冒烟（需 .env: API_KEY / WEBUI_HOST / WEBUI_PORT / MODEL_ROOT / GATEWAY_CLIENT_API_KEY）
uv run modelctl start llamacpp/qwen2.5-0.5b
uv run modelctl gateway start; uv run modelctl stats start; uv run modelctl webui start
uv run modelctl webui stop; uv run modelctl gateway stop; uv run modelctl stats stop
uv run modelctl stop qwen2.5-0.5b-llamacpp
```

## 附录 B 本轮改动清单

| 提交 | 类型 | 内容 |
|---|---|---|
| `6f915a7` / `dda4132` | docs | 实施计划 + 设计文档 |
| `0426cdf` / `68d32f3` | chore | 沙箱重定向目录 gitignore（uv cache / npm tmp） |
| `7ec7554` | **fix(P1-2)** | `web/src/api/client.test.ts` 用 `InternalAxiosRequestConfig` 修 vue-tsc |
| `fdc0c18` | test(P2-8) | `test_envfile.py` 改断结构不变量，不再依赖检出目录名 |
| `19ba4d5` | test(T3) | `test_webui_admin_models_lifecycle.py` 20 钉（启停幂等 / restart 语义 / yaml_override） |
| `3e91e5a` | test(T2) | `test_webui_admin_chat.py` 4 条跨层契约钉 |
| `ba4201b` | test(T1) | `ChatHistoryList` 5 + `ChatStatsPanel` 6 用例 |
| `bab8dcb` | **fix(P1-1)** | `start_profile` 失败三出口归还 GPU 锁 + 5 钉 + pitfall |
| `525aef8` | test(T4) | `ENGINE_PRIORITY ⊇ KNOWN_ENGINES` 完整性钉 |
| `9071b50` | **fix(P1-3)** | llamacpp 定位 `.exe` 与根目录布局 + 2 钉 |
| `53c38e9` | **fix(P1-4)** | 注入覆盖 registry+groups 并集 + 审计回归钉 + 2 篇 pitfall |
| `56a1ff0` | docs | 本复核报告 |
| `add2913` | fix(P3-10/11) | `web/tsconfig.json` 加 `noEmit`；两条代理通道在 `audit_log` 缺失时 warning |

新增/更新 pitfall：`backend/gpu-lock-release-on-start-failure.md`、`backend/llamacpp-windows-prebuilt-exe-name.md`、`backend/group-route-members-miss-audit-injection.md`（索引 3 行已入 `docs/known-pitfalls/README.md`）。

**过程中的自我纠错（如实记录）**
1. 初次覆盖率对照拿上轮 **line%** 比本轮 **combined%** → 误判 4 个模块"齐降"；同口径复核为 +0.5/+2.5/0/+0.9，**结论撤销**，规则写入 `coverage-hotspots.md` 头部。
2. 阶段 3 写用例时引用了不存在的夹具 `monkeypatch_key_env`，以及误用 `engine_config:` 顶层键（真实 schema 是顶层 `<engine>:` 段，`profile.py:180`）→ 均为**测试自身错误**，产品行为正确。
