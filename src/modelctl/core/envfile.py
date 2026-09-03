#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/envfile.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : .env 解析与注入
# ===============================================================================

"""core/envfile.py — .env 解析与注入（优先级：已存在环境变量 > .env）。"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value
    return values


def load_env(env_path: Path | None = None) -> Path:
    path = env_path or PROJECT_ROOT / ".env"
    if not path.is_file():
        return path
    for key, value in parse_env_file(path).items():
        os.environ.setdefault(key, value)
    return path


def set_env_values(values: dict[str, str], env_path: Path | None = None) -> Path:
    """把 values 定点写回 .env：已存在的 key 行原地替换，其余行保留，缺失 key 追加。

    被注释掉的行（#KEY=…）不视为已存在——保留注释、另追加新行。文件不存在时创建。
    值按原样写入（不添加引号）；写后不影响当前进程 os.environ（load_env 的
    setdefault 语义决定下次进程才生效，调用方需同步 setenv 时自行处理）。
    """
    path = env_path or PROJECT_ROOT / ".env"
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(values)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    if remaining:
        if lines and lines[-1] != "":
            lines.append("")
        for key, value in remaining.items():
            lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
