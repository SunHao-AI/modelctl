#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_core_windows_setup.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : Windows Docker 一键安装模块测试
# ===============================================================================

"""core/windows_setup.py 测试（全 mock，不跑真 winget / 不弹 UAC / 不真调 subprocess）。

本机（Windows 开发机）限制：
- 不跑真 `winget install Docker.DockerDesktop`（会触发 UAC + 30min 下载）
- 不跑真 `wsl --version` / `docker run`（子进程一律 mock）
- 文件 IO 通过 monkeypatch `ws.DAEMON_JSON` 重定向到 `tmp_path`
- 平台通过 monkeypatch `ws.sys.platform` 切换（windows_setup 必须 `import sys`
  而非 `from sys import platform`，本文件同时验证该约束）
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modelctl.core import docker_setup as ds
from modelctl.core import windows_setup as ws
from modelctl.core.sse_stage_event import StageEvent


# ---- 公共 mock 辅助 ----


def _win(monkeypatch) -> None:
    """把模块级 `sys.platform` 切到 win32（依赖 windows_setup 用 `import sys`）。"""
    monkeypatch.setattr(ws.sys, "platform", "win32")


def _which(monkeypatch, present: set[str]) -> None:
    """shutil.which 按 present 集合决定命中。"""
    monkeypatch.setattr(ws.shutil, "which", lambda name: f"C:\\Program Files\\{name}" if name in present else None)


class _Proc:
    """伪造 subprocess.run 的 CompletedProcess。"""

    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def _collect_events() -> list[StageEvent]:
    """造一个收集器 on_stage，返回事件列表（供 StageEvent 断言）。"""
    events: list[StageEvent] = []
    return events  # noqa: F841  # 由调用方自行闭包引用


def _stage_seq(events: list[StageEvent]) -> list[str]:
    return [e.stage for e in events]


def _win_install_mocks(monkeypatch, tmp_path, *, with_winget: bool = True,
                       with_docker: bool = True, with_nvidia: bool = False,
                       desktop_installed: bool = False) -> Path:
    """run_install / diagnose 的通用 mock：PATH 探测 + DAEMON_JSON 重定向。

    返回重定向后的 daemon.json 路径。
    """
    target = tmp_path / "daemon.json"
    monkeypatch.setattr(ws, "DAEMON_JSON", target)
    present = set()
    if with_winget:
        present.add("winget")
    if with_docker:
        present.add("docker")
    if with_nvidia:
        present.add("nvidia-smi")
    _which(monkeypatch, present)
    # 显式 mock（False 时也 mock）：本机（开发机）可能已装 Docker Desktop，
    # 不 mock 时真实 _desktop_installed() 会通过 DESKTOP_EXE_CANDIDATES 命中。
    monkeypatch.setattr(ws, "_desktop_installed", lambda: desktop_installed)
    return target


# ---- 1. Check dataclass 字段一致性 ----


def test_check_dataclass_fields_match_docker_setup():
    """windows_setup.Check 字段必须与 docker_setup.Check 字段名一一对齐（含 hint）。

    前端表格靠字段名渲染模板；字段漂移 → 渲染缺列。
    """
    ds_fields = {f.name for f in dataclasses.fields(ds.Check)}
    ws_fields = {f.name for f in dataclasses.fields(ws.Check)}
    assert ds_fields <= ws_fields, (ds_fields, ws_fields)


# ---- 2-4. path_level_missing ----


def test_path_level_missing_no_winget(monkeypatch):
    _which(monkeypatch, set())
    assert ws.path_level_missing() == ["winget"]


def test_path_level_missing_no_docker(monkeypatch):
    _which(monkeypatch, {"winget"})
    assert ws.path_level_missing() == ["docker"]


def test_path_level_missing_all_present(monkeypatch):
    _which(monkeypatch, {"winget", "docker"})
    assert ws.path_level_missing() == []


# ---- 5-7. diagnose ----


def test_diagnose_returns_five_checks(monkeypatch, tmp_path):
    """全齐环境：5 项 Check 全 ok=False→True 取决于 mock；此处看数量与 key 集合。"""
    _win(monkeypatch)
    _win_install_mocks(monkeypatch, tmp_path,
                       with_winget=True, with_docker=True, with_nvidia=False,
                       desktop_installed=True)

    class _Probe:
        returncode = 0
        stdout = "WSL version"

    class _Run:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(ws.subprocess, "run", lambda *a, **kw: _Run())
    monkeypatch.setattr(ws, "_probe_wsl2", lambda: (0, "WSL 12.0.x"))
    checks = ws.diagnose()
    assert len(checks) == 5
    keys = {c.key for c in checks}
    assert keys == {"winget", "wsl2", "desktop", "docker", "gpus"}, keys
    for c in checks:
        assert isinstance(c.label, str) and c.label
        assert isinstance(c.ok, bool)


def test_diagnose_gpu_skip_when_no_nvidia_smi(monkeypatch, tmp_path):
    """无 nvidia-smi → gpus 项 ok=True 且 hint 标注 skip（不阻断安装）。"""
    _win(monkeypatch)
    _win_install_mocks(monkeypatch, tmp_path,
                       with_winget=True, with_docker=True, with_nvidia=False,
                       desktop_installed=True)

    monkeypatch.setattr(ws.subprocess, "run", lambda *a, **kw: _Proc(0))
    monkeypatch.setattr(ws, "_probe_wsl2", lambda: (0, "WSL 12.0.x"))
    checks = {c.key: c for c in ws.diagnose()}
    assert checks["gpus"].ok is True
    assert "skip" in (checks["gpus"].hint or "").lower()


def test_diagnose_gpu_smoke_run_called_when_nvidia_present(monkeypatch, tmp_path):
    """有 nvidia-smi → gpus 项跑 docker run --gpus all 冒烟。"""
    _win(monkeypatch)
    _win_install_mocks(monkeypatch, tmp_path,
                       with_winget=True, with_docker=True, with_nvidia=True,
                       desktop_installed=True)
    calls: list[tuple] = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return _Proc(0, stdout="GPU Available", stderr="")

    monkeypatch.setattr(ws.subprocess, "run", fake_run)
    monkeypatch.setattr(ws, "_probe_wsl2", lambda: (0, "WSL 12.0.x"))
    ws.diagnose()
    smoke = [c for c in calls if c and "docker" in c[0] and "--gpus" in str(c[0])]
    assert smoke, "有 nvidia-smi 时应触发 docker run --gpus all 冒烟"
    cmd = smoke[0][0]
    assert ws.GPU_SMOKE_IMAGE in cmd
    assert "nvidia-smi" in cmd


# ---- 8. install_steps ----


def test_install_steps_order_and_winget_cmd():
    """4 步固定顺序；winget 步骤的 cmd 是真正的外部命令且含 WINGET_ID + --silent。"""
    steps = ws.install_steps(["https://m1"], 2)
    assert len(steps) == 4
    # 占位 _modelctl_* 的步骤不在 winget cmd 里，winget 命令本身必须是外部命令
    winget_cmds = [cmd for _, cmd in steps if "winget" in cmd]
    assert len(winget_cmds) == 1
    cmd = winget_cmds[0]
    assert ws.WINGET_ID in cmd
    assert "--silent" in cmd
    # winget 步骤必须在检测（_modelctl_detect_prereqs）之后、落配置（_modelctl_merge_daemon_json）之前
    cmds = [cmd for _, cmd in steps]
    assert "_modelctl_detect_prereqs" in cmds
    assert "_modelctl_merge_daemon_json" in cmds
    assert cmds.index(winget_cmds[0]) == 1


# ---- 9. render_instructions ----


def test_render_instructions_contains_key_lines():
    """指引节选必须含 winget 安装命令、registry-mirrors 说明与验证引导。"""
    text = ws.render_instructions(["https://m1"], 2)
    assert "winget install" in text
    assert ws.WINGET_ID in text
    assert "registry-mirrors" in text
    assert "--silent" in text


# ---- 10-14. _merge_daemon_json ----


def test_merge_daemon_json_creates_parent_dir(monkeypatch, tmp_path):
    """目标父目录不存在 → 自动 mkdir + 写入 registry-mirrors。"""
    target = tmp_path / "deep" / "nested" / "daemon.json"
    monkeypatch.setattr(ws, "DAEMON_JSON", target)
    assert ws._merge_daemon_json(["https://m1"]) is True
    assert target.is_file()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["registry-mirrors"] == ["https://m1"]


def test_merge_daemon_json_preserves_runtime_fields(monkeypatch, tmp_path):
    """已有 runtime 等用户字段原样保留，只追加 registry-mirrors。"""
    target = tmp_path / "daemon.json"
    target.write_text(json.dumps({
        "runtime": {"nvidia": {"path": "/usr/bin/nvidia-container-runtime"}},
        "log-driver": "json-file",
    }), encoding="utf-8")
    monkeypatch.setattr(ws, "DAEMON_JSON", target)
    assert ws._merge_daemon_json(["https://m1"]) is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["runtime"]["nvidia"]["path"] == "/usr/bin/nvidia-container-runtime"
    assert data["log-driver"] == "json-file"
    assert data["registry-mirrors"] == ["https://m1"]


def test_merge_daemon_json_removes_dead_mirrors(monkeypatch, tmp_path):
    """旧 daemon.json 里残留停服源 → 合并时主动剔除。"""
    target = tmp_path / "daemon.json"
    target.write_text(json.dumps({
        "registry-mirrors": ["https://docker.mirrors.tuna.tsinghua.edu.cn",
                             "https://m1"],
    }), encoding="utf-8")
    monkeypatch.setattr(ws, "DAEMON_JSON", target)
    assert ws._merge_daemon_json(["https://m1"]) is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["registry-mirrors"] == ["https://m1"]


def test_merge_daemon_json_union_keeps_user_order(monkeypatch, tmp_path):
    """union 保序：用户 preferred 排前，新源追加在后；重复不写二次。"""
    target = tmp_path / "daemon.json"
    monkeypatch.setattr(ws, "DAEMON_JSON", target)
    assert ws._merge_daemon_json(["https://a", "https://b"]) is True
    assert json.loads(target.read_text(encoding="utf-8"))["registry-mirrors"] == ["https://a", "https://b"]
    # 再次合并带重复 + 新源
    assert ws._merge_daemon_json(["https://b", "https://a", "https://c"]) is True
    assert json.loads(target.read_text(encoding="utf-8"))["registry-mirrors"] == [
        "https://a", "https://b", "https://c"]


def test_merge_daemon_json_no_change_when_idempotent(monkeypatch, tmp_path):
    """目标状态已一致 → 返回 False 且不重写文件。"""
    target = tmp_path / "daemon.json"
    monkeypatch.setattr(ws, "DAEMON_JSON", target)
    assert ws._merge_daemon_json(["https://m1"], 2) is True
    raw = target.read_text(encoding="utf-8")
    assert ws._merge_daemon_json(["https://m1"], 2) is False
    assert target.read_text(encoding="utf-8") == raw


# ---- 15. run_install 平台门禁 ----


def test_run_install_rejects_non_win32(monkeypatch, tmp_path):
    """非 win32 平台 → 预检阶段 exit 2 + error 事件含平台引导文案。"""
    monkeypatch.setattr(ws.sys, "platform", "linux")
    events: list[StageEvent] = []
    monkeypatch.setattr(ws, "DAEMON_JSON", tmp_path / "daemon.json")
    rc = ws.run_install(None, None, on_stage=events.append)
    assert rc == 2
    assert any(e.type == "error" for e in events)
    err = next(e for e in events if e.type == "error")
    assert "windows" in (err.message or "").lower() or "win32" in (err.message or "").lower()


# ---- 16. run_install 无 winget ----


def test_run_install_no_winget_returns_2(monkeypatch, tmp_path):
    """win32 + 无 winget → 预检阶段 exit 2（不进入 winget 子进程阶段）。"""
    _win(monkeypatch)
    _win_install_mocks(monkeypatch, tmp_path, with_winget=False, with_docker=False)
    # mock 省掉真实 wsl --version 调用的副作用（部分 Windows 上 wsl 输出含
    # 不可解码字节，且不应在单元 mock 测试里 spawn 子进程）
    monkeypatch.setattr(ws, "_probe_wsl2", lambda: (0, "WSL 12.0.x"))
    events: list[StageEvent] = []
    rc = ws.run_install(None, None, on_stage=events.append)
    assert rc == 2
    seq = _stage_seq(events)
    assert "detect_winget" in seq
    assert "winget_running" not in seq
    # detect_wsl2 仍会跑（winget 缺失不影响其它探测）
    assert "detect_wsl2" in seq


# ---- 17. run_install already_installed 分支 ----


def test_run_install_already_installed_branch(monkeypatch, tmp_path):
    """Desktop 已装 → already_installed 分支，不跑 winget。"""
    _win(monkeypatch)
    _win_install_mocks(monkeypatch, tmp_path,
                       with_winget=True, with_docker=True, with_nvidia=False,
                       desktop_installed=True)

    monkeypatch.setattr(ws.subprocess, "run", lambda *a, **kw: _Proc(0, stdout="OK"))
    monkeypatch.setattr(ws, "_probe_wsl2", lambda: (0, "WSL 12.0.x"))

    events: list[StageEvent] = []
    rc = ws.run_install(None, None, on_stage=events.append)
    assert rc == 0
    seq = _stage_seq(events)
    # 首项并尾 item 验证：起始 detect_winget；skip winget_running 跳 already_installed
    assert seq[0] == "detect_winget"
    assert "winget_running" not in seq
    assert "already_installed" in seq
    assert "write_daemon_json" in seq
    assert "test_docker_version" in seq
    assert "post_install_plan" in seq
    assert seq[-1] == "done"
    # 完整阶段流：detect_wsl2 夹在 detect_winget 与 write_daemon_json 之间
    assert "detect_wsl2" in seq


# ---- 18. run_install winget 失败 → error code=3 + payload.tail ----


def test_run_install_winget_fail_emits_error(monkeypatch, tmp_path):
    """winget 子进程 exit != 0 → error(code=3) + payload.tail 末 ≤50 行 stdout。

    生产实现走 subprocess.Popen（用于静默检测 UAC）；此处用 Popen mock 形式
    模拟 winget 立即失败（poll 一次返回 rc=3）+ 长 communicate 输出。
    """
    _win(monkeypatch)
    _win_install_mocks(monkeypatch, tmp_path,
                       with_winget=True, with_docker=False, with_nvidia=False,
                       desktop_installed=False)
    monkeypatch.setattr(ws, "_probe_wsl2", lambda: (0, "WSL 12.0.x"))

    # 200 行 stdout，失败；tail 应只取后 50 行
    long_out = "\n".join(f"line-{i}" for i in range(200)) + "\n"

    class _FailProc:
        """模拟 winget 子进程：立即退出 rc=3，communicate 返回长 stdout。"""
        returncode = 3

        def __init__(self, *args, **kwargs):
            """兼容 `subprocess.Popen(cmd, stdout=..., stderr=..., text=True, ...)`。"""
            pass

        def poll(self):
            return 3

        def communicate(self, timeout=None):
            return long_out.encode("utf-8"), b""

    monkeypatch.setattr(ws.subprocess, "Popen", lambda *a, **kw: _FailProc())
    # UAC 静默阈值 0.05s：让静默检测不在 winget 立即退出路径上误触发
    monkeypatch.setattr(ws, "UAC_SILENCE_SEC", 9999.0)

    events: list[StageEvent] = []
    rc = ws.run_install(None, None, on_stage=events.append)
    assert rc == 3
    assert "winget_running" in _stage_seq(events)
    err = next(e for e in events if e.type == "error")
    assert err.code == 3
    assert err.payload and "tail" in err.payload
    tail = err.payload["tail"]
    assert isinstance(tail, list)
    assert len(tail) <= 50
    # tail 来自原始 stdout 的最后 50 行
    assert any("199" in line for line in tail[-5:])


# ---- 19. run_install UAC 20s 无输出 → need_UAC 事件但仍继续等 ----


def test_run_install_uac_mask_emits_need_uac(monkeypatch, tmp_path):
    """winget 首 20s 无 stdout 输出 → 静默检测 emit need_UAC 但仍继续等，
    最终输出"Installing..."后完成安装，走 already 检测后续阶段。"""
    _win(monkeypatch)
    _win_install_mocks(monkeypatch, tmp_path,
                       with_winget=True, with_docker=True, with_nvidia=False,
                       desktop_installed=False)
    monkeypatch.setattr(ws, "_probe_wsl2", lambda: (0, "WSL 12.0.x"))

    class _FakeProc:
        """可控的 Popen 替身：前 5 次 poll 视为未结束 + 无输出，之后退出。

        实现里采用 `time.monotonic()` 判定静默（first_line_at 设为 None 表示首行未到）。
        当 t - start 超过 UAC_SILENCE_SEC 且 first_line_at is None → emit need_UAC。
        """

        def __init__(self, *args, **kwargs):
            """兼容 `subprocess.Popen(cmd, stdout=..., stderr=..., text=True, close_fds=True)`
            等调用约定：Popen 工厂忽略实参，仅复位内部状态。"""
            self._t0 = time.time()
            self._polls = 0
            # 脚本行为：前 3 次 poll() 期间 time.time() 不动（让 _FakeClock 推进近 21s）

        def poll(self):
            self._polls += 1
            return None if self._polls <= 3 else 0

        def communicate(self, timeout=None):
            """阻塞到结束，返回 stdout 内容（兼容 timeout 关键字参数）。"""
            time.sleep(0)  # 无实际等待
            return b"Installing...\nGenerally the standard...\n", b""

    monkeypatch.setattr(ws.subprocess, "Popen", _FakeProc)
    # 让 time.time 自己推进（默认即 time.time）——但需要保证 UAC 静默判定走完
    # 不 mock time.time，默认即返回真实墙钟。为了让测试稳定，我们不改 time.time，
    # 改为依赖实现内部调用 time.time() 自然递增。由于前 3 次 poll 在真实墙钟的
    # 微小间隔内即触发，若想强制触发 UAC，最快是实现里把 UAC_SILENCE_SEC 读作
    # 模块常量。实现侧会读 ws.UAC_SILENCE_SEC —— 测试可在 _win_install_mocks 后
    # 再 monkeypatch.setattr(ws, "UAC_SILENCE_SEC", 0.05) 来把 20s 改为 50ms，
    # 让真实 wallclock 3 次 poll 循环自然跨过阈值。
    monkeypatch.setattr(ws, "UAC_SILENCE_SEC", 0.05)
    # 旁路非 winget 子进程（docker --version / GPU 冒烟等）：直接返回成功空输出，
    # 避免这些路径再走 Popen 与 _FakeProc 的 stateful 计数器冲突。
    monkeypatch.setattr(
        ws.subprocess, "run",
        lambda *a, **kw: _Proc(0, stdout="Docker version 28.0.0, build 8689098"),
    )

    events: list[StageEvent] = []
    rc = ws.run_install(None, None, on_stage=events.append)
    assert rc == 0
    stages = _stage_seq(events)
    assert "need_UAC" in stages, f"≥ UAC_SILENCE_SEC 静默应触发 need_UAC，实际 stages={stages}"
    # UAC 之后安装不被打断——必须继续到 winget_done / 后续阶段
    assert "winget_done" in stages
    assert stages[-1] == "done"


# ---- 20. post_install_plan ----


def test_post_install_plan_has_five_steps():
    """阶段 B 5 步固定 id/label/action 序 + optional 字段类型。"""
    plan = ws.post_install_plan(nvidia_present=False)
    assert "steps" in plan
    steps = plan["steps"]
    assert len(steps) == 5
    ids = [s["id"] for s in steps]
    assert ids == ["restart", "open_desktop", "enable_wsl2", "enable_gpu", "verify"], ids
    for s in steps:
        assert "label" in s
        assert s["label"]
        assert s["action"] in ("restart", "open_desktop", "verify", None)
        assert s["cmd_hint"] is None or isinstance(s["cmd_hint"], str)
        assert isinstance(s.get("optional", False), bool)
    # 无 nvidia → GPU 步骤标 optional
    assert steps[3]["optional"] is True
    # 有 nvidia → 不再 optional
    plan2 = ws.post_install_plan(nvidia_present=True)
    assert plan2["steps"][3]["optional"] is False


# ---- 21. StageEvent 契约 ----


def test_stage_event_frozen_dataclass():
    """frozen=True + code/payload 缺省 None + to_sse_dict 去 None 字段。"""
    frozen = dataclasses.fields(StageEvent)
    type_ = getattr(frozen, "frozen", None)
    # 检查 frozen=True 通过 replace() 抛 FrozenInstanceError
    with pytest.raises(dataclasses.FrozenInstanceError):
        StageEvent(type="stage", stage="detect_winget", message="m", ts="2026-09-07 10:00:00",
                   )
        ev = StageEvent(type="stage", stage="detect_winget", message="m", ts="t")
        ev.stage = "x"  # noqa: 仅触发抛错
    ev = StageEvent(type="stage", stage="done", message="OK", ts="2026-09-07 10:00:00")
    out = ev.to_sse_dict()
    assert out == {"type": "stage", "stage": "done", "message": "OK", "ts": "2026-09-07 10:00:00"}
    # code/payload 显式带值时输出
    ev2 = StageEvent(type="error", stage="error", message="fail", ts="t", code=3,
                     payload={"tail": ["x"]})
    out2 = ev2.to_sse_dict()
    assert out2["code"] == 3
    assert out2["payload"] == {"tail": ["x"]}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
