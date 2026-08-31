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
本文件只保留 _download.download_repo 入口的薄测试。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import modelctl.engines._download as dl


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
