#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_agent_v2.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 12:00
# @Desc   : Agent 与 reconciler 的接线（ack 投递 / 心跳扩展段 / result 回传 / 启动闸门）
# ===============================================================================
import contextlib
import itertools
import json
import threading

import pytest

pytest.importorskip("websockets")
from websockets.exceptions import ConnectionClosed  # noqa: E402
from websockets.sync.server import serve  # noqa: E402

from modelctl.core.cluster import agent, reconcile, wsproto  # noqa: E402


class Recorder:
    """reconciler 替身：记录被投递的内容，返回固定扩展段。"""

    def __init__(self):
        self.snapshots: list[dict] = []
        self.actions: list[dict] = []
        self.payload = {"profiles": {"qwen": {"stage": "READY"}},
                        "goal_sync": {"revision": "rev-9"}, "drift": [],
                        "local_profiles": ["qwen"], "capacity": {"gpu_count": 2},
                        "runtimes": {"vllm": {"ok": True}}}

    def offer_snapshot(self, snapshot):
        self.snapshots.append(snapshot)

    def handle_actions(self, actions, *, now=None):
        self.actions.extend(actions)

    def flush_results(self):
        return []

    def heartbeat_payload(self):
        return dict(self.payload)


class Boom(Recorder):
    def heartbeat_payload(self):
        raise RuntimeError("探测炸了")


def test_collect_heartbeat_keeps_m0_shape_without_reconciler():
    """rt=None 必须与 M0 逐键一致：中心升级与 worker 升级不同步是常态。"""
    hb = agent.collect_heartbeat()
    assert sorted(hb) == ["gpu", "host", "profiles"]


def test_collect_heartbeat_merges_reconciler_segments():
    hb = agent.collect_heartbeat(Recorder())
    assert hb["goal_sync"] == {"revision": "rev-9"}
    assert hb["local_profiles"] == ["qwen"]
    assert hb["runtimes"] == {"vllm": {"ok": True}}
    assert hb["capacity"] == {"gpu_count": 2}
    assert hb["drift"] == []
    # 基础段仍在（中心 M0 分支还要读 gpu/host）
    assert "gpu" in hb and "host" in hb


def test_collect_heartbeat_drops_profiles_when_reconciler_broken():
    """reconciler 抛错时**整段省略**，尤其不能回 `profiles: {}`。

    空映射在中心侧是"worker 明确说本机没有模型"，会被当作事实覆盖台账——
    一次偶发探测异常就抹掉全部在跑模型，比不上报严重得多。省略 = 未知 = 保留旧值。
    """
    hb = agent.collect_heartbeat(Boom())
    assert "profiles" not in hb and "goal_sync" not in hb and "drift" not in hb
    assert "gpu" in hb and "host" in hb


def test_deliver_ack_noop_without_reconciler():
    """reconciler 尚未创建（启动空窗）时 ack 照常丢弃，不得抛错。"""
    ack = {"t": "ack", "seq": 1, "sync": {"revision": "rev-1", "goals": [{"goal_id": "a"}]}}
    assert agent.deliver_ack(ack, None) == []


def test_deliver_ack_offers_snapshot_and_actions():
    rt = Recorder()
    ack = {"t": "ack", "seq": 3,
           "sync": {"revision": "rev-1", "goals": [{"goal_id": "qwen@@w-1"}]},
           "actions": [{"seq": 9, "action": "stop", "goal_id": "qwen@@w-1", "profile": "qwen"}]}
    assert agent.deliver_ack(ack, rt) == []
    assert rt.snapshots == [{"revision": "rev-1", "goals": [{"goal_id": "qwen@@w-1"}]}]
    assert rt.actions == [{"seq": 9, "action": "stop", "goal_id": "qwen@@w-1",
                           "profile": "qwen"}]


def test_deliver_ack_returns_reconciler_results():
    class WithResult(Recorder):
        def flush_results(self):
            return [{"t": "result", "seq": 9, "ok": True, "detail": "已受理"}]

    out = agent.deliver_ack({"t": "ack"}, WithResult())
    assert out == [{"t": "result", "seq": 9, "ok": True, "detail": "已受理"}]


def test_deliver_ack_tolerates_m0_center_ack():
    """M0 中心的 ack 只有 `{"t":"ack"}`：不得抛错、不得投递任何东西。"""
    rt = Recorder()
    assert agent.deliver_ack({"t": "ack"}, rt) == []
    assert rt.snapshots == [] and rt.actions == []


def test_collect_heartbeat_payload_is_detached_copy():
    """心跳扩展段必须与 rt 内部缓存**结构性脱钩**（Task 9 只读契约）。

    真 Reconciler 的 heartbeat_payload 在降级路径上直接返回 `_last_snapshot` 里的
    引用；若 Agent 只 update 不 deepcopy，任何下游就地改写 hb["profiles"] 都会污染
    reconciler 缓存。Recorder 的 payload 用浅拷贝返回（嵌套共享）正是为了暴露这一点。
    """
    rt = Recorder()
    hb = agent.collect_heartbeat(rt)
    hb["profiles"]["INJECTED"] = {}                    # 就地污染心跳返回值
    assert "INJECTED" not in rt.payload["profiles"], "心跳返回值与 rt 缓存共享引用"


class Causal(Recorder):
    """因果替身：action 的受理回执只在 handle_actions 之后才存在（对齐真实现）。

    真 Reconciler 是先 `handle_actions` 排队 result、再由 `flush_results` 原子换表
    取走。deliver_ack 若先 flush 后投递，本轮 ack 里的 action 回执就要等下一轮才出。
    """

    def __init__(self):
        super().__init__()
        self._pending: list[dict] = []

    def handle_actions(self, actions, *, now=None):
        super().handle_actions(actions, now=now)
        self._pending.extend(wsproto.make_result(a["seq"], True, "已受理") for a in actions)

    def flush_results(self):
        got, self._pending = self._pending, []
        return got


def test_deliver_ack_flushes_after_delivery():
    """result 帧必须承载**本轮** ack 里的 action 回执（先投递、后 flush）。"""
    ack = {"t": "ack", "seq": 11,
           "actions": [{"seq": 11, "action": "stop", "goal_id": "qwen@@w-1", "profile": "qwen"}]}
    out = agent.deliver_ack(ack, Causal())
    assert [f["seq"] for f in out] == [11], f"本轮 action 的回执未随本轮返回：{out}"


# --------------------------------------------------------------------- 端到端（假中心）

@contextlib.contextmanager
def _fake_center(handler, monkeypatch, tmp_path, rt):
    """假中心 + Agent 线程。

    必须用 contextmanager 把断言包在 `with serve(...)` **内部**：若在 `with` 体内
    `return`，上下文立即退出、服务器随即关闭，测试拿到的端口已无人监听——表现为
    "Agent 一直在退避重连、断言超时"，且极易被误读成 Agent 逻辑有 bug。
    假中心沿用 M0 范式：`serve(...)` 只 bind/listen，须自起 `serve_forever` 线程。
    """
    from modelctl.core.cluster import agent as ag

    with serve(handler, "127.0.0.1", 0) as srv:
        port = srv.socket.getsockname()[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        monkeypatch.setenv("CLUSTER_CENTER_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("CLUSTER_NODE_ID", "w-v2")
        monkeypatch.setenv("CLUSTER_JOIN_TOKEN", "JT")
        monkeypatch.setenv("CLUSTER_NODE_TOKEN", "")
        monkeypatch.setattr(ag, "ENV_PATH", tmp_path / ".env")
        stop = threading.Event()
        t = threading.Thread(target=agent.WorkerAgent(stop_event=stop, reconciler=rt).run,
                             daemon=True)
        t.start()
        try:
            yield srv
        finally:
            stop.set()
            t.join(timeout=5)


def test_agent_delivers_ack_to_reconciler(tmp_path, monkeypatch):
    got = threading.Event()
    seen: dict[str, object] = {}

    def handler(conn):
        conn.recv()
        conn.send(wsproto.dumps(wsproto.make_welcome("NT", 1, 1)))
        conn.recv()                                        # heartbeat
        conn.send(wsproto.dumps({"t": "ack", "seq": 1,
                                 "sync": {"revision": "rev-7", "goals": []}}))

    rt = Recorder()
    original = rt.offer_snapshot

    def spy(snapshot):
        original(snapshot)
        seen["snapshot"] = snapshot
        got.set()

    rt.offer_snapshot = spy
    with _fake_center(handler, monkeypatch, tmp_path, rt):
        assert got.wait(5), "ack 里的 sync 未投递给 reconciler"
        assert seen["snapshot"]["revision"] == "rev-7"


def test_agent_sends_result_frame_after_ack(tmp_path, monkeypatch):
    """受理回执必须在**下一次 send** 就搭出去（不等下一次心跳周期）。"""
    arrived = threading.Event()
    frames: list[dict] = []

    def handler(conn):
        conn.recv()
        conn.send(wsproto.dumps(wsproto.make_welcome("NT", 1, 1)))
        conn.recv()                                        # heartbeat
        conn.send(wsproto.dumps({"t": "ack", "seq": 1,
                                 "actions": [{"seq": 4, "action": "stop",
                                              "goal_id": "qwen@@w-1", "profile": "qwen"}]}))
        reply = json.loads(conn.recv())
        frames.append(reply)
        if reply.get("t") == "result":
            arrived.set()

    class LateResult(Recorder):
        def flush_results(self):
            return [wsproto.make_result(4, True, "qwen 已受理，下一轮生效")]

    with _fake_center(handler, monkeypatch, tmp_path, LateResult()):
        assert arrived.wait(5), f"中心未收到 result 帧：{frames}"
        assert frames[0]["seq"] == 4 and frames[0]["ok"] is True


def test_agent_reports_reconciler_revision_to_center(tmp_path, monkeypatch):
    """心跳里的 goal_sync.revision 是中心判断"要不要捎带 sync"的唯一依据。"""
    seen: dict[str, object] = {}
    got = threading.Event()

    def handler(conn):
        conn.recv()
        conn.send(wsproto.dumps(wsproto.make_welcome("NT", 1, 1)))
        msg = json.loads(conn.recv())
        seen["hb"] = msg
        got.set()
        conn.send(wsproto.dumps({"t": "ack"}))

    with _fake_center(handler, monkeypatch, tmp_path, Recorder()):
        assert got.wait(5)
        assert seen["hb"]["payload"]["goal_sync"] == {"revision": "rev-9"}


def test_agent_survives_reconciler_broken(tmp_path, monkeypatch):
    """reconciler 取数炸了也必须继续心跳：断流会被中心按 lease 判离线。"""
    first, second = threading.Event(), threading.Event()
    n = itertools.count()

    def handler(conn):
        conn.recv()
        conn.send(wsproto.dumps(wsproto.make_welcome("NT", 1, 1)))
        for _ in range(2):
            msg = json.loads(conn.recv())
            if msg.get("payload", {}).get("goal_sync"):
                first.set()
            else:
                second.set()
            conn.send(wsproto.dumps({"t": "ack"}))

    class Flaky(Recorder):
        def heartbeat_payload(self):
            if next(n) == 0:
                return dict(self.payload)
            raise RuntimeError("探测炸了")

    with _fake_center(handler, monkeypatch, tmp_path, Flaky()):
        assert first.wait(5), "首拍未上报扩展段"
        assert second.wait(5), "reconciler 抛错后 Agent 停止了心跳"


def test_agent_falls_back_to_global_reconciler(tmp_path, monkeypatch):
    """不注入 reconciler 时必须回落到全局单例——这是生产 webui 的**唯一**路径。

    `start_agent_in_background()` 构造的 WorkerAgent 从不带 reconciler，全靠
    `_reconciler_now()` 读 `reconcile.current()`；该路径此前零回归覆盖。
    不走 `_fake_center`（它固定注入 rt）：单例必须在 Agent 线程启动**前**置好，
    否则首拍心跳先于打桩发出，断言会撞上与 fallback 语义无关的时序竞态。
    """
    from modelctl.core.cluster import agent as ag

    seen: dict[str, object] = {}
    got = threading.Event()

    def handler(conn):
        conn.recv()
        conn.send(wsproto.dumps(wsproto.make_welcome("NT", 1, 1)))
        seen["hb"] = json.loads(conn.recv())
        conn.send(wsproto.dumps({"t": "ack"}))
        got.set()

    with serve(handler, "127.0.0.1", 0) as srv:
        port = srv.socket.getsockname()[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        monkeypatch.setenv("CLUSTER_CENTER_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("CLUSTER_NODE_ID", "w-v2")
        monkeypatch.setenv("CLUSTER_JOIN_TOKEN", "JT")
        monkeypatch.setenv("CLUSTER_NODE_TOKEN", "")
        monkeypatch.setattr(ag, "ENV_PATH", tmp_path / ".env")
        monkeypatch.setattr(reconcile, "_CURRENT", Recorder())   # 假装有全局单例
        stop = threading.Event()
        t = threading.Thread(target=agent.WorkerAgent(stop_event=stop).run, daemon=True)
        t.start()
        try:
            assert got.wait(5), "未注入 reconciler 时 Agent 停止了心跳"
            assert seen["hb"]["payload"]["goal_sync"] == {"revision": "rev-9"}
        finally:
            stop.set()
            t.join(timeout=5)


def test_agent_reads_one_reply_per_result_frame(tmp_path, monkeypatch):
    """中心对**每入帧必回一帧**，Agent 必须每条 result 也 recv 一次排空回帧。

    假中心按稳态循环写（而非脚本化的有限往返）：heartbeat→ack（每拍带递增 revision
    的 sync），result→ack。worker 每轮发 1 心跳 + 1 result 却只 recv 1 帧时，读队列每轮
    净积压 1 帧，第 k 拍心跳的 sync 要到第 ~2k 轮才被消费——sync 应用滞后心跳且缺口
    线性增长。判据取"投递第 k 次 sync 时中心已收到第几拍心跳"：收支平衡时恒等 k
    （Agent 阻塞在 recv，中心此刻自增不了）；失衡时第 2 次投递就变成 hb=3，无需墙钟赌注。
    """
    rounds = 3
    arrived = threading.Event()
    hb = {"n": 0}
    lag: list[tuple[int, int]] = []

    def handler(conn):
        conn.recv()                                        # hello
        conn.send(wsproto.dumps(wsproto.make_welcome("NT", 1, 1)))
        try:
            while True:
                msg = json.loads(conn.recv())
                if msg.get("t") == "heartbeat":
                    hb["n"] += 1
                    conn.send(wsproto.dumps({"t": "ack", "seq": hb["n"],
                                             "sync": {"revision": f"rev-{hb['n']}",
                                                      "goals": [{"goal_id": "qwen@@w-v2"}]}}))
                else:
                    conn.send(wsproto.dumps({"t": "ack"}))  # result→ack（Task 11 契约）
        except ConnectionClosed:
            return

    class LateResult(Recorder):
        """每轮都恰好产出一条 result——正是把读队列逐轮积压的最坏形状。"""

        def flush_results(self):
            return [wsproto.make_result(1, True, "已受理")]

    rt = LateResult()
    original = rt.offer_snapshot

    def spy(snapshot):
        original(snapshot)
        lag.append((len(rt.snapshots), hb["n"]))           # (第几次投递, 中心已收心跳数)
        if len(rt.snapshots) >= rounds:
            arrived.set()

    rt.offer_snapshot = spy
    with _fake_center(handler, monkeypatch, tmp_path, rt):
        assert arrived.wait(10), f"sync 投递不足（仅 {len(rt.snapshots)}/{rounds}）"
        assert lag == [(k, k) for k in range(1, rounds + 1)], (
            f"(投递序号, 中心心跳数) 错位即读队列积压：{lag}")
        assert [s["revision"] for s in rt.snapshots] == [f"rev-{k}" for k in range(1, rounds + 1)]


# --------------------------------------------------------------------- 启动闸门

class Noop:
    """闸门测试用替身：`run` 立即返回，绝不触碰真实 models/ 目录。"""

    def __init__(self, *a, **kw):
        pass

    def run(self, stop_event):
        return None


@pytest.mark.parametrize(("role", "started"), [("solo", False), ("control-plane", False),
                                               ("worker", True), ("both", True)])
def test_background_start_gate_follows_role(monkeypatch, role, started):
    from modelctl.core.webui import server

    monkeypatch.setenv("CLUSTER_ROLE", role)
    monkeypatch.setattr(reconcile, "_CURRENT", None, raising=False)
    monkeypatch.setattr(reconcile, "_STOP", None, raising=False)
    monkeypatch.setattr(reconcile, "Reconciler", Noop)
    assert server.start_cluster_background() is started
    assert (reconcile.current() is not None) is started


def test_background_start_is_idempotent(monkeypatch):
    """重复调用不得起第二个 reconciler（两个循环会互相抢同一批 goal）。"""
    from modelctl.core.webui import server

    monkeypatch.setenv("CLUSTER_ROLE", "worker")
    monkeypatch.setattr(reconcile, "_CURRENT", None, raising=False)
    monkeypatch.setattr(reconcile, "_STOP", None, raising=False)
    monkeypatch.setattr(reconcile, "Reconciler", Noop)
    assert server.start_cluster_background() is True
    first = reconcile.current()
    assert server.start_cluster_background() is True
    assert reconcile.current() is first                  # 第二次未新建实例


def test_background_start_invokes_agent(monkeypatch):
    """`start_cluster_background()` 必须真的拉起 Agent，而不只是 reconciler。

    上两条只观察 `reconcile.current()`，对"忘调 start_agent_in_background"完全无感——
    那种 worker 表现为 reconciler 在跑却永不连中心，是很难从日志一眼看出的静默故障。
    """
    from modelctl.core.cluster import agent as cluster_agent
    from modelctl.core.webui import server

    calls: list[int] = []
    monkeypatch.setattr(cluster_agent, "start_agent_in_background", lambda *a: calls.append(1))
    monkeypatch.setenv("CLUSTER_ROLE", "worker")
    monkeypatch.setattr(reconcile, "_CURRENT", None, raising=False)
    monkeypatch.setattr(reconcile, "_STOP", None, raising=False)
    monkeypatch.setattr(reconcile, "Reconciler", Noop)
    assert server.start_cluster_background() is True
    assert len(calls) == 1, f"start_agent_in_background 调用次数异常：{calls}"
