"""引擎适配器 upstream_model_name() 单元测试（网关模型名改写依据）。"""

from __future__ import annotations

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import Profile
from modelctl.engines import get_adapter


def _profile(name: str, engine: str, port: int, engine_config: dict | None = None) -> Profile:
    return Profile(name=name, engine=engine, port=port, engine_config=engine_config or {})


def test_upstream_model_ollama():
    adapter = get_adapter("ollama")(_profile("qwen3.8", "ollama", 11434, {"model": "qwen3.8:27b"}), Capabilities())
    assert adapter.upstream_model_name() == "qwen3.8:27b"


def test_upstream_model_llamacpp_uses_profile_name():
    adapter = get_adapter("llamacpp")(_profile("deepseek-v4-flash", "llamacpp", 18888), Capabilities())
    assert adapter.upstream_model_name() == "deepseek-v4-flash"


def test_upstream_model_vllm_config_or_name():
    vllm = get_adapter("vllm")(_profile("qwen3.8", "vllm", 8000, {"model": "Qwen/Qwen3.8-27B"}), Capabilities())
    assert vllm.upstream_model_name() == "Qwen/Qwen3.8-27B"
    vllm_empty = get_adapter("vllm")(_profile("qwen3.8", "vllm", 8000), Capabilities())
    assert vllm_empty.upstream_model_name() == "qwen3.8"
