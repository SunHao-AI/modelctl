"""startup_progress 单测：pull 解析 / 模式表 / EMA / 快照 / 阶段序列。"""
from __future__ import annotations

import json

import pytest

from modelctl.core.startup_progress import PullParser, PullUpdate


def test_pull_parser_counts_layers():
    p = PullParser("img:tag")
    assert p.feed("latest: Pulling from vllm/vllm-openai") is None
    u1 = p.feed("aaa: Pulling fs layer")
    assert isinstance(u1, PullUpdate) and u1.total_layers == 1
    p.feed("bbb: Pulling fs layer")
    # 半层下载：512MB/1GB ≈ 0.5
    u = p.feed("aaa: Downloading 512MB/1GB")
    assert u is not None and 0.2 < u.pct < 0.3  # (0.5*0.5)/1... 两半层 → 半*0.5/2
    p.feed("aaa: Download complete")
    p.feed("aaa: Pull complete")
    u2 = p.feed("bbb: Pull complete")
    assert u2.pct == 1.0 and u2.done_layers == 2 and u2.total_layers == 2


def test_pull_parser_already_exists_counts_done():
    p = PullParser("img:tag")
    p.feed("aaa: Already exists")
    u = p.feed("bbb: Pull complete")
    assert u.done_layers == 2 and u.total_layers == 2 and u.pct == 1.0


def test_pull_parser_reset_clears_layers():
    p = PullParser("img:tag")
    p.feed("aaa: Pulling fs layer")
    p.feed("aaa: Pull complete")
    p.reset()
    u = p.feed("zzz: Pulling fs layer")
    assert u.done_layers == 0 and u.total_layers == 1


def test_pull_parser_bytes_units_and_extracting():
    p = PullParser("img")
    p.feed("aaa: Pulling fs layer")
    p.feed("aaa: Downloading 100kB/1MB")
    p.feed("aaa: Download complete")
    # extracting 半程：权重 0.5 + 0.5*0.5 = 0.75
    u = p.feed("aaa: Extracting 500kB/1MB")
    assert 0.7 < u.pct < 0.8


def test_pct_clamped_when_cur_exceeds_total():
    # docker 偶发 cur > total（如 1.5GB/1GB）：pct 必须夹紧在 [0,1]，否则下游 eta 为负
    p = PullParser("img")
    p.feed("aaa: Pulling fs layer")
    u = p.feed("aaa: Downloading 1.5GB/1GB")
    assert u is not None and 0.0 <= u.pct <= 1.0


def test_pct_monotonic_across_download_complete():
    # 单层完整生命周期逐行喂入：pct 序列必须单调不减（进度条不许倒退）
    p = PullParser("img")
    lines = [
        "aaa: Pulling fs layer",
        "aaa: Downloading 900MB/1GB",
        "aaa: Downloading 1GB/1GB",
        "aaa: Download complete",
        "aaa: Extracting 0bytes/1GB",
        "aaa: Extracting 1GB/1GB",
        "aaa: Pull complete",
    ]
    pcts = [u.pct for u in (p.feed(x) for x in lines)]
    assert all(a <= b for a, b in zip(pcts, pcts[1:], strict=False)), pcts


def test_timing_cold_start_returns_none(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    t = StartupTiming(path=tmp_path / "timing.json")
    assert t.eta("vllm", "prepare_env", 0.5) is None


def test_timing_record_then_eta_scales_with_pct(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    t = StartupTiming(path=tmp_path / "timing.json")
    t.record("vllm", "prepare_env", 100.0)
    # pct=0.5 → 剩余 50；pct=None → 全量 100
    assert t.eta("vllm", "prepare_env", 0.5) == 50
    assert t.eta("vllm", "prepare_env", None) == 100


def test_timing_persists_across_instances(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    p = tmp_path / "timing.json"
    StartupTiming(path=p).record("vllm", "loading", 200.0)
    assert StartupTiming(path=p).eta("vllm", "loading", 0.0) == 200


def test_timing_corrupt_file_survives(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    p = tmp_path / "timing.json"
    p.write_text("{ not json", encoding="utf-8")
    t = StartupTiming(path=p)
    assert t.eta("vllm", "loading", 0.5) is None
    t.record("vllm", "loading", 10.0)  # 损坏文件被忽略后仍可写
    assert t.eta("vllm", "loading", 0.5) == 5


@pytest.mark.parametrize("payload", [
    '{"vllm:loading": "oops"}',                      # 值非 dict → AttributeError
    '{"vllm:loading": {"ema_s": NaN, "n": 3}}',      # NaN → int() ValueError
    '{"vllm:loading": {"ema_s": Infinity, "n": 2}}', # 非有限
    '{"vllm:loading": {"n": 9}}',                    # 缺 ema_s → EMA 分支 KeyError
    '{"vllm:loading": {"ema_s": 5.0, "n": "3"}}',    # n 为字符串 → TypeError
])
def test_timing_malformed_records_never_raise(tmp_path, payload):
    # 结构畸形但 JSON 合法的记录：eta/record 均不得抛异常，坏记录被丢弃
    from modelctl.core.startup_progress import StartupTiming

    p = tmp_path / "timing.json"
    p.write_text(payload, encoding="utf-8")
    t = StartupTiming(path=p)
    assert t.eta("vllm", "loading", 0.5) is None
    t.record("vllm", "loading", 10.0)  # 坏记录被过滤后仍可正常记录
    assert t.eta("vllm", "loading", 0.5) == 5


def test_timing_invalid_utf8_file_survives(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    p = tmp_path / "timing.json"
    p.write_bytes(b'{"vllm:loading": {"ema_s": 1.0, "n": 1}} \xff\xfe')
    t = StartupTiming(path=p)  # UnicodeDecodeError 不得冒泡
    assert t.eta("vllm", "loading", 0.5) is None


def test_timing_record_ignores_bad_samples(tmp_path):
    # 非正/非有限耗时样本必须被忽略：既不产生 ETA，也不得污染已有基线
    from modelctl.core.startup_progress import StartupTiming

    p = tmp_path / "timing.json"
    t = StartupTiming(path=p)
    for bad in (-50.0, 0.0, float("nan"), float("inf")):
        t.record("vllm", "loading", bad)
        assert t.eta("vllm", "loading", 0.5) is None
    assert not p.exists()  # 全坏样本不应落盘

    t.record("vllm", "loading", 100.0)
    assert t.eta("vllm", "loading", 0.5) == 50
    t.record("vllm", "loading", -50.0)  # 坏样本不得污染基线
    assert t.eta("vllm", "loading", 0.5) == 50


def test_timing_ema_takes_over_after_min_samples(tmp_path):
    from modelctl.core.startup_progress import StartupTiming

    p = tmp_path / "timing.json"
    t = StartupTiming(path=p)
    for _ in range(5):
        t.record("vllm", "loading", 100.0)
    t.record("vllm", "loading", 550.0)
    # n≥5 后转 EMA(α=0.4)：0.4*550 + 0.6*100 = 280，且跨实例持久化
    assert StartupTiming(path=p).eta("vllm", "loading", 0.0) == 280
    assert json.loads(p.read_text(encoding="utf-8"))["vllm:loading"]["n"] == 6


def test_tracker_emits_and_writes_snapshot(tmp_path):
    from modelctl.core.startup_progress import STAGES, StartupTiming, StartupTracker

    events = []
    snap = tmp_path / "startup.json"
    tr = StartupTracker("q", "vllm", "docker", on_progress=events.append,
                        timing=StartupTiming(path=tmp_path / "t.json"),
                        snapshot_path=snap)
    tr.begin("preflight", "依赖检查")
    tr.done("preflight")
    tr.begin("prepare_env", "准备环境")
    tr.progress("prepare_env", "拉取镜像（3/9 层）", pct=0.4)
    assert events[0].stage == "preflight" and events[0].status == "running"
    assert events[-1].pct == 0.4
    data = json.loads(snap.read_text(encoding="utf-8"))
    assert data["runtime"] == "docker"
    assert [s["stage"] for s in data["stages"]] == list(STAGES)
    assert data["stages"][0]["status"] == "done"
    assert data["stages"][1]["pct"] == 0.4


def test_tracker_fail_marks_error(tmp_path):
    from modelctl.core.startup_progress import StartupTiming, StartupTracker

    events = []
    tr = StartupTracker("q", "vllm", "docker", on_progress=events.append,
                        timing=StartupTiming(path=tmp_path / "t.json"),
                        snapshot_path=tmp_path / "s.json")
    tr.begin("preflight", "依赖检查")
    tr.fail("preflight", "依赖检查", "docker 不在 PATH")
    assert events[-1].status == "error" and "docker" in events[-1].error


def test_tracker_eta_from_timing(tmp_path):
    from modelctl.core.startup_progress import StartupTiming, StartupTracker

    timing = StartupTiming(path=tmp_path / "t.json")
    timing.record("vllm", "loading", 200.0)
    events = []
    tr = StartupTracker("q", "vllm", "docker", on_progress=events.append,
                        timing=timing, snapshot_path=tmp_path / "s.json")
    tr.begin("loading", "加载模型", pct=0.5)
    assert events[-1].eta_s == 100  # 200*(1-0.5)


def test_tracker_fail_then_progress_clears_finished_at(tmp_path):
    # 矛盾态防护：fail 后迟到的 progress 把阶段拉回 running 时，
    # 上一轮的 finished_at 必须清空（否则前端同时看到 running + 旧完成时间）。
    from modelctl.core.startup_progress import STAGES, StartupTiming, StartupTracker

    snap = tmp_path / "s.json"
    tr = StartupTracker("q", "vllm", "docker",
                        timing=StartupTiming(path=tmp_path / "t.json"),
                        snapshot_path=snap)

    def stage_row():
        data = json.loads(snap.read_text(encoding="utf-8"))
        return data["stages"][STAGES.index("loading")]

    tr.begin("loading", "加载模型", pct=0.3)
    tr.fail("loading", "加载模型", "CUDA out of memory")
    failed = stage_row()
    assert failed["status"] == "error" and failed["finished_at"]
    assert failed["error"] == "CUDA out of memory"

    tr.progress("loading", "加载模型", pct=0.4)  # watcher 线程迟到的一帧
    r = stage_row()
    assert r["status"] == "running"
    assert r["finished_at"] is None  # 收尾时间戳不得残留
    assert r["error"] is None


def test_snapshot_tmp_name_unique_per_thread(tmp_path, monkeypatch):
    # 同进程跨线程（LoadingWatcher daemon + 主线程）共用一个 tmp 名 → 交错写坏文件，
    # os.replace 可能搬走半写的 JSON 导致快照损坏。tmp 名必须并入线程标识。
    import os
    import threading
    from pathlib import Path

    from modelctl.core import startup_progress as sp

    snap = tmp_path / "s.json"
    tr = sp.StartupTracker("q", "vllm", "docker",
                           timing=sp.StartupTiming(path=tmp_path / "t.json"),
                           snapshot_path=snap)

    used: list[str] = []
    real_replace = os.replace

    def spy(src, dst, *a, **kw):
        used.append(Path(src).name)
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(sp.os, "replace", spy)

    tr._write_snapshot()  # 主线程
    main_tmp = used[-1]
    assert str(os.getpid()) in main_tmp
    assert str(threading.get_ident()) in main_tmp

    worker = threading.Thread(target=tr._write_snapshot)
    worker.start()
    worker.join(timeout=5)
    assert len(used) == 2
    assert used[1] != main_tmp  # 同 PID 不同线程 → 不同 tmp
    assert list(tmp_path.glob("*.tmp")) == []  # tmp 均已被 replace 消费
    json.loads(snap.read_text(encoding="utf-8"))  # 快照仍是完整 JSON


# ---------------------------------------------------------------------------
# Task 4：引擎日志模式表 + LoadingWatcher
# ---------------------------------------------------------------------------

def test_match_progress_vllm_shards_monotonic():
    from modelctl.core.startup_progress import match_progress

    # banner
    assert match_progress("vllm", "INFO vLLM API server version 0.28.0")[1] == 0.05
    # shard 加载 50% → 0.5+0.3*0.5=0.65
    lab, pct = match_progress("vllm", "(APIServer) Loading safetensors checkpoint shards:  50% Completed | 3/6")
    assert abs(pct - 0.65) < 1e-6
    # CUDA graph 捕获 100% → 0.8+0.15=0.95
    assert abs(match_progress("vllm", "Capturing CUDA graph shapes: 100%")[1] - 0.95) < 1e-6
    # 无关行
    assert match_progress("vllm", "GET /metrics HTTP/1.1 200 OK") is None


def test_match_progress_unknown_engine_banner_only():
    from modelctl.core.startup_progress import match_progress

    # tokenspeed 无 shard 模式 → 只有 banner，其它 None
    assert match_progress("tokenspeed", "Loading safetensors checkpoint shards: 50%") is None


def test_loading_watcher_advances_tracker(tmp_path):
    import time
    from modelctl.core.startup_progress import LoadingWatcher, StartupTracker, StartupTiming

    log = tmp_path / "launch-q.log"
    log.write_text("(APIServer) vLLM API server version 0.28.0\n", encoding="utf-8")
    events = []
    tr = StartupTracker("q", "vllm", "docker", on_progress=events.append,
                        timing=StartupTiming(path=tmp_path / "t.json"),
                        snapshot_path=tmp_path / "s.json")
    tr.begin("loading", "加载模型")
    w = LoadingWatcher(tr, "vllm", log, interval=0.05)
    w.start()
    # 追加一行 shard 加载
    with log.open("a", encoding="utf-8") as f:
        f.write("(APIServer) Loading safetensors checkpoint shards: 100% Completed\n")
    deadline = time.time() + 2
    while time.time() < deadline and not any(e.pct == 0.8 for e in events):
        time.sleep(0.05)
    w.stop()
    assert any(e.pct == 0.8 for e in events)  # 0.5+0.3*1.0


def test_loading_watcher_pct_monotonic(tmp_path):
    # 评审裁决：pct 单调不减由 watcher 自己保证（_last_pct 只升不降）——
    # tail 窗口内出现更小 pct 的行（重复/交错输出）不得回报给 tracker
    import time
    from modelctl.core.startup_progress import LoadingWatcher, StartupTiming, StartupTracker

    log = tmp_path / "launch-q.log"
    # 首 tick 基线化（seen=2），第二 tick 处理追加行
    log.write_text("(APIServer) vLLM API server version 0.28.0\nsome noise\n", encoding="utf-8")
    events = []
    tr = StartupTracker("q", "vllm", "docker", on_progress=events.append,
                        timing=StartupTiming(path=tmp_path / "t.json"),
                        snapshot_path=tmp_path / "s.json")
    tr.begin("loading", "加载模型")
    w = LoadingWatcher(tr, "vllm", log, interval=0.05)
    w.start()
    with log.open("a", encoding="utf-8") as f:
        f.write("Loading safetensors checkpoint shards: 100% Completed\n")
        f.write("Loading safetensors checkpoint shards: 60% Completed\n")  # 回退行：必须忽略
    deadline = time.time() + 2
    while time.time() < deadline and not any(e.pct == 0.8 for e in events):
        time.sleep(0.05)
    w.stop()
    pcts = [e.pct for e in events if e.pct is not None]
    assert any(e.pct == 0.8 for e in events)
    assert all(a <= b for a, b in zip(pcts, pcts[1:], strict=False)), pcts
    assert 0.68 not in pcts  # 0.5+0.3*0.6 的回退帧绝不能出现在事件流里


def test_loading_watcher_fallback_label_after_stall(tmp_path):
    # spec §4.2：loading 持续超阈值且零命中 → 兜底文案 + pct=None（条纹动画）。
    # 阈值注入（fallback_sec）系对简报参考实现的最小偏离：否则单测需真等 120s。
    import time
    from modelctl.core.startup_progress import LoadingWatcher, StartupTiming, StartupTracker

    log = tmp_path / "launch-q.log"
    log.write_text("some unrelated engine output with no pattern hit\n", encoding="utf-8")
    events = []
    tr = StartupTracker("q", "vllm", "docker", on_progress=events.append,
                        timing=StartupTiming(path=tmp_path / "t.json"),
                        snapshot_path=tmp_path / "s.json")
    tr.begin("loading", "加载模型")
    w = LoadingWatcher(tr, "vllm", log, interval=0.05, fallback_sec=0.1)
    w.start()
    deadline = time.time() + 2
    while time.time() < deadline and not any(e.pct is None and "15 分钟" in e.label for e in events):
        time.sleep(0.05)
    w.stop()
    hits = [e for e in events if e.pct is None and "引擎初始化中" in e.label]
    assert hits and "冷启动" in hits[0].label and hits[0].stage == "loading"


def test_loading_watcher_full_window_rescan(tmp_path):
    # tail 窗口（400 行）打满后窗口滑动：按行号增量会恒为空而漏掉新行，
    # 必须退化为整体重扫（重复行由 _last_pct 单调过滤，无害）。
    import time
    from modelctl.core.startup_progress import LoadingWatcher, StartupTiming, StartupTracker

    log = tmp_path / "launch-q.log"
    noise = "\n".join(f"noise line {i}" for i in range(400))
    log.write_text(noise + "\n", encoding="utf-8")
    events = []
    tr = StartupTracker("q", "vllm", "docker", on_progress=events.append,
                        timing=StartupTiming(path=tmp_path / "t.json"),
                        snapshot_path=tmp_path / "s.json")
    tr.begin("loading", "加载模型")
    w = LoadingWatcher(tr, "vllm", log, interval=0.05)
    w.start()
    time.sleep(0.15)  # 首 tick 消费满窗口（seen=400）
    with log.open("a", encoding="utf-8") as f:
        f.write("Capturing CUDA graph shapes: 100%\n")  # 落在滑动后窗口内
    deadline = time.time() + 2
    while time.time() < deadline and not any(e.pct == 0.95 for e in events):
        time.sleep(0.05)
    w.stop()
    assert any(e.pct == 0.95 for e in events)


def test_loading_watcher_survives_missing_log_and_stop_idempotent(tmp_path):
    # 容错：日志尚未生成不得终止 watcher；stop() 幂等且 join 有超时，stop 后不再产生事件
    import time
    from modelctl.core.startup_progress import LoadingWatcher, StartupTiming, StartupTracker

    log = tmp_path / "not-yet.log"  # 故意不存在
    events = []
    tr = StartupTracker("q", "vllm", "docker", on_progress=events.append,
                        timing=StartupTiming(path=tmp_path / "t.json"),
                        snapshot_path=tmp_path / "s.json")
    tr.begin("loading", "加载模型")
    w = LoadingWatcher(tr, "vllm", log, interval=0.05)
    w.start()
    time.sleep(0.2)  # 至少 4 个 tick 全部落在文件缺失分支
    log.write_text("(APIServer) vLLM API server version 0.28.0\n", encoding="utf-8")
    w.stop()
    w.stop()  # 幂等：二次 stop 不得抛
    n = len(events)
    time.sleep(0.2)  # 线程若未死，banner(0.05) 会追加进来
    assert len(events) == n
