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
from collections.abc import Iterable

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


def build_client_auth_map(client_key: str, extra_keys: Iterable[str] = ()) -> str:
    """生成客户端凭据校验 map 片段（nginx http 块），供 B 机 include。

    产出两组白名单，与两条路径的下游校验能力精确对齐：
      $llm_reject      网关 location 用——只认 GATEWAY_CLIENT_API_KEY，与网关自身
                       verify_client 口径完全一致（网关只认这一把，多放会表现为
                       "过了 nginx 却被网关 401" 的配置矛盾）。
      $llm_reject_all  模型直连 / 用量 location 用——额外放行各 profile api_key，
                       因为直连不改写 Authorization，vLLM 等引擎只认 profile key；
                       只放行 client_key 会让既有直连客户端全断。

    双通道与网关一致：Authorization: Bearer <key> 或 x-api-key: <key> 任一命中即放行。

    产物含明文密钥：上传后须 chmod 600，且严禁入库。
    """
    key = (client_key or "").strip()
    if not key:
        raise ProfileError("客户端密钥为空，无法生成 nginx 鉴权片段")
    extra: list[str] = []
    for item in extra_keys:
        e = (item or "").strip()
        if e and e != key and e not in extra:
            extra.append(e)
    for k in [key, *extra]:
        if '"' in k or "\n" in k or "\\" in k:
            raise ProfileError(f"密钥含双引号/反斜杠/换行，nginx map 值不安全：{k[:4]}***")

    def _maps(suffix: str, keys: list[str]) -> list[str]:
        lines = []
        for var, out, prefix in (
            ("$http_authorization", f"$llm_bearer_{suffix}", "Bearer "),
            ("$http_x_api_key", f"$llm_xkey_{suffix}", ""),
        ):
            lines.append(f"map {var} {out} {{")
            lines.append("    default 0;")
            lines += [f'    "{prefix}{k}" 1;' for k in keys]
            lines.append("}")
        reject = "$llm_reject" if suffix == "gw" else "$llm_reject_all"
        lines += [
            f'map "$llm_bearer_{suffix}$llm_xkey_{suffix}" {reject} {{',
            '    "11" 0;',
            '    "10" 0;',
            '    "01" 0;',
            "    default 1;",
            "}",
        ]
        return lines

    lines = [
        "# ---- 客户端凭据校验（modelctl nginx-snippet 生成，勿手改）----",
        "# 产物含明文密钥：chmod 600，严禁入库",
        "# $llm_reject=网关 location 专用（仅 client key）；$llm_reject_all=直连/用量 location 用（含 profile key）",
    ]
    lines += _maps("gw", [key])
    lines += _maps("all", [key, *extra])
    return "\n".join(lines) + "\n"
