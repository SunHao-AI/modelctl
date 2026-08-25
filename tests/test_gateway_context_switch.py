#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_gateway_context_switch.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 网关上下文切换测试
# ===============================================================================

"""modelctl.core.gateway 上下文切换（附录 B.3）单元测试。"""

from __future__ import annotations

import asyncio
import json

import httpx

from modelctl.core.gateway import (
    ContextSwitchRule,
    GatewayModel,
    apply_context_switch,
    create_app,
    estimate_prompt_tokens,
    load_context_switch_rules,
)

DS = "deepseek-v4-flash"
DS_HIGH = "deepseek-v4-flash-vllm-high"
DS_BAL = "deepseek-v4-flash-vllm"
DS_LIGHT = "deepseek-v4-flash-vllm-light"


def _registry():
    return {
        name: GatewayModel(name, "vllm", f"http://127.0.0.1:{i}", name, None, f"http://127.0.0.1:{i}/")
        for i, name in enumerate((DS, DS_HIGH, DS_BAL, DS_LIGHT), start=8000)
    }


def _rules() -> dict[str, list[ContextSwitchRule]]:
    return load_context_switch_rules(
        {
            DS: [
                {"min_prompt_tokens": 32768, "target": DS_HIGH},
                {"min_prompt_tokens": 8192, "target": DS_BAL},
                {"min_prompt_tokens": 0, "target": DS_LIGHT},
            ]
        }
    )


def test_estimate_prompt_tokens_str_content():
    body = {"model": "x", "messages": [{"role": "user", "content": "hello world"}, {"role": "assistant", "content": "hi"}]}
    # 11 + 2 = 13 字符，//4 = 3
    assert estimate_prompt_tokens(body) == 3


def test_estimate_prompt_tokens_multimodal_parts():
    body = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "describe"}, {"type": "image", "image_url": "x"}]}
        ]
    }
    assert estimate_prompt_tokens(body) == 8 // 4


def test_estimate_prompt_tokens_empty():
    assert estimate_prompt_tokens({}) == 0
    assert estimate_prompt_tokens({"messages": []}) == 0
    assert estimate_prompt_tokens({"messages": "not-a-list"}) == 0


def test_load_rules_sorted_desc_and_skips_invalid():
    raw = {
        "m": [
            {"min_prompt_tokens": 0, "target": "light"},
            {"min_prompt_tokens": 999, "target": "high"},
            {"min_prompt_tokens": "x", "target": "bad"},  # 非法阈值 → 跳过
            {"target": "missing-threshold"},  # 缺字段 → 跳过
            "not-a-dict",  # 非 dict → 跳过
            {"min_prompt_tokens": 5, "target": ""},  # 空目标 → 跳过
        ]
    }
    rules = load_context_switch_rules(raw)
    assert [r.min_prompt_tokens for r in rules["m"]] == [999, 0]
    assert load_context_switch_rules({}) == {}
    assert load_context_switch_rules(None) == {}


def test_apply_switch_selects_high_balanced_light():
    reg = _registry()
    rules = _rules()
    assert apply_context_switch(reg, rules, DS, 50000) is reg[DS_HIGH]
    assert apply_context_switch(reg, rules, DS, 32768) is reg[DS_HIGH]  # 边界命中 high
    assert apply_context_switch(reg, rules, DS, 9000) is reg[DS_BAL]
    assert apply_context_switch(reg, rules, DS, 8192) is reg[DS_BAL]  # 边界命中 balanced
    assert apply_context_switch(reg, rules, DS, 100) is reg[DS_LIGHT]


def test_apply_switch_no_rule_or_unknown_model():
    reg = _registry()
    rules = _rules()
    assert apply_context_switch(reg, {}, DS, 100) is None  # 无规则
    assert apply_context_switch(reg, rules, "ghost-model", 100) is None  # 无匹配 base
    assert apply_context_switch(reg, rules, None, 100) is None


def test_apply_switch_target_missing_falls_back_to_none():
    reg = dict(_registry())
    reg.pop(DS_HIGH)  # 目标未注册 → 返回 None，调用方沿用原模型
    assert apply_context_switch(reg, _rules(), DS, 50000) is None


def _run(coro):
    return asyncio.run(coro)


async def _post(app, path: str, json: dict | None = None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=json or {})


def test_proxy_routes_by_context_length():
    """集成：请求体估算输入 token 超过阈值时，网关把 model 改写为目标变体的 upstream_model。"""
    captured = {}

    def upstream(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "1", "model": captured["body"]["model"]})

    reg = _registry()
    app = create_app(reg, default_model=DS, transport=httpx.MockTransport(upstream), context_rules=_rules())

    # 短输入（8 字符 → 2 tokens）→ light 变体
    resp = _run(_post(app, "/v1/chat/completions", json={"model": DS, "messages": [{"role": "user", "content": "hello"}]}))
    assert resp.status_code == 200
    assert captured["body"]["model"] == DS_LIGHT

    # 长输入（4 * 9000 字符 → 9000 tokens）→ balanced 变体
    long_prompt = "x" * (9000 * 4)
    resp = _run(
        _post(app, "/v1/chat/completions", json={"model": DS, "messages": [{"role": "user", "content": long_prompt}]})
    )
    assert captured["body"]["model"] == DS_BAL


def test_proxy_without_rules_keeps_original_target():
    """未配置规则时行为不变（回归）：不改写为变体。"""
    captured = {}

    def upstream(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "1", "model": captured["body"]["model"]})

    reg = _registry()
    app = create_app(reg, default_model=DS, transport=httpx.MockTransport(upstream))
    resp = _run(_post(app, "/v1/chat/completions", json={"model": DS, "messages": [{"role": "user", "content": "hi"}]}))
    assert resp.status_code == 200
    assert captured["body"]["model"] == DS  # 无规则 → upstream_model 即 profile name
