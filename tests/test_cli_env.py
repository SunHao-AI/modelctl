#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cli_env.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/8/27 10:00
# @Desc   : modelctl env 命令族（parser 注册 + 分发 + 三命令）测试
# ===============================================================================

"""modelctl env 命令族测试。"""

from __future__ import annotations

import pytest

from modelctl import cli
from modelctl.core.envs import MANAGED_ENGINES, EngineEnvError


def _main_without_probe(monkeypatch, argv: list[str]) -> int:
    """绕过真实硬件探测（nvidia-smi 在 Windows 上不可用 / 慢）。"""
    class _Caps:
        gpu_count = 0
        gpu_name = None
        vram_free_mb = [0]
        vram_total_mb = None
        cuda_driver = None
        compute_capability = None
        binaries: dict = {}
        binary_paths: dict = {}

    monkeypatch.setattr("modelctl.cli.probe", lambda: _Caps())
    return cli.main(argv)


def test_parser_env_setup(monkeypatch):
    """build_parser 注册 env setup 子命令并解析到 action/engine。"""
    args = cli.build_parser().parse_args(["env", "setup", "vllm"])
    assert args.command == "env"
    assert args.action == "setup"
    assert args.engine == "vllm"


def test_parser_env_list_no_engine(monkeypatch):
    """env list 不接收 engine。"""
    args = cli.build_parser().parse_args(["env", "list"])
    assert args.command == "env"
    assert args.action == "list"
    assert args.engine is None


@pytest.mark.parametrize("engine", ["vllm", "sglang"])
def test_main_env_list_dispatch(monkeypatch, tmp_path, capsys, engine):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(cli, "_cmd_env_list", lambda args, models_dir, caps: 0)
    rc = _main_without_probe(monkeypatch, ["env", "list"])
    assert rc == 0


def test_main_env_remove_dispatch(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(cli, "_cmd_env_remove", lambda args, models_dir, caps: 0)
    rc = _main_without_probe(monkeypatch, ["env", "remove", "sglang"])
    assert rc == 0


def test_main_env_setup_bogus_rejected(monkeypatch, tmp_path, capsys):
    """engine 不在 MANAGED_ENGINES → _cmd_env_setup 拒绝并返回 2。

    engine 是 nargs="?" 自由字符串，不支持 argparse choices 校验（"list" 不需要
    engine，无法做命令行级 choices 校验），因此由 func 内部做覆盖校验。
    """
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    called: dict = {}

    def _boom(engine: str):
        called["engine"] = engine
        return 0

    monkeypatch.setattr(cli, "envs_setup", _boom)

    class _Args:
        engine = "bogus"

    rc = cli._cmd_env_setup(_Args(), None, None)
    assert rc == 2
    assert "bogus" not in MANAGED_ENGINES
    assert called == {}  # _cmd_env_setup 拒绝早于 envs_setup


def test_cmd_env_setup_success(monkeypatch, capsys):
    """_cmd_env_setup 成功路径 → 返回 0。"""
    monkeypatch.setattr(cli, "envs_setup", lambda engine, **kw: 0)

    class _Args:
        engine = "vllm"

    rc = cli._cmd_env_setup(_Args(), None, None)
    assert rc == 0
    assert "vllm" in capsys.readouterr().out


def test_cmd_env_setup_passes_offline_options(monkeypatch, tmp_path):
    """--wheels/--offline → 转成 envs_setup 的 wheels_dir/offline。"""
    called = {}

    def _fake(engine, *, wheels_dir=None, offline=False):
        called["engine"], called["wheels_dir"], called["offline"] = engine, wheels_dir, offline
        return 0

    monkeypatch.setattr(cli, "envs_setup", _fake)

    class _Args:
        engine = "vllm"
        wheels = str(tmp_path)
        offline = True

    assert cli._cmd_env_setup(_Args(), None, None) == 0
    assert called["wheels_dir"] == tmp_path
    assert called["offline"] is True


def test_cmd_env_setup_offline_defaults(monkeypatch):
    """未给 --wheels/--offline → wheels_dir=None、offline=False。"""
    called = {}

    def _fake(engine, *, wheels_dir=None, offline=False):
        called["wheels_dir"], called["offline"] = wheels_dir, offline
        return 0

    monkeypatch.setattr(cli, "envs_setup", _fake)

    class _Args:
        engine = "vllm"

    assert cli._cmd_env_setup(_Args(), None, None) == 0
    assert called["wheels_dir"] is None
    assert called["offline"] is False


def test_cmd_env_setup_engine_env_error(monkeypatch, capsys):
    """envs_setup 抛 EngineEnvError（如未找到 uv）→ _cmd_env_setup 返回 2。"""
    monkeypatch.setattr(
        cli, "envs_setup", lambda engine, **kw: (_ for _ in ()).throw(EngineEnvError("未找到 uv"))
    )

    class _Args:
        engine = "vllm"

    rc = cli._cmd_env_setup(_Args(), None, None)
    assert rc == 2


def test_cmd_env_setup_nonzero_exit(monkeypatch, capsys):
    """envs_setup 返回非 0 → _cmd_env_setup 透传退出码。"""
    monkeypatch.setattr(cli, "envs_setup", lambda engine, **kw: 3)

    class _Args:
        engine = "vllm"

    rc = cli._cmd_env_setup(_Args(), None, None)
    assert rc == 3


def test_cmd_env_setup_missing_engine(monkeypatch, capsys):
    """未指定 engine → _cmd_env_setup 返回 2。"""
    monkeypatch.setattr(cli, "envs_setup", lambda engine: 0)

    class _Args:
        engine = None

    rc = cli._cmd_env_setup(_Args(), None, None)
    assert rc == 2


def test_cmd_env_list_output(monkeypatch, capsys):
    """_cmd_env_list 输出各引擎环境状态。"""
    vllm_state = {"exists": True, "python": "3.12.1", "packages": {"vllm": "0.27.0"}}
    sglang_state = {"exists": False}
    monkeypatch.setattr(
        cli,
        "envs_status",
        lambda: {"vllm": vllm_state, "sglang": sglang_state},
    )

    class _Args:
        pass

    rc = cli._cmd_env_list(_Args(), None, None)
    assert rc == 0
    out = capsys.readouterr().out
    assert "python 3.12.1" in out
    assert "vllm 0.27.0" in out
    assert "sglang" in out
    assert "modelctl env setup sglang" in out


def test_cmd_env_remove_success(monkeypatch, capsys):
    """_cmd_env_remove 成功 → 返回 0 并打印提示。"""
    monkeypatch.setattr(cli, "envs_remove", lambda engine: None)

    class _Args:
        engine = "sglang"

    rc = cli._cmd_env_remove(_Args(), None, None)
    assert rc == 0
    assert "sglang" in capsys.readouterr().out


def test_cmd_env_remove_missing_engine(monkeypatch, capsys):
    """未指定 engine → _cmd_env_remove 返回 2。"""
    monkeypatch.setattr(cli, "envs_remove", lambda engine: None)

    class _Args:
        engine = None

    rc = cli._cmd_env_remove(_Args(), None, None)
    assert rc == 2


# ---- env setup docker（诊断 / 指引 / --run） ----


def _docker_args(run: bool = False, mirrors: list[str] | None = None):
    class _Args:
        engine = "docker"
        wheels = None
        offline = False
    a = _Args()
    a.run = run
    a.registry_mirrors = mirrors
    return a


def test_parser_env_setup_docker():
    """parser 接受 docker 目标与 --run；--registry-mirror 可重复传成列表。"""
    args = cli.build_parser().parse_args(
        ["env", "setup", "docker", "--run",
         "--registry-mirror", "https://m1", "--registry-mirror", "https://m2"])
    assert args.engine == "docker"
    assert args.run is True
    assert args.registry_mirrors == ["https://m1", "https://m2"]


def test_parser_env_setup_docker_mirror_default_none():
    """未传 --registry-mirror → None（由 resolve_registry_mirrors 回退内置默认）。"""
    args = cli.build_parser().parse_args(["env", "setup", "docker"])
    assert args.registry_mirrors is None


def test_env_setup_docker_not_dispatch_to_envs_setup(monkeypatch):
    """docker 分支绝不能落到托管 venv 的 envs_setup。"""
    monkeypatch.setattr(
        cli, "envs_setup",
        lambda engine, **kw: (_ for _ in ()).throw(AssertionError("docker 不应走 envs_setup")),
    )
    monkeypatch.setattr(cli.docker_setup, "diagnose", lambda: [])
    assert cli._cmd_env_setup(_docker_args(), None, None) == 0


def test_cmd_env_setup_docker_all_ok(monkeypatch, capsys):
    from modelctl.core.docker_setup import Check
    monkeypatch.setattr(cli.docker_setup, "diagnose", lambda: [
        Check("docker_cli", "docker CLI", True, ""),
        Check("nvidia_toolkit", "toolkit", True, ""),
    ])
    assert cli._cmd_env_setup(_docker_args(), None, None) == 0
    out = capsys.readouterr().out
    assert "已就绪" in out


def test_cmd_env_setup_docker_prints_instructions(monkeypatch, capsys):
    """缺依赖且无 --run → 只打印指引，不调 run_install。"""
    from modelctl.core.docker_setup import Check
    monkeypatch.setattr(cli.docker_setup, "diagnose", lambda: [
        Check("docker_cli", "docker CLI", False, "docker 命令不在 PATH"),
    ])
    called: dict = {}
    monkeypatch.setattr(cli.docker_setup, "render_instructions",
                        lambda mirrors=None: called.update(mirrors=mirrors) or "SCRIPT")
    monkeypatch.setattr(cli.docker_setup, "run_install",
                        lambda mirrors=None: (_ for _ in ()).throw(AssertionError("不该执行安装")))
    assert cli._cmd_env_setup(_docker_args(mirrors=["https://m"]), None, None) == 0
    out = capsys.readouterr().out
    assert "SCRIPT" in out
    assert called["mirrors"] == ["https://m"]


def test_cmd_env_setup_docker_run_delegates(monkeypatch, capsys):
    """--run → 委托 docker_setup.run_install 并透传镜像列表与退出码。"""
    from modelctl.core.docker_setup import Check
    monkeypatch.setattr(cli.docker_setup, "diagnose", lambda: [
        Check("docker_cli", "docker CLI", False, ""),
    ])
    called: dict = {}
    monkeypatch.setattr(cli.docker_setup, "run_install",
                        lambda mirrors=None: called.update(mirrors=mirrors) or 5)
    assert cli._cmd_env_setup(_docker_args(run=True, mirrors=["https://x"]), None, None) == 5
    assert called["mirrors"] == ["https://x"]


def test_cmd_env_list_includes_docker_status(monkeypatch, capsys):
    """env list 末尾追加 docker 系统依赖状态（PATH 缺失 → 未就绪）。"""
    monkeypatch.setattr(cli, "envs_status", lambda: {})
    monkeypatch.setattr(cli.docker_setup, "path_level_missing", lambda: ["docker 命令不在 PATH"])
    assert cli._cmd_env_list(_Args_empty(), None, None) == 0
    out = capsys.readouterr().out
    assert "docker:" in out and "未就绪" in out and "modelctl env setup docker" in out


class _Args_empty:
    pass
