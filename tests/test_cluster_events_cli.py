#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_events_cli.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : cluster events 子命令（查询编码/中心已格式化字段直展）
# ===============================================================================
from __future__ import annotations

import pytest

BASE = "http://center:4173"


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


def test_events_default_request(probe) -> None:
    probe.reply("GET", "/cluster/events?limit=50", 200,
                {"events": [{"ts": "2026-09-06 10:00:00", "node_id": "w-1",
                             "goal_id": None, "kind": "node.disable",
                             "text": "禁用节点"}]})
    assert _main(["cluster", "events"]) == 0
    method, url, _, _ = probe.calls[0]
    assert method == "GET" and "/cluster/events?" in url and "limit=50" in url


def test_events_filters_are_percent_encoded(probe) -> None:
    _main(["cluster", "events", "--node", "w 1", "--kind", "goal.update", "--limit", "5"])
    url = probe.calls[0][1]
    assert "node_id=w%201" in url and "kind=goal.update" in url and "limit=5" in url


def test_events_renders_center_formatted_fields(probe, capsys) -> None:
    # reply 键带完整 query：Recorder 是 endswith 匹配，缺 ?limit=50 则落默认 (200, {})
    probe.reply("GET", "/cluster/events?limit=50", 200,
                {"events": [{"ts": "2026-09-06 10:00:00", "node_id": "w-1",
                             "goal_id": None, "kind": "node.retire",
                             "text": "节点退役（连带撤销 2 个目标）"}]})
    assert _main(["cluster", "events"]) == 0
    out = capsys.readouterr().out
    assert "2026-09-06 10:00:00" in out and "节点退役（连带撤销 2 个目标）" in out


def test_events_center_down_exit_2(probe) -> None:
    probe.reply("GET", "/cluster/events?limit=50", -1, {"error": "connection refused"})
    assert _main(["cluster", "events"]) == 2
