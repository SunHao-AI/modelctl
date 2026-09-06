#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/sync.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : worker 侧 goal 快照落盘（原子写 + 漂移检测 + 剪枝；§6.3）
# ===============================================================================

"""core/cluster/sync.py — worker 端"中心声明 → 本地文件"的执行层。

两条与直觉相反但必要的设计：

1) **不注入 managed-by 头注释**。头会改变文件字节，使"文件 sha == 中心 sha"这一
   漂移判据失效（要判漂移就得先剥头，多一处可错的地方）。管理信息全部记在
   data/cache/cluster-goals.json 的 goal→{path, sha} 映射里，模型文件与中心
   逐字节相同，`modelctl list` / Web 控制台看到的与中心下发的完全一致。
2) **中心送来的内容一律按外部输入校验**（这是一条"远端能往本机 models/ 写文件"
   的通道）：profile/engine 走白名单名 + KNOWN_ENGINES，尺寸封顶，必须能被
   safe_load 且含 port。校验失败只拒该条（rejected），不中断整份快照。

原子写 = 同目录 .tmp + os.replace；失败必须清掉 .tmp 且不写 state，否则下一轮
revision 短路会以为"已同步"，把半途状态永久固化。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from modelctl.core.cluster.profiles import is_safe_name, profile_sha
from modelctl.core.profile import KNOWN_ENGINES

GOALS_FILE = "cluster-goals.json"
MARKER_FILE = "cluster-sync-marker.json"

#: 单条 goal 落盘前的尺寸/结构校验上限（与中心 profiles.MAX_YAML_BYTES 同值）
MAX_YAML_BYTES = 256 * 1024


@dataclass
class SyncResult:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    revision: str = ""
    drift: list[str] = field(default_factory=list)


def _goals_path(cache_dir: Path) -> Path:
    return cache_dir / GOALS_FILE


def read_state(cache_dir: Path) -> dict[str, Any]:
    """读回本地上一次应用的快照。文件缺失/损坏一律视作"从未同步过"。

    损坏时不清空文件：read_state 是只读路径（心跳也调它），把"读不懂"当成
    "写权限"会让一次偶发截断丢掉全部管理关系，进而让剪枝逻辑失去保护对象。
    """
    path = _goals_path(cache_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"revision": "", "goals": []}
    if not isinstance(data, dict) or not isinstance(data.get("goals"), list):
        return {"revision": "", "goals": []}
    goals = [g for g in data["goals"] if isinstance(g, dict) and g.get("goal_id")]
    return {"revision": str(data.get("revision", "")), "goals": goals}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def managed_paths(cache_dir: Path) -> list[Path]:
    """本地上一次落盘的文件路径（供状态采集与卸载清理）。"""
    out = []
    for g in read_state(cache_dir)["goals"]:
        p = g.get("path")
        if isinstance(p, str) and p:
            out.append(Path(p))
    return out


def _validate(goal: dict[str, Any]) -> str:
    """返回错误文案（空串表示通过）。措辞直接进 events/dashboard，须可行动。"""
    profile, engine = str(goal.get("profile", "")), str(goal.get("engine", ""))
    if not is_safe_name(profile):
        return f"非法 profile 名 {profile!r}"
    if engine not in KNOWN_ENGINES:
        return f"未知 engine {engine!r}"
    text = goal.get("yaml")
    if not isinstance(text, str) or not text.strip():
        return "yaml 为空"
    if len(text.encode("utf-8")) > MAX_YAML_BYTES:
        return f"yaml 过大（>{MAX_YAML_BYTES} B）"
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return f"yaml 语法错误: {exc}"
    except Exception as exc:  # noqa: BLE001 — 深嵌套 RecursionError 等按继承树收口（同 profiles.py 口径）
        return f"yaml 解析失败: {type(exc).__name__}"
    if not isinstance(raw, dict):
        return "yaml 顶层必须是映射"
    if raw.get("port") in (None, ""):
        return "yaml 缺 port"
    want = goal.get("sha")
    if isinstance(want, str) and want and profile_sha(text) != want:
        return "sha 与快照不符（传输被截断或中心数据损坏）"
    return ""


def apply_snapshot(snapshot: dict[str, Any], *, models_dir: Path, cache_dir: Path,
                   now: float, force: bool = False) -> SyncResult:
    """把中心快照落盘。同 revision 直接短路（幂等 + 省 IO）；`force=True` 打破短路。

    `force` 供中心 `POST /nodes/{id}/sync` 使用：本地文件被人为改坏时，revision
    并未变化，若不强制就会**永远修不好**（漂移被短路跳过，只上报不修复）。

    剪枝以"快照全集"为准：上一次落过、这次不在快照里的 goal，其 YAML 被删除。
    中心从不送"已删除列表"——goal 从快照消失即是删除语义（§6.5）。
    """
    revision = str(snapshot.get("revision", "")) if isinstance(snapshot, dict) else ""
    raw_goals = snapshot.get("goals") if isinstance(snapshot, dict) else None
    goals = [g for g in raw_goals if isinstance(g, dict)] if isinstance(raw_goals, list) else []
    state = read_state(cache_dir)
    result = SyncResult(revision=revision)

    if revision and not force and state["revision"] == revision:
        # 订正①（task-8-review 裁决）：短路路径只产 revision + drift，skipped 死字段不赋值
        result.drift = scan_drift(cache_dir, models_dir)
        return result

    # 漂移必须在落盘**前**扫描（判据是上一轮登记的 sha）：本轮写盘会把本地改动
    # 改回中心版本，落盘后再扫永远为空——"drift=本轮覆盖掉了哪些本地手改"。
    result.drift = scan_drift(cache_dir, models_dir)

    previous = {str(g["goal_id"]): g for g in state["goals"]}
    entries: dict[str, dict[str, Any]] = {}
    for goal in goals:
        goal_id = str(goal.get("goal_id", ""))
        if not goal_id:
            continue
        problem = _validate(goal)
        if problem:
            result.rejected.append(goal_id)
            logger.warning(f"集群 sync 拒绝落盘 {goal_id}: {problem}")
            continue
        path = models_dir / str(goal["engine"]) / f"{goal['profile']}.yaml"
        text = str(goal["yaml"])
        before = None
        if path.is_file():
            try:
                before = path.read_text(encoding="utf-8")
            except Exception as exc:  # noqa: BLE001 — 非 UTF-8 等读不动按"无备份"覆盖：中心版本恢复权威即自愈
                logger.warning(f"集群 sync 读取 {path.name} 失败（按无备份覆盖）: {exc}")
        if before != text:
            _atomic_write(path, text, backup=before)
        entries[goal_id] = {"goal_id": goal_id, "profile": str(goal["profile"]),
                            "engine": str(goal["engine"]), "path": str(path),
                            "sha": profile_sha(text), "intent": str(goal.get("intent", "start")),
                            "params": goal.get("params"), "env_overlay": goal.get("env_overlay")}
        # T8（终审）：同 goal_id 换 engine（如 vllm→sglang 的同名 profile）时，旧引擎
        # 目录那份 YAML 已成孤儿——中心快照里再也不会出现该路径，prune 分支永远摸不到
        # 它。不删 = worker 上潜伏一份"中心不知道"的 YAML，被手工 load 后就是一台
        # 中心从未声明的幽灵服务。goal 未撤销故不进 pruned 账（撤销与换目录语义不同）。
        old = previous.get(goal_id)
        if old is not None:
            old_path = Path(str(old.get("path", "")))
            if old_path.name and old_path != path and old_path.is_file():
                _prune(old_path)
                logger.info(f"集群 sync：goal {goal_id} 换引擎，已清理旧文件 {old_path}")
        result.written.append(goal_id)

    # 被拒 ≠ 撤销：同 goal 的坏更新（sha 截断等）被拒时必须**维持上轮版本**——文件
    # 不剪枝、state 保留上轮 entry（否则下轮它变"未登记"，剪枝/漂移都失去保护对象）。
    # 删除语义只属于"goal 从快照消失"。否则一次传输截断就把服役中的模型文件毁掉。
    rejected_ids = set(result.rejected)
    for goal_id, old in previous.items():
        if goal_id in entries or goal_id in rejected_ids:
            entries.setdefault(goal_id, old)
            continue
        _prune(Path(str(old.get("path", ""))))
        result.pruned.append(goal_id)

    _write_json(_goals_path(cache_dir), {"revision": revision, "applied_at": now,
                                         "goals": list(entries.values())})
    _write_json(cache_dir / MARKER_FILE, {"revision": revision, "applied_at": now,
                                          "goal_count": len(entries)})
    return result


def _atomic_write(path: Path, text: str, *, backup: str | None) -> None:
    """同目录临时文件 + os.replace。任一步失败必须清掉 .tmp（不留半成品）。

    清理路径按继承树泛兜（BaseException）：磁盘满之外的编码/权限/未知异常同样
    不得固化半成品——.tmp 残留 + 下一轮同 revision 短路 = 半途状态永久固化。
    unlink 自身失败绝不允许吞掉原异常（嵌套 try）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        if backup is not None and backup != text:      # 内容真的变了才留 .master 回退
            _write_text_quiet(path.with_name(path.name + ".master"), backup)
        tmp.write_text(text, encoding="utf-8")
        # 走 Path.replace 而非 os.replace：语义相同（pathlib 内部即 os.replace），
        # 但保留 monkeypatch Path.replace 的注入缝——原子性测试靠它模拟 rename 失败。
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError as clean_exc:  # noqa: BLE001 — 清理失败只留痕，原异常照常上抛
            logger.warning(f"集群 sync 清理 {tmp.name} 失败: {clean_exc}")
        raise


def _write_text_quiet(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 — 备份失败不值得让整个 sync 回滚
        logger.warning(f"集群 sync 备份 {path.name} 失败（忽略）: {exc}")


def _prune(path: Path) -> None:
    """删除已撤销 goal 的 YAML。**只删登记过的路径**，绝不按目录扫描乱删。"""
    if not str(path):
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001 — 删不掉就报 drift，不阻断其他 goal
        logger.warning(f"集群 sync 剪枝失败 {path}: {exc}")


def scan_drift(cache_dir: Path, models_dir: Path) -> list[str]:
    """返回"中心声明过但本地文件已改/已删"的 goal_id（保序）。

    `models_dir` 参数保留给未来多根目录场景；当前以登记的绝对路径为准，这样
    worker 把 models 目录整体搬走后仍能准确报"文件不见了"而不是误判为未托管。
    """
    del models_dir  # 见 docstring：判定完全依据登记的绝对路径
    out: list[str] = []
    for g in read_state(cache_dir)["goals"]:
        path = Path(str(g.get("path", "")))
        want = str(g.get("sha", ""))
        try:
            got = profile_sha(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 文件不可读/非 UTF-8 一律计 drift（不匹配），绝不崩心跳
            out.append(str(g.get("goal_id", "")))
            continue
        if got != want:
            out.append(str(g.get("goal_id", "")))
    return out
