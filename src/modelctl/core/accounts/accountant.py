#!/usr/bin/env python3
# ===============================================================================
# @File   : src/modelctl/core/accounts/accountant.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/9 11:45
# @Desc   : 请求完成后的异步记账队列（usage + budget + sessions/messages）
# ===============================================================================

"""core/accounts/accountant.py — 数据面 settle 路径的落库 sink。

设计动机：

- 数据面（gateway）在主链路完成后**必须**记录 usage / 对话，但主链路本身
  已经承担了 prompt + completion 的算力开销——**再串行**等 `store` SQLite
  写会把 p99 延迟再抬一次。改为 `settle` 非阻塞入队 + 后台线程 drain。
- 关键约束：**`stop()` 前排空**（同步 drain），进程退出不能丢消息；
  测试路径 (`_drain_once`) 完全不走 worker 线程同步跑，稳定可复现。

**行为契约**（测试以此为 TDD 基准）：

- 单次 settle 副作用（按写入顺序）：
  1. `usage_records` +1 行（含 `total_tokens = p + c`、`status` 透传、`key_id`
     透传）。
  2. `users.budget_consumed` += `p + c` — 仅当 `p + c > 0`（避免 0 token
     触发一次无谓的 UPDATE 打脏 `updated_at`）。
  3. `sessions` upsert-式（`get_or_create_session`）：显式 `session_id`
     命中即复用，避免每轮对话都新建会话；无 `session_id` 时按
     (user_id, key_id, model) 空闲窗口聚合。
  4. `messages`：`user_msg` / `assistant_msg` 各自非空才写（`""` 一律视
     为不写）。
  5. `bump_session(sid, count=写消息数, now)`：单条 UPDATE 保持
     `message_count` 与 `messages` 表行数原子一致（Task 5a 契约）。
- **`request_id`**：调用方（Task 6）应传；未传时**回落** `uuid.uuid4().hex`
  作为可追溯 id 落库（本质是 "这条 usage 记录是否有上游追踪 id"——settle
  层不判空语义，避免多分支）。
- **异常隔离**：单个 job 抛错**只记 log**，不 re-raise、不终止 worker、
  不影响后续 job。使用 `logging.getLogger(__name__)`；Task 9 统一走 accounts
  域 logger 再改。
- **`stop()` 幂等**：多次 start/stop 不泄漏线程；`stop` 内先 join 后台线程
  再**同步** `_drain_once` 一遍，保证 stop 返回时队列为空。
- **`start()` 幂等**：多次 start 只起一个 worker 线程。

**`now` 完全来自 `settle` 入参**：本模块**不取时钟**——生产调用点在 Task 6
用 `time.time()`；测试用固定 float 精确断言。
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from typing import Any

logger = logging.getLogger(__name__)  # Task 9 统一到 accounts 域再改


class Accountant:
    """对 `store` 做异步落库（消息队列 + 后台 worker；测试可通过
    `_drain_once` 同步驱动而不启动 worker 线程）。
    """

    _WORKER_POLL_S = 0.1

    def __init__(self, store: Any) -> None:
        self._store = store
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 队列 API
    # ------------------------------------------------------------------

    def settle(self, *,
               user_id: int,
               key_id: int | None,
               model: str,
               prompt_tokens: int,
               completion_tokens: int,
               status: str,
               client_ip: str = "",
               ttft_ms: int | None = None,
               latency_ms: int | None = None,
               session_id: str | None = None,
               title: str = "",
               user_msg: str = "",
               assistant_msg: str = "",
               request_id: str = "",
               now: float) -> None:
        """非阻塞入队一条 settle job。

        所有字段透传到 `_process`，`now` 亦入队（不取时钟，便于测试）。
        `request_id` 为空串时 `_process` 回落到 `uuid.uuid4().hex`。

        **`queue.Queue` 本身无界**：调用方"不受背压"是刻意的（数据面已抵
        达 settle 阶段，此时阻塞会直接放大主链路延迟并堆积）。真实使用
        场景下 QPS 天花板由 `LimitGuard` 四道防线上探，不会淹没 sink。
        """
        self._queue.put({
            "user_id": int(user_id),
            "key_id": (int(key_id) if key_id is not None else None),
            "model": str(model),
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "status": str(status),
            "client_ip": str(client_ip or ""),
            "ttft_ms": (int(ttft_ms) if ttft_ms is not None else None),
            "latency_ms": (int(latency_ms) if latency_ms is not None else None),
            "session_id": (str(session_id) if session_id else None),
            "title": str(title or ""),
            "user_msg": str(user_msg or ""),
            "assistant_msg": str(assistant_msg or ""),
            "request_id": str(request_id or ""),
            "now": float(now),
        })

    def start(self) -> None:
        """启动后台 worker 线程（幂等）。

        未 `start()` 期间 `settle` 只是入队，`_drain_once` / `_worker_loop`
        都可驱动消费——这样测试不需要延迟 worker 时序（`settle()` 后 `_drain
        _once()` 直接同步断言，isolation 一清二楚）。
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._worker_loop, name="accounts-accountant", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止 worker 并**排空**队列后再返回（幂等）。

        "排空"是本模块的核心承诺：网关进程退出前应调 `stop()`，任何尚未
        drain 的 settle 都会在此刻同步落库——不丢消息。多次 `stop()` 之间
        no-op（第二次调用时 `self._thread` 仍指向已结束的线程对象、`is_alive
        ()=False`，走 join 分支是幂等的）。
        """
        self._stop_evt.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        # 无论 worker 是否已 stop，最终同步 drain 一遍，确保 stop 返回时队列空
        try:
            self._drain_once()
        except Exception:  # pragma: no cover
            logger.exception("accountant.stop drain final sweep raised")
        self._thread = None

    # ------------------------------------------------------------------
    # 内部：消费 worker 线程 & 单 job 处理
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        """后台线程：poll 队列有活就干，没活就 sleep 一小段。"""
        while not self._stop_evt.is_set():
            try:
                job = self._queue.get(timeout=self._WORKER_POLL_S)
            except queue.Empty:
                continue
            try:
                self._process(job)
            except Exception:
                # 契约：单 job 抛错只 log 不 re-raise；后续 job 还要处理
                logger.exception("accountant._process job failed")
            finally:
                self._queue.task_done()

    def _drain_once(self) -> int:
        """同步 drain 队列直到空（测试或 `stop` 的末次 sweep 用）。

        返回本次处理的 job 数——便于测试观察 "修好前先记 N，修好后 drain
        出现 M" 这种 diff 断言；生产路径上返回值没人看。抛错约定同
        `_worker_loop`（cap log、继续）。`queue.Empty` 时 break。

        **不区分来源**：worker 线程活着时 `settle` 与 `_drain_once` 都可能
        消费队列——`queue.Queue.get_nowait` 本身线程安全，任一成功拿走就
        nil，不会双写。
        """
        n = 0
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._process(job)
            except Exception:
                logger.exception("accountant._drain_once job failed")
            finally:
                self._queue.task_done()
                n += 1
        return n

    def _process(self, job: dict[str, Any]) -> None:
        """单条 settle job 落库。异常**上抛给调用方**（由 `_worker_loop` /
        `_drain_once` 统一 cap log 后跳过——本方法不吞）。

        写入顺序见模块 docstring "行为契约"。`request_id` 缺省回落 uuid4。
        """
        store = self._store
        rid = job.get("request_id") or uuid.uuid4().hex
        prompt = max(0, int(job.get("prompt_tokens") or 0))
        completion = max(0, int(job.get("completion_tokens") or 0))
        total = prompt + completion
        now = float(job.get("now") or 0.0)
        user_id = int(job.get("user_id"))
        key_id = job.get("key_id")

        store.insert_usage(
            user_id=user_id,
            key_id=key_id,
            request_id=rid,
            model=str(job.get("model") or ""),
            prompt_tokens=prompt,
            completion_tokens=completion,
            client_ip=str(job.get("client_ip") or ""),
            ttft_ms=job.get("ttft_ms"),
            latency_ms=job.get("latency_ms"),
            status=str(job.get("status") or ""),
            now=now,
        )
        if total > 0:
            store.increment_budget_consumed(user_id, total, now=now)

        session_row = store.get_or_create_session(
            user_id=user_id,
            key_id=key_id,
            model=str(job.get("model") or ""),
            session_id=job.get("session_id"),
            title=str(job.get("title") or ""),
            now=now,
        )
        sid = int(session_row["id"])
        user_msg = str(job.get("user_msg") or "")
        assistant_msg = str(job.get("assistant_msg") or "")
        n_msgs = 0
        if user_msg:
            store.add_message(session_id=sid, role="user", content=user_msg,
                              prompt_tokens=prompt,
                              completion_tokens=0, now=now)
            n_msgs += 1
        if assistant_msg:
            store.add_message(session_id=sid, role="assistant", content=assistant_msg,
                              prompt_tokens=0,
                              completion_tokens=completion, now=now)
            n_msgs += 1
        if n_msgs:
            store.bump_session(sid, count=n_msgs, now=now)
