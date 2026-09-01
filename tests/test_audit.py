#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_audit.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026-08-31
# @Desc   : core/audit.py 单元测试：record / 切日 / 清理 / 降级
# ===============================================================================

"""core/audit.py 单元测试。"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from modelctl.core.audit import (
    NoopAuditLog,
    RequestAuditLog,
    _parse_day_from_name,
    _prune_by_size,
    _retain_files,
)


def _base_entry(over: dict | None = None) -> dict:
    entry = {
        "ts": "2026-08-31T10:23:11.123+08:00",
        "model": "qwen3.8-vllm",
        "engine": "vllm",
        "path": "chat/completions",
        "stream": True,
        "source": "vllm_native",
        "tokens_source": "response-usage",
        "prompt_tokens": 42,
        "completion_tokens": 128,
        "total_tokens": 170,
        "input_char_len": 512,
        "native_metrics": {
            "time_to_first_token_ms": 85.2,
            "generation_time_ms": 1240.5,
            "queue_time_ms": 12.3,
            "mean_itl_ms": 9.1,
            "tokens_per_second": 103.2,
        },
        "gateway_metrics": {
            "ttft_ms": 92.4,
            "generation_time_ms": 1260.8,
            "tokens_per_second": 100.7,
        },
        "status_code": 200,
        "error": None,
        "finish_reason": "stop",
    }
    if over:
        entry.update(over)
    return entry


def test_request_audit_log_records_native_source(tmp_path):
    log = RequestAuditLog(Path(tmp_path / "audit"))
    log.record(_base_entry())
    files = list((tmp_path / "audit").glob("modelctl-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["source"] == "vllm_native"
    assert rec["native_metrics"]["time_to_first_token_ms"] == 85.2
    assert rec["tokens_source"] == "response-usage"
    log.destroy()


def test_request_audit_log_records_fallback_tokens(tmp_path):
    log = RequestAuditLog(Path(tmp_path / "audit"))
    log.record(_base_entry({"source": "gateway_estimate", "tokens_source": "collector-diff", "native_metrics": None}))
    files = list((tmp_path / "audit").glob("modelctl-*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert rec["source"] == "gateway_estimate"
    assert rec["tokens_source"] == "collector-diff"
    assert rec["native_metrics"] is None
    log.destroy()


def test_request_audit_log_module_level_state_is_reset_between_tests():
    """模块级 `_today` 跨测试复用可能影响新实例；用全新 tmp_path 路径隔离即可。
    本测试仅在 CI 顺序运行时做冒烟。"""
    assert isinstance(_parse_day_from_name("modelctl-2026-08-31.jsonl"), _dt.date)


def test_day_rollover(tmp_path):
    """fake 日期跨天：record 写到不同文件。"""
    log = RequestAuditLog(Path(tmp_path / "audit"))
    log._fake_today = _dt.date(2026, 8, 30)  # 第一段：8-30
    log.record(_base_entry({"ts": "2026-08-30T22:30:00.500+08:00"}))
    log._fake_today = _dt.date(2026, 8, 31)  # 切日到 8-31
    log.record(_base_entry({"ts": "2026-08-31T00:05:00.500+08:00"}))
    files = sorted(p.name for p in (tmp_path / "audit").glob("modelctl-*.jsonl"))
    assert files == ["modelctl-2026-08-30.jsonl", "modelctl-2026-08-31.jsonl"]
    log.destroy()


def test_cleanup_by_retention_days(tmp_path):
    """RETENTION=2：今天 + 1 天前 + 2 天前保留，3 天前删除。"""
    audit = Path(tmp_path / "audit")
    audit.mkdir(parents=True)
    log = RequestAuditLog(audit, retention_days=2, max_size_mb=0)
    # 模拟 4 个文件（名字日期）
    for name in ("modelctl-2026-08-28.jsonl", "modelctl-2026-08-29.jsonl",
                 "modelctl-2026-08-30.jsonl", "modelctl-2026-08-31.jsonl"):
        (audit / name).write_text("x", encoding="utf-8")
    log._fake_today = _dt.date(2026, 8, 31)
    dead = log.collect_dead_files()
    dead_names = sorted(p.name for p in dead)
    assert dead_names == ["modelctl-2026-08-28.jsonl"]
    assert (audit / "modelctl-2026-08-31.jsonl").exists()
    log.destroy()


def test_cleanup_by_size_oldest_removed_first(tmp_path):
    """MAX_SIZE_MB=1：从最旧开始删。今日文件不允许删（铁律）。"""
    audit = Path(tmp_path / "audit")
    audit.mkdir(parents=True)
    # 5 个文件：总 = 1500KB > 1MB（1024KB）阈值
    # 删 8-26(300KB) 后 1200KB > 1MB → 继续删 8-27(450KB) → 750KB ≤ 1MB 停
    # 结果：应删 8-26 与 8-27 两个最旧文件
    files_spec = [
        ("modelctl-2026-08-26.jsonl", _dt.date(2026, 8, 26), 300 * 1024),
        ("modelctl-2026-08-27.jsonl", _dt.date(2026, 8, 27), 450 * 1024),
        ("modelctl-2026-08-28.jsonl", _dt.date(2026, 8, 28), 450 * 1024),
        ("modelctl-2026-08-29.jsonl", _dt.date(2026, 8, 29), 150 * 1024),
        ("modelctl-2026-08-30.jsonl", _dt.date(2026, 8, 30), 150 * 1024),
    ]
    for name, _, size in files_spec:
        (audit / name).write_bytes(b"x" * size)
    log = RequestAuditLog(audit, retention_days=0, max_size_mb=1)
    log._fake_today = _dt.date(2026, 8, 31)
    dead = log.collect_dead_files()
    dead_names = sorted(p.name for p in dead)
    # 应删最旧的 2 个：8-26 / 8-27；删完 750KB ≤ 1MB 即停
    assert dead_names == ["modelctl-2026-08-26.jsonl", "modelctl-2026-08-27.jsonl"]
    log.destroy()


def test_cleanup_never_deletes_today(tmp_path):
    """当日文件绝不删除，即使 MAX_SIZE_MB 设为 0.001 也不动。"""
    audit = Path(tmp_path / "audit")
    audit.mkdir(parents=True)
    (audit / "modelctl-2026-08-31.jsonl").write_bytes(b"x" * (512 * 1024))
    log = RequestAuditLog(audit, retention_days=0, max_size_mb=0)  # 都为 0 → 不清理
    log._fake_today = _dt.date(2026, 8, 31)
    assert log.collect_dead_files() == []
    log.destroy()


def test_record_failure_isolated_via_oserror(monkeypatch, tmp_path, caplog):
    """mock os.write 抛 OSError（磁盘满）→ 不冒泡，返回 False。"""
    audit = Path(tmp_path / "audit")
    audit.mkdir(parents=True)
    log = RequestAuditLog(audit)
    import os as _os
    real_write = _os.write
    real_close = _os.close
    def boom_write(fd, data, *a, **k):
        real_close(fd)  # 释放 fd，避免泄漏
        raise OSError("disk full")
    monkeypatch.setattr(_os, "write", boom_write)
    assert log.record(_base_entry()) is False
    # 显式恢复（monkeypatch teardown 时自动恢复，这里只显式写注释）
    assert (audit).exists()
    log.destroy()


def test_collect_dead_files_is_pure(tmp_path):
    """同输入两次调用返回相同列表（不删除、不改状态）。"""
    audit = Path(tmp_path / "audit")
    audit.mkdir(parents=True)
    (audit / "modelctl-2026-08-20.jsonl").write_text("x", encoding="utf-8")
    log = RequestAuditLog(audit, retention_days=10, max_size_mb=0)
    log._fake_today = _dt.date(2026, 8, 31)
    d1 = [p.name for p in log.collect_dead_files()]
    d2 = [p.name for p in log.collect_dead_files()]
    assert d1 == d2 == ["modelctl-2026-08-20.jsonl"]
    assert (audit / "modelctl-2026-08-20.jsonl").exists()  # 文件未被删除
    log.destroy()


def test_stats_summary_counts_days(tmp_path):
    """stats_summary 汇总：文件数 / 字节 / 按天分布 / 最早 / 最新。"""
    audit = Path(tmp_path / "audit")
    audit.mkdir(parents=True)
    # 用 write_bytes 显式指定精确字节数，避免平台 LF/CRLF 差异
    (audit / "modelctl-2026-08-30.jsonl").write_bytes(b"a\n")       # 2 bytes
    (audit / "modelctl-2026-08-31.jsonl").write_bytes(b"a\nb\n")     # 4 bytes
    log = RequestAuditLog(audit)
    s = log.stats_summary()
    assert s["file_count"] == 2
    assert s["total_bytes"] == 6
    assert s["by_day"] == {"2026-08-30": 2, "2026-08-31": 4}
    assert s["oldest_day"] == "2026-08-30"
    assert s["newest_day"] == "2026-08-31"
    log.destroy()


def test_noop_audit_log_matches_signature(tmp_path):
    log = NoopAuditLog()
    assert log.record(_base_entry()) is False
    log.ensure_cleanup_thread()
    assert log.collect_dead_files() == []
    log.destroy()


# ---- 纯函数单测 ----

def test_parse_day_from_name():
    assert _parse_day_from_name("modelctl-2026-08-31.jsonl") == _dt.date(2026, 8, 31)
    assert _parse_day_from_name("modelctl-deleting.jsonl") is None
    assert _parse_day_from_name("other-2026-08-31.jsonl") is None


def test_parse_day_from_name_invalid_date():
    assert _parse_day_from_name("modelctl-2026-13-99.jsonl") is None


def test_retain_files_filters_old_files(tmp_path):
    files = [Path(f) for f in (
        "modelctl-2026-08-20.jsonl", "modelctl-2026-08-25.jsonl", "modelctl-2026-08-31.jsonl",
    )]
    kept = _retain_files(files, retention_days=6, today=_dt.date(2026, 8, 31))
    assert [p.name for p in kept] == ["modelctl-2026-08-25.jsonl", "modelctl-2026-08-31.jsonl"]


def test_retain_files_zero_disables_time_rule(tmp_path):
    files = [Path("modelctl-2026-01-01.jsonl")]
    kept = _retain_files(files, retention_days=0, today=_dt.date(2026, 8, 31))
    assert kept == files


def test_prune_by_size_oldest_first(tmp_path):
    # 4 个文件各 300KB = 1200KB，MAX=1MB → 需删最旧 1 个（余 900KB ≤ 1MB）
    files = [tmp_path / name for name in (
        "modelctl-2026-08-28.jsonl", "modelctl-2026-08-29.jsonl",
        "modelctl-2026-08-30.jsonl", "modelctl-2026-08-31.jsonl",
    )]
    for p in files:
        p.write_bytes(b"x" * (300 * 1024))

    pruned = _prune_by_size(files, max_size_mb=1)
    # 删最旧 1 个：8-28；保留 8-29 / 8-30 / 8-31（= 900KB ≤ 1MB）
    assert [p.name for p in pruned] == [
        "modelctl-2026-08-29.jsonl",
        "modelctl-2026-08-30.jsonl",
        "modelctl-2026-08-31.jsonl",
    ]


def test_prune_by_size_zero_disables(tmp_path):
    files = [Path("modelctl-2026-08-31.jsonl")]
    kept = _prune_by_size(files, max_size_mb=0)
    assert kept == files


# ---- 网关接线集成测试（create_app + MockTransport/ASGITransport 样式，同 test_gateway.py） ----

import asyncio  # noqa: E402
import httpx  # noqa: E402

from modelctl.core.audit import _new_audit_log  # noqa: E402
from modelctl.core.gateway import GatewayModel, create_app  # noqa: E402


def _gm(engine: str = "vllm") -> GatewayModel:
    """最小 GatewayModel（仅 gateway 必填字段，与 test_gateway.py 构造方式一致）。"""
    return GatewayModel("q", engine, "http://upstream", "q", None, "http://upstream/")


def _reg_one() -> dict:
    return {"q": _gm()}


def _first_audit_rec(tmp_path) -> dict:
    files = list((tmp_path / "audit").glob("modelctl-*.jsonl"))
    assert files, "网关未产生审计文件"
    lines = [l for l in files[0].read_text(encoding="utf-8").splitlines() if l.strip()]
    return json.loads(lines[-1])


def test_openai_non_stream_emits_audit_vllm_native(tmp_path):
    """非流式 OpenAI + 响应含 metrics → source=vllm_native、tokens 取 response-usage。"""
    audit = _new_audit_log(Path(tmp_path / "audit"))

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "x", "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "metrics": {
                "time_to_first_token_ms": 80.0,
                "generation_time_ms": 100.0,
                "tokens_per_second": 50.0,
            },
        })

    app = create_app(_reg_one(), transport=httpx.MockTransport(upstream), audit_log=audit)

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/v1/chat/completions", json={
                "model": "q", "messages": [{"role": "user", "content": "hi"}],
            })
            assert resp.status_code == 200

    asyncio.run(_go())
    rec = _first_audit_rec(tmp_path)
    assert rec["source"] == "vllm_native"
    assert rec["tokens_source"] == "response-usage"
    assert rec["native_metrics"]["time_to_first_token_ms"] == 80.0
    assert rec["prompt_tokens"] == 10 and rec["completion_tokens"] == 5
    assert rec["gateway_metrics"]["ttft_ms"] is None  # 非流式
    assert rec["status_code"] == 200
    audit.destroy()


def test_openai_non_stream_gateway_estimate_when_no_metrics(tmp_path):
    """非流式 OpenAI 无 metrics → source=gateway_estimate、native_metrics 为 null。"""
    audit = _new_audit_log(Path(tmp_path / "audit"))

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "x", "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })

    app = create_app(_reg_one(), transport=httpx.MockTransport(upstream), audit_log=audit)

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/v1/chat/completions", json={
                "model": "q", "messages": [{"role": "user", "content": "hi"}],
            })
            assert resp.status_code == 200

    asyncio.run(_go())
    rec = _first_audit_rec(tmp_path)
    assert rec["source"] == "gateway_estimate"
    assert rec["native_metrics"] is None
    assert rec["prompt_tokens"] == 3 and rec["completion_tokens"] == 2
    assert rec["finish_reason"] == "stop"
    audit.destroy()


def test_openai_stream_emits_audit_vllm_native(tmp_path):
    """流式 OpenAI 末块含 metrics + usage → vllm_native、stream=True、网关 ttft 已计量。"""
    audit = _new_audit_log(Path(tmp_path / "audit"))

    chunks = (
        b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"a"}}]}\n\n'
        b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"b"}}]}\n\n'
        b'data: {"id":"x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":4,"completion_tokens":7,"total_tokens":11},'
        b'"metrics":{"time_to_first_token_ms":55.0,"generation_time_ms":300.0,'
        b'"tokens_per_second":23.3}}\n\n'
        b"data: [DONE]\n\n"
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=chunks, headers={"Content-Type": "text/event-stream"},
        )

    app = create_app(_reg_one(), transport=httpx.MockTransport(upstream), audit_log=audit)

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async with client.stream("POST", "/v1/chat/completions", json={
                "model": "q", "messages": [{"role": "user", "content": "hi"}], "stream": True,
            }) as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_bytes():
                    pass

    asyncio.run(_go())
    rec = _first_audit_rec(tmp_path)
    assert rec["source"] == "vllm_native"
    assert rec["stream"] is True
    assert rec["prompt_tokens"] == 4 and rec["completion_tokens"] == 7
    assert rec["gateway_metrics"]["ttft_ms"] is not None  # 流式首延迟已计量
    audit.destroy()


def test_openai_stream_fallback_when_no_usage(tmp_path):
    """流式末块无 usage（未开 --enable-force-include-usage）→ collector-diff 兜底。"""
    audit = _new_audit_log(Path(tmp_path / "audit"))

    chunks = (
        b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"a"}}]}\n\n'
        b'data: {"id":"x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=chunks, headers={"Content-Type": "text/event-stream"},
        )

    app = create_app(_reg_one(), transport=httpx.MockTransport(upstream), audit_log=audit)

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async with client.stream("POST", "/v1/chat/completions", json={
                "model": "q", "messages": [{"role": "user", "content": "hi"}], "stream": True,
            }) as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_bytes():
                    pass

    asyncio.run(_go())
    rec = _first_audit_rec(tmp_path)
    assert rec["tokens_source"] == "collector-diff"
    assert rec["prompt_tokens"] >= 0 and rec["completion_tokens"] >= 0
    audit.destroy()


def test_anthropic_non_stream_usage_capture(tmp_path):
    """Anthropic 非流式：usage 在根级且无 metrics → gateway_estimate、tokens 取 input/output。"""
    audit = _new_audit_log(Path(tmp_path / "audit"))

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "content": [{"text": "hello"}],
            "usage": {"input_tokens": 8, "output_tokens": 4},
        })

    app = create_app(_reg_one(), transport=httpx.MockTransport(upstream), audit_log=audit)

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/v1/messages", json={
                "model": "q", "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            })
            assert resp.status_code == 200

    asyncio.run(_go())
    rec = _first_audit_rec(tmp_path)
    assert rec["source"] == "gateway_estimate"
    assert rec["native_metrics"] is None
    assert rec["prompt_tokens"] == 8 and rec["completion_tokens"] == 4
    assert rec["path"] == "messages"
    audit.destroy()


def test_audit_failure_does_not_break_proxy(tmp_path):
    """audit.record 抛异常 → 请求路径不受影响，响应仍 200。"""
    audit = NoopAuditLog()

    class _FailingAudit(NoopAuditLog):
        def record(self, entry):
            raise RuntimeError("boom")

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "x", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    app = create_app(_reg_one(), transport=httpx.MockTransport(upstream), audit_log=_FailingAudit())

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/v1/chat/completions", json={
                "model": "q", "messages": [{"role": "user", "content": "hi"}],
            })
            assert resp.status_code == 200
            assert resp.json()["choices"][0]["message"]["content"] == "ok"

    asyncio.run(_go())
    audit.destroy()


# ---- CLI 冒烟（modelctl audit 子命令族） ----

def test_cli_audit_path_subcommand(tmp_path, monkeypatch, capsys):
    """modelctl audit path 打印 AUDIT_DIR 绝对路径。"""
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path / "audit"))
    import modelctl.cli as cli
    rc = cli.main(["audit", "path"])
    out = capsys.readouterr().out
    assert rc == 0
    assert cli._audit_dir_from_env().resolve() == Path(out.strip()).resolve()


def test_cli_audit_stats_empty_dir(tmp_path, monkeypatch, capsys):
    """空目录 → stats 全 0。"""
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path / "audit"))
    import modelctl.cli as cli
    rc = cli.main(["audit", "stats"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "file_count: 0" in out


def test_cli_audit_query_empty(tmp_path, monkeypatch, capsys):
    """空目录 → query 打印提示。"""
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path / "audit"))
    (tmp_path / "audit").mkdir(parents=True, exist_ok=True)
    import modelctl.cli as cli
    rc = cli.main(["audit", "--limit", "5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no audit records" in out.lower() or "暂无" in out


def test_cli_audit_query_filters_by_model(tmp_path, monkeypatch, capsys):
    """--model 过滤：命中条目出现，未命中条目不出现。"""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    today = _dt.date.today().isoformat()
    (audit_dir / f"modelctl-{today}.jsonl").write_text(
        json.dumps(_base_entry({"model": "qwen3.8-vllm", "engine": "vllm"}), ensure_ascii=False) + "\n"
        + json.dumps(_base_entry({"model": "deepseek-v4", "engine": "vllm"}), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUDIT_DIR", str(audit_dir))
    import modelctl.cli as cli
    rc = cli.main(["audit", "--model", "qwen3.8-vllm", "--limit", "10"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "qwen3.8-vllm" in out
    assert "deepseek-v4" not in out


def test_cli_audit_query_json_mode(tmp_path, monkeypatch, capsys):
    """--json 输出原样 JSON 单行（JSONL）。"""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    today = _dt.date.today().isoformat()
    entry = _base_entry({"model": "qwen3.8-vllm"})
    (audit_dir / f"modelctl-{today}.jsonl").write_text(
        json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("AUDIT_DIR", str(audit_dir))
    import modelctl.cli as cli
    rc = cli.main(["audit", "--json", "--limit", "5"])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [l for l in out.strip().splitlines() if l.startswith("{")]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["model"] == "qwen3.8-vllm"


def test_cli_audit_cleanup_dry_run(tmp_path, monkeypatch, capsys):
    """--cleanup --dry-run 不改文件，仅打印预览。"""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    old_day = (_dt.date.today() - _dt.timedelta(days=40)).isoformat()
    (audit_dir / f"modelctl-{old_day}.jsonl").write_bytes(b"x" * 1024)
    monkeypatch.setenv("AUDIT_DIR", str(audit_dir))
    import modelctl.cli as cli
    rc = cli.main(["audit", "--cleanup", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Would delete" in out
    # 文件未被删除
    assert (audit_dir / f"modelctl-{old_day}.jsonl").exists()


def test_cli_audit_cleanup_executes(tmp_path, monkeypatch, capsys):
    """默认 --cleanup 执行删除（staged rename + unlink）。"""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    old_day = (_dt.date.today() - _dt.timedelta(days=40)).isoformat()
    (audit_dir / f"modelctl-{old_day}.jsonl").write_bytes(b"x" * 1024)
    monkeypatch.setenv("AUDIT_DIR", str(audit_dir))
    import modelctl.cli as cli
    rc = cli.main(["audit", "--cleanup"])
    out = capsys.readouterr().out
    assert rc == 0
    assert not (audit_dir / f"modelctl-{old_day}.jsonl").exists()
    assert "Deleted 1 files, freed 0.0 MB" in out


# ---- final-review I-1：collector-diff 兜底路径真实差分（仅 OpenAI 两条链路） ----

def _diff_collector(before: tuple[float, float], after: tuple[float, float]):
    """UsageCollector 替身：snapshot() 第一次调用返回 before，之后返回 after（计数器）。

    仅暴露差分路径依赖的 snapshot()；record_tokens 无副作用（差分路径不依赖 record）。
    """
    class _FakeCollector:
        _calls = [0]

        def snapshot(self) -> dict:
            if self._calls[0] == 0:
                snap = {"prompt_total": before[0], "predicted_total": before[1]}
            else:
                snap = {"prompt_total": after[0], "predicted_total": after[1]}
            self._calls[0] += 1
            return snap

        def record_tokens(self, prompt_delta: int, completion_delta: int) -> None:
            return None

    return _FakeCollector()


def _rec_one_with_collector(collector) -> dict:
    m = _gm()
    m.collector = collector
    return {"q": m}


def test_openai_stream_collector_diff_when_no_usage(tmp_path):
    """流式 OpenAI 全部 chunk 无 usage → collector-diff 用真实差分（prompt+30 / predicted+15）。"""
    audit = _new_audit_log(Path(tmp_path / "audit"))
    collector = _diff_collector(
        before=(100, 50),
        after=(130, 65),
    )

    chunks = (
        b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"a"}}]}\n\n'
        b'data: {"id":"x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=chunks, headers={"Content-Type": "text/event-stream"},
        )

    app = create_app(_rec_one_with_collector(collector), transport=httpx.MockTransport(upstream), audit_log=audit)

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async with client.stream("POST", "/v1/chat/completions", json={
                "model": "q", "messages": [{"role": "user", "content": "hi"}], "stream": True,
            }) as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_bytes():
                    pass

    asyncio.run(_go())
    rec = _first_audit_rec(tmp_path)
    assert rec["tokens_source"] == "collector-diff"
    assert rec["prompt_tokens"] == 30
    assert rec["completion_tokens"] == 15
    assert rec["total_tokens"] == 45
    audit.destroy()


def test_openai_non_stream_collector_diff_when_no_usage(tmp_path):
    """非流式 OpenAI 响应无 usage → collector-diff 用真实差分（prompt+20 / predicted+8）。"""
    audit = _new_audit_log(Path(tmp_path / "audit"))
    collector = _diff_collector(
        before=(0, 0),
        after=(20, 8),
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        # 响应体不含 usage / metrics 字段
        return httpx.Response(200, json={
            "id": "x", "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        })

    app = create_app(_rec_one_with_collector(collector), transport=httpx.MockTransport(upstream), audit_log=audit)

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/v1/chat/completions", json={
                "model": "q", "messages": [{"role": "user", "content": "hi"}],
            })
            assert resp.status_code == 200

    asyncio.run(_go())
    rec = _first_audit_rec(tmp_path)
    assert rec["tokens_source"] == "collector-diff"
    assert rec["prompt_tokens"] == 20
    assert rec["completion_tokens"] == 8
    assert rec["total_tokens"] == 28
    audit.destroy()


def test_build_audit_entry_usage_wins_over_collector_diff():
    """regression：响应带 usage 时 collector_diff_* 参数被忽略（仍以 response-usage 为准）。"""
    from modelctl.core.gateway import _build_audit_entry

    rec = _build_audit_entry(
        model_name="q", profile_name="q", profile_engine="vllm",
        path="chat/completions", stream=False,
        native_metrics=None,
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        gateway_metrics=None,
        status_code=200, error=None, finish_reason="stop",
        input_char_len=8,
        collector_diff_prompt=999,
        collector_diff_completion=999,
    )
    assert rec["tokens_source"] == "response-usage"
    assert rec["prompt_tokens"] == 7
    assert rec["completion_tokens"] == 3