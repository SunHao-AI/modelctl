#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_probe.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/2 10:00
# @Desc   : 系统概览与硬件体检 API 端点
# ===============================================================================

"""core/webui/admin_probe.py — 系统概览与硬件体检 API 端点。

提供 /login（API_KEY 校验，POST body {key}）、/health（健康检查，无需认证）、
/overview（3s 轮询聚合端点）、/probe（完整硬件体检，五区块）。

/login、/health 不要求认证（网页登录前先探健康、再调 /login 校验 key）；
/overview、/probe 用 Depends(require_auth) 注入鉴权。

依赖 FastAPI（Web UI 复用 gateway 独立 venv 中已安装的 fastapi）+ loguru；
modelctl.core 子模块在函数体内延迟导入（与 admin_models/admin_envs 同款约定）。
"""

from __future__ import annotations

import asyncio
import os
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from loguru import logger

from modelctl.core.webui.admin_auth import require_auth

router = APIRouter()


def _router() -> APIRouter:
    """子路由工厂：返回 APIRouter（主路由 include 时由其调用）。"""
    return router


# ---------------------------------------------------------------------------
# 辅助函数（helper 放在端点之前，避免"先使用后定义"）
# ---------------------------------------------------------------------------


def _vram_gb(mb) -> float:
    """MB → GB（int mb / 1024，保留 1 位）；异常 / 未命中返回 0.0。"""
    try:
        return round(int(str(mb).strip()) / 1024, 1)
    except Exception:  # noqa: BLE001 — value 可能非数字（如 ''），统一兜底 0.0
        return 0.0


def _serialize_gpu_locks(locks: dict[int, str]) -> list[dict]:
    """list_gpu_locks() 的 {gpu_index: owner} → 结构稳定的 list（便于 JSON 序列化）。"""
    return [
        {"gpu_index": int(idx), "owner": owner}
        for idx, owner in sorted(locks.items())
    ]


def _serialize_engine_binaries(caps) -> list[dict]:
    """Capabilities.binaries（bool）+ binary_paths（str|None）→ 统一列表。"""
    binaries = getattr(caps, "binaries", None) or {}
    paths = getattr(caps, "binary_paths", None) or {}
    out = []
    for name, available in binaries.items():
        out.append({
            "name": name,
            "available": bool(available),
            "path": paths.get(name),
        })
    return out


# ---------------------------------------------------------------------------
# 端点（/health、/login 无鉴权；/overview、/probe 需 require_auth）
# ---------------------------------------------------------------------------


@router.post("/login")
async def login(request: Request):
    """POST /admin/api/login — 校验 API_KEY（body: {key | api_key: string}）。

    成功 → 200 {"ok": true}（后端不签发任何登录态；持久化由前端负责）。
    失败 → 401 {"error": {"code": "auth", "message": "认证失败"}}。
    """
    from modelctl.core.envfile import load_env
    from modelctl.core.webui.admin_auth import is_valid_key

    load_env()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — body 非 JSON 时也按空 dict 处理
        body = {}
    key = "" if isinstance(body, dict) else ""
    if isinstance(body, dict):
        key = body.get("key") or body.get("api_key") or ""
    if is_valid_key(key):
        return {"ok": True}
    return JSONResponse(
        status_code=401,
        content={"error": {"code": "auth", "message": "认证失败"}},
    )


@router.get("/health")
async def health(request: Request):
    """GET /admin/api/health — 健康检查（无需认证）。

    网页登录前先探端口在不在（探到 → 页面，探不到 → 报错/安装页）。
    返回：ok / version / uptime_s / default_model / gateway_port。
    """
    import modelctl as mctl

    from modelctl.core.gateway import GATEWAY_PORT

    tm = request.app.state.task_manager
    uptime = round(tm.uptime(), 1) if hasattr(tm, "uptime") and callable(tm.uptime) else 0.0
    return {
        "ok": True,
        "version": getattr(mctl, "__version__", ""),
        "uptime_s": uptime,
        "default_model": os.environ.get("GATEWAY_DEFAULT_MODEL", ""),
        "gateway_port": GATEWAY_PORT,
    }


@router.get("/overview")
async def overview(request: Request, _: None = Depends(require_auth)):
    """GET /admin/api/overview — 3s 轮询聚合端点（前端按 3s 拉一次）。

    聚合三层数据一次返回：
    - hardware：probe() 的 GPU/显存/引擎二进制（nvidia-smi 探测放 to_thread 里）
    - models：list_profiles + _model_summary（复用 admin_models 的列表形态）
    - services：stats / gateway 的 is_running + port（GATEWAY_PORT、USAGE_PORT）
    与探测端点的 source of truth 一致（GATEWAY_DEFAULT_MODEL / GATEWAY_PORT /
    USAGE_PORT）。本端点 3s 轮询，因此 probe() 调用频率受控（不大、可接受）。
    """
    parts = await asyncio.to_thread(_gather_overview)
    return parts


def _gather_overview() -> dict:
    """同步聚合逻辑（在线程中调用）。

    数据来源（与 probe 端点同源）：
    - probe()：GPU 数 / 显存 / 引擎二进制
    - list_gpu_locks()：GPU 占用映射
    - list_profiles(None) + _model_summary：模型列表
    - is_running("stats" / "gateway")：服务状态
    """
    import modelctl as mctl

    from modelctl.core.capabilities import probe
    from modelctl.core.gateway import GATEWAY_PORT
    from modelctl.core.gpu_lock import list_gpu_locks
    from modelctl.core.profile import list_profiles
    from modelctl.core.process import is_running
    from modelctl.core.stats import USAGE_PORT
    from modelctl.core.webui.admin_models import _model_summary

    profiles = list_profiles(None)
    models = [_model_summary(p) for p in profiles]

    caps = probe()
    gpu_count = int(getattr(caps, "gpu_count", 0) or 0)
    gpu_name = getattr(caps, "gpu_name", "") or ""

    # 总显存：优先用 vram_total_mb_per_gpu(list[int]) 求和，否则回退 vram_total_mb
    total_per_gpu = getattr(caps, "vram_total_mb_per_gpu", None)
    if total_per_gpu:
        total_vram_gb = _vram_gb(sum(total_per_gpu))
    else:
        total_vram_gb = _vram_gb(getattr(caps, "vram_total_mb", 0))

    hardware = {
        "gpu_count": gpu_count,
        "gpu_name": gpu_name,
        "total_vram_gb": total_vram_gb,
        "engine_binaries": {
            name: "available" if path else "missing"
            for name, path in (getattr(caps, "binary_paths", None) or {}).items()
        },
    }

    # 服务状态（用 is_running 而非 status_*：本端点 3s 轮询，免每 3s 各做 3s 健康探测）
    services = {}
    for svc, port in (("stats", USAGE_PORT), ("gateway", GATEWAY_PORT)):
        services[svc] = {
            "state": "running" if is_running(svc) else "stopped",
            "port": port,
        }

    return {
        "version": getattr(mctl, "__version__", ""),
        "uptime_s": None,  # 需 async tm.uptime；本同步函数留空由前端覆盖（产品定）
        "default_model": os.environ.get("GATEWAY_DEFAULT_MODEL", ""),
        "gateway_port": GATEWAY_PORT,
        "model_count": len(models),
        "hardware": hardware,
        "models": models,
        "services": services,
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


@router.get("/probe")
async def probe_detail(request: Request, _: None = Depends(require_auth)):
    """GET /admin/api/probe — 完整硬件体检（五区块）。

    区块 1 GPU：数量 / 型号 / 总显存 / 剩余显存 / CUDA 驱动 / 计算能力（CC）
    区块 2 GPU 锁：list_gpu_locks() → {gpu_index: owner_name}
    区块 3 引擎二进制：binaries（bool）合并 binary_paths（str|None）
    区块 4 环境变量：HF_HOME / MODEL_ROOT / MODELSCOPE_CACHE / LOG_DIR / API_KEY(脱敏)
    区块 5 路径与版本：project_root / cache_dir / models_dir / modelctl 版本

    GPU 锁结构 = [{gpu_index, owner}]，便于前端渲染占卡提示；
    引擎二进制结构 = [{name, available, path}]，path 为绝对路径（venv 内或 PATH）。
    """
    import modelctl as mctl

    from modelctl.core.capabilities import probe
    from modelctl.core.envfile import PROJECT_ROOT
    from modelctl.core.gpu_lock import LOCK_DIR, list_gpu_locks

    caps = await asyncio.to_thread(probe)
    gpu_locks = await asyncio.to_thread(list_gpu_locks)

    api_key = os.environ.get("API_KEY", "")
    api_key_masked = ("****" + api_key[-4:]) if api_key else ""

    gpu_count = int(getattr(caps, "gpu_count", 0) or 0)

    # 总显存：优先 vram_total_mb_per_gpu(list[int]) 求和，否则回退 vram_total_mb
    total_per_gpu = getattr(caps, "vram_total_mb_per_gpu", None)
    raw_total_mb = getattr(caps, "vram_total_mb", 0)
    if total_per_gpu:
        vram_total_mb_value = sum(total_per_gpu)
        vram_total_gb = _vram_gb(vram_total_mb_value)
    else:
        vram_total_mb_value = int(raw_total_mb) if isinstance(raw_total_mb, (int, float)) else 0
        vram_total_gb = _vram_gb(raw_total_mb)

    vram_free_mb = getattr(caps, "vram_free_mb", []) or []

    return {
        "gpu_count": gpu_count,
        "gpu_name": getattr(caps, "gpu_name", "") or "",
        "vram_total_mb": int(vram_total_mb_value),
        "vram_total_gb": vram_total_gb,
        "vram_free_mb": list(vram_free_mb) if isinstance(vram_free_mb, list) else [vram_free_mb],
        "cuda_driver": getattr(caps, "cuda_driver", "") or "",
        "compute_capability": getattr(caps, "compute_capability", "") or "",
        "gpu_locks": _serialize_gpu_locks(gpu_locks),
        "engine_binaries": _serialize_engine_binaries(caps),
        "env_vars": {
            "HF_HOME": os.environ.get("HF_HOME", ""),
            "MODEL_ROOT": os.environ.get("MODEL_ROOT", ""),
            "MODELSCOPE_CACHE": os.environ.get("MODELSCOPE_CACHE", ""),
            "LOG_DIR": os.environ.get("LOG_DIR", ""),
            "API_KEY": api_key_masked,
        },
        "paths": {
            "project_root": str(PROJECT_ROOT),
            "cache_dir": str(LOCK_DIR),
            "models_dir": str(PROJECT_ROOT / "models"),
        },
        "version": getattr(mctl, "__version__", ""),
    }
