"""modelctl/core/capabilities.py 单元测试。"""

from modelctl.core.capabilities import cc_at_least, free_vram_total_mb, probe

SMI_5880 = "\n".join(["RTX 5880 Ada Generation, 49140, 48000, 580.65.05, 8.9"] * 8)


def test_probe_detects_llamacpp_build(tmp_path, monkeypatch):
    """LLAMACPP_SOURCE_DIR/build/bin/llama-server 存在时，llamacpp 视为可用。"""
    build_bin = tmp_path / "build" / "bin"
    build_bin.mkdir(parents=True)
    server = build_bin / "llama-server"
    server.write_text("#!/bin/sh\nexit 0\n")
    server.chmod(0o755)
    monkeypatch.setenv("LLAMACPP_SOURCE_DIR", str(tmp_path))
    monkeypatch.setattr("modelctl.core.capabilities.shutil.which", lambda name: None)
    caps = probe(nvidia_smi_output=SMI_5880)
    assert caps.binaries["llamacpp"] is True
    assert caps.binary_paths["llamacpp"] == str(server)


def test_probe_llamacpp_unavailable_without_build(tmp_path, monkeypatch):
    """源码目录存在但无编译产物时，llamacpp 不可用（pre_start 会编译）。"""
    monkeypatch.setenv("LLAMACPP_SOURCE_DIR", str(tmp_path))
    monkeypatch.setattr("modelctl.core.capabilities.shutil.which", lambda name: None)
    caps = probe(nvidia_smi_output=SMI_5880)
    assert caps.binaries["llamacpp"] is False
    assert caps.binary_paths["llamacpp"] is None


def test_probe_5880():
    caps = probe(nvidia_smi_output=SMI_5880)
    assert caps.gpu_count == 8
    assert caps.gpu_name == "RTX 5880 Ada Generation"
    assert caps.vram_total_mb == 49140
    assert caps.vram_free_mb == [48000] * 8
    assert caps.compute_capability == "8.9"
    assert caps.cuda_driver == "580.65.05"


def test_probe_failure_returns_empty():
    caps = probe(nvidia_smi_output="")
    assert caps.gpu_count == 0
    assert caps.compute_capability == ""


def test_cc_at_least():
    assert cc_at_least("8.9", 8, 9)
    assert cc_at_least("8.9", 8, 0)
    assert not cc_at_least("8.9", 9, 0)
    assert not cc_at_least("", 8, 0)


def test_free_vram_total():
    caps = probe(nvidia_smi_output=SMI_5880)
    assert free_vram_total_mb(caps) == 48000 * 8
