#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/all_service.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 一键启停编排服务
# ===============================================================================

"""core/all_service.py — 一键启停编排与单组件四动作原语（模型/网关/统计）。

供 `modelctl all` 与 `modelctl gateway|stats <动作>` 共用；统一返回 ComponentResult，
cli.py 负责把结果转成退出码与打印，本模块不依赖 cli。
注意：start_profile/restart_profile 在 check_requirements 失败时向上抛 RequirementError，
以便 cli 既有命令保持"配置错误 → exit 2、健康超时 → exit 1"的语义；编排层负责捕获。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from loguru import logger

from modelctl.core.capabilities import Capabilities, probe
from modelctl.core.deps import ensure_packages
from modelctl.core.gateway import GATEWAY_PORT
from modelctl.core.paths import usage_data_dir
from modelctl.core.process import (
    describe_port_listener,
    is_running,
    is_running_any,
    kill_log_tee,
    launch_log,
    log_excerpt,
    pid_file,
    port_in_use,
    spawn_log_tee,
    start_detached,
    stop_instance,
    tail_file,
    wait_health,
)
from modelctl.core.profile import Profile, list_profiles
from modelctl.core.stats import USAGE_PORT
from modelctl.core.vram_estimator import kv_estimate_warnings
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError

# all_service.py 位于 src/modelctl/core/，src 目录 = parents[2]（对应 cli.py 的 parents[1]）
_SRC_DIR = str(Path(__file__).resolve().parents[2])
DEFAULT_MODEL_ID = "deepseek-v4-flash"


@dataclass
class ComponentResult:
    component: str
    status: Literal["ok", "skipped", "error"]
    detail: str = ""


def resolve_default_profile(models_dir: Path | None, model_id: str | None) -> Profile | None:
    """解析默认模型 profile：model_id 缺省取 GATEWAY_DEFAULT_MODEL，未设置回退 deepseek-v4-flash。"""
    mid = model_id or os.environ.get("GATEWAY_DEFAULT_MODEL") or DEFAULT_MODEL_ID
    for p in list_profiles(models_dir):
        if p.name == mid or mid in p.aliases:
            return p
    return None


#: 健康检查默认超时：docker 运行时（容器内引擎冷启动含 import torch + 权重加载，
#: WSL2 上实测单 import 就 800s+）放宽到 1800s，避免慢而正常的启动被误判失败。
START_TIMEOUT_DEFAULT = 600.0
START_TIMEOUT_DOCKER = 1800.0

#: fail 事件的阶段标签（与 startup_progress.STAGE_LABELS 口径一致，避免字面量散落）
STAGE_LABELS_PREFLIGHT = "依赖检查"
STAGE_LABELS_PREPARE_ENV = "准备环境"
STAGE_LABELS_LAUNCH = "拉起进程"


def default_start_timeout(profile: Profile, caps: Capabilities) -> float:
    """健康检查超时缺省值：MODELCTL_START_TIMEOUT > docker 1800 > 其它 600。"""
    raw = (os.environ.get("MODELCTL_START_TIMEOUT") or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning(f"MODELCTL_START_TIMEOUT 非数字，忽略：{raw!r}")
    try:
        if get_adapter(profile.engine)(profile, caps).is_docker_runtime():
            return START_TIMEOUT_DOCKER
    except Exception as exc:  # noqa: BLE001 —— 运行时判定失败退回保守默认
        logger.debug(f"is_docker_runtime 判定异常，用默认超时：{exc}")
    return START_TIMEOUT_DEFAULT


#: 容器 ID / 短 hash 行样式（docker 路径 tee 缺席时 launch log 只有这种行）
_CONTAINER_ID_LINE = re.compile(r"^[0-9a-f]{12,64}$")


def docker_logs_fallback(adapter: Any, excerpt: str) -> str | None:
    """设计 §4.4 兜底：摘录为空或仅容器 ID 行时，直接 `docker logs --tail 50` 取内容。

    仅当引擎适配器提供 log_fallback_cmd（docker 三引擎）时生效；任何异常退回 None
    （调用方保留原摘录），绝不影响主流程。
    """
    text = (excerpt or "").strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # 空摘录，或全部是短 hash/容器 ID 行（≤2 行启发式）→ 视为 tee 未产出有效日志
    if lines and not (len(lines) <= 2 and all(_CONTAINER_ID_LINE.match(ln.strip()) for ln in lines)):
        return None
    try:
        cmd = adapter.log_fallback_cmd()
        if not cmd:
            return None
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                              encoding="utf-8", errors="replace")
        content = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
        return content or None
    except Exception as exc:  # noqa: BLE001 —— 兜底失败退回原摘录
        logger.debug(f"docker logs 兜底失败（忽略）：{exc}")
        return None


def start_profile(profile: Profile, caps: Capabilities, timeout: float,
                  on_progress: "Callable[[Any], None] | None" = None) -> ComponentResult:
    """启动单个模型 profile（幂等：已运行返回 skipped）。

    check_requirements 失败时抛 RequirementError（配置错误语义，交给调用方/编排处理）。
    逻辑迁移自 cli._cmd_start。

    on_progress：可选 `StageEvent` 回调（core.startup_progress.StageEvent）。WebUI 绑
    task SSE、CLI 打日志；缺省 None 时除多写一份快照文件外行为与旧实现一致。
    """
    from modelctl.core.startup_progress import LoadingWatcher, StartupTracker

    tag = f"model:{profile.name}"
    if is_running_any(profile.name, profile):
        return ComponentResult(tag, "skipped", "已在运行")
    # 残留 tee 清理：上一次 start 未走完 / 容器被外力杀时 tee 可能仍在跟随旧容器
    kill_log_tee(profile.name)
    # adapter/tracker 先于端口预检创建（adapter 构造无副作用）：预检失败也要有
    # preflight running→failed 事件与快照，否则前端进度卡片看不到失败原因。
    adapter = get_adapter(profile.engine)(profile, caps)
    is_docker = adapter.is_docker_runtime()
    tracker = StartupTracker(profile.name, profile.engine, "docker" if is_docker else "venv",
                             on_progress=on_progress)

    def _env_sink(label: str, pct: float | None) -> None:
        tracker.progress("prepare_env", label, pct=pct)

    adapter.set_progress_sink(_env_sink if on_progress is not None else None)

    # ---- preflight：依赖检查 / 端口 / 兼容预检 ----
    tracker.begin("preflight")
    try:
        # 端口占用预检：走到这里端口仍被占 ⇒ 占用者不是本 profile，引擎启动后 bind 必然
        # EADDRINUSE 秒退；提前拦截并点名占用者，替代"空等健康检查 + 事后翻日志"。
        # RequirementError → cli exit 2，与配置/环境错误语义一致。
        # ollama 豁免：多个 ollama profile 共享同一 11434 serve 是设计语义（见 stop_profile
        # 同族特判）——第二个 profile 启动时 is_running_any 探 /health 得 404 不会 skip，
        # 靠"新 serve bind 失败但健康检查命中已有 serve"就绪，端口被占是正常状态。
        if profile.engine != "ollama" and port_in_use(profile.port):
            who = describe_port_listener(profile.port)
            raise RequirementError(
                f"端口 {profile.port} 已被占用（{who or '占用者未知'}），无法启动 {profile.name}。"
                f"请先释放该端口，或修改 profile 的 port 后重试"
            )
        adapter.check_requirements()  # RequirementError 向上抛
    except RequirementError as exc:
        tracker.fail("preflight", STAGE_LABELS_PREFLIGHT, str(exc))
        raise
    for warning in adapter.warnings:
        logger.warning(warning)
    for warning in kv_estimate_warnings(profile):  # 附录 B.4：KV 显存预检（仅告警，不拦截）
        logger.warning(warning)
    tracker.done("preflight")

    # ---- prepare_env：pre_start（docker 拉镜像子进度 / 模型下载 / 编译） ----
    tracker.begin("prepare_env")
    try:
        adapter.pre_start()
    except RequirementError as exc:
        tracker.fail("prepare_env", STAGE_LABELS_PREPARE_ENV, str(exc))
        raise
    tracker.done("prepare_env")

    # ---- launch：build_command + start_detached（docker 路径随后挂日志 tee） ----
    tracker.begin("launch")
    try:
        cmd, env = adapter.build_command()
        # docker runtime（is_docker_runtime True）走 `docker run --detach`：容器在 daemon 后台续
        # 不会随 client 早退，PID 文件不写（write_pid=False）；venv runtime 维持默认 write_pid=True。
        pid, proc = start_detached(profile.name, cmd, env, write_pid=not is_docker)
        adapter.spawned_proc = proc  # 供 wait_ready 在进程早退时 fail-fast
        if is_docker:
            tee_cmd = adapter.log_tee_cmd()
            if tee_cmd:
                spawn_log_tee(profile.name, tee_cmd)
        try:
            from modelctl.core.gpu_lock import update_gpu_lock_owner

            if adapter.selected_gpus():
                update_gpu_lock_owner(profile.name, pid)
        except Exception:
            pass
        tracker.done("launch")
    except Exception as exc:  # noqa: BLE001 —— 拉起段任何异常都要落 fail 事件，快照不能永停 running
        kill_log_tee(profile.name)  # tee 若已挂上则回收
        tracker.fail("launch", STAGE_LABELS_LAUNCH, str(exc))
        raise

    # ---- loading：等待窗口内 tail 引擎日志按模式表推进 ----
    logger.info(f"已启动 {profile.name}（PID {pid}），等待健康检查（超时 {timeout:g}s）...")
    tracker.begin("loading", "等待引擎初始化")
    watcher: LoadingWatcher | None = None
    log = launch_log(profile.name)
    if log is not None:
        watcher = LoadingWatcher(tracker, profile.engine, log)
        watcher.start()
    try:
        ready = adapter.wait_ready(timeout)
    finally:
        if watcher is not None:
            watcher.stop()

    if ready:
        tracker.done("loading", "引擎初始化完成")
        tracker.done("health", f"就绪：http://127.0.0.1:{profile.port}")
        upstream_key = adapter.upstream_api_key()
        if upstream_key and upstream_key != profile.api_key:
            logger.info(f"上游 API Key（本次启动自动生成）：{upstream_key}")
        adapter.post_start()
        log = launch_log(profile.name)
        logger.info(f"启动成功：{profile.name} 运行于 http://127.0.0.1:{profile.port}")
        if log is not None:
            logger.info(f"日志：{log}")
        if profile.usage or adapter.metrics_mapping() is not None:
            logger.info("提示：用量统计可通过 `modelctl stats start` 启动")
        return ComponentResult(tag, "ok", f"http://127.0.0.1:{profile.port}")

    # 死亡判定交给引擎适配器：docker 分支以容器状态衡量（客户端进程早退≠容器死亡），
    # venv 分支维持"本工具拉起的进程早退即死亡"的语义
    died = adapter.backend_dead()
    detail = "引擎进程提前退出" if died else "健康检查超时"
    tracker.fail("loading", detail, detail)
    if log is None:
        logger.warning("引擎未在时限内就绪，且未找到启动日志")
    elif died:
        # 进程早退：真实异常通常在日志中部，按错误标记截取上下文；无标记时退回尾部 50 行
        excerpt = log_excerpt(log) or tail_file(log, 50)
        # 设计 §4.4：docker 路径 tee 没挂上/被杀时 launch log 只有容器 ID 行 → docker logs 兜底
        excerpt = docker_logs_fallback(adapter, excerpt) or excerpt
        logger.warning(f"引擎进程提前退出（PID {pid}），未能就绪。相关日志摘录（{log}）：")
        logger.warning(excerpt)
    else:
        logger.warning(f"健康检查超时，日志尾部 50 行（{log}）：")
        logger.warning(tail_file(log, 50))
    # tee 是日志写入方：摘录输出后再回收，先杀会丢未 flush 的尾部行
    kill_log_tee(profile.name)
    return ComponentResult(tag, "error", detail)


def stop_profile(profile: Profile, caps: Capabilities, models_dir: Path | None) -> ComponentResult:
    """停止单个模型 profile（含 ollama 共享 serve 特判）。逻辑迁移自 cli._stop_profile。"""
    tag = f"model:{profile.name}"
    adapter = get_adapter(profile.engine)(profile, caps)
    if profile.engine == "ollama":
        other_ollama_running = any(
            is_running(o.name)
            for o in list_profiles(models_dir)
            if o.engine == "ollama" and o.name != profile.name
        )
        if pid_file(profile.name).is_file() and not other_ollama_running:
            stop_instance(profile.name, profile.port, [])
        else:
            adapter.unload_model()
            pid_file(profile.name).unlink(missing_ok=True)
    else:
        adapter.stop_backend()
    # docker 容器被删后 `docker logs -f` 会自行退出，但主动 kill 保证 PID 文件与句柄即时释放
    kill_log_tee(profile.name)
    logger.info(f"已停止：{profile.name}")
    return ComponentResult(tag, "ok", "已停止")


def restart_profile(profile: Profile, caps: Capabilities, timeout: float,
                    on_progress: "Callable[[Any], None] | None" = None) -> ComponentResult:
    """重启单个模型 profile：运行中先停后启，未运行直接启。

    运行态判定改走 `is_running_any(name, profile)`（端口 /health 2xx 优先 + PID 文件机器
    兜底），docker 路径下 PID 文件不存在时仍能据端口探测判定为运行中。
    """
    if is_running_any(profile.name, profile):
        stop_profile(profile, caps, None)
    return start_profile(profile, caps, timeout, on_progress=on_progress)


def _detached_script(module: str, interpreter: str | None = None) -> tuple[list[str], dict[str, str]]:
    """后台启动 `python -m <module>` 子进程，返回 (命令, 环境变量)。

    - `interpreter`：使用的 Python 解释器绝对路径。
      * `None` → 用当前 sys.executable（适合 stats 等纯 stdlib 子进程）
      * 给定 → 用该 venv 解释器（gateway 走 `.venvs/gateway/` 子环境的解释器）
    - PYTHONPATH 始终注入主项目 src/，让子进程能 import modelctl.core.*
    """
    interp = interpreter or sys.executable
    extra_env = {"PYTHONPATH": _SRC_DIR + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return [interp, "-m", module], extra_env


def _gateway_venv_python() -> Path | None:
    """返回 gateway 子环境解释器路径（存在则 Path，缺失则 None）。"""
    from modelctl.core import envs as _envs
    inter = _envs.engine_python("gateway")
    return inter if inter.is_file() else None


def _ensure_gateway_venv() -> bool:
    """确保 gateway 子环境（.venvs/gateway）已创建。

    - 已创建 → True（首次 0 成本）
    - 未创建 → 调用 `uv sync --project gateway/` 自动搭建；成功 True，失败 False
    """
    from modelctl.core import envs as _envs
    if _envs.has_env("gateway"):
        return True
    logger.info("检测到 gateway 专用 venv 缺失，正在自动创建（`modelctl env setup gateway`） ...")
    try:
        rc = _envs.setup("gateway")
    except _envs.EngineEnvError as error:
        logger.error(str(error))
        return False
    if rc != 0:
        return False
    return _envs.has_env("gateway")


def start_gateway() -> ComponentResult:
    if is_running("llm-gateway"):
        return ComponentResult("gateway", "skipped", "网关已在运行")
    # 优先走独立子环境：gateway 依赖不再随主 lockfile 同步，主 `uv sync` 不会清理它
    vendor = _gateway_venv_python()
    if vendor is None:
        if not _ensure_gateway_venv():
            # 子环境自动创建失败时回退到"主 venv 上 uv pip install 单包"
            logger.warning("gateway 独立 venv 创建失败，回退主 venv 单包补齐模式")
            if not ensure_packages("gateway"):
                return ComponentResult(
                    "gateway", "error",
                    "网关依赖补齐失败，请手动 `modelctl env setup gateway` 后重试",
                )
        else:
            vendor = _gateway_venv_python()
    if vendor is None:
        # 强制回退路径：vendor 仍为 None 表示 _ensure_gateway_venv 返回 True 但 venv 又丢了
        # （极端情况：uv 在 on-the-fly 改了环境）。这种情况用主 env 解释器但单包必须补齐
        if not ensure_packages("gateway"):
            return ComponentResult(
                "gateway", "error",
                "网关依赖补齐失败（fastapi/uvicorn/httpx），请手动 `uv sync --project gateway` 后重试",
            )
        cmd, env = _detached_script("modelctl.core.gateway")
    else:
        cmd, env = _detached_script("modelctl.core.gateway", interpreter=str(vendor))
    # 与 stats 服务共用用量持久化目录（USAGE_DATA_DIR 缺省 data/usage-data），
    # 网关累计的 token 由 stats 服务读出，费率/预算计算保持一致。
    # 总是透传**解析后的绝对路径**：只传"env 是否设置"会让子进程按自己的 PROJECT_ROOT
    # 重新解析相对值，两侧写入不同目录 → token 累计分家。
    env["USAGE_DATA_DIR"] = str(usage_data_dir())
    pid, _ = start_detached("llm-gateway", cmd, env)
    port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
    from modelctl.core.gateway import GATEWAY_CLIENT_KEY_ENV, client_api_key

    if not client_api_key():
        logger.warning(
            f"{GATEWAY_CLIENT_KEY_ENV} 未配置：网关将以 fail-closed 运行，全部 /v1 请求返回 401。"
            "请在 .env 设置该密钥后重启网关。"
        )
    logger.info(f"网关已启动（PID {pid}），监听端口 {port}")
    return ComponentResult("gateway", "ok", f"http://127.0.0.1:{port}")


def stop_gateway() -> ComponentResult:
    port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
    stop_instance("llm-gateway", port, ["modelctl.core.gateway"])
    logger.info("网关已停止")
    return ComponentResult("gateway", "ok", "已停止")


def restart_gateway() -> ComponentResult:
    if is_running("llm-gateway"):
        stop_gateway()
    return start_gateway()


# ---------------------------------------------------------------------------
# Web UI（管理面）：复用 gateway 子环境解释器 + create_app(admin=True)
# 与网关同为一份 FastAPI，只是端口不同、额外挂 /admin/api 与前端静态产物。
# ---------------------------------------------------------------------------


def start_webui(auto_build: bool | None = None) -> ComponentResult:
    """启动 Web UI 管理面（依赖同网关；前端产物缺失时按 auto_build 自动补齐）。

    auto_build 透传给 webui.frontend.ensure_frontend()：
    - None  → 交互终端自动装 Node/依赖并构建，非交互只检测给手动指引
    - True  → 强制执行自动处理；False → 完全跳过（--no-build）
    前端不可用不阻断启动：/admin/api 与 /v1 仍可用，只在 detail 里带上下一步指引。
    """
    from modelctl.core.webui.frontend import ensure_frontend
    from modelctl.core.webui.server import WEBUI_INSTANCE, dist_ready, webui_host, webui_port

    if is_running(WEBUI_INSTANCE):
        return ComponentResult("webui", "skipped", "Web UI 已在运行")
    ok, note = ensure_frontend(auto=auto_build)
    if not ok:
        logger.warning(f"前端未就绪，Web UI 将以仅 API 模式启动：{note}")
    # 依赖与网关完全一致（fastapi/uvicorn/httpx），直接复用网关的 venv 保障逻辑
    vendor = _gateway_venv_python()
    if vendor is None:
        if not _ensure_gateway_venv():
            if not ensure_packages("gateway"):
                return ComponentResult(
                    "webui", "error",
                    "Web UI 依赖补齐失败，请手动 `modelctl env setup gateway` 后重试",
                )
        else:
            vendor = _gateway_venv_python()
    if vendor is None:
        if not ensure_packages("gateway"):
            return ComponentResult(
                "webui", "error",
                "Web UI 依赖补齐失败（fastapi/uvicorn/httpx），请手动 `modelctl env setup gateway` 后重试",
            )
        cmd, env = _detached_script("modelctl.core.webui.server")
    else:
        cmd, env = _detached_script("modelctl.core.webui.server", interpreter=str(vendor))
    # 端口/host 显式注入：本进程已从 .env load_env，此处再写一次保证子进程拿到
    # 的是本次生效值（CLI --port 覆盖时也走这里，由 webui 参数传入）
    host, port = webui_host(), webui_port()
    env["WEBUI_HOST"], env["WEBUI_PORT"] = host, str(port)
    # 同 gateway：webui 内部 create_app(admin=True) 走 get_collector 回退分支读 usage 目录，
    # 显式传绝对路径避免子进程按自身 PROJECT_ROOT 重算相对值
    env["USAGE_DATA_DIR"] = str(usage_data_dir())
    pid, _ = start_detached(WEBUI_INSTANCE, cmd, env)
    hint = "" if dist_ready() else f"（仅 /admin/api 可用；{note.splitlines()[0]}）"
    logger.info(f"Web UI 已启动（PID {pid}），监听端口 {port}{hint}")
    detail = f"http://127.0.0.1:{port}" if host in ("0.0.0.0", "::") else f"http://{host}:{port}"
    return ComponentResult("webui", "ok", detail + hint)


def stop_webui() -> ComponentResult:
    from modelctl.core.webui.server import WEBUI_INSTANCE, webui_port

    stop_instance(WEBUI_INSTANCE, webui_port(), ["modelctl.core.webui.server"])
    logger.info("Web UI 已停止")
    return ComponentResult("webui", "ok", "已停止")


def restart_webui(auto_build: bool | None = None) -> ComponentResult:
    from modelctl.core.webui.server import WEBUI_INSTANCE

    if is_running(WEBUI_INSTANCE):
        stop_webui()
    return start_webui(auto_build=auto_build)


def status_webui() -> ComponentResult:
    from modelctl.core.webui.server import WEBUI_INSTANCE, webui_port

    port = webui_port()
    if is_running(WEBUI_INSTANCE):
        ok = wait_health(f"http://127.0.0.1:{port}/admin/api/health", 3.0)
        detail = f"运行中（端口 {port}），/admin/api/health " + ("正常" if ok else "无响应")
        return ComponentResult("webui", "ok", detail)
    return ComponentResult("webui", "ok", "已停止")


def status_gateway() -> ComponentResult:
    port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
    if is_running("llm-gateway"):
        from modelctl.core.gateway import client_api_key

        key = client_api_key()
        if not key:
            return ComponentResult(
                "gateway", "ok",
                "运行中，但未配置 GATEWAY_CLIENT_API_KEY——fail-closed，全部 /v1 请求返回 401",
            )
        ok = wait_health(f"http://127.0.0.1:{port}/v1/models", 3.0, key)
        return ComponentResult("gateway", "ok", "运行中，/v1/models " + ("正常" if ok else "无响应"))
    return ComponentResult("gateway", "ok", "已停止")


def start_stats() -> ComponentResult:
    if is_running("usage-stats"):
        return ComponentResult("stats", "skipped", "用量统计服务已在运行")
    # stats 服务理论上纯 stdlib，但仍过一遍"core" 清单兜底（loguru/yaml 缺失）
    if not ensure_packages("core") or not ensure_packages("stats"):
        return ComponentResult("stats", "error", "统计服务依赖补齐失败，请手动 `uv sync` 后重试")
    cmd, env = _detached_script("modelctl.core.stats")
    pid, _ = start_detached("usage-stats", cmd, env)
    port = int(os.environ.get("USAGE_PORT", str(USAGE_PORT)))
    logger.info(f"用量统计服务已启动（PID {pid}），监听端口 {port}")
    return ComponentResult("stats", "ok", f"http://127.0.0.1:{port}")


def stop_stats() -> ComponentResult:
    port = int(os.environ.get("USAGE_PORT", str(USAGE_PORT)))
    stop_instance("usage-stats", port, ["modelctl.core.stats"])
    logger.info("用量统计服务已停止")
    return ComponentResult("stats", "ok", "已停止")


def restart_stats() -> ComponentResult:
    if is_running("usage-stats"):
        stop_stats()
    return start_stats()


def status_stats() -> ComponentResult:
    port = int(os.environ.get("USAGE_PORT", str(USAGE_PORT)))
    if is_running("usage-stats"):
        ok = wait_health(f"http://127.0.0.1:{port}/api/usage", 3.0)
        return ComponentResult("stats", "ok", "运行中，/api/usage " + ("正常" if ok else "无响应"))
    return ComponentResult("stats", "ok", "已停止")


def start_all(models_dir: Path | None, model_name: str | None = None,
              timeout: float | None = 300) -> list[ComponentResult]:
    """一键启动：默认模型 → gateway → stats；单组件失败继续后续。

    timeout=None（CLI 未显式指定 --timeout）→ 按 profile 运行时自适应（见 default_start_timeout）。
    """
    caps = probe()
    results: list[ComponentResult] = []
    profile = resolve_default_profile(models_dir, model_name)
    if profile is None:
        mid = model_name or os.environ.get("GATEWAY_DEFAULT_MODEL") or DEFAULT_MODEL_ID
        results.append(
            ComponentResult(
                "model",
                "error",
                f"未找到默认模型 profile（{mid}），请配置 GATEWAY_DEFAULT_MODEL 或 --model；"
                "可运行 `modelctl list` 查看",
            )
        )
    else:
        if timeout is None:
            timeout = default_start_timeout(profile, caps)
        try:
            results.append(start_profile(profile, caps, timeout))
        except RequirementError as error:  # check_requirements 失败（配置错误）
            results.append(ComponentResult(f"model:{profile.name}", "error", str(error)))
    results.append(start_gateway())
    results.append(start_stats())
    return results


def stop_all(models_dir: Path | None) -> list[ComponentResult]:
    """一键关闭：stats → gateway → 全部运行中模型（含非默认）。"""
    caps = probe()
    results: list[ComponentResult] = [stop_stats(), stop_gateway()]
    for profile in list_profiles(models_dir):
        # 用 is_running_any 判定（docker 容器 PID 文件不写，端口探测可识别运行中）
        if is_running_any(profile.name, profile):
            results.append(stop_profile(profile, caps, models_dir))
    return results


def restart_all(models_dir: Path | None, model_name: str | None = None,
                timeout: float | None = 300) -> list[ComponentResult]:
    """一键重启：仅默认模型 + gateway + stats。

    timeout=None（CLI 未显式指定 --timeout）→ 按 profile 运行时自适应（见 default_start_timeout）。
    """
    caps = probe()
    results: list[ComponentResult] = []
    profile = resolve_default_profile(models_dir, model_name)
    if profile is None:
        mid = model_name or os.environ.get("GATEWAY_DEFAULT_MODEL") or DEFAULT_MODEL_ID
        results.append(
            ComponentResult(
                "model",
                "error",
                f"未找到默认模型 profile（{mid}），请配置 GATEWAY_DEFAULT_MODEL"
                " 或 --model",
            )
        )
    else:
        if timeout is None:
            timeout = default_start_timeout(profile, caps)
        try:
            results.append(restart_profile(profile, caps, timeout))
        except RequirementError as error:
            results.append(ComponentResult(f"model:{profile.name}", "error", str(error)))
    results.append(restart_gateway())
    results.append(restart_stats())
    return results


def status_all(models_dir: Path | None) -> list[ComponentResult]:
    """汇总默认模型 + gateway + webui + stats 状态。"""
    results: list[ComponentResult] = []
    profile = resolve_default_profile(models_dir, None)
    if profile is None:
        results.append(ComponentResult("model", "ok", "默认模型未找到（GATEWAY_DEFAULT_MODEL 未匹配任何 profile）"))
    elif is_running(profile.name):
        ok = wait_health(f"http://127.0.0.1:{profile.port}", 3.0)
        results.append(
            ComponentResult(f"model:{profile.name}", "ok", "运行中" + ("，健康正常" if ok else "，健康无响应"))
        )
    else:
        results.append(ComponentResult(f"model:{profile.name}", "ok", "已停止"))
    results.append(status_gateway())
    # webui 与 gateway 数据面独立实例（PID 文件/WebUI_INSTANCE）但共享 gateway venv 解释器
    # （ensure_packages("gateway") 与 .venvs/gateway 解释器路径）
    # 顺序约定：model → gateway → webui → stats，与 `/admin/api/all/status` 前端展示顺序一致
    results.append(status_webui())
    results.append(status_stats())
    return results
