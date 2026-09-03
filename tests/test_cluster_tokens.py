#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_tokens.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : cluster.tokens 令牌生成与恒定时间比较测试
# ===============================================================================
from __future__ import annotations

from modelctl.core.cluster.tokens import new_join_token, new_node_token, token_matches


def test_prefixes_and_uniqueness() -> None:
    a, b = new_join_token(), new_join_token()
    assert a.startswith("JT-") and b.startswith("JT-") and a != b
    assert new_node_token().startswith("NT-")


def test_token_matches_fail_closed() -> None:
    assert token_matches("abc", "abc")
    assert not token_matches("abc", "abd")
    assert not token_matches("", "")          # 空值 fail-closed
    assert not token_matches("abc", "")
    assert not token_matches("", "abc")


def test_token_matches_non_ascii_no_raise() -> None:
    assert not token_matches("密钥", "NT-abc")
    assert not token_matches("NT-abc", "密钥")
    assert token_matches("NT-abc", "NT-abc")
