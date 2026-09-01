#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_engines_aphrodite.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/1 10:00
# @Desc   : Aphrodite 引擎适配器测试
# ===============================================================================

import os

import pytest

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import load_profile
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError

CAPS8 = Capabilities(gpu_count=8, compute_capability="8.9", binaries={"aphrodite": True})


def _write(tmp_path, text, name="m.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return load_profile(name[:-5], tmp_path)


def _stub_venv(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.envs.VENV_ROOT", tmp_path / ".venvs")
    bin_dir = tmp_path / ".venvs" / "aphrodite" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True, exist_ok=True)
    # ensure_env 通过 engine_python 判断环境是否存在；引擎二进制供 build_command 使用
    (bin_dir / ("python.exe" if os.name == "nt" else "python")).write_bytes(b"fake")
    (bin_dir / ("aphrodite.exe" if os.name == "nt" else "aphrodite")).write_bytes(b"fake")
    return bin_dir


def test_aphrodite_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    _stub_venv(tmp_path, monkeypatch)
    p = _write(
        tmp_path,
        "name: q\nengine: aphrodite\nport: 8140\napi_key: sk-test\naphrodite:\n"
        "  model: /models/Qwen3.8-27B-Q4_K_M.gguf\n  tensor_parallel_size: 1\n"
        "  quantization: gguf\n  max_model_len: 32768\n"
        '  extra_args: "--disable-log-requests"\n',
    )
    a = get_adapter("aphrodite")(p, CAPS8)
    a.check_requirements()
    cmd, env = a.build_command()
    assert str(cmd[0]).endswith("aphrodite.exe") or str(cmd[0]).endswith("aphrodite")
    assert cmd[1] == "run"
    assert cmd[2] == "/models/Qwen3.8-27B-Q4_K_M.gguf"
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0"
    assert cmd[cmd.index("--port") + 1] == "8140"
    assert cmd[cmd.index("--tensor-parallel-size") + 1] == "1"
    assert cmd[cmd.index("--quantization") + 1] == "gguf"
    assert cmd[cmd.index("--max-model-len") + 1] == "32768"
    assert cmd[cmd.index("--served-model-name") + 1] == "q"
    assert cmd[cmd.index("--api-key") + 1] == "sk-test"
    assert "--disable-log-requests" in cmd


def test_aphrodite_metrics(tmp_path):
    p = _write(tmp_path, "name: q\nengine: aphrodite\nport: 8140\napi_key: sk-test\naphrodite:\n  model: m\n")
    a = get_adapter("aphrodite")(p, CAPS8)
    mapping = a.metrics_mapping()
    assert "prompt_total" in mapping
    assert "predicted_total" in mapping


def test_aphrodite_missing_model_raises(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch)
    p = _write(tmp_path, "name: q\nengine: aphrodite\nport: 8140\naphrodite:\n  tensor_parallel_size: 1\n")
    a = get_adapter("aphrodite")(p, CAPS8)
    with pytest.raises(RequirementError, match="aphrodite.model"):
        a.check_requirements()


def test_aphrodite_tp_exceeds(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch)
    p = _write(tmp_path, "name: q\nengine: aphrodite\nport: 8140\naphrodite:\n  model: m\n  tensor_parallel_size: 16\n")
    a = get_adapter("aphrodite")(p, CAPS8)
    with pytest.raises(RequirementError, match="GPU"):
        a.check_requirements()


def test_aphrodite_tp_mismatch_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    _stub_venv(tmp_path, monkeypatch)
    p = _write(
        tmp_path,
        "name: q\nengine: aphrodite\nport: 8140\naphrodite:\n"
        "  model: m\n  tensor_parallel_size: 4\n  gpu_list: '1,2'\n",
    )
    a = get_adapter("aphrodite")(p, CAPS8)
    with pytest.raises(RequirementError):
        a.check_requirements()


def test_aphrodite_health_url_default(tmp_path):
    p = _write(tmp_path, "name: q\nengine: aphrodite\nport: 8140\naphrodite:\n  model: m\n")
    a = get_adapter("aphrodite")(p, CAPS8)
    assert a.health_url() == "http://127.0.0.1:8140/health"
