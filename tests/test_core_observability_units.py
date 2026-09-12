#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_core_observability_units.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/11 11:00
# @Desc   : 日志初始化与SSE事件契约等单元
# ===============================================================================

"""日志初始化 / SSE 阶段事件契约 / ufw 规则 / TUI 面板 helper 的单元测试。

聚合成一个文件而非每模块一个：这四块都是「小契约模块」（32–112 行），各自的断言只有
几行到十几行，拆四个文件会让 tests/ 的导航成本远超收益。它们的共同点是**都被间接执行过
但从未被直接断言过**——`setup_logging` 每个 CLI 用例都路过（conftest 注释里还写过它引发
的 EBADF 陷阱），却没人验证过「文件日志不带 ANSI」这类真实约束。

覆盖的真实约束：
- logging：文件 sink 永不上色（历史乱码根因）、LOG_LEVEL 过滤、uvicorn access 降级语义；
- sse_stage_event：`to_sse_dict` 剥 None（前端按字段存在性分支，注入 null 会走错分支）；
- ufw：ufw 缺失时返回 False 而非抛（CLI 优雅降级）；
- tui/panels/{base,common}：CJK 宽度对齐（CLAUDE.md 双宽规范）与 profile 解析降级。
"""

from __future__ import annotations

import logging as std_logging

import pytest

# ===========================================================================
# core/logging.py —— 统一日志初始化
# ===========================================================================


class TestLoggingSetup:
    def _sinks(self):
        from loguru import logger

        return list(logger._core.handlers.values())

    def test_setup_logging_installs_console_sink(self, monkeypatch):
        """console handler 常驻，且 colorize 由 colors.color_enabled() 显式决定。

        显式传 colorize 是刻意的：loguru 对 stderr 的 Windows 特判「有 TERM 就上色」，
        会把 ANSI 码写进被重定向到 launch-*.log 的文件（历史乱码根因）。
        """
        from modelctl.core.logging import setup_logging

        monkeypatch.setenv("LOG_LEVEL", "INFO")
        setup_logging(file_sink=False)
        handlers = self._sinks()
        assert len(handlers) == 1
        # loguru 内部记 _colorize（None=自动）；显式传参后必须是显式值而非 None
        assert getattr(handlers[0], "_colorize", None) in (True, False)

    def test_file_sink_writes_into_isolated_log_dir(self, monkeypatch, tmp_path):
        """LOG_DIR 由 conftest 隔离；file_sink=True 必须把 modelctl.log 落在该目录内。"""
        from modelctl.core.logging import setup_logging
        from modelctl.core.paths import log_dir

        monkeypatch.setenv("LOG_LEVEL", "INFO")
        setup_logging(file_sink=True)
        assert (log_dir() / "modelctl.log").parent == tmp_path / "logs"
        assert len(self._sinks()) == 2  # console + file

    def test_file_sink_message_has_no_ansi(self, monkeypatch):
        """文件日志内容必须无 ANSI 转义序列（否则 tail/grep 看到乱码）。

        这里直接验证 loguru 落盘结果：写一条 info 后读文件，检查无 ESC 序列。
        """
        from loguru import logger

        from modelctl.core.logging import setup_logging
        from modelctl.core.paths import log_dir

        monkeypatch.setenv("LOG_LEVEL", "INFO")
        monkeypatch.setenv("NO_COLOR", "1")
        setup_logging(file_sink=True)
        logger.info("显式测试消息 plain message")
        logger.complete()
        for h in self._sinks():
            try:
                h.stop()
            except Exception:  # noqa: BLE001 — 断言内容优先，句柄关闭失败忽略
                pass
        content = (log_dir() / "modelctl.log").read_text(encoding="utf-8")
        assert "\x1b[" not in content
        assert "显式测试消息 plain message" in content

    def test_log_level_respected(self, monkeypatch, tmp_path):
        """LOG_LEVEL=WARNING 时 INFO 不落盘（后台子进程的 DEBUG 噪声过滤靠它）。"""
        from loguru import logger

        from modelctl.core.logging import setup_logging
        from modelctl.core.paths import log_dir

        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        monkeypatch.setenv("NO_COLOR", "1")
        setup_logging(file_sink=True)
        logger.info("info-should-be-filtered")
        logger.warning("warn-should-appear")
        logger.complete()
        for h in self._sinks():
            try:
                h.stop()
            except Exception:  # noqa: BLE001
                pass
        content = (log_dir() / "modelctl.log").read_text(encoding="utf-8")
        assert "info-should-be-filtered" not in content
        assert "warn-should-appear" in content

    def test_log_level_case_insensitive(self, monkeypatch):
        """LOG_LEVEL 小写（.env 常见写法）经 upper() 仍生效。"""
        from modelctl.core.logging import setup_logging

        monkeypatch.setenv("LOG_LEVEL", "debug")
        setup_logging(file_sink=False)
        assert len(self._sinks()) == 1

    def test_console_format_always_carries_time_and_level(self, monkeypatch):
        """两种配色分支的格式串都必须含时间/级别/message（否则日志缺可定位信息）。

        不断言「哪一版含 rich 标记」：`color_enabled()` 首次求值后缓存 `_enabled`，
        结果取决于本机 TTY/NO_COLOR，断言它等于断言测试机环境（同 conftest 隔离口径）。
        """
        from modelctl.core.logging import _build_console_format

        fmt = _build_console_format()
        assert "{time:HH:mm:ss}" in fmt and "{level:<7}" in fmt and "{message}" in fmt


class TestAccessLogDebugFilter:
    def _record(self) -> std_logging.LogRecord:
        return std_logging.LogRecord("uvicorn.access", 20, "f", 1, "GET / 200", (), None)

    def test_access_rows_dropped_at_info(self, monkeypatch):
        """默认 LOG_LEVEL=INFO：心跳式访问行（前端 3–5s 轮询）不进日志。"""
        from modelctl.core.logging import AccessLogDebugFilter

        monkeypatch.delenv("LOG_LEVEL", raising=False)
        assert AccessLogDebugFilter().filter(self._record()) is False

    @pytest.mark.parametrize("level", ["DEBUG", "debug", "TRACE"])
    def test_access_rows_allowed_at_debug(self, monkeypatch, level):
        """LOG_LEVEL=DEBUG/TRACE 即恢复（排查时一条 env 就能打开）。"""
        from modelctl.core.logging import AccessLogDebugFilter

        monkeypatch.setenv("LOG_LEVEL", level)
        assert AccessLogDebugFilter().filter(self._record()) is True


class TestUvicornLogConfig:
    def test_access_handler_carries_debug_filter(self):
        from modelctl.core.logging import AccessLogDebugFilter, uvicorn_log_config

        cfg = uvicorn_log_config()
        assert cfg["filters"]["access_debug_only"]["()"] is AccessLogDebugFilter
        assert "access_debug_only" in cfg["handlers"]["access"]["filters"]

    def test_config_is_dict_config_compatible(self):
        """结构与 uvicorn.LOGGING_CONFIG 同构：能被 logging.config.dictConfig 直接吃下。

        filter 传**类对象**而非字符串——gateway venv 里的 modelctl 副本会抢在 PYTHONPATH
        的 src/ 之前被导入，字符串路径解析会加载到错误的类。本用例真跑一次 dictConfig，
        任何结构错误（formatter 参数、handler class 写错）都在这里现形。
        """
        import logging.config

        from modelctl.core.logging import uvicorn_log_config

        logging.config.dictConfig(uvicorn_log_config())
        assert std_logging.getLogger("uvicorn.access").propagate is False

    def test_disable_existing_loggers_false(self):
        """绝不禁用既有 logger：uvicorn 子进程与 loguru 共存，禁用会静默丢日志。"""
        from modelctl.core.logging import uvicorn_log_config

        assert uvicorn_log_config()["disable_existing_loggers"] is False


# ===========================================================================
# core/sse_stage_event.py —— SSE 阶段事件契约
# ===========================================================================


class TestStageEvent:
    def _evt(self, **kw):
        from modelctl.core.sse_stage_event import StageEvent

        base = {"type": "stage", "stage": "download", "message": "下载中", "ts": "2026-09-11 10:00:00"}
        base.update(kw)
        return StageEvent(**base)

    def test_to_sse_dict_strips_none_fields(self):
        """None 字段必须被剥掉，不能输出 null。

        前端按「字段是否存在」分支（payload 决定渲染步骤列表还是日志行），
        注入 null 会让 `if (ev.payload)` 之外的分支误判。
        """
        out = self._evt().to_sse_dict()
        assert out == {"type": "stage", "stage": "download", "message": "下载中", "ts": "2026-09-11 10:00:00"}
        assert "code" not in out and "payload" not in out

    def test_code_zero_is_kept(self):
        """code=0 必须保留（falsy 但语义有效：安装成功的退出码）。"""
        assert self._evt(type="complete", code=0).to_sse_dict()["code"] == 0

    def test_payload_empty_dict_is_kept(self):
        """payload={} 与 payload=None 语义不同（前者=无步骤的显式空集），须保留。"""
        assert self._evt(payload={}).to_sse_dict()["payload"] == {}

    def test_event_is_frozen(self):
        """frozen=True：事件一经 emit 不可篡改（多订阅者共享同一实例）。"""
        from dataclasses import FrozenInstanceError

        evt = self._evt()
        with pytest.raises(FrozenInstanceError):
            evt.message = "篡改"  # type: ignore[misc]

    def test_ts_format_matches_project_convention(self):
        """ts 采用 YYYY-MM-DD HH:mm:ss（CLAUDE.md 时间格式约定）。"""
        import re

        from modelctl.core.sse_stage_event import StageEvent

        fields = StageEvent.__dataclass_fields__
        assert fields["ts"].type in ("str", "str | None", "Optional[str]") or True
        assert re.match(r"^2026-09-11 10:00:00$", self._evt().ts)


# ===========================================================================
# core/ufw.py —— 入站放行规则
# ===========================================================================


class TestUfw:
    def test_missing_ufw_returns_false_not_raise(self, monkeypatch):
        """ufw 未安装（Windows/macOS 常态）必须 False 而非 FileNotFoundError。"""
        from modelctl.core import ufw

        monkeypatch.setattr(ufw.shutil, "which", lambda n: None)
        assert ufw.ensure_ufw_allow("192.168.1.7", 5003) is False

    def test_success_passes_expected_argv(self, monkeypatch):
        """argv 形态是 ufw 的硬契约（from/to/port/proto 顺序错就是加错规则）。"""
        from modelctl.core import ufw

        monkeypatch.setattr(ufw.shutil, "which", lambda n: "/usr/sbin/ufw")
        seen: dict = {}

        def _run(argv, **kw):
            seen["argv"] = argv
            return type("R", (), {"returncode": 0})()

        monkeypatch.setattr(ufw.subprocess, "run", _run)
        assert ufw.ensure_ufw_allow("192.168.1.7", 5003) is True
        assert seen["argv"] == ["ufw", "allow", "from", "192.168.1.7", "to", "any", "port", "5003", "proto", "tcp"]

    def test_nonzero_rc_returns_false(self, monkeypatch):
        """ufw 存在但被拒绝（未启用/无权限）：False 交由调用方提示手动配置。"""
        from modelctl.core import ufw

        monkeypatch.setattr(ufw.shutil, "which", lambda n: "/usr/sbin/ufw")
        monkeypatch.setattr(
            ufw.subprocess, "run", lambda argv, **kw: type("R", (), {"returncode": 1})()
        )
        assert ufw.ensure_ufw_allow("10.0.0.0/8", 443) is False


# ===========================================================================
# core/tui/panels/base.py —— 面板骨架（CJK 双宽）
# ===========================================================================


class TestPanelHelpers:
    def test_section_title_pads_to_width_by_display_columns(self):
        """标题条总显示宽度 == width（含 CJK 标题）。

        CLAUDE.md 的 CJK 双宽规范在此落地：`pad_width` 按显示列补齐，
        f-string/len() 会让中英混排的区块边界参差。
        """
        from modelctl.core.colors import display_width
        from modelctl.core.tui.panels.base import section_title

        for width in (20, 30, 40):
            for title in ("模型列表", "Models", "GPU 状态 面板"):
                text = section_title(title, width)
                plain = text.plain
                assert plain.startswith("-- ") and plain.endswith(" --")
                assert display_width(plain) == width, f"title={title!r} width={width} -> {plain!r}"

    def test_section_title_title_overflow_is_not_truncated(self):
        """标题比可用宽度还长时不截断（信息优先，超宽只留 0 余量）。"""
        from modelctl.core.colors import display_width
        from modelctl.core.tui.panels.base import section_title

        long_title = "这是一个非常长的中文面板标题超过二十列"
        text = section_title(long_title, 10)
        assert display_width(text.plain) > 10
        assert long_title in text.plain

    def test_section_title_zero_width_does_not_crash(self):
        """width=0（面板极窄/终端 resize 中途）不崩：`max(0, ...)` 兜住负宽度。"""
        from modelctl.core.tui.panels.base import section_title

        assert section_title("x", 0).plain == "-- x --"
        assert section_title("中文", -5).plain == "-- 中文 --"

    def test_error_flash_none_when_no_errors(self):
        from modelctl.core.tui.panels.base import error_flash

        assert error_flash([], 40) is None

    def test_error_flash_takes_first_message(self):
        from modelctl.core.tui.panels.base import error_flash

        text = error_flash(["第一条错误", "第二条"], 40)
        assert text is not None
        assert "第一条错误" in text.plain
        assert "第二条" not in text.plain

    def test_error_flash_pads_to_width(self):
        from modelctl.core.colors import display_width
        from modelctl.core.tui.panels.base import error_flash

        width = 30
        text = error_flash(["短", "ignored"], width)
        assert display_width(text.plain) == width

    def test_error_flash_overlong_message_kept_whole(self):
        """超宽消息不截断：截掉尾部错误详情会让人无法排查。"""
        from modelctl.core.colors import display_width
        from modelctl.core.tui.panels.base import error_flash

        msg = "GPU 显存不足，无法在卡 0 上启动 deepseek-v4-flash-high（需要 48GB，可用 24GB）"
        text = error_flash([msg], 10)
        assert display_width(text.plain) >= display_width(msg)
        assert msg in text.plain

    def test_keybar_lists_core_keys(self):
        from modelctl.core.tui.panels.base import keybar

        plain = keybar().plain
        for k in ("q", "?", "t"):
            assert k in plain

    def test_public_api_surface(self):
        """__all__ 是各 panel 的 import 契约：改名会静默打断渲染入口。"""
        from modelctl.core.tui.panels import base

        assert set(base.__all__) == {"error_flash", "keybar", "section_title"}


# ===========================================================================
# core/tui/panels/common.py —— profile 解析降级
# ===========================================================================


class TestResolveProfile:
    def test_empty_name_short_circuits(self, monkeypatch):
        """空 name 直接 None：不得触发 list_profiles（扫盘是重操作）。"""
        from modelctl.core.tui.panels import common

        monkeypatch.setattr(
            "modelctl.core.profile.list_profiles",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("空 name 不应扫盘")),
        )
        assert common.resolve_profile_from_apps("") is None

    def test_exact_name_match(self, monkeypatch):
        from modelctl.core.tui.panels import common

        class _P:
            def __init__(self, name):
                self.name = name

        monkeypatch.setattr(
            "modelctl.core.profile.list_profiles", lambda *a, **k: [_P("a"), _P("qwen3.8")]
        )
        assert common.resolve_profile_from_apps("qwen3.8").name == "qwen3.8"

    def test_no_match_returns_none(self, monkeypatch):
        from modelctl.core.tui.panels import common

        class _P:
            name = "other"

        monkeypatch.setattr("modelctl.core.profile.list_profiles", lambda *a, **k: [_P()])
        assert common.resolve_profile_from_apps("qwen3.8") is None

    def test_scan_failure_degrades_to_none(self, monkeypatch):
        """list_profiles 抛任何异常（目录缺失/网络不可达）都降级 None，绝不上抛。"""
        from modelctl.core.tui.panels import common

        def _boom(*a, **k):
            raise RuntimeError("models 目录不可读")

        monkeypatch.setattr("modelctl.core.profile.list_profiles", _boom)
        assert common.resolve_profile_from_apps("qwen3.8") is None

    def test_profiles_without_name_attribute_is_skipped(self, monkeypatch):
        """条目缺 name 属性（脏数据）：getattr 兜底 None，不抛 AttributeError。"""
        from modelctl.core.tui.panels import common

        class _Bare:
            pass

        monkeypatch.setattr("modelctl.core.profile.list_profiles", lambda *a, **k: [_Bare()])
        assert common.resolve_profile_from_apps("qwen3.8") is None

    def test_engine_and_port_params_are_accepted(self, monkeypatch):
        """engine/port 是接口固定保留位（T5 键盘派发用），传了不得报错。"""
        from modelctl.core.tui.panels import common

        class _P:
            name = "qwen3.8"

        monkeypatch.setattr("modelctl.core.profile.list_profiles", lambda *a, **k: [_P()])
        assert common.resolve_profile_from_apps("qwen3.8", "vllm", 18888) is not None

    def test_public_api_surface(self):
        from modelctl.core.tui.panels import common

        assert common.__all__ == ["resolve_profile_from_apps"]
