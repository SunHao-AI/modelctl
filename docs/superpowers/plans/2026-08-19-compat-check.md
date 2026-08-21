# 能力检测框架（硬件 + 软件）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 modelctl 各引擎（vllm/sglang/llamacpp/unsloth/ollama）提供启动前能力检测框架：硬件快照（GpuSpec）+ 软件/环境快照（EnvSpec）+ 内置规则库，两段式（id 预检 + config.json 精检）在启动前拦截不兼容组合并给出修复建议。

**Architecture:** 新增 `core/compat.py`（框架：GpuSpec/EnvSpec/ModelSpec/CompatIssue/CompatRule/run_compat/apply_compat）与 `core/compat_rules.py`（内置规则，导入即注册）。`EngineAdapter` 增加基类方法 `run_compat_checks()`（函数内延迟 import 避免循环依赖），各适配器在 `check_requirements`（预检）与 `pre_start` 末尾（精检）调用。block 抛 `RequirementError`（CLI 现有机制 exit 2），degrade 写 `adapter.warnings`。

**Tech Stack:** Python 3.12、PyYAML、loguru、pytest（tmp_path 伪造虚拟 site-packages 测试 EnvSpec，不引入第三方依赖）。

## Global Constraints

- `requires-python = ">=3.12"`；运行期依赖仅 `PyYAML>=6.0` + `loguru>=0.7`；compat 模块**不得新增第三方依赖**（不引入 packaging，版本比较自实现极简 `_spec_matches`）。
- 代码注释用中文；ruff/mypy 通过（`uv run ruff check src tests`、`uv run mypy src`）。
- 全量测试 `uv run pytest -q` 通过（Windows 开发机可运行）。
- 数据缺失**不误报**：CC 未知 / ldconfig 不可用 / METADATA 缺失 / 模型未下载 → 相关规则返回 None（跳过）。
- 行为分级：`block` → 拼接全部原因抛 `RequirementError`；`degrade` → 写入 `adapter.warnings`。
- 单次 CLI 调用 EnvSpec 只探测一次（check_requirements 创建，pre_start 复用）。
- 现有 `tests/test_engines_vllm.py` 中 4 个 DeepSeek-V4 测试行为必须不变（Ada 拦截 / 本地 config 识别 / Hopper 放行 / CC 未知跳过）。
- 资源类检查（GPU 数 vs TP、显存预检、DSpark 降级）保持各适配器原位，**不并入**框架。

---

### Task 1: compat.py 框架基础 —— GpuSpec / ModelSpec / cc_major

**Files:**
- Create: `src/modelctl/core/compat.py`
- Test: `tests/test_compat.py`（新建）

**Interfaces:**
- Produces:
  - `cc_major(cc: str) -> int | None` —— CC 主版本（"8.9"→8，无法解析→None）
  - `ARCH_FAMILY_LABELS: dict[int, str]`（8=Ampere/Ada、9=Hopper、10=Blackwell、12=Blackwell-Consumer）
  - `GpuSpec`：`cc`/`gpu_count`/`gpu_name`/`vram_total_mb`/`vram_free_mb`；属性 `cc_major`、`arch_family`；类方法 `from_caps(caps: Capabilities) -> GpuSpec`
  - `ModelSpec`：`engine`/`source`("local"|"id")/`architectures: tuple[str,...]`/`model_type`/`quantization`/`name_hint`；属性 `is_deepseek_v4`；类方法 `from_local(engine, path)`、`from_id(engine, model_id, download_id="", quantization="")`

- [ ] **Step 1: 写失败测试 `tests/test_compat.py`**

```python
"""能力检测框架单元测试（GpuSpec / ModelSpec / cc_major）。"""

from __future__ import annotations

from modelctl.core.capabilities import Capabilities
from modelctl.core.compat import GpuSpec, ModelSpec, cc_major


def test_cc_major_parsing():
    assert cc_major("8.9") == 8
    assert cc_major("12.0") == 12
    assert cc_major("") is None
    assert cc_major("abc") is None


def test_gpu_spec_from_caps():
    caps = Capabilities(gpu_count=8, compute_capability="8.9", gpu_name="RTX 5880", vram_total_mb=49152, vram_free_mb=[100, 200])
    gpu = GpuSpec.from_caps(caps)
    assert gpu.cc_major == 8
    assert gpu.arch_family == "Ampere/Ada"
    assert gpu.gpu_count == 8
    assert gpu.vram_free_mb == [100, 200]


def test_gpu_spec_arch_family_unknown():
    assert GpuSpec(cc="").arch_family == "unknown"


def test_model_spec_from_local(tmp_path):
    (tmp_path / "config.json").write_text(
        '{"architectures": ["DeepseekV4ForCausalLM"], "model_type": "deepseek_v4", '
        '"quantization_config": {"quant_method": "deepseek_v4_fp8"}}',
        encoding="utf-8",
    )
    m = ModelSpec.from_local("vllm", tmp_path)
    assert m.source == "local"
    assert m.is_deepseek_v4
    assert "fp8" in m.quantization


def test_model_spec_from_local_missing_config(tmp_path):
    m = ModelSpec.from_local("vllm", tmp_path)
    assert m.source == "local"
    assert not m.is_deepseek_v4
    assert m.quantization == ""


def test_model_spec_from_local_bad_json(tmp_path):
    (tmp_path / "config.json").write_text("{ not json", encoding="utf-8")
    m = ModelSpec.from_local("vllm", tmp_path)
    assert not m.is_deepseek_v4


def test_model_spec_from_id_detects_deepseek_v4():
    m = ModelSpec.from_id("vllm", "deepseek-ai/DeepSeek-V4-Flash")
    assert m.source == "id"
    assert m.is_deepseek_v4


def test_model_spec_from_id_download_id():
    m = ModelSpec.from_id("vllm", "", "deepseek-ai/DeepSeek-V4-Flash")
    assert m.is_deepseek_v4


def test_model_spec_from_id_quantization():
    m = ModelSpec.from_id("vllm", "Qwen/Qwen3-32B", quantization="fp8")
    assert "fp8" in m.quantization
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_compat.py -v`
Expected: FAIL（`ModuleNotFoundError: modelctl.core.compat`）

- [ ] **Step 3: 实现 `src/modelctl/core/compat.py`（本任务部分）**

```python
#!/usr/bin/env python3
"""core/compat.py — 启动前能力检测框架（硬件 GpuSpec + 软件 EnvSpec + 规则库）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from modelctl.core.capabilities import Capabilities

# 计算能力主版本 -> 架构家族（仅用于错误消息展示，不参与规则判定）
ARCH_FAMILY_LABELS: dict[int, str] = {
    8: "Ampere/Ada",
    9: "Hopper",
    10: "Blackwell",
    12: "Blackwell-Consumer",
}

# DeepSeek-V4 的 mHC（Manifold-Constrained Hyper-Connections）层依赖 DeepGEMM 的
# tf32_hc_prenorm_gemm 内核，官方仅提供 SM90（Hopper）/SM100（Blackwell DC）实现。
_DEEPSEEK_V4_ARCHS = ("DeepseekV4ForCausalLM",)
_DEEPSEEK_V4_NAME_MARKERS = ("deepseek-v4", "deepseek_v4")


def cc_major(cc: str) -> int | None:
    """提取 compute capability 主版本号（"8.9" -> 8）；无法解析返回 None。"""
    try:
        return int(cc.split(".", 1)[0])
    except (ValueError, AttributeError):
        return None


def _is_deepseek_v4_name(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _DEEPSEEK_V4_NAME_MARKERS)


@dataclass
class GpuSpec:
    """硬件能力快照。"""

    cc: str = ""
    gpu_count: int = 0
    gpu_name: str = ""
    vram_total_mb: int = 0
    vram_free_mb: list[int] = field(default_factory=list)

    @property
    def cc_major(self) -> int | None:
        return cc_major(self.cc)

    @property
    def arch_family(self) -> str:
        major = self.cc_major
        if major is None:
            return "unknown"
        return ARCH_FAMILY_LABELS.get(major, "unknown")

    @classmethod
    def from_caps(cls, caps: Capabilities) -> "GpuSpec":
        return cls(
            cc=caps.compute_capability,
            gpu_count=caps.gpu_count,
            gpu_name=caps.gpu_name,
            vram_total_mb=caps.vram_total_mb,
            vram_free_mb=list(caps.vram_free_mb),
        )


@dataclass
class ModelSpec:
    """模型特征（预检 source=id / 精检 source=local）。"""

    engine: str
    source: Literal["local", "id"] = "id"
    architectures: tuple[str, ...] = ()
    model_type: str = ""
    quantization: str = ""
    name_hint: str = ""

    @property
    def is_deepseek_v4(self) -> bool:
        if any(a in _DEEPSEEK_V4_ARCHS for a in self.architectures):
            return True
        if "deepseek_v4" in self.model_type.lower():
            return True
        return _is_deepseek_v4_name(self.name_hint)

    @classmethod
    def from_local(cls, engine: str, path: str | Path) -> "ModelSpec":
        data: dict = {}
        config = Path(path).expanduser() / "config.json"
        if config.is_file():
            try:
                data = json.loads(config.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        quant = str((data.get("quantization_config") or {}).get("quant_method") or "").lower()
        return cls(
            engine=engine,
            source="local",
            architectures=tuple(str(a) for a in data.get("architectures") or []),
            model_type=str(data.get("model_type") or ""),
            quantization=quant,
            name_hint=str(path),
        )

    @classmethod
    def from_id(cls, engine: str, model_id: str, download_id: str = "", quantization: str = "") -> "ModelSpec":
        return cls(
            engine=engine,
            source="id",
            quantization=quantization.lower(),
            name_hint=f"{model_id} {download_id}".strip(),
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_compat.py -v`
Expected: PASS（10 passed）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/compat.py tests/test_compat.py
git commit -m "feat(core): 能力检测框架基础类型 GpuSpec/ModelSpec"
```

---

### Task 2: EnvSpec 软件/环境快照

**Files:**
- Modify: `src/modelctl/core/compat.py`（追加 EnvSpec 与探测 helper）
- Test: `tests/test_compat.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 compat.py 骨架。
- Produces:
  - `EnvSpec`：`site_packages: Path | None`、`packages: dict[str, str]`（小写包名→版本）、`wheel_requires: dict[str, dict[str, str]]`（小写 wheel 名→{小写依赖名: 约束串}）、`nvidia_so: set[str]`（相对 site-packages 的 .so 路径，`/` 分隔）、`cuda_libs_resolvable: set[str]`（可解析库文件名）、`libs_resolvable_known: bool`、`env_vars: dict[str, str | None]`、`disk_free_mb: int`
  - 类方法 `EnvSpec.from_env(site_packages: Path | None = None) -> EnvSpec`；`site_packages=None` 时自动定位当前解释器的 site-packages。

- [ ] **Step 1: 写失败测试（追加到 `tests/test_compat.py`）**

```python
def _fake_site_packages(tmp_path) -> Path:
    """构造虚拟 site-packages：vllm METADATA（含 torch 约束）、nvidia cudnn 包与假 .so。"""
    sp = tmp_path / "site-packages"
    vllm_dist = sp / "vllm-0.27.1.dist-info"
    vllm_dist.mkdir(parents=True)
    (vllm_dist / "METADATA").write_text(
        "Name: vllm\nVersion: 0.27.1\nRequires-Dist: torch==2.13.0\nRequires-Dist: xgrammar>=0.2.3\n",
        encoding="utf-8",
    )
    torch_dist = sp / "torch-2.9.1.dist-info"
    torch_dist.mkdir(parents=True)
    (torch_dist / "METADATA").write_text("Name: torch\nVersion: 2.9.1\n", encoding="utf-8")
    cudnn = sp / "nvidia" / "cudnn" / "lib"
    cudnn.mkdir(parents=True)
    (cudnn / "libcudnn.so.9").write_bytes(b"")
    return sp


def test_env_spec_metadata(tmp_path):
    env = EnvSpec.from_env(site_packages=_fake_site_packages(tmp_path))
    assert env.packages["torch"] == "2.9.1"
    assert env.packages["vllm"] == "0.27.1"
    assert env.wheel_requires["vllm"]["torch"] == "==2.13.0"
    assert env.wheel_requires["vllm"]["xgrammar"] == ">=0.2.3"


def test_env_spec_nvidia_so(tmp_path):
    env = EnvSpec.from_env(site_packages=_fake_site_packages(tmp_path))
    assert "nvidia/cudnn/lib/libcudnn.so.9" in env.nvidia_so
    assert "libcudnn.so.9" in env.cuda_libs_resolvable  # venv 内 nvidia 库并入可解析集


def test_env_spec_empty_site_packages(tmp_path):
    env = EnvSpec.from_env(site_packages=tmp_path / "nonexistent")
    assert env.packages == {}
    assert env.wheel_requires == {}
    assert env.nvidia_so == set()


def test_env_spec_env_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HOME", "/data/hf")
    monkeypatch.setenv("MODEL_ROOT", "")
    env = EnvSpec.from_env(site_packages=tmp_path)
    assert env.env_vars["HF_HOME"] == "/data/hf"
    assert env.env_vars["MODEL_ROOT"] == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_compat.py -v`
Expected: FAIL（`NameError: EnvSpec`）

- [ ] **Step 3: 实现 EnvSpec（追加到 compat.py）**

```python
import os
import shutil
import subprocess
from typing import Callable  # 合并到 Task 3 的 import 区


@dataclass
class EnvSpec:
    """软件/环境能力快照（静态元数据 + 文件检查，不导入引擎）。"""

    site_packages: Path | None = None
    packages: dict[str, str] = field(default_factory=dict)
    wheel_requires: dict[str, dict[str, str]] = field(default_factory=dict)
    nvidia_so: set[str] = field(default_factory=set)
    cuda_libs_resolvable: set[str] = field(default_factory=set)
    libs_resolvable_known: bool = True
    env_vars: dict[str, str | None] = field(default_factory=dict)
    disk_free_mb: int = 0

    @classmethod
    def from_env(cls, site_packages: Path | None = None) -> "EnvSpec":
        sp = site_packages if site_packages is not None else _current_site_packages()
        env = cls(site_packages=sp)
        if sp is not None and sp.is_dir():
            env.packages = _read_installed_packages(sp)
            env.wheel_requires = _read_wheel_requires(sp)
            env.nvidia_so = _scan_nvidia_so(sp)
        env.env_vars = {k: os.environ.get(k) for k in ("HF_HOME", "MODEL_ROOT", "MODELSCOPE_CACHE", "LD_LIBRARY_PATH")}
        env.disk_free_mb = _disk_free_mb()
        env.cuda_libs_resolvable, env.libs_resolvable_known = _resolvable_cuda_libs()
        # venv 内 nvidia 库目录通常由启动方加入链接路径，并入可解析集
        env.cuda_libs_resolvable.update(Path(rel).name for rel in env.nvidia_so)
        return env


def _current_site_packages() -> Path | None:
    """定位当前解释器的 site-packages（纯标准库）。"""
    import site

    paths = site.getsitepackages()
    return Path(paths[0]) if paths else None


def _read_installed_packages(sp: Path) -> dict[str, str]:
    """读取 sp 下所有 *.dist-info/METADATA 的 Name/Version。"""
    result: dict[str, str] = {}
    for meta in sp.glob("*.dist-info/METADATA"):
        name = version = ""
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Name:") and not name:
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Version:") and not version:
                version = line.split(":", 1)[1].strip()
            if name and version:
                break
        if name:
            result[name.lower()] = version
    return result


def _read_wheel_requires(sp: Path) -> dict[str, dict[str, str]]:
    """解析各 wheel METADATA 的 Requires-Dist，保留单条目约束串（如 "==2.13.0"）。"""
    result: dict[str, dict[str, str]] = {}
    for meta in sp.glob("*.dist-info/METADATA"):
        name = ""
        reqs: dict[str, str] = {}
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Requires-Dist:"):
                spec = line.split(":", 1)[1].strip()
                marker_pos = spec.find(";")
                if marker_pos != -1:
                    spec = spec[:marker_pos].strip()
                parts = spec.split()
                if parts:
                    dep = parts[0].lower()
                    if len(parts) > 1:
                        reqs[dep] = "".join(parts[1:])
        if name:
            result[name.lower()] = reqs
    return result


def _scan_nvidia_so(sp: Path) -> set[str]:
    """扫描 site-packages/nvidia 下实际存在的 .so 文件（相对路径，/ 分隔）。"""
    nv = sp / "nvidia"
    if not nv.is_dir():
        return set()
    return {str(p.relative_to(sp)).replace("\\", "/") for p in nv.rglob("*.so*") if p.is_file()}


def _resolvable_cuda_libs() -> tuple[set[str], bool]:
    """探测动态链接器可解析的 .so 文件名集合。ldconfig 不可用时返回 (空集, False)。"""
    names: set[str] = set()
    try:
        out = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return names, False
    if out.returncode != 0:
        return names, False
    for line in out.stdout.splitlines():
        parts = line.split("=>")
        if len(parts) == 2:
            names.add(parts[0].strip().split()[-1])
    for d in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep):
        if not d:
            continue
        p = Path(d)
        if p.is_dir():
            names.update(f.name for f in p.glob("*.so*") if f.is_file())
    return names, True


def _disk_free_mb() -> int:
    try:
        return int(shutil.disk_usage(os.getcwd()).free / 1024 / 1024)
    except OSError:
        return 0
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_compat.py -v`
Expected: PASS（14 passed）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/compat.py tests/test_compat.py
git commit -m "feat(core): EnvSpec 软件/环境快照探测"
```

---

### Task 3: CompatIssue / CompatRule / run_compat / apply_compat + 版本比较 helper

**Files:**
- Modify: `src/modelctl/core/compat.py`（追加）
- Test: `tests/test_compat.py`（追加）

**Interfaces:**
- Consumes: Task 1/2 的 GpuSpec/ModelSpec/EnvSpec。
- Produces:
  - `CompatIssue`：`level: Literal["block","degrade"]`/`rule_id: str`/`reason: str`（frozen dataclass）
  - `CompatRule`：`id: str`/`engines: tuple[str,...]`/`check: Callable[[GpuSpec, EnvSpec, ModelSpec | None], CompatIssue | None]`（frozen dataclass）
  - `register_rule(rule: CompatRule) -> None`（重复 id 抛 ValueError）
  - `run_compat(engine: str, gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> list[CompatIssue]`（按 engine 过滤，block 在前）
  - `apply_compat(profile_name: str, engine: str, warnings: list[str], issues: list[CompatIssue]) -> None`（block → 抛 RequirementError；degrade → append warnings）
  - `_spec_matches(requirement: str, version: str) -> bool`（极简单条目版本约束，无法解析返回 True 不误报）

- [ ] **Step 1: 写失败测试（追加）**

```python
from modelctl.core.compat import (
    CompatIssue, CompatRule, EnvSpec, GpuSpec, ModelSpec,
    apply_compat, register_rule, run_compat, _spec_matches,
)
from modelctl.engines.base import RequirementError


def _rule(rule_id: str, issue: CompatIssue | None):
    return CompatRule(id=rule_id, engines=("vllm",), check=lambda g, e, m: issue)


def test_run_compat_filters_by_engine():
    register_rule(_rule("r1", CompatIssue("block", "r1", "x")))
    issues = run_compat("vllm", GpuSpec(), EnvSpec(), None)
    assert [i.rule_id for i in issues] == ["r1"]
    assert run_compat("sglang", GpuSpec(), EnvSpec(), None) == []


def test_run_compat_sorts_block_first():
    register_rule(_rule("d1", CompatIssue("degrade", "d1", "x")))
    register_rule(_rule("b1", CompatIssue("block", "b1", "x")))
    issues = run_compat("vllm", GpuSpec(), EnvSpec(), None)
    assert [i.level for i in issues] == ["block", "degrade"]


def test_register_rule_duplicate_raises():
    register_rule(_rule("dup", None))
    try:
        register_rule(_rule("dup", None))
        assert False, "应抛 ValueError"
    except ValueError:
        pass


def test_apply_compat_block_raises():
    issues = [CompatIssue("degrade", "d", "警告a"), CompatIssue("block", "b", "阻断b")]
    try:
        apply_compat("ds4", "vllm", [], issues)
        assert False, "应抛 RequirementError"
    except RequirementError as e:
        assert "ds4" in str(e) and "阻断b" in str(e)


def test_apply_compat_degrade_writes_warnings():
    warnings: list[str] = []
    apply_compat("ds4", "vllm", warnings, [CompatIssue("degrade", "d", "警告a")])
    assert warnings == ["[d] 警告a"]


def test_spec_matches():
    assert _spec_matches("==2.13.0", "2.13.0")
    assert not _spec_matches("==2.13.0", "2.9.1")
    assert _spec_matches(">=0.2.3", "0.2.3")
    assert _spec_matches(">=0.2.3", "0.3.0")
    assert _spec_matches("garbage", "1.0")  # 无法解析不误报
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_compat.py -v`
Expected: FAIL（`ImportError` / 缺函数）

- [ ] **Step 3: 实现（追加到 compat.py）**

```python
from typing import Callable

from modelctl.engines.base import RequirementError


@dataclass(frozen=True)
class CompatIssue:
    level: Literal["block", "degrade"]
    rule_id: str
    reason: str


@dataclass(frozen=True)
class CompatRule:
    id: str
    engines: tuple[str, ...]
    check: Callable[[GpuSpec, EnvSpec, ModelSpec | None], CompatIssue | None]


_RULES: list[CompatRule] = []


def register_rule(rule: CompatRule) -> None:
    if any(r.id == rule.id for r in _RULES):
        raise ValueError(f"规则重复注册：{rule.id}")
    _RULES.append(rule)


def run_compat(engine: str, gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> list[CompatIssue]:
    """按引擎过滤规则并执行，block 在前、degrade 在后。"""
    issues: list[CompatIssue] = []
    for rule in _RULES:
        if engine not in rule.engines:
            continue
        issue = rule.check(gpu, env, model)
        if issue is not None:
            issues.append(issue)
    return sorted(issues, key=lambda i: 0 if i.level == "block" else 1)


def apply_compat(profile_name: str, engine: str, warnings: list[str], issues: list[CompatIssue]) -> None:
    """block 拼接全部原因抛 RequirementError；degrade 写入 warnings。"""
    blocks = [i for i in issues if i.level == "block"]
    if blocks:
        lines = "\n".join(f"  [{i.rule_id}] {i.reason}" for i in blocks)
        raise RequirementError(f"当前服务器不支持 {engine} 引擎部署 {profile_name} 模型：\n{lines}")
    for issue in issues:
        warnings.append(f"[{issue.rule_id}] {issue.reason}")


def _spec_matches(requirement: str, version: str) -> bool:
    """极简单条目版本约束匹配（==/>=/<=/>/</!=）；无法解析视为匹配（不误报）。"""
    req = requirement.strip()
    for op in ("==", ">=", "<=", "!=", ">", "<"):
        if req.startswith(op):
            target = req[len(op):].strip()
            if op == "!=":
                return version != target
            return _cmp_versions(version, target, op)
    return True


def _cmp_versions(a: str, b: str, op: str) -> bool:
    def _t(v: str) -> tuple:
        parts: list = []
        for x in v.replace("-", ".").split("."):
            try:
                parts.append(int(x))
            except ValueError:
                parts.append(x)
        return tuple(parts)

    try:
        ta, tb = _t(a), _t(b)
    except Exception:  # noqa: BLE001 —— 解析失败不误报
        return True
    ops = {"==": ta == tb, ">=": ta >= tb, "<=": ta <= tb, ">": ta > tb, "<": ta < tb}
    return ops.get(op, True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_compat.py -v`
Expected: PASS（20 passed）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/compat.py tests/test_compat.py
git commit -m "feat(core): 规则注册表 run_compat/apply_compat 与版本比较"
```

---

### Task 4: 内置硬件规则（deepseek_v4_mhc / fp8_quant_cc / fp4_quant_blackwell）

**Files:**
- Create: `src/modelctl/core/compat_rules.py`
- Test: `tests/test_compat.py`（追加，`import modelctl.core.compat_rules` 触发注册）

**Interfaces:**
- Consumes: Task 1-3 的框架 API；`modelctl.core.capabilities.cc_at_least`。
- Produces: 三个已注册规则：
  - `deepseek_v4_mhc`（engines=(vllm, sglang)）：ModelSpec.is_deepseek_v4 且 CC 主版本 ∉ {9,10} → block（CC 未知跳过）
  - `fp8_quant_cc`（engines=(vllm, sglang)）：quantization 含 "fp8" 且 CC < 8.9 → block（CC 为空跳过）
  - `fp4_quant_blackwell`（engines=(vllm,)）：quantization 含 "fp4" 且 CC 主版本 ∉ {10,12} → block（CC 未知跳过）

- [ ] **Step 1: 写失败测试（追加）**

```python
import modelctl.core.compat_rules  # noqa: F401 —— 导入即注册
from modelctl.core.compat import GpuSpec, EnvSpec, ModelSpec, run_compat


def _run(engine: str, cc: str, model: ModelSpec | None):
    return run_compat(engine, GpuSpec(cc=cc), EnvSpec(), model)


def _ds4(engine: str = "vllm") -> ModelSpec:
    return ModelSpec(engine=engine, source="id", name_hint="deepseek-ai/DeepSeek-V4-Flash")


def test_deepseek_v4_mhc_block_on_ada():
    issues = _run("vllm", "8.9", _ds4())
    assert any(i.rule_id == "deepseek_v4_mhc" and i.level == "block" for i in issues)


def test_deepseek_v4_mhc_block_on_sm120():
    issues = _run("sglang", "12.0", _ds4("sglang"))
    assert any(i.rule_id == "deepseek_v4_mhc" for i in issues)


def test_deepseek_v4_mhc_allowed_on_hopper_blackwell_dc():
    assert _run("vllm", "9.0", _ds4()) == []
    assert _run("vllm", "10.0", _ds4()) == []


def test_deepseek_v4_mhc_skips_when_cc_unknown():
    assert _run("vllm", "", _ds4()) == []


def test_deepseek_v4_mhc_not_applicable_to_other_model():
    assert _run("vllm", "8.9", ModelSpec(engine="vllm", name_hint="Qwen/Qwen3-32B")) == []


def test_fp8_quant_cc():
    m = ModelSpec(engine="vllm", name_hint="Qwen/Qwen3-32B", quantization="fp8")
    assert any(i.rule_id == "fp8_quant_cc" for i in _run("vllm", "7.5", m))
    assert _run("vllm", "8.9", m) == []
    assert _run("vllm", "", m) == []  # CC 未知跳过
    assert _run("vllm", "7.5", ModelSpec(engine="vllm", name_hint="m", quantization="awq")) == []


def test_fp4_quant_blackwell():
    m = ModelSpec(engine="vllm", name_hint="m", quantization="fp4")
    assert any(i.rule_id == "fp4_quant_blackwell" for i in _run("vllm", "8.9", m))
    assert _run("vllm", "10.0", m) == []
    assert _run("vllm", "12.0", m) == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_compat.py -v`
Expected: FAIL（`ModuleNotFoundError: modelctl.core.compat_rules`）

- [ ] **Step 3: 实现 `src/modelctl/core/compat_rules.py`**

```python
#!/usr/bin/env python3
"""core/compat_rules.py — 内置能力检测规则注册（导入即注册）。"""

from __future__ import annotations

from modelctl.core.capabilities import cc_at_least
from modelctl.core.compat import (
    CompatIssue,
    CompatRule,
    EnvSpec,
    GpuSpec,
    ModelSpec,
    register_rule,
)


def _deepseek_v4_mhc_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    if model is None or not model.is_deepseek_v4:
        return None
    major = gpu.cc_major
    if major is None:
        return None  # CC 未知，不误报
    if major in (9, 10):
        return None
    gpu_name = gpu.gpu_name or f"GPU（CC {gpu.cc}）"
    return CompatIssue(
        level="block",
        rule_id="deepseek_v4_mhc",
        reason=(
            f"DeepSeek-V4 的 mHC（HyperConnection）层依赖 DeepGEMM hyperconnection 内核，"
            f"官方仅支持 Hopper/Blackwell DC（计算能力 9.0/10.0），"
            f"当前 GPU 为 {gpu_name}（CC {gpu.cc}）。"
            "如仍需在当前架构部署，可改用 llamacpp 引擎运行 GGUF 版本（models/llamacpp/deepseek-v4-flash.yaml）。"
        ),
    )


def _fp8_quant_cc_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    if model is None or "fp8" not in model.quantization:
        return None
    if not gpu.cc or cc_at_least(gpu.cc, 8, 9):
        return None
    return CompatIssue(
        level="block",
        rule_id="fp8_quant_cc",
        reason=f"FP8 量化需要计算能力 ≥ 8.9，当前 CC {gpu.cc}。建议改用 bf16 权重或更换 GPU。",
    )


def _fp4_quant_blackwell_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    if model is None or "fp4" not in model.quantization:
        return None
    major = gpu.cc_major
    if major is None:
        return None
    if major in (10, 12):
        return None
    return CompatIssue(
        level="block",
        rule_id="fp4_quant_blackwell",
        reason=f"FP4 量化仅支持 Blackwell（计算能力 10.0/12.0），当前 CC {gpu.cc}。",
    )


def _register() -> None:
    register_rule(CompatRule(id="deepseek_v4_mhc", engines=("vllm", "sglang"), check=_deepseek_v4_mhc_check))
    register_rule(CompatRule(id="fp8_quant_cc", engines=("vllm", "sglang"), check=_fp8_quant_cc_check))
    register_rule(CompatRule(id="fp4_quant_blackwell", engines=("vllm",), check=_fp4_quant_blackwell_check))


_register()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_compat.py -v`
Expected: PASS（29 passed）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/compat_rules.py tests/test_compat.py
git commit -m "feat(core): 内置硬件能力规则（DeepSeek-V4 mHC / FP8 / FP4）"
```

---

### Task 5: 内置软件规则（vllm_torch_abi / nvidia_pkg_complete / cuda_lib_resolvable / engine_dep_missing / env_var_missing）

**Files:**
- Modify: `src/modelctl/core/compat_rules.py`（追加）
- Test: `tests/test_compat.py`（追加）

**Interfaces:**
- Consumes: Task 3 的 `_spec_matches`；Task 2 的 EnvSpec 字段。
- Produces:
  - `vllm_torch_abi`（engines=(vllm,)）：`wheel_requires["vllm"]["torch"]` 与 `packages["torch"]` 不匹配 → block（含安装建议）
  - `nvidia_pkg_complete`（engines=(vllm, sglang)）：扫描 `nvidia-*.dist-info/RECORD` 中声明的 .so 与 `nvidia_so` 比对，缺失 → block
  - `cuda_lib_resolvable`（engines=(vllm, sglang)）：由 nvidia 包版本推导所需库名，不在 `cuda_libs_resolvable` → block（`libs_resolvable_known=False` 跳过）
  - `engine_dep_missing`（engines=(vllm,)）：vllm 对 xgrammar/flashinfer-python/tokenizers/transformers/triton 的约束不匹配 → block
  - `env_var_missing`（engines=全部）：HF_HOME/MODELSCOPE_CACHE 未设置 → degrade

- [ ] **Step 1: 写失败测试（追加）**

```python
from modelctl.core.compat import EnvSpec, GpuSpec, ModelSpec, run_compat


def _env(tmp_path, vllm_reqs="", packages=None, nvidia_missing=False):
    sp = tmp_path / "sp"
    (sp / "vllm-0.27.1.dist-info").mkdir(parents=True)
    (sp / "vllm-0.27.1.dist-info" / "METADATA").write_text(
        f"Name: vllm\nVersion: 0.27.1\n{vllm_reqs}", encoding="utf-8"
    )
    for pkg, ver in (packages or {"torch": "2.9.1"}).items():
        dist = sp / f"{pkg}-{ver}.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(f"Name: {pkg}\nVersion: {ver}\n", encoding="utf-8")
    env = EnvSpec.from_env(site_packages=sp)
    if nvidia_missing:
        # RECORD 声明 libcudnn.so.9 但磁盘缺失
        dist = sp / "nvidia_cudnn_cu13-9.20.0.48.dist-info"
        dist.mkdir(parents=True)
        (dist / "RECORD").write_text("nvidia/cudnn/lib/libcudnn.so.9,,", encoding="utf-8")
        env = EnvSpec.from_env(site_packages=sp)
    return env


def test_vllm_torch_abi_block():
    env = _env(tmp_path, "Requires-Dist: torch==2.13.0\n")
    issues = run_compat("vllm", GpuSpec(), env, None)
    assert any(i.rule_id == "vllm_torch_abi" and i.level == "block" and "2.13.0" in i.reason for i in issues)


def test_vllm_torch_abi_pass_when_matched():
    env = _env(tmp_path, "Requires-Dist: torch==2.9.1\n", packages={"torch": "2.9.1"})
    assert run_compat("vllm", GpuSpec(), env, None) == []


def test_vllm_torch_abi_skip_when_no_req():
    assert run_compat("vllm", GpuSpec(), _env(tmp_path, ""), None) == []


def test_nvidia_pkg_complete_block(tmp_path):
    env = _env(tmp_path, nvidia_missing=True)
    issues = run_compat("vllm", GpuSpec(), env, None)
    assert any(i.rule_id == "nvidia_pkg_complete" and i.level == "block" for i in issues)


def test_nvidia_pkg_complete_pass_when_present(tmp_path):
    env = EnvSpec.from_env(site_packages=tmp_path)  # 无 nvidia dist-info
    assert run_compat("vllm", GpuSpec(), env, None) == []


def test_cuda_lib_resolvable_block(tmp_path, monkeypatch):
    env = _env(tmp_path, packages={"nvidia-cuda-runtime": "13.0.96"})
    env.cuda_libs_resolvable = set()  # 模拟库不可解析
    env.libs_resolvable_known = True
    issues = run_compat("vllm", GpuSpec(), env, None)
    assert any(i.rule_id == "cuda_lib_resolvable" and "libcudart.so.13" in i.reason for i in issues)


def test_cuda_lib_resolvable_skip_when_unknown(tmp_path):
    env = _env(tmp_path, packages={"nvidia-cuda-runtime": "13.0.96"})
    env.libs_resolvable_known = False
    assert run_compat("vllm", GpuSpec(), env, None) == []


def test_engine_dep_missing(tmp_path):
    env = _env(tmp_path, "Requires-Dist: xgrammar>=0.2.3\n", packages={"xgrammar": "0.1.0"})
    issues = run_compat("vllm", GpuSpec(), env, None)
    assert any(i.rule_id == "engine_dep_missing" and i.level == "block" for i in issues)


def test_env_var_missing_degrade(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("MODELSCOPE_CACHE", raising=False)
    env = EnvSpec.from_env(site_packages=tmp_path)
    issues = run_compat("llamacpp", GpuSpec(), env, None)
    assert any(i.rule_id == "env_var_missing" and i.level == "degrade" for i in issues)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_compat.py -v`
Expected: FAIL（规则未注册，无对应 issue）

- [ ] **Step 3: 实现（追加到 compat_rules.py）**

```python
from pathlib import Path

from modelctl.core.compat import _spec_matches


def _vllm_torch_abi_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    req = env.wheel_requires.get("vllm", {}).get("torch")
    if not req:
        return None
    installed = env.packages.get("torch")
    if installed is None or _spec_matches(req, installed):
        return None
    return CompatIssue(
        level="block",
        rule_id="vllm_torch_abi",
        reason=f"vllm 要求 torch{req}，当前已装 {installed}（ABI 不匹配）。"
        f"建议执行：uv pip install \"torch{req}\"",
    )


def _nvidia_pkg_complete_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    sp = env.site_packages
    if sp is None or not sp.is_dir():
        return None
    missing: list[str] = []
    for record in sorted(sp.glob("nvidia-*.dist-info/RECORD")):
        for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
            rel = line.split(",", 1)[0].replace("\\", "/")
            if rel.endswith(".so") or ".so." in rel:
                if rel not in env.nvidia_so:
                    missing.append(rel)
        if len(missing) >= 5:
            break
    if not missing:
        return None
    return CompatIssue(
        level="block",
        rule_id="nvidia_pkg_complete",
        reason=(
            f"检测到 nvidia 依赖包文件缺失（空壳包）：{', '.join(missing[:5])}。"
            "建议执行：uv pip install --reinstall \"nvidia-cudnn-cu13\" \"nvidia-nccl-cu13\" 等对应包。"
        ),
    )


def _cuda_lib_resolvable_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    if not env.libs_resolvable_known:
        return None
    needed: set[str] = set()
    for pkg, version in env.packages.items():
        if pkg == "nvidia-cuda-runtime":
            needed.add(f"libcudart.so.{version.split('.')[0]}")
        elif pkg.startswith("nvidia-cudnn"):
            needed.add("libcudnn.so.9")
        elif pkg.startswith("nvidia-nccl"):
            needed.add("libnccl.so.2")
    missing = sorted(n for n in needed if n not in env.cuda_libs_resolvable)
    if not missing:
        return None
    return CompatIssue(
        level="block",
        rule_id="cuda_lib_resolvable",
        reason=(
            f"CUDA 运行库无法解析：{', '.join(missing)}。"
            "请将对应 nvidia 库目录加入 LD_LIBRARY_PATH 或 /etc/ld.so.conf.d/ 后执行 ldconfig。"
        ),
    )


_DEP_MISMATCH_KEYS = ("xgrammar", "flashinfer-python", "tokenizers", "transformers", "triton")


def _engine_dep_missing_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    reqs = env.wheel_requires.get("vllm", {})
    problems: list[str] = []
    for dep in _DEP_MISMATCH_KEYS:
        req = reqs.get(dep)
        if not req:
            continue
        installed = env.packages.get(dep)
        if installed is None or _spec_matches(req, installed):
            continue
        problems.append(f"{dep}{req}（当前 {installed}）")
    if not problems:
        return None
    return CompatIssue(
        level="block",
        rule_id="engine_dep_missing",
        reason="vllm 依赖版本不匹配：" + "；".join(problems) + "。建议执行：uv pip install vllm 对应版本以对齐依赖。",
    )


_ENV_VAR_WARN = ("HF_HOME", "MODELSCOPE_CACHE")


def _env_var_missing_check(gpu: GpuSpec, env: EnvSpec, model: ModelSpec | None) -> CompatIssue | None:
    missing = [k for k in _ENV_VAR_WARN if not env.env_vars.get(k)]
    if not missing:
        return None
    return CompatIssue(
        level="degrade",
        rule_id="env_var_missing",
        reason=f"环境变量未设置：{'、'.join(missing)}（将使用默认路径）。",
    )


# _register() 内追加：
    register_rule(CompatRule(id="vllm_torch_abi", engines=("vllm",), check=_vllm_torch_abi_check))
    register_rule(CompatRule(id="nvidia_pkg_complete", engines=("vllm", "sglang"), check=_nvidia_pkg_complete_check))
    register_rule(CompatRule(id="cuda_lib_resolvable", engines=("vllm", "sglang"), check=_cuda_lib_resolvable_check))
    register_rule(CompatRule(id="engine_dep_missing", engines=("vllm",), check=_engine_dep_missing_check))
    register_rule(CompatRule(id="env_var_missing", engines=("vllm", "sglang", "llamacpp", "unsloth", "ollama"), check=_env_var_missing_check))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_compat.py -v`
Expected: PASS（39 passed）

- [ ] **Step 5: Commit**

```bash
git add src/modelctl/core/compat_rules.py tests/test_compat.py
git commit -m "feat(core): 内置软件能力规则（torch ABI / nvidia 完整性 / CUDA 库 / 依赖 / 环境变量）"
```

---

### Task 6: 引擎适配器接入 —— 基类 run_compat_checks + vllm/sglang 两段式

**Files:**
- Modify: `src/modelctl/engines/base.py`（新增基类方法）
- Modify: `src/modelctl/engines/vllm.py`（接入预检/精检，删除原 `_is_deepseek_v4`/`_cc_major` 及 DeepSeek-V4 block、FP8 block 代码）
- Modify: `src/modelctl/engines/sglang.py`（接入预检/精检）
- Modify: `tests/test_engines_vllm.py`（现有 4 个 DeepSeek-V4 测试应仍通过；FP8 用例改为由规则拦截）
- Create: `tests/test_compat_flow.py`（集成）

**Interfaces:**
- Consumes: Task 1-5 的全部框架 API。
- Produces: `EngineAdapter.run_compat_checks(model: ModelSpec | None = None) -> None` —— 基类方法，函数内延迟 import compat（避免循环依赖）；`model=None` 时按 `engine_config["model"]` 构造 id 特征 ModelSpec。
- 调用约定：vllm/sglang 在 `check_requirements` 末尾调 `self.run_compat_checks(ModelSpec.from_id(engine, model, download_id, quantization=cfg.get("quantization") or ""))`；在 `pre_start` 模型文件就位后调 `self.run_compat_checks(ModelSpec.from_local(engine, local_dir))`。

- [ ] **Step 1: 在 base.py 增加基类方法**

顶部 import 区追加（`from __future__ import annotations` 已存在，仅需 TYPE_CHECKING 声明供 mypy 解析）：

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modelctl.core.compat import ModelSpec
```

基类方法（放在 `upstream_api_key` 之后）：

```python
    def run_compat_checks(self, model: ModelSpec | None = None) -> None:
        """能力检测入口：block 抛 RequirementError，degrade 写 self.warnings。

        model 缺省时按 profile 的 model 字段构造 id 特征 ModelSpec（预检）。
        函数内延迟 import 避免 core.compat 与 engines.base 的循环依赖。
        """
        from modelctl.core.compat import EnvSpec, GpuSpec, ModelSpec, apply_compat, run_compat

        if model is None:
            model = ModelSpec.from_id(
                self.profile.engine,
                str(self.profile.engine_config.get("model") or ""),
                str((self.profile.engine_config.get("download") or {}).get("modelscope_id") or ""),
                quantization=str(self.profile.engine_config.get("quantization") or ""),
            )
        # EnvSpec 单次进程内缓存：check_requirements 探测一次，pre_start 精检复用（spec 第 5 节）
        env = getattr(self, "_compat_env", None)
        if env is None:
            env = EnvSpec.from_env()
            self._compat_env = env
        issues = run_compat(self.profile.engine, GpuSpec.from_caps(self.caps), env, model)
        apply_compat(self.profile.name, self.profile.engine, self.warnings, issues)
```

（`ModelSpec` 类型注解在 base.py 顶部使用 `from __future__ import annotations` 即可，函数内 import 供运行时用。）

- [ ] **Step 2: 迁移 vllm.py**

删除模块级 `_DEEPSEEK_V4_ARCHS`/`_DEEPSEEK_V4_NAME_MARKERS`/`_DEEPSEEK_V4_SUPPORTED_CC_MAJORS`、`_cc_major()`、`_is_deepseek_v4()` 与 `json` import；`check_requirements` 中删除原 FP8 与 DeepSeek-V4 检查段，替换为：

```python
        self.run_compat_checks()  # 预检：软件规则 + 模型 id 特征
```

`pre_start` **函数末尾**（无论模型是本地已存在还是刚下载，模型文件均已就位）追加精检：

```python
        # 精检：模型文件就位后，以 config.json 判定更精确的模型特征
        local = Path(str(cfg.get("model") or "")).expanduser()
        if local.is_dir():
            self.run_compat_checks(ModelSpec.from_local(self.profile.engine, local))
```

（注意：vllm 的 `pre_start` 中"模型已存在则直接 return"的分支在 `build_command` 前仍会执行到函数末尾——精检必须放在函数最后而非 return 之前；`ModelSpec` 从 `modelctl.core.compat` 导入。）

- [ ] **Step 3: 迁移测试 `tests/test_engines_vllm.py`**

现有 `test_vllm_deepseek_v4_unsupported_on_ada` / `test_vllm_deepseek_v4_local_config_detection` / `test_vllm_deepseek_v4_allowed_on_hopper` / `test_vllm_deepseek_v4_skips_when_cc_unknown` 保持断言不变，仅需在文件顶部 `import modelctl.core.compat_rules`（导入即注册）。`test_vllm_fp8_cc_check`（原 yaml quantization=fp8 + CC 7.5 → 拦截）改断言由规则给出：文件顶部已注册规则，预检 `ModelSpec.from_id(..., quantization="fp8")` 命中 `fp8_quant_cc`，保持 `match="8.9"` 不变。

- [ ] **Step 4: 接入 sglang.py**

`check_requirements` 末尾加 `self.run_compat_checks()`；`pre_start` 写回后加与 vllm 相同的精检（若 model 为本地目录）。

- [ ] **Step 5: 写集成测试 `tests/test_compat_flow.py`**

```python
"""能力检测两段式集成测试。"""

import modelctl.core.compat_rules  # noqa: F401 —— 导入即注册
from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import load_profile
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError

CAPS8 = Capabilities(gpu_count=8, compute_capability="8.9", binaries={"vllm": True})


def _write(tmp_path, text, name="m.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return load_profile(name[:-5], tmp_path)


def test_vllm_preflight_blocks_deepseek_v4_before_download(tmp_path):
    p = _write(
        tmp_path,
        "name: ds4\nengine: vllm\nport: 8000\nvllm:\n  model: deepseek-ai/DeepSeek-V4-Flash\n",
    )
    adapter = get_adapter("vllm")(p, CAPS8)
    try:
        adapter.check_requirements()
        assert False, "应抛 RequirementError"
    except RequirementError as e:
        assert "deepseek_v4_mhc" in str(e) and "ds4" in str(e)


def test_vllm_preflight_blocks_torch_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", "/x")
    # 虚拟 site-packages：vllm 要求 torch==2.13.0，已装 2.9.1
    import modelctl.core.compat as compat

    sp = tmp_path / "sp"
    (sp / "vllm-0.27.1.dist-info").mkdir(parents=True)
    (sp / "vllm-0.27.1.dist-info" / "METADATA").write_text(
        "Name: vllm\nVersion: 0.27.1\nRequires-Dist: torch==2.13.0\n", encoding="utf-8"
    )
    (sp / "torch-2.9.1.dist-info").mkdir(parents=True)
    (sp / "torch-2.9.1.dist-info" / "METADATA").write_text("Name: torch\nVersion: 2.9.1\n", encoding="utf-8")
    monkeypatch.setattr(compat, "_current_site_packages", lambda: sp)

    p = _write(tmp_path, "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: Qwen/Qwen3-32B\n")
    adapter = get_adapter("vllm")(p, CAPS8)
    try:
        adapter.check_requirements()
        assert False, "应抛 RequirementError"
    except RequirementError as e:
        assert "vllm_torch_abi" in str(e)


def test_vllm_post_download_precise_check(tmp_path, monkeypatch):
    # 精检：目录名不含 DeepSeek 特征（预检 is_deepseek_v4=False 放行），
    # 但本地 config.json 的 architectures 暴露 DeepSeek-V4 → pre_start 精检拦截
    model_dir = tmp_path / "m1"
    model_dir.mkdir()
    (model_dir / "config.json").write_text(
        '{"architectures": ["DeepseekV4ForCausalLM"]}', encoding="utf-8"
    )
    p = _write(
        tmp_path,
        f"name: ds4\nengine: vllm\nport: 8000\nvllm:\n  model: {model_dir}\n",
    )
    adapter = get_adapter("vllm")(p, CAPS8)
    adapter.check_requirements()  # 预检：name_hint 为路径 m1，不含 deepseek-v4 特征 → 放行
    try:
        adapter.pre_start()
        assert False, "精检应抛 RequirementError"
    except RequirementError as e:
        assert "deepseek_v4_mhc" in str(e)
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/test_engines_vllm.py tests/test_compat_flow.py -v`
Expected: PASS（原有 vllm 测试 + 新集成测试全过）

- [ ] **Step 7: Commit**

```bash
git add src/modelctl/engines/base.py src/modelctl/engines/vllm.py src/modelctl/engines/sglang.py tests/test_engines_vllm.py tests/test_compat_flow.py
git commit -m "feat(engines): vllm/sglang 接入两段式能力检测，迁移原 DeepSeek-V4/FP8 检查"
```

---

### Task 7: llamacpp / unsloth / ollama 接入软件环境规则

**Files:**
- Modify: `src/modelctl/engines/llamacpp.py`（check_requirements 末尾）
- Modify: `src/modelctl/engines/unsloth.py`（check_requirements 末尾）
- Modify: `src/modelctl/engines/ollama.py`（check_requirements 末尾）
- Test: `tests/test_engines_llamacpp.py` / `tests/test_engines_unsloth.py` / `tests/test_engines_ollama.py` 各追加一条

**Interfaces:**
- Consumes: Task 6 的 `EngineAdapter.run_compat_checks()`。
- Produces: 无新接口；三个适配器 `check_requirements` 末尾调用 `self.run_compat_checks()`（仅触发 `env_var_missing` degrade 规则，其余规则 engines 不含这些引擎）。

- [ ] **Step 1: llamacpp.py `check_requirements` 末尾（显存预检之后）追加**

```python
        self.run_compat_checks()
```

- [ ] **Step 2: unsloth.py `check_requirements` 末尾追加**

```python
        self.run_compat_checks()
```

- [ ] **Step 3: ollama.py `check_requirements` 末尾追加**

```python
        self.run_compat_checks()
```

- [ ] **Step 4: 追加测试（`tests/test_engines_ollama.py`）**

```python
def test_ollama_env_var_degrade_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("MODELSCOPE_CACHE", raising=False)
    from modelctl.core.profile import load_profile
    from modelctl.engines import get_adapter
    from modelctl.core.capabilities import Capabilities

    p = _write(tmp_path, "name: o\nengine: ollama\nport: 11434\nollama:\n  model: qwen3:8b\n")
    adapter = get_adapter("ollama")(p, Capabilities(binaries={"ollama": True}))
    adapter.check_requirements()
    assert any("[env_var_missing]" in w for w in adapter.warnings)
```

（`_write` 为各测试文件已有 helper；llamacpp/unsloth 测试同理，断言 `env_var_missing` warning 出现。）

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/test_engines_ollama.py tests/test_engines_llamacpp.py tests/test_engines_unsloth.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/modelctl/engines/llamacpp.py src/modelctl/engines/unsloth.py src/modelctl/engines/ollama.py tests/test_engines_ollama.py
git commit -m "feat(engines): llamacpp/unsloth/ollama 接入软件环境能力检测"
```

---

### Task 8: `modelctl probe` 增强 + 全量回归

**Files:**
- Modify: `src/modelctl/cli.py`（`_cmd_probe`）
- Modify: `tests/test_modelctl.py`（probe 相关测试保持/增强）

**Interfaces:**
- Consumes: Task 2 的 `EnvSpec`。
- Produces: `_cmd_probe` 输出追加软件能力摘要（不改变既有输出行）。

- [ ] **Step 1: `_cmd_probe` 末尾追加软件能力输出**

```python
    from modelctl.core.compat import EnvSpec

    env = EnvSpec.from_env()
    print(f"site-packages：{env.site_packages or '未知'}")
    print(f"已安装包：{len(env.packages)} 个")
    print(f"nvidia .so 文件：{len(env.nvidia_so)} 个")
    resolvable_note = ""
    if env.libs_resolvable_known and env.cuda_libs_resolvable:
        resolvable_note = "（" + ", ".join(sorted(env.cuda_libs_resolvable))[:120] + "）"
    print(f"CUDA 库可解析：{'是' if env.libs_resolvable_known else '未知'}{resolvable_note}")
    print("关键环境变量：")
    for key in ("HF_HOME", "MODEL_ROOT", "MODELSCOPE_CACHE"):
        print(f"  {key}={env.env_vars.get(key) or '（未设置）'}")
```

- [ ] **Step 2: 运行既有 probe 相关测试**

Run: `uv run pytest tests/test_modelctl.py -v`
Expected: PASS（若 probe 测试断言输出行数，更新断言为包含新增行）

- [ ] **Step 3: 全量回归 + 静态检查**

```bash
uv run ruff check src tests
uv run mypy src
uv run pytest -q
```

Expected: ruff 无告警；mypy 无错误；pytest 全绿（新增约 45 条，总计约 195 passed）

- [ ] **Step 4: Commit**

```bash
git add src/modelctl/cli.py tests/test_modelctl.py
git commit -m "feat(cli): probe 输出软件能力摘要"
```

---

## 验收（对照 spec 第 7 节）

- `modelctl start deepseek-v4-flash-vllm`（Ada/CC 8.9）→ `check_requirements` 预检即抛多行 block（deepseek_v4_mhc 等）+ exit 2，不等健康检查超时
- torch 版本错配 → `vllm_torch_abi` block 提前拦截
- 数据缺失（无 ldconfig / 无 nvidia / CC 未知 / METADATA 缺失）→ 不误报
- `modelctl probe` 输出硬件 + 软件能力摘要
