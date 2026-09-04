#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_engines_llamacpp.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : llama.cpp 适配器测试
# ===============================================================================

from pathlib import Path

import pytest

import modelctl.core.compat_rules  # noqa: F401 —— 导入即注册
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


def test_build_command_ctx_size_is_per_slot(tmp_path):
    """ctx_size 语义 = 每并发请求完整可用（与其他引擎一致）；--ctx-size 总量 = ctx_size × parallel。"""
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(_profile(tmp_path, "  ctx_size: 32768\n"), caps)
    adapter.check_requirements()
    cmd, _ = adapter.build_command()
    assert cmd[cmd.index("--ctx-size") + 1] == str(32768 * 2)  # parallel=2


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


def test_check_requirements_env_var_degrade_warning(tmp_path, monkeypatch):
    """HF_HOME / MODELSCOPE_CACHE 缺失：check_requirements 产生 env_var_missing 降级警告。"""
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("MODELSCOPE_CACHE", raising=False)
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(_profile(tmp_path), caps)
    adapter.check_requirements()
    assert any("[env_var_missing]" in w for w in adapter.warnings)


def test_pre_start_discovers_draft_after_download(tmp_path, monkeypatch):
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
    monkeypatch.setattr(llamacpp, "require", lambda *a, **k: None)
    monkeypatch.setattr(llamacpp, "run", lambda *a, **k: None)

    adapter.pre_start()
    assert adapter._draft is not None
    assert adapter._draft.name == "dspark-x.gguf"
    assert adapter._dspark is True


def test_vision_mmproj_in_command(tmp_path):
    """vision: on + 模型同目录存在 mmproj：启动命令应包含 --mmproj。"""
    (tmp_path / "m.gguf").write_bytes(b"0" * 1024)
    (tmp_path / "mmproj-F16.gguf").write_bytes(b"0" * 512)
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(_profile(tmp_path, "  vision: on\n"), caps)
    adapter.check_requirements()
    cmd, _ = adapter.build_command()
    assert "--mmproj" in cmd
    assert cmd[cmd.index("--mmproj") + 1] == str((tmp_path / "mmproj-F16.gguf").resolve())
    assert not any("mmproj" in w for w in adapter.warnings)


def test_vision_off_by_default_no_flag(tmp_path):
    """默认 vision off：不传 --mmproj，也不产生 mmproj 警告（文本模型不受影响）。"""
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(_profile(tmp_path), caps)
    adapter.check_requirements()
    cmd, _ = adapter.build_command()
    assert "--mmproj" not in cmd
    assert not any("mmproj" in w.lower() for w in adapter.warnings)


def test_vision_warns_without_download(tmp_path):
    """vision: on 但无 mmproj、无 download 段：降级为警告且不传 --mmproj。"""
    (tmp_path / "m.gguf").write_bytes(b"0" * 1024)
    (tmp_path / "ds.yaml").write_text(
        f"name: ds\nengine: llamacpp\nport: 18888\nllamacpp:\n"
        f"  model: {tmp_path}/m.gguf\n  gpu_count: 8\n  vision: on\n",
        encoding="utf-8",
    )
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(load_profile("ds", tmp_path), caps)
    adapter.check_requirements()
    cmd, _ = adapter.build_command()
    assert "--mmproj" not in cmd
    assert any("mmproj" in w for w in adapter.warnings)


def test_pre_start_downloads_missing_mmproj(tmp_path, monkeypatch):
    """模型已持久化本地但缺 mmproj：pre_start 应补下并传入 --mmproj（用户实际场景）。"""
    from modelctl.engines import llamacpp

    (tmp_path / "repo").mkdir()
    model_shard = tmp_path / "repo" / "Qwen3.8-27B-Q4_K_M.gguf"
    model_shard.write_bytes(b"x")
    (tmp_path / "ds.yaml").write_text(
        f"name: ds\nengine: llamacpp\nport: 18888\nllamacpp:\n"
        f"  model: {model_shard}\n  gpu_count: 8\n  vision: on\n"
        f"  download:\n    modelscope_id: unsloth/Qwen3.8-27B-GGUF\n    quant: Q4_K_M\n"
        f"  source_dir: {tmp_path / 'llama.cpp'}\n",
        encoding="utf-8",
    )
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(load_profile("ds", tmp_path), caps)
    adapter.check_requirements()
    assert adapter._want_mmproj is True and adapter._mmproj is None

    def fake_ensure(mid, root):
        p = Path(root, "Qwen3.8-27B-GGUF", "mmproj-F16.gguf")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        return p

    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "model-gguf"))
    monkeypatch.setattr(llamacpp, "ensure_mmproj", fake_ensure)
    monkeypatch.setattr(llamacpp, "require", lambda *a, **k: None)
    monkeypatch.setattr(llamacpp, "run", lambda *a, **k: None)

    adapter.pre_start()
    assert adapter._mmproj is not None
    assert adapter._mmproj.name == "mmproj-F16.gguf"
    cmd, _ = adapter.build_command()
    assert "--mmproj" in cmd


def test_pre_start_no_download_section_keeps_warning(tmp_path, monkeypatch):
    """vision: on 但无 download 段：check_requirements 即告警，pre_start 不尝试下载。"""
    from modelctl.engines import llamacpp

    (tmp_path / "m.gguf").write_bytes(b"0" * 1024)
    (tmp_path / "ds.yaml").write_text(
        f"name: ds\nengine: llamacpp\nport: 18888\nllamacpp:\n"
        f"  model: {tmp_path}/m.gguf\n  gpu_count: 8\n  vision: on\n"
        f"  source_dir: {tmp_path / 'llama.cpp'}\n",
        encoding="utf-8",
    )
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(load_profile("ds", tmp_path), caps)
    adapter.check_requirements()
    assert adapter._want_mmproj is False
    assert any("mmproj" in w for w in adapter.warnings)

    def fake_ensure(*a, **k):
        raise AssertionError("无 download 段不应触发 mmproj 下载")

    monkeypatch.setattr(llamacpp, "ensure_mmproj", fake_ensure)
    monkeypatch.setattr(llamacpp, "require", lambda *a, **k: None)
    monkeypatch.setattr(llamacpp, "run", lambda *a, **k: None)
    adapter.pre_start()
    cmd, _ = adapter.build_command()
    assert "--mmproj" not in cmd


def test_pre_start_skips_git_cmake_when_binary_exists(tmp_path, monkeypatch):
    """产物已编译好：即使机器缺 git/cmake 也应能启动（不应无条件 require）。"""
    from modelctl.engines import llamacpp

    (tmp_path / "m.gguf").write_bytes(b"0" * 1024)
    source = tmp_path / "llama.cpp"
    binary = source / "build" / "bin" / "llama-server"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"x")
    (tmp_path / "ds.yaml").write_text(
        f"name: ds\nengine: llamacpp\nport: 18888\nllamacpp:\n"
        f"  model: {tmp_path}/m.gguf\n  gpu_count: 8\n  source_dir: {source}\n",
        encoding="utf-8",
    )
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(load_profile("ds", tmp_path), caps)
    adapter.check_requirements()

    def fake_which(name):
        return None

    monkeypatch.setattr(llamacpp.shutil, "which", fake_which)
    monkeypatch.setattr(
        llamacpp, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("产物已存在，不应执行编译"))
    )
    adapter.pre_start()  # 不应抛 RequirementError


def test_pre_start_requires_cmake_when_needing_build(tmp_path, monkeypatch):
    """源码存在但未编译且缺 cmake：应报缺少 cmake。"""
    from modelctl.engines import llamacpp

    (tmp_path / "m.gguf").write_bytes(b"0" * 1024)
    source = tmp_path / "llama.cpp"
    source.mkdir()  # 源码在、build 产物不在 → 需要 cmake
    (tmp_path / "ds.yaml").write_text(
        f"name: ds\nengine: llamacpp\nport: 18888\nllamacpp:\n"
        f"  model: {tmp_path}/m.gguf\n  gpu_count: 8\n  source_dir: {source}\n",
        encoding="utf-8",
    )
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(load_profile("ds", tmp_path), caps)
    adapter.check_requirements()

    monkeypatch.setattr(llamacpp.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    with pytest.raises(RequirementError, match="cmake"):
        adapter.pre_start()


def test_pre_start_requires_git_when_source_missing(tmp_path, monkeypatch):
    """源码目录不存在且缺 git：应报缺少 git（clone 前才校验）。"""
    from modelctl.engines import llamacpp

    (tmp_path / "m.gguf").write_bytes(b"0" * 1024)
    (tmp_path / "ds.yaml").write_text(
        f"name: ds\nengine: llamacpp\nport: 18888\nllamacpp:\n"
        f"  model: {tmp_path}/m.gguf\n  gpu_count: 8\n  source_dir: {tmp_path / 'llama.cpp'}\n",
        encoding="utf-8",
    )
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(load_profile("ds", tmp_path), caps)
    adapter.check_requirements()

    monkeypatch.setattr(llamacpp.shutil, "which", lambda _name: None)
    with pytest.raises(RequirementError, match="git"):
        adapter.pre_start()


def test_ensure_mmproj_local_hit_skips_network(tmp_path, monkeypatch):
    """本地已有 mmproj：跳过 modelscope 安装与下载，直接复用。"""
    from modelctl.engines import llamacpp

    dest = tmp_path / "Qwen3.8-27B-GGUF"
    dest.mkdir(parents=True)
    (dest / "mmproj-F16.gguf").write_bytes(b"x")

    monkeypatch.setattr(llamacpp, "ensure_modelscope", lambda: (_ for _ in ()).throw(AssertionError("不应安装")))
    monkeypatch.setattr(
        llamacpp, "snapshot_download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应下载"))
    )
    got = llamacpp.ensure_mmproj("unsloth/Qwen3.8-27B-GGUF", tmp_path)
    assert got is not None and got.name == "mmproj-F16.gguf"


def test_ensure_mmproj_downloads_when_missing(tmp_path, monkeypatch):
    """本地无 mmproj：仅以 *mmproj*.gguf 模式补下。"""
    from modelctl.engines import llamacpp

    captured = {}

    def fake_snapshot_download(model_id, local_dir, **kwargs):
        captured.update(kwargs)
        Path(local_dir, "mmproj-BF16.gguf").write_bytes(b"x")
        return local_dir

    monkeypatch.setattr(llamacpp, "ensure_modelscope", lambda: None)
    monkeypatch.setattr(llamacpp, "snapshot_download", fake_snapshot_download)

    got = llamacpp.ensure_mmproj("unsloth/Qwen3.8-27B-GGUF", tmp_path)
    assert got is not None and got.name == "mmproj-BF16.gguf"
    assert captured["allow_file_pattern"] == ["*mmproj*.gguf"]


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


def test_llamacpp_gpu_list_sets_cuda_and_tensor_split(tmp_path, monkeypatch):
    # check_requirements 会获取 GPU 锁，隔离到临时目录
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "locks"))
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(_profile(tmp_path, "  gpu_list: '0,1'\n"), caps)
    adapter.check_requirements()
    cmd, env = adapter.build_command()
    assert env["CUDA_VISIBLE_DEVICES"] == "0,1"
    assert cmd[cmd.index("--tensor-split") + 1] == "1,1"


def test_llamacpp_gpu_out_of_range_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    caps = probe(nvidia_smi_output=SMI)
    adapter = get_adapter("llamacpp")(_profile(tmp_path, "  gpu_list: '0,9'\n"), caps)
    with pytest.raises(RequirementError):
        adapter.check_requirements()


def test_llamacpp_gpu_conflict_blocks_second_model(tmp_path, monkeypatch):
    from pathlib import Path as _P

    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "locks"))
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    caps = probe(nvidia_smi_output=SMI)  # 8 GPUs → indices 0..7
    for name, gpu_list in (("a", "'0,1'"), ("b", "'1,2'")):
        (_P(tmp_path) / f"{name}.gguf").write_bytes(b"0" * 1024)
        yaml_text = (
            f"name: {name}\nengine: llamacpp\nport: 18890\nllamacpp:\n"
            f"  model: {_P(tmp_path)}/{name}.gguf\n  dspark: off\n  vision: off\n  gpu_list: {gpu_list}\n"
        )
        (tmp_path / f"{name}.yaml").write_text(yaml_text, encoding="utf-8")
    a = get_adapter("llamacpp")(load_profile("a", tmp_path), caps)
    a.check_requirements()  # acquires lock on GPU 0,1
    b = get_adapter("llamacpp")(load_profile("b", tmp_path), caps)
    with pytest.raises(RequirementError, match="占用"):
        b.check_requirements()


def test_llamacpp_vram_gate_uses_selected_gpus_only(tmp_path, monkeypatch):
    from pathlib import Path as _P

    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "locks"))
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    # Build caps where the SELECTED single GPU has little free VRAM but others have plenty.
    from modelctl.core.capabilities import Capabilities

    caps = Capabilities(gpu_count=4, gpu_indices=[0, 1, 2, 3], compute_capability="9.0",
                        vram_free_mb=[20, 40000, 40000, 40000])
    (_P(tmp_path) / "big.gguf").write_bytes(b"0" * (30 * 1024 * 1024))  # ~30MB → need ~33MB > GPU 0 free 20MB
    yaml_text = (
        f"name: c\nengine: llamacpp\nport: 18891\nllamacpp:\n"
        f"  model: {_P(tmp_path)}/big.gguf\n  dspark: off\n  vision: off\n  gpu_list: '0'\n"
    )
    (tmp_path / "c.yaml").write_text(yaml_text, encoding="utf-8")
    a = get_adapter("llamacpp")(load_profile("c", tmp_path), caps)
    with pytest.raises(RequirementError):  # selected GPU 0 has only 20MB free < needed (~33MB)
        a.check_requirements()
