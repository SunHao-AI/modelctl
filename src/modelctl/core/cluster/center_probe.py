#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/center_probe.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : CLI 侧中心 HTTP 探活/join 预检（stdlib urllib，无 httpx 依赖）
# ===============================================================================

"""core/cluster/center_probe.py — CLI 到中心 REST 的最小 HTTP 客户端。

主包不依赖 httpx（gateway 子项目专属），此处 stdlib urllib 够用：短超时 + JSON 解析，
网络异常一律折叠为 (-1, {"error": ...})，由调用方决定用户提示。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def _request(method: str, url: str, payload: dict | None, api_key: str, timeout: float) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    try:
        # Request 构造须在 try 内：URL 缺 scheme 时其 _parse() 即抛 ValueError（urlopen 之前）
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — 内网 http，scheme 由调用方保证
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, _safe_json(body)
    except urllib.error.HTTPError as exc:
        return exc.code, _safe_json(exc.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        # ValueError：center_url 缺 scheme（如 "mycenter"）时 Request/urlopen 直接抛，须一并折叠
        return -1, {"error": str(exc)}


def _safe_json(body: str) -> dict:
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {"data": parsed}
    except ValueError:
        return {"raw": body[:200]}


def get_json(url: str, api_key: str = "", timeout: float = 5.0) -> tuple[int, dict]:
    return _request("GET", url, None, api_key, timeout)


def post_json(url: str, payload: dict, api_key: str = "", timeout: float = 5.0) -> tuple[int, dict]:
    return _request("POST", url, payload, api_key, timeout)


def check_join(center_url: str, token: str, node_id: str, lan: str = "") -> tuple[bool, str, str]:
    """join 预检：(ok, node_token, message)。center_url 末尾斜杠容错。"""
    base = center_url.rstrip("/")
    status, body = post_json(f"{base}/admin/api/cluster/join-check",
                             {"node_id": node_id, "key": token, "lan": lan})
    if status == 200 and body.get("ok"):
        return True, str(body.get("node_token", "")), ""
    if status == -1:
        return False, "", f"中心不可达: {body.get('error', '未知错误')}"
    detail = body.get("detail")
    message = detail if isinstance(detail, str) else f"HTTP {status}"
    return False, "", message
