#!/usr/bin/env python3
# ===============================================================================
# @File   : src/modelctl/core/webui/frontend.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 14:20
# @Desc   : Web UI 前端环境自举（Node/依赖/构建产物）
# ===============================================================================

"""core/webui/frontend.py — 前端环境自举：缺 Node 装 Node，缺依赖装依赖，缺产物就构建。

`modelctl webui start` 只需要一个可用浏览器入口；服务器上没有 npm / 没装前端依赖 /
没构建过 dist/ 都不该让用户先去做一轮手工环境准备。这里把三件事收敛成一个
`ensure_frontend()`：

1. **产物检查**：项目根 `dist/index.html` 存在 → 直接返回，零成本。
2. **Node.js**：`npm` 不在 PATH 时自动安装（Linux 走系统包管理器 + NodeSource，
   Debian/Ubuntu 用 NodeSource 是因为发行版自带 node 常低于 vite 6 要求的 18+）。
3. **依赖与构建**：`web/node_modules` 缺失则 `npm install`，随后 `npm run build`
   （产物落到项目根 `dist/`，与 `server.dist_dir()` 一致）。

自动安装属于"改动机器状态"的长耗时操作，因此只在**交互终端**默认开启；
`ssh host 'modelctl webui start'` 这类非交互调用不会卡住去装 Node，而是回一条
可直接复制的手动命令。CLI 用 `--build` / `--no-build` 强制覆盖该判断。

安装 Node 需要 root 或 sudo；都不具备时降级为指引，不抛异常。npm 源在用户未自配
`.npmrc` 时默认走 npmmirror，取向与项目 uv 侧的阿里云 PyPI 镜像一致。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from loguru import logger

from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.webui.server import dist_dir, dist_ready

# vite 6 / vue-tsc 2 要求 Node >= 18；低于此版本的发行版包不予采用
NODE_MIN_MAJOR = 18
# 自动安装使用的 Node 主版本（NodeSource 通道）
NODE_MAJOR = "22"
# 用户未自配 .npmrc 时使用的镜像源（与 gateway/uv 的阿里 PyPI 镜像取向一致）
NPM_MIRROR = "https://registry.npmmirror.com"
# 单步 npm/包管理器操作的兜底超时（秒）：install 走镜像后一般远小于此值
STEP_TIMEOUT = 1800


def web_root() -> Path:
    """前端源码目录（含 package.json / vite.config.ts）。"""
    # frontend.py 位于 src/modelctl/core/webui/，项目根 = parents[4]
    return PROJECT_ROOT / "web"


def deps_installed(root: Path | None = None) -> bool:
    """前端依赖是否已安装（node_modules 存在即视为已装，不校验完整性）。"""
    return (root or web_root()).joinpath("node_modules").is_dir()


def find_npm() -> str | None:
    """定位 npm 可执行文件；未安装返回 None。"""
    return shutil.which("npm")


def _is_windows() -> bool:
    return os.name == "nt"


def node_major_version() -> int | None:
    """当前 node 主版本号；解析失败（未安装/输出异常）返回 None。"""
    npm = find_npm()
    if npm is None:
        return None
    try:
        out = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = (out or "").strip().lstrip("v")
    head = token.split(".", 1)[0]
    return int(head) if head.isdigit() else None


def interactive() -> bool:
    """是否交互终端：只有交互场景才默认执行自动安装/构建（避免卡住脚本与 CI）。"""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (ValueError, OSError):
        return False


def _run(argv: list[str], cwd: Path | None = None, timeout: int = STEP_TIMEOUT) -> int:
    """执行子进程并把输出透传到终端（安装/构建过程需要让用户看到进度）。

    Windows 上 npm 实际是 npm.cmd，CreateProcess 无法直接执行批处理，需 shell 模式。
    """
    shell = _is_windows()
    cmd: list[str] | str = " ".join(subprocess.list2cmdline([str(a) for a in argv])) if shell else argv
    try:
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None, shell=shell, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        logger.error(f"命令超时（>{timeout}s）：{argv[0]}")
        return 124
    except OSError as error:
        logger.error(f"命令执行失败：{argv}（{error}）")
        return 127


def _npm_argv(args: list[str]) -> list[str]:
    """npm 调用参数（用绝对路径，避免刚装完 node 但本进程 PATH 未刷新）。"""
    return [find_npm() or "npm", *args]


def _sudo_prefix() -> list[str] | None:
    """提权前缀：root 返回 []，可用 sudo 返回 ['sudo']，都不满足返回 None。"""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    if shutil.which("sudo"):
        return ["sudo"]
    return None


def _package_manager() -> str | None:
    """探测系统包管理器标识（apt / dnf / yum / zypper / pacman / apk）。"""
    for name, tag in (
        ("apt-get", "apt"),
        ("dnf", "dnf"),
        ("yum", "yum"),
        ("zypper", "zypper"),
        ("pacman", "pacman"),
        ("apk", "apk"),
    ):
        if shutil.which(name):
            return tag
    return None


def install_node() -> bool:
    """自动安装 Node.js（仅 Linux；需要 root 或免密 sudo）。

    Debian/Ubuntu 走 NodeSource 仓库——apt 源里的 nodejs 版本普遍偏旧（如 Ubuntu 20.04
    是 12.x），不满足 vite 6 要求；其余发行版先装自带 nodejs + npm，版本不达标再走
    NodeSource。成功与否以"安装后 npm 可用且 node >= NODE_MIN_MAJOR"为准。
    """
    if platform.system() != "Linux":
        logger.error(f"{platform.system()} 下不自动安装 Node.js，请手动安装：{manual_hint()}")
        return False
    sudo = _sudo_prefix()
    if sudo is None:
        logger.error(f"安装 Node.js 需要 root 或 sudo 权限：{manual_hint()}")
        return False
    pm = _package_manager()
    if pm is None:
        logger.error(f"未识别的系统包管理器，无法自动安装 Node.js：{manual_hint()}")
        return False

    node_major = f"{NODE_MAJOR}.x"
    logger.info(f"未检测到可用的 Node.js，开始通过 {pm} 自动安装 Node {NODE_MAJOR} ...")
    _run(sudo + _pm_update_args(pm))
    _run(sudo + _pm_install_args(pm, ["curl", "ca-certificates"]))

    # 主路径：NodeSource 仓库（发行版自带 nodejs 版本常低于 vite 6 要求）
    if pm in ("apt", "dnf", "yum", "zypper") and shutil.which("curl"):
        setup = f"https://deb.nodesource.com/setup_{node_major}" if pm == "apt" else f"https://rpm.nodesource.com/setup_{node_major}"
        if _run_node_source(sudo, setup) == 0 and _run(sudo + _pm_install_args(pm, ["nodejs"])) == 0 and _node_usable():
            return _node_installed(pm)

    # 回退路径：发行版自带 nodejs + npm（pacman / apk 走的就是这条）
    logger.info("尝试通过系统源安装 nodejs / npm")
    _run(sudo + _pm_update_args(pm))
    if _run(sudo + _pm_install_args(pm, ["nodejs", "npm"])) == 0 and _node_usable():
        return _node_installed(pm)
    logger.error(f"Node.js 自动安装失败：{manual_hint()}")
    return False


def _node_installed(pm: str) -> bool:
    """安装成功后的统一出口：打印版本并返回 True（调用方已校验可用性）。"""
    logger.info(f"Node.js 安装完成（node {node_major_version()}，源：{pm}）")
    return True


def _node_usable() -> bool:
    """安装校验：npm 可定位且 node 主版本达标。"""
    major = node_major_version()
    return find_npm() is not None and major is not None and major >= NODE_MIN_MAJOR


def _pm_update_args(pm: str) -> list[str]:
    return {
        "apt": ["apt-get", "update", "-y"],
        "dnf": ["dnf", "makecache", "-y"],
        "yum": ["yum", "makecache", "-y"],
        "zypper": ["zypper", "--non-interactive", "refresh"],
        "pacman": ["pacman", "-Sy", "--noconfirm"],
        "apk": ["apk", "update"],
    }[pm]


def _pm_install_args(pm: str, packages: list[str]) -> list[str]:
    return {
        "apt": ["apt-get", "install", "-y", *packages],
        "dnf": ["dnf", "install", "-y", *packages],
        "yum": ["yum", "install", "-y", *packages],
        "zypper": ["zypper", "--non-interactive", "install", *packages],
        "pacman": ["pacman", "-S", "--noconfirm", *packages],
        "apk": ["apk", "add", "--no-cache", *packages],
    }[pm]


def _run_node_source(sudo: list[str], url: str) -> int:
    """执行 NodeSource 安装脚本：curl <url> | <sudo> bash -（root 时需 -E 保留代理变量）。"""
    tail = "bash -" if not sudo else "sudo -E bash -"
    cmd = f"curl -fsSL {url} | {tail}"
    try:
        return subprocess.run(cmd, shell=True, timeout=STEP_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        logger.warning("NodeSource 源配置超时")
        return 124


def _registry_args() -> list[str]:
    """用户未自配 registry 时附加镜像源（不覆盖已有 .npmrc 的配置）。"""
    for scope in (web_root() / ".npmrc", Path.home() / ".npmrc"):
        try:
            if scope.is_file() and "registry" in scope.read_text(encoding="utf-8"):
                return []
        except OSError:
            continue
    return ["--registry", NPM_MIRROR]


def install_deps(root: Path | None = None) -> bool:
    """安装前端依赖（`npm install`，产物在 web/node_modules）。"""
    target = root or web_root()
    if not target.joinpath("package.json").is_file():
        logger.error(f"未找到前端工程：{target / 'package.json'}")
        return False
    logger.info(f"正在安装前端依赖（npm install，源：{NPM_MIRROR if _registry_args() else '用户 .npmrc'}）...")
    rc = _run(_npm_argv(["install", "--no-audit", "--no-fund", *_registry_args()]), cwd=target)
    if rc != 0:
        logger.error(f"npm install 失败（退出码 {rc}），可手动执行：cd {target} && npm install")
        return False
    return deps_installed(target)


def build(root: Path | None = None) -> bool:
    """构建前端产物（`npm run build` → vue-tsc 类型检查 + vite build，输出到项目根 dist/）。"""
    target = root or web_root()
    logger.info("正在构建前端产物（npm run build，首次约需 1-3 分钟）...")
    rc = _run(_npm_argv(["run", "build"]), cwd=target)
    if rc != 0:
        logger.error(f"npm run build 失败（退出码 {rc}），可手动执行：cd {target} && npm run build 查看完整报错")
        return False
    if not dist_ready():
        logger.error(f"构建命令已退出但产物缺失：{dist_dir() / 'index.html'}")
        return False
    logger.info(f"前端产物已生成：{dist_dir()}")
    return True


def manual_hint() -> str:
    """自动处理不可用时的手动兜底指引（非交互 / 无权限 / 非 Linux 场景）。"""
    return (
        "请手动准备前端环境后重试：\n"
        f"  1) 安装 Node.js >= {NODE_MIN_MAJOR}（如 curl -fsSL https://deb.nodesource.com/setup_{NODE_MAJOR}.x | sudo -E bash - && sudo apt-get install -y nodejs）\n"
        f"  2) cd {web_root()} && npm install\n"
        f"  3) npm run build（产物输出到 {dist_dir()}）\n"
        "  或先用 modelctl webui start --no-build 只启管理 API（浏览器访问根路径会 404）"
    )


def ensure_frontend(auto: bool | None = None) -> tuple[bool, str]:
    """确保前端产物可用；返回 (是否可用, 说明文本)。

    - auto=None：交互终端自动装/建，非交互只检测并给出手动指引
    - auto=True：强制执行（CI / 脚本内明确要构建）
    - auto=False：只检测，缺失即返回指引
    """
    if dist_ready():
        return True, "前端产物已就绪"
    if not web_root().joinpath("package.json").is_file():
        return False, f"前端源码缺失（{web_root() / 'package.json'} 不存在），请确认仓库完整后重试"

    do = interactive() if auto is None else auto
    why = "非交互终端" if auto is None and not interactive() else "已指定 --no-build"
    # 每步都重新探测：安装 Node 后 PATH / 版本才可见，快照会漏掉刚装好的状态
    if not _node_usable():
        if not do:
            return False, f"缺少 Node.js >= {NODE_MIN_MAJOR}（{why}，未自动安装）。\n{manual_hint()}"
        install_node()
        if not _node_usable():
            return False, f"缺少可用的 Node.js >= {NODE_MIN_MAJOR}。\n{manual_hint()}"
    if not deps_installed():
        if not do:
            return False, f"前端依赖未安装（{why}，未自动安装）。\n{manual_hint()}"
        if not install_deps():
            return False, f"前端依赖自动安装失败。\n{manual_hint()}"
    if not do:
        return False, f"前端产物 dist/ 未构建（{why}，未自动构建）。\n{manual_hint()}"
    if not build():
        return False, f"前端自动构建失败。\n{manual_hint()}"
    return True, "前端环境已自动就绪（Node + 依赖 + dist/）"
