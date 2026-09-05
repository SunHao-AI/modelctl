#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/reconcile.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 11:00
# @Desc   : worker 侧 reconciler（把实际状态逼向中心期望状态）
# ===============================================================================

"""core/cluster/reconcile.py — worker 侧声明式状态机。

四条形态规则：只调度不执行 / 失败即终态 / 手动压过 intent / profile 标识用中心侧 stem。

线程模型（单写者）：**所有副作用只在 reconcile 循环线程**。WS(Agent) 线程只能
`offer_snapshot` / `offer_actions` / `handle_actions`（投递）与 `snapshot` /
`flush_results`（读缓存）。投递路径**绝不可加锁**：`_step` 的 start 最长阻塞
`config.start_timeout_s()`（默认 300s），Agent 线程若等 `_lock` 就会让心跳停摆
超过 lease(90s)，中心据此把健康节点标成 stale。线程安全靠 CPython 原子操作：
`list.append` 投递、`a, self._x = self._x, a` 取空。`_lock` 只互斥"循环线程 vs
外部直调 apply_snapshot/reconcile_once"（测试与 CLI 路径）。

stem↔name 归一（规则 4 的落地方式）：`load_profile_at` 缺省推导的 `Profile.name`
是 `{stem}-{engine}`（core/profile.py），与中心 ledger 的 stem、心跳 profiles 键、
测试替身的台账键全部分叉。`_identity_of` 在解析边界把托管 profile 的**台账身份**
（PID 文件、GPU 锁持有者、健康探测名）统一为 `goal.profile`（stem）：中心用哪个词
声明，worker 的本地台账就记在哪个词下，"集群起的模型在本地台账查无此人"从构造上
不可能发生。YAML 显式 `name:` 也一律归一为 stem（M1 裁决，见交付报告偏差项）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from modelctl.core.capabilities import Capabilities, all_vram_total_mb, free_vram_total_mb, probe
from modelctl.core.cluster import config, sync, wsproto
from modelctl.core.cluster.profiles import profile_sha
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.envs import MANAGED_ENGINES, has_env
from modelctl.core.gpu_lock import list_gpu_locks
from modelctl.core.profile import KNOWN_ENGINES, Profile, load_profile_at

STATE_FILE = "cluster-reconcile.json"

#: 连续 N 拍"进程还在但健康检查不通"才降级为 FAILED；单拍抖动（引擎 GC、
#: 权重热加载）不该惊动中心。进程消失（alive=False）不适用此计数，直接终态。
DEGRADE_LIMIT = 3

#: 能力探测（nvidia-smi / which）开销大且在起进程期间会被反复调用，缓存 30s
_CAPS_TTL_S = 30.0

# --- 8 态 stage（spec §7.2）--------------------------------------------------
PENDING_PROFILE_SYNC = "PENDING_PROFILE_SYNC"   # 中心已声明，本地还没有这份文件
PROFILE_SYNCED = "PROFILE_SYNCED"               # 文件已落盘且 sha 相符
RUNTIME_OK = "RUNTIME_OK"                       # 运行时具备启动条件
STARTING = "STARTING"                           # 正在起（含下载权重/健康检查等待）
READY = "READY"
DEGRADED = "DEGRADED"                           # 进程在但健康检查连续失败
FAILED = "FAILED"                               # 终态：需 retry 或 goal 变更
STOPPED = "STOPPED"

VALID_STAGES: tuple[str, ...] = (
    PENDING_PROFILE_SYNC, PROFILE_SYNCED, RUNTIME_OK, STARTING,
    READY, DEGRADED, FAILED, STOPPED,
)

#: stage → 中心 `model_states.state`（与单机 CLI 的八态对齐，dashboard 一套渲染）。
#: 占卡三态（running/starting/degraded）与不占卡五态（pending/synced/
#: ready_to_start/failed/stopped）**必须与 goals.GPU_OCCUPYING_STATES 同源**
#: （Task 5 裁决1，唯一词表来源在 goals.py，本模块不得另写字面量集合）：
#: 一致性由 `_verify_state_vocabulary` 在 Reconciler 构造时惰性校验（goals 函数内
#: import，避免 worker 顶层背中心的 store），任一侧改词表立刻炸，而不是让中心
#: gate 的卡位冲突/占满两项检查静默失效。
_STATE_OF_STAGE: dict[str, str] = {
    PENDING_PROFILE_SYNC: "pending", PROFILE_SYNCED: "synced", RUNTIME_OK: "ready_to_start",
    STARTING: "starting", READY: "running", DEGRADED: "degraded",
    FAILED: "failed", STOPPED: "stopped",
}

#: 占卡三态对应的 stage（READY/STARTING/DEGRADED）——"卡还握着"的三态；
#: 其余 stage（未起动态 + 终态）一律不占卡。词表本身在 goals.py，这里只点名 stage。
_OCCUPYING_STAGES: tuple[str, ...] = (READY, STARTING, DEGRADED)

_vocabulary_checked = False


def _verify_state_vocabulary() -> None:
    """钉死 `_STATE_OF_STAGE` 与 `goals.GPU_OCCUPYING_STATES` 的同源关系（裁决1）。

    中心 gate 的"卡位冲突/节点占满"两项检查完全按 GPU_OCCUPYING_STATES 过滤心跳
    上报的 state。若本模块写入的占卡态不在该词表内（或终态混入词表），检查会
    **静默空转**：既可能把在用的卡当空闲二次下发（双模型撞卡），也可能让已停
    模型永久占位。词表唯一来源在 goals.py，本函数只做引用比对，绝不复制字面量。
    """
    global _vocabulary_checked
    if _vocabulary_checked:
        return
    from modelctl.core.cluster.goals import GPU_OCCUPYING_STATES  # 函数内 import（同 _usable_overlay）

    occupying = {_STATE_OF_STAGE[s] for s in _OCCUPYING_STAGES}
    if not occupying <= GPU_OCCUPYING_STATES:
        raise RuntimeError(f"_STATE_OF_STAGE 占卡态 {sorted(occupying - GPU_OCCUPYING_STATES)} "
                           "不在 goals.GPU_OCCUPYING_STATES 词表内（两侧必须同源，裁决1）")
    free = {_STATE_OF_STAGE[s] for s in VALID_STAGES} - occupying
    if leaked := free & GPU_OCCUPYING_STATES:
        raise RuntimeError(f"_STATE_OF_STAGE 非占卡态 {sorted(leaked)} 混入词表："
                           "会被中心当作握卡永久占用（裁决1）")
    _vocabulary_checked = True

ERROR_CLASSES: tuple[str, ...] = (
    "venv_missing", "gpu_lock", "oom", "startup_timeout", "model_download_failed",
    "runtime_capability", "profile_invalid", "port_conflict", "health_lost",
)

#: 有序子串规则表：先具体后笼统。`[gpu_lock]` 带方括号前缀（gpu_lock.py 的报错格式），
#: 必须早于 `gpu_list`（profile 字段名），否则"卡位被占"会被误判成"profile 写错"。
_ERROR_RULES: tuple[tuple[str, str], ...] = (
    ("[gpu_lock]", "gpu_lock"),
    ("gpu_list", "profile_invalid"),
    ("端口", "port_conflict"),
    ("专用环境未创建", "venv_missing"),
    ("PATH 中找不到", "venv_missing"),
    ("未安装", "venv_missing"),
    ("环境未就绪", "venv_missing"),
    ("显存不足", "oom"),
    ("out of memory", "oom"),
    ("CUDA error", "oom"),
    ("下载失败", "model_download_failed"),
    ("下载超时", "model_download_failed"),
    ("必填", "profile_invalid"),
    ("必须是", "profile_invalid"),
    ("超过实际", "runtime_capability"),
    ("不支持", "runtime_capability"),
    ("需 vLLM", "runtime_capability"),
    ("需 SGLang", "runtime_capability"),
    ("健康检查超时", "startup_timeout"),
    ("进程提前退出", "startup_timeout"),
)


def classify_error(detail: str) -> str:
    """把引擎/编排的中文报错映射到 error_class（中心据此分类展示/告警）。

    兜底 `runtime_capability` 而非 `unknown`：运维看到"环境能力不足"会去查
    驱动/显存/引擎版本，这恰是未分类报错最常见的真实原因；`unknown` 什么也不提示。
    """
    text = detail or ""
    for needle, cls in _ERROR_RULES:
        if needle in text:
            return cls
    return "runtime_capability"


def _raw_of(text: str) -> dict[str, Any]:
    """profile YAML 原文 → 顶层映射（只为读 docker_image 一类免检字段）。

    解析失败返回 {}：`sync._validate` 已在写盘前拒过语法错误，这里宽容是为了
    "本地被人改坏的文件"不该让 reconciler 抛异常中断整轮调度。
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return raw if isinstance(raw, dict) else {}


def runtime_readiness(engine: str, raw: dict[str, Any],
                      caps: Capabilities) -> tuple[bool, str, str]:
    """运行时是否具备启动该引擎的条件 → (ok, reason, error_class)。

    托管引擎看 venv，但 YAML 显式声明 `docker_image` 时用容器 runtime，venv
    不参与（这条路径必须可达，否则是死代码）；非托管引擎看 `probe()` 的 PATH 结果。
    """
    if engine in MANAGED_ENGINES:
        if str(raw.get("docker_image", "")).strip():
            return True, "", ""
        if has_env(engine):
            return True, "", ""
        return False, f"{engine} 专用环境未创建，执行：modelctl env setup {engine}", "venv_missing"
    if caps.binaries.get(engine):
        return True, "", ""
    hint = "确认二进制已安装并在 PATH 中"
    return False, f"引擎 {engine} 的二进制在 PATH 中找不到，{hint}", "venv_missing"


def profile_sha_safe(path: Path) -> str:
    """读文件算 sha；任何 IO 失败 → ""（调用方按"不匹配"处理，不抛异常）。"""
    try:
        return profile_sha(path.read_text(encoding="utf-8"))
    except OSError:
        return ""


def local_profile_paths(models_dir: Path) -> dict[str, Path]:
    """models/ 下所有 profile 文件的 stem → 绝对路径。

    跳过三类"看起来像 YAML 但不是 profile"的文件：`.master` 备份（sync 覆盖前
    留的）、`.tmp`（原子写中间态）、隐藏文件。stem 冲突时按引擎名**字典序取第一**
    并告警——真实仓库里同 stem 跨引擎属病态配置，但确定性优先于"随机覆盖"，
    否则同一台机器两次心跳会给出不同结论。
    """
    out: dict[str, Path] = {}
    root = Path(models_dir)
    if not root.is_dir():
        return out
    for engine_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for f in sorted(engine_dir.glob("*.yaml")):
            if f.name.startswith(".") or f.name.endswith((".master", ".tmp")):
                continue
            stem = f.stem
            if stem in out:
                logger.warning(f"models/ 下 stem 冲突：{stem} 取 {out[stem]}，忽略 {f}")
                continue
            out[stem] = f
    return out


@dataclass
class Outcome:
    """起停替身的统一返回（形状与 all_service.ComponentResult 一致）。"""

    status: str
    detail: str = ""


def _identity_of(prof: Profile, name: str) -> Profile:
    """把 Profile 的**台账身份**归一为中心侧 stem（规则 4 的单点落地）。

    PID 文件、GPU 锁持有者、`is_running_any` 探测全部按 `Profile.name` 记账，而
    中心 ledger、心跳 profiles 键、Task 7 的 `_sync_stages`/`record_model_states`
    全部按 stem（goal.profile）。缺省推导的 name 是 `{stem}-{engine}`，两侧必然
    分叉：起一个集群模型，却留下一个中心永不认识的 PID/锁名。测试替身同样以 stem
    为台账键（rec.starts == ["qwen"]），归一是让"实现与全部测试同向"的唯一写法。
    """
    if prof.name == name:
        return prof
    return replace(prof, name=name)


def _default_cache_dir() -> Path:
    from modelctl.core.process import cache_dir

    return cache_dir()


def _default_starter(profile: Any, caps: Capabilities, timeout: float) -> Outcome:
    """生产起进程入口：委派 all_service（延迟 import，避免拖慢 worker 启动）。"""
    from modelctl.core.all_service import start_profile

    try:
        got = start_profile(profile, caps, timeout)
    except Exception as exc:  # RequirementError / 适配器内部异常 / 下载异常
        return Outcome("error", str(exc))
    return Outcome(got.status, got.detail)


def _default_stopper(profile: Any, caps: Capabilities, models_dir: Path | None) -> Outcome:
    from modelctl.core.all_service import stop_profile

    try:
        got = stop_profile(profile, caps, models_dir)
    except Exception as exc:
        return Outcome("error", str(exc))
    return Outcome(got.status, got.detail)


def _default_prober(profile: Any) -> dict[str, Any]:
    """观测单个 profile → {up, alive, port, pid, gpus, name}。

    `up` = 健康检查通（引擎真的能服务）；`alive` = PID 文件里的进程还活着。
    两者的差集是 DEGRADED 与 FAILED 的分水岭，不可合并成一个布尔。
    """
    from modelctl.core.process import is_pid_alive, is_running_any, pid_file

    up = is_running_any(profile.name, profile)
    pid = None
    path = pid_file(profile.name)
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = None
    alive = is_pid_alive(pid) if pid else False
    gpus = [idx for idx, owner in list_gpu_locks().items() if owner == profile.name]
    return {"up": bool(up), "alive": bool(alive), "port": profile.port,
            "pid": pid, "gpus": gpus, "name": profile.name}


def _usable_overlay(raw: Any) -> dict[str, str]:
    """中心下发的 env_overlay → 可注入进程环境的白名单子集。

    **整份作废**而不是逐键过滤：一份含 `*_API_KEY` 的 overlay 说明调用方误解了
    env_overlay 的用途（密钥必须留在 worker 本地 .env），逐键过滤会让运维以为
    "密钥已生效"却在下发链路里明文传输——这是需要被立刻发现并纠正的用法错误。
    """
    from modelctl.core.cluster.goals import ENV_OVERLAY_ALLOWLIST, SECRET_KEY_HINTS

    if not isinstance(raw, dict) or not raw:
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        k = str(key)
        if any(hint in k.upper() for hint in SECRET_KEY_HINTS):
            logger.warning(f"env_overlay 含疑似密钥键 {k}，整份 overlay 已丢弃")
            return {}
        if k not in ENV_OVERLAY_ALLOWLIST:
            logger.warning(f"env_overlay 含非白名单键 {k}，整份 overlay 已丢弃")
            return {}
        out[k] = str(value)
    return out


class _OverlayScope:
    """在 with 块内注入 overlay，退出时**逐键精确复原**（含"原本不存在 → 删除"）。

    不能整体备份/还原 os.environ 快照：起进程期间其他线程（webui 请求处理）也在
    读写环境，全量还原会吞掉它们的变更。
    """

    def __init__(self, overlay: dict[str, str]) -> None:
        self._overlay = overlay
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> _OverlayScope:
        for key, value in self._overlay.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *exc: Any) -> None:
        for key, before in self._saved.items():
            if before is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = before


#: 判定"下发内容是否与本地台账一致"时比对的 goal 字段（sync 持久化 entry 与中心
#: 快照 goal 的交集键；yaml 以 sha 代表，path/version 不参与——前者本地属性、后者
#: 不进同步语义）。
_SYNC_GOAL_KEYS: tuple[str, ...] = ("goal_id", "profile", "engine", "sha",
                                    "intent", "params", "env_overlay")


def _snapshot_rewrites_state(snapshot: dict[str, Any], state: dict[str, Any]) -> bool:
    """下发快照的 goal 集是否与本地登记不一致（存在任何"中心重新表达"的内容差异）。"""
    delivered_raw = snapshot.get("goals") if isinstance(snapshot, dict) else None
    if not isinstance(delivered_raw, list):
        return False
    delivered = [g for g in delivered_raw if isinstance(g, dict)]
    have = {str(g.get("goal_id", "")): g for g in state["goals"]}
    if len(delivered) != len(have):
        return True
    for goal in delivered:
        old = have.get(str(goal.get("goal_id", "")))
        if old is None:
            return True
        if any(goal.get(k) != old.get(k) for k in _SYNC_GOAL_KEYS):
            return True
    return False


class Reconciler:
    """worker 侧单写者状态机。

    `starter/stopper/prober/caps` 全部可注入：起停真进程与 nvidia-smi 探测在 CI 与
    单测里不可用，而"该不该调"的判断逻辑（本任务的全部价值）必须能被完整测到。
    """

    def __init__(self, models_dir: Path | None = None, cache_dir: Path | None = None, *,
                 starter: Callable[..., Outcome] | None = None,
                 stopper: Callable[..., Outcome] | None = None,
                 prober: Callable[[Any], dict[str, Any]] | None = None,
                 caps: Capabilities | None = None) -> None:
        # 占卡词表同源校验（裁决1）：装错词表绝不上岗——宁可启动即炸，
        # 也不让中心 gate 的卡位检查静默空转
        _verify_state_vocabulary()
        self._models = Path(models_dir) if models_dir else PROJECT_ROOT / "models"
        self._cache = Path(cache_dir) if cache_dir else _default_cache_dir()
        self._starter = starter or _default_starter
        self._stopper = stopper or _default_stopper
        self._prober = prober or _default_prober
        self._caps = caps
        self._lock = threading.RLock()
        # goal_id → {sha, intent, stage, reason, error_class, manual, degrade, path,
        #            engine, profile, port, pid, gpu, at}
        self._recs: dict[str, dict[str, Any]] = {}
        self._loaded = False
        # 投递槽：_incoming 只留最新一份快照（旧的已被更新覆盖，执行它没有意义）
        self._incoming: list[dict[str, Any]] = []
        self._pending: list[dict[str, Any]] = []
        self._results: list[dict[str, Any]] = []
        self._caps_cache: tuple[float, Capabilities] | None = None
        #: models/ 里"没有对应 goal"的本地 profile 观测结果（本地手起的模型也要上报）
        self._local: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 状态持久化

    def _state_path(self) -> Path:
        return self._cache / STATE_FILE

    def _load(self) -> None:
        """恢复上一进程的手动位与终态。

        不持久化的话，worker 每次重启都会把"运维手动停掉的模型"重新拉起、把
        "已判定失败的 goal"再撞一次——重启进程不是清除人工决策的理由。
        """
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        recs = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(recs, dict):
            return
        for goal_id, rec in recs.items():
            if isinstance(rec, dict) and rec.get("stage") in VALID_STAGES:
                self._recs[str(goal_id)] = rec

    def _save(self) -> None:
        """状态文件是"尽力而为"的加速件，写失败只告警：心跳回流才是权威上报。"""
        path = self._state_path()
        tmp = path.with_name(path.name + ".tmp")
        payload = {"updated_at": time.time(), "profiles": self._recs}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning(f"集群 reconciler 状态落盘失败（不影响运行）: {exc}")

    # ------------------------------------------------------------------ 投递（无锁）

    def offer_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Agent 线程投递中心快照。**不得加锁**（见模块头线程模型）。"""
        if isinstance(snapshot, dict):
            self._incoming = [snapshot]

    def handle_actions(self, actions: list[dict[str, Any]], *, now: float | None = None) -> None:
        """校验并受理手动指令，result 帧进 `_results` **唯一出口**（Agent 侧统一
        `flush_results()` 取走回传；本方法不返回帧，避免同一 ack 被发送两次）。

        "受理"≠"已执行"：detail 明说"下一轮生效"，避免 dashboard/CLI 把 ≤5s 的
        异步生效窗口谎报成瞬时完成。真正的状态变化由下一拍 `_step` 产生并随心跳回流。
        未知 action / 本机没有该 goal → ok=False 且**不入队**（否则中心先收到
        "已拒绝"，worker 却又在下一轮把它当有效指令处理）。
        """
        self._load()
        now = time.time() if now is None else now
        out: list[dict[str, Any]] = []
        goals = {str(g.get("goal_id", "")): g for g in sync.read_state(self._cache)["goals"]}
        for act in actions:
            action = str(act.get("action", ""))
            goal_id = str(act.get("goal_id", ""))
            seq = act.get("seq", 0)
            if action not in wsproto.VALID_ACTIONS:
                out.append(wsproto.make_result(seq, False, f"不支持的指令 {action!r}"))
                continue
            goal = goals.get(goal_id)
            if goal is None:
                out.append(wsproto.make_result(seq, False, f"本机没有目标 {goal_id}"))
                continue
            self._pending.append({"seq": seq, "action": action, "goal_id": goal_id,
                                  "ok_seen": now})
            name = str(goal.get("profile", goal_id))
            out.append(wsproto.make_result(seq, True, f"{name} 已受理，下一轮生效"))
        if out:
            self._results.extend(out)      # 无锁：list.extend 是原子的

    def flush_results(self) -> list[dict[str, Any]]:
        """取走并清空待回传的 result 帧（原子换表，Agent 线程可安全调用）。"""
        out, self._results = self._results, []
        return out

    # ------------------------------------------------------------------ 快照应用

    def apply_snapshot(self, snapshot: dict[str, Any], *, now: float | None = None) -> sync.SyncResult:
        """落盘中心快照并让后续拍次看到新 goal（外部直调路径，带锁）。"""
        now = time.time() if now is None else now
        with self._lock:
            return self._apply_locked(snapshot, now)

    def _apply_locked(self, snapshot: dict[str, Any], now: float) -> sync.SyncResult:
        self._load()
        # 何时强制落盘：显式 force，**或**下发内容与本地登记不一致（中心重新表达了
        # 意图）。后者是内容哈希 revision 的必然推论——真中心的 revision 随内容变，
        # 二者等价；但 reconcile 的 goal/intent/sha/env_overlay 变化若被 sync 的同
        # revision 短路吞掉，状态机永远看不到新意图（测试正是手填固定 revision 下发
        # 变更内容，逼出这条"内容变了就必须落盘"的下界）。内容完全一致时仍短路。
        force = bool(snapshot.get("force")) or _snapshot_rewrites_state(
            snapshot, sync.read_state(self._cache))
        out = sync.apply_snapshot(snapshot, models_dir=self._models,
                                  cache_dir=self._cache, force=force, now=now)
        state = sync.read_state(self._cache)
        present = {str(g["goal_id"]): g for g in state["goals"]}
        for goal_id in list(self._recs):
            if goal_id not in present:
                del self._recs[goal_id]          # goal 已从快照消失 = 记录一并作废
        for goal_id, goal in present.items():
            self._ensure_rec(goal_id, goal, now)
        self._save()
        return out

    def _drain_snapshot(self, *, now: float) -> None:
        """循环线程消费投递槽（原子取空后应用，与外部直调互斥）。"""
        pending, self._incoming = self._incoming, []
        for snapshot in pending:
            with self._lock:
                self._apply_locked(snapshot, now)

    def _ensure_rec(self, goal_id: str, goal: dict[str, Any], now: float) -> None:
        """goal 内容或 intent 变化 → 整条重置（含清手动位）。

        重置是"中心重新表达了一次意图"的信号：运维改了 YAML 里的端口或把 intent
        从 stop 改成 start，都意味着此前的人工干预/失败判定不再适用。
        """
        sha, intent = str(goal.get("sha", "")), str(goal.get("intent", "start"))
        rec = self._recs.get(goal_id)
        if rec is not None and rec.get("sha") == sha and rec.get("intent") == intent:
            return
        self._recs[goal_id] = {"sha": sha, "intent": intent, "stage": PENDING_PROFILE_SYNC,
                               "reason": "", "error_class": "", "manual": "",
                               "degrade": 0, "engine": str(goal.get("engine", "")),
                               "profile": str(goal.get("profile", "")),
                               "path": str(goal.get("path", "")),
                               "env_overlay": goal.get("env_overlay"),
                               "port": None, "pid": None, "gpu": [], "at": now}

    def _apply_manual(self, actions: list[dict[str, Any]], *, now: float) -> None:
        for act in actions:
            goal_id = str(act.get("goal_id", ""))
            rec = self._recs.get(goal_id)
            if rec is None:
                continue
            action = str(act.get("action", ""))
            if action == "retry":
                rec.update({"stage": PENDING_PROFILE_SYNC, "reason": "", "error_class": "",
                            "manual": "", "degrade": 0, "at": now})
            elif action in ("stop", "restart", "start"):
                rec["manual"] = action
                rec["at"] = now

    # ------------------------------------------------------------------ 执行

    def _fail(self, rec: dict[str, Any], detail: str, now: float) -> None:
        rec.update({"stage": FAILED, "reason": detail, "error_class": classify_error(detail),
                    "degrade": 0, "at": now})

    def _managed_path(self, rec: dict[str, Any]) -> Path | None:
        """goal → 本地 YAML 路径。

        优先用 sync 落盘时登记的**绝对路径**（那才是真写出来的位置），回退按
        engine/profile 重算——覆盖"状态文件被删但 YAML 还在"的半损坏场景。
        """
        path = str(rec.get("path", ""))
        if path and Path(path).is_file():
            return Path(path)
        engine, profile = str(rec.get("engine", "")), str(rec.get("profile", ""))
        if not engine or not profile:
            return None
        guess = self._models / engine / f"{profile}.yaml"
        return guess if guess.is_file() else None

    def _observe(self, rec: dict[str, Any]) -> dict[str, Any]:
        """按**下发文件本身**解析 Profile 再观测（stem↔name 一致性的唯一保证）。

        解析后立刻 `_identity_of` 归一：交给 prober/starter/stopper 的 Profile 其
        name 恒为中心侧 stem，本地台账（PID/锁/替身运行态）与中心 ledger 同名。
        """
        path = self._managed_path(rec)
        if path is None:
            return {}
        try:
            prof = _identity_of(load_profile_at(path), str(rec.get("profile", "")))
        except Exception as exc:  # ProfileError / yaml 错误 / 字段非法
            return {"up": False, "alive": False, "port": None, "pid": None, "gpus": [],
                    "name": str(rec.get("profile", "")), "profile_error": str(exc)}
        got = dict(self._prober(prof))
        got["profile"] = prof
        return got

    def _step(self, goal_id: str, goal: dict[str, Any], rec: dict[str, Any],
              caps: Capabilities, now: float) -> None:
        """单个 goal 的一拍推进。顺序敏感，改动前先读注释。"""
        # 失败即终态：早退，绝不重复撞同一个错误（本任务规则 2）
        if rec["stage"] == FAILED:
            return
        path = self._managed_path(rec)
        if path is None:
            rec.update({"stage": PENDING_PROFILE_SYNC,
                        "reason": "等待中心下发 profile 文件", "at": now})
            return
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            rec.update({"stage": PENDING_PROFILE_SYNC,
                        "reason": "profile 文件不可读，等待重新下发", "at": now})
            return
        want = str(rec.get("sha", ""))
        got = profile_sha(text)
        if want and got != want:
            rec.update({"stage": PENDING_PROFILE_SYNC,
                        "reason": "本地 profile 文件与中心声明不一致（漂移），等待重新下发",
                        "at": now})
            return

        # **必须在写 PROFILE_SYNCED 之前**取上一拍：否则"上一拍是否 READY"永远为假，
        # 健康降级判据彻底失效（每个 READY 的模型都会在第一拍被判成 SYNCED→重新起）。
        prev = str(rec.get("stage", ""))
        rec["path"], rec["engine"] = str(path), str(goal.get("engine", rec.get("engine", "")))
        rec["profile"] = str(goal.get("profile", rec.get("profile", "")))
        if prev not in (STARTING, READY, DEGRADED):
            rec["stage"] = PROFILE_SYNCED

        manual = str(rec.get("manual", ""))
        intent = manual or str(goal.get("intent", "start"))

        # ---- 方向为停：先停，本轮到此为止（restart 是"停完下轮由 intent 拉起"）
        if intent in ("stop", "restart"):
            obs = self._observe(rec)
            if obs.get("up") or obs.get("alive"):
                prof = obs.get("profile")
                if prof is None:
                    self._fail(rec, "profile 解析失败，无法定位进程", now)
                    return
                with _OverlayScope(_usable_overlay(goal.get("env_overlay"))):
                    out = self._stopper(prof, caps, self._models)
                if out.status == "error":
                    self._fail(rec, out.detail or "停止失败", now)
                    return
                rec.update({"stage": STOPPED, "reason": "", "error_class": "",
                            "degrade": 0, "pid": None, "gpu": [], "at": now})
            else:
                rec.update({"stage": STOPPED, "reason": "", "error_class": "",
                            "degrade": 0, "at": now})
            # 手动 stop **不清位**：清了下轮按 intent=start 复活（规则 3）
            if intent == "restart":
                rec["manual"] = ""
            return

        # ---- 方向为起：已在服务 → READY
        obs = self._observe(rec)
        if obs.get("up"):
            rec.update({"stage": READY, "reason": "", "error_class": "", "degrade": 0,
                        "port": obs.get("port"), "pid": obs.get("pid"),
                        "gpu": list(obs.get("gpus") or []), "at": now})
            return

        # ---- 上一拍在服务、这一拍不通：进程没了直接终态，进程还在才计数降级
        if prev in (READY, DEGRADED):
            if not obs.get("alive"):
                self._fail(rec, "进程已退出，健康检查不再可达（health_lost）", now)
                rec["error_class"] = "health_lost"
                return
            degrade = int(rec.get("degrade", 0)) + 1
            if degrade >= DEGRADE_LIMIT:
                self._fail(rec, f"连续 {degrade} 次健康检查失败，进程仍在但不可服务", now)
                rec["error_class"] = "health_lost"
                return
            rec.update({"stage": DEGRADED, "degrade": degrade,
                        "reason": "健康检查连续失败（进程仍在）", "at": now})
            return

        # ---- 运行时不具备条件：不撞进程，直接终态并说明怎么修
        ok, reason, cls = runtime_readiness(str(rec.get("engine", "")), _raw_of(text), caps)
        if not ok:
            rec.update({"stage": FAILED, "reason": reason, "error_class": cls,
                        "degrade": 0, "at": now})
            return
        rec["stage"] = RUNTIME_OK

        prof = obs.get("profile")
        if prof is None:
            try:
                prof = _identity_of(load_profile_at(path), str(rec.get("profile", "")))
            except Exception as exc:
                self._fail(rec, f"profile 解析失败：{exc}", now)
                return

        # **先落 STARTING 再阻塞起进程**：否则 300s 冷启动期间中心只能看到
        # PROFILE_SYNCED，"指令没到"与"正在启动"在 dashboard 上无法区分。
        rec.update({"stage": STARTING, "reason": "", "error_class": "", "at": now})
        self._save()
        with _OverlayScope(_usable_overlay(goal.get("env_overlay"))):
            out = self._starter(prof, caps, float(config.start_timeout_s()))
        if out.status == "error":
            self._fail(rec, out.detail or "启动失败", now)
            return

        after = self._observe(rec)
        if after.get("up"):
            rec.update({"stage": READY, "port": after.get("port"), "pid": after.get("pid"),
                        "gpu": list(after.get("gpus") or []), "reason": "",
                        "error_class": "", "degrade": 0, "at": now})
        else:
            # skipped（已在运行）或起完还没过健康检查：保持 STARTING，下一拍再判
            rec.update({"port": after.get("port"), "pid": after.get("pid"), "at": now})

    def _observe_unmanaged(self, now: float) -> None:
        """本地存在但**没有 goal** 的 profile：只观测、只上报，绝不代客起停。

        这些是运维在 worker 本机手起的模型。集群接管它们需要 M2 的"收养"流程；
        M1 若擅自 stop，等于中心悄悄杀掉了不属于它管理的进程。
        """
        managed = {str(p) for p in sync.managed_paths(self._cache)}
        seen: set[str] = set()
        for stem, path in local_profile_paths(self._models).items():
            if str(path) in managed:
                continue
            seen.add(stem)
            try:
                prof = load_profile_at(path)
            except Exception:
                continue
            got = self._prober(prof)
            stage = READY if got.get("up") else (STOPPED if not got.get("alive") else DEGRADED)
            self._local.setdefault(stem, {"goal_id": "", "stage": stage,
                                          "state": _STATE_OF_STAGE[stage], "reason": "",
                                          "error_class": "", "managed": False,
                                          "port": got.get("port"), "pid": got.get("pid"),
                                          "gpu": list(got.get("gpus") or []), "at": now})
            self._local[stem]["stage"] = stage
            self._local[stem]["state"] = _STATE_OF_STAGE[stage]
            self._local[stem]["at"] = now
        for stem in set(self._local) - seen:
            del self._local[stem]

    def reconcile_once(self, *, now: float | None = None) -> None:
        """一拍：应用投递 → 处理指令 → 逐个推进 → 观测本地 → 落盘。"""
        now = time.time() if now is None else now
        self._drain_snapshot(now=now)
        with self._lock:
            self._load()
            pending, self._pending = self._pending, []
            self._apply_manual(pending, now=now)
            caps = self._capabilities()
            state = sync.read_state(self._cache)
            goals = {str(g["goal_id"]): g for g in state["goals"]}
            for goal_id, goal in goals.items():
                rec = self._recs.get(goal_id)
                if rec is None:
                    self._ensure_rec(goal_id, goal, now)
                    rec = self._recs[goal_id]
                try:
                    self._step(goal_id, goal, rec, caps, now)
                except Exception as exc:  # 单个 goal 的异常不得中断整轮调度
                    logger.exception(f"reconcile {goal_id} 异常")
                    self._fail(rec, f"内部异常：{exc}", now)
            self._observe_unmanaged(now)
            self._save()

    # ------------------------------------------------------------------ 上报

    def _capabilities(self) -> Capabilities:
        if self._caps is not None:
            return self._caps
        now = time.time()
        hit = self._caps_cache
        if hit and now - hit[0] < _CAPS_TTL_S:
            return hit[1]
        got = probe()
        self._caps_cache = (now, got)
        return got

    def _capacity(self, caps: Capabilities) -> dict[str, Any]:
        """capacity 的显存口径是**节点总量**（all/free 都对全部卡求和），非单卡。"""
        return {"gpu_count": caps.gpu_count, "vram_total_mb": all_vram_total_mb(caps),
                "vram_free_mb": free_vram_total_mb(caps)}

    def _runtimes(self, caps: Capabilities) -> dict[str, dict[str, Any]]:
        """遍历**全部** KNOWN_ENGINES：只报"装过的"会让中心无法区分"没装"与"没探测"，
        于是 gate 会把不可用的引擎放行到这台节点上。"""
        out: dict[str, dict[str, Any]] = {}
        for engine in sorted(KNOWN_ENGINES):
            ok = has_env(engine) if engine in MANAGED_ENGINES else bool(caps.binaries.get(engine))
            out[engine] = {"ok": bool(ok)}
        return out

    def _collect(self) -> dict[str, Any]:
        """每拍重建快照（不缓存上一轮），保证 STARTING/DEGRADED 实时可见。"""
        self._load()
        state = sync.read_state(self._cache)
        caps = self._capabilities()
        drift = sync.scan_drift(self._cache, self._models)
        profiles: dict[str, dict[str, Any]] = {}
        stems: list[str] = []
        for stem in local_profile_paths(self._models):
            stems.append(stem)
        for goal in state["goals"]:
            goal_id = str(goal["goal_id"])
            name = str(goal.get("profile", ""))
            rec = self._recs.get(goal_id)
            if rec is None:
                # 本进程尚未跑过这一条（worker 刚重启）：用磁盘 sha 推导最小可用 stage
                path = Path(str(goal.get("path", "")))
                synced = bool(goal.get("sha")) and profile_sha_safe(path) == str(goal["sha"])
                stage = PROFILE_SYNCED if synced else PENDING_PROFILE_SYNC
                port, pid, gpu = None, None, []
            else:
                stage = str(rec.get("stage", PENDING_PROFILE_SYNC))
                port, pid, gpu = rec.get("port"), rec.get("pid"), list(rec.get("gpu") or [])
            entry = {"goal_id": goal_id, "stage": stage,
                     "state": _STATE_OF_STAGE.get(stage, "pending"),
                     "reason": str((rec or {}).get("reason", "")),
                     "error_class": str((rec or {}).get("error_class", "")),
                     "managed": True, "port": port, "pid": pid, "gpu": gpu,
                     "at": (rec or {}).get("at")}
            # setdefault：有 goal 的条目**不被本地同名观测覆盖**（goal 侧信息更全）
            profiles.setdefault(name, entry)
        for name, entry in self._local.items():
            profiles.setdefault(name, dict(entry))
        return {"revision": state["revision"], "profiles": profiles, "drift": drift,
                "local_profiles": sorted(set(stems)), "capacity": self._capacity(caps),
                "runtimes": self._runtimes(caps)}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._collect()

    def heartbeat_payload(self) -> dict[str, Any]:
        """心跳扩展段（Task 2 的六个键）。缺失段由 Agent 侧决定"整段省略"。"""
        got = self.snapshot()
        return {"profiles": got["profiles"], "goal_sync": {"revision": got["revision"]},
                "drift": got["drift"], "local_profiles": got["local_profiles"],
                "capacity": got["capacity"], "runtimes": got["runtimes"]}

    # ------------------------------------------------------------------ 循环

    def run(self, stop_event: threading.Event) -> None:
        """后台循环。reconcile 周期与心跳周期**刻意不同步**：本地收敛不该等心跳，
        心跳也不该被本地收敛拖慢（两条时间线各自独立自愈）。"""
        interval = config.reconcile_interval_s()
        logger.info(f"集群 reconciler 启动（周期 {interval}s，模型目录 {self._models}）")
        while not stop_event.is_set():
            try:
                self.reconcile_once()
            except Exception:
                logger.exception("集群 reconciler 单轮异常（已忽略，继续下一轮）")
            stop_event.wait(interval)
        logger.info("集群 reconciler 已停止")


# --------------------------------------------------------------------------- 进程内单例

_CURRENT: Reconciler | None = None
_CURRENT_LOCK = threading.Lock()
_STOP: threading.Event | None = None


def current() -> Reconciler | None:
    return _CURRENT


def start_reconciler_in_background() -> Reconciler | None:
    """按角色闸门启动。

    **刻意不看 `center_url`**：中心宕机或未配置时，worker 仍必须按**最后已知的
    goal** 维持推理服务（spec §8.1：中心故障不得影响已下发的推理）。若在此处
    要求 center_url，一次中心重启就会让全部 worker 忘记该跑什么模型。
    """
    global _CURRENT, _STOP
    with _CURRENT_LOCK:
        if _CURRENT is not None:
            return _CURRENT
        if not config.is_worker():
            return None
        rt = Reconciler()
        stop = threading.Event()
        thread = threading.Thread(target=rt.run, args=(stop,),
                                  name="cluster-reconcile", daemon=True)
        _CURRENT, _STOP = rt, stop
        thread.start()
        return rt


def _stop_event() -> threading.Event | None:
    """测试/优雅退出用。"""
    return _STOP
