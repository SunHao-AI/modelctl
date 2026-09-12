#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_models.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/2 10:00
# @Desc   : Web UI 模型管理 API 端点
# ===============================================================================

"""core/webui/admin_models.py — Web UI 模型相关 API 端点。

所有端点前缀 ``/admin/api/models``（由调用方 include_router 时设置）。
依赖 FastAPI + ``admin_auth.require_auth`` + ``admin_tasks.TaskManager``（均在
``modelctl.core.webui`` 子包内），故 fastapi 在顶部导入（Web UI 复用 gateway 独立
venv，其中必然装有 fastapi），modelctl.core 子模块在函数体内延迟导入以避免
循环依赖与不必要的模块加载。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from modelctl.core.webui.admin_auth import require_auth, require_auth_or_query

router = APIRouter()


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _find_profile(name: str):
    """按 name 精确匹配 list_profiles 中的 profile；未找到返回 None。"""
    from modelctl.core.profile import list_profiles

    for p in list_profiles(None):
        if p.name == name:
            return p
    return None


def _read_pid_raw(name: str) -> int | None:
    """读取 PID 文件中的 pid（不过滤存活状态），供前端展示诊断。"""
    from modelctl.core.process import pid_file

    pf = pid_file(name)
    if not pf.is_file():
        return None
    try:
        return int(pf.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _mask_key(key: str | None) -> str | None:
    """API key 脱敏：仅暴露末 4 位（***3876）；None / 空原样返回 None（前端据此显示"未配置"）。

    短于等于 4 位时整体掩成 "***"：`key[-4:]` 对短串等于**全文明**，旧实现
    `f"***{key}"` 会输出 `***abc` 这种"星号打头反而更像已脱敏"的全串，比直出明文
    更具迷惑性（同 admin_auth.mask_key / admin_config._mask_value 口径，见
    known-pitfalls/backend/webui-边界与脱敏.md）。
    """
    if not key:
        return None
    if len(key) <= 4:
        return "***"
    return f"***{key[-4:]}"


def _vision_capability(p) -> bool | None:
    """尽力推断视觉（多模态）能力——profile 层没有统一字段，只能按引擎猜。

    仅 llamacpp 在 `engine_config.vision` 显式声明（见 engines/llamacpp.py 的
    mmproj 判定，on/true/1 为开）。返回 None 表示"该引擎无权威字段"：前端据此
    **不显示徽章但绝不禁用图片按钮**——把猜测当门禁会误杀 vLLM 等已起视觉模型
    但 profile 未声明的情况。
    """
    if p.engine == "llamacpp":
        raw = str(p.engine_config.get("vision", "")).lower()
        if raw in ("on", "true", "1"):
            return True
        if raw in ("off", "false", "0"):
            return False
    return None


def _model_summary(p, running: bool | None = None) -> dict:
    """构建 ModelSummary dict（列表端点用）。

    `running`：调用方已判定的运行态（批量探测 + TTL 缓存路径传入）。传入时**不再**
    自探一次——51 个 profile 的列表端点若逐个再 `is_running_any`，缓存就白接了。
    传 None（CLI / 单条查询等单次调用口径）时保持原行为：自行探测。
    """
    if running is None:
        running = False
        try:
            # 运行态判定改走 is_running_any：docker runtime 容器路径不写 PID 文件，
            # 仅 is_running(name) 会把 docker 启动的 vllm/tokenspeed/tensorrt 模型误标"已停止"
            # （端口 /health 2xx 无法被探测）。is_running_any 端口探测优先 + PID 文件机器兜底，
            # 与 CLI list/status / all status 行为一致。
            from modelctl.core.process import is_running_any

            running = is_running_any(p.name, p)
        except Exception:
            running = False

    # health 口径：
    #  - running 命中过（/health 2xx 或 PID 文件） → healthy；
    #  - running=False → None（未运行 profile 不要为它多耗一次 /health 探测——
    #    docker 场景下 N 个未启动 profile 串行各 2.3s 会让列表整体 >15s 直接超时）。
    # 注：之前 _check_health 二次探测仅用于区分 healthy/unhealthy；改为统一 healthy。
    # unhealthy 基础指标由前端按需直查 /health 维护，避免列表路径上的空转开销。
    health = "healthy" if running else None

    pid = _read_pid_raw(p.name)
    log_path: str | None = None
    try:
        from modelctl.core.process import launch_log

        lp = launch_log(p.name)
        if lp is not None:
            log_path = str(lp)
    except Exception:
        pass

    return {
        "name": p.name,
        "group": p.group,
        "engine": p.engine,
        "variant": p.variant,
        "port": p.port,
        "aliases": list(p.aliases),
        "state": "running" if running else "stopped",
        "health": health,
        "rates": None,  # TODO: from stats later
        "api_key_masked": _mask_key(p.api_key),
        "pid": pid,
        "log_path": log_path,
        "vision": _vision_capability(p),
    }


def _fail_task(task, exit_code: int, message: str, engine: str) -> None:
    """失败收尾统一入口：附启动失败分类码，任务抽屉据此渲染修复动作。

    分类只认 venv_missing/docker_missing（reconcile.classify_start_failure），
    其余失败 code/engine 均为 None——宁缺毋滥。
    """
    from modelctl.core.cluster.reconcile import classify_start_failure

    code, eng = classify_start_failure(message, engine)
    task.error(exit_code=exit_code, message=message, code=code, engine=eng)


def _stage_event_bridge(task, loop: asyncio.AbstractEventLoop):
    """构建 stage 事件桥接回调（start/restart 共用），把进度事件安全投递到事件循环线程。

    `on_progress` 由 worker 线程（`asyncio.to_thread`）与 LoadingWatcher daemon 线程调用，
    而 `Task.event` 依赖当前线程的事件循环——工作线程里没有，直接调用会抛 RuntimeError
    并被其内部 except 静默吞掉，浏览器永远收不到 `stage` 帧。故此处以调用方在**事件循环
    线程**捕获的 `loop` 经 `call_soon_threadsafe` 派发；loop 已关闭时降级 debug 日志
    （进度尽力而为，绝不影响启动）。
    """
    def _on_stage(ev) -> None:
        """阶段事件 → task SSE（与 docker 一键安装同一事件基建，_sse_task_stream 零改动透传）。"""
        payload = {
            "stage": ev.stage,
            "status": ev.status,
            "label": ev.label,
            "pct": ev.pct,
            "etaSeconds": ev.eta_s,
            "error": ev.error,
            "task_id": task.id,
        }
        pct = "" if ev.pct is None else f" {round(ev.pct * 100)}%"
        detail = f"{ev.label}{pct}"

        def _dispatch() -> None:
            task.event("stage", payload)
            task.update_detail(detail)

        try:
            loop.call_soon_threadsafe(_dispatch)
        except (RuntimeError, TypeError) as exc:
            logger.debug(f"stage 事件投递失败（循环已关闭，忽略）：{exc}")

    return _on_stage


async def _do_start(profile, caps, timeout: float, task, gpus: str | None) -> None:
    """在 worker 线程中执行启动，完成后更新 task。"""
    from modelctl.core.all_service import start_profile
    from modelctl.core.gpu_utils import resolve_gpu_list

    # 进入 to_thread 前仍在事件循环线程：此处取到的 loop 才能供跨线程派发
    loop = asyncio.get_running_loop()
    _on_stage = _stage_event_bridge(task, loop)

    # gpus 逗号字符串 → 显式参数透传（不再写 os.environ["MODELCTL_GPUS"]：
    # 全局态在并发 to_thread 任务间互相覆盖，导致 GPU 分配错乱）
    gpu_list = resolve_gpu_list(None, None, gpus) if gpus else None

    try:
        task.update_status("running")
        result = await asyncio.to_thread(
            lambda: start_profile(profile, caps, timeout, on_progress=_on_stage, gpus=gpu_list)
        )
        task.update_detail(result.detail)
        if result.status == "error":
            _fail_task(task, 1, result.detail, profile.engine)
        else:
            task.complete()
    except Exception as exc:
        logger.exception(f"启动任务异常 ({profile.name}): {exc}")
        try:
            from modelctl.engines.base import RequirementError

            if isinstance(exc, RequirementError):
                _fail_task(task, 2, str(exc), profile.engine)
            else:
                _fail_task(task, 1, str(exc), profile.engine)
        except ImportError:
            _fail_task(task, 1, str(exc), profile.engine)


async def _do_restart(profile, caps, timeout: float, task, gpus: str | None) -> None:
    """在 worker 线程中执行重启，完成后更新 task。"""
    from modelctl.core.all_service import restart_profile
    from modelctl.core.gpu_utils import resolve_gpu_list

    # 与 _do_start 同构：事件循环线程捕获 loop，restart 复用 start 的阶段序列
    loop = asyncio.get_running_loop()
    _on_stage = _stage_event_bridge(task, loop)

    # 与 _do_start 同构：显式参数透传，不写全局 MODELCTL_GPUS（并发互污）
    gpu_list = resolve_gpu_list(None, None, gpus) if gpus else None

    try:
        task.update_status("running")
        result = await asyncio.to_thread(
            lambda: restart_profile(profile, caps, timeout, on_progress=_on_stage, gpus=gpu_list)
        )
        task.update_detail(result.detail)
        if result.status == "error":
            _fail_task(task, 1, result.detail, profile.engine)
        else:
            task.complete()
    except Exception as exc:
        logger.exception(f"重启任务异常 ({profile.name}): {exc}")
        try:
            from modelctl.engines.base import RequirementError

            if isinstance(exc, RequirementError):
                _fail_task(task, 2, str(exc), profile.engine)
            else:
                _fail_task(task, 1, str(exc), profile.engine)
        except ImportError:
            _fail_task(task, 1, str(exc), profile.engine)


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


# 共享大池：N 个 profile 并发探测时若走 `asyncio.to_thread` 默认池，本机 cpu=8 →
# min(32, 8+4)=12 worker，51 个端口探测排成 4 波 → 冷路径实测 10-18s。
# 两个列表端点（/admin/api/models 与 /admin/api/overview）在 webui 同一进程内
# 共用这个 64-worker 池：冷的那一轮 51 个探测真正全部并发（≈ max 单个探测），
# 热的那一轮直接命中 TTL 缓存（≈0ms）。池是线程安全的、复用线程，单一实例即可。
_PROBE_EXECUTOR: "ThreadPoolExecutor | None" = None


def probe_executor() -> "ThreadPoolExecutor":
    """返回 webui 进程内共享的探测线程池（懒初始化，64 worker）。"""
    global _PROBE_EXECUTOR
    if _PROBE_EXECUTOR is None:
        _PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=64, thread_name_prefix="webui-probe")
    return _PROBE_EXECUTOR


async def build_summaries(
    request: Request, profiles: list, executor: ThreadPoolExecutor | None = None
) -> list[dict]:
    """批量构建 ModelSummary：运行态走**进程内共享**的 TTL 缓存探测。

    `/admin/api/models` 与 `/admin/api/overview` 共用本函数 + 同一个
    `app.state.group_route_cache`（webui 与 gateway 是两个进程，各自一份缓存；这里是
    webui 进程内两处共享）。overview 3s 轮询填的缓存，列表请求直接命中，反之亦然。

    `executor`：调用方应传 `probe_executor()` 共享大池（51 个 profile 真并发）；
    None 退回 `asyncio.to_thread` 默认池——本机仅 12 worker，冷路径会排 4 波。

    `is_running_any` 在**函数体内**导入：既有测试 patch
    `modelctl.core.process.is_running_any`（模块属性），延迟导入才能在调用时取到桩；
    模块级 `from ... import` 会绑死原始函数、让桩静默失效（探测真跑 → 恒 False）。
    """
    from modelctl.core.gateway import probe_availability
    from modelctl.core.process import is_running_any

    cache = request.app.state.group_route_cache
    by_name = {p.name: p for p in profiles}
    available = await probe_availability(
        cache, list(by_name), lambda n: is_running_any(n, by_name[n]), executor=executor
    )
    return [_model_summary(p, available[p.name]) for p in profiles]


@router.get("")
async def list_models(request: Request, _: None = Depends(require_auth)):
    """GET /admin/api/models — 按家族分组列出所有模型。"""
    from modelctl.core.gateway import ENGINE_PRIORITY
    from modelctl.core.profile import list_profiles

    profiles = list_profiles(None)

    # 按 group 分组，保持首次出现顺序；未声明 group 的归入 "(其它)"
    groups: dict[str, list] = {}
    for p in profiles:
        g = p.group or "(其它)"
        groups.setdefault(g, []).append(p)

    # 组内按引擎优先级排序
    for members in groups.values():
        members.sort(key=lambda m: ENGINE_PRIORITY.get(m.engine, 99))

    # 构建响应：模型状态判定（端口 /health 探测 + PID 文件）走 worker 线程，
    # 全局 gather 让所有 is_running_any 的端口探测同时跑：51 个 profile 串行
    # 2.3s 直接超时。**必须显式走 probe_executor() 共享大池**——退回默认池
    # （本机 12 worker）就变成 4 波排队，冷路径实测 10-18s（与 overview 同源问题）。
    # 注：list_exceptions=True 把单个 _model_summary 异常映射为 stopped + health None，
    # 避免一个模型异常拖垮整个列表。
    groups_sorted = list(sorted(groups.items(), key=lambda x: (x[0] == "(其它)", x[0])))
    flat_profiles = [m for _, members in groups_sorted for m in members]
    summaries = await build_summaries(request, flat_profiles, executor=probe_executor())
    # 重组成 groups 形态：按原 group 切分，保持 group 顺序
    out_groups = []
    cursor = 0
    for g, members in groups_sorted:
        n = len(members)
        out_groups.append({
            "group": g,
            "models": list(summaries[cursor: cursor + n]),
        })
        cursor += n

    return {
        "groups": out_groups,
        "default_model": os.environ.get("GATEWAY_DEFAULT_MODEL", ""),
    }


@router.get("/{name}")
async def get_model(name: str, _: None = Depends(require_auth)):
    """GET /admin/api/models/{name} — 单模型详情。"""
    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )

    running = False
    try:
        # 与 _model_summary 同口径：is_running_any（端口探测优先 + PID 文件机器兜底），
        # 避免 docker runtime 容器路径无 PID 文件导致 detail 页"已停止"误显。
        from modelctl.core.process import is_running_any

        running = await asyncio.to_thread(is_running_any, profile.name, profile)
    except Exception:
        running = False

    # 与 _model_summary 同口径：is_running_any 命中即视为 service 存活 → healthy，
    # 避免再次 _check_health 二次探测（与列表路径一致）。
    health = "healthy" if running else None

    engine_config = profile.engine_config
    # 脱敏 engine_config 中可能存在的 api_key
    ec_out = dict(engine_config)
    if "api_key" in ec_out and isinstance(ec_out["api_key"], str):
        ec_out["api_key"] = _mask_key(ec_out["api_key"])

    # 读取启动日志路径
    launch_log_path: str | None = None
    try:
        from modelctl.core.process import launch_log

        lp = launch_log(profile.name)
        if lp is not None:
            launch_log_path = str(lp)
    except Exception:
        pass

    return {
        "name": profile.name,
        "group": profile.group,
        "engine": profile.engine,
        "variant": profile.variant,
        "port": profile.port,
        "aliases": list(profile.aliases),
        "state": "running" if running else "stopped",
        "health": health,
        "rates": None,
        "api_key_masked": _mask_key(profile.api_key),
        "pid": _read_pid_raw(profile.name),
        "log_path": launch_log_path,
        "engine_config": ec_out,
        "model_path": ec_out.get("model"),
        "tool_call_rounds": profile.tool_call_rounds,
        "max_output_tokens": profile.max_output_tokens,
        "usage": profile.usage,
        "thinking_disabled": profile.thinking_disabled,
    }


@router.post("/{name}/start")
async def start_model(
    name: str,
    request: Request,
    timeout: float | None = Query(default=None, ge=1, le=7200),
    gpus: str | None = Query(default=None),
    yaml_override: str | None = Query(default=None, max_length=1_000_000),
    _: None = Depends(require_auth),
):
    """POST /admin/api/models/{name}/start — 异步启动模型，返回 202 + task_id。

    `yaml_override`：可选查询参数，YAML 文本（URL-encoder 后由 `client.post` 的 params 注入）。
    指定时用**临时** profile（内存构造，源 yaml 文件不改）执行启动——WebUI "个性化
    启动" 入口。校验失败（YAML 语法 / 字段缺失）→ 400；成功 → 202 + task_id，与默认
    启动同一套任务流 / SSE / 进度卡片。
    """
    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )

    # 确保 .env 已加载（API_KEY 等环境变量可用）
    from modelctl.core.envfile import load_env

    load_env()

    # 个性化启动：override 文本 → 临时 profile（源文件不变）
    if yaml_override is not None and yaml_override.strip():
        tmp_profile = await asyncio.to_thread(_build_profile_from_override, name, profile, yaml_override)
        if isinstance(tmp_profile, JSONResponse):
            return tmp_profile
        profile = tmp_profile

    tm: object = request.app.state.task_manager
    lock = await tm.acquire(name, "start")
    if lock is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "task_conflict", "message": f"模型 {name} 已有进行中的任务"}},
        )

    try:
        caps = await asyncio.to_thread(_probe_caps)
        from modelctl.core.all_service import default_start_timeout

        # timeout 缺省 → 按 profile/运行时自适应（docker 1800s，其余 600s）
        eff_timeout = timeout if timeout is not None else await asyncio.to_thread(
            default_start_timeout, profile, caps
        )
        task = tm.create_task(kind="model_start", action="start", target=name)
        task.update_status("queued")
        # 锁所有权移交 worker：spawn 的 runner 结束后才 release（旧 finally 写法
        # 在任务刚投递就解锁，同 target 可被并发重复投递）
        tm.spawn(name, "start", lambda: _do_start(profile, caps, eff_timeout, task, gpus))
        return JSONResponse(
            status_code=202,
            content={"task_id": task.id, "stream_url": f"/admin/api/tasks/{task.id}/stream"},
        )
    except Exception:
        # 仅在投递前（探测/超时计算/建任务）失败时释放；spawn 后由 worker 释放
        await tm.release(name, "start")
        raise


def _build_profile_from_override(name: str, base_profile, yaml_text: str):
    """override 文本 → 临时 Profile（源文件不改）。

    返回新 Profile；解析 / 校验失败时返回 400 JSONResponse（调用方短路返回）。
    强制约束：
      - ``engine`` 必须与 ``base_profile.engine`` 一致（防 override 改写引擎逃逸适配路径）
      - ``port`` 必须与 ``base_profile.port`` 一致（防同 profile 多端口并存造成路由歧义）
    """
    from modelctl.core.profile import ProfileError, load_profile_from_text

    if base_profile.path is None:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "bad_request", "message": "模型源文件未定位，无法执行 override 启动"}},
        )
    try:
        tmp = load_profile_from_text(yaml_text, base_profile.path)
    except ProfileError as exc:
        logger.warning(f"override 启动解析失败（{name}）：{exc}")
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "bad_request", "message": f"YAML 校验失败：{exc}"}},
        )

    if tmp.engine != base_profile.engine:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "bad_request",
                    "message": f"engine 不可改（override={tmp.engine!r}，源={base_profile.engine!r}）",
                },
            },
        )
    if tmp.port != base_profile.port:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "bad_request",
                    "message": f"port 不可改（override={tmp.port}，源={base_profile.port}）",
                },
            },
        )
    # name 强制对齐：override 里手写 name 与路由 name 不一致时按路由 name 锚定，
    # 否则 PID / gpu lock / 日志 / 网关路由都会分叉
    tmp.name = base_profile.name
    # path 保留源路径，仅供 _resolve_engine 兜底 / yaml GET 端点使用；profile 是临时态，
    # 后续 start_profile 链只看 name/engine/port/engine_config 等运行时字段
    tmp.path = base_profile.path
    return tmp


def _probe_caps():
    """探测硬件能力（在线程中调用，避免阻塞事件循环）。"""
    from modelctl.core.capabilities import probe

    return probe()


@router.post("/{name}/stop")
async def stop_model(name: str, request: Request, _: None = Depends(require_auth)):
    """POST /admin/api/models/{name}/stop — 同步停止模型。"""
    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )

    from modelctl.core.all_service import stop_profile
    from modelctl.core.capabilities import probe

    caps = await asyncio.to_thread(probe)
    result = await asyncio.to_thread(stop_profile, profile, caps, None)
    # 运行态判定立即失效：stop 是同步的，缓存里的 True 会让界面在用户点了"停止"之后
    # 仍显示 running 达 avail TTL（5s）——可感知的产品回归。
    # start / restart 不需要同步失效：它们是 202 异步任务，引擎起来本身远超 TTL。
    request.app.state.group_route_cache.invalidate_model(profile.name)
    return {"ok": result.status != "error", "detail": result.detail}


@router.post("/{name}/restart")
async def restart_model(
    name: str,
    request: Request,
    timeout: float | None = Query(default=None, ge=1, le=7200),
    gpus: str | None = Query(default=None),
    _: None = Depends(require_auth),
):
    """POST /admin/api/models/{name}/restart — 异步重启模型，返回 202 + task_id。"""
    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )

    from modelctl.core.envfile import load_env

    load_env()

    tm: object = request.app.state.task_manager
    lock = await tm.acquire(name, "restart")
    if lock is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "task_conflict", "message": f"模型 {name} 已有进行中的任务"}},
        )

    try:
        caps = await asyncio.to_thread(_probe_caps)
        from modelctl.core.all_service import default_start_timeout

        # timeout 缺省 → 按 profile/运行时自适应（docker 1800s，其余 600s）
        eff_timeout = timeout if timeout is not None else await asyncio.to_thread(
            default_start_timeout, profile, caps
        )
        task = tm.create_task(kind="model_restart", action="restart", target=name)
        task.update_status("queued")
        # 锁所有权移交 worker：spawn 的 runner 结束后才 release
        tm.spawn(name, "restart", lambda: _do_restart(profile, caps, eff_timeout, task, gpus))
        return JSONResponse(
            status_code=202,
            content={"task_id": task.id, "stream_url": f"/admin/api/tasks/{task.id}/stream"},
        )
    except Exception:
        await tm.release(name, "restart")
        raise


@router.get("/{name}/startup")
async def get_startup_progress(name: str, _: None = Depends(require_auth)):
    """GET /admin/api/models/{name}/startup — 最近一次启动的阶段进度快照。

    数据源为 all_service 每次阶段事件覆写的 `cache/<name>.startup.json`（尽力而为，
    写失败则无快照 → 404）。非发起者浏览器 / 页面刷新后据此渲染进度卡片，不依赖 SSE 时序。
    profile 存在性优先于文件：快照存在但 profile 已删 → 404（设计 §5）；name 先做
    路径安全校验，拒绝 `..`/分隔符等越目录拼接。
    """
    from modelctl.core.paths import cache_dir
    from modelctl.core.startup_progress import STAGES

    # name 参与文件路径拼接：白名单字符（禁分隔符）+ 拒绝 `..`，防目录穿越（如 ..%2F..%2Fetc）
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or ".." in name:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )
    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )

    path = cache_dir() / f"{name}.startup.json"
    if not path.is_file():
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 暂无启动进度记录"}},
        )
    try:
        raw = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # ValueError 覆盖 JSONDecodeError 与 UnicodeDecodeError（快照半写/编码异常）
        logger.warning(f"启动进度快照不可读（{path}）：{exc}")
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 启动进度快照损坏"}},
        )
    if not isinstance(raw, dict):
        logger.warning(f"启动进度快照结构异常（{path}）：非对象 JSON")
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 启动进度快照损坏"}},
        )
    stages = []
    for s in raw.get("stages") or []:
        if not isinstance(s, dict):
            continue
        stages.append({
            "stage": s.get("stage"),
            "status": s.get("status", "pending"),
            "label": s.get("label") or "",
            "pct": s.get("pct"),
            "etaSeconds": s.get("eta_s"),
            "error": s.get("error"),
            "startedAt": s.get("started_at"),
            "finishedAt": s.get("finished_at"),
        })
    return {
        "profile": raw.get("profile") or name,
        "engine": raw.get("engine") or "",
        "runtime": raw.get("runtime") or "",
        "updatedAt": raw.get("updated_at") or "",
        "stages": stages,
        "knownStages": list(STAGES),
    }


@router.get("/{name}/log")
async def get_model_log(name: str, _: None = Depends(require_auth), lines: int = Query(default=200, ge=1)):
    """GET /admin/api/models/{name}/log — 读取启动日志尾部；docker runtime 回退容器内日志。

    docker runtime 下 launch log 只有一行容器 ID + 镜像名（``docker run --detach`` 客户端
    stdout），真正的 vLLM 内部日志在容器内；本端点在 launch log 缺失 / 仅为容器 ID 行时
    fallback 到 ``docker logs --tail <lines> <container>``，让前端 ``<pre>`` 看到真实日志。
    """
    launch = await asyncio.to_thread(_tail_from_launch_log, name, lines)
    if _launch_log_effective(launch):
        return {"lines": launch[:lines]}
    docker = await asyncio.to_thread(_read_docker_logs, name, lines)
    if docker:
        return {"lines": docker[:lines]}
    return {"lines": launch[:lines]}


def _launch_log_effective(lines: list[str]) -> bool:
    """判断 launch log 行是否"有效内容"（容器 ID 行 / "vllm" 短词不算）。

    与 all_service.docker_logs_fallback 同样的判定：仅当存在 ≥1 行非容器 ID 短词时，
    才认为 launch log 含有效引擎日志；否则回退 ``docker logs``。
    """
    from modelctl.core.all_service import _CONTAINER_ID_LINE

    real = [ln.strip() for ln in lines
            if ln.strip() and not _CONTAINER_ID_LINE.match(ln.strip()) and ln.strip() != "vllm"]
    return len(real) >= 1


@router.get("/{name}/log/stream")
async def stream_model_log(
    name: str,
    key: str = Query(default=""),
    _: None = Depends(require_auth_or_query),
):
    """GET /admin/api/models/{name}/log/stream — SSE 实时日志尾随。

    EventSource 无法携带 Authorization header，前端按 ``?key=`` 传 token，
    鉴权依赖 ``require_auth_or_query`` 与 Bearer 同等强度。
    """
    from modelctl.core.process import launch_log, tail_file

    return StreamingResponse(
        _sse_log_stream(name),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _sse_log_stream(name: str):
    """SSE 日志 async 流：先推送已有行，之后每 2s 轮询新行，每 10s 心跳。

    - venv runtime：``launch-<name>.log`` 持续写，按文件 offset 推送新增行。
    - docker runtime：``launch-<name>.log`` 只有一行 ``docker run --detach`` 客户端容器
      ID + 镜像名（无有效引擎日志），改用 ``/var/lib/docker/containers/<id>/<id>-json.log``
      的 in-container json.line 日志路径按字节 offset 增量推。这种 docker 日志格式每行
      JSON 一行，``split('\\n')`` 解析安全；容器删 / docker daemon 重启时文件消失，
      回退到 ``docker logs --tail 1`` 识别容器是否还在打日志（容器已死则不再重拉，
      下次手动 refresh 再走 ``GET /log`` fallback）。

    **静默态兜底**（修复 vLLM docker 模型"工作日志空白"）：vLLM 容器在权重加载完成后
    进入服务态，几十分钟可能不再向 stdout 打新日志，json.log 字节位置不动 → SSE 静默
    但前端只显示"（暂无日志）"。每 30s 兜底执行一次 ``_read_docker_logs(name, 200)``,
    把首次初始 dump 的前 200 行（启动时的权重加载日志）增量推送给前端，让静默态仍可见
    完整启动过程；用 ``sent_lines`` set 去重，避免容器持续写日志时 30s 周期性重复推送。
    容器删时兜底命令也会失败（返回 []），自然转入 jsonlog-gone 处理路径。
    """
    from modelctl.core.all_service import _CONTAINER_ID_LINE
    from modelctl.core.process import launch_log

    log_path = launch_log(name)
    initial = await asyncio.to_thread(_tail_from_launch_log, name, 200)
    use_docker_fallback = not _launch_log_effective(initial)

    if use_docker_fallback:
        # docker runtime 首次连接：dump 一次容器内日志
        docker_lines = await asyncio.to_thread(_read_docker_logs, name, 200)
        used_json_log = False
        json_log_path: Path | None = None
        json_log_pos = 0
        if docker_lines:
            # docker_core_log_path：
            #   - Linux 直接挂载场景——`docker inspect` 拿到的 LogPath 是走过加农路径的宿主机
            #     绝对路径（/var/lib/docker/containers/<id>/<id>-json.log），可用；
            #   - Windows Docker Desktop (WSL2 / Hyper-V) 场景——LogPath 是 Linux VM
            #     内部路径，从 Windows 宿主机**不可达**，需要 is_file 失败后静默降级。
            # 两种情况我们用同一个判定："路径存在即走字节 offset 增量推；不存在则关闭
            # used_json_log，让 SSE 走 heartbeat + 30s 静默兜底（_read_docker_logs tail 200）"。
            json_log_path = await asyncio.to_thread(docker_core_log_path, name)
            if json_log_path is not None:
                try:
                    json_log_pos = (await asyncio.to_thread(json_log_path.stat)).st_size
                    used_json_log = True
                except OSError:
                    logger.debug(f"[webui] 模型 {name} json log stat 失败，回退 heartbeat-only")
                    json_log_path = None
                    used_json_log = False

        for line in docker_lines:
            yield f"event: log\ndata: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
        if not docker_lines and not initial:
            yield f"event: log\ndata: {json.dumps({'line': '（容器无日志，docker logs 不可用）'}, ensure_ascii=False)}\n\n"
    else:
        for line in initial:
            yield f"event: log\ndata: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
        pos = (await asyncio.to_thread(log_path.stat)).st_size if log_path else 0
        used_json_log = False
        json_log_path = None
        json_log_pos = 0

    last_activity = time.monotonic()
    last_silent_fallback = last_activity
    sent_lines: set[str] = set()

    while True:
        await asyncio.sleep(2)

        if use_docker_fallback:
            # docker runtime：按容器 in-container json log 的字节 offset 推增量（容器存活时新行即推）
            if used_json_log and json_log_path is not None:
                try:
                    if json_log_path.is_file():
                        cur = await asyncio.to_thread(json_log_path.stat).st_size
                        if cur > json_log_pos:
                            raw = await asyncio.to_thread(_read_file_range, json_log_path, json_log_pos, cur)
                            for entry in raw.split("\n"):
                                entry = entry.rstrip("\x00").strip()
                                if not entry:
                                    continue
                                line = _docker_json_line_text(entry)
                                if line:
                                    sent_lines.add(line)
                                    yield f"event: log\ndata: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
                            if raw.strip():
                                json_log_pos = cur
                                last_activity = time.monotonic()
                        # 容器没新输出但 daemon 重启 / 容器重建导致文件消失时，
                        # 下次重链接重读头尾 200 避免丢前文
                    else:
                        # 文件被 daemon 清掉 / 容器删了：标记本次轮询已经结束，
                        # 前端会收到 heartbeat 感知到 "无新日志"
                        yield f"event: stopped\ndata: {{\"reason\": \"jsonlog-gone\"}}\n\n"
                        return
                except OSError:
                    pass
            else:
                # 容器首次没有 json log（docker logs 可用但 json 路径不可访问，
                # 例如 docker-in-docker 或不同挂载点）：仅发心跳，前端下次 refresh 会拉全 200
                pass

            # docker 静默态 30s 兜底（修复 vLLM 容器加载完权重后 json.log 不动导致"工作日志空白"）
            if time.monotonic() - last_silent_fallback >= 30:
                last_silent_fallback = time.monotonic()
                # 先把首次初始 dump 的 200 行灌入 sent_lines，避免 docker logs 拉到的行重复推
                for l in initial:
                    sent_lines.add(l.strip())
                new_tail = await asyncio.to_thread(_read_docker_logs, name, 200)
                emitted = 0
                for line in new_tail:
                    if line in sent_lines:
                        continue
                    sent_lines.add(line)
                    yield f"event: log\ndata: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
                    emitted += 1
                if emitted > 0:
                    last_activity = time.monotonic()

            if time.monotonic() - last_activity >= 10:
                yield "event: heartbeat\ndata: {}\n\n"
                last_activity = time.monotonic()
            continue

        # venv runtime（launch log 持续写）：原有 inotify 风格轮询
        if log_path is None or not log_path.is_file():
            yield f"event: log\ndata: {json.dumps({'line': '（日志文件已删除，等待重建）'}, ensure_ascii=False)}\n\n"
            continue
        try:
            current_size = (await asyncio.to_thread(log_path.stat)).st_size
            if current_size > pos:
                raw = await asyncio.to_thread(_read_file_range, log_path, pos, current_size)
                new_lines = raw.split("\n")
                # 最后一个元素可能不完整（无末尾换行）
                if new_lines and new_lines[-1] == "":
                    new_lines.pop()
                for line in new_lines:
                    yield f"event: log\ndata: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
                pos = current_size
                last_activity = time.monotonic()
        except OSError:
            pass

        # 心跳
        if time.monotonic() - last_activity >= 10:
            yield "event: heartbeat\ndata: {}\n\n"
            last_activity = time.monotonic()


def docker_core_log_path(name: str) -> Path | None:
    """查找 docker-capable 模型对应的 in-container json log 文件路径。

    通过 ``docker inspect <container>`` 拿日志文件绝对路径（dockerd 持久写的
    ``/var/lib/docker/containers/<id>/<id>-json.log``），按字节 offset 增量推 SSE 容器内日志行。
    容器名取引擎的 ``_container_name``，与 ``docker run --detach`` 启动时的命名一致；
    不支持 docker / daemon 不在 / 容器不存在时返回 None。
    """
    profile = _find_profile(name)
    if profile is None or profile.engine not in {"vllm", "tokenspeed", "tensorrt_llm"}:
        return None
    if not (profile.engine_config or {}).get("docker_image"):
        return None
    try:
        from modelctl.capabilities import probe
        from modelctl.engines import get_adapter

        adapter = get_adapter(profile.engine)(profile, probe())
        container = getattr(adapter, "_container_name", None)
        if not container:
            return None
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{.LogPath}}", container],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace"
        )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return None
        p = Path(proc.stdout.strip())
        return p if p.is_file() else None
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[webui] docker inspect LogPath 失败（{name}）：{exc}")
        return None


def _docker_json_line_text(entry: str) -> str | None:
    """单个 ``<id>-json.log`` 行（{"log":"<text>\\n","stream":"stdout","time":"..."}）提取 ``log``。

    每行 JSON 独立对应 docker 一次 write syscall 的完整内容，``log`` 字段以 ``\\n`` 结尾
    （标准输出行为），这里直接 strip 后即为前端 ``<pre>`` 显示用的行文本。
    解析失败（docker 偶尔写半行）整行透传更可读。
    """
    if not entry:
        return None
    try:
        obj = json.loads(entry)
    except (json.JSONDecodeError, ValueError):
        return entry
    text = obj.get("log") if isinstance(obj, dict) else None
    if not isinstance(text, str) or not text:
        return None
    return text.rstrip("\n")


def _read_file_range(path: Path, start: int, end: int) -> str:
    """读取文件的 [start, end) 字节区间（在线程中调用）。"""
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(end - start)
    return data.decode("utf-8", errors="replace")


def _docker_log_cmd(name: str, tail: int = 200) -> list[str] | None:
    """模型走 docker runtime 时返回 ``[docker, logs, --tail, N, <container>]`` 命令；否则 None。

    **容器名优先级**：
    1. 引擎 adapter 的 ``_container_name`` 推理（权威口径——CLI start 时就用这个参数
       给 docker run 的 --name，与 docker ps / start_detached 写 launch log 的容器 ID
       一致）；
    2. launch log 首行（仅当容器推理为空时启用，作为降级轨迹，容器 ID 或 --name
       都会落在 launch log 首行，可读名 ``[A-Za-z0-9._-]{1,64}`` 命中即用）。

    任一来源为空都返回 None。任何构造异常都降级 debug 并返 None（不阻断 SSE 流）。
    """
    profile = _find_profile(name)
    if profile is None or profile.engine not in {"vllm", "tokenspeed", "tensorrt_llm"}:
        return None
    if not (profile.engine_config or {}).get("docker_image"):
        return None

    container = ""
    # 第一来源：engine adapter 推理（权威口径）
    try:
        from modelctl.core.capabilities import probe
        from modelctl.engines import get_adapter

        adapter = get_adapter(profile.engine)(profile, probe())
        container = getattr(adapter, "_container_name", "") or ""
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[webui] 构造 docker logs 命令时 engine 推理失败（{name}）：{exc}")

    # 第二来源：launch log 首行（容器推理为空时启用）
    if not container:
        try:
            from modelctl.core.process import launch_log, tail_file
            from modelctl.core.all_service import _CONTAINER_ID_LINE

            lp = launch_log(name)
            if lp is not None and lp.is_file():
                text = tail_file(lp, 4) or ""
                for ln in text.splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    if _CONTAINER_ID_LINE.match(ln) or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", ln):
                        container = ln
                        break
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[webui] 读 launch log 首行 docker 容器名失败（{name}）：{exc}")

    if not container:
        logger.debug(f"[webui] docker logs 命令缺容器名（{name}），已降级到上下层回退")
        return None
    return ["docker", "logs", "--tail", str(tail), container]


def _read_docker_logs(name: str, tail: int = 200) -> list[str]:
    """按需 ``docker logs --tail <N> <container>``；任何异常 / 命令不可用返回 []。"""
    cmd = _docker_log_cmd(name, tail)
    if not cmd:
        # 命令为空：容器名推理 + 容器名路径都没命中（明确 warn 给用户排查方便）
        logger.warning(f"[webui] docker logs 命令为空（{name}）—— engine 推理或 launch log 都没命中容器名，SSE 流将默认事件 fallback")
        return []
    try:
        import subprocess
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=30, encoding="utf-8", errors="replace")
        merged = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        out = merged.strip().split("\n") if merged.strip() else []
        if not out:
            logger.info(
                f"[webui] docker logs 空输出（{name}）：cmd={cmd} rc={proc.returncode} "
                f"stdout={len(proc.stdout or 0)}B stderr={len(proc.stderr or 0)}B "
                f"stderr_head={(proc.stderr or '')[:160]!r}"
            )
        return out
    except Exception as exc:  # noqa: BLE001 —— 读取失败静默（fallback 口）
        logger.info(f"[webui] docker logs 读取异常（{name}）：{type(exc).__name__}: {exc} | cmd={cmd}")
        return []


def _tail_from_launch_log(name: str, lines: int = 200) -> list[str]:
    """读 launch log 尾部；文件不存在或内容空返回 []。"""
    from modelctl.core.process import launch_log, tail_file

    lp = launch_log(name)
    if lp is None:
        return []
    text = tail_file(lp, lines)
    if not text.strip():
        return []
    return text.strip().split("\n")


@router.get("/{name}/yaml")
async def get_model_yaml(name: str, _: None = Depends(require_auth)):
    """GET /admin/api/models/{name}/yaml — 读取原始 YAML 文本。

    不能用 ``{name}.yaml`` 直接拼路径：YAML 内 name 常自动推导为 ``{group}-{engine}``
    （如 qwen2.5-0.5b-vllm），与磁盘文件 stem（qwen2.5-0.5b）分叉，rglob 匹配不到会 404。
    统一走 _find_profile 拿到 Profile.path 再读，与其它 /models 端点口径一致。
    """
    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None or profile.path is None or not profile.path.is_file():
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 的 YAML 文件未找到"}},
        )
    content = await asyncio.to_thread(profile.path.read_text, encoding="utf-8")
    return {"content": content, "path": str(profile.path)}


@router.post("/{name}/ui/start")
async def start_model_ui(
    name: str,
    request: Request,
    _: None = Depends(require_auth),
):
    """POST /admin/api/models/{name}/ui/start — 启动 Unsloth Web 管理控制台。

    请求体（可选）: ``{port?: int, host?: str, allow_from?: list[str] | str}``
    仅 engine=unsloth 可用，其余返回 412。
    """
    from modelctl.core.capabilities import probe
    from modelctl.core.envfile import load_env
    from modelctl.core.process import (
        is_running,
        launch_log,
        start_detached,
    )
    from modelctl.core.ufw import ensure_ufw_allow
    from modelctl.engines import get_adapter

    load_env()

    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )
    if profile.engine != "unsloth":
        return JSONResponse(
            status_code=412,
            content={"error": {"code": "unsupported_engine", "message": f"引擎 {profile.engine} 不支持 Web 管理控制台（仅 unsloth 支持）"}},
        )

    # 可选 body（无 body 时静默跳过）
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass

    port = body.get("port")
    host = body.get("host")
    allow_from = body.get("allow_from")

    caps = await asyncio.to_thread(probe)
    adapter = get_adapter(profile.engine)(profile, caps)
    spec = adapter.ui_spec(port=port, host=host)

    instance = f"ui-{profile.name}"
    if is_running(instance):
        return {"ok": True, "detail": f"Web 控制台已在运行（http://{spec['host']}:{spec['port']}）", "already_running": True}

    # ufw 白名单
    allow_list = allow_from or spec["allow_from"]
    if isinstance(allow_list, str):
        allow_list = [allow_list]
    for src in allow_list:
        try:
            await asyncio.to_thread(ensure_ufw_allow, src, spec["port"])
        except Exception as exc:
            logger.warning(f"添加 ufw 规则失败（{src} → :{spec['port']}）: {exc}")

    pid, _ = await asyncio.to_thread(start_detached, instance, spec["cmd"], spec["env"])
    log = launch_log(instance)
    return {
        "ok": True,
        "detail": f"Web 控制台已启动（PID {pid}），监听 http://{spec['host']}:{spec['port']}",
        "pid": pid,
        "url": f"http://{spec['host']}:{spec['port']}",
        "allow_from": allow_list,
        "log_path": str(log) if log is not None else None,
    }


@router.post("/{name}/ui/stop")
async def stop_model_ui(name: str, _: None = Depends(require_auth)):
    """POST /admin/api/models/{name}/ui/stop — 停止 Unsloth Web 管理控制台。

    仅 engine=unsloth 可用，其余返回 412。
    """
    from modelctl.core.capabilities import probe
    from modelctl.core.process import is_running, pid_file, stop_instance
    from modelctl.engines import get_adapter

    profile = await asyncio.to_thread(_find_profile, name)
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"模型 {name} 不存在"}},
        )
    if profile.engine != "unsloth":
        return JSONResponse(
            status_code=412,
            content={"error": {"code": "unsupported_engine", "message": f"引擎 {profile.engine} 不支持 Web 管理控制台（仅 unsloth 支持）"}},
        )

    instance = f"ui-{profile.name}"
    caps = await asyncio.to_thread(probe)
    adapter = get_adapter(profile.engine)(profile, caps)

    if not is_running(instance) and not pid_file(instance).is_file():
        return {"ok": True, "detail": "Web 控制台未在运行"}

    ui_port = (adapter.ui_spec() or {}).get("port", 0)
    await asyncio.to_thread(stop_instance, instance, ui_port, [])
    return {"ok": True, "detail": "已停止 Web 控制台"}


# ---------------------------------------------------------------------------
# 子路由工厂（供 admin_router 聚合挂载；prefix="/models" 由聚合方设置）
# ---------------------------------------------------------------------------


def _router() -> APIRouter:
    """返回模型管理子路由（已注册全部端点），供 admin_router 聚合调用。

    使用模块级 router 单例：同一进程内重复调用 create_admin_router() 时，
    既避免重复挂载端点产生路径冲突，也保持前端刷新后 SSE 连接与端点状态一致。
    """
    return router
