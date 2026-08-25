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
