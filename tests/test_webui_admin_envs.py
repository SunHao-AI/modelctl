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

import time as _time
from unittest import mock

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402


def _wait_for(fn, timeout: float = 3.0, poll: float = 0.05) -> bool:
    """轮询等待 fn() 返回真值；超时返回 False。

    用于 daemon 后台线程完成后验证去重窗被清理（线程与 with 生命周期解耦）。
    """
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if fn():
            return True
        _time.sleep(poll)
    return bool(fn())

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
    """默认 os 按 sys.platform 分支：能用的 diagnose 都返回 dict 5/6 字段 + instructions 字符串。"""
    import sys as _sys

    import modelctl.core.docker_setup as ds
    import modelctl.core.windows_setup as ws

    # 主机为 Linux：os 缺省 → linux 分支
    monkeypatch.setattr("modelctl.core.webui.admin_envs.sys.platform", "linux", raising=True)
    monkeypatch.setattr(ds, "diagnose", lambda *a, **k: [
        type("C", (), {"key": "docker_cli", "label": "docker CLI", "ok": False, "detail": "未安装"})()
    ])
    monkeypatch.setattr(ds, "render_instructions", lambda *a, **k: "# script")
    r = _get(admin_client, "/admin/api/envs/docker/diagnose")
    assert r.status_code == 200
    body = r.json()
    assert body["platform"] == "linux"
    assert body["checks"] == [
        {"key": "docker_cli", "label": "docker CLI", "ok": False, "detail": "未安装"}
    ]
    assert body["instructions"] == "# script"

    # 主机为 win32：os=windows 分支；5 字段含 hint
    monkeypatch.setattr("modelctl.core.webui.admin_envs.sys.platform", "win32", raising=True)

    class FakeWins:
        key = "winget"
        label = "winget"
        ok = True
        detail = ""
        hint = ""

    monkeypatch.setattr(ws, "diagnose", lambda *a, **k: [FakeWins()])
    r = _get(admin_client, "/admin/api/envs/docker/diagnose?os=windows")
    assert r.status_code == 200
    body = r.json()
    assert body["platform"] == "windows"
    assert body["checks"][0] == {
        "key": "winget", "label": "winget", "ok": True, "detail": "", "hint": ""
    }
    # Windows 分支不返回 shell 脚本
    assert body["instructions"] == ""


def test_docker_diagnose_requires_auth(admin_client):
    assert admin_client.get("/admin/api/envs/docker/diagnose").status_code == 401


# ---------------------------------------------------------------------------
# Docker 一键安装（Windows-only）：install SSE / system-action / diagnose?os=
# 全部 mock 子进程与平台，避免真跑 winget / UAC / shutdown。
# ---------------------------------------------------------------------------


def _post(client: TestClient, path: str, body: dict):
    return client.post(
        path, json=body, headers={"Authorization": f"Bearer {KEY}"}
    )


def _clear_docker_installs(monkeypatch):
    """清 admin_envs 模块级去重状态，避免用例间串扰。"""
    import modelctl.core.webui.admin_envs as ae

    monkeypatch.setattr(ae, "_user_active_installs", {}, raising=False)
    # 将 task_manager 与本模块解耦重挂，确保每例独立
    from modelctl.core.webui.admin_tasks import TaskManager

    monkeypatch.setattr(ae, "docker_install_task_manager", TaskManager(), raising=False)


def test_install_endpoint_401_unauthed(admin_client):
    """未带 Bearer → 401（统一走 require_auth）。"""
    r = admin_client.post("/admin/api/envs/docker/install", json={"os": "windows"})
    assert r.status_code == 401


def test_install_endpoint_202_win32_returns_task_id(admin_client, monkeypatch):
    """win32 + 合法 body → 202 + task_id + events 路径 + os=windows。

    线程跑完后去重窗应被后台线程 finally 精确清掉（不误删同用户后续任务）。
    """
    import modelctl.core.webui.admin_envs as ae

    _clear_docker_installs(monkeypatch)
    monkeypatch.setattr(ae.sys, "platform", "win32", raising=True)

    # 让 run_install 立即返回 0
    with mock.patch("modelctl.core.windows_setup.run_install") as mi:
        mi.return_value = 0
        r = _post(admin_client, "/admin/api/envs/docker/install", {"os": "windows"})
    assert r.status_code == 202
    body = r.json()
    assert body.get("task_id")
    assert body.get("events") == f"/admin/api/envs/docker/install/{body['task_id']}/events"
    assert body.get("os") == "windows"
    # 等一下让后台线程跑完 → 去重窗被 finally 精确移除
    ok = _wait_for(lambda: len(ae._user_active_installs) == 0)
    assert ok, f"dedup window not cleared after thread done: {dict(ae._user_active_installs)}"
    # run_install 真的被调到了
    assert mi.call_count == 1


def test_install_endpoint_400_linux_os_windows(admin_client, monkeypatch):
    """非 win32 主机 + body.os=windows → 400。"""
    import modelctl.core.webui.admin_envs as ae

    _clear_docker_installs(monkeypatch)
    monkeypatch.setattr(ae.sys, "platform", "linux", raising=True)
    r = _post(admin_client, "/admin/api/envs/docker/install", {"os": "windows"})
    assert r.status_code == 400


def test_install_endpoint_dedup_same_user(admin_client, monkeypatch):
    """同用户 5min 内已有活跃 task → 直接返回原 task_id + already_running: true。"""
    import modelctl.core.webui.admin_envs as ae

    _clear_docker_installs(monkeypatch)
    monkeypatch.setattr(ae.sys, "platform", "win32", raising=True)

    # 预置一条"活跃"记录：task 需存在 + 去重条目时间戳为近值
    # user_id = Bearer token = KEY（_clear_docker_installs 已换成新的 TaskManager）
    tm = ae.docker_install_task_manager
    fake_task = tm.create_task(kind="docker", action="install", target="docker:windows")
    import time as _t

    monkeypatch.setattr(
        ae,
        "_user_active_installs",
        {KEY: {"task_id": fake_task.id, "started_at": _t.time(), "os": "windows"}},
    )
    # 同 Bearer token 复用 → 命中 dedup，不应新建（run_install 不应该被调用）
    with mock.patch("modelctl.core.windows_setup.run_install") as mi:
        mi.return_value = 0  # 即使被调也不出事
        r1 = _post(admin_client, "/admin/api/envs/docker/install", {"os": "windows"})
    assert r1.status_code == 202
    body1 = r1.json()
    assert body1.get("already_running") is True
    assert body1.get("task_id") == fake_task.id
    # run_install 应该没被真的触发（已命中去重窗）
    assert not mi.called


def test_system_action_endpoint_400_non_win32(admin_client, monkeypatch):
    """非 win32 主机上 system-action → 400。"""
    import modelctl.core.webui.admin_envs as ae

    monkeypatch.setattr(ae.sys, "platform", "linux", raising=True)
    r = _post(admin_client, "/admin/api/envs/docker/system-action", {"action": "restart"})
    assert r.status_code == 400


def test_system_action_endpoint_open_desktop_win32_ok(admin_client, monkeypatch):
    """win32 + open_desktop → 200 + executed 含 explorer.exe + ms-settings:developers。"""
    import modelctl.core.webui.admin_envs as ae

    monkeypatch.setattr(ae.sys, "platform", "win32", raising=True)
    calls = []

    class _FakeShell32:
        @staticmethod
        def ShellExecuteW(*a, **kw):
            calls.append((a, kw))
            return 0

    class _FakeWindll:
        shell32 = _FakeShell32

    class _FakeCtypes:
        windll = _FakeWindll

    with mock.patch.object(ae, "ctypes", _FakeCtypes()):
        r = _post(admin_client, "/admin/api/envs/docker/system-action",
                  {"action": "open_desktop"})
    assert r.status_code == 200
    body = r.json()
    assert "explorer.exe" in body.get("executed", "")
    assert "ms-settings:developers" in body.get("executed", "")
    # 至少一次 shell32.ShellExecuteW 调用
    assert calls, "expected at least one ShellExecuteW call"


def test_system_action_endpoint_restart_win32_ok(admin_client, monkeypatch):
    """win32 + restart → 200 + Popen 收到 shutdown /r /t 5。"""
    import modelctl.core.webui.admin_envs as ae

    monkeypatch.setattr(ae.sys, "platform", "win32", raising=True)
    calls = []

    def _fake_popen(args, *a, **kw):
        calls.append((args, kw))
        return object()

    with mock.patch("modelctl.core.webui.admin_envs.subprocess.Popen", side_effect=_fake_popen):
        r = _post(admin_client, "/admin/api/envs/docker/system-action",
                  {"action": "restart"})
    assert r.status_code == 200
    assert calls, "expected at least one Popen call"
    args = calls[0][0]
    joined = " ".join(args)
    assert "shutdown.exe" in joined
    assert "/r" in joined
    assert "/t" in joined
    assert "5" in joined


def test_system_action_endpoint_verify_returns_405(admin_client, monkeypatch):
    """verify 永远走 GET /diagnose，POST → 405。"""
    r = _post(admin_client, "/admin/api/envs/docker/system-action", {"action": "verify"})
    assert r.status_code == 405


def test_diagnose_endpoint_os_windows_non_win32_400(admin_client, monkeypatch):
    """非 win32 主机 + query os=windows → 400。"""
    import modelctl.core.webui.admin_envs as ae

    monkeypatch.setattr(ae.sys, "platform", "linux", raising=True)
    r = _get(admin_client, "/admin/api/envs/docker/diagnose?os=windows")
    assert r.status_code == 400
