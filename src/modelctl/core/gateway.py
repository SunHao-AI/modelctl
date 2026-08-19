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

import asyncio
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from modelctl.core.capabilities import Capabilities
from modelctl.core.envfile import load_env
from modelctl.core.process import is_running
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
    # 对外模型标识（/v1/models 返回的 id），缺省用 name
    aliases: list[str] = field(default_factory=list)


def build_registry(models_dir: Path | None = None, host: str = "127.0.0.1") -> dict[str, GatewayModel]:
    """从 models/*.yaml 构建 模型名/别名 -> 后端信息 注册表（单一来源）。

    profile 的 name 与其 alias 都注册为 key，指向同一 GatewayModel；
    冲突（如不同 profile 声明相同别名）时保留先注册者并告警。
    """
    registry: dict[str, GatewayModel] = {}
    for profile in list_profiles(models_dir):
        adapter = get_adapter(profile.engine)(profile, Capabilities())
        model = GatewayModel(
            name=profile.name,
            engine=profile.engine,
            backend_url=f"http://{host}:{profile.port}",
            upstream_model=adapter.upstream_model_name(),
            api_key=profile.api_key,
            health_url=adapter.health_url(),
            aliases=profile.aliases,
        )
        for key in [profile.name, *profile.aliases]:
            if key in registry:
                logger.warning(f"模型标识 {key} 冲突，已保留 {registry[key].name}，忽略 {profile.name}")
                continue
            registry[key] = model
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
    """后端存活探测（单次探测，连接失败立即返回 False，不重试等待）。

    不再复用 process.wait_health（其内部 sleep 重试会让未运行模型各耗时约 2s，
    /v1/models 对注册表全部模型串行探测时会累积到十几秒）。
    """
    headers = {"Authorization": f"Bearer {model.api_key}"} if model.api_key else {}
    try:
        req = urllib.request.Request(model.health_url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


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
        # 注册表同时含 name 与 alias 两个 key（指向同一 GatewayModel），须按 name 去重
        seen: set[str] = set()
        models = []
        for m in registry.values():
            if m.name not in seen:
                seen.add(m.name)
                models.append(m)
        # 并发健康探测：串行会让未运行模型各耗 timeout 秒，10 个模型累积到十几秒。
        # 过滤条件：modelctl 判定运行中（PID 文件 + 进程存活）且端口健康——
        # 仅端口响应不够（如遗留进程占用端口会被误判为"已停止"模型可用）。
        def _available(m: GatewayModel) -> bool:
            return is_running(m.name) and is_model_healthy(m)

        results = await asyncio.gather(*(asyncio.to_thread(_available, m) for m in models))
        return {
            "object": "list",
            "data": [
                {
                    # 对外 id 优先用 alias（如 deepseek-v4-flash），无 alias 时回退 profile name
                    "id": m.aliases[0] if m.aliases else m.name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "modelctl",
                }
                for m, ok in zip(models, results, strict=False)
                if ok
            ],
        }

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
        # 注意：不能用 `async with` 包裹后返回 StreamingResponse——客户端会在端点
        # 返回时立即关闭，而 SSE 是惰性迭代的，真实 uvicorn 下连接会被提前切断。
        # 因此手动管理生命周期：非流式读完即关；流式由生成器在迭代结束后关闭。
        client = httpx.AsyncClient(timeout=read_timeout, transport=transport)
        try:
            if body.get("stream"):
                req = client.build_request("POST", url, json=body, headers=headers)
                upstream = await client.send(req, stream=True)  # stream=True：连接保持打开，逐块读 SSE
                ctype = upstream.headers.get("content-type")
                if upstream.status_code >= 400:
                    content = await upstream.aread()
                    await client.aclose()
                    return Response(status_code=upstream.status_code, content=content, media_type=ctype)

                async def _sse_stream(upstream=upstream, client=client):
                    """透传后端 SSE 响应体，迭代结束（含异常）后关闭上游连接。"""
                    try:
                        async for chunk in upstream.aiter_bytes():
                            yield chunk
                    finally:
                        await client.aclose()

                return StreamingResponse(_sse_stream(), status_code=upstream.status_code, media_type=ctype)
            upstream = await client.post(url, json=body, headers=headers)
            resp = Response(
                status_code=upstream.status_code,
                content=upstream.content,
                media_type=upstream.headers.get("content-type"),
            )
            await client.aclose()
            return resp
        except httpx.HTTPError as error:
            await client.aclose()
            err_msg = f"后端不可达：{error}"
            return JSONResponse(status_code=502, content={"error": {"message": err_msg, "type": "upstream_error"}})

    return app


def main() -> None:
    """独立运行入口：python -m modelctl.core.gateway。"""
    load_env()
    host = os.environ.get("GATEWAY_HOST", "0.0.0.0")
    port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
    read_timeout = float(os.environ.get("GATEWAY_READ_TIMEOUT", "600"))
    default_model = os.environ.get("GATEWAY_DEFAULT_MODEL")

    import uvicorn

    app = create_app(default_model=default_model, read_timeout=read_timeout)
    print(f"modelctl 网关运行于 http://{host}:{port}/v1（默认模型：{default_model or '未配置'}）", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
