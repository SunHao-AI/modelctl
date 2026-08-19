"""tests/test_engines_unsloth.py — Unsloth 适配器测试。"""

import json as _json

import pytest

from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import load_profile
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError
from modelctl.engines.unsloth import UnslothAdapter

CAPS8 = Capabilities(gpu_count=8, compute_capability="8.9", binaries={"unsloth": True})


def _write(tmp_path, text, name="u.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return load_profile(name[:-5], tmp_path)


def test_unsloth_registered():
    assert get_adapter("unsloth") is UnslothAdapter


def test_unsloth_requirements_rejects_without_binary(tmp_path):
    p = _write(tmp_path, "name: u\nengine: unsloth\nport: 30000\napi_key: k\nunsloth:\n  model: m\n")
    a = get_adapter("unsloth")(p, Capabilities(gpu_count=8, binaries={"unsloth": False}))
    with pytest.raises(RequirementError, match="unsloth"):
        a.check_requirements()


def test_unsloth_requirements_no_api_key_needed(tmp_path):
    # API key 由 unsloth 运行时自动生成并打印到启动日志，profile 无需配置
    p = _write(tmp_path, "name: u\nengine: unsloth\nport: 30000\nunsloth:\n  model: m\n")
    a = get_adapter("unsloth")(p, CAPS8)
    a.check_requirements()


def test_unsloth_requirements_allow_download_only(tmp_path):
    p = _write(
        tmp_path,
        "name: u\nengine: unsloth\nport: 30000\napi_key: k\n" "unsloth:\n  model: ''\n  download:\n    modelscope_id: unsloth/DeepSeek-V4-Flash-0731-GGUF\n" "    quant: UD-Q8_K_XL\n",
    )
    a = get_adapter("unsloth")(p, CAPS8)
    a.check_requirements()  # model 为空但有 download 段时不应报错


def test_unsloth_tensor_parallel_requires_2_gpus(tmp_path):
    p = _write(
        tmp_path,
        "name: u\nengine: unsloth\nport: 30000\napi_key: k\n" "unsloth:\n  model: m\n  tensor_parallel: true\n",
    )
    a = get_adapter("unsloth")(p, Capabilities(gpu_count=1, binaries={"unsloth": True}))
    with pytest.raises(RequirementError, match="2 块 GPU"):
        a.check_requirements()


def test_unsloth_build_command(tmp_path):
    p = _write(
        tmp_path,
        "name: u\nengine: unsloth\nport: 30000\n" "unsloth:\n  model: unsloth/Test-GGUF\n  gguf_variant: UD-Q4_K_XL\n  context_length: 32768\n",
    )
    a = get_adapter("unsloth")(p, CAPS8)
    cmd, _env = a.build_command()
    # studio 是命令组，run 子命令承载模型/网络 flag；不传 --api-key（运行时自动生成）
    assert cmd[:4] == ["unsloth", "studio", "run", "--api-only"]
    assert cmd[cmd.index("-H") + 1] == "0.0.0.0"
    assert cmd[cmd.index("-p") + 1] == "30000"
    assert cmd[cmd.index("--model") + 1] == "unsloth/Test-GGUF:UD-Q4_K_XL"
    assert cmd[cmd.index("--context-length") + 1] == "32768"
    assert "--api-key" not in cmd


def test_unsloth_build_command_local_path_ignores_variant(tmp_path):
    p = _write(
        tmp_path,
        f"name: u\nengine: unsloth\nport: 30000\nunsloth:\n  model: {tmp_path}/model.gguf\n" f"  gguf_variant: UD-Q4_K_XL\n",
    )
    (tmp_path / "model.gguf").write_text("x", encoding="utf-8")
    a = get_adapter("unsloth")(p, CAPS8)
    cmd, _env = a.build_command()
    assert cmd[cmd.index("--model") + 1] == str(tmp_path / "model.gguf")


def test_unsloth_upstream_api_key_prefers_runtime_log(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    p = _write(
        tmp_path,
        "name: u\nengine: unsloth\nport: 30000\napi_key: static-key\nunsloth:\n  model: m\n",
    )
    a = get_adapter("unsloth")(p, CAPS8)
    assert a.upstream_api_key() == "static-key"  # 无启动日志时兜底 profile.api_key
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)  # launch_log() 可能已创建该目录
    (log_dir / "launch-u.log").write_text(
        "Loading model: ...\nAPI Key:      sk-unsloth-abc123\ncurl ... Bearer sk-unsloth-abc123 ...\n",
        encoding="utf-8",
    )
    assert a.upstream_api_key() == "sk-unsloth-abc123"


def test_unsloth_wait_ready_uses_runtime_key(tmp_path, monkeypatch):
    import modelctl.engines.unsloth as mod

    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    p = _write(tmp_path, "name: u\nengine: unsloth\nport: 30000\nunsloth:\n  model: m\n")
    a = get_adapter("unsloth")(p, CAPS8)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "launch-u.log").write_text("API Key: sk-unsloth-xyz\n", encoding="utf-8")
    seen = {}

    def fake_wait(url, timeout, api_key=None):
        seen.update(url=url, key=api_key)
        return True

    monkeypatch.setattr(mod, "wait_health", fake_wait)
    assert a.wait_ready(5.0) is True
    assert seen["url"] == "http://127.0.0.1:30000/v1/models"
    assert seen["key"] == "sk-unsloth-xyz"


def test_unsloth_wait_ready_times_out_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    p = _write(tmp_path, "name: u\nengine: unsloth\nport: 30000\nunsloth:\n  model: m\n")
    a = get_adapter("unsloth")(p, CAPS8)
    assert a.wait_ready(1.0) is False  # 启动日志无 API Key 行 → 等到超时返回 False


def test_unsloth_health_url_and_metrics(tmp_path):
    p = _write(tmp_path, "name: u\nengine: unsloth\nport: 30000\nunsloth:\n  model: m\n")
    a = get_adapter("unsloth")(p, CAPS8)
    assert a.health_url() == "http://127.0.0.1:30000/v1/models"
    assert a.metrics_mapping() is None
    assert a.stop_patterns() == ["unsloth"]


def test_unsloth_pre_start_downloads_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "model-gguf"))
    p = _write(
        tmp_path,
        "name: u\nengine: unsloth\nport: 30000\n" "unsloth:\n  model: ''\n  download:\n" "    modelscope_id: unsloth/DeepSeek-V4-Flash-0731-GGUF\n    quant: UD-Q8_K_XL\n",
    )
    a = get_adapter("unsloth")(p, CAPS8)

    downloaded = tmp_path / "model-gguf" / "DeepSeek-V4-Flash-0731-GGUF" / "UD-Q8_K_XL" / "model.gguf"
    monkeypatch.setattr(
        "modelctl.engines.unsloth.download_gguf",
        lambda mid, root, quant, want_dspark: (downloaded, None),
    )

    a.pre_start()
    assert p.engine_config["model"] == str(downloaded.resolve())
    content = p.path.read_text(encoding="utf-8")
    assert f"model: {downloaded.resolve()}" in content
    assert (tmp_path / "u.yaml.bak").is_file()


def test_unsloth_pre_start_skips_when_model_exists(tmp_path, monkeypatch):
    p = _write(
        tmp_path,
        f"name: u\nengine: unsloth\nport: 30000\nunsloth:\n  model: {tmp_path}/model.gguf\n",
    )
    (tmp_path / "model.gguf").write_text("x", encoding="utf-8")
    a = get_adapter("unsloth")(p, CAPS8)
    calls = []

    def _fail(*args, **kwargs):  # 不应被调用
        calls.append("called")
        return tmp_path

    monkeypatch.setattr("modelctl.engines.unsloth.download_gguf", _fail)
    monkeypatch.setattr("modelctl.engines.unsloth.persist_model_path", _fail)

    a.pre_start()
    assert calls == []


def test_unsloth_pre_start_download_failure_hints_hf(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "model-gguf"))
    p = _write(
        tmp_path,
        "name: u\nengine: unsloth\nport: 30000\n" "unsloth:\n  model: ''\n  download:\n" "    modelscope_id: unsloth/DeepSeek-V4-Flash-0731-GGUF\n    quant: UD-Q8_K_XL\n",
    )
    a = get_adapter("unsloth")(p, CAPS8)

    def _fail(*args, **kwargs):
        raise RequirementError("ModelScope 下载失败")

    monkeypatch.setattr("modelctl.engines.unsloth.download_gguf", _fail)
    with pytest.raises(RequirementError, match="HF_ENDPOINT"):
        a.pre_start()


def test_unsloth_post_start_sends_chat_request(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))  # 隔离：无启动日志时兜底 profile.api_key
    p = _write(tmp_path, "name: u\nengine: unsloth\nport: 30000\napi_key: k\nunsloth:\n  model: m\n")
    a = get_adapter("unsloth")(p, CAPS8)
    seen = {}

    class _Resp:
        def read(self):
            return b"{}"

    def _fake(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = _json.loads(req.data)
        return _Resp()

    monkeypatch.setattr("modelctl.engines.unsloth.urllib.request.urlopen", _fake)

    a.post_start()
    assert seen["url"] == "http://127.0.0.1:30000/v1/chat/completions"
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["messages"][0]["content"] == "ping"


def test_unsloth_post_start_ignores_errors(tmp_path, monkeypatch):
    p = _write(tmp_path, "name: u\nengine: unsloth\nport: 30000\napi_key: k\nunsloth:\n  model: m\n")
    a = get_adapter("unsloth")(p, CAPS8)

    def _boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("modelctl.engines.unsloth.urllib.request.urlopen", _boom)
    a.post_start()  # 预热失败不应抛异常
