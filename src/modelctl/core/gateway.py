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
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from modelctl.core.audit import NoopAuditLog, RequestAuditLog, _new_audit_log
from modelctl.core.capabilities import Capabilities
from modelctl.core.envfile import load_env
from modelctl.core.paths import audit_dir, usage_data_dir
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

# ---------- 数据面客户端鉴权（详见 spec 2026-09-07-gateway-client-auth） ----------
# 与管理面 API_KEY 严格隔离：API_KEY 能改配置/启停模型，绝不可下发给外部推理客户端。
GATEWAY_CLIENT_KEY_ENV = "GATEWAY_CLIENT_API_KEY"

# 校验结果标签：写入审计的 auth 字段，只记结果，绝不记 key 值或片段
AUTH_OK = "ok"
AUTH_MISSING = "missing"
AUTH_INVALID = "invalid"
AUTH_UNCONFIGURED = "unconfigured"

# .env 懒加载标记：webui 同 app 挂载 /v1 时不保证已 load_env（与 webui.admin_auth 同范式）
_client_key_env_loaded: bool = False


def client_api_key() -> str:
    """网关客户端密钥；未配置/为空返回 ""（调用方须按 fail-closed 处理）。"""
    global _client_key_env_loaded
    if not _client_key_env_loaded:
        try:
            load_env()
        except Exception:  # noqa: BLE001 — 加载失败走"未配置"分支，保持 401 路径
            pass
        _client_key_env_loaded = True
    return os.environ.get(GATEWAY_CLIENT_KEY_ENV) or ""


def verify_client(request) -> str:
    """数据面准入校验：通过返回 AUTH_OK，否则返回失败标签（不抛异常）。

    双通道嗅探（CLAUDE.md「嗅探请求头，不能强要求 Bearer」首次落地）：
    Authorization: Bearer <key> 或 x-api-key: <key> 任一命中即通过——Anthropic
    协议客户端（Trae CN 内置 Claude SDK）只带 x-api-key，只认 Bearer 会打挂 /v1/messages。
    比较用 hmac.compare_digest 恒定时间防时序泄露；比较双方均编码为 UTF-8 bytes，
    非 ASCII 输入（如 `Bearer 密钥`）只是编码后字节不一致，按不匹配返回 AUTH_INVALID。
    """
    expected = client_api_key()
    if not expected:
        return AUTH_UNCONFIGURED
    auth = request.headers.get("authorization") or ""
    candidate = ""
    if auth.lower().startswith("bearer "):
        candidate = auth[7:].strip()
    if not candidate:
        candidate = (request.headers.get("x-api-key") or "").strip()
    if not candidate:
        return AUTH_MISSING
    try:
        ok = hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
    except TypeError:  # 纵深防御：headers 混入非 str 等意外类型时视为不匹配，不得冒泡成 500
        ok = False
    return AUTH_OK if ok else AUTH_INVALID


def auth_error_response(label: str):
    """401 响应：OpenAI 错误信封，保证 OpenAI/Anthropic SDK 能正常解析并抛客户端异常。"""
    from fastapi.responses import JSONResponse

    message = {
        AUTH_UNCONFIGURED: "gateway client API key not configured",
        AUTH_MISSING: "missing API key: send 'Authorization: Bearer <key>' or 'x-api-key: <key>'",
    }.get(label, "invalid API key")
    return JSONResponse(status_code=401, content={"error": {"message": message, "type": "authentication_error"}})


def client_ip_of(request) -> str:
    """真实来源 IP：nginx 已补 X-Real-IP / X-Forwarded-For，无前置代理时退回 socket。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or ""


def _safe_int(value: object, *, default: int = 1, lo: int = 1, hi: int = 10_000_000) -> int:
    """客户端可控字段的安全整数化：非数字/越界一律夹到 [lo, hi]，绝不抛。

    网关 TPM 估算在鉴权之前跑，裸 int() 会让匿名请求用一个 `"abc"` 打出 500。
    """
    try:
        if isinstance(value, bool):
            raise ValueError("bool is not a token count")
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _accounts_tpm_estimate(body: dict | None) -> int:
    """Task 6 TPM 预占估算：tokens ≈ chars（英文启发式下上界抬到 chars 量级）。

    组成（vLLM `usage` 字段名）：
    - prompt：`estimate_prompt_tokens(body)`（prompt 总字符 // 4，英文
      chars≈tokens 1:1 上界）
    - completion：`max(body.get("max_tokens"), 1)` 缺省 1（无 max_tokens 时
      至少按 1 token 上界，防极短 prompt + 无限生成撑爆窗口）
    - 合计 = `prompt + completion` —— 对**短 prompt + 长生成**的场景，
      "chars 翻倍" 的旧口径会吞掉 completion 部分估不进 limit，实测会漏
      限流（`test_enabled_tpm_exceeded_429` 就是这一类）。

    body 为 None（如 list_models 或 gate 阶段尚未读 body）返回 0：不参与
    TPM 预占，靠 concurrency / rpm 两道托底。
    """
    if not body:
        return 0
    # estimate_prompt_tokens 定义在本文件更下方；Python 函数体延迟求值
    # （def 时只看名字，call 时才做名字查找），此处顺序无依赖问题。
    prompt = max(0, estimate_prompt_tokens(body))
    completion = _safe_int(body.get("max_tokens"), default=1, lo=1)
    return prompt + completion


def accounts_gate(request, tpm_estimate: int = 0):
    """数据面统一准入 gate：返回 ``(resp, identity, label, mode)``。

    - ``resp is not None``：调用方直接 return resp（401 / 429 短路）。
    - ``identity is not None``：请求放行，把 identity 挂到
      ``request.state.identity`` 供下游 settle/release 使用。
    - ``identity is None`` + ``resp is None``：仅意味着**未启用 accounts**，
      走 legacy 单钥 ``verify_client`` 语义；下游不挂 state。

    ``mode`` ∈ {"legacy", "accounts"}，仅作审计/日志用途（不直接写到 JSONL）。
    label 语义：legacy 沿用 verify_client 结果（ok/missing/invalid/unconfigured）；
    accounts 启用时统一为 ok / accounts_invalid / limit_budget_exceeded /
    limit_concurrency_exceeded / limit_rate_limit_exceeded。

    Lifecycle 契约（务必读）::

        budget → acquire → rpm → tpm（est）
        任一阶段抛 UsageLimitError：
          - 若 acquire 已成功 → 必须 release 回滚
          - rpm / tpm reject 不释放"未获得"的槽（因为根本没 acquire）

    未启用 / legacy 路径严格与既有 verify_client 等效（不入 accounts 逻辑）。
    """
    from fastapi.responses import JSONResponse

    app = request.app
    accounts_on = bool(getattr(app.state, "_accounts_enabled", False))
    if not accounts_on:
        # legacy 路径：完全复刻 verify_client + auth_error_response 的行为
        label = verify_client(request)
        if label != AUTH_OK:
            return (auth_error_response(label), None, label, "legacy")
        return (None, None, AUTH_OK, "legacy")

    # accounts 启用
    from modelctl.core.accounts.auth import extract_credential, resolve_account
    from modelctl.core.accounts.limits import UsageLimitError

    guard = getattr(app.state, "limit_guard", None)
    store = getattr(app.state, "accounts", None)
    if guard is None or store is None:
        # 防御：create_app 配置错（accounts_enabled=True 却漏了 store/guard）
        # 走 502 fail-closed，而不是让请求漏到数据面。
        msg = {"error": {"message": "accounts misconfigured", "type": "server_error"}}
        return (JSONResponse(status_code=503, content=msg), None, "accounts_misconfigured", "accounts")

    now = time.time()
    credential = extract_credential(request)
    identity = resolve_account(store, credential, now=now)
    if identity is None:
        IP = client_ip_of(request)
        logger.warning(f"accounts 请求被拒（未签发/禁用/过期） ip={IP}")
        body = {"error": {"message": "invalid API key", "type": "invalid_api_key"}}
        return (JSONResponse(status_code=401, content=body), None, "accounts_invalid", "accounts")

    # 惰性预算重置（幂等；now < budget_reset_at 时为 no-op）
    try:
        guard.reset_budget_if_needed(
            identity.policy, now=now, store=store, user_id=identity.user_id,
        )
    except Exception as exc:  # pragma: no cover - reset 异常不应阻塞主链路
        logger.warning(f"accounts reset_budget_if_needed 异常 user_id={identity.user_id}: {exc}")

    acquired = False
    est = max(0, int(tpm_estimate or 0))
    try:
        guard.check_budget(identity.policy)
        guard.acquire(identity.user_id, identity.policy.concurrency_limit)
        acquired = True
        guard.check_rpm(identity.user_id, identity.policy.rpm_limit, now=now)
        # 与 `add_tpm_actual` 用同一 request_id 作 pair key（避免 id(request)
        # 在 httpx.REUSE 场景下不稳定、以及请求体不保留）。
        rid = time.time_ns()
        guard.check_tpm_reserve(
            identity.user_id, identity.policy.tpm_limit,
            est_total=est, now=now, request_id=rid,
        )
    except UsageLimitError as ex:
        if acquired:
            guard.release(identity.user_id)
        headers = {"Retry-After": str(int(ex.retry_after))} if ex.retry_after else None
        body = {"error": {"message": ex.message, "type": ex.type}}
        resp = JSONResponse(status_code=ex.status, content=body, headers=headers)
        return (resp, None, f"limit_{ex.type}", "accounts")

    # 放行：把 identity + settle/release 需要的元数据挂 request.state
    request.state.identity = identity
    # accounts_request_id / accounts_tpm_est 一同用于 settle 阶段调
    # `limit_guard.add_tpm_actual(user_id, est, actual, now, request_id)`，
    # request_id 与 check_tpm_reserve 相同（= rid）让 add_tpm_actual 能命中
    # pending 字典完成预占->实际替换。
    request.state.accounts_request_id = rid
    request.state.accounts_tpm_est = est
    return (None, identity, AUTH_OK, "accounts")


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
    auth: str = AUTH_OK,
    client_ip: str = "",
    collector_diff_prompt: int = 0,
    collector_diff_completion: int = 0,
    user_id: int | None = None,
    key_id: int | None = None,
    upstream_request_id: str | None = None,
) -> dict:
    """统一 build 入口（纯函数，无副作用）；token 取值优先级见 spec §2 / §4.2。

    source：上游返回原生 per-request metrics 时为 vllm_native，否则 gateway_estimate。
    tokens_source：响应含 usage 为 response-usage，否则 collector-diff（取 collector
    snapshot 差分 collector_diff_*/已 max(0) 保护负值)；usage 字段名兼容 OpenAI
    （prompt/completion_tokens）与 Anthropic（input/output_tokens）。
    auth：准入校验结果标签（ok/missing/invalid/unconfigured/...），绝不记录 key 值；
    client_ip：nginx 透传的真实来源 IP；
    user_id / key_id：启用了 accounts 时透传身份 id；未启用或 401 时保持
    None（旧 JSONL 无此字段，序列化时 None 视同缺省便于向后兼容）。
    upstream_request_id：上游引擎的 X-Request-Id（vLLM 需 --enable-request-id-headers），
    用于把 JSONL 审计与引擎进程日志 / accounts 会话记录对到同一次请求。
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
    # prefix cache 命中量：vLLM 需 --enable-prompt-tokens-details 才在
    # usage.prompt_tokens_details 回带 cached_tokens。缺省记 None 而非 0——0 表示
    # "确实没命中"，None 表示"引擎未开该 flag"，两者混同会让人误判缓存零命中。
    cached_tokens = None
    if isinstance(usage, dict):
        _det = usage.get("prompt_tokens_details")
        if isinstance(_det, dict) and isinstance(_det.get("cached_tokens"), int):
            cached_tokens = _det["cached_tokens"]
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
        "cached_tokens": cached_tokens,
        "upstream_request_id": upstream_request_id,
        "input_char_len": input_char_len,
        "native_metrics": native_metrics,
        "gateway_metrics": gateway_metrics,
        "auth": auth,
        "client_ip": client_ip,
        "status_code": status_code,
        "error": error,
        "finish_reason": finish_reason,
        "user_id": user_id,
        "key_id": key_id,
    }


def get_collector(profile: Profile, adapter: EngineAdapter, data_dir: Path | None) -> "UsageCollector | None":
    """按引擎用量能力创建收集器：metrics_mapping 非 None 且其 token 计数器可轮询（非恒 0）。

    vLLM 等引擎的 token 计数 gauge 在静默（未用 --enable-metrics）时恒为 0，
    此类引擎返回 None，由网关改用真实请求用量累计（见 proxy 的 record_tokens）。
    其余引擎返回 on-demand 收集器（由调用方注入 GatewayModel.collector）。
    data_dir 为空时回退 usage_data_dir()，与 stats 服务同一口径。
    """
    from modelctl.core.paths import usage_data_dir
    from modelctl.core.stats import _parse_env_bool

    mapping = adapter.metrics_mapping()
    if mapping is None:
        return None
    data = data_dir or usage_data_dir()
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
    match_keys: "Sequence[str]",
    prompt_tokens: int,
) -> GatewayModel | None:
    """按上下文长度规则把请求切换到 high/balanced/light 变体（附录 B.3）。

    `match_keys`：按优先级依次尝试的规则键序列。必须包含**路由前**的语义键
    （请求原始 model / group 名）——家族路由会把 target 换成成员名，
    只用 `target.name` 匹配规则的话，规则 key（base 名）永远对不上。
    目标不在注册表（未配置/未启动）时返回 None，调用方沿用原模型。
    """
    if not rules:
        return None
    for key in match_keys:
        if not key:
            continue
        candidates = rules.get(key)
        if not candidates:
            continue
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
    """家族解析：按引擎优先级顺序返回第一个可用（运行中或外部启动且健康）的成员；无则 None。

    ⚠ 同步阻塞：每个成员一次 `is_running_any` → 回环 /health（端口 listen 但不响应
    时吃满 1.5s，见 process.is_running_any）。数据面每请求路径必须经
    `GroupRouteCache` 复用结果，勿直接调用本函数。
    """
    for m in groups.get(name, []):
        if is_model_available(m):
            return m
    return None


# /health 探测结果缓存 TTL（秒），两层各自独立；均可用同名 env 覆盖，0 关闭。
# route 层影响**路由正确性**（选错成员=请求打错端口），必须小；
# avail 层只是状态展示，陈旧几秒无感，但要**大于前端 3s 轮询间隔**才有命中。
GROUP_ROUTE_CACHE_TTL_S = 2.0
AVAIL_CACHE_TTL_S = 5.0


class GroupRouteCache:
    """同步 /health 探测结果的短 TTL 缓存（进程内、单事件循环使用，无需加锁）。

    两层，**TTL 独立**、键空间互不相通（route 用 group 名、avail 用模型 name）：

    - `route`（`route_ttl`）：家族解析结果（`_resolve_group` 的返回值）。数据面每请求走
      这层，命中即零 HTTP，且保留"命中第一个可用成员就短路"的语义。
    - `avail`（`avail_ttl`）：单成员可用性判定。所有"遍历 profile 探状态"的路径共用它——
      网关 `/v1/models`、WebUI `/admin/api/overview`（3s 轮询）、`/admin/api/models`。
      TTL 必须 > 前端轮询间隔，否则每次轮询都落在过期后，命中率恒 0。

    为何必须有：`_resolve_group` / `is_model_available` / `is_running_any` 走同步 urllib
    打回环 /health，跑在事件循环线程（或把线程池占满）——家族成员越多代价越高
    （qwen3.8 组 11 个成员；管理面 51 个 profile），且有正反馈：引擎高负载 → /health 变慢
    → 事件循环被拖住 / worker 池排干 → 全体请求更慢。

    负缓存与正缓存同 TTL：`stop` 之后运维必然看到失败并重试/重启，几秒的陈旧窗口可接受，
    换来的是"全家族宕机时不再每请求重复探测"——那正是探测最贵的时刻。
    主动失效兜住可感知的滞后：上游转发失败（502）与 WebUI 启停端点都会立即失效相关条目。
    """

    def __init__(
        self,
        ttl: float = GROUP_ROUTE_CACHE_TTL_S,
        *,
        avail_ttl: float = AVAIL_CACHE_TTL_S,
        clock=time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.avail_ttl = avail_ttl
        self._clock = clock
        self._entries: dict[str, tuple[GatewayModel | None, float]] = {}
        self._avail: dict[str, tuple[bool, float]] = {}

    def resolve(
        self, groups: dict[str, list[GatewayModel]], name: str
    ) -> GatewayModel | None:
        if self.ttl <= 0:
            return _resolve_group(groups, name)
        now = self._clock()
        hit = self._entries.get(name)
        if hit is not None and hit[1] > now:
            return hit[0]
        target = _resolve_group(groups, name)
        self._entries[name] = (target, now + self.ttl)
        return target

    def cached(self, name: str) -> bool | None:
        """成员可用性的缓存值；None = 未命中或已过期（`avail_ttl<=0` 时恒 None）。"""
        if self.avail_ttl <= 0:
            return None
        hit = self._avail.get(name)
        if hit is None or hit[1] <= self._clock():
            return None
        return hit[0]

    def record(self, name: str, ok: bool) -> None:
        """回填成员可用性判定；`avail_ttl<=0`（缓存关闭）时为 no-op。"""
        if self.avail_ttl <= 0:
            return
        self._avail[name] = (ok, self._clock() + self.avail_ttl)

    def invalidate(self, name: str) -> None:
        """丢弃某个家族的路由结果；键不存在时静默返回。"""
        self._entries.pop(name, None)

    def invalidate_model(self, name: str) -> None:
        """丢弃某个成员的可用性判定。

        两类场景必须调：
        - 502：与 `invalidate` 成对。只清路由、留着 `available=True`，重解析会立刻把
          同一个死端口再选一次，`/v1/models` 也会继续把死端口列为可用模型。
        - WebUI 启停/重启端点：否则 5s 的 avail TTL 会让界面在用户点了按钮之后
          仍显示旧状态，是可感知的产品回归。
        """
        self._avail.pop(name, None)


def _env_ttl(key: str, default: float) -> float:
    """读取 TTL 类环境变量；非法值告警回退默认（配置写错不该崩在启动期）。"""
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        logger.warning(f"{key} 非法，回退默认 {default}s")
        return default


async def probe_availability(
    cache: GroupRouteCache,
    names: Sequence[str],
    fetch: Callable[[str], bool],
    *,
    executor: ThreadPoolExecutor | None = None,
) -> dict[str, bool]:
    """按 `avail` 层批量探测可用性：TTL 内复用，只并发派发未命中项。

    网关 `/v1/models` 与 WebUI `/admin/api/overview`、`/admin/api/models` 共用本函数
    + 同一个 cache（webui 与 gateway 是**两个进程**，各自一份 cache；进程内三处共享）。

    `fetch` 由调用方提供而非在此直接调 `is_model_available`：各调用点的探测口径与
    **测试 patch 目标**不同——gateway 是模块级 `is_running_any` 绑定，webui 走
    `admin_models` 函数体内的延迟导入（patch `modelctl.core.process` 属性）。在此写死
    任一个绑定都会让另一处的测试桩静默失效（桩没被调到，探测真跑 → 恒 False）。

    值一次快照读全：探测期间 clock 可能跨过 TTL，二次读取会把已过期条目读成 None，
    同一请求内出现两套判定。
    """
    loop = asyncio.get_running_loop()
    snapshot = {n: cache.cached(n) for n in names}
    stale = [n for n in names if snapshot[n] is None]

    def _fetch_one(name: str) -> tuple[str, bool]:
        return name, fetch(name)

    if executor is None:
        results = await asyncio.gather(*(asyncio.to_thread(_fetch_one, n) for n in stale))
    else:
        results = await asyncio.gather(
            *(loop.run_in_executor(executor, _fetch_one, n) for n in stale)
        )

    available = {n: bool(v) for n, v in snapshot.items() if v is not None}
    for name, ok in results:
        cache.record(name, ok)
        available[name] = ok
    return available


def resolve_model(
    registry: dict[str, GatewayModel],
    body_model: str | None,
    default_model: str | None,
    groups: dict[str, list[GatewayModel]] | None = None,
    group_cache: GroupRouteCache | None = None,
) -> GatewayModel | None:
    """按 body.model 解析目标模型；支持家族（group）路由。

    顺序：body_model 命中 group（家族解析，无健康成员即 None 不回退）→
    body_model 精确匹配 name/alias → 回退 default_model（同样 group 优先）→ None。
    groups 为 None（未启用家族路由）时行为与旧版一致。
    group_cache：家族解析的 TTL 缓存；None 时每次实探（CLI / 测试等单次调用口径）。
    """

    def _group(name: str) -> GatewayModel | None:
        return (
            group_cache.resolve(groups, name)
            if group_cache is not None
            else _resolve_group(groups, name)
        )

    if groups and body_model and body_model in groups:
        return _group(body_model)
    if body_model and body_model in registry:
        return registry[body_model]
    if groups and default_model and default_model in groups:
        return _group(default_model)
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
    accounts_enabled: bool | None = None,
    accounts_store=None,
):
    """构建 FastAPI 网关应用（transport 供测试注入 httpx.MockTransport）。

    环境变量：GATEWAY_DEFAULT_MODEL（默认模型，缺省/未知 model 回退目标）；
    GATEWAY_CONTEXT_SWITCH（JSON 上下文切换规则，见 load_context_switch_rules）。
    audit_log：请求级审计日志；缺省时按 AUDIT_DIR（默认 <项目根>/data/audit）从 env 构造。
    groups：家族索引（group -> 成员列表）；调用方注入 registry 时缺省为空 dict，
    未注入时自动从 models/*.yaml 构建。
    stats_data_dir：用量持久化目录（与 stats 服务共用，使网关累计的 token 跨进程保留）。
    admin：True 时额外挂上 /admin/api/*（Web UI 管理面）与前端静态产物。仅
    `modelctl webui` 传 True；`modelctl gateway start` 保持纯数据面，管理 API 不对外暴露。
    accounts_enabled：三态——True 显式启用账号 Key 体系；False 显式关闭（等价 legacy
    单钥 `verify_client`）；None（缺省）读 env `ACCOUNTS_ENABLED`（`_parse_env_bool`，
    非法值回退 False）。启用时会挂 `app.state.accounts/limit_guard/accountant`，
    并在 lifespan 里 `accountant.start()/stop()`。
    accounts_store：可选，注入已初始化的 `AccountsStore`；未注入时按
    `accounts_db_path()` 构造并 `init_db()`（幂等）。
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

    # /health 探测结果缓存：resolve_model 在两条数据面通道上每请求调用，/v1/models 每次
    # 遍历全部 profile，都走同步 /health（阻塞事件循环）。两层 TTL 各自可配，=0 关闭；
    # 非法值告警回退默认（配置写错不该崩在启动期）。
    group_cache = GroupRouteCache(
        _env_ttl("GATEWAY_GROUP_ROUTE_TTL", GROUP_ROUTE_CACHE_TTL_S),
        avail_ttl=_env_ttl("GATEWAY_AVAIL_CACHE_TTL", AVAIL_CACHE_TTL_S),
    )

    # 请求级审计日志：缺省从 AUDIT_DIR（默认 <项目根>/data/audit，见 core/paths.py）构造；
    # 启动幂等的后台清理线程，应用关闭时 destroy 回收（绝不阻塞请求路径）。lifespan 管理线程生命周期。
    audit_log = audit_log or _new_audit_log(audit_dir())

    # ---------- Task 6：账号 Key 体系（账号 Key / 限额 / 会话 / 结算） ----------
    # 三态启用判定（None = 走 env ACCOUNTS_ENABLED；非法值回退 False，
    # 与 accounts.accounts_enabled 口径完全一致，避免"独立实现一遍"分叉）。
    if accounts_enabled is None:
        from modelctl.core.accounts import accounts_enabled as _accounts_env
        _accounts_on: bool = _accounts_env()
    else:
        _accounts_on = bool(accounts_enabled)

    accounts_store_obj = None
    limit_guard_obj = None
    accountant_obj = None
    if _accounts_on:
        # 延迟导入：让未启用 accounts 的部署在缺依赖时也保持可解析
        from modelctl.core.accounts.accountant import Accountant
        from modelctl.core.accounts.limits import LimitGuard
        from modelctl.core.accounts.store import AccountsStore
        # 若调用方注入了 store（测试常用 tmp_path + 预造用户/Key），信任调用方
        # 已 init_db()；否则按 defaults 构造。二者都保证 init_db 幂等不炸。
        if accounts_store is not None:
            accounts_store_obj = accounts_store
        else:
            accounts_store_obj = AccountsStore()
            accounts_store_obj.init_db()
        limit_guard_obj = LimitGuard()
        accountant_obj = Accountant(accounts_store_obj)

    @asynccontextmanager
    async def _lifespan(_app: "FastAPI"):
        """应用生命周期：启动审计清理线程 + accountant worker；关闭时回收。"""
        audit_log.ensure_cleanup_thread()
        if accountant_obj is not None:
            try:
                accountant_obj.start()
            except Exception as exc:  # noqa: BLE001 — start 异常不应阻塞应用启动
                logger.warning(f"accountant 启动异常（不阻塞应用）：{exc}")
        try:
            yield
        finally:
            if accountant_obj is not None:
                try:
                    # stop 会同步 drain 队列，保证 pending settle 一定落库
                    accountant_obj.stop()
                except Exception as exc:  # noqa: BLE001 — 关闭阶段异常不得冒泡
                    logger.warning(f"accountant 关闭异常：{exc}")
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
    # 家族路由 TTL 缓存挂点（测试与排障可直接读取 ttl / 条目数）
    app.state.group_route_cache = group_cache
    # Task 6 accounts 挂点：accounts 启用时全部三件 instance 一并挂上；
    # 未启用时保持 None（gateway.accounts_gate 通过 app.state._accounts_enabled
    # 走 legacy verify_client 分支，绝不触碰空指）。
    app.state._accounts_enabled = _accounts_on
    app.state.accounts = accounts_store_obj
    app.state.limit_guard = limit_guard_obj
    app.state.accountant = accountant_obj

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
    async def list_models(request: Request):
        # Task 6：统一走 gate（accounts 或 legacy verify_client），任一拒绝均落审计
        gate_resp, identity, label, _mode = accounts_gate(request)
        if gate_resp is not None:
            # 401/429 短路：短包审计（model 字段留空，尚无 target；auth 字段记录 label）
            audit_log.record(_build_audit_entry(
                model_name="", profile_name="", profile_engine="",
                path="models", stream=False,
                native_metrics=None, usage=None, gateway_metrics=None,
                status_code=gate_resp.status_code,
                # legacy: auth_<label>（ok/missing/invalid/unconfigured → 已 401 分支短路，故只可能是
                #        后三种）；accounts: label 本身已带语义前缀（accounts_invalid /
                #        limit_budget_exceeded / limit_concurrency_exceeded / limit_rate_limit_exceeded），
                #        直接透传。
                error=label if _mode == "accounts" else f"auth_{label}",
                finish_reason=None, input_char_len=0,
                auth=label, client_ip=client_ip_of(request),
                user_id=(identity.user_id if identity else None),
                key_id=(identity.key_id if identity else None),
            ))
            return gate_resp
        try:
            # 注册表同时含 name 与 alias 两个 key（指向同一 GatewayModel），须按 name 去重
            seen: set[str] = set()
            models = []
            for m in registry.values():
                if m.name not in seen:
                    seen.add(m.name)
                    models.append(m)

            # 探测集 = 注册表去重模型 ∪ 全部家族成员（家族成员可能被同名去重挤掉）。
            # 并集让家族名展示直接复用同一份判定，省掉旧实现里 _group_healthy 的第二轮探测。
            candidates: dict[str, GatewayModel] = {}
            for m in models:
                candidates.setdefault(m.name, m)
            for members in groups.values():
                for m in members:
                    candidates.setdefault(m.name, m)

            # 并发可用性探测：串行会让未运行模型各耗 timeout 秒，10 个模型累积到十几秒。
            # 过滤条件见 is_model_available：受管运行中，或无 PID 文件但端口健康（外部启动）。
            # TTL 内已判定过的直接复用，只派发未命中项——列表接口高频轮询时这是主要收益。
            available = await probe_availability(
                group_cache,
                list(candidates),
                lambda name: is_model_available(candidates[name]),
            )

            data = [
                {
                    "id": m.aliases[0] if m.aliases else m.name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "modelctl",
                }
                for m in models
                if available[m.name]
            ]
            # 家族逻辑名：组内有健康成员时展示（id=group 名，与具体成员 id 去重）
            existing_ids = {item["id"] for item in data}
            for group_name, members in groups.items():
                if group_name in existing_ids:
                    continue
                if any(available[m.name] for m in members):
                    data.append({"id": group_name, "object": "model", "created": 0, "owned_by": "modelctl"})
            return {"object": "list", "data": data}
        finally:
            # list_models 不 settle（无 tokens），但须 release 并发槽
            # （accounts 启用时 gate 已 acquire；未启用时 identity=None 分支 no-op）
            if identity is not None and limit_guard_obj is not None:
                try:
                    limit_guard_obj.release(identity.user_id)
                except Exception as exc:
                    logger.warning(f"list_models release 异常：{exc}")

    @app.post("/v1/messages")
    async def anthropic_proxy(request: Request):
        """Anthropic Messages API（/v1/messages）兼容透传。

        Trae CN 等客户端内置 Claude Agent SDK，使用 Anthropic 协议（POST
        /v1/messages，x-api-key 认证，SSE 流带 event: 行）而非 OpenAI 格式。
        vLLM 0.27+ 原生支持 /v1/messages，此处按 body.model 路由并原样透传；
        流式必须保留 event: 行（不能复用 /v1/chat/completions 的 data: 行解析）。

        Task 6 接入 accounts：先 `await request.json()` 解析 body（gate 前），
        把 `messages.content` 展开喂给 `accounts_gate(tpm_estimate=...)`，
        body 为空 JSON 时 est=0（TPM 不预占，仅 concurrency/rpm 托底）；
        出错（JSONDecode）时 response 400 短路 + release；SSE / 非流式收尾
        统一 `release` + `settle` + `add_tpm_actual`（复用闭包变量避免
        重复计算 usage）。
        """
        # 先读 body（gate 前），让 tpm_estimate 用到真实 prompt 长度
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            # gate 尚未放行时即便 body 出错也须先过 401/429 gate（保持 auth 短路优先）
            gate_resp, identity, label, _mode = accounts_gate(request)
            if gate_resp is not None:
                audit_log.record(_build_audit_entry(
                    model_name="", profile_name="", profile_engine="",
                    path="messages", stream=False,
                    native_metrics=None, usage=None, gateway_metrics=None,
                    status_code=gate_resp.status_code,
                    error=label if _mode == "accounts" else f"auth_{label}",
                    finish_reason=None, input_char_len=0,
                    auth=label, client_ip=client_ip_of(request),
                    user_id=(identity.user_id if identity else None),
                    key_id=(identity.key_id if identity else None),
                ))
                return gate_resp
            # gate 通过但 body 不合法：仅 release（不 settle）
            if identity is not None and limit_guard_obj is not None:
                try:
                    limit_guard_obj.release(identity.user_id)
                except Exception as exc:
                    logger.warning(f"anthropic 400 路径 release 异常：{exc}")
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "请求体必须是 JSON", "type": "invalid_request_error"}},
            )
        if not isinstance(body, dict):
            body = {}
        # Task 6：一次读 body 的 tpm estimate，同时 flush 请求里的 session_id 头
        _tpm_est = _accounts_tpm_estimate(body)
        _session_id_hdr = (request.headers.get("x-session-id") or "").strip() or None
        gate_resp, identity, label, _mode = accounts_gate(request, tpm_estimate=_tpm_est)
        if gate_resp is not None:
            # 401/429 短路：短包审计；被拒请求不 acquire（gate 内部已回滚）
            audit_log.record(_build_audit_entry(
                model_name="", profile_name="", profile_engine="",
                path="messages", stream=False,
                native_metrics=None, usage=None, gateway_metrics=None,
                status_code=gate_resp.status_code,
                error=label if _mode == "accounts" else f"auth_{label}",
                finish_reason=None, input_char_len=0,
                auth=label, client_ip=client_ip_of(request),
                user_id=(identity.user_id if identity else None),
                key_id=(identity.key_id if identity else None),
            ))
            return gate_resp
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
            f"auth_xkey={'x-api-key' in request.headers} auth={label}"
        )
        target = resolve_model(registry, body.get("model"), default_model, groups, group_cache)
        if target is None:
            err_msg = f"model not found: {body.get('model')}"
            # 404：release（不 settle）
            if identity is not None and limit_guard_obj is not None:
                try:
                    limit_guard_obj.release(identity.user_id)
                except Exception as exc:
                    logger.warning(f"anthropic 404 路径 release 异常：{exc}")
            # 审计：与 OpenAI 分支同口径，保留调用方原始模型名便于定位拼写错误。
            if audit_log is not None:
                audit_log.record(_build_audit_entry(
                    model_name=str(body.get("model") or ""), profile_name="", profile_engine="",
                    path="messages", stream=bool(body.get("stream")),
                    native_metrics=None, usage=None, gateway_metrics=None,
                    status_code=404, error=err_msg, finish_reason=None,
                    input_char_len=body_char_len,
                    auth=label, client_ip=client_ip_of(request),
                    user_id=(identity.user_id if identity else None),
                    key_id=(identity.key_id if identity else None),
                ))
            return JSONResponse(
                status_code=404,
                content={"error": {"message": err_msg, "type": "invalid_request_error"}},
            )
        # 审计：每次请求取目标模型的 audit_log（create_app 已统一注入；短路判断避免 502/404 无谓兜底）
        self_audit_log = target.audit_log
        # Task 6 settle 依赖：请求 id / 上游最终 usage / 完成时刻
        _request_id = getattr(request.state, "accounts_request_id", None)
        _tpm_est = getattr(request.state, "accounts_tpm_est", _tpm_est)
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
        # 无 key 引擎则剥离客户端认证头：准入 key 验完即丢，绝不转发上游（防皇冠
        # 密钥溢入不校验认证、却把请求头写进日志的引擎进程）。
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() in ("content-type", "anthropic-version", "anthropic-beta")
        }
        # Task 6 settle：从 body.messages 取最后一条 user content 作为 user_msg（Anthropic
        # 用 list-of-blocks 或 str 表达 content；仅处理 str 以便对话记录简洁）。
        _user_msg_for_settle = ""
        _msgs = body.get("messages") or []
        for _m in reversed(_msgs):
            if not isinstance(_m, dict) or _m.get("role") != "user":
                continue
            _c = _m.get("content")
            if isinstance(_c, str):
                _user_msg_for_settle = _c
                break
            elif isinstance(_c, list):
                # 简单取第一个 text block（避免透传 tools/thinking blocks 干扰）
                for _b in _c:
                    if isinstance(_b, dict) and _b.get("type") == "text" and _b.get("text"):
                        _user_msg_for_settle = str(_b["text"])
                        break
                break
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
                    # 上游非 2xx：SSE 走不到 _raw_sse（立即 aread+返回），
                    # 但 gate 已 acquire → 必须立即释放，否则并发 slot 泄漏。
                    if identity is not None and limit_guard_obj is not None:
                        try:
                            limit_guard_obj.release(identity.user_id)
                        except Exception as exc:
                            logger.warning(f"anthropic SSE 4xx/5xx 路径 release 异常：{exc}")
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
                        _assistant_text = "".join(texts)
                        _p_tokens = int((seen_usage or {}).get("input_tokens") or 0)
                        _c_tokens = int((seen_usage or {}).get("output_tokens") or 0)
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
                                    auth=label, client_ip=client_ip_of(request),
                                    user_id=(identity.user_id if identity else None),
                                    key_id=(identity.key_id if identity else None),
                                ))
                        except Exception as exc:
                            logger.warning(f"审计写盘异常（SSE 不中断）: {exc}")
                        # Task 6：并发槽释放 + 异步 settle + TPM 实际回补
                        if identity is not None and limit_guard_obj is not None:
                            try:
                                limit_guard_obj.release(identity.user_id)
                            except Exception as exc:
                                logger.warning(f"anthropic SSE release 异常：{exc}")
                        if identity is not None and accountant_obj is not None:
                            try:
                                _now_s = time.time()
                                _ttft_ms = int((_t_first - _t0) * 1000) if _t_first is not None else None
                                _latency_ms = int((time.monotonic() - _t0) * 1000)
                                _rid = str(_request_id or "")
                                accountant_obj.settle(
                                    user_id=identity.user_id,
                                    key_id=identity.key_id,
                                    model=target.name,
                                    prompt_tokens=_p_tokens,
                                    completion_tokens=_c_tokens,
                                    status=str(upstream.status_code),
                                    client_ip=client_ip_of(request),
                                    ttft_ms=_ttft_ms,
                                    latency_ms=_latency_ms,
                                    session_id=_session_id_hdr,
                                    title="",
                                    user_msg=_user_msg_for_settle,
                                    assistant_msg=_assistant_text,
                                    request_id=_rid,
                                    now=_now_s,
                                )
                                if limit_guard_obj is not None:
                                    limit_guard_obj.add_tpm_actual(
                                        identity.user_id,
                                        est=_tpm_est,
                                        actual=_p_tokens + _c_tokens,
                                        now=_now_s,
                                        request_id=_request_id,
                                    )
                            except Exception as exc:
                                logger.warning(f"anthropic SSE settle 异常（SSE 不中断）：{exc}")
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
            # Task 6 settle 用：非流式 usage 与 assistant 抽取
            _ns_usage: dict | None = None
            _ns_assistant: str = ""
            try:
                _d = json.loads(upstream.content)
                if isinstance(_d, dict):
                    _u = _d.get("usage")
                    if isinstance(_u, dict):
                        _ns_usage = _u
                    _ns_assistant = "".join(
                        b.get("text") or ""
                        for b in (_d.get("content") or [])
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
            except ValueError:
                pass
            # 审计：Anthropic 非流式 usage 在响应根级，无原生 metrics
            try:
                if self_audit_log is not None:
                    _t1 = time.monotonic()
                    _delta = max(_t1 - _t0, 1e-9)
                    _gm = {
                        "ttft_ms": None,  # 非流式无首延迟
                        "generation_time_ms": round(_delta * 1000.0, 2),
                        "tokens_per_second": (
                            round((_ns_usage.get("output_tokens") or 0) / _delta, 1)
                            if _ns_usage and _ns_usage.get("output_tokens")
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
                        usage=_ns_usage,
                        gateway_metrics=_gm,
                        status_code=upstream.status_code,
                        error=None,
                        finish_reason=None,  # Anthropic 非流式无 finish_reason 字段
                        input_char_len=body_char_len,
                        auth=label, client_ip=client_ip_of(request),
                        user_id=(identity.user_id if identity else None),
                        key_id=(identity.key_id if identity else None),
                    ))
            except Exception as exc:
                logger.warning(f"审计写盘异常（转发不受影响）: {exc}")
            # Task 6：并发槽释放 + 异步 settle + TPM 实际回补
            _ns_p = int((_ns_usage or {}).get("input_tokens") or 0)
            _ns_c = int((_ns_usage or {}).get("output_tokens") or 0)
            if identity is not None and limit_guard_obj is not None:
                try:
                    limit_guard_obj.release(identity.user_id)
                except Exception as exc:
                    logger.warning(f"anthropic 非流式 release 异常：{exc}")
            if identity is not None and accountant_obj is not None:
                try:
                    _now_s = time.time()
                    _rid = str(_request_id or "")
                    accountant_obj.settle(
                        user_id=identity.user_id,
                        key_id=identity.key_id,
                        model=target.name,
                        prompt_tokens=_ns_p,
                        completion_tokens=_ns_c,
                        status=str(upstream.status_code),
                        client_ip=client_ip_of(request),
                        ttft_ms=None,
                        latency_ms=int((time.monotonic() - _t0) * 1000),
                        session_id=_session_id_hdr,
                        title="",
                        user_msg=_user_msg_for_settle,
                        assistant_msg=_ns_assistant,
                        request_id=_rid,
                        now=_now_s,
                    )
                    if limit_guard_obj is not None:
                        limit_guard_obj.add_tpm_actual(
                            identity.user_id,
                            est=_tpm_est,
                            actual=_ns_p + _ns_c,
                            now=_now_s,
                            request_id=_request_id,
                        )
                except Exception as exc:
                    logger.warning(f"anthropic 非流式 settle 异常（转发不受影响）：{exc}")
            resp = Response(
                status_code=upstream.status_code,
                content=upstream.content,
                media_type=upstream.headers.get("content-type"),
            )
            await client.aclose()
            return resp
        except httpx.HTTPError as error:
            # 502 上游路径也须释放并发槽（gate 已 acquire 但转发失败）；
            # 不 settle——成功失败本就非 2xx，usage_records 应保留干净
            if identity is not None and limit_guard_obj is not None:
                try:
                    limit_guard_obj.release(identity.user_id)
                except Exception as exc:
                    logger.warning(f"anthropic 502 路径 release 异常：{exc}")
            # 上游不可达 → 作废该家族的缓存路由 + 该成员的可用性判定，下一请求重新探测；
            # 只清路由会留下 available=True，让 /v1/models 继续把死端口列为可用模型。
            group_cache.invalidate(target.group or target.name)
            group_cache.invalidate_model(target.name)
            await client.aclose()
            err_msg = f"后端不可达：{error}"
            # 审计：502 是最需要留痕的失败（引擎宕机/端口错），无此记录则前端
            # 错误列表永远看不到后端不可用；error 只记异常文本，不含响应体。
            try:
                if target.audit_log is not None:
                    target.audit_log.record(_build_audit_entry(
                        model_name=target.name, profile_name=target.name,
                        profile_engine=target.engine, path="messages",
                        stream=bool(body.get("stream")) if body else False,
                        native_metrics=None, usage=None, gateway_metrics=None,
                        status_code=502, error=err_msg[:500], finish_reason=None,
                        input_char_len=body_char_len,
                        auth=label, client_ip=client_ip_of(request),
                        user_id=(identity.user_id if identity else None),
                        key_id=(identity.key_id if identity else None),
                    ))
            except Exception as exc:
                logger.warning(f"审计写盘异常（502 路径不受影响）: {exc}")
            return JSONResponse(status_code=502, content={"error": {"message": err_msg, "type": "upstream_error"}})

    # /v1 与 /v1/{path:path} 共用同一处理器：redirect_slashes=False 后裸 /v1 不再
    # 307 重定向（重定向 Location 为根路径 /v1/，经 nginx 前缀路由会丢 /<node>/llm）。
    # 裸 /v1（连通性探测，如 hertz 客户端 POST baseUrl）直接返回 200 而非 404，
    # 避免客户端把 404 当作端点不可用而中止；真实请求走 /v1/chat/completions 等子路径。
    # 2026-09-07 起该探测同样要求客户端凭据（verify_client 在短路返回之前，见下）：
    # 无 key 一律 401，不再匿名放行——破坏性变更，依赖 baseUrl 探测的客户端需配 key。
    @app.post("/v1")
    @app.post("/v1/{path:path}")
    async def proxy(request: Request, path: str = ""):
        # Task 6：body 预读（gate 前）——accounts 启用时 tpm 预占估算需要 body
        # 实际 char 长度；legacy 模式下 body 也要提前读（当然 Starlette 允许
        # request.json() 重复调用，因 body bytes 已 cache 在 `self._body`）。
        body: dict | None = None
        if path:
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    body = {}
            except (ValueError, json.JSONDecodeError):
                body = None
        _tpm_est = _accounts_tpm_estimate(body) if body is not None else 0
        gate_resp, identity, label, _mode = accounts_gate(request, tpm_estimate=_tpm_est)
        if gate_resp is not None:
            logger.warning(f"网关请求被拒 path=/v1/{path} auth={label} ip={client_ip_of(request)}")
            audit_log.record(_build_audit_entry(
                model_name="", profile_name="", profile_engine="",
                path=path or "v1", stream=False,
                native_metrics=None, usage=None, gateway_metrics=None,
                status_code=gate_resp.status_code,
                error=f"auth_{label}", finish_reason=None,
                input_char_len=0, auth=label, client_ip=client_ip_of(request),
                user_id=(identity.user_id if identity else None),
                key_id=(identity.key_id if identity else None),
            ))
            return gate_resp
        # gate 放行：把 settle/release 需要的元数据挂局部变量（accounts 模式下
        # 已 attach 到 request.state；legacy 模式 identity=None，直接跳过后续分支）
        _request_id = getattr(request.state, "accounts_request_id", None)
        _tpm_est = getattr(request.state, "accounts_tpm_est", _tpm_est) or 0
        _session_id_hdr = (request.headers.get("x-session-id") or "").strip() or None
        # 预取 user_msg（settle 用；body 未读成功时 fallback ""）
        _user_msg_for_settle = ""
        if body is not None:
            _msgs = body.get("messages") or []
            for _m in reversed(_msgs):
                if not isinstance(_m, dict) or _m.get("role") != "user":
                    continue
                _c = _m.get("content")
                if isinstance(_c, str):
                    _user_msg_for_settle = _c
                    break
                elif isinstance(_c, list):
                    for _b in _c:
                        if isinstance(_b, dict) and _b.get("type") == "text" and _b.get("text"):
                            _user_msg_for_settle = str(_b["text"])
                            break
                    break

        def _release_if_acquired() -> None:
            """gate 已 acquire（identity 非空）时必须释放，否则并发槽泄漏。"""
            if identity is None or limit_guard_obj is None:
                return
            try:
                limit_guard_obj.release(identity.user_id)
            except Exception as exc:
                logger.warning(f"proxy release 异常：{exc}")

        if not path:
            # 裸 /v1 探测：放行后放行前释放（不 settle）
            _release_if_acquired()
            return JSONResponse(status_code=200, content={"status": "ok"})
        if path not in ("chat/completions", "completions", "embeddings"):
            _release_if_acquired()
            return JSONResponse(
                status_code=404,
                content={"error": {"message": f"unknown endpoint: /v1/{path}", "type": "invalid_request_error"}},
            )
        if body is None:
            _release_if_acquired()
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
            f"auth={label} stream_options={'stream_options' in body}"
        )
        target = resolve_model(registry, body.get("model"), default_model, groups, group_cache)
        if target is None:
            _release_if_acquired()
            err_msg = f"model not found: {body.get('model')}"
            # 审计：模型名写请求原始值（resolve 失败即无 profile 可参照），否则
            # "调用方拼错模型名"这类高频错误在审计里完全无痕。
            if audit_log is not None:
                audit_log.record(_build_audit_entry(
                    model_name=str(body.get("model") or ""), profile_name="", profile_engine="",
                    path=path, stream=bool(body.get("stream")),
                    native_metrics=None, usage=None, gateway_metrics=None,
                    status_code=404, error=err_msg, finish_reason=None,
                    input_char_len=len(json.dumps(body, ensure_ascii=False, separators=(",", ":"))),
                    auth=label, client_ip=client_ip_of(request),
                    user_id=(identity.user_id if identity else None),
                    key_id=(identity.key_id if identity else None),
                ))
            return JSONResponse(
                status_code=404,
                content={"error": {"message": err_msg, "type": "invalid_request_error"}},
            )
        # 上下文切换（附录 B.3）：按估算输入长度路由到 high/balanced/light 变体
        if context_rules:
            prompt_tokens = estimate_prompt_tokens(body)
            # 匹配键顺序：请求原始 model → group 名 → 解析后的成员名。
            # 只传 target.name 会让"经 group 路由而来"的请求永远匹配不上规则。
            switched = apply_context_switch(
                registry, context_rules,
                (str(body.get("model") or ""), target.group or "", target.name),
                prompt_tokens,
            )
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
        # 上游认证永远用 profile 有效 key（覆盖客户端自配 key：客户端如 Trae CN 配置的
        # key 可能与后端不一致，透传会导致 vLLM 401；网关代劳认证更稳）。
        # 无 key 引擎（Ollama 等）一律不带 Authorization：客户端准入 key 验完即丢，
        # 绝不转发上游——否则皇冠密钥会溢入不校验该头、却把请求头写进日志的引擎进程。
        if up_key:
            headers["Authorization"] = f"Bearer {up_key}"
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
                _up_rid = (upstream.headers.get("x-request-id") or "").strip() or None
                if upstream.status_code >= 400:
                    content = await upstream.aread()
                    await client.aclose()
                    # 审计：上游 4xx/5xx 原样透传给客户端，但网关侧必须留痕，
                    # 否则排障时只看到"客户端收到 400"却无任何上下文。
                    try:
                        if self_audit_log is not None:
                            self_audit_log.record(_build_audit_entry(
                                model_name=target.name, profile_name=target.name,
                                profile_engine=target.engine, path=path, stream=True,
                                native_metrics=None, usage=None, gateway_metrics=None,
                                status_code=upstream.status_code,
                                error=content.decode("utf-8", "replace")[:500],
                                finish_reason=None, input_char_len=body_char_len,
                                auth=label, client_ip=client_ip_of(request),
                                user_id=(identity.user_id if identity else None),
                                key_id=(identity.key_id if identity else None),
                                upstream_request_id=_up_rid,
                            ))
                    except Exception as exc:
                        logger.warning(f"审计写盘异常（上游错误响应不中断）: {exc}")
                    # 早退分支直接 return Response（非 StreamingResponse），
                    # SSE 生成器的 finally 永不执行 → 必须在此释放并发槽，
                    # 否则 gate 已 acquire 的槽永久泄漏（对照 anthropic 代理同分支）。
                    # 不 settle：非 2xx 保留干净 usage_records（与 404/502 早退一致）。
                    _release_if_acquired()
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
                                    auth=label, client_ip=client_ip_of(request),
                                    user_id=(identity.user_id if identity else None),
                                    key_id=(identity.key_id if identity else None),
                                    collector_diff_prompt=_diff_prompt,
                                    collector_diff_completion=_diff_completion,
                                    upstream_request_id=_up_rid,
                                ))
                        except Exception as exc:
                            logger.warning(f"审计写盘异常（SSE 不中断）: {exc}")
                        # Task 6：并发槽释放 + 异步 settle + TPM 实际回补
                        _assistant_text = "".join(collected["content"])
                        _p_tokens = int((seen_usage or {}).get("prompt_tokens") or 0)
                        _c_tokens = int((seen_usage or {}).get("completion_tokens") or 0)
                        if identity is not None and limit_guard_obj is not None:
                            try:
                                limit_guard_obj.release(identity.user_id)
                            except Exception as exc:
                                logger.warning(f"proxy SSE release 异常：{exc}")
                        if identity is not None and accountant_obj is not None:
                            try:
                                _now_s = time.time()
                                _ttft_ms = int((_t_first - _t0) * 1000) if _t_first is not None else None
                                _latency_ms = int((time.monotonic() - _t0) * 1000)
                                _rid = str(_request_id or "")
                                accountant_obj.settle(
                                    user_id=identity.user_id,
                                    key_id=identity.key_id,
                                    model=target.name,
                                    prompt_tokens=_p_tokens,
                                    completion_tokens=_c_tokens,
                                    status=str(upstream.status_code),
                                    client_ip=client_ip_of(request),
                                    ttft_ms=_ttft_ms,
                                    latency_ms=_latency_ms,
                                    session_id=_session_id_hdr,
                                    title="",
                                    user_msg=_user_msg_for_settle,
                                    assistant_msg=_assistant_text,
                                    request_id=_rid,
                                    now=_now_s,
                                )
                                if limit_guard_obj is not None:
                                    limit_guard_obj.add_tpm_actual(
                                        identity.user_id,
                                        est=_tpm_est,
                                        actual=_p_tokens + _c_tokens,
                                        now=_now_s,
                                        request_id=_request_id,
                                    )
                            except Exception as exc:
                                logger.warning(f"proxy SSE settle 异常（SSE 不中断）：{exc}")
                        await client.aclose()

                return StreamingResponse(_sse_stream(), status_code=upstream.status_code, media_type=ctype)
            # 审计差分基线：发送前取 snapshot（无副作用；禁止用 get_snapshot 触发 HTTP）
            _snap_before = target.collector.snapshot() if target.collector is not None and hasattr(target.collector, "snapshot") else None
            upstream = await client.post(url, json=body, headers=headers)
            _t1 = time.monotonic()
            _up_rid = (upstream.headers.get("x-request-id") or "").strip() or None
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
                _ns_assistant = str(_msg.get("content") or "") if _msg else ""
                logger.info(f"OpenAI 非流式响应摘要 content={str(_msg.get('content'))[:200]!r} " f"tool_calls={bool(_msg.get('tool_calls'))} reasoning={bool(_msg.get('reasoning'))}")
            else:
                _ns_assistant = ""
            # 审计 + Task 6 settle 共用的本地变量（`_usage_a` 在 try 外须有定义）
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
            # 审计：旁路读取 metrics / usage / finish_reason，写入必须包裹，异常不得影响转发
            try:
                if self_audit_log is not None:
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
                        # 上游 4xx/5xx 的响应体留在 error 里；2xx 保持 None 以免污染
                        # `auth_stats`/CLI 的错误统计（_entry_is_error 只看 status_code）。
                        error=(
                            upstream.content.decode("utf-8", "replace")[:500]
                            if upstream.status_code >= 400 else None
                        ),
                        finish_reason=_finish,
                        input_char_len=body_char_len,
                        auth=label, client_ip=client_ip_of(request),
                        user_id=(identity.user_id if identity else None),
                        key_id=(identity.key_id if identity else None),
                        collector_diff_prompt=_diff_prompt,
                        collector_diff_completion=_diff_completion,
                        upstream_request_id=_up_rid,
                    ))
            except Exception as exc:
                logger.warning(f"审计写盘异常（转发不受影响）: {exc}")
            # Task 6：并发槽释放 + 异步 settle + TPM 实际回补
            _ns_p = int((_usage_a or {}).get("prompt_tokens") or 0)
            _ns_c = int((_usage_a or {}).get("completion_tokens") or 0)
            if identity is not None and limit_guard_obj is not None:
                try:
                    limit_guard_obj.release(identity.user_id)
                except Exception as exc:
                    logger.warning(f"proxy 非流式 release 异常：{exc}")
            if identity is not None and accountant_obj is not None:
                try:
                    _now_s = time.time()
                    _rid = str(_request_id or "")
                    accountant_obj.settle(
                        user_id=identity.user_id,
                        key_id=identity.key_id,
                        model=target.name,
                        prompt_tokens=_ns_p,
                        completion_tokens=_ns_c,
                        status=str(upstream.status_code),
                        client_ip=client_ip_of(request),
                        ttft_ms=None,
                        latency_ms=int((_t1 - _t0) * 1000),
                        session_id=_session_id_hdr,
                        title="",
                        user_msg=_user_msg_for_settle,
                        assistant_msg=_ns_assistant,
                        request_id=_rid,
                        now=_now_s,
                    )
                    if limit_guard_obj is not None:
                        limit_guard_obj.add_tpm_actual(
                            identity.user_id,
                            est=_tpm_est,
                            actual=_ns_p + _ns_c,
                            now=_now_s,
                            request_id=_request_id,
                        )
                except Exception as exc:
                    logger.warning(f"proxy 非流式 settle 异常（转发不受影响）：{exc}")
            resp = Response(
                status_code=upstream.status_code,
                content=upstream.content,
                media_type=upstream.headers.get("content-type"),
            )
            await client.aclose()
            return resp
        except httpx.HTTPError as error:
            # 502 上游路径也须释放并发槽（gate 已 acquire 但转发失败）；不 settle
            if identity is not None and limit_guard_obj is not None:
                try:
                    limit_guard_obj.release(identity.user_id)
                except Exception as exc:
                    logger.warning(f"proxy 502 路径 release 异常：{exc}")
            # 上游不可达 → 作废该家族的缓存路由 + 该成员的可用性判定，下一请求重新探测；
            # 只清路由会留下 available=True，让 /v1/models 继续把死端口列为可用模型。
            group_cache.invalidate(target.group or target.name)
            group_cache.invalidate_model(target.name)
            await client.aclose()
            err_msg = f"后端不可达：{error}"
            # 审计：同 anthropic 分支——引擎宕机必须留痕，否则错误列表看不到 502。
            try:
                if target.audit_log is not None:
                    target.audit_log.record(_build_audit_entry(
                        model_name=target.name, profile_name=target.name,
                        profile_engine=target.engine, path=path,
                        stream=bool(body.get("stream")) if body else False,
                        native_metrics=None, usage=None, gateway_metrics=None,
                        status_code=502, error=err_msg[:500], finish_reason=None,
                        input_char_len=body_char_len,
                        auth=label, client_ip=client_ip_of(request),
                        user_id=(identity.user_id if identity else None),
                        key_id=(identity.key_id if identity else None),
                    ))
            except Exception as exc:
                logger.warning(f"审计写盘异常（502 路径不受影响）: {exc}")
            return JSONResponse(status_code=502, content={"error": {"message": err_msg, "type": "upstream_error"}})

    if admin:
        # 管理面：/admin/api/* + 前端 SPA。须在全部 /v1 路由注册后挂载——静态兜底
        # 路由 /{full_path:path} 依赖注册顺序接住未命中的 GET，不能提前注册。
        from modelctl.core.webui.admin_router import create_admin_router
        from modelctl.core.webui.server import mount_static

        admin_router = create_admin_router()
        app.include_router(admin_router, prefix="/admin/api")
        app.state.task_manager = admin_router.task_manager

        # Task 7：账号自助面板（`/api/account/*`）—— 登录/密钥/用量/会话。
        # 走 Bearer JWT（require_account），非管理面 API_KEY；与 /admin/api 语义独立，
        # 故不走 `create_admin_router` 聚合。
        # **无条件注册**：accounts 未启用时端点内部也会读 `app.state.accounts`
        # → None → 503 accounts_disabled；路由不挂则 login 404，前端无法清晰
        # 区分"未部署 accounts"与"权限不足"，503 + code 是给部署者的显性诊断。
        from modelctl.core.webui.account_self import (
            create_account_self_router,
        )

        app.include_router(create_account_self_router(), prefix="/api/account")

        mount_static(app)

    return app


def main() -> None:
    """独立运行入口：python -m modelctl.core.gateway。"""
    from modelctl.core.logging import setup_logging, uvicorn_log_config
    from modelctl.core.timezone import apply_timezone

    load_env()
    # 后台模式下 stdout 已重定向到 launch-<name>.log：只挂 console handler
    # （无 ANSI、LOG_LEVEL 过滤），避免与 CLI 进程争抢 modelctl.log 轮转
    setup_logging(file_sink=False)
    apply_timezone()
    host = os.environ.get("GATEWAY_HOST", "0.0.0.0")
    port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
    read_timeout = float(os.environ.get("GATEWAY_READ_TIMEOUT", "600"))
    default_model = os.environ.get("GATEWAY_DEFAULT_MODEL")
    data_dir = usage_data_dir()  # 与 stats 服务共用持久化目录（同一解析口径，绝不各算一份）

    import uvicorn

    app = create_app(
        default_model=default_model,
        read_timeout=read_timeout,
        stats_data_dir=data_dir,
    )
    print(f"modelctl 网关运行于 http://{host}:{port}/v1（默认模型：{default_model or '未配置'}）", flush=True)
    # access 日志（含 /metrics、/health 等心跳轮询）降级为 DEBUG 语义
    uvicorn.run(app, host=host, port=port, log_level="info", log_config=uvicorn_log_config())


if __name__ == "__main__":
    main()
