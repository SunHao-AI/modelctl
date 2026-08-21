"""能力检测框架单元测试（GpuSpec / ModelSpec / cc_major）。"""

from __future__ import annotations

from pathlib import Path

from modelctl.core.capabilities import Capabilities
from modelctl.core.compat import EnvSpec, GpuSpec, ModelSpec, _resolvable_cuda_libs, cc_major


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


def _fake_site_packages(tmp_path) -> Path:
    """构造虚拟 site-packages：vllm METADATA（含 torch 约束）、nvidia cudnn 包与假 .so。"""
    sp = tmp_path / "site-packages"
    vllm_dist = sp / "vllm-0.27.1.dist-info"
    vllm_dist.mkdir(parents=True)
    (vllm_dist / "METADATA").write_text(
        "Name: vllm\nVersion: 0.27.1\nRequires-Dist: torch==2.13.0\nRequires-Dist: xgrammar>=0.2.3\n",
        encoding="utf-8",
    )
    torch_dist = sp / "torch-2.9.1.dist-info"
    torch_dist.mkdir(parents=True)
    (torch_dist / "METADATA").write_text("Name: torch\nVersion: 2.9.1\n", encoding="utf-8")
    cudnn = sp / "nvidia" / "cudnn" / "lib"
    cudnn.mkdir(parents=True)
    (cudnn / "libcudnn.so.9").write_bytes(b"")
    return sp


def test_env_spec_metadata(tmp_path):
    env = EnvSpec.from_env(site_packages=_fake_site_packages(tmp_path))
    assert env.packages["torch"] == "2.9.1"
    assert env.packages["vllm"] == "0.27.1"
    assert env.wheel_requires["vllm"]["torch"] == "==2.13.0"
    assert env.wheel_requires["vllm"]["xgrammar"] == ">=0.2.3"


def test_env_spec_nvidia_so(tmp_path):
    env = EnvSpec.from_env(site_packages=_fake_site_packages(tmp_path))
    assert "nvidia/cudnn/lib/libcudnn.so.9" in env.nvidia_so
    assert "libcudnn.so.9" in env.cuda_libs_resolvable  # venv 内 nvidia 库并入可解析集


def test_env_spec_empty_site_packages(tmp_path):
    env = EnvSpec.from_env(site_packages=tmp_path / "nonexistent")
    assert env.packages == {}
    assert env.wheel_requires == {}
    assert env.nvidia_so == set()


def test_env_spec_env_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HOME", "/data/hf")
    monkeypatch.setenv("MODEL_ROOT", "")
    env = EnvSpec.from_env(site_packages=tmp_path)
    assert env.env_vars["HF_HOME"] == "/data/hf"
    assert env.env_vars["MODEL_ROOT"] == ""


def test_resolvable_cuda_libs_glibc_ldconfig(monkeypatch):
    """glibc 系统 ldconfig -p 行含架构注释（如 (libc6,x86-64)），应解析出库名而非注释。"""

    class _FakeCompleted:
        returncode = 0
        stdout = "libcudart.so.13 (libc6,x86-64) => /lib/x86_64-linux-gnu/libcudart.so.13\n"
        stderr = ""

    monkeypatch.setattr(
        "modelctl.core.compat.subprocess.run", lambda *args, **kwargs: _FakeCompleted()
    )
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    names, known = _resolvable_cuda_libs()
    assert known is True
    assert names == {"libcudart.so.13"}


def test_env_spec_libs_resolvable_unknown_without_ldconfig(monkeypatch, tmp_path):
    """ldconfig 不可用（命令缺失/非 Linux）时 libs_resolvable_known 应为 False。"""

    def _raise_oserror(*args, **kwargs):
        raise OSError("ldconfig not found")

    monkeypatch.setattr("modelctl.core.compat.subprocess.run", _raise_oserror)
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    env = EnvSpec.from_env(site_packages=tmp_path)
    assert env.libs_resolvable_known is False
