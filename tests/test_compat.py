"""能力检测框架单元测试（GpuSpec / ModelSpec / cc_major）。"""

from __future__ import annotations

from modelctl.core.capabilities import Capabilities
from modelctl.core.compat import GpuSpec, ModelSpec, cc_major


def test_cc_major_parsing():
    assert cc_major("8.9") == 8
    assert cc_major("12.0") == 12
    assert cc_major("") is None
    assert cc_major("abc") is None


def test_gpu_spec_from_caps():
    caps = Capabilities(
        gpu_count=8,
        compute_capability="8.9",
        gpu_name="RTX 5880",
        vram_total_mb=49152,
        vram_free_mb=[100, 200],
    )
    gpu = GpuSpec.from_caps(caps)
    assert gpu.cc_major == 8
    assert gpu.arch_family == "Ampere/Ada"
    assert gpu.gpu_count == 8
    assert gpu.vram_free_mb == [100, 200]


def test_gpu_spec_arch_family_unknown():
    assert GpuSpec(cc="").arch_family == "unknown"


def test_model_spec_from_local(tmp_path):
    (tmp_path / "config.json").write_text(
        '{"architectures": ["DeepseekV4ForCausalLM"], "model_type": "deepseek_v4", '
        '"quantization_config": {"quant_method": "deepseek_v4_fp8"}}',
        encoding="utf-8",
    )
    m = ModelSpec.from_local("vllm", tmp_path)
    assert m.source == "local"
    assert m.is_deepseek_v4
    assert "fp8" in m.quantization


def test_model_spec_from_local_missing_config(tmp_path):
    m = ModelSpec.from_local("vllm", tmp_path)
    assert m.source == "local"
    assert not m.is_deepseek_v4
    assert m.quantization == ""


def test_model_spec_from_local_bad_json(tmp_path):
    (tmp_path / "config.json").write_text("{ not json", encoding="utf-8")
    m = ModelSpec.from_local("vllm", tmp_path)
    assert not m.is_deepseek_v4


def test_model_spec_from_id_detects_deepseek_v4():
    m = ModelSpec.from_id("vllm", "deepseek-ai/DeepSeek-V4-Flash")
    assert m.source == "id"
    assert m.is_deepseek_v4


def test_model_spec_from_id_download_id():
    m = ModelSpec.from_id("vllm", "", "deepseek-ai/DeepSeek-V4-Flash")
    assert m.is_deepseek_v4


def test_model_spec_from_id_quantization():
    m = ModelSpec.from_id("vllm", "Qwen/Qwen3-32B", quantization="fp8")
    assert "fp8" in m.quantization
