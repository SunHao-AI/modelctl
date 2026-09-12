"""管理面模型生命周期端点的投递契约（start / stop / restart + yaml_override）。

这批实现体在 2026-09-12 覆盖率热区里是**全库缺失行数最多**的一片：`start_model`
L446-493、`restart_model` L587-624、`_build_profile_from_override` L504-546 整段未
执行。它们属于"改坏即故障"：202 是否真的投递了 worker、互斥锁是否移交 worker、
override 的 engine/port 越权是否被挡——回归后 WebUI 点启动会静默无任务，或临时
profile 逃出适配器路径。

测试口径：**只测投递契约，不真起进程**——`TaskManager.spawn` 打桩为记录器，
`_probe_caps` / `default_start_timeout` / `stop_profile` / `capabilities.probe` 全打桩。
"""
from __future__ import annotations

import asyncio
import pathlib
import tempfile

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402

KEY = "test_key_lifecycle"


@pytest.fixture()
def admin_client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    logger.remove()
    app = create_app(admin=True)
    # 硬件探测每台机器结果不同（有无 GPU / docker），钉成固定值使断言与环境无关
    monkeypatch.setattr("modelctl.core.capabilities.probe", lambda *a, **k: object())
    with TestClient(app) as c:
        yield c


def _patch_profiles(monkeypatch, *profiles):
    monkeypatch.setattr("modelctl.core.profile.list_profiles", lambda d=None: list(profiles))


def _profile(name="q", engine="vllm", port=8000, path=None):
    from modelctl.core.profile import Profile

    p = Profile(name=name, engine=engine, port=port, engine_config={"model": "/m/x"})
    p.path = path
    return p


def _yaml_file(text: str) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp())
    p = d / "q.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def _capture_spawn(monkeypatch):
    """把 TaskManager.spawn 换成记录器：**不执行** runner，避免触碰真实 start_profile。"""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "modelctl.core.webui.admin_tasks.TaskManager.spawn",
        lambda tm, target, action, coro_factory: calls.append((target, action)),
    )
    return calls


def _stub_start_path(monkeypatch, *, timeout=600):
    """start/restart 投递路径上与环境相关的两个函数钉死。"""
    monkeypatch.setattr("modelctl.core.webui.admin_models._probe_caps", lambda: object())
    monkeypatch.setattr("modelctl.core.all_service.default_start_timeout", lambda p, c: timeout)
    return _capture_spawn(monkeypatch)


def _post(client, path, **params):
    return client.post(path, params=params or None, headers={"Authorization": f"Bearer {KEY}"})


def _lock_now(tm, target):
    """跨事件循环把 target 的锁置为已占用（模拟"已有进行中的任务"）。

    asyncio.Lock 在 3.10+ 不在构造时绑环，独立小环里 acquire 后 `.locked()` 仍为
    True，足够让端点的 `if lock.locked(): return None` 走到 409 分支。
    """
    async def go():
        return await tm.acquire(target, "start")

    return asyncio.run(go())


# ---------------------------------------------------------------------------
# POST /{name}/start —— 404 / 202 投递 / 锁边界 / 409 / 422
# ---------------------------------------------------------------------------


def test_start_404_when_profile_absent(admin_client, monkeypatch):
    _patch_profiles(monkeypatch)  # 一个 profile 都没有
    r = _post(admin_client, "/admin/api/models/ghost/start")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_start_202_delivers_worker_and_keeps_lock_held(admin_client, monkeypatch):
    """202 必须带 task_id + stream_url，worker 已投递，且锁仍归 worker 持有。

    锁的释放边界是 worker 结束（见 `TaskManager.spawn` 注释）。若在 handler 的
    finally 里提前 release，同一模型能被并发重复投递 → 二次拉起进程。
    """
    calls = _stub_start_path(monkeypatch)
    _patch_profiles(monkeypatch, _profile("q"))

    r = _post(admin_client, "/admin/api/models/q/start")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["task_id"].startswith("task-")
    assert body["stream_url"] == f"/admin/api/tasks/{body['task_id']}/stream"
    assert calls == [("q", "start")]

    tm = admin_client.app.state.task_manager
    task = tm.get_task(body["task_id"])
    assert task.kind == "model_start" and task.action == "start" and task.target == "q"
    assert task.status == "queued"
    assert tm.get_lock("q").locked(), "投递后锁必须仍被 worker 持有"


def test_start_409_when_same_target_locked(admin_client, monkeypatch):
    calls = _stub_start_path(monkeypatch)
    _patch_profiles(monkeypatch, _profile("q"))
    tm = admin_client.app.state.task_manager
    assert _lock_now(tm, "q") is not None

    r = _post(admin_client, "/admin/api/models/q/start")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "task_conflict"
    assert calls == [], "409 分支不得投递 worker"


def test_start_422_when_timeout_out_of_range(admin_client, monkeypatch):
    """timeout 的 ge=1/le=7200 只走参数校验，不进业务分支（与 all/start 同口径）。"""
    calls = _stub_start_path(monkeypatch)
    _patch_profiles(monkeypatch, _profile("q"))
    assert _post(admin_client, "/admin/api/models/q/start", timeout=9999).status_code == 422
    assert _post(admin_client, "/admin/api/models/q/start", timeout=0).status_code == 422
    assert calls == []


def test_start_explicit_timeout_skips_default_probe(admin_client, monkeypatch):
    """显式 timeout 时不得再算自适应超时（省一次 probe 往返）。"""
    calls = _stub_start_path(monkeypatch)
    called: list = []
    monkeypatch.setattr("modelctl.core.all_service.default_start_timeout",
                        lambda p, c: called.append(1) or 600)
    _patch_profiles(monkeypatch, _profile("q"))

    assert _post(admin_client, "/admin/api/models/q/start", timeout=120).status_code == 202
    assert called == [], "显式 timeout 应短路自适应计算"
    assert calls == [("q", "start")]


# ---------------------------------------------------------------------------
# yaml_override —— 临时 profile 的越权守卫（源文件不改）
# ---------------------------------------------------------------------------

_OK_YAML = """
name: q
engine: vllm
port: 8000
vllm:
  model: /m/other
"""


def _override(admin_client, monkeypatch, yaml_text, base):
    calls = _stub_start_path(monkeypatch)
    _patch_profiles(monkeypatch, base)
    r = admin_client.post("/admin/api/models/q/start",
                          params={"yaml_override": yaml_text},
                          headers={"Authorization": f"Bearer {KEY}"})
    return r, calls


def test_override_bad_yaml_400(admin_client, monkeypatch):
    base = _profile("q", path=_yaml_file(text=_OK_YAML))
    r, calls = _override(admin_client, monkeypatch, "name: [unclosed", base)
    assert r.status_code == 400
    assert "YAML 校验失败" in r.json()["error"]["message"]
    assert calls == []


def test_override_engine_change_rejected(admin_client, monkeypatch):
    """override 改 engine → 400：否则临时 profile 逃到别的适配器路径。"""
    base = _profile("q", engine="vllm", path=_yaml_file(text=_OK_YAML))
    r, calls = _override(admin_client, monkeypatch, _OK_YAML.replace("vllm", "sglang"), base)
    assert r.status_code == 400
    assert "engine 不可改" in r.json()["error"]["message"]
    assert calls == []


def test_override_port_change_rejected(admin_client, monkeypatch):
    """override 改 port → 400：同 profile 多端口会造成网关路由歧义。"""
    base = _profile("q", engine="vllm", port=8000, path=_yaml_file(text=_OK_YAML))
    r, calls = _override(admin_client, monkeypatch, _OK_YAML.replace("port: 8000", "port: 9001"), base)
    assert r.status_code == 400
    assert "port 不可改" in r.json()["error"]["message"]
    assert calls == []


def test_override_source_path_missing_400(admin_client, monkeypatch):
    base = _profile("q", path=None)
    r, calls = _override(admin_client, monkeypatch, _OK_YAML, base)
    assert r.status_code == 400
    assert "源文件未定位" in r.json()["error"]["message"]
    assert calls == []


def test_override_valid_202_with_name_anchored(admin_client, monkeypatch):
    """合法 override → 202；name 锚回路由 name，path 保留源路径，其余字段生效。

    name 若被 override 改写，PID 文件 / gpu lock / 日志 / 网关路由会全部分叉。
    """
    calls = _stub_start_path(monkeypatch)
    base = _profile("q", engine="vllm", path=_yaml_file(text=_OK_YAML))
    _patch_profiles(monkeypatch, base)

    import modelctl.core.webui.admin_models as am

    real_build = am._build_profile_from_override
    seen: dict = {}

    def spy(name, bp, text):
        out = real_build(name, bp, text)
        if not hasattr(out, "status_code"):
            seen.update(name=out.name, path_kept=out.path == bp.path,
                        model=out.engine_config.get("model"))
        return out

    monkeypatch.setattr(am, "_build_profile_from_override", spy)

    r = admin_client.post("/admin/api/models/q/start",
                          params={"yaml_override": _OK_YAML.replace("name: q", "name: hijacked")},
                          headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 202, r.text
    assert calls == [("q", "start")]
    assert seen["name"] == "q", "override 内的 name 必须被锚定为路由 name"
    assert seen["path_kept"] is True
    assert seen["model"] == "/m/other", "override 的其余字段须生效"


def test_override_source_yaml_file_untouched(admin_client, monkeypatch):
    """override 是内存临时态：源 yaml 文件字节必须不变。"""
    src = _yaml_file(text=_OK_YAML)
    before = src.read_bytes()
    base = _profile("q", engine="vllm", path=src)
    _patch_profiles(monkeypatch, base)
    calls = _stub_start_path(monkeypatch)

    admin_client.post("/admin/api/models/q/start",
                      params={"yaml_override": _OK_YAML.replace("/m/other", "/m/changed")},
                      headers={"Authorization": f"Bearer {KEY}"})
    assert calls == [("q", "start")]
    assert src.read_bytes() == before, "个性化启动不得写回源 yaml"


def test_override_blank_falls_back_to_default_start(admin_client, monkeypatch):
    """空白 override 等价默认启动（`strip()` 判空），不落 override 分支。"""
    calls = _stub_start_path(monkeypatch)
    _patch_profiles(monkeypatch, _profile("q", path=None))  # path=None：若误入 override 分支会 400
    r = admin_client.post("/admin/api/models/q/start",
                          params={"yaml_override": "   "},
                          headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 202, r.text
    assert calls == [("q", "start")]


# ---------------------------------------------------------------------------
# POST /{name}/stop
# ---------------------------------------------------------------------------


def test_stop_404_when_profile_absent(admin_client, monkeypatch):
    _patch_profiles(monkeypatch)
    r = _post(admin_client, "/admin/api/models/ghost/stop")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_stop_invalidates_route_cache_even_when_action_failed(admin_client, monkeypatch):
    """stop 后必须同步失效路由缓存，**失败也要**：否则界面在点停止后仍显示 running。"""
    invalidated: list[str] = []
    _patch_profiles(monkeypatch, _profile("q"))

    class _Res:
        status = "error"
        detail = "引擎未运行"

    monkeypatch.setattr("modelctl.core.all_service.stop_profile", lambda p, c, g: _Res())

    class _Cache:
        def invalidate_model(self, name):
            invalidated.append(name)

    admin_client.app.state.group_route_cache = _Cache()

    r = _post(admin_client, "/admin/api/models/q/stop")
    assert r.status_code == 200
    assert r.json() == {"ok": False, "detail": "引擎未运行"}
    assert invalidated == ["q"]


def test_stop_ok_true_on_success(admin_client, monkeypatch):
    invalidated: list[str] = []
    _patch_profiles(monkeypatch, _profile("q"))

    class _Res:
        status = "ok"
        detail = "已停止"

    monkeypatch.setattr("modelctl.core.all_service.stop_profile", lambda p, c, g: _Res())

    class _Cache:
        def invalidate_model(self, name):
            invalidated.append(name)

    admin_client.app.state.group_route_cache = _Cache()
    r = _post(admin_client, "/admin/api/models/q/stop")
    assert r.json() == {"ok": True, "detail": "已停止"}
    assert invalidated == ["q"]


# ---------------------------------------------------------------------------
# POST /{name}/restart
# ---------------------------------------------------------------------------


def test_restart_404_when_profile_absent(admin_client, monkeypatch):
    _patch_profiles(monkeypatch)
    assert _post(admin_client, "/admin/api/models/ghost/restart").status_code == 404


def test_restart_202_delivers_restart_action_not_start(admin_client, monkeypatch):
    """restart 必须以 action="restart" 投递：复用 "start" 会让重启与启动语义混淆。"""
    calls = _stub_start_path(monkeypatch)
    _patch_profiles(monkeypatch, _profile("q"))

    r = _post(admin_client, "/admin/api/models/q/restart", timeout=120)
    assert r.status_code == 202, r.text
    assert calls == [("q", "restart")]
    task = admin_client.app.state.task_manager.get_task(r.json()["task_id"])
    assert (task.kind, task.action) == ("model_restart", "restart")


def test_restart_409_when_restart_already_running(admin_client, monkeypatch):
    calls = _stub_start_path(monkeypatch)
    _patch_profiles(monkeypatch, _profile("q"))
    tm = admin_client.app.state.task_manager
    assert _lock_now(tm, "q") is not None

    r = _post(admin_client, "/admin/api/models/q/restart")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "task_conflict"
    assert calls == []


# ---------------------------------------------------------------------------
# 鉴权红线：投递类端点也不得免鉴权
# ---------------------------------------------------------------------------


def test_lifecycle_endpoints_require_admin_auth(admin_client, monkeypatch):
    _patch_profiles(monkeypatch, _profile("q"))
    for p in ("/admin/api/models/q/start",
              "/admin/api/models/q/stop",
              "/admin/api/models/q/restart"):
        assert admin_client.post(p).status_code == 401, p


def test_lifecycle_endpoints_reject_gateway_data_plane_key(admin_client, monkeypatch):
    """数据面 key（GATEWAY_CLIENT_API_KEY）不得打通管理面启停——三密钥域隔离。"""
    monkeypatch.setenv("GATEWAY_CLIENT_API_KEY", "data-plane-key-0123456789")
    _patch_profiles(monkeypatch, _profile("q"))
    for p in ("/admin/api/models/q/start",
              "/admin/api/models/q/stop",
              "/admin/api/models/q/restart"):
        r = admin_client.post(p, headers={"Authorization": "Bearer data-plane-key-0123456789"})
        assert r.status_code == 401, p
