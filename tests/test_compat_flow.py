"""能力检测两段式集成测试。"""

import modelctl.core.compat_rules  # noqa: F401 —— 导入即注册
from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import load_profile
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError

CAPS8 = Capabilities(gpu_count=8, compute_capability="8.9", binaries={"vllm": True})


def _write(tmp_path, text, name="m.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return load_profile(name[:-5], tmp_path)


def test_vllm_preflight_blocks_deepseek_v4_before_download(tmp_path):
    p = _write(
        tmp_path,
        "name: ds4\nengine: vllm\nport: 8000\nvllm:\n  model: deepseek-ai/DeepSeek-V4-Flash\n",
    )
    adapter = get_adapter("vllm")(p, CAPS8)
    try:
        adapter.check_requirements()
        raise AssertionError("应抛 RequirementError")
    except RequirementError as e:
        assert "deepseek_v4_mhc" in str(e) and "ds4" in str(e)


def test_vllm_preflight_blocks_torch_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", "/x")
    # 虚拟 site-packages：vllm 要求 torch==2.13.0，已装 2.9.1
    import modelctl.core.compat as compat

    sp = tmp_path / "sp"
    (sp / "vllm-0.27.1.dist-info").mkdir(parents=True)
    (sp / "vllm-0.27.1.dist-info" / "METADATA").write_text(
        "Name: vllm\nVersion: 0.27.1\nRequires-Dist: torch==2.13.0\n", encoding="utf-8"
    )
    (sp / "torch-2.9.1.dist-info").mkdir(parents=True)
    (sp / "torch-2.9.1.dist-info" / "METADATA").write_text("Name: torch\nVersion: 2.9.1\n", encoding="utf-8")
    monkeypatch.setattr(compat, "_current_site_packages", lambda: sp)

    p = _write(tmp_path, "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: Qwen/Qwen3-32B\n")
    adapter = get_adapter("vllm")(p, CAPS8)
    try:
        adapter.check_requirements()
        raise AssertionError("应抛 RequirementError")
    except RequirementError as e:
        assert "vllm_torch_abi" in str(e)


def test_vllm_post_download_precise_check(tmp_path):
    # 精检：目录名不含 DeepSeek 特征（预检 is_deepseek_v4=False 放行），
    # 但本地 config.json 的 architectures 暴露 DeepSeek-V4 → pre_start 精检拦截
    model_dir = tmp_path / "m1"
    model_dir.mkdir()
    (model_dir / "config.json").write_text(
        '{"architectures": ["DeepseekV4ForCausalLM"]}', encoding="utf-8"
    )
    p = _write(
        tmp_path,
        f"name: ds4\nengine: vllm\nport: 8000\nvllm:\n  model: {model_dir}\n",
    )
    adapter = get_adapter("vllm")(p, CAPS8)
    adapter.check_requirements()  # 预检：name_hint 为路径 m1，不含 deepseek-v4 特征 → 放行
    try:
        adapter.pre_start()
        raise AssertionError("精检应抛 RequirementError")
    except RequirementError as e:
        assert "deepseek_v4_mhc" in str(e)
