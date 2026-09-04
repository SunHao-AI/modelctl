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
import time
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

#: 已停服的 Docker Hub 加速域名（子串匹配 mirror URL）。
#: daemon.json 里残留这些域名会让 `docker run` 解析 reference 时 DNS 硬失败，
#: 不回落 registry-1.docker.io，容器根本不启动。合并时主动剔除。
#: 注意别写成裸 "tuna" / "ustc"：TUNA 的 docker-ce **apt** 镜像
#: （mirrors.tuna.tsinghua.edu.cn/docker-ce）仍可用，同前缀 ≠ 同服务。
#: 2026-09-04 本机 curl 实测：标 (000) 的域名 TCP/TLS 均不通，标 (4xx) 的
#: 站点在运营但已不代理 Docker Hub（`/v2/<ns>/<repo>/manifests` 拿不到）。
DEAD_REGISTRY_MIRRORS: tuple[str, ...] = (
    "docker.mirrors.tuna.tsinghua.edu.cn",  # 停服，NXDOMAIN
    "docker.mirrors.ustc.edu.cn",           # 停服，转校内
    "hub-mirror.c.163.com",                 # 网易，停服
    "mirror.baidubce.com",                  # 百度云，停服
    "registry.docker-cn.com",               # Docker 官方中国镜像，早已关闭（000）
    "dockerproxy.com",                      # 关站（000，勿与 dockerproxy.net 混淆）
    "docker.nju.edu.cn",                    # 南大 Docker Hub 加速已下架（/v2/ 403）
    "docker.mirrors.sjtug.sjtu.edu.cn",     # 上交 2024-06-06 因监管下架
    "reg-mirror.qiniu.com",                 # 七牛，停服
)


def is_dead_mirror(url: str) -> bool:
    """mirror URL 是否命中已停服域名。"""
    return any(d in (url or "") for d in DEAD_REGISTRY_MIRRORS)


def split_dead_mirrors(urls: list[str]) -> tuple[list[str], list[str]]:
    """拆成 (存活 mirror, 停服 mirror) 两组，各自保序。"""
    alive = [u for u in urls if not is_dead_mirror(u)]
    dead = [u for u in urls if is_dead_mirror(u)]
    return alive, dead


def resolve_registry_mirrors(user_mirrors: list[str] | None) -> list[str]:
    """用户显式指定 → 原样（去空去重保序、剔除停服源）；未指定 → 内置默认列表。"""
    if user_mirrors:
        seen: list[str] = []
        for m in user_mirrors:
            m = (m or "").strip().rstrip("/")
            if m and m not in seen:
                seen.append(m)
        alive, dead = split_dead_mirrors(seen)
        for m in dead:
            logger.warning(f"registry-mirror 已停服，忽略：{m}")
        if alive:
            return alive
    return list(DEFAULT_REGISTRY_MIRRORS)

#: 合并 daemon.json（registry-mirrors + 拉取并发）的占位指令。
#: run_install 内走 Python 合并，避免整文件覆盖掉 nvidia runtime 等既有键。
MERGE_DAEMON_JSON_CMD = "_modelctl_merge_daemon_json"

#: 大 layer 并发下载数（daemon.json `max-concurrent-downloads`，Docker 出厂默认 3）。
#: 21.8GB 级镜像跨境拉取时，3 路 GB 级 layer 并发互抢同一条跨境链路，会拉长每一层的
#: 传输耗时，从而提高被中间设备按空闲/时长掐断（`short read … unexpected EOF`）的概率。
#: 降到 2 让每层拿到更多带宽、更快传完，实测能显著减少中断次数（配合 ensure_image
#: 的 layer 复用重试即可拉完）。0 = 不改动既有配置。
#: 注意这是 **daemon 级**配置，不是 `docker pull` 的参数（pull 无此选项）。
DEFAULT_MAX_CONCURRENT_DOWNLOADS = 2

# ---- 大镜像拉取重试 ----
#
# 21.8GB 级镜像（vllm/vllm-openai Day-0）跨境拉取几乎必然被中途掐断，典型报错：
#   short read: expected 35254 bytes but got 0: unexpected EOF
# 关键在于 **Docker 复用已下载完成的 layer**：每次重试都从上次断点继续推进
# （实测第二次 pull 能直接复用上一轮的十几个 "Download complete"），
# 因此 pull 是可重入的单调过程，重试而不是换源才是正解。
PULL_ATTEMPTS = 5
PULL_RETRY_WAIT = 5

#: 传输中断类（可重试）：多为跨境链路抖动 / 反代超时，与 mirror 是否"活着"无关
TRANSIENT_MARKERS = (
    "short read", "unexpected eof", "eof", "i/o timeout",
    "tls handshake", "connection reset", "connection refused",
)
#: mirror 域名解析失败（硬失败，重试无意义，必须换源）
DEAD_MIRROR_MARKERS = ("no such host", "dial tcp: lookup", "server misbehaving")
#: 仓库/标签不存在（换源或走反代显式拉取，重试无意义）
MISSING_TAG_MARKERS = ("manifest unknown", "manifest invalid", "not found: manifest")


def classify_pull_error(text: str) -> str:
    """把 docker pull 的失败输出归类，决定"值得重试"还是"必须改配置"。"""
    t = (text or "").lower()
    if any(m in t for m in DEAD_MIRROR_MARKERS):
        return "dead-mirror"
    if any(m in t for m in MISSING_TAG_MARKERS):
        return "missing-tag"
    if any(m in t for m in TRANSIENT_MARKERS):
        return "transient"
    return "unknown"


def image_present(image: str) -> bool:
    """本地是否已有该镜像（不联网，`docker image inspect` 纯查本地元数据）。"""
    try:
        rc = subprocess.run(["docker", "image", "inspect", image],
                            capture_output=True, timeout=30).returncode
    except (OSError, subprocess.SubprocessError):
        return False
    return rc == 0


def ensure_image(image: str, attempts: int | None = None) -> bool:
    """确保 docker_image 就位：本地已有即复用，否则拉取并按错误类型决定是否重试。

    显式 pull 还有个附带好处：失败原因直接进 modelctl 日志。走 `docker run` 隐式
    pull 时，同样的报错会被 launch 日志的截断规则吃掉，只剩健康检查超时的
    `Connection refused`，迫使人去反推。
    """
    if image_present(image):
        logger.info(f"镜像已就位，跳过拉取：{image}")
        return True
    attempts = attempts or PULL_ATTEMPTS
    tail = ""
    for i in range(1, attempts + 1):
        logger.info(f"拉取镜像（第 {i}/{attempts} 次）：{image}")
        try:
            proc = subprocess.run(["docker", "pull", image], capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error(f"docker pull 无法执行：{exc}")
            return False
        if proc.returncode == 0:
            logger.info(f"镜像拉取完成：{image}")
            return True
        err = (proc.stderr or proc.stdout or "").strip()
        tail = err.splitlines()[-1] if err else ""
        kind = classify_pull_error(err)
        if kind == "dead-mirror":
            logger.error(f"registry-mirror 域名解析失败，重试无意义：{tail}")
            logger.error(f"执行 `modelctl env setup docker --run` 清理停服源（{DAEMON_JSON}）")
            return False
        if kind == "missing-tag":
            logger.error(f"mirror 上没有该镜像的 manifest，重试无意义：{tail}")
            logger.error(f"改走反代显式拉取再回打 tag：docker pull <mirror>/{image}")
            return False
        if i < attempts:
            logger.warning(f"拉取中断（{kind}），已完成的 layer 会被复用，"
                           f"{PULL_RETRY_WAIT}s 后重试：{tail}")
            time.sleep(PULL_RETRY_WAIT)
    logger.error(f"镜像拉取连续 {attempts} 次失败，最后一条错误：{tail}")
    return False

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


def install_steps(
    registry_mirrors: list[str] | None = None,
    max_downloads: int | None = None,
) -> list[tuple[str, str]]:
    """返回 [(步骤说明, 单行 shell 命令)]；指引与 --run 共用同一事实来源。

    registry_mirrors 为 None 时用内置默认列表（DEFAULT_REGISTRY_MIRRORS）。
    max_downloads 为 None 时用 DEFAULT_MAX_CONCURRENT_DOWNLOADS，传 0 表示不动该键。
    """
    mirrors = resolve_registry_mirrors(registry_mirrors)
    limit = DEFAULT_MAX_CONCURRENT_DOWNLOADS if max_downloads is None else max_downloads
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
    desc = f"daemon.json 合并 registry-mirrors（多源容灾）: {', '.join(mirrors)}"
    if limit > 0:
        desc += f"，并设 max-concurrent-downloads={limit}（降低大 layer 并发互抢导致的 EOF）"
    steps.append((desc, MERGE_DAEMON_JSON_CMD))
    steps.append(("启动 docker 并验证 runtime 已注册",
                  "systemctl enable --now docker && systemctl restart docker && "
                  "docker info --format '{{.Runtimes}}'"))
    return steps


def render_instructions(
    registry_mirrors: list[str] | None = None,
    max_downloads: int | None = None,
) -> str:
    """渲染可直接复制到 root shell 的安装脚本（含整体说明）。"""
    limit = DEFAULT_MAX_CONCURRENT_DOWNLOADS if max_downloads is None else max_downloads
    lines = [
        "# Docker + NVIDIA Container Toolkit 安装脚本（Ubuntu 20.04/22.04/24.04，需 root）",
        "# 说明：download.docker.com 国内 TLS 握手常失败，这里默认清华 docker-ce 镜像；",
        "#       nvidia 官方仓库实测可达。Docker Hub 大镜像（如 vllm/vllm-openai 约 21.8GB）",
        "#       拉取加速由 registry-mirrors 提供：默认写入内置多源（2026-09 实测可用，",
        "#       docker.1ms.run / docker.xuanyuan.me / docker.m.daocloud.io），",
        "#       可 --registry-mirror <URL>（可重复）显式覆盖。清华 TUNA 的 Hub 加速已停服。",
        "# 对已装好的机器重跑 --run 同样有效：既有 daemon.json 里的停服 Hub 加速域名会被",
        "# 自动剔除（残留坏源会让 docker run 直接 DNS 硬失败，容器根本不起）。",
        f"# max-concurrent-downloads={limit}：daemon 级并发下载数（出厂默认 3）。GB 级 layer",
        "#       三路并发互抢跨境链路会拉长单层耗时、放大 `short read … unexpected EOF`，",
        "#       调小让每层更快传完；--max-concurrent-downloads 0 表示保留机器现值。",
        "# 自动执行：modelctl env setup docker --run",
    ]
    for desc, cmd in install_steps(registry_mirrors, limit):
        lines.append(f"# {desc}")
        if cmd == MERGE_DAEMON_JSON_CMD:
            lines.append("# （由 modelctl 以 Python 合并写入，避免覆盖 nvidia runtime 配置）")
        else:
            lines.append(cmd)
    return "\n".join(lines)


def _merge_daemon_json(registry_mirrors: list[str], max_downloads: int = 0) -> bool:
    """合并 daemon.json：registry-mirrors（剔停服源 + 保序去重）与 max-concurrent-downloads。

    返回是否有写入。已有其它键（如 nvidia-ctk 写的 runtimes）与已有存活 mirror 原样保留；
    命中 DEAD_REGISTRY_MIRRORS 的源一律删除 —— 只追加不清理会让坏域名永久残留，
    `--run` 无法把环境收敛到可用状态。max_downloads <= 0 表示不改并发数。
    """
    data: dict = {}
    if DAEMON_JSON.is_file():
        try:
            data = json.loads(DAEMON_JSON.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            logger.warning(f"{DAEMON_JSON} 不是合法 JSON，跳过合并（请手动配置）")
            return False
    existing, dead = split_dead_mirrors(list(data.get("registry-mirrors") or []))
    if dead:
        logger.warning(f"{DAEMON_JSON} 存在已停服的 registry-mirrors，已剔除：{', '.join(dead)}")
    mirrors = existing
    changed = bool(dead)
    for mirror in registry_mirrors:
        if is_dead_mirror(mirror):
            logger.warning(f"忽略已停服的 registry-mirror：{mirror}")
            continue
        if mirror not in mirrors:
            mirrors.append(mirror)
            changed = True
    if max_downloads > 0 and data.get("max-concurrent-downloads") != max_downloads:
        data["max-concurrent-downloads"] = max_downloads
        changed = True
    if not changed:
        return False
    data["registry-mirrors"] = mirrors
    DAEMON_JSON.parent.mkdir(parents=True, exist_ok=True)
    DAEMON_JSON.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def run_install(
    registry_mirrors: list[str] | None = None,
    max_downloads: int | None = None,
) -> int:
    """实际执行安装（仅 Linux + root）。返回退出码。"""
    if not sys.platform.startswith("linux"):
        logger.error(f"Docker 自动安装仅支持 Linux 部署机（apt 体系），当前平台 {sys.platform!r} 请参考指引手动安装")
        return 2
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        logger.error("Docker 自动安装需要 root 权限（apt / systemd / /etc/docker），请用 root 执行")
        return 2
    mirrors = resolve_registry_mirrors(registry_mirrors)
    limit = DEFAULT_MAX_CONCURRENT_DOWNLOADS if max_downloads is None else max_downloads
    for desc, cmd in install_steps(mirrors, limit):
        if cmd == MERGE_DAEMON_JSON_CMD:
            if _merge_daemon_json(mirrors, limit):
                logger.info(f"daemon.json 已合并写入 {DAEMON_JSON}："
                            f"registry-mirrors={', '.join(mirrors)}"
                            + (f"，max-concurrent-downloads={limit}" if limit > 0 else ""))
            continue
        logger.info(f"[docker setup] {desc}")
        rc = subprocess.run(["bash", "-c", cmd]).returncode
        if rc != 0:
            logger.error(f"步骤失败（退出码 {rc}）：{desc}\n命令：{cmd}")
            return rc
    logger.info("Docker 环境安装完成；可执行 "
                "`docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` 验证 GPU 透传")
    return 0
