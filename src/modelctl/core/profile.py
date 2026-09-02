#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/profile.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 模型 profile 加载与校验
# ===============================================================================

"""core/profile.py — 模型 profile（models/<name>.yaml）加载、${VAR} 插值与校验。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from modelctl.core.envfile import PROJECT_ROOT

KNOWN_ENGINES = {"llamacpp", "ollama", "vllm", "sglang", "unsloth",
                 "aphrodite", "lmdeploy", "tensorrt_llm", "tokenspeed"}
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ProfileError(ValueError):
    """profile 校验或插值失败。"""


@dataclass
class Profile:
    name: str          # CLI 标识符，缺省自动推导为 {group}-{engine}[-{variant}]
    engine: str        # 引擎类型；从父目录名推断或 YAML 显式指定
    port: int
    variant: str = ""               # light / high / pp ...（空=默认变体）
    api_key: str | None = None
    engine_config: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    group: str | None = None        # 模型家族名（网关路由用）
    path: Path | None = None
    tool_call_rounds: int | None = None
    max_output_tokens: int | None = None
    # 网关策略（§1.2 配置化）：
    #   thinking_disabled    : bool   | None  — 强制关闭 thinking（默认 False；None 时由 group 白名单决定）
    #   reasoning_effort_map : dict     | None  — 自定义 reasoning_effort 枚举映射（None 时回退全局 _REASONING_EFFORT_MAP）
    thinking_disabled: bool | None = None
    reasoning_effort_map: dict[str, str] | None = None
    # §1.3 配置化：自定义 per-request 原生指标字段名映射（SGlang/Aphrodite 等
    # 未来的响应 metrics 字段名不同或需要覆盖默认时通过 YAML 提供；None 时
    # 回退到引擎适配器 native_metrics_mapping() 的默认值）
    native_metrics_mapping: dict[str, str] | None = None


_GATEWAY_NATIVE_KEYS = ("rate", "ttft_ms", "gen_time_ms", "prompt_tokens", "completion_tokens")


def _parse_gateway(raw: dict[str, Any], src: str) -> tuple[bool | None, dict[str, str] | None, dict[str, str] | None]:
    """解析顶层 `gateway` 段 → (thinking_disabled, reasoning_effort_map, native_metrics_mapping)。

    字段可缺；类型不匹配的字段告警并返回 None（不抛错）。
    native_metrics_mapping 键必须是 _GATEWAY_NATIVE_KEYS 的子集（允许部分指定）。
    """
    g = raw.get("gateway")
    if not isinstance(g, dict):
        return None, None, None
    td = g.get("thinking_disabled")
    rd = g.get("reasoning_effort_map")
    nm = g.get("native_metrics_mapping")
    thinking = bool(td) if isinstance(td, bool) else None
    if thinking is None and td is not None:
        logger.warning(f"{src}：gateway.thinking_disabled 必须是 bool，已忽略 current={td!r}")
    remap: dict[str, str] | None = None
    if isinstance(rd, dict):
        if all(isinstance(k, str) and isinstance(v, str) for k, v in rd.items()):
            remap = {str(k): str(v) for k, v in rd.items()}
        else:
            logger.warning(f"{src}：gateway.reasoning_effort_map 必须是 str→str 映射，已忽略 current={rd!r}")
    native: dict[str, str] | None = None
    if isinstance(nm, dict):
        bad = [k for k, v in nm.items() if not isinstance(k, str) or not isinstance(v, str) or k not in _GATEWAY_NATIVE_KEYS]
        if not bad:
            native = {k: v for k, v in nm.items()}
        else:
            logger.warning(f"{src}：gateway.native_metrics_mapping 仅支持键 {list(_GATEWAY_NATIVE_KEYS)}；"
                           f"非法键被忽略 {bad!r}（当前 {nm!r}）")
    return thinking, remap, native


def _interpolate(value: Any, source: str) -> Any:
    """递归地对字符串、字典、列表进行 ${VAR} 环境变量插值。"""
    if isinstance(value, str):

        def _sub(m: re.Match) -> str:
            var = m.group(1)
            env_val = os.environ.get(var)
            if env_val is None or env_val == "":
                raise ProfileError(f"{source}：插值变量 {var} 未在环境变量/.env 中定义")
            return env_val

        return _VAR_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _interpolate(v, source) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, source) for v in value]
    return value


def _resolve_engine(raw: dict[str, Any], path: Path) -> str:
    """解析 engine：优先用 YAML 显式值；否则从父目录名推断。"""
    explicit = raw.get("engine")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    # 子目录文件 models/{engine}/{name}.yaml → parent dir name
    parent_name = path.parent.name.lower()  # type: ignore[attr-defined]
    if parent_name in KNOWN_ENGINES:
        logger.debug(f"{path.name}：从父目录推断 engine={parent_name!r}")
        return str(parent_name)
    raise ProfileError(
        f"{path.name}：无法确定引擎类型。请将文件放入 {sorted(KNOWN_ENGINES)} 子目录，"
        "或在 YAML 中显式设置 `engine:`"
    )


def _resolve_group(raw: dict[str, Any], path: Path) -> str:
    """解析 group（模型家族名）：优先用 YAML 值；否则从文件名自动推导。

    若 variant 已声明且文件后缀匹配，则去掉 -{variant} 后剩余部分即为 group。
    """
    explicit = raw.get("group")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    stem = path.stem
    variant = raw.get("variant", "")
    # auto-strip known suffix: {stem}-{variant}
    if isinstance(variant, str) and variant:
        suffix_to_strip = f"-{variant}"
        if stem.endswith(suffix_to_strip):
            candidate = stem[: -len(suffix_to_strip)]
            return candidate.strip()
    # 无匹配或未设 variant → 全文件名作为 group
    return stem


def _to_profile(raw: dict[str, Any], path: Path) -> Profile:
    """将已插值的原始字典转换为 Profile，并执行字段校验。"""
    src = path.name

    # ── port（唯一不可推导的必填项）──
    if "port" not in raw or raw["port"] in (None, ""):
        raise ProfileError(f"{src}：缺少必填字段 `port`")
    port = int(raw["port"])
    if not 1 <= port <= 65535:
        raise ProfileError(f"{src}：port 必须在 1-65535，当前 {port}")

    # ── engine（YAML > parent dir）──
    engine = _resolve_engine(raw, path)
    if engine not in KNOWN_ENGINES:
        raise ProfileError(f"{src}：未知引擎 {engine!r}（支持：{sorted(KNOWN_ENGINES)}）")

    # ── group / variant（显式 > 文件名推导）──
    group = _resolve_group(raw, path)
    raw_variant = str(raw.get("variant", "") or "")

    # ── name（YAML 值优先；缺省自动拼接 {group}-{engine}[-{variant}]）──
    explicit_name = raw.get("name")
    if isinstance(explicit_name, str) and explicit_name.strip():
        resolved_name = explicit_name.strip()
    else:
        base = f"{group}-{engine}"
        resolved_name = f"{base}-{raw_variant}" if raw_variant else base

    # ── engine 配置段（以实际引擎名查找）──
    engine_config = raw.get(engine) or {}
    if not isinstance(engine_config, dict):
        raise ProfileError(f"{src}：{engine} 段必须是映射")

    aliases = _parse_aliases(raw, src, resolved_name)
    thinking_disabled, reasoning_effort_map, native_metrics_mapping = _parse_gateway(raw, src)
    tool_call_rounds = raw.get("tool_call_rounds")
    if tool_call_rounds is not None:
        try:
            tool_call_rounds = int(tool_call_rounds)
        except (TypeError, ValueError) as exc:
            raise ProfileError(f"{src}：tool_call_rounds 必须是整数") from exc
    max_output_tokens = raw.get("max_output_tokens")
    if max_output_tokens is not None:
        try:
            max_output_tokens = int(max_output_tokens)
        except (TypeError, ValueError) as exc:
            raise ProfileError(f"{src}：max_output_tokens 必须是整数") from exc
    return Profile(
        name=resolved_name,
        engine=engine,
        port=port,
        variant=str(raw_variant),
        api_key=raw.get("api_key") or None,
        engine_config=engine_config,
        usage=raw.get("usage") or {},
        aliases=aliases,
        group=group if isinstance(group, str) and group.strip() else None,
        path=path,
        tool_call_rounds=tool_call_rounds,
        max_output_tokens=max_output_tokens,
        thinking_disabled=thinking_disabled,
        reasoning_effort_map=reasoning_effort_map,
        native_metrics_mapping=native_metrics_mapping,
    )


def _parse_aliases(raw: dict[str, Any], src: str, resolved_name: str | None = None) -> list[str]:
    """解析顶层 alias / aliases 字段为别名列表（供网关/nginx 短名路由使用）。

    支持 `alias: short` 或 `aliases: [a, b]` 两种写法；别名须为非空字符串，
    且不得与 profile name 相同。缺省返回空列表。
    """
    value = raw.get("aliases", raw.get("alias", []))
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [str(v) for v in value]
    else:
        return []
    aliases: list[str] = []
    profile_name = resolved_name or (raw.get("name") and str(raw["name"]))
    for alias in candidates:
        if not alias:
            raise ProfileError(f"{src}：alias 必须是非空字符串（当前：{alias!r}）")
        if isinstance(profile_name, str) and profile_name == alias:
            raise ProfileError(
                f"{src}：alias ({alias!r}) 不能与 name ({profile_name!r}) 相同"
            )
        if alias not in aliases:
            aliases.append(alias)
    return aliases


def load_profile(name: str, models_dir: Path | None = None) -> Profile:
    """加载指定 name 的 YAML profile。

    先按文件名匹配 models/<name>.yaml（根目录优先，其次递归子目录）；
    未命中时回退按 YAML 内 name 字段匹配（兼容文件名与标识不一致，
    如 <base>-<engine>.yaml 内 name 为 deepseek-v4-flash-llamacpp）。
    """
    models_dir = models_dir or PROJECT_ROOT / "models"
    candidates = [
        models_dir / f"{name}.yaml",
        *sorted(models_dir.rglob(f"{name}.yaml")),
    ]
    for path in candidates:
        if path.is_file():
            return _load_profile_from_path(path)
    profiles = list_profiles(models_dir)
    for profile in profiles:
        if profile.name == name:
            return profile
    available = [p.name for p in profiles]
    hint = f"可用模型：{', '.join(available)}" if available else "models 目录下暂无可用 profile"
    raise ProfileError(
        f"profile 不存在：{name}（{hint}；可运行 `modelctl list` 查看。"
        "若对应 YAML 存在但未列出，多为 ${VAR} 插值失败，请检查 .env 环境变量）"
    )


def _load_profile_from_path(path: Path) -> Profile:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ProfileError(f"{path.name}：YAML 语法错误：{e}") from e
    if not isinstance(raw, dict):
        raise ProfileError(f"{path.name}：顶层必须是映射")
    return _to_profile(_interpolate(raw, path.name), path)


def list_profiles(models_dir: Path | None = None) -> list[Profile]:
    """递归扫描 models_dir 下所有 *.yaml profile，根目录优先并去重。"""
    models_dir = models_dir or PROJECT_ROOT / "models"
    if not models_dir.is_dir():
        return []
    root_files = sorted(models_dir.glob("*.yaml"))
    sub_files = sorted(p for p in models_dir.rglob("*.yaml") if p not in root_files)
    seen: set[str] = set()
    result: list[Profile] = []
    for p in root_files + sub_files:
        try:
            profile = _load_profile_from_path(p)
        except ProfileError as e:
            logger.warning(f"跳过 profile 文件 {p}：{e}")
            continue
        if profile.name in seen:
            logger.warning(f"忽略子目录中重复的 profile：{profile.name}（{p}）")
            continue
        seen.add(profile.name)
        result.append(profile)
    return result
