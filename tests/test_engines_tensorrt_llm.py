#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_engines_tensorrt_llm.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/1 10:00
# @Desc   : TensorRT-LLM 引擎适配器测试
# ===============================================================================

"""tests/test_engines_tensorrt_llm.py — TensorRT-LLM 引擎适配器测试。"""

from __future__ import annotations

import os

import pytest

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import load_profile
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError

CAPS8 = Capabilities(gpu_count=8, compute_capability="8.9", binaries={"tensorrt_llm": True, "docker": True})


def _write(tmp_path, text, name="m.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return load_profile(name[:-5], tmp_path)


def _stub_venv(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.envs.VENV_ROOT", tmp_path / ".venvs")
    bin_dir = tmp_path / ".venvs" / "tensorrt_llm" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True, exist_ok=True)
    # ensure_env 通过 engine_python 判断环境是否存在
    (bin_dir / ("python.exe" if os.name == "nt" else "python")).write_bytes(b"fake")
    return bin_dir


def test_tensorrt_llm_venv_command(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch)
    engine_dir = tmp_path / "engines" / "qwen3.8-tp4-fp8"
    engine_dir.mkdir(parents=True)
    p = _write(
        tmp_path,
        f"name: q\nengine: tensorrt_llm\nport: 8120\napi_key: sk-test\ntensorrt_llm:\n"
        f"  model: /models/Qwen3.8-27B\n  engine_dir: {engine_dir}\n"
        f"  tensor_parallel_size: 4\n  quantization: fp8\n"
        f"  max_input_len: 32768\n  max_output_len: 8192\n  max_batch_size: 64\n"
        f'  extra_args: "--use_fused_mlp"\n',
    )
    a = get_adapter("tensorrt_llm")(p, CAPS8)
    a.check_requirements()
    cmd, env = a.build_command()
    assert cmd[0].endswith("python.exe") or cmd[0].endswith("python")
    assert cmd[1] == "-m"
    assert cmd[2] == "tensorrt_llm.serve"
    assert cmd[3] == "/models/Qwen3.8-27B"
    assert cmd[cmd.index("--engine_dir") + 1] == str(engine_dir)
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0"
    assert cmd[cmd.index("--port") + 1] == "8120"
    assert cmd[cmd.index("--tp") + 1] == "4"
    assert cmd[cmd.index("--max_input_len") + 1] == "32768"
    assert cmd[cmd.index("--max_output_len") + 1] == "8192"
    assert cmd[cmd.index("--max_batch_size") + 1] == "64"
    assert "--use_fused_mlp" in cmd


def test_tensorrt_llm_docker_command(tmp_path, monkeypatch):
    engine_dir = tmp_path / "engines" / "qwen3.8-tp4-fp8"
    engine_dir.mkdir(parents=True)
    model_dir = tmp_path / "models" / "Qwen3.8-27B"
    model_dir.mkdir(parents=True)
    p = _write(
        tmp_path,
        f"name: q\nengine: tensorrt_llm\nport: 8120\napi_key: sk-test\ntensorrt_llm:\n"
        f"  model: {model_dir}\n  engine_dir: {engine_dir}\n"
        f"  tensor_parallel_size: 4\n"
        f"  docker_image: nvcr.io/nvidia/tensorrt-llm:latest\n"
        f'  extra_args: "--use_fused_mlp"\n',
    )
    a = get_adapter("tensorrt_llm")(p, CAPS8)
    a.check_requirements()
    cmd, env = a.build_command()
    assert cmd[0] == "docker"
    assert cmd[1] == "run"
    assert "--rm" in cmd
    assert "--detach" in cmd
    assert "--gpus" in cmd
    assert "nvcr.io/nvidia/tensorrt-llm:latest" in cmd
    assert f"{p.port}:8000" in cmd
    assert "--tp" in cmd
    assert cmd[cmd.index("--tp") + 1] == "4"
    assert "--use_fused_mlp" in cmd


def test_tensorrt_llm_metrics(tmp_path):
    p = _write(tmp_path, "name: q\nengine: tensorrt_llm\nport: 8120\ntensorrt_llm:\n  model: m\n  engine_dir: /e\n")
    a = get_adapter("tensorrt_llm")(p, CAPS8)
    mapping = a.metrics_mapping()
    assert "prompt_total" in mapping
    assert "predicted_total" in mapping


def test_tensorrt_llm_missing_model_raises(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch)
    p = _write(tmp_path, "name: q\nengine: tensorrt_llm\nport: 8120\ntensorrt_llm:\n  engine_dir: /e\n")
    a = get_adapter("tensorrt_llm")(p, CAPS8)
    with pytest.raises(RequirementError, match="tensorrt_llm.model"):
        a.check_requirements()


def test_tensorrt_llm_missing_engine_dir_raises(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch)
    p = _write(tmp_path, "name: q\nengine: tensorrt_llm\nport: 8120\ntensorrt_llm:\n  model: m\n")
    a = get_adapter("tensorrt_llm")(p, CAPS8)
    with pytest.raises(RequirementError, match="engine_dir"):
        a.check_requirements()


def test_tensorrt_llm_pre_start_warns_on_missing_engine_dir(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch)
    missing_dir = tmp_path / "not_exist"
    p = _write(
        tmp_path,
        f"name: q\nengine: tensorrt_llm\nport: 8120\ntensorrt_llm:\n"
        f"  model: m\n  engine_dir: {missing_dir}\n",
    )
    a = get_adapter("tensorrt_llm")(p, CAPS8)
    a.pre_start()
    assert any("engine_dir" in w for w in a.warnings)


def test_tensorrt_llm_tp_exceeds(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch)
    p = _write(
        tmp_path,
        "name: q\nengine: tensorrt_llm\nport: 8120\ntensorrt_llm:\n"
        "  model: m\n  engine_dir: /e\n  tensor_parallel_size: 16\n",
    )
    a = get_adapter("tensorrt_llm")(p, CAPS8)
    with pytest.raises(RequirementError, match="GPU"):
        a.check_requirements()


def test_tensorrt_llm_tp_mismatch_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    _stub_venv(tmp_path, monkeypatch)
    p = _write(
        tmp_path,
        "name: q\nengine: tensorrt_llm\nport: 8120\ntensorrt_llm:\n"
        "  model: m\n  engine_dir: /e\n  tensor_parallel_size: 4\n  gpu_list: '1,2'\n",
    )
    a = get_adapter("tensorrt_llm")(p, CAPS8)
    with pytest.raises(RequirementError):
        a.check_requirements()


def test_tensorrt_llm_health_url(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch)
    p = _write(
        tmp_path,
        "name: q\nengine: tensorrt_llm\nport: 8120\ntensorrt_llm:\n"
        "  model: m\n  engine_dir: /e\n",
    )
    a = get_adapter("tensorrt_llm")(p, CAPS8)
    assert a.health_url() == "http://127.0.0.1:8120/health"


def test_tensorrt_llm_stop_patterns_venv(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch)
    p = _write(
        tmp_path,
        "name: q\nengine: tensorrt_llm\nport: 8120\ntensorrt_llm:\n"
        "  model: m\n  engine_dir: /e\n",
    )
    a = get_adapter("tensorrt_llm")(p, CAPS8)
    assert "tensorrt_llm.serve" in a.stop_patterns()
