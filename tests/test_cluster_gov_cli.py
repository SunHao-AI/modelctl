#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_gov_cli.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 10:00
# @Desc   : cluster node 治理子命令（请求形状/退出码/确认闸门）
# ===============================================================================
from __future__ import annotations

import pytest

BASE = "http://center:4173"
NODES = "/admin/api/cluster/nodes"


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


@pytest.mark.parametrize("act,path", [
    ("disable", "/disable"), ("enable", "/enable"),
    ("rotate-token", "/rotate-token"), ("kick", "/kick"),
])
def test_node_actions_post_expected_path(probe, act, path) -> None:
    assert _main(["cluster", "node", act, "--node", "w-1"]) == 0
    method, url, _body, key = probe.calls[0]
    assert (method, url) == ("POST", BASE + NODES + "/w-1" + path)
    assert key == "sk-cli"


def test_rotate_token_prints_new_token_and_hint(probe, capsys) -> None:
    probe.reply("POST", "/cluster/nodes/w-1/rotate-token", 200,
                {"node_token": "NT-new", "kicked": True, "hint": "更新 .env 后重启"})
    assert _main(["cluster", "node", "rotate-token", "--node", "w-1"]) == 0
    out = capsys.readouterr().out
    assert "NT-new" in out and "更新 .env 后重启" in out


def test_center_error_maps_exit_2(probe) -> None:
    probe.reply("POST", "/cluster/nodes/ghost/disable", 404, {"detail": "节点不存在"})
    assert _main(["cluster", "node", "disable", "--node", "ghost"]) == 2


def test_retire_requires_node_or_yes_first_counts_goals(probe, monkeypatch) -> None:
    probe.reply("GET", "/cluster/goals?node_id=w-1", 200,
                {"goals": [{"node_id": "w-1"}, {"node_id": "w-1"}]})
    # --yes 跳过交互：先数 goals 再 DELETE，DELETE 响应回带 removed_goals
    probe.reply("DELETE", "/cluster/nodes/w-1", 200, {"removed": True, "removed_goals": 2})
    assert _main(["cluster", "node", "retire", "--node", "w-1", "--yes"]) == 0
    assert probe.one("DELETE")
    methods = [c[0] for c in probe.calls]
    assert methods == ["GET", "DELETE"]        # 确认前只读，DELETE 最后发


def test_retire_declined_makes_no_delete(probe, monkeypatch) -> None:
    probe.reply("GET", "/cluster/goals?node_id=w-1", 200, {"goals": [{"node_id": "w-1"}]})
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert _main(["cluster", "node", "retire", "--node", "w-1"]) == 2
    assert probe.one("DELETE") == []


def test_node_requires_node_arg(probe) -> None:
    with pytest.raises(SystemExit):            # argparse required=True
        _main(["cluster", "node", "disable"])
    assert probe.calls == []
