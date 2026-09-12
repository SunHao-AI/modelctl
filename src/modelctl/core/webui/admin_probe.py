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
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from loguru import logger

from modelctl.core.webui.admin_auth import mask_key, require_auth

router = APIRouter()


def _router() -> APIRouter:
    """子路由工厂：返回 APIRouter（主路由 include 时由其调用）。"""
    return router


# ---------------------------------------------------------------------------
# 辅助函数（helper 放在端点之前，避免"先使用后定义"）
# ---------------------------------------------------------------------------


def _vram_gb(mb) -> float:
    """MB → GB（mb / 1024，保留 1 位）；异常 / 未命中返回 0.0。

    用 float() 而非 int(str(x))：上游经 JSON 反序列化后 mb 可能是原生 float
    （如 24576.7），int("24576.7") 抛 ValueError 会走兜底 0.0，UI 显存静默归零
    （BUG-PRB-01）。float 对 int / 整数字符串 / 浮点字符串均兼容。
    """
    try:
        return round(float(str(mb).strip()) / 1024, 1)
    except Exception:  # noqa: BLE001 — value 可能非数字（如 ''），统一兜底 0.0
        return 0.0


def _serialize_gpu_locks(locks: dict[int, str]) -> list[dict]:
    """list_gpu_locks() 的 {gpu_index: owner} → 结构稳定的 list（便于 JSON 序列化）。"""
    return [
        {"gpu_index": int(idx), "owner": owner}
        for idx, owner in sorted(locks.items())
    ]


def _engine_binary_entry(name: str, available: bool, path: str | None, *, dready: bool | None = None) -> dict:
    """单个引擎可达性条目（docker ∨ venv），并标出可达来源（runtime）。

    available（venv）按 caps.binaries 原值；reachable 加 docker 主路径维度——
    与 _resolve_runtime 的 docker_image > venv 规则对齐：docker_ready() 为真且
    引擎在 DOCKER_CAPABLE_ENGINES 中即可达标，避免 yaml 配 docker_image 的
    模型被 UI 误报 "需先 modelctl env setup <engine>"。

    runtime 把「可达」拆成来源，前端据此区分展示：venv 已装 ≠ 仅有 docker 旁路。
    dready 可由调用方传入以复用单次探测（overview 3s 轮询避免逐引擎重复 which）。
    """
    from modelctl.core.capabilities import docker_ready
    from modelctl.core.envs import DOCKER_CAPABLE_ENGINES

    venv_ok = bool(available)
    docker_capable = name in DOCKER_CAPABLE_ENGINES
    if dready is None:
        dready = docker_ready()
    reachable = venv_ok or (docker_capable and dready)
    return {
        "name": name,
        "available": venv_ok,           # venv 可执行性（保持现状，向后兼容）
        "path": path,
        "reachable": reachable,         # docker ∨ venv：真实可达性
        "runtime": "docker" if (reachable and not venv_ok) else ("venv" if venv_ok else None),
        "docker_capable": docker_capable,
        "docker_ready": bool(dready),
    }


def _serialize_engine_binaries(caps, *, dready: bool | None = None) -> list[dict]:
    """Capabilities.binaries（bool）+ binary_paths（str|None）→ 统一列表。"""
    binaries = getattr(caps, "binaries", None) or {}
    paths = getattr(caps, "binary_paths", None) or {}
    out = []
    for name, available in binaries.items():
        out.append(_engine_binary_entry(name, available, paths.get(name), dready=dready))
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


# 探测大池已收敛到 `admin_models.probe_executor()`（webui 进程内单例，64 worker）：
# 本端点与 /admin/api/models 共用同一个池，冷路径都真正并发、热路径都命中 TTL 缓存。


@router.get("/overview")
async def overview(request: Request, _: None = Depends(require_auth)):
    """GET /admin/api/overview — 3s 轮询聚合端点（前端按 3s 拉一次）。

    聚合三层数据一次返回：
    - hardware：probe() 的 GPU/显存/引擎二进制（nvidia-smi 探测放 to_thread 里）
    - models：list_profiles + _model_summary（复用 admin_models 的列表形态）
    - services：stats / gateway 的 is_running + port（GATEWAY_PORT、USAGE_PORT）
    与探测端点的 source of truth 一致（GATEWAY_DEFAULT_MODEL / GATEWAY_PORT /
    USAGE_PORT）。本端点 3s 轮询，因此 probe() 调用频率受控（不大、可接受）。

    性能 + 并发安全说明（2026-09-09 二次实测后）：
    - 旧方案（51 × _model_summary + probe + is_running×2 全凑一次 gather，走默认
      ThreadPoolExecutor max_workers≈13）实测在 uvicorn 进程内单请求 25-180s
      才返回（前端 30s axios 超时 abort → 控制台 net::ERR_ABORTED 反复出现）。
      本地脚本同一代码只 15s——归结为"uvicorn 共享 loop + Windows 默认线程池
      51 worker 排队 + urllib 2.0s 端口 timeout 不中止"三重叠加。
    - **拆成两波**：
      wave A：list_profiles + probe + is_running×2 一次性 gather（纯 CPU/文件，
              本身 1-2s 内完成，不会抢占 wave B 的端口 worker 槽位）。
      wave B：51 × _model_summary（每个内部 is_running_any 单次 1.5s 端口探测），
              走显式 ThreadPoolExecutor(max_workers=64)，51 worker 真正全部
              并发。wave B 总耗时 ≈ max(单端口 1.5s) ≈ 1.5-3s。
    - 累计单次响应目标：wave A 1-2s + wave B 1.5-3s = 2.5-5s，安全落在前端
      30s axios 超时下方 6x，为 wait_for / 浏览器 cancel 留足余量。
    - **2026-09-10 叠加 TTL 缓存**：wave B 的运行态判定改走 `build_summaries` →
      `probe_availability`，命中 `app.state.group_route_cache` 的 avail 层（默认 5s，
      > 本端点 3s 轮询间隔）。稳态下约每 2 次轮询才真探一轮，wave B 常见耗时 → ~0ms；
      同时该缓存与 `/admin/api/models` 共享，两个视图来回切不再各探一轮。
      **必须保留 64-worker 大池**：缓存只是降低探测频率，未命中的那一轮仍是 51 个
      并发 1.5s 探测，退回默认 13 worker 就又变成上面记录的 4 波排队 ≈ 11s。
    """
    from modelctl.core.capabilities import probe
    from modelctl.core.gateway import GATEWAY_PORT
    from modelctl.core.profile import list_profiles
    from modelctl.core.process import is_running
    from modelctl.core.stats import USAGE_PORT
    from modelctl.core.webui.admin_models import build_summaries, probe_executor

    # wave A：纯 CPU / 文件类探测，无端口阻塞，并行派发。
    wave_a = await asyncio.gather(
        asyncio.to_thread(list_profiles, None),
        asyncio.to_thread(probe),
        asyncio.to_thread(is_running, "stats"),
        asyncio.to_thread(is_running, "gateway"),
    )
    profiles, caps, is_stats, is_gateway = wave_a

    # wave B：运行态判定走共享 TTL 缓存；未命中项显式走 64-worker 大池，让 51 个
    # 端口探测真正全部并发（vs 默认 12 worker 排队波次）。
    summaries = await build_summaries(request, profiles, executor=probe_executor())

    # 总显存：优先 vram_total_mb_per_gpu(list[int]) 求和，否则回退 vram_total_mb
    gpu_count = int(getattr(caps, "gpu_count", 0) or 0)
    gpu_name = getattr(caps, "gpu_name", "") or ""
    total_per_gpu = getattr(caps, "vram_total_mb_per_gpu", None)
    if total_per_gpu:
        total_vram_gb = _vram_gb(sum(total_per_gpu))
    else:
        total_vram_gb = _vram_gb(getattr(caps, "vram_total_mb", 0))

    from modelctl.core.capabilities import docker_ready

    dready = await asyncio.to_thread(docker_ready)
    # 与 /probe 同一形态（含 runtime 来源），前端据此区分「venv 已装」与「仅 docker 旁路」；
    # 旧实现只回 available/missing，docker 旁路会让 venv 未装的引擎在仪表板显示 ✓，
    # 与环境页「未安装」自相矛盾。
    engine_binaries = _serialize_engine_binaries(caps, dready=dready)

    services = {
        "stats": {"state": "running" if is_stats else "stopped", "port": USAGE_PORT},
        "gateway": {"state": "running" if is_gateway else "stopped", "port": GATEWAY_PORT},
    }

    import modelctl as mctl

    return {
        "version": getattr(mctl, "__version__", ""),
        "uptime_s": None,  # 需 async tm.uptime；本同步函数留空由前端覆盖（产品定）
        "default_model": os.environ.get("GATEWAY_DEFAULT_MODEL", ""),
        "gateway_port": GATEWAY_PORT,
        "model_count": len(summaries),
        "hardware": {
            "gpu_count": gpu_count,
            "gpu_name": gpu_name,
            "total_vram_gb": total_vram_gb,
            "docker_ready": dready,
            "engine_binaries": engine_binaries,
        },
        "models": list(summaries),
        "services": services,
        # 带偏移的 ISO：naive 时间会被前端按浏览器时区二次解释，跨时区即偏差
        "probed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
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
    引擎二进制结构 = [{name, available, path, reachable, runtime,
    docker_capable, docker_ready}]，path 为绝对路径（venv 内或 PATH），
    runtime 标出可达来源（venv / docker / null）。
    """
    import modelctl as mctl

    from modelctl.core.capabilities import probe
    from modelctl.core.envfile import PROJECT_ROOT
    from modelctl.core.gpu_lock import list_gpu_locks
    from modelctl.core.paths import cache_dir

    caps = await asyncio.to_thread(probe)
    gpu_locks = await asyncio.to_thread(list_gpu_locks)

    # 统一走 admin_auth.mask_key：短于 4 位时仅 "***"。原内联
    # `"****" + api_key[-4:]` 对短 key 会输出全串（`"abc"[-4:]==全明文`），
    # 且星号数/空值口径与其它掩码函数不一致（BUG-PRB-02）。
    api_key_masked = mask_key(os.environ.get("API_KEY", ""))

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
            "cache_dir": str(cache_dir()),
            "models_dir": str(PROJECT_ROOT / "models"),
        },
        "version": getattr(mctl, "__version__", ""),
    }
