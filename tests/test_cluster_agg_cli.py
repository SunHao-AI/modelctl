#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_agg_cli.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/6 14:00
# @Desc   : status/list --cluster 中心聚合（数据源切换/失败不回退）
# ===============================================================================
from __future__ import annotations

import pytest

BASE = "http://center:4173"

NODES_BODY = {"nodes": [{"node_id": "w-1", "status": "online", "lan_id": "lan-1",
                         "capacity_text": "4 卡 / 154 GiB"},
                        {"node_id": "w-2", "status": "offline", "lan_id": "",
                         "capacity_text": "-"}]}
GOALS_BODY = {"goals": [{"node_id": "w-1", "profile": "qwen", "intent": "start",
                         "stage": "READY", "state": "running", "port": 8001},
                        {"node_id": "w-1", "profile": "ds", "intent": "stop",
                         "stage": "STOPPED", "state": "", "port": None}]}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CLUSTER_ROLE", "CLUSTER_CENTER_URL", "CLUSTER_NODE_ID", "CLUSTER_LAN",
              "CLUSTER_JOIN_TOKEN", "CLUSTER_NODE_TOKEN", "API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLUSTER_CENTER_URL", BASE)
    monkeypatch.setenv("API_KEY", "sk-cli")
    # 聚合视图零 models/ 依赖：根指向空目录，误走本机路径立刻能被形状断言抓住
    import modelctl.core.envfile as ef

    monkeypatch.setattr(ef, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("modelctl.core.profile.PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.chdir(tmp_path)


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


def test_status_cluster_aggregates_nodes_and_goals(probe, capsys) -> None:
    probe.reply("GET", "/cluster/nodes", 200, NODES_BODY)
    probe.reply("GET", "/cluster/goals", 200, GOALS_BODY)
    assert _main(["status", "--cluster"]) == 0
    assert [c[1] for c in probe.calls] == [BASE + "/admin/api/cluster/nodes",
                                           BASE + "/admin/api/cluster/goals"]
    out = capsys.readouterr().out
    # w-1: start×1 且 READY×1 → "1/1"，stop×1 → "1"；w-2 无 goal → "0/0"/"0"
    assert "w-1" in out and "w-2" in out and "1/1" in out and "0/0" in out


def test_list_cluster_groups_by_profile(probe, capsys) -> None:
    probe.reply("GET", "/cluster/nodes", 200, NODES_BODY)
    probe.reply("GET", "/cluster/goals", 200, GOALS_BODY)
    assert _main(["list", "--cluster"]) == 0
    out = capsys.readouterr().out
    assert "qwen" in out and "ds" in out and "READY" in out


def test_center_down_exit2_no_local_fallback(probe, capsys) -> None:
    """中心不可达必须报错退 2：静默回退本机视图 = 假报数据源（spec §3）。"""
    probe.reply("GET", "/cluster/nodes", -1, {"error": "connection refused"})
    assert _main(["status", "--cluster"]) == 2
    assert len(probe.calls) == 1               # 失败即停，不发第二个请求


def test_center_404_exit2(probe) -> None:
    probe.reply("GET", "/cluster/nodes", 404, {"detail": "未启用"})
    assert _main(["list", "--cluster"]) == 2
