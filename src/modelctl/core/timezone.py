#!/usr/bin/env python3
# ===============================================================================
# @File   : src/modelctl/core/timezone.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 14:30
# @Desc   : 进程与子进程/容器时区统一（默认 Asia/Shanghai）
# ===============================================================================

"""core/timezone.py — 统一进程、引擎子进程与容器的本地时区。

项目内所有时间都是"隐式本地时间"：loguru 的 `{time}`、gateway 的
`datetime.now().astimezone()`、audit 的 `date.today()`、引擎自身日志，全部继承
宿主 OS 时区。部署机通常是 UTC，日志就比预期早 8 小时。

配置项用标准 `TZ`（而不是自造变量名）。三条传播路径：

1. **本进程**：`apply_timezone()` → `time.tzset()`，各处隐式时间随之对齐。
2. **venv 引擎子进程**（vllm/sglang/llamacpp/ollama/…、stats 服务）：
   `start_detached` 用 `apply_subprocess_timezone()` 处理 env——POSIX 显式注入 `TZ`
   （glibc 在子进程启动时读取，且防止这份继承被日后"只传白名单 env"的重构静默破坏）；
   Windows 下反向**删除** `TZ`（见下方警告）。
3. **docker 容器**（vllm/tokenspeed/tensorrt_llm 的 docker runtime）：
   `docker run` 的环境**只认 `-e`**，`start_detached` 注入的 env 只进 docker CLI
   宿主进程、进不了容器。故必须用 `container_timezone_args()` 生成 `-e TZ=`。

⚠ Windows 绝不可让 `TZ` 进入子进程环境：UCRT 按 POSIX 语法解析 `TZ`，IANA 名
`Asia/Shanghai` 被切成 STD 名 `Asia` + DST 名 `Shanghai`，DST 段缺省偏移按
+1h 处理，于是所有子进程夏令时期间显示 +0100（实测，比 UTC 更难排查）。
污染源不止本模块写 `os.environ["TZ"]`——`.env` 里的 `TZ` 经 `load_env()` 落进
`os.environ` 后会被 `{**os.environ}` 继承，故 Windows 必须在 spawn 前删除它。
Windows 无 `time.tzset()`，改 TZ 也无法影响本进程，唯一正确做法是改系统时区。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

TZ_DEFAULT = "Asia/Shanghai"


def resolve_timezone() -> str:
    """应生效的 IANA 时区名：环境变量 TZ > TZ_DEFAULT；tz 库不可用时返回空串。

    纯解析、无副作用，Linux/macOS/Windows 上语义一致。
    """
    wanted = (os.environ.get("TZ") or "").strip() or TZ_DEFAULT
    if _valid(wanted):
        return wanted
    return TZ_DEFAULT if _valid(TZ_DEFAULT) else ""


def apply_timezone() -> str:
    """把本进程本地时区钉为生效时区，返回该时区名（空串表示未生效、沿用 OS 时区）。

    须在 `load_env()` 之后调用，`.env` 的 TZ 才会被读到。loguru 的 `{time}` 在
    **记录时**而非 `add()` 时求值，故相对 `setup_logging()` 的先后都安全；本项目
    统一放在其后，让这里的告警也用统一配色格式。
    """
    from loguru import logger

    raw = (os.environ.get("TZ") or "").strip()
    if not tzset_available():
        # Windows：见模块文档——写 TZ 会把子进程时间弄成 +0100，宁可不生效也不写。
        # 用 debug 而非 warning：Windows 只作开发机（部署目标是 Linux），且本机一般
        # 已是 +08，每次 CLI 调用都告警只会变成噪音。
        # 平台判定必须先于 tz 库判定——Windows 上即便 tzdata 齐备也改不了时区，
        # 此时"缺 tzdata"的告警是误导（子进程时间污染另有出口，见
        # apply_subprocess_timezone）。
        logger.debug(f"当前平台无 tzset（Windows），TZ={raw or TZ_DEFAULT} 不生效；如需对齐请改系统时区")
        return ""

    effective = resolve_timezone()
    if not effective:
        logger.warning(f"时区库不可用（缺 tzdata 且系统无 tz 库），日志沿用系统时区（期望 {raw or TZ_DEFAULT}）")
        return ""

    os.environ["TZ"] = effective
    time.tzset()
    if raw and raw != effective:
        logger.warning(f"TZ={raw} 不是合法 IANA 时区名，已回退 {effective}")
    return effective


def apply_subprocess_timezone(env: dict[str, str]) -> dict[str, str]:
    """就地把子进程环境的时区口径钉对：POSIX 注入 TZ，无 tzset 平台**删除** TZ。

    只"不注入"是不够的：`.env` 的 TZ 已由 CLI 的 `load_env()` 落进 `os.environ`，
    经 `{**os.environ}` 原样继承进子进程。Windows 的 UCRT 按 POSIX 语法解析 TZ，
    IANA 名 `Asia/Shanghai` 被切成 STD 名 `Asia` + DST 名 `Shanghai`，DST 段缺省
    偏移按 +1h 处理，于是子进程日志时间比真实时间早 7 小时（实测 02:24 vs 09:24）。
    UCRT 又只在**进程启动前**读一次 TZ，运行期改 `os.environ` 撤销不了，唯一有效
    做法是在 spawn 前把它从 env 里剔掉。
    """
    tz = resolve_timezone()
    if tzset_available():
        if tz:
            env["TZ"] = tz
    else:
        env.pop("TZ", None)
    return env


def tzset_available() -> bool:
    """当前平台能否用 TZ + tzset 改本地时区（False = Windows，TZ 反而有害）。"""
    return hasattr(time, "tzset")


def container_timezone_args(tz: str | None = None) -> list[str]:
    """docker run 的时区参数：`-e TZ=`，宿主机能定位 tz 文件时再挂 `/etc/localtime`。

    单靠 `-e TZ` 不够——镜像内缺 tzdata 时 glibc **静默忽略**它，容器仍是 UTC。
    因此能定位宿主机 tz 文件时额外 `-v <文件>:/etc/localtime:ro`，不依赖镜像内 tzdata。
    挂载源只从 `zoneinfo.TZPATH` 取（Linux 宿主机一般是 /usr/share/zoneinfo）。
    """
    tz = (tz or "").strip() or resolve_timezone()
    if not tz:
        return []
    args = ["-e", f"TZ={tz}"]
    tz_file = _zoneinfo_file(tz)
    if tz_file is not None:
        args += ["-v", f"{tz_file.as_posix()}:/etc/localtime:ro"]
    return args


def _zoneinfo_file(tz: str) -> Path | None:
    """在 `zoneinfo.TZPATH` 下查找 tz 编译文件，供容器挂载 /etc/localtime。"""
    from zoneinfo import TZPATH

    for base in TZPATH:
        candidate = Path(base) / tz
        if candidate.is_file():
            return candidate
    return None


def _valid(name: str) -> bool:
    """IANA 时区名是否可解析（缺 tz 库时返回 False）。"""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
        return True
    except (ValueError, ZoneInfoNotFoundError, OSError):
        return False
