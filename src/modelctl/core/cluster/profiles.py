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
from modelctl.core.profile import KNOWN_ENGINES

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


def find_profile_path(name: str, models_dir: Path | None = None) -> tuple[Path, str] | None:
    """按 name 定位 (path, engine)。查找顺序与 core.profile.load_profile 一致。

    engine 判定：YAML 显式 `engine:` 优先，否则取父目录名；两者都必须落在
    KNOWN_ENGINES 内——engine 决定 worker 侧写入哪个引擎子目录，猜错会让模型
    加载到错误引擎，宁可拒发。
    """
    root = models_dir or PROJECT_ROOT / "models"
    if not root.is_dir():
        return None
    for path in [root / f"{name}.yaml", *sorted(root.rglob(f"{name}.yaml"))]:
        if not path.is_file():
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue  # 交由 read_profile_source 给出具体 reason
        engine = raw.get("engine") if isinstance(raw, dict) else None
        engine = engine.strip().lower() if isinstance(engine, str) and engine.strip() else path.parent.name.lower()
        if engine in KNOWN_ENGINES:
            return path, engine
    return None


def read_profile_source(name: str, models_dir: Path | None = None) -> dict[str, Any]:
    """读取下发源；一切失败返回 {"ok": False, "reason": ...}，**绝不抛异常**。

    返回的 `yaml` 是待下发原文（含未插值占位符），`raw` 是同一文本的解析结果
    （未插值），供 gate 做 GPU 数/显存估算——刻意不走 load_profile 的插值路径，
    因为中心 .env 未必定义 worker 侧的变量（缺变量时 load_profile 会抛错）。
    """
    if not is_safe_name(name):
        return {"name": name, "ok": False, "reason": f"非法 profile 名: {name!r}（仅允许字母数字与 . _ -）"}
    root = models_dir or PROJECT_ROOT / "models"
    if not root.is_dir():
        return {"name": name, "ok": False, "reason": f"profile {name!r} 不存在（{root} 目录缺失）"}

    # 先按文件定位，再判定 engine，以便对"engine 未知"给出精确 reason（而非笼统"不存在"）
    candidates = [p for p in [root / f"{name}.yaml", *sorted(root.rglob(f"{name}.yaml"))] if p.is_file()]
    if not candidates:
        return {"name": name, "ok": False,
                "reason": f"profile {name!r} 不存在（models/<engine>/{name}.yaml 未找到）"}

    last_reason = ""
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            last_reason = f"读取失败: {exc}"
            continue
        if len(text.encode("utf-8")) > MAX_YAML_BYTES:
            last_reason = f"profile 过大（>{MAX_YAML_BYTES} B），拒绝下发"
            continue
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            last_reason = f"YAML 语法错误: {exc}"
            continue
        if not isinstance(raw, dict):
            last_reason = "profile 顶层必须是映射"
            continue
        if raw.get("port") in (None, ""):
            last_reason = "profile 缺 port（worker 侧写盘前置校验项）"
            continue
        explicit = raw.get("engine")
        engine = (explicit.strip().lower() if isinstance(explicit, str) and explicit.strip()
                  else path.parent.name.lower())
        if engine not in KNOWN_ENGINES:
            last_reason = (f"engine {engine!r} 不在 KNOWN_ENGINES（{sorted(KNOWN_ENGINES)}）内，"
                           "请显式设置 engine: 或放入已知引擎子目录")
            continue
        sha = profile_sha(text)
        return {"name": name, "engine": engine, "path": str(path), "yaml": text,
                "sha": sha, "version": default_profile_version(sha), "raw": raw, "ok": True}
    return {"name": name, "ok": False, "reason": last_reason or f"profile {name!r} 不可用"}
