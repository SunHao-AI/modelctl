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
import sys
import time
import urllib.request
from pathlib import Path

from loguru import logger

import modelctl.core.compat_rules  # noqa: F401 —— 导入即注册内置规则
from modelctl.core import all_service
from modelctl.core.capabilities import ENGINE_BINARIES, ENGINE_INSTALL_HINTS, probe
from modelctl.core.envfile import load_env
from modelctl.core.envs import (
    MANAGED_ENGINES,
    EngineEnvError,
    remove as envs_remove,
    setup as envs_setup,
    status as envs_status,
)
from modelctl.core.logging import setup_logging
from modelctl.core.nginx_snippet import build_llm_map
from modelctl.core.process import (
    is_running,
    launch_log,
    pid_file,
    start_detached,
    stop_instance,
)
from modelctl.core.profile import Profile, ProfileError, list_profiles, load_profile
from modelctl.core.ufw import ensure_ufw_allow
from modelctl.engines import get_adapter
from modelctl.engines.base import RequirementError


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
    ep = sub.add_parser("env", help="引擎专用虚拟环境管理（vllm / sglang）")
    ep.add_argument("action", choices=["setup", "list", "remove"])
    ep.add_argument("engine", nargs="?", default=None, help="引擎：vllm 或 sglang（list 不需要）")
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


def _print_table(headers: list[str], rows: list[list]) -> None:
    """按动态列宽打印类 Excel 对齐表格（表头 + 分隔线 + 数据行）。"""
    if not rows:
        print("  ".join(headers))
        return
    col_count = len(headers)
    widths = [_display_width(h) for h in headers]
    for row in rows:
        for i in range(col_count):
            widths[i] = max(widths[i], _display_width(str(row[i])))
    print("  ".join(_ljust_width(headers[i], widths[i]) for i in range(col_count)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(_ljust_width(str(row[i]), widths[i]) for i in range(col_count)))


def _instance_state(name: str) -> str:
    """依据 PID 文件与进程存活判断实例状态。"""
    pf = pid_file(name)
    if not pf.is_file():
        return "已停止"
    if is_running(name):
        return "运行中"
    return "PID 异常"


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
            return {
                "prompt_rate": float(prompt_rate),
                "predicted_rate": float(predicted_rate),
                "ttft_ms": None,
                "source": "stats",
            }
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
        state = _instance_state(p.name)
        health = "-"
        if state == "运行中":
            try:
                adapter = get_adapter(p.engine)(p, caps)
                ok = adapter.wait_ready(3.0)
                health = "正常" if ok else "无响应"
            except Exception:  # noqa: BLE001 —— 健康检查失败不阻塞表格输出
                health = "未知"
        rows.append([p.name, p.engine, p.port, state, health])
    _print_table(["名称", "引擎", "端口", "状态", "健康"], rows)
    if args.name and profiles:
        info = _agent_config_info(profiles[0])
        print("\n智能体配置参考：")
        print(f"  上下文长度：{info['context_length']}")
        print(f"  输入上下文长度：{info['input_context']}")
        print(f"  输出上下文长度：{info['output_context']}")
        print(f"  工具调用轮数：{info['tool_call_rounds']}")
        print(f"  支持图片输入：{info['vision']}")
        print(f"  Temperature：{info['temperature']}")
        print(f"  Top P：{info['top_p']}")
        print(f"  Top K：{info['top_k']}")
        print(f"  Token 计费：{_price_rate_text(profiles[0])}")
        if _instance_state(profiles[0].name) == "运行中":
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


def _group_runtime_target(members: list[Profile]) -> Profile | None:
    """返回组内第一个运行中的 profile。

    成员已按引擎优先级排序（vllm 优先），与网关家族路由 _resolve_group 一致
    （网关额外做健康检查，此处仅以运行状态作答，与状态列口径统一）。
    """
    for p in members:
        if is_running(p.name):
            return p
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
        target = _group_runtime_target(members)
        if target:
            route = f'输入 "{group_name}" 路由至 {target.name}（运行中）'
        else:
            route = f'输入 "{group_name}" 当前无运行成员'
        print(f"{group_name}（{len(members)} 配置）｜{route}")
        rows = []
        for p in members:
            state = _instance_state(p.name)
            rate = "-"
            if state == "运行中":
                r = _stats_token_rate(p)
                # 仅显示非零速率：0/0（空闲且无兜底数据）无信息量，统一显示 -
                if r is not None and (r[0] > 0 or r[1] > 0):
                    rate = f"{r[0]:.1f}/{r[1]:.1f}"
            rows.append([p.engine, p.variant or "-", p.port, state, rate, p.name])
        _print_table(["引擎", "变体", "端口", "状态", "速率(入/出)", "标识符"], rows)

    default_model = os.environ.get("GATEWAY_DEFAULT_MODEL")
    if default_model:
        # 与上方家族块空一行，并以高亮突出默认回退模型提示
        print()
        print(_highlight(f"未匹配任何家族/标识符的请求将回退至默认模型：{default_model}"))
    return 0


def _cmd_probe(args, models_dir: Path | None, caps) -> int:
    free_mb = sum(caps.vram_free_mb)
    print(f"GPU 数量：{caps.gpu_count}")
    print(f"GPU 型号：{caps.gpu_name or '未知'}")
    print(f"单卡显存：{caps.vram_total_mb} MB" if caps.vram_total_mb else "单卡显存：未知")
    print(f"剩余显存总量：{free_mb} MB")
    print(f"CUDA 驱动：{caps.cuda_driver or '未知'}")
    print(f"计算能力（CC）：{caps.compute_capability or '未知'}")
    print("引擎二进制可用性：")
    for name in ENGINE_BINARIES:
        path = caps.binary_paths.get(name)
        if path:
            print(f"  {name}: 可用（{path}）")
        elif name == "llamacpp":
            print(
                "  llamacpp: 不可用（未找到编译产物 llama-server）\n"
                "    源码下载：git clone https://github.com/ggml-org/llama.cpp.git\n"
                "    编译（保守并行度，避免 OOM）：cmake -B build -DGGML_CUDA=ON && cmake --build build -j 4"
            )
        else:
            hint = ENGINE_INSTALL_HINTS.get(name, "")
            print(f"  {name}: 不可用（未在 PATH 中找到 {name} 可执行文件{hint}）")
    # 软件能力摘要（EnvSpec：静态元数据 + 文件检查，不导入引擎）
    from modelctl.core.compat import EnvSpec

    env = EnvSpec.from_env()
    print(f"site-packages：{env.site_packages or '未知'}")
    print(f"已安装包：{len(env.packages)} 个")
    print(f"nvidia .so 文件：{len(env.nvidia_so)} 个")
    resolvable_note = ""
    if env.libs_resolvable_known and env.cuda_libs_resolvable:
        resolvable_note = "（" + ", ".join(sorted(env.cuda_libs_resolvable))[:120] + "）"
    print(f"CUDA 库可解析：{'是' if env.libs_resolvable_known else '未知'}{resolvable_note}")
    print("关键环境变量：")
    for key in ("HF_HOME", "MODEL_ROOT", "MODELSCOPE_CACHE"):
        print(f"  {key}={env.env_vars.get(key) or '（未设置）'}")
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


def _validate_engine(engine: str | None) -> bool:
    """校验 engine 是否为托管引擎；返回是否通过。"""
    if engine is None or engine not in MANAGED_ENGINES:
        return False
    return True


def _cmd_env_setup(args, models_dir: Path | None, caps) -> int:
    if not _validate_engine(args.engine):
        logger.error(
            f"请指定托管引擎（{' / '.join(MANAGED_ENGINES)}）：modelctl env setup <engine>"
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
    print(f"{args.engine} 环境安装完成")
    return 0


def _cmd_env_list(args, models_dir: Path | None, caps) -> int:
    states = envs_status()
    print("托管引擎环境：")
    for engine in MANAGED_ENGINES:
        st = states.get(engine, {"exists": False})
        if st["exists"]:
            detail = f"python {st.get('python', '?')}"
            if st.get("packages"):
                detail += "；" + "、".join(f"{k} {v}" for k, v in st["packages"].items())
            print(f"  {engine}: 已创建（{detail}）")
        else:
            print(f"  {engine}: 未创建（执行 modelctl env setup {engine}）")
    print("ollama / llamacpp / unsloth：原生或官方安装器，无需托管")
    return 0


def _cmd_env_remove(args, models_dir: Path | None, caps) -> int:
    if not _validate_engine(args.engine):
        logger.error(
            f"请指定托管引擎（{' / '.join(MANAGED_ENGINES)}）：modelctl env remove <engine>"
        )
        return 2
    try:
        envs_remove(args.engine)
    except ValueError as exc:
        logger.error(str(exc))
        return 2
    print(f"{args.engine} 环境已移除")
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    argv = list(sys.argv[1:] if argv is None else argv)
    models_dir, rest = _extract_models_dir(argv)
    load_env()
    caps = probe()
    parser = build_parser()
    args = parser.parse_args(rest)
    models_dir = models_dir or args.models_dir
    if getattr(args, "gpus", None):
        os.environ["MODELCTL_GPUS"] = args.gpus
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
    except (ProfileError, RequirementError) as error:
        logger.error(str(error))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
