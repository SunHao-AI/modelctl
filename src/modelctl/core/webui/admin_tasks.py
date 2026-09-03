#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_tasks.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/2 10:00
# @Desc   : 异步任务管理与 SSE 事件广播
# ===============================================================================

"""core/webui/admin_tasks.py — 长任务注册表 + 每 profile 互斥 + SSE 事件广播。

冷启动最长 600s，start/restart/setup/build 等长操作必须以异步任务跑在后台线程
并推送 SSE 进度，不能同步阻塞请求。每个 Task 持有独立的订阅者队列（SSE 客户端
各自 add 一个 asyncio.Queue），事件经 call_soon_threadsafe 跨线程投递；同一
target（profile/service 名）用 asyncio.Lock 互斥，避免并发双开。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

# 任务状态：排队/运行中/成功/跳过/失败
TaskStatus = Literal["queued", "running", "success", "skipped", "error"]

# 注册表保留上限（容量控制，超出按最旧裁剪）
_MAX_TASKS = 200


def _now_iso() -> str:
    """本地时区 ISO 时间戳（毫秒精度），用于 started_at / finished_at。"""
    return _dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass
class Task:
    """单次后台操作（模型/服务/环境/编译）的生命周期与进度快照。"""

    id: str
    kind: str  # model_start|service_start|all_start|env_setup|trtllm_build
    action: str  # start|stop|restart|setup|remove|build
    target: str  # profile 名或 service 名
    status: TaskStatus = "queued"
    exit_code: int = 0
    started_at: str = ""  # ISO 时间戳
    finished_at: str | None = None
    detail: str | None = None
    logs: list[str] = field(default_factory=list)
    _subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        """对外序列化；排除 _subscribers 内部队列。"""
        return {
            "id": self.id,
            "kind": self.kind,
            "action": self.action,
            "target": self.target,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "detail": self.detail,
            "logs": self.logs,
        }

    def subscribe(self) -> asyncio.Queue:
        """SSE 客户端订阅：加入独立队列，返回该队列供生成器迭代。"""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """SSE 客户端断开：移除队列（幂等）。"""
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def event(self, event_type: str, data: dict) -> None:
        """向所有订阅者广播一条 SSE 事件（线程安全，经 call_soon_threadsafe）。

        事件文本符合 SSE 规范：`event: <type>` + `data: <json>` + 空行分隔。
        循环未运行时退化为同步 put_nowait；队列关闭/取消时静默吞掉，绝不
        让广播异常回灌任务线程。
        """
        payload = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for q in list(self._subscribers):
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.call_soon_threadsafe(q.put_nowait, payload)
                else:
                    q.put_nowait(payload)
            except (RuntimeError, asyncio.exceptions.CancelledError):
                pass

    # ------------------------------------------------------------------
    # 状态更新辅助方法（供 admin_models / admin_services 等调用方使用）
    # ------------------------------------------------------------------

    def update_status(self, status: TaskStatus) -> None:
        """更新任务状态并广播 step 事件。started_at 在首次设为 running 时填充。"""
        if status == "running" and not self.started_at:
            self.started_at = _now_iso()
        self.status = status
        self.event("step", {"step": 0, "label": self.target, "status": status, "task_id": self.id})

    def update_detail(self, detail: str) -> None:
        """更新任务详情（不改变 status）。"""
        self.detail = detail

    def complete(self) -> None:
        """标记任务成功完成并广播 done 事件。"""
        self.status = "success"
        self.exit_code = 0
        self.finished_at = _now_iso()
        self.event("done", {"status": "success", "exit_code": 0, "task_id": self.id})

    def error(self, exit_code: int = 1, message: str = "") -> None:
        """标记任务失败并广播 done 事件。exit_code: 2=配置错误, 1=运行错误。"""
        self.status = "error"
        self.exit_code = exit_code
        self.detail = message or self.detail
        self.finished_at = _now_iso()
        self.event("done", {"status": "error", "exit_code": exit_code, "message": self.detail, "task_id": self.id})

    def log_line(self, line: str) -> None:
        """追加一行日志并广播 log 事件。"""
        self.logs.append(line)
        if len(self.logs) > 500:
            self.logs = self.logs[-500:]
        self.event("log", {"line": line})


class TaskManager:
    """应用级任务注册表：id->Task（容量裁剪）+ target->asyncio.Lock（互斥）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._created_at: float = time.time()

    def create_task(self, kind: str, action: str, target: str) -> Task:
        """新建并登记一个任务（status=queued，started_at 置当前时间）。"""
        task = Task(id=f"task-{uuid.uuid4().hex[:12]}", kind=kind, action=action, target=target)
        self._tasks[task.id] = task
        self.trim()
        return task

    def get_task(self, task_id: str) -> Task | None:
        """按 id 取任务；不存在返回 None。"""
        return self._tasks.get(task_id)

    def list_tasks(self, limit: int = _MAX_TASKS) -> list[Task]:
        """最近的任务列表，最新在前（按 started_at 倒序；空值视为最早）。"""
        ordered = sorted(
            self._tasks.values(),
            key=lambda t: t.started_at or "",
            reverse=True,
        )
        return ordered[:limit]

    def get_lock(self, target: str) -> asyncio.Lock:
        """取 target 的互斥锁（不存在则创建）。锁存于 manager 单例，跨调用复用。"""
        lock = self._locks.get(target)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[target] = lock
        return lock

    async def acquire(self, target: str, action: str) -> asyncio.Lock | None:
        """尝试获取指定 target+action 的锁。

        如果已有同一 target 的活动任务返回 None（调用方应返回 409），
        否则返回锁对象（调用方在 finally 中 release）。
        """
        lock = self.get_lock(target)
        if lock.locked():
            return None
        await lock.acquire()
        return lock

    async def release(self, target: str, action: str) -> None:
        """释放 target 的互斥锁（幂等）。"""
        lock = self._locks.get(target)
        if lock is not None and lock.locked():
            lock.release()

    def is_active(self, target: str) -> bool:
        """指定 target 是否有活动任务（running/queued）。"""
        for t in self._tasks.values():
            if t.target == target and t.status in ("queued", "running"):
                return True
        return False

    def uptime(self) -> float:
        """任务管理器存活秒数。"""
        return time.time() - self._created_at

    def trim(self) -> None:
        """容量裁剪：仅保留最近 _MAX_TASKS 个任务（最旧先删）。"""
        if len(self._tasks) <= _MAX_TASKS:
            return
        oldest = sorted(self._tasks.values(), key=lambda t: t.started_at or "")
        overflow = len(self._tasks) - _MAX_TASKS
        for task in oldest[:overflow]:
            self._tasks.pop(task.id, None)
