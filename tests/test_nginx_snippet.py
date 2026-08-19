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
    assert "~^/210/llm/deepseek-v4-flash/  http://192.168.77.210:18888;" in lines
    assert "~^/210/llm/qwen3.8/  http://192.168.77.210:11434;" in lines
    assert lines[-1] == "}"


def test_build_llm_map_rejects_unsafe_name():
    with pytest.raises(ProfileError):
        build_llm_map([Profile(name="a b", engine="vllm", port=8000)], "210", "x")
