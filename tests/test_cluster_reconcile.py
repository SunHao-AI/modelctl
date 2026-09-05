#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_reconcile.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 11:00
# @Desc   : worker 侧 reconciler（8 态 stage / 错误分类 / 手动优先 / 心跳快照）
# ===============================================================================

import os

import pytest

from modelctl.core.capabilities import Capabilities
from modelctl.core.cluster import config, reconcile, sync
from modelctl.core.cluster import profiles as P
from modelctl.core.cluster.reconcile import (
    DEGRADED,
    FAILED,
    PENDING_PROFILE_SYNC,
    PROFILE_SYNCED,
    READY,
    STOPPED,
    Outcome,
    Reconciler,
    classify_error,
    local_profile_paths,
    profile_sha_safe,
    runtime_readiness,
)

YAML_A = "port: 8101\napi_key: ${API_KEY}\n"
YAML_B = "port: 8102\napi_key: ${API_KEY}\n"

_CAPS = Capabilities(gpu_count=4, gpu_indices=[0, 1, 2, 3],
                     vram_total_mb_per_gpu=[40960] * 4)


@pytest.fixture(autouse=True)
def _assume_venvs_ready(monkeypatch):
    """绝大多数用例只关心状态机，不该被"本机没建 vllm venv / 缺 API_KEY"绊住。

    `has_env` 必须 `setattr` 到 **reconcile 命名空间**：reconcile 在模块顶层
    `from core.envs import has_env` 已绑定函数对象，改 envs 里的同名属性无效。
    `API_KEY` 是必须的：`load_profile_at` 走 `_interpolate`，profile YAML 里的
    `${API_KEY}` 缺变量会抛 ProfileError，让所有状态机用例统一挂在"解析失败"上。
    """
    monkeypatch.setattr(reconcile, "has_env", lambda target: True)
    monkeypatch.setenv("API_KEY", "sk-test-local")


# --------------------------------------------------------------------------- 纯函数

@pytest.mark.parametrize("detail,expected", [
    ("[gpu_lock] GPU 0 已被 deepseek 占用", "gpu_lock"),
    ("gpu_list 指定的卡位不存在", "profile_invalid"),          # 早于 gpu_lock 规则
    ("端口 8101 已被占用（nginx:80）", "port_conflict"),
    ("vllm 专用环境未创建，执行 modelctl env setup vllm", "venv_missing"),
    ("引擎 ollama 的二进制在 PATH 中找不到", "venv_missing"),
    ("llamacpp 未安装", "venv_missing"),
    ("显存不足：需要 80GiB，实际 40GiB", "oom"),
    ("CUDA out of memory", "oom"),
    ("CUDA error: device-side assert", "oom"),
    ("模型下载失败（网络不可达）", "model_download_failed"),
    ("模型下载超时", "model_download_failed"),
    ("profile 必填字段 model 缺失", "profile_invalid"),
    ("port 必须是整数", "profile_invalid"),
    ("max_model_len 超过实际显存", "runtime_capability"),
    ("该量化格式不支持当前 GPU 计算能力", "runtime_capability"),
    # 顺序守卫：两条规则同时命中时，先声明者胜（现有单命中串对调任意两规则均
    # 不可红，只有多命中串才能把 _ERROR_RULES 的顺序敏感真正钉死）
    ("[gpu_lock] 冲突：gpu_list 指定的卡位已被占用", "gpu_lock"),
    ("端口 8101 已被占用，无法完成必填校验", "port_conflict"),
])
def test_classify_error_rules_in_priority_order(detail, expected):
    assert classify_error(detail) == expected


def test_classify_error_falls_back_to_runtime_capability():
    assert classify_error("") == "runtime_capability"
    assert classify_error("完全没见过的报错") == "runtime_capability"


def test_runtime_readiness_managed_engine_with_venv():
    ok, reason, cls = runtime_readiness("vllm", {}, _CAPS)
    assert ok is True and reason == "" and cls == ""


def test_runtime_readiness_docker_image_skips_venv(monkeypatch):
    """docker runtime 不需要 venv：免检路径必须真实可达，否则是死代码。"""
    monkeypatch.setattr(reconcile, "has_env", lambda target: False)
    ok, _reason, _cls = runtime_readiness("vllm", {"docker_image": "vllm/vllm:latest"}, _CAPS)
    assert ok is True


def test_runtime_readiness_managed_engine_without_venv(monkeypatch):
    monkeypatch.setattr(reconcile, "has_env", lambda target: False)
    ok, reason, cls = runtime_readiness("vllm", {}, _CAPS)
    assert ok is False and cls == "venv_missing" and "env setup" in reason


def test_runtime_readiness_unmanaged_engine_uses_binary_probe():
    caps = Capabilities(binaries={"ollama": False})
    ok, _reason, cls = runtime_readiness("ollama", {}, caps)
    assert ok is False and cls == "venv_missing"
    assert runtime_readiness("ollama", {}, Capabilities(binaries={"ollama": True}))[0] is True


def test_local_profile_paths_skips_sidecars(tmp_path):
    (tmp_path / "vllm").mkdir()
    (tmp_path / "ollama").mkdir()
    (tmp_path / "vllm" / "qwen.yaml").write_text(YAML_A, encoding="utf-8")
    (tmp_path / "vllm" / "qwen.yaml.master").write_text(YAML_B, encoding="utf-8")
    (tmp_path / "vllm" / ".qwen.yaml.tmp").write_text(YAML_B, encoding="utf-8")
    (tmp_path / "ollama" / "deepseek.yaml").write_text(YAML_B, encoding="utf-8")
    got = local_profile_paths(tmp_path)
    assert sorted(got) == ["deepseek", "qwen"]
    assert got["qwen"] == tmp_path / "vllm" / "qwen.yaml"


def test_local_profile_paths_stem_collision_is_deterministic(tmp_path):
    for engine in ("ollama", "vllm"):
        (tmp_path / engine).mkdir()
        (tmp_path / engine / "dup.yaml").write_text(YAML_A, encoding="utf-8")
    assert local_profile_paths(tmp_path)["dup"] == tmp_path / "ollama" / "dup.yaml"


def test_profile_sha_safe_missing_file_returns_empty(tmp_path):
    assert profile_sha_safe(tmp_path / "nope.yaml") == ""
    p = tmp_path / "x.yaml"
    p.write_text(YAML_A, encoding="utf-8")
    assert profile_sha_safe(p) == P.profile_sha(YAML_A)


# --------------------------------------------------------------------------- 替身与夹具

class Recorder:
    """起停替身：共享一份运行态，让 start/stop 真的翻转 up/alive。"""

    def __init__(self) -> None:
        self.up: dict[str, bool] = {}
        self.dead: set[str] = set()
        self.starts: list[str] = []
        self.stops: list[str] = []
        self.start_detail = ""
        self.stop_detail = ""

    def start(self, prof, caps, timeout):
        self.starts.append(prof.name)
        if self.start_detail:
            return Outcome("error", self.start_detail)
        self.dead.discard(prof.name)
        self.up[prof.name] = True
        return Outcome("ok", f"http://127.0.0.1:{prof.port}")

    def stop(self, prof, caps, models_dir):
        self.stops.append(prof.name)
        if self.stop_detail:
            return Outcome("error", self.stop_detail)
        self.up[prof.name] = False
        self.dead.add(prof.name)       # 真实 stop 会带走进程：alive 必须同步翻转
        return Outcome("ok", "已停止")

    def probe(self, prof):
        alive = prof.name not in self.dead
        return {"up": bool(self.up.get(prof.name)), "alive": alive, "port": prof.port,
                "pid": 4242 if alive else None, "gpus": [0] if self.up.get(prof.name) else [],
                "name": prof.name}


def _goal(profile="qwen", engine="vllm", *, text=YAML_A, intent="start", goal_id=None,
          env_overlay=None):
    return {"goal_id": goal_id or f"{profile}@@w-1", "profile": profile, "engine": engine,
            "yaml": text, "sha": P.profile_sha(text), "version": "v1", "intent": intent,
            "params": None, "env_overlay": env_overlay}


def _snap(*goals, revision="rev-1"):
    return {"revision": revision, "goals": list(goals)}


@pytest.fixture()
def dirs(tmp_path):
    models, cache = tmp_path / "models", tmp_path / "cache"
    cache.mkdir()
    return models, cache


def _rt(dirs, rec=None):
    models, cache = dirs
    rec = rec or Recorder()
    rt = Reconciler(models_dir=models, cache_dir=cache, starter=rec.start,
                    stopper=rec.stop, prober=rec.probe, caps=_CAPS)
    return rt, rec


# --------------------------------------------------------------------------- 状态机配方

def test_fresh_goal_goes_start_to_ready(dirs):
    rt, rec = _rt(dirs)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    assert rec.starts == ["qwen"]
    got = rt.snapshot()["profiles"]["qwen"]
    assert got["stage"] == READY and got["state"] == "running" and got["managed"] is True


def test_snapshot_missing_file_waits_for_sync(dirs):
    rt, rec = _rt(dirs)
    rt.reconcile_once(now=1.0)
    assert rec.starts == []


def test_start_failure_is_terminal_and_classified(dirs):
    rt, rec = _rt(dirs)
    rec.start_detail = "端口 8101 已被占用（nginx:80）"
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    first = rt.snapshot()["profiles"]["qwen"]
    assert first["stage"] == FAILED and first["error_class"] == "port_conflict"
    rt.reconcile_once(now=3.0)
    assert rec.starts == ["qwen"]                       # 不自动重试
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == FAILED


def test_retry_action_resets_failed_goal(dirs):
    rt, rec = _rt(dirs)
    rec.start_detail = "显存不足：需要 80GiB"
    g = _goal()
    rt.apply_snapshot(_snap(g), now=1.0)
    rt.reconcile_once(now=2.0)
    assert rt.snapshot()["profiles"]["qwen"]["error_class"] == "oom"
    rec.start_detail = ""
    rt.handle_actions([{"seq": 7, "action": "retry", "goal_id": g["goal_id"],
                        "profile": "qwen"}], now=3.0)
    out = list(rt.flush_results())
    assert out[0]["ok"] is True and out[0]["seq"] == 7
    rt.reconcile_once(now=4.0)
    assert rec.starts == ["qwen", "qwen"]
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == READY


def test_intent_stop_stops_and_stays_stopped(dirs):
    rt, rec = _rt(dirs)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    rt.apply_snapshot(_snap(_goal(intent="stop")), now=3.0)
    rt.reconcile_once(now=4.0)
    rt.reconcile_once(now=5.0)
    assert rec.stops == ["qwen"]
    assert rec.starts == ["qwen"]                       # 没有被 intent 再拉起来
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == STOPPED


def test_manual_stop_survives_intent_start(dirs):
    """手动 stop 压过声明式 intent=start，且位不清（清了就复活）。"""
    rt, rec = _rt(dirs)
    g = _goal()
    rt.apply_snapshot(_snap(g), now=1.0)
    rt.reconcile_once(now=2.0)
    rt.handle_actions([{"seq": 1, "action": "stop", "goal_id": g["goal_id"],
                        "profile": "qwen"}], now=3.0)
    rt.reconcile_once(now=4.0)
    assert rec.stops == ["qwen"]
    rt.reconcile_once(now=5.0)
    rt.reconcile_once(now=6.0)
    assert rec.starts == ["qwen"]                       # 关键：手动位仍在，未被重新拉起
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == STOPPED


def test_manual_stop_is_cleared_by_goal_change(dirs):
    rt, rec = _rt(dirs)
    g = _goal()
    rt.apply_snapshot(_snap(g), now=1.0)
    rt.reconcile_once(now=2.0)
    rt.handle_actions([{"seq": 1, "action": "stop", "goal_id": g["goal_id"],
                        "profile": "qwen"}], now=3.0)
    rt.reconcile_once(now=4.0)
    # 中心改了 YAML（sha 变）→ 视为"中心重新表达了一次意图"，手动位失效
    rt.apply_snapshot(_snap(_goal(text=YAML_B)), now=5.0)
    rt.reconcile_once(now=6.0)
    assert rec.starts == ["qwen", "qwen"]
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == READY


def test_restart_action_stops_once_and_intent_pulls_back(dirs):
    rt, rec = _rt(dirs)
    g = _goal()
    rt.apply_snapshot(_snap(g), now=1.0)
    rt.reconcile_once(now=2.0)
    rt.handle_actions([{"seq": 1, "action": "restart", "goal_id": g["goal_id"],
                        "profile": "qwen"}], now=3.0)
    rt.reconcile_once(now=4.0)
    assert rec.stops == ["qwen"]
    # restart 本轮只负责停：manual 已清，但"拉起"发生在下一拍（一拍只做一件事）
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == STOPPED
    rt.reconcile_once(now=5.0)
    assert rec.starts == ["qwen", "qwen"]
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == READY


def test_unknown_action_or_goal_rejected_in_result(dirs):
    rt, rec = _rt(dirs)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.handle_actions([
        {"seq": 1, "action": "destroy", "goal_id": "qwen@@w-1", "profile": "qwen"},
        {"seq": 2, "action": "stop", "goal_id": "ghost@@w-1", "profile": "ghost"},
    ], now=2.0)
    out = list(rt.flush_results())
    assert [r["ok"] for r in out] == [False, False]
    assert list(rt.flush_results()) == []               # 已取走，不重复回传
    rt.reconcile_once(now=3.0)
    assert rec.stops == []                              # 被拒的指令绝不进执行队列


def test_health_lost_with_dead_process_fails_immediately(dirs):
    rt, rec = _rt(dirs)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    rec.dead.add("qwen")          # 进程不存在：健康检查必然同时失败
    rec.up["qwen"] = False
    rt.reconcile_once(now=3.0)
    got = rt.snapshot()["profiles"]["qwen"]
    assert got["stage"] == FAILED and got["error_class"] == "health_lost"
    assert rec.starts == ["qwen"]                          # 不自动重拉


def test_health_lost_with_live_process_degrades_then_fails(dirs):
    rt, rec = _rt(dirs)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    rec.up["qwen"] = False                              # 进程还在，只是 /health 不通
    rt.reconcile_once(now=3.0)
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == DEGRADED
    rt.reconcile_once(now=4.0)
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == DEGRADED
    rt.reconcile_once(now=5.0)                          # 第 DEGRADE_LIMIT 次 → 终态
    got = rt.snapshot()["profiles"]["qwen"]
    assert got["stage"] == FAILED and got["error_class"] == "health_lost"


def test_missing_venv_never_calls_starter(dirs, monkeypatch):
    monkeypatch.setattr(reconcile, "has_env", lambda target: False)
    rt, rec = _rt(dirs)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    assert rec.starts == []
    got = rt.snapshot()["profiles"]["qwen"]
    assert got["stage"] == FAILED and got["error_class"] == "venv_missing"


def test_state_survives_process_restart(dirs):
    """手动位/终态跨进程存活：新实例首拍绝不能复活已停/已失败的 goal。"""
    rec = Recorder()
    rt, _ = _rt(dirs, rec)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    assert (dirs[1] / reconcile.STATE_FILE).is_file()
    rt2, _ = _rt(dirs, rec)        # 共享运行态：模型仍在服务
    rt2.reconcile_once(now=3.0)    # 未显式 _load：循环自己会从状态文件恢复
    assert rt2._recs["qwen@@w-1"]["stage"] == READY
    assert rec.starts == ["qwen"]  # 已 READY 的不重复起


def test_rejected_snapshot_goal_never_reaches_ready(dirs):
    rt, rec = _rt(dirs)
    bad = _goal(profile="../evil")
    rt.apply_snapshot(_snap(_goal(), bad), now=1.0)
    rt.reconcile_once(now=2.0)
    assert rec.starts == ["qwen"]
    assert "../evil" not in rt.snapshot()["profiles"]


# --------------------------------------------------------------------------- 心跳快照

def test_heartbeat_payload_shape(dirs):
    rt, _rec = _rt(dirs)
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    rt.reconcile_once(now=2.0)
    rt._local.setdefault("solo-local", {"goal_id": "", "stage": READY, "state": "running",
                                        "reason": "", "error_class": "", "managed": False,
                                        "port": 9999, "pid": 1, "gpu": [3], "at": 2.0})
    got = rt.heartbeat_payload()
    assert sorted(got) == ["capacity", "drift", "goal_sync", "local_profiles", "profiles",
                           "runtimes"]
    assert got["goal_sync"] == {"revision": "rev-1"}
    assert got["profiles"]["qwen"]["stage"] == READY
    assert got["profiles"]["solo-local"]["managed"] is False
    assert "qwen" in got["local_profiles"]
    assert set(got["runtimes"]) == {"llamacpp", "ollama", "vllm", "sglang", "unsloth",
                                    "aphrodite", "lmdeploy", "tensorrt_llm", "tokenspeed"}
    assert got["capacity"]["gpu_count"] == 4


def test_goal_without_rec_derives_stage_from_disk(dirs):
    """goal 已落盘但本进程还没跑过（worker 刚重启）→ stage 由磁盘 sha 推导，
    且绝不能在这一步顺手把进程起起来（要等 reconcile 循环正式拍）。"""
    models, cache = dirs
    g = _goal()
    sync.apply_snapshot(_snap(g), models_dir=models, cache_dir=cache, now=1.0)
    rt, rec = _rt(dirs)
    snap = rt.snapshot()
    assert snap["revision"] == "rev-1"
    assert snap["profiles"]["qwen"]["stage"] == PROFILE_SYNCED
    assert snap["profiles"]["qwen"]["state"] == "synced"
    assert rec.starts == []
    (models / "vllm" / "qwen.yaml").write_text("port: 7777\n", encoding="utf-8")
    assert rt.snapshot()["profiles"]["qwen"]["stage"] == PENDING_PROFILE_SYNC


def test_drift_reported_once_repaired_by_resync(dirs):
    rt, _rec = _rt(dirs)
    models, cache = dirs
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    (models / "vllm" / "qwen.yaml").write_text("port: 9999\n", encoding="utf-8")
    assert rt.snapshot()["drift"] == ["qwen@@w-1"]
    rt.apply_snapshot(_snap(_goal()), now=2.0)          # 同 revision 会被短路
    assert rt.snapshot()["drift"] == ["qwen@@w-1"]
    rt.apply_snapshot({"revision": "rev-2", "goals": [_goal()], "force": True}, now=3.0)
    assert rt.snapshot()["drift"] == []


# --------------------------------------------------------------------------- env_overlay

def test_env_overlay_applied_and_restored_exactly(dirs, monkeypatch):
    seen: dict[str, str | None] = {}
    rt, rec = _rt(dirs)
    g = _goal(env_overlay={"MODEL_ROOT": "/mnt/nas/models"})

    def spy_start(prof, caps, timeout):
        seen["MODEL_ROOT"] = os.environ.get("MODEL_ROOT")
        return rec.start(prof, caps, timeout)

    rt._starter = spy_start
    monkeypatch.setenv("MODEL_ROOT", "/original")
    rt.apply_snapshot(_snap(g), now=1.0)
    rt.reconcile_once(now=2.0)
    assert seen["MODEL_ROOT"] == "/mnt/nas/models"
    assert os.environ["MODEL_ROOT"] == "/original"      # 精确复原，不污染全局


def test_secret_keys_in_overlay_never_reach_environ(dirs, monkeypatch):
    """白名单外一律丢弃：整份 overlay 作废（宁可少给，不可多给）。"""
    seen: dict[str, str | None] = {}
    rt, rec = _rt(dirs)
    g = _goal(env_overlay={"MODEL_ROOT": "/m", "DEEPSEEK_API_KEY": "sk-leak"})

    def spy_start(prof, caps, timeout):
        seen["MODEL_ROOT"] = os.environ.get("MODEL_ROOT")
        seen["DEEPSEEK_API_KEY"] = os.environ.get("DEEPSEEK_API_KEY")
        return rec.start(prof, caps, timeout)

    rt._starter = spy_start
    monkeypatch.delenv("MODEL_ROOT", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    rt.apply_snapshot(_snap(g), now=1.0)
    rt.reconcile_once(now=2.0)
    assert seen == {"MODEL_ROOT": None, "DEEPSEEK_API_KEY": None}


# --------------------------------------------------------------------------- 投递路径与配置

def test_delivery_paths_do_not_take_the_lock(dirs):
    """中心宕机不起、心跳不停摆：起进程最长 300s 期间 Agent 线程投递 ack 必须返回。

    若 offer/handle 路径与 `_lock` 绑定，`t.join` 会一直等到 release.set()，本用例
    的第一个 `join(2)` 超时即失败——这正是"节点正在拉 70B 模型却被中心标 stale"的根因。
    """
    import threading

    rt, rec = _rt(dirs)
    started, release = threading.Event(), threading.Event()

    def slow_start(prof, caps, timeout):
        started.set()
        out = rec.start(prof, caps, timeout)
        release.wait(5)
        return out

    rt._starter = slow_start
    rt.apply_snapshot(_snap(_goal()), now=1.0)
    t = threading.Thread(target=rt.reconcile_once, kwargs={"now": 2.0})
    t.start()
    assert started.wait(5)
    rt.offer_snapshot(_snap(_goal(), revision="rev-2"))
    rt.handle_actions([{"seq": 1, "action": "retry", "goal_id": "qwen@@w-1",
                        "profile": "qwen"}], now=2.5)
    frames = list(rt.flush_results())
    assert frames and frames[0]["seq"] == 1                  # 投递真的被受理（非空判据）
    t.join(2)
    assert t.is_alive()                                      # 主线程从未被 reconcile 阻塞
    release.set()
    t.join(5)
    assert not t.is_alive()
    rt._drain_snapshot(now=3.0)                              # 新快照排在阻塞结束之后应用
    assert rt.snapshot()["revision"] == "rev-2"
    assert rec.starts == ["qwen"]                            # retry 未提前打断本轮


def test_reconcile_config_floors(monkeypatch):
    monkeypatch.delenv("CLUSTER_RECONCILE_INTERVAL_S", raising=False)
    monkeypatch.delenv("CLUSTER_START_TIMEOUT_S", raising=False)
    assert config.reconcile_interval_s() == 5
    assert config.start_timeout_s() == 300
    monkeypatch.setenv("CLUSTER_RECONCILE_INTERVAL_S", "0")
    monkeypatch.setenv("CLUSTER_START_TIMEOUT_S", "1")
    assert config.reconcile_interval_s() == 5            # 低于 floor 回退默认
    assert config.start_timeout_s() == 300


def test_local_only_profiles_are_reported_unmanaged(dirs):
    """本地手起的模型（无 goal）也必须进心跳，否则中心会以为节点空转。"""
    models, cache = dirs
    (models / "ollama").mkdir(parents=True)
    (models / "ollama" / "local.yaml").write_text(YAML_B, encoding="utf-8")
    rt, rec = _rt(dirs)
    rt._prober = lambda prof: {"up": True, "alive": True, "port": prof.port, "pid": 7,
                               "gpus": [], "name": prof.name}
    rt.reconcile_once(now=1.0)
    got = rt.snapshot()["profiles"]["local"]
    assert got["managed"] is False and got["stage"] == READY
    assert rec.starts == []                              # 未托管的绝不代客起停