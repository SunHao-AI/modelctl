#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/cluster/tokens.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : 集群准入令牌与节点令牌生成
# ===============================================================================

"""core/cluster/tokens.py — join_token（一次性准入）/ node_token（节点长期身份）。

设计文档 §10.2。比较一律经 hmac.compare_digest 恒定时间，空值 fail-closed。
"""

from __future__ import annotations

import hmac
import secrets

_ENTROPY_BYTES = 24


def new_join_token() -> str:
    return "JT-" + secrets.token_urlsafe(_ENTROPY_BYTES)


def new_node_token() -> str:
    return "NT-" + secrets.token_urlsafe(_ENTROPY_BYTES)


def token_matches(candidate: str, expected: str) -> bool:
    """恒定时间比较；任一为空返回 False（fail-closed）。"""
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate, expected)
