#!/usr/bin/env python3
"""core/ufw.py — ufw 入站放行规则的幂等管理（UI 控制台来源 IP 白名单）。"""

from __future__ import annotations

import shutil
import subprocess


def ensure_ufw_allow(source_ip: str, port: int) -> bool:
    """确保存在允许 source_ip 访问本机 TCP 端口的 ufw 规则；ufw 对相同规则自动去重。

    ufw 未安装或执行失败时返回 False，由调用方告警并提示手动配置。
    """
    if shutil.which("ufw") is None:
        return False
    result = subprocess.run(
        ["ufw", "allow", "from", source_ip, "to", "any", "port", str(port), "proto", "tcp"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0
