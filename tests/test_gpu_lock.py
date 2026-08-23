"""core/gpu_lock.py 单元测试。"""

import json

import pytest

from modelctl.core.gpu_lock import acquire_gpu_lock, list_gpu_locks, release_gpu_lock
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
