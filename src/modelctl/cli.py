#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/cli.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 多模型部署启动器 CLI 入口
# ===============================================================================

"""modelctl.py — 多模型部署启动器 CLI 入口。

子命令：start <name> [--timeout 300] / stop <name> / restart <name> /
        status [name] / list / probe /
        stats start|stop|restart|status / gateway start|stop|restart|status /
        all start|stop|restart|status / ui start|stop <name>

流程约定：
- 所有子命令先 load_env()（注入 .env），再 probe() 探测硬件能力。
- start：load_profile → get_adapter → check_requirements（打印 warnings）→
  pre_start → build_command → start_detached → wait_health（等待期间引擎进程
  早退则立即失败并按错误标记截取日志摘录；仍存活才按"健康检查超时 + 日志尾部
  50 行"处理，均返回 1）→ post_start → 打印访问地址与日志路径。
- stop 对 ollama 引擎特判：serve 由本工具拉起（PID 文件存在）且无其他
  ollama profile 在运行时才停掉 serve；否则仅 unload_model + 删除 PID 记录。
- 错误处理：ProfileError / RequirementError 捕获后打印消息并返回 2。
"""

from __future__ import annotations

import argparse
import json
import os
import re as _re
import sys
import time
import urllib.request
from datetime import datetime as _dt_dt, timedelta
from pathlib import Path

from loguru import logger

import modelctl.core.compat_rules  # noqa: F401 —— 导入即注册内置规则
from modelctl.core import all_service
from modelctl.core.capabilities import ENGINE_BINARIES, ENGINE_INSTALL_HINTS, probe
from modelctl.core.colors import _apply, color_enabled, format_status
from modelctl.core.deps import ensure_packages
from modelctl.core.envfile import load_env
from modelctl.core.envs import (
    EngineEnvError,
    known_targets,
    remove as envs_remove,
    setup as envs_setup,
    status as envs_status,
)
from modelctl.core.logging import setup_logging
from modelctl.core.nginx_snippet import build_llm_map
from modelctl.core.process import (
    is_running,
    launch_log,
    open_local,
    pid_file,
    start_detached,
    stop_instance,
)
from modelctl.core.profile import Profile, ProfileError, list_profiles, load_profile
from modelctl.core.ufw import ensure_ufw_allow
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError

# 受管虚拟环境目标：托管引擎（vllm / sglang）+ 独立子项目（gateway）。
# 与 core.envs.known_targets() 保持单一事实来源；改 envs 配置这里自动跟着变。
ENV_TARGETS: list[str] = list(known_targets())


def _extract_models_dir(argv: list[str]) -> tuple[Path | None, list[str]]:
    """提取任意位置的 --models-dir（含 = 形式），保证测试中放在子命令后也可用。

    返回 (models_dir, 移除该参数后的剩余 argv)。
    """
    rest: list[str] = []
    models_dir: Path | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--models-dir":
            if i + 1 < len(argv):
                models_dir = Path(argv[i + 1])
                i += 2
                continue
        elif arg.startswith("--models-dir="):
            models_dir = Path(arg.split("=", 1)[1])
            i += 1
            continue
        rest.append(arg)
        i += 1
    return models_dir, rest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modelctl", description="多模型部署启动器")
    parser.add_argument("--models-dir", type=Path, default=None, help="models 目录（默认项目根 models/，测试用）")
    parser.add_argument("--no-color", action="store_true", help="禁用彩色输出（等同 MODELCTL_NO_COLOR=1）")
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd in ("start", "stop", "restart", "status"):
        p = sub.add_parser(cmd)
        p.add_argument("name", nargs="?" if cmd == "status" else None)
        if cmd in ("start", "restart"):
            # 默认 600s：vLLM 首次冷启动（torch.compile + warmup + CUDA graph 捕获）实测约 6 分钟
            p.add_argument("--timeout", type=float, default=600, help="健康检查超时秒数（默认 600）")
            p.add_argument("--gpus", default=None, help="逗号分隔的 GPU 索引，如 0,1,2（覆盖环境变量 MODELCTL_GPUS）")
    sub.add_parser("list", help="列出所有 profile")
    sub.add_parser("probe", help="探测硬件与引擎二进制")
    sp = sub.add_parser("stats", help="用量统计服务控制")
    sp.add_argument("action", choices=["start", "stop", "restart", "status"])
    gp = sub.add_parser("gateway", help="统一网关（model 参数路由）控制")
    gp.add_argument("action", choices=["start", "stop", "restart", "status"])
    ap = sub.add_parser("all", help="一键启停（默认模型 + 网关 + 统计）")
    ap.add_argument("action", choices=["start", "stop", "restart", "status"])
    ap.add_argument("--model", default=None, help="默认模型 profile（缺省解析 GATEWAY_DEFAULT_MODEL）")
    ap.add_argument("--timeout", type=float, default=600, help="模型健康检查超时秒数（默认 600）")
    ap.add_argument("--gpus", default=None, help="逗号分隔的 GPU 索引，如 0,1,2（覆盖环境变量 MODELCTL_GPUS）")
    au = sub.add_parser("audit", help="请求级审计日志查询/统计/清理")
    au.add_argument("sub", nargs="?", choices=["path", "stats"], default=None,
                    help="子命令：path | stats（缺省=查询最近 N 条；--cleanup 走清理）")
    au.add_argument("--model", default=None, help="按 model 字段过滤")
    au.add_argument("--endpoints", default=None,
                    help="逗号分隔端点列表，如 chat/completions,messages")
    au.add_argument("--since", default=None, dest="since_str",
                    help="起始时间：1h / 24h / 7d / ISO，如 \"2026-08-31T08:00:00\"")
    au.add_argument("--limit", type=int, default=20, help="条数上限，默认 20")
    au.add_argument("--json", action="store_true", help="JSONL 输出")
    au.add_argument("--cleanup", action="store_true", help="清理过期审计文件")
    au.add_argument("--dry-run", action="store_true", help="配合 --cleanup：仅打印不删除")
    up = sub.add_parser("ui", help="Web 管理控制台控制（unsloth studio UI）")
    up.add_argument("action", choices=["start", "stop"])
    up.add_argument("name", help="profile 名称（实例记为 ui-<name>，与推理服务独立）")
    up.add_argument("--port", type=int, default=None, help="控制台监听端口（默认 yaml unsloth.ui.port 或 8888）")
    up.add_argument("--host", default=None, help="控制台绑定地址（默认 yaml unsloth.ui.host 或 0.0.0.0）")
    up.add_argument(
        "--allow-from",
        dest="allow_from",
        action="append",
        default=[],
        metavar="IP",
        help="允许直连端口的来源 IP，启动时添加对应 ufw 规则；可重复（默认 yaml unsloth.ui.allow_from）",
    )
    ns = sub.add_parser("nginx-snippet", help="生成 nginx 多模型路由 map 片段")
    ns.add_argument("--node", required=True, help="节点编号（URL 前缀，如 210）")
    ns.add_argument("--host", required=True, help="节点 IP（如 192.168.77.210）")
    ep = sub.add_parser("env", help="专用虚拟环境管理（vllm / sglang / gateway）")
    ep.add_argument("action", choices=["setup", "list", "remove"])
    ep.add_argument(
        "engine",
        nargs="?",
        default=None,
        choices=ENV_TARGETS,
        help=f"受管目标：{' / '.join(ENV_TARGETS)}（list 不需要）",
    )
    # §2.2 TensorRT-LLM 引擎编译
    tp = sub.add_parser("trtllm", help="TensorRT-LLM 编译/检查子命令")
    tp.add_argument("action", choices=["build", "status"])
    tp.add_argument("name", help="profile 名称（status 可选名，缺省=all）")
    return parser


def _display_width(text: str) -> int:
    """估算字符串在等宽终端中的显示宽度（CJK 字符计为 2）。"""
    width = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf" or "\uf900" <= ch <= "\ufaff":
            width += 2
        else:
            width += 1
    return width


def _ljust_width(text: str, width: int) -> str:
    """按显示宽度左对齐，不足部分补空格。"""
    return text + " " * max(width - _display_width(text), 0)


def _print_table(headers: list[str], rows: list[list], *, dim_indices: tuple[int, ...] = ()) -> None:
    """按动态列宽打印类 Excel 对齐表格（表头 + 分隔线 + 数据行）。

    颜色规则（非 TTY 自动回退纯文本）：
    - 表头：青色加粗（TABLE_HEADER）
    - dim_indices 中列索引：灰色（DIM）—— 用于端口/速率/标识符等次要列。
    - 状态列（值为已知状态字符串自动识别）：按状态着色。
    """
    if not rows:
        # 空表也输出表头（带颜色），让结构可见
        print(_table_paint("  ".join(headers), "TABLE_HEADER"))
        return
    col_count = len(headers)
    widths = [_display_width(h) for h in headers]
    for row in rows:
        for i in range(col_count):
            widths[i] = max(widths[i], _display_width(str(row[i])))

    # 表头（彩色）
    header_cells = [_table_paint(_ljust_width(str(headers[i]), widths[i]), "TABLE_HEADER") for i in range(col_count)]
    print("  ".join(header_cells))
    # 分隔线（灰色）
    print(_table_paint("  ".join("-" * w for w in widths), "TABLE_SEP"))
    # 数据行（状态列按值着色，次要列灰色）
    state_words = {"运行中", "已外部启动", "已停止", "正常", "无响应", "PID 异常", "未就绪"}
    for row in rows:
        cells: list[str] = []
        for i in range(col_count):
            value = str(row[i])
            cell_pad = _ljust_width(value, widths[i])
            if value in state_words:
                # 状态值按语义着色；用空格补齐剩余宽度（padding 不参与着色）
                padding = " " * max(widths[i] - _display_width(value), 0)
                cells.append(_status_paint(value) + padding)
            elif i in dim_indices:
                cells.append(_table_paint(cell_pad, "DIM"))
            else:
                cells.append(cell_pad)
        print("  ".join(cells))


def _status_paint(value: str) -> str:
    """状态值 → 语义色 ANSI 字符串（非 TTY 原样返回）。"""
    if not color_enabled():
        return value
    return format_status(value, value)


def _table_paint(text: str, style: str) -> str:
    """按表格样式 ANSI 上色（非 TTY 原样返回）。"""
    if not color_enabled() or not text:
        return text
    return _apply(text, style)


def _port_health_ok(profile: Profile, timeout: float = 2.0) -> bool:
    """探测实例端口 /health 是否响应（带上游 API key），用于发现非 modelctl 拉起的运行实例。

    绕过代理（open_local）以适配设置了系统代理的机器；任何异常（连接拒绝/超时等）
    均视为"未运行"，不阻断状态输出。
    """
    try:
        params = profile.engine_config or {}
        key = params.get("api_key") or profile.api_key
    except Exception:  # noqa: BLE001
        key = profile.api_key
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{profile.port}/health", headers=headers)
        with open_local(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001 —— 连接失败/超时/HTTP 错误均视为未运行
        return False


def _instance_state(profile: Profile | None = None, name: str | None = None) -> str:
    """判断实例状态（PID 文件优先，无 PID 时按端口兜底探测"外部运行"）。

    状态取值：
    - 运行中       PID 文件存在且进程存活（modelctl 拉起或受管实例）
    - PID 异常     PID 文件存在但进程已退出
    - 已外部启动   无 PID 文件，但端口 /health 响应正常（如 docker 容器拉起）
    - 已停止       无 PID 文件且端口无响应
    """
    if name is None and profile is not None:
        name = profile.name
    assert name is not None
    pf = pid_file(name)
    if pf.is_file():
        return "运行中" if is_running(name) else "PID 异常"
    # 无 PID 文件：兜底探测端口是否由外部服务（docker/supervise 等）撑起
    if profile is not None and _port_health_ok(profile):
        return "已外部启动"
    return "已停止"


def _cmd_start(args, models_dir: Path | None, caps) -> int:
    profile = load_profile(args.name, models_dir)
    r = all_service.start_profile(profile, caps, args.timeout)
    if r.status == "skipped":
        logger.info(r.detail)
    return 0 if r.status in ("ok", "skipped") else 1


def _cmd_stop(args, models_dir: Path | None, caps) -> int:
    profile = load_profile(args.name, models_dir)
    all_service.stop_profile(profile, caps, models_dir)
    return 0


def _cmd_restart(args, models_dir: Path | None, caps) -> int:
    profile = load_profile(args.name, models_dir)
    r = all_service.restart_profile(profile, caps, args.timeout)
    return 0 if r.status in ("ok", "skipped") else 1


def _recommended_output_tokens(context_length: int) -> int:
    """根据总上下文长度推荐输出长度，保证 输入 + 输出 <= 总上下文。

    启发式规则：输出长度约为总长度的 1/8，同时限制在 1024-8192 之间，
    并为输入至少预留 1024 tokens；小上下文（<=1024）时各分一半。
    """
    if context_length <= 1024:
        return max(256, context_length // 2)
    return min(max(1024, context_length // 8), context_length - 1024, 8192)


def _agent_config_info(profile: Profile) -> dict[str, str]:
    """从 profile 提取智能体常用配置参数（上下文窗口、采样参数、视觉支持等）。"""
    ec = profile.engine_config
    engine = profile.engine

    if engine == "llamacpp":
        ctx = ec.get("ctx_size")
        # llamacpp 的 ctx_size 留空时引擎默认 1,048,576 tokens/槽
        ctx_display = "1048576" if ctx in (None, "") else str(int(ctx))
    elif engine == "vllm":
        ctx = ec.get("max_model_len")
        ctx_display = str(int(ctx)) if ctx is not None else "-"
    else:  # ollama / sglang / unsloth
        ctx = ec.get("context_length")
        ctx_display = str(int(ctx)) if ctx is not None else "-"

    if profile.max_output_tokens is not None:
        output_context = str(profile.max_output_tokens)
    else:
        try:
            output_context = str(_recommended_output_tokens(int(ctx_display)))
        except ValueError:
            output_context = "-"

    try:
        input_context = str(int(ctx_display) - int(output_context))
    except ValueError:
        input_context = "-"

    if engine == "llamacpp":
        # 默认开启视觉；仅当显式 vision: off/false/no/0 时关闭
        vision_val = str(ec.get("vision", "on")).lower()
        vision = "否" if vision_val in ("off", "false", "no", "0") else "是"
    else:
        # ollama / vllm / sglang / unsloth 后端本身具备多模态能力
        vision = "是"

    return {
        "context_length": ctx_display,
        "input_context": input_context,
        "output_context": output_context,
        "tool_call_rounds": str(profile.tool_call_rounds) if profile.tool_call_rounds is not None else "-",
        "vision": vision,
        "temperature": str(ec.get("temperature", "-")),
        "top_p": str(ec.get("top_p", "-")),
        "top_k": str(ec.get("top_k", "-")),
    }


def _price_rate_text(profile: Profile) -> str:
    """从 usage 段读取 token 计费费率（元/千 token）。

    返回如 "输入 1.0 元/千token，输出 2.0 元/千token"；未配置返回 "未配置"。
    """
    usage = profile.usage or {}
    price_in = usage.get("price_in")
    price_out = usage.get("price_out")
    if price_in is None and price_out is None:
        return "未配置"

    def _fmt(value: object) -> str:
        try:
            return f"{float(value):g}"
        except (TypeError, ValueError):
            return "-"

    return f"输入 {_fmt(price_in)} 元/千token，输出 {_fmt(price_out)} 元/千token"


def _benchmark_token_rate(adapter) -> tuple[float, float, int] | None:
    """主动测速：发一次短流式请求，返回 (input_rate, output_rate, ttft_ms)。

    实现复用 modelctl.core.stats.benchmark_rates（stats 服务窗口无流量时也用它兜底）。
    """
    from modelctl.core.stats import benchmark_rates

    url = f"http://127.0.0.1:{adapter.profile.port}/v1/chat/completions"
    return benchmark_rates(url, adapter.upstream_api_key(), adapter.upstream_model_name())


def _token_rate_data(profile, caps) -> dict:
    """Token 速率数据：stats 优先，无效/为 0 时主动测速。

    返回 {"prompt_rate": float|None, "predicted_rate": float|None,
          "ttft_ms": int|None, "source": "stats"|"bench"|None}。
    """
    port = int(os.environ.get("USAGE_PORT", "5002"))
    url = f"http://127.0.0.1:{port}/api/usage?model={profile.name}"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError, json.JSONDecodeError):
        data = {}
    if isinstance(data, dict) and data.get("isValid"):
        prompt_rate = data.get("prompt_rate")
        predicted_rate = data.get("predicted_rate")
        if isinstance(prompt_rate, (int, float)) and isinstance(predicted_rate, (int, float)) and (prompt_rate > 0 or predicted_rate > 0):
            native_ttft = data.get("ttft_ms")
            if isinstance(native_ttft, (int, float)) and native_ttft > 0:
                ttft_out = int(native_ttft)
            else:
                ttft_out = None
            return {
                "prompt_rate": float(prompt_rate),
                "predicted_rate": float(predicted_rate),
                "ttft_ms": ttft_out,
                "source": "stats",
            }
    # USAGE_BENCH_FALLBACK 显式关闭时跳过 bench 兜底
    if os.environ.get("USAGE_BENCH_FALLBACK", "true").strip().lower() in {"0", "false", "no", "off"}:
        return {"prompt_rate": None, "predicted_rate": None, "ttft_ms": None, "source": None}
    # stats 无效/速率为 0 → 主动测速
    try:
        adapter = get_adapter(profile.engine)(profile, caps)
    except Exception:  # noqa: BLE001 —— 构造 adapter 失败（如 profile 无 engine）不阻塞测速尝试
        adapter = None
    try:
        result = _benchmark_token_rate(adapter)
    except Exception:  # noqa: BLE001 —— 测速失败不阻塞 status 输出
        result = None
    if result is None:
        return {"prompt_rate": None, "predicted_rate": None, "ttft_ms": None, "source": None}
    prompt_rate, predicted_rate, ttft_ms = result
    return {"prompt_rate": prompt_rate, "predicted_rate": predicted_rate, "ttft_ms": ttft_ms, "source": "bench"}


def _stats_token_rate(profile) -> tuple[float, float] | None:
    """只读 stats 服务的 Token 速率（不主动测速）：stats 不可用/无数据返回 None。

    供 list 的速率列使用：stats 服务自身已有"窗口无流量时主动测速并缓存"的兜底，
    这里直接复用其缓存值即可，避免 list 逐模型触发伪造请求。
    """
    port = int(os.environ.get("USAGE_PORT", "5002"))
    url = f"http://127.0.0.1:{port}/api/usage?model={profile.name}"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("isValid"):
        return None
    prompt_rate = data.get("prompt_rate")
    predicted_rate = data.get("predicted_rate")
    if isinstance(prompt_rate, (int, float)) and isinstance(predicted_rate, (int, float)):
        return float(prompt_rate), float(predicted_rate)
    return None


def _cmd_status(args, models_dir: Path | None, caps) -> int:
    profiles = list_profiles(models_dir)
    if args.name:
        profiles = [p for p in profiles if p.name == args.name]
        if not profiles:
            logger.warning(f"未找到 profile：{args.name}")
            return 0
    rows = []
    for p in profiles:
        state = _instance_state(profile=p)
        health = "-"
        if state in ("运行中", "已外部启动"):
            try:
                adapter = get_adapter(p.engine)(p, caps)
                ok = adapter.wait_ready(3.0)
                health = "正常" if ok else "无响应"
            except Exception:  # noqa: BLE001 —— 健康检查失败不阻塞表格输出
                health = "未知"
        rows.append([p.name, p.engine, p.port, state, health])
    _print_table(["名称", "引擎", "端口", "状态", "健康"], rows, dim_indices=(1, 2))
    if args.name and profiles:
        info = _agent_config_info(profiles[0])
        print(f"\n{_table_paint('智能体配置参考：', 'SECTION')}")
        print(f"  上下文长度：{info['context_length']}")
        print(f"  输入上下文长度：{info['input_context']}")
        print(f"  输出上下文长度：{info['output_context']}")
        print(f"  工具调用轮数：{info['tool_call_rounds']}")
        print(f"  支持图片输入：{info['vision']}")
        print(f"  Temperature：{info['temperature']}")
        print(f"  Top P：{info['top_p']}")
        print(f"  Top K：{info['top_k']}")
        print(f"  Token 计费：{_price_rate_text(profiles[0])}")
        if _instance_state(profile=profiles[0]) in ("运行中", "已外部启动"):
            rate = _token_rate_data(profiles[0], caps)
            if rate["source"] is None:
                rate_text = "输入 -，输出 -（测速失败）"
            else:
                rate_text = f"输入 {rate['prompt_rate']:.1f} tok/s，输出 {rate['predicted_rate']:.1f} tok/s"
                if rate["source"] == "bench":
                    rate_text += "（实测）"
            print(f"  Token 速率：{rate_text}")
            ttft = rate["ttft_ms"]
            print(f"  首 Token 耗时：{ttft} ms" if ttft is not None else "  首 Token 耗时：-")
        else:
            print("  Token 速率：输入 -，输出 -")
            print("  首 Token 耗时：-")
    return 0


def _group_runtime_target(members: list[Profile], states: list[str]) -> tuple[Profile, str] | None:
    """返回组内第一个可用的成员及其状态（"运行中"或"已外部启动"）。

    states 为调用方已探得的各成员状态（避免重复端口探测）。成员已按引擎优先级
    排序（vllm 优先），与网关家族路由 _resolve_group 一致——两者按同一可用口径判定
    （受管运行中，或无 PID 文件但端口健康的外部启动实例）。
    """
    for p, state in zip(members, states, strict=False):
        if state in ("运行中", "已外部启动"):
            return p, state
    return None


def _highlight(text: str) -> str:
    """终端高亮（加粗 + 青色）；非 TTY（重定向/管道/测试）时回退纯文本。"""
    if not sys.stdout.isatty():
        return text
    return f"\x1b[1;36m{text}\x1b[0m"


def _cmd_list(args, models_dir: Path | None, caps) -> int:
    """列出可用模型目录：按家族（group）分组，展示引擎/变体/端口/状态与网关路由映射。"""
    from modelctl.core.gateway import ENGINE_PRIORITY

    profiles = list_profiles(models_dir)
    if not profiles:
        logger.info("models 目录下暂无可用 profile")
        return 0

    grouped: dict[str, list[Profile]] = {}
    for p in profiles:
        grouped.setdefault(p.group or "（未分组）", []).append(p)
    # 组内排序：引擎优先级（与网关家族路由一致，vllm 优先）→ 默认变体优先 → name
    for members in grouped.values():
        members.sort(key=lambda p: (ENGINE_PRIORITY.get(p.engine, 99), p.variant, p.name))

    for idx, group_name in enumerate(sorted(grouped)):
        if idx > 0:
            print()  # 家族块之间空一行，便于阅读
        members = grouped[group_name]
        rows = []
        for p in members:
            state = _instance_state(profile=p)
            rate = "-"
            if state in ("运行中", "已外部启动"):
                r = _stats_token_rate(p)
                # 仅显示非零速率：0/0（空闲且无兜底数据）无信息量，统一显示 -
                if r is not None and (r[0] > 0 or r[1] > 0):
                    rate = f"{r[0]:.1f}/{r[1]:.1f}"
            rows.append([p.engine, p.variant or "-", p.port, state, rate, p.name])
        # 路由映射提示复用上方已探得的状态列，与网关家族路由的可用口径一致；
        # 括号内直接取状态列原值（运行中 / 已外部启动），避免与表格自相矛盾
        target = _group_runtime_target(members, [r[3] for r in rows])
        if target:
            target_profile, target_state = target
            route = f'输入 "{group_name}" 路由至 {target_profile.name}（{target_state}）'
        else:
            route = f'输入 "{group_name}" 当前无运行成员'
        header = f"{group_name}（{len(members)} 配置）"
        print(_table_paint(header, "SECTION") + "｜" + route)
        _print_table(["引擎", "变体", "端口", "状态", "速率(入/出)", "标识符"], rows, dim_indices=(0, 1, 4))

    default_model = os.environ.get("GATEWAY_DEFAULT_MODEL")
    if default_model:
        # 与上方家族块空一行，并以高亮突出默认回退模型提示
        print()
        print(_highlight(f"未匹配任何家族/标识符的请求将回退至默认模型：{default_model}"))
    return 0


def _fmt_mb(mb: int) -> str:
    """MB 显存 → "48.0 GB (49140 MB)"。0 时仅 "0 MB"。"""
    if not mb:
        return "0 MB"
    gb = mb / 1024
    return f"{gb:.1f} GB ({mb} MB)"


def _cmd_probe(args, models_dir: Path | None, caps) -> int:
    """输出硬件/软件能力摘要，分四区块：GPU 硬件、引擎二进制、软件环境、关键环境变量。"""
    free_mb = sum(caps.vram_free_mb)

    def unknown() -> str:
        return _table_paint("未知", "DIM")

    def section(title: str) -> None:
        print(_table_paint(f"== {title} ==", "SECTION"))

    def kv(key: str, value: str) -> None:
        print(f"  {key:<14}  {value}")

    # ── 区域一：GPU 硬件 ──
    section("GPU 硬件")
    kv("GPU 数量", str(caps.gpu_count))
    kv("GPU 型号", caps.gpu_name or unknown())
    kv("单卡显存", _fmt_mb(caps.vram_total_mb) if caps.vram_total_mb else unknown())
    kv("总显存", _fmt_mb(sum(caps.vram_total_mb_per_gpu)) if caps.vram_total_mb_per_gpu else unknown())
    kv("剩余显存", _fmt_mb(free_mb))
    kv("CUDA 驱动", caps.cuda_driver or unknown())
    kv("计算能力", caps.compute_capability or unknown())

    # ── 区域二：引擎二进制 ──
    print()
    section("引擎二进制")
    name_w = max(len(n) for n in ENGINE_BINARIES)
    for name in ENGINE_BINARIES:
        path = caps.binary_paths.get(name)
        if path:
            status = _table_paint("可用", "SUCCESS") + "   "
            suffix = _table_paint(path, "DIM")
        elif name == "llamacpp":
            status = _table_paint("不可用", "ERROR")
            suffix = "(未找到编译产物 llama-server)"
        else:
            status = _table_paint("不可用", "ERROR")
            suffix = f"(缺失 {name} 可执行文件" + (ENGINE_INSTALL_HINTS.get(name, "") or "") + ")"
        print(f"  {name:<{name_w}}  {status}{suffix}")
        # llamacpp 额外两行安装提示，进一步缩进
        if not path and name == "llamacpp":
            pad2 = " " * (2 + name_w + 2 + 3)  # 对齐到 status 之后
            print(f"{pad2}源码:  git clone https://github.com/ggml-org/llama.cpp.git")
            print(f"{pad2}编译:  cmake -B build -DGGML_CUDA=ON && cmake --build build -j 4")
    available_count = sum(1 for n in ENGINE_BINARIES if caps.binaries.get(n))
    total = len(ENGINE_BINARIES)
    print()
    print(_table_paint(f"  共 {total} 项，可用 {available_count} 项，缺失 {total - available_count} 项", "DIM"))

    # ── 区域三：软件环境 ──
    print()
    section("软件环境")
    from modelctl.core.compat import EnvSpec
    env = EnvSpec.from_env()
    kv("site-packages", env.site_packages or unknown())
    kv("已安装包", f"{len(env.packages)} 个")
    kv("nvidia .so", f"{len(env.nvidia_so)} 个")
    if env.libs_resolvable_known:
        if env.cuda_libs_resolvable:
            res_val = _table_paint("是", "SUCCESS")
            res_note = _table_paint(f"（{len(env.cuda_libs_resolvable)} 个 .so 可解析）", "DIM")
        else:
            res_val = _table_paint("否", "ERROR")
            res_note = ""
        kv("CUDA 可解析", res_val + res_note)
    else:
        kv("CUDA 可解析", unknown())

    # ── 区域四：关键环境变量 ──
    print()
    section("关键环境变量")
    for key in ("HF_HOME", "MODEL_ROOT", "MODELSCOPE_CACHE"):
        val = env.env_vars.get(key)
        if val:
            kv(key, _table_paint(val, "INFO"))
        else:
            kv(key, _table_paint("(未设置)", "DIM"))
    return 0


def _cmd_stats_start() -> int:
    r = all_service.start_stats()
    if r.status == "skipped":
        logger.info(r.detail)
    return 0


def _cmd_stats_stop() -> int:
    all_service.stop_stats()
    return 0


def _cmd_stats_restart(args, models_dir: Path | None, caps) -> int:
    r = all_service.restart_stats()
    (logger.error if r.status == "error" else logger.info)(f"用量统计：{r.detail}")
    return 0 if r.status in ("ok", "skipped") else 2


def _cmd_stats_status(args, models_dir: Path | None, caps) -> int:
    r = all_service.status_stats()
    logger.info(f"用量统计：{r.detail}")
    return 0


def _cmd_gateway_start() -> int:
    r = all_service.start_gateway()
    if r.status == "skipped":
        logger.info(r.detail)
    return 0


def _cmd_gateway_stop() -> int:
    all_service.stop_gateway()
    return 0


def _cmd_gateway_restart(args, models_dir: Path | None, caps) -> int:
    r = all_service.restart_gateway()
    (logger.error if r.status == "error" else logger.info)(f"网关：{r.detail}")
    return 0 if r.status in ("ok", "skipped") else 2


def _cmd_gateway_status() -> int:
    logger.info(f"网关：{all_service.status_gateway().detail}")
    return 0


def _cmd_all(args, models_dir: Path | None, caps) -> int:
    if args.action == "start":
        results = all_service.start_all(models_dir, args.model, args.timeout)
        exit_code = 2
    elif args.action == "stop":
        results = all_service.stop_all(models_dir)
        exit_code = 1
    elif args.action == "restart":
        results = all_service.restart_all(models_dir, args.model, args.timeout)
        exit_code = 2
    else:
        results = all_service.status_all(models_dir)
        exit_code = 0
    for r in results:
        line = f"[{r.status}] {r.component}"
        if r.detail:
            line += f"：{r.detail}"
        if r.status == "error":
            logger.error(line)
        else:
            logger.info(line)
    if any(r.status == "error" for r in results):
        logger.info("提示：可执行 `modelctl status` 细查模型状态" "（网关/统计用 `modelctl gateway status` / `modelctl stats status`）")
        return exit_code
    return 0


def _cmd_ui_start(args, models_dir: Path | None, caps) -> int:
    profile = load_profile(args.name, models_dir)
    adapter = get_adapter(profile.engine)(profile, caps)
    spec = adapter.ui_spec(port=args.port, host=args.host)
    if spec is None:
        logger.error(f"{profile.name}：引擎 {profile.engine} 不提供 Web 管理控制台（当前仅 unsloth 支持）")
        return 2
    instance = f"ui-{profile.name}"
    if is_running(instance):
        logger.info(f"Web 控制台已在运行（{instance}, " f"http://{spec['host']}:{spec['port']}）；重启请先 `modelctl ui stop {args.name}`")
        return 0
    # ufw 入站白名单：只放行指定来源 IP 直连 UI 端口，避免控制台裸奔
    allow_from = args.allow_from or spec["allow_from"]
    for src in allow_from:
        if not ensure_ufw_allow(src, spec["port"]):
            logger.warning(f"添加 ufw 规则失败（{src} → :{spec['port']}），请手动执行：" f"ufw allow from {src} to any port {spec['port']} proto tcp")
    if not allow_from:
        logger.warning(f"未配置 --allow-from / yaml allow_from，端口 {spec['port']} 在局域网无访问限制，注意安全")
    pid, _ = start_detached(instance, spec["cmd"], spec["env"])
    log = launch_log(instance)
    logger.info(f"Web 控制台已启动（{instance}, PID {pid}），监听 http://{spec['host']}:{spec['port']}")
    if allow_from:
        logger.info("允许的来源 IP（ufw 白名单）：" + ", ".join(allow_from))
    if log is not None:
        logger.info(f"日志：{log}")
    return 0


def _cmd_ui_stop(args, models_dir: Path | None, caps) -> int:
    profile = load_profile(args.name, models_dir)
    adapter = get_adapter(profile.engine)(profile, caps)
    instance = f"ui-{profile.name}"
    if not is_running(instance) and not pid_file(instance).is_file():
        logger.info(f"Web 控制台未在运行（{instance}）")
        return 0
    # 仅按 PID/端口终止；不按进程名 pkill，避免误杀 `unsloth studio run` 推理实例
    stop_instance(instance, (adapter.ui_spec() or {}).get("port", 0), [])
    logger.info(f"已停止 Web 控制台（{instance}）")
    return 0


def _cmd_nginx_snippet(args, models_dir) -> int:
    gateway_port = int(os.environ.get("GATEWAY_PORT", "5003"))
    print(build_llm_map(list_profiles(models_dir), args.node, args.host, gateway_port), end="")
    return 0


def _validate_target(target: str | None) -> bool:
    """校验 target 是否属于受管目标（托管引擎 + 独立 gateway 子项目）。"""
    return target is not None and target in ENV_TARGETS


def _cmd_env_setup(args, models_dir: Path | None, caps) -> int:
    if not _validate_target(args.engine):
        logger.error(
            f"请指定受管目标（{' / '.join(ENV_TARGETS)}）：modelctl env setup <target>"
        )
        return 2
    try:
        code = envs_setup(args.engine)
    except EngineEnvError as exc:
        logger.error(str(exc))
        return 2
    if code != 0:
        logger.error(f"env setup {args.engine} 失败（退出码 {code}），请检查 uv 输出后重试")
        return code
    print(_table_paint(f"{args.engine} 环境安装完成", "SUCCESS"))
    return 0


def _cmd_env_list(args, models_dir: Path | None, caps) -> int:
    states = envs_status()
    print(_table_paint("受管虚拟环境（.venvs/）：", "SECTION"))
    for target in ENV_TARGETS:
        st = states.get(target, {"exists": False})
        if st["exists"]:
            detail = f"python {st.get('python', '?')}"
            if st.get("packages"):
                pkgs = st["packages"]
                head = list(pkgs.items())[:6]
                detail += "；" + "、".join(f"{k} {v}" for k, v in head)
                if len(pkgs) > 6:
                    detail += f" …（共 {len(pkgs)} 个包）"
            print(f"  {target}: {_table_paint('已创建', 'SUCCESS')}（{detail}）")
        else:
            print(f"  {target}: {_table_paint('未创建', 'DIM')}（执行 modelctl env setup {target}）")
    print(_table_paint("ollama / llamacpp / unsloth：", "DIM") + _table_paint("原生或官方安装器，无需托管", "DIM"))
    return 0


def _cmd_env_remove(args, models_dir: Path | None, caps) -> int:
    if not _validate_target(args.engine):
        logger.error(
            f"请指定受管目标（{' / '.join(ENV_TARGETS)}）：modelctl env remove <target>"
        )
        return 2
    try:
        envs_remove(args.engine)
    except ValueError as exc:
        logger.error(str(exc))
        return 2
    print(f"{args.engine} 环境已移除")
    return 0


# ----------------------------------------------------------------------
# audit 子命令族（请求级审计日志：查询/统计/清理）
# 与网关/硬件探测解耦：audit handler 不使用 caps / models_dir / probe 结果。
# ----------------------------------------------------------------------


def _audit_dir_from_env() -> Path:
    """从 env 读 AUDIT_DIR，缺省 data/audit。"""
    return Path(os.environ.get("AUDIT_DIR", "data/audit"))


def _read_audit_entries(
    audit_dir: Path,
    limit: int,
    *,
    since: _dt_dt | None = None,
    model: str | None = None,
    endpoints: frozenset[str] | None = None,
) -> list[dict]:
    """读 JSONL（按天从新到旧、文件内行倒序），应用过滤，返回最多 limit 条。

    JSON 解析失败的行静默跳过；since 越过时立即停止读取更早文件（时间窗短路）。
    """
    if not audit_dir.is_dir():
        return []
    all_files = sorted(
        (
            p
            for p in audit_dir.iterdir()
            if p.is_file()
            and p.name.startswith("modelctl-")
            and p.name.endswith(".jsonl")
            and not p.name.startswith("modelctl-deleting")
        ),
        key=lambda p: p.name,
        reverse=True,  # 天倒序：新 → 旧
    )
    out: list[dict] = []
    for f in all_files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):  # 文件内倒序：新 → 旧
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw = rec.get("ts") or ""
            try:
                rec_ts = _dt_dt.fromisoformat(ts_raw)
            except ValueError:
                rec_ts = None
            if since is not None and rec_ts is not None and rec_ts < since:
                return out  # 时间窗已过，停止读更早文件
            if model is not None and rec.get("model") != model:
                continue
            if endpoints is not None and rec.get("path") not in endpoints:
                continue
            out.append(rec)
            if len(out) >= limit:
                return out
    return out


def _parse_since_arg(s: str) -> _dt_dt:
    """解析 --since：相对（1h / 24h / 7d）或 ISO 8601 绝对时间。"""
    m = _re.match(r"^(\d+)([hd])$", s)
    if m:
        n = int(m.group(1))
        delta = timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n)
        return _dt_dt.now().astimezone() - delta
    try:
        return _dt_dt.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"无法解析 --since: {s!r}（可用：1h / 24h / 7d / ISO）") from None


def _format_audit_table(records: list[dict]) -> list[str]:
    """审计记录表格化输出（固定列 + ljust 对齐，不依赖终端宽度）。"""
    if not records:
        return []
    headers = ["ts", "model", "endpoint", "stream", "src", "tokens (in/out)", "ttft_ms", "tps", "status"]

    def _field(rec: dict, key: str) -> str:
        val = (rec.get("gateway_metrics") or {}).get(key)
        return str(val) if val is not None else "-"

    rows = [
        [
            (r.get("ts") or "")[:19],
            (r.get("model") or "")[:18],
            (r.get("path") or "")[:16],
            str(bool(r.get("stream", False))).lower(),
            (r.get("source") or "")[:12],
            f'{r.get("prompt_tokens", 0)}/{r.get("completion_tokens", 0)}',
            _field(r, "ttft_ms"),
            _field(r, "tokens_per_second"),
            str(r.get("status_code", "-")),
        ]
        for r in records
    ]
    widths = [
        max(len(headers[i]), max((len(rows[j][i]) for j in range(len(rows))), default=0))
        for i in range(len(headers))
    ]
    out: list[str] = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    for r in rows:
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
    return out


def _cmd_audit_query(args) -> int:
    """查询审计记录（默认表格 / --json 输出 JSONL）。"""
    audit_dir = _audit_dir_from_env()
    filters_model = getattr(args, "model", None)
    endpoints_raw = getattr(args, "endpoints", None)
    endpoints = frozenset(e.strip() for e in endpoints_raw.split(",") if e.strip()) if endpoints_raw else None
    since_str = getattr(args, "since_str", None)
    since = _parse_since_arg(since_str) if since_str else None
    limit = int(getattr(args, "limit", 0) or 20)
    records = _read_audit_entries(audit_dir, limit, since=since, model=filters_model, endpoints=endpoints)
    if not records:
        print(_table_paint("no audit records / 暂无审计记录", "DIM"))
        return 0
    if getattr(args, "json", False):
        for r in records:
            print(json.dumps(r, ensure_ascii=False))
        return 0
    for line in _format_audit_table(records):
        print(line)
    return 0


def _cmd_audit_path() -> int:
    """打印 AUDIT_DIR 绝对路径。"""
    print(_audit_dir_from_env().resolve())
    return 0


def _cmd_audit_stats() -> int:
    """输出审计目录统计（stats_summary）。"""
    from modelctl.core.audit import _new_audit_log

    audit_log = _new_audit_log(_audit_dir_from_env())
    s = audit_log.stats_summary()
    print(f"file_count: {s['file_count']}")
    print(f"total_bytes: {s['total_bytes']}")
    print(f"oldest_day: {s['oldest_day']}")
    print(f"newest_day: {s['newest_day']}")
    if s["by_day"]:
        print("by_day:")
        for day, sz in sorted(s["by_day"].items()):
            print(f"  {day}: {sz} bytes")
    return 0


def _cmd_audit_cleanup(args) -> int:
    """清理过期审计文件：--dry-run 仅打印预览；否则 staged rename + unlink 删除。"""
    from modelctl.core.audit import _new_audit_log

    audit_dir = _audit_dir_from_env()
    audit_log = _new_audit_log(audit_dir)
    dead = audit_log.collect_dead_files()
    total_freed = sum(p.stat().st_size if p.exists() else 0 for p in dead)
    freed_mb = total_freed / (1024 * 1024)
    if getattr(args, "dry_run", False):
        names = ", ".join(p.name for p in dead[:10]) + ("..." if len(dead) > 10 else "")
        print(f"Would delete {len(dead)} files ({freed_mb:.1f} MB): {names}")
        return 0
    deleted = 0
    for p in dead:
        if not p.exists():
            continue
        # 先改名到 staging 再 unlink，避免读取方在删除中途打开已被清空的文件
        staged = audit_dir / f".audit-deleting-{int(time.time() * 1000)}-{p.name}"
        try:
            p.rename(staged)
            staged.unlink()
            deleted += 1
        except OSError as exc:
            logger.warning(f"删除 {p.name} 失败: {exc}")
    print(f"Deleted {deleted} files, freed {freed_mb:.1f} MB")
    return 0


# ---- §2.2 TensorRT-LLM 编译子命令 ----

def _cmd_trtllm_build(args, models_dir: Path | None, caps) -> int:
    """§2.2：执行 `trtllm-build` 编译 HuggingFace 模型到 engine_dir。

    流程：加载 profile → 校验 venv 可用 → 编译命令（build_compile_command）→ subprocess run
    （同步阻塞，由 modelctl start 等待健康）。编译成功后 engine_dir 含 .trt 等产物，
    即可 `modelctl start <name>` 启动服务。
    """
    import subprocess
    from loguru import logger as _logger

    profile = load_profile(args.name, models_dir)
    if profile.engine != "tensorrt_llm":
        _logger.error(f"profile {profile.name} engine 必须是 tensorrt_llm（实际 {profile.engine!r}）")
        return 2
    adapter = get_adapter(profile.engine)(profile, caps)
    # 复用通用需求检查（会清理 docker 残留、acquire GPU 锁）
    try:
        adapter.check_requirements()
    except RequirementError as exc:
        _logger.error(str(exc))
        return 2
    for w in adapter.warnings:
        _logger.warning(w)
    # 静态校验
    adapter.ensure_bin()
    cmd, env = adapter.build_compile_command()
    _logger.info(f"[trtllm build] 编译 {args.name}: {' '.join(cmd)}")
    _logger.info(f"[trtllm build] 同步执行（首次冷编译约 28 分钟，含 --dump_intermediates 时更久）")
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        _logger.error(f"[trtllm build] 编译失败 (exit={result.returncode})：")
        _logger.error(f"stdout: {result.stdout[-3000:] if result.stdout else '(empty)'}")
        _logger.error(f"stderr: {result.stderr[-3000:] if result.stderr else '(empty)'}")
        return 1
    _logger.info(f"[trtllm build] 编译完成：{profile.engine_config.get('engine_dir')!r}")
    _logger.info(f"现在可执行 `modelctl start {profile.name}` 启动服务")
    return 0


def _cmd_trtllm_status(args, models_dir: Path | None, caps) -> int:
    """查询 profile 的编译产物状态（engine_dir 是否存在且非空）。"""
    profile = load_profile(args.name, models_dir)
    if profile.engine != "tensorrt_llm":
        print(f"{profile.name}: engine 是 {profile.engine!r}（非 tensorrt_llm），跳过")
        return 1
    engine_dir = __import__("pathlib").Path(str(profile.engine_config.get("engine_dir") or "")).expanduser()
    if not engine_dir.exists():
        print(f"{profile.name}: engine_dir {engine_dir} 不存在 → 未编译")
        return 1
    files = [p.name for p in engine_dir.iterdir()]
    size_kb = sum(p.stat().st_size for p in engine_dir.iterdir() if p.is_file()) // 1024
    state = "已编译" if files else "空目录（未编译）"
    print(f"{profile.name}: engine_dir={engine_dir} {state} ({len(files)} 个文件，{size_kb} KB)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 先扫描 --no-color（在全局 argparse 之前），以便 setup_logging 同步禁用颜色
    if "--no-color" in argv:
        os.environ["MODELCTL_NO_COLOR"] = "1"
    setup_logging()
    models_dir, rest = _extract_models_dir(argv)
    load_env()
    # 命令路由前先做一次"CLI 自身"缺失依赖检测（loguru/yaml）。
    # 缺失时自动经 uv/pip 多源回退补齐；补不齐则直接退出 2（后续命令也没法跑）。
    # 启动类命令（start/restart/all+start）进入对应 handler 时，handler 会
    # 按服务（gateway/stats/llama-cpp 等）再 ensure 一次自己的依赖。
    if not ensure_packages("core"):
        logger.error("CLI 核心依赖（PyYAML/loguru）缺失且自动安装失败，请手动 `uv sync` 后重试")
        return 2
    parser = build_parser()
    args = parser.parse_args(rest)
    models_dir = models_dir or args.models_dir
    if getattr(args, "gpus", None):
        os.environ["MODELCTL_GPUS"] = args.gpus
    caps = probe()
    try:
        if args.command == "start":
            return _cmd_start(args, models_dir, caps)
        if args.command == "stop":
            return _cmd_stop(args, models_dir, caps)
        if args.command == "restart":
            return _cmd_restart(args, models_dir, caps)
        if args.command == "status":
            return _cmd_status(args, models_dir, caps)
        if args.command == "list":
            return _cmd_list(args, models_dir, caps)
        if args.command == "probe":
            return _cmd_probe(args, models_dir, caps)
        if args.command == "stats":
            if args.action == "start":
                return _cmd_stats_start()
            if args.action == "stop":
                return _cmd_stats_stop()
            if args.action == "restart":
                return _cmd_stats_restart(args, models_dir, caps)
            return _cmd_stats_status(args, models_dir, caps)
        if args.command == "audit":
            if getattr(args, "cleanup", False):
                # 与 query/path/stats 三态互斥：--cleanup 必须单独使用
                if args.sub is not None:
                    parser.error("audit: --cleanup 与 sub(path/stats) 互斥")
                return _cmd_audit_cleanup(args)
            if getattr(args, "sub", None) == "path":
                return _cmd_audit_path()
            if getattr(args, "sub", None) == "stats":
                return _cmd_audit_stats()
            # 默认 query（sub=None）
            return _cmd_audit_query(args)
        if args.command == "gateway":
            if args.action == "start":
                return _cmd_gateway_start()
            if args.action == "stop":
                return _cmd_gateway_stop()
            if args.action == "restart":
                return _cmd_gateway_restart(args, models_dir, caps)
            return _cmd_gateway_status()
        if args.command == "all":
            return _cmd_all(args, models_dir, caps)
        if args.command == "ui":
            if args.action == "start":
                return _cmd_ui_start(args, models_dir, caps)
            return _cmd_ui_stop(args, models_dir, caps)
        if args.command == "nginx-snippet":
            return _cmd_nginx_snippet(args, models_dir)
        if args.command == "env":
            if args.action == "setup":
                return _cmd_env_setup(args, models_dir, caps)
            if args.action == "list":
                return _cmd_env_list(args, models_dir, caps)
            return _cmd_env_remove(args, models_dir, caps)
        if args.command == "trtllm":
            if args.action == "build":
                return _cmd_trtllm_build(args, models_dir, caps)
            return _cmd_trtllm_status(args, models_dir, caps)
    except (ProfileError, RequirementError, EngineEnvError) as error:
        logger.error(str(error))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
