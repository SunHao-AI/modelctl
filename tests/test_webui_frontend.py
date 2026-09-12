#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_webui_frontend.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/11 10:30
# @Desc   : 前端环境自举与 SPA 挂载测试
# ===============================================================================

"""core/webui/frontend.py + core/webui/server.py 测试（前端自举 / 静态挂载 / 端口）。

这两个模块是 `modelctl webui start` 的「零前置条件」承诺：产物缺失时该自动装 Node、
装依赖、构建；装不了就必须回一条可直接复制的手动命令，**绝不能**把用户卡在一个
看不懂的堆栈上。因此断言重心是：

1. 决策分流的**每一支**都给出可执行指引（非交互 / 无权限 / 非 Linux / 构建失败）；
2. 探测函数对「未安装 / 输出异常 / 超时」三类外部世界的不确定性一律降级为 None/False，
   绝不上抛（一个 FileNotFoundError 会让 webui 起不来）；
3. SPA 兜底路由**不得**吞掉 API 前缀（历史坑：/v1/* 返回 HTML，客户端表现为诡异解析失败）。

全部子进程调用被打桩，零 npm/node 依赖，可在无 Node 的机器上跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402

from modelctl.core.webui import frontend as fe  # noqa: E402
from modelctl.core.webui import server as sv  # noqa: E402

# ---------------------------------------------------------------------------
# web_root / deps_installed / find_npm
# ---------------------------------------------------------------------------


def test_web_root_is_project_root_web(monkeypatch, tmp_path):
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    assert fe.web_root() == tmp_path / "web"


def test_deps_installed_true_only_when_node_modules_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    assert fe.deps_installed() is False
    (tmp_path / "web" / "node_modules").mkdir(parents=True)
    assert fe.deps_installed() is True


def test_deps_installed_false_when_node_modules_is_file(monkeypatch, tmp_path):
    """同名文件（异常状态）不算已安装——is_dir 而非 exists。"""
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    (tmp_path / "web").mkdir(parents=True)
    (tmp_path / "web" / "node_modules").write_text("x", encoding="utf-8")
    assert fe.deps_installed() is False


def test_find_npm_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(fe.shutil, "which", lambda name: None)
    assert fe.find_npm() is None


def test_find_npm_returns_absolute_path(monkeypatch):
    monkeypatch.setattr(fe.shutil, "which", lambda name: "/usr/local/bin/npm" if name == "npm" else None)
    assert fe.find_npm() == "/usr/local/bin/npm"


# ---------------------------------------------------------------------------
# node_major_version —— 外部世界不确定性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stdout,expected",
    [
        ("v22.11.0\n", 22),
        ("v18.0.0", 18),
        ("  v20.10.0  \n", 20),
        ("22.1.0", 22),          # 无 v 前缀（部分发行版 wrapper）
        ("", None),              # 空输出
        ("not-a-version", None),  # 非数字主版本
        ("v.x.y", None),
    ],
)
def test_node_major_version_parses_and_degrades(monkeypatch, stdout, expected):
    monkeypatch.setattr(fe.shutil, "which", lambda name: "/usr/bin/npm")
    monkeypatch.setattr(
        fe.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": stdout})()
    )
    assert fe.node_major_version() == expected


def test_node_major_version_none_when_npm_missing(monkeypatch):
    """npm 未装时**不得**派生 node 子进程（无谓的进程开销）。"""
    monkeypatch.setattr(fe.shutil, "which", lambda name: None)

    def _boom(*a, **k):
        raise AssertionError("npm 缺失时不应执行 node --version")

    monkeypatch.setattr(fe.subprocess, "run", _boom)
    assert fe.node_major_version() is None


@pytest.mark.parametrize("exc", [OSError("node 执行失败"), fe.subprocess.TimeoutExpired("node", 10)])
def test_node_major_version_swallows_oserror_and_timeout(monkeypatch, exc):
    """子进程异常/超时降级为 None：探测函数上抛会让 webui 启动直接崩。"""
    monkeypatch.setattr(fe.shutil, "which", lambda name: "/usr/bin/npm")

    def _raise(*a, **k):
        raise exc

    monkeypatch.setattr(fe.subprocess, "run", _raise)
    assert fe.node_major_version() is None


# ---------------------------------------------------------------------------
# interactive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stdin_tty,stdout_tty,expected", [
    (True, True, True),
    (True, False, False),   # 输出被重定向（管道/日志）不算交互
    (False, True, False),   # ssh host 'modelctl webui start' 形态
    (False, False, False),
])
def test_interactive_requires_both_tty(monkeypatch, stdin_tty, stdout_tty, expected):
    monkeypatch.setattr(sys, "stdin", type("S", (), {"isatty": lambda self: stdin_tty})())
    monkeypatch.setattr(sys, "stdout", type("S", (), {"isatty": lambda self: stdout_tty})())
    assert fe.interactive() is expected


def test_interactive_false_when_streams_closed(monkeypatch):
    """已关闭的 stdio 抛 ValueError，须降级 False 而非冒泡。"""

    def _raise(self):
        raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stdin", type("S", (), {"isatty": _raise})())
    assert fe.interactive() is False


# ---------------------------------------------------------------------------
# _run / _sudo_prefix / _package_manager / 包管理器参数表
# ---------------------------------------------------------------------------


def test_run_returns_child_returncode(monkeypatch):
    monkeypatch.setattr(fe.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 3})())
    assert fe._run(["npm", "install"]) == 3


def test_run_timeout_maps_to_124(monkeypatch):
    """超时映射 124（与 GNU timeout 口径一致），供上层判「装不动」而非崩溃。"""

    def _raise(*a, **k):
        raise fe.subprocess.TimeoutExpired("npm", 1)

    monkeypatch.setattr(fe.subprocess, "run", _raise)
    assert fe._run(["npm", "install"]) == 124


def test_run_oserror_maps_to_127(monkeypatch):
    """命令不存在映射 127（shell 的 command-not-found 口径）。"""

    def _raise(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(fe.subprocess, "run", _raise)
    assert fe._run(["npm", "install"]) == 127


@pytest.mark.parametrize(
    "geteuid,sudo_available,expected",
    [
        (None, False, None),      # 非 root、无 sudo（Windows 走这支：无 geteuid）
        (0, False, []),           # root 免前缀
        (1000, True, ["sudo"]),   # 普通用户 + sudo
        (1000, False, None),      # 普通用户无 sudo → 降级指引
    ],
)
def test_sudo_prefix_decision(monkeypatch, geteuid, sudo_available, expected):
    if geteuid is None:
        monkeypatch.delattr(fe.os, "geteuid", raising=False)
    else:
        monkeypatch.setattr(fe.os, "geteuid", lambda: geteuid, raising=False)
    monkeypatch.setattr(fe.shutil, "which", lambda n: "/usr/bin/sudo" if (n == "sudo" and sudo_available) else None)
    assert fe._sudo_prefix() == expected


@pytest.mark.parametrize("pm", ["apt", "dnf", "yum", "zypper", "pacman", "apk"])
def test_package_manager_arg_tables_are_complete(pm):
    """六个包管理器的 update/install 参数表都必须可取（KeyError 会在安装中途炸）。"""
    assert fe._pm_update_args(pm)
    install = fe._pm_install_args(pm, ["nodejs", "npm"])
    assert "nodejs" in install and "npm" in install


@pytest.mark.parametrize(
    "present,expected",
    [(["apt-get"], "apt"), (["dnf"], "dnf"), (["apk"], "apk"), ([], None),
     (["apt-get", "dnf"], "apt")],  # 多个共存时按声明顺序取首个
)
def test_package_manager_detection(monkeypatch, present, expected):
    monkeypatch.setattr(fe.shutil, "which", lambda n: f"/usr/bin/{n}" if n in present else None)
    assert fe._package_manager() == expected


# ---------------------------------------------------------------------------
# install_node —— 权限/平台护栏
# ---------------------------------------------------------------------------


def test_install_node_refuses_non_linux(monkeypatch):
    monkeypatch.setattr(fe.platform, "system", lambda: "Windows")
    assert fe.install_node() is False


def test_install_node_refuses_without_privilege(monkeypatch):
    monkeypatch.setattr(fe.platform, "system", lambda: "Linux")
    monkeypatch.delattr(fe.os, "geteuid", raising=False)
    monkeypatch.setattr(fe.shutil, "which", lambda n: None)
    assert fe.install_node() is False


def test_install_node_refuses_without_package_manager(monkeypatch):
    monkeypatch.setattr(fe.platform, "system", lambda: "Linux")
    monkeypatch.setattr(fe.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(fe.shutil, "which", lambda n: None)  # 无任何包管理器
    assert fe.install_node() is False


def test_install_node_succeeds_via_nodesource(monkeypatch):
    """NodeSource 主路径成功：apt + curl 就绪，安装后 node 达标。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(fe.platform, "system", lambda: "Linux")
    monkeypatch.setattr(fe.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        fe.shutil, "which", lambda n: f"/usr/bin/{n}" if n in ("apt-get", "curl") else None
    )
    monkeypatch.setattr(fe, "_run", lambda argv, **k: calls.append(list(argv)) or 0)
    monkeypatch.setattr(fe, "_run_node_source", lambda sudo, url: calls.append([url]) or 0)
    monkeypatch.setattr(fe, "_node_usable", lambda: True)
    monkeypatch.setattr(fe, "node_major_version", lambda: 22)
    assert fe.install_node() is True
    assert any("deb.nodesource.com" in c[0] for c in calls)


def test_install_node_falls_back_to_distro_package(monkeypatch):
    """NodeSource 配置失败 → 回退发行版自带 nodejs/npm。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(fe.platform, "system", lambda: "Linux")
    monkeypatch.setattr(fe.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        fe.shutil, "which", lambda n: f"/usr/bin/{n}" if n in ("apt-get", "curl") else None
    )
    monkeypatch.setattr(fe, "_run", lambda argv, **k: calls.append(list(argv)) or 0)
    monkeypatch.setattr(fe, "_run_node_source", lambda sudo, url: 1)  # 主路径失败
    monkeypatch.setattr(fe, "_node_usable", lambda: True)
    monkeypatch.setattr(fe, "node_major_version", lambda: 18)
    assert fe.install_node() is True
    assert any("nodejs" in c and "npm" in c for c in calls)


def test_install_node_reports_failure_when_version_still_low(monkeypatch):
    """安装命令返回 0 但 node 版本仍不达标 → 整体判失败（不能只看退出码）。"""
    monkeypatch.setattr(fe.platform, "system", lambda: "Linux")
    monkeypatch.setattr(fe.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(fe.shutil, "which", lambda n: f"/usr/bin/{n}" if n == "apk" else None)
    monkeypatch.setattr(fe, "_run", lambda argv, **k: 0)
    monkeypatch.setattr(fe, "_node_usable", lambda: False)
    assert fe.install_node() is False


def test_node_usable_thresholds(monkeypatch):
    monkeypatch.setattr(fe.shutil, "which", lambda n: "/usr/bin/npm")
    monkeypatch.setattr(fe, "node_major_version", lambda: fe.NODE_MIN_MAJOR)
    assert fe._node_usable() is True
    monkeypatch.setattr(fe, "node_major_version", lambda: fe.NODE_MIN_MAJOR - 1)
    assert fe._node_usable() is False
    monkeypatch.setattr(fe, "node_major_version", lambda: None)
    assert fe._node_usable() is False
    monkeypatch.setattr(fe.shutil, "which", lambda n: None)
    monkeypatch.setattr(fe, "node_major_version", lambda: 22)
    assert fe._node_usable() is False


# ---------------------------------------------------------------------------
# _registry_args —— 尊重用户已有配置
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    """把家目录指到 tmp：_registry_args 会读 `~/.npmrc`，不隔离就依赖开发者机器。

    走 env（USERPROFILE/HOME）而非 monkeypatch pathlib.Path.home——后者是全局类方法，
    patch 期间 pytest 自身的 home 查询也会被劫持。
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    return home


def test_registry_args_uses_mirror_by_default(isolated_home):
    assert fe._registry_args() == ["--registry", fe.NPM_MIRROR]


def test_registry_args_respects_user_npmrc(isolated_home):
    """用户已自配 registry 时**不得**追加 --registry（会覆盖用户镜像）。"""
    isolated_home.joinpath(".npmrc").write_text("registry=https://my.corp/registry\n", encoding="utf-8")
    assert fe._registry_args() == []


def test_registry_args_project_npmrc_also_respected(isolated_home):
    """工程级 web/.npmrc（= PROJECT_ROOT/web/.npmrc）配了 registry 同样生效。"""
    web = isolated_home.parent / "web"  # PROJECT_ROOT = tmp_path，web_root() = tmp_path/web
    web.mkdir()
    web.joinpath(".npmrc").write_text("registry=https://corp/\n", encoding="utf-8")
    assert fe.web_root() == web
    assert fe._registry_args() == []


def test_registry_args_survives_non_utf8_npmrc(isolated_home):
    """非 UTF-8 的 ~/.npmrc 不得抛栈（BUG-FE-01 回归钉）。

    历史实现只捕 OSError，而 UnicodeDecodeError 属 ValueError——中文 Windows 上用
    GBK 存过 .npmrc 的用户，`modelctl webui start` 走到这里直接崩。现在以
    errors="ignore" 读取：ASCII 键名 `registry=` 在 ignore 下完好保留，既能识别用户
    已配的 registry，也不会因后面的中文字节抛错。
    """
    isolated_home.joinpath(".npmrc").write_bytes("registry=中文\r\n".encode("gbk"))
    assert fe._registry_args() == []


def test_registry_args_non_utf8_without_registry_still_uses_mirror(isolated_home):
    """非 UTF-8 但没配 registry：忽略解码噪声后仍应回落到镜像源。"""
    isolated_home.joinpath(".npmrc").write_bytes("prefix=C:\\节点\n".encode("gbk"))
    assert fe._registry_args() == ["--registry", fe.NPM_MIRROR]


# ---------------------------------------------------------------------------
# install_deps / build
# ---------------------------------------------------------------------------


def test_install_deps_requires_package_json(monkeypatch, tmp_path):
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    assert fe.install_deps() is False


def test_install_deps_appends_mirror_and_checks_result(monkeypatch, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    web.joinpath("package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    seen: dict = {}

    def _fake_run(argv, cwd=None, **k):
        seen["argv"] = list(argv)
        seen["cwd"] = cwd
        web.joinpath("node_modules").mkdir()  # 模拟安装成功
        return 0

    monkeypatch.setattr(fe, "_run", _fake_run)
    monkeypatch.setattr(fe, "find_npm", lambda: "/usr/bin/npm")
    monkeypatch.setattr(fe.Path, "home", classmethod(lambda cls: tmp_path / "none"))
    assert fe.install_deps() is True
    assert seen["argv"][0] == "/usr/bin/npm"       # 用绝对路径（PATH 未刷新场景）
    assert "install" in seen["argv"]
    assert "--registry" in seen["argv"]
    assert Path(seen["cwd"]) == web


def test_install_deps_false_on_nonzero_rc(monkeypatch, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    web.joinpath("package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(fe, "_run", lambda argv, **k: 1)
    monkeypatch.setattr(fe.Path, "home", classmethod(lambda cls: tmp_path / "none"))
    assert fe.install_deps() is False


def test_install_deps_false_when_node_modules_still_missing(monkeypatch, tmp_path):
    """rc=0 但 node_modules 仍缺失（npm 假成功）→ 判失败，避免后续构建再炸。"""
    web = tmp_path / "web"
    web.mkdir()
    web.joinpath("package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(fe, "_run", lambda argv, **k: 0)
    monkeypatch.setattr(fe.Path, "home", classmethod(lambda cls: tmp_path / "none"))
    assert fe.install_deps() is False


def test_build_false_on_nonzero_rc(monkeypatch):
    monkeypatch.setattr(fe, "_run", lambda argv, **k: 2)
    assert fe.build() is False


def test_build_false_when_dist_missing(monkeypatch):
    """构建命令 0 退出但无产物（vite 输出目录漂移/被清理）→ 判失败并指路。"""
    monkeypatch.setattr(fe, "_run", lambda argv, **k: 0)
    monkeypatch.setattr(fe, "dist_ready", lambda *a: False)
    assert fe.build() is False


def test_build_true_when_dist_ready(monkeypatch):
    monkeypatch.setattr(fe, "_run", lambda argv, **k: 0)
    monkeypatch.setattr(fe, "dist_ready", lambda *a: True)
    assert fe.build() is True


# ---------------------------------------------------------------------------
# ensure_frontend —— 决策矩阵
# ---------------------------------------------------------------------------


def test_ensure_frontend_short_circuits_when_dist_ready(monkeypatch):
    """产物就绪时零副作用：绝不探测 node、绝不装依赖。"""
    monkeypatch.setattr(fe, "dist_ready", lambda *a: True)

    def _boom(*a, **k):
        raise AssertionError("dist 就绪时不应触碰 node/npm")

    monkeypatch.setattr(fe, "_node_usable", _boom)
    ok, msg = fe.ensure_frontend()
    assert ok is True
    assert "就绪" in msg


def test_ensure_frontend_missing_sources_is_not_buildable(monkeypatch, tmp_path):
    """克隆不完整（无 web/package.json）：直接给结论，不去装 Node。"""
    monkeypatch.setattr(fe, "dist_ready", lambda *a: False)
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    ok, msg = fe.ensure_frontend(auto=True)
    assert ok is False
    assert "前端源码缺失" in msg


def test_ensure_frontend_non_interactive_gives_manual_hint(monkeypatch, tmp_path):
    """非交互终端 + 缺 Node：只给手动命令，不自动安装（CI/ssh 不能卡住）。"""
    web = tmp_path / "web"
    web.mkdir()
    web.joinpath("package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fe, "dist_ready", lambda *a: False)
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(fe, "interactive", lambda: False)
    monkeypatch.setattr(fe, "_node_usable", lambda: False)
    ok, msg = fe.ensure_frontend()
    assert ok is False
    assert "非交互终端" in msg and "npm install" in msg and "npm run build" in msg


def test_ensure_frontend_explicit_no_build_says_so(monkeypatch, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    web.joinpath("package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fe, "dist_ready", lambda *a: False)
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(fe, "_node_usable", lambda: False)
    ok, msg = fe.ensure_frontend(auto=False)
    assert ok is False and "--no-build" in msg


def test_ensure_frontend_stops_when_node_install_fails(monkeypatch, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    web.joinpath("package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fe, "dist_ready", lambda *a: False)
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(fe, "_node_usable", lambda: False)
    monkeypatch.setattr(fe, "install_node", lambda: False)
    ok, msg = fe.ensure_frontend(auto=True)
    assert ok is False and "Node.js" in msg


def test_ensure_frontend_stops_when_deps_install_fails(monkeypatch, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    web.joinpath("package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fe, "dist_ready", lambda *a: False)
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(fe, "_node_usable", lambda: True)
    monkeypatch.setattr(fe, "deps_installed", lambda *a: False)
    monkeypatch.setattr(fe, "install_deps", lambda *a: False)
    ok, msg = fe.ensure_frontend(auto=True)
    assert ok is False and "依赖自动安装失败" in msg


def test_ensure_frontend_non_interactive_with_deps_gives_build_hint(monkeypatch, tmp_path):
    """Node + 依赖都在、只差 dist：非交互仍不构建，指引里必须含 build 命令。"""
    web = tmp_path / "web"
    (web / "node_modules").mkdir(parents=True)
    web.joinpath("package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fe, "dist_ready", lambda *a: False)
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(fe, "interactive", lambda: False)
    monkeypatch.setattr(fe, "_node_usable", lambda: True)
    ok, msg = fe.ensure_frontend()
    assert ok is False and "未构建" in msg and "npm run build" in msg


def test_ensure_frontend_full_auto_path(monkeypatch, tmp_path):
    """auto=True 且一切就绪：装依赖 → 构建 → 成功（无 npm 依赖，全打桩）。"""
    web = tmp_path / "web"
    web.mkdir()
    web.joinpath("package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fe, "dist_ready", lambda *a: False)
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(fe, "_node_usable", lambda: True)
    monkeypatch.setattr(fe, "deps_installed", lambda *a: True)
    monkeypatch.setattr(fe, "build", lambda *a: True)
    ok, msg = fe.ensure_frontend(auto=True)
    assert ok is True and "已自动就绪" in msg


def test_ensure_frontend_reports_build_failure(monkeypatch, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    web.joinpath("package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fe, "dist_ready", lambda *a: False)
    monkeypatch.setattr(fe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(fe, "_node_usable", lambda: True)
    monkeypatch.setattr(fe, "deps_installed", lambda *a: True)
    monkeypatch.setattr(fe, "build", lambda *a: False)
    ok, msg = fe.ensure_frontend(auto=True)
    assert ok is False and "自动构建失败" in msg


def test_manual_hint_mentions_all_three_steps():
    hint = fe.manual_hint()
    assert str(fe.NODE_MIN_MAJOR) in hint
    assert "npm install" in hint and "npm run build" in hint and "--no-build" in hint


# ---------------------------------------------------------------------------
# server.py：端口 / 主机 / dist 判定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [("", sv.WEBUI_DEFAULT_PORT), ("5180", 5180)])
def test_webui_port_valid_or_default(monkeypatch, raw, expected):
    monkeypatch.setenv("WEBUI_PORT", raw)
    assert sv.webui_port() == expected


@pytest.mark.parametrize("raw", ["abc", "4173.5", "0x10", " 4173 "])
def test_webui_port_invalid_falls_back_to_default(monkeypatch, raw):
    """非法端口回退默认：用户 .env 写错不该让 webui 起不来（或起在随机端口）。"""
    monkeypatch.setenv("WEBUI_PORT", raw)
    assert sv.webui_port() == sv.WEBUI_DEFAULT_PORT


def test_webui_host_defaults_to_loopback(monkeypatch):
    """缺省必须绑回环：管理 API 无细粒度鉴权，默认对公网开放等于裸奔。"""
    monkeypatch.delenv("WEBUI_HOST", raising=False)
    assert sv.webui_host() == "127.0.0.1"
    monkeypatch.setenv("WEBUI_HOST", "")
    assert sv.webui_host() == "127.0.0.1"


def test_webui_host_respects_env(monkeypatch):
    monkeypatch.setenv("WEBUI_HOST", "0.0.0.0")
    assert sv.webui_host() == "0.0.0.0"


def test_dist_dir_resolves_inside_project_root():
    """server.py 位于 src/modelctl/core/webui/，parents[4] 必须是仓库根。"""
    d = sv.dist_dir()
    assert d.name == "dist"
    assert d.parent.name != "webui"


def test_dist_ready_requires_index_html(tmp_path):
    assert sv.dist_ready(tmp_path) is False
    tmp_path.joinpath("index.html").write_text("<html></html>", encoding="utf-8")
    assert sv.dist_ready(tmp_path) is True


# ---------------------------------------------------------------------------
# mount_static —— SPA 兜底与 API 前缀护栏
# ---------------------------------------------------------------------------


@pytest.fixture()
def spa_app(monkeypatch, tmp_path):
    """造一个带 dist 产物 + 一个真实 API 路由的最小 app，再挂 SPA 兜底。"""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html><body>SPA</body></html>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(sv, "dist_dir", lambda: root)

    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.get("/admin/api/ping")
    async def _ping():
        return JSONResponse({"ok": True})

    assert sv.mount_static(app) is True
    return app


def test_mount_static_false_when_dist_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(sv, "dist_dir", lambda: tmp_path / "nope")
    app = FastAPI()
    assert sv.mount_static(app) is False


def test_spa_fallback_serves_index_for_deep_link(spa_app):
    """history 模式深链刷新必须回 index.html，否则 /models/qwen3.8 刷新 404。"""
    from fastapi.testclient import TestClient

    with TestClient(spa_app) as c:
        r = c.get("/models/qwen3.8")
        assert r.status_code == 200
        assert "SPA" in r.text


def test_spa_fallback_serves_root(spa_app):
    from fastapi.testclient import TestClient

    with TestClient(spa_app) as c:
        assert "SPA" in c.get("/").text


@pytest.mark.parametrize("prefix", ["v1", "admin", "docs", "openapi.json", "redoc", "health"])
def test_spa_fallback_never_swallows_api_prefixes(spa_app, prefix):
    """API/文档前缀未命中路由时须回 404 JSON。

    历史坑：兜底路由把 /v1/chat/completions 接住回 HTML，nginx 转发的请求
    表现为「客户端 JSON 解析失败」这种极难归因的错误。
    """
    from fastapi.testclient import TestClient

    with TestClient(spa_app) as c:
        r = c.get(f"/{prefix}/does-not-exist")
        assert r.status_code == 404, f"/{prefix}/* -> {r.status_code}"
        assert r.headers["content-type"].startswith("application/json")
        assert "<html" not in r.text.lower()


def test_registered_api_route_wins_over_spa_fallback(spa_app):
    from fastapi.testclient import TestClient

    with TestClient(spa_app) as c:
        assert c.get("/admin/api/ping").json() == {"ok": True}


def test_static_assets_are_served(spa_app):
    from fastapi.testclient import TestClient

    with TestClient(spa_app) as c:
        r = c.get("/assets/app.js")
        assert r.status_code == 200
        assert "console.log" in r.text


def test_mount_static_without_assets_dir_still_works(monkeypatch, tmp_path):
    """极简产物（只有 index.html、无 assets/）：不挂 /assets 也不能崩。"""
    root = tmp_path / "dist"
    root.mkdir()
    root.joinpath("index.html").write_text("<html>MIN</html>", encoding="utf-8")
    monkeypatch.setattr(sv, "dist_dir", lambda: root)
    app = FastAPI()
    assert sv.mount_static(app) is True
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        assert "MIN" in c.get("/anything").text


# ---------------------------------------------------------------------------
# start_cluster_background —— 后台线程编排
# ---------------------------------------------------------------------------


def test_cluster_background_skipped_for_solo(monkeypatch):
    """solo 角色零副作用：既不起 reconciler 也不起 Agent。"""
    monkeypatch.setattr("modelctl.core.cluster.config.is_worker", lambda: False)
    calls: list[str] = []
    monkeypatch.setattr(
        "modelctl.core.cluster.reconcile.start_reconciler_in_background",
        lambda: calls.append("reconciler"),
    )
    monkeypatch.setattr(
        "modelctl.core.cluster.agent.start_agent_in_background",
        lambda: calls.append("agent"),
    )
    assert sv.start_cluster_background() is False
    assert calls == []


def test_cluster_background_starts_reconciler_before_agent(monkeypatch):
    """顺序必须是 reconciler → Agent：反了会出现「Agent 已连上但本机无 reconciler」的空窗。"""
    monkeypatch.setattr("modelctl.core.cluster.config.is_worker", lambda: True)
    calls: list[str] = []
    monkeypatch.setattr(
        "modelctl.core.cluster.reconcile.start_reconciler_in_background",
        lambda: calls.append("reconciler"),
    )
    monkeypatch.setattr(
        "modelctl.core.cluster.agent.start_agent_in_background",
        lambda: calls.append("agent"),
    )
    assert sv.start_cluster_background() is True
    assert calls == ["reconciler", "agent"]


def test_cluster_background_failure_degrades_quietly(monkeypatch):
    """集群后台启动失败只降级告警，绝不阻断 Web UI（管理面必须可用）。"""
    monkeypatch.setattr("modelctl.core.cluster.config.is_worker", lambda: True)

    def _boom():
        raise RuntimeError("集群依赖缺失")

    monkeypatch.setattr("modelctl.core.cluster.reconcile.start_reconciler_in_background", _boom)
    assert sv.start_cluster_background() is False
