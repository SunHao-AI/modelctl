#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_webui_admin_envs.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 14:30
# @Desc   : 环境端点扩展字段与 Docker 旁路指引/诊断测试
# ===============================================================================

"""admin_envs：GET /envs 扩展字段 + GET /envs/docker/diagnose 测试。"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_envs"


@pytest.fixture()
def admin_client(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    logger.remove()
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c


def _get(client: TestClient, path: str):
    return client.get(path, headers={"Authorization": f"Bearer {KEY}"})


def test_list_envs_extended_fields_non_linux(admin_client, monkeypatch):
    """非 Linux 态：托管引擎 platform_supported=False，gateway 恒 True。"""
    monkeypatch.setattr("modelctl.core.envs._is_linux", lambda: False)
    r = _get(admin_client, "/admin/api/envs")
    assert r.status_code == 200
    by_name = {t["name"]: t for t in r.json()["targets"]}
    assert {"vllm", "sglang", "gateway"} <= set(by_name)
    for name, t in by_name.items():
        assert {"name", "installed", "detail", "platform_supported", "docker_supported"} <= set(t)
        assert t["platform_supported"] is (name == "gateway"), name
    assert by_name["vllm"]["docker_supported"] is True
    assert by_name["sglang"]["docker_supported"] is False
    assert by_name["gateway"]["docker_supported"] is False


def test_list_envs_platform_linux(admin_client, monkeypatch):
    monkeypatch.setattr("modelctl.core.envs._is_linux", lambda: True)
    body = _get(admin_client, "/admin/api/envs").json()
    assert all(t["platform_supported"] for t in body["targets"])


def test_list_envs_docker_env_and_bypass(admin_client):
    body = _get(admin_client, "/admin/api/envs").json()
    de = body["docker_env"]
    assert isinstance(de["ready"], bool)
    assert isinstance(de["missing"], list)
    assert de["guide"]
    assert de["ready"] == (len(de["missing"]) == 0)

    bypass = {e["name"]: e for e in body["docker_bypass"]}
    assert set(bypass) == {"vllm", "sglang", "aphrodite", "lmdeploy", "tokenspeed", "tensorrt_llm"}
    # 指引常量表的键集合必须与能力矩阵单一事实来源严格一致（防两集合漂移）
    from modelctl.core.envs import DOCKER_CAPABLE_ENGINES
    from modelctl.core.webui.admin_envs import DOCKER_BYPASS_GUIDES

    assert set(DOCKER_BYPASS_GUIDES) == set(DOCKER_CAPABLE_ENGINES)
    v = bypass["vllm"]
    assert v["docker_supported"] is True
    assert v["image_example"].startswith("vllm/vllm-openai:")
    assert v["yaml_field_path"] == "vllm.docker_image"
    assert v["example_yaml"] == "models/vllm/qwen3.8-flash-next.yaml"
    assert len(v["steps"]) == 3
    assert v["steps"][0].startswith("编辑 models/vllm/")
    assert bypass["tokenspeed"]["image_example"] == "lightseekorg/tokenspeed:latest"
    assert bypass["tensorrt_llm"]["image_example"].startswith("nvcr.io/nvidia/tensorrt-llm:")
    for name in ("sglang", "aphrodite", "lmdeploy"):
        assert bypass[name]["docker_supported"] is False
        assert bypass[name]["note"]
        assert "steps" not in bypass[name]  # 不造假指引


def test_docker_diagnose_shape(admin_client, monkeypatch):
    import modelctl.core.docker_setup as ds

    class FakeCheck:
        key = "docker_cli"
        label = "docker CLI（PATH）"
        ok = False
        detail = "未安装"

    monkeypatch.setattr(ds, "diagnose", lambda: [FakeCheck()])
    monkeypatch.setattr(ds, "render_instructions", lambda *a, **k: "# script")
    r = _get(admin_client, "/admin/api/envs/docker/diagnose")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"] == [
        {"key": "docker_cli", "label": "docker CLI（PATH）", "ok": False, "detail": "未安装"}
    ]
    assert body["instructions"] == "# script"


def test_docker_diagnose_requires_auth(admin_client):
    assert admin_client.get("/admin/api/envs/docker/diagnose").status_code == 401
