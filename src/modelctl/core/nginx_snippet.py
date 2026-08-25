#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/nginx_snippet.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : nginx 路由片段生成
# ===============================================================================

"""core/nginx_snippet.py — 从 models/*.yaml 生成 nginx 多模型路由 map 片段。"""

from __future__ import annotations

import re

from modelctl.core.profile import Profile, ProfileError

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def build_llm_map(profiles: list[Profile], node_id: str, host: str, gateway_port: int = 5003) -> str:
    """生成 `map $uri $llm_model_target` 片段，供 B 机 nginx include。

    node_id 为 URL 数字前缀（如 210），host 为节点 IP（如 192.168.77.210）。
    每个 profile 的 name 与 alias 都会生成条目，指向同一后端；
    模型名/别名必须是 nginx 正则安全的标识符（字母数字、点、连字符、下划线）。

    统一网关入口 `/<node>/llm/v1`（按 body.model 分发，见 GATEWAY_PORT）也生成条目，
    否则该路径会落空并掉入 nginx 兜底规则（如 location /），导致 502。
    """
    for p in profiles:
        for identifier in [p.name, *p.aliases]:
            if not _SAFE_NAME_RE.match(identifier):
                raise ProfileError(
                    f"模型标识 {identifier} 含 nginx 正则不安全字符（仅允许 [A-Za-z0-9._-]）"
                )
    lines = ["map $uri $llm_model_target {", '    default "";']
    # 统一网关入口（v1 非模型名，先声明避免歧义；精确匹配无尾斜杠的 /<node>/llm/v1）
    lines.append(f"    ~^/{node_id}/llm/v1/  http://{host}:{gateway_port};")
    lines.append(f"    ~^/{node_id}/llm/v1$  http://{host}:{gateway_port};")
    for p in sorted(profiles, key=lambda x: x.name):
        lines.append(f"    ~^/{node_id}/llm/{p.name}/  http://{host}:{p.port};")
        for alias in p.aliases:
            lines.append(f"    ~^/{node_id}/llm/{alias}/  http://{host}:{p.port};")
    lines.append("}")
    return "\n".join(lines) + "\n"
