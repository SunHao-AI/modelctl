#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : tests/test_webui_admin_probe.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/11 10:00
# @Desc   : 系统概览与硬件体检端点测试
# ===============================================================================

"""core/webui/admin_probe.py 测试（/login /health /overview /probe + 四个 helper）。

分层理由：/overview 与 /probe 是「capabilities + gpu_lock + profile + admin_models
build_summaries」的聚合点，跨模块契约只能在挂载后的 app 上验证，故本文件整体归
integration（文件名 test_webui_* 前缀由 conftest 自动打 marker）。

/test_probe 的断言重点是**契约稳定性**而非探测真值：前端 ProbeView 直接按固定键渲染，
字段缺失或类型变化会让整块 UI 空白，因此五区块的键集合与归一化行为（None→""、
非 list 显存→单元素 list、总显存优先 per-GPU 求和）是必须钉住的行为。
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from modelctl.core.capabilities import Capabilities  # noqa: E402
from modelctl.core.gateway import create_app  # noqa: E402
from modelctl.core.webui.admin_probe import (  # noqa: E402
    _engine_binary_entry,
    _serialize_engine_binaries,
    _serialize_gpu_locks,
    _vram_gb,
)

KEY = "probe_test_key_0001"
CLIENT_KEY = "sk-probe-client-9d21"


@pytest.fixture()
def admin_client(monkeypatch):
    """管理面客户端：API_KEY + 网关客户端 key 都注入，绕开各端点的 fail-closed。"""
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", CLIENT_KEY)
    app = create_app(admin=True)
    with TestClient(app) as c:
        yield c


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {KEY}"}


# ---------------------------------------------------------------------------
# helper：_vram_gb
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (24576, 24.0),          # 24G 卡：整除
        (24564, 24.0),          # 非整除按 1 位四舍五入
        (512, 0.5),             # 小于 1G 保留小数
        ("8192", 8.0),          # 字符串数字（nvidia-smi 输出即字符串）
        ("  8192  ", 8.0),      # 两端空白容忍
        ("8192.0", 8.0),        # 浮点字符串（JSON 反序列化常见形态）
        (0, 0.0),
        ("", 0.0),              # 空串 → 0.0（非数字兜底）
        ("N/A", 0.0),           # 驱动异常时的非数字列
        (None, 0.0),
        ([], 0.0),              # 不可解析的类型也不得上抛
    ],
)
def test_vram_gb_normalizes_and_never_raises(raw, expected):
    assert _vram_gb(raw) == expected


def test_vram_gb_accepts_native_float():
    """原生 float 入参不得被兜底成 0.0（BUG-PRB-01 回归钉）。

    旧实现 `int(str(x))` 对 24576.7 抛 ValueError → 返回 0.0，与「真的是 0 显存」
    混成同一返回值，UI 静默显示 0.0 GB。改用 float() 后按 1 位小数正常换算。
    """
    assert _vram_gb(24576.7) == 24.0
    assert _vram_gb(512.0) == 0.5


# ---------------------------------------------------------------------------
# helper：_serialize_gpu_locks
# ---------------------------------------------------------------------------


def test_serialize_gpu_locks_sorts_by_index_and_json_serializable():
    """{gpu_index: owner} → 按卡位升序的 list，且键类型稳定（int/str）。"""
    locks = {3: "qwen3.8", 0: "deepseek-v4-flash", 1: "kimi-k2.5"}
    out = _serialize_gpu_locks(locks)
    assert [e["gpu_index"] for e in out] == [0, 1, 3]
    assert out[0] == {"gpu_index": 0, "owner": "deepseek-v4-flash"}
    # 前端按数组渲染占卡提示：空锁必须是空列表而非 None
    assert _serialize_gpu_locks({}) == []


def test_serialize_gpu_locks_accepts_str_keys():
    """锁文件反序列化后键可能是 str，须归一为 int（前端按数值排序渲染）。"""
    assert _serialize_gpu_locks({"2": "a"}) == [{"gpu_index": 2, "owner": "a"}]


# ---------------------------------------------------------------------------
# helper：_engine_binary_entry / _serialize_engine_binaries（docker ∨ venv 可达性）
# ---------------------------------------------------------------------------


def test_engine_binary_entry_venv_available_marks_runtime_venv(monkeypatch):
    monkeypatch.setattr("modelctl.core.capabilities.docker_ready", lambda: False)
    e = _engine_binary_entry("vllm", True, "/venvs/vllm/bin/vllm")
    assert e == {
        "name": "vllm",
        "available": True,
        "path": "/venvs/vllm/bin/vllm",
        "reachable": True,
        "runtime": "venv",
        "docker_capable": True,
        "docker_ready": False,
    }


def test_engine_binary_entry_docker_capable_engine_reachable_without_venv(monkeypatch):
    """yaml 配了 docker_image 的引擎在 venv 缺失时仍可达（docker 主路径优先）。

    这是本函数的存在理由：只按 venv 判定会让「容器绕过」路线被 UI 误报成
    「需先 modelctl env setup <engine>」。
    """
    monkeypatch.setattr("modelctl.core.capabilities.docker_ready", lambda: True)
    e = _engine_binary_entry("vllm", False, None)
    assert e["available"] is False
    assert e["reachable"] is True
    assert e["runtime"] == "docker"


def test_engine_binary_entry_non_docker_engine_stays_unreachable(monkeypatch):
    """不支持 docker 的引擎（llamacpp/unsloth）即使 docker 就绪也不可达。"""
    monkeypatch.setattr("modelctl.core.capabilities.docker_ready", lambda: True)
    e = _engine_binary_entry("llamacpp", False, None)
    assert e["reachable"] is False
    assert e["runtime"] is None


def test_engine_binary_entry_docker_not_ready_leaves_no_runtime(monkeypatch):
    monkeypatch.setattr("modelctl.core.capabilities.docker_ready", lambda: False)
    e = _engine_binary_entry("vllm", False, None)
    assert (e["reachable"], e["runtime"]) == (False, None)


def test_serialize_engine_binaries_pairs_binaries_with_paths(monkeypatch):
    """binaries 决定条目集合，binary_paths 只补 path；缺失路径的引擎也得有条目。"""
    monkeypatch.setattr("modelctl.core.capabilities.docker_ready", lambda: False)

    class _Caps:
        binaries = {"vllm": True, "sglang": False}
        binary_paths = {"vllm": "/p/vllm"}  # sglang 无路径

    out = _serialize_engine_binaries(_Caps())
    assert [e["name"] for e in out] == ["vllm", "sglang"]
    assert out[0]["path"] == "/p/vllm"
    assert out[1] == {
        "name": "sglang",
        "available": False,
        "path": None,
        "reachable": False,
        "runtime": None,
        "docker_capable": False,
        "docker_ready": False,
    }


def test_serialize_engine_binaries_tolerates_missing_attributes():
    """caps 缺 binaries/binary_paths 属性时返回空列表（getattr 兜底），不抛。"""

    class _Bare:
        pass

    assert _serialize_engine_binaries(_Bare()) == []


# ---------------------------------------------------------------------------
# GET /health —— 无需认证（登录前先探端口）
# ---------------------------------------------------------------------------


def test_health_requires_no_auth(admin_client):
    r = admin_client.get("/admin/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["gateway_port"], int)
    # 时间/版本由后端负责格式化口径：version 必须是字符串（前端直出不二次加工）
    assert isinstance(body["version"], str)


def test_health_reports_uptime_numeric(admin_client):
    """uptime_s 必须是数值：前端做「运行时长」展示，None 会渲染成空。"""
    body = admin_client.get("/admin/api/health").json()
    assert isinstance(body["uptime_s"], (int, float))


def test_health_reflects_default_model_env(monkeypatch):
    """default_model 直接透传 GATEWAY_DEFAULT_MODEL（与网关真值同源）。"""
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("GATEWAY_DEFAULT_MODEL", "qwen3.8")
    with TestClient(create_app(admin=True)) as c:
        assert c.get("/admin/api/health").json()["default_model"] == "qwen3.8"


# ---------------------------------------------------------------------------
# POST /login —— 请求体形态容忍度
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["key", "api_key"])
def test_login_accepts_both_field_names(admin_client, field):
    assert admin_client.post("/admin/api/login", json={field: KEY}).status_code == 200


def test_login_rejects_wrong_key(admin_client):
    r = admin_client.post("/admin/api/login", json={"key": "wrong"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "auth"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"key": ""},
        {"key": None},
        {"other": "x"},
        [],           # JSON 数组（非 dict）
        "plain",      # JSON 字符串
        None,         # body 为空
    ],
)
def test_login_never_500_on_unexpected_body(admin_client, body):
    """body 形态千奇百怪（含非法 JSON）时一律 401，绝不得冒泡成 500。

    /login 是无鉴权公开端点：任何未捕获异常都是可被匿名触发的拒绝服务面。
    """
    r = admin_client.post("/admin/api/login", json=body)
    assert r.status_code == 401, f"body={body!r} -> {r.status_code}"


def test_login_rejects_malformed_json_body(admin_client):
    """Content-Type: application/json + 非法 JSON：解析异常按空 dict 处理 → 401。"""
    r = admin_client.post(
        "/admin/api/login",
        content=b"{not-json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 401


def test_login_fails_closed_when_api_key_unset(monkeypatch):
    """API_KEY 未配置时 login 恒 401（即使提交空 key）——不存在「默认放行」。"""
    monkeypatch.delenv("API_KEY", raising=False)
    with TestClient(create_app(admin=True)) as c:
        assert c.post("/admin/api/login", json={"key": ""}).status_code == 401


# ---------------------------------------------------------------------------
# GET /probe —— 五区块契约
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_caps(monkeypatch):
    """把 capabilities.probe 钉成固定快照，并让 docker 判定与本机无关。"""
    caps = Capabilities(
        gpu_count=2,
        gpu_indices=[0, 1],
        vram_total_mb_per_gpu=[24576, 24576],
        gpu_name="RTX 5880 Ada",
        vram_total_mb=49152,
        vram_free_mb=[20000, 21000],
        cuda_driver="550.54.14",
        compute_capability="8.9",
        binaries={"vllm": True, "sglang": False},
        binary_paths={"vllm": "/venvs/vllm/bin/vllm", "sglang": None},
    )
    monkeypatch.setattr("modelctl.core.capabilities.probe", lambda: caps)
    monkeypatch.setattr("modelctl.core.capabilities.docker_ready", lambda: False)
    monkeypatch.setattr("modelctl.core.gpu_lock.list_gpu_locks", lambda: {1: "qwen3.8"})
    return caps


def test_probe_returns_all_five_blocks(admin_client, fake_caps):
    r = admin_client.get("/admin/api/probe", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    # 区块 1 GPU / 2 锁 / 3 引擎 / 4 环境变量 / 5 路径与版本
    for block in ("gpu_count", "gpu_locks", "engine_binaries", "env_vars", "paths", "version"):
        assert block in body, f"probe 响应缺少区块 {block}"
    assert body["gpu_count"] == 2
    assert body["gpu_name"] == "RTX 5880 Ada"
    assert body["cuda_driver"] == "550.54.14"
    assert body["compute_capability"] == "8.9"
    assert body["vram_free_mb"] == [20000, 21000]


def test_probe_totals_vram_from_per_gpu_list(admin_client, fake_caps):
    """总显存优先按 per-GPU 求和（多卡），而非 caps.vram_total_mb 单值。"""
    body = admin_client.get("/admin/api/probe", headers=_auth()).json()
    assert body["vram_total_mb"] == 49152
    assert body["vram_total_gb"] == 48.0


def test_probe_falls_back_to_single_vram_total(admin_client, monkeypatch):
    """无 per-GPU 明细时回退 vram_total_mb（单卡/老驱动路径）。"""
    caps = Capabilities(gpu_count=1, vram_total_mb=24576, gpu_name="A10")
    monkeypatch.setattr("modelctl.core.capabilities.probe", lambda: caps)
    monkeypatch.setattr("modelctl.core.gpu_lock.list_gpu_locks", lambda: {})
    body = admin_client.get("/admin/api/probe", headers=_auth()).json()
    assert body["vram_total_mb"] == 24576
    assert body["vram_total_gb"] == 24.0


def test_probe_wraps_scalar_vram_free_as_list(admin_client, monkeypatch):
    """vram_free_mb 是标量时须包成单元素 list：前端 .map 渲染，标量会抛。"""
    caps = Capabilities(gpu_count=1, vram_total_mb=24576, vram_free_mb=12345)
    monkeypatch.setattr("modelctl.core.capabilities.probe", lambda: caps)
    monkeypatch.setattr("modelctl.core.gpu_lock.list_gpu_locks", lambda: {})
    body = admin_client.get("/admin/api/probe", headers=_auth()).json()
    assert body["vram_free_mb"] == [12345]


def test_probe_masks_api_key(monkeypatch):
    """区块 4 的 API_KEY 只回显末 4 位——体检页是可截屏的管理面。"""
    secret = "sk-super-secret-value-7788"
    monkeypatch.setenv("API_KEY", secret)
    with TestClient(create_app(admin=True)) as c:
        r = c.get("/admin/api/probe", headers={"Authorization": f"Bearer {secret}"})
        assert r.status_code == 200
        masked = r.json()["env_vars"]["API_KEY"]
    assert masked == "***7788"
    assert "secret" not in masked
    assert secret not in masked


def test_probe_masks_api_key_short_key_fully_masked(monkeypatch):
    """短于 4 位的 API_KEY 必须整体掩掉（BUG-PRB-02 回归钉）。

    旧内联实现 `"****" + key[-4:]` 在 len(key) < 4 时 `key[-4:] == 全文明`，
    输出 `****abc` 等于把密钥原样贴到可截屏的体检页。现统一走 admin_auth.mask_key
    （len<=4 → 仅 "***"），与 admin_config._mask_value 口径一致。
    """
    secret = "abc"
    monkeypatch.setenv("API_KEY", secret)
    with TestClient(create_app(admin=True)) as c:
        body = c.get("/admin/api/probe", headers={"Authorization": f"Bearer {secret}"}).json()
    masked = body["env_vars"]["API_KEY"]
    assert masked == "***"
    assert secret not in masked


def test_probe_lists_engine_binaries_with_reachability(admin_client, fake_caps):
    out = admin_client.get("/admin/api/probe", headers=_auth()).json()["engine_binaries"]
    assert {e["name"] for e in out} == {"vllm", "sglang"}
    by_name = {e["name"]: e for e in out}
    assert by_name["vllm"]["runtime"] == "venv"
    assert by_name["sglang"]["reachable"] is False


def test_probe_gpu_locks_sorted(admin_client, fake_caps, monkeypatch):
    monkeypatch.setattr("modelctl.core.gpu_lock.list_gpu_locks", lambda: {2: "b", 0: "a"})
    out = admin_client.get("/admin/api/probe", headers=_auth()).json()["gpu_locks"]
    assert [e["gpu_index"] for e in out] == [0, 2]


def test_probe_requires_auth(admin_client):
    assert admin_client.get("/admin/api/probe").status_code == 401


def test_probe_paths_point_at_isolated_dirs(admin_client, fake_caps):
    """paths.cache_dir 落在 conftest 隔离的 tmp 目录，测试不得触碰仓库 data/。"""
    paths = admin_client.get("/admin/api/probe", headers=_auth()).json()["paths"]
    assert paths["models_dir"].endswith("models")
    assert "cache" in paths["cache_dir"].replace("\\", "/")


# ---------------------------------------------------------------------------
# GET /overview —— 3s 轮询聚合端点
# ---------------------------------------------------------------------------


@pytest.fixture()
def overview_env(monkeypatch, tmp_path):
    """把 overview 的 wave A/B 依赖全部打桩，断言只剩聚合逻辑本身。"""
    caps = Capabilities(
        gpu_count=2,
        vram_total_mb_per_gpu=[24576, 24576],
        gpu_name="RTX 5880 Ada",
        binaries={"vllm": True, "llamacpp": False},
        binary_paths={"vllm": "/p/vllm", "llamacpp": None},
    )
    monkeypatch.setattr("modelctl.core.capabilities.probe", lambda: caps)
    monkeypatch.setattr("modelctl.core.capabilities.docker_ready", lambda: False)
    monkeypatch.setattr("modelctl.core.process.is_running", lambda name: name == "gateway")

    class _P:
        name = "qwen3.8"

    monkeypatch.setattr("modelctl.core.profile.list_profiles", lambda _d=None: [_P()])

    calls: dict = {}

    async def _fake_build_summaries(request, profiles, executor=None):
        calls["executor"] = executor
        return [{"name": p.name, "state": "running"} for p in profiles]

    monkeypatch.setattr("modelctl.core.webui.admin_models.build_summaries", _fake_build_summaries)
    return calls


def test_overview_aggregates_three_layers(admin_client, overview_env):
    r = admin_client.get("/admin/api/overview", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["model_count"] == 1
    assert body["models"] == [{"name": "qwen3.8", "state": "running"}]
    assert body["hardware"]["gpu_count"] == 2
    assert body["hardware"]["total_vram_gb"] == 48.0
    # services 是「状态 + 端口」二元组，前端据此渲染服务卡片
    assert body["services"]["gateway"]["state"] == "running"
    assert body["services"]["stats"]["state"] == "stopped"
    assert isinstance(body["services"]["gateway"]["port"], int)
    assert isinstance(body["services"]["stats"]["port"], int)


def test_overview_uses_shared_probe_executor(admin_client, overview_env):
    """wave B 必须走 64-worker 共享池：退回默认池会让 51 个探测排 4 波（历史 P1）。"""
    admin_client.get("/admin/api/overview", headers=_auth())
    executor = overview_env.get("executor")
    assert executor is not None
    assert getattr(executor, "_max_workers", None) == 64


def _hb_engine_map(hb: dict) -> dict:
    """overview.hardware.engine_binaries 列表 → {name: entry}。"""
    return {e["name"]: e for e in hb["engine_binaries"]}


def test_overview_engine_binaries_docker_or_venv(admin_client, overview_env):
    """llamacpp 无 venv 且 docker 未就绪 → runtime None；vllm 有路径 → runtime venv。"""
    hb = admin_client.get("/admin/api/overview", headers=_auth()).json()["hardware"]
    by_name = _hb_engine_map(hb)
    assert by_name["vllm"]["available"] is True
    assert by_name["vllm"]["runtime"] == "venv"
    assert by_name["llamacpp"]["reachable"] is False
    assert by_name["llamacpp"]["runtime"] is None
    assert hb["docker_ready"] is False


def test_overview_engine_binaries_reachable_via_docker(admin_client, overview_env, monkeypatch):
    """docker 就绪时，非 docker 引擎（llamacpp）仍不可达（runtime None）。

    vllm 在此 fixture 里 available=True（venv 已装），故 runtime 仍是 venv
    而非 docker——docker 只在 venv 缺位时才作为来源标注。
    """
    monkeypatch.setattr("modelctl.core.capabilities.docker_ready", lambda: True)
    hb = admin_client.get("/admin/api/overview", headers=_auth()).json()["hardware"]
    by_name = _hb_engine_map(hb)
    assert hb["docker_ready"] is True
    assert by_name["llamacpp"]["reachable"] is False
    assert by_name["llamacpp"]["runtime"] is None


def test_overview_probed_at_carries_timezone(admin_client, overview_env):
    """probed_at 必须带时区偏移：naive ISO 会被浏览器按本地时区二次解释（跨时区即偏）。"""
    ts = admin_client.get("/admin/api/overview", headers=_auth()).json()["probed_at"]
    parsed = datetime.fromisoformat(ts)  # 无偏移时 fromisoformat 给 naive，须显式断言 aware
    assert parsed.tzinfo is not None, f"probed_at 缺时区偏移: {ts}"
    assert parsed.utcoffset() is not None


def test_overview_totals_vram_from_per_gpu(admin_client, overview_env):
    assert admin_client.get("/admin/api/overview", headers=_auth()).json()["hardware"][
        "total_vram_gb"
    ] == 48.0


def test_overview_falls_back_to_single_vram_total(admin_client, overview_env, monkeypatch):
    caps = Capabilities(gpu_count=1, vram_total_mb=24576, gpu_name="A10", binary_paths={})
    monkeypatch.setattr("modelctl.core.capabilities.probe", lambda: caps)
    hb = admin_client.get("/admin/api/overview", headers=_auth()).json()["hardware"]
    assert hb["total_vram_gb"] == 24.0
    # engine_binaries 已是列表形态；caps.binaries 为空 → 空列表
    assert hb["engine_binaries"] == []


def test_overview_requires_auth(admin_client):
    assert admin_client.get("/admin/api/overview").status_code == 401


def test_overview_empty_registry_is_not_an_error(admin_client, overview_env, monkeypatch):
    """没有任何 profile 时仍须 200 且 model_count=0（首次部署空态）。"""
    monkeypatch.setattr("modelctl.core.profile.list_profiles", lambda _d=None: [])
    body = admin_client.get("/admin/api/overview", headers=_auth()).json()
    assert body["model_count"] == 0
    assert body["models"] == []
