# 全覆盖复核方案设计（2026-09-12）

> 输入基线：`docs/health-checks/2026-09-10-full-scan-report.md`、`docs/health-checks/2026-09-11-test-coverage-report.md`、`docs/TODO.md`
> 范围裁决：后端工具链 + 全量 pytest、前端 E2E、真实服务/引擎冒烟、TODO.md 存量缺口定性 —— 四项全选
> 交付定位：复核 + 补测 + 报告 **三者等量**（不追覆盖率数字提升）
> 缺陷处置：P0/P1 当场修 + 回归钉 + 沉淀 known-pitfalls；P2/P3 只入报告
> 冒烟路径：llama.cpp 官方 CPU 预编译包 + `llamacpp/qwen2.5-0.5b`（ModelScope GGUF）
> 组织方式：方案 A「基线先行 · 风险驱动」

---

## 第 1 节｜设计总纲

### 目标

对 modelctl 做一次**全覆盖复核**，产出：

1. 本轮实跑的工具链与覆盖率数字（不用上轮数字代替）
2. 带 P0–P3 分级的缺陷清单 + 可优化项分级清单
3. 针对真实怀疑点的回归用例（不为抬覆盖率而写）
4. 真引擎冒烟的可复现记录

### 与既有两轮报告的关系

| 轮次 | 定性 | 本轮如何衔接 |
|---|---|---|
| 2026-09-10 全量健康检查 | 仅报告不改码（P0 条件性 1 / P1 18 / P2 ~30 / P3 ~45） | 抽查其 P1 是否真闭环（尤其 TUI 三项、gateway 三项） |
| 2026-09-11 测试覆盖报告 | 补测 + 基建 + 门禁（+248 用例，4 缺陷全修） | 承接其 §9「剩余缺口与建议」6 条 |
| **本轮 2026-09-12** | 复核 + 补测 + 报告等量 | 重点覆盖 09-11 之后的新代码（81 个文件 diff） |

### 显式不改动范围

- 覆盖率门禁保持 `fail_under=80`，不上调
- CI 五 job 拓扑不改
- `docs/TODO.md` 内的功能补齐（只定性影响面，不实现）
- `docs/superpowers/` 下历史 spec/plan 不改写
- `CLAUDE.md` 规范条款不改

### 缺陷 vs 缺口的裁决规则（避免"P0/P1 当场修"与"TODO 不实现"打架）

同一条问题既在 TODO.md 登记过、又可能被判为 P1 时（典型：T4 的 `ENGINE_PRIORITY` 缺 4 个新引擎），按下述口径归类：

| 归类 | 判据 | 处置 |
|---|---|---|
| **缺陷** | 实现与既有 spec/契约/文档承诺不一致；或本轮改动新引入；或存在"改坏即故障"的静默错误路径 | 按级别走 P0/P1 当场修流程 |
| **存量缺口** | TODO.md / 上轮报告已显式登记为"未实现/降级/待验证"，且行为与登记时一致 | 只在 §6 定性影响面，不当场实现 |

即：**已登记且行为未变 = 缺口**；**行为与承诺不符 = 缺陷**。归类结论写入报告 §3 或 §6，二者不重复计数。

---

## 第 2 节｜环境事实与约束（2026-09-12 实测）

| 项 | 实测值 | 对方案的影响 |
|---|---|---|
| uv / Python / Node | 0.12.1 / **3.12.13** / v24.16 | 与 CI 的 Py3.12 口径一致，无版本漂移 |
| GPU | **GTX 1060 6GB（CC 6.1，Pascal）** | vLLM/SGLang 需 CC ≥ 7.0 → **本机 GPU 引擎路线不可用** |
| `.venv` / `node_modules` / Playwright 浏览器 | **全部缺失** | 阶段 1 必须先 `uv sync` + `npm ci` + `playwright install` |
| 引擎二进制 | `llama-server`/`ollama`/`vllm` 均不在 PATH | 走 CPU 预编译包（见 §6） |
| `data/`、`.env` | 均不存在 | 冒烟需自建最小 `.env`，测后删除 |
| docker | CLI 在，**daemon 未运行** | docker 分支不入冒烟 |
| 构建链 | `gcc`/`g++`/`mingw32-make`/`nvcc(11.5)`/`wsl` 在；**`cmake`/`ninja`/`cl` 缺失** | 无法从源码编译 llama.cpp → 必须下载预编译包 |
| 网络 / 磁盘 | 清华源 200 可达；D 盘 115.9 GB 空闲 | 依赖安装与 GGUF 下载可行 |

### 已确认的加载路径

`engines/llamacpp.py` 的 `find_server()` 依次探测 `source/build/bin/llama-server` 与 `source/llama-server`；
`source` 取 `cfg["source_dir"]` → `LLAMACPP_SOURCE_DIR` → `PROJECT_ROOT.parent/llama.cpp`。
`pre_start()` 在产物已存在时**不校验 cmake/git**，因此把预编译包解压到
`d:\Workplace\llama.cpp\`（目录内含 `llama-server.exe`）即可被加载，无需编译。

---

## 第 3 节｜四阶段编排

```
阶段 1 基线取证（并行编排，避免串行等待）
  ├─ 1.1 uv sync --extra dev --extra test           ← 前置，其余后端步骤依赖它
  ├─ 1.2 后台：pytest -m "not perf" --cov（本地约 39 min）
  ├─ 1.3 并行：ruff check / mypy / pip-audit
  ├─ 1.4 并行：npm ci → vue-tsc / vitest --coverage / npm audit
  └─ 1.5 npm run build → playwright install → E2E（chromium 必跑，三内核视下载情况）
       │  ★ 检查点 C1：数字表落盘，交用户过目
阶段 2 风险清单（数据驱动排序）
  └─ 三集合求交：本轮 81 文件 diff × 覆盖率升序 × TODO.md 存量缺口
       │  ★ 检查点 C2：清单排序交用户过目
阶段 3 定点审查 + 补测
  ├─ 3.1 逐条读码找缺陷（跨层契约优先）
  ├─ 3.2 P0/P1 → 先写复现测试（红）→ 修 → 绿 → 沉淀 known-pitfalls
  └─ 3.3 P2/P3 → 只入报告
       │  ★ 检查点 C3：每修一项跑子域套件
阶段 4 真实服务 + 小模型冒烟
  ├─ 4.1 生产拓扑起 webui（同源 API + SPA）
  ├─ 4.2 起 gateway（数据面 key）
  ├─ 4.3 llama-server CPU 包 + llamacpp/qwen2.5-0.5b 闭环
  └─ 4.4 冒烟断言清单逐条实测
       │
收尾 全量门禁复跑（确认无回退）→ 报告 → known-pitfalls 沉淀
```

### 阶段 1 判定口径

| 维度 | 命令 | 取数方式 |
|---|---|---|
| 后端 lint | `uv run ruff check src tests script` | 告警数 + 规则分布 |
| 后端类型 | `uv run mypy src/modelctl` | 错误数 / 文件数 / 按文件分布 |
| 后端测试 | `uv run pytest tests/ -q -m "not perf" --cov=modelctl --cov-branch` | passed/failed/skipped + 覆盖率 |
| 覆盖率热区 | `--cov-report=json` → 按行覆盖率升序 | 逐模块百分比 + miss 数 |
| 安全红线 | `uv run pytest tests/ -m security` | 用例数 / 结果 |
| 性能基线 | `uv run pytest -m perf --benchmark-autosave` | 与上轮 §6 基线相对比 |
| 依赖漏洞 | `uv run pip-audit`（主 + gateway 两份） | CVE 计数 |
| 前端类型 | `npm run typecheck` | 错误数 |
| 前端单测 | `npm run test:coverage` | 用例数 + 模块覆盖率 |
| 前端依赖 | `npm audit` | 漏洞分级计数 |
| E2E | `npx playwright test` | passed/failed/skip |

---

## 第 4 节｜初始风险清单（阶段 2 按实测重排）

依据：本轮 `git log --since=2026-09-11` 命中 81 个 `src`/`web` 文件 + 文件体量 + 上轮遗留缺口。

| 编号 | 风险条目 | 证据 | 级别预判 |
|---|---|---|---|
| **T1** | chat 前端组件与视图**零测试**：`ChatHistoryList.vue`/`ChatParamsPanel.vue`/`ChatRawPanel.vue`/`ChatStatsPanel.vue`/`views/chat/index.vue` 均无用例（同目录 `ChatComposer`/`ChatMessage` 有） | 本轮 diff + `web/src` 用例分布 | P2 |
| **T2** | chat SSE 代理跨层契约：`webui/admin_chat.py`（新文件）转发到网关数据面；SSE 分帧、上游 502 header 形状（`c06f556` I-1）、abort 语义（`9e699ee`）三处交叉 | commit `223c158`/`c06f556`/`9e699ee` | P1 |
| **T3** | `webui/admin_models.py` 910 行，上轮行覆盖 **35.7%**，本轮又被改动；启停/日志流/启动进度 × 引擎状态矩阵 | 上轮 §9.1 + 本轮 diff | P2 |
| **T4** | `core/gateway.py` 1747 行本轮改 2 次（`prepare_openai_upstream` 深拷贝 `0c58420`、状态导出 `5693222`）。**注意**：TODO.md §1.1 所称「`ENGINE_PRIORITY` 缺 4 个新引擎」经核实**已过时**——[gateway.py:56-59](file:///d:/Workplace/modelctl-1/src/modelctl/core/gateway.py#L56-L59) 已登记全 9 引擎。本条改为「文档滞后」类缺口 + 补一条登记完整性断言钉 | TODO.md §1.1（过时）+ 本轮 diff | P3（文档）|
| **T5** | TUI 重构：`tui/{app,keyboard}.py` + `panels/{detail,plan}.py`；上轮记录 `_SnapshotBase has no attribute` 系列与"渲染期副作用"类 P1 | 09-10 报告 §1.2 + 本轮 diff | P1 |
| **T6** | 存量薄弱：`all_service.py` 65.5% · `cli.py` 1798 行 77.2% · `process.py` 79.8% · `colors.py` 40.1% · `engines/_download.py` 仅 1 用例 | 上轮 §9 + TODO.md §3/§4 | P2 |
| **T7** | 本轮 14 个 `engines/*.py` 全被改动，需核对 `build_command` 参数与 `metrics_mapping` 在 9 个适配器间的一致性 | 本轮 diff | P2 |
| **T8** | cluster 侧 `goals.py`/`reconcile.py`/`store.py` + `webui/admin_cluster.py` 本轮均改动；`reconcile.py` 837 行是收敛状态机核心 | 本轮 diff | P1 |

**排序原则**：跨层契约裂缝 > 零测试的新代码 > 大文件低覆盖 > 风格项。

---

## 第 5 节｜冒烟设计（阶段 4）

### 准备

1. 从 llama.cpp 官方 Release 下载 **CPU 版 Windows 预编译包**（资产名形如 `llama-*-bin-win-cpu-x64.zip`；实际 tag 与资产名在阶段 4 执行时经 GitHub Releases API 取最新 stable 确定，不写死版本号），解压到 `d:\Workplace\llama.cpp\`，确认 `llama-server.exe` 位于该目录根（匹配 `find_server` 第二候选）
2. 建最小 `.env`（仅 `API_KEY`、`GATEWAY_CLIENT_API_KEY`、`ACCOUNTS_JWT_SECRET`、`MODEL_ROOT`），**测后删除**
3. 端口固定（避开 18888-18896 / 18909 的既有占用段）：webui `14173`、gateway `15003`（沿用 09-10 冒烟端口口径）、llamacpp `18910`（`models/llamacpp/qwen2.5-0.5b.yaml` 现值）。开跑前先探测三者空闲。

### 断言清单

| # | 断言 | 验证手段 |
|---|---|---|
| S1 | `modelctl start llamacpp/qwen2.5-0.5b` 拉起 → `/health` 200 | CLI + `Invoke-WebRequest` |
| S2 | 直连引擎端口带错误 key → 401；带正确 key → 200 | 引擎回环鉴权 |
| S3 | 经 gateway 数据面 key 打 `/v1/chat/completions` → 流式返回完整 | SSE 帧计数 |
| S4 | 管理面 key 打数据面 → 401（两域不互换） | 契约复核 |
| S5 | 用量统计出现该请求的 prompt/completion tokens | `modelctl stats` |
| S6 | 审计日志落盘且**不含明文密钥**（sha256 摘要比对） | 审计文件核对 |
| S7 | webui 生产拓扑：SPA 深链回 `index.html`、API 前缀 404 为 JSON、401 形状恒 `{"code":"auth"}` | `Invoke-WebRequest` |
| S8 | WebUI chat 视图经 `/admin/api/chat/completions` 代理流式可用 | 浏览器或 HTTP |
| S9 | `modelctl stop` 后 PID 文件清理、端口释放、无孤儿进程 | 进程与端口探测 |
| S10 | `--n-gpu-layers 999` + `--flash-attn on` 在 CPU-only 二进制上的真实行为 | **不预判**，实测记录 |

### 已知连带风险

`llamacpp/qwen2.5-0.5b.yaml` 硬编码 `--n-gpu-layers 999`（[llamacpp.py](file:///d:/Workplace/modelctl-1/src/modelctl/engines/llamacpp.py#L354-L366)）。CPU-only 二进制下该组合可能直接退出。
**处置原则**：这是真实发现，按级别记入报告，**不改 profile 绕过**。若确需继续跑通 S3–S8，用 `.env`/临时副本覆盖参数，并在报告中标注"该组合为人工构造，非仓库默认"。

---

## 第 6 节｜报告结构与验证纪律

### 报告骨架（`docs/health-checks/2026-09-12-full-coverage-review.md`）

```
0. 执行摘要         维度 × 命令 × 结果 三列表 + 总体判断 + 缺陷计数
1. 工具链结果       ruff / mypy / pytest / vue-tsc / vitest / E2E / pip-audit / npm audit
2. 覆盖率热区       升序表 + 本轮 diff 标记 + 门禁判定（80%）
3. 缺陷清单         编号 · 级别 · 位置 · 根因 · 复现命令 · 状态
4. 可优化项         架构 / 一致性 / 可观测性 / 测试基建 四类，各带收益与代价
5. 冒烟记录         S1–S10 断言 × 实测结果
6. TODO.md 缺口定性 存量项的本轮影响面评估（不实现）
7. 剩余缺口与建议   按优先级，含"待验证"移交项
附录 A 复现命令速查
附录 B 本轮改动文件清单
```

严重级沿用仓库约定：**P0** 可利用/必崩 · **P1** 高概率故障/重大信息暴露/鉴权 · **P2** 边界与一致性 · **P3** 风格与可维护性。

### 验证纪律（每条都不可豁免）

1. **数字必须本轮实跑**——禁止引用 09-10/09-11 的数字代替本轮结果。
2. **缺陷必须可复现**——每个缺陷附一条可复制粘贴的复现命令或失败用例名。
3. **修复必须闭环**——P0/P1 遵循「复现测试（红）→ 修复（绿）→ 子域回归 → 全量门禁复跑」四步，四步齐备才在报告中标"已修复"。
4. **全量门禁基线**——任何生产代码改动后复跑 `pytest -m "not perf" --cov`，对照上轮 2118 passed / 2 skipped / 80.38%，出现回退不得收尾。
5. **pitfall 沉淀**——每个已修 P0/P1 按渐进式披露写入 `docs/known-pitfalls/`（摘要索引 + 主题聚合文件）。
6. **测试隔离**——新增用例须遵守 `tests/conftest.py` 的隔离约定（`CACHE_DIR`/`LOG_DIR`/`AUDIT_DIR`/`USAGE_DATA_DIR` + `GATEWAY_*`/`CLUSTER_*` delenv），新增 env 键若影响控制流须同步补 delenv。
7. **分层 marker**——新测试文件按命名约定落地（`test_security_*` / `test_webui_*` / `*_http` / `*_cli` 等），确保 `-m` 筛选不漏用例。

### 完成判据

- [ ] §3 阶段 1 十一项维度全部有本轮实测数字（含失败项，失败不隐瞒）
- [ ] §4 风险清单 T1–T8 每条有明确结论：「确认缺陷 / 无问题 / 待验证」三选一
- [ ] 冒烟 S1–S10 每条有实测结果或不可执行原因
- [ ] P0/P1 全修且四步闭环；P2/P3 全部入报告
- [ ] 全量门禁复跑通过，覆盖率 ≥ 80%
- [ ] known-pitfalls 已更新，`docs/health-checks/` 新报告落盘

---

## 第 7 节｜交付物清单

| 交付物 | 路径 | 说明 |
|---|---|---|
| 复核报告 | `docs/health-checks/2026-09-12-full-coverage-review.md` | 主交付物 |
| 覆盖率原始数据 | `docs/health-checks/raw/coverage.json` | 热区表可追溯 |
| 新增后端用例 | `tests/test_*.py` | 仅针对真实怀疑点与已修缺陷 |
| 新增前端用例 | `web/src/**/*.test.ts` | T1 组件 + T2 契约 |
| P0/P1 修复 | `src/modelctl/**`、`web/src/**` | 附回归用例 |
| pitfall 沉淀 | `docs/known-pitfalls/**` | 摘要层 + 详情层同步 |
