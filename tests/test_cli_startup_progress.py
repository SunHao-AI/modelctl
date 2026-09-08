"""CLI：--timeout 未显式指定时按运行时自适应；start 传 on_progress 打单行进度。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from modelctl import cli
from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import Profile

CAPS = Capabilities(gpu_count=1, compute_capability="8.9", binaries={})
_P = Profile(name="q", engine="vllm", port=8000, engine_config={"model": "/m/x"})


def test_start_timeout_none_delegates(monkeypatch):
    args = SimpleNamespace(timeout=None)
    with mock.patch.object(cli.all_service, "default_start_timeout", return_value=1800.0) as d:
        assert cli._start_timeout(args, _P, CAPS) == 1800.0
    d.assert_called_once_with(_P, CAPS)


def test_start_timeout_explicit_wins(monkeypatch):
    args = SimpleNamespace(timeout=42)
    with mock.patch.object(cli.all_service, "default_start_timeout", return_value=1800.0):
        assert cli._start_timeout(args, _P, CAPS) == 42.0


def test_cmd_start_passes_progress_callback():
    events = []
    # CPython 每次 `events.append` 属性访问都新建 builtin method 对象，`is` 比较必须先固定引用
    sink = events.append
    with mock.patch.object(cli.all_service, "start_profile", return_value=SimpleNamespace(
            status="ok", detail="ok")) as sp, \
         mock.patch.object(cli, "load_profile", return_value=_P), \
         mock.patch.object(cli, "_start_timeout", return_value=600.0), \
         mock.patch.object(cli, "_cli_progress", return_value=sink):
        assert cli._cmd_start(SimpleNamespace(name="q", timeout=None), None, CAPS) == 0
    assert sp.call_args.kwargs.get("on_progress") is sink


def test_cli_progress_line_single_and_label():
    from modelctl.core.startup_progress import StageEvent

    msgs: list[str] = []
    sink = cli._cli_progress(_P)
    sink(StageEvent("prepare_env", "running", "拉取镜像 img:tag（3/9 层）", pct=0.4, eta_s=120))
    # 通过 logger 捕获验证单行 + 关键内容
    from loguru import logger
    logger.remove()
    logger.add(lambda m: msgs.append(m.record["message"]))
    sink(StageEvent("prepare_env", "running", "拉取镜像 img:tag（3/9 层）", pct=0.4, eta_s=120))
    assert len(msgs) == 1 and "\n" not in msgs[0]
    assert "拉取镜像" in msgs[0] and "40%" in msgs[0] and "约剩 2 分钟" in msgs[0]


def test_cli_progress_error_state_shows_error():
    from modelctl.core.startup_progress import StageEvent

    msgs: list[str] = []
    from loguru import logger
    logger.remove()
    logger.add(lambda m: msgs.append(m.record["message"]))
    cli._cli_progress(_P)(StageEvent("preflight", "error", "依赖检查", error="docker 不在 PATH"))
    assert "docker 不在 PATH" in msgs[-1]
