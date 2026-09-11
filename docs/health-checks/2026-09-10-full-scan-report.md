# modelctl 全项目健康检查报告（一次性检测）

- 日期：2026-09-10
- 分支：`feature/tui`（含 ~35 个未提交工作区改动，未做 HEAD 基线 diff）
- 范围：CLI / TUI / WebUI 后端 / Web 前端 / gateway+accounts / cluster+engines（全覆盖）
- 深度：工具链 + 覆盖率 + 依赖安全 + 服务冒烟（不含真实引擎/GPU 端到端）+ 六域人工审查 + 跨层一致性
- 处置：仅报告，未修复任何代码；DDL/DML 未执行
- 中间产物：`docs/health-checks/raw/`（`coverage.json`、`smoke_check*.py`）

> 严重级：P0=可利用/必崩 · P1=高概率故障/重大信息暴露/鉴权 · P2=边界与一致性 · P3=风格与可维护性

---

## 0. 执行摘要

| 检测维度 | 命令/方式 | 结果 |
|---|---|---|
| 后端 lint | `ruff check src tests script` | ❌ 229 项（含 **ruff 自身 panic 98 项→部分文件未被检查**，见 §1.1） |
| 后端类型 | `mypy src/modelctl`（本地 Py3.13） | ❌ **109 错误 / 34 文件**（Py3.13 本地口径，CI 为 3.12，见 §1.2） |
| 测试 | `pytest tests`（注入 cov/httpx/fastapi/bcrypt/pyjwt） | ❌ **1819 passed / 4 failed / 2 skipped**（34 分钟） |
| 覆盖率 | `--cov=modelctl` | **81%**（14445 stmts） |
| 前端类型 | `vue-tsc --noEmit` | ✅ 0 错误（strict、零 any） |
| 前端构建 | `vite build` | ✅ 成功 |
| 依赖安全 | `pip-audit` ×2（主+gateway） | ✅ 无已知漏洞 |
| 前端依赖 | `npm audit` | ⚠️ 1 个 moderate（echarts XSS，`<6.1.0`） |
| 服务冒烟 | 隔离端口 14173（19 项断言） | ❌ 3 项实锤 P1/P2（见 §3） |

**总体判断**：工程纪律高（known-pitfalls 沉淀在主干被广泛吸收、SSE/鉴权/SQL/GPU 权威参数等历史坑多数已闭环），但存在**一批可稳定复现的 P1**，集中在"同一修复只落一半 / 多路径兜底不全 / 跨层契约错接"三类。当前 TUI 属"可演示、不可投产"，WebUI 审计页已在磁盘版本修复（审查窗口读到旧版）。**无已确证的可利用 P0**。

问题计数（去重后）：**P0 条件性 1 · P1 18 · P2 约 30 · P3 约 45**。

---

## 1. 工具链结果

### 1.1 Ruff — ❌ 含 ruff 自身崩溃（P2，检查盲区）

> **【2026-09-11 更正】** 下方"ruff bug，需升级版本"的定性**已被推翻**：panic 根因是本地 `.ruff_cache` 损坏，`ruff clean` 后 panic 归零，与版本/文件内容无关；真实告警数是 **272**（不是 229），"CI 疑点"亦随之消解（CI 全新 checkout 不复用本地缓存）。详见 `docs/health-checks/raw/ruff-panic-triage.md` 与 §1.5。以下为原始记录。

`ruff check src tests script` → `Found 229 errors`，其中：

- **`panic: 98`（P2）**：ruff 在 lint 过程中 panic（ruff bug），panic 的文件**未被完成检查**——这意味着"229"不是完整数字，存在检查盲区。触发文件需隔离复现并升级 ruff 版本。
- 其余按规则：`E501 line-too-long 55` / `B008 16` / `I001 unsorted-imports 15` / `F401 unused-import 13` / `B904 raise-without-from 11` / `F541 7` / `UP037 6` / `UP035 4` / `E741 2` / `UP017 1` / `UP041 1`；47 项可 `--fix`（本次按约未执行）。
- `script/` 单独：✅ All checks passed。
- **CI 疑点（P2）**：`.github/workflows/ci.yml` 每次都跑 `ruff check src tests`，而本地存在 229 项——要么 CI 长期为红/被忽略，要么 ruff 版本差异。二者都值得核实。

### 1.2 Mypy — ❌ 109 错误（P2，新功能未过类型门）
`Found 109 errors in 34 files`。分布（错误数）：`webui/admin_models 18` · `webui/admin_services 9` · `cli 8` · `tui/keyboard 7` · `accounts/store 7` · `tui/app 6` · `accounts/limits 6` · `gateway 5` · 其余零散。典型：

- `cli.py:1492` `EngineAdapter has no attribute build_compile_command`（**与 CLI P1-1 同源**，见 §4.2）
- `tui/app.py`/`tui/data.py` 大量 `_SnapshotBase has no attribute ...`（快照基类缺类型契约，TUI 新功能）
- `tui/panels/{detail,plan}.py` `EngineAdapter 第2参 object|None vs Capabilities`（**与 TUI P1 副作用同源**）

> 说明：本地为 Python 3.13，CI 用 3.12；未与 HEAD 基线 diff，故**无法区分存量 vs 本次未提交改动引入**（列入 §7 未覆盖）。

### 1.3 前端类型/构建 — ✅
`vue-tsc --noEmit` 0 错误（`strict + noUnusedLocals + noUnusedParameters`）；`vite build` 成功。

### 1.4 pytest — ❌ 4 失败（3 真实回归 + 1 顺序污染）

| 用例 | 定性 | 根因 | 级别 |
|---|---|---|---|
| `test_docker_setup_pull::test_ensure_image_streams_progress` | **真实回归**（单跑亦失败） | commit `05c0a27` 把 `for line in proc` 改为 `for line in proc.stdout`，但 `_FakeProc.stdout=""`（空串可迭代出 0 行）→ `max(seen)` 空 → `ValueError` | P2 |
| `test_docker_setup_pull::test_ensure_image_kills_proc_on_keyboard_interrupt` | **真实回归** | `_InterruptProc` 无 `stdout` 属性 → `AttributeError`（实现读 `proc.stdout`，测试桩未跟进） | P2 |
| `test_core_docker_setup::test_ensure_image_stops_on_dead_mirror` | **真实回归** | 同上：fake 的 dead-mirror 错误行走不到早退分支 → 重试 5 次（`assert 5 == 1`） | P2 |
| `test_compat_flow::test_run_compat_checks_falls_back_to_current_env_when_no_venv` | **顺序污染**（单跑通过） | 全量时 site-packages 被其他用例劫持为 uv 缓存临时路径——`test-isolation.md` 记录的同族污染，`conftest` 未覆盖该键 | P2 |

> 前 3 项生产代码/测试文件均无未提交改动（`git status` clean），属 **HEAD 既有失败**；CI 在 ubuntu 跑，需核实是否同样红。

### 1.5 CI 对齐定性（2026-09-11 补记，工具链门-B）

CI（`.github/workflows/ci.yml`）口径：`ubuntu-latest` + `setup-uv@v5` + **Python 3.12**，依次跑
`uv sync --extra dev` → `ruff check src tests` → `mypy src/modelctl` → `pytest tests/ -q`。
`pyproject.toml` 的 dev extra 是**浮动版本**（`ruff>=0.4`、`mypy>=1.10`），`[tool.ruff].target-version`
与 `[tool.mypy].python_version` 均钉 `py312`。

| 工具 | 本地（Win / Py3.13） | CI（ubuntu / Py3.12） | 差异定性 |
| ---- | ---- | ---- | ---- |
| ruff | 0.16.5；带缓存跑 → 100 panic（缓存损坏） | 每次全新 checkout，无 `.ruff_cache` 残留 | **不存在版本差异导致的分歧**；差异纯由本地缓存损坏产生，`ruff clean` 即对齐（见 §1.1 更正） |
| mypy | 2.3.1 **INTERNAL ERROR 崩溃、零诊断**（见 `raw/baseline-head-mypy.txt`） | 未本地复现 | 本地 mypy 在 Py3.13 上不可用；本报告 §1.2 的 109 错误来自更早一次可运行的采集，**当前本地无法复算**，属工具链门 |
| pytest | 3.13；HEAD 基线 worktree 全量 1 failed / 1822 passed | 3.12（平台亦不同） | 失败项均可在本地复现（docker 桩契约 3 项 + compat_flow 1 项），非平台专属 |

**结论与建议**：

1. ruff 侧无需 bump 版本、无需 per-file-ignore；把「本地出现 `panic:` 先 `ruff clean`」写进门禁口径。
2. mypy 与 pytest 属 **3.13/Windows 本地口径 vs 3.12/ubuntu CI 口径**的双向漂移：本地 mypy 直接崩溃，无法作为发布门。
3. **发布门建议**：合入前用 `uv run --python 3.12 --with ruff --with mypy ...` 在 3.12 上跑一次
   ruff + mypy + pytest，作为与 CI 同口径的门禁；本地 3.13 以 **pytest 全量 + 隔离端口冒烟**为硬门禁
   （本次修复即按此口径验收）。

---

## 2. 覆盖率地图（81%）

**零覆盖 / 极低覆盖模块（优先补测）**：

| 模块 | 覆盖率 | 说明 |
|---|---|---|
| `core/webui/frontend.py` | **0%** | 162 stmts 全未测（SPA 兜底路由/404 JSON 逻辑） |
| `core/tui/panels/base.py` | **0%** | 死代码（`section_title/error_flash/keybar` 全仓无引用） |
| `core/tui/__main__.py` / `__main__.py` | **0%** | 入口 |
| `core/webui/admin_models.py` | **35%** | 564 stmts，最大管理面文件，覆盖最低 |
| `core/webui/admin_config.py` | 38% | trtllm build / config 写侧 |
| `core/colors.py` | 40% | CJK 对齐基准函数本身覆盖低 |
| `core/webui/admin_services.py` | 50% | |
| `core/webui/admin_router.py` | 53% | 子路由注册（异常静默 P2） |
| `core/all_service.py` | 65% | 启停编排核心 |
| `cli.py` | 77% | 1456 stmts（体量最大文件） |
| `core/gateway.py` | 81% | 972 stmts |

**结构性缺口**：`cli.py` 无独立单测文件（仅经 `test_cli_*` 间接）、`gateway/` 无独立测试工程、`webui/` 16 文件仅 `test_webui_*`/`test_admin_*` 3–4 个文件。

---

## 3. 服务冒烟（隔离端口 14173，运行时实锤）

起 `create_app(admin=True)` 于隔离端口/独立数据目录（不复用你正在跑的 4173/5003，跑毕回收）。19 项断言，**实锤问题**：

| 断言 | 结果 | 关联 |
|---|---|---|
| 免鉴权 `/health`；错误 key `login`→401；Bearer `overview`→200；未知 API→404 JSON（非 SPA HTML）；账号面未启用→503 明确 | ✅ | 鉴权骨架/SPA 兜底正确 |
| **`POST /v1/chat/completions` body `max_tokens="abc"` 或 `["x"]`** | **→ 500** | **GW-P1-1 实锤**（§4.3） |
| **`login` / `overview` 非 ASCII key（Bearer 中文）** | **→ 500**（期望 401） | **WEB-P2-1 实锤**：`hmac.compare_digest(str,str)` 非 ASCII TypeError（§4.2） |
| `config/static` 响应形状 | `{path,exists,entries}` | 前端类型全错（WEB/X-P2，§4.4）；脱敏名单仅 2 键（WEB-P1-1，见下） |
| `config/static` 脱敏精确核验 | 当前 `.env` 的 `API_KEY/UNSLOTH_API_KEY` 已正确 `***` 掩码；因当前 `.env` **无** `*_TOKEN/*_SECRET/ACCOUNTS_JWT_SECRET` 键，**本次未实际泄漏** | WEB-P1-1 属"名单 vs 配置键演化"设计缺陷（触发前提见 §4.2） |
| **`nginx-snippet` 响应形状** | 后端 `{content}` | **前端读 `.snippet` → 恒空 → 功能失效**（X-P1，§4.4 实锤） |
| `audit` / `audit/stats` / `audit/cleanup` | `{entries,error_count,total}`/`{by_day,...}`/body `{days,dry_run:false}` | **前端契约在当前磁盘版本已一致**（审查旧版结论作废） |

---

## 4. 分域问题清单（P0/P1/P2）

### 4.1 TUI（`core/tui/`）— 可演示、不可投产

- **TUI-P1-1｜渲染有副作用（条件 P0）** `panels/detail.py:215` `panels/plan.py:289`：渲染"健康预检"Tab 时真调 `adapter.check_requirements()`，引擎实现里含 `clear_stale_docker_container`（`docker rm -f` 可杀运行中容器）、`acquire_gpu_lock`（写锁文件抢占）。**用户切一下 Tab 即可能杀容器/抢 GPU 锁**；唯一纯度守护 `test_tui_no_side_effects` 恰未覆盖 precheck 路径。
- **TUI-P1-2｜非 smoke 无主循环** `app.py:run()`：`loop_begin→render_once→loop_end` 一帧即退，keybar 承诺的按键全无实现，`modelctl tui` 实际不可交互（T6 未接线）。
- **TUI-P1-3｜Windows 方向键/控制键错乱** `keyboard.py:_read_windows:178`：`getwch` 后又 `getch` 二次取键→控制键阻塞；方向键前缀 `0x00/0xE0` 落入 `<0x20` 分支→按 ↑ 触发 `Key.H`。
- TUI-P2：`app.py:realize_render_once` 先 revalidate 后 sync 名→切模型后日志串数据最多 1s；`main_dashboard:_profile_row` CJK 名超 COL_NAME 不截断→行溢出 rich 软换行错位；翻页后高亮用页内下标 vs 全局 active_index 错位；`data.py` 与 `panels/cluster.py` 降级哨兵串各写→中心宕机 SoloStub 永不命中；`detail:_render_yaml` 非 UTF-8 YAML 直接崩（`UnicodeDecodeError` 非 `OSError`）；`keyboard.restore_term` 恒 no-op（接主循环后会泄漏 raw mode）；smoke 路径 `save_theme(..,None)` 污染真实 cache。
- TUI-P3：`panels/base.py` 整模块死代码（连带 `HardwareSnapshot.probe_errors` 无 UI 出口）；每帧全仓 rglob 扫盘刷 50+ WARNING；monitor 并排渲染两次、合并丢色；cluster `_summary_line` 漏计 stale；`_console_size` 宽度<80 抬到 80 掩盖而非解决窄屏。

### 4.2 WebUI 后端（`core/webui/`）— 鉴权扎实，配置面与任务并发是风险中心

- **WEB-P1-1｜config 面密钥脱敏"名单式"过窄（条件 P0，触发即升）** `admin_config.py:31` `_SENSITIVE_KEYS={API_KEY,UNSLOTH_API_KEY}`：`GET /admin/api/config/static` 明文回显 `.env` 中**除这 2 键外的全部键值**。经运行时精确核验：当前 `.env` 恰无 `*_TOKEN/*_SECRET/ACCOUNTS_JWT_SECRET` 类键，故**本次未实际泄漏**；但按 `.env.example` 规划会写入的 `GATEWAY_CLIENT_API_KEY`（数据面客户端钥）/`CLUSTER_JOIN_TOKEN`/`CLUSTER_NODE_TOKEN`（集群准入凭据）一旦被设置即明文回显；**尤其若启用账号体系并配 `ACCOUNTS_JWT_SECRET`（或它回退到 API_KEY，见 GW-P2-5），持有管理员 API_KEY 者可离线拿到 JWT 签名密钥→伪造任意 is_admin token**→P0。根因是"名单枚举 vs 配置键演化"的赛跑（与 `test-isolation.md` conftest 白名单同族）。修复：脱敏改 `*_KEY/*_SECRET/*_TOKEN/PASSWORD` 模式匹配。
- **WEB-P1-2｜docker_install 校验后置→永久假 429** `admin_envs.py:349`：先 `create_task`+写 `_user_pending` 再校验 `max_concurrent_downloads`，非法值 `return 400` 不回滚 pending；`_DOCKER_MAX_ACTIVE_PER_USER=3` 且无过期→同用户 3 次非法请求后**永久 429**，仅重启可复。
- **WEB-P1-3｜MODELCTL_GPUS 全局态传参→并发启动互污** `admin_models.py:_do_start/_do_restart` `admin_services.py:_do_all`：以 `os.environ["MODELCTL_GPUS"]` 写-跑-finally 恢复传 gpus，锁仅按 target 隔离；不同模型并发 start 时互相覆盖/误恢复→按错 GPU 起或误报 RequirementError（known-pitfalls 已记录的测试污染变体的 WebUI 侧）。
- **WEB-P1-4｜任务锁"创建即释放"+`is_active` 死代码** `admin_tasks.py:205`：全部调用侧在 `finally` 于 create_task 后立即 release，同模型 start 未结束即可创建第二个并发任务（抢端口/GPU）；`is_active()` 有定义零调用。
- WEB-P2：**P2-1 `hmac.compare_digest(str,str)` 非 ASCII→500**（`admin_auth.py:71/95`，冒烟实锤，陷阱文档已给 `.encode()` 解法，漏网）；P2-2 `admin_models.py:902` 导入笔误 `modelctl.capabilities`（应 `modelctl.core.capabilities`）被宽 except 吞→docker json-log 增量推送静默失效（已实测 `ModuleNotFoundError`）；P2-3 账号 JWT 无撤销通道（删号 8h 内 JWT 仍全功能，承诺的 invalidate 未实现）；P2-4 后台线程跨 loop `put_nowait` 不安全（对比 `admin_models` 的 `call_soon_threadsafe` 正确写法）；P2-5 `admin_router._include_subrouter` 静默吞 ImportError→整组端点无告警变 404；P2-6 `_run_trtllm_build_sync` `subprocess.run` 无 timeout→永久 running；P2-7 `TaskManager.trim()` 无终态保护可裁 running 任务；P2-8 `process.tail_file` 全量读文件取尾行（引擎日志数百 MB）。
- WEB-P3（摘）：`assert row is not None` 用于生产；`_mask_key` ≤4 位返全量；login 无限速；`docker/system-action restart` 直接 `shutdown /r /t 5` 仅单钥保护；REST 端点 `async def` 内直调同步 SQLite（配合 CLU-P1-1）。

### 4.3 gateway + accounts — 安全基线扎实，"多路径兜底不全"是主患

- **GW-P1-1｜未认证即 500（实锤）** `gateway.py:157` `_accounts_tpm_estimate`：`int(body["max_tokens"])` 对客户端可控值裸调，异常发生在 `accounts_gate` **之前**→错误 key 的匿名请求也 500，且绕过鉴权审计（`float('inf')`→OverflowError 同族）。
- **GW-P1-2｜proxy 流式 ≥400 早退不 release→并发槽永久泄漏** `gateway.py:1605`：该分支不返回 StreamingResponse→SSE 生成器 `finally` 的 release/settle 永不执行；`concurrency_limit=1` 账号连发 2 次 stream-400→第 2 次直接 429（自我 DoS）。对照 `anthropic_proxy:1185` 同分支已正确 release（漏改复制）。
- **GW-P1-3｜context switch 传 `target.name` 与规则 key 口径错配→按官方文档配置整体静默失效** `gateway.py:1560`：`apply_context_switch(..., target.name, ...)`，但 group 路由后 target 是成员（`deepseek-v4-flash-vllm`），规则 key 是 group 名（文档 B.3 示例即此）→永不命中切不到 `-high`；测试夹具名恰等于规则 key 故全绿（最恶性变体）。
- GW-P2：P2-1 `_tpm_pending` 无界增长（所有"放行不 settle"路径只 release→pending 键永留，实测只增不减）；P2-2 生产把 `time.time()` 传给按 `_mono` 设计的 `check_rpm/check_tpm_reserve`→NTP 回拨窗口异常；P2-3 预算检查 TOCTOU（并发同时过 `check_budget`，budget 跨组件最终一致→软预算超支窗口）；P2-4 上游 4xx/5xx body 不脱敏透传并入审计（内部堆栈/路径风险），死端口 502 message 空串信息量为零；**P2-5 JWT 密钥回退管理面 `API_KEY`**（`account_auth.py:56`，两信任域共用一把秘密）。
- GW-P3（摘）：`GATEWAY_HOST` 缺省 `0.0.0.0`；`read_timeout` 兼作 connect 超时（600s≈无超时）；`add_tpm_actual(est=)` 死参数；`budget_reset_at=NULL`→未 touch 用户预算永不周期重置；CJK `chars//4` 低估 4×。
- 已查干净：双通道嗅探/恒定时间比较/fail-closed/验完即丢/SQL 参数化白名单/JWT iat-exp-int-秒/HS256 固定/SSE 手工生命周期/事务边界——**无 P0 鉴权绕过/注入**。

### 4.4 跨层一致性（含冒烟实锤）

- **X-P1｜nginx-snippet 键名错接（实锤）** 后端 `admin_config.py:97` 返回 `{content}`，前端 `types.ts:482`+`ConfigView.vue:32` 读 `.snippet ?? ''`→**生成成功但界面永远空白**，无报错。
- X-P2：`config/static` 后端 `{path,exists,entries}` vs 前端 `StaticConfigResponse{version,default_model,port,paths}` 全错（`ConfigView` 靠 `JSON.stringify` 侥幸显示）；`GetLog.path` 前端非可选、后端从不返回。
- X-P2：**`.env.example` 死键 `NODE_ID/NODE_HOST`**（注释称 nginx map 用，实由 CLI/REST 参数传入，改之无效，纯误导）；反向有 7 个代码读但 example 缺的键（`MODELCTL_GPUS`/`MODELCTL_TORCH_CUDA_MAJOR`/`ACCOUNTS_JWT_SECRET` 等）。
- X-P3：后端为 UI 下发但前端未声明/未消费的键 8 处（`runtime_ready/docker_ready/reachable`… 零命中）；`trtllm build/status` 后端有 Web 无（仅 CLI）；`auth.ts` 失败体声明 `message?` 实为 `{error:{code,message}}`。
- X-死数据（P3）：`cluster/store.py` `model_states` 6 列（endpoint_url/metrics_p50_ms…）DDL 有代码从不写、`metrics_rollups/token_ops/audit` 建表零读写、`nodes.gateway_url` 死列；CLAUDE.md 引用的 `docker/init/01-schema.sql` 仓内不存在（规范脱节）。
- X-yaml（P3）：`tool_call_rounds/max_output_tokens`/`gateway.thinking_*` 是 Profile 一等字段但 0/51 yaml 声明（功能空置）；7 份 yaml 无 `usage`→stats 按硬编码默认价 1.0/2.0 静默计费；vllm 7 份 `kv_cache_dtype: fp8` 绕过 `compat_rules` 只看 `quantization` 的 fp8 CC 预检。
- **✅ 健康主干**：cluster 全端点/models 主体/services/tasks-SSE/accounts schema 跨层逐键吻合，统一 snake_case。

### 4.5 Web 前端（`web/src/`）— 良好（B+）

- **FE-P1｜已消解**：审查窗口检出 `AuditLogView`+`api/audit.ts` 与后端"4 连脱节"（清理只试运行/筛选无效/错误数恒 0/14 天图空）。**经运行时+磁盘源码复核，当前版本前后端契约已一致**（`admin_audit.py` 已支持 level/keyword/total/error_count/by_day，`audit.ts` 已用 body `{days,dry_run:false}`）——结论作废，无需修。
- FE-P2：`ServicesMatrixView:77` 全家启停丢弃 TaskRef 未 `tasksStore.track()`→脱离任务中心、仅 2.5s setTimeout 刷新（全家启动远超）；`AuditLogView:95` 清理成功后确认框不关；`Header.vue:53` 健康徽章轮询被注释→永不更新。
- FE-P3：时间格式 3 处偏差（`AuditLogView:181` 缺年份、`Probe/Settings` `toLocaleString` locale 漂移）违反 `YYYY-MM-DD HH:mm:ss`；keep-alive/name 条款全 0/13（无 `<keep-alive>` 故暂无影响，引入即全失效）；`ModelDetailView` 不 watch `route.params.name`→A→B 复用最长 5s 展示旧数据；SSE 401 无登录联动（靠轮询兜底登出有假死窗）；散落脚本/索引 key/死代码若干。
- **✅ 亮点**：TS strict 零 any；轮询防重叠、竞态快照、reactive 代理回读、tick 存活期四大陷阱均按正例落地；无 `v-html`；无 P0。

### 4.6 cluster + engines — 状态机不变式扎实，规模化与静默失效是短板

- **CLU-P1-1｜async WS 内直调同步 SQLite+全量哈希→event loop 头阻塞** `admin_cluster.py:641`：`async def ws_cluster` 里直调 `handle_heartbeat/append_event/_sweep_if_due`（逐条 commit + 8MiB YAML sha256 + BEGIN IMMEDIATE 全表扫），33 个 REST 端点同样 `async def` 内直调 store；64worker×10s 心跳+轮询即饱和 loop→超 `ping_timeout`→大面积 stale+重连风暴（64-worker keepalive 同族）。
- **CLU-P1-2｜events 表无保留策略→无界增长** `cluster/store.py:586`：全仓无 `DELETE FROM events/purge/prune`；心跳 overflow/每 action/每重连各一条，payload 无上限（`goal.update` 带 YAML 原文可达 256KiB）。
- CLU-P2：**P2-1 docker 启动 GPU 锁 owner 写秒退的 `docker run` PID→锁被陈旧清理删除→互斥静默失效**（`all_service.py:208` `update_gpu_lock_owner` 未被 `is_docker` 包裹，`gpu_lock.py:50` `is_pid_alive` 判假即 unlink）；P2-2 `stop_instance` 的 `pkill -f <引擎名>` 全局杀→同引擎多 profile 互杀（`process.py:413`）；P2-3 同 stem 跨引擎"本地手起实例"被 `setdefault` 丢→中心 GPU 占用视图缺项→gate 超卖 OOM（stem-ledger 修复只做到上报层，采集层顶层键仍二维）；P2-4 tensorrt_llm 两分支漏拼 `api_key_args()`→配了 api_key 仍以无鉴权启动且无失败信号；P2-5 rotate-token 后 worker 永不自愈（无 401→join-token 降级回路，须人工改 .env 重启）；P2-6 ollama `post_start` 裸 `urlopen(timeout=600)` 异常穿透+占死 reconcile 单写者锁（引擎已起却判 fail）；P2-7 WS 无入站帧尺寸/并发/失败速率闸门（accept 早于鉴权，`uvicorn` 未设 `ws_max_size`）；P2-8 下载失败 `rmtree(destination)` 连带删既有分片（GGUF 目录 `_is_populated` 判 False→误删在服役权重）。
- CLU-P3（摘）：`touch_heartbeat` 无条件写 online 短暂盖 disabled；`goal_id_of` 不含 engine→同 stem 双引擎 goal 中心侧不能共存，但注释/迁移文档描述"支持"误导后续维护者；`_default_docker_alive` 跨模块取 `adapter._container_name` 私有属性；`deps.py` 三处 `subprocess.run` 无 timeout；docker 分支 `model_local.parent` 极端布局可挂 `/`。
- **✅ 已查干净**：命令拼装零 shell 注入（list argv）、令牌熵+fail-closed+防跨节点劫持、连接世袭来者胜、单写者锁模型、幂等四道去重、WAL+busy_timeout、进程终止杀进程组、GPU 选择校验、docker/venv 分支四件套一致、`bind_host` 权威顺序——**无 P0**。

---

## 5. 依赖安全

- `pip-audit` 主环境：✅ No known vulnerabilities（editable modelctl 跳过）。
- `pip-audit` gateway 环境（含 fastapi/uvicorn/bcrypt/pyjwt）：✅ No known vulnerabilities。
- `npm audit`：⚠️ **echarts `<6.1.0` XSS（moderate，GHSA-fgmj-fm8m-jvvx）**，`npm audit fix --force` 需升 echarts@6（breaking）。建议评估 echarts 6 迁移或上游补丁。

---

## 6. 已检测且结论为"干净"的项（避免"没查"被误读为"没问题"）

- **鉴权**：WebUI 双体系约 70 路由 0 漏挂、fail-closed、恒定时间比较；gateway 双通道嗅探不强求 Bearer；bcrypt rounds=12、JWT iat/exp int 秒 HS256 固定。
- **注入**：全后端无 SQL 拼接（白名单列名+参数化）、无 shell 注入（list argv、无 `shell=True`）。
- **路径穿越**：`/{name}/log`、`/{name}/yaml` 无法穿越（Starlette path param 不含 `/`）。
- **前端三大已沉淀陷阱**（computed-tick / polling-overlap / reactive-proxy）全部按正例落地；定时器/SSE 清理成对。
- **known-pitfalls 主干吸收**：`is_running_any`（除 `status_all`）、PYTHONIOENCODING 末位注入、Windows TZ pop、display_width（除 `cli._print_table` 本地复刻）、conftest env 隔离、`bind_host` 权威值、GPU 锁目录即时解析——历史坑多数已闭环。

---

## 7. 未覆盖范围声明

1. **真实引擎/GPU 端到端**：未拉起任何引擎，未占 GPU，未下载模型——GW-P1-2/3、CLU-P2-4 等的生产级触发未做端到端复现（依静态+内存 ASGI 推断）。
2. **TUI 交互主循环**：`app.run()` 非 smoke 未实跑（依赖尚不存在的 T6 主循环）。
3. **HEAD 基线 diff**：未区分"存量 vs 本次未提交改动引入"（工作区 ~35 改动未 stash/worktree 比对）；mypy 109 与 3 个 docker 测试失败的归属需 git worktree 检出 HEAD 复跑确认。
4. **生产数据库**：全程未连任何真实库（依 CLAUDE.md 严禁线上 DDL/DML）。
5. **mypy 版本口径**：本地 Py3.13，与 CI 3.12 可能有差异。
6. **并发压力**：CLU-P1-1、GW-P2-3、WEB-P1-3/4 的竞态为静态+小规模推断，未做高压复现。

---

## 8. 建议修复优先级（仅排序，本次不改）

1. **安全/信息暴露**：WEB-P1-1（脱敏模式匹配，条件 P0）→ GW-P2-5（JWT 独立密钥）。
2. **可稳定复现的线上故障**：GW-P1-1（500）→ GW-P1-2（槽泄漏）→ WEB-P1-2（假 429）→ WEB-P2-1（compare_digest 转 bytes，一行）。
3. **跨层/口径**：X-P1（nginx-snippet）→ GW-P1-3（context switch 三点一线）→ CLU-P2-1（docker GPU 锁）→ CLU-P1-1（心跳卸载 to_thread）。
4. **测试红灯**：3 个 docker_setup 桩跟进 `05c0a27` + compat_flow 污染 conftest 补 delenv。
5. **TUI 投产门**：TUI-P1-1（precheck 只读变体+守护测试补 precheck）→ P1-2/P1-3（接主循环时一并修终端所有权）。
6. **工具链门**：查 ruff panic 触发文件并升级 → CI ruff/mypy 与本地对齐 → echarts XSS。
