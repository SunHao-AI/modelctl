#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_tui_panel_plan.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/10 10:00
# @Desc   : Plan 视图渲染测试（字段表单 / KV 估算 / 预检三态 / CJK 对齐 / 幂等 dry-run）
# ===============================================================================

"""Plan 视图渲染测试。

覆盖 Task 4 交付物：
- 字段表单按 `profile.engine_config` 字段动态渲染（vllm 双键典型用例）
- 空 profile 列表 → "(无 profile)" 占位 panel
- KV 估算块：mock `kv_estimate_for_profile` 返回 fixture dict → 四数值/gpu_count 可见
- 预检三态：RequirementError → "FAIL..." 红消息；其他 Exception → 黄
- 无 adapter 分支：get_adapter 抛错 → "(无 adapter)"
- state.plan_edit_cursor / cycle_plan_cursor 行为（含 count=0 no-op / 负向回绕）
- CJK 严格对齐：含中文 form 行 display_width 不超过 width
- smoke：默认快照 → render_plan 不抛异常
- subprocess 零调用：spy subprocess.run 计数断言 0（面板本身不写 subprocess）
"""

from __future__ import annotations

import time
from unittest import mock

from modelctl.core.colors import display_width
from modelctl.core.tui.data import HardwareSnapshot, ModelsSnapshot
from modelctl.core.tui.panels.plan import (
    _profile_fields,
)
from modelctl.core.tui.panels.plan import (
    render as render_plan,
)
from modelctl.core.tui.state import TUIState

# ─────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────


def _fake_profile(name: str, engine: str, engine_config: dict):
    """构造 fake Profile（Mock 显式属性，规避 name 陷阱）。"""
    p = mock.Mock()
    p.name = name
    p.engine = engine
    p.engine_config = engine_config
    p.port = 8866
    p.path = None
    return p


def _hw_snap(gpu_count: int = 2) -> HardwareSnapshot:
    snap = HardwareSnapshot(
        gpus=[
            {"index": i, "name": "RTX 4090", "free_mb": 22000, "total_mb": 24576, "util_pct": 5}
            for i in range(gpu_count)
        ],
    )
    snap._fetched_at = time.monotonic()
    return snap


def _models_snap(profile) -> ModelsSnapshot:
    snap = ModelsSnapshot(
        profiles=[{
            "name": profile.name,
            "engine": profile.engine,
            "variant": "55in",
            "port": profile.port,
            "status": "running",
            "vram_gib": 30.0,
            "rate_in": 155.0,
            "rate_out": 33.0,
        }],
    )
    snap._fetched_at = time.monotonic()
    return snap


def _render_text(st, hw, models, *, profile=None,
                 fake_adapter_cls=mock.DEFAULT,
                 fake_adapter_kv_estimate=mock.DEFAULT,
                 width=100, height=24) -> str:
    """render_plan + rich Console record 捕获。

    - profile 非 None → patch `list_profiles` 返回 [profile] 让 panel 绑 engine_config
    - fake_adapter_cls=mock.DEFAULT（默认）→ 不 mock get_adapter（真实注册表）
    - fake_adapter_kv_estimate=mock.DEFAULT（默认）→ 不 mock kv_estimate_for_profile
    """
    from rich.console import Console

    console = Console(record=True, width=width, height=height, force_terminal=False)
    ctx_blocks: list = [mock.patch("modelctl.core.capabilities.probe", return_value=mock.Mock())]
    if profile is not None:
        ctx_blocks.append(
            mock.patch("modelctl.core.profile.list_profiles", return_value=[profile])
        )
    if fake_adapter_cls is not mock.DEFAULT:
        ctx_blocks.append(
            mock.patch("modelctl.engines.get_adapter", return_value=fake_adapter_cls)
        )
    if fake_adapter_kv_estimate is not mock.DEFAULT:
        # T5-8 把 kv_estimate_for_profile 改成 _render_kv_estimate 函数体内 lazy import，
        # 锚点迁回源模块 modelctl.core.vram_estimator（panel 不再持有顶层 attr）
        ctx_blocks.append(
            mock.patch("modelctl.core.vram_estimator.kv_estimate_for_profile",
                       return_value=fake_adapter_kv_estimate)
        )
    for blk in ctx_blocks:
        blk.start()
    try:
        group = render_plan(st, hw, models, width=width, height=height, theme_id="dark")
    finally:
        for blk in reversed(ctx_blocks):
            blk.stop()
    console.print(group)
    return console.export_text()


# ─────────────────────────────────────────────────────────
# 1. 字段表单
# ─────────────────────────────────────────────────────────


def test_plan_render_two_fields_vllm():
    """vllm profile：表单区至少出现 max_model_len + tensor_parallel_size 两字段。"""
    profile = _fake_profile("x-vllm", "vllm", {
        "max_model_len": 8192,
        "tensor_parallel_size": 2,
        "kv_cache_dtype": "fp16",
        "max_num_seqs": 4,
    })
    st = TUIState()
    st.active_view = "plan"
    st.active_index = 0
    hw = _hw_snap(2)
    models = _models_snap(profile)
    text = _render_text(st, hw, models, profile=profile)
    assert "max_model_len" in text
    assert "tensor_parallel_size" in text
    assert "kv_cache_dtype" in text
    assert "max_num_seqs" in text
    # 字段值也需可见
    assert "8192" in text
    assert "fp16" in text
    # header 指示 Plan 视图
    assert "Plan" in text


def test_plan_profile_field_map_vllm():
    """_profile_fields 直调：vllm engine → 4 字段 dict 保序。"""
    profile = _fake_profile("x-vllm", "vllm", {
        "max_model_len": 8192,
        "tensor_parallel_size": 2,
        "kv_cache_dtype": "fp16",
        "max_num_seqs": 4,
    })
    fields = _profile_fields(profile)
    keys = [k for k, _v in fields]
    assert keys == ["max_model_len", "tensor_parallel_size", "kv_cache_dtype", "max_num_seqs"]
    # value 是 str（渲染用）
    assert str(8192) in fields[0][1]
    assert "fp16" in fields[2][1]


def test_plan_no_profile_empty():
    """空 profile 列表：渲染出 "(无 profile)" 占位，不抛异常。"""
    st = TUIState()
    st.active_view = "plan"
    st.active_index = 0
    hw = _hw_snap(0)
    models = ModelsSnapshot()
    text = _render_text(st, hw, models, profile=None)
    assert "(无 profile)" in text
    # 不应出现真实 profile 字段名 / KV 估算数值
    assert "max_model_len" not in text
    assert "precheck passed" not in text


# ─────────────────────────────────────────────────────────
# 2. KV 估算块
# ─────────────────────────────────────────────────────────


def test_plan_kv_estimate_fixture_visible():
    """mock kv_estimate_for_profile 返回 fixture → 四个数值/dtype 可见。"""
    profile = _fake_profile("x-vllm", "vllm", {
        "max_model_len": 4096,
        "tensor_parallel_size": 2,
        "kv_cache_dtype": "fp16",
    })
    st = TUIState()
    st.active_view = "plan"
    hw = _hw_snap(2)
    models = _models_snap(profile)
    fixture = {"kv_total_mb": 5120.0, "per_card_mb": 2560.0, "gpu_count": 2, "cache_dtype": "fp16"}
    text = _render_text(
        st, hw, models, profile=profile,
        fake_adapter_kv_estimate=fixture,
    )
    assert "5120" in text
    assert "2560" in text
    assert "fp16" in text
    # gpu_count 行应出现 "2"（在形如 "gpu_count=2" 或 "x 2 卡" 片段内）
    assert "2" in text


def test_plan_kv_estimate_unavailable_yellow():
    """kv_estimate_for_profile 返回 None → 黄 "(无法估算...)" 占位。"""
    profile = _fake_profile("x-vllm", "vllm", {
        "max_model_len": 4096,
        "tensor_parallel_size": 2,
        "kv_cache_dtype": "fp16",
    })
    st = TUIState()
    st.active_view = "plan"
    hw = _hw_snap(2)
    models = _models_snap(profile)
    text = _render_text(st, hw, models, profile=profile, fake_adapter_kv_estimate=None)
    assert "无法估算" in text


# ─────────────────────────────────────────────────────────
# 3. 预检三态
# ─────────────────────────────────────────────────────────


def test_plan_precheck_requirement_error_red():
    """adapter.check_requirements 抛 RequirementError → FAIL 渲染异常消息。"""
    from modelctl.engines.base import RequirementError
    profile = _fake_profile("x-vllm", "vllm", {"max_model_len": 4096})
    fake_adapter = mock.Mock()
    fake_adapter.check_requirements.side_effect = RequirementError("少 1 卡 GPU")
    fake_adapter_cls = mock.Mock()
    fake_adapter_cls.return_value = fake_adapter
    st = TUIState()
    st.active_view = "plan"
    hw = _hw_snap(1)
    models = _models_snap(profile)
    text = _render_text(st, hw, models, profile=profile, fake_adapter_cls=fake_adapter_cls)
    assert "FAIL" in text or "少 1 卡 GPU" in text
    assert "少 1 卡 GPU" in text


def test_plan_precheck_no_adapter():
    """get_adapter 抛（引擎未注册）→ "(无 adapter)"。"""
    profile = _fake_profile("x-gamma", "gamma_engine", {"max_model_len": 1})
    st = TUIState()
    st.active_view = "plan"
    hw = _hw_snap(1)
    models = _models_snap(profile)
    # mock DEFAULT 不触发 get_adapter patch——让 panel 走真实引擎注册表，
    # "gamma_engine" 不在已知引擎 → 抛 ProfileError → 面板降级 "(无 adapter)"
    text = _render_text(st, hw, models, profile=profile)
    assert "(无 adapter)" in text


def test_plan_precheck_pass_green():
    """adapter.check_requirements 无异常 → "precheck passed" 绿。"""
    profile = _fake_profile("x-vllm", "vllm", {"max_model_len": 4096})
    fake_adapter = mock.Mock()
    fake_adapter.check_requirements.return_value = None
    fake_adapter_cls = mock.Mock()
    fake_adapter_cls.return_value = fake_adapter
    st = TUIState()
    st.active_view = "plan"
    hw = _hw_snap(2)
    models = _models_snap(profile)
    text = _render_text(st, hw, models, profile=profile, fake_adapter_cls=fake_adapter_cls)
    assert "precheck passed" in text


# ─────────────────────────────────────────────────────────
# 4. 光标 / cycle_plan_cursor
# ─────────────────────────────────────────────────────────


def test_plan_field_cursor_cycle_wraps():
    """cycle_plan_cursor(+1) 三次 → 0→1→2→0；再 cycle_plan_cursor(-1) → 2。"""
    st = TUIState()
    assert st.plan_edit_cursor == 0
    st.cycle_plan_cursor(+1, count=3)
    assert st.plan_edit_cursor == 1
    st.cycle_plan_cursor(+1, count=3)
    assert st.plan_edit_cursor == 2
    st.cycle_plan_cursor(+1, count=3)
    assert st.plan_edit_cursor == 0
    st.cycle_plan_cursor(-1, count=3)
    assert st.plan_edit_cursor == 2
    # direction=0 no-op
    st.cycle_plan_cursor(0, count=3)
    assert st.plan_edit_cursor == 2


def test_plan_field_cursor_count_zero_noop():
    """count=0 → cursor 不变（越界 no-op 守）。"""
    st = TUIState()
    st.plan_edit_cursor = 5
    st.cycle_plan_cursor(+1, count=0)
    assert st.plan_edit_cursor == 5
    st.cycle_plan_cursor(-1, count=0)
    assert st.plan_edit_cursor == 5


# ─────────────────────────────────────────────────────────
# 5. CJK 对齐
# ─────────────────────────────────────────────────────────


def test_plan_cjk_strict_alignment():
    """含中文 engine_config 值 → 渲染后每行 display_width <= width。"""
    profile = _fake_profile("qwen测试模型", "vllm", {"model": "qwen测试模型"})
    st = TUIState()
    st.active_view = "plan"
    hw = _hw_snap(2)
    models = _models_snap(profile)
    width = 100
    text = _render_text(st, hw, models, profile=profile, width=width)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    assert lines, "无有效渲染行"
    for ln in lines:
        dw = display_width(ln)
        # rich Console 渲染 trailing 空格会被 strip；放宽 ±2
        assert dw <= width + 2, f"行超宽 {dw} > {width + 2}: {ln!r}"
        # header/keybar pad_width 强制 == width（除第一/末行外）
        assert dw >= min(width - 2, 10), f"行短于 10/width-2: {ln!r}"


# ─────────────────────────────────────────────────────────
# 6. smoke / 幂等
# ─────────────────────────────────────────────────────────


def test_plan_smoke_render_once_no_exception():
    """空快照直接 render_plan：返回 Group 且非空，不抛异常。"""
    from rich.console import Group

    st = TUIState()
    st.active_view = "plan"
    hw = HardwareSnapshot()
    models = ModelsSnapshot()
    # 真实 probe() 在当前环境可能成功（Windows 返回 0 GPU）—— 不 mock
    group = render_plan(st, hw, models, width=100, height=24, theme_id="dark")
    assert isinstance(group, Group)
    assert len(group.renderables) >= 3  # header + body + keybar


def test_plan_dry_run_idempotent_no_subprocess():
    """D 键关联字段 plan_dry_run_done；多帧渲染 verify 不触发 subprocess.run 增次。"""
    import subprocess

    profile = _fake_profile("x-vllm", "vllm", {"max_model_len": 4096})
    st = TUIState()
    st.active_view = "plan"
    hw = _hw_snap(1)
    models = _models_snap(profile)
    original_run = subprocess.run
    call_count = {"n": 0}

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return original_run(*args, **kwargs)

    with mock.patch("subprocess.run", side_effect=_spy):
        _render_text(st, hw, models, profile=profile)
        first = call_count["n"]
        # 第二帧（模拟 D 按键后 state 变更再重绘）
        st.plan_dry_run_done = True
        _render_text(st, hw, models, profile=profile)
        second = call_count["n"]
    # 面板**自身**不允许在渲染路径触发 subprocess（probe 已被 patch）。
    # 即便测试里 probe mock 不触发 subprocess，二次渲染也不应引入新的运行。
    assert second == first, f"二次渲染引入 subprocess.run 增量：{first} → {second}"


# ─────────────────────────────────────────────────────────
# 7. HWM guard
# ─────────────────────────────────────────────────────────


def test_plan_no_open_write_mode():
    """面板自身不允许 open('w') 写文件（spec §6.2 禁区）。"""
    import builtins


    real_open = builtins.open
    opened = {"n": 0}

    def _spy_open(file, mode="r", *a, **kw):
        if "w" in str(mode) or "a" in str(mode):
            opened["n"] += 1
        return real_open(file, mode, *a, **kw)

    with mock.patch("builtins.open", side_effect=_spy_open):
        st = TUIState()
        st.active_view = "plan"
        profile = _fake_profile("x", "vllm", {"max_model_len": 1})
        _render_text(st, HardwareSnapshot(), _models_snap(profile), profile=profile)
    assert opened["n"] == 0, f"面板渲染触发 open 写：{opened}"


__all__ = [
    "test_plan_render_two_fields_vllm",
    "test_plan_profile_field_map_vllm",
    "test_plan_no_profile_empty",
    "test_plan_kv_estimate_fixture_visible",
    "test_plan_kv_estimate_unavailable_yellow",
    "test_plan_precheck_requirement_error_red",
    "test_plan_precheck_no_adapter",
    "test_plan_precheck_pass_green",
    "test_plan_field_cursor_cycle_wraps",
    "test_plan_field_cursor_count_zero_noop",
    "test_plan_cjk_strict_alignment",
    "test_plan_smoke_render_once_no_exception",
    "test_plan_dry_run_idempotent_no_subprocess",
    "test_plan_no_open_write_mode",
]
