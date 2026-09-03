#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/gateway.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : OpenAI 兼容网关服务
# ===============================================================================

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
import datetime as _dt
import json
import os
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from modelctl.core.audit import NoopAuditLog, RequestAuditLog, _new_audit_log
from modelctl.core.capabilities import Capabilities
from modelctl.core.envfile import load_env
from modelctl.core.process import is_running_any, open_local
from modelctl.core.profile import Profile, list_profiles
from modelctl.core.stats import UsageCollector
from modelctl.engines import get_adapter
from modelctl.engines.base import EngineAdapter

GATEWAY_PORT = 5003

# 家族路由引擎优先级（数值越小越优先）；未知引擎兜底 99
# 排序依据：成熟度 + 吞吐 + 混合注意力支持；aphrodite/tokenspeed/lmdeploy/tensorrt_llm
# 为 2024-2025 新引擎，保守排在 clang++(llamacpp) 之后
ENGINE_PRIORITY = {
    "vllm": 0, "sglang": 1, "unsloth": 2, "ollama": 3, "llamacpp": 4,
    "aphrodite": 5, "tokenspeed": 6, "lmdeploy": 7, "tensorrt_llm": 8,
}

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
# §1.2 配置化：全局默认 map；profile 顶层 gateway.reasoning_effort_map 可整段覆盖。
DEFAULT_REASONING_EFFORT_MAP: dict[str, str] = {
    "high": "xhigh",
    "ultra": "xhigh",
    "extreme": "xhigh",
    "balanced": "medium",
    "minimal": "low",
}
# 旧名保留（兼容既有导入）
_REASONING_EFFORT_MAP = DEFAULT_REASONING_EFFORT_MAP


def _normalize_reasoning_effort(body: dict, mapping: dict[str, str] | None = None) -> None:
    """就地改写不兼容的 reasoning_effort 枚举（vLLM 仅支持 xhigh/medium/low）。

    Claude Code 等客户端把 effort 放在顶层 reasoning_effort、Anthropic 的
    thinking.effort / reasoning.effort 嵌套字段，或 Anthropic 新版协议的
    output_config.effort（Claude Code 默认发送 {"effort": "high"}）；
    Qwen3.8 的 chat template 会读取这些值并校验枚举，high/ultra 等会触发 500，
    统一映射为支持值。参见 QwenLM/Qwen3.8#217。
    """
    m = mapping if mapping is not None else DEFAULT_REASONING_EFFORT_MAP
    for key in ("reasoning_effort",):
        effort = body.get(key)
        if isinstance(effort, str) and effort.lower() in m:
            body[key] = m[effort.lower()]
            logger.info(f"reasoning_effort 兼容映射：{effort} -> {body[key]}")
    for key in ("thinking", "reasoning"):
        block = body.get(key)
        if not isinstance(block, dict):
            continue
        effort = block.get("effort")
        if isinstance(effort, str) and effort.lower() in m:
            block["effort"] = m[effort.lower()]
            logger.info(f"{key}.effort 兼容映射：{effort} -> {block['effort']}")
    output_config = body.get("output_config")
    if isinstance(output_config, dict):
        effort = output_config.get("effort")
        if isinstance(effort, str) and effort.lower() in m:
            output_config["effort"] = m[effort.lower()]
            logger.info(f"output_config.effort 兼容映射：{effort} -> {output_config['effort']}")


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
    # 请求级审计日志：create_app 统一注入（None 表示仅静态信息，handler 跳过写入）
    audit_log: RequestAuditLog | NoopAuditLog | None = None
    # §1.2 策略配置化：来自 profile 顶层 gateway 段（缺省时由白名单/全局 map 决定）
    thinking_disabled: bool | None = None
    reasoning_effort_map: dict[str, str] | None = None
    # §1.3 配置化：自定义 per-request 原生指标字段名映射（None 时回退引擎适配器默认）
    native_metrics_mapping: dict[str, str] | None = None

    def upstream_api_key(self) -> str | None:
        """上游 Bearer key：unsloth 等自管认证引擎的 key 每次启动自动生成，
        需经适配器实时解析；其余引擎即 profile.api_key。"""
        if self.adapter is not None:
            return self.adapter.upstream_api_key()
        return self.api_key


def _build_audit_entry(
    *,
    model_name: str,
    profile_name: str,
    profile_engine: str,
    path: str,
    stream: bool,
    native_metrics: dict | None,
    usage: dict | None,
    gateway_metrics: dict | None,
    status_code: int,
    error: str | None,
    finish_reason: str | None,
    input_char_len: int,
    collector_diff_prompt: int = 0,
    collector_diff_completion: int = 0,
) -> dict:
    """统一 build 入口（纯函数，无副作用）；token 取值优先级见 spec §2 / §4.2。

    source：上游返回原生 per-request metrics 时为 vllm_native，否则 gateway_estimate。
    tokens_source：响应含 usage 为 response-usage，否则 collector-diff（取 collector
    snapshot 差分 collector_diff_*/已 max(0) 保护负值）；usage 字段名兼容 OpenAI
    （prompt/completion_tokens）与 Anthropic（input/output_tokens）。
    """
    source = "vllm_native" if native_metrics else "gateway_estimate"
    if usage:
        tokens_source = "response-usage"
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
        total = usage.get("total_tokens") or ((prompt or 0) + (completion or 0) or None)
    else:
        tokens_source = "collector-diff"
        prompt = max(0, int(collector_diff_prompt))
        completion = max(0, int(collector_diff_completion))
        total = prompt + completion if (prompt or completion) else None
    return {
        "ts": _dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "model": profile_name or model_name,
        "engine": profile_engine,
        "path": path,
        "stream": stream,
        "source": source,
        "tokens_source": tokens_source,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "input_char_len": input_char_len,
        "native_metrics": native_metrics,
        "gateway_metrics": gateway_metrics,
        "status_code": status_code,
        "error": error,
        "finish_reason": finish_reason,
    }


def get_collector(profile: Profile, adapter: EngineAdapter, data_dir: Path) -> "UsageCollector | None":
    """按引擎用量能力创建收集器：metrics_mapping 非 None 且其 token 计数器可轮询（非恒 0）。

    vLLM 等引擎的 token 计数 gauge 在静默（未用 --enable-metrics）时恒为 0，
    此类引擎返回 None，由网关改用真实请求用量累计（见 proxy 的 record_tokens）。
    其余引擎返回 on-demand 收集器（由调用方注入 GatewayModel.collector）。
    """
    from modelctl.core.process import cache_dir
    from modelctl.core.stats import _parse_env_bool

    mapping = adapter.metrics_mapping()
    if mapping is None:
        return None
    data = data_dir or cache_dir()
    try:
        native_mapping = adapter.native_metrics_mapping()
    except (NotImplementedError, AttributeError):
        native_mapping = None
    # §1.3 配置化：profile 顶层 gateway.native_metrics_mapping 优先；
    # 允许部分键覆盖（与引擎默认 merge），其余键保留引擎默认。
    if profile.native_metrics_mapping:
        if not native_mapping:
            native_mapping = dict(profile.native_metrics_mapping)
        else:
            merged = dict(native_mapping)
            merged.update(profile.native_metrics_mapping)
            native_mapping = merged
    return UsageCollector(
        profile.name,
        f"http://127.0.0.1:{profile.port}",
        5.0,
        profile.api_key,
        data,
        mode="on-demand",
        mapping=mapping,
        native_mapping=native_mapping,
        bench_fallback=_parse_env_bool(os.environ.get("USAGE_BENCH_FALLBACK")),
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
            thinking_disabled=profile.thinking_disabled,
            reasoning_effort_map=profile.reasoning_effort_map,
            native_metrics_mapping=profile.native_metrics_mapping,
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
            thinking_disabled=profile.thinking_disabled,
            reasoning_effort_map=profile.reasoning_effort_map,
            native_metrics_mapping=profile.native_metrics_mapping,
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


def is_model_available(model: GatewayModel) -> bool:
    """模型是否可路由：端口 /health 2xx 优先，PID 文件机器兜底（与原 is_running 退化一致）。

    adapter.profile 缺省（旧 GatewayModel / 未注入 adapter 时）退回纯 PID 探测——
    等效 venv-only 路径下"PID 文件可读 + 进程 alive"语义，无回归。
    """
    return is_running_any(model.name, model.adapter.profile if model.adapter else None)


def _resolve_group(groups: dict[str, list[GatewayModel]], name: str) -> GatewayModel | None:
    """家族解析：按引擎优先级顺序返回第一个可用（运行中或外部启动且健康）的成员；无则 None。"""
    for m in groups.get(name, []):
        if is_model_available(m):
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

    注：本函数在 2026-09-02 分支中已无 src/ 内部调用者——family 路由与
    is_model_available 均改用 ``process.is_running_any``（同时判定端口健康 +
    venv PID 文件存活，无副作用不 unlink）。保留本函数仅因
    ``tests/test_gateway.py::test_is_model_healthy_fails_fast_on_connection_error``
    对其做单元测试；该测试删除后可一并移除本函数。
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
    audit_log: RequestAuditLog | NoopAuditLog | None = None,
    admin: bool = False,
):
    """构建 FastAPI 网关应用（transport 供测试注入 httpx.MockTransport）。

    环境变量：GATEWAY_DEFAULT_MODEL（默认模型，缺省/未知 model 回退目标）；
    GATEWAY_CONTEXT_SWITCH（JSON 上下文切换规则，见 load_context_switch_rules）。
    audit_log：请求级审计日志；缺省时按 AUDIT_DIR（默认 data/audit）从 env 构造。
    groups：家族索引（group -> 成员列表）；调用方注入 registry 时缺省为空 dict，
    未注入时自动从 models/*.yaml 构建。
    stats_data_dir：用量持久化目录（与 stats 服务共用，使网关累计的 token 跨进程保留）。
    admin：True 时额外挂上 /admin/api/*（Web UI 管理面）与前端静态产物。仅
    `modelctl webui` 传 True；`modelctl gateway start` 保持纯数据面，管理 API 不对外暴露。
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
    # redirect_slashes=False：禁止把裸 /v1 自动重定向到 /v1/。
    # FastAPI 默认重定向的 Location 是根相对路径（/v1/），经 B 机 nginx 前缀
    # 路由后客户端跟随重定向会丢失 /<node>/llm 前缀，第二次请求落空（502）。
    # 裸 /v1 由下方 @app.post("/v1") 直接处理，返回明确 404 而非 307。
    # 请求级审计日志：缺省从 AUDIT_DIR（默认 data/audit）构造；启动幂等的后台清理线程，
    # 应用关闭时 destroy 回收（绝不阻塞请求路径）。lifespan 管理线程生命周期。
    audit_log = audit_log or _new_audit_log(Path(os.environ.get("AUDIT_DIR", "data/audit")))

    @asynccontextmanager
    async def _lifespan(_app: "FastAPI"):
        """应用生命周期：启动审计清理线程；关闭时 destroy 回收。"""
        audit_log.ensure_cleanup_thread()
        try:
            yield
        finally:
            try:
                audit_log.destroy()
            except Exception as exc:  # noqa: BLE001 — 关闭阶段异常不得冒泡
                logger.warning(f"审计日志关闭异常：{exc}")

    app = FastAPI(
        title="modelctl gateway",
        docs_url="/docs",
        openapi_url="/openapi.json",
        redirect_slashes=False,
        lifespan=_lifespan,
    )
    app.state.audit_log = audit_log

    # 用量收集：为注册表中"引擎 metrics 不可精确轮询"（vLLM token 计数恒 0）的模型注入
    # 收集器，网关按真实请求累计；其余模型走引擎 /metrics 轮询（stats 服务），无需注入。
    for model in registry.values():
        if model.collector is None and model.adapter is not None:
            model.collector = get_collector(
                model.adapter.profile,
                model.adapter,
                stats_data_dir,
            )
        model.audit_log = audit_log

    @app.get("/v1/models")
    async def list_models() -> dict:
        # 注册表同时含 name 与 alias 两个 key（指向同一 GatewayModel），须按 name 去重
        seen: set[str] = set()
        models = []
        for m in registry.values():
            if m.name not in seen:
                seen.add(m.name)
                models.append(m)

        # 并发可用性探测：串行会让未运行模型各耗 timeout 秒，10 个模型累积到十几秒。
        # 过滤条件见 is_model_available：受管运行中，或无 PID 文件但端口健康（外部启动）。
        def _available(m: GatewayModel) -> bool:
            return is_model_available(m)

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
            return any(is_model_available(m) for m in members)

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
        # 审计：请求体字节长度（从 body 计算，request.content 在 Starlette 里可能已被消费）
        body_char_len = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
        logger.info(
            f"Anthropic 代理请求 model={body.get('model')!r} stream={body.get('stream')} "
            f"max_tokens={body.get('max_tokens')} tools={'tools' in body} "
            f"msgs={len(body.get('messages') or [])} "
            f"thinking={body.get('thinking')!r} reasoning={body.get('reasoning')!r} "
            f"effort={body.get('reasoning_effort')!r} "
            f"top_keys={sorted(body.keys())} "
            f"msg_blocks={[ [b.get('type') for b in (m.get('content') or []) if isinstance(b, dict)] if isinstance(m.get('content'), list) else type(m.get('content')).__name__ for m in (body.get('messages') or []) ]} "
            f"auth_xkey={'x-api-key' in request.headers} auth={'Authorization' in request.headers}"
        )
        target = resolve_model(registry, body.get("model"), default_model, groups)
        if target is None:
            err_msg = f"model not found: {body.get('model')}"
            return JSONResponse(
                status_code=404,
                content={"error": {"message": err_msg, "type": "invalid_request_error"}},
            )
        # 审计：每次请求取目标模型的 audit_log（create_app 已统一注入；短路判断避免 502/404 无谓兜底）
        self_audit_log = target.audit_log
        # 改写为后端期望的模型名（同 OpenAI 端点）
        body["model"] = target.upstream_model
        _normalize_reasoning_effort(body, target.reasoning_effort_map)
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
        headers = {k: v for k, v in request.headers.items() if k.lower() in ("content-type", "authorization", "x-api-key", "anthropic-version", "anthropic-beta")}
        up_key = target.upstream_api_key()
        if up_key:
            headers["x-api-key"] = up_key
            headers["Authorization"] = f"Bearer {up_key}"
        url = f"{target.backend_url}/v1/messages"
        client = httpx.AsyncClient(timeout=read_timeout, transport=transport)
        # 审计用计时基线（Anthropic 全路径；native_metrics 恒 None）
        _t0 = time.monotonic()
        try:
            if body.get("stream"):
                req = client.build_request("POST", url, json=body, headers=headers)
                upstream = await client.send(req, stream=True)
                _t_first = time.monotonic()
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
                    seen_usage: dict | None = None  # 审计：message_delta.usage（流式最终用量）
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
                                # 审计：取 message_delta 的 usage（增量累计，末次即最终）
                                if data.get("type") == "message_delta":
                                    _u = data.get("usage")
                                    if isinstance(_u, dict):
                                        seen_usage = _u
                                # content_block_delta：text_delta -> 正文，thinking_delta -> 思考
                                delta = data.get("delta")
                                if isinstance(delta, dict) and delta.get("type") == "text_delta" and delta.get("text"):
                                    texts.append(delta["text"])
                                elif isinstance(delta, dict) and delta.get("type") == "thinking_delta" and delta.get("thinking"):
                                    thinking_len += len(delta["thinking"])
                    finally:
                        if pending:
                            yield pending
                        logger.info(f"Anthropic 流式响应摘要 content={''.join(texts)[:200]!r} thinking_len={thinking_len}")
                        # 审计：写入必须包裹，异常不得中断客户端流
                        try:
                            if self_audit_log is not None:
                                _gen_ms = (time.monotonic() - _t0) * 1000.0
                                _gm = {
                                    "ttft_ms": round((_t_first - _t0) * 1000.0, 2),
                                    "generation_time_ms": round(_gen_ms, 2),
                                    "tokens_per_second": None,  # Anthropic usage 不保证累计，无法可靠折算速率
                                }
                                self_audit_log.record(_build_audit_entry(
                                    model_name=target.name,
                                    profile_name=target.name,
                                    profile_engine=target.engine,
                                    path="messages",
                                    stream=True,
                                    native_metrics=None,  # Anthropic 响应无原生 metrics
                                    usage=seen_usage,
                                    gateway_metrics=_gm,
                                    status_code=upstream.status_code,
                                    error=None,
                                    finish_reason=None,
                                    input_char_len=body_char_len,
                                ))
                        except Exception as exc:
                            logger.warning(f"审计写盘异常（SSE 不中断）: {exc}")
                        await client.aclose()

                return StreamingResponse(_raw_sse(), status_code=upstream.status_code, media_type=ctype)
            upstream = await client.post(url, json=body, headers=headers)
            try:
                _data = json.loads(upstream.content)
                _texts = [b.get("text") for b in (_data.get("content") or []) if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
                _thinking_len = sum(len(b.get("thinking") or "") for b in (_data.get("content") or []) if isinstance(b, dict) and b.get("type") == "thinking")
                logger.info(f"Anthropic 非流式响应摘要 content={''.join(_texts)[:200]!r} thinking_len={_thinking_len}")
            except ValueError:
                pass
            # 审计：Anthropic 非流式 usage 在响应根级，无原生 metrics
            try:
                if self_audit_log is not None:
                    _t1 = time.monotonic()
                    _usage = None
                    try:
                        _d = json.loads(upstream.content)
                        if isinstance(_d, dict):
                            _usage = _d.get("usage")
                    except ValueError:
                        pass
                    _delta = max(_t1 - _t0, 1e-9)
                    _gm = {
                        "ttft_ms": None,  # 非流式无首延迟
                        "generation_time_ms": round(_delta * 1000.0, 2),
                        "tokens_per_second": (
                            round((_usage.get("output_tokens") or 0) / _delta, 1)
                            if _usage and _usage.get("output_tokens")
                            else None
                        ),
                    }
                    self_audit_log.record(_build_audit_entry(
                        model_name=target.name,
                        profile_name=target.name,
                        profile_engine=target.engine,
                        path="messages",
                        stream=False,
                        native_metrics=None,  # Anthropic 响应无原生 metrics
                        usage=_usage if isinstance(_usage, dict) else None,
                        gateway_metrics=_gm,
                        status_code=upstream.status_code,
                        error=None,
                        finish_reason=None,  # Anthropic 非流式无 finish_reason 字段
                        input_char_len=body_char_len,
                    ))
            except Exception as exc:
                logger.warning(f"审计写盘异常（转发不受影响）: {exc}")
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

    # /v1 与 /v1/{path:path} 共用同一处理器：redirect_slashes=False 后裸 /v1 不再
    # 307 重定向（重定向 Location 为根路径 /v1/，经 nginx 前缀路由会丢 /<node>/llm）。
    # 裸 /v1（连通性探测，如 hertz 客户端 POST baseUrl）直接返回 200 而非 404，
    # 避免客户端把 404 当作端点不可用而中止；真实请求走 /v1/chat/completions 等子路径。
    @app.post("/v1")
    @app.post("/v1/{path:path}")
    async def proxy(request: Request, path: str = ""):
        if not path:
            return JSONResponse(status_code=200, content={"status": "ok"})
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
        # 审计：请求体字节长度（从 body 计算，request.content 在 Starlette 里可能已被消费）
        body_char_len = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
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
        # 审计：每次请求取目标模型的 audit_log（create_app 已统一注入）
        self_audit_log = target.audit_log
        # 改写为后端期望的模型名（ollama 严格校验，llamacpp 忽略）
        body["model"] = target.upstream_model
        _normalize_reasoning_effort(body, target.reasoning_effort_map)
        # 思考型模型家族默认关闭 thinking（见 _THINKING_DISABLED_GROUPS 注释）；
        # §1.2 配置化：profile 顶层 gateway.thinking_disabled（True/False）优先；
        # 缺省（None）时按 group 白名单判断；请求显式传 chat_template_kwargs 时尊重调用方意图，不覆盖。
        _should_disable_thinking = (
            target.thinking_disabled
            if target.thinking_disabled is not None
            else (target.group in _THINKING_DISABLED_GROUPS and target.engine in _THINKING_DISABLED_ENGINES)
        )
        if _should_disable_thinking and "chat_template_kwargs" not in body:
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
        # 审计用计时基线：t0=发送前；t_first=上游流式首包（TTFT 用，流式分支赋值）
        _t0 = time.monotonic()
        _t_first: float | None = None
        try:
            if body.get("stream"):
                # 审计差分基线：发送前取 snapshot（无副作用；禁止用 get_snapshot 触发 HTTP）
                _collector = target.collector
                _snap_before = _collector.snapshot() if _collector is not None and hasattr(_collector, "snapshot") else None
                req = client.build_request("POST", url, json=body, headers=headers)
                upstream = await client.send(req, stream=True)  # stream=True：连接保持打开，逐块读 SSE
                _t_first = time.monotonic()
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

                def _record_native_metrics(m: dict | None) -> None:
                    try:
                        collector.record_native_metrics(m)
                    except Exception as exc:
                        logger.warning(f"stats 记录 native metrics 异常（SSE 不中断）: {exc}")

                async def _sse_stream(upstream=upstream, client=client):
                    """透传后端 SSE 响应体，迭代结束（含异常）后关闭上游连接。

                    原始字节透传（保留 SSE 的空行事件分隔与 event/data 结构）；
                    仅旁路解析 data: 行做用量统计与响应摘要，不改写输出——
                    按行重组会丢失空行分隔符，导致严格解析的客户端（Trae CN）失败。
                    """
                    pending = b""
                    collected: dict = {"content": [], "reasoning": [], "tool_calls": False}
                    # 审计旁路状态：末块 metrics / usage 沿用（vLLM 仅在末块回带）
                    seen_metrics: dict | None = None
                    seen_usage: dict | None = None
                    seen_finish: str | None = None
                    try:
                        async for chunk in upstream.aiter_bytes():
                            if not isinstance(chunk, bytes):
                                chunk = b"".join(chunk)  # 某些 httpx 版本整批 yield 列表
                            yield chunk  # 原始透传，保留 SSE 格式
                            pending += chunk
                            while b"\n" in pending:
                                line, pending = pending.split(b"\n", 1)
                                line = line.strip()
                                if not line.startswith(b"data:"):
                                    continue
                                payload = line[5:].strip()
                                if not payload or payload == b"[DONE]":
                                    continue
                                try:
                                    data = json.loads(payload)
                                except ValueError:
                                    continue
                                _record_usage(data, seen_tokens)
                                _m = data.get("metrics")
                                if isinstance(_m, dict):
                                    seen_metrics = _m
                                _u = data.get("usage")
                                if isinstance(_u, dict):
                                    seen_usage = _u
                                delta = ((data.get("choices") or [{}])[0].get("delta")) or {}
                                if delta.get("content"):
                                    collected["content"].append(delta["content"])
                                if delta.get("reasoning") or delta.get("reasoning_content"):
                                    collected["reasoning"].append(delta.get("reasoning") or delta.get("reasoning_content"))
                                if delta.get("tool_calls"):
                                    collected["tool_calls"] = True
                                if delta.get("finish_reason"):
                                    seen_finish = delta.get("finish_reason")
                    finally:
                        if pending:
                            yield pending
                        logger.info(
                            f"OpenAI 流式响应摘要 content={''.join(collected['content'])[:200]!r} "
                            f"reasoning_len={sum(len(x) for x in collected['reasoning'])} "
                            f"tool_calls={collected['tool_calls']}"
                        )
                        if seen_metrics is not None:
                            _record_native_metrics(seen_metrics)
                        # 审计差分终点：aclose 之前取，确保所有 chunk 的 record_tokens 已完成
                        _snap_after = collector.snapshot() if collector is not None and hasattr(collector, "snapshot") else None
                        if _snap_before is not None and _snap_after is not None:
                            _diff_prompt = max(0, int(round(_snap_after["prompt_total"] - _snap_before["prompt_total"])))
                            _diff_completion = max(0, int(round(_snap_after["predicted_total"] - _snap_before["predicted_total"])))
                        else:
                            _diff_prompt = _diff_completion = 0
                        # 审计：写入必须包裹，异常不得中断客户端流（在 aclose 之前记录）
                        try:
                            if self_audit_log is not None:
                                _elapsed = time.monotonic() - _t0
                                _gm = {
                                    "ttft_ms": round((_t_first - _t0) * 1000.0, 2) if _t_first is not None else None,
                                    "generation_time_ms": round(_elapsed * 1000.0, 2),
                                    "tokens_per_second": (
                                        round((seen_usage.get("completion_tokens") or 0) / _elapsed, 1)
                                        if seen_usage and _elapsed > 0 else None
                                    ),
                                }
                                self_audit_log.record(_build_audit_entry(
                                    model_name=target.name,
                                    profile_name=target.name,
                                    profile_engine=target.engine,
                                    path=path,
                                    stream=True,
                                    native_metrics=seen_metrics,
                                    usage=seen_usage,
                                    gateway_metrics=_gm,
                                    status_code=upstream.status_code,
                                    error=None,
                                    finish_reason=seen_finish,
                                    input_char_len=body_char_len,
                                    collector_diff_prompt=_diff_prompt,
                                    collector_diff_completion=_diff_completion,
                                ))
                        except Exception as exc:
                            logger.warning(f"审计写盘异常（SSE 不中断）: {exc}")
                        await client.aclose()

                return StreamingResponse(_sse_stream(), status_code=upstream.status_code, media_type=ctype)
            # 审计差分基线：发送前取 snapshot（无副作用；禁止用 get_snapshot 触发 HTTP）
            _snap_before = target.collector.snapshot() if target.collector is not None and hasattr(target.collector, "snapshot") else None
            upstream = await client.post(url, json=body, headers=headers)
            _t1 = time.monotonic()
            # 非流式：响应体完整读回，直接统计 usage（后端未回 usage 时静默跳过）
            _ns_data: dict | None
            try:
                _parsed = json.loads(upstream.content)
                _ns_data = _parsed if isinstance(_parsed, dict) else None
            except ValueError:
                _ns_data = None
            _ns_native: dict | None = _ns_data.get("metrics") if isinstance(_ns_data, dict) else None
            _diff_prompt = _diff_completion = 0
            if _ns_data is not None:
                if target.collector is not None:
                    usage = _ns_data.get("usage")
                    if isinstance(usage, dict):
                        prompt = usage.get("prompt_tokens")
                        completion = usage.get("completion_tokens")
                        if isinstance(prompt, int) and isinstance(completion, int):
                            target.collector.record_tokens(prompt, completion)
                            if _ns_native is not None:
                                try:
                                    target.collector.record_native_metrics(_ns_native)
                                except Exception as exc:
                                    logger.warning(f"stats 记录 native metrics 异常（转发不受影响）: {exc}")
                # 审计差分终点：record_tokens 完成后取；无 usage 时差分即 0（确无 token 可记）
                _snap_after = target.collector.snapshot() if target.collector is not None and hasattr(target.collector, "snapshot") else None
                if _snap_before is not None and _snap_after is not None:
                    _diff_prompt = max(0, int(round(_snap_after["prompt_total"] - _snap_before["prompt_total"])))
                    _diff_completion = max(0, int(round(_snap_after["predicted_total"] - _snap_before["predicted_total"])))
                _msg = ((_ns_data.get("choices") or [{}])[0].get("message")) or {}
                logger.info(f"OpenAI 非流式响应摘要 content={str(_msg.get('content'))[:200]!r} " f"tool_calls={bool(_msg.get('tool_calls'))} reasoning={bool(_msg.get('reasoning'))}")
            # 审计：旁路读取 metrics / usage / finish_reason，写入必须包裹，异常不得影响转发
            try:
                if self_audit_log is not None:
                    _usage_a: dict | None = _ns_data.get("usage") if _ns_data else None
                    _native: dict | None = _ns_data.get("metrics") if _ns_data else None
                    _finish: str | None = None
                    if _ns_data:
                        _choices_a = _ns_data.get("choices")
                        if isinstance(_choices_a, list) and _choices_a:
                            _c0 = _choices_a[0]
                            if isinstance(_c0, dict):
                                _fr = _c0.get("finish_reason")
                                if _fr is not None:
                                    _finish = _fr
                    _delta = max(_t1 - _t0, 1e-9)
                    _completion = (_usage_a.get("completion_tokens") or 0) if isinstance(_usage_a, dict) else 0
                    _gm = {
                        "ttft_ms": None,  # 非流式无首延迟
                        "generation_time_ms": round(_delta * 1000.0, 2),
                        "tokens_per_second": (
                            round(_completion / _delta, 1) if (_completion and _delta > 0) else None
                        ),
                    }
                    self_audit_log.record(_build_audit_entry(
                        model_name=target.name,
                        profile_name=target.name,
                        profile_engine=target.engine,
                        path=path,
                        stream=False,
                        native_metrics=_native if isinstance(_native, dict) else None,
                        usage=_usage_a if isinstance(_usage_a, dict) else None,
                        gateway_metrics=_gm,
                        status_code=upstream.status_code,
                        error=None,
                        finish_reason=_finish,
                        input_char_len=body_char_len,
                        collector_diff_prompt=_diff_prompt,
                        collector_diff_completion=_diff_completion,
                    ))
            except Exception as exc:
                logger.warning(f"审计写盘异常（转发不受影响）: {exc}")
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

    if admin:
        # 管理面：/admin/api/* + 前端 SPA。须在全部 /v1 路由注册后挂载——静态兜底
        # 路由 /{full_path:path} 依赖注册顺序接住未命中的 GET，不能提前注册。
        from modelctl.core.webui.admin_router import create_admin_router
        from modelctl.core.webui.server import mount_static

        admin_router = create_admin_router()
        app.include_router(admin_router, prefix="/admin/api")
        app.state.task_manager = admin_router.task_manager
        mount_static(app)

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

    app = create_app(
        default_model=default_model,
        read_timeout=read_timeout,
        stats_data_dir=data_dir,
    )
    print(f"modelctl 网关运行于 http://{host}:{port}/v1（默认模型：{default_model or '未配置'}）", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
