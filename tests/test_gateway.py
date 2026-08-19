"""modelctl.core.gateway 单元测试（注册表构建 + model 解析）。"""

from __future__ import annotations

from modelctl.core.gateway import GatewayModel, build_registry, resolve_model


def test_build_registry(tmp_path):
    (tmp_path / "qwen.yaml").write_text(
        "name: qwen3.8\nengine: ollama\nport: 11434\n\nollama:\n  model: qwen3.8:27b\n", encoding="utf-8"
    )
    (tmp_path / "ds.yaml").write_text(
        "name: deepseek-v4-flash\nengine: llamacpp\nport: 18888\n", encoding="utf-8"
    )
    reg = build_registry(models_dir=tmp_path)
    assert set(reg) == {"qwen3.8", "deepseek-v4-flash"}
    assert reg["qwen3.8"].backend_url == "http://127.0.0.1:11434"
    assert reg["qwen3.8"].upstream_model == "qwen3.8:27b"
    assert reg["deepseek-v4-flash"].upstream_model == "deepseek-v4-flash"


def test_resolve_model():
    reg = {
        "a": GatewayModel("a", "ollama", "http://127.0.0.1:1", "a:1", None, "http://127.0.0.1:1/"),
        "b": GatewayModel("b", "llamacpp", "http://127.0.0.1:2", "b", None, "http://127.0.0.1:2/"),
    }
    assert resolve_model(reg, "a", "b") is reg["a"]        # 显式命中
    assert resolve_model(reg, "unknown", "a") is reg["a"]  # 未知 → 回退默认
    assert resolve_model(reg, None, "b") is reg["b"]       # 省略 → 默认
    assert resolve_model(reg, "unknown", None) is None
