#!/usr/bin/env python3
# ===============================================================================
# @File   : tests/test_paths.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 18:40
# @Desc   : 运行时数据目录统一解析测试
# ===============================================================================

"""core/paths.py 单元测试：默认值口径 + 相对路径按 PROJECT_ROOT 解析。

历史缺陷（本文件即回归防线）：LOG_DIR / CACHE_DIR / USAGE_DATA_DIR / AUDIT_DIR 的默认值
散在各调用点，结果 logs 落到项目根**上级**、usage 与 cache 撞目录、audit 是**相对 CWD**
的 data/audit（从别处执行 CLI 就写错位置）。
"""

from pathlib import Path

import pytest

from modelctl.core import paths


@pytest.fixture()
def fake_root(tmp_path, monkeypatch):
    """把 PROJECT_ROOT / DATA_ROOT 指向 tmp_path，杜绝测试触碰仓库真实 data/。"""
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(paths, "PROJECT_ROOT", root)
    monkeypatch.setattr(paths, "DATA_ROOT", root / "data")
    for key in ("LOG_DIR", "CACHE_DIR", "USAGE_DATA_DIR", "AUDIT_DIR"):
        monkeypatch.delenv(key, raising=False)
    return root


@pytest.mark.parametrize(
    "func,subdir",
    [
        (paths.log_dir, "logs"),
        (paths.cache_dir, "cache"),
        (paths.usage_data_dir, "usage-data"),
        (paths.audit_dir, "audit"),
    ],
)
def test_default_under_data_root(fake_root, func, subdir):
    """四项默认全部落在 <项目根>/data/<子目录>，且互不撞目录。"""
    assert func() == fake_root / "data" / subdir


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_env_falls_back_to_default(fake_root, value):
    """env 缺失 / 空串 / 纯空白一律视为未设置。"""
    assert paths.resolve_data_dir(value, "logs") == fake_root / "data" / "logs"


def test_relative_value_resolved_against_project_root(fake_root, monkeypatch):
    """相对值按 PROJECT_ROOT 解析，与当前工作目录无关（旧实现按 CWD，从别处跑 CLI 就写错位）。"""
    elsewhere = monkeypatch_tmp_cwd(monkeypatch, fake_root)
    got = paths.resolve_data_dir("data/logs", "logs")
    assert got == fake_root / "data" / "logs"
    assert got != elsewhere / "data" / "logs"


def test_absolute_value_wins(fake_root, tmp_path):
    """绝对 env 值原样采用（挂大容量盘的场景），且不受 subdir 影响。"""
    target = tmp_path / "raid5" / "logs"
    assert paths.resolve_data_dir(str(target), "logs") == target


@pytest.mark.parametrize(
    "func,key",
    [
        (paths.log_dir, "LOG_DIR"),
        (paths.cache_dir, "CACHE_DIR"),
        (paths.usage_data_dir, "USAGE_DATA_DIR"),
        (paths.audit_dir, "AUDIT_DIR"),
    ],
)
def test_env_override_takes_effect(fake_root, monkeypatch, func, key):
    """每次调用重读 env：模块导入后设置环境变量同样生效（旧 LOCK_DIR 常量做不到）。"""
    target = fake_root / "elsewhere"
    monkeypatch.setenv(key, str(target))
    assert func() == target


@pytest.mark.parametrize("func", [paths.log_dir, paths.cache_dir, paths.usage_data_dir])
def test_readwrite_dirs_are_created(fake_root, func):
    """写入型目录取用时幂等创建。"""
    d = func()
    assert d.is_dir()
    func()  # 幂等


def test_audit_dir_does_not_create(fake_root):
    """只读的 audit_dir 不建目录：写入方 RequestAuditLog 落盘前自行 mkdir。"""
    d = paths.audit_dir()
    assert not d.exists()


def monkeypatch_tmp_cwd(monkeypatch, fake_root) -> Path:
    """把 CWD 切到与 PROJECT_ROOT 无关的临时目录，返回该目录。"""
    elsewhere = fake_root.parent / "cwd"
    elsewhere.mkdir(exist_ok=True)
    monkeypatch.chdir(elsewhere)
    return elsewhere
