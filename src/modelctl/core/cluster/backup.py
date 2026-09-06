#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/backup.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : 中心台账热备/校验/恢复（M2 spec §2.5，零 DDL）
# ===============================================================================

"""core/cluster/backup.py — sqlite backup API 热备 + 校验 + 停机恢复。

restore 前置"中心未运行"是本模块的安全边界：运行中的 webui 进程持着 SQLite 连接，
在线替换其底层文件 = 让进程对着被抽换的 inode 继续写 WAL，损坏是必然不是偶然。
因此 restore 仅 CLI 可达（无 REST 端点）；_center_running 单独成函数供测试注入。
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from loguru import logger

_REQUIRED_TABLES = frozenset({"nodes", "goals", "model_states", "events", "meta"})


class BackupError(Exception):
    """备份面业务失败：CLI 转退出码 2，REST 转 400。"""


def _live_db_path() -> Path:
    from modelctl.core.cluster.store import ClusterStore

    return ClusterStore().db_path


def _center_running() -> bool:
    from modelctl.core.process import is_running
    from modelctl.core.webui.server import WEBUI_INSTANCE

    return is_running(WEBUI_INSTANCE)


def create_backup(dest: Path, *, force: bool = False) -> dict[str, Any]:
    """当前台账 → dest 的一致性热备（在线可执行，不阻写）。返回 {sha256, bytes}。"""
    dest = Path(dest)
    if not dest.parent.is_dir():
        raise BackupError(f"目标目录不存在: {dest.parent}")
    if dest.exists() and not force:
        raise BackupError(f"目标已存在（覆盖需 --force）: {dest}")
    src = _live_db_path()
    if not src.is_file():
        raise BackupError(f"台账不存在，请先 cluster init: {src}")
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        # sqlite3 的 with 只结束事务不关连接；Windows 上残留句柄会让随后的 replace 报
        # WinError 32（另一程序占用），故显式 closing 后再改名，语义与非 Windows 一致。
        with contextlib.closing(sqlite3.connect(str(src))) as src_conn:
            with contextlib.closing(sqlite3.connect(str(tmp))) as dst_conn:
                src_conn.backup(dst_conn)      # online backup API：读锁不阻写者
                dst_conn.commit()
        data = tmp.read_bytes()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.replace(dest)
    except (sqlite3.Error, OSError) as exc:   # read_bytes/replace 在 Windows dest 被占用时抛 PermissionError
        tmp.unlink(missing_ok=True)
        raise BackupError(f"备份失败: {exc}") from exc
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def verify_backup(src: Path) -> tuple[bool, str]:
    """可打开 + integrity_check ok + 必备表齐备。join_token 缺失仅告警（老备份可能无）。"""
    src = Path(src)
    if not src.is_file():
        return False, f"文件不存在: {src}"
    try:
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return False, f"无法打开: {exc}"
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or str(row[0]).lower() != "ok":
            return False, f"integrity_check 失败: {row[0] if row else '无结果'}"
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = _REQUIRED_TABLES - have
        if missing:
            return False, f"缺必备表: {sorted(missing)}"
        jt = conn.execute("SELECT value FROM meta WHERE key='join_token'").fetchone()
        if jt is None:
            logger.warning("备份无 join_token（老备份可接受；下次备份后消失）")
        return True, ""
    except sqlite3.Error as exc:
        return False, f"校验失败: {exc}"
    finally:
        conn.close()


def restore_backup(src: Path, *, assume_stopped: bool = False) -> Path:
    """校验通过 → 现库就近 .bak → 文件替换（清 -wal/-shm）→ 事件写新库。返回 .bak 路径。"""
    src = Path(src)
    ok, reason = verify_backup(src)
    if not ok:
        raise BackupError(f"备份校验失败，现库未做任何改动: {reason}")
    if not assume_stopped and _center_running():
        raise BackupError("中心 webui 运行中：请先 modelctl webui stop 再恢复")
    db = _live_db_path()
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = db.with_name(db.name + f".pre-restore.{ts}.bak")
    if db.is_file():
        # 台账是 WAL 模式（store.py 强制 journal_mode=WAL）：中心崩溃/PID 残留被误判
        # "未运行"时，-wal 里可能压着已提交未 checkpoint 的事务，裸 copyfile 出来的安全网
        # 会静默缺最近数据。走 backup API 才能取到含 WAL 的一致性快照。
        try:
            with contextlib.closing(sqlite3.connect(str(db))) as src_conn:
                with contextlib.closing(sqlite3.connect(str(bak))) as dst_conn:
                    src_conn.backup(dst_conn)
                    dst_conn.commit()
        except (sqlite3.Error, OSError) as exc:
            bak.unlink(missing_ok=True)
            raise BackupError(f"恢复前安全网备份失败，现库未做任何改动: {exc}") from exc
    # 覆写活库必须原子：copyfile 直写 db 中途失败 = 半截库。先写同目录临时文件再改名。
    tmp = db.with_name(db.name + ".restore-tmp")
    try:
        shutil.copyfile(src, tmp)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"恢复失败（现库未做任何改动）: {exc}") from exc
    for suffix in ("-wal", "-shm"):           # 陈旧 WAL/SHM 配新主文件 = 日志回放进错库
        side = db.with_name(db.name + suffix)
        side.unlink(missing_ok=True)
    try:
        tmp.replace(db)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"恢复失败（现库未做任何改动）: {exc}") from exc
    from modelctl.core.cluster.store import ClusterStore

    store = ClusterStore(db)
    store.init_db()                            # 老备份缺 M1 列时经幂等补列（只增不删）
    store.append_event("db.restore", payload={"source": str(src)})
    return bak
