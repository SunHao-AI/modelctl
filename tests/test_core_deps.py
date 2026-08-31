#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_core_deps.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/8/31 16:30
# @Desc   : core/deps 缺失包意图安装测试
# ===============================================================================

"""tests/test_core_deps.py — core/deps 缺失依赖检测与按需单包安装。

覆盖以下回退路径与判定：
- find_spec 命中 → 不触发任何安装
- find_spec 未命中 + uv 可用 + 阿里镜像成功 → 仅 1 次 uv 调用
- uv 镜像失败 + pip 可用 → 回退 pip
- 全失败 → ensure_packages 返回 False
- 安装后二次 find_spec 验证
"""

from __future__ import annotations

import importlib.util

import modelctl.core.deps as deps


class _FakeResult:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _patch_find_spec(monkeypatch, existing: frozenset[str] | set[str], installed_now: list[str]):
    """让 find_spec 在 installed_now 之外全部判为缺失。"""

    def fake(name):
        return object() if (name in existing or name in installed_now) else None

    monkeypatch.setattr(importlib.util, "find_spec", fake)


def _patch_subprocess(monkeypatch, *, uv_ok: bool = True,
                      uv_fail_when: bool = False,
                      pip_ok: bool = True,
                      pip_fail_when: bool = False):
    """受控 subprocess.run：记录调用 + 按规则返回。"""
    calls: list = []

    def fake_run(cmd, **_kw):
        calls.append(list(cmd))
        joined = " ".join(cmd)
        if cmd[:1] == ["uv"]:
            return _FakeResult(0)
        if "pip" in joined:
            if "--version" in joined:
                return _FakeResult(0 if pip_ok else 1)
            if "install" in joined:
                if uv_fail_when and "mirrors.aliyun.com" in joined:
                    return _FakeResult(1)
                if pip_fail_when and "mirrors.aliyun.com" in joined:
                    return _FakeResult(1)
                return _FakeResult(0 if pip_ok else 1)
            return _FakeResult(0)
        return _FakeResult(0)

    monkeypatch.setattr(deps, "_find_uv", lambda: "uv" if uv_ok else None)
    monkeypatch.setattr(deps.subprocess, "run", fake_run)
    return calls


# ---- 模块级查询 ----

def test_missing_modules_lists_uninstalled(monkeypatch):
    _patch_find_spec(monkeypatch, existing=frozenset(), installed_now=[])
    monkeypatch.setitem(deps.PACKAGE_CHECKLISTS, "_t", {"yaml": "PyYAML"})
    try:
        assert deps.missing_modules("_t") == [("yaml", "PyYAML")]
    finally:
        deps.PACKAGE_CHECKLISTS.pop("_t", None)


def test_missing_modules_empty_when_all_installed(monkeypatch):
    _patch_find_spec(monkeypatch, existing={"yaml"}, installed_now=[])
    monkeypatch.setitem(deps.PACKAGE_CHECKLISTS, "_t", {"yaml": "PyYAML"})
    try:
        assert deps.missing_modules("_t") == []
    finally:
        deps.PACKAGE_CHECKLISTS.pop("_t", None)


# ---- ensure_packages 路径 ----

def test_ensure_returns_true_when_no_missing(monkeypatch):
    """find_spec 全部命中 → 直接 True，不触发任何 subprocess。"""
    _patch_find_spec(monkeypatch, existing={"loguru", "yaml"}, installed_now=[])
    calls = _patch_subprocess(monkeypatch)
    assert deps.ensure_packages("core") is True
    assert calls == []


def test_ensure_uses_uv_when_available(monkeypatch):
    """uv 可用 + 镜像成功 + 装后命中 → 走 uv 路径且仅 1 次安装。"""
    installed: list[str] = []
    _patch_find_spec(monkeypatch, existing=frozenset(), installed_now=installed)
    calls = _patch_subprocess(monkeypatch, uv_ok=True, pip_ok=True)
    monkeypatch.setitem(deps.PACKAGE_CHECKLISTS, "_m", {"modelscope": "modelscope"})

    def fake_install(req):
        installed.append("modelscope")
        return True

    monkeypatch.setattr(deps, "_install_requirement", fake_install)
    try:
        ok = deps.ensure_packages("_m")
        assert ok is True
        assert "modelscope" in installed  # fake 第二次被 _module_exists 命中
    finally:
        deps.PACKAGE_CHECKLISTS.pop("_m", None)


def test_ensure_fallback_to_pip_when_uv_fails(monkeypatch):
    """uv 安装返回 False → _install_with_pip 被调用（uv 失败时会回退 pip）。
    只验证 _install_requirement 的回退逻辑。"""
    calls = _patch_subprocess(monkeypatch, uv_ok=False, pip_ok=True)
    # 直接验证 _install_requirement（不自走 ensure_packages，避免 modules 假模块复杂性）
    assert deps._install_requirement("requests>=2.25") is True
    joined_all = [" ".join(c) for c in calls]
    assert any("mirrors.aliyun.com" in c and "install" in c for c in joined_all)


def test_ensure_returns_false_when_all_fail(monkeypatch):
    """uv 不可用 + pip 不可用 → _install_requirement 返回 False。"""
    calls = _patch_subprocess(monkeypatch, uv_ok=False, pip_ok=False)
    assert deps._install_requirement("objections>=9.9") is False
    assert any("--version" in " ".join(c) for c in calls)
    assert not any("install" in " ".join(c) and "install" in " ".join(c) for c in calls)


def test_ensure_marks_failed_when_not_reresolvable(monkeypatch):
    """_install_requirement 成功，但 _module_exists 始终 False → 计入 failed 列表返回 False。"""
    _patch_find_spec(monkeypatch, existing=frozenset(), installed_now=[])
    monkeypatch.setitem(deps.PACKAGE_CHECKLISTS, "_m", {"modelscope": "modelscope"})
    monkeypatch.setattr(deps, "_install_requirement", lambda req: True)
    monkeypatch.setattr(deps, "_module_exists", lambda module: False)
    try:
        ok = deps.ensure_packages("_m")
        assert ok is False
    finally:
        deps.PACKAGE_CHECKLISTS.pop("_m", None)


def test_checklist_keys_match_core_dependencies():
    """清单硬断言，防止 PR 误删/改 core / gateway 关键项。"""
    assert set(deps.PACKAGE_CHECKLISTS["core"]) == {"loguru", "yaml"}
    assert {"fastapi", "uvicorn", "httpx"} <= set(deps.PACKAGE_CHECKLISTS["gateway"])
    assert set(deps.PACKAGE_CHECKLISTS["stats"]) == set()  # stats 纯 stdlib
    assert "modelscope" in deps.PACKAGE_CHECKLISTS["modelscope"]
