#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_envwrite.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : envfile.set_env_values .env 定点写回测试
# ===============================================================================
"""set_env_values：已存在 key 原地替换、注释与顺序保留、新 key 追加。"""
from __future__ import annotations

from pathlib import Path

from modelctl.core.envfile import parse_env_file, set_env_values


def test_replace_existing_key_preserves_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# 头注释\nAPI_KEY=abc\n# 中间注释\nWEBUI_PORT=4173\n", encoding="utf-8")
    set_env_values({"API_KEY": "xyz"}, env_path=env)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# 头注释"          # 注释保留
    assert lines[1] == "API_KEY=xyz"       # 原地替换
    assert lines[2] == "# 中间注释"
    assert lines[3] == "WEBUI_PORT=4173"   # 其余行不动


def test_append_new_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    set_env_values({"B": "2", "C": "3"}, env_path=env)
    assert parse_env_file(env) == {"A": "1", "B": "2", "C": "3"}


def test_create_missing_file(tmp_path: Path) -> None:
    env = tmp_path / "sub" / ".env"
    env.parent.mkdir()
    set_env_values({"K": "v"}, env_path=env)
    assert parse_env_file(env) == {"K": "v"}


def test_commented_key_untouched_and_appended(tmp_path: Path) -> None:
    """被注释的旧值（#KEY=old）不算存在：保留注释行，另起一行新值。"""
    env = tmp_path / ".env"
    env.write_text("#CLUSTER_ROLE=solo\n", encoding="utf-8")
    set_env_values({"CLUSTER_ROLE": "worker"}, env_path=env)
    assert parse_env_file(env) == {"CLUSTER_ROLE": "worker"}
    assert "#CLUSTER_ROLE=solo" in env.read_text(encoding="utf-8")
