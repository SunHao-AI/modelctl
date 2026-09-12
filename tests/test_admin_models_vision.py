#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_admin_models_vision.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/12 10:00
# @Desc   : 模型摘要视觉能力软徽章推断单测
# ===============================================================================

"""_vision_capability 单元测试：多模态只能尽力推断，不能硬门禁。"""

from __future__ import annotations

from modelctl.core.profile import Profile
from modelctl.core.webui.admin_models import _vision_capability


def test_llamacpp_vision_on():
    assert _vision_capability(Profile(name="m", engine="llamacpp", port=1,
                                      engine_config={"vision": "on"})) is True


def test_llamacpp_vision_off():
    assert _vision_capability(Profile(name="m", engine="llamacpp", port=1,
                                      engine_config={"vision": "off"})) is False


def test_llamacpp_vision_absent_is_unknown():
    assert _vision_capability(Profile(name="m", engine="llamacpp", port=1, engine_config={})) is None


def test_other_engine_is_unknown():
    """vLLM 等引擎 profile 层无权威视觉字段 → None（前端只不显示徽章，绝不禁用图片）。"""
    assert _vision_capability(Profile(name="m", engine="vllm", port=1, engine_config={})) is None
