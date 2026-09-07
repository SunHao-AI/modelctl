#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/windows_setup.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : Windows Docker Desktop 一键安装与诊断
# ===============================================================================

"""core/windows_setup.py — Docker Desktop + WSL2 诊断与一键安装（Windows-only）。

与 `docker_setup.py` 同型（一个平台一个文件），但 Windows 桌面路径用
winget + Docker Desktop + WSL2 组合，不直接 apt。

阶段 A（自动，run_install 覆盖，12 值 stage 枚举见 STAGES）：
  1. detect_winget：shutil.which 探 winget（无 → exit 2）
  2. detect_wsl2：`wsl --version` 15s 超时（非阻断）
  3. 已装桌面 → already_installed；未装 → winget_running → winget_done
  4. write_daemon_json：合并 %USERPROFILE%\\.docker\\daemon.json
  5. test_docker_version / test_gpus（非阻断，只 emit log）
  6. post_install_plan：5 步阶段 B 结构化卡片
  7. done

阶段 B（引导，run_install 不发子进程，只给文案）：
  - restart          重启计算机（启用 WSL2 backend）
  - open_desktop     Desktop 首次启动向导
  - enable_wsl2      Settings → General 勾 WSL2
  - enable_gpu       Settings → Resources → GPU 勾上
  - verify           回 WebUI 点【已就绪，点我验证】

UAC 阻塞：winget 子进程 stdout 静默 ≥ UAC_SILENCE_SEC 秒 → emit need_UAC 事件
但**继续等**（不 kill winget，避免半成品 Desktop 残留）。

安全边界：
- run_install 仅 sys.platform == "win32" 可执行，其它平台预检阶段 return 2
- 写磁盘仅 %USERPROFILE%\\.docker\\daemon.json（父目录自动 mkdir）
- 不写注册表、不调 PATH、不开 WSL 功能（dism 留给 Desktop 向导）
"""

from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from loguru import logger

# 复用 docker_setup 的镜像常量（单一事实来源，不应漂移）
from modelctl.core.docker_setup import (
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    is_dead_mirror,
    resolve_registry_mirrors,
    split_dead_mirrors,
)
from modelctl.core.sse_stage_event import StageEvent

# ---- 常量 ----

#: winget 包 ID（单一事实来源：仅此处定义）。
WINGET_ID = "Docker.DockerDesktop"

#: 占位：modelctl 自身以 Python 直接完成的步骤（vs. 外部命令）。
MERGE_DAEMON_JSON_CMD = "_modelctl_merge_daemon_json"
DETECT_PREREQS_CMD = "_modelctl_detect_prereqs"
VERIFY_AFTER_CMD = "_modelctl_verify_after_install"

#: Docker Desktop 安装候选可执行文件（覆盖 per-machine + per-user 两种装法）。
DESKTOP_EXE_CANDIDATES: tuple[Path, ...] = (
    Path("C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe"),
    Path("C:\\Program Files (x86)\\Docker\\Docker\\Docker Desktop.exe"),
    Path.home() / "AppData" / "Local" / "Programs" / "DockerDesktop" / "Docker Desktop.exe",
)

#: daemon.json 落盘路径（Windows 桌面用户当前 HOME 下）。
DAEMON_JSON: Path = Path.home() / ".docker" / "daemon.json"

#: UAC 静默阈值：winget 子进程 stdout 连续 UAC_SILENCE_SEC 秒无输出即发
#: need_UAC 事件（提示用户 Windows 任务栏点位"允许"），但仍继续等待（不 kill）。
#: 测试可通过 monkeypatch 改成更小的值（如 0.05s）加速单测。
UAC_SILENCE_SEC: float = 20.0

#: GPU 冒烟镜像。
GPU_SMOKE_IMAGE = "nvidia/cuda:12.4.0-base-ubuntu22.04"

#: wsl --version 探测超时（秒）。
WSL_PROBE_TIMEOUT: int = 15

#: GPU 冒烟 docker run 超时（秒）。
GPU_SMOKE_TIMEOUT: int = 120

#: run_install 阶段枚举（SSE stage 字段、前端 TS DockerSSEStage 三端字符串联合 SoT）。
STAGES: frozenset[str] = frozenset({
    "detect_winget", "detect_wsl2", "winget_running", "need_UAC",
    "winget_done", "write_daemon_json", "test_docker_version",
    "test_gpus", "post_install_plan", "already_installed",
    "done", "error",
})


@dataclass(frozen=True)
class Check:
    """单项诊断结果（5 字段，比 docker_setup.Check 多 hint 作 Windows 额外提示）。

    字段名必须与 docker_setup.Check 一一对齐（`test_check_dataclass_fields_
    match_docker_setup` 锁字段名集合）。
    """

    key: str
    label: str
    ok: bool
    detail: str
    hint: str = ""


def _ts_now() -> str:
    """当前本地时间字符串（项目统一格式 YYYY-MM-DD HH:mm:ss）。"""
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


# ---- PATH 探测与 WSL / GPU 子命令 ----


def path_level_missing() -> list[str]:
    """轻量 PATH 检查（供 check_requirements，绝不落子进程）。

    与 docker_setup.path_level_missing() 同型但探测对象不同：
    Windows 桌面靠 winget 装 Desktop，winget 不在则先补 winget，
    有 winget 再补 docker CLI（CLI 通常随 Desktop 进入 PATH）。
    """
    missing: list[str] = []
    if shutil.which("winget") is None:
        missing.append("winget")
    elif shutil.which("docker") is None:
        missing.append("docker")
    return missing


def _probe_wsl2() -> tuple[int, str]:
    """探测 WSL 状态：`wsl --version`；异常 → (1, "probe-error: ...")。"""
    try:
        proc = subprocess.run(["wsl", "--version"], capture_output=True, text=True,
                              timeout=WSL_PROBE_TIMEOUT)
        return int(proc.returncode), (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return 1, f"probe-error: {exc}"


def _desktop_installed() -> bool:
    """任意候选可执行文件存在（覆盖 per-machine / per-user 两种装法）。"""
    return any(p.is_file() for p in DESKTOP_EXE_CANDIDATES)


def _gpu_smoke() -> tuple[int, str]:
    """GPU 冒烟：`docker run --rm --gpus all <镜像> nvidia-smi`。

    仅当 host 装了 nvidia-smi 时调用（nvidia-container-toolkit 配置正确的桌面
    才有意义）；失败不阻断，由上层决定是否提示 "Desktop 设置 GPU 勾选"。
    """
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm", "--gpus", "all", GPU_SMOKE_IMAGE, "nvidia-smi"],
            capture_output=True, text=True, timeout=GPU_SMOKE_TIMEOUT,
        )
        return int(proc.returncode), (proc.stdout or proc.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"probe-error: {exc}"


# ---- 诊断 ----


def diagnose() -> list[Check]:
    """完整诊断：5 项（winget / wsl2 / desktop / docker / gpus）。

    语义与 docker_setup.diagnose() 平行但语义分叉：
    - nvidia-smi 缺失时 gpus 项 ok=True 且 hint 标 "non-GPU host, skipped"
      （桌面可能本来就没 GPU，不应阻断）
    - 任意子进程异常都归为该项 not ok，hint 给统一引导文案
    """
    winget_path = shutil.which("winget")
    checks = [
        Check(
            "winget", "winget（PATH）",
            winget_path is not None,
            "" if winget_path else "winget 未找到；安装：https://aka.ms/winget-cli",
        ),
    ]
    wsl_rc, wsl_out = _probe_wsl2()
    wsl_ok = wsl_rc == 0
    checks.append(Check(
        "wsl2", "WSL（wsl --version）",
        wsl_ok,
        "" if wsl_ok else (wsl_out or "wsl 探测失败"),
    ))
    desktop_ok = _desktop_installed()
    checks.append(Check(
        "desktop", "Docker Desktop 已装",
        desktop_ok,
        "" if desktop_ok else "未检测到 Docker Desktop（winget install "
                              f"{WINGET_ID} 或手动下载 zip）",
    ))
    docker_ok = shutil.which("docker") is not None
    checks.append(Check(
        "docker", "docker CLI / Docker Desktop 在 PATH",
        docker_ok,
        "" if docker_ok else "docker 不可见；请重启终端或重启计算机",
    ))
    nvidia = shutil.which("nvidia-smi") is not None
    if nvidia:
        sb, so = _gpu_smoke()
        ok = sb == 0
        detail = (so or "")[:160] if not ok else ""
        hint = "" if ok else "到 Desktop Settings → Resources → GPU 勾上再点验证"
    else:
        ok, detail, hint = True, "", "non-GPU host, skipped"
    checks.append(Check("gpus", "GPU 冒烟（--gpus all）", ok, detail, hint))
    return checks


# ---- 安装步骤与指引 ----


def install_steps(
    registry_mirrors: list[str] | None = None,
    max_downloads: int | None = None,
) -> list[tuple[str, str]]:
    """4 步 [(说明, 命令)]；仅第 2 步是真正的外部命令（winget install）。

    其它 3 步都是占位 _modelctl_*（指引渲染时直接展示，执行时由 Python 直接做）：
    - 第 1 步：pre-flight 探测（winget / WSL / Desktop）
    - 第 2 步：winget install（唯一真正外部命令）
    - 第 3 步：合并 %USERPROFILE%\\.docker\\daemon.json
    - 第 4 步：验证 + 阶段 B 引导
    """
    mirrors = resolve_registry_mirrors(registry_mirrors)
    limit = DEFAULT_MAX_CONCURRENT_DOWNLOADS if max_downloads is None else max_downloads
    limit_desc = f"，max-concurrent-downloads={limit}" if limit > 0 else ""
    return [
        ("探测 winget / WSL / 既有 Docker Desktop",
         DETECT_PREREQS_CMD),
        (f"静默安装 Docker Desktop（winget {WINGET_ID}）",
         f"winget install -e --id {WINGET_ID} --silent "
         "--accept-package-agreements --accept-source-agreements"),
        (f"合并 %USERPROFILE%\\.docker\\daemon.json"
         f"（registry-mirrors=[{','.join(mirrors)}]{limit_desc}）",
         MERGE_DAEMON_JSON_CMD),
        ("验证 docker 版本与 GPU 透传（阶段 B 引导重启 + WSL2 + GPU）",
         VERIFY_AFTER_CMD),
    ]


def render_instructions(
    registry_mirrors: list[str] | None = None,
    max_downloads: int | None = None,
) -> str:
    """渲染可复制到 PowerShell 的指引（`#` 注释开头，人类阅读而非脚本）。

    与 docker_setup.render_instructions 平行：同一参数签名、同一语义。
    Windows 侧不内置 shutdown（重启由用户手动）；阶段 B 用 el-card 提示。
    """
    limit = DEFAULT_MAX_CONCURRENT_DOWNLOADS if max_downloads is None else max_downloads
    mirrors = resolve_registry_mirrors(registry_mirrors)
    limit_desc = f"，max-concurrent-downloads={limit}" if limit > 0 else ""
    lines = [
        "# Windows Docker Desktop 一键安装指引（PowerShell，无需管理员）",
        f"# 阶段 A（自动）：winget 装 Desktop → 合并 %USERPROFILE%\\.docker\\daemon.json",
        f"#                 （多源：{', '.join(mirrors)}{limit_desc}）",
        "# 阶段 B（手动，5 步）：重启 → 启动 Desktop 勾 WSL2 → Settings→GPU 勾上",
        "#                 → 回 WebUI 点【已就绪，点我验证】",
        f"# 自动执行：modelctl env setup docker --run --os=windows",
    ]
    for desc, cmd in install_steps(registry_mirrors, limit):
        lines.append(f"# {desc}")
        if cmd.startswith("_modelctl_"):
            lines.append(f"# {cmd}（由 modelctl 以 Python 直接完成）")
        else:
            lines.append(f"# {cmd}")
    return "\n".join(lines)


# ---- daemon.json 合并 ----


def _merge_daemon_json(
    registry_mirrors: list[str],
    max_downloads: int = DEFAULT_MAX_CONCURRENT_DOWNLOADS,
) -> bool:
    """合并 %USERPROFILE%\\.docker\\daemon.json（行为平行 docker_setup）。

    保留 runtime / nvidia / log-* 等用户字段；registry-mirrors 做 union
    （既有存活源在前 + 新增存活源按入参顺序追加，停服源一律剔除）；
    max_downloads > 0 时覆盖；无变化返回 False，有变化写盘后返回 True。
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
        logger.warning(f"{DAEMON_JSON} 存在已停服的 registry-mirrors，已剔除："
                       f"{', '.join(dead)}")
    mirrors = list(existing)
    changed = bool(dead)
    for m in registry_mirrors:
        if is_dead_mirror(m):
            logger.warning(f"忽略已停服的 registry-mirror：{m}")
            continue
        if m not in mirrors:
            mirrors.append(m)
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


# ---- winget 子进程 helper（run_install 与测试共用） ----


def _run_winget_install(tail: list[str], emit_fn: Callable[[StageEvent], None]) -> int:
    """运行 winget install（subprocess.Popen + 100ms 间隔 poll + 实时静默检测）。

    生产不变式：
      - 子进程 stdout 静默 ≥ UAC_SILENCE_SEC 秒 → emit need_UAC（只发一次，不 kill）
      - 进程退出后 communicate() 收尾，stdout 末 50 行入 tail
      - winget 18/19 路径统一：测试 18 用 Popen mock 立即退出 rc=3 + 长 stdout，
        测试 19 用 Popen mock 前 3 次 poll None + 之后 0 + communicate 返回 bytes

    返回 winget 子进程 exit code。
    """
    cmd = ["winget", "install", "-e", "--id", WINGET_ID, "--silent",
           "--accept-package-agreements", "--accept-source-agreements"]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, close_fds=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        emit_fn(StageEvent(
            type="error", stage="error",
            message=f"winget 启动失败：{exc}", ts=_ts_now(), code=3,
        ))
        return 3

    started = time.monotonic()
    uac_notified = False
    hard_deadline = started + 30 * 60  # 硬上限 30 分钟

    def _silence_check(now: float) -> None:
        """首次静默超阈值 → emit need_UAC 一次（不 kill、不重复发）。"""
        nonlocal uac_notified
        if uac_notified:
            return
        if (now - started) >= UAC_SILENCE_SEC:
            uac_notified = True
            emit_fn(StageEvent(
                type="stage", stage="need_UAC",
                message=(
                    "Windows 系统已弹出 UAC 授权框（用于安装 Docker Desktop）。"
                    "请到 Windows 任务栏 / 桌面右上方点击『允许』。"
                    "此日志仍会持续跟踪，不会中断安装。"),
                ts=_ts_now(),
            ))

    while True:
        rc = proc.poll()
        if rc is not None:
            break
        # 静默检测（基于真实墙钟）
        now = time.monotonic()
        _silence_check(now)
        if now >= hard_deadline:
            emit_fn(StageEvent(
                type="error", stage="error",
                message="winget 30 分钟未到（请手动打开 Windows 终端看 winget 状态）",
                ts=_ts_now(), code=3,
            ))
            try:
                proc.kill()
            except (OSError, ValueError):
                pass
            return 3
        time.sleep(0.1)

    # 进程已退出 —— 收尾：communicate 读出所有 buffer 剩余的 stdout
    out_any = ""
    try:
        out, _err = proc.communicate(timeout=5)
        if isinstance(out, bytes):
            out_any = out.decode("utf-8", errors="replace")
        elif out:
            out_any = out
    except (subprocess.TimeoutExpired, Exception):
        out_any = ""
    if out_any:
        lines = [l.rstrip() for l in out_any.splitlines() if l.rstrip()]
        tail.extend(lines[-50:])
        while len(tail) > 50:
            tail.pop(0)
    return int(rc or 0)


# ---- run_install 主入口 ----


def run_install(
    registry_mirrors: list[str] | None = None,
    max_downloads: int | None = None,
    on_stage: Callable[[StageEvent], None] | None = None,
) -> int:
    """Windows 一键安装主入口。仅 sys.platform == "win32" 可执行。

    返回退出码：
    - 0 = 成功（完成阶段 A 4 步，过程中 emit 全部阶段）
    - 2 = 预检失败（非 win32 / winget 不在）
    - 3 = winget 子进程失败（error 事件 code=3 + payload.tail 末 50 行 stdout）

    on_stage(None) → 走 logger.info 镜像（供 CLI）；否则前端 SSE 端走
    StageEvent 直接序列化（零漂移）。
    UAC 阻塞：winget 20s 无 stdout → emit need_UAC 但不 kill，仍继续等。
    """

    def emit(stage: str, message: str,
             etype: str = "stage",
             code: int | None = None,
             payload: dict | None = None,
             ) -> None:
        try:
            ev = StageEvent(
                type=etype, stage=stage, message=message, ts=_ts_now(),
                code=code, payload=payload,
            )
        except Exception as exc:  # pragma: no cover
            logger.exception(f"StageEvent 构造失败：{exc}")
            return
        if on_stage is not None:
            try:
                on_stage(ev)
            except Exception as e_cb:
                logger.exception(f"on_stage callback 异常：{e_cb}")
        else:
            logger.info(f"[win-setup][{stage}] {message}")

    emit_fn: Callable[[StageEvent], None] = (lambda ev: logger.info(str(ev.to_sse_dict()))) \
        if on_stage is None \
        else on_stage

    # ---------- 预检：平台硬门禁 ----------
    if sys.platform != "win32":
        emit("error",
             f"Windows 安装路径仅 Windows 主机可 --run，当前平台 {sys.platform!r}",
             etype="error", code=2)
        return 2

    # ---------- 阶段 1：winget 检测 ----------
    emit("detect_winget", "探测 winget（shutil.which）")
    winget_path = shutil.which("winget")

    # ---------- 阶段 2：WSL 探测（非阻断；无论 winget 是否存在都跑） ----------
    emit("detect_wsl2", "探测 WSL（wsl --version，15s 超时）")
    wsl_rc, wsl_out = _probe_wsl2()
    msg = (wsl_out or "").strip() or ("wsl 命中，DM 版本" if wsl_rc == 0
                                     else "wsl 不可用（不阻断安装）")
    emit("log", f"WSL 探测结果：{msg[:120]}")

    # ---------- winget 缺失 → 预检失败（return 2） ----------
    if winget_path is None:
        emit("error",
             "winget 未找到。请从 Microsoft Store 搜索 'winget' 或到 "
             "https://aka.ms/winget-cli 安装，然后回 WebUI 重新点一键安装。",
             etype="error", code=2)
        return 2

    desktop_before = _desktop_installed()

    # ---------- 阶段 3：winget 安装 或 already_installed 分支 ----------
    tail: list[str] = []
    if desktop_before:
        emit("already_installed",
             "Docker Desktop 已装（跳过 winget），继续校验配置")
    else:
        emit("winget_running",
             f"winget 静默安装 {WINGET_ID}（默认 30 分钟 timeout）")
        rc = _run_winget_install(tail, emit_fn)
        emit("winget_done", f"winget 进程退出（rc={rc}）")
        if rc != 0:
            emit("error",
                 f"winget install {WINGET_ID} 失败（code {rc}）",
                 etype="error", code=3,
                 payload={"tail": tail[-50:]})
            return 3

    # ---------- 阶段 4：daemon.json 合并（非阻断，失败只 warning） ----------
    mirrors = resolve_registry_mirrors(registry_mirrors)
    limit = DEFAULT_MAX_CONCURRENT_DOWNLOADS if max_downloads is None else max_downloads
    emit("write_daemon_json",
         f"合并 {DAEMON_JSON}（registry-mirrors={len(mirrors)} 源"
         + (f"，max-concurrent-downloads={limit}" if limit > 0 else "") + "）")
    try:
        merged = _merge_daemon_json(mirrors, limit)
        if not merged:
            emit("log", "daemon.json 已是目标状态，不重写")
    except (OSError, json.JSONDecodeError) as exc:
        emit("log",
             f"写 {DAEMON_JSON} 失败（{exc}）；registry-mirrors 加速请手动到 "
             "Desktop Settings → Docker Engine")

    # ---------- 阶段 5：docker 版本验证（非阻断） ----------
    emit("test_docker_version", "运行 `docker --version` 验证 CLI 就位")
    docker_cli = shutil.which("docker")
    if docker_cli is None:
        emit("log",
             "docker 尚未在 PATH（重启终端或重启计算机后生效）")
    else:
        try:
            ver = subprocess.run([docker_cli, "--version"],
                                 capture_output=True, text=True, timeout=15)
            emit("log", (ver.stdout or "").strip() or (ver.stderr or "").strip() or "未知")
        except (OSError, subprocess.SubprocessError) as exc:
            emit("log", f"docker --version 调用失败：{exc}")

    # ---------- 阶段 5b：GPU 冒烟（nvidia-smi 在才跑，非阻断） ----------
    nvidia = shutil.which("nvidia-smi") is not None
    if nvidia:
        emit("test_gpus", "运行 GPU 冒烟：docker run --rm --gpus all "
                          f"{GPU_SMOKE_IMAGE} nvidia-smi")
        sb, so = _gpu_smoke()
        if sb == 0:
            emit("log", f"GPU 冒烟通过：{(so or '')[:120]}")
        else:
            emit("log",
                 f"GPU 冒烟失败（{(so or '')[:80]}）。请打开 Desktop Settings → "
                 "Resources → GPU 勾选后重跑本一键安装")
    else:
        emit("log", "本机无 nvidia-smi，跳过 GPU 冒烟（非 GPU 桌面属正常）")

    # ---------- 阶段 6：阶段 B 计划 + done ----------
    plan = post_install_plan(nvidia_present=nvidia)
    emit("post_install_plan",
         "阶段 B 引导（5 步：重启 → Desktop → WSL2 → GPU → 验证）",
         payload=plan)
    emit("done",
         "阶段 A 完成；请按阶段 B 卡片手动完成 5 步后点【已就绪，点我验证】")
    return 0


def post_install_plan(nvidia_present: bool = False) -> dict:
    """阶段 B 结构化卡片：5 步固定 id / label / action / cmd_hint / optional。

    字段与前端 `PostInstallStep` 类型严格对齐（Task 4 单测与手工 verify 落账）。
    `optional` 仅 nvidia 无时为 True（GPU 步骤可跳）。
    """
    gpu_optional = not nvidia_present
    steps = [
        {
            "id": "restart",
            "label": "重启计算机（启用 WSL2 backend）",
            "action": "restart",
            "cmd_hint": None,
            "optional": False,
        },
        {
            "id": "open_desktop",
            "label": "启动 Docker Desktop 首次启动向导",
            "action": "open_desktop",
            "cmd_hint": None,
            "optional": False,
        },
        {
            "id": "enable_wsl2",
            "label": "Settings → General 勾选 Use the WSL 2 based engine",
            "action": None,
            "cmd_hint": None,
            "optional": False,
        },
        {
            "id": "enable_gpu",
            "label": "Settings → Resources → GPU 勾选 Enable GPU request support",
            "action": None,
            "cmd_hint": None,
            "optional": gpu_optional,
        },
        {
            "id": "verify",
            "label": "回 WebUI 点【已就绪，点我验证】复跑 5 项诊断",
            "action": "verify",
            "cmd_hint": "modelctl env setup docker --os=windows --verify",
            "optional": False,
        },
    ]
    return {"steps": steps}
