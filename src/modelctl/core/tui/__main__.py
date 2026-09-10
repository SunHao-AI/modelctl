#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/tui/__main__.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/7 10:00
# @Desc   : python -m modelctl.core.tui 独立入口（复用 cli 的 tui 子命令 handler）
# ===============================================================================

"""`python -m modelctl.core.tui` 独立入口，等价于 `modelctl tui`。"""

from __future__ import annotations

import sys


def main() -> int:
    from modelctl.cli import main as cli_main

    return cli_main(["tui"])


if __name__ == "__main__":
    sys.exit(main())
