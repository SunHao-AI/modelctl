#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/admin_chat.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/12 10:00
# @Desc   : AI 对话（模型调试台）代理端点
# ===============================================================================

"""core/webui/admin_chat.py — AI 对话（模型调试台）代理端点。

由 `_include_subrouter` 挂 `/chat` 前缀，最终路径 `/admin/api/chat/completions`。
鉴权走**管理面** `require_auth`（Bearer API_KEY）：前端只持有管理面 token，
数据面 `GATEWAY_CLIENT_API_KEY` 默认空且刻意不进浏览器（见 .env.example:118）。

复用 Task 1 抽出的 `prepare_openai_upstream`，两种模式：
  - route_mode=direct （默认）：把 model 当精确 profile 名，禁家族/上下文/默认回退，
    选谁打谁——调试结果可信；
  - route_mode=gateway：完整复现网关路由，回答"外部客户端打这个名字会落到哪"。
两种都**不走 HTTP 自调用 /v1**：复现路由判定即可，不必占两条连接、不必占数据面
并发槽、不必计入 accounts 记账与限流（调试流量不该污染生产统计）。

上游 4xx/5xx 原样透传状态码与响应体，并同样带上 X-Chat-Routed-To——"打给谁却报
400"是最常见的调试场景。落点信息走响应头而非自造 SSE 帧：前端一次 fetch 就能拿全。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from loguru import logger

from modelctl.core.webui.admin_auth import require_auth

router = APIRouter()

# base64 图片必须有界：24 MB 约合 6 张 1280px JPEG（前端已压到 200-400 KB）
_MAX_BODY_BYTES = 24 * 1024 * 1024
# 透传给上游的采样字段白名单（未知字段丢弃，不整体转发调用方 JSON）
_PASSTHROUGH_FIELDS = ("temperature", "top_p", "max_tokens")


def _router() -> APIRouter:
    return router


def _err(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"message": message, "type": "invalid_request_error", **extra}})


@router.post("/completions")
async def chat_completions(request: Request, _: None = Depends(require_auth)):
    # 经模块属性访问 is_model_available，测试可 patch modelctl.core.gateway.is_model_available
    from modelctl.core import gateway as gw

    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        return _err(413, "请求体超过 24MB 上限（图片过大）")
    try:
        payload = json.loads(raw or b"{}")
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        return _err(400, "请求体必须是 JSON 对象")

    model = str(payload.get("model") or "")
    route_mode = str(payload.get("route_mode") or "direct")
    if route_mode not in ("direct", "gateway"):
        return _err(400, f"route_mode 只能是 direct/gateway：{route_mode}")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return _err(400, "messages 不能为空")

    state = request.app.state
    # system 由服务端转成首条 system 消息：前端消息数组里不存 system，避免与历史混淆
    sys_prompt = payload.get("system")
    upstream_messages = (
        [{"role": "system", "content": sys_prompt}] if isinstance(sys_prompt, str) and sys_prompt else []
    ) + list(messages)

    gateway_body: dict = {"model": model, "messages": upstream_messages, "stream": True}
    for field in _PASSTHROUGH_FIELDS:
        if payload.get(field) is not None:
            gateway_body[field] = payload[field]
    if payload.get("include_usage", True):
        # 统计全靠 usage；个别引擎的 OpenAI 兼容层不认 stream_options → 前端关掉重发
        gateway_body["stream_options"] = {"include_usage": True}

    if route_mode == "direct":
        target = state.gateway_registry.get(model)
        # direct 语义 = 精确 profile 名：注册表键可能是 alias / group 名，
        # 与 profile.name 不一致时视为未命中（不做家族解析，选谁打谁）
        if target is None or target.name != model:
            return _err(404, f"model not found: {model}")
        if not gw.is_model_available(target):
            return JSONResponse(status_code=409, content={"error": {
                "code": "model_not_running", "message": f"模型未运行：{model}",
                "type": "invalid_request_error"}})
        # 单元素注册表 + 空 groups/rules/default → 不可能发生家族/上下文跳转
        prepared = gw.prepare_openai_upstream({model: target}, {}, None, None, {},
                                              gateway_body, "chat/completions")
    else:
        prepared = gw.prepare_openai_upstream(
            state.gateway_registry, state.gateway_groups,
            getattr(state, "group_route_cache", None),
            state.gateway_default_model, state.gateway_context_rules,
            gateway_body, "chat/completions",
        )
        if isinstance(prepared, gw.PreparedError):
            return JSONResponse(status_code=prepared.status_code, content=prepared.payload)
        if not gw.is_model_available(prepared.target):
            return JSONResponse(status_code=409, content={"error": {
                "code": "model_not_running", "message": f"路由目标未运行：{prepared.target.name}",
                "type": "invalid_request_error"}})

    route_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "X-Chat-Routed-To": prepared.target.name,
        "X-Chat-Route-Reason": prepared.route_reason or "",
    }

    import httpx

    client = httpx.AsyncClient(
        timeout=float(getattr(state, "gateway_read_timeout", 600.0)),
        transport=getattr(state, "chat_transport", None),
    )
    try:
        req = client.build_request("POST", prepared.url, json=prepared.body, headers=prepared.headers)
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as error:
        await client.aclose()
        logger.warning(f"chat 上游不可达 model={prepared.target.name}: {error}")
        return JSONResponse(status_code=502, content={"error": {
            "message": f"后端不可达：{error}", "type": "upstream_error"}})

    if upstream.status_code >= 400:
        content = await upstream.aread()
        await client.aclose()
        return Response(status_code=upstream.status_code, content=content,
                        media_type=upstream.headers.get("content-type"), headers=route_headers)

    async def relay():
        # 生命周期教训同 gateway.proxy 流式分支：不能用 async with 包住再返回
        # StreamingResponse——SSE 惰性迭代，客户端会在端点返回时提前切断。
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(relay(),
                             media_type=upstream.headers.get("content-type", "text/event-stream"),
                             headers=route_headers)
