"""tests/test_engines_persist.py — persist_model_path 写回测试。"""


def test_persist_model_path_updates_yaml(tmp_path):
    yaml_path = tmp_path / "demo.yaml"
    yaml_path.write_text(
        "name: demo\nengine: llamacpp\nport: 8000\nllamacpp:\n  model: ''\n  parallel: 2\n",
        encoding="utf-8",
    )
    from modelctl.engines._persist import persist_model_path

    persist_model_path(yaml_path, "llamacpp", "/downloaded/model.gguf")
    content = yaml_path.read_text(encoding="utf-8")
    assert "model: /downloaded/model.gguf" in content
    assert (yaml_path.with_name(yaml_path.name + ".bak")).is_file()


def test_persist_preserves_other_fields(tmp_path):
    yaml_path = tmp_path / "demo.yaml"
    yaml_path.write_text(
        "name: demo\nengine: vllm\nport: 8000\nvllm:\n  model: ''\n  tensor_parallel_size: 2\n",
        encoding="utf-8",
    )
    from modelctl.engines._persist import persist_model_path

    persist_model_path(yaml_path, "vllm", "/downloaded/dir")
    content = yaml_path.read_text(encoding="utf-8")
    assert "model: /downloaded/dir" in content
    assert "tensor_parallel_size: 2" in content
    assert "name: demo" in content


def test_persist_preserves_comments(tmp_path):
    """文本级替换保留 YAML 注释与其余格式，仅更新 model 值。"""
    yaml_path = tmp_path / "demo.yaml"
    yaml_path.write_text(
        "# 学习要点：保留注释\n"
        "name: demo\n"
        "engine: llamacpp\n"
        "port: 8000\n"
        "llamacpp:\n"
        "  # 模型路径注释\n"
        "  model: ''  # 行尾注释\n"
        "  parallel: 2\n",
        encoding="utf-8",
    )
    import yaml

    from modelctl.engines._persist import persist_model_path

    persist_model_path(yaml_path, "llamacpp", "/downloaded/model.gguf")
    content = yaml_path.read_text(encoding="utf-8")
    # 注释全部保留
    assert "# 学习要点：保留注释" in content
    assert "# 模型路径注释" in content
    assert "# 行尾注释" in content
    # 仅 model 值更新，其余行原样
    assert "model: /downloaded/model.gguf  # 行尾注释" in content
    assert "parallel: 2" in content
    assert "engine: llamacpp" in content
    # 写回后仍为合法 YAML 且 engine 段 model 已更新
    raw = yaml.safe_load(content)
    assert raw["llamacpp"]["model"] == "/downloaded/model.gguf"
