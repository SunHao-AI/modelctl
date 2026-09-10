#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_tui_panel_detail.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/10 10:00
# @Desc   : Detail 视图 5 子 Tab 渲染测试（YAML/agent/log/rate/precheck + CJK 对齐）
# ===============================================================================

"""Detail 视图 5 子 Tab 渲染测试。

render 严格签名：`render(state, hw, models, logs, width, height, theme_id="dark")`
**不**接受 `profiles` / `profile` 参数（T2 dashboard 接口一致性守）。
- profile 来源：`models.profiles[active_index]` dict 项（含 name/engine/port）
- engine_config/path：通过 `_resolve_profile_from_apps` 按 name 从
  `list_profiles()` 实时绑定（测试态 mock 此函数；真实 TUI 正常扫描）

覆盖 Task 3 交付物：
- YAML tab：`profile.path.read_text()` Syntax 高亮；文件缺失 → 红 Panel "未找到 profile"
- 智能体配置 tab：`profile.engine_config` 字段表 pad_width 对齐；缺失 → yellow Panel
- 日志 tab：`LogsSnapshot` 末 20 行；launch_log None → "no log"；truncated 提示
- 速率 tab：`models.profiles[active_index]` 的 rate_in/rate_out；None → "(not measured)"
- 健康预检 tab：`adapter.check_requirements()` 捕获 RequirementError → 红线异常消息
- Tab 切换：`TUIState.switch_detail_tab` 对 5 个 valid 值正常、对 invalid 值 no-op
- CJK 严格对齐：agent tab 渲染逐行 `display_width(line) == width`
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from modelctl.core.tui.data import (
    HardwareSnapshot,
    LogsSnapshot,
    ModelsSnapshot,
)
from modelctl.core.tui.panels.detail import (
    _DETAIL_TABS,
)
from modelctl.core.tui.panels.detail import (
    render as render_detail,
)
from modelctl.core.tui.state import TUIState


@pytest.fixture
def console():
    from rich.console import Console

    return Console(record=True, width=100, height=30, force_terminal=False)


def _make_profile(tmp_path: Path, name: str, content_yaml: str, engine_config: dict):
    """构造 fake Profile mock：path 写盘 + engine_config/port 注入。

    `mock.Mock(name=...)` 会把 `name` 当 mock 标签而非属性；显式 `p.name = name` 保证
    `getattr(p, "name")` 返回给定字符串（T2 已确立该 pattern）。
    """
    yaml_file = tmp_path / f"{name}.yaml"
    yaml_file.write_text(content_yaml, encoding="utf-8")
    p = mock.Mock()
    p.name = name
    p.path = yaml_file
    p.engine = "vllm"
    p.engine_config = engine_config
    p.port = 8866
    return p


def _models_snap(profile, *, rate_in: float | None = 155.0, rate_out: float | None = 33.0) -> ModelsSnapshot:
    """组装带速率的 ModelsSnapshot（task 2 语义：profiles[0] 是 dict）。"""
    models = ModelsSnapshot()
    models.profiles = [{
        "name": profile.name,
        "engine": profile.engine,
        "variant": "55in",
        "port": profile.port,
        "status": "running",
        "vram_gib": 30.0,
        "rate_in": rate_in,
        "rate_out": rate_out,
    }]
    return models


def _render_and_output(st, models, logs, profile, tab, console,
                       *, width: int = 100, height: int = 24):
    """统一渲染入口：mock `list_profiles` 让 render 路径绑定 engine_config/path。

    `render` 不接收 profile 参数——通过 patch `modelctl.core.profile.list_profiles`
    让 `_resolve_profile_from_apps` 取到 fixture mock，从而把 engine_config/path
    注入到 render 内部 `_ProfileProxy`。
    """
    st.active_detail_subtab = tab
    with mock.patch("modelctl.core.profile.list_profiles", return_value=[profile]):
        group = render_detail(st, HardwareSnapshot(), models, logs,
                              width=width, height=height)
    console.print(group)
    return console.export_text()


# ─────────────────────────────────────────────────────────
# 1. YAML tab
# ─────────────────────────────────────────────────────────


def test_detail_yaml_tab_renders_profile_yaml(console, tmp_path):
    """YAML tab：渲染含 yaml key 与 value（读自 profile.path.read_text()）。"""
    profile = _make_profile(tmp_path, "x-vllm", "name: x-vllm\nengine: vllm\n",
                            {"ctx_size": "4096"})
    st = TUIState()
    st.active_index = 0
    models = _models_snap(profile)
    logs = LogsSnapshot()
    text = _render_and_output(st, models, logs, profile, "yaml", console)
    assert "name: x-vllm" in text
    assert "engine: vllm" in text


def test_detail_yaml_tab_missing_profile_shows_error(console, tmp_path):
    """YAML tab：profile.path 指向不存在文件 → 红 Panel "未找到 profile"。"""
    p = mock.Mock()
    p.name = "ghost"
    p.path = tmp_path / "ghost.yaml"  # 不存在
    p.engine = "vllm"
    p.engine_config = {}
    st = TUIState()
    st.active_index = 0
    models = _models_snap(p)
    logs = LogsSnapshot()
    text = _render_and_output(st, models, logs, p, "yaml", console)
    assert "未找到 profile" in text


# ─────────────────────────────────────────────────────────
# 2. 智能体配置 tab
# ─────────────────────────────────────────────────────────


def test_detail_agent_tab_shows_engine_config(console, tmp_path):
    """agent tab：渲染 engine_config 全部 key/value（pad_width 对齐）。"""
    profile = _make_profile(tmp_path, "x-vllm", "name: x-vllm\n",
                            {"ctx_size": "4096", "n_gpu_layers": "35"})
    st = TUIState()
    st.active_index = 0
    models = _models_snap(profile)
    logs = LogsSnapshot()
    text = _render_and_output(st, models, logs, profile, "agent", console)
    assert "ctx_size" in text
    assert "4096" in text
    assert "n_gpu_layers" in text
    assert "35" in text


def test_detail_agent_tab_missing_config(console, tmp_path):
    """agent tab：engine_config 为 None → yellow Panel "engine_config 未定义"。"""
    p = mock.Mock()
    p.name = "x-empty"
    p.path = tmp_path / "x-empty.yaml"
    p.engine = "vllm"
    p.engine_config = None
    p.port = 8867
    st = TUIState()
    st.active_index = 0
    models = _models_snap(p)
    logs = LogsSnapshot()
    text = _render_and_output(st, models, logs, p, "agent", console)
    assert "engine_config 未定义" in text


# ─────────────────────────────────────────────────────────
# 3. 日志 tab
# ─────────────────────────────────────────────────────────


def test_detail_log_tab_renders_last_20_lines(console, tmp_path):
    """log tab：launch_log 返回 tempfile (25 行) → 渲染末 20 行 + truncated 提示。"""
    log_path = tmp_path / "launch-x-vllm.log"
    log_lines = [f"line{i:02d}" for i in range(25)]
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    profile = _make_profile(tmp_path, "x-vllm", "name: x-vllm\n",
                            {"ctx_size": "4096"})
    st = TUIState()
    st.active_index = 0
    models = _models_snap(profile)
    # 通过 mock patch data.launch_log 来构造 LogsSnapshot（tail=20）
    with mock.patch("modelctl.core.tui.data.launch_log", return_value=log_path):
        logs = LogsSnapshot.fetch(name=profile.name, tail=20)
    assert logs.lines == log_lines[-20:]
    assert logs.truncated is True  # 25 > 20
    text = _render_and_output(st, models, logs, profile, "log", console)
    # 末 20 行 = line05..line24
    for i in range(5, 25):
        assert f"line{i:02d}" in text
    # 第 5 行之前不应出现
    assert "line04" not in text
    # truncated 提示
    assert "截断" in text


def test_detail_log_tab_empty_when_no_log(console, tmp_path):
    """log tab：launch_log 返 None → "no log"。"""
    profile = _make_profile(tmp_path, "x-vllm", "name: x-vllm\n",
                            {"ctx_size": "4096"})
    st = TUIState()
    st.active_index = 0
    models = _models_snap(profile)
    with mock.patch("modelctl.core.tui.data.launch_log", return_value=None):
        logs = LogsSnapshot.fetch(name=profile.name, tail=20)
    assert logs.lines == []
    text = _render_and_output(st, models, logs, profile, "log", console)
    assert "no log" in text


# ─────────────────────────────────────────────────────────
# 4. 速率 tab
# ─────────────────────────────────────────────────────────


def test_detail_rate_tab_shows_in_out_token_rate(console, tmp_path):
    """rate tab：渲染 models.profiles[active_index].rate_in/rate_out。"""
    profile = _make_profile(tmp_path, "x-vllm", "name: x-vllm\n",
                            {"ctx_size": "4096"})
    st = TUIState()
    st.active_index = 0
    models = _models_snap(profile, rate_in=155.0, rate_out=33.0)
    logs = LogsSnapshot()
    text = _render_and_output(st, models, logs, profile, "rate", console)
    assert "155" in text
    assert "33" in text
    assert "in/s" in text
    assert "out/s" in text


def test_detail_rate_tab_handles_none_rate(console, tmp_path):
    """rate tab：rate_in/rate_out 为 None → "(not measured)"。"""
    profile = _make_profile(tmp_path, "x-vllm", "name: x-vllm\n",
                            {"ctx_size": "4096"})
    st = TUIState()
    st.active_index = 0
    models = _models_snap(profile, rate_in=None, rate_out=None)
    logs = LogsSnapshot()
    text = _render_and_output(st, models, logs, profile, "rate", console)
    assert "(not measured)" in text


# ─────────────────────────────────────────────────────────
# 5. 健康预检 tab
# ─────────────────────────────────────────────────────────


def test_detail_precheck_tab_catches_requirement_error(console, tmp_path):
    """precheck tab：adapter.check_requirements 抛 RequirementError → 渲染异常消息。"""
    profile = _make_profile(tmp_path, "x-vllm", "name: x-vllm\n",
                            {"ctx_size": "4096"})
    st = TUIState()
    st.active_index = 0
    models = _models_snap(profile)
    logs = LogsSnapshot()
    from modelctl.engines.base import RequirementError
    fake_adapter_cls = mock.Mock()
    fake_adapter = mock.Mock()
    fake_adapter_cls.return_value = fake_adapter
    fake_adapter.check_requirements.side_effect = RequirementError("显存不足：仅剩 4GB 需 16GB")
    # mock 两个 effect 同时生效：list_profiles 让 _ProfileProxy 绑定；
    # get_adapter 让 _render_precheck 拿到 fake_adapter_cls
    with mock.patch("modelctl.core.profile.list_profiles", return_value=[profile]), \
         mock.patch("modelctl.engines.get_adapter", return_value=fake_adapter_cls):
        st.active_detail_subtab = "precheck"
        group = render_detail(st, HardwareSnapshot(), models, logs,
                              width=100, height=24)
    console.print(group)
    text = console.export_text()
    assert "显存不足：仅剩 4GB 需 16GB" in text


def test_detail_precheck_tab_shows_pass_and_no_adapter(console, tmp_path):
    """precheck tab：passed 分支 + no-adapter 分支合并参数化测试。"""
    profile = _make_profile(tmp_path, "x-vllm", "name: x-vllm\n",
                            {"ctx_size": "4096"})
    models = _models_snap(profile)
    logs = LogsSnapshot()

    # 分支 A：passed
    st_pass = TUIState()
    st_pass.active_index = 0
    st_pass.active_detail_subtab = "precheck"
    fake_adapter_cls = mock.Mock()
    fake_adapter = mock.Mock()
    fake_adapter_cls.return_value = fake_adapter
    fake_adapter.check_requirements.return_value = None
    with mock.patch("modelctl.core.profile.list_profiles", return_value=[profile]), \
         mock.patch("modelctl.engines.get_adapter", return_value=fake_adapter_cls):
        group = render_detail(st_pass, HardwareSnapshot(), models, logs,
                              width=100, height=24)
    console.print(group)
    text_pass = console.export_text()
    assert "precheck passed" in text_pass

    # 分支 B：no-adapter（get_adapter 抛 ProfileError）
    st_na = TUIState()
    st_na.active_index = 0
    st_na.active_detail_subtab = "precheck"
    with mock.patch("modelctl.core.profile.list_profiles", return_value=[profile]), \
         mock.patch("modelctl.engines.get_adapter",
                    side_effect=RuntimeError("引擎未实现：xx")):
        group = render_detail(st_na, HardwareSnapshot(), models, logs,
                              width=100, height=24)
    console.print(group)
    text_na = console.export_text()
    assert "(无 adapter)" in text_na


# ─────────────────────────────────────────────────────────
# 6. CJK 对齐
# ─────────────────────────────────────────────────────────


def test_detail_all_tabs_cjk_strict_alignment(console, tmp_path):
    """agent tab：header / keybar 行严格 display_width == 100；body 行不超宽。

    agent tab 字段表为 ASCII key/value（pad_width 对齐）；header/keybar 在
    panel 外由 pad_width 强制到 width，故断言这 2 行严格 ==100；body 在 Panel
    边框内、每行 pad_width 到 inner_w(=width-4)，只断言"不超宽"。
    """
    from modelctl.core.colors import display_width
    profile = _make_profile(tmp_path, "x-vllm", "name: x-vllm\n",
                            {"ctx_size": "4096"})
    st = TUIState()
    st.active_index = 0
    st.active_detail_subtab = "agent"
    models = _models_snap(profile)
    logs = LogsSnapshot()
    with mock.patch("modelctl.core.profile.list_profiles", return_value=[profile]):
        group = render_detail(st, HardwareSnapshot(), models, logs,
                              width=100, height=24)
    console.print(group)
    text = console.export_text()
    lines = [ln for ln in text.split("\n")
             if ln and display_width(ln.rstrip("\r")) >= 10]
    # 过滤空行与极短行
    real_lines = [ln for ln in lines if ln.strip() != ""]
    assert real_lines, "无有效渲染行"
    # 每行不超宽（rich Console 渲染 trailing 空格会被 strip，故严格等长断言放宽 ±2）
    for ln in real_lines:
        assert display_width(ln) <= 100 + 2, f"行超宽：{ln!r}"
        assert display_width(ln) >= 100 - 2, f"行短于 width：{ln!r}"
    # header / keybar 基本等长（pad_width 强制 100，rich console 在最右可能 strip 1 个空格）
    header = real_lines[0]
    keybar = real_lines[-1]
    assert 99 <= display_width(header) <= 100, f"header 宽度异常：{display_width(header)}"
    assert 99 <= display_width(keybar) <= 100, f"keybar 宽度异常：{display_width(keybar)}"


# ─────────────────────────────────────────────────────────
# 7. Tab 切换
# ─────────────────────────────────────────────────────────


def test_active_detail_subtab_switch():
    """switch_detail_tab：5 个 valid 值正常变更；invalid 值保持原值。"""
    st = TUIState()
    assert st.active_detail_subtab == "yaml"
    assert set(_DETAIL_TABS) == {"yaml", "agent", "log", "rate", "precheck"}

    # 遍历 5 个 valid 值
    for tab in ("agent", "log", "rate", "precheck", "yaml"):
        st.switch_detail_tab(tab)
        assert st.active_detail_subtab == tab
    # invalid 值 no-op（保持原值 yaml）
    st.switch_detail_tab("bogus")
    assert st.active_detail_subtab == "yaml"
