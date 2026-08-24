#!/usr/bin/env python3
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

KNOWN_ENGINES = {"llamacpp", "ollama", "vllm", "sglang", "unsloth"}
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ProfileError(ValueError):
    """profile 校验或插值失败。"""


@dataclass
class Profile:
    name: str
    engine: str
    port: int
    api_key: str | None = None
    engine_config: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    path: Path | None = None
    tool_call_rounds: int | None = None


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


def _to_profile(raw: dict[str, Any], path: Path) -> Profile:
    """将已插值的原始字典转换为 Profile，并执行字段校验。"""
    src = path.name
    for key in ("name", "engine", "port"):
        if key not in raw or raw[key] in (None, ""):
            raise ProfileError(f"{src}：缺少必填字段 {key}")
    engine = str(raw["engine"])
    if engine not in KNOWN_ENGINES:
        raise ProfileError(f"{src}：未知引擎 {engine}（支持：{sorted(KNOWN_ENGINES)}）")
    port = int(raw["port"])
    if not 1 <= port <= 65535:
        raise ProfileError(f"{src}：port 必须在 1-65535，当前 {port}")
    engine_config = raw.get(engine) or {}
    if not isinstance(engine_config, dict):
        raise ProfileError(f"{src}：{engine} 段必须是映射")
    aliases = _parse_aliases(raw, src)
    tool_call_rounds = raw.get("tool_call_rounds")
    if tool_call_rounds is not None:
        try:
            tool_call_rounds = int(tool_call_rounds)
        except (TypeError, ValueError) as exc:
            raise ProfileError(f"{src}：tool_call_rounds 必须是整数") from exc
    return Profile(
        name=str(raw["name"]),
        engine=engine,
        port=port,
        api_key=raw.get("api_key") or None,
        engine_config=engine_config,
        usage=raw.get("usage") or {},
        aliases=aliases,
        path=path,
        tool_call_rounds=tool_call_rounds,
    )


def _parse_aliases(raw: dict[str, Any], src: str) -> list[str]:
    """解析顶层 alias / aliases 字段为别名列表（供网关/nginx 短名路由使用）。

    支持 `alias: short` 或 `aliases: [a, b]` 两种写法；别名须为非空字符串，
    且不得与 name 相同。缺省返回空列表。
    """
    value = raw.get("aliases", raw.get("alias", []))
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [str(v) for v in value]
    else:
        return []
    aliases: list[str] = []
    for alias in candidates:
        if not alias or alias == raw.get("name"):
            raise ProfileError(f"{src}：alias 必须是非空字符串且不能与 name 相同（当前：{alias!r}）")
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
