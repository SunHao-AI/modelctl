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

    def boom(profile, caps, timeout, on_progress=None, gpus=None):
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

    def fail(profile, caps, timeout, on_progress=None, gpus=None):
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

    def boom(profile, caps, timeout, on_progress=None, gpus=None):
        raise RequirementError("端口 8101 已被占用（nginx:80）")

    monkeypatch.setattr(all_service, "start_profile", boom)
    task = Task(id="task-s3", kind="model_start", action="start", target="qwen-vllm")
    asyncio.run(am._do_start(_profile(), None, 1.0, task, None))
    assert task.exit_code == 2
    assert task.code is None and task.engine is None


def test_do_restart_exception_venv_missing_attaches_code(monkeypatch):
    import modelctl.core.all_service as all_service
    from modelctl.core.envs import EngineEnvError

    def boom(profile, caps, timeout, on_progress=None, gpus=None):
        raise EngineEnvError("vllm 的专用环境未创建，请先执行：modelctl env setup vllm")

    monkeypatch.setattr(all_service, "restart_profile", boom)
    task = Task(id="task-s4", kind="model_restart", action="restart", target="qwen-vllm")
    asyncio.run(am._do_restart(_profile(), None, 1.0, task, None))
    assert task.code == "venv_missing" and task.engine == "vllm"


# ---------------------------------------------------------------------------
# docker runtime 日志 fallback 工具函数单测（不依赖 docker daemon）
# ---------------------------------------------------------------------------

def test_launch_log_effective_container_id_only():
    """launch log 仅含容器 ID 行 + 'vllm' 短词时，应判为无效（fallback docker logs）。"""
    assert am._launch_log_effective([]) is False
    assert am._launch_log_effective(
        ["ac1cea1c5d440f54d7b294f54438fa56fda7299dcc7704309957bcc01fdb354e", "vllm"]
    ) is False
    assert am._launch_log_effective(["ac1cea1c5d440f54", ""]) is False


def test_launch_log_effective_with_real_line():
    """存在 1 行真实日志（非容器 ID、非 'vllm'）时，应判为有效。"""
    assert am._launch_log_effective(
        ["ac1cea1c5d440f54d7b294f54438fa56fda7299dcc7704309957bcc01fdb354e", "INFO: Loaded model"]
    ) is True
    assert am._launch_log_effective(["vllm", "ERROR: port 8101 in use"]) is True


def test_docker_json_line_text_stdout():
    """<id>-json.log 行（stdout）提取 log 字段并 strip 结尾换行。"""
    import json as _json

    entry = _json.dumps({"log": "INFO: vLLM starting\n", "stream": "stdout", "time": "2026-01-01T00:00:00Z"})
    assert am._docker_json_line_text(entry) == "INFO: vLLM starting"


def test_docker_json_line_text_stderr():
    """stderr 同理，提取 log。"""
    import json as _json

    entry = _json.dumps({"log": "WARNING: deprecated flag\n", "stream": "stderr", "time": "2026-01-01T00:00:01Z"})
    assert am._docker_json_line_text(entry) == "WARNING: deprecated flag"


def test_docker_json_line_text_malformed_passthrough():
    """JSON 解析失败（docker 写半行），整行透传更可读。"""
    assert am._docker_json_line_text('{"log": "ba') == '{"log": "ba'


def test_docker_json_line_text_empty():
    """空 / 缺 log 字段返回 None。"""
    import json as _json

    assert am._docker_json_line_text("") is None
    assert am._docker_json_line_text(_json.dumps({"stream": "stderr"})) is None


# ---------------------------------------------------------------------------
# 模型运行态判定 _model_summary（is_running_any）回归
# ---------------------------------------------------------------------------

def test_model_summary_uses_is_running_any(monkeypatch):
    """_model_summary 必须走 is_running_any（端口/health 2xx 探测优先 + PID 文件兜底）。

    docker runtime `start_profile` 不写 PID 文件 → is_running(name) 恒 False，
    旧版只取 PID 文件会让 docker 启动的 vllm 模型（如 qwen2.5-0.5b-vllm）列表
    与详情均误显"已停止"。此处 monkeypatch 两步抽象拼回真相：is_running_any
    直接断言返回 True（端口 health 2xx 命中），保证 _model_summary 没退化回
    is_running(name) 这样的单源判定（那会让本用例拿到 False 红）。
    """
    import modelctl.core.process as process

    # 模拟：PID 文件不存在（docker 路径），但端口 /health 2xx 可达 → is_running_any True
    monkeypatch.setattr(process, "is_running", lambda name: False, raising=True)
    monkeypatch.setattr(process, "is_running_any", lambda name, profile: True, raising=True)

    p = SimpleNamespace(
        name="qwen2.5-0.5b-vllm",
        engine="vllm",
        port=8108,
        path=None,
        yaml_path=None,
        group=None,
        display_name=None,
        api_key="test-key",
        host="127.0.0.1",
        gpu_count=None,
        cli_args=None,
        extra_env=None,
        docker_image="vllm/vllm-openai:latest",
        docker_env=None,
        engine_config={"docker_image": "vllm/vllm-openai:latest", "enforce_eager": True},
        variant=None,
        aliases=[],
    )

    # _model_summary 必须调用 is_running_any（不是 is_running(name) 单源 PID 文件）
    s = am._model_summary(p)
    # state 来自端口/health 探测 (is_running_any=True) 而非 PID 文件（is_running=False）
    assert s["state"] == "running"


def test_model_summary_falls_back_to_pid_file_when_health_probe_off(monkeypatch):
    """端口探测失败（health 非 2xx / 引擎不可达）+ 无 PID 文件 → 详情 running=False。

    is_running_any 内部"先 health 再 PID 文件"顺序由 process.py 保证；这里钉
    住「webui 走 is_running_any」这一对外契约——无论内部如何分支，
    _model_summary 都须尊重其最终返回值。
    """
    import modelctl.core.process as process

    monkeypatch.setattr(process, "is_running", lambda name: True, raising=True)
    # is_running_any 返回 False 模拟：端口没起来 + PID 文件也没了（双重"已停止"）
    monkeypatch.setattr(process, "is_running_any", lambda name, profile: False, raising=True)

    p = SimpleNamespace(
        name="qwen2.5-0.5b-vllm",
        engine="vllm",
        port=8108,
        path=None,
        yaml_path=None,
        group=None,
        display_name=None,
        api_key="test-key",
        host="127.0.0.1",
        gpu_count=None,
        cli_args=None,
        extra_env=None,
        docker_image="vllm/vllm-openai:latest",
        docker_env=None,
        engine_config={"docker_image": "vllm/vllm-openai:latest", "enforce_eager": True},
        variant=None,
        aliases=[],
    )

    s = am._model_summary(p)
    assert s["state"] == "stopped"


# ---------- build_summaries 共享 TTL 缓存（/admin/api/models 与 /overview 合流）----------


def _summary_profile(name: str) -> SimpleNamespace:
    """_model_summary 只用到这几个字段，鸭子类型即可。"""
    return SimpleNamespace(
        name=name, group=None, engine="vllm", variant=None,
        port=8108, aliases=[], api_key="k",
    )


def _fake_request(cache):
    from modelctl.core.gateway import GroupRouteCache

    assert isinstance(cache, GroupRouteCache)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(group_route_cache=cache)))


def test_build_summaries_reuses_cache_across_calls(monkeypatch):
    """两次 build_summaries 只探一轮——overview 3s 轮询与列表视图共用同一份判定。"""
    import modelctl.core.process as process
    from modelctl.core.gateway import GroupRouteCache

    calls = []
    monkeypatch.setattr(process, "is_running_any",
                        lambda name, profile: calls.append(name) or True, raising=True)
    req = _fake_request(GroupRouteCache(2.0, avail_ttl=600.0))
    profiles = [_summary_profile("a"), _summary_profile("b")]

    first = asyncio.run(am.build_summaries(req, profiles))
    second = asyncio.run(am.build_summaries(req, profiles))

    assert [s["state"] for s in first] == ["running", "running"]
    assert [s["state"] for s in second] == ["running", "running"]
    assert calls == ["a", "b"]  # 第二次零探测


def test_build_summaries_uses_injected_executor(monkeypatch):
    """overview 的 64-worker 大池必须透传到探测派发（退回默认池即旧 11s 排队回归）。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import modelctl.core.process as process
    from modelctl.core.gateway import GroupRouteCache

    seen_threads = []

    def probe(name, profile):
        seen_threads.append(threading.current_thread().name)
        return True

    monkeypatch.setattr(process, "is_running_any", probe, raising=True)
    req = _fake_request(GroupRouteCache(2.0, avail_ttl=600.0))

    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="probe-test")
    try:
        asyncio.run(am.build_summaries(req, [_summary_profile("a")], executor=pool))
    finally:
        pool.shutdown(wait=True)

    assert seen_threads and all(n.startswith("probe-test") for n in seen_threads), (
        f"探测未走注入的 executor：{seen_threads}"
    )


def test_build_summaries_zero_avail_ttl_probes_every_call(monkeypatch):
    """avail 缓存关闭时每次调用都实探（排障口径）。"""
    import modelctl.core.process as process
    from modelctl.core.gateway import GroupRouteCache

    calls = []
    monkeypatch.setattr(process, "is_running_any",
                        lambda name, profile: calls.append(name) or False, raising=True)
    req = _fake_request(GroupRouteCache(2.0, avail_ttl=0))
    profiles = [_summary_profile("a")]

    asyncio.run(am.build_summaries(req, profiles))
    second = asyncio.run(am.build_summaries(req, profiles))

    assert calls == ["a", "a"]
    assert second[0]["state"] == "stopped"
