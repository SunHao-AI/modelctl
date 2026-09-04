#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/profiles.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : 中心侧 profile 源读取（原文 + sha256 + 路径安全，供 goal 下发）
# ===============================================================================

"""core/cluster/profiles.py — goal 下发内容的来源读取（设计文档 §6.1/§6.3）。

中心把本机 models/<engine>/<name>.yaml 的**原始文本**作为下发内容，三个理由：
1) `${VAR}` 占位符必须由 worker 本地 envfile 解析——中心若插值，API_KEY 明文会
   随 sync 进入中心 SQLite 与网络帧，违反 §10.5"密钥不出中心"；
2) `profile_sha` 取原始文本而非"safe_load 后再 dump"，避免键序/引号风格等往返
   差异被 worker 侧漂移检测误判成本地篡改；
3) 下发的 name/engine 就是 worker 侧写盘路径成分，故白名单正则 + KNOWN_ENGINES
   双闸，杜绝 `../` 穿越与任意目录写。

本模块**不 import fastapi、不碰网络**，纯文件系统读取，可脱离中心单测。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.profile import KNOWN_ENGINES, _resolve_group

#: 与 core.profile 的命名现实一致：ASCII 字母数字开头，允许 . _ -，总长 ≤ 64
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: 单份 profile YAML 上限；worker 写盘前再校验一次（双侧纵深防御）
MAX_YAML_BYTES = 256 * 1024


def is_safe_name(value: str) -> bool:
    """路径安全名（同时用于 goal_id 组成与 worker 侧写盘文件名）。"""
    return bool(value) and SAFE_NAME_RE.fullmatch(value) is not None


def profile_sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_profile_version(sha: str) -> str:
    """可读且可追溯的版本占位（Triton version policy 借鉴）：日期 + sha 前缀。"""
    return f"{_dt.date.today().isoformat()}-{sha[len('sha256:'):13]}"


def display_name_of(raw: dict[str, Any], path: Path, engine: str) -> str:
    """展示名，与 core.profile._to_profile 同口径：YAML 显式 `name:` > {group}-{engine}[-{variant}]。

    复用 `_resolve_group` 而非复制推导逻辑——展示名一旦两套口径，CLI 里能寻址的
    名字与 UI/网关展示的名字就会分裂（用户裁决 2026-09-04 条款④）。
    """
    explicit = raw.get("name")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    group = _resolve_group(raw, path)
    variant = str(raw.get("variant", "") or "")
    base = f"{group}-{engine}"
    return f"{base}-{variant}" if variant else base


def _scan(root: Path) -> tuple[list[Path], str]:
    """单次目录遍历：root 下全部 YAML **文件**，排序去重；失败返回 `([], reason)`。

    四条必须由遍历本身兜住的现实：① rglob 的 `**` 匹配零层目录，根目录那份会同时
    出现在"根候选"与递归结果里，不去重会把同一文件解析两遍、歧义清单重复列同一行；
    ② rglob 也匹配目录（手建的 `vllm/whatever.yaml/` 目录即命中），必须过滤；
    ③ 按文件名与按展示名两种寻址共用这一份结果，一次读取只遍历一次目录；
    ④ **rglob 是惰性生成器**，异常在 `sorted()` 消费时才抛出，故 try 必须包住整个
    遍历而不只是那次调用：子目录 ACL 拒绝或符号链接成环会让 `is_file()` 里的
    `stat()` 抛非忽略类 OSError（`PermissionError`/ELOOP 不在 pathlib 的忽略清单内，
    不会像 `Path.walk` 那样被静默吞掉），含 NUL/无法编码的路径抛 `ValueError`，超深
    目录树抛 `RecursionError`。本模块契约是"绝不抛异常"，漏一类 Task 5 的 goal set
    就直接 500——故按"继承树之上"兜（`Exception`）而非列举想到的几种。

    失败必须 **fail-closed**（返回空清单 + reason），绝不能把已 yield 的半份结果当作
    完整清单：半份清单会漏掉另一个引擎下的同名文件，把"歧义拒发"降级成"静默下发到
    错误引擎"，正是本模块第一条硬约束禁止的"猜"。
    """
    try:
        return sorted({p for p in root.rglob("*.yaml") if p.is_file()}), ""
    except Exception as exc:  # noqa: BLE001 — 遍历失败绝不冒泡（契约"绝不抛异常"）
        return [], f"profile 目录遍历失败（{root}）：{type(exc).__name__}: {exc}"


def _root_first(paths: list[Path], root: Path) -> list[Path]:
    """根目录那份排到最前，其余保持原路径序（稳定二次排序，不得依赖 set 迭代序）。

    文件名与展示名两种寻址必须共用同一优先级：`core.profile.load_profile` 与
    `list_profiles` 都是"根目录优先"，中心若按纯路径序取子目录那份，worker 侧
    `load_profile(展示名)` 解析到的却是根目录那份——同一个 profile 名在两侧指向
    不同文件，sha 漂移检测彻底失真。
    """
    return sorted(paths, key=lambda p: p.parent != root)


def _port_reason(value: Any) -> str:
    """port 校验与 core.profile._to_profile 同口径（int 可转且 1-65535）。

    只判"有没有 port"是不够的：worker 侧同样只判存在性，坏值会原样落盘，
    直到引擎启动 / load_profile 才炸，排查成本极高，必须在中心就拦下。
    """
    if value in (None, ""):
        return "profile 缺 port（worker 侧写盘前置校验项）"
    try:
        port = int(value)
    except (TypeError, ValueError):
        return f"port 必须是整数，当前 {value!r}"
    if not 1 <= port <= 65535:
        return f"port 必须在 1-65535，当前 {port}"
    return ""


def _load_candidate(path: Path) -> tuple[str, dict[str, Any], str] | str:
    """读取并校验单个候选文件；成功返回 (engine, raw, text)，失败返回 reason 文本。

    尺寸上限用 stat 在**读盘前**判定（超限文件绝不整份进内存）。异常兜底必须含
    UnicodeDecodeError（ValueError 子类，UTF-16/二进制残留即触发）——本模块契约是
    "绝不抛异常"，漏一层 Task 5 的 goal set 就直接 500。
    """
    try:
        if path.stat().st_size > MAX_YAML_BYTES:
            return f"profile 过大（>{MAX_YAML_BYTES} B），拒绝下发"
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"读取失败: {exc}"
    except UnicodeDecodeError as exc:
        return f"profile 文件不是 UTF-8 编码: {exc}"
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return f"YAML 语法错误: {exc}"
    if not isinstance(raw, dict):
        return "profile 顶层必须是映射"
    port_reason = _port_reason(raw.get("port"))
    if port_reason:
        return port_reason
    explicit = raw.get("engine")
    if isinstance(explicit, str) and explicit.strip():
        # 显式值**不做 lower**：与 core.profile._resolve_engine 同口径。中心若替它
        # 归一大小写，落盘的文件在 worker 上 load_profile 必炸"未知引擎"。
        engine = explicit.strip()
    else:
        engine = path.parent.name.lower()   # 目录名两侧都 lower（core.profile 同口径）
    if engine not in KNOWN_ENGINES:
        case_hint = ("；engine 值大小写敏感，请用其小写形式"
                     if engine.lower() in KNOWN_ENGINES else "")
        return (f"engine {engine!r} 不在 KNOWN_ENGINES（{sorted(KNOWN_ENGINES)}）内{case_hint}，"
                "请显式设置 engine: 或放入已知引擎子目录")
    return engine, raw, text


def _resolve(root: Path, name: str, engine: str | None = None) -> tuple[Path, str, dict[str, Any], str] | str:
    """定位 + 校验的唯一入口；成功 (path, engine, raw, text)，失败 reason。

    寻址名（用户裁决 2026-09-04 条款④）：先按**文件名**匹配，文件名候选全部未命中
    才做**展示名回退**（扫描全部 *.yaml，逐个过 `_load_candidate` 同套校验后按
    `display_name_of` 匹配）；文件名命中优先于展示名命中。成功时 `path.stem` 即
    goal 内部使用的规范文件名——goal/写盘一律文件名，镜像一致性由此保证。

    engine 决定 worker 侧写入哪个引擎子目录、由哪个引擎启动：同名文件散落在
    **多个引擎子目录**（本仓 models/*/qwen3.8.yaml 即有 8 份）时静默取排序首个
    会把模型下发到错误引擎，故拒发并列出候选；调用方用 `engine=` 显式选边
    （同引擎内重名仍按根目录优先，与 core.profile.load_profile 一致）。展示名
    命中多个引擎候选时沿用同一歧义规则。
    """
    viable: list[tuple[Path, str, dict[str, Any], str]] = []
    paths, scan_reason = _scan(root)
    if scan_reason:
        return scan_reason
    stems = _root_first([p for p in paths if p.stem == name], root)
    if stems:
        last_reason = ""
        for path in stems:
            got = _load_candidate(path)
            if isinstance(got, str):
                last_reason = got
                continue
            viable.append((path, *got))
        if not viable:
            return last_reason or f"profile {name!r} 不可用"
    else:
        # 展示名回退：展示名不是路径成分，全目录扫描无穿越风险；未通过校验的文件
        # 静默跳过——它们与本次寻址无关，其报错进 last_reason 只会误导用户
        unsafe_stems: list[str] = []
        for path in _root_first(paths, root):
            got = _load_candidate(path)
            if isinstance(got, str):
                continue
            if display_name_of(got[1], path, got[0]) != name:
                continue
            # 展示名可以任意，但归一出的 stem 是 worker 侧写盘文件名（Task 8 按
            # is_safe_name 拒收）：中心放行 = 下发一条 worker 必拒、永不收敛的 goal
            if not is_safe_name(path.stem):
                unsafe_stems.append(f"{path.relative_to(root)}（stem {path.stem!r}）")
                continue
            viable.append((path, *got))
        if not viable:
            if unsafe_stems:
                return (f"profile {name!r} 命中的文件名不是安全文件名（{'; '.join(unsafe_stems)}），"
                        "stem 是 worker 侧写盘文件名（仅允许字母数字与 . _ -），请重命名该 YAML")
            return f"profile {name!r} 不存在（models/<engine>/*.yaml 的文件名与展示名均未匹配）"
    if engine:
        wanted = engine.strip().lower()
        picked = [hit for hit in viable if hit[1] == wanted]
        if not picked:
            got_engines = ", ".join(sorted({e for _, e, _, _ in viable}))
            return (f"profile {name!r} 在 engine {wanted!r} 下不存在（该名的可用引擎：{got_engines}）")
        return picked[0]
    engines = {engine for _, engine, _, _ in viable}
    if len(engines) > 1:
        hits = ", ".join(str(p.relative_to(root)) for p, *_ in viable)
        return (f"profile {name!r} 存在歧义：多个引擎子目录都有该 profile（{hits}），"
                "engine 决定 worker 写盘目录与启动引擎，请用 --engine 指定其一")
    return viable[0]


def find_profile_path(name: str, models_dir: Path | None = None,
                      engine: str | None = None) -> tuple[Path, str] | None:
    """按 name（文件名或展示名）定位 (path, engine)；与 read 共用同一套定位/校验。"""
    if not is_safe_name(name):
        return None
    root = models_dir or PROJECT_ROOT / "models"
    if not root.is_dir():
        return None
    got = _resolve(root, name, engine)
    if isinstance(got, str):
        return None
    path, resolved, _, _ = got
    return path, resolved


def read_profile_source(name: str, models_dir: Path | None = None,
                        engine: str | None = None) -> dict[str, Any]:
    """读取下发源；一切失败返回 {"ok": False, "reason": ...}，**绝不抛异常**。

    入参 `name` 允许**文件名**或**展示名**（条款④）；成功返回的 `name` 恒为文件
    stem——goal.profile 与 worker 写盘文件名都用它，`display_name` 仅供回显。

    返回的 `yaml` 是待下发原文（含未插值占位符），`raw` 是同一文本的解析结果
    （未插值），供 gate 做 GPU 数/显存估算——刻意不走 load_profile 的插值路径，
    因为中心 .env 未必定义 worker 侧的变量（缺变量时 load_profile 会抛错）。
    """
    if not is_safe_name(name):
        return {"name": name, "ok": False, "reason": f"非法 profile 名: {name!r}（仅允许字母数字与 . _ -）"}
    root = models_dir or PROJECT_ROOT / "models"
    if not root.is_dir():
        return {"name": name, "ok": False, "reason": f"profile {name!r} 不存在（{root} 目录缺失）"}
    got = _resolve(root, name, engine)
    if isinstance(got, str):
        return {"name": name, "ok": False, "reason": got}
    path, resolved, raw, text = got
    stem = path.stem
    display = display_name_of(raw, path, resolved)
    sha = profile_sha(text)
    result: dict[str, Any] = {"name": stem, "engine": resolved, "path": str(path), "yaml": text,
                              "sha": sha, "version": default_profile_version(sha), "raw": raw, "ok": True}
    if display != stem:  # 与文件名相同时省略，避免下游回显出现 "qwen (qwen)"
        result["display_name"] = display
    return result
