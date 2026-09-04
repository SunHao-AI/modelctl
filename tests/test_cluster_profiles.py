#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_profiles.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : cluster/profiles.py 中心侧 profile 源读取（原文/sha/路径安全）
# ===============================================================================

import itertools
from pathlib import Path

import pytest

from modelctl.core.cluster import profiles as P
from modelctl.core.profile import ProfileError, load_profile


@pytest.fixture()
def models(tmp_path):
    (tmp_path / "vllm").mkdir(parents=True)
    (tmp_path / "vllm" / "qwen.yaml").write_text(
        "port: 8101\napi_key: ${API_KEY}\nengine_config:\n  model: Qwen/Qwen3-8B\n", encoding="utf-8")
    (tmp_path / "vllm" / "noport.yaml").write_text("engine: vllm\n", encoding="utf-8")
    (tmp_path / "vllm" / "badyaml.yaml").write_text("a: [1,\n", encoding="utf-8")
    (tmp_path / "evil").mkdir()
    (tmp_path / "evil" / "x.yaml").write_text("port: 1\n", encoding="utf-8")
    return tmp_path


def test_is_safe_name_rejects_traversal_and_cjk():
    assert P.is_safe_name("qwen3.8-vllm") and P.is_safe_name("a_b-1.2")
    for bad in ("../etc/passwd", "a/b", "a\\b", "", "-lead", "x" * 65, "中文", "a b", ".hidden"):
        assert not P.is_safe_name(bad), bad


def test_sha_prefixed_stable_and_distinguishing():
    a = P.profile_sha("port: 1\n")
    assert a.startswith("sha256:") and len(a) == 71
    assert a == P.profile_sha("port: 1\n") and a != P.profile_sha("port: 2\n")


def test_default_version_is_date_plus_sha_prefix():
    sha = P.profile_sha("x")
    ver = P.default_profile_version(sha)
    assert len(ver.split("-")[0]) == 4 and ver.endswith(sha[7:13])


def test_read_keeps_raw_text_and_never_interpolates(models):
    got = P.read_profile_source("qwen", models)
    assert got["ok"] is True and got["engine"] == "vllm"
    assert "${API_KEY}" in got["yaml"]          # 原文下发，中心绝不插值
    assert got["raw"]["engine_config"]["model"] == "Qwen/Qwen3-8B"  # 未插值的解析结果
    assert got["sha"] == P.profile_sha(got["yaml"])


def test_engine_from_explicit_yaml_field_wins(models):
    (models / "vllm" / "expl.yaml").write_text("port: 1\nengine: sglang\n", encoding="utf-8")
    assert P.read_profile_source("expl", models)["engine"] == "sglang"


def test_missing_profile_reports_hint(models):
    got = P.read_profile_source("nope", models)
    assert got["ok"] is False and "不存在" in got["reason"]


def test_unsafe_name_never_touches_fs(models):
    got = P.read_profile_source("../escape", models)
    assert got["ok"] is False and "非法" in got["reason"]


def test_requires_port_mapping_and_valid_yaml(models):
    assert P.read_profile_source("noport", models)["ok"] is False
    assert "YAML" in P.read_profile_source("badyaml", models)["reason"]


def test_engine_must_be_known(tmp_path):
    (tmp_path / "evil").mkdir()
    (tmp_path / "evil" / "qwen.yaml").write_text("port: 1\n", encoding="utf-8")
    got = P.read_profile_source("qwen", tmp_path)
    assert got["ok"] is False and "engine" in got["reason"]


def test_size_cap_rejects_huge_file(tmp_path):
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "big.yaml").write_text(
        "port: 1\npad: " + "x" * (P.MAX_YAML_BYTES + 10), encoding="utf-8")
    assert P.read_profile_source("big", tmp_path)["ok"] is False


def test_missing_models_dir_is_not_crash(tmp_path):
    assert P.read_profile_source("qwen", tmp_path / "absent")["ok"] is False


def test_find_profile_path_returns_none_for_root_file_without_engine(tmp_path):
    """根目录 YAML 且无显式 engine：engine 决定 worker 写盘子目录，宁可拒发也不猜。"""
    (tmp_path / "loose.yaml").write_text("port: 1\n", encoding="utf-8")
    assert P.find_profile_path("loose", tmp_path) is None


# ---------------- fix round 1：port 范围 / 同名歧义 / 绝不抛异常 ----------------

@pytest.mark.parametrize("bad", ["notanumber", "0", "99999", "-1", "[]"])
def test_port_must_be_int_in_range(tmp_path, bad):
    """中心必须按 core.profile 的同一口径拦掉非法 port：worker 只校验"有没有 port"，
    放行的话坏值会落到磁盘上，直到引擎启动/load_profile 才炸，排查成本极高。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "p.yaml").write_text(f"port: {bad}\n", encoding="utf-8")
    got = P.read_profile_source("p", tmp_path)
    assert got["ok"] is False and "port" in got["reason"], got


def test_port_accepts_numeric_string(tmp_path):
    """`port: "8101"` 是 YAML 字符串但 int() 可转，core.profile 同样放行——不能误拒。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "p.yaml").write_text('port: "8101"\n', encoding="utf-8")
    assert P.read_profile_source("p", tmp_path)["ok"] is True


def test_same_stem_across_engines_is_refused_not_guessed(tmp_path):
    """同名文件散落在多个引擎子目录（本仓 qwen3.8.yaml 有 8 份）：静默取排序首个会把
    模型下发到错误引擎，必须拒发并列出候选，让调用方改用可寻址的唯一名。"""
    for eng in ("aphrodite", "vllm", "sglang"):
        (tmp_path / eng).mkdir()
        (tmp_path / eng / "qwen.yaml").write_text("port: 1\n", encoding="utf-8")
    got = P.read_profile_source("qwen", tmp_path)
    assert got["ok"] is False
    assert "歧义" in got["reason"] and "aphrodite" in got["reason"] and "vllm" in got["reason"]


def test_same_stem_same_engine_root_file_wins(tmp_path):
    """同引擎的重名不是歧义（core.profile.load_profile 同样根目录优先，行为保持一致）。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "qwen.yaml").write_text("port: 9\nengine: vllm\n", encoding="utf-8")
    (tmp_path / "vllm" / "qwen.yaml").write_text("port: 1\n", encoding="utf-8")
    got = P.read_profile_source("qwen", tmp_path)
    assert got["ok"] is True and got["engine"] == "vllm" and got["raw"]["port"] == 9


def test_unknown_engine_file_does_not_make_viable_one_ambiguous(tmp_path):
    """engine 不可判定的同名文件不该把可用那份一起拖成"歧义"——它本就该被忽略。"""
    (tmp_path / "evil").mkdir()
    (tmp_path / "evil" / "qwen.yaml").write_text("port: 1\n", encoding="utf-8")
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "qwen.yaml").write_text("port: 1\n", encoding="utf-8")
    got = P.read_profile_source("qwen", tmp_path)
    assert got["ok"] is True and got["engine"] == "vllm"


def test_engine_argument_breaks_the_ambiguity(tmp_path):
    """歧义不是死路：调用方（Task 12 的 `--engine`）显式选边即可，选错引擎名则报可用引擎。"""
    for eng in ("aphrodite", "vllm"):
        (tmp_path / eng).mkdir()
        (tmp_path / eng / "qwen.yaml").write_text(f"port: {1 if eng == 'vllm' else 2}\n", encoding="utf-8")
    got = P.read_profile_source("qwen", tmp_path, engine="VLLM")
    assert got["ok"] is True and got["engine"] == "vllm" and got["raw"]["port"] == 1
    bad = P.read_profile_source("qwen", tmp_path, engine="sglang")
    assert bad["ok"] is False and "sglang" in bad["reason"] and "aphrodite" in bad["reason"]
    assert P.find_profile_path("qwen", tmp_path, engine="vllm") == (Path(got["path"]), "vllm")


def test_non_utf8_file_never_raises(tmp_path):
    """契约"绝不抛异常"：UTF-16/二进制残留会让 read_text 抛 UnicodeDecodeError
    （ValueError 子类，不在 OSError/YAMLError 内），Task 5 未包 try → REST 500。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "bin.yaml").write_bytes(b"\xff\xfeport: 1\n")
    got = P.read_profile_source("bin", tmp_path)
    assert got["ok"] is False and "UTF-8" in got["reason"]


def test_oversize_file_rejected_without_reading_it(tmp_path, monkeypatch):
    """尺寸上限必须在读盘**前**用 stat 判定，否则 10 GB 的 YAML 会先整份进内存。"""
    (tmp_path / "vllm").mkdir()
    big = tmp_path / "vllm" / "big.yaml"
    big.write_text("port: 1\npad: " + "x" * (P.MAX_YAML_BYTES + 10), encoding="utf-8")
    def boom(*a, **k):
        raise AssertionError("超尺寸文件不应被整份读取")

    monkeypatch.setattr(P.Path, "read_text", boom)
    got = P.read_profile_source("big", tmp_path)
    assert got["ok"] is False and "过大" in got["reason"]


def test_find_profile_path_agrees_with_read(tmp_path):
    """两个入口共用同一套定位/校验：否则 Task 5 用 read、worker 侧用 find 会各判一次。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "qwen.yaml").write_text("port: 1\n", encoding="utf-8")
    got = P.read_profile_source("qwen", tmp_path)
    assert P.find_profile_path("qwen", tmp_path) == (Path(got["path"]), got["engine"])
    (tmp_path / "sglang").mkdir()
    (tmp_path / "sglang" / "qwen.yaml").write_text("port: 2\n", encoding="utf-8")
    assert P.find_profile_path("qwen", tmp_path) is None


# ---------------- fix round 1（条款④）：展示名回退寻址 ----------------

def test_display_name_fallback_returns_file_stem(tmp_path):
    """用户视角的名字是展示名（core.profile 口径 {group}-{engine}），goal 内部必须是文件名：
    中心/worker 同路径同内容，sha 漂移检测才成立。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "qwen.yaml").write_text("port: 1\n", encoding="utf-8")
    got = P.read_profile_source("qwen-vllm", tmp_path)  # 无 qwen-vllm.yaml，按展示名命中 qwen.yaml
    assert got["ok"] is True and got["name"] == "qwen" and got["display_name"] == "qwen-vllm"
    assert P.find_profile_path("qwen-vllm", tmp_path) == (tmp_path / "vllm" / "qwen.yaml", "vllm")


def test_display_name_from_explicit_yaml_field(tmp_path):
    """YAML 显式 name: 优先于自动拼接（与 _to_profile 同口径，含 variant 拼接）。"""
    (tmp_path / "sglang").mkdir()
    (tmp_path / "sglang" / "llama-light.yaml").write_text("port: 1\nvariant: light\n", encoding="utf-8")
    got = P.read_profile_source("llama-sglang-light", tmp_path)
    assert got["ok"] is True and got["name"] == "llama-light"
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "misc.yaml").write_text("port: 2\nname: renamed\n", encoding="utf-8")
    assert P.read_profile_source("renamed", tmp_path)["name"] == "misc"


def test_filename_match_wins_over_display_name_match(tmp_path):
    """文件名与展示名同时可命中时文件名优先：寻址语义唯一，防止某天新增展示名撞名
    把用户明确指定的文件换掉。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "qwen-vllm.yaml").write_text("port: 1\n", encoding="utf-8")
    (tmp_path / "sglang").mkdir()
    (tmp_path / "sglang" / "other.yaml").write_text("port: 2\nname: qwen-vllm\n", encoding="utf-8")
    got = P.read_profile_source("qwen-vllm", tmp_path)
    assert got["ok"] is True and got["engine"] == "vllm" and got["raw"]["port"] == 1


def test_display_name_ambiguity_and_engine_pick(tmp_path):
    """展示名命中多个引擎候选 → 沿用文件名同款歧义规则，engine= 显式选边后 name=文件 stem。"""
    for eng, fname, port in (("vllm", "a", 1), ("sglang", "b", 2)):
        (tmp_path / eng).mkdir()
        (tmp_path / eng / f"{fname}.yaml").write_text(f"port: {port}\nname: shared\n", encoding="utf-8")
    got = P.read_profile_source("shared", tmp_path)
    assert got["ok"] is False and "歧义" in got["reason"]
    picked = P.read_profile_source("shared", tmp_path, engine="sglang")
    assert picked["ok"] is True and picked["name"] == "b" and picked["display_name"] == "shared"


def test_display_name_equal_filename_omits_key(tmp_path):
    """展示名与文件名一致时省略 display_name，避免下游回显 'qwen (qwen)'。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "explicit.yaml").write_text("port: 1\nname: explicit\n", encoding="utf-8")
    assert "display_name" not in P.read_profile_source("explicit", tmp_path)


def test_display_name_fallback_applies_same_validation(tmp_path):
    """回退扫描不能成为校验旁路：展示名指向坏 port 文件时按未命中处理，绝不放行。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "bad.yaml").write_text("port: 0\nname: wanted\n", encoding="utf-8")
    got = P.read_profile_source("wanted", tmp_path)
    assert got["ok"] is False and "不存在" in got["reason"]


# ---------------- fix round 2：回退 stem 写盘白名单 / 单次遍历去重 ----------------

def test_display_name_fallback_refuses_unsafe_file_stem(tmp_path):
    """展示名可以任意，但归一出的 stem 是 worker 侧 models/<engine>/ 的写盘文件名：
    CJK / 隐藏文件的 stem 过不了 worker 的 is_safe_name，中心 ok=True 等于下发一条
    worker 必拒、永远无法收敛的 goal（与 round 1 的坏 port 同属"中心放行 worker 必拒"）。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "千问.yaml").write_text("port: 1\nname: ascii-name\n", encoding="utf-8")
    (tmp_path / "vllm" / ".hidden.yaml").write_text("port: 2\nname: dotted\n", encoding="utf-8")
    for query, stem in (("ascii-name", "千问"), ("dotted", ".hidden")):
        got = P.read_profile_source(query, tmp_path)
        assert got["ok"] is False and "安全文件名" in got["reason"], (query, got)
        assert stem in got["reason"], got          # reason 必须点名是哪个文件，否则用户无从改名
        assert P.find_profile_path(query, tmp_path) is None


def test_display_name_fallback_still_picks_safe_sibling(tmp_path):
    """不安全 stem 只让该候选出局，不能把同展示名的安全候选一起拖掉（否则等于又造一种 404）。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "ollama").mkdir()
    (tmp_path / "vllm" / "qwen.yaml").write_text("port: 1\nname: shared\n", encoding="utf-8")
    (tmp_path / "ollama" / "千问.yaml").write_text("port: 2\nname: shared\n", encoding="utf-8")
    got = P.read_profile_source("shared", tmp_path)
    assert got["ok"] is True and got["name"] == "qwen" and got["engine"] == "vllm"


def test_root_file_listed_once_in_ambiguity_reason(tmp_path):
    """rglob 的 `**` 匹配零层目录：根目录那份同时在"根候选"与"递归结果"里，不去重会把
    同一文件解析两遍，歧义原因里还重复列同一行，误导用户以为有两个候选需要选边。"""
    (tmp_path / "qwen.yaml").write_text("port: 1\nengine: vllm\n", encoding="utf-8")
    (tmp_path / "sglang").mkdir()
    (tmp_path / "sglang" / "qwen.yaml").write_text("port: 2\n", encoding="utf-8")
    got = P.read_profile_source("qwen", tmp_path)
    assert got["ok"] is False and "歧义" in got["reason"]
    hits = got["reason"].split("（", 1)[1].split("）", 1)[0]     # 括号内即候选清单
    assert hits.count("qwen.yaml") == 2, hits                    # 根目录 1 + sglang 1


def test_one_directory_walk_per_read(tmp_path, monkeypatch):
    """一次读取只允许一次目录遍历：文件名与展示名共用同一份扫描结果；且 rglob 会匹配
    目录，扫描结果必须只剩文件（目录项进 _load_candidate 只是白跑一趟 stat+read）。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "qwen.yaml").write_text("port: 1\n", encoding="utf-8")
    (tmp_path / "vllm" / "other.yaml").write_text("port: 2\nname: display-name\n", encoding="utf-8")
    (tmp_path / "vllm" / "fake-dir.yaml").mkdir()
    calls: list[str] = []
    real_rglob = P.Path.rglob

    def spy(self, pattern):
        calls.append(pattern)
        return real_rglob(self, pattern)

    monkeypatch.setattr(P.Path, "rglob", spy)
    assert P.read_profile_source("qwen", tmp_path)["ok"] is True   # 文件名命中
    assert calls == ["*.yaml"], calls
    assert P.read_profile_source("display-name", tmp_path)["ok"] is True   # 展示名回退复用同一次遍历
    assert calls == ["*.yaml", "*.yaml"], calls
    assert P.read_profile_source("nope", tmp_path)["ok"] is False
    assert calls == ["*.yaml", "*.yaml", "*.yaml"], calls


# ---------------- fix round 3：显式 engine 大小写口径 / 回退根目录优先 ----------------

def test_explicit_engine_is_case_sensitive_like_core_profile(tmp_path):
    """显式 `engine: VLLM` 在本地 load_profile 是硬失败（_resolve_engine 不 lower），
    中心却 lower 后放行 → 落盘的文件在 worker 上 load 必炸"未知引擎"，goal 永不收敛。
    与 round 1/2 同类：中心比 worker 的权威口径宽松。父目录推断才允许大小写不敏感
    （core.profile 对 parent dir 做了 lower）。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "a.yaml").write_text("port: 1\nengine: VLLM\n", encoding="utf-8")
    got = P.read_profile_source("a", tmp_path)
    assert got["ok"] is False and "engine" in got["reason"], got
    assert "大小写" in got["reason"], got          # reason 必须可行动：只说"未知引擎"用户想不到是大小写
    assert P.find_profile_path("a", tmp_path) is None
    with pytest.raises(ProfileError):     # 同口径双证：本地加载同一文件同样失败
        load_profile("a", tmp_path)
    (tmp_path / "vllm" / "b.yaml").write_text("port: 2\nengine: vllm\n", encoding="utf-8")
    assert P.read_profile_source("b", tmp_path)["ok"] is True


def test_uppercase_parent_dir_still_resolves(tmp_path):
    """父目录名大写（`models/VLLM/`）：core.profile 对目录名 lower，中心必须同样放行，
    否则就是反向的口径分叉（本地能跑、中心拒发）。"""
    (tmp_path / "VLLM").mkdir()
    (tmp_path / "VLLM" / "q.yaml").write_text("port: 1\n", encoding="utf-8")
    assert P.read_profile_source("q", tmp_path)["engine"] == "vllm"


def test_display_name_fallback_prefers_root_file(tmp_path):
    """展示名回退必须与文件名寻址、与 core.profile.list_profiles 同为"根目录优先"：
    否则中心下发的是子目录那份，而 worker 上 load_profile(展示名) 解析到的是根目录那份
    —— 中心与 worker 各自的"同一个 profile"指向不同文件，sha 漂移检测彻底失真。"""
    (tmp_path / "aaa").mkdir()
    (tmp_path / "misc.yaml").write_text("port: 9\nengine: vllm\nname: shared\n", encoding="utf-8")
    (tmp_path / "aaa" / "other.yaml").write_text("port: 1\nengine: vllm\nname: shared\n", encoding="utf-8")
    got = P.read_profile_source("shared", tmp_path)
    assert got["ok"] is True and got["name"] == "misc" and got["raw"]["port"] == 9, got
    assert P.find_profile_path("shared", tmp_path) == (tmp_path / "misc.yaml", "vllm")
    assert load_profile("shared", tmp_path).port == 9   # 本地口径同样命中根目录那份


# ---------------- fix round 4：rglob 自身异常兜底 + fail-closed ----------------

def _rglob_fails(real_rglob, exc: BaseException, n: int = 0):
    """把 rglob 换成"吐 n 条命中后抛"（n=0 即首个 next() 就抛）。

    rglob 是惰性生成器，异常一律在 `sorted()` **消费期**抛出；n>0 用来模拟"遍历到一半
    才失败"（子目录 ACL 拒绝 / 符号链接成环只挡住其中一支），此时已 yield 的结果是半份。
    """

    def boom(self, pattern):
        yield from itertools.islice(real_rglob(self, pattern), n)
        raise exc

    return boom


def test_scan_failure_never_raises(tmp_path, monkeypatch):
    """契约"绝不抛异常"的最后一条缝隙：此前只兜住了**读单个文件**的异常，遍历本身
    没兜——子目录被设 ACL 拒绝、目录树超深都会让 rglob 抛出，Task 5 的 goal set 直接 500。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "qwen.yaml").write_text("port: 1\n", encoding="utf-8")
    monkeypatch.setattr(P.Path, "rglob", _rglob_fails(P.Path.rglob, PermissionError(13, "Permission denied")))
    got = P.read_profile_source("qwen", tmp_path)
    assert got["ok"] is False and "遍历失败" in got["reason"], got
    assert "PermissionError" in got["reason"], got      # reason 要能定位是哪类 IO 故障
    assert P.find_profile_path("qwen", tmp_path) is None


@pytest.mark.parametrize("exc", [RecursionError("maximum recursion depth exceeded"),
                                 ValueError("embedded null byte")])
def test_scan_non_oserror_never_raises(tmp_path, monkeypatch, exc):
    """遍历期的异常不止 OSError：超深目录树抛 RecursionError、路径无法编码/NUL 抛
    ValueError。按"想到的几种"列举兜底必然漏（与 round 1 的 UnicodeDecodeError 同族）。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "qwen.yaml").write_text("port: 1\n", encoding="utf-8")
    monkeypatch.setattr(P.Path, "rglob", _rglob_fails(P.Path.rglob, exc))
    got = P.read_profile_source("qwen", tmp_path)
    assert got["ok"] is False and "遍历失败" in got["reason"], got


def test_scan_error_during_iteration_is_caught(tmp_path, monkeypatch):
    """rglob 是**惰性生成器**：异常在 `sorted()` 消费时才抛，把 try 只包住
    `root.rglob(...)` 那次调用等于没兜。"""
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm" / "qwen.yaml").write_text("port: 1\n", encoding="utf-8")
    monkeypatch.setattr(P.Path, "rglob", _rglob_fails(P.Path.rglob, OSError(40, "Too many levels"), n=1))
    got = P.read_profile_source("qwen", tmp_path)
    assert got["ok"] is False and "遍历失败" in got["reason"], got


def test_partial_scan_never_downgrades_ambiguity_to_silent_pick(tmp_path, monkeypatch):
    """半份清单比报错更危险：遍历到一半失败若把已 yield 的结果当完整清单，就会漏掉
    另一个引擎下的同名文件 → "歧义拒发"被降级成"静默下发到 vllm"，正是本模块第一条
    硬约束禁止的"猜"。故失败必须 fail-closed（空清单 + reason）。"""
    (tmp_path / "qwen.yaml").write_text("port: 9\nengine: vllm\n", encoding="utf-8")
    (tmp_path / "sglang").mkdir()
    (tmp_path / "sglang" / "qwen.yaml").write_text("port: 2\n", encoding="utf-8")
    monkeypatch.setattr(P.Path, "rglob", _rglob_fails(P.Path.rglob, PermissionError(13, "denied"), n=1))
    got = P.read_profile_source("qwen", tmp_path)
    assert got["ok"] is False and "遍历失败" in got["reason"], got
    assert P.find_profile_path("qwen", tmp_path) is None
