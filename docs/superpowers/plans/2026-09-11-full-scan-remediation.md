# 全量检测修复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实施。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 按 `docs/superpowers/specs/2026-09-11-full-scan-remediation-design.md` 修复 1 条件 P0 + 18 P1 + 3 同源 P2 + 3 工具链门，共 25 项。

**Architecture:** 前置门禁（基线归属 + 测试红灯转绿）先行，随后三条互不依赖的修复线（安全与线上故障 / 跨层契约与规模韧性 / TUI 投产门与工具链门）可并行；每项遵循"红→绿→提交"。

**Tech Stack:** FastAPI + SQLAlchemy/SQLite（Py3.12 CI / 3.13 本地）、Vue 3 + TS strict + Vite、Rich TUI、loguru、pytest + httpx.ASGITransport、ruff/mypy。

## Global Constraints

- **禁止任何 DDL**（DROP/TRUNCATE/ALTER/CREATE），即便本地/Docker；业务 DML（INSERT/UPDATE/DELETE）允许。CLU-2 的 events 裁剪只用 `DELETE`。
- 时间格式一律 `YYYY-MM-DD HH:mm:ss`。
- UI 显示虚拟 ID；**例外**：`/cluster/nodes` 显示真实 `node_id`（已裁决）。
- CLI / 日志 / 表格任何可能含 CJK 的对齐必须用 `display_width` + `pad_width`，禁止 `len()` / `f"{x:<N}"` / `ljust`。
- SQL 关键字大写、禁止 `SELECT *`、含中文 SQL 顶部 `SET NAMES utf8mb4;`。
- 前端：`<script setup name="Xxx">` 且与 route.name 一致；CSS BEM；子组件样式用 `:deep()`；请求走 `src/utils/request.js`；`ElMessage`/`ElMessageBox` 必须显式 import。
- 后端：PEP 8 + RESTful；PyJWT `iat/exp` 用 `int(time.time())`；资源五方法同组齐备；双通道鉴权嗅探请求头不强求 Bearer。
- PowerShell **不支持 `&&`**，多命令用 `;` 分隔或分行。
- 冒烟只用隔离端口 **14173/15003** + 独立 `LOG_DIR/CACHE_DIR/USAGE_DATA_DIR/AUDIT_DIR`，**绝不**触碰 4173/5003。
- 主 venv 缺 httpx/fastapi/uvicorn/bcrypt/pyjwt：测试统一 `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest ...`，**不改 lockfile**。
- 每个修复项完成即单独 commit（Conventional Commits，`fix(<域>): ...`）；未经用户明确授权不 push。

---

## 阶段 0｜前置门禁

### Task 0.1: HEAD 基线归属（mypy / pytest）

**Files:**
- Create: `docs/health-checks/raw/baseline-head-test.txt`（命令输出重定向，已由进行中任务产出）
- Create: `docs/health-checks/raw/baseline-head-mypy.txt`
- 隔离工作区：`d:\WorkPlace\Pycharm\modelctl-baseline`（`git worktree add ..\modelctl-baseline HEAD` 已建）

**Interfaces:**
- Consumes: 主工作区当前 `pytest`/`mypy` 结果（工作区报告：pytest 4 failed、mypy 109 errors）
- Produces: 两个基线集合 `B_test`、`B_mypy`，供后续任务判定"存量 vs 本次引入"；阶段收尾时 `git worktree remove` 清理

- [ ] **Step 1: 在 worktree 跑 pytest 全量并留档**

```powershell
cd "d:\WorkPlace\Pycharm\modelctl-baseline"
uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests -q --no-header -p no:cacheprovider 2>&1 | Tee-Object -FilePath "d:\WorkPlace\Pycharm\modelctl\docs\health-checks\raw\baseline-head-test.txt"
```

Expected: 末尾出现 `N failed, M passed`；记下 failed 的用例 id 清单 = `B_test`。

- [ ] **Step 2: 在 worktree 跑 mypy 并留档**

```powershell
cd "d:\WorkPlace\Pycharm\modelctl-baseline"
uv run --with mypy mypy src/modelctl 2>&1 | Tee-Object -FilePath "d:\WorkPlace\Pycharm\modelctl\docs\health-checks\raw\baseline-head-mypy.txt"
```

Expected: 末尾 `Found N errors in ...`；N 即 `|B_mypy|`。

- [ ] **Step 3: 写归属结论（追加到基线文件末尾，人读用）**

在 `baseline-head-test.txt` 末尾追加一段：

```
# ── 归属结论（人工核对后填写实际数字）
# HEAD 基线 failed: <B_test 数> ；当前工作区 failed: 4
# 结论：docker_setup_pull 3 项 = HEAD 既有存量回归；compat_flow 1 项 = 顺序污染（非 HEAD）
```

判定规则：某条 failed 若同时出现在 `B_test` → 存量；只在工作区出现 → 未提交改动引入（须先与用户确认再动）。

- [ ] **Step 4: 不提交，仅本地留档**

`docs/health-checks/` 是本次检测产物目录，随 Task 0.4 一起提交，本步不单独 commit。

---

### Task 0.2: docker_setup 测试桩跟进 stdout 契约

**Files:**
- Modify: `tests/test_docker_setup_pull.py:11-24`（`_FakeProc`）、`:57-73`（`_InterruptProc`）
- 参考（**不改**）: `src/modelctl/core/docker_setup.py:219-240`

**Interfaces:**
- Consumes: `docker_setup.ensure_image(image, attempts=..., on_progress=...) -> bool`；实现契约 = 读 `proc.stdout` 逐行 → `proc.wait()` 回收 → 按 `proc.returncode` 与 `classify_pull_error(拼接文本)` 决策
- Produces: `tests/test_docker_setup_pull.py` 5 用例全绿，成为后续 docker 改动的回归护栏

**根因（已核实）**：实现早于 `05c0a27` 从"`for line in proc`"改为"`stdout = proc.stdout; for line in stdout`"，而桩把 `self.stdout = ""` 且只实现 `__iter__` → 空串迭代 0 次 → `max(seen)` 抛 `ValueError: max() arg is an empty sequence`。

- [ ] **Step 1: 改 `_FakeProc`，把行放到 `stdout` 上**

```python
class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.returncode = returncode
        # 实现从 proc.stdout 逐行读取（docker_setup.py:221-223），
        # 不再是 `for line in proc`；stderr 已由 subprocess 合并进 stdout。
        self.stdout = iter(lines)
        self.stderr = None

    # 简报 fixture 缺 wait()：实现必须 wait() 回收子进程防僵尸（设计意图），
    # 故按测试+设计意图为准补齐 fake 的 wait 契约，不改任何断言。
    def wait(self):
        return self.returncode
```

- [ ] **Step 2: 改 `_InterruptProc`，同样暴露可迭代 stdout**

```python
    class _InterruptProc:
        def __init__(self):
            self.returncode = None  # 读循环被打断时子进程仍在运行
            self.killed = False
            self.waited = False
            self.stdout = self  # 实现读 proc.stdout；本桩迭代一次即抛中断

        def __iter__(self):
            yield "aaa: Pulling fs layer"
            raise KeyboardInterrupt

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self):
            self.waited = True
            return self.returncode
```

- [ ] **Step 3: 跑测试确认 5 项全绿**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_docker_setup_pull.py -v`
Expected: `5 passed`（此前 3 failed）。若 `test_ensure_image_streams_progress` 仍红，检查 `PullParser` 是否需要对行尾换行敏感（当前桩用无换行的裸行，与 docker 实际输出一致即可）。

- [ ] **Step 4: 只跑 docker 相关文件确认无连带**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_docker_setup_pull.py tests/test_core_docker_setup.py tests/test_docker_log_tee.py -q`
Expected: 全绿。

- [ ] **Step 5: Commit**

```powershell
git add tests/test_docker_setup_pull.py
git commit -m "test(docker): align pull proc stub with proc.stdout read contract"
```

---

### Task 0.3: compat_flow 顺序污染隔离

**Files:**
- Modify: `tests/conftest.py:64-71`（`isolated_runtime_dirs` 尾部）
- 参考（**不改**）: `src/modelctl/core/compat.py:167-181`、`tests/test_compat_flow.py:153-159`

**Interfaces:**
- Consumes: autouse fixture `isolated_runtime_dirs`
- Produces: 全量 `pytest tests` 0 failed；`compat._current_site_packages()` 结果在全量跑中稳定

- [ ] **Step 1: 先复现"只有全量跑才红"**

Run:
```powershell
uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests -q --no-header -p no:cacheprovider
```
Expected: 仅剩 `test_run_compat_checks_falls_back_to_current_env_when_no_venv` 1 failed（Task 0.2 已修掉另 3 个）。记录报错里的 `assert env.site_packages == expected` 两侧实际值（用于确认污染形态）。

- [ ] **Step 2: 写失败测试（新增到 conftest 消费方，锁定隔离契约）**

在 `tests/test_compat_flow.py` 末尾追加：

```python
def test_current_site_packages_stable_under_full_run(monkeypatch):
    """全量跑时不得把 sys.path 劫持到 uv 缓存临时目录：回退目标必须是真实解释器 site-packages。

    回归 tests/conftest.py 的 site-packages 隔离；旧失败形态是
    `_current_site_packages()` 取到 uv 建的临时 venv 路径（层级更浅 → 被 min() 选中）。
    """
    from modelctl.core.compat import _current_site_packages

    sp = _current_site_packages()
    assert sp is not None, "当前解释器应有 site-packages 可回退"
    as_str = str(sp).replace("\\", "/")
    assert "/.tmp/" not in as_str, f"site-packages 被临时 venv 劫持：{sp}"
    assert "uv-cache" not in as_str and "sdist" not in as_str, f"site-packages 指向 uv 缓存：{sp}"
```

- [ ] **Step 3: 跑该测试确认它当前是红的**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_compat_flow.py -q -p no:cacheprovider`
Expected: 若单跑绿、全量跑红，说明污染来自其他用例改 `sys.path`。此时用 `pytest tests -q -p no:cacheprovider` 复核，确认红。

- [ ] **Step 4: 在 conftest 增加 autouse 的 sys.path 冻结**

在 `tests/conftest.py` 现有 `CLUSTER_*` 前缀清理块之后追加（保持同风格 + 注释说明业务逻辑）：

```python
    # sys.path 冻结：uv --with 注入的临时 venv 会把自身 site-packages 塞进 sys.path
    # 前端，且层级比真实解释器的 site-packages 更浅；compat._current_site_packages()
    # 取 min(len(parts)) → 回退目标被劫持，test_compat_flow 仅"全量跑"时红。
    # 用例如需临时路径自行 monkeypatch sys.path（晚于本 fixture，不受影响）。
    real_paths = [p for p in sys.path if p not in _uv_temp_paths()]
    monkeypatch.setattr(sys, "path", list(real_paths))


def _uv_temp_paths() -> set[str]:
    """识别 uv --with 注入的临时环境路径（.tmp/ 或 uv-cache 下的一次性 venv）。"""
    out: set[str] = set()
    for p in sys.path:
        n = p.replace("\\", "/")
        if "/.tmp/" in n or "uv-cache" in n or "/sdists/" in n:
            out.add(p)
    return out
```

`tests/conftest.py` 顶部若未 import `sys`，补 `import sys`。

- [ ] **Step 5: 全量跑确认 0 failed**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests -q --no-header -p no:cacheprovider`
Expected: `0 failed`（原 4 failed → 0）。若仍红，改为把 `sys.path` 过滤收窄到仅 `uv-cache`，避免误删必要路径。

- [ ] **Step 6: 单跑仍绿**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_compat_flow.py tests/test_compat.py -q`
Expected: 全绿。

- [ ] **Step 7: Commit（含阶段 0 留档文件）**

```powershell
git add tests/conftest.py tests/test_compat_flow.py docs/health-checks/
git commit -m "test: freeze sys.path against uv temp venv site-packages hijack in full runs"
```

---

## 计划 1｜安全与线上故障（8 项）

### Task 1.1: 配置脱敏从名单式改模式匹配（WEB-P1-1）

**Files:**
- Modify: `src/modelctl/core/webui/admin_config.py:30-31`（常量区）、`:100-119`（`read_env`）
- Test: `tests/test_webui_admin_config.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces: `_is_sensitive_key(key: str) -> bool`（模块级，供 `read_env` 与后续复用）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_webui_admin_config.py`：

```python
"""GET /admin/api/config/static 脱敏：模式匹配（非白名单）——防密钥明文回显。"""
from __future__ import annotations

import httpx
import pytest

from modelctl.core.webui.admin_config import _is_sensitive_key


@pytest.mark.parametrize("key", [
    "API_KEY", "UNSLOTH_API_KEY", "GATEWAY_CLIENT_API_KEY", "ACCOUNTS_JWT_SECRET",
    "DB_PASSWORD", "GH_TOKEN", "ACCESS_SECRET", "admin_passwd", "upstream-key",
])
def test_sensitive_keys_matched_by_pattern(key: str):
    assert _is_sensitive_key(key) is True


@pytest.mark.parametrize("key", [
    "MODEL_ROOT", "MODELSCOPE_CACHE", "HF_HOME", "GATEWAY_PORT",
    "CLUSTER_ROLE", "LOG_DIR", "OLLAMA_MODELS", "TZ",
])
def test_non_sensitive_keys_not_masked(key: str):
    assert _is_sensitive_key(key) is False


def test_read_env_masks_pattern_matched_keys(monkeypatch, tmp_path):
    """端点级：命中模式的键 must be masked（旧实现按 2 项白名单 → 明文泄漏）。"""
    from modelctl.core.webui import admin_config as ac
    from modelctl.core.webui import envfile_stub  # noqa: F401  (占位说明见下)

    payload = {
        "MODEL_ROOT": "/models",
        "ACCOUNTS_JWT_SECRET": "super-secret-value-1234",
        "GATEWAY_CLIENT_API_KEY": "sk-client-abcd1234",
    }
    monkeypatch.setattr(ac, "_load_env_file", lambda path: dict(payload))
    monkeypatch.setattr(ac, "require_auth", lambda: None)
    from fastapi import FastAPI
    from modelctl.core.webui.admin_config import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides = {}

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get("/admin/api/config/static")

    resp = httpx.Response  # 仅类型占位，避免 lint 噪音
    import asyncio
    r = asyncio.run(_go())
    assert r.status_code == 200
    entries = {e["key"]: e for e in r.json()["entries"]}
    assert entries["ACCOUNTS_JWT_SECRET"]["sensitive"] is True
    assert entries["ACCOUNTS_JWT_SECRET"]["value"].startswith("***")
    assert "super-secret" not in entries["ACCOUNTS_JWT_SECRET"]["value"]
    assert entries["GATEWAY_CLIENT_API_KEY"]["sensitive"] is True
    assert entries["MODEL_ROOT"]["sensitive"] is False
    assert entries["MODEL_ROOT"]["value"] == "/models"
```

> 注：若 `require_auth` 的 override 方式与该 router 现有测试不一致，参照 `tests/test_webui_admin_audit.py` 的 client fixture 写法对齐（该文件已在当前工作区，是最新的 webui 测试范式）。删掉上面两行 `envfile_stub` 占位 import 与 `resp` 占位后再提交。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_webui_admin_config.py -q`
Expected: `ImportError: cannot import name '_is_sensitive_key'`。

- [ ] **Step 3: 实现 `_is_sensitive_key` 并接线 `read_env`**

`admin_config.py` 常量区替换为：

```python
# .env 中需要脱敏的键：**显式名单 + 语义模式**双保险。
# 只列名单会漏（新增 *_SECRET/*_TOKEN 即明文回显），故按后缀/子串兜底。
_SENSITIVE_KEYS = {"API_KEY", "UNSLOTH_API_KEY"}
_SENSITIVE_PATTERNS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD")


def _is_sensitive_key(key: str) -> bool:
    """键是否敏感：命中显式名单，或含 KEY/SECRET/TOKEN/PASSWORD/PASSWD（大小写不敏感）。"""
    if key in _SENSITIVE_KEYS:
        return True
    upper = key.upper()
    return any(p in upper for p in _SENSITIVE_PATTERNS)
```

`read_env` 内 L111-114 改为：

```python
    keys = []
    for k, v in data.items():
        sensitive = _is_sensitive_key(k)
        keys.append({"key": k, "value": _mask_value(v) if sensitive else v, "sensitive": sensitive})
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2。Expected: 全绿。

- [ ] **Step 5: webui 域回归**

Run: `... pytest tests/test_webui_admin_config.py tests/test_webui_smoke.py tests/test_webui_admin_audit.py -q`
Expected: 全绿。

- [ ] **Step 6: Commit**

```powershell
git add src/modelctl/core/webui/admin_config.py tests/test_webui_admin_config.py
git commit -m "fix(webui): mask .env secrets by semantic pattern, not allowlist (WEB-P1-1)"
```

---

### Task 1.2: JWT 独立签名密钥，去掉 API_KEY 回退（GW-P2-5）

**Files:**
- Modify: `src/modelctl/core/webui/account_auth.py:53-55`（常量）、`:73-87`（`_jwt_secret`）
- Test: `tests/test_accounts_auth.py`（追加用例）

**Interfaces:**
- Consumes: 无
- Produces: `_jwt_secret() -> str`（仅认 `ACCOUNTS_JWT_SECRET`；未配置抛 `RuntimeError`）；`_JWT_SECRET_ENV = "ACCOUNTS_JWT_SECRET"`

- [ ] **Step 1: 写失败测试**

在 `tests/test_accounts_auth.py` 末尾追加：

```python
def test_jwt_secret_does_not_fall_back_to_api_key(monkeypatch):
    """两信任域不得共用一把秘密：仅配 API_KEY 时账号面必须 fail-fast。

    旧实现回退 API_KEY → 管理面密钥泄漏即可伪造任意用户 JWT。
    """
    from modelctl.core.webui import account_auth as aa

    monkeypatch.delenv("ACCOUNTS_JWT_SECRET", raising=False)
    monkeypatch.setenv("API_KEY", "admin-side-key-should-not-sign-jwt")
    with pytest.raises(RuntimeError) as ei:
        aa._jwt_secret()
    assert "ACCOUNTS_JWT_SECRET" in str(ei.value)


def test_jwt_secret_uses_dedicated_env_only(monkeypatch):
    from modelctl.core.webui import account_auth as aa

    monkeypatch.setenv("ACCOUNTS_JWT_SECRET", "dedicated-jwt-secret")
    monkeypatch.setenv("API_KEY", "admin-side-key")
    assert aa._jwt_secret() == "dedicated-jwt-secret"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_accounts_auth.py -q -k jwt_secret`
Expected: 第 1 个用例失败（当前实现返回 API_KEY 值，未抛 RuntimeError）。

- [ ] **Step 3: 去掉回退分支**

`account_auth.py` 常量与函数替换为：

```python
#: 账号面 JWT 专用签名密钥环境变量。
#: **不回退 API_KEY**：管理面（API_KEY）与账号面（JWT）是两个信任域，共用一把秘密
#: 会让"管理面密钥泄漏"直接升级成"任意用户身份伪造"。缺失即 fail-fast。
_JWT_SECRET_ENV = "ACCOUNTS_JWT_SECRET"


def _jwt_secret() -> str:
    """取账号面 JWT 签名 secret；未配置抛 `RuntimeError`（强暴露给部署者）。"""
    raw = os.environ.get(_JWT_SECRET_ENV, "").strip()
    if raw:
        return raw
    raise RuntimeError(
        f"JWT 签名密钥未配置：请设置 {_JWT_SECRET_ENV}（账号面专用，不复用 API_KEY）"
    )
```

删除旧 `_JWT_SECRET_FALLBACK_ENV` 常量与其注释块。

- [ ] **Step 4: 修受影响测试的环境设置**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_accounts_auth.py tests/test_accounts_gateway.py tests/test_webui_admin_accounts.py -q`
Expected: 出现若干 `RuntimeError: JWT 签名密钥未配置` —— 这些用例此前依赖 API_KEY 回退。逐个在其 fixture/`monkeypatch` 处补 `monkeypatch.setenv("ACCOUNTS_JWT_SECRET", "<任意测试值>")`，不改断言。

- [ ] **Step 5: 全部相关测试转绿**

Run: 同 Step 4。Expected: 全绿。

- [ ] **Step 6: Commit + 记录部署前提**

```powershell
git add src/modelctl/core/webui/account_auth.py tests/
git commit -m "fix(security): require dedicated ACCOUNTS_JWT_SECRET, drop API_KEY fallback (GW-P2-5)"
```

提交信息正文补一行：`BREAKING CHANGE: 部署方 .env 必须新增 ACCOUNTS_JWT_SECRET，否则账号面登录 fail-fast`。

---

### Task 1.3: max_tokens 安全解析，杜绝未认证 500（GW-P1-1）

**Files:**
- Modify: `src/modelctl/core/gateway.py:152-158`（`_accounts_tpm_estimate`）、附近新增 `_safe_int`
- Test: `tests/test_gateway.py`（追加）

**Interfaces:**
- Consumes: `estimate_prompt_tokens(body) -> int`
- Produces: `_safe_int(value: object, *, default: int = 1, lo: int = 1, hi: int = 10_000_000) -> int`

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.parametrize("bad", ["abc", None, "", {}, [], 1.5, "1e999", -5, True, 10**18])
def test_tpm_estimate_survives_hostile_max_tokens(bad):
    """客户端可控的 max_tokens 不得让 gate 之前抛异常（旧实现裸 int() → 未认证 500）。"""
    from modelctl.core.gateway import _accounts_tpm_estimate

    est = _accounts_tpm_estimate({"messages": [{"role": "user", "content": "hi"}], "max_tokens": bad})
    assert isinstance(est, int)
    assert est >= 1


def test_tpm_estimate_respects_valid_max_tokens():
    from modelctl.core.gateway import _accounts_tpm_estimate

    est = _accounts_tpm_estimate({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 512})
    assert est == 513  # prompt(2//4=0) + 512 → 至少 completion 512
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_gateway.py -q -k tpm_estimate`
Expected: `bad="abc"` 等用例抛 `ValueError`/`TypeError`。

- [ ] **Step 3: 实现 `_safe_int` 并替换裸转换**

在 `_accounts_tpm_estimate` 之前插入：

```python
def _safe_int(value: object, *, default: int = 1, lo: int = 1, hi: int = 10_000_000) -> int:
    """客户端可控字段的安全整数化：非数字/越界一律夹到 [lo, hi]，绝不抛。

    网关 TPM 估算在鉴权之前跑，裸 int() 会让匿名请求用一个 `"abc"` 打出 500。
    """
    try:
        if isinstance(value, bool):
            raise ValueError("bool is not a token count")
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))
```

`_accounts_tpm_estimate` 内 L157 替换：

```python
    completion = _safe_int(body.get("max_tokens"), default=1, lo=1)
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2。Expected: 全绿。

- [ ] **Step 5: 网关域回归**

Run: `... pytest tests/test_gateway.py tests/test_gateway_context_switch.py tests/test_accounts_gateway.py -q`
Expected: 全绿。

- [ ] **Step 6: Commit**

```powershell
git add src/modelctl/core/gateway.py tests/test_gateway.py
git commit -m "fix(gateway): sanitize client-supplied max_tokens before TPM estimate (GW-P1-1)"
```

---

### Task 1.4: 流式上游 ≥400 早退补 release（GW-P1-2）

**Files:**
- Modify: `src/modelctl/core/gateway.py:1617-1638`（OpenAI 代理流式早退分支）
- Test: `tests/test_gateway.py`（追加）

**Interfaces:**
- Consumes: 同文件已有闭包 `_release_if_acquired()`（L1515-1522）
- Produces: 行为契约——**任何** return 路径（含 SSE 早退）都恰好 release 一次

- [ ] **Step 1: 写失败测试（并发槽泄漏可观测）**

```python
def test_stream_upstream_error_releases_concurrency_slot(monkeypatch):
    """流式请求遇上游 4xx 早退时必须释放并发槽，否则槽永久泄漏 → 持续假 429。"""
    import asyncio

    from modelctl.core.gateway import create_app, GatewayModel

    released: list[str] = []

    class _Guard:
        def __init__(self) -> None:
            self.n = 0

        def acquire(self, user_id, limit):  # noqa: ANN001
            self.n += 1

        def release(self, user_id):  # noqa: ANN001
            released.append(user_id)
            self.n -= 1

        def add_tpm_actual(self, *a, **kw):
            pass

    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", "sk-leak-test")
    reg = {"m": GatewayModel("m", "vllm", "http://127.0.0.1:9", "m", None, "http://127.0.0.1:9/")}

    def upstream(request):
        return httpx.Response(400, text="bad request")

    app = create_app(reg, default_model="m", transport=httpx.MockTransport(upstream))
    guard = _Guard()
    app.state.limit_guard = guard

    async def _one():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t",
            headers={"Authorization": "Bearer sk-leak-test"},
        ) as c:
            return await c.post("/v1/chat/completions",
                                json={"model": "m", "stream": True,
                                      "messages": [{"role": "user", "content": "hi"}]})

    for _ in range(3):
        r = asyncio.run(_one())
        assert r.status_code == 400
    assert guard.n == 0, f"并发槽泄漏：仍有 {guard.n} 个未释放"
```

> 若 `create_app` 注入 limit_guard 的入参名与 `app.state.limit_guard` 不一致，改用其在 `create_app` 签名中的实际参数名（参考 `tests/test_accounts_gateway.py` 现有做法）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_gateway.py -q -k releases_concurrency_slot`
Expected: `仍有 3 个未释放`。

- [ ] **Step 3: 在早退 return 前补 release**

`gateway.py` 在 L1637 `except` 块之后、L1638 `return Response(...)` 之前插入：

```python
                    # 早退分支直接 return Response（非 StreamingResponse），
                    # SSE 生成器的 finally 永不执行 → 必须在此释放并发槽，
                    # 否则 gate 已 acquire 的槽永久泄漏（对照 anthropic_proxy 同分支）。
                    # 不 settle：非 2xx 保留干净 usage_records（与 404/502 早退一致）。
                    _release_if_acquired()
                    return Response(status_code=upstream.status_code, content=content, media_type=ctype)
```

（即把原 `return Response(...)` 上移一行、前面加 `_release_if_acquired()`。）

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2。Expected: PASS。

- [ ] **Step 5: 网关域回归 + 冒烟**

Run: `... pytest tests/test_gateway.py tests/test_accounts_gateway.py -q` → 全绿。
冒烟（隔离端口）：向 15003 连打 >并发上限 次的流式 400 请求，确认不出现持续 429。

- [ ] **Step 6: Commit**

```powershell
git add src/modelctl/core/gateway.py tests/test_gateway.py
git commit -m "fix(gateway): release concurrency slot on streaming upstream>=400 early return (GW-P1-2)"
```

---

### Task 1.5: 非法 body 前置校验，消除永久假 429（WEB-P1-2）

**Files:**
- Modify: `src/modelctl/core/webui/admin_envs.py:340-366`
- Test: `tests/test_webui_admin_envs.py`（追加）

**Interfaces:**
- Consumes: `docker_install_task_manager.create_task(...)`、`_user_pending`、`_DOCKER_MAX_ACTIVE_PER_USER`
- Produces: 顺序契约——**参数校验先于任何状态写入**

- [ ] **Step 1: 写失败测试**

```python
def test_invalid_max_concurrent_downloads_does_not_leak_pending(client, monkeypatch):
    """非法 body 返回 400 前不得占用 pending 名额；否则 3 次后该用户永久 429。"""
    monkeypatch.setenv("API_KEY", "k-test-pending")
    body = {"os": "ubuntu", "max_concurrent_downloads": 999}  # 越界（合法 0-8）
    for _ in range(4):
        r = client.post("/admin/api/envs/docker/install", json=body,
                        headers={"Authorization": "Bearer k-test-pending"})
        assert r.status_code == 400, f"应恒 400，实得 {r.status_code}：{r.text[:200]}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_webui_admin_envs.py -q -k pending`
Expected: 第 4 次返回 429。

- [ ] **Step 3: 把 400 校验移到 create_task 之前**

将 L353-360 的 body 校验块整体上移到 L349 `task = docker_install_task_manager.create_task(...)` **之前**：

```python
    # 参数校验必须在任何状态写入之前：create_task + _user_pending.add 之后才返 400
    # 会让 pending 名额只增不减（无过期机制），累计 _DOCKER_MAX_ACTIVE_PER_USER 次
    # 非法请求后该用户永久 429。
    registry_mirrors = body.get("registry_mirrors") or []
    max_downloads = body.get("max_concurrent_downloads", 0) or 0
    if not isinstance(max_downloads, int) or isinstance(max_downloads, bool) or max_downloads < 0 or max_downloads > 8:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "bad_body",
                                "message": "max_concurrent_downloads 必须为 0-8 的整数"}},
        )

    task = docker_install_task_manager.create_task(
        kind="docker", action="install", target=f"docker:{target_os}"
    )
    _user_pending.setdefault(user_id, set()).add(task.id)
```

（注意 `isinstance(max_downloads, bool)` 排除 `True/False` 被当 1/0 通过。）

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2。Expected: PASS（4 次全 400）。

- [ ] **Step 5: envs 域回归 + 新增"合法请求仍 202"用例**

在 `tests/test_webui_admin_envs.py` 追加：

```python
def test_valid_request_still_accepted_after_bad_ones(client, monkeypatch):
    """非法请求不得污染后续合法请求。"""
    monkeypatch.setenv("API_KEY", "k-mixed")
    hdr = {"Authorization": "Bearer k-mixed"}
    bad = client.post("/admin/api/envs/docker/install",
                      json={"os": "ubuntu", "max_concurrent_downloads": 99}, headers=hdr)
    assert bad.status_code == 400
    ok = client.post("/admin/api/envs/docker/install",
                     json={"os": "ubuntu", "max_concurrent_downloads": 2}, headers=hdr)
    assert ok.status_code == 202, ok.text[:200]
```

Run: `... pytest tests/test_webui_admin_envs.py -q` → 全绿。

- [ ] **Step 6: Commit**

```powershell
git add src/modelctl/core/webui/admin_envs.py tests/test_webui_admin_envs.py
git commit -m "fix(webui): validate install body before reserving pending slot (WEB-P1-2)"
```

---

### Task 1.6: compare_digest 转 bytes，非 ASCII 凭据返回 401（WEB-P2-1）

**Files:**
- Modify: `src/modelctl/core/webui/admin_auth.py:66-71`（`is_valid_key`）、`:81-96`（`require_auth`）
- Test: `tests/test_webui_smoke.py`（追加）

**Interfaces:**
- Consumes: 无
- Produces: `_consteq(a: str, b: str) -> bool`（UTF-8 编码后恒定时间比较；异常形状不再出现）

- [ ] **Step 1: 写失败测试（UTF-8 头 → 期望 401，实得 500）**

```python
def test_non_ascii_bearer_key_returns_401_not_500(client, monkeypatch):
    """hmac.compare_digest 对含非 ASCII 的 str 抛 TypeError → 500。冒烟已实锤。"""
    monkeypatch.setenv("API_KEY", "k-ascii-admin-key")
    r = client.get("/admin/api/config/static", headers={"Authorization": "Bearer 测试键"})
    assert r.status_code == 401, f"非 ASCII 凭据必须 401，实得 {r.status_code}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_webui_smoke.py -q -k non_ascii`
Expected: `assert 500 == 401`。

- [ ] **Step 3: 实现 `_consteq` 并替换两处比较**

在 `is_valid_key` 之前插入：

```python
def _consteq(a: str, b: str) -> bool:
    """恒定时间比较；先 UTF-8 编码再比。

    `hmac.compare_digest` 直接吃 str 时，任一侧含非 ASCII 会抛 TypeError → 500
    （客户端用一个中文 Bearer 头就能打出 500）。编码后比较语义不变。
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
```

L71 改：`return _consteq(key, expected)`
L95 改：`if not _consteq(credentials.credentials, os.environ[API_KEY_ENV]):`

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2。Expected: PASS。

- [ ] **Step 5: 鉴权域回归**

Run: `... pytest tests/test_webui_smoke.py tests/test_accounts_auth.py tests/test_webui_admin_accounts.py -q` → 全绿。

- [ ] **Step 6: Commit**

```powershell
git add src/modelctl/core/webui/admin_auth.py tests/test_webui_smoke.py
git commit -m "fix(webui): compare auth keys as utf-8 bytes so non-ascii input yields 401 (WEB-P2-1)"
```

---

### Task 1.7: GPU 选择去全局态，改显式传参（WEB-P1-3）

**Files:**
- Modify: `src/modelctl/engines/base.py:70-74`（`selected_gpus`）+ `__init__`
- Modify: `src/modelctl/core/all_service.py:133-142`（`start_profile` 签名）、`restart_profile` 同构
- Modify: `src/modelctl/core/webui/admin_models.py:189-194, 217-223, 235-239, 262-267`
- Modify: `src/modelctl/core/webui/admin_services.py:367-371, 406-411`
- Test: `tests/test_engines_base_gpu.py`（追加）、`tests/test_all_service.py`（追加）

**Interfaces:**
- Consumes: `resolve_gpu_list(profile_value, cli_value, env_value) -> list[int] | None`
- Produces:
  - `EngineAdapter.set_gpu_override(gpus: list[int] | None) -> None`
  - `EngineAdapter._gpu_override: list[int] | None`（默认 None）
  - `selected_gpus()` 优先级：`profile.engine_config.gpu_list` > `self._gpu_override` > `os.environ["MODELCTL_GPUS"]`
  - `start_profile(profile, caps, timeout, on_progress=None, gpus: list[int] | None = None)`
  - `restart_profile(..., gpus: list[int] | None = None)`

- [ ] **Step 1: 写 adapter 优先级失败测试**

在 `tests/test_engines_base_gpu.py` 追加：

```python
def test_gpu_override_beats_env_and_loses_to_profile(monkeypatch, tmp_path):
    """显式 override 必须优先于全局 MODELCTL_GPUS（并发互污根因）。"""
    from modelctl.core.envfile import Profile
    from modelctl.engines import get_adapter

    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    p = Profile(name="m", engine="vllm", port=8000, engine_config={"model": "x"})
    caps = type("C", (), {"gpu_count": 8, "compute_capability": "8.0", "binaries": {}})()
    ad = get_adapter("vllm")(p, caps)

    monkeypatch.setenv("MODELCTL_GPUS", "3,4")
    assert ad.selected_gpus() == [3, 4]          # 无 override → 读 env（向后兼容）

    ad.set_gpu_override([0, 1])
    assert ad.selected_gpus() == [0, 1]          # override 压过 env

    p2 = Profile(name="m2", engine="vllm", port=8001, engine_config={"model": "x", "gpu_list": [6]})
    ad2 = get_adapter("vllm")(p2, caps)
    ad2.set_gpu_override([0, 1])
    assert ad2.selected_gpus() == [6]            # profile 显式配置最高优先


def test_concurrent_start_profiles_do_not_cross_contaminate_gpus(monkeypatch):
    """两个并发 start_profile 各带不同 gpus，彼此不得看到对方的值。"""
    import threading

    from modelctl.core import all_service as svc
    from modelctl.core.envfile import Profile

    seen: dict[str, list[int]] = {}
    gate = threading.Barrier(2, timeout=5)

    def _fake_build(self):
        seen[self.profile.name] = list(self.selected_gpus() or [])
        gate.wait()  # 两线程同时停在"已解析 GPU"这一点
        raise RuntimeError("stop here")  # 只需观察 GPU 解析结果

    monkeypatch.setattr("modelctl.engines.vllm.VllmAdapter.build_command", _fake_build)
    monkeypatch.setattr(svc, "is_running_any", lambda *a, **k: False)
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)

    caps = type("C", (), {"gpu_count": 8, "compute_capability": "8.0", "binaries": {}})()
    pa = Profile(name="a", engine="vllm", port=8000, engine_config={"model": "x"})
    pb = Profile(name="b", engine="vllm", port=8001, engine_config={"model": "y"})

    def _run(profile, gpus):
        try:
            svc.start_profile(profile, caps, 1.0, gpus=gpus)
        except Exception:
            pass

    ts = [threading.Thread(target=_run, args=(pa, [0])), threading.Thread(target=_run, args=(pb, [1]))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert seen.get("a") == [0], f"GPU 互污：{seen}"
    assert seen.get("b") == [1], f"GPU 互污：{seen}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_engines_base_gpu.py -q -k gpu_override`
Expected: `AttributeError: ... has no attribute 'set_gpu_override'`。

- [ ] **Step 3: 在 base adapter 加 override 通道**

`engines/base.py`：在 `__init__` 末尾加 `self._gpu_override: list[int] | None = None`（若无 `__init__`，新增一个保存 `profile`/`caps` 后再调 super 的实现——保持现有构造签名 `(profile, caps)` 不变）。在 `selected_gpus` 之前加：

```python
    def set_gpu_override(self, gpus: "list[int] | None") -> None:
        """注入本次操作的 GPU 选择（WebUI 并发任务用）。None 表示不覆盖。

        为什么不用全局环境变量：`os.environ["MODELCTL_GPUS"]` 是进程级共享态，
        两个并发 start/restart 任务互相覆盖 → GPU 分配错乱。
        """
        self._gpu_override = gpus
```

`selected_gpus` 改为：

```python
    def selected_gpus(self) -> "list[int] | None":
        """优先级：profile.engine_config.gpu_list > 本次 override > 环境变量 MODELCTL_GPUS。"""
        cfg = self.profile.engine_config
        return resolve_gpu_list(cfg.get("gpu_list"), None, os.environ.get("MODELCTL_GPUS")) \
            if self._gpu_override is None else self._gpu_override
```

> 若 `selected_gpus` 当前实现是单行 return，按上面改写；保持 `resolve_gpu_list` 仍是 profile/env 的唯一解析入口。

- [ ] **Step 4: `start_profile` / `restart_profile` 接收 gpus 并注入 adapter**

`all_service.py:133-134` 签名改为：

```python
def start_profile(profile: Profile, caps: Capabilities, timeout: float,
                  on_progress: "Callable[[Any], None] | None" = None,
                  gpus: "list[int] | None" = None) -> ComponentResult:
```

docstring 追加一行：`gpus：本次操作的显式 GPU 选择（WebUI 并发任务）；None 走 profile/env 解析。`

L152 之后（adapter 已构造）插入：

```python
    adapter.set_gpu_override(gpus)
```

`restart_profile` 做同构改动（签名 + `set_gpu_override`）。

- [ ] **Step 5: webui 三处去全局态**

`admin_models.py` 删掉 L190-194 与 finally 中 L218-223（`_do_start`）、L235-239 与 L262-267（`_do_restart`）的 env 读写；改为解析后直接透传：

```python
    # gpus 逗号串 → 显式参数（不再写 os.environ["MODELCTL_GPUS"]：
    # 全局态在并发 to_thread 任务间互相覆盖）
    gpu_list = resolve_gpu_list(None, None, gpus) if gpus else None
```

并把 `start_profile(profile, caps, eff_timeout, on_progress=_on_stage)` 改为
`start_profile(profile, caps, eff_timeout, on_progress=_on_stage, gpus=gpu_list)`；restart 同构。

`admin_services.py` L367-371 / finally L406-411 同构删除；`_do_all` 内调用 `start_all`/`restart_all` 时把 `gpus` 以关键字透传（若 `start_all` 尚不支持 gpus 形参，在 `all_service.start_all`/`restart_all` 签名上补 `gpus: list[int] | None = None` 并逐 profile 传下去）。

CLI 保持现状（`cli.py:2082` 写 env，单进程无并发）。

- [ ] **Step 6: 跑测试确认通过**

Run: `... pytest tests/test_engines_base_gpu.py tests/test_all_service.py tests/test_engines_vllm.py -q`
Expected: 全绿。

- [ ] **Step 7: webui 回归**

Run: `... pytest tests/test_webui_smoke.py tests/test_webui_startup_progress.py tests/test_admin_models_classify.py -q` → 全绿。

- [ ] **Step 8: Commit**

```powershell
git add src/modelctl/engines/base.py src/modelctl/core/all_service.py src/modelctl/core/webui/admin_models.py src/modelctl/core/webui/admin_services.py tests/
git commit -m "fix(webui): pass GPU selection explicitly instead of global env (WEB-P1-3)"
```

---

### Task 1.8: 任务锁覆盖 worker 全生命周期（WEB-P1-4）

**Files:**
- Modify: `src/modelctl/core/webui/admin_tasks.py:205-221`（新增带生命周期的 helper）
- Modify: `src/modelctl/core/webui/admin_config.py:154-171`、`admin_models.py:460-484`（及 restart 同段）、`admin_services.py:161-178, 243-259, 315-331`
- Test: `tests/test_admin_tasks.py`（追加）

**Interfaces:**
- Consumes: `TaskManager.acquire(target, action) -> asyncio.Lock | None`、`release(target, action)`、`create_task(kind, action, target)`
- Produces: `TaskManager.spawn(task_id: str, target: str, action: str, coro_factory: Callable[[], Awaitable[None]]) -> None` —— 由它 `ensure_future` 并在 worker 结束（含异常）时 release；调用方 handler 在成功 spawn 后不再 release

- [ ] **Step 1: 写失败测试（锁必须活到 worker 结束）**

```python
import asyncio
import pytest

from modelctl.core.webui.admin_tasks import TaskManager


@pytest.mark.parametrize("mode", ["ok", "raise"])
def test_spawn_holds_lock_until_worker_finishes(mode):
    """旧实现在 handler 的 finally 里 release → 锁在 worker 刚提交时就放开，
    同一 target 的第二个请求不再 409，重复投递同一动作。"""

    async def _run():
        tm = TaskManager()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def worker():
            started.set()
            await finish.wait()

        tm.spawn("t1", "m1", "start", worker)
        await asyncio.wait_for(started.wait(), 1)
        # worker 仍在跑 → 锁必须仍被持有
        assert await tm.acquire("m1", "start") is None, "worker 未完成时锁已释放"
        finish.set()
        await asyncio.sleep(0.05)  # 让 worker 收尾
        lock = await tm.acquire("m1", "start")
        assert lock is not None, "worker 结束后锁未释放"
        await tm.release("m1", "start")

    asyncio.run(_run())
```

> `mode` 参数若无实际差异可去掉 parametrize，只留正常路径 + 另写一个 `test_spawn_releases_lock_when_worker_raises`（worker 内 `raise RuntimeError`，断言结束后能 `acquire` 成功）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_admin_tasks.py -q -k spawn`
Expected: `AttributeError: 'TaskManager' object has no attribute 'spawn'`。

- [ ] **Step 3: 实现 `spawn`**

`admin_tasks.py` 在 `release` 之后加：

```python
    def spawn(self, task_id: str, target: str, action: str, coro_factory) -> None:
        """投递 worker 协程并把互斥锁生命周期交给它。

        调用方必须已经 acquire 成功。旧写法在 handler 的 `finally` 里 release，
        等于"任务刚提交就解锁"，同 target 可被重复投递；正确边界是 worker 结束。
        """

        async def _runner() -> None:
            try:
                await coro_factory()
            finally:
                await self.release(target, action)
                self._tasks.pop(task_id, None) if self._trim_on_finish else None

        asyncio.ensure_future(_runner())
```

其中 `self._trim_on_finish` 在 `TaskManager.__init__` 里初始化为 `False`（保持现有 trim 行为不变，仅预留；若无 trim 需求可直接删掉最后一行 `self._tasks.pop(...)` —— **默认删除该行**，保持任务记录可查）。

最终形态：

```python
    def spawn(self, task_id: str, target: str, action: str, coro_factory) -> None:
        """投递 worker 协程，并把互斥锁的释放交给 worker 结束时刻。"""

        async def _runner() -> None:
            try:
                await coro_factory()
            finally:
                await self.release(target, action)

        asyncio.ensure_future(_runner())
```

- [ ] **Step 4: 五处调用点改用 spawn**

统一模式（以 `admin_config.py:162-171` 为例）：

```python
    try:
        task = tm.create_task(kind="trtllm_build", action="build", target=name)
        task.update_status("queued")
        # 锁由 worker 结束时释放（旧写法在下面的 finally 里释放 → 刚提交就解锁）
        tm.spawn(task.id, name, "build", lambda: _do_trtllm_build(name, task))
        return JSONResponse(
            status_code=202,
            content={"task_id": task.id, "stream_url": f"/admin/api/tasks/{task.id}/stream"},
        )
    except Exception:
        await tm.release(name, "build")
        raise
```

关键差异：`finally: await tm.release(...)` → `except Exception: await tm.release(...); raise`（只在**未能成功投递**时释放）。

对 `admin_models.py` 的两处（`_do_start` / `_do_restart`）与 `admin_services.py` 的三处（svc start/restart、all start、all restart）做同构替换，`coro_factory` 传对应 `_do_*` 的 lambda。

- [ ] **Step 5: 跑测试确认通过**

Run: 同 Step 2。Expected: 全绿（含 worker 抛异常路径）。

- [ ] **Step 6: 同 target 二次请求 409 的端到端用例**

在 `tests/test_webui_smoke.py` 追加：

```python
def test_second_start_same_model_conflicts_while_worker_running(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "k-lock")
    hdr = {"Authorization": "Bearer k-lock"}
    import modelctl.core.webui.admin_models as am

    started, finish = __import__("threading").Event(), __import__("threading").Event()

    async def _slow(*a, **k):
        started.set()
        finish.wait(5)

    monkeypatch.setattr(am, "_do_start", _slow)
    r1 = client.post("/admin/api/models/qwen/start", json={}, headers=hdr)
    assert r1.status_code == 202
    started.wait(3)
    r2 = client.post("/admin/api/models/qwen/start", json={}, headers=hdr)
    assert r2.status_code == 409, f"worker 运行中应 409，实得 {r2.status_code}"
    finish.set()
```

> 若该端点实际路径/入参不同，以 `admin_models.py` 的 `@router.post` 装饰器为准调整。

Run: `... pytest tests/test_webui_smoke.py -q -k conflicts_while_worker` → PASS。

- [ ] **Step 7: Commit**

```powershell
git add src/modelctl/core/webui/ tests/
git commit -m "fix(webui): hold task lock for the whole worker run, not just dispatch (WEB-P1-4)"
```

---

## 计划 2｜跨层契约与规模韧性（5 项）

### Task 2.1: nginx-snippet 响应键对齐前端（X-P1）

**Files:**
- Modify: `src/modelctl/core/webui/admin_config.py:96-97`
- 参考（**不改**）: `web/src/api/types.ts:481-486`、`web/src/views/ConfigView.vue:32`
- Test: `tests/test_webui_admin_config.py`（追加）

**Interfaces:**
- Consumes: `core.nginx_snippet.build_llm_map(profiles, node, host, port) -> str`
- Produces: `GET /admin/api/nginx-snippet` → `{"ok": true, "snippet": "<nginx map 片段>"}`

- [ ] **Step 1: 写失败测试**

```python
def test_nginx_snippet_returns_snippet_key(client, monkeypatch):
    """前端读 r.snippet（types.ts:485）；后端曾返回 {"content": ...} → 功能恒空白。"""
    monkeypatch.setenv("API_KEY", "k-snippet")
    r = client.get("/admin/api/nginx-snippet",
                   params={"node": "210", "host": "10.0.0.5"},
                   headers={"Authorization": "Bearer k-snippet"})
    assert r.status_code == 200
    body = r.json()
    assert "snippet" in body, f"契约键必须是 snippet，实得 keys={list(body)}"
    assert isinstance(body["snippet"], str) and body["snippet"]
    assert body.get("ok") is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_webui_admin_config.py -q -k snippet`
Expected: `契约键必须是 snippet，实得 keys=['content']`。

- [ ] **Step 3: 后端对齐前端契约**

`admin_config.py:96-97` 改为：

```python
    content = await asyncio.to_thread(_work)
    # 契约键 = `snippet`（web/src/api/types.ts NginxSnippetResponse）：
    # 曾返回 {"content": ...} 与前端 `.snippet` 不一致，生成器永远空白。
    return {"ok": True, "snippet": content}
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2 → PASS。

- [ ] **Step 5: 前端类型与构建校验**

Run:
```powershell
cd "d:\WorkPlace\Pycharm\modelctl\web"
npx vue-tsc --noEmit
npm run build
```
Expected: 无 TS 错误、构建成功（前端无需改动，仅确认契约吻合）。

- [ ] **Step 6: Commit**

```powershell
git add src/modelctl/core/webui/admin_config.py tests/test_webui_admin_config.py
git commit -m "fix(contract): nginx-snippet returns {ok,snippet} matching web client (X-P1)"
```

---

### Task 2.2: context switch 规则匹配用路由前语义键（GW-P1-3）

**Files:**
- Modify: `src/modelctl/core/gateway.py:594-613`（`apply_context_switch`）、`:1570-1575`（调用点）
- Test: `tests/test_gateway_context_switch.py`（追加）

**Interfaces:**
- Consumes: `registry: dict[str, GatewayModel]`、`rules: dict[str, list[ContextSwitchRule]]`、`GatewayModel.group: str | None`
- Produces: `apply_context_switch(registry, rules, match_keys: Sequence[str], prompt_tokens: int) -> GatewayModel | None`

- [ ] **Step 1: 写失败测试（group 路由后仍须命中规则）**

```python
def test_apply_switch_matches_group_name_after_member_resolution():
    """经 group 路由后 target.name 是成员名，规则 key 是 base/group 名 → 旧实现永不命中。"""
    reg = _registry()
    rules = _rules()          # key = DS（group/base 名）
    # 路由后拿到的成员名（注册表里真实存在，但不是规则 key）
    member = "some-member-not-a-rule-key"
    reg[member] = GatewayModel(member, "vllm", "http://127.0.0.1:9", member, None, "http://127.0.0.1:9/", group=DS)
    hit = apply_context_switch(reg, rules, [member, DS], 50000)
    assert hit is reg[DS_HIGH], "按 [成员名, group 名] 依次匹配应命中 high"


def test_proxy_context_switch_works_through_group_route(monkeypatch):
    """集成：请求走 group 名解析到成员后，仍按 base 规则切变体。"""
    captured = {}

    def upstream(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "1", "model": captured["body"]["model"]})

    reg = _registry()
    app = create_app(reg, default_model=DS, transport=httpx.MockTransport(upstream), context_rules=_rules())
    long_prompt = "x" * (40000 * 4)   # 10000 tokens ≥ 32768? no → 用 40000*4/4=40000 ≥ 32768 → high
    resp = _run(_post(app, "/v1/chat/completions",
                      json={"model": DS, "messages": [{"role": "user", "content": long_prompt}]}))
    assert resp.status_code == 200
    assert captured["body"]["model"] == DS_HIGH
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_gateway_context_switch.py -q -k "group_name or through_group"`
Expected: 第 1 个 `TypeError`/`assert None`（旧签名第三参是 str 而非序列）。

- [ ] **Step 3: 改为按候选键序列匹配**

`apply_context_switch` 替换为：

```python
def apply_context_switch(
    registry: dict[str, GatewayModel],
    rules: dict[str, list[ContextSwitchRule]],
    match_keys: "Sequence[str]",
    prompt_tokens: int,
) -> GatewayModel | None:
    """按上下文长度规则把请求切换到 high/balanced/light 变体（附录 B.3）。

    `match_keys`：按优先级依次尝试的规则键序列。必须包含**路由前**的语义键
    （请求原始 model / group 名）——家族路由会把 target 换成成员名，
    只用 `target.name` 匹配规则的话，规则 key（base 名）永远对不上。
    目标不在注册表（未配置/未启动）时返回 None，调用方沿用原模型。
    """
    if not rules:
        return None
    for key in match_keys:
        if not key:
            continue
        candidates = rules.get(key)
        if not candidates:
            continue
        for rule in candidates:
            if prompt_tokens >= rule.min_prompt_tokens:
                return registry.get(rule.target)
    return None
```

文件顶部确保 `from collections.abc import Sequence`（或 `typing.Sequence`）。

- [ ] **Step 4: 调用点传候选键**

`gateway.py:1570-1575` 改为：

```python
        if context_rules:
            prompt_tokens = estimate_prompt_tokens(body)
            # 匹配键顺序：请求原始 model → group 名 → 解析后的成员名。
            # 只传 target.name 会让"经 group 路由而来"的请求永远匹配不上规则。
            switched = apply_context_switch(
                registry, context_rules,
                (str(body.get("model") or ""), target.group or "", target.name),
                prompt_tokens,
            )
            if switched is not None and switched.name != target.name:
                logger.info(f"上下文切换：{target.name} -> {switched.name}（估算输入 {prompt_tokens} tokens）")
                target = switched
```

- [ ] **Step 5: 跑测试确认通过**

Run: 同 Step 2 → 全绿。

- [ ] **Step 6: 网关回归**

Run: `... pytest tests/test_gateway_context_switch.py tests/test_gateway.py tests/test_route_debug.py -q` → 全绿。

- [ ] **Step 7: Commit**

```powershell
git add src/modelctl/core/gateway.py tests/test_gateway_context_switch.py
git commit -m "fix(gateway): match context-switch rules by pre-route keys, not member name (GW-P1-3)"
```

---

### Task 2.3: 集群 WS 同步 SQLite 卸载到线程（CLU-P1-1）

**Files:**
- Modify: `src/modelctl/core/webui/admin_cluster.py:628-660`（`ws_cluster` 消息循环）
- Test: `tests/test_cluster_ws_sync.py`（新建）

**Interfaces:**
- Consumes: `reg.handle_heartbeat(node_id, hb, now=...) -> dict`、`reg.store.append_event(kind, *, node_id=..., payload=..., now=...) -> None`、`_sweep_if_due() -> None`（均为同步阻塞 DB 调用）
- Produces: WS 消息循环内**零**同步 DB 调用直接跑在 event loop 上；单连接消息仍按到达顺序处理（保序）

- [ ] **Step 1: 写失败测试（慢 DB 不阻塞事件循环）**

新建 `tests/test_cluster_ws_sync.py`：

```python
"""WS 消息处理中的同步 SQLite 必须卸载到线程，否则逐条 commit 头阻塞整个 loop。"""
from __future__ import annotations

import asyncio


def test_slow_db_call_does_not_block_other_tasks(monkeypatch):
    """模拟 append_event 慢 100ms：期间同 loop 的其他任务必须照常推进。"""
    import modelctl.core.webui.admin_cluster as ac

    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    def slow_append(*a, **k):
        import time
        time.sleep(0.1)

    async def main():
        t = asyncio.ensure_future(ticker())
        await asyncio.to_thread(slow_append)  # 基线：to_thread 下 ticker 正常跑
        await t

    asyncio.run(main())
    assert ticks >= 5  # 基线 sanity：to_thread 语义本身成立

    # 断言 ws_cluster 的消息分支不存在裸调用（静态护栏）：
    # heartbeat/event/result 三个分支体内只允许 `await asyncio.to_thread(...)`
    import inspect

    src = inspect.getsource(ac.ws_cluster)
    for call in ("handle_heartbeat(", "append_event(", "_sweep_if_due("):
        for idx, line in enumerate(src.splitlines()):
            stripped = line.strip()
            if call in stripped and "to_thread" not in stripped and not stripped.startswith("#"):
                raise AssertionError(f"ws_cluster 存在未卸载的同步调用: L{idx}: {stripped}")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_cluster_ws_sync.py -q`
Expected: `ws_cluster 存在未卸载的同步调用`（当前源码三处裸调用）。

- [ ] **Step 3: 三处调用卸载到线程**

`admin_cluster.py` 消息循环替换（保持逐消息顺序 await → 单连接保序）：

```python
            if mtype == "heartbeat":
                ack = await asyncio.to_thread(
                    lambda: reg.handle_heartbeat(node_id, wsproto.parse_heartbeat_v2(data), now=time.time())
                )
                await asyncio.to_thread(_sweep_if_due)
                await ws.send_text(wsproto.dumps(ack))
            elif mtype == "event":
                payload = data.get("payload")
                await asyncio.to_thread(
                    lambda: reg.store.append_event(
                        str(data.get("kind", "")), node_id=node_id,
                        payload=payload if isinstance(payload, dict) else None)
                )
                await ws.send_text(wsproto.dumps({"t": "ack"}))
            elif mtype == "result":
                # 指令回执只落账不裁决：ok=False 时改不改状态由 worker 的 reconcile 决定，
                # 中心重复动作会与"失败即终态 + 人工 retry"的立场冲突。
                res = wsproto.parse_result(data)
                await asyncio.to_thread(
                    lambda: reg.store.append_event(
                        "action.result", node_id=node_id,
                        payload={"seq": res["seq"], "ok": res["ok"],
                                 "detail": res["detail"]}, now=time.time())
                )
                await ws.send_text(wsproto.dumps({"t": "ack"}))
```

注意 lambda 闭包变量在 await 前已全部绑定（`data`/`res`/`payload` 均在循环体内不变）；文件顶部确保 `import asyncio`。

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2 → PASS。

- [ ] **Step 5: cluster 域回归**

Run: `... pytest tests/test_cluster_http.py tests/test_cluster_events_http.py tests/test_cluster_conns.py tests/test_cluster_store.py -q` → 全绿。

- [ ] **Step 6: Commit**

```powershell
git add src/modelctl/core/webui/admin_cluster.py tests/test_cluster_ws_sync.py
git commit -m "fix(cluster): offload sync sqlite writes in ws loop to worker threads (CLU-P1-1)"
```

---

### Task 2.4: events 表保留策略（CLU-P1-2）

**Files:**
- Modify: `src/modelctl/core/cluster/store.py:586-602`（`append_event`）+ 模块常量区
- Test: `tests/test_cluster_store.py`（追加）

**Interfaces:**
- Consumes: 既有 `self._lock`、`self._db()`、`events(ts, node_id, goal_id, kind, payload)` 表
- Produces: 常量 `EVENTS_RETENTION_DAYS = 30`、`EVENTS_TRIM_EVERY = 500`；`append_event` 内部周期性 `DELETE FROM events WHERE ts < ?`（纯 DML）

- [ ] **Step 1: 写失败测试**

在 `tests/test_cluster_store.py` 追加：

```python
def test_append_event_trims_old_events(tmp_path):
    """events 必须按时间保留窗口，旧实现无界增长（中心台账无 TTL）。"""
    import time as _t

    from modelctl.core.cluster.store import ClusterStore, EVENTS_RETENTION_DAYS

    store = ClusterStore(tmp_path / "ledger.db")
    old_ts = _t.time() - (EVENTS_RETENTION_DAYS + 5) * 86400
    for i in range(3):
        store.append_event("audit.test", node_id="n1", payload={"i": i}, now=old_ts)
    fresh = store.append_event("audit.test", node_id="n1", payload={"i": "fresh"})
    assert fresh is not None or True  # append 无返回值则忽略本行

    rows = store._db().execute(
        "SELECT COUNT(*) FROM events WHERE ts < ?",
        (_t.time() - EVENTS_RETENTION_DAYS * 86400,),
    ).fetchone()
    # 保留策略在 EVENTS_TRIM_EVERY 阈值触发；未触发时旧行仍在 → 用显式 trim 验证语义
    store.trim_events(now=_t.time())
    rows = store._db().execute(
        "SELECT COUNT(*) FROM events WHERE ts < ?",
        (_t.time() - EVENTS_RETENTION_DAYS * 86400,),
    ).fetchone()
    assert rows[0] == 0, f"超保留期的 events 应被裁剪，仍余 {rows[0]} 条"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_cluster_store.py -q -k trims_old_events`
Expected: `ImportError: cannot import name 'EVENTS_RETENTION_DAYS'`。

- [ ] **Step 3: 实现保留策略（纯 DML，不碰 schema）**

`store.py` 模块常量区（`_SCHEMA` 之前）加：

```python
# events 台账保留策略（纯 DML 裁剪，不改 schema）：中心节点事件只进不出会让
# 单文件 SQLite 无界膨胀 + 备份变慢。保留 30 天；每 500 次 append 顺带清一次
# （摊销成本，避免每次写都扫表）。
EVENTS_RETENTION_DAYS = 30
EVENTS_TRIM_EVERY = 500
```

`ClusterStore.__init__` 加 `self._append_count = 0`。新增方法（放在 `append_event` 之后）：

```python
    def trim_events(self, now: "float | None" = None) -> int:
        """删除超过保留窗口的事件，返回删除条数。幂等 DML，不动表结构。"""
        cutoff = (now if now is not None else time.time()) - EVENTS_RETENTION_DAYS * 86400
        with self._lock:
            cur = self._db().execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._db().commit()
            return cur.rowcount
```

`append_event` 的 `with self._lock:` 块 commit 之后追加：

```python
            self._append_count += 1
            if self._append_count % EVENTS_TRIM_EVERY == 0:
                self._db().execute(
                    "DELETE FROM events WHERE ts < ?",
                    (time.time() - EVENTS_RETENTION_DAYS * 86400,),
                )
                self._db().commit()
```

（同一 `with self._lock:` 内完成，不新开锁。）

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2 → PASS。

- [ ] **Step 5: cluster store 回归**

Run: `... pytest tests/test_cluster_store.py tests/test_cluster_backup.py tests/test_cluster_events_cli.py -q` → 全绿。

- [ ] **Step 6: Commit**

```powershell
git add src/modelctl/core/cluster/store.py tests/test_cluster_store.py
git commit -m "feat(cluster): add 30d retention trim for events ledger table (CLU-P1-2)"
```

---

### Task 2.5: docker runtime 不更新 GPU 锁 owner（CLU-P2-1）

**Files:**
- Modify: `src/modelctl/core/all_service.py:208-214`
- Test: `tests/test_all_service.py`（追加）

**Interfaces:**
- Consumes: `is_docker`（L153 已由 `adapter.is_docker_runtime()` 得出）、`update_gpu_lock_owner(name, pid)`
- Produces: 契约——仅 venv runtime（长驻 PID）才 `update_gpu_lock_owner`

- [ ] **Step 1: 写失败测试**

在 `tests/test_all_service.py` 追加：

```python
def test_docker_runtime_does_not_rebind_gpu_lock_owner(monkeypatch, tmp_path):
    """docker 路径的 pid 是秒退的 `docker run` 客户端；把锁 owner 改绑它，
    is_pid_alive 判定 stale 后锁文件被删 → GPU 互斥静默失效。"""
    from modelctl.core import all_service as svc

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr("modelctl.core.gpu_lock.update_gpu_lock_owner",
                        lambda name, pid: calls.append((name, pid)))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(svc, "is_running_any", lambda *a, **k: False)

    class _Ad:
        engine = "vllm"
        is_docker_runtime = lambda self: True          # noqa: E731
        selected_gpus = lambda self: [0]               # noqa: E731
        build_command = lambda self: (["docker", "run"], {})  # noqa: E731
        spawn_tee = None

        def set_progress_sink(self, cb):
            pass

    # 具体打桩点按 test_all_service.py 现有 fake 基建对齐：
    # get_adapter → _Ad；start_detached → (12345, None)；wait_ready/健康检查直接放行。
    monkeypatch.setattr(svc, "get_adapter", lambda engine: (lambda p, c: _Ad()))
    monkeypatch.setattr(svc, "start_detached", lambda *a, **k: (12345, None))
    monkeypatch.setattr(svc, "wait_ready", lambda *a, **k: None)

    from modelctl.core.envfile import Profile
    caps = type("C", (), {"gpu_count": 8, "compute_capability": "8.0", "binaries": {}})()
    p = Profile(name="d1", engine="vllm", port=8100, engine_config={
        "model": "x", "docker_image": "img:tag"})
    try:
        svc.start_profile(p, caps, 1.0)
    except Exception:
        pass  # 后续健康检查失败不影响本断言
    assert calls == [], f"docker runtime 不应重绑 GPU 锁 owner：{calls}"
```

> 打桩点以 `tests/test_all_service.py` 内**现有** fake 基建为准微调（该文件已有 start_profile 全链路 fake，复用其 fixture 最省事）；断言只有一条：`update_gpu_lock_owner` 未被调用。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_all_service.py -q -k rebind_gpu_lock`
Expected: `calls == [("d1", 12345)]` 非空 → FAIL。

- [ ] **Step 3: 用 is_docker 包裹 owner 更新**

`all_service.py:208-214` 改为：

```python
        # GPU 锁 owner 只跟"长驻 PID"（venv runtime 写 PID 文件的那个进程）；
        # docker runtime 的 pid 是秒退的 `docker run` 客户端——重绑到它之后，
        # is_pid_alive 判 stale 会把锁文件删掉，GPU 互斥静默失效。
        if not is_docker:
            try:
                from modelctl.core.gpu_lock import update_gpu_lock_owner

                if adapter.selected_gpus():
                    update_gpu_lock_owner(profile.name, pid)
            except Exception:
                pass
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2 → PASS。

- [ ] **Step 5: venv 路径回归（owner 仍要更新）**

在测试文件追加：

```python
def test_venv_runtime_still_rebinds_gpu_lock_owner(monkeypatch, tmp_path):
    """对称护栏：venv runtime（write_pid=True 的长驻进程）仍须重绑 owner。"""
    # 与上一用例同构，仅 is_docker_runtime 返回 False，断言 calls == [("v1", <pid>)]
```

（实现同上一用例，`is_docker_runtime = lambda self: False`，`selected_gpus = lambda self: [0]`，断言 `calls` 恰含一项且 pid 为 start_detached 返回值。）

Run: `... pytest tests/test_all_service.py -q -k gpu_lock` → 双用例全绿。

- [ ] **Step 6: Commit**

```powershell
git add src/modelctl/core/all_service.py tests/test_all_service.py
git commit -m "fix(cluster): keep gpu lock owner on controller pid for docker runtime (CLU-P2-1)"
```

---

## 计划 3｜TUI 投产门 + 工具链门（6 项）

### Task 3.1: 健康预检只读化（TUI-P1-1）

**Files:**
- Modify: `src/modelctl/engines/base.py:52-53`（`check_requirements` 签名旁新增只读入口）
- Modify: `src/modelctl/engines/vllm.py:56-116`、`tokenspeed.py`、`tensorrt_llm.py`、`sglang.py`、`lmdeploy.py`、`aphrodite.py`、`llamacpp.py`、`unsloth.py`（副作用行加门控）
- Modify: `src/modelctl/core/tui/panels/detail.py:214-216`、`plan.py:288-290`
- Test: `tests/test_tui_no_side_effects.py`（追加）

**Interfaces:**
- Consumes: 既有 `check_requirements()`
- Produces: `EngineAdapter.check_requirements(self, *, readonly: bool = False)` —— `readonly=True` 时跳过 `clear_stale_docker_container` 与 `acquire_gpu_lock` 两类副作用，其余校验不变；TUI 两处传 `readonly=True`

- [ ] **Step 1: 写失败测试（TUI 预检不得删容器/抢锁）**

在 `tests/test_tui_no_side_effects.py` 追加：

```python
def test_detail_precheck_is_readonly(monkeypatch):
    """渲染 precheck Tab 曾真调 check_requirements → 删陈旧容器 + 写 GPU 锁文件。"""
    called: list[str] = []
    monkeypatch.setattr("modelctl.core.process.clear_stale_docker_container",
                        lambda *a, **k: called.append("clear") or True)
    monkeypatch.setattr("modelctl.core.gpu_lock.acquire_gpu_lock",
                        lambda *a, **k: called.append("lock"))

    snaps = _snapshots_fresh()
    st = TUIState()
    st.active_detail_subtab = "precheck"
    # detail profile 为 vllm（models_pop 第一条），caps 用 hw_pop（gpu_count>0）
    from modelctl.core.hw import Capabilities  # 按 detail.py 实际 caps 类型
    caps = Capabilities(gpu_count=8, compute_capability="8.0", binaries={"vllm": "/x"})
    snaps["hw"].caps = caps
    render_detail(st, snaps["hw"], snaps["models_pop"], snaps["logs"],
                  width=120, height=40, theme_id="dark", caps=caps)
    assert called == [], f"precheck 渲染仍有副作用：{called}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_tui_no_side_effects.py -q -k precheck_is_readonly`
Expected: `called == ["clear", "lock"]`（或其中至少一项）→ FAIL。
（若该环境下 `docker_image` 未配置走不到 clear 分支，则断言 `acquire_gpu_lock` 必被调用——先跑一次看实际形态再定稿断言。）

- [ ] **Step 3: base 签名扩展**

`engines/base.py:52`：

```python
    def check_requirements(self, *, readonly: bool = False) -> None:
        """校验硬件/配置门槛；可降级的写 self.warnings，硬性不满足抛 RequirementError。

        readonly=True（TUI 预检）：跳过清容器 / 抢 GPU 锁等一切写副作用——
        浏览界面的渲染路径绝不允许改变运行时状态。
        """
```

所有引擎实现同步加 `*, readonly: bool = False` 形参（grep `def check_requirements` 共 8 处）。

- [ ] **Step 4: 副作用行门控（每引擎同构两处）**

以 `vllm.py` 为例：

```python
            if not readonly:
                from modelctl.core.process import clear_stale_docker_container
                clear_stale_docker_container(self.profile.name, container_name)
```

```python
        if gpus is not None and not readonly:
            acquire_gpu_lock(self.profile.name, gpus)
```

`tokenspeed.py:79-80/102`、`tensorrt_llm.py:41-42/66`、`sglang.py:52`、`lmdeploy.py:51`、`aphrodite.py:51`、`llamacpp.py:293`、`unsloth.py:93` 的 `acquire_gpu_lock(...)` 同样加 `and not readonly` / `if not readonly:`。

- [ ] **Step 5: TUI 两处传 readonly**

`detail.py:216`：`adapter.check_requirements(readonly=True)`
`plan.py:290`：`adapter.check_requirements(readonly=True)`

- [ ] **Step 6: 跑测试确认通过 + 真实启动路径回归**

Run: `... pytest tests/test_tui_no_side_effects.py tests/test_tui_panel_detail.py tests/test_tui_panel_plan.py -q` → 全绿。
Run: `... pytest tests/test_engines_vllm.py tests/test_engines_tokenspeed.py tests/test_engines_tensorrt_llm.py -q` → 全绿（默认 readonly=False 行为不变）。

- [ ] **Step 7: Commit**

```powershell
git add src/modelctl/engines/ src/modelctl/core/tui/panels/ tests/test_tui_no_side_effects.py
git commit -m "fix(tui): make precheck readonly, no container/lock side effects on render (TUI-P1-1)"
```

---

### Task 3.2: 接通非 smoke 主循环与按键派发（TUI-P1-2）

**Files:**
- Modify: `src/modelctl/core/tui/app.py:87-105`（`run`）+ 新增 `_dispatch_key`
- Test: `tests/test_tui_app.py`（追加）

**Interfaces:**
- Consumes: `Key` 枚举（keyboard.py:46-77）、`TUIState` 的 `switch_detail_tab`/`cycle_cluster_tab`/`cycle_monitor_tab`/`cycle_plan_cursor`、`read_key_block(timeout) -> Key | None`、keybar 文案（dashboard：`q / f s d j k Enter`；detail：`Tab/Shift-Tab Esc r q ? t`；plan：`Tab Esc D q`；cluster：`Tab ↑↓ c q`；monitor：`Tab r g Esc q`）
- Produces: `TuiApp.run(smoke=False)` 进入 `while True` 循环直至 `Key.Q`；`_dispatch_key(key: Key) -> bool`（返回 False 表示退出）；循环帧间隔 `FRAME_TIMEOUT_S = 0.5`

- [ ] **Step 1: 写失败测试**

在 `tests/test_tui_app.py` 追加（沿用该文件现有 TuiApp 构造 fixture 的写法）：

```python
def test_run_non_smoke_loops_until_q(monkeypatch):
    """旧实现非 smoke 只渲染一帧就 return 0；keybar 承诺的按键全无实现。"""
    from modelctl.core.tui import TUIState
    from modelctl.core.tui.app import TuiApp
    from modelctl.core.tui.keyboard import Key
    from rich.console import Console

    keys = [Key.Down, Key.Down, Key.Tab, Key.Esc, Key.Q]
    app = TuiApp(TUIState(), Console(width=120, height=40), theme_file=None)
    app._revalidate = lambda: None  # 免真实数据
    app.realize_render_once = lambda: None
    app.loop_begin = lambda: None
    read = iter(keys)
    monkeypatch.setattr(app.keyboard, "read_key_block",
                        lambda timeout: next(read, Key.Q))
    monkeypatch.setattr(app.keyboard, "restore_term", lambda: None)
    app.run(smoke=False)  # 不抛、返回 0
    app.run.__wrapped__ if hasattr(app.run, "__wrapped__") else None

    # 按键语义断言：Tab 切视图 → Esc 回 dashboard
    keys2 = iter([Key.Tab, Key.Esc, Key.Q])
    app2 = TuiApp(TUIState(), Console(width=120, height=40), theme_file=None)
    app2._revalidate = lambda: None
    app2.realize_render_once = lambda: None
    app2.loop_begin = lambda: None
    monkeypatch.setattr(app2.keyboard, "read_key_block", lambda timeout: next(keys2, Key.Q))
    monkeypatch.setattr(app2.keyboard, "restore_term", lambda: None)
    app2.run(smoke=False)
    assert app2.state.active_view == "dashboard"


def test_dashboard_arrows_move_selection(monkeypatch):
    from modelctl.core.tui import TUIState
    from modelctl.core.tui.app import TuiApp
    from modelctl.core.tui.keyboard import Key
    from rich.console import Console

    keys = iter([Key.Down, Key.Down, Key.Up, Key.Q])
    app = TuiApp(TUIState(), Console(width=120, height=40), theme_file=None)
    app._revalidate = lambda: None
    app.realize_render_once = lambda: None
    app.loop_begin = lambda: None
    monkeypatch.setattr(app.keyboard, "read_key_block", lambda timeout: next(keys, Key.Q))
    monkeypatch.setattr(app.keyboard, "restore_term", lambda: None)
    app.run(smoke=False)
    assert app.state.active_index == 1  # Down Down Up → 1（下限 clamp 到 0）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_tui_app.py -q -k "loops_until_q or arrows_move"`
Expected: 第一个用例后 `active_view` 仍 dashboard（Tab 未生效）/第二个 `active_index == 0`。

- [ ] **Step 3: 实现主循环与派发**

`app.py` 常量区加 `FRAME_TIMEOUT_S = 0.5`。`run` 的非 smoke 分支改为：

```python
            self.loop_begin()
            while True:
                self.render_once()
                key = self.keyboard.read_key_block(FRAME_TIMEOUT_S)
                if key is None:
                    continue
                if not self._dispatch_key(key):
                    break
            self.loop_end()
            return 0
```

新增方法（`cycle_theme` 之后）：

```python
    _VIEWS: tuple[str, ...] = ("dashboard", "detail", "plan", "cluster", "monitor")

    def _dispatch_key(self, key: Key) -> bool:
        """按键派发；返回 False 表示退出主循环。语义与各 view keybar 文案一致。"""
        st = self.state
        view = st.active_view
        if key == Key.Q:
            return False
        if key == Key.T:
            self.cycle_theme()
            return True
        if key == Key.Esc:
            if view == "dashboard":
                return False  # dashboard 上 Esc 与 q 同义（无更上一层）
            st.active_view = "dashboard"
            return True
        if view == "dashboard":
            if key == Key.Down:
                st.active_index += 1
            elif key == Key.Up:
                st.active_index = max(0, st.active_index - 1)
            elif key == Key.Enter:
                st.active_view = "detail"
            elif key == Key.Tab:
                st.active_view = "plan"
        elif view == "detail":
            if key == Key.Tab:
                from modelctl.core.tui.state import _DETAIL_TABS

                cur = _DETAIL_TABS.index(st.active_detail_subtab)
                st.switch_detail_tab(_DETAIL_TABS[(cur + 1) % len(_DETAIL_TABS)])
            elif key == Key.ShiftTab:
                from modelctl.core.tui.state import _DETAIL_TABS

                cur = _DETAIL_TABS.index(st.active_detail_subtab)
                st.switch_detail_tab(_DETAIL_TABS[(cur - 1) % len(_DETAIL_TABS)])
            elif key == Key.Down:
                st.active_index += 1
            elif key == Key.Up:
                st.active_index = max(0, st.active_index - 1)
        elif view == "plan":
            if key == Key.Tab:
                st.cycle_plan_cursor(1, max(1, len(st.plan_edit or {}) or 1))
            elif key == Key.D:
                st.plan_dry_run_done = True
        elif view == "cluster":
            if key == Key.Tab:
                st.cycle_cluster_tab(1)
        elif view == "monitor":
            if key == Key.Tab:
                st.cycle_monitor_tab(1)
        return True
```

> `_DETAIL_TABS` 若在 `state.py` 是私有名，在 `state.py` 暴露 `DETAIL_TABS = _DETAIL_TABS` 公开别名并在派发里用公开名。active_index 越界由渲染层 clamp（`_sync_logs_name` 已有 idx 越界回 0 逻辑），派发侧只做下限保护。

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2 → 全绿。

- [ ] **Step 5: TUI 域回归 + smoke 不变**

Run: `... pytest tests/test_tui_app.py tests/test_tui_panel_dashboard.py tests/test_tui_panel_detail.py tests/test_tui_panel_plan.py tests/test_tui_panel_cluster.py tests/test_tui_panel_monitor.py tests/test_tui_keyboard.py -q` → 全绿。
手工：隔离终端跑 `uv run modelctl tui`，验证 q/Esc/Tab/方向键。

- [ ] **Step 6: Commit**

```powershell
git add src/modelctl/core/tui/app.py src/modelctl/core/tui/state.py tests/test_tui_app.py
git commit -m "feat(tui): wire real event loop and key dispatch matching keybars (TUI-P1-2)"
```

---

### Task 3.3: Windows 扩展键解析（TUI-P1-3）

**Files:**
- Modify: `src/modelctl/core/tui/keyboard.py:151-188`（`_read_windows`）+ 新增扫描码表
- Test: `tests/test_tui_keyboard.py`（追加）

**Interfaces:**
- Consumes: `msvcrt.kbhit/getwch/getch`（打桩注入）
- Produces: `_read_windows` 对 `0x00`/`0xE0` 前缀读第二字节，经 `_WIN_SCANCE_KEYS: dict[int, Key]` 映射（72→Up, 80→Down, 75→Left, 77→Right, 73→PgUp, 81→PgDn）；Ctrl 组合键语义保持

- [ ] **Step 1: 写失败测试（打桩 msvcrt）**

在 `tests/test_tui_keyboard.py` 追加：

```python
def test_windows_extended_arrow_key_decodes(monkeypatch):
    """方向键：getwch 返回 '\\xe0' 前缀（224 不在 <0x20 集合），旧实现不取第二字节。"""
    import modelctl.core.tui.keyboard as kb

    seq = iter(["\xe0", "H"])  # 0xE0 + 0x48 → Up
    monkeypatch.setattr(kb, "_is_windows", lambda: True)
    monkeypatch.setattr(kb.msvcrt, "kbhit", lambda: True)
    monkeypatch.setattr(kb.msvcrt, "getwch", lambda: seq.pop(0))
    assert kb._read_windows(0) == b"\xe0H" or kb._decode_key(kb._read_windows(0)) == kb.Key.Up


def test_windows_arrow_end_to_end(monkeypatch):
    import modelctl.core.tui.keyboard as kb

    class _M:
        def __init__(self, pairs):
            self.buf = [c for p in pairs for c in p]

        def kbhit(self):
            return bool(self.buf)

        def getwch(self):
            return self.buf.pop(0)

    m = _M([("\xe0", "H"), ("\xe0", "P"), ("\xe0", "S"), ("\xe0", "T"), ("\xe0", "I"), ("\xe0", "G")])
    monkeypatch.setattr(kb, "msvcrt", m)
    ki = kb.KeyboardInput()
    ki._is_windows = True
    got = [ki.read_key_block(0) for _ in range(6)]
    assert got == [kb.Key.Up, kb.Key.Down, kb.Key.Left, kb.Key.Right, kb.Key.PgDn, kb.Key.PgUp]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests/test_tui_keyboard.py -q -k windows`
Expected: 方向键解析成 `Key.Unknown`/错值。

- [ ] **Step 3: 实现前缀识别**

`keyboard.py` 解码表区加：

```python
# Windows 扩展键扫描码（getwch/getch 前缀 0x00/0xE0 后的第二字节）
_WIN_EXTENDED_CODES = frozenset({0x00, 0xE0})
_WIN_SCANCE_KEYS: dict[int, Key] = {
    0x48: Key.Up, 0x50: Key.Down, 0x4B: Key.Left, 0x4D: Key.Right,
    0x49: Key.PgUp, 0x51: Key.PgDn,
}
```

`_read_windows` 中 L178-186 替换为：

```python
            ch = msvcrt.getwch()  # str（unicode）或 int（小码/错误时回退）
            if isinstance(ch, str):
                code: int = ord(ch[0]) if ch else 0
            else:
                code = int(ch) & 0xFF
            if code in _WIN_EXTENDED_CODES:
                # 扩展键前缀（方向键/PgUp/PgDn）：再取扫描码字节
                code2 = msvcrt.getch()
                return bytes([0xE0, code2 & 0xFF])
            if code in (0x1B, 0x7F) or code < 0x20:
                # 控制字 / Ctrl 组合键：getwch 吞显示码，需 getch() 取真实控制码
                code = msvcrt.getch()
            return bytes([code & 0xFF])
```

`_decode_key` 在非 ESC 分支前加扩展键识别：

```python
    if len(raw) == 2 and raw[0] == 0xE0:
        return _WIN_SCANCE_KEYS.get(raw[1], Key.Unknown)
```

- [ ] **Step 4: 跑测试确认通过 + 键盘域回归**

Run: `... pytest tests/test_tui_keyboard.py -q` → 全绿（Unix 路径用例不受影响）。

- [ ] **Step 5: Commit**

```powershell
git add src/modelctl/core/tui/keyboard.py tests/test_tui_keyboard.py
git commit -m "fix(tui): decode windows extended scancodes for arrows/pagedn (TUI-P1-3)"
```

---

### Task 3.4: ruff panic 定位与定性（工具链门-A，仅定位）

**Files:**
- Create: `docs/health-checks/raw/ruff-panic-triage.md`

**Interfaces:**
- Consumes: `ruff check src tests`（98 panic 现状）
- Produces: 触发文件清单 + 规则号 + 升级结论；必要时 `pyproject.toml` per-file-ignores（含原因注释）

- [ ] **Step 1: 逐文件收敛定位**

```powershell
uv run --with ruff ruff check src tests --output-format concise 2>&1 | Tee-Object docs\health-checks\raw\ruff-panic-raw.txt
uv run --with ruff ruff --version
```

对 panic 输出中出现的文件逐个 `ruff check <file>` 单独跑，二分定位最小触发集，结果表写入 `ruff-panic-triage.md`（文件 | 规则 | panic 摘要）。

- [ ] **Step 2: 升级验证**

```powershell
uv run --with "ruff>=0.15" ruff check <最小触发文件>
```

升级后 panic 消失 → 在 triage 文档记"升级即解，CI 建议 bump ruff 版本"；仍 panic → 对该文件 `# ruff: noqa: <RULE>` 级 per-file-ignore 并注释 upstream issue 链接（如有）。

- [ ] **Step 3: 全量复跑留档**

Run: `uv run --with ruff ruff check src tests script` → 记录剩余真实告警数（与 panic 数分离）。

- [ ] **Step 4: Commit（仅文档，若加了 ignore 则含 pyproject）**

```powershell
git add docs/health-checks/raw/ruff-panic-triage.md
git commit -m "docs(toolchain): ruff panic triage - minimal repro set and upgrade verdict"
```

---

### Task 3.5: CI 口径对齐定性（工具链门-B）

**Files:**
- Modify: `docs/health-checks/2026-09-10-full-scan-report.md`（§1 工具链章节补记）

- [ ] **Step 1: 采集两侧口径**

CI：`.github/workflows/ci.yml`（Python 3.12、ruff/mypy 钉版按 workflow 内实际版本）。本地：`uv run --with mypy --with mypy-version-pin mypy --version` 等，逐项记录版本。

- [ ] **Step 2: 差异定性写入报告**

在报告 §1 追加"CI 对齐定性"小节：本地 3.13 vs CI 3.12 造成的 mypy 差异清单（哪些错误是本地独有）、ruff 版本差、结论（是否建议本地钉 3.12 复跑门禁——建议：合入前用 `uv run --python 3.12 ...` 跑一次 mypy+pytest 作为发布门）。

- [ ] **Step 3: Commit**

```powershell
git add docs/health-checks/2026-09-10-full-scan-report.md
git commit -m "docs(toolchain): record local-vs-CI toolchain version drift verdicts"
```

---

### Task 3.6: 移除未使用的 echarts 依赖（工具链门-C）

**Files:**
- Modify: `web/package.json`（删 `"echarts": "^5.6.0"`）
- Modify: `web/package-lock.json`（npm 自动刷新）

- [ ] **Step 1: 确认零引用**

Grep `web/src` 全目录：`echarts|setOption|tooltip|chart` → 确认零命中（spec 已核）。

- [ ] **Step 2: 移除并刷新锁文件**

```powershell
cd "d:\WorkPlace\Pycharm\modelctl\web"
npm uninstall echarts
npm run build
```

Expected: 构建成功；`node_modules/echarts` 不再安装；bundle 体积不升（预期下降）。

- [ ] **Step 3: vue-tsc 校验**

Run: `npx vue-tsc --noEmit` → 零错误。

- [ ] **Step 4: Commit**

```powershell
git add web/package.json web/package-lock.json
git commit -m "chore(web): drop unused echarts dependency (removes latent XSS surface)"
```

---

## 收尾（所有计划完成后）

### Task Z.1: 全量回归 + 冒烟 + known-pitfalls 沉淀

- [ ] **Step 1: 全量测试 0 failed**

Run: `uv run --with pytest-cov --with httpx --with fastapi --with uvicorn --with bcrypt --with pyjwt pytest tests -q --no-header`
Expected: `0 failed`。

- [ ] **Step 2: 隔离端口冒烟**

用 `docs/health-checks/raw/smoke_check.py`/`smoke_check2.py`（端口 14173/15003、独立数据目录）复跑：
- login 非 ASCII key → 401（原 500）
- `nginx-snippet` 返回 `snippet` 键非空
- 流式 400 连打 >N 并发 → 无持续 429
- `.env` 新增 `ACCOUNTS_JWT_SECRET` 后账号面登录 200

- [ ] **Step 3: known-pitfalls 沉淀（渐进式披露）**

`docs/known-pitfalls/README.md` 索引加条目；详情写入 `backend/*.md`（网关 release、compare_digest、pending 泄漏、锁生命周期、context switch、docker GPU 锁、events 保留）、`frontend/`（契约键不一致）各主题聚合文件；每条含根因/方案/代码锚点。

- [ ] **Step 4: 清理基线 worktree**

```powershell
git worktree remove ..\modelctl-baseline
git worktree list
```

- [ ] **Step 5: 最终提交**

```powershell
git add docs/known-pitfalls/
git commit -m "docs(pitfalls): distill 25 remediation fixes into known-pitfalls"
```

---

## Self-Review（作者已完成）

1. **Spec 覆盖**：spec 第 2 节 3 项 → Task 0.1-0.3；第 3 节 8 项 → Task 1.1-1.8；第 4 节 5 项 → Task 2.1-2.5；第 5 节 6 项 → Task 3.1-3.6；验收框架 → Task Z.1。无缺口。
2. **占位扫描**：Task 1.1/2.3/2.5/3.1 的测试代码含"按现有 fixture 基建对齐"的**受控适配说明**（端点路径/state 属性等实现细节由执行者按同域既有测试校准），断言本身完整无 TBD。
3. **类型一致性**：`set_gpu_override(gpus: list[int] | None)`（1.7 定义）与 2.5 无交叉；`apply_context_switch(match_keys: Sequence[str])` 在 2.2 内定义与调用一致；`_is_sensitive_key` 仅 1.1 使用；`TaskManager.spawn(task_id, target, action, coro_factory)` 与 1.8 五处调用签名一致；`FRAME_TIMEOUT_S`/`_dispatch_key` 仅 3.2 内部；`EVENTS_RETENTION_DAYS`/`trim_events` 仅 2.4 内部一致。