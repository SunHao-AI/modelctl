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
