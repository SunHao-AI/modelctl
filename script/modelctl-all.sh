#!/usr/bin/env bash
# modelctl-all.sh — 一键启停（默认模型 + 网关 + 统计），通过 uv 调用 modelctl all
set -euo pipefail

# 优先使用原生（Linux/WSL）的 uv；仅当不存在时才回退到 Windows 的 uv.exe
if command -v uv >/dev/null 2>&1; then
    UV=uv
else
    UV=uv.exe
fi

"$UV" run modelctl all "$@"
