#!/usr/bin/env python3
# ===============================================================================
# @File   : tests/test_timezone.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 15:35
# @Desc   : 进程/子进程/容器时区统一单元测试
# ===============================================================================

"""core/timezone.py 单元测试。

POSIX 上 tzset() 改的是进程级 C 时区状态，monkeypatch 只还原 os.environ，
故用例结束后按还原后的 TZ 再 tzset() 一次。

平台分支靠 monkeypatch 增删 `time.tzset` 覆盖，Windows 开发机与 Linux CI 都能
跑到两侧分支；只有"时钟真的对齐"这一条依赖真实 tzset，故 skipif 限定 POSIX。
"""

from __future__ import annotations

import os
import time

import pytest

from modelctl.core import timezone as tz

POSIX_ONLY = pytest.mark.skipif(not hasattr(time, "tzset"), reason="需要真实 time.tzset 才能改本地时区")

# 模块导入期抓住真实 tzset：fixture teardown 早于 monkeypatch 还原，
# 届时 time.tzset 可能仍是用例注入的假函数，调它无法复原 C 时区。
_REAL_TZSET = getattr(time, "tzset", None)


@pytest.fixture(autouse=True)
def _restore_tz(monkeypatch):
    """用例结束后把 TZ 与 C 级本地时区一并还原。"""
    original = os.environ.get("TZ")
    yield
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    if _REAL_TZSET is not None:
        _REAL_TZSET()


@pytest.fixture()
def fake_tzset(monkeypatch) -> list[int]:
    """伪造 tzset 以便在任意平台走 POSIX 分支；返回调用记录。"""
    calls: list[int] = []
    # raising=False：Windows 上 time.tzset 本就不存在，注入后才能覆盖 POSIX 分支
    monkeypatch.setattr(time, "tzset", lambda: calls.append(1), raising=False)
    return calls


@pytest.fixture()
def no_tzset(monkeypatch):
    """删除 tzset 以模拟 Windows（Linux CI 也能覆盖该分支）。"""
    monkeypatch.delattr(time, "tzset", raising=False)


# ---------- resolve_timezone：纯解析，跨平台语义一致 ----------


def test_resolve_default_is_shanghai(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    assert tz.resolve_timezone() == tz.TZ_DEFAULT == "Asia/Shanghai"


def test_resolve_env_tz_wins(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    assert tz.resolve_timezone() == "UTC"


def test_resolve_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("TZ", "Not/AZone")
    assert tz.resolve_timezone() == tz.TZ_DEFAULT


def test_resolve_blank_uses_default(monkeypatch):
    monkeypatch.setenv("TZ", "   ")
    assert tz.resolve_timezone() == tz.TZ_DEFAULT


def test_resolve_empty_when_no_tzdb(monkeypatch):
    monkeypatch.setattr(tz, "_valid", lambda name: False)
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    assert tz.resolve_timezone() == ""


# ---------- apply_timezone：POSIX 分支 ----------


def test_apply_writes_env_and_calls_tzset(fake_tzset, monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    assert tz.apply_timezone() == tz.TZ_DEFAULT
    assert os.environ["TZ"] == tz.TZ_DEFAULT
    assert fake_tzset, "必须真正调用 tzset，否则本进程时间不会变"


def test_apply_invalid_tz_falls_back(fake_tzset, monkeypatch):
    monkeypatch.setenv("TZ", "Not/AZone")
    assert tz.apply_timezone() == tz.TZ_DEFAULT
    assert os.environ["TZ"] == tz.TZ_DEFAULT


@POSIX_ONLY
def test_apply_really_aligns_local_clock(monkeypatch):
    """tzset 生效的硬证据：本地 UTC 偏移与 ZoneInfo 一致（而非只改了环境变量）。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setenv("TZ", "Asia/Shanghai")
    tz.apply_timezone()
    assert datetime.now().astimezone().utcoffset() == datetime.now(ZoneInfo("Asia/Shanghai")).utcoffset()


# ---------- apply_timezone：无 tzset 平台（Windows）----------


def test_apply_without_tzset_returns_empty_and_keeps_env(no_tzset, monkeypatch):
    """UCRT 把 IANA 名解析成 +0100，会污染所有子进程 → 无 tzset 时绝不采用该值。"""
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    assert tz.apply_timezone() == ""
    assert os.environ["TZ"] == "Asia/Shanghai"  # 用户原值不动，但未被当作生效值


def test_subprocess_env_empty_without_tzset(no_tzset, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    assert tz.subprocess_timezone() == {}


# ---------- subprocess_timezone：子进程显式兜底 ----------


def test_subprocess_tz_defaults_to_shanghai(fake_tzset, monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    assert tz.subprocess_timezone() == {"TZ": "Asia/Shanghai"}


def test_subprocess_tz_respects_env(fake_tzset, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    assert tz.subprocess_timezone() == {"TZ": "UTC"}


# ---------- container_timezone_args：容器只认 -e，与宿主平台无关 ----------


def test_container_args_always_has_e_tz(no_tzset, monkeypatch):
    """宿主是 Windows 也要注入——docker daemon 是 Linux，容器时区只认 -e。"""
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    assert tz.container_timezone_args()[0:2] == ["-e", "TZ=Asia/Shanghai"]


def test_container_args_explicit_tz_overrides_env(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    assert tz.container_timezone_args("America/New_York")[0:2] == ["-e", "TZ=America/New_York"]


def test_container_args_mounts_localtime_when_found(monkeypatch, tmp_path):
    """能定位宿主机 tz 文件 → 额外挂 /etc/localtime，避免镜像缺 tzdata 时静默 UTC。"""
    (tmp_path / "Asia").mkdir(parents=True)
    tzfile = tmp_path / "Asia" / "Shanghai"
    tzfile.write_bytes(b"fake-tzdata")
    monkeypatch.setattr("zoneinfo.TZPATH", (str(tmp_path),))
    assert tz.container_timezone_args("Asia/Shanghai")[2:4] == ["-v", f"{tzfile.as_posix()}:/etc/localtime:ro"]


def test_container_args_skips_mount_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("zoneinfo.TZPATH", (str(tmp_path / "nowhere"),))
    assert tz.container_timezone_args("Asia/Shanghai") == ["-e", "TZ=Asia/Shanghai"]


def test_container_args_empty_when_no_tzdb(monkeypatch):
    """tz 库完全不可用时不注入任何参数（时区是展示问题，不阻断启动）。"""
    monkeypatch.setattr(tz, "_valid", lambda name: False)
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    assert tz.container_timezone_args() == []
