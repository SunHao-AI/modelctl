# 环境页 Docker 旁路指引 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** WebUI 环境页为当前平台不支持建 venv 的托管引擎（Windows 上的 vllm/sglang/…）提供可操作的 Docker 镜像旁路指引：常驻指引区块 + Docker 环境诊断 + Setup 按钮禁用与失败引导。

**Architecture:** 后端在 `envs.py` 落地 `DOCKER_CAPABLE_ENGINES` 作为单一事实来源；`admin_envs.py` 扩展 `GET /envs`（targets 增平台/docker 支持字段 + 顶层 `docker_env`/`docker_bypass`）并新增只读诊断端点 `GET /envs/docker/diagnose`（透传现成 `docker_setup.diagnose()` / `render_instructions()`）。前端 EnvsView 新增"Docker 旁路"区块（样式仿既有"非托管引擎"区块），TaskButton 增加外部禁用 props。

**Tech Stack:** FastAPI + pytest（`TestClient`）；Vue 3 `<script setup>` + TypeScript + Pinia（项目无前端单测框架，前端门禁为 `npm run build`）。

**Spec:** `docs/superpowers/specs/2026-09-07-envs-docker-bypass-guide-design.md`

## Global Constraints

- 回复与代码注释用中文；PowerShell 多命令用 `;` 分隔（不支持 `&&`）。
- UI 显示均为虚拟 ID；时间格式 `YYYY-MM-DD HH:mm:ss`（本计划无新增时间字段）。
- **严禁任何 DDL**；本功能纯只读，**不得新增任何写操作**（无 yaml 写入、不执行 docker 命令）。
- Python 测试命令固定（`.venvs\gateway` 是过期复制安装，必须前置 PYTHONPATH）：
  `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; d:\WorkPlace\Pycharm\modelctl\.venvs\gateway\Scripts\python.exe -m pytest <target> -v`
- 前端门禁固定：`cd d:\WorkPlace\Pycharm\modelctl\web; npm run build`（要求 exit 0）。
- 同一文件的多处修改必须**串行**编辑（禁止并行编辑同文件，历史上丢过改动）。
- 每个 Task 结束 commit 一次；提交信息沿用仓库 conventional commits（`feat`/`test`/`fix`/`docs` + scope）。
- 已知环境噪音：全量 pytest 有 timezone / engine-docker 类既有失败（非本分支回归）；只要求本计划新增及直接涉及的测试通过。
- 新建 Python 测试文件必须带仓库标准文件头（参照 `tests/test_admin_tasks.py` 前 10 行）。

---

### Task 1: `envs.py` 新增 `DOCKER_CAPABLE_ENGINES` + 适配器一致性锚定

**Files:**
- Modify: `src/modelctl/core/envs.py`（`MANAGED_ENGINES` 定义之后，L16 附近区块内）
- Test: `tests/test_core_envs.py`（文件末尾追加，勿改既有用例）

**Interfaces:**
- Consumes: 无（纯新增常量）
- Produces: `modelctl.core.envs.DOCKER_CAPABLE_ENGINES: tuple[str, ...] = ("vllm", "tokenspeed", "tensorrt_llm")` —— Task 2 以 `t in envs.DOCKER_CAPABLE_ENGINES` 计算 `docker_supported`。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_core_envs.py` 末尾）

```python
def test_docker_capable_engines_matches_adapters(tmp_path):
    """DOCKER_CAPABLE_ENGINES 必须与适配器 _resolve_runtime 实现一致（防漂移锚点）。

    已支持三者：docker_image 非空 → ('docker', image)；
    未支持引擎：适配器类不得定义 _resolve_runtime（即无 docker 分支）。
    """
    import modelctl.core.compat_rules  # noqa: F401 —— 导入即注册
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.envs import DOCKER_CAPABLE_ENGINES, MANAGED_ENGINES
    from modelctl.core.profile import load_profile
    from modelctl.engines import get_adapter

    assert set(DOCKER_CAPABLE_ENGINES) == {"vllm", "tokenspeed", "tensorrt_llm"}
    assert set(DOCKER_CAPABLE_ENGINES) <= set(MANAGED_ENGINES)

    caps = Capabilities(gpu_count=1, compute_capability="8.9", binaries={})
    for i, engine in enumerate(DOCKER_CAPABLE_ENGINES):
        f = tmp_path / f"m{i}.yaml"
        f.write_text(
            f"name: m{i}\nengine: {engine}\nport: {8000 + i}\n"
            f"{engine}:\n  model: /models/x\n  docker_image: img:tag\n",
            encoding="utf-8",
        )
        adapter = get_adapter(engine)(load_profile(f"m{i}", tmp_path), caps)
        assert adapter._resolve_runtime() == ("docker", "img:tag"), engine

    # 未支持引擎：注册表里的适配器类不应定义 _resolve_runtime（当前仅三适配器定义）
    for engine in set(MANAGED_ENGINES) - set(DOCKER_CAPABLE_ENGINES):
        assert not hasattr(get_adapter(engine), "_resolve_runtime"), engine
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; d:\WorkPlace\Pycharm\modelctl\.venvs\gateway\Scripts\python.exe -m pytest tests/test_core_envs.py::test_docker_capable_engines_matches_adapters -v`
Expected: FAIL —— `ImportError: cannot import name 'DOCKER_CAPABLE_ENGINES'`

- [ ] **Step 3: 最小实现**（`src/modelctl/core/envs.py`，紧跟 `MANAGED_ENGINES = (...)` 行之后插入）

```python
# 已实现 docker_image 分支的引擎（engines/<name>.py 的 _resolve_runtime 把
# cfg.docker_image 非空解析为 ('docker', image)）；其余托管引擎仅 venv，UI 不得渲染 docker 指引。
DOCKER_CAPABLE_ENGINES = ("vllm", "tokenspeed", "tensorrt_llm")
```

- [ ] **Step 4: 运行确认通过**（连带既有平台用例回归）

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; d:\WorkPlace\Pycharm\modelctl\.venvs\gateway\Scripts\python.exe -m pytest tests/test_core_envs.py -v`
Expected: 全部 PASS（含既有 `test_platform_supports_*` / `test_platform_limitation_message_*`）

- [ ] **Step 5: Commit**

```powershell
git add src/modelctl/core/envs.py tests/test_core_envs.py ; git commit -m "feat(envs): add DOCKER_CAPABLE_ENGINES with adapter-consistency anchor test"
```

---

### Task 2: `admin_envs.py` 扩展 GET /envs + Docker 诊断端点

**Files:**
- Modify: `src/modelctl/core/webui/admin_envs.py`（指引常量表插在 `UNMANAGED_INSTALL_HINTS` 之后；`list_envs` 内 `out.append(...)` 与 `return` 改造；新端点插在 `list_envs` 之后、`setup_env` 之前）
- Test: `tests/test_webui_admin_envs.py`（新建）

**Interfaces:**
- Consumes: `envs.platform_supports(t) -> bool`、`envs.MANAGED_ENGINES`、`envs.DOCKER_CAPABLE_ENGINES`（Task 1）、`docker_setup.path_level_missing() -> list[str]`、`docker_setup.MSG_GUIDE: str`、`docker_setup.diagnose() -> list[Check]`（frozen dataclass，字段 `key/label/ok/detail`）、`docker_setup.render_instructions() -> str`。
- Produces（Task 3/4 逐字依赖的响应契约）:
  - `GET /admin/api/envs` → `{targets: [{name, installed, detail, platform_supported: bool, docker_supported: bool}], unmanaged: [...], docker_env: {ready: bool, missing: string[], guide: string}, docker_bypass: [{name, docker_supported, image_example?, yaml_field_path?, example_yaml?, steps?: string[3], note?}]}`；`docker_bypass` 覆盖 6 个托管引擎（不含 gateway）。
  - `GET /admin/api/envs/docker/diagnose` → `{checks: [{key, label, ok, detail}], instructions: string}`；未认证 401。

- [ ] **Step 1: 写失败测试**（新建 `tests/test_webui_admin_envs.py`）

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_webui_admin_envs.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 14:30
# @Desc   : 环境端点扩展字段与 Docker 旁路指引/诊断测试
# ===============================================================================

"""admin_envs：GET /envs 扩展字段 + GET /envs/docker/diagnose 测试。"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_envs"


@pytest.fixture()
def admin_client(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    logger.remove()
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c


def _get(client: TestClient, path: str):
    return client.get(path, headers={"Authorization": f"Bearer {KEY}"})


def test_list_envs_extended_fields_non_linux(admin_client, monkeypatch):
    """非 Linux 态：托管引擎 platform_supported=False，gateway 恒 True。"""
    monkeypatch.setattr("modelctl.core.envs._is_linux", lambda: False)
    r = _get(admin_client, "/admin/api/envs")
    assert r.status_code == 200
    by_name = {t["name"]: t for t in r.json()["targets"]}
    assert {"vllm", "sglang", "gateway"} <= set(by_name)
    for name, t in by_name.items():
        assert {"name", "installed", "detail", "platform_supported", "docker_supported"} <= set(t)
        assert t["platform_supported"] is (name == "gateway"), name
    assert by_name["vllm"]["docker_supported"] is True
    assert by_name["sglang"]["docker_supported"] is False
    assert by_name["gateway"]["docker_supported"] is False


def test_list_envs_platform_linux(admin_client, monkeypatch):
    monkeypatch.setattr("modelctl.core.envs._is_linux", lambda: True)
    body = _get(admin_client, "/admin/api/envs").json()
    assert all(t["platform_supported"] for t in body["targets"])


def test_list_envs_docker_env_and_bypass(admin_client):
    body = _get(admin_client, "/admin/api/envs").json()
    de = body["docker_env"]
    assert isinstance(de["ready"], bool)
    assert isinstance(de["missing"], list)
    assert de["guide"]
    assert de["ready"] == (len(de["missing"]) == 0)

    bypass = {e["name"]: e for e in body["docker_bypass"]}
    assert set(bypass) == {"vllm", "sglang", "aphrodite", "lmdeploy", "tokenspeed", "tensorrt_llm"}
    v = bypass["vllm"]
    assert v["docker_supported"] is True
    assert v["image_example"].startswith("vllm/vllm-openai:")
    assert v["yaml_field_path"] == "vllm.docker_image"
    assert v["example_yaml"] == "models/vllm/qwen3.8-flash-next.yaml"
    assert len(v["steps"]) == 3
    assert v["steps"][0].startswith("编辑 models/vllm/")
    assert bypass["tokenspeed"]["image_example"] == "lightseekorg/tokenspeed:latest"
    assert bypass["tensorrt_llm"]["image_example"].startswith("nvcr.io/nvidia/tensorrt-llm:")
    for name in ("sglang", "aphrodite", "lmdeploy"):
        assert bypass[name]["docker_supported"] is False
        assert bypass[name]["note"]
        assert "steps" not in bypass[name]  # 不造假指引


def test_docker_diagnose_shape(admin_client, monkeypatch):
    import modelctl.core.docker_setup as ds

    class FakeCheck:
        key = "docker_cli"
        label = "docker CLI（PATH）"
        ok = False
        detail = "未安装"

    monkeypatch.setattr(ds, "diagnose", lambda: [FakeCheck()])
    monkeypatch.setattr(ds, "render_instructions", lambda *a, **k: "# script")
    r = _get(admin_client, "/admin/api/envs/docker/diagnose")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"] == [
        {"key": "docker_cli", "label": "docker CLI（PATH）", "ok": False, "detail": "未安装"}
    ]
    assert body["instructions"] == "# script"


def test_docker_diagnose_requires_auth(admin_client):
    assert admin_client.get("/admin/api/envs/docker/diagnose").status_code == 401
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; d:\WorkPlace\Pycharm\modelctl\.venvs\gateway\Scripts\python.exe -m pytest tests/test_webui_admin_envs.py -v`
Expected: FAIL —— 扩展字段 `KeyError: 'platform_supported'` / diagnose 返回 404

- [ ] **Step 3: 实现——指引常量表**（`admin_envs.py`，插在 `UNMANAGED_INSTALL_HINTS = {...}` 之后）

```python
# —— Docker 旁路指引：镜像名/示例 yaml 事实与 models/<engine>/*.yaml 保持同步；
#    支持矩阵的事实来源是 envs.DOCKER_CAPABLE_ENGINES（有测试锚定），勿在此另行硬编码集合。
_DOCKER_STEP_PREP = (
    "部署机准备 Docker + NVIDIA Container Toolkit（点上方「完整诊断」查看检查项与可复制安装脚本，"
    "或执行 CLI：modelctl env setup docker --run）"
)
_DOCKER_STEP_START = (
    "modelctl stop <模型> && modelctl start <模型>；docker 路径要求 model 为本地已有目录"
    "（HuggingFace id 需先跑一次触发下载落地）"
)
DOCKER_BYPASS_GUIDES: dict[str, dict] = {
    "vllm": {
        "image_example": "vllm/vllm-openai:<tag>",
        "yaml_field_path": "vllm.docker_image",
        "example_yaml": "models/vllm/qwen3.8-flash-next.yaml",
    },
    "tokenspeed": {
        "image_example": "lightseekorg/tokenspeed:latest",
        "yaml_field_path": "tokenspeed.docker_image",
        "example_yaml": "models/tokenspeed/qwen3.5-397b.yaml",
    },
    "tensorrt_llm": {
        "image_example": "nvcr.io/nvidia/tensorrt-llm:<tag>",
        "yaml_field_path": "tensorrt_llm.docker_image",
        "example_yaml": "models/tensorrt_llm/qwen3.8.yaml",
    },
}
# 未实现 docker 分支的引擎统一说明（不造假指引；缺口记录见 docs/TODO.md 2.2）
DOCKER_UNSUPPORTED_NOTE = (
    "modelctl 的该引擎适配器暂不支持 docker 运行时，官方镜像无法经 modelctl 启动；"
    "建议改用已支持的引擎（vllm / tokenspeed / tensorrt_llm），或在 Linux 部署机上建托管 venv"
)
```

- [ ] **Step 4: 实现——`list_envs` 扩展**（同文件，串行两处编辑）

4a. 把 `for t in targets:` 循环内的 `out.append({"name": t, "installed": installed, "detail": detail})` 替换为：

```python
        out.append(
            {
                "name": t,
                "installed": installed,
                "detail": detail,
                "platform_supported": envs.platform_supports(t),
                "docker_supported": t in envs.DOCKER_CAPABLE_ENGINES,
            }
        )
```

4b. 把 `list_envs` 末尾从 `unmanaged = await asyncio.to_thread(_unmanaged_targets)` 到 `return {"targets": out, "unmanaged": unmanaged}` 为止的整段，替换为：

```python
    unmanaged = await asyncio.to_thread(_unmanaged_targets)

    # Docker 环境就绪探测：纯 shutil.which，绝不落子进程，不拖慢列表页加载
    from modelctl.core import docker_setup

    missing = docker_setup.path_level_missing()
    docker_env = {"ready": not missing, "missing": missing, "guide": docker_setup.MSG_GUIDE}

    # Docker 旁路指引：已支持引擎给镜像事实 + 三步；其余仅 note（gateway 不适用旁路，天然排除）
    bypass: list[dict] = []
    for name in envs.MANAGED_ENGINES:
        guide = DOCKER_BYPASS_GUIDES.get(name)
        if guide:
            bypass.append(
                {
                    "name": name,
                    "docker_supported": True,
                    "image_example": guide["image_example"],
                    "yaml_field_path": guide["yaml_field_path"],
                    "example_yaml": guide["example_yaml"],
                    "steps": [
                        f"编辑 models/{name}/<模型>.yaml，在 {name}: 块下加一行 "
                        f"docker_image: {guide['image_example']}",
                        _DOCKER_STEP_PREP,
                        _DOCKER_STEP_START,
                    ],
                }
            )
        else:
            bypass.append({"name": name, "docker_supported": False, "note": DOCKER_UNSUPPORTED_NOTE})

    return {"targets": out, "unmanaged": unmanaged, "docker_env": docker_env, "docker_bypass": bypass}
```

- [ ] **Step 5: 实现——诊断端点**（同文件，插在 `list_envs` 函数之后、`setup_env` 之前）

```python
@router.get("/docker/diagnose")
async def docker_diagnose(_: None = Depends(require_auth)):
    """GET /admin/api/envs/docker/diagnose — Docker 环境完整诊断（只读，按需触发）。

    透传 ``docker_setup.diagnose()``（含 ``docker info`` 子进程，内建 15s 超时，
    故走 to_thread 且仅由前端按钮按需调用）与 ``render_instructions()``（可复制的
    root 安装脚本）。本端点绝不执行任何安装动作——实际安装仍只走既有 CLI 通道
    ``modelctl env setup docker --run``。
    """
    from modelctl.core import docker_setup

    try:
        checks = await asyncio.to_thread(docker_setup.diagnose)
        return {
            "checks": [
                {"key": c.key, "label": c.label, "ok": c.ok, "detail": c.detail} for c in checks
            ],
            "instructions": docker_setup.render_instructions(),
        }
    except Exception as exc:  # noqa: BLE001 — 诊断失败统一 500（与同文件 remove_env 惯例一致）
        logger.exception(f"Docker 诊断异常: {exc}")
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal", "message": f"诊断失败: {exc}"}},
        )
```

- [ ] **Step 6: 运行确认通过**（含既有 webui 冒烟回归）

Run: `$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; d:\WorkPlace\Pycharm\modelctl\.venvs\gateway\Scripts\python.exe -m pytest tests/test_webui_admin_envs.py tests/test_webui_smoke.py tests/test_admin_tasks.py -v`
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```powershell
git add src/modelctl/core/webui/admin_envs.py tests/test_webui_admin_envs.py ; git commit -m "feat(webui): expose docker bypass guides, env readiness and diagnose endpoint"
```

---

### Task 3: 前端 API 类型 + TaskButton 外部禁用 props

**Files:**
- Modify: `web/src/api/types.ts`（`EnvTarget` 接口，约 L254-262）
- Modify: `web/src/api/envs.ts`（`envTargets` 返回类型 + 新增 `dockerDiagnose`）
- Modify: `web/src/components/common/TaskButton.vue`（props 定义、`onClick` 守卫、模板 `:disabled`/`:title`）

**Interfaces:**
- Consumes: Task 2 的响应字段名（`platform_supported` / `docker_supported` / `docker_env` / `docker_bypass` / `checks` / `instructions` 逐字一致）。
- Produces:
  - 类型 `DockerEnv`、`DockerBypassEntry`、`DockerDiagnose`；`envTargets()` 返回含 `docker_env?` / `docker_bypass?`；`dockerDiagnose(): Promise<DockerDiagnose>` —— Task 4 消费。
  - TaskButton 新 props `disabled?: boolean`、`disabledReason?: string` —— Task 4 消费。

- [ ] **Step 1: `types.ts` 替换 `EnvTarget` 并追加三接口**

```ts
/** 环境 target 信息（get /envs） */
export interface EnvTarget {
  /** 环境名（如 "vllm"、"gateway"） */
  name: string;
  /** 是否已安装 */
  installed: boolean;
  /** 描述文本（python 版本 / 包数量等） */
  detail: string;
  /** 当前运行平台是否支持建托管 venv（托管引擎仅 Linux；gateway 恒 true） */
  platform_supported: boolean;
  /** 适配器是否支持 docker_image 运行时（UI 据此决定能否走 Docker 旁路） */
  docker_supported: boolean;
}

/** Docker 环境就绪探测（PATH 级，无子进程） */
export interface DockerEnv {
  ready: boolean;
  /** 缺失项描述（docker CLI / toolkit），ready=true 时为空数组 */
  missing: string[];
  /** 缺失时的统一安装指引文案 */
  guide: string;
}

/** 单引擎 Docker 旁路指引（支持时带 steps，否则仅 note） */
export interface DockerBypassEntry {
  name: string;
  docker_supported: boolean;
  /** 官方镜像示例（如 vllm/vllm-openai:<tag>） */
  image_example?: string;
  /** yaml 字段路径（如 vllm.docker_image） */
  yaml_field_path?: string;
  /** 仓库内可直接参考的示例 yaml 路径 */
  example_yaml?: string;
  /** 三步操作指引 */
  steps?: string[];
  /** 不支持时的说明文案 */
  note?: string;
}

/** Docker 完整诊断（GET /envs/docker/diagnose） */
export interface DockerDiagnose {
  checks: Array<{ key: string; label: string; ok: boolean; detail: string }>;
  /** 可复制到部署机 root shell 的安装脚本 */
  instructions: string;
}
```

- [ ] **Step 2: `envs.ts` 扩展**（`envSetup` / `envRemove` 保持原样不动）

```ts
import client, { dataOf } from './client';
import type {
  ActionResponse,
  DockerBypassEntry,
  DockerDiagnose,
  DockerEnv,
  EnvTarget,
  TaskRef,
  UnmanagedTarget,
} from './types';

/** envTargets 响应（docker_* 为旁路指引与环境探测） */
export interface EnvTargetsResponse {
  targets: EnvTarget[];
  unmanaged?: UnmanagedTarget[];
  docker_env?: DockerEnv;
  docker_bypass?: DockerBypassEntry[];
}

/** 列出所有受管 venv target 及状态；unmanaged 为非托管引擎说明。 */
export function envTargets(): Promise<EnvTargetsResponse> {
  return dataOf<EnvTargetsResponse>(client.get('/envs'));
}

/** Docker 环境完整诊断（只读，按需触发；内含 15s 级子进程探测，勿在列表加载时调用）。 */
export function dockerDiagnose(): Promise<DockerDiagnose> {
  return dataOf<DockerDiagnose>(client.get('/envs/docker/diagnose'));
}
```

- [ ] **Step 3: TaskButton 外部禁用**（同文件串行三处编辑）

3a. `defineProps<{ ... }>()` 内、`taskTarget` 字段之前插入两个字段：

```ts
    /** 外部强制禁用（如当前平台不支持建托管 venv） */
    disabled?: boolean;
    /** 外部禁用原因（禁用态下作为按钮 title 展示） */
    disabledReason?: string;
```

3b. `onClick` 首行守卫替换：

```ts
  if (props.disabled || phase.value !== 'idle') return;
```

3c. 模板 `<button>` 上的 `:disabled` 与 `:title` 两行替换：

```
    :disabled="disabled || phase !== 'idle'"
    :title="disabled && disabledReason ? disabledReason : text"
```

- [ ] **Step 4: 构建门禁**

Run: `cd d:\WorkPlace\Pycharm\modelctl\web; npm run build`
Expected: exit 0（EnvsView 本步尚未消费新字段，不应报错）

- [ ] **Step 5: Commit**

```powershell
git add web/src/api/types.ts web/src/api/envs.ts web/src/components/common/TaskButton.vue ; git commit -m "feat(web): env api docker-bypass types and TaskButton external disabled props"
```

---

### Task 4: EnvsView Docker 旁路区块 + Setup 禁用 + 失败引导

**Files:**
- Modify: `web/src/views/EnvsView.vue`（script 扩展；表格 TaskButton 属性；新区块插在"受管目标"表格 `</section>` 与"非托管引擎"注释之间）

**Interfaces:**
- Consumes: Task 3 的 `dockerDiagnose()`、`DockerEnv`/`DockerBypassEntry`/`DockerDiagnose`/`EnvTarget` 类型、TaskButton `disabled`/`disabled-reason` props；后端平台限制失败文案含固定关键字"docker 镜像绕过"（`envs.py::platform_limitation_message`）。
- Produces: 终端 UI，无下游任务消费。

- [ ] **Step 1: script 扩展**（同文件串行编辑）

1a. import 两行替换为：

```ts
import { dockerDiagnose, envRemove, envSetup, envTargets } from '@/api/envs';
import type { DockerBypassEntry, DockerDiagnose, DockerEnv, EnvTarget, UnmanagedTarget } from '@/api/types';
```

1b. `const removeBusy = ref(false);` 之后追加状态：

```ts
/** Docker 环境就绪探测（随列表加载返回） */
const dockerEnv = ref<DockerEnv | null>(null);
/** 逐引擎 Docker 旁路指引 */
const dockerBypass = ref<DockerBypassEntry[]>([]);
/** 诊断面板：展开态 / 结果（懒加载一次）/ 错误 / 加载态 */
const diagOpen = ref(false);
const diagData = ref<DockerDiagnose | null>(null);
const diagErr = ref('');
const diagBusy = ref(false);
/** 复制反馈的当前 key（'inst' 或引擎名） */
const copiedKey = ref('');
```

1c. `load()` 内 `unmanaged.value = r.unmanaged ?? [];` 之后追加：

```ts
    dockerEnv.value = r.docker_env ?? null;
    dockerBypass.value = r.docker_bypass ?? [];
```

1d. `onMounted(load);` 之前追加三个函数：

```ts
/** Setup 失败提示：平台限制类错误附旁路区块引导语 */
function onSetupError(name: string, msg: string) {
  notice.value = `${name} setup 失败：${msg}`;
  if (msg.includes('docker 镜像绕过')) {
    notice.value += '——旁路步骤见下方「Docker 旁路」区块';
  }
}

/** 展开/收起完整诊断；首次展开懒加载一次 */
async function onDiagnose() {
  diagOpen.value = !diagOpen.value;
  if (!diagOpen.value || diagData.value || diagBusy.value) return;
  diagBusy.value = true;
  diagErr.value = '';
  try {
    diagData.value = await dockerDiagnose();
  } catch (err) {
    console.warn('dockerDiagnose 失败:', err);
    diagErr.value = (err as { message?: string })?.message || '诊断失败';
  } finally {
    diagBusy.value = false;
  }
}

/** 复制任意文本（ConfigView copySnippet 同款 1.5s 反馈） */
async function copyText(key: string, text: string) {
  try {
    await navigator.clipboard.writeText(text);
    copiedKey.value = key;
    setTimeout(() => (copiedKey.value = ''), 1500);
  } catch (err) {
    console.warn('复制失败:', err);
  }
}
```

- [ ] **Step 2: 表格 Setup 按钮禁用 + 失败回调接线**

把 `<TaskButton v-if="!t.installed" ... />` 整块替换为：

```vue
                <TaskButton
                  v-if="!t.installed"
                  label="Setup"
                  variant="primary"
                  :target="t.name"
                  :disabled="!t.platform_supported"
                  :disabled-reason="
                    t.platform_supported ? undefined : '托管 venv 仅支持 Linux 部署机，可走下方「Docker 旁路」区块'
                  "
                  :task-target="() => envSetup(t.name)"
                  @success="() => load()"
                  @error="(msg) => onSetupError(t.name, msg)"
                />
```

- [ ] **Step 3: 新增"Docker 旁路"区块**（插在受管目标表格 `</section>` 之后、`<!-- 非托管引擎说明…` 注释之前）

```vue
    <!-- Docker 旁路：托管 venv 仅支持 Linux，已支持引擎可改用官方 docker 镜像 -->
    <section v-if="dockerBypass.length" class="card space-y-3">
      <div class="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 class="text-sm font-medium text-slate-200">Docker 旁路</h2>
          <p class="mt-0.5 text-xs text-slate-500">
            托管 venv 仅支持 Linux；下列引擎可改用官方 docker 镜像绕过 venv（编辑模型 yaml 后仍由 modelctl 启停）
          </p>
        </div>
        <div class="flex items-center gap-2">
          <span
            class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs"
            :class="dockerEnv?.ready
              ? 'bg-emerald-600/15 text-emerald-300 border border-emerald-500/30'
              : 'bg-red-600/15 text-red-300 border border-red-500/30'"
            :title="dockerEnv && !dockerEnv.ready ? dockerEnv.missing.join('；') : undefined"
          >
            <span class="size-1.5 rounded-full" :class="dockerEnv?.ready ? 'bg-emerald-400' : 'bg-red-400'" />
            {{ dockerEnv?.ready ? 'Docker 环境就绪' : 'Docker 环境缺失' }}
          </span>
          <button class="btn-ghost !py-1 !px-2 text-xs" :disabled="diagBusy" @click="onDiagnose">
            {{ diagBusy ? '诊断中…' : diagOpen ? '收起诊断' : '完整诊断' }}
          </button>
        </div>
      </div>

      <p v-if="dockerEnv && !dockerEnv.ready" class="text-xs text-slate-500">{{ dockerEnv.guide }}</p>

      <!-- 完整诊断：首次展开懒加载一次（后端含子进程探测） -->
      <div v-if="diagOpen" class="space-y-1.5 border-t border-slate-800/40 pt-3">
        <p v-if="diagErr" class="text-xs text-red-400">{{ diagErr }}</p>
        <template v-else-if="diagData">
          <div v-for="c in diagData.checks" :key="c.key" class="flex flex-wrap items-center gap-x-2 text-xs">
            <span class="size-1.5 rounded-full" :class="c.ok ? 'bg-emerald-400' : 'bg-red-400'" />
            <span class="text-slate-300">{{ c.label }}</span>
            <span v-if="!c.ok" class="break-all text-slate-500">{{ c.detail }}</span>
          </div>
          <div class="mt-2">
            <div class="mb-1 flex items-center justify-between">
              <span class="text-xs text-slate-500">
                安装脚本（复制到部署机 root shell；WebUI 只展示不执行）
              </span>
              <button class="btn-ghost !py-1 !px-2 text-xs" @click="copyText('inst', diagData.instructions)">
                {{ copiedKey === 'inst' ? '已复制' : '复制脚本' }}
              </button>
            </div>
            <pre
              class="max-h-72 overflow-auto bg-[#0b1120] p-3 font-mono text-xs leading-6 whitespace-pre text-slate-300"
            >{{ diagData.instructions }}</pre>
          </div>
        </template>
        <p v-else class="text-xs text-slate-500">诊断中…</p>
      </div>

      <!-- 逐引擎指引 -->
      <div class="space-y-2">
        <div v-for="b in dockerBypass" :key="b.name" class="space-y-1.5 border-t border-slate-800/40 pt-2.5">
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
            <span class="font-mono text-slate-300">{{ b.name }}</span>
            <span
              class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5"
              :class="b.docker_supported
                ? 'bg-emerald-600/15 text-emerald-300 border border-emerald-500/30'
                : 'bg-slate-600/15 text-slate-400 border border-slate-500/30'"
            >
              <span class="size-1.5 rounded-full" :class="b.docker_supported ? 'bg-emerald-400' : 'bg-slate-500'" />
              {{ b.docker_supported ? '支持 docker 运行时' : '暂不支持 docker' }}
            </span>
          </div>
          <p v-if="!b.docker_supported" class="text-xs text-slate-500">{{ b.note }}</p>
          <template v-else>
            <div class="flex flex-wrap items-center gap-2 text-xs">
              <span class="text-slate-500">yaml 片段</span>
              <code class="break-all text-slate-300">{{ b.yaml_field_path }}: {{ b.image_example }}</code>
              <button class="btn-ghost !py-0.5 !px-2 text-xs" @click="copyText(b.name, `${b.yaml_field_path}: ${b.image_example}`)">
                {{ copiedKey === b.name ? '已复制' : '复制' }}
              </button>
              <span class="text-slate-600">·</span>
              <span class="break-all text-slate-500">示例 <code class="text-slate-400">{{ b.example_yaml }}</code></span>
            </div>
            <ol class="ml-4 list-decimal space-y-0.5 text-xs text-slate-400">
              <li v-for="(s, i) in b.steps" :key="i" class="break-all">{{ s }}</li>
            </ol>
          </template>
        </div>
      </div>
    </section>
```

- [ ] **Step 4: 构建门禁**

Run: `cd d:\WorkPlace\Pycharm\modelctl\web; npm run build`
Expected: exit 0

- [ ] **Step 5: Commit**

```powershell
git add web/src/views/EnvsView.vue ; git commit -m "feat(web): envs page docker-bypass section with diagnose and disabled setup guidance"
```

---

### Task 5: 端到端验收 + known-pitfalls 沉淀

**Files:**
- Create: `docs/known-pitfalls/backend/docker-bypass-guide-single-source.md`（详情层）
- Modify: `docs/known-pitfalls/README.md`（摘要索引追加一行）

**Interfaces:**
- Consumes: Task 1-4 全部产物；运行中的 webui（`http://127.0.0.1:4173`）。
- Produces: 验收结论 + 知识库条目。

- [ ] **Step 1: 重启 webui 加载新后端代码**

```powershell
$env:PYTHONPATH="d:\WorkPlace\Pycharm\modelctl\src"; d:\WorkPlace\Pycharm\modelctl\.venvs\gateway\Scripts\python.exe -m modelctl.cli webui restart
```

Run: `Invoke-RestMethod -Uri "http://127.0.0.1:4173/admin/api/health" -TimeoutSec 5`
Expected: `{"ok":true,...}`

- [ ] **Step 2: HTTP 契约验收（带 Bearer）**

密钥从 `.env` 的 `API_KEY` 读取，**不得把明文密钥写入任何提交文件**：

```powershell
$tok = (Select-String -Path "d:\WorkPlace\Pycharm\modelctl\.env" -Pattern '^API_KEY=(.+)$').Matches[0].Groups[1].Value.Trim()
$h = @{Authorization="Bearer $tok"}
(Invoke-RestMethod -Uri "http://127.0.0.1:4173/admin/api/envs" -Headers $h).docker_env | ConvertTo-Json -Compress
(Invoke-RestMethod -Uri "http://127.0.0.1:4173/admin/api/envs" -Headers $h).docker_bypass | Select-Object name,docker_supported | ConvertTo-Json -Compress
(Invoke-RestMethod -Uri "http://127.0.0.1:4173/admin/api/envs/docker/diagnose" -Headers $h).checks | ConvertTo-Json -Compress
```

Expected:
- `docker_env`：Windows 本机 `ready:false`，`missing` 含 docker CLI/toolkit 缺失项，`guide` 非空。
- `docker_bypass`：6 条；vllm/tokenspeed/tensorrt_llm 为 `true`，sglang/aphrodite/lmdeploy 为 `false`。
- `diagnose.checks`：4 项（`docker_cli` / `docker_daemon` / `nvidia_toolkit` / `nvidia_runtime`）。

- [ ] **Step 3: 浏览器 E2E 验收**（TRAE-browseruse；用已登录态，若需登录则密钥取自 `.env` 的 `API_KEY`）

导航 `http://127.0.0.1:4173/envs`，逐项核验并在报告中给出快照证据：
1. 托管表格中未安装的引擎（vllm/sglang/…）Setup 按钮**呈禁用态**，hover `title` 显示"托管 venv 仅支持 Linux 部署机，可走下方「Docker 旁路」区块"；gateway 行不受影响（已安装 → 显示"移除"）。
2. 出现"Docker 旁路"区块：徽标显示"Docker 环境缺失"（Windows 本机），下方展示 `guide` 文案。
3. 点"完整诊断"→ 展开 4 项检查（本机多为红点）+ 安装脚本 `<pre>`，点"复制脚本"出现"已复制"反馈。
4. 引擎列表：vllm/tokenspeed/tensorrt_llm 显示"支持 docker 运行时" + yaml 片段 + 复制按钮 + 3 步 `steps`；sglang/aphrodite/lmdeploy 显示"暂不支持 docker" + note，**无 steps**。
5. 若第 1 项按钮因故仍可点击（如平台判定异常），点击后失败 notice 应含"旁路步骤见下方「Docker 旁路」区块"。

- [ ] **Step 4: 回归确认既有环境页能力未破坏**

- 已安装的 gateway 行仍显示"移除"，点击移除弹窗文案与行为不变。
- "非托管引擎"区块仍正常展示 ollama/unsloth/llamacpp 与安装命令。
- `GET /admin/api/envs` 既有消费方（前端 `envTargets`）无类型/字段断裂（`npm run build` 已在 Task 4 通过）。

- [ ] **Step 5: 写 known-pitfalls 详情文件**（新建 `docs/known-pitfalls/backend/docker-bypass-guide-single-source.md`，内容如下——外层四反引号仅为本文档转义，写入文件时去掉外层围栏）

````markdown
# Docker 旁路指引：能力矩阵必须有单一事实来源

> 原始单文件已并入本文件归档（本主题为首次沉淀，无历史归档）。

## 平台限制文案承诺了 UI 无法兑现的旁路路径

**现象**：Windows 环境页点 Setup，任务失败提示"…或走 docker 镜像绕过 venv"，但 UI 上没有任何对应操作入口，用户无处可去。

**根因**：`envs.platform_limitation_message()` 的文案是手写字符串，与实际能力（仅 vllm/tokenspeed/tensorrt_llm 适配器实现 `_resolve_runtime` 的 docker_image 分支）没有代码级关联；`docker_setup.diagnose()/render_instructions()` 只在 CLI 可达，webui 层 grep "docker" 零命中。

**解决**：
1. 能力矩阵落到常量，并由测试锚定实现，避免文案与实现漂移：

```python
# envs.py
DOCKER_CAPABLE_ENGINES = ("vllm", "tokenspeed", "tensorrt_llm")

# tests/test_core_envs.py —— 常量成员必须 _resolve_runtime() == ('docker', image)，
# 未列入的引擎适配器类不得定义 _resolve_runtime
```

2. UI 指引由常量表驱动，未支持引擎只给 `note` 不给 `steps`（严禁对没有 docker 分支的引擎渲染镜像指引）：

```python
guide = DOCKER_BYPASS_GUIDES.get(name)
if guide: ...          # 镜像事实 + 三步
else: {"docker_supported": False, "note": DOCKER_UNSUPPORTED_NOTE}
```

## 只读探测与子进程诊断必须分层

**现象/风险**：`docker_setup.diagnose()` 内含 `docker info`（15s 超时）；若并入 `GET /envs` 列表端点，Docker 缺失或 daemon 卡顿时环境页加载会阻塞数秒。

**解决**：列表端点只用 `path_level_missing()`（纯 `shutil.which`，零子进程）做徽标；完整 4 项诊断放独立端点 `GET /envs/docker/diagnose`，由前端"完整诊断"按钮首次展开时懒加载一次，并用 `asyncio.to_thread` 包裹避免阻塞事件循环：

```python
missing = docker_setup.path_level_missing()          # 列表内联，永不抛错
checks = await asyncio.to_thread(docker_setup.diagnose)  # 按需端点
```

## 平台不支持的操作应在入口禁用而非仅事后报错

**现象**：环境页对 Windows 不可能成功的 Setup 按钮照常亮着，用户必须点一次、等任务失败才知道不行。

**解决**：响应带 `platform_supported`，前端把门禁前移到按钮，并让失败文案兜底二次引导（`disabled` + `disabled-reason` 走 TaskButton 新 props；`@error` 回调检测固定关键字追加引导语）：

```vue
<TaskButton :disabled="!t.platform_supported" :disabled-reason="'托管 venv 仅支持 Linux 部署机，可走下方「Docker 旁路」区块'" />
```

```ts
if (msg.includes('docker 镜像绕过')) notice.value += '——旁路步骤见下方「Docker 旁路」区块';
```

注意：TaskButton 的 `disabled` 是**外部**门禁，与既有内部 `phase !== 'idle'` 取或，不可替换掉原有判断。
````

- [ ] **Step 6: 更新摘要索引**（`docs/known-pitfalls/README.md` 表格末尾追加一行；该表列序为 **日期 | 分类 | 标题 | 一句话描述 | 详情**，详情列用 markdown 链接）

```markdown
| 2026-09-07 | 后端 / 环境管理 | 平台限制文案承诺"走 docker 镜像绕过"，UI 却没有任何落地入口 | 能力矩阵必须落常量并由测试锚定实现（`DOCKER_CAPABLE_ENGINES`），未支持引擎只给 note 不造假 steps；只读 PATH 探测与子进程诊断分层，平台不支持的操作在按钮入口即禁用而非事后报错。 | [backend/docker-bypass-guide-single-source.md](backend/docker-bypass-guide-single-source.md) |
```

- [ ] **Step 7: Commit**

```powershell
git add docs/known-pitfalls/README.md docs/known-pitfalls/backend/docker-bypass-guide-single-source.md ; git commit -m "docs: known-pitfalls for docker-bypass single source and read-only diagnose layering"
```

---

## Self-Review 结论（已执行）

1. **Spec 覆盖**：§3.1 → Task 1；§3.2 → Task 2；§3.3 → Task 2；§4.1 → Task 3；§4.2/4.3/4.4 → Task 4；§5 错误处理 → Task 2 Step 5 + Task 4 Step 1d；§6 测试 → Task 1/2/3/4 各自门禁 + Task 5 端到端；§7 决策非目标（无 yaml 写、无 docker 执行、不补 sglang）→ 全计划无任何写操作，符合。
2. **占位符扫描**：无 TBD/TODO/"类似 Task N"；所有代码步骤均给出完整代码。
3. **类型一致性**：`DOCKER_CAPABLE_ENGINES`（Task 1）在 Task 2 引用名一致；`docker_env{ready,missing,guide}` / `docker_bypass[{name,docker_supported,image_example,yaml_field_path,example_yaml,steps,note}]` / `docker_diagnose{checks[{key,label,ok,detail}],instructions}` 三处（Task 2 生产、Task 3 类型、Task 4 消费）字段名逐字一致；TaskButton `disabled`/`disabledReason`（Task 3 定义、Task 4 以 kebab `disabled-reason` 使用）符合 Vue prop 命名转换规则。