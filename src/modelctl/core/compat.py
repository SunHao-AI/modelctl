#!/usr/bin/env python3
"""core/compat.py — 启动前能力检测框架（硬件 GpuSpec + 软件 EnvSpec + 规则库）。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from modelctl.core.capabilities import Capabilities
from modelctl.engines.base import RequirementError

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
    def from_caps(cls, caps: Capabilities) -> GpuSpec:
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
    def from_local(cls, engine: str, path: str | Path) -> ModelSpec:
        data: dict = {}
        config = Path(path).expanduser() / "config.json"
        if config.is_file():
            try:
                data = json.loads(config.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        if not isinstance(data, dict):
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
    def from_id(cls, engine: str, model_id: str, download_id: str = "", quantization: str = "") -> ModelSpec:
        return cls(
            engine=engine,
            source="id",
            quantization=quantization.lower(),
            name_hint=f"{model_id} {download_id}".strip(),
        )


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
    def from_env(cls, site_packages: Path | None = None) -> EnvSpec:
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
                    else:
                        # PEP 508 无空格形式（如 torch==2.13.0）：拆出包名与约束串
                        token = parts[0]
                        cut = next((i for i, ch in enumerate(token) if ch in "=<>!~"), -1)
                        if cut > 0:
                            reqs[token[:cut].lower()] = token[cut:]
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
            # ldconfig -p 行形如 "libcuda.so.1 (libc6,x86-64) => /lib/x86_64-linux-gnu/libcuda.so.1"，
            # 左侧可能带架构注释 token，取 => 右侧路径的 basename 最稳妥。
            names.add(Path(parts[1].strip()).name)
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
