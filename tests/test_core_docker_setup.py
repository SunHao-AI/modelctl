#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_core_docker_setup.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : Docker 环境诊断与安装模块测试
# ===============================================================================

"""core/docker_setup.py 测试（全 mock，不依赖真实 docker / root）。"""

from __future__ import annotations

import json
import sys
import types

import pytest

from modelctl.core import docker_setup as ds


# ---- path_level_missing ----


def test_path_level_missing_all(monkeypatch):
    """docker 与 nvidia-smi 均缺失 → 两条缺失项 + 统一指引常量可用。"""
    monkeypatch.setattr(ds.shutil, "which", lambda name: None)
    missing = ds.path_level_missing()
    assert missing == [ds.MSG_DOCKER_MISSING, ds.MSG_TOOLKIT_MISSING]
    assert "modelctl env setup docker" in ds.MSG_GUIDE


def test_path_level_missing_none(monkeypatch):
    monkeypatch.setattr(ds.shutil, "which", lambda name: "/usr/bin/" + name)
    assert ds.path_level_missing() == []


def test_path_level_missing_toolkit_only(monkeypatch):
    """docker 在、nvidia-smi 不在 → 只报 toolkit。"""
    monkeypatch.setattr(
        ds.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    assert ds.path_level_missing() == [ds.MSG_TOOLKIT_MISSING]


# ---- 指引与安装步骤 ----


def test_render_instructions_uses_tuna_mirror():
    text = ds.render_instructions()
    assert ds.DOCKER_APT_MIRROR in text
    assert "download.docker.com" in text  # 官方源作为备注出现
    assert "modelctl env setup docker --run" in text
    assert "nvidia-container-toolkit" in text


def test_render_instructions_lists_mirrors():
    """缺省指引即展示内置默认多源；显式指定则展示用户列表。"""
    default_text = ds.render_instructions()
    for m in ds.DEFAULT_REGISTRY_MIRRORS:
        assert m in default_text
    custom = ds.render_instructions(["https://my.mirror"])
    assert "https://my.mirror" in custom


def test_install_steps_always_include_merge():
    """无论是否显式指定镜像，合并 daemon.json 步骤恒存在（默认多源）。"""
    cmds = [cmd for _, cmd in ds.install_steps()]
    assert ds.MERGE_DAEMON_JSON_CMD in cmds
    cmds2 = [cmd for _, cmd in ds.install_steps(["https://m1"])]
    assert ds.MERGE_DAEMON_JSON_CMD in cmds2


# ---- resolve_registry_mirrors ----


def test_resolve_mirrors_default():
    assert ds.resolve_registry_mirrors(None) == list(ds.DEFAULT_REGISTRY_MIRRORS)
    assert ds.resolve_registry_mirrors([]) == list(ds.DEFAULT_REGISTRY_MIRRORS)
    assert ds.resolve_registry_mirrors(["  "]) == list(ds.DEFAULT_REGISTRY_MIRRORS)


def test_resolve_mirrors_user_wins():
    """用户显式指定 → 完全以用户为准（不追加默认），去空/去重/去尾斜杠保序。"""
    got = ds.resolve_registry_mirrors(
        ["https://a.example/", "", None, "https://b.example", "https://a.example"])
    assert got == ["https://a.example", "https://b.example"]


# ---- 停服源识别 ----


def test_is_dead_mirror_tuna_hub_but_keeps_tuna_apt():
    """停服的只是 Hub 加速域名；TUNA 的 docker-ce apt 镜像绝不能被判死。"""
    assert ds.is_dead_mirror("https://docker.mirrors.tuna.tsinghua.edu.cn")
    assert not ds.is_dead_mirror(ds.DOCKER_APT_MIRROR)
    assert not ds.is_dead_mirror("https://docker.1ms.run")
    assert not ds.is_dead_mirror("")


def test_resolve_mirrors_drops_dead_user_mirror():
    """用户显式传停服源 → 剔除后仍有存活源则用之；全是停服源则回落默认。"""
    assert ds.resolve_registry_mirrors(
        ["https://docker.mirrors.tuna.tsinghua.edu.cn", "https://m1"]
    ) == ["https://m1"]
    assert ds.resolve_registry_mirrors(
        ["https://docker.mirrors.ustc.edu.cn"]
    ) == list(ds.DEFAULT_REGISTRY_MIRRORS)


# ---- daemon.json 合并 ----


def test_merge_daemon_json_preserves_existing(monkeypatch, tmp_path):
    target = tmp_path / "daemon.json"
    target.write_text(json.dumps({"runtimes": {"nvidia": {"path": "/usr/bin/x"}}}), encoding="utf-8")
    monkeypatch.setattr(ds, "DAEMON_JSON", target)
    assert ds._merge_daemon_json(["https://m1", "https://m2"]) is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["runtimes"]["nvidia"]["path"] == "/usr/bin/x"
    assert data["registry-mirrors"] == ["https://m1", "https://m2"]
    # 幂等：重复合并不追加、返回 False；新源追加在后面
    assert ds._merge_daemon_json(["https://m1", "https://m2"]) is False
    assert ds._merge_daemon_json(["https://m2", "https://m3"]) is True
    assert json.loads(target.read_text(encoding="utf-8"))["registry-mirrors"] == [
        "https://m1", "https://m2", "https://m3"]


# ---- pull 错误分类 ----


def test_classify_pull_error_transient_eof():
    """大 layer 传输被掐断：属可重试的传输中断。"""
    assert ds.classify_pull_error(
        "short read: expected 201 bytes but got 0: unexpected EOF") == "transient"
    assert ds.classify_pull_error("net/http: TLS handshake timeout") == "transient"


def test_classify_pull_error_hard_failures():
    """DNS 失败 / tag 不存在分别归类，两者重试都没意义。"""
    assert ds.classify_pull_error(
        "dial tcp: lookup docker.mirrors.tuna.tsinghua.edu.cn: no such host") == "dead-mirror"
    assert ds.classify_pull_error("manifest unknown") == "missing-tag"
    assert ds.classify_pull_error("something else") == "unknown"


# ---- ensure_image 重试 ----


class _PullProc:
    """伪造 docker pull 的子进程（ensure_image 流式化后走 Popen 契约：proc.stdout 可迭代 + wait）。"""

    def __init__(self, rc: int, stderr: str = ""):
        self.returncode = rc
        # stderr 已合并进 stdout（stderr=STDOUT），实现只读 proc.stdout
        self.stderr = None
        self.stdout = iter([f"{stderr}\n"] if stderr else [])

    def wait(self):
        return self.returncode


def test_ensure_image_skips_when_present(monkeypatch):
    monkeypatch.setattr(ds, "image_present", lambda image: True)
    monkeypatch.setattr(ds.subprocess, "run",
                        lambda *a, **kw: pytest.fail("已就位不应再 pull"))
    assert ds.ensure_image("img:1") is True


def test_ensure_image_retries_transient(monkeypatch):
    """传输中断 → 重试并最终成功（Docker 复用已下载 layer，pull 可重入）。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(ds, "image_present", lambda image: False)
    monkeypatch.setattr(ds, "PULL_RETRY_WAIT", 0)

    def fake_popen(cmd, **kw):
        calls.append(cmd)
        return _PullProc(1 if len(calls) < 3 else 0, "short read: unexpected EOF")

    monkeypatch.setattr(ds.subprocess, "Popen", fake_popen)
    assert ds.ensure_image("img:1", attempts=5) is True
    assert len(calls) == 3


def test_ensure_image_stops_on_dead_mirror(monkeypatch):
    """停服源 DNS 失败 → 第一次就退出，不浪费时间重试。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(ds, "image_present", lambda image: False)
    monkeypatch.setattr(ds, "PULL_RETRY_WAIT", 0)

    def fake_popen(cmd, **kw):
        calls.append(cmd)
        return _PullProc(1, "lookup x on 127.0.0.53:53: no such host")

    monkeypatch.setattr(ds.subprocess, "Popen", fake_popen)
    assert ds.ensure_image("img:1", attempts=5) is False
    assert len(calls) == 1


def test_ensure_image_exhausts_attempts(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(ds, "image_present", lambda image: False)
    monkeypatch.setattr(ds, "PULL_RETRY_WAIT", 0)

    def fake_popen(cmd, **kw):
        calls.append(cmd)
        return _PullProc(1, "i/o timeout")

    monkeypatch.setattr(ds.subprocess, "Popen", fake_popen)
    assert ds.ensure_image("img:1", attempts=3) is False
    assert len(calls) == 3


def test_image_present_false_on_missing_docker(monkeypatch):
    """docker 不在 PATH（FileNotFoundError）时不能抛异常，按"没有"处理。"""
    def boom(*a, **kw):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(ds.subprocess, "run", boom)
    assert ds.image_present("img:1") is False


def test_merge_daemon_json_sets_max_concurrent_downloads(monkeypatch, tmp_path):
    """并发数写进 daemon.json，且不覆盖 nvidia runtime 等既有键。"""
    target = tmp_path / "daemon.json"
    target.write_text(json.dumps({"runtimes": {"nvidia": {"path": "/usr/bin/x"}}}), encoding="utf-8")
    monkeypatch.setattr(ds, "DAEMON_JSON", target)
    assert ds._merge_daemon_json(["https://m1"], 2) is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["max-concurrent-downloads"] == 2
    assert data["runtimes"]["nvidia"]["path"] == "/usr/bin/x"
    # 已是目标值 → 无变更（幂等）
    assert ds._merge_daemon_json(["https://m1"], 2) is False


def test_merge_daemon_json_zero_keeps_existing_limit(monkeypatch, tmp_path):
    """max_downloads=0 表示保留机器现值，绝不能把用户的 5 改成默认 2。"""
    target = tmp_path / "daemon.json"
    target.write_text(json.dumps({"max-concurrent-downloads": 5}), encoding="utf-8")
    monkeypatch.setattr(ds, "DAEMON_JSON", target)
    assert ds._merge_daemon_json(["https://m1"], 0) is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["max-concurrent-downloads"] == 5


def test_run_install_writes_default_limit(monkeypatch, tmp_path):
    """--run 缺省即收敛到 DEFAULT_MAX_CONCURRENT_DOWNLOADS。"""
    monkeypatch.setattr(ds.sys, "platform", "linux")
    monkeypatch.setattr(ds.os, "geteuid", lambda: 0, raising=False)
    target = tmp_path / "daemon.json"
    monkeypatch.setattr(ds, "DAEMON_JSON", target)

    class _R:
        returncode = 0

    monkeypatch.setattr(ds.subprocess, "run", lambda cmd, **kw: _R())
    assert ds.run_install() == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["max-concurrent-downloads"] == ds.DEFAULT_MAX_CONCURRENT_DOWNLOADS


def test_render_instructions_mentions_limit():
    """指引必须说明并发数与 0 的语义，否则用户不知道那行配置从哪来。"""
    text = ds.render_instructions()
    assert "max-concurrent-downloads" in text
    assert str(ds.DEFAULT_MAX_CONCURRENT_DOWNLOADS) in text
    assert ds.render_instructions(max_downloads=7).count("max-concurrent-downloads=7") >= 1


def test_merge_daemon_json_prunes_dead_mirror(monkeypatch, tmp_path):
    """既有 daemon.json 残留停服源 → --run 必须剔除（只追加会让坏源永久残留）。"""
    target = tmp_path / "daemon.json"
    target.write_text(json.dumps({
        "runtimes": {"nvidia": {"path": "/usr/bin/x"}},
        "registry-mirrors": [
            "https://docker.mirrors.tuna.tsinghua.edu.cn",
            "https://docker.1ms.run",
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(ds, "DAEMON_JSON", target)
    # 传已存在的存活源 → 无新增，但坏源被剔除仍算有写入
    assert ds._merge_daemon_json(["https://docker.1ms.run"]) is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["runtimes"]["nvidia"]["path"] == "/usr/bin/x"
    assert data["registry-mirrors"] == ["https://docker.1ms.run"]
    # 幂等：坏源已清、存活源已在 → 返回 False
    assert ds._merge_daemon_json(["https://docker.1ms.run"]) is False


def test_merge_daemon_json_ignores_dead_user_mirror(monkeypatch, tmp_path):
    """显式传入停服源 → 不写盘，只保留默认/存活源。"""
    target = tmp_path / "daemon.json"
    monkeypatch.setattr(ds, "DAEMON_JSON", target)
    assert ds._merge_daemon_json(
        ["https://docker.mirrors.tuna.tsinghua.edu.cn", "https://m1"]) is True
    assert json.loads(target.read_text(encoding="utf-8"))["registry-mirrors"] == ["https://m1"]


def test_merge_daemon_json_creates_missing_file(monkeypatch, tmp_path):
    target = tmp_path / "docker" / "daemon.json"
    monkeypatch.setattr(ds, "DAEMON_JSON", target)
    assert ds._merge_daemon_json(["https://m1"]) is True
    assert json.loads(target.read_text(encoding="utf-8"))["registry-mirrors"] == ["https://m1"]


def test_merge_daemon_json_invalid_json(monkeypatch, tmp_path):
    target = tmp_path / "daemon.json"
    target.write_text("not-json{", encoding="utf-8")
    monkeypatch.setattr(ds, "DAEMON_JSON", target)
    assert ds._merge_daemon_json(["https://m1"]) is False
    assert target.read_text(encoding="utf-8") == "not-json{"  # 不破坏原文件


# ---- run_install 前置校验与执行 ----


def test_run_install_rejects_non_linux(monkeypatch):
    """_install_linux 在非 Linux 平台直接拒绝（不再经由 dispatcher 自动探测）。"""
    monkeypatch.setattr(ds.sys, "platform", "win32")
    assert ds._install_linux() == 2


def test_run_install_rejects_non_root(monkeypatch):
    monkeypatch.setattr(ds.sys, "platform", "linux")
    monkeypatch.setattr(ds.os, "geteuid", lambda: 1000, raising=False)
    assert ds.run_install() == 2


def test_run_install_executes_steps_in_order(monkeypatch, tmp_path):
    monkeypatch.setattr(ds.sys, "platform", "linux")
    monkeypatch.setattr(ds.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(ds, "DAEMON_JSON", tmp_path / "daemon.json")
    calls: list[list[str]] = []

    class _R:
        returncode = 0

    monkeypatch.setattr(ds.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _R())
    assert ds.run_install() == 0
    # merge 步骤走 Python 合并（落 tmp），bash 步骤与 install_steps 顺序一致
    bash_cmds = [cmd for _, cmd in ds.install_steps() if cmd != ds.MERGE_DAEMON_JSON_CMD]
    assert calls == [["bash", "-c", cmd] for cmd in bash_cmds]
    merged = json.loads((tmp_path / "daemon.json").read_text(encoding="utf-8"))
    assert merged["registry-mirrors"] == list(ds.DEFAULT_REGISTRY_MIRRORS)


def test_run_install_user_mirrors_written(monkeypatch, tmp_path):
    monkeypatch.setattr(ds.sys, "platform", "linux")
    monkeypatch.setattr(ds.os, "geteuid", lambda: 0, raising=False)
    target = tmp_path / "daemon.json"
    monkeypatch.setattr(ds, "DAEMON_JSON", target)

    class _R:
        returncode = 0

    monkeypatch.setattr(ds.subprocess, "run", lambda cmd, **kw: _R())
    assert ds.run_install(["https://user.example"]) == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["registry-mirrors"] == ["https://user.example"]


def test_run_install_stops_on_first_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ds.sys, "platform", "linux")
    monkeypatch.setattr(ds.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(ds, "DAEMON_JSON", tmp_path / "daemon.json")
    n = {"i": 0}

    class _R:
        @property
        def returncode(self):
            n["i"] += 1
            return 7 if n["i"] == 2 else 0

    calls: list[list[str]] = []
    monkeypatch.setattr(ds.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _R())
    assert ds.run_install() == 7
    assert len(calls) == 2  # 第二步失败即停


# ---- diagnose ----


def test_diagnose_all_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ds.shutil, "which", lambda name: None)
    monkeypatch.setattr(ds, "DAEMON_JSON", tmp_path / "absent.json")
    checks = {c.key: c for c in ds.diagnose()}
    assert not checks["docker_cli"].ok
    assert not checks["docker_daemon"].ok
    assert not checks["nvidia_toolkit"].ok
    assert not checks["nvidia_runtime"].ok


def test_diagnose_runtime_configured(monkeypatch, tmp_path):
    target = tmp_path / "daemon.json"
    target.write_text(json.dumps({"runtimes": {"nvidia": {}}}), encoding="utf-8")
    monkeypatch.setattr(ds, "DAEMON_JSON", target)
    checks = {c.key: c for c in ds.diagnose()}
    assert checks["nvidia_runtime"].ok


# ---- Check dataclass 字段锚定（防两端漂移） ----


def test_check_dataclass_keeps_5_fields_match_windows_setup():
    """防未来漂移：docker_setup.Check 字段名是 windows_setup.Check 字段名真子集。

    背景：windows_setup 比重版本（hint 字段，Windows 专用提示），但字段名
    集合必须对齐，这样前端模板（按字段名渲染表格）才能两端共用。
    本测试断言 `ds.Check` 字段 ⊆ `ws.Check` 字段（即 `ws.Check` 是超集）。
    """
    from dataclasses import fields
    from modelctl.core import windows_setup as ws

    ds_fields = {f.name for f in fields(ds.Check)}
    ws_fields = {f.name for f in fields(ws.Check)}
    assert ds_fields <= ws_fields, (ds_fields, ws_fields)


# ---- run_install dispatcher（--os linux|windows 分派） ----


def test_run_install_dispatcher_linux_default(monkeypatch):
    """Linux 主机缺省调用 → 路由到 _install_linux（透传参数与退出码）。"""
    monkeypatch.setattr(ds.sys, "platform", "linux")
    called: dict = {}

    def _fake_linux(registry_mirrors=None, max_downloads=None, on_stage=None):
        called.update(registry_mirrors=registry_mirrors,
                      max_downloads=max_downloads, on_stage=on_stage)
        return 0

    monkeypatch.setattr(ds, "_install_linux", _fake_linux)
    assert ds.run_install(["https://m1"], 2) == 0
    assert called["registry_mirrors"] == ["https://m1"]
    assert called["max_downloads"] == 2


def test_run_install_dispatcher_windows_hints_windows(monkeypatch):
    """win32 主机 + os_hint=windows → 委托 windows_setup.run_install（透传参数与退出码）。"""
    monkeypatch.setattr(ds.sys, "platform", "win32")
    called: dict = {}

    def _fake_ws_run_install(registry_mirrors=None, max_downloads=None, on_stage=None):
        called.update(registry_mirrors=registry_mirrors,
                      max_downloads=max_downloads, on_stage=on_stage)
        return 0

    fake_mod = types.ModuleType("modelctl.core.windows_setup")
    fake_mod.run_install = _fake_ws_run_install
    # 同时设置包属性 + sys.modules，确保 from modelctl.core import windows_setup 命中 mock
    _core_pkg = sys.modules["modelctl.core"]
    monkeypatch.setitem(sys.modules, "modelctl.core.windows_setup", fake_mod)
    monkeypatch.setattr(_core_pkg, "windows_setup", fake_mod, raising=False)
    assert ds.run_install(["https://mw"], 1, os_hint="windows") == 0
    assert called["registry_mirrors"] == ["https://mw"]
    assert called["max_downloads"] == 1


def test_run_install_dispatcher_windows_on_linux_reject(monkeypatch):
    """Linux 主机 + os_hint=windows → 平台门禁拒绝（return 2，不调 windows_setup）。"""
    monkeypatch.setattr(ds.sys, "platform", "linux")
    # 不 mock windows_setup.run_install（若 dispatcher 误调它，会真实执行 winget —— 视为回归）
    assert ds.run_install(None, None, os_hint="windows") == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
