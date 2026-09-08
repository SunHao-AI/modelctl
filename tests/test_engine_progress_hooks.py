#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_engine_progress_hooks.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/8 10:00
# @Desc   : 适配器进度钩子 set_progress_sink / log_tee_cmd 测试
# ===============================================================================

"""适配器进度钩子：默认 None、docker 子类返回 tee 命令、pre_start 透传 ensure_image。"""
from __future__ import annotations

from unittest import mock

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import Profile
from modelctl.engines.tensorrt_llm import TensorRtLlmAdapter
from modelctl.engines.tokenspeed import TokenSpeedAdapter
from modelctl.engines.vllm import VllmAdapter

CAPS = Capabilities(gpu_count=1, compute_capability="8.9", binaries={})


def _profile(name="q", engine="vllm"):
    return Profile(name=name, engine=engine, port=8000,
                   engine_config={"model": "/m/x", "docker_image": "img:tag"})


def test_default_hooks_none():
    a = VllmAdapter(_profile(), CAPS)
    with mock.patch.object(a, "_resolve_runtime", return_value=("venv", None, None)):
        assert a.log_tee_cmd() is None
    assert a._progress_cb is None
    assert a.progress_cb is None


def test_docker_log_tee_cmd():
    a = VllmAdapter(_profile(), CAPS)
    with mock.patch.object(a, "_resolve_runtime", return_value=("docker", "img:tag", None)):
        assert a.log_tee_cmd() == ["docker", "logs", "-f", "--tail", "all", "q-vllm"]


def test_docker_log_tee_cmd_tokenspeed_and_trtllm():
    """三 docker 适配器同构覆盖：容器名用各自 property，勿硬编码。"""
    ts = TokenSpeedAdapter(_profile(name="q", engine="tokenspeed"), CAPS)
    with mock.patch.object(ts, "_resolve_runtime", return_value=("docker", "img:tag", None)):
        assert ts.log_tee_cmd() == ["docker", "logs", "-f", "--tail", "all", "q-tokenspeed"]
    trt = TensorRtLlmAdapter(_profile(name="q", engine="tensorrt_llm"), CAPS)
    with mock.patch.object(trt, "_resolve_runtime", return_value=("docker", "img:tag")):
        assert trt.log_tee_cmd() == ["docker", "logs", "-f", "--tail", "all", "q-trtllm"]
    # 非 docker runtime 一律 None
    with mock.patch.object(trt, "_resolve_runtime", return_value=("venv", None)):
        assert trt.log_tee_cmd() is None


def test_log_fallback_cmd_docker_and_venv():
    """F8：docker 三引擎 log_fallback_cmd = `docker logs --tail 50 <container>`；venv 一律 None。"""
    a = VllmAdapter(_profile(), CAPS)
    with mock.patch.object(a, "_resolve_runtime", return_value=("docker", "img:tag", None)):
        assert a.log_fallback_cmd() == ["docker", "logs", "--tail", "50", "q-vllm"]
    with mock.patch.object(a, "_resolve_runtime", return_value=("venv", None, None)):
        assert a.log_fallback_cmd() is None
    ts = TokenSpeedAdapter(_profile(name="q", engine="tokenspeed"), CAPS)
    with mock.patch.object(ts, "_resolve_runtime", return_value=("docker", "img:tag", None)):
        assert ts.log_fallback_cmd() == ["docker", "logs", "--tail", "50", "q-tokenspeed"]
    trt = TensorRtLlmAdapter(_profile(name="q", engine="tensorrt_llm"), CAPS)
    with mock.patch.object(trt, "_resolve_runtime", return_value=("docker", "img:tag")):
        assert trt.log_fallback_cmd() == ["docker", "logs", "--tail", "50", "q-trtllm"]
    with mock.patch.object(trt, "_resolve_runtime", return_value=("venv", None)):
        assert trt.log_fallback_cmd() is None


def test_pre_start_forwards_progress_to_ensure_image():
    a = VllmAdapter(_profile(), CAPS)
    seen = []
    a.set_progress_sink(lambda lab, pct: seen.append(lab))
    assert a.progress_cb is not None
    with mock.patch.object(a, "_resolve_runtime", return_value=("docker", "img:tag", None)), \
         mock.patch("modelctl.engines.vllm.docker_setup.ensure_image", return_value=True) as ei, \
         mock.patch("modelctl.engines.vllm.Path.is_dir", return_value=True), \
         mock.patch("modelctl.engines.vllm.Path.is_file", return_value=False):
        a.pre_start()
    kw = ei.call_args.kwargs
    assert "on_progress" in kw
    kw["on_progress"]("拉取中", 0.5)
    assert seen == ["拉取中"]
