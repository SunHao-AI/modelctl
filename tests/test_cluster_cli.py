#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_cluster_cli.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : modelctl cluster CLI 子命令测试（无 HTTP，probe 打桩）
# ===============================================================================
"""cluster init/join/nodes/join-token：exit code、.env 写回、probe 调用参数。"""
from __future__ import annotations

import pytest

from modelctl.core.cluster.store import ClusterStore

CLUSTER_KEYS = ["CLUSTER_ROLE", "CLUSTER_CENTER_URL", "CLUSTER_NODE_ID", "CLUSTER_LAN",
                "CLUSTER_JOIN_TOKEN", "CLUSTER_NODE_TOKEN", "API_KEY"]


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for k in CLUSTER_KEYS:
        monkeypatch.delenv(k, raising=False)
    # .env 读写整体重定向到 tmp（load_env/set_env_values 缺省路径均随之改变）
    import modelctl.core.envfile as ef

    monkeypatch.setattr(ef, "PROJECT_ROOT", tmp_path)


def _main(argv):
    from modelctl import cli

    return cli.main(argv)


def test_init_on_center_creates_join_token(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLUSTER_ROLE", "both")
    assert _main(["cluster", "init"]) == 0
    store = ClusterStore()  # CACHE_DIR 已由 conftest 隔离
    assert store.get_meta("join_token").startswith("JT-")


def test_init_refuses_solo() -> None:
    assert _main(["cluster", "init"]) == 2  # 默认 solo：拒绝并提示先设 CLUSTER_ROLE


def test_join_writes_env_on_success(monkeypatch, tmp_path) -> None:
    import modelctl.core.cluster.center_probe as cp

    monkeypatch.setattr(cp, "check_join", lambda *a, **k: (True, "NT-signed", ""))
    assert _main(["cluster", "join", "--center", "http://c:4173", "--token", "JT-1",
                  "--node-id", "w-1", "--lan", "lan-2"]) == 0
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "CLUSTER_CENTER_URL=http://c:4173" in env_text
    assert "CLUSTER_NODE_ID=w-1" in env_text
    assert "CLUSTER_LAN=lan-2" in env_text
    assert "CLUSTER_ROLE=worker" in env_text
    assert "CLUSTER_NODE_TOKEN=NT-signed" in env_text


def test_join_fails_without_writing(monkeypatch, tmp_path) -> None:
    import modelctl.core.cluster.center_probe as cp

    monkeypatch.setattr(cp, "check_join", lambda *a, **k: (False, "", "token 无效"))
    assert _main(["cluster", "join", "--center", "http://c:4173", "--token", "bad",
                  "--node-id", "w-1"]) == 2
    assert not (tmp_path / ".env").exists()


def test_join_token_rotate_on_center(monkeypatch) -> None:
    monkeypatch.setenv("CLUSTER_ROLE", "both")
    assert _main(["cluster", "init"]) == 0
    store = ClusterStore()
    old = store.get_meta("join_token")
    assert _main(["cluster", "join-token", "--rotate"]) == 0
    assert store.get_meta("join_token") != old


def test_nodes_requires_center_url(monkeypatch) -> None:
    """未设 CLUSTER_CENTER_URL 时回探本机 webui；中心未启用集群（404）→ 退出码 2。

    不打桩会真连 127.0.0.1:WEBUI_PORT：本机若恰有匹配 API_KEY 的 webui 在跑则 rc==0（flaky）。
    """
    import modelctl.core.cluster.center_probe as cp

    monkeypatch.setenv("CLUSTER_ROLE", "both")
    monkeypatch.delenv("CLUSTER_CENTER_URL", raising=False)
    called: list[str] = []

    def fake_get_json(url, api_key="", timeout=5.0):
        called.append(url)
        return 404, {}

    monkeypatch.setattr(cp, "get_json", fake_get_json)
    assert _main(["cluster", "nodes"]) == 2
    assert len(called) == 1, "应只发一次中心请求"
    assert called[0].startswith("http://127.0.0.1"), f"回退 base 应为本机 webui，实际 {called[0]}"
    assert called[0].endswith("/admin/api/cluster/nodes")


def test_center_probe_unreachable_schemeless_url_no_traceback() -> None:
    """center_url 缺 scheme（如 mycenter）：urlopen 的 ValueError 须折叠为不可达，不得裸 traceback。"""
    from modelctl.core.cluster.center_probe import check_join

    ok, node_token, message = check_join("mycenter", "JT-1", "w-1")
    assert ok is False and node_token == ""
    assert "中心不可达" in message
