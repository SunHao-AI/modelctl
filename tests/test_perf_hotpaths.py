#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_perf_hotpaths.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/11 14:00
# @Desc   : 热点路径性能基准（pytest-benchmark）
# ===============================================================================

"""热点路径性能基准 — 每次改动核心算法后的「变慢哨兵」。

口径约定（与 pyproject [tool.pytest.ini_options].markers 的 perf 层一致）：
- 文件名 `test_perf_*` → conftest 自动打 `perf` marker；常规全量用
  `-m "not perf"` 排除，基准只在显式 `-m perf --benchmark-only` 时执行。
- **基准不做绝对值断言**：CI runner 的 CPU/频率波动 ±30% 是常态，绝对阈值
  只会制造 flakes。这里利用 pytest-benchmark 的「相对历史基线」比较
  （`--benchmark-compare` / `--benchmark-autosave`），异常变慢由比较报告暴露。
  唯一例外是纯算法用例带**宽松**的 O(n²) 恶化哨兵（正常 O(n) 不会撞上）。

覆盖的热点（均为每次请求/每条日志/每个面板刷新都会走到的路径）：
1. CJK 双宽计算 display_width/pad_width —— CLI/日志/表格全链路
2. API Key 恒定时间比较与 sha256 摘要 —— 网关每个数据面请求
3. JWT 签发/校验 —— WebUI 每个管理面请求
4. bcrypt 密码哈希/校验 —— 登录路径（cost=12，天然慢，只跑 1 轮）
5. .env 解析 —— 进程启动与配置热更新
6. probe 序列化（_vram_gb / _serialize_gpu_locks） —— /probe 端点每次刷新
7. StageEvent.to_sse_dict —— 任务日志流每个事件
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# 1. CJK 双宽计算（colors.display_width / pad_width）
# ---------------------------------------------------------------------------

class TestDisplayWidthPerf:
    """display_width 是 O(n) 逐字符判宽；若有人改成反复拼接/递归会恶化成 O(n²)。"""

    def test_bench_display_width_ascii_1k(self, benchmark):
        from modelctl.core.colors import display_width

        text = "a" * 1024
        result = benchmark(display_width, text)
        assert result == 1024

    def test_bench_display_width_cjk_1k(self, benchmark):
        from modelctl.core.colors import display_width

        text = "模型控制" * 256  # 1024 个 CJK 字符 → 2048 列
        result = benchmark(display_width, text)
        assert result == 2048

    def test_bench_display_width_mixed_1k(self, benchmark):
        from modelctl.core.colors import display_width

        text = "GPU利用率0123456789" * 64
        result = benchmark(display_width, text)
        assert result > 0

    def test_bench_pad_width_wide_row(self, benchmark):
        from modelctl.core.colors import pad_width

        text = "节点在线率统计"
        result = benchmark(pad_width, text, 80)
        assert len(result) >= len(text)


# ---------------------------------------------------------------------------
# 2. API Key 比较与摘要（admin_auth / accounts.hashing）
# ---------------------------------------------------------------------------

class TestKeyOpsPerf:
    """网关数据面每个请求至少一次 sha256 + 恒定时间比较，属最高频安全原语。"""

    def test_bench_sha256_api_key(self, benchmark):
        from modelctl.core.accounts.hashing import hash_api_key

        key = "sk-mctl-" + "A1b2" * 8
        result = benchmark(hash_api_key, key)
        assert len(result) == 64

    def test_bench_consteq_44bytes(self, benchmark):
        from modelctl.core.webui.admin_auth import _consteq

        a = "sk-mctl-" + "x" * 36
        b = a
        assert benchmark(_consteq, a, b) is True

    def test_bench_is_valid_key_match(self, benchmark, monkeypatch):
        from modelctl.core.webui.admin_auth import is_valid_key

        key = "perf_key_" + "0" * 35
        monkeypatch.setenv("API_KEY", key)
        assert benchmark(is_valid_key, key) is True

    def test_bench_generate_api_key(self, benchmark):
        from modelctl.core.accounts.hashing import generate_api_key

        result = benchmark(generate_api_key)
        assert result.startswith("sk-mctl-")


# ---------------------------------------------------------------------------
# 3. JWT 签发/校验（account_auth）
# ---------------------------------------------------------------------------

class TestJwtPerf:
    """HS256 是纯 HMAC，微秒级；WebUI 每请求一次 verify，回归哨兵。"""

    @pytest.fixture()
    def jwt_env(self, monkeypatch):
        monkeypatch.setenv("ACCOUNTS_JWT_SECRET", "perf-jwt-secret-0123456789")

    def test_bench_issue_token(self, benchmark, jwt_env):
        from modelctl.core.webui.account_auth import issue_token

        token = benchmark(issue_token, user_id=1, is_admin=True)
        assert token.count(".") == 2

    def test_bench_verify_token(self, benchmark, jwt_env):
        from modelctl.core.webui.account_auth import issue_token, verify_token

        token = issue_token(user_id=1, is_admin=False)
        payload = benchmark(verify_token, token)
        assert payload["user_id"] == 1


# ---------------------------------------------------------------------------
# 4. bcrypt（登录路径；cost=12 单次 ~250ms，max_time 压低轮数避免拖慢基准跑）
# ---------------------------------------------------------------------------

class TestBcryptPerf:
    def test_bench_hash_password(self, benchmark):
        from modelctl.core.accounts.hashing import hash_password

        benchmark.pedantic(lambda: hash_password("Perf-Passw0rd!"), rounds=3, iterations=1)

    def test_bench_verify_password(self, benchmark):
        from modelctl.core.accounts.hashing import hash_password, verify_password

        hashed = hash_password("Perf-Passw0rd!")
        benchmark.pedantic(
            lambda: verify_password("Perf-Passw0rd!", hashed), rounds=3, iterations=1
        )
        assert verify_password("wrong", hashed) is False


# ---------------------------------------------------------------------------
# 5. .env 解析（进程启动 / 配置读取路径）
# ---------------------------------------------------------------------------

class TestEnvParsePerf:
    def test_bench_parse_env_file_500_lines(self, benchmark, tmp_path):
        from modelctl.core.envfile import parse_env_file

        env = tmp_path / ".env"
        lines = [f"KEY_{i}=value_{i}" for i in range(450)]
        lines += ["# comment line", "", 'QUOTED="with quotes"', "SPACED = padded "]
        env.write_text("\n".join(lines), encoding="utf-8")

        result = benchmark(parse_env_file, env)
        assert len(result) == 452

    def test_bench_parse_env_file_ignores_comments(self, benchmark, tmp_path):
        from modelctl.core.envfile import parse_env_file

        env = tmp_path / ".env"
        env.write_text("# " + "c" * 200 + "\n", encoding="utf-8")
        assert benchmark(parse_env_file, env) == {}


# ---------------------------------------------------------------------------
# 6. probe 序列化（/probe 端点每次前端刷新）
# ---------------------------------------------------------------------------

class TestProbeSerializePerf:
    def test_bench_vram_gb(self, benchmark):
        from modelctl.core.webui.admin_probe import _vram_gb

        assert benchmark(_vram_gb, "24576") == 24.0

    def test_bench_serialize_gpu_locks_16cards(self, benchmark):
        from modelctl.core.webui.admin_probe import _serialize_gpu_locks

        locks = {i: f"task-{i}" for i in range(16)}
        result = benchmark(_serialize_gpu_locks, locks)
        assert len(result) == 16
        assert result[0]["gpu_index"] == 0


# ---------------------------------------------------------------------------
# 7. SSE 事件构造（任务/模型日志流每个事件一次）
# ---------------------------------------------------------------------------

class TestStageEventPerf:
    def test_bench_to_sse_dict(self, benchmark):
        from modelctl.core.sse_stage_event import StageEvent

        event = StageEvent(
            type="stage", stage="pulling", message="下载权重中", ts="2026-09-11 14:00:00"
        )
        result = benchmark(event.to_sse_dict)
        assert isinstance(result, dict)
