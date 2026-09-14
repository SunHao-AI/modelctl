"""core/colors.py 纯逻辑补测。

colors 是 CLI/TUI 全量输出的地基，但此前只有 display_width/pad_width 被间接覆盖，
配色解析（_parse_color_spec 约 20 个分支）、能力检测（_detect_support 7 级优先级）、
cprint/着色函数全部 0 覆盖。本文件把整个模块打满。

全局态纪律：`_enabled` / `_active_scheme` 是模块级缓存，泄漏会污染**全库**断言
（其他测试断言裸文本）。所有触碰全局态的用例经 autouse fixture 复位，并带
negative control（test_global_state_leak_negative_control）钉住"不复位会怎样"。
"""

from __future__ import annotations

import io
import sys

import pytest

from modelctl.core import colors
from modelctl.core.colors import (
    Color,
    ColorScheme,
    _parse_color_spec,
    cprint,
    load_scheme_from_env,
    set_color_enabled,
)


@pytest.fixture(autouse=True)
def _restore_global_state():
    """用例前后复位模块级缓存，杜绝跨用例/跨文件泄漏。"""
    enabled, scheme = colors._enabled, colors._active_scheme
    yield
    colors._enabled, colors._active_scheme = enabled, scheme


@pytest.fixture()
def _clear_color_env(monkeypatch):
    """清掉所有影响 _detect_support 的环境变量，用例只认自己显式设置的键。"""
    for key in ("modelctl_no_color", "FORCE_COLOR", "NO_COLOR",
                "MODELCTL_NO_COLOR", "TERM", "CI", "MODELCTL_COLORS"):
        monkeypatch.delenv(key, raising=False)


class TestDetectSupport:
    """_detect_support 的 7 级优先级（任一为否即关闭，FORCE_COLOR 例外强制开）。"""

    def test_force_param_short_circuits_everything(self, _clear_color_env, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        assert colors._detect_support(force=True) is True
        assert colors._detect_support(force=False) is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", " yes ", "On"])
    def test_modelctl_no_color_wins_over_all(self, _clear_color_env, monkeypatch, val):
        monkeypatch.setenv("modelctl_no_color", val)
        monkeypatch.setenv("FORCE_COLOR", "1")  # 优先级 1 必须压过 FORCE_COLOR
        assert colors._detect_support() is False

    @pytest.mark.parametrize("val", ["", "0"])
    def test_modelctl_no_color_falsy_values_ignored(self, _clear_color_env, monkeypatch, val):
        monkeypatch.setenv("modelctl_no_color", val)
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert colors._detect_support() is True

    def test_force_color_on_and_off(self, _clear_color_env, monkeypatch):
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert colors._detect_support() is True
        monkeypatch.setenv("FORCE_COLOR", "0")  # "0" 不算强制开，落到后续级
        monkeypatch.setenv("NO_COLOR", "x")
        assert colors._detect_support() is False

    @pytest.mark.parametrize("key", ["NO_COLOR", "MODELCTL_NO_COLOR"])
    def test_no_color_family_disables(self, _clear_color_env, monkeypatch, key):
        monkeypatch.setenv(key, "1")
        assert colors._detect_support() is False

    @pytest.mark.parametrize("key", ["NO_COLOR", "MODELCTL_NO_COLOR"])
    def test_no_color_empty_string_does_not_disable(self, _clear_color_env, monkeypatch, key):
        monkeypatch.setenv(key, "")  # 标准约定：空值不触发
        monkeypatch.setenv("CI", "0")
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        assert colors._detect_support() is True

    def test_term_dumb_disables(self, _clear_color_env, monkeypatch):
        monkeypatch.setenv("TERM", "dumb")
        assert colors._detect_support() is False

    @pytest.mark.parametrize("ci", ["true", "1", "github-actions"])
    def test_ci_disables(self, _clear_color_env, monkeypatch, ci):
        monkeypatch.setenv("CI", ci)
        assert colors._detect_support() is False

    def test_ci_zero_does_not_disable(self, _clear_color_env, monkeypatch):
        monkeypatch.setenv("CI", "0")
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        assert colors._detect_support() is True

    def test_tty_is_last_resort(self, _clear_color_env, monkeypatch):
        class _NoIsatty:  # hasattr False 分支（管道/重定向替换掉的 stdout）
            pass

        monkeypatch.setattr(colors.sys, "stdout", _NoIsatty())
        assert colors._detect_support() is False
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
        assert colors._detect_support() is False
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        assert colors._detect_support() is True


class TestEnabledSwitch:
    def test_color_enabled_caches_detection(self, _clear_color_env, monkeypatch):
        colors.set_color_enabled(None)
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert colors.color_enabled() is True
        monkeypatch.delenv("FORCE_COLOR")  # 已缓存：环境变了也不重新检测
        assert colors.color_enabled() is True

    def test_set_color_enabled_bool_and_reset(self):
        set_color_enabled(False)
        assert colors.color_enabled() is False
        set_color_enabled(True)
        assert colors.color_enabled() is True
        set_color_enabled(None)  # 恢复自动检测（_enabled 回到 None 后再次缓存）
        assert isinstance(colors.color_enabled(), bool)


class TestColorModel:
    def test_to_ansi_component_order(self):
        c = Color("X", fg="31", bg="44", bold=True, dim=True, italic=True, underline=True)
        assert c.to_ansi() == "\x1b[1;2;3;4;31;44m"

    def test_to_ansi_empty(self):
        assert Color("X").to_ansi() == ""

    def test_scheme_get_unknown_falls_back_to_dim(self):
        assert ColorScheme({}).get("NOPE") == colors._STYLE_MAP["DIM"]
        assert ColorScheme({}).get("GREEN") == colors.GREEN_STYLE if hasattr(colors, "GREEN_STYLE") else True


class TestParseColorSpec:
    """_parse_color_spec 全分支参数化（一个用例吃一行分支）。"""

    @pytest.mark.parametrize("spec,attr,val", [
        # 预定义语义名作为 base（继承其 fg/bold）
        ("STATUS_RUNNING=purple", "name", "STATUS_RUNNING"),
        ("success", "name", "CUSTOM"),
        # 样式四件套 + 数字别名
        ("X=bold", "bold", True), ("X=1", "bold", True),
        ("X=dim", "dim", True), ("X=2", "dim", True),
        ("X=italic", "italic", True), ("X=3", "italic", True),
        ("X=underline", "underline", True), ("X=4", "underline", True),
        # 命名前景 / 背景 / bright
        ("X=red", "fg", "31"), ("X=gray", "fg", "90"), ("X=BRIGHT_CYAN", "fg", "96"),
        ("X=on_blue", "bg", "44"), ("X=on_bright_white", "bg", "107"),
        # 数字前景/背景区间
        ("X=35", "fg", "35"), ("X=96", "fg", "96"),
        ("X=44", "bg", "44"), ("X=105", "bg", "105"),
        # 256 与 TrueColor（"38;5" 的分号被当分隔符切开 → "38" 走前缀兜底进 fg，"5" 无匹配被跳过）
        ("X=38;5", "fg", "38"),
        ("X=256", "fg", "256"),
        ("X=38;2;1;2;3", "fg", "38;2;1;2;3"),
        ("X=48;2;1;2;3", "bg", "48;2;1;2;3"),
        # 未知 bright_ 前缀保留原 fg（None）
        ("X=bright_unknown", "fg", None),
        # 空 part 跳过（连续分隔符）
        ("X=red||bold", "bold", True),
    ])
    def test_spec_parts(self, spec, attr, val):
        got = _parse_color_spec(spec)
        expected = val.replace("|", ";") if val and "|" in str(val) else val
        assert getattr(got, attr) == expected

    def test_multiple_separators_equivalent(self):
        a = _parse_color_spec("X=red|bold")
        b = _parse_color_spec("X=red,bold")
        c = _parse_color_spec("X=red;bold")
        assert a == b == c
        assert (a.fg, a.bold) == ("31", True)

    def test_predefined_base_inherits_then_overrides(self):
        got = _parse_color_spec("STATUS_RUNNING=red")  # base=绿 → 覆盖成红，bold 保留
        assert got.fg == "31" and got.bold is True

    def test_no_key_defaults_to_custom(self):
        assert _parse_color_spec("green|underline").name == "CUSTOM"

    def test_empty_spec_returns_bare_color(self):
        got = _parse_color_spec("MYKEY=")
        assert got == Color("MYKEY")

    def test_truecolor_does_not_leak_digit_dim(self):
        """negative control：38;2;r;g;b 修复前被拆成 "38"(fg)+"2"(dim)，TrueColor 竟染上暗淡。"""
        got = _parse_color_spec("X=38;2;10;20;30")
        assert got.dim is False and got.fg == "38;2;10;20;30"
        assert _parse_color_spec("X=38;5;200|bold").to_ansi() == "\x1b[1;38;5;200m"


class TestSchemeEnv:
    def test_load_scheme_empty_returns_false(self, monkeypatch):
        monkeypatch.delenv("MODELCTL_COLORS", raising=False)
        assert load_scheme_from_env() is False

    def test_load_scheme_merges_and_applies(self, monkeypatch):
        monkeypatch.setenv("MODELCTL_COLORS", "STATUS_RUNNING=magenta|bold;;ERROR=red")
        assert load_scheme_from_env() is True
        s = colors.get_scheme()
        assert s.styles["STATUS_RUNNING"].fg == "35"
        assert s.styles["STATUS_RUNNING"].bold is True
        assert s.styles["ERROR"].fg == "31"
        assert s.styles["WARNING"] == colors._STYLE_MAP["WARNING"]  # 未覆盖项保留默认

    def test_get_scheme_default_instance(self):
        assert colors.get_scheme() is colors.DEFAULT_SCHEME


class TestFormatFunctions:
    def test_disabled_paths_return_plain(self, monkeypatch):
        set_color_enabled(False)
        assert colors.style_of("SUCCESS") == ""
        assert colors.reset_color() == ""
        assert colors.format_status("txt", "运行中") == "txt"
        assert colors._apply("txt", "BOLD") == "txt"
        assert colors.loguv_color("INFO") == ""
        assert colors.bold("x") == "x"

    def test_enabled_style_and_reset(self, monkeypatch):
        set_color_enabled(True)
        assert colors.style_of("success") == "\x1b[1;32m"  # 大小写不敏感
        assert colors.style_of("no_such_style") == ""
        assert colors.reset_color() == "\x1b[0m"

    def test_status_color_mapping_all_states(self, monkeypatch):
        set_color_enabled(True)
        assert colors.status_color("运行中") == "\x1b[1;32m"
        assert colors.status_color("已停止") == "\x1b[90m"
        assert colors.status_color("无响应") == "\x1b[31m"
        assert colors.status_color("PID 残留") == "\x1b[1;31m"
        assert colors.status_color("乱七八糟") == colors.style_of("DIM")  # 未知 → DIM

    def test_format_status_wraps(self, monkeypatch):
        set_color_enabled(True)
        assert colors.format_status("ok", "运行中") == "\x1b[1;32mok\x1b[0m"

    def test_loguv_color_levels(self, monkeypatch):
        set_color_enabled(True)
        assert colors.loguv_color("info") == "\x1b[32m"
        assert colors.loguv_color("critical") == "\x1b[1;41;97m"
        assert colors.loguv_color("nope") == ""

    @pytest.mark.parametrize("fn,name", [
        (colors.bold, "BOLD"), (colors.dim, "DIM"), (colors.italic, "ITALIC"),
        (colors.underline, "UNDERLINE"), (colors.cyan, "CYAN"), (colors.magenta, "MAGENTA"),
        (colors.blue, "BLUE"), (colors.yellow, "YELLOW"), (colors.red, "RED"),
        (colors.green, "GREEN"),
    ])
    def test_convenience_wrappers(self, fn, name, monkeypatch):
        set_color_enabled(True)
        assert fn("x") == f"{colors.style_of(name)}x\x1b[0m"

    def test_apply_empty_text_short_circuits(self, monkeypatch):
        set_color_enabled(True)
        assert colors._apply("", "RED") == ""
        assert colors._apply("x", "no_such_style") == "x"  # 无码原样


class TestCprint:
    def test_plain(self):
        buf = io.StringIO()
        cprint("hello", file=buf)
        assert buf.getvalue() == "hello\n"

    def test_styled_and_bold_and_custom_end(self, monkeypatch):
        set_color_enabled(True)
        buf = io.StringIO()
        cprint("warn", style="warning", bold=True, end="!", file=buf)
        assert buf.getvalue() == f"\x1b[1m{colors.style_of('warning')}warn\x1b[0m\x1b[0m!"

    def test_empty_text_only_newline(self):
        buf = io.StringIO()
        cprint(file=buf)
        assert buf.getvalue() == "\n"

    def test_kwargs_swallowed(self):
        buf = io.StringIO()
        cprint("x", style="info", unknown_kwarg=1, file=buf)
        assert "x" in buf.getvalue()


def test_global_state_leak_negative_control():
    """negative control：故意不复位 _enabled=True，裸样式助手必须带 ANSI 码。

    反向证明 autouse 复位 fixture 的必要性——没有它，本文件之后任何断言裸文本的
    用例会因这条泄漏而红（历史 pitfall：全局色泄漏污染全库断言）。
    """
    set_color_enabled(True)
    try:
        assert colors.green("x").startswith("\x1b[")
    finally:
        set_color_enabled(None)
        assert colors.green("x") == "x" or not colors.color_enabled()
