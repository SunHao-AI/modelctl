# 全量检测修复方案设计（2026-09-11）

> 输入基线：`docs/health-checks/2026-09-10-full-scan-report.md`
> 范围裁决：P0+P1 全集 + 与 P1 同源 P2；纯 P3 延后
> 基线策略：先定 HEAD 基线，再在当前工作区（含 35 个未提交改动）之上叠加修复
> 验收标准：聚焦 P1，每项配最小回归测试；工具链门仅复现定位，不强求全量清零
> 拆分方案：方案 A 风险优先横切（前置门禁 → 计划 1/2/3）

---

## 第 1 节｜设计总纲

### 目标

把检测报告中的 1 条件 P0 + 18 P1（+ 同源 P2）收敛为可执行、可回归、可验收的修复集合。

### 修复模式归类（决定每项怎么改、测试怎么写）

| 模式 | 说明 | 代表项 |
|---|---|---|
| A 输入边界加固 | 客户端可控值进入裸转换/裸比较前先兜底 | GW-P1-1、WEB-P2-1 |
| B 资源生命周期闭合 | 含早退在内的**所有**分支都要 release/settle | GW-P1-2、WEB-P1-2、WEB-P1-4 |
| C 状态传递去全局化 | 用显式参数替代 `os.environ` 全局态 | WEB-P1-3 |
| D 密钥/信任域隔离 | 去掉跨域回退、独立密钥；脱敏名单式→模式式 | GW-P2-5、WEB-P1-1 |
| E 契约对齐 | 后端返回键 = 前端读取键；路由 key 口径统一 | X-P1、GW-P1-3 |
| F 阻塞卸载 + 规模约束 | 同步 SQLite 卸 `to_thread`；加数据保留策略 | CLU-P1-1、CLU-P1-2、CLU-P2-1 |
| G TUI 投产门 | 渲染去副作用 + 真主循环 + 跨平台键盘 | TUI-P1-1/2/3 |
| H 工具链门 | 只复现定位，不追求全量清零 | ruff panic / CI 口径 / echarts XSS |

### 交付结构与依赖

```
阶段 0 前置门禁（先做，否则分不清存量/新增）
  ├─ 0.1 git worktree 定 HEAD 基线归属
  ├─ 0.2 3 个 docker_setup 测试桩跟进 05c0a27
  └─ 0.3 compat_flow conftest 补隔离 → pytest 0 failed
       │
       ├─ 计划 1 安全与线上故障（8 项）──┐
       ├─ 计划 2 跨层契约与规模韧性（5 项）─┤ 三者无硬依赖，可并行
       └─ 计划 3 TUI 投产门 + 工具链门（6 项）─┘
```

计划 3 的 TUI-P1-1 与计划 1 的 WEB-P1-3 同根因（渲染/传参期副作用 + 全局态），复用同一"只读探测"思路，除此之外各计划独立。

### 统一验收框架（每个修复项 4 条，缺一不判完成）

1. **最小回归测试**：先写能复现该缺陷的测试（红）→ 修复后转绿。
2. **子域回归**：所在测试模块全绿。
3. **冒烟复跑**：隔离端口 14173/15003 重跑 `smoke_check`，无 500、无槽泄漏。
4. **锚点核对**：本 spec 记录的文件/行为锚点与改动后代码一致。

### 显式不改动范围

- 纯 P3 风格项（约 45 项）
- 真实引擎/GPU 端到端（无 GPU 环境）
- CLAUDE.md 引用的 `docker/init/01-schema.sql`（仓库不存在，仅记文档待办，不新建）

---

## 第 2 节｜阶段 0：前置门禁

### 0.1 HEAD 基线归属

**做法**：`git worktree add ../modelctl-baseline HEAD`，在纯净 HEAD 上复跑 `mypy src/modelctl` 与 `pytest tests`（uv 临时注入 httpx/fastapi/uvicorn/bcrypt/pyjwt/pytest-cov），产出两份基线清单：
- `mypy` 错误集合 B_mypy（当前工作区 109 条 → 减去 B_mypy = 未提交改动新增）
- `pytest` failed 集合 B_test（当前 4 failed → 区分 HEAD 既有 vs 新增）

**产出**：`docs/health-checks/raw/baseline-head-{mypy,test}.txt`（仅本地留档，不入正式报告）。
**退出条件**：能明确回答"某条错误是存量还是本次引入"。
**约束**：worktree 只读；结束后 `git worktree remove` 清理；**不**修改用户工作区。

### 0.2 docker_setup 测试桩跟进 `05c0a27`

**现状**：`tests/test_docker_setup_pull.py` 的 `_FakeProc.stdout=""` 未跟进 `05c0a27` 对 pull 输出解析的改动（`_InterruptProc` 无 `stdout` 属性、dead-mirror 早退逻辑），导致 HEAD 既有 failed。

**做法**：按 `05c0a27` 后 `docker_setup` 的真实读取契约补齐桩（`stdout`/`stderr`/`wait`/`poll` 语义对齐），使其覆盖新解析分支而非绕过。
**验收**：`pytest tests/test_docker_setup_pull.py` 全绿。

### 0.3 compat_flow 顺序污染隔离

**现状**：`tests/test_compat_flow.py::test_run_compat_checks_falls_back_to_current_env_when_no_venv` 单跑绿、全量跑红。根因：全量跑时 `sys.path` 的 site-packages 被其他用例劫持成 uv 缓存临时路径，而 `compat._current_site_packages()` 用 `min(candidates, key=lambda p: len(p.parts))` 取层级最浅者。

**做法**：在 `tests/conftest.py` 的 `isolated_runtime_dirs` autouse fixture 增加"site-packages 稳定化"隔离——按现有 `CLUSTER_*` 前缀式 delenv 同风格，把会污染 sys.path 顺序的环境（`PYTHONPATH`）显式 `delenv`，并冻结 `sys.path` 快照供 compat 用例断言（具体键以 0.1 复现结果为准，优先最小改动：仅 `delenv("PYTHONPATH", raising=False)`）。
**验收**：`pytest tests`（全量）**0 failed**（4→0），且单跑仍绿。

---

## 第 3 节｜计划 1：安全与线上故障（8 项）

### 1.1 WEB-P1-1 配置脱敏名单式→模式匹配（含条件 P0）

- **锚点**：`admin_config.py:31` `_SENSITIVE_KEYS = {"API_KEY", "UNSLOTH_API_KEY"}`；`read_env:111-114` 用 `k in _SENSITIVE_KEYS` 判定。
- **风险**：任何含密钥语义的键（`*_SECRET`/`*_TOKEN`/`*PASSWORD`/`GATEWAY_CLIENT_API_KEY`/`ACCOUNTS_JWT_SECRET` 等）经 `/admin/api/config/static` **明文回显**。
- **方案**：新增 `_is_sensitive_key(key)`：命中显式集合 **或** 后缀/子串模式（`KEY`/`SECRET`/`TOKEN`/`PASSWORD`/`PASSWD`，大小写不敏感）即敏感；`read_env` 改用该函数。掩码算法 `_mask_value` 不变（`***`+末4位）。
- **测试**：构造含 `FOO_SECRET`/`X_API_KEY`/`DB_PASSWORD` 的临时 .env，断言 `entries[].sensitive==True` 且 `value.startswith("***")`；普通键（`MODEL_ROOT`）仍明文。

### 1.2 GW-P2-5 JWT 独立密钥（去掉 API_KEY 回退）

- **锚点**：`account_auth.py:74-87 _jwt_secret()` 三段策略（`ACCOUNTS_JWT_SECRET`→回退 `API_KEY`→RuntimeError）；`_JWT_SECRET_FALLBACK_ENV="API_KEY"`。
- **风险**：管理面 API_KEY 与账号面 JWT 签名共用一把秘密——管理面泄漏即可伪造任意用户 JWT（跨信任域）。
- **方案**：去掉 `API_KEY` 回退段；`ACCOUNTS_JWT_SECRET` 为空时**保持**抛 RuntimeError（不静默生成随机密钥，避免重启后旧 token 全失效且难排障）。启动路径若无该变量则 fail-fast，错误信息指向"请设置 ACCOUNTS_JWT_SECRET"。
- **测试**：仅设 `API_KEY` 不设 `ACCOUNTS_JWT_SECRET` → `_jwt_secret()` 抛 RuntimeError；两者都设 → 用 `ACCOUNTS_JWT_SECRET` 签发/验签。
- **部署提示**：spec 注明这是**破坏性变更**，需在 `.env` 补 `ACCOUNTS_JWT_SECRET`（属配置动作，非 DDL，交用户）。

### 1.3 GW-P1-1 max_tokens 安全解析（未认证即 500）

- **锚点**：`gateway.py:157` `completion = max(1, int(body.get("max_tokens") or 1))`（位于 `_accounts_tpm_estimate`）。异常在 `accounts_gate` 之前抛出 → 未认证客户端传 `max_tokens:"abc"`/超大值 → 500。
- **方案**：安全解析 `max_tokens`：非 int/非 str-digit → 视为 1；负数 clamp 到 1；合理上限（如 10_000_000）封顶防 TPM 溢出。封装 `_safe_int(value, default=1, lo=1, hi=...)`。
- **测试**：`max_tokens="abc"` → est 不抛异常、走 TPM 估算=1；`max_tokens=10**18` → 封顶；正常值不受影响。

### 1.4 GW-P1-2 流式上游 ≥400 早退漏 release（并发槽永久泄漏）

- **锚点**：`gateway.py:1617-1638`：流式分支中 `if upstream.status_code >= 400: ... return Response(...)`，**直接 return 而非 StreamingResponse** → `_sse_stream()` 的 `finally`（L1764-1802 的 release+settle）永不执行。对照 `anthropic_proxy:1198-1203` 同分支已正确 release。
- **方案**：在该 `return Response(...)` **之前**补 `_release_if_acquired()`（本文件 OpenAI 代理已有该闭包 L1515-1522）。不 settle（非 2xx 保留干净 usage_records，与既有 404/502 早退一致）。
- **测试**：accounts 启用 + 并发上限=N，反复打上游恒 400 的流式请求 >N 次，断言不出现持续 429（槽被回收）。

### 1.5 WEB-P1-2 非法 body 永久假 429（pending 不回滚）

- **锚点**：`admin_envs.py:349` `create_task` → L352 `_user_pending.add(task.id)` → L355 才校验 `max_concurrent_downloads`，非法 `return 400` **不回滚 pending**。`_DOCKER_MAX_ACTIVE_PER_USER=3` 且 pending 无过期 → 累计 3 次非法请求后该用户永久 429。
- **方案**：把 400 校验**前移**到 `create_task`/`_user_pending.add` 之前（先校验后建任务）。保留 429 判定顺序在 400 之前不变（L341-347）。
- **测试**：连续 3 次非法 `max_concurrent_downloads` → 第 4 次合法请求仍 202（证明 pending 未被污染）。

### 1.6 WEB-P2-1 非 ASCII 凭据 hmac 500（compare_digest 转 bytes）

- **锚点**：`admin_auth.py:71`（`is_valid_key`）与 `:95`（`require_auth`）均 str↔str `hmac.compare_digest`，非 ASCII → `TypeError` → 500（冒烟实锤：login non-ascii-key → 500）。
- **方案**：两处比较前 `.encode("utf-8")`（或统一封装 `_consteq(a,b)`）。语义不变，仅消除 500，非 ASCII 错误 key 仍走 401。
- **测试**：Bearer 头传 UTF-8 中文 key → 401（非 500）。

### 1.7 WEB-P1-3 GPU 传参去全局态（并发互污）

- **锚点**：三处同构写 `os.environ["MODELCTL_GPUS"]` 再 finally 恢复——`admin_models.py:190-194/219-223`（_do_start）、`:235-239/262-267`（_do_restart）、`admin_services.py:367-371/406-411`（_do_all）；消费端 `engines/base.py:74 selected_gpus()` 读该 env。
- **风险**：`to_thread` 并发跑多个 start/restart 时，全局 env 被互相覆盖 → GPU 分配错乱。
- **方案**：新增"显式 gpu 覆盖参数"通道。最小侵入：`EngineAdapter` 增加实例级 `gpu_override`（构造后、`check_requirements` 前注入），`selected_gpus()` 优先读 `self._gpu_override`，回退 `os.environ`。webui 侧改为把 `gpus` 传入 `start_profile`/`restart_profile`（新增可选参 `gpus`），在函数内构造 adapter 后 set override，**不再写 os.environ**。CLI `--gpus` 保持写 env（单进程无并发，无互污）。
- **测试**：并发两次 `start_profile(gpus="0")` 与 `start_profile(gpus="1")`，断言各自 adapter 读到正确 GPU、互不覆盖（用假 caps/monkeypatch build_command 捕获）。

### 1.8 WEB-P1-4 任务锁提前释放（create_task 后立即 release）

- **锚点**：调用侧模式 `lock = await tm.acquire(...)` → try 内 `create_task`+`ensure_future` → `finally: await tm.release(...)`——`admin_config.py:155/171`、`admin_models.py:461/484`、`admin_services.py:162/178`、`:244/259`、`:316/331`。锁在任务**刚提交**就释放，非"任务结束"释放 → 同 target 并发重复投递。
- **方案**：锁的生命周期应覆盖 worker 协程。改为 worker 协程（`_do_*`）负责 `finally: release`，端点 handler 仅在"未成功投递 worker"的异常路径释放。统一封装：`ensure_future` 包一层，worker 完成时释放；handler try 体成功 ensure_future 后**不再** release（转移所有权），异常时 release。
- **测试**：同 target 连发两次 start，断言第二次因锁被 worker 持有而 409（或第一次 worker 结束前无法重复投递）。

---

## 第 4 节｜计划 2：跨层契约与规模韧性（5 项）

### 2.1 X-P1 nginx-snippet 三点一线（功能恒空白）

- **锚点**：后端 `admin_config.py:97 return {"content": content}`；前端 `types.ts:485 snippet: string` + `ConfigView.vue:32 r.snippet ?? ''`。键名不一致 → 前端恒取空串 → 生成器永远空白（冒烟实锤 keys=['content']）。
- **方案**：**改后端对齐前端契约**（前端已有 `ok`+`snippet` 类型定义，且 snippet 语义更贴切）：`return {"ok": True, "snippet": content}`。
- **测试**：`GET /admin/api/nginx-snippet?node=&host=` 返回体含 `snippet` 非空键；前端类型无需改。

### 2.2 GW-P1-3 context switch 口径错配（永不错命）

- **锚点**：`gateway.py:1572 apply_context_switch(registry, context_rules, target.name, ...)`。经 group 路由后 `target.name` 已是成员名（如 `deepseek-v4-flash-vllm`），而 `context_rules` 的 key 是 base/group 名 → `rules.get(body_model)` 恒 miss → 上下文切换永不生效。
- **方案**：传"路由前请求语义键"。在 `resolve_model` 之前保留原始请求模型名（或 group 名）用于规则匹配。最小改动：`apply_context_switch` 增加 `match_keys: list[str]`（依次试 `请求原始 model` → `target.group` → `target.name`），命中任一即用；调用处传入 `body.get("model")` 与 `target.group`。
- **测试**：配一条规则 key=`deepseek-v4-flash`（group 名），请求该 group → 断言切到 high 变体（当前实现下该测试必红）。

### 2.3 CLU-P1-1 WS 内同步 SQLite 头阻塞（卸 to_thread）

- **锚点**：`admin_cluster.py:642/647/654 ws_cluster` 在 `async def` 内直调 `reg.handle_heartbeat` / `store.append_event` / `_sweep_if_due()`——均逐条 commit + `BEGIN IMMEDIATE`（`store.py:596`），阻塞 event loop。
- **方案**：把这些同步 DB 调用用 `await asyncio.to_thread(...)` 卸载；ack 发送在 await 之后。WS 单连接内消息仍需保序 → 同一连接的处理保持顺序 await（不同连接天然并发，不引入乱序）。
- **测试**：心跳/事件消息处理期间模拟 DB 慢（monkeypatch append_event sleep），断言 WS 接收循环不阻塞其他协程（用可观测的并发 counter）。

### 2.4 CLU-P1-2 events 表无保留策略（无界增长）

- **锚点**：`store.py:586-602 append_event` 全仓无 DELETE/purge/prune（其他表有 `DELETE`，events 无）→ 中心台账 events 表无界增长 + 逐条 commit。
- **方案**：`append_event` 内按阈值轻量清理（复用 `sweep_expired` 同风格）：每写入 N 条或按 `ts` 保留最近 M 天/最近 K 条，超出 `DELETE FROM events WHERE ts < ?`。**DDL 不动表结构**（纯 DML DELETE，符合 CLAUDE.md）。保留阈值常量集中定义。
- **测试**：插入 >保留上限条事件，触发 append，断言旧事件被裁剪、总数不超上限。

### 2.5 CLU-P2-1 docker 下 GPU 锁 owner 秒退误删

- **锚点**：`all_service.py:208-214` `update_gpu_lock_owner(profile.name, pid)` 未被 `is_docker` 包裹。docker runtime 下 `pid` 是秒退的 `docker run` 客户端 PID，`gpu_lock._read_lock` 用 `is_pid_alive(pid)` 判定 → 容器还在跑但锁被判 stale 并 `unlink` → GPU 互斥静默失效。
- **方案**：docker 分支不调 `update_gpu_lock_owner`（锁 owner 保持 controller 进程语义，或写入容器级 sentinel）；仅 venv runtime（write_pid=True 的长驻 PID）才 update owner。用 `if not is_docker:` 包裹现有块。
- **测试**：mock docker runtime start，断言未调用 `update_gpu_lock_owner`（venv 分支仍调用）。

---

## 第 5 节｜计划 3：TUI 投产门 + 工具链门（6 项）

### 3.1 TUI-P1-1 健康预检渲染有副作用（条件 P0）

- **锚点**：`tui/panels/detail.py:214-216` 与 `tui/panels/plan.py:288-290` 在渲染"健康预检"Tab 时 `adapter_cls(profile, caps); adapter.check_requirements()`。而 `vllm/tokenspeed/tensorrt_llm.check_requirements` 内含 **`clear_stale_docker_container`（删容器）** 与 **`acquire_gpu_lock`（占锁/改文件）** 副作用——浏览 TUI 即误删运行中容器、抢占 GPU。
- **方案**：给引擎增加**只读探测**能力（如 `precheck_readonly()` 或 `check_requirements(write=False)`），仅做校验、跳过 `clear_stale_docker_container`/`acquire_gpu_lock` 等写操作；TUI 两处改用只读模式。副作用版仍用于真实 start 路径。
- **测试**：TUI 预检渲染后，断言未调用 `clear_stale_docker_container`/`acquire_gpu_lock`（monkeypatch 记录）。

### 3.2 TUI-P1-2 非 smoke 无主循环（一帧即退）

- **锚点**：`tui/app.py:87-105 run()`：`smoke=False` 走 `loop_begin(); render_once(); loop_end(); return 0`——只渲染一帧即退出；keybar 承诺的方向键/切换/quit 全无实现。
- **方案**：实现真实事件循环：`loop_begin()` 进入 raw 模式；`while True: render_once(); key=read_key_block(timeout=帧间隔); 按 Key 分发（q=退出、方向键切 Tab/profile、r=刷新等，对齐 keybar 文案）`；`loop_end()` 已在 restore_term。KeyboardInterrupt 兜底保留。
- **测试**：注入预置按键序列（q）→ `run(smoke=False)` 返回 0；注入序列验证至少能处理"切换+退出"两类事件而不崩（headless，用 fake keyboard）。

### 3.3 TUI-P1-3 Windows 键盘方向键解析错误

- **锚点**：`keyboard.py:183-185`：`if code < 0x20 or code in (0x1B,0x7F): code = msvcrt.getch()`。方向键在 Windows 下 `getwch` 返回前缀 `0x00`/`0xE0`，`0xE0`(224) 不 `< 0x20` 也不在 ESC/DEL 集合 → 不取第二字节，方向键被误判；`0x00` 落 `<0x20` 分支但语义处理不完整。
- **方案**：显式识别 `0x00`/`0xE0` 前缀 → 再 `getch()` 取扫描码 → 映射到方向键/F 键枚举（补全 Windows 扩展键表）；控制字/Ctrl 组合键分支单列。
- **测试**：以 msvcrt 打桩（返回 `0xE0`+`0x48`）断言解析为 Key.Up。Windows-only，用 `sys.platform` 条件或 monkeypatch `_is_windows`。

### 3.4 工具链门-A：ruff panic 定位 + 升级评估

- **现状**：`ruff check src tests` 触发 98 项 panic（ruff 自身 bug），是检查盲区。
- **方案**（仅定位，不强清零）：二分/分文件定位触发 panic 的具体文件与规则；给出 ruff 升级后是否消除的结论；升级不可行则在 `ruff.toml`/`pyproject` 对该规则局部 `per-file-ignores` 并注释原因。
- **产出**：`docs/health-checks/raw/ruff-panic-triage.md`（触发文件清单 + 定性）。

### 3.5 工具链门-B：CI 口径对齐定性

- **现状**：CI 用 Python **3.12**（`.github/workflows/ci.yml:14`，`pyproject.toml:51` mypy 亦 `python_version = "3.12"`），本地实际 3.13；`requires-python = ">=3.12"`。本地工具链与 CI 版本/口径存在漂移。
- **方案**：仅**定性**记录本地 vs CI 的 ruff/mypy 版本差与结果差（哪些差异来自版本、哪些来自环境），产出对齐建议（是否本地钉版到 CI 版本）。不强制本地结果与 CI 完全一致。
- **产出**：并入 `docs/health-checks/2026-09-10-full-scan-report.md` 的工具链章节补记。

### 3.6 工具链门-C：echarts XSS

- **核查结论（自审更新）**：`web/package.json:16` 声明了 `echarts: ^5.6.0`，但全量检索 `web/src` **无任何** echarts 使用点（`echarts`/`setOption`/`tooltip`/`chart`/`canvas` 均零命中）——即 echarts 是"声明但未引入"的依赖，当前不存在 tooltip formatter / `innerHTML` 注入面，报告的 "echarts XSS" 在现有代码中**不可达**。
- **方案（改为依赖清理，非 XSS 修复）**：从 `web/package.json` 移除未使用的 `echarts` 依赖并 `npm install` 刷新 lockfile——既消除报告担心的潜在注入面，也减小前端包体。若后续确有图表需求再按需引入并统一转义。
- **验收**：`npm run build` 通过；`grep -r echarts web/src` 仍零命中；`package.json` 不再含 echarts。

---

## 第 6 节｜测试、错误处理与风险

### 测试策略

- 每项遵循"红→绿"：先提交能复现缺陷的失败测试，再改实现。
- 复用 `tests/conftest.py::isolated_runtime_dirs`（autouse）隔离运行时目录；新增隔离键遵循其既有 `delenv` 风格。
- 网关测试走 httpx ASGITransport（uv 临时注入依赖），不改 lockfile。
- 冒烟复跑用隔离端口 14173/15003 + 独立 `LOG_DIR/CACHE_DIR/USAGE_DATA_DIR/AUDIT_DIR`，绝不复用 4173/5003。

### 错误处理一致性

- 鉴权类失败一律 401（非 500）；客户端可控输入非法一律 400（非 500）；上游失败保持 502/透传。
- release/settle 全部 try-except + logger.warning，绝不因释放异常中断响应。
- JWT 密钥缺失 fail-fast（RuntimeError 指向明确变量名），不静默兜底。

### 主要风险与缓解

| 风险 | 缓解 |
|---|---|
| GW-P2-5 破坏性（去 API_KEY 回退使旧部署登录失败） | spec 明示需在 .env 补 ACCOUNTS_JWT_SECRET；错误信息清晰；属配置动作非 DDL |
| WEB-P1-3 adapter gpu_override 改动触及多引擎 | 仅加实例属性 + 读取优先级回退，默认行为不变；覆盖 start/restart 全链路测试 |
| CLU-P1-2 events 裁剪误删审计需要的事件 | 阈值保守（保留足够窗口）；纯 DELETE 不动 schema；可配置常量 |
| 计划 3 TUI-P1-2 主循环是较大新增 | 放最后；headless fake keyboard 保证可测；不影响 smoke 路径 |
| echarts XSS 可能误报 | 先复现再改；复现不了则降级观察，不臆造代码 |

### 完成后沉淀（执行后，非本设计阶段）

修复落地后按 CLAUDE.md 渐进式披露写入 `docs/known-pitfalls/`：摘要层加条目，详情按 backend/frontend 分类聚合。本设计阶段不写 pitfalls。
