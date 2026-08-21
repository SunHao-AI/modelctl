#!/usr/bin/env python3
"""modelctl.py — 多模型部署启动器 CLI 入口。

子命令：start <name> [--timeout 300] / stop <name> / restart <name> /
        status [name] / list / probe / stats start|stop / ui start|stop <name>

流程约定：
- 所有子命令先 load_env()（注入 .env），再 probe() 探测硬件能力。
- start：load_profile → get_adapter → check_requirements（打印 warnings）→
  pre_start → build_command → start_detached → wait_health（超时打印日志尾部
  50 行并返回 1）→ post_start → 打印访问地址与日志路径。
- stop 对 ollama 引擎特判：serve 由本工具拉起（PID 文件存在）且无其他
  ollama profile 在运行时才停掉 serve；否则仅 unload_model + 删除 PID 记录。
- 错误处理：ProfileError / RequirementError 捕获后打印消息并返回 2。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from loguru import logger

import modelctl.core.compat_rules  # noqa: F401 —— 导入即注册内置规则
from modelctl.core.capabilities import ENGINE_BINARIES, ENGINE_INSTALL_HINTS, probe
from modelctl.core.envfile import load_env
from modelctl.core.gateway import GATEWAY_PORT
from modelctl.core.logging import setup_logging
from modelctl.core.nginx_snippet import build_llm_map
from modelctl.core.process import (
    is_running,
    launch_log,
    pid_file,
    start_detached,
    stop_instance,
    tail_file,
    wait_health,
)
from modelctl.core.profile import ProfileError, list_profiles, load_profile
from modelctl.core.stats import USAGE_PORT
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
            p.add_argument("--timeout", type=float, default=300, help="健康检查超时秒数（默认 300）")
    sub.add_parser("list", help="列出所有 profile")
    sub.add_parser("probe", help="探测硬件与引擎二进制")
    sp = sub.add_parser("stats", help="用量统计服务控制")
    sp.add_argument("action", choices=["start", "stop"])
    gp = sub.add_parser("gateway", help="统一网关（model 参数路由）控制")
    gp.add_argument("action", choices=["start", "stop", "status"])
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


def _stop_profile(profile, caps, models_dir: Path | None) -> None:
    """按引擎语义停止单个 profile 实例。"""
    adapter = get_adapter(profile.engine)(profile, caps)
    if profile.engine == "ollama":
        # 特判：serve 为共享常驻服务，仅当由本工具拉起且无其他 ollama
        # profile 在运行时才停 serve；否则只卸载模型并清理 PID 记录。
        pf = pid_file(profile.name)
        other_ollama_running = any(
            is_running(o.name) for o in list_profiles(models_dir) if o.engine == "ollama" and o.name != profile.name
        )
        if pf.is_file() and not other_ollama_running:
            stop_instance(profile.name, profile.port, [])
        else:
            adapter.unload_model()
            pid_file(profile.name).unlink(missing_ok=True)
    else:
        stop_instance(profile.name, profile.port, adapter.stop_patterns())


def _cmd_start(args, models_dir: Path | None, caps) -> int:
    name = args.name
    profile = load_profile(name, models_dir)
    adapter = get_adapter(profile.engine)(profile, caps)
    adapter.check_requirements()
    for warning in adapter.warnings:
        logger.warning(warning)
    adapter.pre_start()
    cmd, env = adapter.build_command()
    pid = start_detached(name, cmd, env)
    logger.info(f"已启动 {name}（PID {pid}），等待健康检查（超时 {args.timeout:g}s）...")
    if adapter.wait_ready(args.timeout):
        upstream_key = adapter.upstream_api_key()
        if upstream_key and upstream_key != profile.api_key:
            # unsloth 等自管认证引擎的运行时 key，客户端需以此调用后端
            logger.info(f"上游 API Key（本次启动自动生成）：{upstream_key}")
        adapter.post_start()
        log = launch_log(name)
        logger.info(f"启动成功：{name} 运行于 http://127.0.0.1:{profile.port}")
        if log is not None:
            logger.info(f"日志：{log}")
        if profile.usage or adapter.metrics_mapping() is not None:
            logger.info("提示：用量统计可通过 `modelctl stats start` 启动")
        return 0
    log = launch_log(name)
    if log is not None:
        logger.warning(f"健康检查超时，日志尾部 50 行（{log}）：")
        logger.warning(tail_file(log, 50))
    else:
        logger.warning("健康检查超时，且未找到启动日志")
    return 1


def _cmd_stop(args, models_dir: Path | None, caps) -> int:
    profile = load_profile(args.name, models_dir)
    _stop_profile(profile, caps, models_dir)
    logger.info(f"已停止：{profile.name}")
    return 0


def _cmd_restart(args, models_dir: Path | None, caps) -> int:
    profile = load_profile(args.name, models_dir)
    _stop_profile(profile, caps, models_dir)
    logger.info(f"已停止：{profile.name}，正在重新启动...")
    return _cmd_start(args, models_dir, caps)


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
    return 0


def _cmd_list(args, models_dir: Path | None, caps) -> int:
    profiles = list_profiles(models_dir)
    rows = [[p.name, p.engine, p.port] for p in profiles]
    _print_table(["名称", "引擎", "端口"], rows)
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
    if is_running("usage-stats"):
        logger.info("用量统计服务已在运行")
        return 0
    # 后台独立进程：python -m modelctl.core.stats。统计目标由
    # modelctl.core.stats 的 _targets_from_profiles() 从 models/*.yaml 加载全部
    # profile 构造——未运行的模型会返回不可用状态（isValid=False），而非在此过滤。
    # 这是计划"独立进程入口"的合理实现，故此处不预构造 targets。
    script_dir = str(Path(__file__).resolve().parents[1])
    extra_env = {"PYTHONPATH": script_dir + os.pathsep + os.environ.get("PYTHONPATH", "")}
    pid = start_detached("usage-stats", [sys.executable, "-m", "modelctl.core.stats"], extra_env)
    port = int(os.environ.get("USAGE_PORT", str(USAGE_PORT)))
    logger.info(f"用量统计服务已启动（PID {pid}），监听端口 {port}")
    return 0


def _cmd_stats_stop() -> int:
    port = int(os.environ.get("USAGE_PORT", str(USAGE_PORT)))
    stop_instance("usage-stats", port, ["modelctl.core.stats"])
    logger.info("用量统计服务已停止")
    return 0


def _cmd_gateway_start() -> int:
    if is_running("llm-gateway"):
        logger.info("网关已在运行")
        return 0
    script_dir = str(Path(__file__).resolve().parents[1])
    extra_env = {"PYTHONPATH": script_dir + os.pathsep + os.environ.get("PYTHONPATH", "")}
    pid = start_detached("llm-gateway", [sys.executable, "-m", "modelctl.core.gateway"], extra_env)
    port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
    logger.info(f"网关已启动（PID {pid}），监听端口 {port}")
    return 0


def _cmd_gateway_stop() -> int:
    port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
    stop_instance("llm-gateway", port, ["modelctl.core.gateway"])
    logger.info("网关已停止")
    return 0


def _cmd_gateway_status() -> int:
    if is_running("llm-gateway"):
        port = int(os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT)))
        ok = wait_health(f"http://127.0.0.1:{port}/v1/models", 3.0)
        print("网关：运行中，/v1/models " + ("正常" if ok else "无响应"))
        return 0
    print("网关：已停止")
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
        logger.info(
            f"Web 控制台已在运行（{instance}, "
            f"http://{spec['host']}:{spec['port']}）；重启请先 `modelctl ui stop {args.name}`"
        )
        return 0
    # ufw 入站白名单：只放行指定来源 IP 直连 UI 端口，避免控制台裸奔
    allow_from = args.allow_from or spec["allow_from"]
    for src in allow_from:
        if not ensure_ufw_allow(src, spec["port"]):
            logger.warning(
                f"添加 ufw 规则失败（{src} → :{spec['port']}），请手动执行："
                f"ufw allow from {src} to any port {spec['port']} proto tcp"
            )
    if not allow_from:
        logger.warning(f"未配置 --allow-from / yaml allow_from，端口 {spec['port']} 在局域网无访问限制，注意安全")
    pid = start_detached(instance, spec["cmd"], spec["env"])
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
    print(build_llm_map(list_profiles(models_dir), args.node, args.host), end="")
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
            return _cmd_stats_start() if args.action == "start" else _cmd_stats_stop()
        if args.command == "gateway":
            if args.action == "start":
                return _cmd_gateway_start()
            if args.action == "stop":
                return _cmd_gateway_stop()
            return _cmd_gateway_status()
        if args.command == "ui":
            if args.action == "start":
                return _cmd_ui_start(args, models_dir, caps)
            return _cmd_ui_stop(args, models_dir, caps)
        if args.command == "nginx-snippet":
            return _cmd_nginx_snippet(args, models_dir)
    except (ProfileError, RequirementError) as error:
        logger.error(str(error))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
