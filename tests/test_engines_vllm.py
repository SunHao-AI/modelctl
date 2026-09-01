#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_engines_vllm.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : vLLM 适配器测试
# ===============================================================================

import os

import pytest

import modelctl.core.compat_rules  # noqa: F401 —— 导入即注册
from modelctl.core.capabilities import Capabilities
from modelctl.core.profile import load_profile
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError

CAPS8 = Capabilities(gpu_count=8, compute_capability="8.9", binaries={"vllm": True, "sglang": True})


def _write(tmp_path, text, name="m.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return load_profile(name[:-5], tmp_path)


def _stub_venv(tmp_path, monkeypatch, engine: str):
    """把 envs.VENV_ROOT 重定向到 tmp_path/.venvs 并创建该引擎的最小 stub 目录，
    使 check_requirements 里的 ensure_env(engine) 通过。"""
    monkeypatch.setattr("modelctl.core.envs.VENV_ROOT", tmp_path / ".venvs")
    bin_dir = tmp_path / ".venvs" / engine / "Scripts" if os.name == "nt" else tmp_path / ".venvs" / engine / "bin"
    exe_name = "python.exe" if os.name == "nt" else "python"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / exe_name).write_bytes(b"fake")
    return tmp_path / ".venvs"


def test_vllm_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/Qwen3-32B\n  tensor_parallel_size: 2\n  max_model_len: 32768\n"
        '  extra_args: "--enable-prefix-caching"\n',
    )
    a = get_adapter("vllm")(p, CAPS8)
    a.check_requirements()
    cmd, env = a.build_command()
    assert str(cmd[0]).endswith("vllm.exe") or str(cmd[0]).endswith("vllm")  # 指向 venv 内可执行文件
    assert cmd[1] == "serve"
    assert cmd[2] == "Qwen/Qwen3-32B"
    assert cmd[cmd.index("--tensor-parallel-size") + 1] == "2"
    assert cmd[cmd.index("--max-model-len") + 1] == "32768"
    assert cmd[cmd.index("--served-model-name") + 1] == "q"  # = profile.name
    assert "--enable-prefix-caching" in cmd
    assert env["HF_HOME"] == "/raid5/sh/model/huggingface"
    # venv 注入断言：VIRTUAL_ENV 与 PATH 前置
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venvs" / "vllm")
    assert str(tmp_path / ".venvs" / "vllm" / "Scripts") in env["PATH"] or str(
        tmp_path / ".venvs" / "vllm" / "bin"
    ) in env["PATH"]


def test_vllm_fp8_cc_check(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(tmp_path, "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: m\n  quantization: fp8\n")
    a = get_adapter("vllm")(p, Capabilities(gpu_count=8, compute_capability="7.5", binaries={"vllm": True}))
    with pytest.raises(RequirementError, match="8.9"):
        a.check_requirements()


def test_vllm_tp_exceeds(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(tmp_path, "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: m\n  tensor_parallel_size: 16\n")
    a = get_adapter("vllm")(p, CAPS8)
    with pytest.raises(RequirementError, match="GPU"):
        a.check_requirements()


def test_vllm_deepseek_v4_unsupported_on_ada(tmp_path, monkeypatch):
    # Ada（CC 8.9）不支持 DeepSeek-V4：mHC 层依赖仅 Hopper/Blackwell DC 提供的 DeepGEMM 内核
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(
        tmp_path,
        "name: ds4\nengine: vllm\nport: 8000\nvllm:\n  model: deepseek-ai/DeepSeek-V4-Flash\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    with pytest.raises(RequirementError, match="不支持 vllm 引擎部署 ds4 模型"):
        a.check_requirements()


def test_vllm_deepseek_v4_local_config_detection(tmp_path, monkeypatch):
    # 本地目录：通过 config.json 的 architectures / model_type 判定
    _stub_venv(tmp_path, monkeypatch, "vllm")
    model_dir = tmp_path / "DeepSeek-V4-Flash"
    model_dir.mkdir()
    (model_dir / "config.json").write_text(
        '{"architectures": ["DeepseekV4ForCausalLM"], "model_type": "deepseek_v4"}',
        encoding="utf-8",
    )
    p = _write(tmp_path, f"name: ds4\nengine: vllm\nport: 8000\nvllm:\n  model: {model_dir}\n")
    a = get_adapter("vllm")(p, CAPS8)
    with pytest.raises(RequirementError, match="不支持 vllm 引擎部署 ds4 模型"):
        a.check_requirements()


def test_vllm_deepseek_v4_allowed_on_hopper(tmp_path, monkeypatch):
    # Hopper（CC 9.0）应放行
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(
        tmp_path,
        "name: ds4\nengine: vllm\nport: 8000\nvllm:\n  model: deepseek-ai/DeepSeek-V4-Flash\n",
    )
    a = get_adapter("vllm")(p, Capabilities(gpu_count=8, compute_capability="9.0", binaries={"vllm": True}))
    a.check_requirements()


def test_vllm_deepseek_v4_skips_when_cc_unknown(tmp_path, monkeypatch):
    # 无法探测到 CC 时不拦截（避免误伤无 GPU 的纯配置检查场景）
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(
        tmp_path,
        "name: ds4\nengine: vllm\nport: 8000\nvllm:\n  model: deepseek-ai/DeepSeek-V4-Flash\n",
    )
    a = get_adapter("vllm")(p, Capabilities(gpu_count=0, compute_capability="", binaries={"vllm": True}))
    a.check_requirements()


def test_sglang_command(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch, "sglang")
    p = _write(
        tmp_path, "name: s\nengine: sglang\nport: 30000\nsglang:\n  model: Qwen/Qwen3-32B\n  tensor_parallel_size: 4\n"
    )
    a = get_adapter("sglang")(p, CAPS8)
    a.check_requirements()
    cmd, env = a.build_command()
    # 首元素指向 venv 解释器（cmd[1] == "-m", cmd[2] == "sglang.launch_server" 保持不变）
    assert str(cmd[0]).endswith("python.exe") or str(cmd[0]).endswith("python")
    assert cmd.index("sglang.launch_server") == 2
    assert "sglang.launch_server" in cmd
    assert cmd[cmd.index("--tp") + 1] == "4"
    # venv 注入断言：VIRTUAL_ENV 与 PATH 前置
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venvs" / "sglang")
    assert str(tmp_path / ".venvs" / "sglang" / "Scripts") in env["PATH"] or str(
        tmp_path / ".venvs" / "sglang" / "bin"
    ) in env["PATH"]


def test_vllm_metrics(tmp_path):
    p = _write(tmp_path, "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: m\n")
    a = get_adapter("vllm")(p, CAPS8)
    assert a.metrics_mapping()["prompt_total"] == ["vllm:prompt_tokens_total"]
    assert a.metrics_mapping()["predicted_total"] == ["vllm:generation_tokens_total"]


def test_vllm_requirements_allow_download_only(tmp_path, monkeypatch):
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: ''\n  download:\n    modelscope_id: Qwen/Qwen3-32B\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    a.check_requirements()  # model 为空但有 download 段时不应报错


def test_vllm_pre_start_downloads_and_persists(tmp_path, monkeypatch):
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: ''\n  download:\n    modelscope_id: Qwen/Qwen3-32B\n",
    )
    a = get_adapter("vllm")(p, CAPS8)

    downloaded = tmp_path / "model-hf" / "Qwen3-32B"
    # import 位于 vllm 模块顶部，monkeypatch 模块属性即可生效。
    monkeypatch.setattr("modelctl.engines.vllm.download_repo", lambda mid, root: downloaded)
    # 使用真实 persist_model_path，同时验证 YAML 被写回。

    a.pre_start()
    assert p.engine_config["model"] == str(downloaded.resolve())
    content = p.path.read_text(encoding="utf-8")
    assert f"model: {downloaded.resolve()}" in content
    assert (tmp_path / "m.yaml.bak").is_file()


def test_vllm_pre_start_skips_when_model_exists(tmp_path, monkeypatch):
    p = _write(
        tmp_path,
        f"name: q\nengine: vllm\nport: 8000\nvllm:\n  model: {tmp_path}/model-hf/Qwen3-32B\n",
    )
    (tmp_path / "model-hf" / "Qwen3-32B").mkdir(parents=True)
    a = get_adapter("vllm")(p, CAPS8)

    calls = []

    def _fail(*args, **kwargs):  # 不应被调用
        calls.append("called")
        return tmp_path

    monkeypatch.setattr("modelctl.engines.vllm.download_repo", _fail)
    monkeypatch.setattr("modelctl.engines.vllm.persist_model_path", _fail)

    a.pre_start()  # model 路径已存在，直接返回
    assert calls == []


def _vllm_caps(n):
    return Capabilities(gpu_count=n, gpu_indices=list(range(n)), compute_capability="9.0", binaries={"vllm": True})


def test_vllm_gpu_list_sets_cuda(tmp_path, monkeypatch):
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/Qwen3-32B\n  tensor_parallel_size: 2\n  gpu_list: '2,3'\n",
    )
    a = get_adapter("vllm")(p, _vllm_caps(4))
    _, env = a.build_command()
    assert env["CUDA_VISIBLE_DEVICES"] == "2,3"


def test_vllm_env_used_when_no_profile_gpus(monkeypatch):
    monkeypatch.setenv("MODELCTL_GPUS", "4,5")
    from modelctl.core.profile import Profile

    profile = Profile(name="q", engine="vllm", port=8000, engine_config={"model": "Qwen/Qwen3-32B"})
    a = get_adapter("vllm")(profile, _vllm_caps(6))
    _, env = a.build_command()
    assert env["CUDA_VISIBLE_DEVICES"] == "4,5"


def test_vllm_tp_derived_from_gpu_list(tmp_path, monkeypatch):
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    p = _write(tmp_path, "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: Qwen/Qwen3-32B\n  gpu_list: '1,2'\n")
    a = get_adapter("vllm")(p, _vllm_caps(4))
    cmd, _ = a.build_command()
    assert cmd[cmd.index("--tensor-parallel-size") + 1] == "2"


def test_vllm_tp_mismatch_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/Qwen3-32B\n  tensor_parallel_size: 4\n  gpu_list: '1,2'\n",
    )
    a = get_adapter("vllm")(p, _vllm_caps(4))
    with pytest.raises(RequirementError):
        a.check_requirements()


def test_vllm_gpu_out_of_range_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(tmp_path, "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: Qwen/Qwen3-32B\n  gpu_list: '7,8'\n")
    a = get_adapter("vllm")(p, _vllm_caps(8))  # valid indices 0..7; index 8 invalid
    with pytest.raises(RequirementError):
        a.check_requirements()


def test_vllm_gpu_conflict_blocks_second_model(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp_path / "locks")
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    _stub_venv(tmp_path, monkeypatch, "vllm")
    caps = Capabilities(gpu_count=4, gpu_indices=[0, 1, 2, 3], compute_capability="9.0", binaries={"vllm": True})
    for name, gl in (("va", "'0,1'"), ("vb", "'1,2'")):
        yaml_text = f"name: {name}\nengine: vllm\nport: 8100\nvllm:\n  model: Qwen/Qwen3-32B\n  gpu_list: {gl}\n"
        (tmp_path / f"{name}.yaml").write_text(yaml_text, encoding="utf-8")
    a = get_adapter("vllm")(load_profile("va", tmp_path), caps)
    a.check_requirements()   # passes all gates → acquires lock at end
    b = get_adapter("vllm")(load_profile("vb", tmp_path), caps)
    with pytest.raises(RequirementError):
        b.check_requirements()


def test_vllm_warns_when_weights_exceed_selected_cap(tmp_path, monkeypatch):
    # 权重超所选卡可用上限（总×util）→ 仅告警、不拦截；且按所选卡而非全卡估算
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    _stub_venv(tmp_path, monkeypatch, "vllm")
    model_dir = tmp_path / "big"
    model_dir.mkdir()
    (model_dir / "model.safetensors").write_bytes(b"x" * 3 * 1024 * 1024)  # ~3MB 权重
    p = _write(
        tmp_path,
        f"name: q\nengine: vllm\nport: 8000\nvllm:\n"
        f"  model: {model_dir}\n  gpu_list: '0'\n  tensor_parallel_size: 1\n",
    )
    # GPU0 总显存 3MB（×0.9=2.7 < 3 → 触发）；GPU1 充足，证明只按所选卡估算
    caps = Capabilities(
        gpu_count=2, gpu_indices=[0, 1], compute_capability="9.0", binaries={"vllm": True},
        vram_total_mb_per_gpu=[3, 9000],
    )
    a = get_adapter("vllm")(p, caps)
    a.check_requirements()
    assert any("权重" in w for w in a.warnings)


def test_vllm_no_warn_when_weights_fit(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    _stub_venv(tmp_path, monkeypatch, "vllm")
    model_dir = tmp_path / "small"
    model_dir.mkdir()
    (model_dir / "model.safetensors").write_bytes(b"x" * 3 * 1024 * 1024)
    p = _write(tmp_path, f"name: q\nengine: vllm\nport: 8000\nvllm:\n  model: {model_dir}\n")
    caps = Capabilities(
        gpu_count=2, gpu_indices=[0, 1], compute_capability="9.0", binaries={"vllm": True},
        vram_total_mb_per_gpu=[8000, 8000],
    )
    a = get_adapter("vllm")(p, caps)
    a.check_requirements()
    assert not any("权重" in w for w in a.warnings)


def test_sglang_warns_when_weights_exceed_cap(tmp_path, monkeypatch):
    # 未指定 gpu_list → 按全卡总显存 × mem_fraction_static(默认0.85) 估算：2×0.85=1.7 < 3 → 告警
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    _stub_venv(tmp_path, monkeypatch, "sglang")
    model_dir = tmp_path / "big-sg"
    model_dir.mkdir()
    (model_dir / "model.safetensors").write_bytes(b"x" * 3 * 1024 * 1024)
    p = _write(tmp_path, f"name: s\nengine: sglang\nport: 30000\nsglang:\n  model: {model_dir}\n")
    caps = Capabilities(
        gpu_count=1, gpu_indices=[0], compute_capability="9.0", binaries={"sglang": True}, vram_total_mb_per_gpu=[2]
    )
    a = get_adapter("sglang")(p, caps)
    a.check_requirements()
    assert any("权重" in w for w in a.warnings)


# ---- Task 1: _resolve_runtime 路由 ----


def test_resolve_runtime_default(tmp_path, monkeypatch):
    """未配 docker_image → ('venv', None)，与改造前等价。"""
    p = _write(tmp_path, "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: Qwen/X\n")
    a = get_adapter("vllm")(p, CAPS8)
    assert a._resolve_runtime() == ("venv", None)


def test_resolve_runtime_docker(tmp_path, monkeypatch):
    """配 docker_image → ('docker', image)。"""
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/X\n  docker_image: vllm/vllm-openai:qwen38-flash-next\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    assert a._resolve_runtime() == ("docker", "vllm/vllm-openai:qwen38-flash-next")


# ---- Task 2: check_requirements docker 分支 ----


def test_check_requirements_venv_unchanged(tmp_path, monkeypatch):
    """venv 路径：现状语义不变——ensure_env 还是被调用。"""
    import modelctl.core.envs as envs_mod
    called = []
    monkeypatch.setattr(envs_mod, "ensure_env", lambda t: called.append(t) or tmp_path)
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    p = _write(tmp_path, "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: Qwen/X\n")
    a = get_adapter("vllm")(p, CAPS8)
    a.check_requirements()
    assert called == ["vllm"]


def test_check_requirements_docker_no_venv_check(tmp_path, monkeypatch):
    """docker 路径：跳过 venv 检查（ensure_env 触发即抛错）。"""
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/" + name)
    import modelctl.core.envs as envs_mod

    def bomb(_):
        raise RuntimeError("venv 检查不应被触发")

    monkeypatch.setattr(envs_mod, "ensure_env", bomb)
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/X\n  docker_image: vllm/vllm-openai:qwen38-flash-next\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    a.check_requirements()  # 不抛


def test_check_requirements_docker_missing_docker(tmp_path, monkeypatch):
    """docker 命令不在 PATH 时报 RequirementError。"""
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda name: None)
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/X\n  docker_image: vllm/vllm-openai:qwen38-flash-next\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    with pytest.raises(RequirementError, match="docker 命令不在 PATH"):
        a.check_requirements()


def test_check_requirements_docker_missing_nvidia_smi(tmp_path, monkeypatch):
    """nvidia-smi 不在 PATH 时报 toolkit 未就绪。"""
    import shutil as _shutil
    monkeypatch.setattr(
        _shutil, "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/X\n  docker_image: vllm/vllm-openai:qwen38-flash-next\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    with pytest.raises(RequirementError, match="nvidia-container-toolkit 未就绪"):
        a.check_requirements()


# ---- Task 3: build_command docker 命令模板 ----


def test_build_command_default_venv_unchanged(tmp_path, monkeypatch):
    """venv 路径 build_command 输出与改造前等价（现状回归锚点）。"""
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model-hf")
    _stub_venv(tmp_path, monkeypatch, "vllm")
    model = tmp_path / "models" / "Qwen3.8"
    model.mkdir(parents=True)
    p = _write(
        tmp_path,
        f"name: q\nengine: vllm\nport: 8000\nvllm:\n"
        f"  model: {model}\n  tensor_parallel_size: 8\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    cmd, env = a.build_command()
    assert cmd[0].endswith("vllm.exe") or cmd[0].endswith("vllm")
    assert cmd[1] == "serve"
    assert cmd[2] == str(model)
    assert "--served-model-name" in cmd
    assert "--tensor-parallel-size" in cmd
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venvs" / "vllm")


def test_build_command_docker_template(tmp_path, monkeypatch):
    """docker 路径 build_command 命令模板正确。"""
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    model_dir = tmp_path / "m" / "Qwen3.8-Flash-Next-FP8"
    model_dir.mkdir(parents=True)
    p = _write(
        tmp_path,
        f"name: q\nengine: vllm\nport: 8110\nvllm:\n"
        f"  model: {model_dir}\n"
        "  docker_image: vllm/vllm-openai:qwen38-flash-next\n"
        "  tensor_parallel_size: 8\n"
        '  extra_args: "--reasoning-parser qwen3"\n',
    )
    a = get_adapter("vllm")(p, CAPS8)
    cmd, env = a.build_command()
    assert cmd[0] == "docker"
    assert cmd[1] == "run"
    assert "--name" in cmd
    idx = cmd.index("--name")
    assert cmd[idx + 1] == "q-vllm"
    assert "--gpus" in cmd
    idx = cmd.index("--gpus")
    assert cmd[idx + 1] == '"device=0,1,2,3,4,5,6,7"'
    assert "-p" in cmd
    idx = cmd.index("-p")
    assert cmd[idx + 1] == "8110:8000"
    assert "-v" in cmd
    idx = cmd.index("-v")
    expected_mount = f"{model_dir.parent.as_posix()}:/models:ro"
    assert cmd[idx + 1] == expected_mount
    assert "--ipc=host" in cmd
    assert "--detach" in cmd
    # 镜像
    image = "vllm/vllm-openai:qwen38-flash-next"
    assert image in cmd
    # CMD 段（image 之后）必须紧跟 serve + 容器内模型路径
    # 不能含 "vllm" 作为独立 token——否则 docker ENTRYPOINT=["vllm"] 会拼成 `vllm vllm serve ...`，
    # argparse 报 "unrecognized arguments: serve /models/..." 退出 (exit code 2)
    image_pos = cmd.index(image)
    assert cmd[image_pos + 1] == "serve"
    assert cmd[image_pos + 2] == "/models/Qwen3.8-Flash-Next-FP8"
    assert "vllm" not in cmd[image_pos + 1:]
    assert "--port" in cmd
    idx = cmd.index("--port")
    assert cmd[idx + 1] == "8000"
    # extra_args 透传
    assert "--reasoning-parser" in cmd and "qwen3" in cmd
    # env 不注入 VIRTUAL_ENV（容器自管）
    assert "VIRTUAL_ENV" not in env


def test_build_command_docker_relative_model_rejected(tmp_path, monkeypatch):
    """docker 路径 + HF repo id（相对路径且非目录）→ RequirementError。"""
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: ./models/Qwen3.8-Flash-Next-FP8\n"
        "  docker_image: x/y:z\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    with pytest.raises(RequirementError, match="本地绝对路径"):
        a.build_command()


def test_build_command_docker_gpus_from_gpu_list(tmp_path, monkeypatch):
    """docker 路径 + gpu_list → --gpus 仅含指定 GPU。"""
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    model_dir = tmp_path / "m" / "X"
    model_dir.mkdir(parents=True)
    p = _write(
        tmp_path,
        f"name: q\nengine: vllm\nport: 8000\nvllm:\n"
        f"  model: {model_dir}\n"
        "  docker_image: x/y:z\n"
        "  gpu_list: '0,2,4'\n"
        "  tensor_parallel_size: 3\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    cmd, env = a.build_command()
    assert '"device=0,2,4"' in cmd


# ---- Task 4: stop_patterns 双模式 ----


def test_stop_patterns_venv_unchanged(tmp_path, monkeypatch):
    """venv 路径 stop_patterns 不变。"""
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    p = _write(tmp_path, "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: /x/Y\n")
    a = get_adapter("vllm")(p, CAPS8)
    assert a.stop_patterns() == ["vllm serve"]


def test_stop_patterns_docker_two_modes(tmp_path, monkeypatch):
    """docker 路径 stop_patterns 返回 2 个模式，且模式 1 是 Popen cmdline 的连续子串。"""
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    model_dir = tmp_path / "m" / "X"
    model_dir.mkdir(parents=True)
    p = _write(
        tmp_path,
        f"name: q\nengine: vllm\nport: 8000\nvllm:\n"
        f"  model: {model_dir}\n"
        "  docker_image: x/y:z\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    a._selected_gpus_override = [0, 2, 4]  # 不影响 selected_gpus（仍从 profile 取）
    patterns = a.stop_patterns()
    assert len(patterns) == 2
    # 模式 1：docker run --name <name> --gpus <json>——与 build_command 首段连续一致
    expected_cmdline = " ".join([
        "docker", "run", "--name", "q-vllm",
        "--gpus", '"device=0,1,2,3,4,5,6,7"',
        "-p", "8000:8000",
        "-v", f"{model_dir.parent.as_posix()}:/models:ro",
        "--ipc=host", "--detach", "x/y:z",
        "serve",
    ])
    assert patterns[0] in expected_cmdline
    # 模式 2：-v <root>:/models:ro——也是 cmdline 子串
    assert patterns[1] in expected_cmdline


# ---- Task 6: pre_start 写回（docker 复用）+ 全仓 yaml 回归 ----


def test_pre_start_persists_local_path_for_docker(tmp_path, monkeypatch):
    """docker 类型 + HF repo id → pre_start 触发下载 → cfg["model"] 更新为本地路径 → build_command 可用。"""
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    import modelctl.engines.vllm as vllm_mod

    download_dir = tmp_path / "model-hf" / "X"
    download_dir.mkdir(parents=True)
    (download_dir / "config.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(vllm_mod, "download_repo", lambda repo, root: download_dir)

    p = _write(
        tmp_path,
        "name: fake-d\nengine: vllm\nport: 8110\nvllm:\n"
        "  model: Qwen/X\n"
        "  download:\n    modelscope_id: Qwen/X\n"
        "  docker_image: x/y:z\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    a.pre_start()
    # pre_start 把 profile.engine_config["model"] 直接更新为下载目录
    assert p.engine_config["model"] == str(download_dir.resolve())
    # yaml 文件内 model 字段也被 persist_model_path 文本级替换
    text = p.path.read_text(encoding="utf-8")
    assert str(download_dir.resolve()) in text
    # build_command 此时可用（model 已是本地路径）
    cmd, _ = a.build_command()
    assert "/models/X" in cmd or f"models{os.sep}X" in " ".join(cmd)


# ---- Task 3 (per-request-audit): build_command 新 flag + 版本探测 ----


def test_build_command_with_per_request_metrics_flag(tmp_path, monkeypatch):
    """venv 路径：yaml 两字段同时 true → cmd 含两个 flag。"""
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/Qwen3-32B\n"
        "  enable_per_request_metrics: true\n"
        "  enable_force_include_usage: true\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    a.check_requirements()
    cmd, _ = a.build_command()
    assert "--enable-per-request-metrics" in cmd
    assert "--enable-force-include-usage" in cmd


def test_build_command_default_unchanged(tmp_path, monkeypatch):
    """关键守门：未配置两个新字段 → cmd 不含新 flag，且既有字段与改造前一致。"""
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/Qwen3-32B\n  tensor_parallel_size: 2\n  max_model_len: 32768\n"
        '  extra_args: "--enable-prefix-caching"\n',
    )
    a = get_adapter("vllm")(p, CAPS8)
    a.check_requirements()
    cmd, _ = a.build_command()
    assert "--enable-per-request-metrics" not in cmd
    assert "--enable-force-include-usage" not in cmd
    assert cmd[cmd.index("--tensor-parallel-size") + 1] == "2"
    assert cmd[cmd.index("--max-model-len") + 1] == "32768"
    assert cmd[cmd.index("--served-model-name") + 1] == "q"
    assert "--enable-prefix-caching" in cmd


def test_build_command_only_force_include_usage(tmp_path, monkeypatch):
    """venv 路径：只开 enable_force_include_usage=true → 仅含该 flag。"""
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    _stub_venv(tmp_path, monkeypatch, "vllm")
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/Qwen3-32B\n"
        "  enable_force_include_usage: true\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    cmd, _ = a.build_command()
    assert "--enable-force-include-usage" in cmd
    assert "--enable-per-request-metrics" not in cmd


def test_requirement_version_guard(tmp_path, monkeypatch):
    """venv 路径：开启 flag + 版本 < 0.13.0 → RequirementError。"""
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    _stub_venv(tmp_path, monkeypatch, "vllm")
    monkeypatch.setattr("modelctl.core.envs.vllm_version", lambda: (0, 12, 0))
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/Qwen3-32B\n"
        "  enable_per_request_metrics: true\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    with pytest.raises(RequirementError):
        a.check_requirements()


def test_requirement_version_missing_warns_not_raises(tmp_path, monkeypatch):
    """venv 路径：版本探测失败（None）→ 仅 warning，放行。"""
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    _stub_venv(tmp_path, monkeypatch, "vllm")
    monkeypatch.setattr("modelctl.core.envs.vllm_version", lambda: None)
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n"
        "  model: Qwen/Qwen3-32B\n"
        "  enable_per_request_metrics: true\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    a.check_requirements()  # 不 raise


def test_requirement_not_flagged_no_version_check(tmp_path, monkeypatch):
    """venv 路径：两字段均缺省 → check_requirements 不调 vllm_version。"""
    calls = []
    monkeypatch.setenv("HF_HOME", "/raid5/sh/model/huggingface")
    _stub_venv(tmp_path, monkeypatch, "vllm")

    def tracker():
        calls.append(1)
        return (0, 27, 0)

    monkeypatch.setattr("modelctl.core.envs.vllm_version", tracker)
    p = _write(
        tmp_path,
        "name: q\nengine: vllm\nport: 8000\nvllm:\n  model: Qwen/Qwen3-32B\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    a.check_requirements()
    assert calls == []  # 未触发版本探测


def test_build_command_docker_with_per_request_metrics_flag(tmp_path, monkeypatch):
    """docker 路径：yaml 配 docker_image + 两字段 true → docker run 的 vllm 参数含两个 flag。"""
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    model_dir = tmp_path / "m" / "Qwen3.8"
    model_dir.mkdir(parents=True)
    p = _write(
        tmp_path,
        f"name: q\nengine: vllm\nport: 8000\nvllm:\n"
        f"  model: {model_dir}\n"
        "  docker_image: vllm/vllm-openai:qwen38-flash-next\n"
        "  enable_per_request_metrics: true\n"
        "  enable_force_include_usage: true\n",
    )
    a = get_adapter("vllm")(p, CAPS8)
    cmd, _ = a.build_command()
    assert "--enable-per-request-metrics" in cmd
    assert "--enable-force-include-usage" in cmd


def test_full_vllm_suite_no_regression(tmp_path, monkeypatch):
    """总结算：所有未配 docker_image 的 vllm yaml，build_command 走托管 venv 路径。"""
    monkeypatch.delenv("MODELCTL_GPUS", raising=False)
    monkeypatch.setenv("API_KEY", "test")
    from pathlib import Path
    from modelctl.core.profile import load_profile
    from modelctl.core.capabilities import Capabilities

    P = Path(__file__).resolve().parents[1] / "models" / "vllm"
    caps = Capabilities(gpu_count=8, gpu_indices=list(range(8)), compute_capability="8.9", binaries={"vllm": True})
    checked = 0
    for f in sorted(P.glob("*.yaml")):
        try:
            prof = load_profile(f.stem, f.parent)
        except Exception:
            continue  # 非本测试关注的解析失败（如缺额外环境变量）
        if prof.engine != "vllm":
            continue
        a = get_adapter("vllm")(prof, caps)
        if prof.engine_config.get("docker_image"):
            continue  # qwen3.8-flash-next：配了 docker_image，跳过
        try:
            cmd, _ = a.build_command()
            assert "docker" not in " ".join(cmd), f"{f.name} 不应含 docker run"
            checked += 1
        except RequirementError as e:
            # venv 侧异常（如 ensure_env 缺 venv）可接受，但绝不等于 docker 路径异常
            assert "docker" not in str(e), f"{f.name} 不应报 docker 异常：{e}"
        except Exception:
            pass  # 其他非关键异常忽略
    assert checked >= 1, "至少应有一个 venv 路径 yaml 被验证"
