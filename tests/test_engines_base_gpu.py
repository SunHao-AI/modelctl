#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_engines_base_gpu.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 引擎基类 GPU 测试
# ===============================================================================

"""engines/base.py GPU helpers 单元测试。"""

import pytest

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import Profile
from modelctl.engines.base import EngineAdapter, RequirementError


class DummyAdapter(EngineAdapter):
    def build_command(self):
        return [], {}

    def check_requirements(self):
        pass

    def metrics_mapping(self):
        return None


def test_profile_wins_over_env(monkeypatch):
    monkeypatch.setenv("MODELCTL_GPUS", "2,3")
    profile = Profile(name="x", engine="dummy", port=1, engine_config={"gpu_list": "0,1"})
    adapter = DummyAdapter(profile, Capabilities(gpu_indices=[0, 1, 2, 3]))
    assert adapter.selected_gpus() == [0, 1]


def test_env_fallback(monkeypatch):
    monkeypatch.setenv("MODELCTL_GPUS", "4,5")
    profile = Profile(name="x", engine="dummy", port=1, engine_config={})
    adapter = DummyAdapter(profile, Capabilities(gpu_indices=[0, 1, 2, 3, 4, 5]))
    assert adapter.selected_gpus() == [4, 5]


def test_none_when_unset(monkeypatch):
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    profile = Profile(name="x", engine="dummy", port=1, engine_config={})
    adapter = DummyAdapter(profile, Capabilities(gpu_indices=[0, 1, 2, 3]))
    assert adapter.selected_gpus() is None


def test_validate_gpu_selection_raises():
    profile = Profile(name="x", engine="dummy", port=1, engine_config={"gpu_list": "0,8"})
    adapter = DummyAdapter(profile, Capabilities(gpu_indices=[0, 1, 2, 3]))
    with pytest.raises(RequirementError, match="超出可用范围"):
        adapter.validate_gpu_selection(adapter.selected_gpus())


def test_cuda_visible_devices():
    adapter = DummyAdapter(Profile(name="x", engine="dummy", port=1), Capabilities())
    assert adapter.cuda_visible_devices([0, 2]) == {"CUDA_VISIBLE_DEVICES": "0,2"}


def test_cli_parser_parses_gpus():
    from modelctl.cli import build_parser

    args = build_parser().parse_args(["start", "some-model", "--gpus", "0,1"])
    assert args.gpus == "0,1"
