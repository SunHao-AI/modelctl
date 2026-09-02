#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/process.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 进程生命周期管理
# ===============================================================================

"""core/process.py — 引擎无关的进程生命周期：后台启动、PID、停止、健康检查。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import typing
import urllib.error
import urllib.request
from pathlib import Path

from loguru import logger

from modelctl.core.envfile import PROJECT_ROOT

if typing.TYPE_CHECKING:
    from modelctl.core.profile import Profile

if sys.platform == "win32":
    import ctypes


def is_pid_alive(pid: int) -> bool:
    """探测 pid 对应进程是否存活（PID 文件 / GPU 锁共用的统一入口）。"""
    if sys.platform == "win32":
        # Windows 实测：对不存在的 PID 调 CPython os.kill(pid, 0)（内部 OpenProcess）
        # 之后控制台会被投递异步 Ctrl-C 事件、连带杀掉宿主会话；改用 ctypes 直连
        # kernel32.OpenProcess 做存在性探测可完全规避。
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)  # signal 0 = existence probe
        return True
    except OSError:
        return False


def log_dir() -> Path:
    d = Path(os.environ.get("LOG_DIR") or PROJECT_ROOT.parent / "logs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    """进程元数据目录（PID 文件），默认项目根 data/cache（与用量统计缓存一致）。"""
    d = Path(os.environ.get("CACHE_DIR") or PROJECT_ROOT / "data" / "cache")
    d.mkdir(parents=True, exist_ok=True)
    return d


def pid_file(name: str) -> Path:
    return cache_dir() / f"{name}.pid"


def launch_log(name: str) -> Path | None:
    """当前实例的启动日志（固定文件名 launch-<name>.log；未启动过则为 None）。

    固定文件名 + 每次启动覆盖，避免多份时间戳日志堆积。
    """
    path = log_dir() / f"launch-{name}.log"
    return path if path.is_file() else None


def start_detached(name: str, command: list[str], extra_env: dict[str, str],
                   write_pid: bool = True) -> tuple[int, subprocess.Popen]:
    """后台启动进程，返回 (pid, Popen)。Popen 供调用方在等待健康检查期间探测早退（fail-fast）。

    write_pid=False：docker runtime 专用——`docker run --detach` 客户端在容器创建后
    ~1s 内退出、Popen.pid 写入后即为已死号，后续状态判定会被该 PID 误导，
    故 docker 路径调用方传 False 不写 PID 文件，改用容器名作为身份标识。
    返回签名不变：pid 仍为本机 Popen.pid，仅作日志显示用。"""
    log_path = log_dir() / f"launch-{name}.log"
    env = {**os.environ, **extra_env}
    fp = open(log_path, "w", encoding="utf-8")  # "w"：每次启动覆盖旧日志
    kwargs: dict = {"stdout": fp, "stderr": subprocess.STDOUT, "env": env, "stdin": subprocess.DEVNULL}
    kwargs["start_new_session"] = True  # nohup 语义：SSH 断开不影响
    proc = subprocess.Popen(command, **kwargs)
    if write_pid:
        pid_file(name).write_text(str(proc.pid), encoding="utf-8")
    return proc.pid, proc


def is_running(name: str) -> bool:
    pf = pid_file(name)
    if not pf.is_file():
        return False
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except ValueError:
        # 无法解析的 PID 文件直接删除，视为异常
        pf.unlink(missing_ok=True)
        return False
    if is_pid_alive(pid):
        return True
    # 进程已不存在，清理残留的 PID 文件
    pf.unlink(missing_ok=True)
    return False


def is_running_any(name: str, profile: Profile | None) -> bool:
    """统一运行态判定：端口 /health 2xx 优先，PID 文件机器兜底（venv 路径残留清理 / dead PID 清理）。

    profile 缺省：gateway.stats 等无 Profile 场景退回纯 PID 探测，与原 is_running(name) 行为一致。
    profile 存在（通常 venv/docker 都能获取 port）：先探测 127.0.0.1:{profile.port}/health
    （单次 2s 超时，不重试），2xx 直接 True；失败/不可达再回到 PID 文件探测。
    任何异常（端口不通 / PID 文件损坏 / profile.short 访问异常）一律 False，绝不抛错。
    命中 dead-PID 路径时会顺手 unlink PID 文件并发出一次 warning，把"PID 残留"语义
    交由更上层（cli._instance_state）判定——本函数只负责"还活着吗"。
    """
    # 1. 端口健康探测（仅 profile 非 None 时）
    port_ok = False
    if profile is not None:
        port: int | None = getattr(profile, "port", None)
        if port is not None:
            headers: dict[str, str] = {}
            try:
                key = (getattr(profile, "api_key", None)
                       or (profile.engine_config or {}).get("api_key"))
            except Exception:  # noqa: BLE001 —— 配置字段缺失不阻塞判定
                key = None
            if key:
                headers["Authorization"] = f"Bearer {key}"
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers=headers)
                with open_local(req, timeout=2.0) as resp:
                    if 200 <= resp.status < 300:
                        port_ok = True
            except (urllib.error.URLError, OSError, ValueError):
                pass
    # 2. PID 文件探测（无论端口是否健康都执行：端口健康时顺带清理 dead/损坏的残留 PID 文件）
    pid_ok = False
    pf = pid_file(name)
    if pf.is_file():
        try:
            pid = int(pf.read_text(encoding="utf-8").strip())
        except ValueError:
            pf.unlink(missing_ok=True)
            logger.warning(f"{name}：PID 文件无法解析，已清理")
        else:
            if is_pid_alive(pid):
                pid_ok = True
            else:
                pf.unlink(missing_ok=True)
    return port_ok or pid_ok


def stop_docker_instance(name: str, container_name: str) -> bool:
    """docker runtime 路径停止：`docker rm -f <container_name>` + 清本地 PID + 释放 GPU 锁。

    docker rm -f 幂等（容器不存在亦退出码 0；非零退出码仅警告、不阻断）；本地 PID
    文件（venv 路径才有）一并清理以防环境切换残留（docker→venv 反复切换场景）。
    """
    try:
        result = subprocess.run(["docker", "rm", "-f", container_name],
                                capture_output=True, timeout=10)
        if result.returncode != 0:
            logger.warning(
                f"docker rm -f {container_name} 返回码 {result.returncode}："
                f"{(result.stderr or b'').decode(errors='replace').strip()}")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"docker rm -f {container_name} 执行失败：{exc}")
    pf = pid_file(name)
    if pf.is_file():
        pf.unlink(missing_ok=True)
    try:
        from modelctl.core.gpu_lock import release_gpu_lock
        release_gpu_lock(name)
    except Exception:  # noqa: BLE001
        pass
    return True


def stop_instance(name: str, port: int, patterns: list[str]) -> bool:
    """先按 PID 优雅终止（POSIX：进程组 SIGTERM→SIGKILL；Windows：taskkill /T /F），
    POSIX 平台再按端口/进程名兜底。返回是否执行了基于 PID 的停止。"""
    stopped = False
    pf = pid_file(name)
    if pf.is_file():
        try:
            pid = int(pf.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = None
        if sys.platform != "win32":
            if pid is not None:
                try:
                    os.killpg(pid, signal.SIGTERM)  # type: ignore[attr-defined]  # POSIX-only，Windows 类型桩无此 API
                except OSError:
                    pass
                deadline = time.time() + 10
                while time.time() < deadline:
                    if not is_pid_alive(pid):
                        break
                    time.sleep(0.5)
                else:
                    try:
                        os.killpg(pid, signal.SIGKILL)  # type: ignore[attr-defined]  # POSIX-only，Windows 类型桩无此 API
                    except OSError:
                        pass
                stopped = True
        elif pid is not None:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            stopped = True
        pf.unlink(missing_ok=True)
    if sys.platform != "win32":
        subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
        for pat in patterns:
            subprocess.run(["pkill", "-f", pat], capture_output=True)
    try:
        from modelctl.core.gpu_lock import release_gpu_lock

        release_gpu_lock(name)
    except Exception:
        pass
    return stopped


def open_local(request: urllib.request.Request, timeout: float):
    """本机回环探测专用 opener：绕过 http(s)_proxy/no_proxy 环境变量。

    项目内所有健康检查目标均为 127.0.0.1；若沿用 urlopen 默认行为，设置了系统代理的机器上
    回环请求也会被转发给代理（通常无法访问本机端口），导致探测永远失败、启动卡满超时。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout)


def wait_health(url: str, timeout: float, api_key: str | None = None, alive_check=None) -> bool:
    """轮询探测健康端点直至成功或超时。

    alive_check：可选的进程存活探针（返回 bool）。引擎进程先行退出时立即结束等待，
    不再空转到超时——但先完成当次探测再判定，保证共享后端场景（如 ollama 多 profile
    共用一个 serve）中本实例子进程退出、端口仍由他人服务时不误报失败。
    """
    deadline = time.time() + timeout
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    interval = 1.0
    last_err = ""
    while True:
        healthy = False
        try:
            req = urllib.request.Request(url, headers=headers)
            with open_local(req, timeout=5) as resp:
                if 200 <= resp.status < 300:
                    healthy = True
                else:
                    last_err = f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            # URLError.reason 比 str(e) 更简洁（如 "Name or service not known"）
            last_err = str(getattr(e, "reason", None) or e)
        if healthy:
            return True
        if alive_check is not None and not alive_check():
            logger.warning("引擎进程已提前退出，中止健康检查等待")
            break
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
        interval = min(interval * 2, 5.0)
    if last_err:
        logger.warning(f"健康检查未通过（{url}），最后错误：{last_err}")
    return False


def docker_container_alive(container_name: str) -> bool:
    """探测 docker 容器是否仍在运行（docker inspect 容器 Running 状态）。

    **保守语义**：只有 `docker inspect` 明确返回「容器不存在 / 容器已退出（Running=false）」
    才返回 False；其它任何情况（docker 不在 PATH / daemon 不可用 / inspect 超时 /
    解析异常 / 返回非预期值）一律返回 True（视为存活）。

    设计动机：docker 分支的 fail-fast 是 *nice-to-have*（能少等不写得到），但误报
    （容器其实活着却判死了）*must-avoid*（跨 Engine / 权限问题会导致健康检查被 1 秒
    截断、提示「进程提前退出」且不是准确原因）。保守侧把 any 不确定性当存活，
    探针只在 daemon 与 Engine 在同一台主机、且容器真实死亡时才触发 fail-fast——
    跨 Engine 调用（如 Win 自检 Linux 容器）时 docker inspect 查不到 → 探针
    返回 True → 不触发假死，仍走 /health 600s 兜底。
    返回 True 不等于服务已就绪，只是「没有证据说它死了」。
    """
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        # docker 不可用 / 超时——保守视为存活，保留健康检查兜底
        return True
    if out.returncode != 0:
        # inspect 非零退出 = 容器不存在（含 --rm 容器崩溃后已被 daemon 回收）
        # 或 docker 拒绝访问。区分对待：不存在才是「后端已死」，其余守 True
        err = (out.stderr or "").strip()
        # docker 容器不存在会给 exit 1 且 stderr 为 "No such object: <name>"
        return "No such object" not in err
    # 容器存在——看 Running 标志
    return out.stdout.strip() != "false"


def tail_file(path: Path, lines: int) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


# 启动失败诊断的错误标记：命中行的前 _EXCERPT_BEFORE、后 _EXCERPT_AFTER 行构成一个上下文块。
_EXCERPT_MARKERS = (
    "Traceback (most recent call last)",
    "CUDA error",
    "out of memory",
    "OutOfMemory",
    "NCCL",
    "RuntimeError",
    "ValueError",
    "AssertionError",
    "ImportError",
    "ModuleNotFoundError",
    "Address already in use",
    "Engine core initialization failed",
)
_EXCERPT_BEFORE = 10
_EXCERPT_AFTER = 60
_EXCERPT_MAX_BLOCKS = 3
_EXCERPT_LINE_WIDTH = 240


def log_excerpt(path: Path) -> str | None:
    """按错误标记截取日志关键片段（多区块合并），用于进程早退时的失败诊断。

    vLLM 等引擎崩溃时真实异常常位于日志中部，尾部 50 行可能只是 traceback 的尾巴；
    此函数定位 Traceback / OOM / NCCL 等标记并带上下文输出（最多 3 个区块）。
    无标记或读取失败返回 None。
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    ranges: list[list[int]] = []  # [start, end)，按出现顺序收集、重叠即并入前一区段
    for i, line in enumerate(lines):
        if any(m in line for m in _EXCERPT_MARKERS):
            start = max(0, i - _EXCERPT_BEFORE)
            end = min(len(lines), i + _EXCERPT_AFTER)
            if ranges and start <= ranges[-1][1]:
                ranges[-1][1] = max(ranges[-1][1], end)
            else:
                ranges.append([start, end])
            if len(ranges) >= _EXCERPT_MAX_BLOCKS:
                break
    if not ranges:
        return None
    out: list[str] = []
    for start, end in ranges:
        out.append(f"—— 第 {start + 1}-{end} 行 ——")
        for n in range(start, end):
            text = lines[n]
            if len(text) > _EXCERPT_LINE_WIDTH:
                text = text[:_EXCERPT_LINE_WIDTH] + " …(截断)"
            out.append(f"{n + 1:>6} | {text}")
        if end < len(lines):
            out.append(f"...（后续还有 {len(lines) - end} 行，完整内容见日志文件）")
    return "\n".join(out)
