#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : script/benchmark_latency.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : LLM 推理延迟基准测试
# ===============================================================================

"""script/benchmark_latency.py — 本地 LLM 推理延迟/吞吐基准测试。

用法示例：
    python script/benchmark_latency.py \
        --base-url http://127.0.0.1:18888/v1 \
        --api-key root123456 \
        --model deepseek-v4-flash-llamacpp \
        --output data/benchmark/deepseek-v4-flash-llamacpp.json

可选参数：
    --iterations N   每场景重复次数（默认 1，取每次结果逐条输出）
    --prompt-len N   自定义输入字符数（默认 0 = 使用场景内置 prompt）
    --max-tokens N   覆盖生成上限（默认 0 = 使用场景默认）

测试内容：
- short：短输入，测量首 token 延迟和短输出速率
- medium：中等输入，模拟日常 agent 问答
- long：长输入（默认 8K tokens），测量长上下文解码能力

输出（--output 以 .csv 结尾时输出 CSV，否则输出 JSON）字段：
- model：模型名
- scenario：short/medium/long
- prompt_tokens：输入 token 数（优先取响应 usage 的真实计数，否则按字符/4 估算）
- completion_tokens：输出 token 数（同上）
- first_token_latency_ms：首 token 延迟（毫秒）
- total_time_s：总耗时（秒）
- tok_per_s：输出 token 速率（tok/s）
- vram_delta_mb：请求期间各 GPU 已用显存的最大增量（MB，nvidia-smi 不可用时为 null）
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from openai import OpenAI
except ImportError as exc:  # pragma: no cover
    print(f"错误：缺少 openai 包，请安装：pip install openai ({exc})")
    sys.exit(1)


SCENARIOS: dict[str, tuple[str, int]] = {
    "short": ("Hello, how are you?", 256),
    "medium": (
        "Explain the concept of overfitting in machine learning, including bias-variance tradeoff, "
        "regularization techniques, and how cross-validation helps. Keep it concise but complete.",
        512,
    ),
    "long": (
        "Summarize the following research abstract in 500 words:\n\n"
        + (
            "Large language models have demonstrated remarkable capabilities across a wide range of tasks, "
            "but their deployment at scale remains challenging due to memory and computational requirements. "
        )
        * 500,
        1024,
    ),
}


def _ensure_output_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _query_vram_mb() -> list[int] | None:
    """查询各 GPU 已用显存（MB）；nvidia-smi 不可用或解析失败时返回 None。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode != 0:
            return None
        values = [int(line.strip()) for line in out.stdout.splitlines() if line.strip()]
        return values or None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _build_prompt(scenario_prompt: str, prompt_len: int) -> str:
    """按指定字符数构造输入 prompt；prompt_len <= 0 时使用场景内置 prompt。"""
    if prompt_len <= 0:
        return scenario_prompt
    filler = "基准测试占位提示词，用于控制输入长度以测量延迟与吞吐。"
    return (filler * (prompt_len // len(filler) + 1))[:prompt_len]


def _extract_usage(chunk) -> tuple[int | None, int | None]:
    """从流式 chunk 提取 usage 计数（stream_options=include_usage 时最后一个 chunk 携带）。"""
    usage = getattr(chunk, "usage", None)
    if usage is None:
        return None, None
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    return (
        int(prompt_tokens) if isinstance(prompt_tokens, int) else None,
        int(completion_tokens) if isinstance(completion_tokens, int) else None,
    )


def _run_one(
    client: OpenAI,
    model: str,
    scenario: str,
    prompt: str,
    max_tokens: int,
    iteration: int,
) -> dict[str, Any]:
    """执行单次推理并记录指标（真实 token 计数 + 峰值显存增量）。"""
    vram_before = _query_vram_mb()
    # 流式请求以便测量首 token 延迟；include_usage 让最后一个 chunk 携带真实 token 计数
    t_start = time.perf_counter()
    stream = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        stream=True,
        stream_options={"include_usage": True},
    )
    first_token_seen = False
    first_token_time: float | None = None
    chunks: list[str] = []
    usage_prompt: int | None = None
    usage_completion: int | None = None
    for chunk in stream:
        if not first_token_seen:
            first_token_time = time.perf_counter()
            first_token_seen = True
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            chunks.append(delta)
        up, uc = _extract_usage(chunk)
        if up is not None and uc is not None:
            usage_prompt, usage_completion = up, uc
    t_end = time.perf_counter()

    completion = "".join(chunks)
    # 优先使用响应 usage 的真实计数，缺失时按字符数粗略估算（tiktoken 可能未安装）
    prompt_tokens = usage_prompt if usage_prompt is not None else len(prompt) // 4
    completion_tokens = usage_completion if usage_completion is not None else len(completion) // 4 or 1

    first_latency_ms = (
        round((first_token_time - t_start) * 1000, 2)
        if first_token_time is not None
        else None
    )
    total_time = round(t_end - t_start, 3)

    # 峰值显存增量：请求前后各 GPU 已用显存的最大差值（容错，测不到记 null）
    vram_delta_mb: int | None = None
    vram_after = _query_vram_mb()
    if vram_before and vram_after and len(vram_before) == len(vram_after):
        deltas = [a - b for a, b in zip(vram_after, vram_before, strict=False)]
        if deltas:
            vram_delta_mb = max(deltas)

    return {
        "model": model,
        "scenario": scenario,
        "iteration": iteration,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "first_token_latency_ms": first_latency_ms,
        "total_time_s": total_time,
        "tok_per_s": round(completion_tokens / total_time, 2) if total_time > 0 else 0,
        "vram_delta_mb": vram_delta_mb,
    }


def _write_json(results: list[dict[str, Any]], output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


def _write_csv(results: list[dict[str, Any]], output_path: Path) -> None:
    if not results:
        return
    columns = list(results[0].keys())
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(results)


def main() -> int:
    parser = argparse.ArgumentParser(description="本地 LLM 推理基准测试")
    parser.add_argument("--base-url", default="http://127.0.0.1:18888/v1", help="OpenAI 兼容 API 基础 URL")
    parser.add_argument("--api-key", default="root123456", help="API Key")
    parser.add_argument("--model", required=True, help="模型名（profile name）")
    parser.add_argument(
        "--scenarios",
        default="short,medium,long",
        help="逗号分隔的测试场景（默认 short,medium,long）",
    )
    parser.add_argument("--iterations", type=int, default=1, help="每场景重复次数（默认 1）")
    parser.add_argument("--prompt-len", type=int, default=0, help="自定义输入字符数；0=使用场景内置 prompt")
    parser.add_argument("--max-tokens", type=int, default=0, help="覆盖生成上限；0=使用场景默认")
    parser.add_argument("--output", default="data/benchmark/result.json", help="结果输出路径（.json 或 .csv）")
    args = parser.parse_args()

    client = OpenAI(base_url=args.base_url, api_key=args.api_key)
    requested = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    results: list[dict[str, Any]] = []

    for scenario in requested:
        if scenario not in SCENARIOS:
            print(f"未知场景：{scenario}，可用：{', '.join(SCENARIOS)}")
            continue
        scenario_prompt, default_max_tokens = SCENARIOS[scenario]
        prompt = _build_prompt(scenario_prompt, args.prompt_len)
        max_tokens = args.max_tokens or default_max_tokens
        print(f"运行场景：{scenario}（iterations={args.iterations}，prompt_len={len(prompt)}）...", flush=True)
        for iteration in range(1, args.iterations + 1):
            try:
                result = _run_one(client, args.model, scenario, prompt, max_tokens, iteration)
                results.append(result)
                print(
                    f"  [{iteration}/{args.iterations}] prompt={result['prompt_tokens']} "
                    f"completion={result['completion_tokens']} "
                    f"first_token={result['first_token_latency_ms']}ms "
                    f"total={result['total_time_s']}s tok/s={result['tok_per_s']} "
                    f"vram_delta={result['vram_delta_mb']}MB",
                    flush=True,
                )
            except Exception as exc:  # pragma: no cover
                print(f"  [{iteration}/{args.iterations}] 失败：{exc}", flush=True)
                results.append({
                    "model": args.model,
                    "scenario": scenario,
                    "iteration": iteration,
                    "error": str(exc),
                })

    output_path = Path(args.output)
    _ensure_output_dir(output_path)
    if output_path.suffix.lower() == ".csv":
        _write_csv(results, output_path)
    else:
        _write_json(results, output_path)
    print(f"\n结果已保存：{output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
