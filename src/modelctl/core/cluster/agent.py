#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/agent.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : worker 侧常驻 Agent：主动连中心、心跳上报、node_token 落盘、退避重连
# ===============================================================================

"""core/cluster/agent.py — WorkerAgent（设计文档 §5；reconciler 的 M0 前奏）。

阻塞式 websockets.sync.client 跑在 daemon 线程；M0 心跳仅 GPU 概要（模型级留 M1）。
welcome 带回的 node_token 写回 .env（set_env_values）并驻内存，下次重连免用一次性
join_token。ENV_PATH 模块级变量便于测试重定向。
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Any

from loguru import logger

from modelctl.core.cluster import config, wsproto
from modelctl.core.envfile import PROJECT_ROOT, set_env_values

ENV_PATH = PROJECT_ROOT / ".env"

_BACKOFF_MAX_S = 30


def ws_url(center_url: str, insecure: bool) -> str:
    """中心 http(s) URL → WS URL；insecure=True 强制 ws://（仅内网调试）。"""
    base = center_url.rstrip("/")
    if insecure:
        for prefix in ("https://", "http://"):
            if base.startswith(prefix):
                base = "ws://" + base[len(prefix):]
                break
        else:
            base = "ws://" + base
    elif base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    else:
        base = "ws://" + base
    return base + "/admin/api/ws/cluster"


def collect_heartbeat() -> dict[str, Any]:
    """M0 心跳 payload：GPU 概要 + 空 profiles/主机段（模型级采集属 M1 reconciler）。"""
    gpu: dict[str, Any] = {}
    try:
        from modelctl.core.capabilities import probe

        caps = probe()
        gpu = {"count": caps.gpu_count, "vram_total_mb_per_gpu": caps.vram_total_mb_per_gpu}
    except Exception as exc:  # noqa: BLE001 — 探测失败不阻断心跳
        logger.debug(f"心跳 GPU 探测失败（忽略）: {exc}")
    return {"profiles": {}, "gpu": gpu, "host": {}}


class WorkerAgent:
    """worker→中心长连接维护者。异常一律吞掉进退避重连，绝不让线程带崩宿主 webui。"""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event
        self._node_token = config.node_token()

    def _current_key(self) -> str:
        return self._node_token or config.join_token()

    def _persist_node_token(self, token: str) -> None:
        self._node_token = token
        try:
            set_env_values({"CLUSTER_NODE_TOKEN": token}, env_path=ENV_PATH)
        except OSError as exc:  # noqa: BLE001 — 落盘失败不影响本连接
            logger.warning(f"node_token 写回 .env 失败（内存态仍生效）: {exc}")

    def _connect_and_serve(self) -> None:
        from websockets.sync.client import connect

        url = ws_url(config.center_url(), config.ws_insecure())
        with connect(url, open_timeout=10) as ws:
            ws.send(wsproto.dumps(wsproto.make_hello(
                config.node_id(), config.lan_id(), self._current_key(),
                {"hostname": socket.gethostname()})))
            welcome = json.loads(ws.recv())
            if welcome.get("t") != "welcome":
                raise ConnectionError(f"中心拒绝注册: {welcome}")
            issued = str(welcome.get("node_token", ""))
            if issued and issued != self._node_token:
                self._persist_node_token(issued)
            interval = float(welcome.get("interval_s", config.heartbeat_interval_s()))
            while not self._stop.is_set():
                if self._stop.wait(interval):
                    break
                ws.send(wsproto.dumps(wsproto.make_heartbeat(collect_heartbeat())))
                ws.recv()  # 等 ack，保持请求-应答有序

    def run(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            if not (config.center_url() and config.node_id()):
                logger.debug("缺 CLUSTER_CENTER_URL/CLUSTER_NODE_ID，Agent 暂不连接")
                if self._stop.wait(backoff):
                    break
                continue
            try:
                self._connect_and_serve()
                backoff = 1  # 正常断开（如中心重启）后从 1s 重新起步
            except Exception as exc:  # noqa: BLE001 — 连接级异常统一退避重连
                logger.warning(f"集群中心连接中断，{backoff}s 后重连: {exc}")
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, _BACKOFF_MAX_S)


def start_agent_in_background() -> threading.Thread | None:
    """webui server 启动钩子：worker/both 角色且配置齐备时起 daemon 线程，否则 None。"""
    if not (config.is_worker() and config.center_url() and config.node_id()):
        return None
    stop = threading.Event()
    thread = threading.Thread(target=WorkerAgent(stop_event=stop).run,
                              name="modelctl-worker-agent", daemon=True)
    thread.start()
    return thread
