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
    monkeypatch.setattr(cli, "envs_setup", lambda engine: 0)

    class _Args:
        engine = "vllm"

    rc = cli._cmd_env_setup(_Args(), None, None)
    assert rc == 0
    assert "vllm" in capsys.readouterr().out


def test_cmd_env_setup_engine_env_error(monkeypatch, capsys):
    """envs_setup 抛 EngineEnvError（如未找到 uv）→ _cmd_env_setup 返回 2。"""
    monkeypatch.setattr(cli, "envs_setup", lambda engine: (_ for _ in ()).throw(EngineEnvError("未找到 uv")))

    class _Args:
        engine = "vllm"

    rc = cli._cmd_env_setup(_Args(), None, None)
    assert rc == 2


def test_cmd_env_setup_nonzero_exit(monkeypatch, capsys):
    """envs_setup 返回非 0 → _cmd_env_setup 透传退出码。"""
    monkeypatch.setattr(cli, "envs_setup", lambda engine: 3)

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
