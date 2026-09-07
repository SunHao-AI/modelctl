#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_admin_models_classify.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:24
# @Desc   : 启动失败分类接线测试
# ===============================================================================

"""_do_start/_do_restart 失败路径的分类码接线测试（不拉真引擎，全部 monkeypatch）。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from modelctl.core.webui import admin_models as am  # noqa: E402
from modelctl.core.webui.admin_tasks import Task  # noqa: E402


def _profile(engine: str = "vllm") -> SimpleNamespace:
    """_do_start 只用到 profile.name / profile.engine，鸭子类型即可。"""
    return SimpleNamespace(name=f"qwen-{engine}", engine=engine)


@pytest.fixture(autouse=True)
def _restore_event_loop():
    """asyncio.run 收尾会把主线程 policy loop 置 None，导致同次 pytest 会话里
    test_admin_tasks 的 Task.event() 走 get_event_loop() 抛 RuntimeError 被静默
    吞掉、SSE 队列拿不到事件；每个用例结束补一个新 loop 隔离该副作用。"""
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def test_do_start_exception_venv_missing_attaches_code(monkeypatch):
    import modelctl.core.all_service as all_service
    from modelctl.core.envs import EngineEnvError

    def boom(profile, caps, timeout):
        raise EngineEnvError("vllm 的专用环境未创建，请先执行：modelctl env setup vllm")

    monkeypatch.setattr(all_service, "start_profile", boom)
    task = Task(id="task-s1", kind="model_start", action="start", target="qwen-vllm")
    asyncio.run(am._do_start(_profile(), None, 1.0, task, None))
    assert task.status == "error"
    assert task.code == "venv_missing"
    assert task.engine == "vllm"


def test_do_start_error_result_unactionable_no_code(monkeypatch):
    import modelctl.core.all_service as all_service
    from modelctl.core.all_service import ComponentResult

    def fail(profile, caps, timeout):
        return ComponentResult("model:x", "error", "引擎进程提前退出")

    monkeypatch.setattr(all_service, "start_profile", fail)
    task = Task(id="task-s2", kind="model_start", action="start", target="qwen-vllm")
    asyncio.run(am._do_start(_profile(), None, 1.0, task, None))
    assert task.status == "error"
    assert task.code is None and task.engine is None


def test_do_start_requirement_error_keeps_exit_code_2(monkeypatch):
    """RequirementError（如端口占用）仍 exit_code=2，且不可操作码不挂分类。"""
    import modelctl.core.all_service as all_service
    from modelctl.engines.base import RequirementError

    def boom(profile, caps, timeout):
        raise RequirementError("端口 8101 已被占用（nginx:80）")

    monkeypatch.setattr(all_service, "start_profile", boom)
    task = Task(id="task-s3", kind="model_start", action="start", target="qwen-vllm")
    asyncio.run(am._do_start(_profile(), None, 1.0, task, None))
    assert task.exit_code == 2
    assert task.code is None and task.engine is None


def test_do_restart_exception_venv_missing_attaches_code(monkeypatch):
    import modelctl.core.all_service as all_service
    from modelctl.core.envs import EngineEnvError

    def boom(profile, caps, timeout):
        raise EngineEnvError("vllm 的专用环境未创建，请先执行：modelctl env setup vllm")

    monkeypatch.setattr(all_service, "restart_profile", boom)
    task = Task(id="task-s4", kind="model_restart", action="restart", target="qwen-vllm")
    asyncio.run(am._do_restart(_profile(), None, 1.0, task, None))
    assert task.code == "venv_missing" and task.engine == "vllm"
