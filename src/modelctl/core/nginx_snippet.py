#!/usr/bin/env python3
"""core/nginx_snippet.py — 从 models/*.yaml 生成 nginx 多模型路由 map 片段。"""

from __future__ import annotations

import re

from modelctl.core.profile import Profile, ProfileError

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def build_llm_map(profiles: list[Profile], node_id: str, host: str) -> str:
    """生成 `map $uri $llm_model_target` 片段，供 B 机 nginx include。

    node_id 为 URL 数字前缀（如 210），host 为节点 IP（如 192.168.77.210）。
    每个 profile 的 name 与 alias 都会生成条目，指向同一后端；
    模型名/别名必须是 nginx 正则安全的标识符（字母数字、点、连字符、下划线）。
    """
    for p in profiles:
        for identifier in [p.name, *p.aliases]:
            if not _SAFE_NAME_RE.match(identifier):
                raise ProfileError(
                    f"模型标识 {identifier} 含 nginx 正则不安全字符（仅允许 [A-Za-z0-9._-]）"
                )
    lines = ["map $uri $llm_model_target {", '    default "";']
    for p in sorted(profiles, key=lambda x: x.name):
        lines.append(f"    ~^/{node_id}/llm/{p.name}/  http://{host}:{p.port};")
        for alias in p.aliases:
            lines.append(f"    ~^/{node_id}/llm/{alias}/  http://{host}:{p.port};")
    lines.append("}")
    return "\n".join(lines) + "\n"
