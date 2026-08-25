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
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from modelctl.core.capabilities import Capabilities
from modelctl.core.envfile import load_env
from modelctl.core.process import is_running, open_local
from modelctl.core.profile import Profile, list_profiles
from modelctl.core.stats import UsageCollector
from modelctl.engines import get_adapter
from modelctl.engines.base import EngineAdapter

GATEWAY_PORT = 5003

# 家族路由引擎优先级（数值越小越优先）；未知引擎兜底 99
ENGINE_PRIORITY = {"vllm": 0, "sglang": 1, "unsloth": 2, "ollama": 3, "llamacpp": 4}

# 网关默认关闭 thinking 的模型家族（group）及其引擎。
# 背景：Qwen3.5 家族 chat 模板强制把 <think> 放入 prompt，模型总是先思考，
# 流式响应中思考过程全部路由到 delta.reasoning、delta.content 长时间为空；
# 不识别 reasoning 通道的客户端（如 Trae CN）会表现为"有输入无输出"。
# 注入 chat_template_kwargs.enable_thinking=false 后 content 从首个 token 开始输出。
_THINKING_DISABLED_GROUPS: frozenset[str] = frozenset({"qwen3.8"})
# 仅对原生支持 chat_template_kwargs 的引擎注入；llama.cpp 不识别该字段
_THINKING_DISABLED_ENGINES: frozenset[str] = frozenset({"vllm", "sglang", "unsloth"})

# vLLM 0.27 的 reasoning_effort 枚举仅支持 xhigh/medium/low（默认 xhigh）；
# Claude Code 等客户端发送 high/ultra 等枚举会触发 500
# （"Unexpected reasoning effort high. Supported types are xhigh (default), medium, and low"），
# 网关统一映射为最接近的支持值。
_REASONING_EFFORT_MAP: dict[str, str] = {
    "high": "xhigh",
    "ultra": "xhigh",
    "extreme": "xhigh",
    "balanced": "medium",
    "minimal": "low",
}


def _normalize_reasoning_effort(body: dict) -> None:
    """就地改写不兼容的 reasoning_effort 枚举（vLLM 仅支持 xhigh/medium/low）。

    Claude Code 等客户端把 effort 放在顶层 reasoning_effort、Anthropic 的
    thinking.effort 或 reasoning.effort 嵌套字段；Qwen3.8 的 chat template
    会读取这些值并校验枚举，high/ultra 等会触发 500，统一映射为支持值。
    """
    for key in ("reasoning_effort",):
        effort = body.get(key)
        if isinstance(effort, str) and effort.lower() in _REASONING_EFFORT_MAP:
            body[key] = _REASONING_EFFORT_MAP[effort.lower()]
            logger.info(f"reasoning_effort 兼容映射：{effort} -> {body[key]}")
    for key in ("thinking", "reasoning"):
        block = body.get(key)
        if not isinstance(block, dict):
            continue
        effort = block.get("effort")
        if isinstance(effort, str) and effort.lower() in _REASONING_EFFORT_MAP:
            block["effort"] = _REASONING_EFFORT_MAP[effort.lower()]
            logger.info(f"{key}.effort 兼容映射：{effort} -> {block['effort']}")


@dataclass
class ContextSwitchRule:
    """上下文切换规则：估算输入 token >= min_prompt_tokens 时路由到 target 模型。"""

    min_prompt_tokens: int
    target: str


def load_context_switch_rules(raw: dict) -> dict[str, list[ContextSwitchRule]]:
    """从配置 dict 解析上下文切换规则，按 min_prompt_tokens 降序排序。

    raw 格式：{base_model: [{"min_prompt_tokens": int, "target": str}, ...]}
    非法项跳过；返回空 dict 表示未配置。降序排序保证第一个命中的阈值即最合适的目标。
    """
    rules: dict[str, list[ContextSwitchRule]] = {}
    for base, items in (raw or {}).items():
        parsed: list[ContextSwitchRule] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    threshold = int(item["min_prompt_tokens"])
                    target = str(item["target"])
                except (KeyError, TypeError, ValueError):
                    continue
                if threshold < 0 or not target:
                    continue
                parsed.append(ContextSwitchRule(threshold, target))
        if parsed:
            parsed.sort(key=lambda r: r.min_prompt_tokens, reverse=True)
            rules[str(base)] = parsed
    return rules


def _env_context_rules() -> dict:
    """从环境变量 GATEWAY_CONTEXT_SWITCH（JSON 字符串）读取切换规则；非法时返回空 dict。"""
    raw = os.environ.get("GATEWAY_CONTEXT_SWITCH")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("GATEWAY_CONTEXT_SWITCH 不是合法 JSON，已忽略")
        return {}
    return data if isinstance(data, dict) else {}


def estimate_prompt_tokens(body: dict) -> int:
    """按请求体启发式估算输入 token 数（字符数 / 4，与 benchmark 脚本口径一致）。

    仅统计 messages 中各 role 的 content 文本；无法估算时返回 0。
    """
    total_chars = 0
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total_chars += len(part["text"])
    return total_chars // 4


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
    # 模型家族名（qwen3.8 / deepseek-v4-flash ...），用于家族路由与思考开关注入
    group: str | None = None
    adapter: EngineAdapter | None = None
    # 用量收集器：vLLM 等引擎自带 token 计数 gauge 恒为 0，须由网关按真实请求累计；
    # 由 create_app 按引擎能力注入（None = 走引擎 /metrics 轮询统计）
    collector: UsageCollector | None = None

    def upstream_api_key(self) -> str | None:
        """上游 Bearer key：unsloth 等自管认证引擎的 key 每次启动自动生成，
        需经适配器实时解析；其余引擎即 profile.api_key。"""
        if self.adapter is not None:
            return self.adapter.upstream_api_key()
        return self.api_key


def get_collector(profile: Profile, adapter: EngineAdapter, data_dir: Path) -> "UsageCollector | None":
    """按引擎用量能力创建收集器：metrics_mapping 非 None 且其 token 计数器可轮询（非恒 0）。

    vLLM 等引擎的 token 计数 gauge 在静默（未用 --enable-metrics）时恒为 0，
    此类引擎返回 None，由网关改用真实请求用量累计（见 proxy 的 record_tokens）。
    其余引擎返回 on-demand 收集器（由调用方注入 GatewayModel.collector）。
    """
    from modelctl.core.process import cache_dir

    mapping = adapter.metrics_mapping()
    if mapping is None:
        return None
    data = data_dir or cache_dir()
    return UsageCollector(
        profile.name,
        f"http://127.0.0.1:{profile.port}",
        5.0,
        profile.api_key,
        data,
        mode="on-demand",
        mapping=mapping,
    )


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
            group=profile.group,
            adapter=adapter,
        )
        for key in [profile.name, *profile.aliases]:
            if key in registry:
                logger.warning(f"模型标识 {key} 冲突，已保留 {registry[key].name}，忽略 {profile.name}")
                continue
            registry[key] = model
    return registry


def build_groups(models_dir: Path | None = None, host: str = "127.0.0.1") -> dict[str, list[GatewayModel]]:
    """group 名 -> 按引擎优先级排序的成员 GatewayModel 列表（同引擎保持扫描顺序）。

    未声明 group 的 profile 不进入任何家族；组内排序见 ENGINE_PRIORITY。
    """
    groups: dict[str, list[GatewayModel]] = {}
    for profile in list_profiles(models_dir):
        if not profile.group:
            continue
        adapter = get_adapter(profile.engine)(profile, Capabilities())
        model = GatewayModel(
            name=profile.name,
            engine=profile.engine,
            backend_url=f"http://{host}:{profile.port}",
            upstream_model=adapter.upstream_model_name(),
            api_key=profile.api_key,
            health_url=adapter.health_url(),
            aliases=profile.aliases,
            group=profile.group,
            adapter=adapter,
        )
        groups.setdefault(profile.group, []).append(model)
    for members in groups.values():
        members.sort(key=lambda m: ENGINE_PRIORITY.get(m.engine, 99))
    return groups


def apply_context_switch(
    registry: dict[str, GatewayModel],
    rules: dict[str, list[ContextSwitchRule]],
    body_model: str | None,
    prompt_tokens: int,
) -> GatewayModel | None:
    """按上下文长度规则把请求切换到 high/balanced/light 变体（附录 B.3）。

    规则按 base 模型名匹配请求中的 model 字段；选择第一个 min_prompt_tokens <=
    估算输入 token 数的目标。目标不在注册表（未配置/未启动）时返回 None，调用方沿用原模型。
    """
    if not rules or not body_model:
        return None
    candidates = rules.get(body_model)
    if not candidates:
        return None
    for rule in candidates:
        if prompt_tokens >= rule.min_prompt_tokens:
            return registry.get(rule.target)
    return None


def _resolve_group(groups: dict[str, list[GatewayModel]], name: str) -> GatewayModel | None:
    """家族解析：按引擎优先级顺序返回第一个运行中且健康的成员；无则 None。"""
    for m in groups.get(name, []):
        if is_running(m.name) and is_model_healthy(m):
            return m
    return None


def resolve_model(
    registry: dict[str, GatewayModel],
    body_model: str | None,
    default_model: str | None,
    groups: dict[str, list[GatewayModel]] | None = None,
) -> GatewayModel | None:
    """按 body.model 解析目标模型；支持家族（group）路由。

    顺序：body_model 命中 group（家族解析，无健康成员即 None 不回退）→
    body_model 精确匹配 name/alias → 回退 default_model（同样 group 优先）→ None。
    groups 为 None（未启用家族路由）时行为与旧版一致。
    """
    if groups and body_model and body_model in groups:
        return _resolve_group(groups, body_model)
    if body_model and body_model in registry:
        return registry[body_model]
    if groups and default_model and default_model in groups:
        return _resolve_group(groups, default_model)
    if default_model and default_model in registry:
        return registry[default_model]
    return None


def is_model_healthy(model: GatewayModel, timeout: float = 2.0) -> bool:
    """后端存活探测（单次探测，连接失败立即返回 False，不重试等待）。

    不再复用 process.wait_health（其内部 sleep 重试会让未运行模型各耗时约 2s，
    /v1/models 对注册表全部模型串行探测时会累积到十几秒）。
    """
    api_key = model.upstream_api_key()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        req = urllib.request.Request(model.health_url, headers=headers)
        # open_local：绕过系统代理（同 wait_health，回环探测不走 http_proxy）
        with open_local(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def create_app(
    registry: dict[str, GatewayModel] | None = None,
    default_model: str | None = None,
    read_timeout: float = 600.0,
    transport=None,
    context_rules: dict[str, list[ContextSwitchRule]] | None = None,
    groups: dict[str, list[GatewayModel]] | None = None,
    stats_data_dir: Path | None = None,
):
    """构建 FastAPI 网关应用（transport 供测试注入 httpx.MockTransport）。

    环境变量：GATEWAY_DEFAULT_MODEL（默认模型，缺省/未知 model 回退目标）；
    GATEWAY_CONTEXT_SWITCH（JSON 上下文切换规则，见 load_context_switch_rules）。
    groups：家族索引（group -> 成员列表）；调用方注入 registry 时缺省为空 dict，
    未注入时自动从 models/*.yaml 构建。
    stats_data_dir：用量持久化目录（与 stats 服务共用，使网关累计的 token 跨进程保留）。
    """
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    if registry is None:
        registry = build_registry()
        if groups is None:
            groups = build_groups()
    else:
        groups = groups or {}
    default_model = default_model or os.environ.get("GATEWAY_DEFAULT_MODEL")
    context_rules = context_rules if context_rules is not None else load_context_switch_rules(_env_context_rules())
    app = FastAPI(title="modelctl gateway", docs_url="/docs", openapi_url="/openapi.json")

    # 用量收集：为注册表中"引擎 metrics 不可精确轮询"（vLLM token 计数恒 0）的模型注入
    # 收集器，网关按真实请求累计；其余模型走引擎 /metrics 轮询（stats 服务），无需注入。
    for model in registry.values():
        if model.collector is None and model.adapter is not None:
            model.collector = get_collector(
                model.adapter.profile,
                model.adapter,
                stats_data_dir,
            )

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
        data = [
            {
                "id": m.aliases[0] if m.aliases else m.name,
                "object": "model",
                "created": 0,
                "owned_by": "modelctl",
            }
            for m, ok in zip(models, results, strict=False)
            if ok
        ]
        # 家族逻辑名：组内有健康成员时展示（id=group 名，与具体成员 id 去重）
        existing_ids = {item["id"] for item in data}

        def _group_healthy(members: list[GatewayModel]) -> bool:
            return any(is_running(m.name) and is_model_healthy(m) for m in members)

        for group_name, members in groups.items():
            if group_name in existing_ids:
                continue
            if await asyncio.to_thread(_group_healthy, members):
                data.append({"id": group_name, "object": "model", "created": 0, "owned_by": "modelctl"})
        return {"object": "list", "data": data}

    @app.post("/v1/messages")
    async def anthropic_proxy(request: Request):
        """Anthropic Messages API（/v1/messages）兼容透传。

        Trae CN 等客户端内置 Claude Agent SDK，使用 Anthropic 协议（POST
        /v1/messages，x-api-key 认证，SSE 流带 event: 行）而非 OpenAI 格式。
        vLLM 0.27+ 原生支持 /v1/messages，此处按 body.model 路由并原样透传；
        流式必须保留 event: 行（不能复用 /v1/chat/completions 的 data: 行解析）。
        """
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "请求体必须是 JSON", "type": "invalid_request_error"}},
            )
        logger.info(
            f"Anthropic 代理请求 model={body.get('model')!r} stream={body.get('stream')} "
            f"max_tokens={body.get('max_tokens')} tools={'tools' in body} "
            f"msgs={len(body.get('messages') or [])} "
            f"thinking={body.get('thinking')!r} reasoning={body.get('reasoning')!r} "
            f"effort={body.get('reasoning_effort')!r} "
            f"auth_xkey={'x-api-key' in request.headers} auth={'Authorization' in request.headers}"
        )
        target = resolve_model(registry, body.get("model"), default_model, groups)
        if target is None:
            err_msg = f"model not found: {body.get('model')}"
            return JSONResponse(
                status_code=404,
                content={"error": {"message": err_msg, "type": "invalid_request_error"}},
            )
        # 改写为后端期望的模型名（同 OpenAI 端点）
        body["model"] = target.upstream_model
        _normalize_reasoning_effort(body)
        # Claude Code 新版发 thinking: {"type": "adaptive"}（Anthropic 自动思考）；
        # vLLM 0.27.1 不支持 adaptive，会映射出 effort=high 触发 Qwen3.8 模板 500，
        # 转为 vLLM 支持的 disabled（关闭思考，与 OpenAI 端点 enable_thinking=false 策略一致）。
        thinking = body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "adaptive":
            thinking["type"] = "disabled"
            logger.info("thinking.type adaptive -> disabled（vLLM 兼容）")
        # 透传 Anthropic 版本头等；认证头以 profile 有效 key 为准：
        # 后端（vLLM 等）的 /v1/messages 认证头格式因实现而异（x-api-key /
        # Authorization Bearer），客户端自配 key 可能与后端不一致，故用
        # target 的有效 key 同时设置两种头，确保认证成功。
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() in ("content-type", "authorization", "x-api-key", "anthropic-version", "anthropic-beta")
        }
        up_key = target.upstream_api_key()
        if up_key:
            headers["x-api-key"] = up_key
            headers["Authorization"] = f"Bearer {up_key}"
        url = f"{target.backend_url}/v1/messages"
        client = httpx.AsyncClient(timeout=read_timeout, transport=transport)
        try:
            if body.get("stream"):
                req = client.build_request("POST", url, json=body, headers=headers)
                upstream = await client.send(req, stream=True)
                ctype = upstream.headers.get("content-type")
                if upstream.status_code >= 400:
                    content = await upstream.aread()
                    await client.aclose()
                    return Response(status_code=upstream.status_code, content=content, media_type=ctype)

                async def _raw_sse(upstream=upstream, client=client):
                    """原始字节透传（保留 event:/data: 行），迭代结束关闭上游连接。"""
                    pending = b""
                    texts: list[str] = []
                    thinking_len = 0
                    try:
                        async for chunk in upstream.aiter_bytes():
                            if not isinstance(chunk, bytes):
                                chunk = b"".join(chunk)
                            pending += chunk
                            lines = pending.split(b"\n")
                            pending = lines.pop()
                            for raw in lines:
                                line = raw.strip()
                                yield raw + b"\n"  # 始终透传所有行（保留 event:/data:）
                                if not line.startswith(b"data:"):
                                    continue
                                payload = line[5:].strip()
                                if not payload:
                                    continue
                                try:
                                    data = json.loads(payload)
                                except ValueError:
                                    continue
                                # content_block_delta：text_delta -> 正文，thinking_delta -> 思考
                                delta = data.get("delta")
                                if isinstance(delta, dict) and delta.get("type") == "text_delta" and delta.get("text"):
                                    texts.append(delta["text"])
                                elif isinstance(delta, dict) and delta.get("type") == "thinking_delta" and delta.get("thinking"):
                                    thinking_len += len(delta["thinking"])
                    finally:
                        if pending:
                            yield pending
                        logger.info(
                            f"Anthropic 流式响应摘要 content={''.join(texts)[:200]!r} thinking_len={thinking_len}"
                        )
                        await client.aclose()

                return StreamingResponse(_raw_sse(), status_code=upstream.status_code, media_type=ctype)
            upstream = await client.post(url, json=body, headers=headers)
            try:
                _data = json.loads(upstream.content)
                _texts = [
                    b.get("text")
                    for b in (_data.get("content") or [])
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                ]
                _thinking_len = sum(
                    len(b.get("thinking") or "")
                    for b in (_data.get("content") or [])
                    if isinstance(b, dict) and b.get("type") == "thinking"
                )
                logger.info(
                    f"Anthropic 非流式响应摘要 content={''.join(_texts)[:200]!r} thinking_len={_thinking_len}"
                )
            except ValueError:
                pass
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
        logger.info(
            f"OpenAI 代理请求 {path} model={body.get('model')!r} stream={body.get('stream')} "
            f"max_tokens={body.get('max_tokens')} tools={'tools' in body} "
            f"resp_format={'response_format' in body} msgs={len(body.get('messages') or [])} "
            f"auth={'Authorization' in request.headers} stream_options={'stream_options' in body}"
        )
        target = resolve_model(registry, body.get("model"), default_model, groups)
        if target is None:
            err_msg = f"model not found: {body.get('model')}"
            return JSONResponse(
                status_code=404,
                content={"error": {"message": err_msg, "type": "invalid_request_error"}},
            )
        # 上下文切换（附录 B.3）：按估算输入长度路由到 high/balanced/light 变体
        if context_rules:
            prompt_tokens = estimate_prompt_tokens(body)
            switched = apply_context_switch(registry, context_rules, target.name, prompt_tokens)
            if switched is not None and switched.name != target.name:
                logger.info(f"上下文切换：{target.name} -> {switched.name}（估算输入 {prompt_tokens} tokens）")
                target = switched
        # 改写为后端期望的模型名（ollama 严格校验，llamacpp 忽略）
        body["model"] = target.upstream_model
        _normalize_reasoning_effort(body)
        # 思考型模型家族默认关闭 thinking（见 _THINKING_DISABLED_GROUPS 注释）；
        # 请求显式传 chat_template_kwargs 时尊重调用方意图，不覆盖。
        if (
            target.group in _THINKING_DISABLED_GROUPS
            and target.engine in _THINKING_DISABLED_ENGINES
            and "chat_template_kwargs" not in body
        ):
            body["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {"Content-Type": "application/json"}
        up_key = target.upstream_api_key()
        if up_key:
            # 用 profile 有效 key 认证（覆盖客户端自配 key）：客户端（如 Trae CN）
            # 配置的 key 可能与后端不一致，透传会导致 vLLM 401；网关代劳认证更稳
            auth = f"Bearer {up_key}"
        else:
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

                # 已累计到该 chunk 的 token（不含当前块，避免重复累计）
                seen_tokens = {"prompt": 0, "completion": 0}
                collector = target.collector

                def _record_usage(data: dict, seen: dict) -> None:
                    usage = data.get("usage")
                    if not isinstance(usage, dict) or collector is None:
                        return
                    prompt = usage.get("prompt_tokens")
                    completion = usage.get("completion_tokens")
                    if not isinstance(prompt, int) or not isinstance(completion, int):
                        return
                    collector.record_tokens(prompt - seen["prompt"], completion - seen["completion"])
                    seen["prompt"] = prompt
                    seen["completion"] = completion

                async def _sse_stream(upstream=upstream, client=client):
                    """透传后端 SSE 响应体，迭代结束（含异常）后关闭上游连接。"""
                    pending = b""
                    collected: dict = {"content": [], "reasoning": [], "tool_calls": False}
                    try:
                        async for chunk in upstream.aiter_bytes():
                            # 单 chunk 内可能含多条 data: 行，也可能一条被切成多个 chunk；
                            # 先按行缓冲，整行再解析，防止流式 usage 增量被拆行漏统计。
                            if not isinstance(chunk, bytes):
                                chunk = b"".join(chunk)  # 某些 httpx 版本整批 yield 列表
                            pending += chunk
                            lines = pending.split(b"\n")
                            pending = lines.pop()  # 末段可能是不完整行，留到下一轮
                            for raw in lines:
                                line = raw.strip()
                                if not line.startswith(b"data:"):
                                    continue
                                payload = line[5:].strip()
                                if not payload:
                                    continue
                                if payload != b"[DONE]":
                                    try:
                                        data = json.loads(payload)
                                    except ValueError:
                                        continue
                                    _record_usage(data, seen_tokens)
                                    delta = ((data.get("choices") or [{}])[0].get("delta")) or {}
                                    if delta.get("content"):
                                        collected["content"].append(delta["content"])
                                    if delta.get("reasoning") or delta.get("reasoning_content"):
                                        collected["reasoning"].append(delta.get("reasoning") or delta.get("reasoning_content"))
                                    if delta.get("tool_calls"):
                                        collected["tool_calls"] = True
                                yield raw + b"\n"
                    finally:
                        if pending:
                            yield pending
                        logger.info(
                            f"OpenAI 流式响应摘要 content={''.join(collected['content'])[:200]!r} "
                            f"reasoning_len={sum(len(x) for x in collected['reasoning'])} "
                            f"tool_calls={collected['tool_calls']}"
                        )
                        await client.aclose()

                return StreamingResponse(_sse_stream(), status_code=upstream.status_code, media_type=ctype)
            upstream = await client.post(url, json=body, headers=headers)
            # 非流式：响应体完整读回，直接统计 usage（后端未回 usage 时静默跳过）
            if target.collector is not None:
                try:
                    data = json.loads(upstream.content)
                    usage = data.get("usage")
                    if isinstance(usage, dict):
                        prompt = usage.get("prompt_tokens")
                        completion = usage.get("completion_tokens")
                        if isinstance(prompt, int) and isinstance(completion, int):
                            target.collector.record_tokens(prompt, completion)
                except ValueError:
                    pass
            try:
                _data = json.loads(upstream.content)
                _msg = ((_data.get("choices") or [{}])[0].get("message")) or {}
                logger.info(
                    f"OpenAI 非流式响应摘要 content={str(_msg.get('content'))[:200]!r} "
                    f"tool_calls={bool(_msg.get('tool_calls'))} reasoning={bool(_msg.get('reasoning'))}"
                )
            except ValueError:
                pass
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
    data_dir = Path(os.environ.get("USAGE_DATA_DIR", "")) or None  # 与 stats 服务共用持久化目录

    import uvicorn

    app = create_app(default_model=default_model, read_timeout=read_timeout, stats_data_dir=data_dir)
    print(f"modelctl 网关运行于 http://{host}:{port}/v1（默认模型：{default_model or '未配置'}）", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
