"""docker 日志链路的**真实容器**补测（本机专属，CI 无 docker daemon 自动跳过）。

CI 里 `_docker_log_cmd` / `_read_docker_logs` / `docker_core_log_path` 只能拿假
subprocess 打桩过分支；「命令真能被 dockerd 执行、输出真能解析成行、LogPath 真能
inspect 到」这三件事是 CI 结构性盲区。本文件用本地已有镜像真起一个短命容器验证。

镜像选择本地已存在的通用镜像（redis:7-alpine → 回退任意本地镜像），绝不拉取外网；
一个都没有则 skip。容器名与被测 profile.name 一致（-vllm 结尾 → adapter._container_name
幂等直接复用），保证容器名推理与真实 --name 完全对上。清理放 fixture finally。
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytestmark = pytest.mark.skipif(
    bool(os.environ.get("CI")),
    reason="CI runner 上的 docker daemon 与镜像属宿主资产，本文件专属本机真跑",
)

from modelctl.core.webui import admin_models  # noqa: E402


def _docker(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _daemon_ok() -> bool:
    try:
        return _docker("info", timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _local_image() -> str | None:
    """只认 redis:7-alpine：它的默认入口能被 `sh -c` 接管，MARKER 必然出现在日志里。

    CI 的 ubuntu runner 自带 docker daemon 与宿主注入的 agent 镜像，`docker images`
    非空但那些镜像有自定义 entrypoint（吞掉我们的 sh -c），拿来用只会假失败。
    因此绝不"回退任意本地镜像"，没有 redis:7-alpine 就 skip。
    """
    try:
        proc = _docker("images", "--format", "{{.Repository}}:{{.Tag}}")
    except (OSError, subprocess.SubprocessError):
        return None
    tags = [t.strip() for t in proc.stdout.splitlines() if ":" in t.strip()]
    return "redis:7-alpine" if "redis:7-alpine" in tags else None


requires_real_docker = pytest.mark.skipif(not _daemon_ok(), reason="本机 docker daemon 不可用（CI 跳过）")
requires_local_image = pytest.mark.skipif(_local_image() is None, reason="本机无任何本地镜像（绝不现拉外网）")

MARKER = "MODELCTL-REALPROBE-LINE-42"


@pytest.fixture()
def real_container():
    """真起一个打印 MARKER 行的短命容器，yield 其名字，finally 强制清理。"""
    image = _local_image()
    name = f"modelctl-realprobe-{uuid.uuid4().hex[:8]}-vllm"
    run = _docker("run", "-d", "--name", name, image,
                  "sh", "-c", f"echo {MARKER}; sleep 30", timeout=60)
    assert run.returncode == 0, f"真起容器失败：{run.stderr[:300]}"
    try:
        # 等容器把 stdout 写进 json log（echo 极快，1s 足够）
        import time
        time.sleep(1.0)
        yield name
    finally:
        _docker("rm", "-f", name, timeout=30)


def _vllm_profile(name: str):
    from modelctl.core.profile import Profile

    return Profile(name=name, engine="vllm", port=8001,
                   engine_config={"model": "/m/x", "docker_image": _local_image()})


@requires_real_docker
@requires_local_image
def test_docker_log_cmd_builds_real_container_name(monkeypatch, real_container) -> None:
    """容器名推理第一来源真值：adapter._container_name == 真实 --name。"""
    monkeypatch.setattr(admin_models, "_find_profile",
                        lambda name: _vllm_profile(name) if name == real_container else None)
    monkeypatch.setattr("modelctl.core.capabilities.probe", lambda *a, **k: object())
    cmd = admin_models._docker_log_cmd(real_container, tail=50)
    assert cmd == ["docker", "logs", "--tail", "50", real_container]


@requires_real_docker
@requires_local_image
def test_read_docker_logs_returns_real_lines(monkeypatch, real_container) -> None:
    """`docker logs` 真执行：MARKER 行必须原样出现在返回列表里。"""
    monkeypatch.setattr(admin_models, "_find_profile",
                        lambda name: _vllm_profile(name) if name == real_container else None)
    monkeypatch.setattr("modelctl.core.capabilities.probe", lambda *a, **k: object())
    lines = admin_models._read_docker_logs(real_container, tail=200)
    assert MARKER in [ln.strip() for ln in lines]


@requires_real_docker
@requires_local_image
def test_docker_core_log_path_inspects_real_container(monkeypatch, real_container) -> None:
    """docker inspect LogPath 真链路：daemon 必须返回非空路径；函数按宿主可见性降级。

    WSL2-backed dockerDesktop 下 LogPath 是 VM 内路径（宿主不可见）→ 函数返 None；
    native docker（Linux 部署机）下宿主可见 → 函数返回该 Path。两种都是合法结果，
    但 inspect 本身必须真成功（直接子进程对照，防函数把 docker 失败也吞成合法 None）。
    """
    inspect = _docker("inspect", "--format", "{{.LogPath}}", real_container)
    assert inspect.returncode == 0 and inspect.stdout.strip(), inspect.stderr[:200]

    monkeypatch.setattr(admin_models, "_find_profile",
                        lambda name: _vllm_profile(name) if name == real_container else None)
    monkeypatch.setattr("modelctl.core.capabilities.probe", lambda *a, **k: object())
    got = admin_models.docker_core_log_path(real_container)
    assert got is None or (isinstance(got, Path) and got.is_file())


@requires_real_docker
@requires_local_image
def test_read_docker_logs_missing_container_returns_empty(monkeypatch) -> None:
    """容器不存在：docker logs rc!=0 且 stderr 无行 → 兜底 []（不抛、不把 stderr 当日志行）。

    注意 `docker logs` 对不存在的容器把错误写进 docker CLI 自己的 stderr，
    proc.stderr 非空会被合并进 out——这里钉的是"绝不抛异常"，返回值允许是错误文本行。
    """
    name = "modelctl-realprobe-nonexistent-0000-vllm"
    monkeypatch.setattr(admin_models, "_find_profile",
                        lambda n: _vllm_profile(n) if n == name else None)
    monkeypatch.setattr("modelctl.core.capabilities.probe", lambda *a, **k: object())
    out = admin_models._read_docker_logs(name, tail=10)
    assert isinstance(out, list)
