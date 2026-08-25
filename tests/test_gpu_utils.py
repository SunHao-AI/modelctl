#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_gpu_utils.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : GPU 工具测试
# ===============================================================================

"""core/gpu_utils.py 单元测试。"""

import pytest

from modelctl.core.gpu_utils import GPUValidationError, parse_gpu_list, resolve_gpu_list, validate_gpu_selection


def test_parse_gpu_list_string():
    assert parse_gpu_list("0,1,2") == [0, 1, 2]


def test_parse_gpu_list_list():
    assert parse_gpu_list([3, 4]) == [3, 4]


def test_parse_gpu_list_none():
    assert parse_gpu_list(None) is None


def test_parse_gpu_list_empty_string():
    assert parse_gpu_list("") is None


def test_parse_gpu_list_duplicate():
    with pytest.raises(ValueError, match="重复"):
        parse_gpu_list("0,1,1")


def test_parse_gpu_list_bad_list_item():
    with pytest.raises(GPUValidationError, match="非整数"):
        parse_gpu_list([0, None])


def test_parse_gpu_list_non_integer_string():
    with pytest.raises(GPUValidationError, match="非整数"):
        parse_gpu_list("0,a")


def test_validate_gpu_selection_ok():
    validate_gpu_selection([0, 1], [0, 1, 2, 3])


def test_validate_gpu_selection_out_of_range():
    with pytest.raises(GPUValidationError, match="超出可用范围"):
        validate_gpu_selection([0, 5], [0, 1, 2, 3])


def test_resolve_gpu_list_priority():
    assert resolve_gpu_list("0,1", "2,3", "4,5") == [0, 1]
    assert resolve_gpu_list(None, "2,3", "4,5") == [2, 3]
    assert resolve_gpu_list(None, None, "4,5") == [4, 5]
    assert resolve_gpu_list(None, None, None) is None
