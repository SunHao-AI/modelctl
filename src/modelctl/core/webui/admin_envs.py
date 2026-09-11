#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_envs.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/2 10:00
# @Desc   : Web UI 环境管理 API 端点
# ===============================================================================

"""core/webui/admin_envs.py — Web UI 托管引擎环境（venv）管理 API 端点。

所有端点前缀 ``/admin/api/envs``（由调用方 include_router 时设置）。
托管引擎（vllm/sglang/...）与 gateway 子项目的 venv 建在同一 repo 下，依赖
``modelctl.core.envs`` 的 ``known_targets()/status()/setup()/remove()``。

setup 是一个 28min+ 的在线程中跑的长操作，走 TaskManager 异步任务 + SSE（见
admin_models._do_start 同款模式）；remove 是快速的 rmtree，同步返回。
"""

from __future__ import annotations

import asyncio
import ctypes
import subprocess
import sys
import threading
import time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from modelctl.core.webui.admin_auth import require_auth, require_auth_or_query
from modelctl.core.webui.admin_tasks import TaskManager

router = APIRouter()

# —— Docker 一键安装（Windows-only）单例
# 端点注册顺序：这 5 个精确路由必须在下方 `POST /{target}/setup` 之前注册，
# 否则 `{target}` 路径参数会把 "docker" 吞进 setup 端点。
docker_install_task_manager = TaskManager()
_user_active_installs: dict[str, dict] = {}  # user_id -> {task_id, started_at}
_user_pending: dict[str, set[str]] = {}      # user_id -> {task_id} 未完成 docker install 集合
_DOCKER_DEDUP_WINDOW_SEC: float = 300.0     # 5 分钟去重窗
_DOCKER_MAX_ACTIVE_PER_USER: int = 3

# 非托管引擎：原生二进制 / 官方安装器 / 源码编译，不建 venv，因此不出现在 targets 表格里。
# 仅用于在 UI 上说明「为什么这里看不到它们」并给出安装方式。
UNMANAGED_ENGINES = ("ollama", "unsloth", "llamacpp")
# llamacpp 无发布版可执行文件，需源码编译出 llama-server（产物路径由 pre_start 定位）
_LLAMACPP_CLONE = "git clone https://github.com/ggml-org/llama.cpp.git"
_LLAMACPP_BUILD = "cmake -B build -DGGML_CUDA=ON && cmake --build build -j 4"
UNMANAGED_INSTALL_HINTS = {
    "ollama": "curl -fsSL https://ollama.com/install.sh | sh",
    "unsloth": "curl -fsSL https://unsloth.ai/install.sh | sh",
    "llamacpp": f"{_LLAMACPP_CLONE}\n{_LLAMACPP_BUILD}",
}

# —— Docker 旁路指引：镜像名/示例 yaml 事实与 models/<engine>/*.yaml 保持同步；
#    支持矩阵的事实来源是 envs.DOCKER_CAPABLE_ENGINES（有测试锚定），勿在此另行硬编码集合。
_DOCKER_STEP_PREP = (
    "部署机准备 Docker + NVIDIA Container Toolkit（点上方「完整诊断」查看检查项与可复制安装脚本，"
    "或执行 CLI：modelctl env setup docker --run）"
)
_DOCKER_STEP_START = (
    "modelctl stop <模型> && modelctl start <模型>；docker 路径要求 model 为本地已有目录"
    "（HuggingFace id 需先跑一次触发下载落地）"
)
DOCKER_BYPASS_GUIDES: dict[str, dict] = {
    "vllm": {
        "image_example": "vllm/vllm-openai:<tag>",
        "yaml_field_path": "vllm.docker_image",
        "example_yaml": "models/vllm/qwen3.8-flash-next.yaml",
    },
    "tokenspeed": {
        "image_example": "lightseekorg/tokenspeed:latest",
        "yaml_field_path": "tokenspeed.docker_image",
        "example_yaml": "models/tokenspeed/qwen3.5-397b.yaml",
    },
    "tensorrt_llm": {
        "image_example": "nvcr.io/nvidia/tensorrt-llm:<tag>",
        "yaml_field_path": "tensorrt_llm.docker_image",
        "example_yaml": "models/tensorrt_llm/qwen3.8.yaml",
    },
}
# 未实现 docker 分支的引擎统一说明（不造假指引；缺口记录见 docs/TODO.md 2.2）
DOCKER_UNSUPPORTED_NOTE = (
    "modelctl 的该引擎适配器暂不支持 docker 运行时，官方镜像无法经 modelctl 启动；"
    "建议改用已支持的引擎（vllm / tokenspeed / tensorrt_llm），或在 Linux 部署机上建托管 venv"
)


def _router() -> APIRouter:
    """子路由工厂：返回 APIRouter（主路由 include_router 时由其调用）。"""
    return router


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


def _unmanaged_targets() -> list[dict]:
    """非托管引擎的安装情况（原生二进制 / 安装器 / 编译产物），供 UI 说明用。

    探测口径与 ``modelctl probe`` 一致：PATH 探测，llamacpp 额外看编译产物。
    """
    from modelctl.core.capabilities import binary_paths, find_llamacpp_binary

    paths = binary_paths(list(UNMANAGED_ENGINES))
    if not paths.get("llamacpp"):
        paths["llamacpp"] = find_llamacpp_binary()
    return [
        {
            "name": name,
            "installed": bool(paths.get(name)),
            "path": paths.get(name),
            "install_hint": UNMANAGED_INSTALL_HINTS.get(name, ""),
        }
        for name in UNMANAGED_ENGINES
    ]


@router.get("")
async def list_envs(_: None = Depends(require_auth)):
    """GET /admin/api/envs — 列出全部受管 target（6 托管引擎 + gateway）及其状态。

    同时返回 ``unmanaged``：ollama / unsloth / llamacpp 的安装情况。它们不走
    modelctl 托管 venv（原生二进制或官方安装器），故不在 targets 中，仅在 UI 说明。
    """
    from modelctl.core import envs
    from modelctl.core.capabilities import docker_ready

    status = await asyncio.to_thread(envs.status)
    targets = await asyncio.to_thread(envs.known_targets)

    # dready 先算（与 admin_probe._gather_overview 同源：capabilities.docker_ready），
    # 既要渲染 targets[].runtime_docker_ready 又要填 docker_env.runtime_ready。
    dready = docker_ready()

    out = []
    for t in targets:
        entry = status.get(t) or {}
        if not entry:
            # status() 缺 key 时的兜底（防御性：保证输出结构稳定）
            entry = {"exists": False}
        if entry.get("exists"):
            installed = True
            parts: list[str] = []
            py = entry.get("python")
            if py:
                parts.append(f"python {py}")
            packages = entry.get("packages")
            if isinstance(packages, dict) and packages:
                parts.append(f"{len(packages)} 个包")
            detail = "，".join(parts) if parts else "已安装"
        else:
            installed = False
            detail = "未安装"
        out.append(
            {
                "name": t,
                "installed": installed,
                "detail": detail,
                "platform_supported": envs.platform_supports(t),
                "docker_supported": t in envs.DOCKER_CAPABLE_ENGINES,
                # runtime_docker_ready：docker 主路径是否就绪（capabilities.docker_ready）。
                # 为 true 时 yaml 写 docker_image 即可绕过 venv；为 false 但 docker_supported
                # 为 true 时，UI 可用 docker_env.runtime_ready / docker_bypass.steps[0] 补
                # "先去部署机建 docker" 指引。gateway 无 docker 运行时，恒为 False。
                "runtime_docker_ready": (t in envs.DOCKER_CAPABLE_ENGINES) and dready,
            }
        )

    unmanaged = await asyncio.to_thread(_unmanaged_targets)

    # Docker 环境就绪探测：纯 shutil.which，绝不落子进程，不拖慢列表页加载
    from modelctl.core import docker_setup

    missing = docker_setup.path_level_missing()
    docker_env = {"ready": not missing, "missing": missing, "guide": docker_setup.MSG_GUIDE,
                  "runtime_ready": dready}

    # Docker 旁路指引：已支持引擎给镜像事实 + 三步；其余仅 note（gateway 不适用旁路，天然排除）
    bypass: list[dict] = []
    for name in envs.MANAGED_ENGINES:
        guide = DOCKER_BYPASS_GUIDES.get(name)
        if guide:
            bypass.append(
                {
                    "name": name,
                    "docker_supported": True,
                    "image_example": guide["image_example"],
                    "yaml_field_path": guide["yaml_field_path"],
                    "example_yaml": guide["example_yaml"],
                    "steps": [
                        f"编辑 models/{name}/<模型>.yaml，在 {name}: 块下加一行 "
                        f"docker_image: {guide['image_example']}",
                        _DOCKER_STEP_PREP,
                        _DOCKER_STEP_START,
                    ],
                }
            )
        else:
            bypass.append({"name": name, "docker_supported": False, "note": DOCKER_UNSUPPORTED_NOTE})

    return {"targets": out, "unmanaged": unmanaged, "docker_env": docker_env, "docker_bypass": bypass}


@router.get("/docker/diagnose")
async def docker_diagnose(
    request: Request,
    os: str | None = Query(default=None, description="目标 OS：\"linux\" | \"windows\"；缺省按 sys.platform"),
    _: None = Depends(require_auth),
):
    """GET /admin/api/envs/docker/diagnose — Docker 环境完整诊断（只读，按需触发）。

    ``os`` 缺省按 ``sys.platform`` 判分支：
    - windows → 走 ``windows_setup.diagnose()``（winget / WSL2 / Docker Desktop /
      GPU 冒烟 5 项），非 win32 主机 400 硬拒；
    - linux   → 走 ``docker_setup.diagnose()``（含 ``docker info`` 子进程，15s 超时）。
    本端点绝不执行任何安装动作——实际安装仍只走既有 CLI 通道或
    ``POST /envs/docker/install``（异步任务）新端点。
    """
    target_os = os or ("windows" if sys.platform == "win32" else "linux")
    if target_os == "windows" and sys.platform != "win32":
        return JSONResponse(
            status_code=400,
            content={"error": {
                "code": "platform_mismatch",
                "message": f"Windows 诊断仅 Windows 主机可执行，当前平台 {sys.platform!r}",
            }},
        )
    if target_os not in ("linux", "windows"):
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "bad_os", "message": f"未知 os: {os!r}"}},
        )
    try:
        if target_os == "windows":
            from modelctl.core import windows_setup
            checks = await asyncio.to_thread(windows_setup.diagnose)
            checks_out = [{"key": c.key, "label": c.label, "ok": c.ok,
                           "detail": c.detail, "hint": c.hint} for c in checks]
            instructions = ""  # Windows 分支不返回 shell 脚本（走 winget 弹窗）
        else:
            from modelctl.core import docker_setup
            checks = await asyncio.to_thread(docker_setup.diagnose)
            checks_out = [{"key": c.key, "label": c.label, "ok": c.ok, "detail": c.detail}
                          for c in checks]
            instructions = docker_setup.render_instructions()
        return {"platform": target_os, "checks": checks_out, "instructions": instructions}
    except Exception as exc:  # noqa: BLE001 — 诊断失败统一 500（与同文件 remove_env 惯例一致）
        logger.exception(f"Docker 诊断异常: {exc}")
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal", "message": f"诊断失败: {exc}"}},
        )


# ---------------------------------------------------------------------------
# Docker 一键安装（Windows-only）：install SSE / system-action / diagnose?os=
# 5 端点，必须在 `POST /{target}/setup` 之前注册（`{target}` 会吞精确路径）。
# ---------------------------------------------------------------------------

# 二次 Depends(bearer_scheme) 让 FastAPI 注入凭据对象，用于派生 user_id
# （require_auth 用的依赖返回 None，不能直接读 credentials）。
from modelctl.core.webui.admin_auth import (  # noqa: E402
    bearer_scheme as _docker_bearer,
    HTTPAuthorizationCredentials as _HTTPCredentials,
)


@router.post("/docker/install")
async def docker_install(
    request: Request,
    os_: str = Query(default="", alias="os", description="可省略，body.os 优先"),
    credentials: _HTTPCredentials = Depends(_docker_bearer),
    _: None = Depends(require_auth),
):
    """POST /admin/api/envs/docker/install — 异步触发 Docker 一键安装，返回 202 + task_id。

    body 结构：``{"os": "linux"|"windows"(可选), "registry_mirrors": [...],
    "max_concurrent_downloads": 0~8}``。``os`` 缺省按 ``sys.platform`` 判分支：

    - 非 win32 主机 + ``os=windows`` → 400 platform_mismatch（浏览器点按钮场景最常见）。
    - 同一 user_id 在 5 分钟窗口内已有活跃 task → 直接返回原 task_id +
      ``already_running: true``（不新建任务，前端刷新不会重复开线程）。
    - 同一 user_id 未完成任务已达上限 → 429 rate_limited。
    - 其它情况 → 建新 task，后台线程跑 ``windows_setup.run_install``（os=windows）
      或 ``docker_setup.run_install``（os=linux），逐 stage 通过
      ``task.event("stage", ev.to_sse_dict())`` 广播给 SSE 订阅者。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — 空 body 视为 {}
        body = {}
    if not isinstance(body, dict):
        body = {}

    target_os = body.get("os") or os_ or ("windows" if sys.platform == "win32" else "linux")
    if target_os == "windows" and sys.platform != "win32":
        return JSONResponse(
            status_code=400,
            content={"error": {
                "code": "platform_mismatch",
                "message": f"Windows 安装仅 Windows 主机可执行（当前 {sys.platform!r}），"
                            "请在本机浏览器上操作",
            }},
        )
    if target_os not in ("linux", "windows"):
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "bad_os", "message": f"未知 os: {target_os!r}"}},
        )

    user_id = credentials.credentials if credentials and credentials.credentials else "anonymous"

    # 去重窗：同一 user 5 分钟内已有活跃 task → 直接复用
    existing = _user_active_installs.get(user_id)
    if existing:
        elapsed = time.time() - float(existing.get("started_at") or 0)
        if elapsed < _DOCKER_DEDUP_WINDOW_SEC:
            prev = docker_install_task_manager.get_task(existing.get("task_id", ""))
            if prev is not None:
                return JSONResponse(
                    status_code=202,
                    content={
                        "task_id": prev.id,
                        "events": f"/admin/api/envs/docker/install/{prev.id}/events",
                        "os": target_os,
                        "already_running": True,
                    },
                )
            # prev 已被 trim 但没有清去重状态：清掉让下一请求可以创建新任务
            _user_active_installs.pop(user_id, None)

    # 每用户 max 未完成任务数（per-user pending 集合计数，避免跨 user 累加误 429）
    active_count = len(_user_pending.get(user_id, ()))
    if active_count >= _DOCKER_MAX_ACTIVE_PER_USER:
        return JSONResponse(
            status_code=429,
            content={"error": {"code": "rate_limited",
                                "message": f"用户 {user_id} 未完成的 docker install 已达 {active_count} 任务，请稍后再试"}},
        )

    # 参数校验必须在任何状态写入之前：create_task + _user_pending.add 之后才返 400
    # 会让 pending 名额只增不减（无过期机制），累计 _DOCKER_MAX_ACTIVE_PER_USER 次
    # 非法请求后该用户永久 429。
    registry_mirrors = body.get("registry_mirrors") or []
    max_downloads = body.get("max_concurrent_downloads", 0) or 0
    if not isinstance(max_downloads, int) or isinstance(max_downloads, bool) or max_downloads < 0 or max_downloads > 8:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "bad_body",
                                "message": "max_concurrent_downloads 必须为 0-8 的整数"}},
        )

    task = docker_install_task_manager.create_task(
        kind="docker", action="install", target=f"docker:{target_os}"
    )
    _user_pending.setdefault(user_id, set()).add(task.id)

    _user_active_installs[user_id] = {
        "task_id": task.id,
        "started_at": time.time(),
        "os": target_os,
    }

    thread = threading.Thread(
        target=_run_docker_install_task,
        kwargs={"task": task, "target_os": target_os,
                "registry_mirrors": registry_mirrors, "max_downloads": max_downloads,
                "user_id": user_id},
        daemon=True,
    )
    thread.start()

    return JSONResponse(
        status_code=202,
        content={
            "task_id": task.id,
            "events": f"/admin/api/envs/docker/install/{task.id}/events",
            "os": target_os,
        },
    )


def _run_docker_install_task(task, target_os: str,
                              registry_mirrors: list[str], max_downloads: int,
                              user_id: str) -> None:
    """后台线程：调用 windows_setup / docker_setup 的 run_install，逐 stage 广播。

    线程与 FastAPI 事件循环解耦（daemon）：客户端 SSE 断开不 kill 子进程，
    winget / docker 会继续跑完（可能半吊子，但不会残留半成品 Desktop）。
    ``user_id`` 仅用于在 finally 里精确清掉本任务占用的去重窗条目，
    避免误删同用户后续任务。
    """
    task.update_status("running")

    def _on_stage(ev):
        task.event("stage", ev.to_sse_dict())
        # 回写 detail 让 GET /{task_id} fallback 能读到当前 stage
        if getattr(ev, "stage", None) and ev.stage != "unknown":
            task.update_detail(ev.stage)

    try:
        from modelctl.core import docker_setup
        rc = docker_setup.run_install(
            registry_mirrors, max_downloads, os_hint=target_os, on_stage=_on_stage,
        )
        if rc == 0:
            task.complete()
        else:
            task.error(exit_code=rc, message=f"docker install 退出码 {rc}")
    except Exception as exc:  # noqa: BLE001 — 后台线程异常统一标记 error
        logger.exception(f"docker install 线程异常: {exc}")
        task.error(exit_code=1, message=f"异常: {exc}")
    finally:
        # 精确清去重窗：只清匹配 task_id 的条目
        info = _user_active_installs.get(user_id)
        if info and info.get("task_id") == task.id:
            _user_active_installs.pop(user_id, None)
        # 精确清 pending 集合
        pending = _user_pending.get(user_id)
        if pending:
            pending.discard(task.id)
            if not pending:
                _user_pending.pop(user_id, None)


@router.get("/docker/install/{task_id}/events")
async def docker_install_events(
    task_id: str,
    key: str = Query(default=""),
    _: None = Depends(require_auth_or_query),
):
    """GET /admin/api/envs/docker/install/{task_id}/events — SSE 流。

    复用 ``admin_router._sse_task_stream`` 同款模式：先 flush 已有 logs + 当前
    status，再实时广播；10s 心跳保活；收到 ``done`` 立即结束。
    """
    task = docker_install_task_manager.get_task(task_id)
    if task is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"install task {task_id} 不存在或已过期"}},
        )
    from modelctl.core.webui.admin_router import _sse_task_stream

    return StreamingResponse(
        _sse_task_stream(task),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/docker/install/{task_id}")
async def docker_install_status(
    task_id: str,
    _: None = Depends(require_auth),
):
    """GET /admin/api/envs/docker/install/{task_id} — 只读状态（无 logs 全文）。

    SSE 断开后前端重连时的 fallback：仅返回 stage/done/last_ts 三字段，
    不返回 logs 避免长 payload。
    """
    task = docker_install_task_manager.get_task(task_id)
    if task is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"install task {task_id} 不存在或已过期"}},
        )
    last_ts = task.finished_at or task.started_at or ""
    # stage 从 detail 里还原（后台线程用 task.update_detail(stage) 写入）
    return {
        "task_id": task.id,
        "stage": task.detail if isinstance(task.detail, str) else "unknown",
        "done": task.status in ("success", "error"),
        "status": task.status,
        "last_ts": last_ts,
    }


@router.post("/docker/system-action")
async def docker_system_action(
    request: Request,
    _: None = Depends(require_auth),
):
    """POST /admin/api/envs/docker/system-action — 引导卡片触发的系统动作（Windows-only）。

    仅识别 3 个 action：
    - ``open_desktop`` → ShellExecuteW explorer.exe ms-settings:developers
    - ``restart``      → subprocess.Popen shutdown.exe /r /t 5
    - ``verify``       → 405（永远走 GET /docker/diagnose 只读，禁止 POST 触发探测）

    Windows-only：非 win32 一律 400 platform_mismatch。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    action = body.get("action")
    if action not in ("open_desktop", "restart", "verify"):
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "bad_action",
                                "message": "action 必须为 open_desktop|restart|verify"}},
        )
    if action == "verify":
        return JSONResponse(
            status_code=405,
            content={"error": {"code": "method_not_allowed",
                                "message": "verify 请使用 GET /admin/api/envs/docker/diagnose?os=windows"}},
        )
    if sys.platform != "win32":
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "platform_mismatch",
                                "message": f"{action} 仅 Windows 主机可用（当前 {sys.platform!r}）"}},
        )
    from modelctl.core.windows_setup import _ts_now as _ts_now_win

    if action == "open_desktop":
        ctypes.windll.shell32.ShellExecuteW(
            None, "open", "explorer.exe", "ms-settings:developers", None, 1
        )
        executed = "explorer.exe ms-settings:developers"
    elif action == "restart":
        subprocess.Popen(
            ["shutdown.exe", "/r", "/t", "5", "/c", "modelctl 正在重启以启用 WSL2 backend"],
            close_fds=True,
        )
        executed = "shutdown.exe /r /t 5"
    else:  # pragma: no cover — defensive
        return JSONResponse(status_code=400,
                            content={"error": {"code": "bad_action", "message": f"未知 action: {action!r}"}})
    return {"executed": executed, "ts": _ts_now_win()}


@router.post("/{target}/setup")
async def setup_env(
    target: str,
    request: Request,
    _: None = Depends(require_auth),
):
    """POST /admin/api/envs/{target}/setup — 异步建 venv，返回 202 + task_id。"""
    from modelctl.core.envs import known_targets
    from modelctl.core.envfile import load_env

    load_env()

    is_known = await asyncio.to_thread(lambda: target in known_targets())
    if not is_known:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"target {target} 不受管"}},
        )

    tm: object = request.app.state.task_manager
    lock = await tm.acquire(target, "setup")
    if lock is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "task_conflict", "message": f"target {target} 已有进行中的任务"}},
        )

    try:
        task = tm.create_task(kind="env_setup", action="setup", target=target)
        task.update_status("queued")
        # 锁所有权移交 worker：spawn 的 runner 结束后才 release
        tm.spawn(target, "setup", lambda: _do_env_setup(target, task))
        return JSONResponse(
            status_code=202,
            content={"task_id": task.id, "stream_url": f"/admin/api/tasks/{task.id}/stream"},
        )
    except Exception:
        await tm.release(target, "setup")
        raise


@router.post("/{target}/remove")
async def remove_env(
    target: str,
    _: None = Depends(require_auth),
):
    """POST /admin/api/envs/{target}/remove — 同步删除 venv，返回 ok/detail。"""
    from modelctl.core import envs

    is_known = await asyncio.to_thread(lambda: target in envs.known_targets())
    if not is_known:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"target {target} 不受管"}},
        )

    try:
        await asyncio.to_thread(envs.remove, target)
        return {"ok": True, "detail": f"已移除 {target} 环境"}
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "bad_target", "message": str(exc)}},
        )
    except Exception as exc:  # noqa: BLE001 — 删除失败统一 500
        logger.exception(f"环境移除异常 ({target}): {exc}")
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal", "message": f"移除失败: {exc}"}},
        )


# ---------------------------------------------------------------------------
# 工作线程
# ---------------------------------------------------------------------------


async def _do_env_setup(target: str, task) -> None:
    """在 worker 线程中执行环境安装，完成后更新 task。"""
    from modelctl.core.envs import setup as envs_setup

    task.update_status("running")
    try:
        await asyncio.to_thread(envs_setup, target)
        task.complete()
    except Exception as exc:
        logger.exception(f"环境安装异常 ({target}): {exc}")
        task.error(exit_code=1, message=str(exc))
