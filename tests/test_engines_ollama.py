import pytest

import modelctl.core.compat_rules  # noqa: F401 —— 导入即注册
from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import load_profile
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError


def _profile(tmp_path):
    (tmp_path / "qwen3-ollama.yaml").write_text(
        "name: qwen3-ollama\nengine: ollama\nport: 11434\n"
        "ollama:\n  model: qwen3:32b\n  num_parallel: 2\n  context_length: 32768\n",
        encoding="utf-8",
    )
    return load_profile("qwen3-ollama", tmp_path)


def _write(tmp_path, text, name="o.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return load_profile(name[:-5], tmp_path)


def test_ollama_env_var_degrade_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("MODELSCOPE_CACHE", raising=False)
    p = _write(tmp_path, "name: o\nengine: ollama\nport: 11434\nollama:\n  model: qwen3:8b\n")
    adapter = get_adapter("ollama")(p, Capabilities(binaries={"ollama": True}))
    adapter.check_requirements()
    assert any("[env_var_missing]" in w for w in adapter.warnings)


def test_build_command(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", "/raid5/sh/model/ollama-models")
    caps = Capabilities(binaries={"ollama": True})
    a = get_adapter("ollama")(_profile(tmp_path), caps)
    cmd, env = a.build_command()
    assert cmd == ["ollama", "serve"]
    assert env["OLLAMA_HOST"] == "0.0.0.0:11434"
    assert env["OLLAMA_MODELS"] == "/raid5/sh/model/ollama-models"
    assert env["OLLAMA_NUM_PARALLEL"] == "2"
    assert env["OLLAMA_CONTEXT_LENGTH"] == "32768"


def test_missing_binary(tmp_path):
    caps = Capabilities(binaries={"ollama": False})
    a = get_adapter("ollama")(_profile(tmp_path), caps)
    with pytest.raises(RequirementError, match="ollama"):
        a.check_requirements()


def test_metrics_mapping_none(tmp_path):
    caps = Capabilities(binaries={"ollama": True})
    a = get_adapter("ollama")(_profile(tmp_path), caps)
    assert a.metrics_mapping() is None


def test_health_url_root(tmp_path):
    caps = Capabilities(binaries={"ollama": True})
    a = get_adapter("ollama")(_profile(tmp_path), caps)
    assert a.health_url() == "http://127.0.0.1:11434/"


def test_pre_start_pulls_when_tag_not_installed(tmp_path, monkeypatch):
    """本地只有同名不同 tag（qwen3:8b）时，仍应 pull 目标 tag（qwen3:32b）。"""
    import subprocess as sp

    from modelctl.engines import ollama as ollama_mod

    caps = Capabilities(binaries={"ollama": True})
    a = get_adapter("ollama")(_profile(tmp_path), caps)
    calls = []

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["ollama", "list"]:
            return sp.CompletedProcess(cmd, 0, stdout="qwen3:8b\n", stderr="")
        calls.append(cmd)
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ollama_mod.subprocess, "run", fake_run)
    a.pre_start()
    assert calls == [["ollama", "pull", "qwen3:32b"]]


def test_pre_start_skips_pull_when_installed(tmp_path, monkeypatch):
    """目标 tag 已安装时跳过 pull。"""
    import subprocess as sp

    from modelctl.engines import ollama as ollama_mod

    caps = Capabilities(binaries={"ollama": True})
    a = get_adapter("ollama")(_profile(tmp_path), caps)
    calls = []

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["ollama", "list"]:
            return sp.CompletedProcess(cmd, 0, stdout="qwen3:32b\n", stderr="")
        calls.append(cmd)
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ollama_mod.subprocess, "run", fake_run)
    a.pre_start()
    assert calls == []


def test_pre_start_pull_failure_raises_friendly_error(tmp_path, monkeypatch):
    """pull 失败时抛出携带 stderr 的 RequirementError，而非裸 CalledProcessError。"""
    import subprocess as sp

    from modelctl.engines import ollama as ollama_mod

    caps = Capabilities(binaries={"ollama": True})
    a = get_adapter("ollama")(_profile(tmp_path), caps)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["ollama", "list"]:
            return sp.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise sp.CalledProcessError(1, cmd, output="", stderr="pull model manifest: file does not exist")

    monkeypatch.setattr(ollama_mod.subprocess, "run", fake_run)
    with pytest.raises(RequirementError, match="file does not exist"):
        a.pre_start()
