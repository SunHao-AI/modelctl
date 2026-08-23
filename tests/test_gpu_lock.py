"""core/gpu_lock.py 单元测试。"""

import json
import os

import pytest

from modelctl.core.gpu_lock import acquire_gpu_lock, list_gpu_locks, release_gpu_lock, update_gpu_lock_owner
from modelctl.engines.base import RequirementError


def test_acquire_and_list(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp_path)
    acquire_gpu_lock("a", [0, 1])
    assert list_gpu_locks() == {0: "a", 1: "a"}


def test_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp_path)
    acquire_gpu_lock("a", [0, 1])
    with pytest.raises(RequirementError, match="已被模型 a 占用"):
        acquire_gpu_lock("b", [1, 2])


def test_release(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp_path)
    acquire_gpu_lock("a", [0])
    release_gpu_lock("a")
    assert list_gpu_locks() == {}


def test_stale_lock_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp_path)
    lock = tmp_path / "stale.gpu-lock"
    lock.write_text(json.dumps({"gpus": [0], "pid": 9999999, "updated_at": 0}), encoding="utf-8")
    acquire_gpu_lock("a", [0])
    assert list_gpu_locks() == {0: "a"}


def test_same_name_reacquire_allowed(tmp_path, monkeypatch):
    # restart of the same model must not conflict with its own (still-live) lock
    monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp_path)
    acquire_gpu_lock("a", [0, 1])
    acquire_gpu_lock("a", [0, 1])  # no error
    assert list_gpu_locks() == {0: "a", 1: "a"}


def _dead_pid() -> int:
    import ctypes
    import subprocess
    import sys

    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait(timeout=15)
    if sys.platform == "win32":
        # Windows：须主动释放父进程对已退出子进程的句柄，否则内核对象驻留、OpenProcess 仍会探测为存活
        h = int(p._handle)
        p._handle.closed = True  # 防止 Popen 析构时二次 CloseHandle 报警告
        ctypes.windll.kernel32.CloseHandle(h)
    return p.pid


def test_conflict_survives_cli_exit_engine_alive(tmp_path, monkeypatch):
    # After start_profile updates the owner to the long-lived engine pid, an overlapping
    # model must still be blocked even though the original *acquire* was done by a (now-gone) CLI.
    monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp_path)
    acquire_gpu_lock("a", [0, 1])                 # acquired with current (CLI-like) pid
    update_gpu_lock_owner("a", os.getpid())       # simulate re-pointing owner at a live engine process
    with pytest.raises(RequirementError, match="占用"):
        acquire_gpu_lock("b", [1, 2])


def test_stale_cleaned_when_engine_dead(tmp_path, monkeypatch):
    monkeypatch.setattr("modelctl.core.gpu_lock.LOCK_DIR", tmp_path)
    acquire_gpu_lock("a", [0])
    dead = _dead_pid()                             # a PID that is definitely not running
    update_gpu_lock_owner("a", dead)               # owner died → lock becomes stale
    assert list_gpu_locks().get(0) != "a"          # auto-cleaned on next listing
