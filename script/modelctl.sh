#!/usr/bin/env bash
# modelctl.sh — 通过 uv 调用 modelctl 命令（自动适配 Windows / Linux）
set -euo pipefail

# 优先使用原生（Linux/WSL）的 uv；仅当不存在时才回退到 Windows 的 uv.exe
# 注意：WSL 下 command -v uv.exe 也能命中 Windows 的 uv.exe，但无法直接执行，
# 因此必须先检测原生 uv，避免误选 uv.exe 导致 Exec format error。
if command -v uv >/dev/null 2>&1; then
    UV=uv
else
    UV=uv.exe
fi

# 默认健康检查超时：vLLM 首次冷启动（torch.compile + warmup + CUDA graph 捕获）实测约 6 分钟。
# 仅对 start/restart/all 生效，且用户显式传 --timeout 时以用户值为准。
DEFAULT_TIMEOUT=600
args=("$@")
case "${1:-}" in
start|restart|all)
    has_timeout=false
    for a in "$@"; do
        if [[ "$a" == --timeout* ]]; then has_timeout=true; break; fi
    done
    if ! $has_timeout; then
        args+=(--timeout "$DEFAULT_TIMEOUT")
    fi
    ;;
esac

"$UV" run modelctl "${args[@]}"
