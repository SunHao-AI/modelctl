"""all_service 单组件原语与默认模型解析测试。"""

from __future__ import annotations

import pytest

from modelctl.core.all_service import (
    ComponentResult,
    resolve_default_profile,
    start_gateway,
    start_profile,
    status_gateway,
    status_stats,
    stop_gateway,
    stop_profile,
    stop_stats,
)
from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import Profile
from modelctl.engines.base import RequirementError


def _profile(name: str = "m", aliases: list[str] | None = None) -> Profile:
    return Profile(
        name=name, engine="llamacpp", port=18080,
        aliases=aliases or [], engine_config={"model": "m.gguf"},
    )


class _FakeProc:
    """假 Popen：poll() 固定返回给定退出码（None = 存活）。"""

    def __init__(self, exit_code=None):
        self._exit = exit_code

    def poll(self):
        return self._exit


class _FakeAdapter:
    """可配置 wait_ready 结果的假引擎适配器。"""

    def __init__(self, profile, caps, ready: bool = True):
        self.warnings: list[str] = []
        self.profile = profile
        self._ready = ready

    def check_requirements(self):
        return None

    def pre_start(self):
        return None

    def build_command(self):
        return ["echo", "hi"], {}

    def wait_ready(self, timeout):
        return self._ready

    def upstream_api_key(self):
        return None

    def post_start(self):
        return None

    def metrics_mapping(self):
        return None


# ---- 默认模型解析 ----

def test_resolve_default_profile_by_name(tmp_path):
    (tmp_path / "a.yaml").write_text(
        "name: deepseek-v4-flash-llamacpp\n"
        "alias: deepseek-v4-flash\n"
        "engine: llamacpp\n"
        "port: 18080\n"
        "llamacpp:\n  model: m.gguf\n",
        encoding="utf-8",
    )
    r = resolve_default_profile(tmp_path, "deepseek-v4-flash-llamacpp")
    assert r is not None and r.name == "deepseek-v4-flash-llamacpp"


def test_resolve_default_profile_by_alias(tmp_path):
    (tmp_path / "a.yaml").write_text(
        "name: deepseek-v4-flash-llamacpp\n"
        "alias: deepseek-v4-flash\n"
        "engine: llamacpp\n"
        "port: 18080\n"
        "llamacpp:\n  model: m.gguf\n",
        encoding="utf-8",
    )
    r = resolve_default_profile(tmp_path, "deepseek-v4-flash")
    assert r is not None and r.name == "deepseek-v4-flash-llamacpp"


def test_resolve_default_profile_fallback(tmp_path, monkeypatch):
    (tmp_path / "a.yaml").write_text(
        "name: deepseek-v4-flash-llamacpp\n"
        "alias: deepseek-v4-flash\n"
        "engine: llamacpp\n"
        "port: 18080\n"
        "llamacpp:\n  model: m.gguf\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("GATEWAY_DEFAULT_MODEL", raising=False)
    assert resolve_default_profile(tmp_path, None) is not None  # 回退 deepseek-v4-flash 经 alias 命中


def test_resolve_default_profile_missing(tmp_path):
    assert resolve_default_profile(tmp_path, "nonexistent") is None


# ---- 模型原语 ----

def test_start_profile_skips_when_running(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: True)
    r = start_profile(_profile(), Capabilities(), 5.0)
    assert r.status == "skipped" and r.component == "model:m"


def test_start_profile_check_raises_requirement(monkeypatch):
    """check_requirements 失败应抛 RequirementError（cli 依赖 main 捕获返回 2）。"""
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: False)

    class _FailingAdapter:
        def __init__(self, profile, caps):
            pass

        def check_requirements(self):
            raise RequirementError("无法运行")

    monkeypatch.setattr(all_service, "get_adapter", lambda engine: _FailingAdapter)
    with pytest.raises(RequirementError):
        start_profile(_profile(), Capabilities(), 5.0)


def test_start_profile_ok(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: False)
    monkeypatch.setattr(all_service, "get_adapter", lambda engine: lambda p, c: _FakeAdapter(p, c, ready=True))
    monkeypatch.setattr(all_service, "start_detached", lambda name, cmd, env: (123, _FakeProc(None)))
    r = start_profile(_profile(), Capabilities(), 5.0)
    assert r.status == "ok" and "18080" in r.detail


def test_start_profile_health_timeout(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: False)
    monkeypatch.setattr(all_service, "get_adapter", lambda engine: lambda p, c: _FakeAdapter(p, c, ready=False))
    # 进程仍存活（poll() → None）：走"健康检查超时"分支而非"提前退出"
    monkeypatch.setattr(all_service, "start_detached", lambda name, cmd, env: (123, _FakeProc(None)))
    monkeypatch.setattr(all_service, "launch_log", lambda name: None)
    r = start_profile(_profile(), Capabilities(), 5.0)
    assert r.status == "error" and "超时" in r.detail


def test_start_profile_early_exit(monkeypatch):
    """回归：引擎进程未能就绪前已退出时应报"提前退出"（此前空转满超时且只见 traceback 尾巴）。"""
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: False)
    monkeypatch.setattr(all_service, "get_adapter", lambda engine: lambda p, c: _FakeAdapter(p, c, ready=False))
    monkeypatch.setattr(all_service, "start_detached", lambda name, cmd, env: (123, _FakeProc(1)))
    monkeypatch.setattr(all_service, "launch_log", lambda name: None)
    r = start_profile(_profile(), Capabilities(), 5.0)
    assert r.status == "error" and "提前退出" in r.detail


def test_stop_profile_calls_stop_instance(monkeypatch):
    from modelctl.core import all_service

    calls: list[str] = []
    # get_adapter 须返回"类"（stop_profile 内部会 (profile, caps) 实例化），假类需可接收这两个参数
    monkeypatch.setattr(all_service, "get_adapter", lambda engine: type("A", (), {
        "__init__": lambda self, p, c: None,
        "stop_patterns": lambda self: ["llama-server"],
    }))
    monkeypatch.setattr(all_service, "stop_instance", lambda name, port, pat: calls.append(name))
    r = stop_profile(_profile(), Capabilities(), None)
    assert r.status == "ok" and calls == ["m"]


# ---- 网关 / 统计原语 ----

def test_start_gateway_skips_when_running(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: True)
    r = start_gateway()
    assert r.status == "skipped" and r.component == "gateway"


def test_start_gateway_ok(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: False)
    monkeypatch.setattr(all_service, "start_detached", lambda name, cmd, env: (1, None))
    r = start_gateway()
    assert r.status == "ok" and r.component == "gateway"


def test_status_gateway_stopped(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: False)
    assert status_gateway().detail == "已停止"


def test_status_stats_stopped(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running", lambda name: False)
    assert status_stats().detail == "已停止"


def test_stop_gateway_and_stats(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "stop_instance", lambda *a, **k: True)
    assert stop_gateway().status == "ok"
    assert stop_stats().status == "ok"


# ---- 一键编排 ----

def test_start_all_order_and_continue(tmp_path, monkeypatch):
    """默认模型解析失败 → error 但继续启动 gateway/stats。"""
    from modelctl.core import all_service

    calls: list[str] = []
    monkeypatch.setattr(all_service, "resolve_default_profile", lambda d, m: None)
    monkeypatch.setattr(
        all_service,
        "start_gateway",
        lambda: (calls.append("gateway") or ComponentResult("gateway", "ok", "")),
    )
    monkeypatch.setattr(
        all_service,
        "start_stats",
        lambda: (calls.append("stats") or ComponentResult("stats", "ok", "")),
    )
    results = all_service.start_all(tmp_path, "nonexistent")
    assert results[0].status == "error" and "model" in results[0].component
    assert calls == ["gateway", "stats"]  # 模型失败不阻断后续


def test_start_all_starts_default_model_first(tmp_path, monkeypatch):
    from modelctl.core import all_service

    order: list[str] = []
    monkeypatch.setattr(all_service, "resolve_default_profile", lambda d, m: _profile("m"))
    monkeypatch.setattr(
        all_service,
        "start_profile",
        lambda p, c, t: (order.append("model") or ComponentResult("model:m", "ok", "")),
    )
    monkeypatch.setattr(
        all_service,
        "start_gateway",
        lambda: (order.append("gateway") or ComponentResult("gateway", "ok", "")),
    )
    monkeypatch.setattr(
        all_service,
        "start_stats",
        lambda: (order.append("stats") or ComponentResult("stats", "ok", "")),
    )
    all_service.start_all(tmp_path)
    assert order == ["model", "gateway", "stats"]


def test_stop_all_stops_every_running_model(tmp_path, monkeypatch):
    """stop 遍历全部 profiles，非默认模型也停；未运行不记录。"""
    from modelctl.core import all_service

    stopped: list[str] = []
    monkeypatch.setattr(all_service, "probe", lambda: Capabilities())
    monkeypatch.setattr(all_service, "stop_stats", lambda: ComponentResult("stats", "ok", ""))
    monkeypatch.setattr(all_service, "stop_gateway", lambda: ComponentResult("gateway", "ok", ""))
    monkeypatch.setattr(all_service, "list_profiles", lambda d: [_profile("a"), _profile("b")])
    monkeypatch.setattr(all_service, "is_running", lambda name: name == "a")  # 仅 a 运行
    monkeypatch.setattr(
        all_service,
        "stop_profile",
        lambda p, c, d: (stopped.append(p.name) or ComponentResult(f"model:{p.name}", "ok", "")),
    )
    results = all_service.stop_all(tmp_path)
    assert [r.component for r in results] == ["stats", "gateway", "model:a"]  # 顺序 stats→gateway→模型；b 未运行不记录
    assert stopped == ["a"]


def test_restart_all_only_default_model(tmp_path, monkeypatch):
    from modelctl.core import all_service

    restarted: list[str] = []
    monkeypatch.setattr(all_service, "resolve_default_profile", lambda d, m: _profile("m"))
    monkeypatch.setattr(
        all_service,
        "restart_profile",
        lambda p, c, t: (restarted.append(p.name) or ComponentResult("model:m", "ok", "")),
    )
    monkeypatch.setattr(all_service, "restart_gateway", lambda: ComponentResult("gateway", "ok", ""))
    monkeypatch.setattr(all_service, "restart_stats", lambda: ComponentResult("stats", "ok", ""))
    all_service.restart_all(tmp_path)
    assert restarted == ["m"]  # 仅默认模型
