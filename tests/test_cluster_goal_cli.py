#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_goal_cli.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/4 10:00
# @Desc   : cluster goal/launch/stop/sync 子命令（probe 打桩，只断言请求形状与退出码）
# ===============================================================================
"""CLI 是 REST 的薄壳：断言"发了什么请求"与"退出码/输出了什么"，不复测 core 判定。"""
from __future__ import annotations

import pytest

BASE = "http://center:4173"
GOALS = "/admin/api/cluster/goals"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CLUSTER_ROLE", "CLUSTER_CENTER_URL", "CLUSTER_NODE_ID", "CLUSTER_LAN",
              "CLUSTER_JOIN_TOKEN", "CLUSTER_NODE_TOKEN", "API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLUSTER_CENTER_URL", BASE)
    monkeypatch.setenv("API_KEY", "sk-cli")
    import modelctl.core.envfile as ef

    monkeypatch.setattr(ef, "PROJECT_ROOT", tmp_path)


class Recorder:
    """记录调用 + 按 (method, path 后缀) 返回预置响应；未预置一律 (200, {})。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._replies: dict[tuple[str, str], tuple[int, dict]] = {}

    def reply(self, method: str, path: str, status: int, body: dict) -> None:
        self._replies[(method, path)] = (status, body)

    def call(self, method, url, payload, api_key=""):
        self.calls.append((method, url, payload, api_key))
        for (m, path), out in self._replies.items():
            if m == method and url.endswith(path):
                return out
        return 200, {}

    def one(self, method: str):
        return [c for c in self.calls if c[0] == method]


@pytest.fixture()
def probe(monkeypatch):
    import modelctl.core.cluster.center_probe as cp

    rec = Recorder()
    monkeypatch.setattr(cp, "get_json",
                        lambda url, api_key="", timeout=5.0: rec.call("GET", url, None, api_key))
    monkeypatch.setattr(cp, "post_json",
                        lambda url, payload, api_key="", timeout=5.0: rec.call("POST", url, payload, api_key))
    monkeypatch.setattr(cp, "put_json",
                        lambda url, payload, api_key="", timeout=5.0: rec.call("PUT", url, payload, api_key))
    monkeypatch.setattr(cp, "delete_json",
                        lambda url, api_key="", timeout=5.0: rec.call("DELETE", url, None, api_key))
    return rec


def _main(argv):
    from modelctl import cli

    return cli.main(argv)


# ---------------- goal set / launch ----------------
def test_goal_set_posts_full_body(probe) -> None:
    assert _main(["cluster", "goal", "set", "qwen", "--node", "w-1", "--create"]) == 0
    method, url, body, key = probe.calls[0]
    assert (method, url) == ("POST", BASE + GOALS)
    assert body["profile"] == "qwen" and body["node_ids"] == ["w-1"]
    assert body["create"] is True and body["intent"] == "start" and body["dry_run"] is False
    assert key == "sk-cli"                                  # Bearer 由 API_KEY 提供


def test_goal_set_multiple_nodes(probe) -> None:
    _main(["cluster", "goal", "set", "qwen", "--node", "w-1", "--node", "w-2"])
    assert probe.calls[0][2]["node_ids"] == ["w-1", "w-2"]


def test_goal_set_all_flag(probe) -> None:
    _main(["cluster", "goal", "set", "qwen", "--all"])
    body = probe.calls[0][2]
    assert body["all_nodes"] is True and body["node_ids"] is None


def test_goal_set_requires_node_or_all(probe) -> None:
    assert _main(["cluster", "goal", "set", "qwen"]) == 2
    assert probe.calls == []                                # 参数不合意时绝不发请求


def test_goal_set_node_and_all_conflict(probe) -> None:
    assert _main(["cluster", "goal", "set", "qwen", "--node", "w-1", "--all"]) == 2
    assert probe.calls == []


def test_goal_set_env_pairs_parsed(probe) -> None:
    _main(["cluster", "goal", "set", "qwen", "--node", "w-1",
           "--env", "MODEL_ROOT=/mnt/nas", "--env", "LOG_DIR=/var/log/m"])
    assert probe.calls[0][2]["env_overlay"] == {"MODEL_ROOT": "/mnt/nas", "LOG_DIR": "/var/log/m"}


def test_goal_set_env_pair_without_equals(probe) -> None:
    assert _main(["cluster", "goal", "set", "qwen", "--node", "w-1", "--env", "NOPPE"]) == 2
    assert probe.calls == []


def test_goal_set_dry_run_and_gpus_passthrough(probe) -> None:
    _main(["cluster", "goal", "set", "qwen", "--node", "w-1", "--dry-run", "--gpus", "0,1"])
    body = probe.calls[0][2]
    assert body["dry_run"] is True and body["gpus"] == "0,1"   # 解析交给中心，CLI 不重复一套


def test_goal_set_prints_gate_report(probe, capsys) -> None:
    probe.reply("POST", GOALS, 200, {"created": 1, "skipped": 0, "errors": 0,
                                     "report": "[dry-run] [ok] w-1  engine=vllm"})
    _main(["cluster", "goal", "set", "qwen", "--node", "w-1", "--dry-run"])
    assert "[dry-run] [ok] w-1" in capsys.readouterr().out     # core 已排好版，CLI 原样打印


def test_goal_set_center_400_prints_detail(probe, capsys) -> None:
    probe.reply("POST", GOALS, 400, {"detail": "env_overlay 禁止下发凭据类键"})
    assert _main(["cluster", "goal", "set", "qwen", "--node", "w-1"]) == 2
    assert "凭据类键" in capsys.readouterr().err


def test_goal_set_all_candidates_rejected_exit_2(probe) -> None:
    probe.reply("POST", GOALS, 200, {"created": 0, "skipped": 0, "errors": 2, "report": "r"})
    assert _main(["cluster", "goal", "set", "qwen", "--all"]) == 2


def test_goal_set_only_skips_is_success(probe, capsys) -> None:
    """幂等重跑：全 skip 也算成功，否则脚本里第二次下发必然非零退出。"""
    probe.reply("POST", GOALS, 200, {"created": 0, "skipped": 2, "errors": 0, "report": "r"})
    assert _main(["cluster", "goal", "set", "qwen", "--all"]) == 0
    assert "无变更" in capsys.readouterr().out


def test_goal_set_center_unreachable_exit_2(probe) -> None:
    probe.reply("POST", GOALS, -1, {"error": "中心不可达"})
    assert _main(["cluster", "goal", "set", "qwen", "--node", "w-1"]) == 2


def test_launch_is_goal_set_with_start_intent(probe) -> None:
    assert _main(["cluster", "launch", "qwen", "--node", "w-1", "--create"]) == 0
    body = probe.one("POST")[0][2]
    assert body["intent"] == "start" and body["create"] is True


def test_launch_dry_run_passthrough(probe) -> None:
    _main(["cluster", "launch", "qwen", "--node", "w-1", "--dry-run"])
    assert probe.one("POST")[0][2]["dry_run"] is True


# ---------------- goal list ----------------
def test_goal_list_table_columns(probe, capsys) -> None:
    probe.reply("GET", GOALS, 200, {"goals": [
        {"node_id": "w-1", "profile": "qwen", "intent": "start", "stage": "READY",
         "reason": "", "gpu": [0, 1], "port": 8101, "updated_at": "2026-09-04 10:00:00"}]})
    assert _main(["cluster", "goal", "list"]) == 0
    out = capsys.readouterr().out
    assert "w-1" in out and "READY" in out and "8101" in out
    assert "2026-09-04 10:00:00" in out           # 中心已格式化，CLI 不二次加工


def test_goal_list_empty_hint(probe, capsys) -> None:
    probe.reply("GET", GOALS, 200, {"goals": []})
    assert _main(["cluster", "goal", "list"]) == 0
    assert "goal set" in capsys.readouterr().out   # 空表要给出下一步命令


def test_goal_list_filters_in_query(probe) -> None:
    _main(["cluster", "goal", "list", "--node", "w-1", "--profile", "qwen"])
    url = probe.calls[0][1]
    assert "node_id=w-1" in url and "profile=qwen" in url


def test_goal_list_query_values_percent_encoded(probe) -> None:
    """query 值含 #/空格必须 percent-encode（终审 C2）。

    `#` 不编码时 urllib 会把 `#` 之后当 fragment 整段丢掉——中心收到的 profile
    被静默截断，list 会"成功"返回另一个 profile 的 goal（假过滤）；空格更直接
    是非法 URL。钉 URL 尾部保留完整编码值而非截断原值。
    """
    _main(["cluster", "goal", "list", "--node", "w 1", "--profile", "qwen#v2"])
    url = probe.calls[0][1]
    assert "node_id=w%201" in url and "profile=qwen%23v2" in url
    assert "qwen#v2" not in url and "w 1" not in url      # 绝不允许裸值泄漏进 URL


def test_remove_all_profile_with_hash_encodes_list_query(probe) -> None:
    """remove/stop --all 的 --all 展开走 `_goal_nodes_of_profile` 的 profile 查询，
    同一编码义务：含 # 时查询段必须完整，否则列到的是别的 profile 的节点集。"""
    probe.reply("GET", "/admin/api/cluster/goals?profile=qwen%23v2", 200,
                {"goals": [{"node_id": "w-1"}]})
    assert _main(["cluster", "goal", "remove", "qwen#v2", "--all"]) == 0
    get_url = probe.one("GET")[0][1]
    assert get_url == BASE + GOALS + "?profile=qwen%23v2"
    assert len(probe.one("DELETE")) == 1                  # 展开成功而非截断后空转


def test_goal_list_json_raw(probe, capsys) -> None:
    probe.reply("GET", GOALS, 200, {"goals": [{"goal_id": "qwen@@w-1"}]})
    _main(["cluster", "goal", "list", "--json"])
    assert '"goal_id": "qwen@@w-1"' in capsys.readouterr().out


# ---------------- goal remove ----------------
def test_goal_remove_deletes_by_goal_id(probe) -> None:
    probe.reply("DELETE", "/admin/api/cluster/goals/qwen%40%40w-1", 200,
                {"removed": ["qwen@@w-1"], "missing": []})
    assert _main(["cluster", "goal", "remove", "qwen", "--node", "w-1"]) == 0
    assert probe.calls[0][0] == "DELETE"
    assert probe.calls[0][1] == BASE + "/admin/api/cluster/goals/qwen%40%40w-1"


def test_goal_remove_all_lists_then_deletes_each(probe) -> None:
    probe.reply("GET", "/admin/api/cluster/goals?profile=qwen", 200,
                {"goals": [{"node_id": "w-1"}, {"node_id": "w-2"}]})
    assert _main(["cluster", "goal", "remove", "qwen", "--all"]) == 0
    deleted = [c[1] for c in probe.one("DELETE")]
    assert len(deleted) == 2 and all(u.startswith(BASE + GOALS + "/") for u in deleted)


def test_goal_remove_all_without_goals_is_noop(probe) -> None:
    probe.reply("GET", "/admin/api/cluster/goals?profile=qwen", 200, {"goals": []})
    assert _main(["cluster", "goal", "remove", "qwen", "--all"]) == 0
    assert probe.one("DELETE") == []


def test_goal_remove_404_exit_2(probe) -> None:
    probe.reply("DELETE", GOALS + "/qwen%40%40w-1", 404, {"detail": "目标 qwen@@w-1 不存在"})
    assert _main(["cluster", "goal", "remove", "qwen", "--node", "w-1"]) == 2


# ---------------- goal retry / stop / sync ----------------
def test_goal_retry_posts_retry_endpoint(probe) -> None:
    assert _main(["cluster", "goal", "retry", "qwen", "--node", "w-1"]) == 0
    method, url, _, _ = probe.calls[0]
    assert method == "POST" and url.endswith("/goals/qwen%40%40w-1/retry")


def test_goal_retry_requires_node(probe) -> None:
    assert _main(["cluster", "goal", "retry", "qwen"]) == 2
    assert probe.calls == []


def test_stop_puts_intent_stop_not_action(probe) -> None:
    """stop 必须是声明式 PUT intent=stop：走 action 会和 goal 打架（模型自己复活）。"""
    assert _main(["cluster", "stop", "qwen", "--node", "w-1"]) == 0
    method, url, body, _ = probe.calls[0]
    assert method == "PUT" and url.endswith("/goals/qwen%40%40w-1")
    assert body == {"intent": "stop"}


def test_stop_all_lists_profile_goals_then_puts(probe) -> None:
    probe.reply("GET", "/admin/api/cluster/goals?profile=qwen", 200,
                {"goals": [{"node_id": "w-1"}, {"node_id": "w-2"}]})
    assert _main(["cluster", "stop", "qwen", "--all"]) == 0
    assert len(probe.one("PUT")) == 2


def test_stop_unknown_goal_exit_2(probe) -> None:
    probe.reply("PUT", GOALS + "/qwen%40%40ghost", 404, {"detail": "目标不存在"})
    assert _main(["cluster", "stop", "qwen", "--node", "ghost"]) == 2


def test_sync_posts_node_sync_endpoint(probe) -> None:
    assert _main(["cluster", "sync", "--node", "w-1"]) == 0
    method, url, _, _ = probe.calls[0]
    assert method == "POST" and url == BASE + "/admin/api/cluster/nodes/w-1/sync"


def test_sync_all_targets_every_listed_node(probe) -> None:
    probe.reply("GET", "/admin/api/cluster/nodes", 200,
                {"nodes": [{"node_id": "w-1"}, {"node_id": "w-2"}]})
    assert _main(["cluster", "sync", "--all"]) == 0
    urls = [c[1] for c in probe.one("POST")]
    assert urls == [BASE + "/admin/api/cluster/nodes/w-1/sync",
                    BASE + "/admin/api/cluster/nodes/w-2/sync"]


def test_sync_requires_node_or_all(probe) -> None:
    assert _main(["cluster", "sync"]) == 2
    assert probe.calls == []


# ---------------- center_probe 新方法 ----------------
class _Resp:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_put_and_delete_json_use_correct_methods(monkeypatch) -> None:
    """DELETE 必须真是 DELETE（urllib 默认 POST 语义容易写错）；PUT 亦然。"""
    import modelctl.core.cluster.center_probe as cp

    seen: list[str] = []

    def fake_urlopen(req, timeout=5.0):
        seen.append(req.method)
        return _Resp(b'{"ok": true}')

    monkeypatch.setattr(cp.urllib.request, "urlopen", fake_urlopen)
    assert cp.put_json("http://c/x", {"a": 1}) == (200, {"ok": True})
    assert cp.delete_json("http://c/x") == (200, {"ok": True})
    assert seen == ["PUT", "DELETE"]


def test_delete_json_network_error_folded(monkeypatch) -> None:
    import modelctl.core.cluster.center_probe as cp

    def boom(req, timeout=5.0):
        raise OSError("connection refused")

    monkeypatch.setattr(cp.urllib.request, "urlopen", boom)
    status, body = cp.delete_json("http://c/x")
    assert status == -1 and "connection refused" in body["error"]


# ---------------- _cluster_request 方法兜底（终审 C3）----------------
def test_cluster_request_unknown_method_raises_without_sending(probe) -> None:
    """未知方法必须 ValueError 且**一个请求都不发**。

    旧实现把兜底写成 DELETE：拼错的方法名（PATCH/HEAD/POSTT）静默变成一次删除
    ——控制面 CLI 的最坏失效模式。钉raises 之外必须钉 probe.calls == []，
    否则"先删再抛"也能全绿。
    """
    from modelctl.cli import _cluster_request

    with pytest.raises(ValueError, match="不支持的方法 PATCH"):
        _cluster_request("PATCH", "/cluster/goals")
    assert probe.calls == []


# ---------------- P-1（Task 12 review）：--engine 全链路透传 ----------------
def test_goal_set_and_launch_pass_engine(probe) -> None:
    """--engine 必须进请求体：REST 侧 Pydantic 静默忽略未知字段，漏传等于没有选边
    出口——同名 YAML 散落多引擎时用户遇 `[err] 歧义` 后无路可走（条款②"绝不猜"变死路）。"""
    assert _main(["cluster", "goal", "set", "qwen", "--node", "w-1",
                  "--create", "--engine", "vllm"]) == 0
    assert probe.one("POST")[0][2]["engine"] == "vllm"
    assert _main(["cluster", "launch", "qwen", "--node", "w-1", "--engine", "sglang"]) == 0
    assert probe.one("POST")[1][2]["engine"] == "sglang"
