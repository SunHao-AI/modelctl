#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_engines_tokenspeed.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/1 10:00
# @Desc   : TokenSpeed 适配器测试
# ===============================================================================

import os

import pytest

import modelctl.core.compat_rules  # noqa: F401 —— 导入即注册
from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import load_profile
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError

CAPS8 = Capabilities(gpu_count=8, compute_capability="8.9", binaries={"docker": True})


def _write(tmp_path, text, name="m.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return load_profile(name[:-5], tmp_path)


def _stub_venv(tmp_path, monkeypatch):
    """把 envs.VENV_ROOT 重定向到 tmp_path/.venvs 并创建 tokenspeed 最小 stub 目录。"""
    monkeypatch.setattr("modelctl.core.envs.VENV_ROOT", tmp_path / ".venvs")
    bin_dir = tmp_path / ".venvs" / "tokenspeed" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True, exist_ok=True)
    # has_env 以 python 解释器存在为判定；engine_bin 需要 tokenspeed 可执行文件
    py_name = "python.exe" if os.name == "nt" else "python"
    exe_name = "tokenspeed.exe" if os.name == "nt" else "tokenspeed"
    (bin_dir / py_name).write_bytes(b"fake")
    (bin_dir / exe_name).write_bytes(b"fake")
    return bin_dir


def test_tokenspeed_docker_command(tmp_path, monkeypatch):
    model_dir = tmp_path / "models" / "Qwen3.5-397B-A17B"
    model_dir.mkdir(parents=True)
    p = _write(
        tmp_path,
        f"name: q\nengine: tokenspeed\nport: 8150\napi_key: sk-test\ntokenspeed:\n"
        f"  model: {model_dir}\n  tensor_parallel_size: 8\n"
        f"  max_model_len: 131072\n"
        f"  docker_image: lightseekorg/tokenspeed:latest\n"
        f'  extra_args: "--enable-prefix-caching"\n',
    )
    a = get_adapter("tokenspeed")(p, CAPS8)
    a.check_requirements()
    cmd, env = a.build_command()
    assert cmd[0] == "docker"
    assert cmd[1] == "run"
    assert "--gpus" in cmd
    assert f"{p.port}:8000" in cmd  # 8150:8000
    assert "lightseekorg/tokenspeed:latest" in cmd
    assert f"/models/{model_dir.name}" in cmd
    assert cmd[cmd.index("--tp") + 1] == "8"
    assert "--enable-prefix-caching" in cmd


def test_tokenspeed_venv_command(tmp_path, monkeypatch):
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    _stub_venv(tmp_path, monkeypatch)
    model_dir = tmp_path / "models" / "Qwen3.5-397B-A17B"
    model_dir.mkdir(parents=True)
    p = _write(
        tmp_path,
        f"name: q\nengine: tokenspeed\nport: 8150\napi_key: sk-test\ntokenspeed:\n"
        f"  model: {model_dir}\n  tensor_parallel_size: 2\n"
        f"  max_model_len: 131072\n"
        f'  extra_args: "--enable-prefix-caching"\n',
    )
    a = get_adapter("tokenspeed")(p, CAPS8)
    a.check_requirements()
    cmd, env = a.build_command()
    assert str(cmd[0]).endswith("tokenspeed.exe") or str(cmd[0]).endswith("tokenspeed")
    assert cmd[1] == "serve"
    assert cmd[2] == str(model_dir)
    assert cmd[cmd.index("--tp") + 1] == "2"
    assert cmd[cmd.index("--max-model-len") + 1] == "131072"
    assert "--api-key" in cmd
    assert "--enable-prefix-caching" in cmd
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venvs" / "tokenspeed")


def test_tokenspeed_tp_mismatch_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    _stub_venv(tmp_path, monkeypatch)
    p = _write(
        tmp_path,
        "name: q\nengine: tokenspeed\nport: 8150\ntokenspeed:\n"
        "  model: /models/X\n  tensor_parallel_size: 4\n  gpu_list: '0,1'\n",
    )
    caps = Capabilities(
        gpu_count=8, gpu_indices=list(range(8)), compute_capability="8.9", binaries={"tokenspeed": True}
    )
    a = get_adapter("tokenspeed")(p, caps)
    with pytest.raises(RequirementError, match="tensor_parallel_size"):
        a.check_requirements()


def test_tokenspeed_metrics(tmp_path):
    p = _write(tmp_path, "name: q\nengine: tokenspeed\nport: 8150\ntokenspeed:\n  model: m\n")
    a = get_adapter("tokenspeed")(p, CAPS8)
    mapping = a.metrics_mapping()
    assert mapping["prompt_total"] == ["tokenspeed:prompt_tokens_total"]
    assert mapping["predicted_total"] == ["tokenspeed:generation_tokens_total"]
