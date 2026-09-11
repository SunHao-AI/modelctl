#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_tui_no_side_effects.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/10 10:00
# @Desc   : TUI 5 panels 护栏——render 必须无 subprocess / 写文件 / kill / nvml 副作用
# ===============================================================================

"""TUI 5 panels 写路径禁区护栏（spec §6.2 / §6.1 no_side_effects）。

策略：
- 把 5 个写通道 stub 并计数：
  - `subprocess.Popen`（任何 spawn）
  - `subprocess.run`（任何同步调用）
  - `builtins.open`（写模式 'w'/'a'/'x'/'' 等）
  - `os.kill`（进程信号）
  - `pynvml` 三级注入：`sys.modules['pynvml']` +
    `sys.modules['modelctl.core.hw']`（向前兼容占位） +
    `sys.modules['modelctl.core.hw.nvml']`——防止 T6/T7 接 pynvml 后
    出现 TUI render 内 monkey-import

- 5 view 各渲染 1 帧，喂给代表性 snapshot（pop + 空 兼中）。
- 断言：所有 stub 调用计数均 == 0。

为何护栏写在这里（而不是 lint）：
- lint 只能拦字面量匹配（`subprocess.Popen`、`open(..., 'w')`），动态
  import / 间接调用拦不住；运行时 guard 才不会被绕。
- 5 view × 3 width (80/120/200) 共 15 帧，覆盖建立后所有 view 的 import
  关系；新 view 只要加入 `_render_views` 注册，自动纳入护栏。
"""

from __future__ import annotations

import builtins
import os
import subprocess
import sys
import time

from modelctl.core.tui import TUIState
from modelctl.core.tui.data import (
    ClusterSnapshot,
    HardwareSnapshot,
    LogsSnapshot,
    ModelsSnapshot,
    MonitorSnapshot,
)
from modelctl.core.tui.panels.cluster import render as render_cluster
from modelctl.core.tui.panels.detail import render as render_detail
from modelctl.core.tui.panels.main_dashboard import render as render_dashboard
from modelctl.core.tui.panels.monitor import render as render_monitor
from modelctl.core.tui.panels.plan import render as render_plan


def _render_views(width: int, snaps: dict[str, object]) -> None:
    """5 view × (pop + empty) 代表性 snapshot 各渲 1 帧。"""
    st = TUIState()
    # dashboard（full / medium / narrow 由 width 派生；pop + empty 兼中）
    render_dashboard(st, snaps["hw"], snaps["models_pop"], snaps["cluster_pop"],
                     width=width, height=24, theme_id="dark")
    render_dashboard(st, snaps["hw"], snaps["models_empty"], snaps["cluster_empty"],
                     width=width, height=24, theme_id="dark")
    # detail（active_index=0 命中 profile 0；empty models 兼中 fallback 分支）
    render_detail(st, snaps["hw"], snaps["models_pop"], snaps["logs"],
                  width=width, height=24, theme_id="dark")
    render_detail(st, snaps["hw"], snaps["models_empty"], snaps["logs"],
                  width=width, height=24, theme_id="dark")
    # plan（plan_edit 空；pop models + empty models 兼中）
    render_plan(st, snaps["hw"], snaps["models_pop"],
                width=width, height=24, theme_id="dark")
    render_plan(st, snaps["hw"], snaps["models_empty"],
                width=width, height=24, theme_id="dark")
    # cluster（pop 节点 + 空节点 兼中）
    render_cluster(st, snaps["cluster_pop"],
                   width=width, height=24, theme_id="dark")
    render_cluster(st, snaps["cluster_empty"],
                   width=width, height=24, theme_id="dark")
    # monitor（info pop + empty 兼中；hw pop + empty 兼中）
    render_monitor(st, snaps["models_pop"], snaps["hw"], snaps["monitor"],
                   width=width, height=24, theme_id="dark")
    render_monitor(st, snaps["models_empty"], snaps["hw_empty"], snaps["monitor_empty"],
                   width=width, height=24, theme_id="dark")


def _snapshots_fresh() -> dict[str, object]:
    """构造 5 个 snapshot，_fetched_at=now 表示 fresh（render 内不会 revalidate）。"""
    now = time.monotonic()
    hw_pop = HardwareSnapshot(
        gpus=[
            {"index": 0, "name": "NVIDIA H100 80GB", "free_mb": 40000,
             "total_mb": 80000, "util_pct": 0},
            {"index": 1, "name": "NVIDIA A100", "free_mb": 12000,
             "total_mb": 40000, "util_pct": 0},
        ],
        binaries={"vllm": "/opt/vllm"},
        cpu_info="CC 8.0",
        probe_errors=["nvidia-smi not found"],
    )
    hw_pop._fetched_at = now
    hw_empty = HardwareSnapshot(gpus=[], binaries={}, cpu_info="", probe_errors=[])
    hw_empty._fetched_at = now
    models_pop = ModelsSnapshot(
        profiles=[
            {"name": "qwen2.5-0.5b", "engine": "vllm", "variant": "fp16", "port": 8500,
             "status": "running", "vram_gib": 1.0, "rate_in": 155.0, "rate_out": 33.0},
        ]
    )
    models_pop._fetched_at = now
    models_empty = ModelsSnapshot(profiles=[])
    models_empty._fetched_at = now
    logs = LogsSnapshot(name="qwen2.5-0.5b", tail="")
    logs._fetched_at = now
    cluster_pop = ClusterSnapshot(
        nodes=[{"node_id": "w-210", "status": "READY", "lan_id": "0.0.0.0",
                "center_visible": "https://center"}],
        goals=[{"profile": "qwen2.5-0.5b", "node": "w-210", "intent": "start",
                "stage": "READY", "start": 1, "ready": 1, "port": 8500}],
        center_visible="https://center",
    )
    cluster_pop._fetched_at = now
    cluster_empty = ClusterSnapshot(nodes=[], goals=[], center_visible="(中心不可达)")
    cluster_empty._fetched_at = now
    monitor_pop = MonitorSnapshot(
        info=[{"name": "qwen2.5-0.5b", "rate_in": 155.0, "rate_out": 33.0}],
    )
    monitor_pop._fetched_at = now
    monitor_empty = MonitorSnapshot(info=[])
    monitor_empty._fetched_at = now
    return {
        "hw": hw_pop,
        "hw_empty": hw_empty,
        "models_pop": models_pop,
        "models_empty": models_empty,
        "logs": logs,
        "cluster_pop": cluster_pop,
        "cluster_empty": cluster_empty,
        "monitor": monitor_pop,
        "monitor_empty": monitor_empty,
    }


class _GuardState:
    """单测期间 collect 的调用记录 + sys.modules 备份。"""

    def __init__(self) -> None:
        self.popen: list = []
        self.run: list = []
        self.open_w: list = []
        self.kill: list = []
        self.nvml: list = []
        self._real_open = builtins.open
        self._real_popen = subprocess.Popen
        self._real_run = subprocess.run
        self._real_kill = os.kill
        self._sys_modules_pynvml = sys.modules.get("pynvml")
        self._sys_modules_hw = sys.modules.get("modelctl.core.hw")
        self._sys_modules_hw_nvml = sys.modules.get("modelctl.core.hw.nvml")

    # -- 5 个 stub -----------------------------------------------------------
    def mock_popen(self, *a, **kw):
        self.popen.append((a, kw))
        raise AssertionError("subprocess.Popen is banned in TUI render (spec §6.2)")

    def mock_run(self, *a, **kw):
        self.run.append((a, kw))
        raise AssertionError("subprocess.run is banned in TUI render (spec §6.2)")

    def mock_open(self, file, mode="r", *a, **kw):
        # 仅拦写类模式；'r' 保留（rich 内部可能读资源文件，无害）
        if any(m in str(mode) for m in ("w", "a", "x", "+")):
            self.open_w.append((file, mode))
            raise AssertionError(
                "open(..., write) is banned in TUI render (spec §6.2)"
            )
        return self._real_open(file, mode, *a, **kw)

    def mock_kill(self, *a, **kw):
        self.kill.append((a, kw))
        raise AssertionError("os.kill is banned in TUI render (spec §6.2)")

    # -- 安装 / 卸载 ---------------------------------------------------------
    def install(self) -> None:
        subclass = type(sys)("modelctl.core.hw")
        subclass.__path__ = ()  # type: ignore[attr-defined]
        class _NvmlProxy:
            """任意属性访问 → 抛错（兜底）。"""

            def __getattr__(self, item):
                raise AssertionError(
                    f"modelctl.core.hw.nvml.{item} banned in TUI render (spec §6.2)"
                )

            def __call__(self, *a, **kw):
                raise AssertionError(
                    "modelctl.core.hw.nvml.* call banned in TUI render (spec §6.2)"
                )

        nvml_mod = type(sys)("modelctl.core.hw.nvml")
        nvml_mod.nvml = _NvmlProxy()  # type: ignore[attr-defined]
        nvml_mod.__path__ = ()  # type: ignore[attr-defined]
        subclass.nvml = nvml_mod
        sys.modules["modelctl.core.hw"] = subclass
        sys.modules["modelctl.core.hw.nvml"] = nvml_mod
        # 第三方 pynvml 也占位（T6/T7 接的可能是 pynvml 直接 import）
        pynvml_mod = type(sys)("pynvml")
        pynvml_mod.__path__ = ()  # type: ignore[attr-defined]

        def _pynvml_entry(*_a, **_kw):
            raise AssertionError("pynvml.* call banned in TUI render (spec §6.2)")

        pynvml_mod.nvml = _pynvml_entry  # type: ignore[attr-defined]
        pynvml_mod.init = _pynvml_entry  # type: ignore[attr-defined]
        pynvml_mod.nvmlDeviceGetHandleByIndex = _pynvml_entry  # type: ignore[attr-defined]
        sys.modules["pynvml"] = pynvml_mod
        subprocess.Popen = self.mock_popen  # type: ignore[misc, assignment]
        subprocess.run = self.mock_run  # type: ignore[misc, assignment]
        os.kill = self.mock_kill  # type: ignore[misc, assignment]
        builtins.open = self.mock_open  # type: ignore[assignment]

    def uninstall(self) -> None:
        subprocess.Popen = self._real_popen  # type: ignore[misc, assignment]
        subprocess.run = self._real_run  # type: ignore[misc, assignment]
        os.kill = self._real_kill  # type: ignore[misc, assignment]
        builtins.open = self._real_open  # type: ignore[assignment]
        # 恢复 sys.modules（None → pop；有原值 → 重置）
        for key in ("pynvml", "modelctl.core.hw", "modelctl.core.hw.nvml"):
            original = {
                "pynvml": self._sys_modules_pynvml,
                "modelctl.core.hw": self._sys_modules_hw,
                "modelctl.core.hw.nvml": self._sys_modules_hw_nvml,
            }[key]
            if original is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = original


def test_no_side_effects_all_views_render():
    """5 view × (pop + empty 兼中) × 3 width (80/120/200)，stub 计数均 == 0。"""
    guard = _GuardState()
    guard.install()
    try:
        snaps = _snapshots_fresh()
        for w in (80, 120, 200):
            _render_views(w, snaps)
    finally:
        guard.uninstall()
    assert guard.popen == [], f"subprocess.Popen 被调用：{guard.popen!r}"
    assert guard.run == [], f"subprocess.run 被调用：{guard.run!r}"
    assert guard.open_w == [], f"open(..., 'w') 被调用：{guard.open_w!r}"
    assert guard.kill == [], f"os.kill 被调用：{guard.kill!r}"
    assert guard.nvml == [], f"nvml proxy 被调用：{guard.nvml!r}"


# ---------------------------------------------------------------------------
# TUI-P1-1：precheck Tab 的 check_requirements 必须只读
# ---------------------------------------------------------------------------

def _stub_side_effect_channels(monkeypatch) -> list[str]:
    """把两类写副作用通道换成记录调用的 stub，返回记录列表。

    记录顺序即执行顺序：`clear` = 删陈旧容器，`lock` = 抢 GPU 锁。

    **patch 目标必须是 `modelctl.engines.vllm` 里的名字**：vllm.py 顶部是
    `from modelctl.core.gpu_lock import acquire_gpu_lock`（模块级绑定），patch
    `modelctl.core.gpu_lock.acquire_gpu_lock` 对已绑定的名字完全无效——那样对照组
    永远缺 `lock`，而只读组会因为"根本没观察到副作用"假绿。
    `clear_stale_docker_container` 在函数内延迟 import，patch process 模块才有效。
    """
    called: list[str] = []
    monkeypatch.setattr(
        "modelctl.core.process.clear_stale_docker_container",
        lambda *a, **k: called.append("clear") or True,
    )
    monkeypatch.setattr(
        "modelctl.engines.vllm.acquire_gpu_lock",
        lambda *a, **k: called.append("lock"),
    )
    # docker PATH 检查放行，确保真走到 clear 分支（本机可能没装 docker）
    monkeypatch.setattr("modelctl.core.docker_setup.path_level_missing", lambda: [])
    return called


def _vllm_docker_profile():
    """docker 运行时 + 显式 gpu_list 的 vllm profile：两类副作用分支都会命中。

    `gpu_list` 必填——否则 `selected_gpus()` 返回 None，抢锁分支根本不执行，
    对照组就失去灵敏度。
    """
    from modelctl.core.profile import Profile

    return Profile(
        name="qwen2.5-0.5b", engine="vllm", port=8500,
        engine_config={"docker_image": "vllm/vllm-openai:test", "model": "/models/q",
                       "gpu_list": "0"},
    )


def test_precheck_writes_side_effects_when_not_readonly(monkeypatch):
    """对照组：真实启动口径（readonly=False）**必须**清容器 + 抢锁。

    本用例证明护栏真的能观察到这两类副作用——否则 readonly=True 那条断言
    只是因为压根走不到副作用分支而假绿。
    """
    from modelctl.core.capabilities import Capabilities
    from modelctl.engines import get_adapter

    called = _stub_side_effect_channels(monkeypatch)
    caps = Capabilities(gpu_count=8, gpu_indices=list(range(8)),
                        compute_capability="9.0", binaries={"vllm": True})
    adapter = get_adapter("vllm")(_vllm_docker_profile(), caps)
    adapter.check_requirements()
    assert called == ["clear", "lock"], f"非只读预检副作用缺失：{called}"


def test_detail_precheck_is_readonly(monkeypatch):
    """Detail 的 precheck Tab 渲染曾真调 check_requirements → 删容器 + 写 GPU 锁。"""
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.tui.data import ModelsSnapshot
    from modelctl.core.tui.panels.detail import render as render_detail

    called = _stub_side_effect_channels(monkeypatch)
    monkeypatch.setattr(
        "modelctl.core.profile.list_profiles", lambda *a, **k: [_vllm_docker_profile()])

    snaps = _snapshots_fresh()
    st = TUIState()
    st.active_detail_subtab = "precheck"
    caps = Capabilities(gpu_count=8, gpu_indices=list(range(8)),
                        compute_capability="9.0", binaries={"vllm": True})
    models = ModelsSnapshot(profiles=[
        {"name": "qwen2.5-0.5b", "engine": "vllm", "port": 8500, "status": "stopped"}])
    render_detail(st, snaps["hw"], models, snaps["logs"],
                  width=120, height=40, theme_id="dark", caps=caps)
    assert called == [], f"precheck 渲染仍有副作用：{called}"


def test_plan_precheck_is_readonly(monkeypatch):
    """Plan 视图常驻预检 Panel 同样不得有写副作用。"""
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.tui.data import ModelsSnapshot
    from modelctl.core.tui.panels.plan import render as render_plan

    called = _stub_side_effect_channels(monkeypatch)
    monkeypatch.setattr(
        "modelctl.core.profile.list_profiles", lambda *a, **k: [_vllm_docker_profile()])

    snaps = _snapshots_fresh()
    st = TUIState()
    caps = Capabilities(gpu_count=8, gpu_indices=list(range(8)),
                        compute_capability="9.0", binaries={"vllm": True})
    models = ModelsSnapshot(profiles=[
        {"name": "qwen2.5-0.5b", "engine": "vllm", "port": 8500, "status": "stopped"}])
    render_plan(st, snaps["hw"], models, width=120, height=40, theme_id="dark", caps=caps)
    assert called == [], f"plan 预检渲染仍有副作用：{called}"
