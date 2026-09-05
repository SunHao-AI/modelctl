#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_sync_writer.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : worker 侧快照落盘（原子写/幂等/漂移/剪枝/不可信输入拒绝）
# ===============================================================================

from pathlib import Path

import pytest
import yaml

from modelctl.core.cluster import profiles as P
from modelctl.core.cluster.sync import (
    GOALS_FILE,
    MARKER_FILE,
    apply_snapshot,
    managed_paths,
    read_state,
    scan_drift,
)

YAML_A = "port: 8101\napi_key: ${API_KEY}\n"


def _goal(profile="qwen", engine="vllm", *, text=YAML_A, intent="start", goal_id=None):
    sha = P.profile_sha(text)
    return {"goal_id": goal_id or f"{profile}@@w-1", "profile": profile, "engine": engine,
            "yaml": text, "sha": sha, "version": "v1", "intent": intent,
            "params": None, "env_overlay": None}


@pytest.fixture()
def dirs(tmp_path):
    models = tmp_path / "models"
    cache = tmp_path / "cache"
    cache.mkdir()
    return models, cache


def _apply(dirs, goals, revision="r1"):
    models, cache = dirs
    return apply_snapshot({"revision": revision, "goals": goals},
                          models_dir=models, cache_dir=cache, now=100.0)


def test_writes_verbatim_text_and_state_file(dirs):
    models, cache = dirs
    g = _goal()
    out = _apply(dirs, [g])
    path = models / "vllm" / "qwen.yaml"
    assert path.read_text(encoding="utf-8") == YAML_A        # 逐字节相同，无注入头
    assert out.written == ["qwen@@w-1"] and out.revision == "r1"
    state = read_state(cache)
    assert state["revision"] == "r1" and state["goals"][0]["sha"] == g["sha"]
    assert (cache / MARKER_FILE).is_file()


def test_second_apply_with_same_revision_is_noop(dirs):
    _apply(dirs, [_goal()])
    out = _apply(dirs, [_goal()], revision="r1")
    assert out.written == [] and out.skipped == []           # 同 revision 直接短路


def test_same_revision_short_circuit_even_if_snapshot_changed(dirs):
    """revision 是唯一的"要不要动手"判据：中心不会在 revision 不变时改内容。"""
    _apply(dirs, [_goal()])
    out = _apply(dirs, [_goal(text="port: 9999\n")], revision="r1")
    assert (dirs[0] / "vllm" / "qwen.yaml").read_text(encoding="utf-8") == YAML_A
    assert out.written == []


def test_changed_content_writes_and_keeps_master_backup(dirs):
    models, _cache = dirs
    _apply(dirs, [_goal()])
    out = _apply(dirs, [_goal(text="port: 8102\n")], revision="r2")
    assert out.written == ["qwen@@w-1"]
    assert (models / "vllm" / "qwen.yaml").read_text(encoding="utf-8") == "port: 8102\n"
    assert (models / "vllm" / "qwen.yaml.master").read_text(encoding="utf-8") == YAML_A


def test_identical_content_does_not_touch_master(dirs):
    """同内容重写（如中心改了别的字段导致 revision 变）不得刷备份。"""
    models, _ = dirs
    _apply(dirs, [_goal()])
    (models / "vllm" / "qwen.yaml").write_text("本地改过\n", encoding="utf-8")
    out = _apply(dirs, [_goal()], revision="r2")
    assert out.written == ["qwen@@w-1"]
    assert (models / "vllm" / "qwen.yaml").read_text(encoding="utf-8") == YAML_A


def test_absent_goal_gets_pruned(dirs):
    models, _ = dirs
    _apply(dirs, [_goal("a"), _goal("b")], revision="r1")
    out = _apply(dirs, [_goal("a")], revision="r2")
    assert out.pruned == ["b@@w-1"]
    assert not (models / "vllm" / "b.yaml").exists()
    assert (models / "vllm" / "a.yaml").is_file()


def test_pruned_goal_leaves_no_state_entry(dirs):
    _apply(dirs, [_goal("a"), _goal("b")], revision="r1")
    _apply(dirs, [_goal("a")], revision="r2")
    assert [g["goal_id"] for g in read_state(dirs[1])["goals"]] == ["a@@w-1"]


def test_drift_detected_when_local_file_modified(dirs):
    models, cache = dirs
    _apply(dirs, [_goal()])
    (models / "vllm" / "qwen.yaml").write_text("port: 1\n# 我手改了\n", encoding="utf-8")
    assert scan_drift(cache, models) == ["qwen@@w-1"]


def test_drift_detected_when_file_deleted(dirs):
    models, cache = dirs
    _apply(dirs, [_goal()])
    (models / "vllm" / "qwen.yaml").unlink()
    assert scan_drift(cache, models) == ["qwen@@w-1"]


def test_drift_reported_inside_apply(dirs):
    models, _ = dirs
    _apply(dirs, [_goal()], revision="r1")
    (models / "vllm" / "qwen.yaml").write_text("port: 1\n", encoding="utf-8")
    out = _apply(dirs, [_goal()], revision="r2")
    assert out.drift == ["qwen@@w-1"] and out.written == ["qwen@@w-1"]  # 声明式：改回中心版本


def test_read_state_tolerates_missing_and_corrupt(dirs):
    _models, cache = dirs
    assert read_state(cache) == {"revision": "", "goals": []}
    (cache / GOALS_FILE).write_text("{坏 json", encoding="utf-8")
    assert read_state(cache) == {"revision": "", "goals": []}


# ---------------- 不可信输入拒绝（中心→worker 是可写文件的通道）----------------
def test_rejects_unsafe_profile_name(dirs):
    out = _apply(dirs, [_goal(profile="../escape")])
    assert out.rejected == ["../escape@@w-1"]
    assert not list((dirs[0]).rglob("escape.yaml"))


def test_rejects_unknown_engine(dirs):
    out = _apply(dirs, [_goal(engine="rm -rf")])
    assert out.rejected and not (dirs[0] / "rm -rf").exists()


def test_rejects_oversize_yaml(dirs):
    out = _apply(dirs, [_goal(text="port: 1\npad: " + "x" * (P.MAX_YAML_BYTES + 1))])
    assert out.rejected == ["qwen@@w-1"]
    assert not (dirs[0] / "vllm" / "qwen.yaml").exists()


def test_rejects_unparseable_yaml(dirs):
    out = _apply(dirs, [_goal(text="a: [1,\n")])
    assert out.rejected == ["qwen@@w-1"]


def test_rejects_non_mapping_yaml(dirs):
    assert _apply(dirs, [_goal(text="- 1\n- 2\n")]).rejected == ["qwen@@w-1"]


def test_rejects_yaml_without_port(dirs):
    assert _apply(dirs, [_goal(text="engine: vllm\n")]).rejected == ["qwen@@w-1"]


def test_rejected_goal_excluded_from_state(dirs):
    out = _apply(dirs, [_goal("good"), _goal("bad", engine="evil")], revision="r9")
    ids = [g["goal_id"] for g in read_state(dirs[1])["goals"]]
    assert ids == ["good@@w-1"] and out.written == ["good@@w-1"]


def test_revision_change_writes_atomically_without_tmp_leftover(dirs, monkeypatch):
    models, _ = dirs
    boom = RuntimeError("disk full")

    real_replace = Path.replace

    def fake_replace(self, target):
        if str(self).endswith("qwen.yaml.tmp") and target.name == "qwen.yaml":
            raise boom
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fake_replace)
    with pytest.raises(RuntimeError):
        _apply(dirs, [_goal()])
    assert not (models / "vllm" / "qwen.yaml").exists()
    assert not (models / "vllm" / "qwen.yaml.tmp").exists()      # 失败不得留临时文件
    assert read_state(dirs[1])["goals"] == []                     # 失败不落 state（下轮重试）


def test_managed_paths_lists_written_files(dirs):
    _apply(dirs, [_goal()])
    assert managed_paths(dirs[1]) == [dirs[0] / "vllm" / "qwen.yaml"]


def test_written_file_is_loadable_by_repo_profile_loader(dirs, monkeypatch, tmp_path):
    """落盘产物必须能被既有 loader 读取（占位符由 worker 本地 .env 解析）。"""
    from modelctl.core.profile import load_profile

    _apply(dirs, [_goal()])
    monkeypatch.setenv("API_KEY", "sk-local")
    prof = load_profile("qwen", dirs[0])
    assert prof.port == 8101 and prof.api_key == "sk-local"
    assert yaml.safe_load((dirs[0] / "vllm" / "qwen.yaml").read_text(encoding="utf-8"))["port"] == 8101


# ---------------- fix round 1（task-8-review M-1/M-2/M-3 + 字节口径盲区）----------------
def test_rejected_update_keeps_previous_file(dirs):
    """被拒 ≠ 撤销：同 goal 的坏更新（sha 截断）被拒时必须维持上轮文件与 state 登记。"""
    models, cache = dirs
    _apply(dirs, [_goal()], revision="r1")
    bad = _goal()
    bad["sha"] = "0" * 64                                   # 模拟传输截断：sha 与 yaml 不符
    out = _apply(dirs, [bad], revision="r2")
    assert out.rejected == ["qwen@@w-1"]
    assert (models / "vllm" / "qwen.yaml").read_text(encoding="utf-8") == YAML_A
    entries = {g["goal_id"]: g for g in read_state(cache)["goals"]}
    assert entries["qwen@@w-1"]["sha"] == P.profile_sha(YAML_A)  # state 保留上轮正确 sha
    assert scan_drift(cache, models) == []                        # 不误报漂移


def test_non_utf8_existing_file_rewritten_and_drift_survives(dirs):
    """既有文件被外部编辑器改成非 UTF-8：apply 不崩并覆盖回中心版本；scan_drift 计漂移。"""
    models, cache = dirs
    _apply(dirs, [_goal()], revision="r1")
    target = models / "vllm" / "qwen.yaml"
    target.write_bytes("端口：8101\n".encode("gbk"))
    out = _apply(dirs, [_goal()], revision="r2")            # 读不动按"无备份"覆盖 → 自愈
    assert out.written == ["qwen@@w-1"]
    assert target.read_text(encoding="utf-8") == YAML_A
    target.write_bytes("端口：8101\n".encode("gbk"))        # 再改坏：scan_drift 别崩心跳
    assert scan_drift(cache, models) == ["qwen@@w-1"]


def test_rejects_deeply_nested_yaml(dirs):
    """深嵌套 flow 结构击穿递归上限抛 RecursionError（非 YAMLError）→ 拒该条不炸 apply。"""
    out = _apply(dirs, [_goal(text="a: " + "[" * 4000)])
    assert out.rejected == ["qwen@@w-1"]


def test_oversize_yaml_measured_in_utf8_bytes(dirs):
    """尺寸按 UTF-8 字节口径：中文字符数 < 上限但字节数 > 上限，同样必须拒。"""
    text = "port: 1\npad: " + "汉" * (P.MAX_YAML_BYTES // 3 + 10)
    assert len(text) < P.MAX_YAML_BYTES                     # 按 len() 算"没超限"的口径漏洞
    assert _apply(dirs, [_goal(text=text)]).rejected == ["qwen@@w-1"]
