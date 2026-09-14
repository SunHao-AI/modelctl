"""cluster/events.py event_text 全 kind 分支补测（纯函数，零打桩）。

展示文本是"中心单端拼装，前端/CLI 零加工"契约的唯一实现点；每个 kind 一句中文，
此前只有 3 条钉子（词表守卫 + 两条 poison 不抛）。本文件把 19 个 kind + 兜底分支
全部钉死，并对"payload 形状任意"的对端输入做类型过滤断言。
"""

from __future__ import annotations

import pytest

from modelctl.core.cluster import events


class TestKindVocabulary:
    def test_is_known_kind(self):
        assert events.is_known_kind("goal.create")
        assert not events.is_known_kind("goal.whatever")

    def test_every_kind_has_a_dedicated_text(self):
        """词表守卫：每个登记 kind 必须命中专属文案（兜底文案以 kind 原样开头）。"""
        for kind in events.EVENT_KINDS:
            text = events.event_text({"kind": kind, "payload": {}})
            if kind in {"node.join", "node.sync", "goal.drift"}:
                continue  # 这三条专属文案不含 kind 字面量，单独钉
            assert not text.startswith(kind + " "), f"{kind} 落进了兜底分支（专属文案缺失）"


class TestEventTextPerKind:
    @pytest.mark.parametrize("row,expect", [
        ({"kind": "goal.create",
          "payload": {"profile": "a", "intent": "run", "operator": "bob"}},
         "创建目标（a intent→run；操作者 bob）"),
        ({"kind": "goal.create", "payload": {}},
         "创建目标（? intent→?；操作者 -）"),
        ({"kind": "goal.update",
          "payload": {"fields": ["a", 1, {"x": 1}], "profile": "p"}},
         "更新目标（p 字段 a,1；操作者 -）"),  # 非 str/int 元素逐项过滤
        ({"kind": "goal.update", "payload": {"fields": "notalist"}},
         "更新目标（? 字段 notalist；操作者 -）"),  # 非 list → 包成单元素
        ({"kind": "goal.update", "payload": {}},
         "更新目标（? 字段 -；操作者 -）"),
        ({"kind": "goal.delete", "payload": {"profile": "p", "operator": "op"}},
         "撤销目标（p；操作者 op）"),
        ({"kind": "goal.retry", "payload": {"queued": True}},
         "人工重试（排队=是；操作者 -）"),
        ({"kind": "goal.retry", "payload": {}}, "人工重试（排队=否；操作者 -）"),
        ({"kind": "goal.drift", "payload": {}},
         "漂移：worker 本地 profile 与中心声明不一致"),
        ({"kind": "action.result", "payload": {"ok": True, "seq": 3, "detail": "done"}},
         "指令回执（seq=3 成功）done"),
        ({"kind": "action.result", "payload": {"ok": False, "seq": 4}},
         "指令回执（seq=4 失败）"),
        ({"kind": "action.result", "payload": {"ok": None, "seq": 7, "detail": "x" * 200}},
         f"指令回执（seq=7 未知）{'x' * 120}"),  # 三态未知 + detail 120 截断
        ({"kind": "node.join", "payload": {}}, "节点接入（WS hello）"),
        ({"kind": "node.join_check", "payload": {"result": "ok"}}, "join 预检（ok）"),
        ({"kind": "node.sync", "payload": {"operator": "alice"}},
         "强制全量同步（操作者 alice）"),
        ({"kind": "node.model_action", "payload": {"verb": "start", "queued": True}},
         "远程指令 start（排队=是）"),
        ({"kind": "node.model_action", "payload": {}}, "远程指令 -（排队=否）"),
        ({"kind": "token.rotate", "payload": {"scope": "join"}}, "轮换 join token"),
        ({"kind": "token.rotate", "payload": {}}, "轮换 节点 token"),
        ({"kind": "node.heartbeat", "payload": {}}, "心跳"),
        ({"kind": "node.disable", "payload": {"kicked": True}}, "禁用节点（顺带断连）"),
        ({"kind": "node.disable", "payload": {}}, "禁用节点"),
        ({"kind": "node.enable", "payload": {}}, "解除禁用"),
        ({"kind": "node.kick", "payload": {"kicked": True}}, "主动踢除连接"),
        ({"kind": "node.kick", "payload": {}}, "主动踢除连接（当时无连接）"),
        ({"kind": "node.retire", "payload": {"removed_goals": 3}},
         "节点退役（连带撤销 3 个目标）"),
        ({"kind": "db.backup", "payload": {"bytes": 4096}}, "台账备份（4096 字节）"),
        ({"kind": "db.backup", "payload": {}}, "台账备份（- 字节）"),
        ({"kind": "db.restore", "payload": {"source": "bak.db"}},
         "台账自备份恢复而来（源 bak.db）"),
        ({"kind": "goal.sync_overflow", "payload": {"bytes": 9, "limit": 8}},
         "快照超限未下发（9 > 上限 8）"),
    ])
    def test_dedicated_texts(self, row, expect):
        assert events.event_text(row) == expect


class TestFallbackAndPoison:
    def test_unknown_kind_digest_first_four(self):
        row = {"kind": "weird.kind",
               "payload": {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}}
        assert events.event_text(row) == "weird.kind a=1 b=2 c=3 d=4"  # 兜底只取前 4 项

    def test_unknown_kind_empty_payload(self):
        assert events.event_text({"kind": "weird.kind"}) == "weird.kind"

    def test_kind_missing_is_empty_digest(self):
        assert events.event_text({"payload": {}}) == ""

    @pytest.mark.parametrize("payload", ["not-a-dict", 42, None, ["list"]])
    def test_payload_non_dict_never_raises(self, payload):
        text = events.event_text({"kind": "goal.create", "payload": payload})
        assert isinstance(text, str) and text  # _p 兜底 {} → 专属文案仍成形

    def test_kind_non_string_coerced(self):
        assert events.event_text({"kind": 123}) == "123"

    @pytest.mark.parametrize("kind", sorted(events.EVENT_KINDS))
    def test_all_kinds_with_garbage_payload_never_raise(self, kind):
        for payload in ({}, {"profile": None}, {"fields": [None, [1]]}, "junk"):
            assert isinstance(events.event_text({"kind": kind, "payload": payload}), str)
