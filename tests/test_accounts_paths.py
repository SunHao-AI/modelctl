#!/usr/bin/env python3
# ===============================================================================
# @File   : tests/test_accounts_paths.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/8 16:25
# @Desc   : 账号库路径与开关配置解析测试
# ===============================================================================

"""账号体系的配置底座：`accounts_db_path()` 与 `accounts_enabled()`。

二者都是"每次调用重读 os.environ"的口径（见 core/paths.py 顶部约束 3），
因此全部断言依赖 monkeypatch.setenv/delenv，不做模块级缓存假设。
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------- accounts_db_path ----------


def test_db_path_default_under_data_root(tmp_path, monkeypatch):
    """未设置 ACCOUNTS_DB_PATH → 默认 <DATA_ROOT>/modelctl_accounts.db。"""
    from modelctl.core.envfile import PROJECT_ROOT
    from modelctl.core.paths import accounts_db_path

    monkeypatch.delenv("ACCOUNTS_DB_PATH", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)

    assert accounts_db_path() == PROJECT_ROOT / "data" / "modelctl_accounts.db"


def test_db_path_absolute_env_value_wins(tmp_path, monkeypatch):
    """绝对路径 env 值原样采用。"""
    from modelctl.core.paths import accounts_db_path

    target = tmp_path / "nested" / "accounts.db"
    monkeypatch.setenv("ACCOUNTS_DB_PATH", str(target))

    assert accounts_db_path() == target


@pytest.mark.parametrize("raw", ["data/custom_accounts.db", "data\\custom_accounts.db"])
def test_db_path_relative_resolved_by_project_root(raw, monkeypatch):
    """相对值按 PROJECT_ROOT 解析，绝不跟随 CWD（与其余 *_dir() 同一口径）。"""
    from modelctl.core.envfile import PROJECT_ROOT
    from modelctl.core.paths import accounts_db_path

    monkeypatch.setenv("ACCOUNTS_DB_PATH", raw)

    assert accounts_db_path() == PROJECT_ROOT / "data" / "custom_accounts.db"


def test_db_path_blank_env_value_falls_back(monkeypatch):
    """空串/纯空白等同未设置。"""
    from modelctl.core.envfile import PROJECT_ROOT
    from modelctl.core.paths import accounts_db_path

    monkeypatch.setenv("ACCOUNTS_DB_PATH", "   ")

    assert accounts_db_path() == PROJECT_ROOT / "data" / "modelctl_accounts.db"


def test_db_path_does_not_create_parent_dir(tmp_path, monkeypatch):
    """返回路径时不建目录：只读方（CLI 查询）不该有建目录副作用。"""
    from modelctl.core.paths import accounts_db_path

    target = tmp_path / "not-created" / "accounts.db"
    monkeypatch.setenv("ACCOUNTS_DB_PATH", str(target))

    assert accounts_db_path() == target
    assert not target.parent.exists()


# ---------- accounts_enabled ----------


@pytest.mark.parametrize("raw", ["true", "True", "1", "yes", "on"])
def test_enabled_true_values(raw, monkeypatch):
    from modelctl.core.accounts import accounts_enabled

    monkeypatch.setenv("ACCOUNTS_ENABLED", raw)
    assert accounts_enabled() is True


@pytest.mark.parametrize("raw", ["false", "False", "0", "no", "off"])
def test_enabled_false_values(raw, monkeypatch):
    from modelctl.core.accounts import accounts_enabled

    monkeypatch.setenv("ACCOUNTS_ENABLED", raw)
    assert accounts_enabled() is False


@pytest.mark.parametrize("raw", [None, "", "   ", "garbage"])
def test_enabled_defaults_to_false(raw, monkeypatch):
    """未设置/空串/非法值 → False（零回归：默认关闭，行为等同现有单钥 fail-closed）。"""
    from modelctl.core.accounts import accounts_enabled

    if raw is None:
        monkeypatch.delenv("ACCOUNTS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ACCOUNTS_ENABLED", raw)
    assert accounts_enabled() is False


def test_accounts_package_importable_without_gateway_deps():
    """accounts 包可独立 import（主包未装 bcrypt/PyJWT 时不得在包级 import 崩溃）。"""
    import importlib

    mod = importlib.import_module("modelctl.core.accounts")
    assert callable(mod.accounts_enabled)
    assert isinstance(mod.accounts_db_path(), Path)
