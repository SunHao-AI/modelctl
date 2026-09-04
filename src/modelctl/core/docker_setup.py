#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/docker_setup.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : Docker 运行环境诊断与安装
# ===============================================================================

"""core/docker_setup.py — Docker + NVIDIA Container Toolkit 诊断与安装。

docker_image 型引擎（vllm / tokenspeed / tensorrt_llm）依赖宿主机 docker +
nvidia-container-toolkit。本模块统一三件事：

- path_level_missing()：适配器 check_requirements 用的轻量 PATH 检查（无子进程），
  缺失项文案含统一指引 `modelctl env setup docker`；
- diagnose()：完整诊断（CLI / daemon / toolkit / runtime 配置），供 CLI 展示；
- render_instructions() / run_install()：可直接复制的安装脚本（默认清华 docker-ce
  镜像源，官方源 TLS 握手在国内常被干扰）与 --run 实际执行。

安全边界：安装动作只在 `modelctl env setup docker --run` 显式触发时执行，
且要求 Linux + root；诊断与指引在任何平台只读不写。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

#: env 子命令族的系统级目标名（区别于托管 venv 目标）
TARGET = "docker"

# 安装源：docker-ce 默认清华镜像（download.docker.com 国内 TLS 握手常失败）；
# nvidia 官方仓库实测可达。
DOCKER_APT_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/ubuntu"
DOCKER_APT_OFFICIAL = "https://download.docker.com/linux/ubuntu"
NVIDIA_TOOLKIT_BASE = "https://nvidia.github.io/libnvidia-container"
NVIDIA_TOOLKIT_REPO = f"{NVIDIA_TOOLKIT_BASE}/stable/deb"

DOCKER_LIST = "/etc/apt/sources.list.d/docker.list"
DOCKER_KEYRING = "/etc/apt/keyrings/docker.gpg"
NVIDIA_LIST = "/etc/apt/sources.list.d/nvidia-container-toolkit.list"
NVIDIA_KEYRING = "/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg"
DAEMON_JSON = Path("/etc/docker/daemon.json")

DOCKER_PKGS = "docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"

#: Docker Hub 拉取加速的内置默认源（registry-mirrors，按推荐顺序多源容灾）。
#: 2026-09 多来源交叉确认可用；清华 TUNA / 中科大 / 网易的 Docker Hub 加速已停服，
#: 勿再使用。用户显式传 --registry-mirror 时以用户列表为准（不再追加默认）。
DEFAULT_REGISTRY_MIRRORS: tuple[str, ...] = (
    "https://docker.1ms.run",
    "https://docker.xuanyuan.me",
    "https://docker.m.daocloud.io",
)


def resolve_registry_mirrors(user_mirrors: list[str] | None) -> list[str]:
    """用户显式指定 → 原样（去空去重保序）；未指定 → 内置默认列表。"""
    if user_mirrors:
        seen: list[str] = []
        for m in user_mirrors:
            m = (m or "").strip().rstrip("/")
            if m and m not in seen:
                seen.append(m)
        if seen:
            return seen
    return list(DEFAULT_REGISTRY_MIRRORS)

#: 合并 registry-mirrors 的占位指令（run_install 内走 Python 合并，避免覆盖 nvidia runtime）
MERGE_MIRROR_CMD = "_modelctl_merge_daemon_mirror"

# 统一缺失文案（适配器 RequirementError 与诊断输出共用；测试锚定其中的关键子串）
MSG_DOCKER_MISSING = "docker 命令不在 PATH"
MSG_TOOLKIT_MISSING = "nvidia-smi 不在 PATH / nvidia-container-toolkit 未就绪"
MSG_GUIDE = "执行 `modelctl env setup docker` 查看安装指引（部署机上加 `--run` 可自动安装）"


def path_level_missing() -> list[str]:
    """轻量 PATH 检查（供 check_requirements，绝不落子进程）。"""
    missing: list[str] = []
    if shutil.which("docker") is None:
        missing.append(MSG_DOCKER_MISSING)
    if shutil.which("nvidia-smi") is None:
        missing.append(MSG_TOOLKIT_MISSING)
    return missing


@dataclass(frozen=True)
class Check:
    """单项诊断结果。"""

    key: str
    label: str
    ok: bool
    detail: str


def _docker_cli_ok() -> bool:
    return shutil.which("docker") is not None


def _daemon_running() -> bool:
    if not _docker_cli_ok():
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _toolkit_installed() -> bool:
    return shutil.which("nvidia-ctk") is not None or shutil.which("nvidia-container-runtime") is not None


def _runtime_configured() -> bool:
    """/etc/docker/daemon.json 是否已注册 nvidia runtime（nvidia-ctk configure 的产物）。"""
    try:
        data = json.loads(DAEMON_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return "nvidia" in (data.get("runtimes") or {})


def diagnose() -> list[Check]:
    """完整诊断（含子进程探测；仅 CLI 展示用，别放进 check_requirements）。"""
    cli_ok = _docker_cli_ok()
    checks = [
        Check("docker_cli", "docker CLI（PATH）", cli_ok,
              "" if cli_ok else f"{MSG_DOCKER_MISSING}；未安装 docker-ce"),
    ]
    daemon_ok = cli_ok and _daemon_running()
    checks.append(Check(
        "docker_daemon", "docker daemon 运行中", daemon_ok,
        "" if daemon_ok or not cli_ok else "daemon 未运行（systemctl start docker）",
    ))
    toolkit_ok = _toolkit_installed()
    checks.append(Check(
        "nvidia_toolkit", "nvidia-container-toolkit", toolkit_ok,
        "" if toolkit_ok else "nvidia-ctk / nvidia-container-runtime 均未安装",
    ))
    runtime_ok = _runtime_configured()
    checks.append(Check(
        "nvidia_runtime", "daemon.json 已注册 nvidia runtime", runtime_ok,
        "" if runtime_ok else "执行 `nvidia-ctk runtime configure --runtime=docker` 后重启 docker",
    ))
    return checks


def install_steps(registry_mirrors: list[str] | None = None) -> list[tuple[str, str]]:
    """返回 [(步骤说明, 单行 shell 命令)]；指引与 --run 共用同一事实来源。

    registry_mirrors 为 None 时用内置默认列表（DEFAULT_REGISTRY_MIRRORS）。
    """
    mirrors = resolve_registry_mirrors(registry_mirrors)
    steps: list[tuple[str, str]] = [
        ("安装 apt 前置工具",
         "apt-get update -qq && apt-get install -y -qq ca-certificates curl gnupg"),
        (f"导入 docker-ce 签名密钥（清华镜像；官方源可换 {DOCKER_APT_OFFICIAL}）",
         f"install -m 0755 -d /etc/apt/keyrings && "
         f"curl -fsSL {DOCKER_APT_MIRROR}/gpg | gpg --dearmor -o {DOCKER_KEYRING}"),
        ("写入 docker-ce apt 源",
         f'echo "deb [arch=$(dpkg --print-architecture) signed-by={DOCKER_KEYRING}] '
         f'{DOCKER_APT_MIRROR} $(. /etc/os-release && echo $VERSION_CODENAME) stable" '
         f"> {DOCKER_LIST}"),
        ("安装 Docker CE",
         f"apt-get update && apt-get install -y {DOCKER_PKGS}"),
        ("导入 NVIDIA Container Toolkit 密钥与源",
         f"curl -fsSL {NVIDIA_TOOLKIT_BASE}/gpgkey | gpg --dearmor -o {NVIDIA_KEYRING} && "
         f"curl -s -L {NVIDIA_TOOLKIT_REPO}/nvidia-container-toolkit.list "
         f"| sed 's#deb https://#deb [signed-by={NVIDIA_KEYRING}] https://#g' > {NVIDIA_LIST} && "
         "apt-get update"),
        ("安装 nvidia-container-toolkit",
         "apt-get install -y nvidia-container-toolkit"),
        ("注册 nvidia runtime（合并写 /etc/docker/daemon.json）",
         "nvidia-ctk runtime configure --runtime=docker"),
    ]
    steps.append((f"daemon.json 合并 registry-mirrors（多源容灾）: {', '.join(mirrors)}",
                  MERGE_MIRROR_CMD))
    steps.append(("启动 docker 并验证 runtime 已注册",
                  "systemctl enable --now docker && systemctl restart docker && "
                  "docker info --format '{{.Runtimes}}'"))
    return steps


def render_instructions(registry_mirrors: list[str] | None = None) -> str:
    """渲染可直接复制到 root shell 的安装脚本（含整体说明）。"""
    lines = [
        "# Docker + NVIDIA Container Toolkit 安装脚本（Ubuntu 20.04/22.04/24.04，需 root）",
        "# 说明：download.docker.com 国内 TLS 握手常失败，这里默认清华 docker-ce 镜像；",
        "#       nvidia 官方仓库实测可达。Docker Hub 大镜像（如 vllm/vllm-openai 约 21.8GB）",
        "#       拉取加速由 registry-mirrors 提供：默认写入内置多源（2026-09 实测可用，",
        "#       docker.1ms.run / docker.xuanyuan.me / docker.m.daocloud.io），",
        "#       可 --registry-mirror <URL>（可重复）显式覆盖。清华 TUNA 的 Hub 加速已停服。",
        "# 自动执行：modelctl env setup docker --run",
    ]
    for desc, cmd in install_steps(registry_mirrors):
        lines.append(f"# {desc}")
        if cmd == MERGE_MIRROR_CMD:
            lines.append("# （由 modelctl 以 Python 合并写入，避免覆盖 nvidia runtime 配置）")
        else:
            lines.append(cmd)
    return "\n".join(lines)


def _merge_daemon_json(registry_mirrors: list[str]) -> bool:
    """把 registry_mirrors 合并进 daemon.json 的 registry-mirrors（保序去重）。

    返回是否有写入。已有其它键（如 nvidia-ctk 写的 runtimes）与已有 mirror 原样保留。
    """
    data: dict = {}
    if DAEMON_JSON.is_file():
        try:
            data = json.loads(DAEMON_JSON.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            logger.warning(f"{DAEMON_JSON} 不是合法 JSON，跳过 registry-mirrors 合并（请手动配置）")
            return False
    mirrors = list(data.get("registry-mirrors") or [])
    changed = False
    for mirror in registry_mirrors:
        if mirror not in mirrors:
            mirrors.append(mirror)
            changed = True
    if not changed:
        return False
    data["registry-mirrors"] = mirrors
    DAEMON_JSON.parent.mkdir(parents=True, exist_ok=True)
    DAEMON_JSON.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def run_install(registry_mirrors: list[str] | None = None) -> int:
    """实际执行安装（仅 Linux + root）。返回退出码。"""
    if not sys.platform.startswith("linux"):
        logger.error(f"Docker 自动安装仅支持 Linux 部署机（apt 体系），当前平台 {sys.platform!r} 请参考指引手动安装")
        return 2
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        logger.error("Docker 自动安装需要 root 权限（apt / systemd / /etc/docker），请用 root 执行")
        return 2
    mirrors = resolve_registry_mirrors(registry_mirrors)
    for desc, cmd in install_steps(mirrors):
        if cmd == MERGE_MIRROR_CMD:
            if _merge_daemon_json(mirrors):
                logger.info(f"registry-mirrors 已合并写入 {DAEMON_JSON}：{', '.join(mirrors)}")
            continue
        logger.info(f"[docker setup] {desc}")
        rc = subprocess.run(["bash", "-c", cmd]).returncode
        if rc != 0:
            logger.error(f"步骤失败（退出码 {rc}）：{desc}\n命令：{cmd}")
            return rc
    logger.info("Docker 环境安装完成；可执行 "
                "`docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` 验证 GPU 透传")
    return 0
