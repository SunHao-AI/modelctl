"""docker 日志 tee 生命周期：spawn 监督器、残留清理、kill、保活重连。"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

from modelctl.core import process


def test_spawn_log_tee_spawns_supervisor_and_writes_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(process, "cache_dir", lambda: tmp_path)
    with mock.patch.object(process.subprocess, "Popen") as popen:
        popen.return_value.pid = 4321
        pid = process.spawn_log_tee("q", ["docker", "logs", "-f", "--tail", "all", "q-vllm"])
    assert pid == 4321
    assert (tmp_path / "q.log-tee.pid").read_text(encoding="utf-8") == "4321"
    cmd, env = popen.call_args.args[0], popen.call_args.kwargs["env"]
    # 不再直接 spawn docker logs，而是 spawn 保活 supervisor：-m modelctl.core.process --log-tee
    assert cmd[:4] == [sys.executable, "-m", "modelctl.core.process", "--log-tee"]
    assert cmd[-2:] == ["q-vllm", str(tmp_path / "launch-q.log")]
    # PYTHONPATH 注入主项目 src/，保证 supervisor 子进程能 import modelctl.core.*
    src = str(Path(process.__file__).resolve().parents[2])
    assert src in env["PYTHONPATH"]
    # supervisor 自身输出进 devnull，容器日志由其内部 docker logs 续写
    assert popen.call_args.kwargs["stdout"] == process.subprocess.DEVNULL


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


def test_log_tee_supervisor_reconnects_with_tail_zero(tmp_path, monkeypatch):
    """docker logs 断裂且容器仍存活 → 以 `--tail 0` 重连（不重放全量历史）。"""
    log_path = str(tmp_path / "launch-q.log")
    # 容器存活：进循环两次；wait 返回后 aliveness 依次 True（重连）、False（退出）
    with mock.patch.object(process, "docker_container_alive", side_effect=[True, True, True, False]) as alive, \
         mock.patch.object(process.subprocess, "Popen") as popen, \
         mock.patch.object(process.time, "sleep") as sleep:
        popen.return_value.wait.return_value = 0
        process._log_tee_supervisor("q-vllm", log_path, retry_sec=0.01)
    assert popen.call_count == 2
    assert popen.call_args_list[0].args[0] == ["docker", "logs", "-f", "--tail", "all", "q-vllm"]
    assert popen.call_args_list[1].args[0] == ["docker", "logs", "-f", "--tail", "0", "q-vllm"]
    assert sleep.called
    # 重连痕迹已落盘
    assert "自动重连第 1 次" in Path(log_path).read_text(encoding="utf-8", errors="replace")


def test_log_tee_supervisor_stops_when_container_dead(tmp_path):
    """容器已死 → 不再 spawn docker logs，直接静默退出。"""
    with mock.patch.object(process, "docker_container_alive", return_value=False) as alive, \
         mock.patch.object(process.subprocess, "Popen") as popen:
        process._log_tee_supervisor("q-vllm", str(tmp_path / "launch-q.log"))
    assert alive.called
    popen.assert_not_called()


def test_log_tee_supervisor_retries_until_container_dead(tmp_path):
    """docker CLI 抛错（OSError）且容器仍存活 → 退避重连；容器死透才退出。"""
    with mock.patch.object(process, "docker_container_alive", side_effect=[True, True, True, True, False]) as alive, \
         mock.patch.object(process.subprocess, "Popen", side_effect=OSError("docker missing")) as popen, \
         mock.patch.object(process.time, "sleep") as sleep:
        process._log_tee_supervisor("q-vllm", str(tmp_path / "launch-q.log"), retry_sec=0.01)
    assert popen.call_count == 2
    assert sleep.called


def test_main_unknown_args_logs_warning():
    """`__main__` 未知参数返回非零且告警、不抛。"""
    with mock.patch.object(process.logger, "warning") as warn:
        rc = process._main(["bogus"])
    assert rc == 2
    warn.assert_called_once()
