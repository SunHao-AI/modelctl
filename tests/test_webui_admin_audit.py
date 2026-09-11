#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_webui_admin_audit.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/10 10:00
# @Desc   : 审计 Web UI 端点与前端契约测试（level/keyword 过滤、by_day 聚合、清理响应字段）
# ===============================================================================

"""admin_audit：审计列表/统计/清理端点与 web/src/api/audit.ts 的响应契约。

前端（AuditLogView.vue + types.ts）消费的字段必须与这里一一对应，历史上双方
各写各的：前端传 level/keyword 而后端不认、读 error_count/by_day/removed/freed_bytes
而后端不返回、按 endpoint 过滤而写入侧落的是 path 键——四处错位让级别过滤、
端点过滤、错误数卡片和近 14 天柱状图全部静默失效。
"""
from __future__ import annotations

import datetime as _dt
import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_admin_audit"


@pytest.fixture()
def admin_client(monkeypatch):
    """管理面客户端；AUDIT_DIR 由 conftest autouse 隔离到 tmp_path/audit。"""
    monkeypatch.setenv("API_KEY", KEY)
    logger.remove()
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c


def _audit_dir(tmp_path):
    from modelctl.core.paths import audit_dir

    return audit_dir()


def _write_day(tmp_path, day: _dt.date, entries: list[dict]) -> None:
    """按网关落盘格式写一天的 JSONL（AUDIT_DIR 已由 conftest 指向 tmp_path）。"""
    d = _audit_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, ensure_ascii=False) for e in entries]
    (d / f"modelctl-{day.isoformat()}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _entry(ts: _dt.datetime, *, path="chat/completions", model="q", status=200, error=None) -> dict:
    """与 gateway._build_audit_entry 输出同构的最小审计记录（无 level 字段）。"""
    return {
        "ts": ts.isoformat(timespec="milliseconds"),
        "model": model,
        "engine": "vllm",
        "path": path,
        "stream": False,
        "status_code": status,
        "error": error,
        "prompt_tokens": 10,
        "completion_tokens": 5,
    }


def _get(client, path, **kw):
    return client.get(path, headers={"Authorization": f"Bearer {KEY}"}, params=kw or None)


def _post(client, path, body=None):
    return client.post(path, headers={"Authorization": f"Bearer {KEY}"}, json=body)


def test_list_returns_total_and_error_count_beyond_limit(tmp_path, admin_client):
    """error_count 是过滤后未截断的计数：limit 只裁 entries，不裁统计。"""
    now = _dt.datetime.now().astimezone()
    _write_day(
        tmp_path,
        now.date(),
        [_entry(now, status=200) for _ in range(5)] + [_entry(now, status=502, error="后端不可达")],
    )
    body = _get(admin_client, "/admin/api/audit", limit=2).json()
    assert len(body["entries"]) == 2
    assert body["total"] == 6
    assert body["error_count"] == 1


def test_list_injects_level_derived_from_status(tmp_path, admin_client):
    """JSONL 无 level 字段：后端按 status_code 派生后注入（5xx→error，4xx→warn）。"""
    now = _dt.datetime.now().astimezone()
    _write_day(
        tmp_path,
        now.date(),
        [_entry(now, status=200), _entry(now, status=404, error="未知模型"), _entry(now, status=500)],
    )
    body = _get(admin_client, "/admin/api/audit", limit=10).json()
    by_status = {e["status_code"]: e["level"] for e in body["entries"]}
    assert by_status == {200: "info", 404: "warn", 500: "error"}


def test_list_filters_by_level(tmp_path, admin_client):
    """前端下拉的 level 参数必须真正参与过滤（后端曾完全不认）。"""
    now = _dt.datetime.now().astimezone()
    _write_day(
        tmp_path,
        now.date(),
        [_entry(now, status=200), _entry(now, status=404), _entry(now, status=502)],
    )
    assert _get(admin_client, "/admin/api/audit", level="error").json()["total"] == 1
    assert _get(admin_client, "/admin/api/audit", level="warn").json()["total"] == 1
    assert _get(admin_client, "/admin/api/audit", level="info").json()["total"] == 1


def test_list_filters_by_keyword(tmp_path, admin_client):
    """keyword 在标量字段上做大小写不敏感子串匹配（后端曾完全不认）。"""
    now = _dt.datetime.now().astimezone()
    _write_day(
        tmp_path,
        now.date(),
        [_entry(now, model="qwen3-32b"), _entry(now, model="deepseek-v3")],
    )
    body = _get(admin_client, "/admin/api/audit", keyword="QWEN").json()
    assert body["total"] == 1
    assert body["entries"][0]["model"] == "qwen3-32b"


def test_list_filters_by_endpoint_using_path_key(tmp_path, admin_client):
    """endpoints 过滤器读写入侧真实键 path（历史读 endpoint 恒 None → 过滤永久失效）。"""
    now = _dt.datetime.now().astimezone()
    _write_day(
        tmp_path,
        now.date(),
        [_entry(now, path="chat/completions"), _entry(now, path="messages")],
    )
    body = _get(admin_client, "/admin/api/audit", endpoints="messages").json()
    assert body["total"] == 1
    assert body["entries"][0]["path"] == "messages"


def test_list_since_returns_resolved_value(tmp_path, admin_client):
    """since 参数被接受并回显解析结果（前端 types.ts 声明了该字段）。"""
    now = _dt.datetime.now().astimezone()
    _write_day(tmp_path, now.date(), [_entry(now)])
    body = _get(admin_client, "/admin/api/audit", since="1h").json()
    assert body["since"]
    assert body["total"] == 1


def test_stats_returns_by_day_array(tmp_path, admin_client):
    """stats 必须返回前端画图用的 by_day=[{date,total,error}]（后端历史上只有 by_model）。"""
    today = _dt.datetime.now().astimezone().date()
    yesterday = today - _dt.timedelta(days=1)
    _write_day(
        tmp_path,
        yesterday,
        [_entry(_dt.datetime.now().astimezone(), status=200) for _ in range(3)]
        + [_entry(_dt.datetime.now().astimezone(), status=500)],
    )
    _write_day(tmp_path, today, [_entry(_dt.datetime.now().astimezone(), status=200)])

    body = _get(admin_client, "/admin/api/audit/stats").json()
    assert body["total"] == 5
    assert body["errors"] == 1
    days = {d["date"]: d for d in body["by_day"]}
    assert days[yesterday.isoformat()] == {"date": yesterday.isoformat(), "total": 4, "error": 1}
    assert days[today.isoformat()]["total"] == 1
    assert body["by_model"] == {"q": 5}


def test_stats_honours_since(tmp_path, admin_client):
    """stats 与列表同源：since=2h 时整日早于下限的分片不纳入统计。"""
    now = _dt.datetime.now().astimezone()
    _write_day(tmp_path, now.date(), [_entry(now)])
    _write_day(tmp_path, now.date() - _dt.timedelta(days=5), [_entry(now, model="old")])

    body = _get(admin_client, "/admin/api/audit/stats", since="2h").json()
    assert body["total"] == 1
    assert "old" not in body["by_model"]


def test_cleanup_dry_run_reports_removed_and_freed_bytes(tmp_path, admin_client):
    """dry_run 响应含前端读的 removed/freed_bytes，且不真删文件。"""
    now = _dt.datetime.now().astimezone()
    _write_day(tmp_path, now.date(), [_entry(now)])
    old = now.date() - _dt.timedelta(days=40)
    _write_day(tmp_path, old, [_entry(now, model="old")])

    body = _post(admin_client, "/admin/api/audit/cleanup", {"days": 30, "dry_run": True}).json()
    assert body["ok"] is True
    assert body["dry_run"] is True
    assert body["removed"] == 1
    assert body["deleted"] == [f"modelctl-{old.isoformat()}.jsonl"]
    assert body["freed_bytes"] > 0
    assert (_audit_dir(tmp_path) / f"modelctl-{old.isoformat()}.jsonl").exists()


def test_cleanup_days_param_removes_old_shards(tmp_path, admin_client):
    """days 走 body（前端 auditCleanup 曾把它塞进 query，后端读不到恒按 30 天）。"""
    now = _dt.datetime.now().astimezone()
    keep = now.date() - _dt.timedelta(days=3)
    gone = now.date() - _dt.timedelta(days=10)
    _write_day(tmp_path, now.date(), [_entry(now)])
    _write_day(tmp_path, keep, [_entry(now, model="keep")])
    _write_day(tmp_path, gone, [_entry(now, model="gone")])

    body = _post(admin_client, "/admin/api/audit/cleanup", {"days": 5, "dry_run": False}).json()
    assert body["dry_run"] is False
    assert body["retention_days"] == 5
    assert body["removed"] == 1
    assert (_audit_dir(tmp_path) / f"modelctl-{gone.isoformat()}.jsonl").exists() is False
    assert (_audit_dir(tmp_path) / f"modelctl-{keep.isoformat()}.jsonl").exists()
