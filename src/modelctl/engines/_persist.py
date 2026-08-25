#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/engines/_persist.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 模型路径写回 profile
# ===============================================================================

"""engines/_persist.py — 将下载后的本地 model 路径写回 profile YAML。"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_MODEL_LINE_RE = re.compile(r"^(\s*)model:(.*)$")


def persist_model_path(profile_path: Path, engine: str, model_path: str) -> None:
    """仅更新 YAML 中 <engine>.model 字段，写回前备份原文件为 .yaml.bak。

    文本级替换 model 行的值，保留原文件注释、缩进与其他格式；
    仅校验 YAML 可解析且 <engine> 段为映射。下载成功后才调用。
    """
    original = profile_path.read_text(encoding="utf-8")
    raw = yaml.safe_load(original)
    if not isinstance(raw, dict):
        raise ValueError(f"{profile_path} 顶层必须是映射")
    engine_config = raw.get(engine)
    if not isinstance(engine_config, dict):
        raise ValueError(f"{profile_path} 中 {engine} 段必须是映射")

    backup = profile_path.with_name(profile_path.name + ".bak")
    backup.write_text(original, encoding="utf-8")

    lines = original.splitlines(keepends=True)
    out: list[str] = []
    in_engine = False
    for line in lines:
        # 顶层键（缩进 0）切换所在段；profile 顶层为映射
        if line.strip() and not line[0].isspace():
            in_engine = line.split(":", 1)[0].strip() == engine
        m = _MODEL_LINE_RE.match(line)
        if in_engine and m:
            indent = m.group(1)
            _, sep, comment = m.group(2).partition("#")
            new = f"{indent}model: {model_path}"
            if sep and comment.strip():
                new += f"  # {comment.strip()}"
            out.append(new + "\n")
            continue
        out.append(line)
    profile_path.write_text("".join(out), encoding="utf-8")
