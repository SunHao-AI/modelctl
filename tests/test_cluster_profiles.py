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

import pytest

from modelctl.core.cluster import profiles as P


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
