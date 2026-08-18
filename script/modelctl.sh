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

"$UV" run modelctl "$@"
