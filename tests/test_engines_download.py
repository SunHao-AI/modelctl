"""tests/test_engines_download.py — ModelScope 下载工具测试。"""

import importlib.util
import sys
import types
from pathlib import Path

import modelctl.engines._download as dl


def test_ensure_modelscope_uses_pip_when_available(monkeypatch):
    calls = []

    def fake_find_spec(name):
        return None

    class FakeResult:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["-m", "pip"] and cmd[3] == "--version":
            return FakeResult(0)
        return FakeResult(0)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(dl.subprocess, "run", fake_run)
    dl.ensure_modelscope()
    assert calls == [
        [sys.executable, "-m", "pip", "--version"],
        [sys.executable, "-m", "pip", "install", "-U", "modelscope"],
    ]


def test_ensure_modelscope_falls_back_to_uv_without_pip(monkeypatch):
    """uv 虚拟环境无 pip 时回退 uv pip install（--python 指定当前解释器）。"""
    calls = []

    def fake_find_spec(name):
        return None

    class FakeResult:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["-m", "pip"] and cmd[3] == "--version":
            return FakeResult(1)  # 无 pip 模块
        return FakeResult(0)  # uv pip install 成功

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(dl.subprocess, "run", fake_run)
    dl.ensure_modelscope()
    assert calls == [
        [sys.executable, "-m", "pip", "--version"],
        ["uv", "pip", "install", "--python", sys.executable, "-U", "modelscope"],
    ]


def test_ensure_modelscope_skips_when_installed(monkeypatch):
    calls = []

    def fake_find_spec(name):
        return object()

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return None

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(dl.subprocess, "run", fake_run)
    dl.ensure_modelscope()
    assert calls == []


def test_download_repo_uses_modelscope(tmp_path, monkeypatch):
    calls = []

    def fake_snapshot_download(model_id, local_dir, **kwargs):
        calls.append((model_id, local_dir))
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        return local_dir

    # 注入假 modelscope 模块，避免依赖真实安装；并禁用自动安装。
    fake_modelscope = types.ModuleType("modelscope")
    fake_modelscope.snapshot_download = fake_snapshot_download
    monkeypatch.setitem(sys.modules, "modelscope", fake_modelscope)
    monkeypatch.setattr(dl, "ensure_modelscope", lambda: None)

    result = dl.download_repo("unsloth/Qwen3.8-27B-GGUF", tmp_path)
    assert calls == [("unsloth/Qwen3.8-27B-GGUF", str(tmp_path / "Qwen3.8-27B-GGUF"))]
    assert result == tmp_path / "Qwen3.8-27B-GGUF"
