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

from modelctl.core.nginx_snippet import build_client_auth_map, build_llm_map
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


def test_build_client_auth_map_gateway_group_only_client_key():
    """网关 location 的白名单必须只含 client_key：与网关自身校验口径完全一致，
    否则 profile key 过了 nginx 却被网关 401，表现为无法解释的配置矛盾。"""
    out = build_client_auth_map("sk-abc123", extra_keys=["profile-key-1"])
    gw = out.split("map $http_authorization $llm_bearer_gw")[1].split("map $http_x_api_key $llm_xkey_gw")[0]
    assert '"Bearer sk-abc123" 1;' in gw
    assert "profile-key-1" not in gw


def test_build_client_auth_map_all_group_includes_profile_keys():
    """直连/用量 location 的白名单需同时含两把 key：直连 location 不改写 Authorization，
    引擎只认 profile key；只放行 client_key 会让既有直连客户端全断。"""
    out = build_client_auth_map("sk-abc123", extra_keys=["profile-key-1"])
    assert '"Bearer profile-key-1" 1;' in out
    assert '"profile-key-1" 1;' in out


def test_build_client_auth_map_emits_reject_pairs():
    out = build_client_auth_map("sk-abc123")
    assert 'map "$llm_bearer_gw$llm_xkey_gw" $llm_reject' in out
    assert 'map "$llm_bearer_all$llm_xkey_all" $llm_reject_all' in out
    assert 'default 1;' in out
    assert out.count('"11" 0;') == 2  # 两组各一个放行组合表


def test_build_client_auth_map_dedups_and_skips_empty_extra_keys():
    out = build_client_auth_map("sk-abc123", extra_keys=["sk-abc123", "", "  "])
    assert out.count('"Bearer sk-abc123" 1;') == 2  # gw 与 all 各一次，不因重复而多写


def test_build_client_auth_map_rejects_empty_key():
    with pytest.raises(ProfileError):
        build_client_auth_map("")


def test_build_client_auth_map_rejects_quote_in_key():
    """key 含双引号会破坏 nginx 配置解析，必须拒绝而非静默产出错误配置。"""
    with pytest.raises(ProfileError):
        build_client_auth_map('sk-bad"key')
