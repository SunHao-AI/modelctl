#!/usr/bin/env python3
"""core/gateway.py — 轻量 OpenAI 兼容网关（按请求体 model 参数路由）。

将本节点 models/*.yaml 中的模型注册为 OpenAI 兼容后端，按请求体中的 model 字段路由到对应引擎端口；
未知/省略 model 回退默认模型。
依赖 fastapi / uvicorn / httpx（可选 extra "gateway"），本模块顶部不导入。
独立运行：    python -m modelctl.core.gateway

注意：本文件不使用 `from __future__ import annotations`。因为 fastapi/httpx 需
延迟到 create_app() 内部导入（避免未安装 gateway extra 时破坏既有命令），若开启
字符串注解，路由处理器里的 `request: Request` 将因 Request 不在模块命名空间而无法
被 FastAPI 解析为依赖注入对象；关闭后注解即时求值，可在 create_app 局部作用域解析。
"""

import os
from dataclasses import dataclass
from pathlib import Path

from modelctl.core.capabilities import Capabilities
from modelctl.core.process import wait_health
from modelctl.core.profile import list_profiles
from modelctl.engines import get_adapter

GATEWAY_PORT = 5003


@dataclass
class GatewayModel:
    name: str
    engine: str
    backend_url: str
    upstream_model: str
    api_key: str | None
    health_url: str


def build_registry(models_dir: Path | None = None, host: str = "127.0.0.1") -> dict[str, GatewayModel]:
    """从 models/*.yaml 构建 模型名 -> 后端信息 注册表（单一来源）。"""
    registry: dict[str, GatewayModel] = {}
    for profile in list_profiles(models_dir):
        adapter = get_adapter(profile.engine)(profile, Capabilities())
        registry[profile.name] = GatewayModel(
            name=profile.name,
            engine=profile.engine,
            backend_url=f"http://{host}:{profile.port}",
            upstream_model=adapter.upstream_model_name(),
            api_key=profile.api_key,
            health_url=adapter.health_url(),
        )
    return registry


def resolve_model(
    registry: dict[str, GatewayModel], body_model: str | None, default_model: str | None
) -> GatewayModel | None:
    """按 body.model 解析目标模型；未知或省略均回退 default_model，都不可用时返回 None。"""
    if body_model and body_model in registry:
        return registry[body_model]
    if default_model and default_model in registry:
        return registry[default_model]
    return None


def is_model_healthy(model: GatewayModel, timeout: float = 2.0) -> bool:
    """后端存活探测（复用 process.wait_health，失败不抛异常）。"""
    return wait_health(model.health_url, timeout, model.api_key)


def create_app(
    registry: dict[str, GatewayModel] | None = None,
    default_model: str | None = None,
    read_timeout: float = 600.0,
    transport=None,
):
    """构建 FastAPI 网关应用（transport 供测试注入 httpx.MockTransport）。

    环境变量：GATEWAY_DEFAULT_MODEL（默认模型，缺省/未知 model 回退目标）。
    """
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    registry = registry if registry is not None else build_registry()
    default_model = default_model or os.environ.get("GATEWAY_DEFAULT_MODEL")
    app = FastAPI(title="modelctl gateway", docs_url="/docs", openapi_url="/openapi.json")

    @app.get("/v1/models")
    async def list_models() -> dict:
        data = [
            {"id": m.name, "object": "model", "created": 0, "owned_by": "modelctl"}
            for m in registry.values()
            if is_model_healthy(m)
        ]
        return {"object": "list", "data": data}

    @app.post("/v1/{path:path}")
    async def proxy(path: str, request: Request):
        if path not in ("chat/completions", "completions", "embeddings"):
            return JSONResponse(
                status_code=404,
                content={"error": {"message": f"unknown endpoint: /v1/{path}", "type": "invalid_request_error"}},
            )
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "请求体必须是 JSON", "type": "invalid_request_error"}},
            )
        target = resolve_model(registry, body.get("model"), default_model)
        if target is None:
            err_msg = f"model not found: {body.get('model')}"
            return JSONResponse(
                status_code=404,
                content={"error": {"message": err_msg, "type": "invalid_request_error"}},
            )
        # 改写为后端期望的模型名（ollama 严格校验，llamacpp 忽略）
        body["model"] = target.upstream_model
        headers = {"Content-Type": "application/json"}
        auth = request.headers.get("Authorization") or (f"Bearer {target.api_key}" if target.api_key else None)
        if auth:
            headers["Authorization"] = auth
        url = f"{target.backend_url}/v1/{path}"
        async with httpx.AsyncClient(timeout=read_timeout, transport=transport) as client:
            try:
                if body.get("stream"):
                    req = client.build_request("POST", url, json=body, headers=headers)
                    upstream = await client.send(req, stream=True)  # stream=True：连接保持打开，逐块读 SSE
                    ctype = upstream.headers.get("content-type")
                    if upstream.status_code >= 400:
                        content = await upstream.aread()
                        return Response(status_code=upstream.status_code, content=content, media_type=ctype)
                    return StreamingResponse(_iter_stream(upstream), status_code=upstream.status_code, media_type=ctype)
                upstream = await client.post(url, json=body, headers=headers)
                return Response(
                    status_code=upstream.status_code,
                    content=upstream.content,
                    media_type=upstream.headers.get("content-type"),
                )
            except httpx.HTTPError as error:
                err_msg = f"后端不可达：{error}"
                return JSONResponse(status_code=502, content={"error": {"message": err_msg, "type": "upstream_error"}})

    return app


async def _iter_stream(upstream):
    """逐块透传后端响应体（SSE 流式）。"""
    async for chunk in upstream.aiter_bytes():
        yield chunk
