#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_all_service.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : all_service 编排测试
# ===============================================================================

"""all_service 单组件原语与默认模型解析测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
        self.spawned_proc = None

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

    def is_docker_runtime(self) -> bool:
        """Task 4 引入的 start_profile 必填项（docker runtime 决定 write_pid）。默认 False。"""
        return False

    def stop_backend(self) -> None:
        """Task 4 引入的 stop_profile 必填项（默认 venv 路径；docker 子类覆盖为 stop_docker_instance）。"""
        # 该路径会被 stop_gateway/stop_stats 等 非 model 分支 测试命中（实际走 stop_instance 直接打 all_service.stop_instance）
        # ——  fake adapter 的 stop_backend 无实际副作用，调用无错误即可。
        return None

    def backend_dead(self):
        """mirror base.EngineAdapter：本工具拉起的进程早退即视为死亡（docker 子类覆盖为容器状态探测）。"""
        return self.spawned_proc is not None and self.spawned_proc.poll() is not None


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

    # Task 4 后运行态判定走 is_running_any(name, profile)；保留 is_running 的旧 mock 作兼容
    monkeypatch.setattr(all_service, "is_running_any", lambda name, p: True)
    r = start_profile(_profile(), Capabilities(), 5.0)
    assert r.status == "skipped" and r.component == "model:m"


def test_start_profile_check_raises_requirement(monkeypatch):
    """check_requirements 失败应抛 RequirementError（cli 依赖 main 捕获返回 2）。"""
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running_any", lambda name, p: False)
    monkeypatch.setattr(all_service, "port_in_use", lambda port: False)

    class _FailingAdapter:
        def __init__(self, profile, caps):
            pass

        def check_requirements(self):
            raise RequirementError("无法运行")

    monkeypatch.setattr(all_service, "get_adapter", lambda engine: _FailingAdapter)
    with pytest.raises(RequirementError):
        start_profile(_profile(), Capabilities(), 5.0)


def test_start_profile_port_in_use_raises(monkeypatch):
    """端口被外部占用时启动前即抛 RequirementError，且点名占用者。"""
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running_any", lambda name, p: False)
    monkeypatch.setattr(all_service, "port_in_use", lambda port: True)
    monkeypatch.setattr(all_service, "describe_port_listener", lambda port: "PID 4242")

    def _boom(*a, **kw):  # 走到适配器说明预检没拦住
        raise AssertionError("端口占用应在 check_requirements 之前被拦截")

    monkeypatch.setattr(all_service, "get_adapter", _boom)
    with pytest.raises(RequirementError, match="端口 18080 已被占用（PID 4242）"):
        start_profile(_profile(), Capabilities(), 5.0)


def test_start_profile_ollama_shares_port(monkeypatch):
    """ollama 多 profile 共享同一 serve 端口是设计语义，端口被占不得拦截。"""
    from modelctl.core import all_service

    p = Profile(name="ollama-b", engine="ollama", port=11434, engine_config={"model": "m:1"})
    monkeypatch.setattr(all_service, "is_running_any", lambda name, prof: False)
    monkeypatch.setattr(all_service, "port_in_use", lambda port: True)

    started: list = []

    class _Ok(_FakeAdapter):
        def check_requirements(self):
            started.append("checked")

    monkeypatch.setattr(all_service, "get_adapter", lambda engine: lambda prof, caps: _Ok(prof, caps, ready=True))
    monkeypatch.setattr(all_service, "start_detached", lambda *a, **kw: (999, _FakeProc()))
    r = start_profile(p, Capabilities(), 5.0)
    assert started == ["checked"] and r.status == "ok"


def test_start_profile_ok(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running_any", lambda name, p: False)
    monkeypatch.setattr(all_service, "get_adapter", lambda engine: lambda p, c: _FakeAdapter(p, c, ready=True))
    # Task 4 后 start_detached 必带 write_pid kwarg（venv is_docker_runtime False → True）
    monkeypatch.setattr(all_service, "start_detached",
                        lambda name, cmd, env, write_pid: (123, _FakeProc(None)))
    r = start_profile(_profile(), Capabilities(), 5.0)
    assert r.status == "ok" and "18080" in r.detail


def test_start_profile_health_timeout(monkeypatch):
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running_any", lambda name, p: False)
    monkeypatch.setattr(all_service, "get_adapter", lambda engine: lambda p, c: _FakeAdapter(p, c, ready=False))
    # 进程仍存活（poll() → None）：走"健康检查超时"分支而非"提前退出"
    monkeypatch.setattr(all_service, "start_detached",
                        lambda name, cmd, env, write_pid: (123, _FakeProc(None)))
    monkeypatch.setattr(all_service, "launch_log", lambda name: None)
    r = start_profile(_profile(), Capabilities(), 5.0)
    assert r.status == "error" and "超时" in r.detail


def test_start_profile_early_exit(monkeypatch):
    """回归：引擎进程未能就绪前已退出时应报"提前退出"（此前空转满超时且只见 traceback 尾巴）。"""
    from modelctl.core import all_service

    monkeypatch.setattr(all_service, "is_running_any", lambda name, p: False)
    monkeypatch.setattr(all_service, "get_adapter", lambda engine: lambda p, c: _FakeAdapter(p, c, ready=False))
    monkeypatch.setattr(all_service, "start_detached",
                        lambda name, cmd, env, write_pid: (123, _FakeProc(1)))
    monkeypatch.setattr(all_service, "launch_log", lambda name: None)
    r = start_profile(_profile(), Capabilities(), 5.0)
    assert r.status == "error" and "提前退出" in r.detail


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
    # Task 4 后 stop_all 走 is_running_any(name, profile) 双参；is_running 旧 mock 作废
    monkeypatch.setattr(all_service, "is_running_any", lambda name, p: name == "a")  # 仅 a 运行
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


# ---- Task 4 回归：start_profile 的 write_pid 分派 / stop_profile 的 stop_backend 委派 /
#      stop_all 改走 is_running_any ----

def test_start_profile_docker_write_pid_false(monkeypatch, tmp_path):
    """docker runtime（is_docker_runtime True）时 start_detached 必须收到 write_pid=False。

    - fake adapter 启 docker 路径：is_docker_runtime() → True
    - start_detached 由 MagicMock 接住，从 kwargs 取 write_pid 字段断言 is False
    - 旧代码 start_detached(profile.name, cmd, env) 不传 write_pid → kwargs 缺失 →
      fake 直接 KeyError → RED（测试失败）。
    - patch 目标必须打到 all_service 命名空间（`from modelctl.engines import get_adapter` 在
      导入时已绑定），否则真实 VllmAdapter 跑起来需要 docker / nvidia-smi 都在 PATH。
    """
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import Profile
    from modelctl.core.all_service import start_profile

    profile = Profile(name="qd", engine="vllm", port=8100,
                      engine_config={"docker_image": "vllm/vllm-openai:test", "model": "/m"})
    fake_adapter = MagicMock()
    fake_adapter.profile = profile
    fake_adapter.is_docker_runtime.return_value = True
    fake_adapter.build_command.return_value = (["docker", "run", "x"], {})
    fake_adapter.selected_gpus.return_value = None
    fake_adapter.wait_ready.return_value = True
    fake_adapter.upstream_api_key.return_value = None
    fake_adapter.metrics_mapping.return_value = None

    fake_proc = MagicMock(); fake_proc.pid = 123
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    with (
        # patch all_service 命名空间，使 start_profile 内的 get_adapter 走 fake
        patch("modelctl.core.all_service.get_adapter",
              return_value=lambda p, c: fake_adapter),
        patch("modelctl.core.all_service.is_running_any", return_value=False),
        patch("modelctl.core.all_service.start_detached",
              return_value=(123, fake_proc)) as fake_start,
        patch("modelctl.core.all_service.wait_health", return_value=True),
    ):
        r = start_profile(profile, Capabilities(), 1.0)
    assert r.status == "skipped" or r.status == "ok"
    # 关键断言：start_detached 必须以 write_pid=False 调用（docker 路径不写 PID）
    assert fake_start.call_args.kwargs["write_pid"] is False


def test_start_profile_venv_write_pid_default_true(monkeypatch, tmp_path):
    """venv runtime（is_docker_runtime False）时 start_detached 维持默认 write_pid=True（向后兼容）。

    RED 失败点：旧代码 start_detached(profile.name, cmd, env) kwargs 中无 write_pid，
    `fake_start.call_args.kwargs["write_pid"] is True` 会 KeyError。
   GREEN 校验：要么显式传 True、要么走默认（True），两种形态都接受。
    """
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import Profile
    from modelctl.core.all_service import start_profile

    profile = Profile(name="qv", engine="vllm", port=8101, engine_config={"model": "/m"})
    fake_adapter = MagicMock()
    fake_adapter.profile = profile
    fake_adapter.is_docker_runtime.return_value = False
    fake_adapter.build_command.return_value = (["vllm", "serve", "/m"], {})
    fake_adapter.selected_gpus.return_value = None
    fake_adapter.wait_ready.return_value = True
    fake_adapter.upstream_api_key.return_value = None
    fake_adapter.metrics_mapping.return_value = None
    fake_proc = MagicMock(); fake_proc.pid = 124
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    with (
        # patch all_service 命名空间，使 start_profile 内的 get_adapter 走 fake（避免
        # 真实 VllmAdapter 校验 venv 不存在而抛 EngineEnvError）
        patch("modelctl.core.all_service.get_adapter",
              return_value=lambda p, c: fake_adapter),
        patch("modelctl.core.all_service.is_running_any", return_value=False),
        patch("modelctl.core.all_service.start_detached",
              return_value=(124, fake_proc)) as fake_start,
    ):
        start_profile(profile, Capabilities(), 1.0)
    # venv 路径：要么不传 write_pid（默认 True），要么显式传 True
    kw = fake_start.call_args.kwargs
    assert kw.get("write_pid", True) is True


def test_stop_profile_calls_adapter_stop_backend(monkeypatch, tmp_path):
    """stop_profile 非 ollama 分支改走 `adapter.stop_backend()`，不再直接 stop_instance。

    用 MagicMock 作为 fake adapter 实例（注意：get_adapter 仍返回"类"=lambda p, c 以保留
    (profile, caps) 实例化约定，但实际不做任何事）。使用 `_profile()`（engine 默认 llamacpp）
    避开 vllm 真实适配器的 docker/venv 环境校验。
    RED 失败点：旧代码 stop_profile 走 `stop_instance(...)` → fake_adapter.stop_backend
    未被调用 → `fake_adapter.stop_backend.assert_called_once_with()` 失败。
    """
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.all_service import stop_profile

    fake_adapter = MagicMock()
    fake_adapter.profile = _profile()  # engine="llamacpp"：非 ollama 分支
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    # patch all_service 命名空间（`from modelctl.engines import get_adapter` 已绑定到本模块），
    # 使 stop_profile 内 `get_adapter(profile.engine)(profile, caps)` 走 fake 而非真实
    # LlamacppAdapter（后者构造也不出错但 stop_backend 走基类会真止进程）。
    monkeypatch.setattr("modelctl.core.all_service.get_adapter",
                        lambda engine: lambda p, c: fake_adapter)
    r = stop_profile(fake_adapter.profile, Capabilities(), tmp_path)
    assert r.status == "ok"
    fake_adapter.stop_backend.assert_called_once_with()
    # stop_instance 旧路径不应被直接调用（stop_backend 内部才调；fake 用 MagicMock 不真做副作用）


def test_stop_all_uses_is_running_any(monkeypatch, tmp_path):
    """一键关闭按 is_running_any(name, profile) 判是否 stop（docker 容器在跑也要 stop）。

    以 seen 列表登记 is_running_any 被询问的 profile 列表 → 两个 profile 都需被询问。
    RED 失败点：旧代码 stop_all 走 `is_running(profile.name)`（单参），is_running_any 不被
    调用，seen 列表为空 → 断言失败。
    """
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import Profile
    from modelctl.core.all_service import stop_all

    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    profiles = [Profile(name="n1", engine="ollama", port=11434,
                        engine_config={"model": "/m"}),
                Profile(name="n2", engine="vllm", port=8113,
                        engine_config={"docker_image": "x", "model": "/m"})]
    monkeypatch.setattr("modelctl.core.all_service.list_profiles", lambda d: profiles)
    seen = []
    monkeypatch.setattr("modelctl.core.all_service.is_running_any",
                        lambda n, p: seen.append(n) or (n == "n2"))
    monkeypatch.setattr("modelctl.core.all_service.stop_stats",
                        lambda: stop_all.__globals__["ComponentResult"]("s", "ok"))
    monkeypatch.setattr("modelctl.core.all_service.stop_gateway",
                        lambda: stop_all.__globals__["ComponentResult"]("g", "ok"))
    monkeypatch.setattr("modelctl.core.all_service.stop_profile",
                        lambda p, c, d: stop_all.__globals__["ComponentResult"](f"m:{p.name}", "ok"))
    r = stop_all(tmp_path)
    assert "n2" in seen and "n1" in seen  # 两个 profile 都问过了 is_running_any
    assert len([x for x in r if x.component.startswith("m:")]) == 1  # 仅 n2（is_running_any True）
