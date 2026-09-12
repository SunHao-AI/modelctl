# modelctl 全覆盖复核 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对 modelctl 做一次全覆盖复核——跑齐本轮工具链与覆盖率数字、按风险清单定点审查并补测、真引擎冒烟，产出带 P0–P3 分级的复核报告。

**Architecture:** 基线先行·风险驱动的四阶段流水线。阶段 1 并行取全客观数字（后端后台跑、前端并行跑），阶段 2 用「本轮 diff × 覆盖率升序 × TODO 存量」三集合求交排风险清单，阶段 3 逐条审查 + TDD 修复，阶段 4 起真实服务 + llama-server CPU 包跑闭环，最后全量门禁复跑 + 落报告。

**Tech Stack:** Python 3.12（uv，`--extra dev --extra test`）、pytest + pytest-cov + pytest-benchmark、ruff、mypy、pip-audit；Vue 3 + Vite + vitest + @vue/test-utils + Playwright；FastAPI webui/gateway 生产拓扑；llama.cpp CPU 预编译包。

## Global Constraints

- 严重级：P0=可利用/必崩 · P1=高概率故障/重大信息暴露/鉴权 · P2=边界与一致性 · P3=风格与可维护性（数字与结论按此分级）
- 覆盖率门禁保持 `fail_under=80`，**不上调**
- CI 五 job 拓扑、`CLAUDE.md` 规范条款、`docs/superpowers/` 历史 spec/plan **不改**
- `docs/TODO.md` 功能缺口只定性影响面，**不实现**（缺陷 vs 缺口裁决口径见 spec §1「裁决规则」：已登记且行为未变=缺口，行为与承诺不符=缺陷）
- 数字必须本轮实跑，**禁止**引用 09-10/09-11 数字代替
- 每个缺陷必须附可复制的复现命令或失败用例名
- 缺陷修复遵循四步闭环：复现测试（红）→ 修复（绿）→ 子域回归 → 全量门禁复跑
- 全量门禁基线：对照上轮 `2118 passed / 2 skipped / 80.38%`，出现回退不得收尾
- 时间格式 `YYYY-MM-DD HH:mm:ss`；含 CJK 的终端/日志输出用 `display_width`+`pad_width` 对齐
- 新增后端用例遵守 `tests/conftest.py` 隔离约定；新文件按命名约定落地以自动打分层 marker
- 每个已修 P0/P1 按渐进式披露写入 `docs/known-pitfalls/`
- 冒烟端口固定：webui `14173`、gateway `15003`、llamacpp `18910`；`.env` 测后删除

---

## 文件结构（本轮创建/修改）

| 文件 | 职责 | 动作 |
|---|---|---|
| `docs/health-checks/raw/baseline-toolchain.txt` | 阶段 1 工具链原始输出留档 | 创建 |
| `docs/health-checks/raw/coverage.json` | 覆盖率明细（热区表数据源） | 创建 |
| `docs/health-checks/raw/coverage-hotspots.md` | 覆盖率升序热区表 | 创建 |
| `docs/health-checks/raw/risk-list.md` | 阶段 2 排序后风险清单 | 创建 |
| `docs/health-checks/2026-09-12-full-coverage-review.md` | **主交付物** · 复核报告 | 创建 |
| `web/src/components/chat/*.test.ts` | T1 零测试组件补测 | 创建 |
| `tests/test_webui_admin_chat.py` | T2 契约补强（追加用例） | 修改 |
| `tests/test_perf_hotpaths.py` | 性能基线对比（如需追加） | 修改 |
| `docs/known-pitfalls/**` | 已修 P0/P1 沉淀 | 修改 |
| 生产代码 | 阶段 3 发现的 P0/P1（具体文件待审查确定） | 修改 |

> 阶段 3 的测试/生产代码文件是「审查产物驱动」的——确切文件名在对应发现确定后才能锁定。下方 Task 10–13 给出了**调查程序 + 判据 + 可运行的代表性测试骨架**，而非占位符：骨架本身是有效测试，若审查确认某处无缺陷则该用例作为回归钉保留，若发现缺陷则据其失败现象扩写。

---

## 阶段 1 · 基线取证

### Task 1: 后端环境准备

**Files:**
- 无源码改动（生成 `.venv/`、`uv.lock` 校验）

- [ ] **Step 1: 同步依赖（dev + test extra）**

Run:
```powershell
uv sync --extra dev --extra test
```
Expected: 结束无报错；输出含 `Audited`/`Installed` 若干包。若网络抖动，重跑一次（清华源 first-match，见 `uv.toml`）。

- [ ] **Step 2: 校验关键测试依赖可导入**

Run:
```powershell
uv run python -c "import httpx,fastapi,uvicorn,bcrypt,jwt,pytest_cov; print('test-deps-ok')"
```
Expected: 打印 `test-deps-ok`（缺任一会 ImportError → 回到 Step 1）。

---

### Task 2: 后端 lint + 类型基线

**Files:**
- Create: `docs/health-checks/raw/baseline-toolchain.txt`（追加写入）

- [ ] **Step 1: ruff 检查（先 clean 缓存，规避 09-10 记录的缓存损坏 panic）**

Run:
```powershell
uv run ruff clean
uv run ruff check src tests script 2>&1 | Tee-Object -FilePath docs/health-checks/raw/baseline-toolchain.txt
```
Expected: 记录告警总数与规则分布。若出现 `panic:` 字样 → 属工具链门（P2），如实留档不强修。

- [ ] **Step 2: mypy 类型检查（Py3.12，与 CI 口径一致）**

Run:
```powershell
uv run mypy src/modelctl 2>&1 | Tee-Object -FilePath docs/health-checks/raw/baseline-toolchain.txt -Append
```
Expected: 记录 `Found N errors in M files`。按文件分布取 top（上轮 09-10 分布：`admin_models 18`/`admin_services 9`/`cli 8`/`tui/keyboard 7`…），本轮重算并对照。

- [ ] **Step 3: 记录本机 Python 版本以证明与 CI 同口径**

Run:
```powershell
uv run python --version
```
Expected: `Python 3.12.x`（写入报告 §1 说明本地/CI 无版本漂移，纠正 09-10 的 3.13 口径偏差）。

---

### Task 3: 后端全量测试 + 覆盖率（后台长任务）

**Files:**
- Create: `docs/health-checks/raw/coverage.json`（`--cov-report=json` 产出）

- [ ] **Step 1: 后台启动全量 pytest（约 39 min，不阻塞其他任务）**

Run（`blocking=false`，`command_type=long_running_process`）:
```powershell
uv run pytest tests/ -q -m "not perf" --cov=modelctl --cov-branch --cov-report=json:docs/health-checks/raw/coverage.json --cov-report=term-missing 2>&1 | Tee-Object -FilePath docs/health-checks/raw/pytest-full.log
```
记下返回的 command id 供 `CheckCommandStatus` 轮询。

- [ ] **Step 2: 并行推进 Task 4–8（前端与安全/性能），Task 3 完成后再回填数字**

在 Task 3 运行期间**不等待**，继续 Task 4。

- [ ] **Step 3: 轮询直到结束，抓取计数**

用 `CheckCommandStatus`（`output_priority=bottom`）取末行摘要。Expected: `N passed`（基线 `2118`）、`2 skipped`（存量条件跳过）。任何 `failed` → 逐条记入报告 §3，每条附用例名。

- [ ] **Step 4: 记录覆盖率总水位与门禁判定**

Run:
```powershell
uv run python -c "import json;d=json.load(open('docs/health-checks/raw/coverage.json'));t=d['totals'];print('line',t['percent_covered_display'],'branch_missing',t.get('missing_branches'))"
```
Expected: 行覆盖率 ≥ 80（门禁）。基线对照 `80.38%`。

---

### Task 4: 覆盖率热区表

**Files:**
- Create: `docs/health-checks/raw/coverage-hotspots.md`
- Consumes: `docs/health-checks/raw/coverage.json`（Task 3 产物）

- [ ] **Step 1: 生成按行覆盖率升序的模块表**

Run:
```powershell
uv run python -c "import json;d=json.load(open('docs/health-checks/raw/coverage.json'));rows=sorted(((round(f['summary']['percent_covered'],1),f['missing_lines'],f['filename']) for f in d['files'].values()));[print(f'{p:5} {m:5} {n}') for p,m,n in rows]"
```
Expected: 打印全部模块 `覆盖率 miss数 路径`，升序。

- [ ] **Step 2: 写入热区表并标注本轮 diff 命中**

把 Step 1 输出整理为 Markdown 表，新增「本轮改动」列——对照 spec §2 的 81 文件 diff（用 `git log --since="2026-09-11" --name-only` 核对）。至少覆盖上轮 §9 点名的：`admin_models`(35.7%)、`all_service`(65.5%)、`cli`(77.2%)、`process`(79.8%)、`colors`(40.1%)。

---

### Task 5: 安全红线 + 性能基线

**Files:**
- 无（结果写入报告；perf 用 `--benchmark-autosave` 存档）

- [ ] **Step 1: 安全套件**

Run:
```powershell
uv run pytest tests/ -m security -q
```
Expected: 基线 `50/50` 全绿。

- [ ] **Step 2: 性能基准（显式跑 + 自动存档）**

Run:
```powershell
uv run pytest tests/ -m perf --benchmark-autosave -q
```
Expected: 基线 `17/17`。数值与上轮 §6 基线相对照（`hash_api_key` 1.66µs / `bcrypt` ~273ms 等），波动 >20% 记入报告但不判失败（runner CPU 敏感）。

---

### Task 6: 依赖漏洞扫描

**Files:**
- 无（结果写入报告 §1）

- [ ] **Step 1: 主包 + dev/test extra 审计**

Run:
```powershell
uv export --extra dev --extra test --no-emit-project --format requirements-txt > requirements-audit.txt
uv run pip-audit -r requirements-audit.txt --strict
```
Expected: 无漏洞则 `No known vulnerabilities`。有 CVE 记入报告并按包判级。

- [ ] **Step 2: gateway 子项目审计**

Run:
```powershell
uv export --project gateway --no-emit-project --format requirements-txt > requirements-gateway.txt; uv run pip-audit -r requirements-gateway.txt --strict
```
Expected: 同上。用完删除两个临时 `requirements-*.txt`（不入库）。

---

### Task 7: 前端类型 + 单测

**Files:**
- 无（`build/coverage-web/` 产出）

- [ ] **Step 1: 安装前端依赖**

Run:
```powershell
cd web; npm ci
```
Expected: `added N packages`，无 error。

- [ ] **Step 2: 类型检查**

Run:
```powershell
cd web; npm run typecheck
```
Expected: 基线 0 错误（strict、零 any）。

- [ ] **Step 3: 单测 + 覆盖率**

Run:
```powershell
cd web; npm run test:coverage
```
Expected: 基线 `50/50`。记录核心模块覆盖率（router 100% / auth 95.2% / sse 98.2%）。

- [ ] **Step 4: 前端依赖漏洞**

Run:
```powershell
cd web; npm audit --json | Out-File -Encoding utf8 ../docs/health-checks/raw/npm-audit.json; npm audit
```
Expected: 记录漏洞分级计数（上轮曾 1 个 moderate echarts XSS，核对是否已修）。

---

### Task 8: 前端 E2E（生产拓扑）

**Files:**
- 无

- [ ] **Step 1: 构建前端产物（E2E 测生产挂载形态，必须先 build）**

Run:
```powershell
cd web; npm run build
```
Expected: `vite build` 成功，产出到 `dist/`。

- [ ] **Step 2: 安装浏览器（沙箱内用仓库内路径 + 镜像）**

Run:
```powershell
cd web; $env:PLAYWRIGHT_BROWSERS_PATH="$PWD/../.playwright-browsers"; $env:PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright"; npm run test:e2e:install
```
Expected: 至少 chromium 就绪。firefox/webkit 若下载受限则记录（与上轮一致，CI 覆盖三内核）。

- [ ] **Step 3: 跑 E2E（chromium 必跑）**

Run:
```powershell
cd web; $env:PLAYWRIGHT_BROWSERS_PATH="$PWD/../.playwright-browsers"; npx playwright test --project=chromium
```
Expected: 基线 chromium `10 passed / 2 skip`（移动视口按 viewportSize skip）。

---

## ★ 检查点 C1：基线数字交用户过目

汇总 Task 1–8 全部数字，产出执行摘要表（维度 × 命令 × 结果），交用户确认后再进入阶段 2。

---

## 阶段 2 · 风险清单

### Task 9: 三集合求交排风险清单

**Files:**
- Create: `docs/health-checks/raw/risk-list.md`
- Consumes: `coverage-hotspots.md`（Task 4）、本轮 diff、`docs/TODO.md`

- [ ] **Step 1: 取本轮 diff 文件集**

Run:
```powershell
git log --since="2026-09-11 00:00" --name-only --pretty=format:"" -- src web/src web/e2e | Sort-Object -Unique | Where-Object { $_ -ne "" } | Out-File -Encoding utf8 docs/health-checks/raw/diff-since-0911.txt
```
Expected: 约 81 行（spec §2 已采得）。

- [ ] **Step 2: 排风险清单（三集合求交）**

对 spec §4 的 T1–T8 逐条标注：① 是否命中 diff、② 对应模块本轮覆盖率、③ 是否 TODO.md 已登记。产出排序表（跨层契约裂缝 > 零测试新代码 > 大文件低覆盖 > 风格项），写入 `risk-list.md`，每条给「预判级别 + 证据」。

- [ ] **Step 3: 结合 C1 数字修正预判级别**

若某模块实际覆盖率远高于/低于 spec 预估，相应升降该条优先级。

## ★ 检查点 C2：风险清单排序交用户过目

---

## 阶段 3 · 定点审查 + 补测

### Task 10: T1 — chat 前端零测试组件补测

**Files:**
- Create: `web/src/components/chat/ChatHistoryList.test.ts`
- Create: `web/src/components/chat/ChatStatsPanel.test.ts`
- 审查（可能改）：`ChatParamsPanel.vue`、`ChatRawPanel.vue`、`views/chat/index.vue`

**Interfaces:**
- Consumes: `ChatSession`/`ChatMsg`（`@/stores/chat`，字段见 spec）
- Produces: 无下游依赖

- [ ] **Step 1: 写 ChatHistoryList 的失败/回归测试骨架**

```typescript
// web/src/components/chat/ChatHistoryList.test.ts
import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import ChatHistoryList from './ChatHistoryList.vue';

const sessions = [
  { id: 'a', title: '会话A', model: 'm1', createdAt: '2026-09-12 10:00:00', messages: [] },
  { id: 'b', title: '会话B', model: 'm2', createdAt: '2026-09-12 10:01:00', messages: [] },
];

describe('ChatHistoryList', () => {
  it('渲染每个会话标题；activeId 命中的条目高亮', () => {
    const w = mount(ChatHistoryList, { props: { sessions, activeId: 'b' } });
    const items = w.findAll('.cursor-pointer'); // 会话条目是带 cursor-pointer 的 div（非 button）
    expect(items).toHaveLength(2);
    expect(items[1].classes()).toContain('bg-slate-800'); // b 命中 activeId
    expect(items[0].classes()).not.toContain('bg-slate-800');
    expect(items[0].text()).toBe('会话A');
  });

  it('空态显示"暂无历史"，非空不显示', () => {
    expect(mount(ChatHistoryList, { props: { sessions: [] } }).text()).toContain('暂无历史');
    expect(mount(ChatHistoryList, { props: { sessions } }).text()).not.toContain('暂无历史');
  });

  it('degraded=true 显示容量降级提示', () => {
    const w = mount(ChatHistoryList, { props: { sessions: [], degraded: true } });
    expect(w.text()).toContain('本地存储已满');
  });

  it('点击条目 emit select(id)；点击"新建"emit new', async () => {
    const w = mount(ChatHistoryList, { props: { sessions } });
    await w.findAll('.cursor-pointer')[0].trigger('click');
    expect(w.emitted('select')?.[0]).toEqual(['a']);
    await w.get('button').trigger('click');
    expect(w.emitted('new')).toHaveLength(1);
  });
});
```

- [ ] **Step 2: 跑测试**

Run: `cd web; npx vitest run src/components/chat/ChatHistoryList.test.ts`
Expected: 若 `ChatHistoryList.vue` 无 `role="listitem"`，第一个用例走 `.cursor-pointer` 分支。断言若与现组件行为不符 → 是缺陷还是测试假设错，按 spec「缺陷 vs 缺口」裁决：组件行为与 `stores/chat` 契约不符=缺陷（P2，补守卫 + 修）；纯样式类名假设错=改测试。

- [ ] **Step 3: 写 ChatStatsPanel 的测试骨架（重点：tps 除零与缺字段）**

```typescript
// web/src/components/chat/ChatStatsPanel.test.ts
import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import ChatStatsPanel from './ChatStatsPanel.vue';

const base = { id: 'x', role: 'assistant' as const, content: '', reasoning: '', images: [] };

describe('ChatStatsPanel', () => {
  it('null 消息显示"发送后显示"', () => {
    expect(mount(ChatStatsPanel, { props: { msg: null } }).text()).toContain('发送后显示');
  });

  it('totalMs=0 时 tok/s 显示"-"而非 Infinity/NaN', () => {
    const msg = { ...base, usage: { prompt_tokens: 1, completion_tokens: 5 }, totalMs: 0 };
    const t = mount(ChatStatsPanel, { props: { msg } }).text();
    expect(t).not.toMatch(/Infinity|NaN/);
    expect(t).toContain('tok/s');
  });

  it('有 usage 与 totalMs 时正确算 tok/s', () => {
    const msg = { ...base, usage: { prompt_tokens: 10, completion_tokens: 20 }, totalMs: 4000 };
    // 20 / (4000/1000) = 5.0
    expect(mount(ChatStatsPanel, { props: { msg } }).text()).toContain('5.0');
  });

  it('无 usage 显示"无 usage"', () => {
    expect(mount(ChatStatsPanel, { props: { msg: { ...base } } }).text()).toContain('无 usage');
  });
});
```

- [ ] **Step 4: 跑测试 + 全绿**

Run: `cd web; npx vitest run src/components/chat/ChatStatsPanel.test.ts`
Expected: 全绿（`ChatStatsPanel.vue` 已有 `totalMs <= 0` 守卫，预期 PASS；此用例是回归钉）。

- [ ] **Step 5: 审查 ChatParamsPanel / ChatRawPanel / views/chat/index.vue**

读三文件，重点：`ChatParamsPanel` 的 `v-model` 双向绑定是否回写 `store.params`（`temperature/top_p/max_tokens` 越界值——如 `max_tokens=0` 或负——是否被夹取）；`views/chat/index.vue` 的模型下拉是否从 `/admin/api/models` 正确取数。发现与契约不符即记 P2/P3，P1 才当场修。

- [ ] **Step 6: 提交**

```powershell
git add web/src/components/chat/ChatHistoryList.test.ts web/src/components/chat/ChatStatsPanel.test.ts
git commit -m "test(web): cover ChatHistoryList & ChatStatsPanel edge cases (T1)"
```

---

### Task 11: T2 — chat SSE 代理跨层契约补强

**Files:**
- Modify: `tests/test_webui_admin_chat.py`（追加用例）
- 审查：`src/modelctl/core/webui/admin_chat.py`、`web/src/api/chat.ts`

**Interfaces:**
- Consumes: `create_app(admin=True, registry=...)`、`httpx.MockTransport`、既有 `_gm()`/`_post()`/`_sse_capture()` 助手（同文件已定义）
- Produces: 无下游依赖

- [ ] **Step 1: 追加 3 条未覆盖分支的用例**

先核对既有 12 条用例已覆盖什么（同文件 L94-L220）：direct 命中 / direct 忽略家族 / gateway 家族路由（**已**断言 `x-chat-route-reason: group_route`）/ 未运行 409 / 上游 4xx 透传 / system 前置+白名单 / include_usage=false / 空 messages 400 / 超限 413 / 上游不可达 502 带路由头 / 需管理面鉴权。

**未覆盖**的三条分支补在下面（勿重复造已存在的断言）：

```python
# 追加到 tests/test_webui_admin_chat.py 末尾

def test_route_mode_invalid_400():
    """route_mode 只认 direct/gateway；拼错必须 400，不能静默落默认路由。
    校验发生在 messages 校验之前（admin_chat.py L72-77），故 messages 合法与否都该 400。
    """
    resp = _post(_app({"a": _gm("a", 8001)}),
                 {"model": "a", "route_mode": "bogus",
                  "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 400
    assert "route_mode" in resp.json()["error"]["message"]


def test_json_array_body_400_not_500():
    """合法 JSON 但非对象（数组）→ 400。裸 payload.get 会 AttributeError→500，须被 isinstance 守卫拦住。"""
    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app({"a": _gm("a", 8001)})), base_url="http://t"
        ) as c:
            return await c.post("/admin/api/chat/completions", content=b"[1,2,3]",
                                headers={"Authorization": f"Bearer {_ADMIN_KEY}",
                                         "content-type": "application/json"})

    resp = _run(go())
    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "请求体必须是 JSON 对象"


def test_gateway_mode_unknown_model_404_from_prepared_error(monkeypatch):
    """gateway 模式未命中 → 透传 prepare_openai_upstream 的 PreparedError（404），不是 500 也不是 409。"""
    monkeypatch.setattr("modelctl.core.gateway.is_model_available", lambda m: True)
    resp = _post(_app({"a": _gm("a", 8001)}),
                 {"model": "nope", "route_mode": "gateway",
                  "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 404
    assert "model not found" in resp.json()["error"]["message"]
```

- [ ] **Step 2: 追加流式生命周期用例（客户端消费完后上游必须被 close）**

`relay()` 的 `finally` 关上游（[admin_chat.py:151-159](file:///d:/Workplace/modelctl-1/src/modelctl/core/webui/admin_chat.py#L151-L159)）是 known-pitfalls 记录过的坑（不能用 `async with`），但**无回归钉**。用一个可观测 close 的流补上：

```python
class _TrackBody(httpx.AsyncByteStream):
    """记录自身是否被 aclose()，用于钉住 relay() 的上游生命周期。"""

    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for c in self._chunks:
            yield c

    async def aclose(self):
        self.closed = True


def test_stream_close_propagates_to_upstream(monkeypatch):
    body = _TrackBody([b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n', b"data: [DONE]\n\n"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    monkeypatch.setattr("modelctl.core.gateway.is_model_available", lambda m: True)
    app = _app({"a": _gm("a", 8001)})
    app.state.chat_transport = httpx.MockTransport(handler)

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=30
        ) as c:
            async with c.stream(
                "POST", "/admin/api/chat/completions",
                json={"model": "a", "route_mode": "direct",
                      "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {_ADMIN_KEY}"},
            ) as r:
                return b"".join([chunk async for chunk in r.aiter_bytes()])

    out = _run(go())
    assert b"[DONE]" in out
    assert body.closed is True   # 上游流被关闭 → 不泄漏连接
```

- [ ] **Step 3: 跑新增用例**

Run: `uv run pytest tests/test_webui_admin_chat.py -q`
Expected: 原 12 条 + 新 4 条全绿。任一失败 → 定位是 `admin_chat.py` 缺陷（鉴权/路由语义错=P1，边界=P2）还是助手误用；缺陷走四步闭环。

- [ ] **Step 4: 核对前端 `web/src/api/chat.ts` 与后端响应头契约**

确认前端读取的 header 名（`x-chat-routed-to`/`x-chat-route-reason`）与后端 `route_headers`（[admin_chat.py:121-127](file:///d:/Workplace/modelctl-1/src/modelctl/core/webui/admin_chat.py#L121-L127)）大小写/键名一致；上游 `>=400` 分支带 `route_headers`（L145-149）前端是否消费。不一致记 P2。

- [ ] **Step 5: 提交**

```powershell
git add tests/test_webui_admin_chat.py
git commit -m "test(webui): pin chat proxy route_mode/non-object-body/prepared-error/stream-close contracts (T2)"
```

---

### Task 12: T3/T4/T6/T8 — 后端大文件与跨层契约审查

**Files:**
- 审查：`webui/admin_models.py`（910 行，35.7%）、`core/gateway.py`（`prepare_openai_upstream`）、`cluster/reconcile.py`（837 行）、`all_service.py`、`cli.py`、`process.py`、`colors.py`、`engines/_download.py`
- Create（按需）：`tests/test_webui_admin_models_<topic>.py`、`tests/test_engines_download.py`（扩测）
- Modify（按需）：仅当发现 P0/P1

**Interfaces:**
- Consumes: 现有 `create_app(admin=True, ...)`、`TestClient`
- Produces: 审查结论（缺陷/无问题/待验证 三选一）写入报告 §3

- [ ] **Step 1: admin_models 启停/日志流/启动进度端点矩阵审查**

该文件共 11 个端点（已核实行号）：`GET ""`(L323) · `GET /{name}`(L367) · `POST /{name}/start`(L430) · `POST /{name}/stop`(L556) · `POST /{name}/restart`(L578) · `GET /{name}/startup`(L627) · `GET /{name}/log`(L697) · `GET /{name}/log/stream`(L727) · `GET /{name}/yaml`(L1052) · `POST /{name}/ui/start`(L1070) · `POST /{name}/ui/stop`(L1146)。

用覆盖率 JSON 定位未覆盖分支：
```powershell
uv run python -c "import json;d=json.load(open('docs/health-checks/raw/coverage.json'));k=[x for x in d['files'] if 'admin_models' in x][0];print(k, d['files'][k]['missing_lines']);print(d['files'][k]['missing_percentages'] if 'missing_percentages' in d['files'][k] else d['files'][k]['executed_lines'][:5])"
```
把 `missing_lines` 按上面的端点行号区间分桶，找出**未覆盖行数最多**的端点。优先补测这两条最可能出错的分支：

1. `POST /{name}/start` 在「已在启动中/已运行」时的重复调用——是否幂等、是否二次拉起进程、返回码是否区分（应为 409/400 而非 200）
2. `POST /{name}/restart` 在「模型未运行」时的行为——应先起还是报错，与 `stop`+`start` 是否等价

用 `TestClient` + patch `modelctl.core.webui.admin_models` 内的启动函数（勿起真进程），断言状态码与副作用次数。

- [ ] **Step 2: gateway ENGINE_PRIORITY 与 TODO §1.1 的一致性裁决**

已核实 [gateway.py:56-59](file:///d:/Workplace/modelctl-1/src/modelctl/core/gateway.py#L56-L59) **已登记全 9 引擎**（aphrodite:5/tokenspeed:6/lmdeploy:7/tensorrt_llm:8）。→ TODO.md §1.1「缺 4 引擎」**已过时**。裁决：这是**文档滞后**（缺口），非缺陷；记入报告 §6 并建议更新 TODO.md（不当场改 TODO，除非用户要求）。补一条 family 多引擎排序断言钉住现状：
```python
# 追加到 tests/test_gateway.py
def test_engine_priority_covers_all_known_engines():
    from modelctl.core.gateway import ENGINE_PRIORITY
    from modelctl.core.profile import KNOWN_ENGINES
    assert set(KNOWN_ENGINES) <= set(ENGINE_PRIORITY), (
        f"ENGINE_PRIORITY 未覆盖：{set(KNOWN_ENGINES) - set(ENGINE_PRIORITY)}")
```
Run: `uv run pytest tests/test_gateway.py::test_engine_priority_covers_all_known_engines -q`
Expected: PASS（钉住 9 引擎全覆盖，防未来新增引擎漏登记）。

- [ ] **Step 3: reconcile 状态机关键转移审查**

读 `cluster/reconcile.py` 收敛循环，核对 FAILED 终态不被 retry 重置（上轮 cluster 记录）与 GPU 锁释放（早退分支）。仅审查 + 记录，发现缺陷走四步闭环。

- [ ] **Step 4: T6 存量薄弱区——先核实 TODO.md 是否滞后，再决定补测**

TODO.md §4 称 `tests/test_engines_download.py`「仅 1 用例，需扩到覆盖 ensure_modelscope / 路径回退 / 失败不阻断 / 幂等复用」。**已核实该条滞后**：该文件实有 12 条用例，已覆盖
`repo_local_dir` 确定性路径、`_is_populated` 幂等复用、404/429/网络错三类中文包装、失败清 partial 目录、aphrodite/lmdeploy `pre_start` 下载与「不写回 YAML」。

→ 裁决：**文档滞后（缺口）**，非缺陷；不重复补测。报告 §6 记「TODO §1.1 与 §4 两条高优先项均已过时，建议同步 TODO.md」。

对 `all_service.py` / `cli.py` / `process.py` / `colors.py` 按 Task 4 热区表取本轮真实覆盖率（**不用 65.5%/77.2%/79.8%/40.1% 旧值**），仅对「本轮 diff 命中 + 覆盖率低于 80%」的模块补 1–2 条关键分支用例；纯存量缺口维持"随迭代顺带补"，不为凑数写测试。

- [ ] **Step 5: 提交（仅当有测试/代码改动）**

```powershell
git add tests/test_gateway.py
git commit -m "test(gateway): pin ENGINE_PRIORITY covers all KNOWN_ENGINES (T4)"
```

---

### Task 13: T5/T7 — TUI 重构与引擎适配器一致性审查

**Files:**
- 审查：`tui/{app,keyboard,panels/detail,panels/plan}.py`、`engines/*.py`
- Modify（按需）：仅当发现 P0/P1

**Interfaces:**
- Consumes: 现有 `tests/test_tui_*.py`、`tests/test_engines_*.py`
- Produces: 审查结论写入报告 §3

- [ ] **Step 1: TUI mypy 错误闭环核对**

从 Task 2 的 mypy 输出筛 `tui/` 行。若仍有 `_SnapshotBase has no attribute` 类错误 → 对照上轮 09-10 报告 TUI-P1 是否真闭环。有残留按 P2 记录（类型契约缺失），若涉渲染期副作用（P1 类）则当场修。

- [ ] **Step 2: 引擎适配器 build_command/metrics_mapping 一致性**

跨 9 个适配器核对：`--n-gpu-layers`/`--flash-attn`（llamacpp）、`metrics_mapping()` 返回值（vllm 有、unsloth `None`）。发现参数在引擎间不一致且无注释解释的记 P3。相关已有用例 `tests/test_engines_*.py` 复跑：
```powershell
uv run pytest tests/ -k "engines" -q
```
Expected: 全绿。

- [ ] **Step 3: 无代码改动则跳过提交；有 P0/P1 修复则按四步闭环单独提交并写 pitfall**

---

## 阶段 4 · 真实服务 + 小模型冒烟

### Task 14: 冒烟环境准备

**Files:**
- Create（临时，测后删）：`.env`
- Create（外部，不入库）：`d:\Workplace\llama.cpp\llama-server.exe`

- [ ] **Step 1: 取 llama.cpp 最新 stable 的 CPU Windows 包并解压**

Run:
```powershell
$rel = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
$asset = $rel.assets | Where-Object { $_.name -match 'bin-win-cpu-x64\.zip$' } | Select-Object -First 1
Invoke-WebRequest $asset.browser_download_url -OutFile "$env:TEMP\llama-cpu.zip"
Expand-Archive "$env:TEMP\llama-cpu.zip" -DestinationPath "d:\Workplace\llama-tmp" -Force
New-Item -ItemType Directory -Force -Path "d:\Workplace\llama.cpp" | Out-Null
Copy-Item "d:\Workplace\llama-tmp\*\*" "d:\Workplace\llama.cpp\" -Recurse -Force
Test-Path "d:\Workplace\llama.cpp\llama-server.exe"
```
Expected: `True`。若 GitHub API 不可达，改镜像或记录为冒烟阻塞项（不硬编造结果）。

- [ ] **Step 2: 写最小 `.env`（测后删）**

Run:
```powershell
@"
API_KEY=smoke-admin-key-123456
WEBUI_HOST=127.0.0.1
WEBUI_PORT=14173
MODEL_ROOT=$PWD/data/models
"@ | Out-File -Encoding utf8 .env
```
Expected: `.env` 生成。`MODEL_ROOT` 指向仓库内，GGUF 落在 `data/models/`。

- [ ] **Step 3: 端口空闲探测**

Run:
```powershell
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 14173,15003,18910 } | Format-Table -AutoSize
```
Expected: 无输出（三端口空闲）。占用则改 `.env` 端口并同步报告。

---

### Task 15: S1–S2 引擎启动与回环鉴权

**Files:**
- 无

- [ ] **Step 1: 启 llamacpp/qwen2.5-0.5b（触发 GGUF 下载 ~0.3GB + llama-server 拉起）**

Run（`blocking=false`）:
```powershell
$env:LLAMACPP_SOURCE_DIR="d:\Workplace\llama.cpp"; uv run modelctl start llamacpp/qwen2.5-0.5b 2>&1 | Tee-Object -FilePath docs/health-checks/raw/smoke-start.log
```
Expected: S1 通过 = 进程存活 + `http://127.0.0.1:18910/health` 返回 200。用 `CheckCommandStatus` 观察启动进度；若 `--n-gpu-layers 999`/`--flash-attn on` 在 CPU 二进制上报错退出 → **S10 真实发现**，按级别记录，不私改 profile（用临时 env 覆盖参数继续 S3–S8，并在报告标注"人工构造"）。

- [ ] **Step 2: 健康探测**

Run:
```powershell
Invoke-WebRequest "http://127.0.0.1:18910/health" -UseBasicParsing | Select-Object StatusCode
```
Expected: `200`。

- [ ] **Step 3: 直连引擎——错误 key 401、正确 key 200（S2）**

Run:
```powershell
$h=@{Authorization="Bearer wrong"}; try{ Invoke-WebRequest "http://127.0.0.1:18910/v1/chat/completions" -Method POST -Headers $h -ContentType "application/json" -Body '{"model":"x","messages":[{"role":"user","content":"hi"}]}'}catch{"wrong-key => $($_.Exception.Response.StatusCode.value__)"}
```
Expected: 错误 key 返回 `401`。

---

### Task 16: S3–S6 网关代理 + 鉴权隔离 + 用量 + 审计

**Files:**
- 无

- [ ] **Step 1: 起 gateway（数据面 key，端口 15003）**

Run（`blocking=false`）:
```powershell
$env:GATEWAY_PORT="15003"; $env:GATEWAY_CLIENT_API_KEY="smoke-client-key-abcdef"; uv run modelctl gateway start 2>&1 | Tee-Object -FilePath docs/health-checks/raw/smoke-gateway.log
```

- [ ] **Step 2: 数据面 key 打 /v1/chat/completions 流式返回完整（S3）**

Run:
```powershell
& curl.exe -N -s -H "Authorization: Bearer smoke-client-key-abcdef" -H "Content-Type: application/json" -d '{"model":"qwen2.5-0.5b","messages":[{"role":"user","content":"say hi"}],"stream":true}' "http://127.0.0.1:15003/v1/chat/completions"
```
Expected: 多行 `data:` chunk + `data: [DONE]`。

- [ ] **Step 3: 管理面 key 打数据面 401（S4，两域不互换）**

Run:
```powershell
& curl.exe -s -o NUL -w "%{http_code}" -H "Authorization: Bearer smoke-admin-key-123456" -H "Content-Type: application/json" -d '{"model":"qwen2.5-0.5b","messages":[{"role":"user","content":"x"}]}' "http://127.0.0.1:15003/v1/chat/completions"
```
Expected: `401`。

- [ ] **Step 4: 用量统计出现该请求 tokens（S5）**

Run:
```powershell
uv run modelctl stats 2>&1 | Tee-Object -FilePath docs/health-checks/raw/smoke-stats.log
```
Expected: qwen2.5-0.5b 出现非零 prompt/completion tokens。

- [ ] **Step 5: 审计落盘且无明文密钥（S6）**

Run:
```powershell
$audit = Get-ChildItem data/audit -Recurse -File | Sort-Object LastWriteTime | Select-Object -Last 1
Get-Content $audit.FullName -Raw | Select-String -Pattern "smoke-client-key|smoke-admin-key" -Quiet
```
Expected: `False`（审计不含明文 key，符合脱敏红线）。

---

### Task 17: S7–S9 webui 生产拓扑 + 收尾

**Files:**
- 无（`dist/` 来自 Task 8 构建）

- [ ] **Step 1: 起 webui（生产拓扑，同源 API + SPA，端口 14173）**

Run（`blocking=false`）:
```powershell
uv run modelctl webui start 2>&1 | Tee-Object -FilePath docs/health-checks/raw/smoke-webui.log
```
Expected: 监听 14173。

- [ ] **Step 2: SPA 深链回 index、API 404 为 JSON、401 形状（S7）**

Run:
```powershell
# PS5：非 2xx 走异常，用 try/catch 取状态码（-SkipHttpErrorCheck 仅 PS7+）
try { (Invoke-WebRequest "http://127.0.0.1:14173/cluster/nodes" -UseBasicParsing).StatusCode } catch { "deep-link => $($_.Exception.Response.StatusCode.value__)" }   # 期望 200 SPA
try { (Invoke-WebRequest "http://127.0.0.1:14173/admin/api/nope" -UseBasicParsing).StatusCode } catch { "api-404 => $($_.Exception.Response.StatusCode.value__)" }     # 期望 404 JSON
try { (Invoke-WebRequest "http://127.0.0.1:14173/admin/api/overview" -UseBasicParsing).StatusCode } catch { "no-token => $($_.Exception.Response.StatusCode.value__)" } # 期望 401
```
Expected: 深链 200（回 index.html）、未知 API 404、无 token 打管理面 401。

- [ ] **Step 3: S8 chat 代理经后端可用（可选，视浏览器工具可用性）**

若有 puppeteer/MCP：打开 `http://127.0.0.1:14173`，登录→chat 视图发一条，验证流式落地。无浏览器工具则用 `curl` 打 `/admin/api/chat/completions` 带管理面 key 验 SSE，记「浏览器交互项由 E2E/手工承担」。

- [ ] **Step 4: S9 停服清理（PID 清理 + 端口释放 + 无孤儿）**

Run:
```powershell
uv run modelctl stop llamacpp/qwen2.5-0.5b
Start-Sleep -Seconds 3
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 14173,15003,18910 } | Format-Table -AutoSize
```
Expected: 18910 释放；无 `llama-server` 孤儿进程。

- [ ] **Step 5: 清理临时件**

```powershell
Remove-Item .env -ErrorAction SilentlyContinue
git status --short
```
Expected: `.env` 删除；`git status` 无 `.env`/`data/` 泄漏（`data/` 已 gitignore）。

---

## 收尾 · 报告与门禁

### Task 18: 全量门禁复跑（确认无回退）

- [ ] **Step 1: 若阶段 3 改过生产代码，复跑全量门禁**

Run:
```powershell
uv run pytest tests/ -q -m "not perf" --cov=modelctl --cov-branch
```
Expected: 无回退（对照 `2118 passed / 2 skipped / ≥80%`）。失败 → 修到绿才收尾。

### Task 19: 撰写复核报告

**Files:**
- Create: `docs/health-checks/2026-09-12-full-coverage-review.md`

- [ ] **Step 1: 按 spec §6 骨架逐节填写**

0 执行摘要 / 1 工具链 / 2 覆盖率热区 / 3 缺陷清单（编号·级别·位置·根因·复现·状态）/ 4 可优化项 / 5 冒烟 S1–S10 / 6 TODO 缺口定性 / 7 剩余缺口 / 附录 A 复现命令 / 附录 B 改动清单。每个数字标"本轮实跑"。

- [ ] **Step 2: known-pitfalls 沉淀（每个已修 P0/P1）**

按渐进式披露：`docs/known-pitfalls/README.md` 加摘要条目 + 对应分类主题文件加 `## <问题标题>` 详情。

- [ ] **Step 3: 提交报告与沉淀**

```powershell
git add docs/health-checks/ docs/known-pitfalls/
git commit -m "docs(health-checks): 2026-09-12 full-coverage review report + pitfalls"
```

### Task 20: 完成判据核对

- [ ] 对照 spec §6「完成判据」六条逐项打勾：11 维度有本轮数字 / T1–T8 每条三选一结论 / S1–S10 有结果或不可执行原因 / P0-P1 四步闭环 / 门禁 ≥80% / pitfall 已更新 / 报告落盘。任一未达 → 回到对应 Task。
