#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_engines_download.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Desc   : 模型下载与包安装测试
# ===============================================================================

"""tests/test_engines_download.py — ModelScope 下载工具测试。

核心依赖安装已迁移到 core/deps.py（详见 tests/test_core_deps.py），
本文件保留 _download.download_repo 入口的薄测试 + §2.2 各引擎 pre_start 接入测试。
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest
import modelctl.engines._download as dl
from modelctl.engines._download import ModelDownloadError


def test_download_repo_uses_modelscope(tmp_path, monkeypatch):
    """download_repo：透传 modelscope.snapshot_download，返回目标目录。"""
    calls: list = []

    def fake_snapshot_download(model_id, local_dir, **_kwargs):
        calls.append((model_id, local_dir))
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        return local_dir

    # 注入假 modelscope：避免依赖真实安装
    fake_modelscope = types.ModuleType("modelscope")
    fake_modelscope.snapshot_download = fake_snapshot_download
    monkeypatch.setitem(sys.modules, "modelscope", fake_modelscope)
    # 同时屏蔽 core/deps 的自动安装路径（测试环境无需网络）
    monkeypatch.setattr(dl, "ensure_packages", lambda _checklist: True)

    result = dl.download_repo("unsloth/Qwen3.8-27B-GGUF", tmp_path)
    assert calls == [("unsloth/Qwen3.8-27B-GGUF", str(tmp_path / "Qwen3.8-27B-GGUF"))]
    assert result == tmp_path / "Qwen3.8-27B-GGUF"


def test_download_repo_skips_when_already_populated(tmp_path, monkeypatch):
    """目录已含权重/配置文件 → 直接复用，不触发 modelscope 下载。

    这是取消"写回 YAML"后仍不重复下载的前提：落地路径由 MODEL_ROOT + modelscope_id
    确定性推导，同一台机器下次启动会推导出同一路径。
    """
    calls: list = []
    monkeypatch.setattr(dl, "_snapshot_download", lambda *a, **k: calls.append(a))

    dest = tmp_path / "Qwen3.8-27B-GGUF"
    dest.mkdir(parents=True)
    (dest / "config.json").write_text("{}", encoding="utf-8")

    assert dl.download_repo("unsloth/Qwen3.8-27B-GGUF", tmp_path) == dest
    assert calls == []


def test_repo_local_dir_is_deterministic(tmp_path):
    """落地路径只由 modelscope_id + local_root 决定（无需持久化即可复用）。"""
    a = dl.repo_local_dir("unsloth/Qwen3.8-27B-GGUF", tmp_path)
    b = dl.repo_local_dir("unsloth/Qwen3.8-27B-GGUF", tmp_path)
    assert a == b == tmp_path / "Qwen3.8-27B-GGUF"


# ---- 404 / 限流 / 网络包装为中文 ModelDownloadError ----

def _make_fake_fail(raise_exc):
    """测试辅助：构造一个会抛 raise_exc 的 fake modelscope 模块。"""
    fake = types.ModuleType("modelscope")
    fake.snapshot_download = lambda *a, **k: (_ for _ in ()).throw(raise_exc)
    return fake


def test_download_repo_404_wraps_to_friendly_error(tmp_path, monkeypatch):
    """上游 404 → 包装为 ModelDownloadError，message 含中文提示 + 仓库 ID，cause 保留原始错。"""
    monkeypatch.setitem(sys.modules, "modelscope", _make_fake_fail(
        RuntimeError("404 Client Error: record not found (request_id=abc-123)")
    ))
    monkeypatch.setattr(dl, "ensure_packages", lambda _c: True)
    monkeypatch.setattr(dl, "snapshot_download", None, raising=False)

    with pytest.raises(ModelDownloadError) as exc_info:
        dl.download_repo("ModelCloud/Qwen2.5-0.5B-Instruct-GPTQ", tmp_path)
    msg = str(exc_info.value)
    assert "不存在" in msg or "已改名" in msg
    assert "ModelCloud/Qwen2.5-0.5B-Instruct-GPTQ" in msg
    assert "ModelScope" in msg  # 中文“ModelScope 仓库不存在…”
    assert exc_info.value.cause_message
    assert "404" in exc_info.value.cause_message


def test_download_repo_rate_limit_wraps_to_friendly_error(tmp_path, monkeypatch):
    """上游 429 限流 → 中文提示含重试/HF_ENDPOINT 镜像。"""
    monkeypatch.setitem(sys.modules, "modelscope", _make_fake_fail(
        RuntimeError("429 Too Many Requests: rate limit")
    ))
    monkeypatch.setattr(dl, "ensure_packages", lambda _c: True)
    monkeypatch.setattr(dl, "snapshot_download", None, raising=False)

    with pytest.raises(ModelDownloadError) as exc_info:
        dl.download_repo("org/model", tmp_path)
    assert "429" in str(exc_info.value)
    assert "HF_ENDPOINT" in str(exc_info.value)


def test_download_repo_network_error_wraps_to_friendly_error(tmp_path, monkeypatch):
    """上游 connection/timeout → 通用中文提示（代理 / HF_ENDPOINT）。"""
    monkeypatch.setitem(sys.modules, "modelscope", _make_fake_fail(
        RuntimeError("proxy connect timeout after 30s")
    ))
    monkeypatch.setattr(dl, "ensure_packages", lambda _c: True)
    monkeypatch.setattr(dl, "snapshot_download", None, raising=False)

    with pytest.raises(ModelDownloadError) as exc_info:
        dl.download_repo("org/model", tmp_path)
    msg = str(exc_info.value)
    assert "代理" in msg or "HF_ENDPOINT" in msg


def test_download_repo_failure_cleans_partial_dir(tmp_path, monkeypatch):
    """下载中途失败 → 清掉已 mkdir 的 destination，避免污染下次 _is_populated。"""
    calls: list = []

    def fake_fail(model_id, local_dir, **_kw):
        calls.append(local_dir)
        # 模拟已写部分权重
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        (Path(local_dir) / "partial.safetensors").write_bytes(b"x" * 1024)
        raise RuntimeError("network interrupted")

    fake = types.ModuleType("modelscope")
    fake.snapshot_download = fake_fail
    monkeypatch.setitem(sys.modules, "modelscope", fake)
    monkeypatch.setattr(dl, "ensure_packages", lambda _c: True)
    monkeypatch.setattr(dl, "snapshot_download", None, raising=False)

    with pytest.raises(ModelDownloadError):
        dl.download_repo("org/model", tmp_path)

    # 测试三段：(1) 触发了下载  (2) partial 目录已清理  (3) 再 download 时不会复用
    assert len(calls) == 1
    assert not (tmp_path / "model").exists()
    assert not dl._is_populated(tmp_path / "model")


# ---- §2.2：aphrodite / lmdeploy 下载 pre_start 路径 ----

def test_aphrodite_pre_start_downloads_when_model_missing(tmp_path, monkeypatch):
    """§2.2: aphrodite pre_start 在下载目录下缺失时，触发 download 并写回 YAML。"""
    import modelctl.engines._download as dl
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import load_profile
    from modelctl.engines import get_adapter

    bin_dir = tmp_path / ".venvs" / "aphrodite" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / ("python.exe" if os.name == "nt" else "python")).write_bytes(b"fake")
    (bin_dir / ("aphrodite.exe" if os.name == "nt" else "aphrodite")).write_bytes(b"fake")
    monkeypatch.setattr("modelctl.core.envs.VENV_ROOT", tmp_path / ".venvs")
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))

    def fake_snapshot_download(model_id, local_dir, **kw):
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        (Path(local_dir) / "config.json").write_text("{}", encoding="utf-8")
        return local_dir
    # download_repo 内部通过 _snapshot_download 间接调用 modelscope.snapshot_download。
    # 这里直接 patch _snapshot_download 兜底路径，避免触发真实安装流程。
    monkeypatch.setattr(dl, "_snapshot_download", fake_snapshot_download)

    (tmp_path / "m.yaml").write_text(
        "name: q\nengine: aphrodite\nport: 8140\naphrodite:\n"
        "  model: /nonexistent/model\n"
        "  download:\n    modelscope_id: org/Qwen3.8-27B\n",
        encoding="utf-8",
    )
    p = load_profile("m", tmp_path)
    a = get_adapter("aphrodite")(p, Capabilities(gpu_count=8, binaries={"aphrodite": True}))
    a.pre_start()
    # cfg.model 更新为本地目录（仅内存）
    assert Path(p.engine_config["model"]).is_dir()
    # 不写回 YAML：profile 保持原样，git 干净
    assert "model: /nonexistent/model" in (tmp_path / "m.yaml").read_text(encoding="utf-8")
    assert not (tmp_path / "m.yaml.bak").exists()


def test_lmdeploy_pre_start_noop_when_model_exists(tmp_path, monkeypatch):
    """model 已是目录 → pre_start 直接返回（无副作用）。"""
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import load_profile
    from modelctl.engines import get_adapter

    bin_dir = tmp_path / ".venvs" / "lmdeploy" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / ("python.exe" if os.name == "nt" else "python")).write_bytes(b"fake")
    (bin_dir / ("lmdeploy.exe" if os.name == "nt" else "lmdeploy")).write_bytes(b"fake")
    monkeypatch.setattr("modelctl.core.envs.VENV_ROOT", tmp_path / ".venvs")

    model_dir = tmp_path / "qwen3.8"
    model_dir.mkdir(parents=True)

    (tmp_path / "m.yaml").write_text(
        f"name: q\nengine: lmdeploy\nport: 8150\nlmdeploy:\n  model: {model_dir}\n",
        encoding="utf-8",
    )
    p = load_profile("m", tmp_path)
    a = get_adapter("lmdeploy")(p, Capabilities(gpu_count=8, binaries={"lmdeploy": True}))
    # 无副作用：pre_start 静默返回
    a.pre_start()
    assert a.warnings == []


def test_lmdeploy_pre_start_downloads_when_model_missing(tmp_path, monkeypatch):
    """model 缺失 + download 段声明 → download_repo 被触发（写回 YAML 并重载 profile）。"""
    import modelctl.engines._download as dl
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import load_profile
    from modelctl.engines import get_adapter

    bin_dir = tmp_path / ".venvs" / "lmdeploy" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / ("python.exe" if os.name == "nt" else "python")).write_bytes(b"fake")
    (bin_dir / ("lmdeploy.exe" if os.name == "nt" else "lmdeploy")).write_bytes(b"fake")
    monkeypatch.setattr("modelctl.core.envs.VENV_ROOT", tmp_path / ".venvs")
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))

    # download_repo 内部通过 _snapshot_download 间接调用 modelscope.snapshot_download。
    # 这里直接 patch _snapshot_download 兜底路径，避免触发真实安装流程。
    def fake_snapshot_download(model_id, local_dir, **kw):
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        return local_dir
    monkeypatch.setattr(dl, "_snapshot_download", fake_snapshot_download)

    (tmp_path / "m.yaml").write_text(
        "name: q\nengine: lmdeploy\nport: 8150\nlmdeploy:\n"
        "  model: /nonexistent/model\n"
        "  download:\n    modelscope_id: org/Qwen3.8-27B\n",
        encoding="utf-8",
    )
    p = load_profile("m", tmp_path)
    a = get_adapter("lmdeploy")(p, Capabilities(gpu_count=8, binaries={"lmdeploy": True}))
    a.pre_start()
    # cfg.model 更新为 MODEL_ROOT 下推导出的本地目录（仅内存）
    assert Path(p.engine_config["model"]).is_dir()
    # 不写回 YAML：profile 保持原样，git 干净
    assert "model: /nonexistent/model" in (tmp_path / "m.yaml").read_text(encoding="utf-8")
    assert not (tmp_path / "m.yaml.bak").exists()
