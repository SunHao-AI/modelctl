#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/engines/__init__.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 引擎注册表
# ===============================================================================

"""engines/__init__.py — 引擎注册表。"""

from __future__ import annotations

from modelctl.core.profile import ProfileError
from modelctl.engines.base import EngineAdapter
from modelctl.engines.llamacpp import LlamaCppAdapter
from modelctl.engines.ollama import OllamaAdapter
from modelctl.engines.sglang import SglangAdapter
from modelctl.engines.aphrodite import AphroditeAdapter
from modelctl.engines.lmdeploy import LmdeployAdapter
from modelctl.engines.tensorrt_llm import TensorRtLlmAdapter
from modelctl.engines.tokenspeed import TokenSpeedAdapter
from modelctl.engines.unsloth import UnslothAdapter
from modelctl.engines.vllm import VllmAdapter

_REGISTRY: dict[str, type[EngineAdapter]] = {
    "llamacpp": LlamaCppAdapter,
    "ollama": OllamaAdapter,
    "vllm": VllmAdapter,
    "sglang": SglangAdapter,
    "unsloth": UnslothAdapter,
    "aphrodite": AphroditeAdapter,
    "lmdeploy": LmdeployAdapter,
    "tensorrt_llm": TensorRtLlmAdapter,
    "tokenspeed": TokenSpeedAdapter,
}


def get_adapter(engine: str) -> type[EngineAdapter]:
    try:
        return _REGISTRY[engine]
    except KeyError:
        raise ProfileError(f"引擎未实现：{engine}（已实现：{sorted(_REGISTRY)}）") from None
