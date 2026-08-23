#!/usr/bin/env python3
"""core/gpu_utils.py — GPU 列表解析与校验（引擎无关，避免与 engines.base 循环依赖）。"""

from __future__ import annotations


class GPUValidationError(ValueError):
    """GPU 列表解析或校验失败。"""


def parse_gpu_list(raw: str | list[int] | None) -> list[int] | None:
    """把逗号分隔字符串 / 整数列表解析为去重的 GPU 索引列表；空值返回 None。"""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    if isinstance(raw, list):
        items = [int(x) for x in raw]
    else:
        parts = [p.strip() for p in str(raw).split(",") if p.strip() != ""]
        try:
            items = [int(p) for p in parts]
        except ValueError as exc:
            raise GPUValidationError(f"gpu_list 包含非整数项：{raw!r}") from exc
    if len(items) != len(set(items)):
        dup = next(x for x in items if items.count(x) > 1)
        raise GPUValidationError(f"gpu_list 存在重复 GPU 索引：{dup}")
    return items


def validate_gpu_selection(gpus: list[int], available: list[int]) -> None:
    """严格校验选中 GPU 是否都在可用范围内；越界抛 GPUValidationError。"""
    if not gpus:
        return
    available_set = set(available)
    invalid = [g for g in gpus if g not in available_set]
    if invalid:
        raise GPUValidationError(
            f"[gpu_list] 配置的 GPU 索引 {gpus} 超出可用范围。\n"
            f"当前可用 GPU 索引：{','.join(str(g) for g in sorted(available_set))}"
        )


def resolve_gpu_list(
    profile_value: str | list[int] | None,
    cli_value: str | None,
    env_value: str | None,
) -> list[int] | None:
    """按优先级解析最终 GPU 列表：profile > CLI > 环境变量；均未指定返回 None。"""
    for candidate in (profile_value, cli_value, env_value):
        parsed = parse_gpu_list(candidate)
        if parsed is not None:
            return parsed
    return None
