#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_nginx_snippet.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : nginx 片段生成测试
# ===============================================================================

"""modelctl.core.nginx_snippet 单元测试。"""

from __future__ import annotations

import pytest

from modelctl.core.nginx_snippet import build_llm_map
from modelctl.core.profile import Profile, ProfileError


def test_build_llm_map():
    profiles = [
        Profile(name="deepseek-v4-flash", engine="llamacpp", port=18888),
        Profile(name="qwen3.8", engine="ollama", port=11434),
    ]
    out = build_llm_map(profiles, "210", "192.168.77.210")
    lines = out.splitlines()
    assert lines[0] == "map $uri $llm_model_target {"
    assert '    default "";' in lines
    assert '    ~^/210/llm/v1/  http://192.168.77.210:5003;' in lines
    assert '    ~^/210/llm/v1$  http://192.168.77.210:5003;' in lines
    assert '    ~^/210/llm/deepseek-v4-flash/  http://192.168.77.210:18888;' in lines
    assert '    ~^/210/llm/qwen3.8/  http://192.168.77.210:11434;' in lines
    assert lines[-1] == "}"


def test_build_llm_map_gateway_port():
    profiles = [Profile(name="qwen3.8", engine="vllm", port=8101)]
    out = build_llm_map(profiles, "208", "192.168.77.208", gateway_port=5003)
    assert '    ~^/208/llm/v1/  http://192.168.77.208:5003;' in out
    out = build_llm_map(profiles, "208", "192.168.77.208", gateway_port=5004)
    assert '    ~^/208/llm/v1/  http://192.168.77.208:5004;' in out


def test_build_llm_map_rejects_unsafe_name():
    with pytest.raises(ProfileError):
        build_llm_map([Profile(name="a b", engine="vllm", port=8000)], "210", "x")


def test_build_llm_map_includes_aliases():
    profiles = [
        Profile(name="deepseek-v4-flash-llamacpp", engine="llamacpp", port=18888, aliases=["deepseek-v4-flash"])
    ]
    out = build_llm_map(profiles, "210", "192.168.77.210")
    assert "    ~^/210/llm/deepseek-v4-flash-llamacpp/  http://192.168.77.210:18888;" in out
    assert "    ~^/210/llm/deepseek-v4-flash/  http://192.168.77.210:18888;" in out


def test_build_llm_map_rejects_unsafe_alias():
    with pytest.raises(ProfileError):
        build_llm_map([Profile(name="a", engine="vllm", port=8000, aliases=["bad alias"])], "210", "x")
