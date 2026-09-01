#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_engines_lmdeploy.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/1 10:00
# @Desc   : LMDeploy 引擎适配器测试
# ===============================================================================

"""tests/test_engines_lmdeploy.py — LMDeploy 引擎适配器测试。"""

from __future__ import annotations

import os

import pytest

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import load_profile
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError

CAPS8 = Capabilities(gpu_count=8, compute_capability="8.9", binaries={"lmdeploy": True})


def _write(tmp_path, text, name="m.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return load_profile(name[:-5], tmp_path)


def _stub_venv(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.envs.VENV_ROOT", tmp_path / ".venvs")
    bin_dir = tmp_path / ".venvs" / "lmdeploy" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True, exist_ok=True)
    # ensure_env 通过 engine_python 判断环境是否存在；引擎二进制供 build_command 使用
    (bin_dir / ("python.exe" if os.name == "nt" else "python")).write_bytes(b"fake")
    (bin_dir / ("lmdeploy.exe" if os.name == "nt" else "lmdeploy")).write_bytes(b"fake")
    return bin_dir


def test_lmdeploy_command(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch)
    p = _write(
        tmp_path,
        "name: q\nengine: lmdeploy\nport: 8130\napi_key: sk-test\nlmdeploy:\n"
        "  model: /models/Qwen3.8-27B\n  tensor_parallel_size: 1\n"
        "  session_len: 32768\n  cache_max_entry_count: 0.8\n"
        "  quant_policy: 4\n"
        '  extra_args: "--enable-prefix-caching"\n',
    )
    a = get_adapter("lmdeploy")(p, CAPS8)
    a.check_requirements()
    cmd, env = a.build_command()
    assert str(cmd[0]).endswith("lmdeploy.exe") or str(cmd[0]).endswith("lmdeploy")
    assert cmd[1] == "serve"
    assert cmd[2] == "api_server"
    assert cmd[3] == "/models/Qwen3.8-27B"
    assert cmd[cmd.index("--server-name") + 1] == "0.0.0.0"
    assert cmd[cmd.index("--server-port") + 1] == "8130"
    assert cmd[cmd.index("--tp") + 1] == "1"
    assert cmd[cmd.index("--session-len") + 1] == "32768"
    assert cmd[cmd.index("--cache-max-entry-count") + 1] == "0.8"
    assert cmd[cmd.index("--quant-policy") + 1] == "4"
    assert "--enable-prefix-caching" in cmd
    assert cmd[cmd.index("--api-keys") + 1] == "sk-test"
    assert cmd[cmd.index("--model-name") + 1] == "q"


def test_lmdeploy_metrics(tmp_path):
    p = _write(tmp_path, "name: q\nengine: lmdeploy\nport: 8130\nlmdeploy:\n  model: m\n")
    a = get_adapter("lmdeploy")(p, CAPS8)
    mapping = a.metrics_mapping()
    assert mapping == {
        "prompt_total": ["lmdeploy:prompt_tokens_total"],
        "predicted_total": ["lmdeploy:generation_tokens_total"],
        "prompt_rate": ["lmdeploy:avg_prompt_throughput_toks_per_sec"],
        "predicted_rate": ["lmdeploy:avg_generation_throughput_toks_per_sec"],
    }


def test_lmdeploy_gpu_tp_mismatch(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch)
    p = _write(
        tmp_path,
        "name: q\nengine: lmdeploy\nport: 8130\napi_key: sk-test\nlmdeploy:\n"
        "  model: /models/Qwen3.8-27B\n  tensor_parallel_size: 2\n",
    )
    monkeypatch.setenv("MODELCTL_GPUS", "0")
    a = get_adapter("lmdeploy")(p, CAPS8)
    with pytest.raises(RequirementError):
        a.check_requirements()


def test_lmdeploy_health_url(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch)
    p = _write(
        tmp_path,
        "name: q\nengine: lmdeploy\nport: 8130\nlmdeploy:\n"
        "  model: /models/Qwen3.8-27B\n",
    )
    a = get_adapter("lmdeploy")(p, CAPS8)
    assert a.health_url() == "http://127.0.0.1:8130/health"
