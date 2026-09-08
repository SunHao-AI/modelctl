"""docker 日志 tee 生命周期：spawn 参数、残留清理、kill、非 docker 不 spawn。"""
from __future__ import annotations

from unittest import mock

from modelctl.core import process


def test_spawn_log_tee_appends_and_writes_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(process, "cache_dir", lambda: tmp_path)
    with mock.patch.object(process.subprocess, "Popen") as popen:
        popen.return_value.pid = 4321
        pid = process.spawn_log_tee("q", ["docker", "logs", "-f", "--tail", "all", "q-vllm"])
    assert pid == 4321
    assert (tmp_path / "q.log-tee.pid").read_text(encoding="utf-8") == "4321"
    kwargs = popen.call_args.kwargs
    assert kwargs["stdout"] is not None and kwargs["stderr"] == process.subprocess.STDOUT
    # 以 append 打开，不截断 start_detached 写入的容器 ID 首行
    assert kwargs["stdout"].mode.startswith("ab") or "a" in kwargs["stdout"].mode


def test_spawn_log_tee_kills_residual_first(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(process, "cache_dir", lambda: tmp_path)
    (tmp_path / "q.log-tee.pid").write_text("9999", encoding="utf-8")
    with mock.patch.object(process, "is_pid_alive", return_value=True), \
         mock.patch.object(process, "kill_pid_tree") as killed, \
         mock.patch.object(process.subprocess, "Popen") as popen:
        popen.return_value.pid = 111
        process.spawn_log_tee("q", ["docker", "logs", "-f", "c"])
    killed.assert_called_once_with(9999)


def test_kill_log_tee_idempotent_without_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "cache_dir", lambda: tmp_path)
    process.kill_log_tee("nope")  # 不应抛
