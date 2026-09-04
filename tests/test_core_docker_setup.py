#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_core_docker_setup.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : Docker 环境诊断与安装模块测试
# ===============================================================================

"""core/docker_setup.py 测试（全 mock，不依赖真实 docker / root）。"""

from __future__ import annotations

import json

import pytest

from modelctl.core import docker_setup as ds


# ---- path_level_missing ----


def test_path_level_missing_all(monkeypatch):
    """docker 与 nvidia-smi 均缺失 → 两条缺失项 + 统一指引常量可用。"""
    monkeypatch.setattr(ds.shutil, "which", lambda name: None)
    missing = ds.path_level_missing()
    assert missing == [ds.MSG_DOCKER_MISSING, ds.MSG_TOOLKIT_MISSING]
    assert "modelctl env setup docker" in ds.MSG_GUIDE


def test_path_level_missing_none(monkeypatch):
    monkeypatch.setattr(ds.shutil, "which", lambda name: "/usr/bin/" + name)
    assert ds.path_level_missing() == []


def test_path_level_missing_toolkit_only(monkeypatch):
    """docker 在、nvidia-smi 不在 → 只报 toolkit。"""
    monkeypatch.setattr(
        ds.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    assert ds.path_level_missing() == [ds.MSG_TOOLKIT_MISSING]


# ---- 指引与安装步骤 ----


def test_render_instructions_uses_tuna_mirror():
    text = ds.render_instructions()
    assert ds.DOCKER_APT_MIRROR in text
    assert "download.docker.com" in text  # 官方源作为备注出现
    assert "modelctl env setup docker --run" in text
    assert "nvidia-container-toolkit" in text


def test_render_instructions_lists_mirrors():
    """缺省指引即展示内置默认多源；显式指定则展示用户列表。"""
    default_text = ds.render_instructions()
    for m in ds.DEFAULT_REGISTRY_MIRRORS:
        assert m in default_text
    custom = ds.render_instructions(["https://my.mirror"])
    assert "https://my.mirror" in custom


def test_install_steps_always_include_merge():
    """无论是否显式指定镜像，合并 registry-mirrors 步骤恒存在（默认多源）。"""
    cmds = [cmd for _, cmd in ds.install_steps()]
    assert ds.MERGE_MIRROR_CMD in cmds
    cmds2 = [cmd for _, cmd in ds.install_steps(["https://m1"])]
    assert ds.MERGE_MIRROR_CMD in cmds2


# ---- resolve_registry_mirrors ----


def test_resolve_mirrors_default():
    assert ds.resolve_registry_mirrors(None) == list(ds.DEFAULT_REGISTRY_MIRRORS)
    assert ds.resolve_registry_mirrors([]) == list(ds.DEFAULT_REGISTRY_MIRRORS)
    assert ds.resolve_registry_mirrors(["  "]) == list(ds.DEFAULT_REGISTRY_MIRRORS)


def test_resolve_mirrors_user_wins():
    """用户显式指定 → 完全以用户为准（不追加默认），去空/去重/去尾斜杠保序。"""
    got = ds.resolve_registry_mirrors(
        ["https://a.example/", "", None, "https://b.example", "https://a.example"])
    assert got == ["https://a.example", "https://b.example"]


# ---- daemon.json 合并 ----


def test_merge_daemon_json_preserves_existing(monkeypatch, tmp_path):
    target = tmp_path / "daemon.json"
    target.write_text(json.dumps({"runtimes": {"nvidia": {"path": "/usr/bin/x"}}}), encoding="utf-8")
    monkeypatch.setattr(ds, "DAEMON_JSON", target)
    assert ds._merge_daemon_json(["https://m1", "https://m2"]) is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["runtimes"]["nvidia"]["path"] == "/usr/bin/x"
    assert data["registry-mirrors"] == ["https://m1", "https://m2"]
    # 幂等：重复合并不追加、返回 False；新源追加在后面
    assert ds._merge_daemon_json(["https://m1", "https://m2"]) is False
    assert ds._merge_daemon_json(["https://m2", "https://m3"]) is True
    assert json.loads(target.read_text(encoding="utf-8"))["registry-mirrors"] == [
        "https://m1", "https://m2", "https://m3"]


def test_merge_daemon_json_creates_missing_file(monkeypatch, tmp_path):
    target = tmp_path / "docker" / "daemon.json"
    monkeypatch.setattr(ds, "DAEMON_JSON", target)
    assert ds._merge_daemon_json(["https://m1"]) is True
    assert json.loads(target.read_text(encoding="utf-8"))["registry-mirrors"] == ["https://m1"]


def test_merge_daemon_json_invalid_json(monkeypatch, tmp_path):
    target = tmp_path / "daemon.json"
    target.write_text("not-json{", encoding="utf-8")
    monkeypatch.setattr(ds, "DAEMON_JSON", target)
    assert ds._merge_daemon_json(["https://m1"]) is False
    assert target.read_text(encoding="utf-8") == "not-json{"  # 不破坏原文件


# ---- run_install 前置校验与执行 ----


def test_run_install_rejects_non_linux(monkeypatch):
    monkeypatch.setattr(ds.sys, "platform", "win32")
    assert ds.run_install() == 2


def test_run_install_rejects_non_root(monkeypatch):
    monkeypatch.setattr(ds.sys, "platform", "linux")
    monkeypatch.setattr(ds.os, "geteuid", lambda: 1000, raising=False)
    assert ds.run_install() == 2


def test_run_install_executes_steps_in_order(monkeypatch, tmp_path):
    monkeypatch.setattr(ds.sys, "platform", "linux")
    monkeypatch.setattr(ds.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(ds, "DAEMON_JSON", tmp_path / "daemon.json")
    calls: list[list[str]] = []

    class _R:
        returncode = 0

    monkeypatch.setattr(ds.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _R())
    assert ds.run_install() == 0
    # merge 步骤走 Python 合并（落 tmp），bash 步骤与 install_steps 顺序一致
    bash_cmds = [cmd for _, cmd in ds.install_steps() if cmd != ds.MERGE_MIRROR_CMD]
    assert calls == [["bash", "-c", cmd] for cmd in bash_cmds]
    merged = json.loads((tmp_path / "daemon.json").read_text(encoding="utf-8"))
    assert merged["registry-mirrors"] == list(ds.DEFAULT_REGISTRY_MIRRORS)


def test_run_install_user_mirrors_written(monkeypatch, tmp_path):
    monkeypatch.setattr(ds.sys, "platform", "linux")
    monkeypatch.setattr(ds.os, "geteuid", lambda: 0, raising=False)
    target = tmp_path / "daemon.json"
    monkeypatch.setattr(ds, "DAEMON_JSON", target)

    class _R:
        returncode = 0

    monkeypatch.setattr(ds.subprocess, "run", lambda cmd, **kw: _R())
    assert ds.run_install(["https://user.example"]) == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["registry-mirrors"] == ["https://user.example"]


def test_run_install_stops_on_first_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ds.sys, "platform", "linux")
    monkeypatch.setattr(ds.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(ds, "DAEMON_JSON", tmp_path / "daemon.json")
    n = {"i": 0}

    class _R:
        @property
        def returncode(self):
            n["i"] += 1
            return 7 if n["i"] == 2 else 0

    calls: list[list[str]] = []
    monkeypatch.setattr(ds.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _R())
    assert ds.run_install() == 7
    assert len(calls) == 2  # 第二步失败即停


# ---- diagnose ----


def test_diagnose_all_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ds.shutil, "which", lambda name: None)
    monkeypatch.setattr(ds, "DAEMON_JSON", tmp_path / "absent.json")
    checks = {c.key: c for c in ds.diagnose()}
    assert not checks["docker_cli"].ok
    assert not checks["docker_daemon"].ok
    assert not checks["nvidia_toolkit"].ok
    assert not checks["nvidia_runtime"].ok


def test_diagnose_runtime_configured(monkeypatch, tmp_path):
    target = tmp_path / "daemon.json"
    target.write_text(json.dumps({"runtimes": {"nvidia": {}}}), encoding="utf-8")
    monkeypatch.setattr(ds, "DAEMON_JSON", target)
    checks = {c.key: c for c in ds.diagnose()}
    assert checks["nvidia_runtime"].ok


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
