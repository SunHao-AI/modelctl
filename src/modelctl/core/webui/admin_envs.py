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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from loguru import logger

from modelctl.core.webui.admin_auth import require_auth

router = APIRouter()

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

    status = await asyncio.to_thread(envs.status)
    targets = await asyncio.to_thread(envs.known_targets)

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
            }
        )

    unmanaged = await asyncio.to_thread(_unmanaged_targets)

    # Docker 环境就绪探测：纯 shutil.which，绝不落子进程，不拖慢列表页加载
    from modelctl.core import docker_setup

    missing = docker_setup.path_level_missing()
    docker_env = {"ready": not missing, "missing": missing, "guide": docker_setup.MSG_GUIDE}

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
async def docker_diagnose(_: None = Depends(require_auth)):
    """GET /admin/api/envs/docker/diagnose — Docker 环境完整诊断（只读，按需触发）。

    透传 ``docker_setup.diagnose()``（含 ``docker info`` 子进程，内建 15s 超时，
    故走 to_thread 且仅由前端按钮按需调用）与 ``render_instructions()``（可复制的
    root 安装脚本）。本端点绝不执行任何安装动作——实际安装仍只走既有 CLI 通道
    ``modelctl env setup docker --run``。
    """
    from modelctl.core import docker_setup

    try:
        checks = await asyncio.to_thread(docker_setup.diagnose)
        return {
            "checks": [
                {"key": c.key, "label": c.label, "ok": c.ok, "detail": c.detail} for c in checks
            ],
            "instructions": docker_setup.render_instructions(),
        }
    except Exception as exc:  # noqa: BLE001 — 诊断失败统一 500（与同文件 remove_env 惯例一致）
        logger.exception(f"Docker 诊断异常: {exc}")
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal", "message": f"诊断失败: {exc}"}},
        )


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
        asyncio.ensure_future(_do_env_setup(target, task))
        return JSONResponse(
            status_code=202,
            content={"task_id": task.id, "stream_url": f"/admin/api/tasks/{task.id}/stream"},
        )
    finally:
        await tm.release(target, "setup")


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
