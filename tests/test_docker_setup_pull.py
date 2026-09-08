"""ensure_image 流式 pull：on_progress 收到聚合进度、失败分类不变、回调缺省兼容。"""
from __future__ import annotations

from unittest import mock

import pytest

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


def test_ensure_image_kills_proc_on_keyboard_interrupt():
    """中断路径与旧 subprocess.run 语义等价：kill 子进程再上抛，且回收不留僵尸。"""
    class _InterruptProc:
        def __init__(self):
            self.returncode = None  # 读循环被打断时子进程仍在运行
            self.killed = False
            self.waited = False

        def __iter__(self):
            yield "aaa: Pulling fs layer"
            raise KeyboardInterrupt

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self):
            self.waited = True
            return self.returncode

    proc = _InterruptProc()
    with mock.patch.object(docker_setup, "image_present", return_value=False), \
         mock.patch.object(docker_setup.subprocess, "Popen", return_value=proc):
        with pytest.raises(KeyboardInterrupt):
            docker_setup.ensure_image("img:tag")
    assert proc.killed is True
    assert proc.waited is True


def test_ensure_image_pull_uses_utf8_stream():
    """显式 UTF-8：docker CLI 输出为 UTF-8，Windows GBK 环境不设 encoding 会乱码。"""
    proc = _FakeProc(["Status: Downloaded newer image"])
    with mock.patch.object(docker_setup, "image_present", return_value=False), \
         mock.patch.object(docker_setup.subprocess, "Popen", return_value=proc) as popened:
        assert docker_setup.ensure_image("img:tag") is True
    kwargs = popened.call_args.kwargs
    assert kwargs.get("encoding") == "utf-8"
    assert kwargs.get("errors") == "replace"
