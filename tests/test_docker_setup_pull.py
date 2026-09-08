"""ensure_image 流式 pull：on_progress 收到聚合进度、失败分类不变、回调缺省兼容。"""
from __future__ import annotations

from unittest import mock

from modelctl.core import docker_setup


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self._lines = lines
        self.returncode = returncode
        self.stderr = ""
        self.stdout = ""

    def __iter__(self):
        return iter(self._lines)

    # 简报 fixture 缺 wait()：实现必须 wait() 回收子进程防僵尸（设计意图），
    # 故按测试+设计意图为准补齐 fake 的 wait 契约，不改任何断言。
    def wait(self):
        return self.returncode


def test_ensure_image_streams_progress():
    lines = [
        "aaa: Pulling fs layer",
        "aaa: Downloading 512MB/1GB",
        "aaa: Pull complete",
        "Status: Downloaded newer image",
    ]
    seen = []
    with mock.patch.object(docker_setup, "image_present", return_value=False), \
         mock.patch.object(docker_setup.subprocess, "Popen", return_value=_FakeProc(lines)):
        ok = docker_setup.ensure_image("img:tag", on_progress=lambda lab, pct: seen.append(pct))
    assert ok is True
    assert 0 < max(seen) <= 1.0


def test_ensure_image_cached_skips_pull():
    with mock.patch.object(docker_setup, "image_present", return_value=True):
        assert docker_setup.ensure_image("img:tag", on_progress=lambda l, p: None) is True


def test_ensure_image_failure_no_callback_still_works():
    proc = _FakeProc(["Error: manifest unknown"], returncode=1)
    with mock.patch.object(docker_setup, "image_present", return_value=False), \
         mock.patch.object(docker_setup.subprocess, "Popen", return_value=proc), \
         mock.patch.object(docker_setup.time, "sleep"):
        assert docker_setup.ensure_image("img:tag", attempts=1) is False
