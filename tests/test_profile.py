"""tests/test_profile.py — Profile 加载、插值与校验测试。"""

import pytest

from modelctl.core.profile import Profile, ProfileError, list_profiles, load_profile

YAML = """
name: demo
engine: llamacpp
port: 18888
api_key: ${TEST_KEY}
llamacpp:
  model: /models/x.gguf
  parallel: 2
usage:
  price_in: 1.0
"""


def _write(tmp_path, text, name="demo.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_load_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "secret")
    d = _write(tmp_path, YAML)
    p = load_profile("demo", d)
    assert isinstance(p, Profile)
    assert p.name == "demo" and p.engine == "llamacpp" and p.port == 18888
    assert p.api_key == "secret"
    assert p.engine_config == {"model": "/models/x.gguf", "parallel": 2}
    assert p.usage == {"price_in": 1.0}


def test_missing_required_field(tmp_path):
    d = _write(tmp_path, "name: demo\nengine: llamacpp\n")
    with pytest.raises(ProfileError, match="port"):
        load_profile("demo", d)


def test_unknown_engine(tmp_path):
    d = _write(tmp_path, "name: demo\nengine: tensorrt\nport: 1\n")
    with pytest.raises(ProfileError, match="tensorrt"):
        load_profile("demo", d)


def test_interpolate_missing_var(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE_VAR", raising=False)
    d = _write(tmp_path, "name: demo\nengine: vllm\nport: 8000\napi_key: ${NOPE_VAR}\n")
    with pytest.raises(ProfileError, match="NOPE_VAR"):
        load_profile("demo", d)


def test_nested_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOT", "/raid5/sh/model")
    d = _write(tmp_path, "name: demo\nengine: ollama\nport: 11434\nollama:\n  model: ${ROOT}/x\n")
    p = load_profile("demo", d)
    assert p.engine_config["model"] == "/raid5/sh/model/x"


def test_alias_str(tmp_path):
    d = _write(tmp_path, "name: demo\nengine: vllm\nport: 8000\nalias: short\n")
    p = load_profile("demo", d)
    assert p.aliases == ["short"]


def test_aliases_list(tmp_path):
    d = _write(tmp_path, "name: demo\nengine: vllm\nport: 8000\naliases:\n  - a\n  - b\n")
    p = load_profile("demo", d)
    assert p.aliases == ["a", "b"]


def test_aliases_default_empty(tmp_path):
    d = _write(tmp_path, "name: demo\nengine: vllm\nport: 8000\n")
    p = load_profile("demo", d)
    assert p.aliases == []


def test_alias_equals_name_rejected(tmp_path):
    d = _write(tmp_path, "name: demo\nengine: vllm\nport: 8000\nalias: demo\n")
    with pytest.raises(ProfileError):
        load_profile("demo", d)


def test_list_profiles_sorted(tmp_path):
    _write(tmp_path, "name: b\nengine: vllm\nport: 1\n", "b.yaml")
    _write(tmp_path, "name: a\nengine: vllm\nport: 2\n", "a.yaml")
    assert [p.name for p in list_profiles(tmp_path)] == ["a", "b"]


def test_load_by_yaml_name_when_filename_differs(tmp_path, monkeypatch):
    """文件名 <base>-<engine>.yaml 内 name 为 <base>-<engine> 时，可按 name 加载。"""
    monkeypatch.setenv("TEST_KEY", "secret")
    (tmp_path / "llamacpp").mkdir()
    (tmp_path / "llamacpp" / "deepseek-v4-flash.yaml").write_text(
        "name: deepseek-v4-flash-llamacpp\nengine: llamacpp\nport: 18888\n"
        "api_key: ${TEST_KEY}\nllamacpp:\n  model: /x.gguf\n",
        encoding="utf-8",
    )
    p = load_profile("deepseek-v4-flash-llamacpp", tmp_path)
    assert p.name == "deepseek-v4-flash-llamacpp"
    assert p.engine == "llamacpp"
    assert p.path == tmp_path / "llamacpp" / "deepseek-v4-flash.yaml"


def test_missing_file(tmp_path):
    with pytest.raises(ProfileError, match="不存在"):
        load_profile("ghost", tmp_path)


def test_missing_file_hints_interpolation_failure(tmp_path):
    with pytest.raises(ProfileError, match=r"\$\{VAR\} 插值失败"):
        load_profile("ghost", tmp_path)


def test_list_profiles_skips_interpolation_failure(tmp_path, monkeypatch):
    """${VAR} 插值失败的 profile 被跳过，不阻塞其余 profile 的加载。"""
    monkeypatch.delenv("NOPE_VAR2", raising=False)
    (tmp_path / "llamacpp").mkdir()
    (tmp_path / "llamacpp" / "bad.yaml").write_text(
        "name: bad\nengine: llamacpp\nport: 8000\napi_key: ${NOPE_VAR2}\nllamacpp:\n  model: /x.gguf\n",
        encoding="utf-8",
    )
    (tmp_path / "ok.yaml").write_text(
        "name: ok\nengine: llamacpp\nport: 8001\nllamacpp:\n  model: /x.gguf\n",
        encoding="utf-8",
    )
    assert [p.name for p in list_profiles(tmp_path)] == ["ok"]


def test_load_profile_from_engine_subdir(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "secret")
    (tmp_path / "llamacpp").mkdir()
    (tmp_path / "llamacpp" / "qwen3.yaml").write_text(
        "name: qwen3\nengine: llamacpp\nport: 8000\nllamacpp:\n  model: /x.gguf\n",
        encoding="utf-8",
    )
    p = load_profile("qwen3", tmp_path)
    assert p.name == "qwen3" and p.engine == "llamacpp"
    assert p.path == tmp_path / "llamacpp" / "qwen3.yaml"


def test_list_profiles_prefers_root_over_subdir(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("TEST_KEY", "secret")
    (tmp_path / "qwen3.yaml").write_text(
        "name: qwen3\nengine: ollama\nport: 11434\nollama:\n  model: qwen3:root\n",
        encoding="utf-8",
    )
    (tmp_path / "llamacpp").mkdir()
    (tmp_path / "llamacpp" / "qwen3.yaml").write_text(
        "name: qwen3\nengine: llamacpp\nport: 8000\nllamacpp:\n  model: /x.gguf\n",
        encoding="utf-8",
    )
    profiles = list_profiles(tmp_path)
    assert [p.name for p in profiles] == ["qwen3"]
    assert profiles[0].engine == "ollama"


def test_tool_call_rounds_parsed(tmp_path):
    d = _write(tmp_path, "name: demo\nengine: vllm\nport: 8000\ntool_call_rounds: 5\n")
    p = load_profile("demo", d)
    assert p.tool_call_rounds == 5


def test_tool_call_rounds_invalid_rejected(tmp_path):
    d = _write(tmp_path, "name: demo\nengine: vllm\nport: 8000\ntool_call_rounds: not_a_number\n")
    with pytest.raises(ProfileError, match="tool_call_rounds"):
        load_profile("demo", d)


def test_max_output_tokens_parsed(tmp_path):
    d = _write(tmp_path, "name: demo\nengine: vllm\nport: 8000\nmax_output_tokens: 4096\n")
    p = load_profile("demo", d)
    assert p.max_output_tokens == 4096


def test_max_output_tokens_invalid_rejected(tmp_path):
    d = _write(tmp_path, "name: demo\nengine: vllm\nport: 8000\nmax_output_tokens: not_a_number\n")
    with pytest.raises(ProfileError, match="max_output_tokens"):
        load_profile("demo", d)


def test_group_field(tmp_path):
    d = _write(tmp_path, "name: demo\ngroup: qwen3.8\nengine: ollama\nport: 11434\n")
    p = load_profile("demo", d)
    assert p.group == "qwen3.8"


def test_group_missing_derived_from_filename(tmp_path):
    """group 缺省时自动从文件名（stem）推导。"""
    d = _write(tmp_path, "name: demo\nengine: ollama\nport: 11434\n")
    p = load_profile("demo", d)
    assert p.group == "demo"


def test_group_empty_derived_from_filename(tmp_path):
    """group 为空字符串时回退文件名推导。"""
    d = _write(tmp_path, "name: demo\ngroup: \"\"\nengine: ollama\nport: 11434\n")
    p = load_profile("demo", d)
    assert p.group == "demo"


def test_group_derived_strips_variant_suffix(tmp_path):
    """文件名带 variant 后缀且声明 variant 时，group 自动去掉 -{variant}。"""
    d = _write(tmp_path, "variant: high\nengine: vllm\nport: 8103\n", name="qwen3.8-high.yaml")
    p = load_profile("qwen3.8-vllm-high", d)
    assert p.group == "qwen3.8"
    assert p.variant == "high"
    assert p.name == "qwen3.8-vllm-high"


def test_name_auto_derived_without_variant(tmp_path):
    """name 缺省 = group-engine（无 variant）。"""
    d = _write(tmp_path, "group: qwen3.8\nengine: vllm\nport: 8101\nvllm:\n  model: q\n", name="qwen3.8.yaml")
    p = load_profile("qwen3.8-vllm", d)
    assert p.name == "qwen3.8-vllm"
    assert p.group == "qwen3.8"


def test_name_auto_derived_with_variant(tmp_path):
    """name 缺省 = group-engine-variant（有 variant）。"""
    d = _write(tmp_path, "group: qwen3.8\nvariant: light\nengine: vllm\nport: 8105\nvllm:\n  model: q\n", name="qwen3.8-light.yaml")
    p = load_profile("qwen3.8-vllm-light", d)
    assert p.name == "qwen3.8-vllm-light"
    assert p.variant == "light"


def test_engine_inferred_from_parent_dir(tmp_path):
    """engine 可从父目录名推导（models/{engine}/{name}.yaml）。"""
    sub = tmp_path / "ollama"
    sub.mkdir()
    (sub / "kimi.yaml").write_text("group: kimi-k2.5\nport: 11434\nollama:\n  model: kimi\n", encoding="utf-8")
    p = load_profile("kimi-k2.5-ollama", tmp_path)
    assert p.engine == "ollama"
    assert p.name == "kimi-k2.5-ollama"


def test_engine_missing_and_no_parent_dir_raises(tmp_path):
    """无 engine 且不在引擎子目录 → 报错。"""
    d = _write(tmp_path, "name: demo\nport: 11434\n")
    with pytest.raises(ProfileError, match="引擎"):
        load_profile("demo", d)
