from pathlib import Path

import pytest

from modelctl.core.capabilities import probe
from modelctl.core.profile import ProfileError, load_profile
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError
from modelctl.engines.llamacpp import _find_first

SMI = "\n".join(["RTX 5880 Ada Generation, 49140, 48000, 580.65.05, 8.9"] * 8)


def _profile(tmp_path, extra=""):
    (tmp_path / "m.gguf").write_bytes(b"0" * 1024)
    (tmp_path / "dspark-x.gguf").write_bytes(b"0" * 512)
    yaml_text = f"""
name: ds
engine: llamacpp
port: 18888
llamacpp:
  model: {tmp_path}/m.gguf
  parallel: 2
  gpu_count: 8
{extra}"""
    (tmp_path / "ds.yaml").write_text(yaml_text, encoding="utf-8")
    return load_profile("ds", tmp_path)


def test_build_command(tmp_path):
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(_profile(tmp_path), caps)
    adapter.check_requirements()
    cmd, env = adapter.build_command()
    assert "--model" in cmd and "18888" in cmd
    assert cmd[cmd.index("--ctx-size") + 1] == str(2 * 1048576)
    assert "--model-draft" in cmd
    assert "--cache-type-k" in cmd
    assert "--metrics" in cmd


def test_build_command_on_off_from_yaml_bool(tmp_path):
    """PyYAML 将 reasoning: on / fit: off 解析为布尔，应透传 on/off 而非 True/False。"""
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(
        _profile(tmp_path, "  reasoning: on\n  fit: off\n"), caps
    )
    adapter.check_requirements()
    cmd, _ = adapter.build_command()
    assert cmd[cmd.index("--reasoning") + 1] == "on"
    assert cmd[cmd.index("--fit") + 1] == "off"


def test_dspark_disabled_when_no_draft(tmp_path):
    (tmp_path / "m.gguf").write_bytes(b"0" * 1024)
    (tmp_path / "ds.yaml").write_text(
        f"name: ds\nengine: llamacpp\nport: 18888\nllamacpp:\n  model: {tmp_path}/m.gguf\n  gpu_count: 8\n",
        encoding="utf-8",
    )
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(load_profile("ds", tmp_path), caps)
    adapter.check_requirements()
    cmd, _ = adapter.build_command()
    assert "--model-draft" not in cmd
    assert any("park" in w.lower() for w in adapter.warnings)


def test_gpu_count_exceeds_hw(tmp_path):
    caps = probe(nvidia_smi_output="\n".join(["RTX 5880 Ada Generation, 49140, 48000, 580.65.05, 8.9"] * 2))
    adapter = get_adapter("llamacpp")(_profile(tmp_path), caps)
    with pytest.raises(RequirementError, match="GPU"):
        adapter.check_requirements()


def test_sampling_params_passthrough(tmp_path):
    caps = probe(nvidia_smi_output=SMI)
    extra = (
        "  repeat_penalty: 1.1\n  repeat_last_n: 256\n  temperature: 0.6\n"
        "  top_p: 0.95\n  top_k: 40\n  stops:\n    - '<｜DSML｜tool_calls'\n"
    )
    adapter = get_adapter("llamacpp")(_profile(tmp_path, extra=extra), caps)
    adapter.check_requirements()
    cmd, _ = adapter.build_command()
    assert cmd[cmd.index("--repeat-penalty") + 1] == "1.1"
    assert cmd[cmd.index("--repeat-last-n") + 1] == "256"
    assert cmd[cmd.index("--temp") + 1] == "0.6"
    assert cmd[cmd.index("--top-p") + 1] == "0.95"
    assert cmd[cmd.index("--top-k") + 1] == "40"
    assert cmd[cmd.index("--stops") + 1] == "<｜DSML｜tool_calls"


def test_sampling_params_absent_by_default(tmp_path):
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(_profile(tmp_path), caps)
    adapter.check_requirements()
    cmd, _ = adapter.build_command()
    assert "--temp" not in cmd
    assert "--top-p" not in cmd
    assert "--top-k" not in cmd
    assert "--repeat-penalty" not in cmd
    assert "--stops" not in cmd


def test_temperature_zero_is_respected(tmp_path):
    # temperature=0 是合法值（贪心），不能因 falsy 判断被吞掉
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(_profile(tmp_path, extra="  temperature: 0\n"), caps)
    adapter.check_requirements()
    cmd, _ = adapter.build_command()
    assert cmd[cmd.index("--temp") + 1] == "0"


def test_metrics_mapping_keys(tmp_path):
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(_profile(tmp_path), caps)
    m = adapter.metrics_mapping()
    assert "llamacpp:prompt_tokens_total" in m["prompt_total"]
    assert "llamacpp:tokens_predicted_total" in m["predicted_total"]


def test_unknown_engine():
    with pytest.raises(ProfileError):
        get_adapter("tensorrt")


def test_download_gguf_skips_when_local_exists(tmp_path, monkeypatch):
    """本地已有匹配分片与草稿：跳过 modelscope 安装与下载，直接复用。"""
    from modelctl.engines import llamacpp

    dest = tmp_path / "Qwen3.8-27B-GGUF"
    dest.mkdir(parents=True)
    (dest / "qwen3-Q4_K_M-00001-of-00002.gguf").write_bytes(b"x")
    (dest / "dspark-DeepSeek-V4-Flash-0731-Q8_0.gguf").write_bytes(b"x")

    captured = {}

    def fake_snapshot_download(model_id, local_dir, **kwargs):
        captured.update(kwargs)
        return local_dir

    def fake_ensure():
        raise AssertionError("本地已存在模型分片，不应安装 modelscope")

    monkeypatch.setattr(llamacpp, "ensure_modelscope", fake_ensure)
    monkeypatch.setattr(llamacpp, "snapshot_download", fake_snapshot_download)

    model, draft = llamacpp.download_gguf("unsloth/Qwen3.8-27B-GGUF", tmp_path, "Q4_K_M", True)
    assert model.name == "qwen3-Q4_K_M-00001-of-00002.gguf"
    assert draft is not None
    assert draft.name == "dspark-DeepSeek-V4-Flash-0731-Q8_0.gguf"
    assert captured == {}  # 未调用 snapshot_download


def test_download_gguf_skips_when_local_exists_no_draft(tmp_path, monkeypatch):
    """本地已有分片但 want_dspark=False：跳过下载，草稿为 None。"""
    from modelctl.engines import llamacpp

    dest = tmp_path / "Qwen3.8-27B-GGUF"
    dest.mkdir(parents=True)
    (dest / "qwen3-Q4_K_M-00001-of-00002.gguf").write_bytes(b"x")

    captured = {}

    def fake_snapshot_download(model_id, local_dir, **kwargs):
        captured.update(kwargs)
        return local_dir

    def fake_ensure():
        raise AssertionError("本地已存在模型分片，不应安装 modelscope")

    monkeypatch.setattr(llamacpp, "ensure_modelscope", fake_ensure)
    monkeypatch.setattr(llamacpp, "snapshot_download", fake_snapshot_download)

    model, draft = llamacpp.download_gguf("unsloth/Qwen3.8-27B-GGUF", tmp_path, "Q4_K_M", False)
    assert model.name == "qwen3-Q4_K_M-00001-of-00002.gguf"
    assert draft is None
    assert captured == {}


def test_download_gguf_downloads_when_missing(tmp_path, monkeypatch):
    """本地无匹配分片：安装 modelscope 并调用 snapshot_download。"""
    from modelctl.engines import llamacpp

    dest = tmp_path / "Qwen3.8-27B-GGUF"
    dest.mkdir(parents=True)

    captured = {}

    def fake_snapshot_download(model_id, local_dir, **kwargs):
        captured.update(kwargs)
        # 模拟下载产物：首个分片 + 草稿
        Path(local_dir, "qwen3-Q4_K_M-00001-of-00002.gguf").write_bytes(b"x")
        Path(local_dir, "dspark-DeepSeek-V4-Flash-0731-Q8_0.gguf").write_bytes(b"x")
        return local_dir

    monkeypatch.setattr(llamacpp, "ensure_modelscope", lambda: None)
    monkeypatch.setattr(llamacpp, "snapshot_download", fake_snapshot_download)

    model, draft = llamacpp.download_gguf("unsloth/Qwen3.8-27B-GGUF", tmp_path, "Q4_K_M", True)
    assert model.name == "qwen3-Q4_K_M-00001-of-00002.gguf"
    assert draft is not None
    assert draft.name == "dspark-DeepSeek-V4-Flash-0731-Q8_0.gguf"
    assert "allow_file_pattern" in captured
    assert "*Q4_K_M*" in "|".join(captured["allow_file_pattern"])
    assert "dspark" in "|".join(captured["allow_file_pattern"])


def test_check_requirements_allows_empty_model_with_download(tmp_path):
    (tmp_path / "ds.yaml").write_text(
        "name: ds\nengine: llamacpp\nport: 18888\nllamacpp:\n"
        "  model: ''\n  download:\n    modelscope_id: x/y\n    quant: Q4_K_M\n  gpu_count: 8\n",
        encoding="utf-8",
    )
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(load_profile("ds", tmp_path), caps)
    adapter.check_requirements()  # 不应抛错
    assert adapter._model is None


def test_check_requirements_dspark_intent_with_empty_model(tmp_path):
    # model 留空 + download 段 + dspark on（默认）：不应静默关闭，dspark 意图保留
    (tmp_path / "ds.yaml").write_text(
        "name: ds\nengine: llamacpp\nport: 18888\nllamacpp:\n"
        "  model: ''\n  download:\n    modelscope_id: x/y\n    quant: Q4_K_M\n"
        "  gpu_count: 8\n  dspark: on\n",
        encoding="utf-8",
    )
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(load_profile("ds", tmp_path), caps)
    adapter.check_requirements()
    assert adapter._model is None
    assert adapter._dspark is True  # 意图保留，下载后重新发现草稿
    assert not any("未找到" in w for w in adapter.warnings)


def test_pre_start_discovers_draft_after_download(tmp_path, monkeypatch):
    from modelctl.engines import _persist as persist_mod
    from modelctl.engines import llamacpp

    (tmp_path / "ds.yaml").write_text(
        f"name: ds\nengine: llamacpp\nport: 18888\nllamacpp:\n"
        f"  model: ''\n  download:\n    modelscope_id: x/y\n    quant: Q4_K_M\n"
        f"  gpu_count: 8\n  dspark: on\n  source_dir: {tmp_path / 'llama.cpp'}\n",
        encoding="utf-8",
    )
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(load_profile("ds", tmp_path), caps)
    adapter.check_requirements()
    assert adapter._dspark is True

    # 构造下载后的状态：模型分片 + 同目录 dspark 草稿
    dest = tmp_path / "model-gguf" / "y"
    dest.mkdir(parents=True)
    model_shard = dest / "m-Q4_K_M-00001-of-00002.gguf"
    model_shard.write_bytes(b"x")
    (dest / "dspark-x.gguf").write_bytes(b"x")

    # download_gguf 只返回模型分片（auto_draft=None），验证走重新发现分支
    monkeypatch.setattr(llamacpp, "download_gguf", lambda mid, root, quant, wd: (model_shard, None))
    monkeypatch.setattr(persist_mod, "persist_model_path", lambda *a, **k: None)
    monkeypatch.setattr(llamacpp, "require", lambda *a, **k: None)
    monkeypatch.setattr(llamacpp, "run", lambda *a, **k: None)

    adapter.pre_start()
    assert adapter._draft is not None
    assert adapter._draft.name == "dspark-x.gguf"
    assert adapter._dspark is True


def test_find_first_skips_directory_named_dspark(tmp_path):
    """回归：仓库内 dspark 为目录时不得被当作草稿文件（rglob 会命中目录）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "dspark").mkdir()  # 旧逻辑会误匹配此目录
    real_draft = repo / "dspark" / "dspark-DeepSeek-V4-Flash-0731-Q8_0.gguf"
    real_draft.write_bytes(b"gguf")

    got = _find_first(repo, ["*dspark*.gguf"])
    assert got is not None
    assert got == real_draft
    assert got.is_file()

    # 即使 pattern 更宽泛（如 *dspark*），也必须跳过目录
    got_wide = _find_first(repo, ["*dspark*"])
    assert got_wide is not None and got_wide.is_file()
